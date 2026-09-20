from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    AttackRun,
    VpnAccessKey,
    VpnCustomer,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
)


DEVICE_SLOT_STATUSES = ("pending_sync", "syncing", "active", "pending_revoke")
VPN_MUTATION_ACTIONS = {
    "vpn_install",
    "vpn_update",
    "vpn_restart",
    "vpn_autoconfig",
    "vpn_create_inbound",
}


@dataclass(frozen=True, slots=True)
class VpnNodeEligibility:
    eligible: bool
    reasons: tuple[str, ...]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_subscription_access(
    subscription: VpnSubscription,
    customer: VpnCustomer | None,
    *,
    device_slots: int,
    now: datetime | None = None,
) -> str | None:
    current_time = _as_utc(now or utcnow())
    starts_at = _as_utc(subscription.starts_at)
    expires_at = _as_utc(subscription.expires_at)
    assert current_time is not None

    if customer is None or customer.status != "active":
        return "VPN customer is not active"
    if subscription.status not in {"active", "trial"}:
        return f"VPN subscription is {subscription.status}"
    if starts_at is not None and starts_at > current_time:
        return "VPN subscription has not started"
    if expires_at is not None and expires_at <= current_time:
        return "VPN subscription has expired"
    if device_slots >= subscription.max_devices:
        return "VPN device limit reached"
    return None


async def count_device_slots(
    session: AsyncSession,
    subscription_id: int,
    *,
    exclude_key_id: int | None = None,
) -> int:
    query = select(func.count(VpnAccessKey.id)).where(
        VpnAccessKey.subscription_id == subscription_id,
        VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
    )
    if exclude_key_id is not None:
        query = query.where(VpnAccessKey.id != exclude_key_id)
    return int(await session.scalar(query) or 0)


async def lock_vpn_subscription(
    session: AsyncSession,
    subscription_id: int,
) -> VpnSubscription | None:
    return await session.scalar(
        select(VpnSubscription)
        .where(VpnSubscription.id == subscription_id)
        .with_for_update()
    )


async def lock_vpn_worker(
    session: AsyncSession,
    worker_id: int,
) -> WorkerNode | None:
    return await session.scalar(
        select(WorkerNode)
        .where(WorkerNode.id == worker_id)
        .with_for_update()
    )


async def active_vpn_mutation_worker_ids(session: AsyncSession) -> set[int]:
    result = await session.execute(
        select(WorkerMaintenanceJob.worker_id)
        .where(
            WorkerMaintenanceJob.action.in_(VPN_MUTATION_ACTIONS),
            WorkerMaintenanceJob.status.in_(("queued", "running")),
        )
        .distinct()
    )
    return {int(worker_id) for worker_id in result.scalars().all()}


async def active_attack_worker_ids(session: AsyncSession) -> set[int]:
    result = await session.execute(
        select(WorkerTask.worker_id)
        .join(AttackRun, AttackRun.id == WorkerTask.attack_run_id)
        .where(
            WorkerTask.status.in_(("queued", "running")),
            AttackRun.status.in_(("planned", "running")),
        )
        .distinct()
    )
    return {int(worker_id) for worker_id in result.scalars().all()}


async def evaluate_vpn_node(session: AsyncSession, worker: WorkerNode) -> VpnNodeEligibility:
    reasons: list[str] = []
    if not worker.is_enabled or worker.status in {"offline", "disabled"}:
        reasons.append("Worker is not online and enabled")
    if not worker.vpn_enabled or worker.vpn_role == "none":
        reasons.append("VPN role is disabled")
    if worker.vpn_runtime_status != "ready":
        reasons.append("VPN runtime is not ready")
    if not worker.ssh_access_configured:
        reasons.append("Worker SSH access is not configured")
    if not worker.vpn_inbound_id:
        reasons.append("VPN inbound ID is not configured")
    if not worker.vpn_public_host:
        reasons.append("VPN public host is not configured")
    if worker.id in await active_attack_worker_ids(session):
        reasons.append("Worker is assigned to an active domain attack")
    return VpnNodeEligibility(eligible=not reasons, reasons=tuple(reasons))


async def select_vpn_node(
    session: AsyncSession,
    *,
    worker_id: int | None = None,
) -> WorkerNode | None:
    query = select(WorkerNode).where(WorkerNode.archived_at.is_(None))
    if worker_id is not None:
        query = query.where(WorkerNode.id == worker_id)
    workers = (await session.execute(query.order_by(WorkerNode.id.asc()))).scalars().all()

    eligible: list[tuple[int, datetime | None, int, WorkerNode]] = []
    for worker in workers:
        result = await evaluate_vpn_node(session, worker)
        if not result.eligible:
            continue
        active_keys = int(
            await session.scalar(
                select(func.count(VpnAccessKey.id)).where(
                    VpnAccessKey.worker_id == worker.id,
                    VpnAccessKey.status == "active",
                )
            )
            or 0
        )
        eligible.append((active_keys, worker.vpn_last_checked_at, worker.id, worker))

    eligible.sort(
        key=lambda item: (
            item[0],
            item[1] is not None,
            _as_utc(item[1]) or datetime.min.replace(tzinfo=timezone.utc),
            item[2],
        )
    )
    return eligible[0][3] if eligible else None
