"""Logical devices; caller owns the customer lock and transaction for writes."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnDevice, VpnSubscription
from app.services.vpn_display import validate_display_name


async def lock_customer_devices(
    db: AsyncSession, customer_id: int,
) -> tuple[VpnCustomer | None, list[VpnSubscription], list[VpnAccessKey]]:
    """Use the same customer -> every subscription -> every key order as strict control."""
    def locked(query):
        return query.with_for_update().execution_options(populate_existing=True)

    customer = await db.scalar(locked(select(VpnCustomer).where(VpnCustomer.id == customer_id)))
    if customer is None:
        return None, [], []
    subscriptions = list((await db.scalars(locked(select(VpnSubscription).where(
        VpnSubscription.customer_id == customer_id,
    ).order_by(VpnSubscription.id)))).all())
    keys = list((await db.scalars(locked(select(VpnAccessKey).where(
        VpnAccessKey.subscription_id.in_([subscription.id for subscription in subscriptions]),
    ).order_by(VpnAccessKey.id)))).all())
    return customer, subscriptions, keys


async def lock_key_device_context(db: AsyncSession, key_id: int) -> VpnAccessKey | None:
    customer_id = await db.scalar(select(VpnSubscription.customer_id).join(
        VpnAccessKey, VpnAccessKey.subscription_id == VpnSubscription.id,
    ).where(VpnAccessKey.id == key_id))
    if customer_id is None:
        return None
    _, _, keys = await lock_customer_devices(db, customer_id)
    return next((key for key in keys if key.id == key_id), None)


async def checked_device(db: AsyncSession, key: VpnAccessKey) -> VpnDevice | None:
    if key.device_id is None:
        return None
    # A fresh parent read must not discard this transaction's staged parent edits.
    await db.flush()
    device = await db.get(VpnDevice, key.device_id, populate_existing=True)
    if device is None or device.subscription_id != key.subscription_id:
        raise ValueError("vpn_device_binding_invalid")
    for key_id in (device.preferred_key_id, device.pending_key_id):
        if key_id is not None:
            binding = (await db.execute(select(VpnAccessKey.device_id, VpnAccessKey.subscription_id).where(
                VpnAccessKey.id == key_id,
            ))).one_or_none()
            if binding is None or tuple(binding) != (device.id, device.subscription_id):
                raise ValueError("vpn_device_binding_invalid")
    return device


async def attach_initial_device(db: AsyncSession, key: VpnAccessKey) -> VpnDevice:
    if key.device_id is not None:
        device = await checked_device(db, key)
        assert device is not None
        return device
    from app.services.vpn_customer_view import profile_display_name

    device = await db.scalar(select(VpnDevice).where(VpnDevice.legacy_profile_id == key.id))
    if device is None:
        device = VpnDevice(subscription_id=key.subscription_id, legacy_profile_id=key.id,
                           preferred_key_id=key.id, display_name=profile_display_name(key), token_generation=0)
        db.add(device)
        await db.flush()
    if device.subscription_id != key.subscription_id:
        raise ValueError("vpn_device_binding_invalid")
    key.device_id = device.id
    await db.flush()
    await checked_device(db, key)
    return device


async def rename_device(
    db: AsyncSession, key: VpnAccessKey, display_name: str, *, now: datetime | None = None,
) -> None:
    name, current = validate_display_name(display_name), now or utcnow()
    device = await checked_device(db, key)
    children = [key]
    if device is not None:
        device.display_name, device.updated_at = name, current
        children = (await db.scalars(select(VpnAccessKey).where(
            VpnAccessKey.device_id == device.id, VpnAccessKey.subscription_id == device.subscription_id,
        ).order_by(VpnAccessKey.id))).all()
    for child in children:
        child.display_name, child.updated_at = name, current
    await db.flush()


async def stage_device_revoke(
    db: AsyncSession, key: VpnAccessKey, *, now: datetime | None = None,
) -> VpnDevice:
    current = now or utcnow()
    device = await attach_initial_device(db, key)
    if device.revoke_requested_at is None:
        device.revoke_requested_at = current
        device.token_generation += 1
    device.pending_key_id = None
    device.updated_at = current
    children = (await db.scalars(select(VpnAccessKey).where(
        VpnAccessKey.device_id == device.id, VpnAccessKey.subscription_id == device.subscription_id,
    ).order_by(VpnAccessKey.id))).all()
    for child in children:
        if child.status != "revoked":
            child.revoke_requested_at = child.revoke_requested_at or current
            child.status, child.updated_at = "pending_revoke", current
    await db.flush()
    return device
