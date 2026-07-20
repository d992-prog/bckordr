from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import DiscoveryDomain, DiscoveryObservation, DiscoveryWorkerTask, WorkerNode
from app.schemas.runtime import DiscoveryWorkerTaskResultRequest
from app.services.discovery import (
    DiscoveryObservationInput,
    _build_observation_model,
    _build_transition_notification,
    apply_discovery_observation,
    trim_discovery_observations,
)

ACTIVE_DISCOVERY_TASK_STATUSES = {"queued", "running"}
ELIGIBLE_WORKER_STATUSES = {"ready", "waiting", "running"}


def _active_task_statuses() -> list[str]:
    return sorted(ACTIVE_DISCOVERY_TASK_STATUSES)


def _eligible_worker_statuses() -> list[str]:
    return sorted(ELIGIBLE_WORKER_STATUSES)


async def load_eligible_discovery_workers(session: AsyncSession) -> list[WorkerNode]:
    result = await session.execute(
        select(WorkerNode)
        .where(
            WorkerNode.is_enabled.is_(True),
            WorkerNode.control_token.is_not(None),
            WorkerNode.status.in_(_eligible_worker_statuses()),
        )
        .order_by(WorkerNode.id.asc())
    )
    return list(result.scalars().all())


async def expire_stale_discovery_worker_tasks(
    session: AsyncSession,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> int:
    cutoff = now - timedelta(seconds=max(int(stale_after_seconds), 1))
    result = await session.execute(
        select(DiscoveryWorkerTask).where(
            DiscoveryWorkerTask.status.in_(_active_task_statuses()),
            DiscoveryWorkerTask.assigned_at.is_not(None),
            DiscoveryWorkerTask.assigned_at < cutoff,
        )
    )
    tasks = list(result.scalars().all())
    for task in tasks:
        task.status = "failed"
        task.finished_at = now
        task.error_message = "Discovery worker task expired before result"
        task.updated_at = now
    return len(tasks)


async def enqueue_due_discovery_worker_tasks(
    session: AsyncSession,
    *,
    now: datetime,
    batch_size: int,
) -> int:
    workers = await load_eligible_discovery_workers(session)
    if not workers:
        return 0

    active_domain_ids = (
        select(DiscoveryWorkerTask.discovery_domain_id)
        .where(DiscoveryWorkerTask.status.in_(_active_task_statuses()))
        .subquery()
    )
    result = await session.execute(
        select(DiscoveryDomain)
        .where(
            DiscoveryDomain.is_enabled.is_(True),
            DiscoveryDomain.status.notin_(["available", "ignored"]),
            (DiscoveryDomain.next_check_at.is_(None)) | (DiscoveryDomain.next_check_at <= now),
            DiscoveryDomain.id.notin_(select(active_domain_ids.c.discovery_domain_id)),
        )
        .order_by(DiscoveryDomain.next_check_at.asc(), DiscoveryDomain.id.asc())
        .limit(max(int(batch_size), 1))
    )
    domains = list(result.scalars().all())
    if not domains:
        return 0

    active_counts = Counter[int]()
    counts_result = await session.execute(
        select(DiscoveryWorkerTask.worker_id, func.count(DiscoveryWorkerTask.id))
        .where(DiscoveryWorkerTask.status.in_(_active_task_statuses()))
        .group_by(DiscoveryWorkerTask.worker_id)
    )
    for worker_id, count in counts_result.all():
        active_counts[int(worker_id)] = int(count)

    created = 0
    for domain in domains:
        worker = min(workers, key=lambda item: (active_counts[item.id], item.id))
        active_counts[worker.id] += 1
        session.add(
            DiscoveryWorkerTask(
                discovery_domain_id=domain.id,
                worker_id=worker.id,
                status="queued",
                source_mode=domain.source_mode or "rdap",
                assigned_at=now,
            )
        )
        created += 1
    return created


async def apply_discovery_worker_task_result(
    session: AsyncSession,
    task: DiscoveryWorkerTask,
    payload: DiscoveryWorkerTaskResultRequest,
    *,
    now: datetime | None = None,
) -> str | None:
    completed_at = now or utcnow()
    domain = await session.get(DiscoveryDomain, task.discovery_domain_id)
    if domain is None:
        task.status = "failed"
        task.finished_at = completed_at
        task.error_message = "Discovery domain not found"
        task.updated_at = completed_at
        return None

    previous_status = domain.status
    previous_pending_at = domain.first_seen_pending_delete_at
    previous_available_at = domain.available_first_seen_at

    observation = DiscoveryObservationInput(
        source=payload.source,
        observed_at=payload.observed_at,
        http_status=payload.http_status,
        latency_ms=payload.latency_ms,
        lifecycle_stage=payload.lifecycle_stage,
        availability_status=payload.availability_status,
        status_codes=payload.status_codes,
        raw_response=payload.raw_response,
        error=payload.error,
    )
    apply_discovery_observation(domain, observation)
    session.add(_build_observation_model(domain, observation))
    await session.flush()
    await trim_discovery_observations(session, domain.id)

    task.status = "completed"
    task.finished_at = completed_at
    task.error_message = payload.error
    task.updated_at = completed_at

    return _build_transition_notification(
        domain,
        previous_status=previous_status,
        previous_pending_at=previous_pending_at,
        previous_available_at=previous_available_at,
    )
