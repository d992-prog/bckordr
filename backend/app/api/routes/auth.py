from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import PromoCode, PromoRedemption, User, UserSession
from app.db.session import get_db
from app.schemas.auth import (
    ChangePasswordRequest,
    LoginRequest,
    PromoApplyRequest,
    RegisterRequest,
    SessionResponse,
    TelegramSettingsRequest,
    UserResponse,
)
from app.services.notifier import TelegramNotifier
from app.services.security import (
    SESSION_COOKIE_NAME,
    build_session_expiry,
    generate_session_token,
    hash_password,
    hash_session_token,
    user_has_feature_access,
    verify_password,
)

router = APIRouter(prefix="/auth", tags=["auth"])


def serialize_user(user: User, *, masked: bool) -> UserResponse:
    token = user.telegram_token
    chat_id = user.telegram_chat_id
    if masked and token:
        token = token[:3] + "***"
    if masked and chat_id:
        chat_id = chat_id[:3] + "***"

    return UserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        language=user.language,
        max_domains=user.max_domains,
        access_expires_at=user.access_expires_at,
        status_message=user.status_message,
        telegram_token=token,
        telegram_chat_id=chat_id,
        last_login_at=user.last_login_at,
        deleted_at=user.deleted_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


def build_session_payload(user: User) -> SessionResponse:
    access = user_has_feature_access(user)
    return SessionResponse(user=serialize_user(user, masked=not access), has_feature_access=access)


@router.post("/register", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    del payload, response, db
    raise HTTPException(
        status_code=403,
        detail="Registration is disabled in this control-only build",
    )


@router.post("/login", response_model=SessionResponse)
async def login(
    payload: LoginRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> SessionResponse:
    settings = get_settings()
    username = payload.username.strip().lower()
    result = await db.execute(select(User).where(User.username == username, User.deleted_at.is_(None)))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid credentials")

    now = utcnow()
    if user.login_locked_until and user.login_locked_until > now:
        raise HTTPException(status_code=429, detail="Too many login attempts. Try later.")

    if not verify_password(payload.password, user.password_hash):
        user.login_failed_attempts += 1
        if user.login_failed_attempts >= settings.login_rate_limit_attempts:
            user.login_locked_until = now + timedelta(minutes=settings.login_lock_minutes)
            user.login_failed_attempts = 0
        await db.commit()
        raise HTTPException(status_code=401, detail="Invalid credentials")

    user.login_failed_attempts = 0
    user.login_locked_until = None
    user.last_login_at = now
    await db.commit()
    await db.refresh(user)
    await _create_session_cookie(response, db, user, remember_me=payload.remember_me)
    return build_session_payload(user)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if raw_token:
        token_hash = hash_session_token(raw_token)
        result = await db.execute(select(UserSession).where(UserSession.token_hash == token_hash))
        session = result.scalar_one_or_none()
        if session:
            session.revoked_at = utcnow()
            await db.commit()
    response.delete_cookie(SESSION_COOKIE_NAME)
    return {"detail": "Logged out"}


@router.get("/me", response_model=SessionResponse)
async def get_me(user: User = Depends(get_current_user)) -> SessionResponse:
    return build_session_payload(user)


@router.post("/change-password", response_model=SessionResponse)
async def change_password(
    payload: ChangePasswordRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionResponse:
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is invalid")
    user.password_hash = hash_password(payload.new_password)
    user.updated_at = utcnow()
    await db.commit()
    await db.refresh(user)
    return build_session_payload(user)


@router.post("/telegram", response_model=SessionResponse)
async def update_telegram_settings(
    payload: TelegramSettingsRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionResponse:
    user.telegram_token = payload.telegram_token or None
    user.telegram_chat_id = payload.telegram_chat_id or None
    user.updated_at = utcnow()
    await db.commit()
    await db.refresh(user)
    return build_session_payload(user)


@router.post("/telegram/test")
async def test_telegram(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, str]:
    if not user.telegram_token or not user.telegram_chat_id:
        raise HTTPException(status_code=400, detail="Telegram settings are empty")
    notifier = TelegramNotifier(get_settings())
    await notifier.send_domain_available(
        "test-domain.fr",
        utcnow(),
        token=user.telegram_token,
        chat_id=user.telegram_chat_id,
    )
    return {"detail": "Test notification sent"}


@router.post("/promo/apply", response_model=SessionResponse)
async def apply_promo_code(
    payload: PromoApplyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SessionResponse:
    if user.status == "blocked":
        raise HTTPException(status_code=403, detail="Blocked users cannot redeem promo codes")

    code = payload.code.strip()
    result = await db.execute(select(PromoCode).where(PromoCode.code == code, PromoCode.is_active.is_(True)))
    promo = result.scalar_one_or_none()
    if promo is None:
        raise HTTPException(status_code=404, detail="Promo code not found")

    now = utcnow()
    if promo.expires_at and promo.expires_at <= now:
        raise HTTPException(status_code=400, detail="Promo code expired")
    if promo.max_activations is not None and promo.activation_count >= promo.max_activations:
        raise HTTPException(status_code=400, detail="Promo code exhausted")

    redemption_result = await db.execute(
        select(PromoRedemption).where(PromoRedemption.promo_code_id == promo.id, PromoRedemption.user_id == user.id)
    )
    if redemption_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="You already used this promo code")

    base_time = user.access_expires_at if user.access_expires_at and user.access_expires_at > now else now
    user.access_expires_at = base_time + timedelta(seconds=promo.duration_seconds)
    if user.status in {"pending", "rejected"}:
        user.status = "approved"
        user.status_message = None
    promo.activation_count += 1
    db.add(
        PromoRedemption(
            promo_code_id=promo.id,
            user_id=user.id,
            duration_seconds=promo.duration_seconds,
        )
    )
    await db.commit()
    await db.refresh(user)
    return build_session_payload(user)


async def _create_session_cookie(
    response: Response,
    db: AsyncSession,
    user: User,
    *,
    remember_me: bool,
) -> None:
    raw_token = generate_session_token()
    expiry = utcnow() + build_session_expiry(remember_me)
    db.add(
        UserSession(
            user_id=user.id,
            token_hash=hash_session_token(raw_token),
            remember_me=remember_me,
            expires_at=expiry,
            last_used_at=utcnow(),
        )
    )
    await db.commit()
    response.set_cookie(
        SESSION_COOKIE_NAME,
        raw_token,
        httponly=True,
        samesite="lax",
        secure=get_settings().session_cookie_secure,
        expires=int(expiry.timestamp()),
    )
