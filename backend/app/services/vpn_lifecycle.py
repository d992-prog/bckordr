from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json

from sqlalchemy import and_, case, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnNodeEvent, VpnSubscription, WorkerNode
from app.services.app_settings import VPN_LIFECYCLE_LAST_RESULT_KEY, set_app_setting
from app.services.vpn_policy import (
    count_device_slots,
    evaluate_vpn_node,
    lock_vpn_worker,
    lock_vpn_subscription,
    select_vpn_node,
    validate_subscription_access,
)
from app.services.vpn_provisioning import (
    provision_vpn_access_key,
    revoke_vpn_access_key,
    sanitize_vpn_error,
    suspend_vpn_access_key,
)
from app.services.vpn_mutations import vpn_mutation_lock
from app.services.vpn_subscription_sync import RESTORABLE_KEY_STATUSES, subscription_key_action


EXPIRING_SUBSCRIPTION_STATUSES = ("active", "trial")


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _bounded_key_error(
    exc: Exception,
    worker: WorkerNode | None,
    access_key: VpnAccessKey,
) -> str:
    return sanitize_vpn_error(exc, worker=worker, access_key=access_key)


def _new_result(current_time: datetime) -> dict[str, int | str]:
    return {
        "ran_at": current_time.isoformat(),
        "expired_subscriptions": 0,
        "checked_keys": 0,
        "provisioned_keys": 0,
        "revoked_keys": 0,
        "suspended_keys": 0,
        "pending_suspend_keys": 0,
        "pending_sync_keys": 0,
        "pending_revoke_keys": 0,
        "skipped_unsafe_keys": 0,
        "failed_keys": 0,
    }


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
    *,
    suspend: bool = False,
) -> None:
    completed = "suspended" if suspend else "revoked"
    pending = "pending_suspend" if suspend else "pending_revoke"
    operation = "suspension" if suspend else "revoke"
    if access_key.worker_id is None:
        if not access_key.config_uri:
            access_key.status = completed
            if not suspend:
                access_key.revoked_at = current_time
            access_key.last_error = None
            result[f"{completed}_keys"] += 1
        else:
            access_key.status = pending
            access_key.last_error = f"Assigned VPN node is missing; {operation} is pending"
            result[f"{pending}_keys"] += 1
        return

    worker = await lock_vpn_worker(db, access_key.worker_id)
    if worker is None:
        access_key.status = pending
        access_key.last_error = f"Assigned VPN node was not found; {operation} is pending"
        result[f"{pending}_keys"] += 1
        return

    eligibility = await evaluate_vpn_node(db, worker)
    if not eligibility.eligible:
        reason = "; ".join(eligibility.reasons)
        access_key.status = pending
        access_key.last_error = reason[:2000]
        result[f"{pending}_keys"] += 1
        result["skipped_unsafe_keys"] += 1
        await _record_unsafe_skip(db, access_key, worker, reason)
        return

    transition = suspend_vpn_access_key if suspend else revoke_vpn_access_key
    await transition(db, access_key, worker=worker)
    if access_key.status == completed:
        result[f"{completed}_keys"] += 1
    else:
        access_key.status = pending
        result[f"{pending}_keys"] += 1


async def _process_provision(
    db: AsyncSession,
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    result: dict[str, int | str],
    current_time: datetime,
) -> None:
    if access_key.worker_id is None and access_key.config_uri:
        access_key.status = "pending_sync"
        access_key.last_error = "Assigned VPN node is missing; automatic migration is not allowed"
        result["pending_sync_keys"] += 1
        return
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

    worker = await lock_vpn_worker(db, worker.id)
    if worker is None:
        access_key.status = "pending_sync"
        access_key.last_error = "Selected VPN node was not found"
        result["pending_sync_keys"] += 1
        result["skipped_unsafe_keys"] += 1
        return
    eligibility = await evaluate_vpn_node(db, worker)
    if not eligibility.eligible:
        reason = "; ".join(eligibility.reasons)
        access_key.status = "pending_sync"
        access_key.last_error = reason[:2000]
        result["pending_sync_keys"] += 1
        result["skipped_unsafe_keys"] += 1
        await _record_unsafe_skip(db, access_key, worker, reason)
        return

    access_key.worker_id = worker.id
    await provision_vpn_access_key(db, access_key, subscription=subscription, worker=worker)
    if access_key.status == "active":
        result["provisioned_keys"] += 1
    else:
        access_key.status = "pending_sync"
        access_key.last_error = sanitize_vpn_error(
            access_key.last_error or "VPN provisioning failed",
            worker=worker,
            access_key=access_key,
        )
        if worker.vpn_last_error:
            worker.vpn_last_error = sanitize_vpn_error(
                worker.vpn_last_error,
                worker=worker,
                access_key=access_key,
            )
        result["pending_sync_keys"] += 1


async def _run_vpn_lifecycle_maintenance(
    db: AsyncSession,
    *,
    current_time: datetime,
    batch_size: int,
    key_timeout_seconds: float,
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

    permanent_policy = or_(VpnSubscription.status == "cancelled", VpnCustomer.status == "archived")
    suspend_policy = or_(
        VpnSubscription.status.not_in(EXPIRING_SUBSCRIPTION_STATUSES),
        VpnSubscription.starts_at > current_time,
        VpnSubscription.expires_at <= current_time,
        VpnCustomer.status != "active",
        VpnCustomer.id.is_(None),
    )
    # SQL NULL timestamps mean unrestricted, not an unknown eligibility result.
    usable_policy = and_(
        VpnSubscription.status.in_(EXPIRING_SUBSCRIPTION_STATUSES),
        or_(VpnSubscription.starts_at.is_(None), VpnSubscription.starts_at <= current_time),
        or_(VpnSubscription.expires_at.is_(None), VpnSubscription.expires_at > current_time),
        VpnCustomer.status == "active",
    )
    disable_condition = or_(
        VpnAccessKey.status == "pending_revoke",
        and_(
            VpnAccessKey.status.in_(RESTORABLE_KEY_STATUSES),
            permanent_policy,
        ),
        and_(
            VpnAccessKey.status.in_(tuple(s for s in RESTORABLE_KEY_STATUSES if s != "suspended")),
            suspend_policy,
        ),
    )
    query = (
        select(VpnAccessKey, VpnSubscription)
        .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
        .outerjoin(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
    )
    revoke_rows = (
        await db.execute(
            query.where(disable_condition)
            .order_by(
                case((VpnAccessKey.status.in_(("pending_revoke", "pending_suspend")), 1), else_=0).asc(),
                VpnAccessKey.updated_at.asc(),
                VpnAccessKey.id.asc(),
            )
            .limit(batch_size)
        )
    ).all()
    remaining = batch_size - len(revoke_rows)
    provision_rows = []
    if remaining > 0:
        provision_rows = (
            await db.execute(
                query.where(
                    usable_policy,
                    VpnAccessKey.status.in_(("pending_sync", "pending_suspend", "suspended")),
                )
                .order_by(VpnAccessKey.updated_at.asc(), VpnAccessKey.id.asc())
                .limit(remaining)
            )
        ).all()
    rows = [*revoke_rows, *provision_rows]

    for access_key, subscription in rows:
        subscription = await lock_vpn_subscription(db, subscription.id)
        await db.refresh(access_key)
        if subscription is None or access_key.status == "revoked":
            continue
        customer = await db.get(VpnCustomer, subscription.customer_id)
        action = (
            "revoke" if access_key.status == "pending_revoke"
            else subscription_key_action(subscription, customer, current_time)
        )
        result["checked_keys"] += 1
        worker = await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None
        try:
            try:
                if action in {"revoke", "suspend"}:
                    await asyncio.wait_for(
                        _process_revoke(db, access_key, result, current_time, suspend=action == "suspend"),
                        timeout=key_timeout_seconds,
                    )
                else:
                    await asyncio.wait_for(
                        _process_provision(db, access_key, subscription, result, current_time),
                        timeout=key_timeout_seconds,
                    )
            except TimeoutError as exc:
                raise RuntimeError(
                    f"VPN lifecycle key operation timed out after {key_timeout_seconds:g} seconds"
                ) from exc
        except Exception as exc:
            access_key.last_error = _bounded_key_error(exc, worker, access_key)
            result["failed_keys"] += 1
            access_key.status = f"pending_{action}"
            result[f"pending_{action}_keys"] += 1
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
        access_key.updated_at = current_time

        # Every remote transition is its own durable checkpoint. A cycle-level
        # timeout may cancel a later SSH call, but it must never roll back keys
        # whose external 3x-UI operation has already completed.
        await set_app_setting(
            db,
            VPN_LIFECYCLE_LAST_RESULT_KEY,
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
        await db.commit()

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
    key_timeout_seconds: float = 30.0,
) -> dict[str, int | str]:
    async with vpn_mutation_lock():
        current_time = _as_utc(now or utcnow())
        assert current_time is not None
        return await _run_vpn_lifecycle_maintenance(
            db,
            current_time=current_time,
            batch_size=max(1, batch_size),
            key_timeout_seconds=max(0.01, key_timeout_seconds),
        )
