from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.base import utcnow
from app.db.models import AdminAuditLog, Domain, PromoCode, Proxy, User
from app.db.session import get_db
from app.schemas.admin import (
    AdminAuditLogResponse,
    AdminUserResponse,
    AdminUserUpdateRequest,
    DiagnosticTelegramSettingsRequest,
    DiagnosticTelegramSettingsResponse,
    GrantAccessRequest,
    ManualUserCreateRequest,
    PromoCodeCreateRequest,
    PromoCodeResponse,
)
from app.schemas.auth import UserResponse
from app.services.app_settings import (
    DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY,
    DIAGNOSTIC_TELEGRAM_TOKEN_KEY,
    get_diagnostic_telegram_settings,
    set_app_setting,
)
from app.services.audit import add_audit_log
from app.services.notifier import TelegramNotifier
from app.core.config import get_settings

router = APIRouter(prefix="/admin", tags=["admin"])


def get_monitoring(request: Request):
    return request.app.state.monitoring


def serialize_admin_user(user: User, domain_count: int, proxy_count: int) -> AdminUserResponse:
    return AdminUserResponse(
        id=user.id,
        username=user.username,
        role=user.role,
        status=user.status,
        language=user.language,
        max_domains=user.max_domains,
        access_expires_at=user.access_expires_at,
        status_message=user.status_message,
        last_login_at=user.last_login_at,
        deleted_at=user.deleted_at,
        created_at=user.created_at,
        updated_at=user.updated_at,
        domain_count=domain_count,
        proxy_count=proxy_count,
    )


@router.get("/users", response_model=list[AdminUserResponse])
async def list_users(
    status_filter: str | None = None,
    include_deleted: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminUserResponse]:
    del admin
    query = select(User).order_by(User.created_at.desc())
    if status_filter:
        query = query.where(User.status == status_filter)
    if not include_deleted:
        query = query.where(User.deleted_at.is_(None))

    result = await db.execute(query)
    users = result.scalars().all()
    items: list[AdminUserResponse] = []
    for user in users:
        domain_count = await db.scalar(select(func.count(Domain.id)).where(Domain.owner_id == user.id))
        proxy_count = await db.scalar(select(func.count(Proxy.id)).where(Proxy.owner_id == user.id))
        items.append(serialize_admin_user(user, int(domain_count or 0), int(proxy_count or 0)))
    return items


@router.post("/users", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user_manually(
    payload: ManualUserCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    del payload, db, admin
    raise HTTPException(
        status_code=403,
        detail="Manual user creation is disabled in this control-only build",
    )


@router.patch("/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: int,
    payload: AdminUserUpdateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    previous_status = user.status
    if payload.status is not None:
        user.status = payload.status
    if "status_message" in payload.model_fields_set:
        user.status_message = payload.status_message
    if payload.role is not None:
        user.role = payload.role
    if payload.language is not None:
        user.language = payload.language
    if "max_domains" in payload.model_fields_set:
        user.max_domains = payload.max_domains
    if "access_expires_at" in payload.model_fields_set:
        user.access_expires_at = payload.access_expires_at
    user.updated_at = utcnow()

    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=user.id,
        action="user_update",
        details=f"status={user.status} role={user.role} max_domains={user.max_domains}",
    )
    await db.commit()
    await db.refresh(user)

    if previous_status != user.status and user.status in {"blocked", "rejected"}:
        await _stop_user_domains(user.id, request, db)

    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/grant-access", response_model=UserResponse)
async def grant_access(
    user_id: int,
    payload: GrantAccessRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    now = utcnow()
    base_time = user.access_expires_at if user.access_expires_at and user.access_expires_at > now else now
    user.access_expires_at = base_time + timedelta(seconds=payload.duration_seconds)
    if user.status in {"pending", "rejected"}:
        user.status = "approved"
        user.status_message = None
    user.updated_at = now
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=user.id,
        action="grant_access",
        details=f"duration_seconds={payload.duration_seconds}",
    )
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/soft-delete", response_model=UserResponse)
async def soft_delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.deleted_at = utcnow()
    user.status = "blocked"
    user.status_message = "Аккаунт деактивирован администратором."
    await add_audit_log(db, actor_user_id=admin.id, target_user_id=user.id, action="soft_delete_user")
    await db.commit()
    await db.refresh(user)
    await _stop_user_domains(user.id, request, db)
    return UserResponse.model_validate(user)


@router.post("/users/{user_id}/restore", response_model=UserResponse)
async def restore_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> UserResponse:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    user.deleted_at = None
    if user.status == "blocked":
        user.status = "approved"
        user.status_message = None
    await add_audit_log(db, actor_user_id=admin.id, target_user_id=user.id, action="restore_user")
    await db.commit()
    await db.refresh(user)
    return UserResponse.model_validate(user)


@router.delete("/users/{user_id}")
async def hard_delete_user(
    user_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, str]:
    user = await db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")

    await _stop_user_domains(user.id, request, db)
    await add_audit_log(db, actor_user_id=admin.id, target_user_id=user.id, action="hard_delete_user")
    await db.delete(user)
    await db.commit()
    return {"detail": "User deleted permanently"}


@router.get("/promo-codes", response_model=list[PromoCodeResponse])
async def list_promo_codes(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[PromoCode]:
    del admin
    result = await db.execute(select(PromoCode).order_by(PromoCode.created_at.desc()))
    return list(result.scalars().all())


@router.post("/promo-codes", response_model=PromoCodeResponse, status_code=status.HTTP_201_CREATED)
async def create_promo_code(
    payload: PromoCodeCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> PromoCode:
    code = payload.code.strip()
    existing = await db.execute(select(PromoCode).where(PromoCode.code == code))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Promo code already exists")

    promo = PromoCode(
        code=code,
        duration_seconds=payload.duration_seconds,
        max_activations=payload.max_activations,
        expires_at=payload.expires_at,
        is_active=payload.is_active,
        created_by_user_id=admin.id,
    )
    db.add(promo)
    await db.flush()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="create_promo_code",
        details=f"code={promo.code} duration_seconds={promo.duration_seconds}",
    )
    await db.commit()
    await db.refresh(promo)
    return promo


@router.get("/audit-logs", response_model=list[AdminAuditLogResponse])
async def list_audit_logs(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AdminAuditLog]:
    del admin
    result = await db.execute(
        select(AdminAuditLog).order_by(AdminAuditLog.created_at.desc()).limit(min(limit, 500))
    )
    return list(result.scalars().all())


async def _stop_user_domains(user_id: int, request: Request, db: AsyncSession) -> None:
    result = await db.execute(select(Domain.id).where(Domain.owner_id == user_id, Domain.is_active.is_(True)))
    domain_ids = [row[0] for row in result.all()]
    monitoring = get_monitoring(request)
    for domain_id in domain_ids:
        await monitoring.stop_domain(domain_id)


@router.get("/diagnostic-telegram", response_model=DiagnosticTelegramSettingsResponse)
async def get_diagnostic_telegram(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiagnosticTelegramSettingsResponse:
    del admin
    token, chat_id = await get_diagnostic_telegram_settings(db)
    return DiagnosticTelegramSettingsResponse(
        telegram_token=token,
        telegram_chat_id=chat_id,
    )


@router.post("/diagnostic-telegram", response_model=DiagnosticTelegramSettingsResponse)
async def update_diagnostic_telegram(
    payload: DiagnosticTelegramSettingsRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiagnosticTelegramSettingsResponse:
    token = payload.telegram_token or None
    chat_id = payload.telegram_chat_id or None
    await set_app_setting(db, DIAGNOSTIC_TELEGRAM_TOKEN_KEY, token)
    await set_app_setting(db, DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY, chat_id)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="update_diagnostic_telegram",
        details=f"configured={bool(token and chat_id)}",
    )
    await db.commit()
    return DiagnosticTelegramSettingsResponse(
        telegram_token=token,
        telegram_chat_id=chat_id,
    )


@router.post("/diagnostic-telegram/test")
async def test_diagnostic_telegram(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> dict[str, str]:
    token, chat_id = await get_diagnostic_telegram_settings(db)
    if not token or not chat_id:
        raise HTTPException(status_code=400, detail="Diagnostic Telegram settings are empty")
    notifier = TelegramNotifier(get_settings())
    await notifier.send_diagnostic(
        "Diagnostic channel test",
        f"Admin @{admin.username} requested a test message at {utcnow().isoformat()}",
        token=token,
        chat_id=chat_id,
    )
    return {"detail": "Diagnostic Telegram message sent"}
