from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription


ARCHIVABLE_SUBSCRIPTION_STATUSES = ("active", "trial")
REVOKABLE_KEY_STATUSES = ("active", "pending_sync", "syncing", "pending_revoke")


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
    customer = await session.scalar(
        select(VpnCustomer).where(VpnCustomer.id == customer_id).with_for_update()
    )
    if customer is None:
        raise VpnCustomerArchiveNotFoundError("VPN customer not found")
    if customer.status == "archived":
        raise VpnCustomerArchiveConflictError("VPN customer is already archived")

    subscriptions = list(
        (
            await session.scalars(
                select(VpnSubscription)
                .where(VpnSubscription.customer_id == customer_id)
                .with_for_update()
            )
        ).all()
    )
    subscription_ids = [subscription.id for subscription in subscriptions]
    access_keys: list[VpnAccessKey] = []
    if subscription_ids:
        access_keys = list(
            (
                await session.scalars(
                    select(VpnAccessKey)
                    .where(
                        VpnAccessKey.subscription_id.in_(subscription_ids),
                        VpnAccessKey.status.in_(REVOKABLE_KEY_STATUSES),
                    )
                    .order_by(VpnAccessKey.id.asc())
                    .with_for_update()
                )
            ).all()
        )

    customer.status = "archived"
    customer.updated_at = changed_at
    disabled_count = 0
    for subscription in subscriptions:
        if subscription.status in ARCHIVABLE_SUBSCRIPTION_STATUSES:
            subscription.status = "disabled"
            subscription.updated_at = changed_at
            disabled_count += 1
    for access_key in access_keys:
        access_key.status = "pending_revoke"
        access_key.updated_at = changed_at

    return StagedVpnCustomerArchive(
        customer=customer,
        disabled_subscription_count=disabled_count,
        access_keys=access_keys,
    )
