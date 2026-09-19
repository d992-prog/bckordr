from __future__ import annotations

from datetime import datetime

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnSubscription, WorkerNode
from app.services.vpn_provisioning import revoke_vpn_access_key


EXPIRING_SUBSCRIPTION_STATUSES = ("active", "trial")
REVOKABLE_KEY_STATUSES = ("active", "pending_sync", "syncing", "pending_revoke")
TERMINAL_SUBSCRIPTION_STATUSES = ("expired", "cancelled", "disabled")


async def run_vpn_lifecycle_maintenance(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current_time = now or utcnow()

    expired_subscriptions = (
        await db.scalars(
            select(VpnSubscription).where(
                VpnSubscription.status.in_(EXPIRING_SUBSCRIPTION_STATUSES),
                VpnSubscription.expires_at.is_not(None),
                VpnSubscription.expires_at <= current_time,
            )
        )
    ).all()
    for subscription in expired_subscriptions:
        subscription.status = "expired"
        subscription.updated_at = current_time

    await db.flush()

    keys_to_revoke = (
        await db.scalars(
            select(VpnAccessKey)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .where(
                VpnAccessKey.status.in_(REVOKABLE_KEY_STATUSES),
                or_(
                    and_(VpnAccessKey.expires_at.is_not(None), VpnAccessKey.expires_at <= current_time),
                    VpnSubscription.status.in_(TERMINAL_SUBSCRIPTION_STATUSES),
                ),
            )
        )
    ).all()

    revoked_keys = 0
    pending_revoke_keys = 0
    for access_key in keys_to_revoke:
        worker = await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None
        await revoke_vpn_access_key(db, access_key, worker=worker)
        if access_key.status == "revoked":
            revoked_keys += 1
        else:
            pending_revoke_keys += 1

    await db.flush()
    return {
        "expired_subscriptions": len(expired_subscriptions),
        "checked_keys": len(keys_to_revoke),
        "revoked_keys": revoked_keys,
        "pending_revoke_keys": pending_revoke_keys,
    }
