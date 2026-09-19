from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnNodeEvent, VpnSubscription, WorkerNode
from app.services.app_settings import VPN_LIFECYCLE_LAST_RESULT_KEY, set_app_setting
from app.services.vpn_policy import (
    count_device_slots,
    evaluate_vpn_node,
    select_vpn_node,
    validate_subscription_access,
)
from app.services.vpn_provisioning import provision_vpn_access_key, revoke_vpn_access_key


EXPIRING_SUBSCRIPTION_STATUSES = ("active", "trial")
REVOKABLE_KEY_STATUSES = ("active", "pending_sync", "syncing", "pending_revoke")
TERMINAL_SUBSCRIPTION_STATUSES = ("expired", "cancelled", "disabled")

_vpn_lifecycle_lock = asyncio.Lock()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_key_error(exc: Exception, worker: WorkerNode | None) -> str:
    message = str(exc)
    if worker is not None:
        for secret in (worker.ssh_password, worker.vpn_panel_password):
            if secret:
                message = message.replace(secret, "<redacted>")
    return message[:2000]


def _new_result(current_time: datetime) -> dict[str, int | str]:
    return {
        "ran_at": current_time.isoformat(),
        "expired_subscriptions": 0,
        "checked_keys": 0,
        "provisioned_keys": 0,
        "revoked_keys": 0,
        "pending_sync_keys": 0,
        "pending_revoke_keys": 0,
        "skipped_unsafe_keys": 0,
        "failed_keys": 0,
    }


def _needs_revoke(access_key: VpnAccessKey, subscription: VpnSubscription, current_time: datetime) -> bool:
    expires_at = _as_utc(access_key.expires_at)
    return (
        access_key.status == "pending_revoke"
        or subscription.status in TERMINAL_SUBSCRIPTION_STATUSES
        or (expires_at is not None and expires_at <= current_time)
    )


async def _record_unsafe_skip(
    db: AsyncSession,
    access_key: VpnAccessKey,
    worker: WorkerNode,
    reason: str,
) -> None:
    db.add(
        VpnNodeEvent(
            worker_id=worker.id,
            level="warning",
            event_type="lifecycle_unsafe_skip",
            message="VPN lifecycle skipped an unsafe node",
            details={"access_key_id": access_key.id, "reason": reason[:2000]},
        )
    )


async def _process_revoke(
    db: AsyncSession,
    access_key: VpnAccessKey,
    result: dict[str, int | str],
    current_time: datetime,
) -> None:
    if access_key.worker_id is None:
        if not access_key.config_uri:
            access_key.status = "revoked"
            access_key.revoked_at = current_time
            access_key.last_error = None
            result["revoked_keys"] += 1
        else:
            access_key.status = "pending_revoke"
            access_key.last_error = "Assigned VPN node is missing; revoke is pending"
            result["pending_revoke_keys"] += 1
        return

    worker = await db.get(WorkerNode, access_key.worker_id)
    if worker is None:
        access_key.status = "pending_revoke"
        access_key.last_error = "Assigned VPN node was not found; revoke is pending"
        result["pending_revoke_keys"] += 1
        return

    eligibility = await evaluate_vpn_node(db, worker)
    if not eligibility.eligible:
        reason = "; ".join(eligibility.reasons)
        access_key.status = "pending_revoke"
        access_key.last_error = reason[:2000]
        result["pending_revoke_keys"] += 1
        result["skipped_unsafe_keys"] += 1
        await _record_unsafe_skip(db, access_key, worker, reason)
        return

    await revoke_vpn_access_key(db, access_key, worker=worker)
    if access_key.status == "revoked":
        result["revoked_keys"] += 1
    else:
        access_key.status = "pending_revoke"
        result["pending_revoke_keys"] += 1


async def _process_provision(
    db: AsyncSession,
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    result: dict[str, int | str],
    current_time: datetime,
) -> None:
    customer = await db.get(VpnCustomer, subscription.customer_id)
    slots = await count_device_slots(db, subscription.id, exclude_key_id=access_key.id)
    validation_error = validate_subscription_access(
        subscription,
        customer,
        device_slots=slots,
        now=current_time,
    )
    if validation_error:
        access_key.status = "pending_sync"
        access_key.last_error = validation_error
        result["pending_sync_keys"] += 1
        return

    worker = await select_vpn_node(db, worker_id=access_key.worker_id)
    if worker is None:
        access_key.status = "pending_sync"
        if access_key.worker_id is not None:
            assigned_worker = await db.get(WorkerNode, access_key.worker_id)
            if assigned_worker is not None:
                eligibility = await evaluate_vpn_node(db, assigned_worker)
                access_key.last_error = "; ".join(eligibility.reasons)[:2000]
                await _record_unsafe_skip(db, access_key, assigned_worker, access_key.last_error)
            else:
                access_key.last_error = "Assigned VPN node was not found"
        else:
            access_key.last_error = "No safe VPN node is currently available"
        result["pending_sync_keys"] += 1
        result["skipped_unsafe_keys"] += 1
        return

    access_key.worker_id = worker.id
    await provision_vpn_access_key(db, access_key, subscription=subscription, worker=worker)
    if access_key.status == "active":
        result["provisioned_keys"] += 1
    else:
        access_key.status = "pending_sync"
        result["pending_sync_keys"] += 1


async def _run_vpn_lifecycle_maintenance(
    db: AsyncSession,
    *,
    current_time: datetime,
    batch_size: int,
) -> dict[str, int | str]:
    result = _new_result(current_time)
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
    result["expired_subscriptions"] = len(expired_subscriptions)
    await db.flush()

    rows = (
        await db.execute(
            select(VpnAccessKey, VpnSubscription)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .where(
                or_(
                    VpnAccessKey.status == "pending_sync",
                    VpnAccessKey.status == "pending_revoke",
                    and_(
                        VpnAccessKey.status.in_(REVOKABLE_KEY_STATUSES),
                        or_(
                            and_(
                                VpnAccessKey.expires_at.is_not(None),
                                VpnAccessKey.expires_at <= current_time,
                            ),
                            VpnSubscription.status.in_(TERMINAL_SUBSCRIPTION_STATUSES),
                        ),
                    ),
                )
            )
            .order_by(VpnAccessKey.id.asc())
            .limit(batch_size)
        )
    ).all()

    for access_key, subscription in rows:
        result["checked_keys"] += 1
        worker = await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None
        revoke = _needs_revoke(access_key, subscription, current_time)
        try:
            if revoke:
                await _process_revoke(db, access_key, result, current_time)
            else:
                await _process_provision(db, access_key, subscription, result, current_time)
        except Exception as exc:
            access_key.last_error = _bounded_key_error(exc, worker)
            result["failed_keys"] += 1
            if revoke:
                access_key.status = "pending_revoke"
                result["pending_revoke_keys"] += 1
            else:
                access_key.status = "pending_sync"
                result["pending_sync_keys"] += 1
            if worker is not None:
                db.add(
                    VpnNodeEvent(
                        worker_id=worker.id,
                        level="error",
                        event_type="lifecycle_key_failed",
                        message="VPN lifecycle key transition failed",
                        details={"access_key_id": access_key.id, "error": access_key.last_error},
                    )
                )

    await set_app_setting(
        db,
        VPN_LIFECYCLE_LAST_RESULT_KEY,
        json.dumps(result, ensure_ascii=False, separators=(",", ":")),
    )
    await db.commit()
    return result


async def run_vpn_lifecycle_maintenance(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = 50,
) -> dict[str, int | str]:
    current_time = _as_utc(now or utcnow())
    assert current_time is not None
    async with _vpn_lifecycle_lock:
        return await _run_vpn_lifecycle_maintenance(
            db,
            current_time=current_time,
            batch_size=max(1, batch_size),
        )
