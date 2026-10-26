from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer
from app.services.vpn_devices import lock_customer_devices, lock_device_targets, stage_device_revoke


ARCHIVABLE_SUBSCRIPTION_STATUSES = ("active", "trial")
REVOKABLE_KEY_STATUSES = (
    "active", "pending_sync", "syncing", "pending_revoke", "pending_suspend", "suspended", "failed",
)


class VpnCustomerArchiveNotFoundError(LookupError):
    pass


class VpnCustomerArchiveConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class StagedVpnCustomerArchive:
    customer: VpnCustomer
    disabled_subscription_count: int
    access_keys: list[VpnAccessKey]


async def stage_vpn_customer_archive(
    session: AsyncSession,
    customer_id: int,
    *,
    now: datetime | None = None,
) -> StagedVpnCustomerArchive:
    changed_at = now or utcnow()
    customer, subscriptions, all_keys = await lock_customer_devices(session, customer_id)
    if customer is None:
        raise VpnCustomerArchiveNotFoundError("VPN customer not found")
    if customer.status == "archived":
        raise VpnCustomerArchiveConflictError("VPN customer is already archived")

    access_keys = [key for key in all_keys if key.status in REVOKABLE_KEY_STATUSES]

    customer.status = "archived"
    customer.updated_at = changed_at
    disabled_count = 0
    for subscription in subscriptions:
        if subscription.status in ARCHIVABLE_SUBSCRIPTION_STATUSES:
            subscription.status = "disabled"
            subscription.updated_at = changed_at
            disabled_count += 1
    for access_key in all_keys:
        await stage_device_revoke(session, access_key, now=changed_at)

    await session.flush()
    try:
        await lock_device_targets(session, access_keys)
    except ValueError:
        raise VpnCustomerArchiveConflictError("VPN endpoint binding changed; retry archive") from None

    return StagedVpnCustomerArchive(
        customer=customer,
        disabled_subscription_count=disabled_count,
        access_keys=access_keys,
    )
