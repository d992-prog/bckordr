from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import Response

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnCustomerSession,
    VpnFriendInvitation,
    VpnPortalLoginAttempt,
    VpnPortalMiniAppExchange,
    VpnSubscription,
)
from app.services.vpn_portal_telegram import (
    TelegramAuthenticationError,
    verify_mini_app,
)
from app.services.vpn_telegram_identity import (
    resolve_telegram_customer,
    telegram_user_id,
)


SESSION_COOKIE = "veltrix_customer_session"
BINDING_COOKIE = "veltrix_login_binding"
SESSION_LIFETIME = timedelta(days=7)
LOGIN_ATTEMPT_LIFETIME = timedelta(minutes=10)
MINI_APP_CLAIM_LIFETIME = timedelta(minutes=10)
CUSTOMER_COOKIE_MAX_AGE = 604_800
BINDING_COOKIE_MAX_AGE = 600
MAX_OPAQUE_TOKEN_LENGTH = 128
CSRF_MESSAGE = b"veltrix-portal-csrf-v1"
DNS_HOST_PATTERN = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)


@dataclass(frozen=True)
class PortalPrincipal:
    customer: VpnCustomer
    session: VpnCustomerSession
    csrf: str


@dataclass(frozen=True)
class MiniAppExchangeResult:
    principal: PortalPrincipal
    raw_session: str | None


class PortalAuthenticationError(ValueError):
    def __init__(self, *, account_conflict: bool = False) -> None:
        self.account_conflict = account_conflict
        message = "portal_account_conflict" if account_conflict else "portal_authentication_failed"
        super().__init__(message)


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def digest_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf_token(raw_session: str) -> str:
    return hmac.new(raw_session.encode(), CSRF_MESSAGE, hashlib.sha256).hexdigest()


def public_origin(settings: Settings) -> str:
    configured = settings.vpn_portal_public_origin
    try:
        if (
            not configured
            or configured != configured.strip()
            or not configured.isascii()
            or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in configured)
            or "\\" in configured
            or "?" in configured
            or "#" in configured
        ):
            raise ValueError
        parsed = urlsplit(configured)
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.netloc
            or parsed.netloc.endswith(":")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        hostname = parsed.hostname
        port = parsed.port
        if not hostname or port == 0:
            raise ValueError
        hostname = hostname.lower()
        try:
            ipaddress.ip_address(hostname)
        except ValueError:
            if DNS_HOST_PATTERN.fullmatch(hostname) is None:
                raise ValueError
        scheme = parsed.scheme.lower()
        if scheme == "http" and not (
            settings.vpn_portal_allow_local_http
            and hostname in {"localhost", "127.0.0.1"}
        ):
            raise ValueError
        default_port = 443 if scheme == "https" else 80
        host = f"[{hostname}]" if ":" in hostname else hostname
        authority = host if port is None or port == default_port else f"{host}:{port}"
        return f"{scheme}://{authority}"
    except (TypeError, ValueError):
        raise ValueError("portal_configuration_invalid") from None


def identity_allowed(settings: Settings, user_id: object) -> bool:
    if not settings.vpn_portal_enabled:
        return False
    try:
        canonical = telegram_user_id(user_id)
    except ValueError:
        return False
    if settings.vpn_portal_public_access:
        return True
    allowed: set[str] = set()
    for candidate in settings.vpn_portal_allowed_telegram_ids.split(","):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            allowed.add(telegram_user_id(candidate))
        except ValueError:
            continue
    return canonical in allowed


def friend_admission_query(user_id: str, now: datetime):
    return (
        select(VpnFriendInvitation.slot)
        .select_from(VpnFriendInvitation)
        .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
        .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
        .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
        .where(
            VpnFriendInvitation.telegram_user_id == user_id,
            VpnCustomer.telegram_user_id == user_id,
            VpnFriendInvitation.revoked_at.is_(None),
            VpnCustomer.status == "active",
            VpnSubscription.status.in_(("active", "trial")),
            VpnSubscription.starts_at <= now,
            VpnSubscription.expires_at > now,
        )
        .limit(1)
    )


async def identity_admitted(
    db: AsyncSession,
    settings: Settings,
    user_id: object,
    *,
    now: datetime | None = None,
) -> bool:
    if not settings.vpn_portal_enabled:
        return False
    try:
        normalized = telegram_user_id(user_id)
    except ValueError:
        return False
    if identity_allowed(settings, normalized):
        return True
    current = as_utc(now or utcnow())
    return bool(await db.scalar(friend_admission_query(normalized, current)))


def _valid_opaque_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= MAX_OPAQUE_TOKEN_LENGTH
        and value.isascii()
        and not any(character.isspace() for character in value)
    )


async def issue_session(
    db: AsyncSession,
    customer_id: int,
    verified_user_id: object,
    now: datetime | None = None,
) -> str:
    current_time = as_utc(now or utcnow())
    try:
        canonical_user_id = telegram_user_id(verified_user_id)
    except ValueError:
        raise ValueError("customer_unavailable") from None
    customer = await db.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == customer_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        customer is None
        or customer.status != "active"
        or customer.telegram_user_id != canonical_user_id
    ):
        raise ValueError("customer_unavailable")
    raw_session = secrets.token_urlsafe(32)
    db.add(
        VpnCustomerSession(
            token_hash=digest_token(raw_session),
            customer_id=customer.id,
            telegram_user_id=canonical_user_id,
            created_at=current_time,
            expires_at=current_time + SESSION_LIFETIME,
        )
    )
    await db.flush()
    return raw_session


async def lookup_session(
    db: AsyncSession,
    raw_session: str | None,
    settings: Settings,
    now: datetime | None = None,
) -> PortalPrincipal | None:
    if not _valid_opaque_token(raw_session) or not settings.vpn_portal_enabled:
        return None
    current_time = as_utc(now or utcnow())
    row = (
        await db.execute(
            select(VpnCustomerSession, VpnCustomer)
            .join(VpnCustomer, VpnCustomer.id == VpnCustomerSession.customer_id)
            .where(VpnCustomerSession.token_hash == digest_token(raw_session))
            .execution_options(populate_existing=True)
        )
    ).one_or_none()
    if row is None:
        return None
    session, customer = row
    if (
        session.revoked_at is not None
        or as_utc(session.expires_at) <= current_time
        or customer.status != "active"
        or customer.telegram_user_id != session.telegram_user_id
        or not await identity_admitted(
            db,
            settings,
            session.telegram_user_id,
            now=current_time,
        )
    ):
        return None
    return PortalPrincipal(customer=customer, session=session, csrf=csrf_token(raw_session))


async def _lock_current_principal(
    db: AsyncSession,
    principal: PortalPrincipal,
    settings: Settings,
    current_time: datetime,
    *,
    expected_customer_id: int | None = None,
) -> PortalPrincipal | None:
    customer = await db.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == principal.customer.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if customer is None:
        return None
    session = await db.scalar(
        select(VpnCustomerSession)
        .where(
            VpnCustomerSession.token_hash == principal.session.token_hash,
            VpnCustomerSession.customer_id == customer.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        session is None
        or (expected_customer_id is not None and customer.id != expected_customer_id)
        or session.revoked_at is not None
        or as_utc(session.expires_at) <= current_time
        or customer.status != "active"
        or customer.telegram_user_id != session.telegram_user_id
        or not await identity_admitted(
            db,
            settings,
            session.telegram_user_id,
            now=current_time,
        )
    ):
        return None
    return PortalPrincipal(customer=customer, session=session, csrf=principal.csrf)


def valid_mutation(
    origin: str | None,
    token: str | None,
    principal: PortalPrincipal,
    settings: Settings,
) -> bool:
    try:
        configured_origin = public_origin(settings)
    except ValueError:
        return False
    return (
        isinstance(origin, str)
        and origin == configured_origin
        and isinstance(token, str)
        and len(token) == 64
        and token.isascii()
        and hmac.compare_digest(token, principal.csrf)
    )


def _set_cookie(response: Response, key: str, value: str, max_age: int, settings: Settings) -> None:
    secure = public_origin(settings).startswith("https://")
    response.set_cookie(
        key,
        value,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def set_session_cookie(response: Response, raw_session: str, settings: Settings) -> None:
    _set_cookie(response, SESSION_COOKIE, raw_session, CUSTOMER_COOKIE_MAX_AGE, settings)


def set_binding_cookie(response: Response, binding: str, settings: Settings) -> None:
    _set_cookie(response, BINDING_COOKIE, binding, BINDING_COOKIE_MAX_AGE, settings)


def _delete_cookie(response: Response, key: str, settings: Settings) -> None:
    secure = public_origin(settings).startswith("https://")
    response.set_cookie(
        key,
        "",
        max_age=0,
        expires=0,
        httponly=True,
        secure=secure,
        samesite="lax",
        path="/",
    )


def delete_session_cookie(response: Response, settings: Settings) -> None:
    _delete_cookie(response, SESSION_COOKIE, settings)


def delete_binding_cookie(response: Response, settings: Settings) -> None:
    _delete_cookie(response, BINDING_COOKIE, settings)


async def revoke_session(
    db: AsyncSession,
    principal: PortalPrincipal,
    now: datetime | None = None,
) -> None:
    current_time = as_utc(now or utcnow())
    session = await db.scalar(
        select(VpnCustomerSession)
        .where(
            VpnCustomerSession.token_hash == principal.session.token_hash,
            VpnCustomerSession.customer_id == principal.customer.id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = current_time
        await db.flush()


async def create_login_attempt(
    db: AsyncSession,
    now: datetime | None = None,
) -> tuple[str, str, str]:
    current_time = as_utc(now or utcnow())
    state = secrets.token_urlsafe(32)
    binding = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    db.add(
        VpnPortalLoginAttempt(
            state_hash=digest_token(state),
            binding_hash=digest_token(binding),
            code_verifier=verifier,
            created_at=current_time,
            expires_at=current_time + LOGIN_ATTEMPT_LIFETIME,
        )
    )
    await db.flush()
    return state, binding, verifier


async def consume_login_attempt(
    db: AsyncSession,
    state: str,
    binding: str,
    now: datetime,
) -> str | None:
    if not _valid_opaque_token(state) or not _valid_opaque_token(binding):
        return None
    current_time = as_utc(now)
    attempt = await db.scalar(
        select(VpnPortalLoginAttempt)
        .where(
            VpnPortalLoginAttempt.state_hash == digest_token(state),
            VpnPortalLoginAttempt.binding_hash == digest_token(binding),
            VpnPortalLoginAttempt.consumed_at.is_(None),
            VpnPortalLoginAttempt.expires_at > current_time,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if attempt is None or attempt.code_verifier is None:
        return None
    verifier = attempt.code_verifier
    attempt.code_verifier = None
    attempt.consumed_at = current_time
    await db.commit()
    return verifier


async def exchange_mini_app_session(
    db: AsyncSession,
    raw_init_data: str,
    settings: Settings,
    *,
    raw_session: str | None = None,
    now: datetime | None = None,
) -> MiniAppExchangeResult:
    """Exchange verified init data atomically; the caller commits successful results."""
    current_time = as_utc(now or utcnow())
    try:
        identity, digest, _auth_timestamp = verify_mini_app(
            raw_init_data,
            settings.vpn_telegram_bot_token,
            int(current_time.timestamp()),
        )
    except TelegramAuthenticationError:
        raise PortalAuthenticationError() from None
    if not await identity_admitted(db, settings, identity.user_id, now=current_time):
        raise PortalAuthenticationError()

    principal = await lookup_session(db, raw_session, settings, now=current_time)
    existing_claim = await db.get(VpnPortalMiniAppExchange, digest)
    if existing_claim is not None:
        if principal is None:
            raise PortalAuthenticationError()
        principal = await _lock_current_principal(
            db,
            principal,
            settings,
            current_time,
            expected_customer_id=existing_claim.customer_id,
        )
        if principal is None or principal.session.telegram_user_id != identity.user_id:
            raise PortalAuthenticationError()
        return MiniAppExchangeResult(principal=principal, raw_session=None)
    if principal is not None:
        principal = await _lock_current_principal(
            db,
            principal,
            settings,
            current_time,
        )
        if principal is None:
            raise PortalAuthenticationError()
        if principal.session.telegram_user_id != identity.user_id:
            raise PortalAuthenticationError(account_conflict=True)

    async with db.begin_nested():
        customer = await resolve_telegram_customer(db, identity)
        if customer.status != "active" or customer.telegram_user_id != identity.user_id:
            raise PortalAuthenticationError()

        try:
            async with db.begin_nested():
                db.add(
                    VpnPortalMiniAppExchange(
                        digest=digest,
                        customer_id=customer.id,
                        created_at=current_time,
                        expires_at=current_time + MINI_APP_CLAIM_LIFETIME,
                    )
                )
                await db.flush()
        except IntegrityError:
            existing_claim = await db.get(VpnPortalMiniAppExchange, digest)
            if (
                principal is not None
                and existing_claim is not None
                and existing_claim.customer_id == principal.customer.id
                and principal.session.telegram_user_id == identity.user_id
            ):
                return MiniAppExchangeResult(principal=principal, raw_session=None)
            raise PortalAuthenticationError() from None

        if principal is not None:
            if customer.id != principal.customer.id:
                raise PortalAuthenticationError()
            return MiniAppExchangeResult(principal=principal, raw_session=None)
        try:
            new_raw_session = await issue_session(db, customer.id, identity.user_id, now=current_time)
        except ValueError:
            raise PortalAuthenticationError() from None
        new_principal = await lookup_session(db, new_raw_session, settings, now=current_time)
        if new_principal is None:
            raise PortalAuthenticationError()
        return MiniAppExchangeResult(principal=new_principal, raw_session=new_raw_session)


async def cleanup_expired_portal_auth(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    current_time = as_utc(now or utcnow())

    async def delete_batch(model, primary_key):
        candidates = (
            select(primary_key)
            .where(model.expires_at <= current_time)
            .order_by(model.expires_at.asc(), primary_key.asc())
            .limit(100)
        )
        result = await db.execute(delete(model).where(primary_key.in_(candidates)))
        return result.rowcount

    return {
        "login_attempts": await delete_batch(VpnPortalLoginAttempt, VpnPortalLoginAttempt.state_hash),
        "mini_app_exchanges": await delete_batch(VpnPortalMiniAppExchange, VpnPortalMiniAppExchange.digest),
        "customer_sessions": await delete_batch(VpnCustomerSession, VpnCustomerSession.token_hash),
    }
