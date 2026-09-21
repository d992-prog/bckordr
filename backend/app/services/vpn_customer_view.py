from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.schemas.vpn_portal import (
    PortalConnection,
    PortalProfile,
    PortalSubscription,
)
from app.services.vpn_display import (
    InvalidVpnDisplay,
    display_uri,
    validate_display_name,
)
from app.services.vpn_policy import DEVICE_SLOT_STATUSES


class MissingCustomerProfile(LookupError):
    """The requested profile is absent from the customer's owned profiles."""

    def __init__(self) -> None:
        super().__init__("customer_profile_not_found")


class UnavailableCustomerConnection(RuntimeError):
    """The owned profile has no currently usable, safe connection."""

    def __init__(self) -> None:
        super().__init__("customer_connection_unavailable")


class _SubscriptionLike(Protocol):
    status: str
    starts_at: datetime | None
    expires_at: datetime | None


class _CustomerLike(Protocol):
    status: str


class _AccessKeyLike(Protocol):
    status: str
    expires_at: datetime | None
    config_uri: str | None
    display_name: str | None


_SUBSCRIPTION_STATES = {
    "expired": "expired",
    "disabled": "disabled",
    "cancelled": "cancelled",
}
_PROFILE_STATES = frozenset((*DEVICE_SLOT_STATUSES, "revoked"))


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _current_time(now: datetime | None) -> datetime:
    current = _as_utc(now or utcnow())
    assert current is not None
    return current


def subscription_state(
    subscription: _SubscriptionLike,
    now: datetime,
) -> str:
    """Return the safe customer-visible subscription state."""
    if subscription.status not in {"active", "trial"}:
        return _SUBSCRIPTION_STATES.get(subscription.status, "unavailable")

    current_time = _current_time(now)
    starts_at = _as_utc(subscription.starts_at)
    expires_at = _as_utc(subscription.expires_at)
    if starts_at is not None and starts_at > current_time:
        return "scheduled"
    if expires_at is not None and expires_at <= current_time:
        return "expired"
    return subscription.status


def profile_display_name(access_key: _AccessKeyLike) -> str:
    """Return the validated customer-facing name shared by all VPN surfaces."""
    try:
        if access_key.display_name is not None:
            return validate_display_name(access_key.display_name)
    except InvalidVpnDisplay:
        pass
    return "Профиль"


def _profile_state(access_key: _AccessKeyLike) -> str:
    if access_key.status in _PROFILE_STATES:
        return access_key.status
    return "unavailable"


def may_read_connection(
    customer: _CustomerLike,
    subscription: _SubscriptionLike,
    key: _AccessKeyLike,
    now: datetime,
) -> bool:
    """Return whether an already-issued profile is currently readable."""
    current_time = _current_time(now)
    key_expires_at = _as_utc(key.expires_at)
    if (
        customer.status != "active"
        or subscription_state(subscription, current_time) not in {"active", "trial"}
        or key.status != "active"
        or not key.config_uri
        or (key_expires_at is not None and key_expires_at <= current_time)
    ):
        return False

    try:
        display_uri(key.config_uri, profile_display_name(key))
    except InvalidVpnDisplay:
        return False
    return True


async def _current_customer(
    db: AsyncSession,
    customer_id: int,
) -> VpnCustomer | None:
    return await db.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == customer_id)
        .execution_options(populate_existing=True)
    )


def _owned_profile_query(customer_id: int, profile_id: int):
    return (
        select(VpnAccessKey, VpnSubscription)
        .join(
            VpnSubscription,
            VpnSubscription.id == VpnAccessKey.subscription_id,
        )
        .where(
            VpnAccessKey.id == profile_id,
            VpnSubscription.customer_id == customer_id,
        )
        .execution_options(populate_existing=True)
    )


def _portal_profile(
    customer: _CustomerLike,
    subscription: VpnSubscription,
    access_key: VpnAccessKey,
    now: datetime,
) -> PortalProfile:
    return PortalProfile(
        id=access_key.id,
        subscription_id=subscription.id,
        display_name=profile_display_name(access_key),
        state=_profile_state(access_key),
        can_connect=may_read_connection(customer, subscription, access_key, now),
    )


async def list_customer_subscriptions(
    db: AsyncSession,
    customer_id: int,
    now: datetime | None = None,
) -> list[PortalSubscription]:
    """List only subscriptions owned by the requested customer."""
    reserved_profiles = (
        select(func.count(VpnAccessKey.id))
        .where(
            VpnAccessKey.subscription_id == VpnSubscription.id,
            VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
        )
        .correlate(VpnSubscription)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(VpnSubscription, reserved_profiles)
            .where(VpnSubscription.customer_id == customer_id)
            .execution_options(populate_existing=True)
        )
    ).all()
    current_time = _current_time(now)
    subscriptions = [
        PortalSubscription(
            id=subscription.id,
            state=subscription_state(subscription, current_time),
            starts_at=_as_utc(subscription.starts_at),
            expires_at=_as_utc(subscription.expires_at),
            profile_limit=subscription.max_devices,
            profiles_used=int(profiles_used),
            traffic_limit_gb_per_profile=subscription.traffic_limit_gb,
        )
        for subscription, profiles_used in rows
    ]
    subscriptions.sort(
        key=lambda item: (
            item.state not in {"active", "trial"},
            item.expires_at is None,
            item.expires_at or datetime.max.replace(tzinfo=UTC),
            item.id,
        )
    )
    return subscriptions


async def list_customer_profiles(
    db: AsyncSession,
    customer: VpnCustomer,
    now: datetime | None = None,
) -> list[PortalProfile]:
    """List safe profile metadata for the current customer."""
    current_customer = await _current_customer(db, customer.id)
    if current_customer is None:
        return []
    rows = (
        await db.execute(
            select(VpnAccessKey, VpnSubscription)
            .join(
                VpnSubscription,
                VpnSubscription.id == VpnAccessKey.subscription_id,
            )
            .where(VpnSubscription.customer_id == customer.id)
            .order_by(VpnAccessKey.id.asc())
            .execution_options(populate_existing=True)
        )
    ).all()
    current_time = _current_time(now)
    return [
        _portal_profile(current_customer, subscription, access_key, current_time)
        for access_key, subscription in rows
    ]


async def customer_connection(
    db: AsyncSession,
    customer: VpnCustomer,
    profile_id: int,
    now: datetime | None = None,
) -> PortalConnection:
    """Return a safely relabelled connection for an owned, usable profile."""
    row = (await db.execute(_owned_profile_query(customer.id, profile_id))).first()
    if row is None:
        raise MissingCustomerProfile()
    access_key, subscription = row
    current_customer = await _current_customer(db, customer.id)
    current_time = _current_time(now)
    if current_customer is None or not may_read_connection(
        current_customer, subscription, access_key, current_time
    ):
        raise UnavailableCustomerConnection()

    try:
        uri = display_uri(access_key.config_uri, profile_display_name(access_key))
    except InvalidVpnDisplay:
        raise UnavailableCustomerConnection() from None
    return PortalConnection(uri=uri)


async def rename_customer_profile(
    db: AsyncSession,
    customer: VpnCustomer,
    profile_id: int,
    display_name: str,
    now: datetime | None = None,
) -> PortalProfile:
    """Rename one owned profile locally; the caller controls the transaction."""
    row = (
        await db.execute(
            _owned_profile_query(customer.id, profile_id).with_for_update(
                of=VpnAccessKey
            )
        )
    ).first()
    if row is None:
        raise MissingCustomerProfile()
    access_key, subscription = row
    access_key.display_name = validate_display_name(display_name)
    await db.flush()

    current_customer = await _current_customer(db, customer.id)
    current_time = _current_time(now)
    return _portal_profile(
        current_customer or customer,
        subscription,
        access_key,
        current_time,
    )
