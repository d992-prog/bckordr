from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    AttackRun,
    VpnAccessKey,
    VpnNodeEvent,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
)
from app.services.vpn_policy import DEVICE_SLOT_STATUSES


class WorkerDecommissionNotFoundError(ValueError):
    pass


class WorkerDecommissionConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerDecommissionResult:
    worker: WorkerNode
    retired_key_count: int


async def decommission_worker(
    session: AsyncSession,
    worker_id: int,
    *,
    now: datetime | None = None,
) -> WorkerDecommissionResult:
    archived_at = now or utcnow()
    worker = await session.scalar(
        select(WorkerNode)
        .where(WorkerNode.id == worker_id, WorkerNode.archived_at.is_(None))
        .with_for_update()
    )
    if worker is None:
        raise WorkerDecommissionNotFoundError("Worker not found")

    active_attack_task_id = await session.scalar(
        select(WorkerTask.id)
        .join(AttackRun, AttackRun.id == WorkerTask.attack_run_id)
        .where(
            WorkerTask.worker_id == worker_id,
            WorkerTask.status.in_(("queued", "planned", "running")),
            AttackRun.status.in_(("planned", "running")),
        )
        .limit(1)
    )
    if active_attack_task_id is not None:
        raise WorkerDecommissionConflictError("Worker is assigned to an active domain attack")

    active_maintenance_id = await session.scalar(
        select(WorkerMaintenanceJob.id)
        .where(
            WorkerMaintenanceJob.worker_id == worker_id,
            WorkerMaintenanceJob.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active_maintenance_id is not None:
        raise WorkerDecommissionConflictError("Worker has active maintenance")

    keys = list(
        (
            await session.scalars(
                select(VpnAccessKey)
                .where(
                    VpnAccessKey.worker_id == worker_id,
                    VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
                )
                .with_for_update()
            )
        ).all()
    )
    for access_key in keys:
        access_key.status = "revoked"
        access_key.revoked_at = archived_at
        access_key.config_uri = None
        access_key.last_error = "Node was decommissioned; remote revoke was not confirmed"
        access_key.updated_at = archived_at

    previous_status = worker.status
    previous_vpn_status = worker.vpn_runtime_status
    worker.archived_at = archived_at
    worker.is_enabled = False
    worker.status = "archived"
    worker.control_token = None
    worker.ssh_password = None
    worker.ssh_key_path = None
    worker.vpn_enabled = False
    worker.vpn_role = "none"
    worker.vpn_runtime_status = "decommissioned"
    worker.vpn_panel_password = None
    worker.updated_at = archived_at
    session.add(
        VpnNodeEvent(
            worker_id=worker.id,
            level="warning",
            event_type="node_decommissioned",
            message="Worker and VPN node were removed from active use",
            details={
                "retired_key_count": len(keys),
                "previous_status": previous_status,
                "previous_vpn_status": previous_vpn_status,
                "remote_revoke_confirmed": False,
            },
        )
    )
    await session.flush()
    return WorkerDecommissionResult(worker=worker, retired_key_count=len(keys))
