from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
import re
from typing import Literal
from uuid import uuid4

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import (
    AppSetting,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnFriendInvitation,
    VpnPlan,
    VpnSubscription,
    WorkerNode,
)
from app.services.app_settings import _VPN_PUBLIC_RELEASE_READY_KEY
from app.services.vpn_control_intents import (
    VpnControlIntentError,
    stage_vpn_control_operation,
)
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    load_transport_snapshot,
)
from app.services.vpn_policy import select_public_vpn_endpoint
from app.services.vpn_telegram_identity import (
    TelegramIdentity,
    resolve_telegram_customer,
    telegram_user_id,
)


TrialState = Literal[
    "disabled", "available", "capacity_paused", "preparing", "active", "used"
]


@dataclass(frozen=True, slots=True)
class PublicTrialView:
    state: TrialState
    duration_days: int = 7
    profile_limit: int = 1
    subscription_id: int | None = None
    access_key_id: int | None = None
    expires_at: datetime | None = None


class PublicTrialUnavailable(RuntimeError):
    pass


class PublicTrialConflict(RuntimeError):
    pass


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _unavailable() -> None:
    raise PublicTrialUnavailable("public_trial_unavailable") from None


async def _existing_trial(
    db: AsyncSession, customer: VpnCustomer, now: datetime
) -> PublicTrialView | None:
    # An invitation owns its chain even if its redemption metadata is damaged.
    # Fail closed instead of reclassifying any friend key as a public replay.
    friend_slot = await db.scalar(
        select(VpnFriendInvitation.slot)
        .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
        .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
        .where(VpnSubscription.customer_id == customer.id)
        .limit(1)
    )
    if friend_slot is not None:
        return PublicTrialView("used")
    trial_filter = VpnSubscription.status == "trial"
    if customer.trial_started_at is not None:
        trial_filter = or_(
            trial_filter, VpnSubscription.starts_at == customer.trial_started_at
        )
    row = (
        await db.execute(
            select(VpnSubscription, VpnAccessKey)
            .outerjoin(VpnAccessKey, VpnAccessKey.subscription_id == VpnSubscription.id)
            .where(VpnSubscription.customer_id == customer.id, trial_filter)
            .order_by(VpnSubscription.id.desc(), VpnAccessKey.id.desc())
            .limit(1)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        return (
            PublicTrialView("used") if customer.trial_started_at is not None else None
        )
    subscription, key = row
    expires = (
        _utc(subscription.expires_at) if subscription.expires_at is not None else None
    )
    state: TrialState = "preparing"
    if (
        customer.status != "active"
        or subscription.status != "trial"
        or expires is None
        or expires <= now
        or key is None
        or key.status
        in {"disabled", "revoked", "suspended", "pending_revoke", "pending_suspend"}
        or key.revoked_at is not None
        or key.revoke_requested_at is not None
        or (key.expires_at is not None and _utc(key.expires_at) <= now)
    ):
        state = "used"
    else:
        operation = await db.scalar(
            select(VpnControlOperation)
            .join(VpnAccessKey, VpnAccessKey.id == VpnControlOperation.access_key_id)
            .where(
                VpnAccessKey.id == key.id,
                VpnControlOperation.generation == VpnAccessKey.operation_generation,
                VpnAccessKey.operation_generation > 0,
            )
            .execution_options(populate_existing=True)
        )
        if (
            operation is not None
            and operation.state == "succeeded"
            and operation.action == "provision"
            and key.status == "active"
            and key.config_uri
        ):
            state = "active"
    return PublicTrialView(
        state,
        subscription_id=subscription.id,
        access_key_id=key.id if key is not None else None,
        expires_at=expires,
    )


async def _ready(db: AsyncSession, settings: Settings, now: datetime, *, lock: bool):
    if (
        not settings.vpn_public_trial_enabled
        or not settings.vpn_control_dispatch_enabled
        or re.fullmatch(r"[0-9a-f]{64}", settings.vpn_public_trial_release_id) is None
    ):
        _unavailable()
    marker = await db.scalar(
        select(AppSetting.value).where(AppSetting.key == _VPN_PUBLIC_RELEASE_READY_KEY)
    )
    if marker != settings.vpn_public_trial_release_id:
        _unavailable()
    plan = await db.scalar(
        select(VpnPlan)
        .where(
            VpnPlan.slug == settings.vpn_public_trial_plan_slug,
            VpnPlan.is_active.is_(True),
            VpnPlan.duration_days == 7,
            VpnPlan.max_devices == 1,
        )
        .execution_options(populate_existing=True)
    )
    if plan is None:
        _unavailable()
    selected = await select_public_vpn_endpoint(
        db,
        now=now,
        health_max_age_seconds=settings.vpn_endpoint_health_max_age_seconds,
        lock=lock,
    )
    if selected is None:
        _unavailable()
    worker = await db.get(WorkerNode, selected.endpoint.worker_id)
    try:
        load_transport_snapshot(worker, Path(settings.vpn_control_known_hosts_path))
    except (VpnNodeTransportError, OSError, TypeError, ValueError):
        _unavailable()
    return plan, selected.endpoint


async def public_trial_status(
    db: AsyncSession,
    settings: Settings,
    customer: VpnCustomer,
    now: datetime,
) -> PublicTrialView:
    current = _utc(now)
    existing = await _existing_trial(db, customer, current)
    if existing is not None:
        return existing
    if not settings.vpn_public_trial_enabled:
        return PublicTrialView("disabled")
    try:
        await _ready(db, settings, current, lock=False)
    except PublicTrialUnavailable:
        return PublicTrialView("capacity_paused")
    return PublicTrialView("available")


async def activate_public_trial(
    db: AsyncSession,
    settings: Settings,
    identity: TelegramIdentity,
    now: datetime,
) -> PublicTrialView:
    """Stage exactly one trial; the caller owns commit and rollback."""
    current = _utc(now)
    try:
        telegram_user_id(identity.user_id)
    except ValueError:
        raise PublicTrialConflict("public_trial_conflict") from None
    customer = await resolve_telegram_customer(db, identity)
    customer = await db.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == customer.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    assert customer is not None
    existing = await _existing_trial(db, customer, current)
    if existing is not None:
        if existing.state == "used":
            raise PublicTrialConflict("public_trial_conflict")
        return existing
    # Resolver has flushed, and no trial rows/marker exist before capacity allocation.
    plan, endpoint = await _ready(db, settings, current, lock=True)
    subscription = VpnSubscription(
        customer_id=customer.id,
        plan_id=plan.id,
        status="trial",
        starts_at=current,
        expires_at=current + timedelta(days=plan.duration_days),
        traffic_limit_gb=plan.traffic_limit_gb,
        max_devices=plan.max_devices,
    )
    db.add(subscription)
    await db.flush()
    key = VpnAccessKey(
        subscription_id=subscription.id,
        worker_id=endpoint.worker_id,
        endpoint_id=endpoint.id,
        protocol="vless",
        external_uuid=str(uuid4()),
        verified_client_email=f"veltrix-trial-{customer.id}",
        panel_sub_id=uuid4().hex,
        display_name="Veltrix VPN",
        config_uri=None,
        status="pending_sync",
        issued_at=current,
        expires_at=subscription.expires_at,
    )
    db.add(key)
    await db.flush()
    try:
        await stage_vpn_control_operation(db, key.id, "provision", now=current)
    except VpnControlIntentError:
        _unavailable()
    customer.trial_started_at = current
    await db.flush()
    return PublicTrialView(
        "preparing",
        subscription_id=subscription.id,
        access_key_id=key.id,
        expires_at=_utc(subscription.expires_at),
    )
