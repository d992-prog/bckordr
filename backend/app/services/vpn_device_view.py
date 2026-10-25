"""Owned device metadata; credentials are read only on the connection endpoint."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnDevice,
    VpnEndpoint,
    VpnSubscription,
)
from app.schemas.vpn_portal import (
    PortalDevice,
    PortalDeviceConnection,
    PortalDeviceLocation,
    PortalDeviceLocations,
)
from app.services.vpn_customer_view import may_read_connection
from app.services.vpn_device_subscriptions import (
    device_subscription_url,
    device_token,
    subscription_connection_uri,
)
from app.services.vpn_devices import MissingCustomerDevice
from app.services.vpn_display import InvalidVpnDisplay, validate_display_name
from app.services.vpn_policy import DEVICE_SLOT_STATUSES, select_public_vpn_endpoint


def _owned_query(customer_id: int):
    return (select(VpnDevice, VpnSubscription, VpnCustomer)
            .join(VpnSubscription, VpnSubscription.id == VpnDevice.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(VpnCustomer.id == customer_id).execution_options(populate_existing=True))


async def owned_device(db: AsyncSession, customer_id: int, device_id: int):
    row = (await db.execute(_owned_query(customer_id).where(VpnDevice.id == device_id))).first()
    if row is None:
        raise MissingCustomerDevice()
    return row


async def _children(db: AsyncSession, device: VpnDevice) -> list[VpnAccessKey]:
    return list((await db.scalars(select(VpnAccessKey).where(
        VpnAccessKey.device_id == device.id, VpnAccessKey.subscription_id == device.subscription_id,
    ).order_by(VpnAccessKey.id).execution_options(populate_existing=True))).all())


def _readable(customer, subscription, device, key, now):
    return bool(device.revoke_requested_at is None and key is not None
                and key.revoke_requested_at is None and key.revoked_at is None
                and may_read_connection(customer, subscription, key, now))


async def _device_metadata(db, row, now) -> PortalDevice:
    device, subscription, customer = row
    children = await _children(db, device)
    preferred = next((key for key in children if key.id == device.preferred_key_id), None)
    if device.revoke_requested_at is not None:
        state = "pending_revoke" if any(key.status in DEVICE_SLOT_STATUSES for key in children) else "revoked"
    else:
        state = preferred.status if preferred is not None else "unavailable"
    return PortalDevice(id=device.id, subscription_id=subscription.id, display_name=device.display_name,
                        legacy_profile_id=device.legacy_profile_id, state=state,
                        can_connect=_readable(customer, subscription, device, preferred, now))


async def list_customer_devices(db: AsyncSession, customer_id: int, *, now: datetime | None = None):
    rows = (await db.execute(_owned_query(customer_id).order_by(VpnDevice.id))).all()
    return [await _device_metadata(db, row, now or utcnow()) for row in rows]


async def customer_device(db: AsyncSession, customer_id: int, device_id: int, *, now: datetime | None = None):
    return await _device_metadata(db, await owned_device(db, customer_id, device_id), now or utcnow())


def _location(endpoint: VpnEndpoint) -> PortalDeviceLocation:
    try:
        name = validate_display_name(endpoint.public_name)
    except InvalidVpnDisplay:
        name = "Ранее выданное подключение"
    country = endpoint.country_code
    if not (isinstance(country, str) and len(country) == 2 and country.isascii() and country.isupper() and country.isalpha()):
        country = None
    return PortalDeviceLocation(endpoint_id=endpoint.id, name=name, country_code=country,
                                is_published=endpoint.is_published)


async def customer_device_locations(
    db: AsyncSession, customer_id: int, device_id: int, settings: Settings, *, now: datetime | None = None,
) -> PortalDeviceLocations:
    current_time = now or utcnow()
    device, subscription, customer = await owned_device(db, customer_id, device_id)
    children = await _children(db, device)
    preferred = next((key for key in children if key.id == device.preferred_key_id), None)
    pending = next((key for key in children if key.id == device.pending_key_id), None)
    current = await db.get(VpnEndpoint, preferred.endpoint_id, populate_existing=True) if preferred and preferred.endpoint_id else None
    state = "ready" if _readable(customer, subscription, device, preferred, current_time) else "unavailable"
    if pending is not None and device.revoke_requested_at is None:
        operation = await db.scalar(select(VpnControlOperation).where(
            VpnControlOperation.access_key_id == pending.id,
            VpnControlOperation.generation == pending.operation_generation,
        ).execution_options(populate_existing=True))
        state = "preparing"
        if operation is not None and operation.state == "uncertain":
            state = "verification_required"
        elif operation is not None and operation.state in {"failed", "superseded"}:
            state = "failed"
    available = []
    from app.services.vpn_policy import validate_subscription_access

    if device.revoke_requested_at is None and validate_subscription_access(
        subscription, customer, device_slots=0, enforce_device_limit=False, now=current_time,
    ) is None:
        endpoint_ids = (await db.scalars(select(VpnEndpoint.id).where(VpnEndpoint.is_published.is_(True))
                                       .order_by(VpnEndpoint.id))).all()
        for endpoint_id in endpoint_ids:
            child = next((key for key in children if key.endpoint_id == endpoint_id), None)
            if child is not None and (child.status in {"revoked", "pending_revoke"}
                                      or child.revoke_requested_at is not None or child.revoked_at is not None):
                continue
            selected = await select_public_vpn_endpoint(
                db, endpoint_id=endpoint_id, reusable_key_id=child.id if child is not None else None,
                now=current_time, health_max_age_seconds=settings.vpn_endpoint_health_max_age_seconds,
            )
            if selected is not None and selected.endpoint.protocol == "vless":
                available.append(_location(selected.endpoint))
    return PortalDeviceLocations(current=_location(current) if current else None,
                                 pending_endpoint_id=pending.endpoint_id if pending else None,
                                 selection_state=state, available=available)


async def customer_device_connection(db: AsyncSession, customer_id: int, device_id: int, settings: Settings):
    device, _, _ = await owned_device(db, customer_id, device_id)
    return PortalDeviceConnection(subscription_url=device_subscription_url(settings, device),
                                  direct_uri=await subscription_connection_uri(db, settings, device_token(settings, device)))
