"""Logical devices; caller owns the customer lock and transaction for writes."""

from datetime import datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnDevice, VpnSubscription
from app.services.vpn_display import validate_display_name
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    load_transport_snapshot,
)


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
            binding = (await db.execute(select(VpnAccessKey.device_id, VpnAccessKey.subscription_id,
                                              VpnAccessKey.credential_generation).where(
                VpnAccessKey.id == key_id,
            ))).one_or_none()
            if binding is None or tuple(binding) != (device.id, device.subscription_id, device.credential_generation):
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
    from app.db.models import VpnDeviceOperation

    operations = (await db.scalars(select(VpnDeviceOperation).where(
        VpnDeviceOperation.device_id == device.id, VpnDeviceOperation.action.in_(("create", "replace")),
        VpnDeviceOperation.finished_at.is_(None),
    ))).all()
    for operation in operations:
        operation.stage, operation.public_error = "failed", "customer_device_revoked"
        operation.finished_at, operation.updated_at = current, current
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


class MissingCustomerDevice(LookupError):
    def __init__(self) -> None:
        super().__init__("customer_device_not_found")


async def lock_owned_device(db: AsyncSession, customer_id: int, device_id: int):
    customer, subscriptions, keys = await lock_customer_devices(db, customer_id)
    by_id = {subscription.id: subscription for subscription in subscriptions}
    device = await db.scalar(select(VpnDevice).where(
        VpnDevice.id == device_id, VpnDevice.subscription_id.in_(by_id),
    ).execution_options(populate_existing=True))
    if customer is None or device is None:
        raise MissingCustomerDevice()
    return customer, by_id[device.subscription_id], device, keys


async def select_device_location(
    db: AsyncSession, *, customer_id: int, device_id: int,
    endpoint_id: int, settings: Settings, now: datetime,
) -> VpnDevice:
    from app.db.models import VpnControlOperation, WorkerNode
    from app.services.vpn_control_intents import (
        VpnControlIntentError,
        stage_vpn_control_operation,
    )
    from app.services.vpn_customer_view import may_read_connection
    from app.services.vpn_policy import (
        select_public_vpn_endpoint,
        validate_subscription_access,
    )
    from app.services.vpn_provisioning import build_vpn_client_email

    try:
        async with db.begin_nested():
            customer, subscription, device, keys = await lock_owned_device(db, customer_id, device_id)
            from app.services.vpn_device_operations import pending_device_operation

            if (await pending_device_operation(db, device.id) is not None
                    or device.revoke_requested_at is not None or validate_subscription_access(
                subscription, customer, device_slots=0, enforce_device_limit=False, now=now,
            )):
                raise ValueError("customer_location_unavailable")
            child = next((key for key in keys if (key.device_id, key.subscription_id, key.endpoint_id,
                                                 key.credential_generation)
                          == (device.id, subscription.id, endpoint_id, device.credential_generation)), None)
            if child is not None and (child.status in {"revoked", "pending_revoke"}
                                      or child.revoked_at is not None or child.revoke_requested_at is not None):
                raise ValueError("customer_location_unavailable")
            if child is not None:
                operation = await db.scalar(select(VpnControlOperation).where(
                    VpnControlOperation.access_key_id == child.id,
                    VpnControlOperation.generation == child.operation_generation,
                ))
                if operation is not None and operation.state == "uncertain":
                    raise ValueError("customer_location_verification_required")
            selected = await select_public_vpn_endpoint(
                db, endpoint_id=endpoint_id, require_published=True,
                reusable_key_id=child.id if child is not None else None,
                now=now, health_max_age_seconds=settings.vpn_endpoint_health_max_age_seconds, lock=True,
            )
            if selected is None or selected.endpoint.protocol != "vless":
                raise ValueError("customer_location_unavailable")
            endpoint = selected.endpoint
            worker = await db.get(WorkerNode, endpoint.worker_id)
            load_transport_snapshot(worker, Path(settings.vpn_control_known_hosts_path))
            if child is not None and may_read_connection(customer, subscription, child, now):
                device.preferred_key_id, device.pending_key_id = child.id, None
                device.updated_at = now
                await db.flush()
                return device
            if child is None:
                child = VpnAccessKey(
                    device_id=device.id, subscription_id=subscription.id,
                    credential_generation=device.credential_generation,
                    worker_id=endpoint.worker_id, endpoint_id=endpoint.id, protocol="vless",
                    external_uuid=str(uuid4()), panel_sub_id=uuid4().hex, display_name=device.display_name,
                    status="pending_sync", issued_at=now, expires_at=subscription.expires_at,
                )
                db.add(child)
                await db.flush()
                child.verified_client_email = build_vpn_client_email(child.id, None)
                await db.flush()
            device.pending_key_id, device.updated_at = child.id, now
            await db.flush()
            await stage_vpn_control_operation(db, child.id, "provision", now=now, retry_failed=True)
            return device
    except VpnControlIntentError as error:
        code = ("customer_location_verification_required" if error.code == "vpn_control_reconciliation_required"
                else "customer_location_unavailable")
        raise ValueError(code) from None
    except (VpnNodeTransportError, OSError, TypeError):
        raise ValueError("customer_location_unavailable") from None


async def promote_prepared_device_key(db: AsyncSession, key: VpnAccessKey, *, now: datetime) -> None:
    if key.device_id is None:
        return
    # Flush the authoritative receipt edits before refreshing only its parent.
    await db.flush()
    device = await db.scalar(select(VpnDevice).where(
        VpnDevice.id == key.device_id, VpnDevice.subscription_id == key.subscription_id,
    ).execution_options(populate_existing=True))
    if (device is None or device.revoke_requested_at is not None or device.pending_key_id != key.id
            or key.credential_generation != device.credential_generation
            or key.status != "active" or key.revoke_requested_at is not None
            or key.revoked_at is not None or not key.config_uri):
        return
    device.preferred_key_id, device.pending_key_id, device.updated_at = key.id, None, now
