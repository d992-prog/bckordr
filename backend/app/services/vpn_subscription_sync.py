from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.services.vpn_policy import DEVICE_SLOT_STATUSES, _as_utc, count_device_slots


POLICY_FIELDS = frozenset({"status", "starts_at", "expires_at", "traffic_limit_gb", "max_devices"})
RESTORABLE_KEY_STATUSES = ("active", "pending_sync", "syncing", "pending_suspend", "suspended", "failed")


class SubscriptionPolicyError(ValueError):
    pass


class SubscriptionPolicyConflict(ValueError):
    pass


def subscription_key_action(
    subscription: VpnSubscription, customer: VpnCustomer | None, now: datetime,
) -> str:
    if subscription.status == "cancelled" or (customer is not None and customer.status == "archived"):
        return "revoke"
    start, end = _as_utc(subscription.starts_at), _as_utc(subscription.expires_at)
    if (
        customer is None or customer.status != "active"
        or subscription.status not in {"active", "trial"}
        or (start is not None and start > now)
        or (end is not None and end <= now)
    ):
        return "suspend"
    return "sync"


async def stage_subscription_update(
    db: AsyncSession, subscription: VpnSubscription, updates: dict,
) -> None:
    """Validate and stage policy atomically; caller owns the lock and commit."""
    proposed_status = updates.get("status", subscription.status)
    max_devices = updates.get("max_devices", subscription.max_devices)
    if proposed_status not in {"active", "trial", "disabled", "expired", "cancelled"}:
        raise SubscriptionPolicyError("Invalid VPN subscription status")
    if max_devices is None:
        raise SubscriptionPolicyError("VPN device limit cannot be null")
    start = _as_utc(updates.get("starts_at", subscription.starts_at))
    end = _as_utc(updates.get("expires_at", subscription.expires_at))
    if start is not None and end is not None and end <= start:
        raise SubscriptionPolicyError("Subscription expiration must be after its start")

    policy_changed = any(
        (_as_utc(getattr(subscription, field)) != _as_utc(value)
         if field in {"starts_at", "expires_at"} else getattr(subscription, field) != value)
        for field, value in updates.items() if field in POLICY_FIELDS
    )
    customer = await db.get(VpnCustomer, subscription.customer_id)
    if policy_changed and proposed_status in {"active", "trial"}:
        if customer is None or customer.status != "active":
            raise SubscriptionPolicyConflict("VPN customer is not active")
    if max_devices < subscription.max_devices and max_devices < await count_device_slots(db, subscription.id):
        raise SubscriptionPolicyConflict("Revoke excess VPN keys before reducing the device limit")

    now = utcnow()
    for field, value in updates.items():
        setattr(subscription, field, value)
    subscription.updated_at = now
    if not policy_changed:
        return
    keys = (await db.scalars(
        select(VpnAccessKey).where(
            VpnAccessKey.subscription_id == subscription.id,
            VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
        ).order_by(VpnAccessKey.id).with_for_update()
    )).all()
    action = subscription_key_action(subscription, customer, now)
    for key in keys:
        # Pending manual revoke is never cancelled by subscription edits.
        if key.status == "pending_revoke":
            continue
        key.expires_at = subscription.expires_at
        key.status = f"pending_{action}"
        key.last_error = None
        key.updated_at = now
