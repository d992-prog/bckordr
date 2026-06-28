from __future__ import annotations

import asyncio
import logging

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.base import utcnow
from app.services.app_settings import get_diagnostic_telegram_settings
from app.services.attack_runtime import (
    autoplan_due_attack_runs,
    rebalance_worker_pool,
    refresh_active_task_targets,
    recompute_run_statistics,
    recompute_worker_domain_counts,
    supervise_worker_pool,
)
from app.services.discovery import process_due_discovery_domains
from app.services.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class ControlRuntimeOrchestrator:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        interval_seconds: float = 1.0,
        worker_supervisor_interval_seconds: float = 15.0,
        worker_stall_threshold_seconds: int = 45,
        settings: Settings | None = None,
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
        self._discovery_timeout_seconds = max(settings.discovery_timeout_seconds, 0.25) if settings else 5.0
        self._discovery_rdap_bootstrap_url = (
            settings.discovery_rdap_bootstrap_url if settings else "https://data.iana.org/rdap/dns.json"
        )
        self._notifier = TelegramNotifier(settings) if settings else None
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_worker_supervision_at = None
        self._last_discovery_at = None

    async def bootstrap(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop(), name="control-runtime-orchestrator")

    async def shutdown(self) -> None:
        self._stop_event.set()
        task = self._task
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def ensure_domain(self, domain_id: int) -> None:
        del domain_id
        await self.run_cycle()

    async def stop_domain(self, domain_id: int) -> bool:
        del domain_id
        return False

    def worker_count(self) -> int:
        return 1 if self._task is not None and not self._task.done() else 0

    async def run_cycle(self) -> None:
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
            await autoplan_due_attack_runs(session, now=now)
            await refresh_active_task_targets(session, now=now)
            await rebalance_worker_pool(session, now=now)
            await recompute_worker_domain_counts(session)
            await recompute_run_statistics(session)
            if (
                self._discovery_enabled
                and (
                    self._last_discovery_at is None
                    or (now - self._last_discovery_at).total_seconds() >= self._discovery_scheduler_interval_seconds
                )
            ):
                await process_due_discovery_domains(
                    session,
                    now=now,
                    batch_size=self._discovery_batch_size,
                    bootstrap_url=self._discovery_rdap_bootstrap_url,
                    timeout_seconds=self._discovery_timeout_seconds,
                    notify=lambda message: self._send_discovery_notification(session, message),
                )
                self._last_discovery_at = now
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
