from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import re
import secrets
from typing import Literal
from uuid import uuid4

from sqlalchemy import and_, func, select, true, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import (
    AppSetting,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnCustomerSession,
    VpnEndpoint,
    VpnFriendInvitation,
    VpnSubscription,
    WorkerNode,
)
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    load_transport_snapshot,
)
from app.services.vpn_control_intents import (
    VpnControlIntentError,
    stage_vpn_control_operation,
)
from app.services.vpn_telegram_identity import (
    TelegramIdentity,
    resolve_telegram_customer,
    telegram_user_id,
)


INVITE_LIFETIME = timedelta(days=7)
TRIAL_LIFETIME = timedelta(days=7)
TOKEN_PREFIX = "veltrix-friend-invite-v1\0"
BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")
INVITE_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)

_VPN_FRIEND_BETA_READINESS_KEY = "vpn_friend_beta_release_ready_v1"
_RELEASE_ID = re.compile(r"[0-9a-f]{64}")
_RETRYABLE_ERRORS = frozenset(
    {"vpn_node_preflight_failed", "vpn_node_interrupted_before_mutation"}
)

InviteState = Literal[
    "unused",
    "preparing",
    "active",
    "failed",
    "needs_verification",
    "expired",
    "disabled",
]


class FriendInvitationError(RuntimeError):
    pass


class FriendInvitationUnavailable(FriendInvitationError):
    pass


class FriendInvitationConflict(FriendInvitationError):
    pass


@dataclass(frozen=True)
class FriendInvitationView:
    slot: int
    invite_state: InviteState
    telegram_user_id: str | None
    telegram_username: str | None
    display_name: str | None
    subscription_expires_at: datetime | None
    provisioning_error_code: str | None
    can_rotate: bool
    can_retry: bool
    can_disable: bool


@dataclass(frozen=True)
class IssuedFriendInvitation:
    view: FriendInvitationView
    link: str = field(repr=False)


@dataclass(frozen=True)
class RedeemedFriendInvitation:
    customer_id: int
    subscription_id: int
    access_key_id: int
    external_uuid: str = field(repr=False)
    starts_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class _FriendBinding:
    access_key_id: int
    subscription_id: int
    customer_id: int
    telegram_user_id: str
    revoked_at: datetime | None


def digest_invite_token(token: str) -> str:
    return hashlib.sha256((TOKEN_PREFIX + token).encode("ascii")).hexdigest()


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _current_time(value: datetime) -> datetime:
    current = _as_utc(value)
    assert current is not None
    return current


def _unavailable() -> None:
    raise FriendInvitationUnavailable("friend_beta_unavailable") from None


def _rejected() -> None:
    raise FriendInvitationConflict("friend_invitation_rejected") from None


def _bot_username(settings: Settings) -> str:
    username = settings.vpn_telegram_bot_username
    if BOT_USERNAME.fullmatch(username) is None or not username.lower().endswith("bot"):
        _unavailable()
    return username


async def require_friend_invitation_readiness(
    db: AsyncSession,
    settings: Settings,
) -> VpnEndpoint:
    release_id = settings.vpn_friend_beta_release_id
    if (
        not settings.vpn_friend_beta_enabled
        or not settings.vpn_control_dispatch_enabled
        or settings.vpn_portal_public_access
        or _RELEASE_ID.fullmatch(release_id) is None
    ):
        _unavailable()

    row = (
        await db.execute(
            select(AppSetting.value, VpnEndpoint, WorkerNode)
            .select_from(AppSetting)
            .join(VpnEndpoint, true())
            .join(WorkerNode, WorkerNode.id == VpnEndpoint.worker_id)
            .where(
                AppSetting.key == _VPN_FRIEND_BETA_READINESS_KEY,
                VpnEndpoint.status == "ready",
                VpnEndpoint.security == "reality",
                VpnEndpoint.verified_at.is_not(None),
                WorkerNode.archived_at.is_(None),
            )
            .order_by(VpnEndpoint.id.asc())
            .limit(1)
        )
    ).first()
    if row is None:
        _unavailable()

    marker, endpoint, worker = row
    if marker != release_id:
        _unavailable()
    try:
        load_transport_snapshot(
            worker,
            Path(settings.vpn_control_known_hosts_path),
        )
    except VpnNodeTransportError:
        _unavailable()
    return endpoint


def _empty_view(slot: int) -> FriendInvitationView:
    return FriendInvitationView(
        slot=slot,
        invite_state="unused",
        telegram_user_id=None,
        telegram_username=None,
        display_name=None,
        subscription_expires_at=None,
        provisioning_error_code=None,
        can_rotate=False,
        can_retry=False,
        can_disable=False,
    )


def _display_name(customer: VpnCustomer) -> str:
    full_name = " ".join(
        part.strip()
        for part in (customer.first_name, customer.last_name)
        if part and part.strip()
    )
    if full_name:
        return full_name
    if customer.telegram_username:
        return f"@{customer.telegram_username.strip().lstrip('@')}"
    return "Клиент Veltrix VPN"


def _has_exact_chain(
    invitation: VpnFriendInvitation,
    access_key: VpnAccessKey | None,
    subscription: VpnSubscription | None,
    customer: VpnCustomer | None,
) -> bool:
    return bool(
        invitation.redeemed_at is not None
        and invitation.telegram_user_id
        and access_key is not None
        and invitation.access_key_id == access_key.id
        and subscription is not None
        and access_key.subscription_id == subscription.id
        and customer is not None
        and subscription.customer_id == customer.id
        and invitation.telegram_user_id == customer.telegram_user_id
    )


def _state(
    invitation: VpnFriendInvitation,
    access_key: VpnAccessKey | None,
    subscription: VpnSubscription | None,
    customer: VpnCustomer | None,
    operation: VpnControlOperation | None,
    now: datetime,
) -> InviteState:
    if invitation.redeemed_at is None:
        if invitation.revoked_at is not None:
            return "disabled"
        expires_at = _as_utc(invitation.redeem_expires_at)
        return "expired" if expires_at is not None and expires_at <= now else "unused"
    if not _has_exact_chain(invitation, access_key, subscription, customer):
        return "failed"
    if invitation.revoked_at is not None:
        return "disabled"

    assert access_key is not None
    assert subscription is not None
    assert customer is not None
    if (
        customer.status != "active"
        or subscription.status in {"disabled", "cancelled"}
        or access_key.status in {"pending_revoke", "revoked"}
        or access_key.revoked_at is not None
    ):
        return "disabled"

    starts_at = _as_utc(subscription.starts_at)
    expires_at = _as_utc(subscription.expires_at)
    if subscription.status == "expired" or (expires_at is not None and expires_at <= now):
        return "expired"
    if operation is None:
        return "failed"
    if operation.state == "uncertain":
        return "needs_verification"
    if operation.state == "failed":
        return "failed"
    if operation.state in {"queued", "claimed"} or (
        starts_at is not None and starts_at > now
    ):
        return "preparing"
    if (
        operation.state == "succeeded"
        and operation.action == "provision"
        and subscription.status in {"active", "trial"}
        and access_key.status == "active"
        and bool(access_key.config_uri)
    ):
        return "active"
    return "failed"


def _view(
    invitation: VpnFriendInvitation,
    access_key: VpnAccessKey | None,
    subscription: VpnSubscription | None,
    customer: VpnCustomer | None,
    operation: VpnControlOperation | None,
    now: datetime,
) -> FriendInvitationView:
    exact_chain = _has_exact_chain(invitation, access_key, subscription, customer)
    invite_state = _state(
        invitation,
        access_key,
        subscription,
        customer,
        operation,
        now,
    )
    retryable = bool(
        exact_chain
        and invite_state == "failed"
        and operation is not None
        and operation.action == "provision"
        and operation.state == "failed"
        and operation.error_code in _RETRYABLE_ERRORS
    )
    return FriendInvitationView(
        slot=invitation.slot,
        invite_state=invite_state,
        telegram_user_id=invitation.telegram_user_id,
        telegram_username=customer.telegram_username if exact_chain else None,
        display_name=_display_name(customer) if exact_chain and customer is not None else None,
        subscription_expires_at=(
            _as_utc(subscription.expires_at)
            if exact_chain and subscription is not None
            else None
        ),
        provisioning_error_code=(
            operation.error_code
            if exact_chain and operation is not None
            else None
        ),
        can_rotate=invitation.redeemed_at is None and invitation.revoked_at is None,
        can_retry=retryable,
        can_disable=exact_chain and invitation.revoked_at is None,
    )


def _unredeemed_view(invitation: VpnFriendInvitation, now: datetime) -> FriendInvitationView:
    return _view(invitation, None, None, None, None, now)


async def list_friend_invitations(
    db: AsyncSession,
    now: datetime,
) -> list[FriendInvitationView]:
    latest_generation = (
        select(func.max(VpnControlOperation.generation))
        .where(VpnControlOperation.access_key_id == VpnAccessKey.id)
        .correlate(VpnAccessKey)
        .scalar_subquery()
    )
    rows = (
        await db.execute(
            select(
                VpnFriendInvitation,
                VpnAccessKey,
                VpnSubscription,
                VpnCustomer,
                VpnControlOperation,
            )
            .outerjoin(
                VpnAccessKey,
                VpnAccessKey.id == VpnFriendInvitation.access_key_id,
            )
            .outerjoin(
                VpnSubscription,
                VpnSubscription.id == VpnAccessKey.subscription_id,
            )
            .outerjoin(
                VpnCustomer,
                VpnCustomer.id == VpnSubscription.customer_id,
            )
            .outerjoin(
                VpnControlOperation,
                and_(
                    VpnControlOperation.access_key_id == VpnAccessKey.id,
                    VpnControlOperation.generation == latest_generation,
                ),
            )
            .order_by(VpnFriendInvitation.slot.asc())
            .execution_options(populate_existing=True)
        )
    ).all()
    current = _current_time(now)
    issued = {
        invitation.slot: _view(
            invitation,
            access_key,
            subscription,
            customer,
            operation,
            current,
        )
        for invitation, access_key, subscription, customer, operation in rows
    }
    return [issued.get(slot, _empty_view(slot)) for slot in range(1, 11)]


async def _discover_friend_binding(
    db: AsyncSession,
    slot: int,
) -> _FriendBinding:
    row = (
        await db.execute(
            select(
                VpnFriendInvitation.access_key_id,
                VpnAccessKey.subscription_id,
                VpnSubscription.customer_id,
                VpnFriendInvitation.telegram_user_id,
                VpnFriendInvitation.revoked_at,
            )
            .select_from(VpnFriendInvitation)
            .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(
                VpnFriendInvitation.slot == slot,
                VpnFriendInvitation.redeemed_at.is_not(None),
                VpnFriendInvitation.telegram_user_id == VpnCustomer.telegram_user_id,
            )
        )
    ).one_or_none()
    if row is None:
        raise FriendInvitationConflict("friend_invitation_not_actionable") from None
    return _FriendBinding(*row)


async def _locked_friend_chain(
    db: AsyncSession,
    slot: int,
    binding: _FriendBinding,
) -> tuple[VpnFriendInvitation, VpnAccessKey, VpnSubscription, VpnCustomer]:
    row = (
        await db.execute(
            select(VpnFriendInvitation, VpnAccessKey, VpnSubscription, VpnCustomer)
            .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(
                VpnFriendInvitation.slot == slot,
                VpnFriendInvitation.access_key_id == binding.access_key_id,
                VpnAccessKey.subscription_id == binding.subscription_id,
                VpnSubscription.customer_id == binding.customer_id,
                VpnFriendInvitation.telegram_user_id == binding.telegram_user_id,
                VpnCustomer.telegram_user_id == binding.telegram_user_id,
            )
            .with_for_update(of=VpnFriendInvitation)
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        raise FriendInvitationConflict("friend_invitation_not_actionable") from None
    return row


async def _friend_operation(
    db: AsyncSession,
    access_key: VpnAccessKey,
) -> VpnControlOperation | None:
    return await db.scalar(
        select(VpnControlOperation).where(
            VpnControlOperation.access_key_id == access_key.id,
            VpnControlOperation.generation == access_key.operation_generation,
        )
    )


def _friend_control_error(error: VpnControlIntentError) -> None:
    code = {
        "vpn_control_reconciliation_required": "friend_invitation_reconciliation_required",
        "vpn_control_retry_required": "friend_invitation_retry_required",
    }.get(error.code, "friend_invitation_not_actionable")
    raise FriendInvitationConflict(code) from None


async def retry_friend_invitation(
    db: AsyncSession,
    slot: int,
    now: datetime,
) -> FriendInvitationView:
    current = _current_time(now)
    async with db.begin_nested():
        binding = await _discover_friend_binding(db, slot)
        if binding.revoked_at is not None:
            raise FriendInvitationConflict("friend_invitation_not_actionable") from None
        access_key = await db.get(VpnAccessKey, binding.access_key_id)
        if access_key is None:
            raise FriendInvitationConflict("friend_invitation_not_actionable") from None
        prior = await _friend_operation(db, access_key)
        if not (
            prior is not None
            and prior.action == "provision"
            and prior.state == "failed"
            and prior.error_code in _RETRYABLE_ERRORS
        ):
            if prior is not None and prior.state == "uncertain":
                raise FriendInvitationConflict(
                    "friend_invitation_reconciliation_required"
                ) from None
            raise FriendInvitationConflict("friend_invitation_retry_required") from None
        try:
            operation = await stage_vpn_control_operation(
                db,
                binding.access_key_id,
                "provision",
                now=current,
                retry_failed=True,
            )
        except VpnControlIntentError as error:
            _friend_control_error(error)
        invitation, access_key, subscription, customer = await _locked_friend_chain(
            db,
            slot,
            binding,
        )
        if operation.generation != prior.generation + 1:
            raise FriendInvitationConflict("friend_invitation_retry_required") from None
        return _view(
            invitation,
            access_key,
            subscription,
            customer,
            operation,
            current,
        )


async def disable_friend_invitation(
    db: AsyncSession,
    slot: int,
    now: datetime,
) -> FriendInvitationView:
    current = _current_time(now)
    savepoint = await db.begin_nested()
    try:
        binding = await _discover_friend_binding(db, slot)
        if binding.revoked_at is not None:
            invitation, access_key, subscription, customer = await _locked_friend_chain(
                db,
                slot,
                binding,
            )
            operation = await _friend_operation(db, access_key)
            await savepoint.commit()
            return _view(
                invitation,
                access_key,
                subscription,
                customer,
                operation,
                current,
            )
        try:
            operation = await stage_vpn_control_operation(
                db,
                binding.access_key_id,
                "revoke",
                now=current,
            )
        except VpnControlIntentError as error:
            _friend_control_error(error)
        invitation, access_key, subscription, customer = await _locked_friend_chain(
            db,
            slot,
            binding,
        )
        if invitation.revoked_at is not None:
            await savepoint.rollback()
            fresh = await _discover_friend_binding(db, slot)
            invitation, access_key, subscription, customer = await _locked_friend_chain(
                db,
                slot,
                fresh,
            )
            operation = await _friend_operation(db, access_key)
            return _view(
                invitation,
                access_key,
                subscription,
                customer,
                operation,
                current,
            )
        invitation.revoked_at = current
        await db.execute(
            update(VpnCustomerSession)
            .where(
                VpnCustomerSession.customer_id == customer.id,
                VpnCustomerSession.revoked_at.is_(None),
            )
            .values(revoked_at=current)
        )
        await db.flush()
        view = _view(
            invitation,
            access_key,
            subscription,
            customer,
            operation,
            current,
        )
        await savepoint.commit()
        return view
    except BaseException:
        if savepoint.is_active:
            await savepoint.rollback()
        raise


def _new_token() -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    return token, digest_invite_token(token)


def _issued(
    invitation: VpnFriendInvitation,
    username: str,
    token: str,
    now: datetime,
) -> IssuedFriendInvitation:
    return IssuedFriendInvitation(
        view=_unredeemed_view(invitation, now),
        link=f"https://t.me/{username}?start=i_{token}",
    )


async def issue_friend_invitation(
    db: AsyncSession,
    settings: Settings,
    actor_user_id: int | None,
    now: datetime,
) -> IssuedFriendInvitation:
    username = _bot_username(settings)
    await require_friend_invitation_readiness(db, settings)
    current = _current_time(now)
    occupied_slots = set(await db.scalars(select(VpnFriendInvitation.slot)))

    for slot in range(1, 11):
        if slot in occupied_slots:
            continue
        while True:
            token, digest = _new_token()
            try:
                async with db.begin_nested():
                    invitation = VpnFriendInvitation(
                        slot=slot,
                        token_digest=digest,
                        created_by_user_id=actor_user_id,
                        created_at=current,
                        redeem_expires_at=current + INVITE_LIFETIME,
                    )
                    db.add(invitation)
                    await db.flush()
                return _issued(invitation, username, token, current)
            except IntegrityError:
                if await db.scalar(
                    select(VpnFriendInvitation.slot).where(
                        VpnFriendInvitation.slot == slot
                    )
                ) is not None:
                    break
                if await db.scalar(
                    select(VpnFriendInvitation.slot).where(
                        VpnFriendInvitation.token_digest == digest
                    )
                ) is not None:
                    continue
                raise
    raise FriendInvitationConflict("friend_invitation_cohort_full")


async def rotate_friend_invitation(
    db: AsyncSession,
    settings: Settings,
    slot: int,
    actor_user_id: int | None,
    now: datetime,
) -> IssuedFriendInvitation:
    username = _bot_username(settings)
    await require_friend_invitation_readiness(db, settings)
    current = _current_time(now)
    invitation = await db.scalar(
        select(VpnFriendInvitation)
        .where(VpnFriendInvitation.slot == slot)
        .with_for_update(of=VpnFriendInvitation)
        .execution_options(populate_existing=True)
    )
    if (
        invitation is None
        or invitation.redeemed_at is not None
        or invitation.revoked_at is not None
    ):
        raise FriendInvitationConflict("friend_invitation_not_rotatable")

    while True:
        token, digest = _new_token()
        try:
            async with db.begin_nested():
                await db.execute(
                    update(VpnFriendInvitation)
                    .where(VpnFriendInvitation.slot == slot)
                    .values(
                        token_digest=digest,
                        created_at=current,
                        redeem_expires_at=current + INVITE_LIFETIME,
                    )
                )
                await db.flush()
            break
        except IntegrityError:
            if await db.scalar(
                select(VpnFriendInvitation.slot).where(
                    VpnFriendInvitation.token_digest == digest,
                    VpnFriendInvitation.slot != slot,
                )
            ) is not None:
                continue
            raise

    await db.refresh(invitation)
    return _issued(invitation, username, token, current)


def _redemption_result(
    customer: VpnCustomer,
    subscription: VpnSubscription,
    access_key: VpnAccessKey,
) -> RedeemedFriendInvitation:
    starts_at = _as_utc(subscription.starts_at)
    expires_at = _as_utc(subscription.expires_at)
    if (
        access_key.external_uuid is None
        or starts_at is None
        or expires_at is None
    ):
        _rejected()
    return RedeemedFriendInvitation(
        customer_id=customer.id,
        subscription_id=subscription.id,
        access_key_id=access_key.id,
        external_uuid=access_key.external_uuid,
        starts_at=starts_at,
        expires_at=expires_at,
    )


async def _replay_redemption(
    db: AsyncSession,
    invitation: VpnFriendInvitation,
    normalized_user_id: str,
) -> RedeemedFriendInvitation:
    row = (
        await db.execute(
            select(VpnAccessKey, VpnSubscription, VpnCustomer)
            .join(
                VpnSubscription,
                VpnSubscription.id == VpnAccessKey.subscription_id,
            )
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(VpnAccessKey.id == invitation.access_key_id)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        _rejected()
    access_key, subscription, customer = row
    if (
        invitation.revoked_at is not None
        or invitation.telegram_user_id != normalized_user_id
        or not _has_exact_chain(invitation, access_key, subscription, customer)
    ):
        _rejected()
    return _redemption_result(customer, subscription, access_key)


async def redeem_friend_invitation(
    db: AsyncSession,
    settings: Settings,
    token: str,
    identity: TelegramIdentity,
    now: datetime,
) -> RedeemedFriendInvitation:
    if type(token) is not str or INVITE_TOKEN.fullmatch(token) is None:
        _rejected()
    try:
        normalized_user_id = telegram_user_id(identity.user_id)
    except ValueError:
        _rejected()
    digest = digest_invite_token(token)
    current = _current_time(now)

    try:
        async with db.begin_nested():
            invitation = await db.scalar(
                select(VpnFriendInvitation)
                .where(VpnFriendInvitation.token_digest == digest)
                .with_for_update(of=VpnFriendInvitation)
                .execution_options(populate_existing=True)
            )
            if invitation is None:
                _rejected()
            if invitation.redeemed_at is not None:
                return await _replay_redemption(
                    db,
                    invitation,
                    normalized_user_id,
                )
            redeem_expires_at = _as_utc(invitation.redeem_expires_at)
            if (
                invitation.revoked_at is not None
                or redeem_expires_at is None
                or redeem_expires_at <= current
            ):
                _rejected()

            endpoint = await require_friend_invitation_readiness(db, settings)
            customer = await resolve_telegram_customer(db, identity)
            if customer.trial_started_at is not None:
                _rejected()
            # Legacy or damaged friend bindings consume the trial even when
            # the marker was never backfilled. The resolver holds customer's lock.
            prior_friend_slot = await db.scalar(
                select(VpnFriendInvitation.slot)
                .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
                .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
                .where(VpnSubscription.customer_id == customer.id)
                .limit(1)
            )
            if prior_friend_slot is not None:
                _rejected()
            subscription = VpnSubscription(
                customer_id=customer.id,
                status="trial",
                starts_at=current,
                expires_at=current + TRIAL_LIFETIME,
                max_devices=1,
            )
            db.add(subscription)
            await db.flush()
            access_key = VpnAccessKey(
                subscription_id=subscription.id,
                worker_id=endpoint.worker_id,
                endpoint_id=endpoint.id,
                protocol="vless",
                external_uuid=str(uuid4()),
                verified_client_email=f"veltrix-beta-{invitation.slot}",
                panel_sub_id=uuid4().hex,
                display_name="Veltrix VPN",
                config_uri=None,
                status="pending_sync",
                issued_at=current,
                expires_at=subscription.expires_at,
            )
            db.add(access_key)
            await db.flush()
            await stage_vpn_control_operation(
                db,
                access_key.id,
                "provision",
                now=current,
            )
            invitation.redeemed_at = current
            customer.trial_started_at = current
            invitation.telegram_user_id = normalized_user_id
            invitation.access_key_id = access_key.id
            await db.flush()
            return _redemption_result(customer, subscription, access_key)
    except (FriendInvitationUnavailable, FriendInvitationConflict):
        raise
    except VpnControlIntentError:
        _unavailable()
