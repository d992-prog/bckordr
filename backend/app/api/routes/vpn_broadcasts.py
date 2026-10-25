from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.routes.control import _require_same_origin
from app.core.config import get_settings
from app.db.models import User, VpnBotChat, VpnBroadcastCampaign
from app.db.session import get_db
from app.schemas.vpn_broadcasts import (
    BroadcastBodyRequest,
    BroadcastConfigResponse,
    BroadcastConfirmRequest,
    BroadcastDetail,
    BroadcastEmptyRequest,
    BroadcastSummary,
)
from app.services import vpn_broadcasts as service
from app.services.vpn_bot_chats import configured_vpn_bot_id, registry_state


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/control/vpn/broadcasts",
    tags=["control"],
    dependencies=[Depends(_no_store)],
)
Database = Annotated[AsyncSession, Depends(get_db)]
Administrator = Annotated[User, Depends(require_admin)]


def _error(code: str) -> HTTPException:
    allowed = {
        "vpn_broadcast_not_found": 404,
        "vpn_broadcast_not_draft": 409,
        "vpn_broadcast_registry_unavailable": 409,
        "vpn_broadcast_preview_changed": 409,
        "vpn_broadcast_audience_changed": 409,
        "vpn_broadcasts_disabled": 409,
        "vpn_bot_unconfigured": 409,
    }
    return HTTPException(
        status_code=allowed.get(code, 422),
        detail=code if code in allowed else "vpn_broadcast_invalid_request",
        headers={"Cache-Control": "no-store"},
    )


async def _get(db: AsyncSession, campaign_id: int) -> VpnBroadcastCampaign:
    row = await db.get(VpnBroadcastCampaign, campaign_id)
    if row is None:
        raise _error("vpn_broadcast_not_found")
    return row


def _summary(row: VpnBroadcastCampaign) -> dict:
    return {
        name: getattr(row, name)
        for name in (
            "id",
            "revision",
            "state",
            "created_at",
            "confirmed_at",
            "cancelled_at",
        )
    }


async def _detail(db: AsyncSession, row: VpnBroadcastCampaign) -> BroadcastDetail:
    return BroadcastDetail(
        **_summary(row),
        body=row.body,
        preview_revision=row.preview_revision,
        preview_recipient_count=row.preview_recipient_count,
        previewed_at=row.previewed_at,
        counts=await service.broadcast_counts(db, row.id),
    )


async def _saved(db: AsyncSession, row: VpnBroadcastCampaign) -> BroadcastDetail:
    result = await _detail(db, row)
    await db.commit()
    return result


@router.get("/config", response_model=BroadcastConfigResponse)
async def config(db: Database, admin: Administrator) -> BroadcastConfigResponse:
    del admin
    settings = get_settings()
    state = await registry_state(db, settings)
    count = None
    if state == "ready":
        count = await db.scalar(
            select(func.count())
            .select_from(VpnBotChat)
            .where(
                VpnBotChat.bot_id == configured_vpn_bot_id(settings),
                VpnBotChat.blocked_at.is_(None),
            )
        )
    explanations = {
        "history_unverified": "История чатов ещё не привязана к проверенному VPN-боту. Нужна проверка истории и восстановление реестра перед рассылкой",
        "bot_changed": "VPN-бот изменился. Перед рассылкой нужно проверить и привязать историю чатов к новому боту",
        "unconfigured": "VPN-бот не настроен. Настройте бота перед рассылкой",
        "ready": "Новость получат пользователи, которые уже писали боту и не заблокировали его",
    }
    explanation = explanations[state]
    if state == "ready" and not settings.vpn_broadcasts_enabled:
        explanation = "Отправка рассылок отключена в настройках сервера. Черновики можно сохранять"
    return BroadcastConfigResponse(
        enabled=settings.vpn_broadcasts_enabled,
        registry_state=state,
        eligible_recipients=count,
        explanation=explanation,
    )


@router.get("", response_model=list[BroadcastSummary])
async def list_campaigns(
    db: Database,
    admin: Administrator,
    before_id: int | None = Query(default=None, ge=1),
    limit: int = Query(default=50, ge=1, le=50),
) -> list[dict]:
    del admin
    query = (
        select(VpnBroadcastCampaign)
        .order_by(VpnBroadcastCampaign.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        query = query.where(VpnBroadcastCampaign.id < before_id)
    return [_summary(row) for row in await db.scalars(query)]


@router.post("", response_model=BroadcastDetail, status_code=201)
async def create(
    payload: BroadcastBodyRequest, request: Request, db: Database, admin: Administrator
) -> BroadcastDetail:
    _require_same_origin(request)
    try:
        return await _saved(
            db,
            await service.create_broadcast_draft(
                db, get_settings(), payload.body, actor_id=admin.id
            ),
        )
    except ValueError as error:
        await db.rollback()
        raise _error(str(error)) from None


@router.get("/{campaign_id}", response_model=BroadcastDetail)
async def detail(
    campaign_id: int, db: Database, admin: Administrator
) -> BroadcastDetail:
    del admin
    return await _detail(db, await _get(db, campaign_id))


@router.patch("/{campaign_id}", response_model=BroadcastDetail)
async def edit(
    campaign_id: int,
    payload: BroadcastBodyRequest,
    request: Request,
    db: Database,
    admin: Administrator,
) -> BroadcastDetail:
    _require_same_origin(request)
    try:
        return await _saved(
            db,
            await service.update_broadcast_draft(
                db, campaign_id, payload.body, actor_id=admin.id
            ),
        )
    except ValueError as error:
        await db.rollback()
        raise _error(str(error)) from None


@router.post("/{campaign_id}/preview", response_model=BroadcastDetail)
async def preview(
    campaign_id: int,
    payload: BroadcastEmptyRequest,
    request: Request,
    db: Database,
    admin: Administrator,
) -> BroadcastDetail:
    del payload, admin
    _require_same_origin(request)
    try:
        return await _saved(
            db, await service.preview_broadcast(db, get_settings(), campaign_id)
        )
    except ValueError as error:
        await db.rollback()
        raise _error(str(error)) from None


@router.post("/{campaign_id}/confirm", response_model=BroadcastDetail, status_code=202)
async def confirm(
    campaign_id: int,
    payload: BroadcastConfirmRequest,
    request: Request,
    db: Database,
    admin: Administrator,
) -> BroadcastDetail:
    _require_same_origin(request)
    settings = get_settings()
    row = await _get(db, campaign_id)
    if row.state == "draft" and not settings.vpn_broadcasts_enabled:
        raise _error("vpn_broadcasts_disabled")
    try:
        return await _saved(
            db,
            await service.confirm_broadcast(
                db,
                settings,
                campaign_id,
                expected_revision=payload.expected_revision,
                expected_recipient_count=payload.expected_recipient_count,
                actor_id=admin.id,
            ),
        )
    except ValueError as error:
        await db.rollback()
        raise _error(str(error)) from None


@router.post("/{campaign_id}/cancel", response_model=BroadcastDetail)
async def cancel(
    campaign_id: int,
    payload: BroadcastEmptyRequest,
    request: Request,
    db: Database,
    admin: Administrator,
) -> BroadcastDetail:
    del payload
    _require_same_origin(request)
    try:
        return await _saved(
            db, await service.cancel_broadcast(db, campaign_id, actor_id=admin.id)
        )
    except ValueError as error:
        await db.rollback()
        raise _error(str(error)) from None
