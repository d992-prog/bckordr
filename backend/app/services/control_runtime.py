from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import math
from pathlib import Path
import re

from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import AppSetting, VpnEndpoint, WorkerNode, ZoneScanJob
from app.db.session import VpnControlDatabase, create_vpn_control_database
from app.services.app_settings import (
    _VPN_FRIEND_BETA_RELEASE_READY_KEY,
    _VPN_PUBLIC_RELEASE_READY_KEY,
    get_diagnostic_telegram_settings,
    get_discovery_runtime_settings,
)
from app.services.attack_runtime import (
    autoplan_due_attack_runs,
    finalize_expired_attack_runs,
    rebalance_worker_pool,
    refresh_active_task_targets,
    recompute_run_statistics,
    recompute_worker_domain_counts,
    supervise_worker_pool,
)
from app.services.discovery import process_due_discovery_domains
from app.services.discovery_worker_runtime import (
    enqueue_due_discovery_worker_tasks,
    expire_stale_discovery_worker_tasks,
    load_eligible_discovery_workers,
)
from app.services.notifier import TelegramNotifier
from app.services.vpn_control_dispatcher import dispatch_next_vpn_control_operation
from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
from app.services.vpn_node_transport import VpnNodeTransportError, load_transport_snapshot
from app.services.vpn_portal_auth import cleanup_expired_portal_auth
from app.services.vpn_ready_notifications import (
    ReadyNoticeSender,
    deliver_next_ready_notice,
)
from app.services.vpn_telegram import send_telegram_message
from app.services.zone_scanner import run_zone_scan_job

logger = logging.getLogger(__name__)
PORTAL_AUTH_CLEANUP_TIMEOUT_SECONDS = 5.0
_VPN_CONTROL_RELEASE_ID = re.compile(r"[0-9a-f]{64}")


class ControlRuntimeOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: float = 1.0,
        worker_supervisor_interval_seconds: float = 15.0,
        worker_stall_threshold_seconds: int = 45,
        settings: Settings | None = None,
        vpn_control_database_factory: Callable[
            [Settings], VpnControlDatabase
        ] = create_vpn_control_database,
        vpn_control_dispatcher: Callable[..., Awaitable[bool]] = (
            dispatch_next_vpn_control_operation
        ),
        vpn_control_snapshot_loader: Callable[[WorkerNode, Path], object] = (
            load_transport_snapshot
        ),
        vpn_ready_sender: ReadyNoticeSender = send_telegram_message,
    ) -> None:
        self._session_factory = session_factory
        self._interval_seconds = max(interval_seconds, 0.25)
        self._worker_supervisor_interval_seconds = max(worker_supervisor_interval_seconds, 0.25)
        self._worker_stall_threshold_seconds = max(int(worker_stall_threshold_seconds), 1)
        self._settings = settings
        self._discovery_enabled = settings.discovery_enabled if settings else True
        self._discovery_scheduler_interval_seconds = (
            max(settings.discovery_scheduler_interval_seconds, 0.25) if settings else 5.0
        )
        self._discovery_batch_size = max(settings.discovery_batch_size, 1) if settings else 10
        self._discovery_concurrency = max(settings.discovery_concurrency, 1) if settings else 5
        self._discovery_timeout_seconds = max(settings.discovery_timeout_seconds, 0.25) if settings else 5.0
        self._discovery_worker_enabled = settings.discovery_worker_enabled if settings else True
        self._discovery_worker_task_stale_seconds = (
            max(settings.discovery_worker_task_stale_seconds, 1) if settings else 180
        )
        self._discovery_local_fallback_enabled = settings.discovery_local_fallback_enabled if settings else True
        self._discovery_rdap_bootstrap_url = (
            settings.discovery_rdap_bootstrap_url if settings else "https://data.iana.org/rdap/dns.json"
        )
        self._vpn_lifecycle_enabled = settings.vpn_lifecycle_enabled if settings else True
        self._vpn_lifecycle_interval_seconds = max(settings.vpn_lifecycle_interval_seconds, 1.0) if settings else 60.0
        self._vpn_lifecycle_batch_size = max(settings.vpn_lifecycle_batch_size, 1) if settings else 50
        self._vpn_lifecycle_key_timeout_seconds = (
            max(settings.vpn_lifecycle_key_timeout_seconds, 0.01) if settings else 30.0
        )
        self._vpn_lifecycle_cycle_timeout_seconds = (
            max(settings.vpn_lifecycle_cycle_timeout_seconds, 0.1) if settings else 300.0
        )
        self._notifier = TelegramNotifier(settings) if settings else None
        self._vpn_control_database_factory = vpn_control_database_factory
        self._vpn_control_dispatcher = vpn_control_dispatcher
        self._vpn_control_snapshot_loader = vpn_control_snapshot_loader
        self._vpn_ready_notifications_enabled = (
            settings.vpn_ready_notifications_enabled if settings else False
        )
        self._vpn_ready_sender = vpn_ready_sender
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._zone_scan_tasks: dict[int, asyncio.Task[None]] = {}
        self._vpn_lifecycle_task: asyncio.Task[None] | None = None
        self._vpn_control_database: VpnControlDatabase | None = None
        self._vpn_control_dispatch_task: asyncio.Task[None] | None = None
        self._vpn_ready_notification_task: asyncio.Task[None] | None = None
        self._last_worker_supervision_at = None
        self._last_discovery_at = None
        self._last_vpn_lifecycle_at = None
        self._last_vpn_control_dispatch_at = None

    async def bootstrap(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="control-runtime-orchestrator")

    async def shutdown(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._task = None
        lifecycle_task = self._vpn_lifecycle_task
        if lifecycle_task is not None and not lifecycle_task.done():
            lifecycle_task.cancel()
            try:
                await lifecycle_task
            except asyncio.CancelledError:
                pass
        self._vpn_lifecycle_task = None
        dispatch_task = self._vpn_control_dispatch_task
        if dispatch_task is not None and not dispatch_task.done():
            dispatch_task.cancel()
            try:
                await dispatch_task
            except asyncio.CancelledError:
                pass
        self._vpn_control_dispatch_task = None
        ready_notification_task = self._vpn_ready_notification_task
        if ready_notification_task is not None and not ready_notification_task.done():
            ready_notification_task.cancel()
            try:
                await ready_notification_task
            except asyncio.CancelledError:
                pass
        self._vpn_ready_notification_task = None
        database = self._vpn_control_database
        if database is not None:
            await database.engine.dispose()
        self._vpn_control_database = None

    async def ensure_domain(self, domain_id: int) -> None:
        del domain_id
        await self.run_cycle()

    async def stop_domain(self, domain_id: int) -> bool:
        del domain_id
        return False

    def worker_count(self) -> int:
        return 1 if self._task is not None and not self._task.done() else 0

    async def run_cycle(self) -> None:
        start_vpn_lifecycle = False
        start_vpn_control_dispatch = False
        lifecycle_now = None
        async with self._session_factory() as session:
            now = utcnow()
            if (
                self._last_worker_supervision_at is None
                or (now - self._last_worker_supervision_at).total_seconds() >= self._worker_supervisor_interval_seconds
            ):
                await supervise_worker_pool(
                    session,
                    now=now,
                    stall_threshold_seconds=self._worker_stall_threshold_seconds,
                )
                self._last_worker_supervision_at = now
            await finalize_expired_attack_runs(
                session,
                now=now,
                bootstrap_url=self._discovery_rdap_bootstrap_url,
            )
            await autoplan_due_attack_runs(session, now=now)
            await refresh_active_task_targets(session, now=now)
            await rebalance_worker_pool(session, now=now)
            await recompute_worker_domain_counts(session)
            await recompute_run_statistics(session)
            discovery_runtime_settings = (
                await get_discovery_runtime_settings(session, self._settings)
                if self._settings is not None
                else None
            )
            discovery_enabled = (
                discovery_runtime_settings.discovery_enabled
                if discovery_runtime_settings is not None
                else self._discovery_enabled
            )
            discovery_scheduler_interval_seconds = (
                discovery_runtime_settings.discovery_scheduler_interval_seconds
                if discovery_runtime_settings is not None
                else self._discovery_scheduler_interval_seconds
            )
            if (
                discovery_enabled
                and (
                    self._last_discovery_at is None
                    or (now - self._last_discovery_at).total_seconds() >= discovery_scheduler_interval_seconds
                )
            ):
                discovery_workers = []
                discovery_worker_enabled = (
                    discovery_runtime_settings.discovery_worker_enabled
                    if discovery_runtime_settings is not None
                    else self._discovery_worker_enabled
                )
                discovery_worker_task_stale_seconds = (
                    discovery_runtime_settings.discovery_worker_task_stale_seconds
                    if discovery_runtime_settings is not None
                    else self._discovery_worker_task_stale_seconds
                )
                discovery_batch_size = (
                    discovery_runtime_settings.discovery_batch_size
                    if discovery_runtime_settings is not None
                    else self._discovery_batch_size
                )
                if discovery_worker_enabled:
                    await expire_stale_discovery_worker_tasks(
                        session,
                        now=now,
                        stale_after_seconds=discovery_worker_task_stale_seconds,
                    )
                    discovery_workers = await load_eligible_discovery_workers(session)
                    if discovery_workers:
                        await enqueue_due_discovery_worker_tasks(
                            session,
                            now=now,
                            batch_size=discovery_batch_size,
                        )
                discovery_local_fallback_enabled = (
                    discovery_runtime_settings.discovery_local_fallback_enabled
                    if discovery_runtime_settings is not None
                    else self._discovery_local_fallback_enabled
                )
                if not discovery_workers and discovery_local_fallback_enabled:
                    discovery_concurrency = (
                        discovery_runtime_settings.discovery_concurrency
                        if discovery_runtime_settings is not None
                        else self._discovery_concurrency
                    )
                    discovery_timeout_seconds = (
                        discovery_runtime_settings.discovery_timeout_seconds
                        if discovery_runtime_settings is not None
                        else self._discovery_timeout_seconds
                    )
                    await process_due_discovery_domains(
                        session,
                        now=now,
                        batch_size=discovery_batch_size,
                        concurrency=discovery_concurrency,
                        bootstrap_url=self._discovery_rdap_bootstrap_url,
                        timeout_seconds=discovery_timeout_seconds,
                        notify=lambda message: self._send_discovery_notification(session, message),
                    )
                self._last_discovery_at = now
            if (
                self._vpn_lifecycle_enabled
                and (self._vpn_lifecycle_task is None or self._vpn_lifecycle_task.done())
                and (
                    self._last_vpn_lifecycle_at is None
                    or (now - self._last_vpn_lifecycle_at).total_seconds() >= self._vpn_lifecycle_interval_seconds
                )
            ):
                self._last_vpn_lifecycle_at = now
                lifecycle_now = now
                start_vpn_lifecycle = True
            if self._vpn_control_dispatch_due(now):
                worker = await self._load_vpn_control_worker(session)
                if worker is not None:
                    try:
                        self._vpn_control_snapshot_loader(
                            worker,
                            Path(self._settings.vpn_control_known_hosts_path),
                        )
                    except VpnNodeTransportError:
                        pass
                    else:
                        start_vpn_control_dispatch = True
            await session.commit()
        if start_vpn_lifecycle:
            self._vpn_lifecycle_task = asyncio.create_task(
                self._run_vpn_lifecycle(lifecycle_now),
                name="vpn-lifecycle-maintenance",
            )
        if start_vpn_control_dispatch:
            self._start_vpn_control_dispatch(now)
        if self._vpn_ready_notifications_enabled and (
            self._vpn_ready_notification_task is None
            or self._vpn_ready_notification_task.done()
        ):
            self._vpn_ready_notification_task = asyncio.create_task(
                self._run_vpn_ready_notification(),
                name="vpn-ready-notification",
            )
        await self._start_zone_scan_jobs_if_needed()

    def _vpn_control_dispatch_due(self, now) -> bool:
        settings = self._settings
        if settings is None:
            return False
        command_timeout = settings.vpn_control_db_command_timeout_seconds
        statement_timeout = settings.vpn_control_db_statement_timeout_ms
        dispatch_interval = settings.vpn_control_dispatch_interval_seconds
        finalize_timeout = settings.vpn_control_finalize_timeout_seconds
        if (
            not settings.vpn_control_dispatch_enabled
            or settings.vpn_portal_public_access
            or not self._vpn_control_release_markers()
            or not settings.vpn_control_known_hosts_path.strip()
            or not math.isfinite(command_timeout)
            or command_timeout <= 0
            or statement_timeout <= 0
            or not math.isfinite(dispatch_interval)
            or dispatch_interval <= 0
            or not math.isfinite(finalize_timeout)
            or finalize_timeout <= 0
        ):
            return False
        task = self._vpn_control_dispatch_task
        if task is not None and not task.done():
            return False
        return (
            self._last_vpn_control_dispatch_at is None
            or (now - self._last_vpn_control_dispatch_at).total_seconds()
            >= settings.vpn_control_dispatch_interval_seconds
        )

    def _vpn_control_release_markers(self) -> tuple[tuple[str, str], ...]:
        assert self._settings is not None
        settings = self._settings
        candidates = (
            (
                settings.vpn_friend_beta_enabled,
                _VPN_FRIEND_BETA_RELEASE_READY_KEY,
                settings.vpn_friend_beta_release_id,
            ),
            (
                bool(settings.vpn_public_trial_release_id),
                _VPN_PUBLIC_RELEASE_READY_KEY,
                settings.vpn_public_trial_release_id,
            ),
        )
        return tuple(
            (key, release_id)
            for enabled, key, release_id in candidates
            if enabled and _VPN_CONTROL_RELEASE_ID.fullmatch(release_id) is not None
        )

    async def _load_vpn_control_worker(
        self,
        session: AsyncSession,
    ) -> WorkerNode | None:
        assert self._settings is not None
        markers = self._vpn_control_release_markers()
        if not markers:
            return None
        row = (
            await session.execute(
                select(VpnEndpoint, WorkerNode)
                .select_from(AppSetting)
                .join(VpnEndpoint, true())
                .join(WorkerNode, WorkerNode.id == VpnEndpoint.worker_id)
                .where(
                    or_(
                        *(
                            and_(AppSetting.key == key, AppSetting.value == release_id)
                            for key, release_id in markers
                        )
                    ),
                    VpnEndpoint.status == "ready",
                    VpnEndpoint.security == "reality",
                    VpnEndpoint.verified_at.is_not(None),
                    WorkerNode.archived_at.is_(None),
                )
                .order_by(VpnEndpoint.id.asc())
                .limit(1)
            )
        ).first()
        if row is None:
            return None
        return row[1]

    def _start_vpn_control_dispatch(self, now) -> None:
        settings = self._settings
        assert settings is not None
        if not self._vpn_control_dispatch_due(now):
            return
        if self._vpn_control_database is None:
            self._vpn_control_database = self._vpn_control_database_factory(settings)
        self._last_vpn_control_dispatch_at = now
        self._vpn_control_dispatch_task = asyncio.create_task(
            self._run_vpn_control_dispatch(),
            name="vpn-control-dispatch",
        )

    async def _run_vpn_control_dispatch(self) -> None:
        settings = self._settings
        database = self._vpn_control_database
        assert settings is not None and database is not None
        try:
            await self._vpn_control_dispatcher(
                database.session_factory,
                Path(settings.vpn_control_known_hosts_path),
                finalize_timeout=settings.vpn_control_finalize_timeout_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("VPN control dispatch failed")

    async def _run_vpn_ready_notification(self) -> None:
        assert self._settings is not None
        try:
            await deliver_next_ready_notice(
                self._session_factory,
                self._settings,
                self._vpn_ready_sender,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.error("VPN ready notification task failed")

    async def _run_vpn_lifecycle(self, now) -> None:
        async with self._session_factory() as session:
            try:
                await asyncio.wait_for(
                    run_vpn_lifecycle_maintenance(
                        session,
                        now=now,
                        batch_size=self._vpn_lifecycle_batch_size,
                        key_timeout_seconds=self._vpn_lifecycle_key_timeout_seconds,
                    ),
                    timeout=self._vpn_lifecycle_cycle_timeout_seconds,
                )
            except TimeoutError:
                await session.rollback()
                logger.error(
                    "VPN lifecycle exceeded %.1f seconds and was cancelled",
                    self._vpn_lifecycle_cycle_timeout_seconds,
                )
            except Exception:
                await session.rollback()
                logger.exception("VPN lifecycle background task failed")
        try:
            await asyncio.wait_for(
                self._cleanup_expired_portal_auth(now),
                timeout=PORTAL_AUTH_CLEANUP_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            logger.warning("VPN portal expired-auth cleanup timed out")
        except Exception:
            logger.warning("VPN portal expired-auth cleanup failed")

    async def _cleanup_expired_portal_auth(self, now) -> None:
        async with self._session_factory() as session:
            await cleanup_expired_portal_auth(session, now=now)
            await session.commit()

    async def _send_discovery_notification(self, session: AsyncSession, message: str) -> None:
        if self._notifier is None:
            return
        token, chat_id = await get_diagnostic_telegram_settings(session)
        if not token or not chat_id:
            return
        await self._notifier.send_diagnostic(
            "Drop discovery",
            message,
            token=token,
            chat_id=chat_id,
        )

    async def _start_zone_scan_jobs_if_needed(self) -> None:
        if self._settings is None:
            return
        self._zone_scan_tasks = {
            job_id: task for job_id, task in self._zone_scan_tasks.items() if not task.done()
        }
        max_jobs = max(int(self._settings.zone_scan_max_concurrent_jobs), 1)
        if len(self._zone_scan_tasks) >= max_jobs:
            return
        async with self._session_factory() as session:
            result = await session.execute(
                select(ZoneScanJob.id)
                .where(ZoneScanJob.status.in_(["queued", "downloading", "scanning"]))
                .order_by(ZoneScanJob.created_at.asc(), ZoneScanJob.id.asc())
                .limit(max_jobs - len(self._zone_scan_tasks))
            )
            job_ids = [int(item) for item in result.scalars().all()]
        for job_id in job_ids:
            if job_id in self._zone_scan_tasks:
                continue
            self._zone_scan_tasks[job_id] = asyncio.create_task(
                run_zone_scan_job(self._session_factory, job_id=job_id, settings=self._settings),
                name=f"zone-scan-job-{job_id}",
            )

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.run_cycle()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Control runtime cycle failed")

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_seconds)
            except asyncio.TimeoutError:
                continue
