"""Durable customer device transitions; remote work stays in the existing control queue."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnControlOperation, VpnDevice, VpnDeviceOperation, WorkerNode
from app.services import vpn_devices
from app.services.vpn_control_intents import VpnControlIntentError, stage_vpn_control_operation
from app.services.vpn_display import validate_display_name
from app.services.vpn_node_transport import VpnNodeTransportError
from app.services.vpn_policy import count_device_slots, select_public_vpn_endpoint, validate_subscription_access
from app.services.vpn_provisioning import build_vpn_client_email


def _utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def owned_device_operation(db, customer_id, operation_id):
    operation = await db.scalar(select(VpnDeviceOperation).where(
        VpnDeviceOperation.id == str(operation_id), VpnDeviceOperation.customer_id == customer_id,
    ).execution_options(populate_existing=True))
    if operation is None:
        raise vpn_devices.MissingCustomerDevice()
    return operation


async def _replay(db, customer_id, request_id, identity):
    if not isinstance(request_id, UUID) or request_id.int == 0:
        raise ValueError("customer_device_request_invalid")
    operation = await db.scalar(select(VpnDeviceOperation).where(
        VpnDeviceOperation.customer_id == customer_id, VpnDeviceOperation.request_id == str(request_id),
    ).execution_options(populate_existing=True))
    if operation is not None and operation.request_identity != identity:
        raise ValueError("customer_device_request_conflict")
    return operation


async def pending_device_operation(db, device_id):
    return await db.scalar(select(VpnDeviceOperation).where(
        VpnDeviceOperation.device_id == device_id, VpnDeviceOperation.finished_at.is_(None),
    ).order_by(VpnDeviceOperation.created_at, VpnDeviceOperation.id).execution_options(populate_existing=True))


async def _endpoint(db, endpoint_id, settings, now, *, reusable_key_id=None):
    selected = await select_public_vpn_endpoint(
        db, endpoint_id=endpoint_id, require_published=True, reusable_key_id=reusable_key_id,
        now=now, health_max_age_seconds=settings.vpn_endpoint_health_max_age_seconds, lock=True,
    )
    if selected is None or selected.endpoint.protocol != "vless":
        raise ValueError("customer_device_endpoint_unavailable")
    worker = await db.get(WorkerNode, selected.endpoint.worker_id)
    try:
        vpn_devices.load_transport_snapshot(worker, Path(settings.vpn_control_known_hosts_path))
    except (VpnNodeTransportError, OSError, TypeError):
        raise ValueError("customer_device_endpoint_unavailable") from None
    return selected.endpoint


async def _prepare(db, device, subscription, endpoint, now):
    key = VpnAccessKey(
        device_id=device.id, credential_generation=device.credential_generation,
        subscription_id=subscription.id, worker_id=endpoint.worker_id, endpoint_id=endpoint.id,
        protocol="vless", external_uuid=str(uuid4()), panel_sub_id=uuid4().hex,
        display_name=device.display_name, status="pending_sync", issued_at=now, expires_at=subscription.expires_at,
    )
    db.add(key)
    await db.flush()
    key.verified_client_email = build_vpn_client_email(key.id, None)
    device.pending_key_id, device.updated_at = key.id, now
    await db.flush()
    await stage_vpn_control_operation(db, key.id, "provision", now=now, retry_failed=True)
    return key


def _operation(customer_id, request_id, device, action, stage, generation, endpoint_id, identity, now):
    return VpnDeviceOperation(
        id=str(uuid4()), customer_id=customer_id, request_id=str(request_id), device_id=device.id,
        action=action, stage=stage, credential_generation=generation, endpoint_id=endpoint_id,
        request_identity=identity, created_at=now, updated_at=now,
    )


async def request_device_creation(
    db: AsyncSession, *, customer_id: int, subscription_id: int, endpoint_id: int,
    display_name: str, request_id: UUID, settings: Settings, now: datetime,
) -> VpnDeviceOperation:
    name = validate_display_name(display_name)
    identity = dict(action="create", subscription_id=subscription_id, endpoint_id=endpoint_id, display_name=name)
    async with db.begin_nested():
        customer, subscriptions, _ = await vpn_devices.lock_customer_devices(db, customer_id)
        replay = await _replay(db, customer_id, request_id, identity)
        if replay is not None:
            return replay
        subscription = next((item for item in subscriptions if item.id == subscription_id), None)
        if customer is None or subscription is None:
            raise vpn_devices.MissingCustomerDevice()
        slots = await count_device_slots(db, subscription.id)
        error = validate_subscription_access(subscription, customer, device_slots=slots, now=now)
        if error:
            raise ValueError("customer_device_limit_reached" if slots >= subscription.max_devices
                             else "customer_device_subscription_unavailable")
        endpoint = await _endpoint(db, endpoint_id, settings, now)
        device = VpnDevice(subscription_id=subscription.id, display_name=name)
        db.add(device)
        await db.flush()
        operation = _operation(customer_id, request_id, device, "create", "provisioning", 0, endpoint.id, identity, now)
        db.add(operation)
        await db.flush()
        try:
            await _prepare(db, device, subscription, endpoint, now)
        except VpnControlIntentError:
            raise ValueError("customer_device_unavailable") from None
        return operation


async def request_device_operation(
    db: AsyncSession, *, customer_id: int, device_id: int, action: str,
    request_id: UUID, settings: Settings, now: datetime,
) -> VpnDeviceOperation:
    if action not in {"replace", "revoke"}:
        raise ValueError("customer_device_request_invalid")
    identity = dict(action=action, device_id=device_id)
    async with db.begin_nested():
        customer, subscription, device, keys = await vpn_devices.lock_owned_device(db, customer_id, device_id)
        replay = await _replay(db, customer_id, request_id, identity)
        if replay is not None:
            return replay
        pending = await pending_device_operation(db, device.id)
        if pending is not None and (action != "revoke" or pending.action == "revoke"):
            raise ValueError("customer_device_operation_pending")
        endpoint_id = None
        generation = device.credential_generation
        if action == "replace":
            if device.revoke_requested_at is not None or validate_subscription_access(
                subscription, customer, device_slots=0, enforce_device_limit=False, now=now,
            ):
                raise ValueError("customer_device_subscription_unavailable")
            recent = await db.scalar(select(VpnDeviceOperation.id).where(
                VpnDeviceOperation.device_id == device.id, VpnDeviceOperation.action == "replace",
                VpnDeviceOperation.stage == "completed", VpnDeviceOperation.finished_at > _utc(now) - timedelta(days=1),
            ))
            if recent is not None:
                raise ValueError("customer_device_replacement_limited")
            preferred = next((key for key in keys if key.id == device.preferred_key_id
                              and key.device_id == device.id and key.credential_generation == generation), None)
            if preferred is None or preferred.endpoint_id is None:
                raise ValueError("customer_device_endpoint_unavailable")
            await vpn_devices.lock_device_targets(db, [key for key in keys if key.device_id == device.id],
                                                  endpoint_id=preferred.endpoint_id)
            endpoint = await _endpoint(db, preferred.endpoint_id, settings, now, reusable_key_id=preferred.id)
            endpoint_id, generation = endpoint.id, generation + 1
        operation = _operation(customer_id, request_id, device, action, "revoking", generation, endpoint_id, identity, now)
        db.add(operation)
        if action == "revoke":
            await db.flush()
            anchor = next((key for key in keys if key.device_id == device.id), None)
            if anchor is not None:
                await vpn_devices.stage_device_revoke(db, anchor, now=now)
            else:
                if device.revoke_requested_at is None:
                    device.revoke_requested_at, device.token_generation = now, device.token_generation + 1
                device.pending_key_id, device.updated_at = None, now
        else:
            device.token_generation += 1
            device.preferred_key_id, device.pending_key_id, device.updated_at = None, None, now
            for key in keys:
                if key.device_id == device.id and key.status != "revoked":
                    key.status, key.updated_at = "pending_revoke", now
                    key.revoke_requested_at = key.revoke_requested_at or now
        await db.flush()
        await _advance(db, operation, customer, subscription, device, keys, settings, now)
        return operation


async def replacement_for_old_key(db, device, key):
    if device is None or device.revoke_requested_at is not None:
        return None
    return await db.scalar(select(VpnDeviceOperation).where(
        VpnDeviceOperation.device_id == device.id, VpnDeviceOperation.action == "replace",
        VpnDeviceOperation.finished_at.is_(None),
        VpnDeviceOperation.credential_generation > key.credential_generation,
    ).execution_options(populate_existing=True))


async def _advance(db, operation, customer, subscription, device, keys, settings, now):
    children = [key for key in keys if key.device_id == device.id and key.subscription_id == subscription.id]
    if operation.action != "revoke" and device.revoke_requested_at is not None:
        operation.stage, operation.public_error, operation.finished_at = "failed", "customer_device_revoked", now
        operation.updated_at = now
        await db.flush()
        return
    old = children if operation.action == "revoke" else [
        key for key in children if key.credential_generation < operation.credential_generation
    ]
    controls = list((await db.scalars(select(VpnControlOperation).where(
        VpnControlOperation.access_key_id.in_([key.id for key in children]),
    ).execution_options(populate_existing=True))).all())
    if any(control.state == "uncertain" for control in controls):
        operation.stage, operation.public_error, operation.updated_at = "uncertain", "customer_device_verification_required", now
        await db.flush()
        return
    try:
        await vpn_devices.lock_device_targets(db, children, endpoint_id=operation.endpoint_id)
    except ValueError:
        operation.stage, operation.public_error, operation.updated_at = "failed", "customer_device_endpoint_unavailable", now
        await db.flush()
        return
    for key in old:
        if key.status != "revoked":
            if key.endpoint_id is not None:
                try:
                    await stage_vpn_control_operation(
                        db, key.id, "revoke", now=now, retry_failed=True,
                        device_operation_id=operation.id if operation.action == "replace" else None,
                    )
                except VpnControlIntentError:
                    operation.stage, operation.public_error = "failed", "customer_device_revoke_pending"
                    operation.updated_at = now
                    await db.flush()
                    return
    if any(key.endpoint_id is None and key.status != "revoked" and (key.external_uuid or key.config_uri) for key in old):
        operation.stage, operation.public_error, operation.updated_at = "failed", "customer_device_revoke_pending", now
        await db.flush()
        return
    if any(key.status != "revoked" or key.revoked_at is None for key in old) or any(
        control.state == "claimed" for control in controls if control.access_key_id in {key.id for key in old}
    ):
        operation.stage, operation.public_error, operation.updated_at = "revoking", None, now
        await db.flush()
        return
    if operation.action == "revoke":
        operation.stage, operation.public_error, operation.finished_at, operation.updated_at = "completed", None, now, now
        await db.flush()
        return
    fresh = next((key for key in children if key.credential_generation == operation.credential_generation
                  and key.endpoint_id == operation.endpoint_id), None)
    if fresh is None:
        if validate_subscription_access(subscription, customer, device_slots=0, enforce_device_limit=False, now=now):
            operation.stage, operation.public_error, operation.updated_at = "failed", "customer_device_subscription_unavailable", now
            await db.flush()
            return
        try:
            endpoint = await _endpoint(db, operation.endpoint_id, settings, now)
            device.credential_generation = operation.credential_generation
            await db.flush()
            fresh = await _prepare(db, device, subscription, endpoint, now)
        except (VpnControlIntentError, ValueError):
            operation.stage, operation.public_error, operation.updated_at = "failed", "customer_device_endpoint_unavailable", now
            await db.flush()
            return
    control = next((item for item in controls if item.access_key_id == fresh.id
                    and item.generation == fresh.operation_generation), None)
    if control is None or control.state in {"failed", "superseded"}:
        try:
            await stage_vpn_control_operation(db, fresh.id, "provision", now=now, retry_failed=True)
        except VpnControlIntentError:
            operation.stage, operation.public_error, operation.updated_at = "failed", "customer_device_provision_pending", now
            await db.flush()
            return
    if (control is not None and control.action == "provision" and control.state == "succeeded"
            and fresh.status == "active" and fresh.config_uri and fresh.revoked_at is None
            and fresh.revoke_requested_at is None and device.credential_generation == fresh.credential_generation):
        await vpn_devices.promote_prepared_device_key(db, fresh, now=now)
        operation.stage, operation.public_error, operation.finished_at = "completed", None, now
    else:
        operation.stage, operation.public_error = "provisioning", None
    operation.updated_at = now
    await db.flush()


async def resume_device_operations(db: AsyncSession, *, settings: Settings | None = None,
                                   now: datetime | None = None, batch_size: int = 50) -> int:
    config, current = settings or get_settings(), now or utcnow()
    candidates = (await db.execute(select(VpnDeviceOperation.id, VpnDeviceOperation.customer_id,
                                         VpnDeviceOperation.device_id).where(
        VpnDeviceOperation.finished_at.is_(None),
    ).order_by(VpnDeviceOperation.updated_at, VpnDeviceOperation.id).limit(max(1, batch_size)))).all()
    for identity, customer_id, device_id in candidates:
        customer, subscription, device, keys = await vpn_devices.lock_owned_device(db, customer_id, device_id)
        operation = await owned_device_operation(db, customer_id, identity)
        if operation.finished_at is None:
            await _advance(db, operation, customer, subscription, device, keys, config, current)
        await db.commit()
    return len(candidates)
