from __future__ import annotations

import logging
from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.core.config import get_settings
from app.db.models import (
    AttackEvent,
    AttackRun,
    ContactProfile,
    DiscoveryDomain,
    DiscoveryWorkerTask,
    DropDomain,
    LiveCreateLease,
    RegistrarAccount,
    WorkerNode,
    WorkerTask,
    ZoneStrategy,
)
from app.db.session import get_db
from app.schemas.runtime import (
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    DiscoveryWorkerTaskPayloadResponse,
    DiscoveryWorkerTaskResponseEnvelope,
    DiscoveryWorkerTaskResultRequest,
    WorkerTaskAckRequest,
    WorkerTaskCreatePermitReleaseRequest,
    WorkerTaskCreatePermitRequest,
    WorkerTaskCreatePermitResponse,
    WorkerTaskPayloadResponse,
    WorkerTaskProgressRequest,
    WorkerTaskStatusResponse,
    WorkerTaskResponseEnvelope,
    WorkerTaskResultRequest,
    WorkerTaskResultResponse,
)
from app.services.attack_runtime import rebalance_worker_pool, recompute_run_statistics
from app.services.app_settings import get_diagnostic_telegram_settings, get_discovery_runtime_settings
from app.services.discovery_worker_runtime import apply_discovery_worker_task_result
from app.services.notifier import TelegramNotifier
from app.services.strategy_runtime import resolve_effective_gandi_parameters

router = APIRouter(prefix="/worker-runtime", tags=["worker-runtime"])
logger = logging.getLogger(__name__)


async def _get_worker_by_token(
    worker_id: int,
    db: AsyncSession,
    x_worker_token: str | None,
) -> WorkerNode:
    worker = await db.get(WorkerNode, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    if not worker.control_token or not x_worker_token or worker.control_token != x_worker_token:
        raise HTTPException(status_code=401, detail="Invalid worker token")
    return worker


async def _resolve_contact_payload(db: AsyncSession, domain: DropDomain, account: RegistrarAccount | None) -> ContactProfile | None:
    if domain.contact_profile_id:
        return await db.get(ContactProfile, domain.contact_profile_id)
    if account and account.default_contact_profile_id:
        return await db.get(ContactProfile, account.default_contact_profile_id)
    result = await db.execute(select(ContactProfile).where(ContactProfile.is_default.is_(True)).limit(1))
    return result.scalar_one_or_none()


async def _get_zone_strategy_for_domain(db: AsyncSession, domain: DropDomain) -> ZoneStrategy | None:
    if domain.zone_strategy_id:
        return await db.get(ZoneStrategy, domain.zone_strategy_id)
    result = await db.execute(
        select(ZoneStrategy)
        .where(ZoneStrategy.zone == domain.zone, ZoneStrategy.is_active.is_(True))
        .order_by(ZoneStrategy.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_worker_task_for_update(
    task_id: int,
    worker: WorkerNode,
    db: AsyncSession,
) -> tuple[WorkerTask, AttackRun, DropDomain]:
    task_result = await db.execute(select(WorkerTask).where(WorkerTask.id == task_id).with_for_update())
    task = task_result.scalar_one_or_none()
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Task not found")
    run_result = await db.execute(select(AttackRun).where(AttackRun.id == task.attack_run_id).with_for_update())
    run = run_result.scalar_one_or_none()
    domain = await db.get(DropDomain, task.domain_id)
    if run is None or domain is None:
        raise HTTPException(status_code=404, detail="Related run/domain not found")
    return task, run, domain


async def _clear_expired_live_create_leases(db: AsyncSession, run_id: int, now) -> None:
    await db.execute(
        delete(LiveCreateLease).where(
            LiveCreateLease.attack_run_id == run_id,
            LiveCreateLease.expires_at <= now,
        )
    )


async def _sync_legacy_live_create_lease_fields(db: AsyncSession, run: AttackRun, now) -> None:
    result = await db.execute(
        select(LiveCreateLease)
        .where(LiveCreateLease.attack_run_id == run.id, LiveCreateLease.expires_at > now)
        .order_by(LiveCreateLease.expires_at.desc(), LiveCreateLease.id.desc())
        .limit(1)
    )
    active_lease = result.scalar_one_or_none()
    if active_lease is None:
        run.live_create_lease_worker_id = None
        run.live_create_lease_task_id = None
        run.live_create_lease_expires_at = None
        return
    run.live_create_lease_worker_id = active_lease.worker_id
    run.live_create_lease_task_id = active_lease.worker_task_id
    run.live_create_lease_expires_at = active_lease.expires_at


@router.post("/heartbeat", response_model=WorkerHeartbeatResponse)
async def worker_heartbeat(
    payload: WorkerHeartbeatRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerHeartbeatResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    worker.status = payload.status
    worker.ip_address = payload.ip_address or worker.ip_address
    worker.region = payload.region or worker.region
    worker.current_rps = payload.current_rps
    worker.current_capacity_rps = payload.current_capacity_rps
    worker.cpu_load = payload.cpu_load
    worker.ram_usage_percent = payload.ram_usage_percent
    worker.clock_drift_ms = payload.clock_drift_ms
    worker.runtime_mode = payload.runtime_mode
    worker.registration_concurrency_multiplier = payload.registration_concurrency_multiplier
    worker.registration_max_concurrency = payload.registration_max_concurrency
    worker.last_seen_at = utcnow()
    worker.last_heartbeat_at = utcnow()
    await db.commit()
    return WorkerHeartbeatResponse(detail="heartbeat accepted", server_time=utcnow())


@router.get("/tasks/next", response_model=WorkerTaskResponseEnvelope)
async def get_next_task(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResponseEnvelope:
    worker = await _get_worker_by_token(worker_id, db, x_worker_token)
    result = await db.execute(
        select(WorkerTask)
        .where(WorkerTask.worker_id == worker.id, WorkerTask.status == "queued")
        .order_by(WorkerTask.created_at.asc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return WorkerTaskResponseEnvelope(task=None)

    run = await db.get(AttackRun, task.attack_run_id)
    domain = await db.get(DropDomain, task.domain_id)
    if run is None or domain is None:
        return WorkerTaskResponseEnvelope(task=None)
    if run.status == "success" or run.live_create_accepted_at is not None or domain.status == "success":
        now = utcnow()
        task.status = "cancelled"
        task.finished_at = now
        task.stop_reason = "Domain already has an accepted live create"
        await db.commit()
        return WorkerTaskResponseEnvelope(task=None)

    account = await db.get(RegistrarAccount, domain.registrar_account_id) if domain.registrar_account_id else None
    contact = await _resolve_contact_payload(db, domain, account)
    zone_strategy = await _get_zone_strategy_for_domain(db, domain)
    if contact is None:
        db.add(
            AttackEvent(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                level="error",
                event_type="worker_task_missing_contact",
                message=f"No contact profile resolved for {domain.fqdn}",
            )
        )
        await db.commit()
        return WorkerTaskResponseEnvelope(task=None)
    gandi_parameters = resolve_effective_gandi_parameters(domain, contact=contact, zone_strategy=zone_strategy)

    payload = WorkerTaskPayloadResponse(
        task_id=task.id,
        attack_run_id=run.id,
        domain_id=domain.id,
        worker_id=worker.id,
        fqdn=domain.fqdn,
        zone=domain.zone,
        planned_start_at=run.planned_start_at,
        planned_end_at=run.planned_end_at,
        planned_rps=task.planned_rps,
        requested_duration_years=domain.requested_duration_years,
        registration_extra_parameters=gandi_parameters.registration_extra_parameters,
        registrar={
            "id": account.id if account else None,
            "name": account.name if account else None,
            "registrar_slug": domain.registrar_slug,
            "api_token": account.api_token if account else None,
            "sharing_id": account.sharing_id if account else None,
            "api_base_url": worker.api_base_url or (account.api_base_url if account else None),
            "supports_dry_run": account.supports_dry_run if account else True,
        },
        contact={
            "id": contact.id,
            "label": contact.label,
            "person_type": contact.person_type,
            "given_name": contact.given_name,
            "family_name": contact.family_name,
            "organization_name": contact.organization_name,
            "email": contact.email,
            "phone": contact.phone,
            "mobile": contact.mobile,
            "fax": contact.fax,
            "lang": contact.lang,
            "street_address": contact.street_address,
            "city": contact.city,
            "state": contact.state,
            "zip_code": contact.zip_code,
            "country_code": contact.country_code,
            "data_obfuscated": contact.data_obfuscated,
            "mail_obfuscated": contact.mail_obfuscated,
            "icann_contract_accept": contact.icann_contract_accept,
            "extra_parameters": gandi_parameters.contact_extra_parameters,
        },
    )
    return WorkerTaskResponseEnvelope(task=payload)


@router.post("/tasks/{task_id}/ack", response_model=WorkerTaskResultResponse)
async def acknowledge_task(
    task_id: int,
    payload: WorkerTaskAckRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task = await db.get(WorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Task not found")
    now = utcnow()
    first_ack = task.status == "queued"
    if first_ack:
        task.acknowledged_at = task.acknowledged_at or now
        task.status = "running"
        task.started_at = task.started_at or now

    run = await db.get(AttackRun, task.attack_run_id)
    domain = await db.get(DropDomain, task.domain_id)
    if first_ack and run is not None and run.status == "planned":
        run.status = "running"
        run.started_at = run.started_at or now
    if first_ack and domain is not None and domain.status in {"scheduled", "queued", "ready"}:
        domain.status = "attacking"

    if first_ack:
        db.add(
            AttackEvent(
                attack_run_id=task.attack_run_id,
                domain_id=task.domain_id,
                worker_id=worker.id,
                level="info",
                event_type="task_ack",
                message=f"Worker {worker.name} acknowledged task #{task.id}",
            )
        )
    await db.commit()
    return WorkerTaskResultResponse(detail="task acknowledged")


@router.get("/tasks/{task_id}/status", response_model=WorkerTaskStatusResponse)
async def get_task_status(
    task_id: int,
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskStatusResponse:
    worker = await _get_worker_by_token(worker_id, db, x_worker_token)
    task = await db.get(WorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Task not found")
    return WorkerTaskStatusResponse(
        task_id=task.id,
        status=task.status,
        stop_reason=task.stop_reason,
        planned_rps=task.planned_rps,
    )


@router.post("/tasks/{task_id}/create-permit/acquire", response_model=WorkerTaskCreatePermitResponse)
async def acquire_create_permit(
    task_id: int,
    payload: WorkerTaskCreatePermitRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskCreatePermitResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task, run, domain = await _get_worker_task_for_update(task_id, worker, db)
    now = utcnow()

    if run.status == "success" or run.live_create_accepted_at is not None or domain.status == "success":
        task.status = "cancelled"
        task.finished_at = task.finished_at or now
        task.stop_reason = "Domain already has an accepted live create"
        await db.commit()
        return WorkerTaskCreatePermitResponse(allowed=False, stop=True, reason=task.stop_reason)
    if task.status not in {"queued", "running"}:
        await db.commit()
        return WorkerTaskCreatePermitResponse(allowed=False, stop=True, reason=f"Task is {task.status}")

    await _clear_expired_live_create_leases(db, run.id, now)
    existing_task_lease = (
        await db.execute(
            select(LiveCreateLease)
            .where(
                LiveCreateLease.worker_task_id == task.id,
                LiveCreateLease.expires_at > now,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing_task_lease is not None:
        await _sync_legacy_live_create_lease_fields(db, run, now)
        await db.commit()
        return WorkerTaskCreatePermitResponse(
            allowed=False,
            stop=False,
            reason="This task already has a live create request in flight",
            lease_expires_at=existing_task_lease.expires_at,
        )

    lease_seconds = max(1.0, float(get_settings().live_create_lease_seconds))
    max_in_flight = max(1, int(get_settings().live_create_max_in_flight_per_run))
    active_lease_count = await db.scalar(
        select(func.count(LiveCreateLease.id)).where(
            LiveCreateLease.attack_run_id == run.id,
            LiveCreateLease.expires_at > now,
        )
    )
    if int(active_lease_count or 0) >= max_in_flight:
        await _sync_legacy_live_create_lease_fields(db, run, now)
        await db.commit()
        return WorkerTaskCreatePermitResponse(
            allowed=False,
            stop=False,
            reason="Live create capacity is full",
            lease_expires_at=run.live_create_lease_expires_at,
        )

    lease = LiveCreateLease(
        attack_run_id=run.id,
        worker_id=worker.id,
        worker_task_id=task.id,
        expires_at=now + timedelta(seconds=lease_seconds),
    )
    db.add(lease)
    run.live_create_lease_worker_id = worker.id
    run.live_create_lease_task_id = task.id
    run.live_create_lease_expires_at = lease.expires_at
    run.updated_at = now
    await db.commit()
    return WorkerTaskCreatePermitResponse(
        allowed=True,
        stop=False,
        reason=None,
        lease_expires_at=run.live_create_lease_expires_at,
    )


@router.post("/tasks/{task_id}/create-permit/release", response_model=WorkerTaskResultResponse)
async def release_create_permit(
    task_id: int,
    payload: WorkerTaskCreatePermitReleaseRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    _task, run, _domain = await _get_worker_task_for_update(task_id, worker, db)
    await db.execute(
        delete(LiveCreateLease).where(
            LiveCreateLease.worker_task_id == task_id,
            LiveCreateLease.worker_id == worker.id,
        )
    )
    if run.live_create_accepted_at is None:
        await _sync_legacy_live_create_lease_fields(db, run, utcnow())
        run.updated_at = utcnow()
    await db.commit()
    return WorkerTaskResultResponse(detail="create permit released")


@router.post("/tasks/{task_id}/progress", response_model=WorkerTaskResultResponse)
async def report_task_progress(
    task_id: int,
    payload: WorkerTaskProgressRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task = await db.get(WorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Task not found")
    task.actual_rps = payload.actual_rps
    task.total_attempts = payload.total_attempts
    task.success_attempts = payload.success_attempts
    task.latency_ms = payload.latency_ms
    task.last_http_status = payload.last_http_status
    task.last_error = payload.last_error
    task.response_status_counts = payload.response_status_counts
    task.response_error_counts = payload.response_error_counts
    task.response_samples = payload.response_samples
    task.updated_at = utcnow()
    await recompute_run_statistics(db)
    await db.commit()
    return WorkerTaskResultResponse(detail="task progress accepted")


@router.post("/tasks/{task_id}/result", response_model=WorkerTaskResultResponse)
async def report_task_result(
    task_id: int,
    payload: WorkerTaskResultRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task = await db.get(WorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Task not found")
    run = await db.get(AttackRun, task.attack_run_id)
    domain = await db.get(DropDomain, task.domain_id)
    if run is None or domain is None:
        raise HTTPException(status_code=404, detail="Related run/domain not found")

    now = utcnow()
    task.status = payload.status
    task.total_attempts = payload.total_attempts
    task.success_attempts = payload.success_attempts
    task.latency_ms = payload.latency_ms
    task.last_http_status = payload.last_http_status
    task.last_error = payload.last_error
    task.response_status_counts = payload.response_status_counts
    task.response_error_counts = payload.response_error_counts
    task.response_samples = payload.response_samples
    task.finished_at = now

    if payload.status == "success":
        await db.execute(delete(LiveCreateLease).where(LiveCreateLease.attack_run_id == run.id))
        run.status = "success"
        run.finished_at = now
        run.success_worker_id = worker.id
        run.live_create_accepted_worker_id = worker.id
        run.live_create_accepted_task_id = task.id
        run.live_create_accepted_at = now
        run.live_create_lease_worker_id = worker.id
        run.live_create_lease_task_id = task.id
        run.live_create_lease_expires_at = None
        domain.status = "success"
        domain.success_at = now
        domain.success_worker_id = worker.id
        domain.success_response_code = payload.success_response_code
        domain.success_message = payload.success_message

        siblings = (
            await db.execute(
                select(WorkerTask).where(
                    WorkerTask.domain_id == domain.id,
                    WorkerTask.id != task.id,
                    WorkerTask.status.in_(["queued", "running"]),
                )
            )
        ).scalars().all()
        for sibling in siblings:
            sibling.status = "cancelled"
            sibling.finished_at = now
            sibling.stop_reason = "Domain registered by another worker"
        db.add(
            AttackEvent(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                level="success",
                event_type="domain_registered",
                message=f"{domain.fqdn} registered by worker {worker.name}",
            )
        )
    elif payload.status in {"failed", "cancelled", "stopped"}:
        remaining = (
            await db.execute(
                select(WorkerTask).where(
                    WorkerTask.attack_run_id == run.id,
                    WorkerTask.status.in_(["queued", "running"]),
                )
            )
        ).scalars().all()
        if not remaining and run.status != "success":
            run.status = "failed"
            run.finished_at = now
            domain.status = "queued" if domain.attack_enabled else "paused"
        db.add(
            AttackEvent(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                level="warning" if payload.status != "failed" else "error",
                event_type="task_result",
                message=f"Worker {worker.name} reported {payload.status} for {domain.fqdn}",
            )
        )

    worker.last_seen_at = now
    await rebalance_worker_pool(db, now=now)
    await recompute_run_statistics(db)
    await db.commit()
    return WorkerTaskResultResponse(detail="task result accepted")


@router.get("/discovery/tasks/next", response_model=DiscoveryWorkerTaskResponseEnvelope)
async def get_next_discovery_task(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> DiscoveryWorkerTaskResponseEnvelope:
    worker = await _get_worker_by_token(worker_id, db, x_worker_token)
    result = await db.execute(
        select(DiscoveryWorkerTask)
        .where(DiscoveryWorkerTask.worker_id == worker.id, DiscoveryWorkerTask.status == "queued")
        .order_by(DiscoveryWorkerTask.created_at.asc(), DiscoveryWorkerTask.id.asc())
        .limit(1)
    )
    task = result.scalar_one_or_none()
    if task is None:
        return DiscoveryWorkerTaskResponseEnvelope(task=None)

    discovery_domain = await db.get(DiscoveryDomain, task.discovery_domain_id)
    if discovery_domain is None:
        task.status = "failed"
        task.error_message = "Discovery domain not found"
        task.finished_at = utcnow()
        await db.commit()
        return DiscoveryWorkerTaskResponseEnvelope(task=None)

    settings = get_settings()
    discovery_settings = await get_discovery_runtime_settings(db, settings)
    return DiscoveryWorkerTaskResponseEnvelope(
        task=DiscoveryWorkerTaskPayloadResponse(
            task_id=task.id,
            discovery_domain_id=discovery_domain.id,
            worker_id=worker.id,
            fqdn=discovery_domain.fqdn,
            zone=discovery_domain.zone,
            source_mode=task.source_mode or discovery_domain.source_mode or "rdap",
            bootstrap_url=settings.discovery_rdap_bootstrap_url,
            timeout_seconds=discovery_settings.discovery_timeout_seconds,
        )
    )


@router.post("/discovery/tasks/{task_id}/ack", response_model=WorkerTaskResultResponse)
async def acknowledge_discovery_task(
    task_id: int,
    payload: WorkerTaskAckRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task = await db.get(DiscoveryWorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Discovery task not found")
    now = utcnow()
    task.acknowledged_at = now
    if task.status == "queued":
        task.status = "running"
    task.updated_at = now
    await db.commit()
    return WorkerTaskResultResponse(detail="discovery task acknowledged")


@router.post("/discovery/tasks/{task_id}/result", response_model=WorkerTaskResultResponse)
async def report_discovery_task_result(
    task_id: int,
    payload: DiscoveryWorkerTaskResultRequest,
    db: AsyncSession = Depends(get_db),
    x_worker_token: str | None = Header(default=None),
) -> WorkerTaskResultResponse:
    worker = await _get_worker_by_token(payload.worker_id, db, x_worker_token)
    task = await db.get(DiscoveryWorkerTask, task_id)
    if task is None or task.worker_id != worker.id:
        raise HTTPException(status_code=404, detail="Discovery task not found")
    message = await apply_discovery_worker_task_result(db, task, payload)
    worker.last_seen_at = utcnow()
    await db.commit()
    if message:
        token, chat_id = await get_diagnostic_telegram_settings(db)
        if token and chat_id:
            try:
                await TelegramNotifier(get_settings()).send_diagnostic(
                    "Drop discovery",
                    message,
                    token=token,
                    chat_id=chat_id,
                )
            except Exception:
                logger.exception("Failed to send discovery Telegram notification")
    return WorkerTaskResultResponse(detail="discovery task result accepted")
