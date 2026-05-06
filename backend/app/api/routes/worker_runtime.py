from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import AttackEvent, AttackRun, ContactProfile, DropDomain, RegistrarAccount, WorkerNode, WorkerTask
from app.db.session import get_db
from app.schemas.runtime import (
    WorkerHeartbeatRequest,
    WorkerHeartbeatResponse,
    WorkerTaskAckRequest,
    WorkerTaskPayloadResponse,
    WorkerTaskProgressRequest,
    WorkerTaskStatusResponse,
    WorkerTaskResponseEnvelope,
    WorkerTaskResultRequest,
    WorkerTaskResultResponse,
)
from app.services.attack_runtime import rebalance_worker_pool, recompute_run_statistics

router = APIRouter(prefix="/worker-runtime", tags=["worker-runtime"])


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
        .where(WorkerTask.worker_id == worker.id, WorkerTask.status.in_(["queued", "running"]))
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

    account = await db.get(RegistrarAccount, domain.registrar_account_id) if domain.registrar_account_id else None
    contact = await _resolve_contact_payload(db, domain, account)
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
        registration_extra_parameters=domain.registration_extra_parameters,
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
            "extra_parameters": contact.extra_parameters,
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
    task.acknowledged_at = utcnow()
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
    task.finished_at = now

    if payload.status == "success":
        run.status = "success"
        run.finished_at = now
        run.success_worker_id = worker.id
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
