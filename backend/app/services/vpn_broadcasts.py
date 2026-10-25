"""Explicitly confirmed plain-text campaigns; no network calls in draft workflow."""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import and_, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnBotChat, VpnBroadcastCampaign, VpnBroadcastDelivery
from app.services.audit import add_audit_log
from app.services.vpn_bot_chats import configured_vpn_bot_id, registry_state
from app.services.vpn_policy import _as_utc


def validate_body(body: object) -> str:
    if not isinstance(body, str) or not body.strip():
        raise ValueError("vpn_broadcast_invalid_body")
    body = body.replace("\r\n", "\n")
    try:
        body.encode("utf-8", "strict")
    except UnicodeEncodeError:
        raise ValueError("vpn_broadcast_invalid_body") from None
    if len(body) > 4096:
        raise ValueError("vpn_broadcast_invalid_body")
    return body


async def _campaign(db: AsyncSession, campaign_id: int) -> VpnBroadcastCampaign:
    await db.flush()
    row = await db.scalar(
        select(VpnBroadcastCampaign)
        .where(VpnBroadcastCampaign.id == campaign_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if row is None:
        raise ValueError("vpn_broadcast_not_found")
    return row


async def _audit(
    db: AsyncSession,
    row: VpnBroadcastCampaign,
    actor_id: int | None,
    action: str,
    **extra: int,
) -> None:
    await add_audit_log(
        db,
        actor_user_id=actor_id,
        target_user_id=None,
        action=action,
        details=json.dumps({"campaign_id": row.id, "revision": row.revision, **extra}),
    )


async def _require_registry(
    db: AsyncSession, settings: Settings, row: VpnBroadcastCampaign
) -> None:
    if await registry_state(
        db, settings
    ) != "ready" or row.bot_id != configured_vpn_bot_id(settings):
        raise ValueError("vpn_broadcast_registry_unavailable")


def _audience(row: VpnBroadcastCampaign, cutoff: datetime):
    return select(VpnBotChat).where(
        VpnBotChat.bot_id == row.bot_id,
        VpnBotChat.blocked_at.is_(None),
        VpnBotChat.first_seen_at <= cutoff,
    )


async def create_broadcast_draft(
    db: AsyncSession,
    settings: Settings,
    body: str,
    *,
    actor_id: int | None,
    now: datetime | None = None,
) -> VpnBroadcastCampaign:
    body = validate_body(body)
    row = VpnBroadcastCampaign(
        bot_id=configured_vpn_bot_id(settings),
        body=body,
        created_by_user_id=actor_id,
        created_at=_as_utc(now or utcnow()),
    )
    db.add(row)
    await db.flush()
    await _audit(db, row, actor_id, "vpn_broadcast_draft_created")
    return row


async def update_broadcast_draft(
    db: AsyncSession, campaign_id: int, body: str, *, actor_id: int | None
) -> VpnBroadcastCampaign:
    body = validate_body(body)
    row = await _campaign(db, campaign_id)
    if row.state != "draft":
        raise ValueError("vpn_broadcast_not_draft")
    row.body, row.revision = body, row.revision + 1
    row.preview_revision = row.preview_recipient_count = row.previewed_at = None
    await db.flush()
    await _audit(db, row, actor_id, "vpn_broadcast_draft_updated")
    return row


async def preview_broadcast(
    db: AsyncSession,
    settings: Settings,
    campaign_id: int,
    *,
    now: datetime | None = None,
) -> VpnBroadcastCampaign:
    row = await _campaign(db, campaign_id)
    if row.state != "draft":
        raise ValueError("vpn_broadcast_not_draft")
    await _require_registry(db, settings, row)
    moment = _as_utc(now or utcnow())
    count = await db.scalar(
        select(func.count()).select_from(_audience(row, moment).subquery())
    )
    row.preview_revision, row.preview_recipient_count, row.previewed_at = (
        row.revision,
        count,
        moment,
    )
    await db.flush()
    return row


async def confirm_broadcast(
    db: AsyncSession,
    settings: Settings,
    campaign_id: int,
    *,
    expected_revision: int,
    expected_recipient_count: int,
    actor_id: int | None,
    now: datetime | None = None,
) -> VpnBroadcastCampaign:
    row = await _campaign(db, campaign_id)
    if row.state != "draft":
        return row
    await _require_registry(db, settings, row)
    if (
        type(expected_revision) is not int
        or type(expected_recipient_count) is not int
        or expected_revision != row.revision
        or row.preview_revision != row.revision
        or expected_recipient_count != row.preview_recipient_count
        or row.previewed_at is None
    ):
        raise ValueError("vpn_broadcast_preview_changed")
    audience = _audience(row, _as_utc(row.previewed_at)).subquery()
    if (
        await db.scalar(select(func.count()).select_from(audience))
        != expected_recipient_count
    ):
        raise ValueError("vpn_broadcast_audience_changed")
    moment = _as_utc(now or utcnow())
    async with db.begin_nested():
        inserted = await db.execute(
            insert(VpnBroadcastDelivery).from_select(
                [
                    "campaign_id",
                    "bot_id",
                    "chat_id",
                    "state",
                    "available_at",
                    "attempt_count",
                ],
                select(
                    literal(row.id),
                    audience.c.bot_id,
                    audience.c.chat_id,
                    literal("pending"),
                    literal(moment),
                    literal(0),
                ),
            )
        )
        # Recheck the actual INSERT snapshot too: a chat can block/unblock between SQL statements.
        if inserted.rowcount != expected_recipient_count:
            raise ValueError("vpn_broadcast_audience_changed")
    row.state = "running" if expected_recipient_count else "completed"
    row.confirmed_at, row.confirmed_by_user_id = moment, actor_id
    await db.flush()
    await _audit(
        db,
        row,
        actor_id,
        "vpn_broadcast_confirmed",
        recipient_count=expected_recipient_count,
    )
    return row


async def cancel_broadcast(
    db: AsyncSession,
    campaign_id: int,
    *,
    actor_id: int | None,
    now: datetime | None = None,
) -> VpnBroadcastCampaign:
    row = await _campaign(db, campaign_id)
    if row.state not in ("draft", "running"):
        return row
    moment = _as_utc(now or utcnow())
    row.state, row.cancelled_at, row.cancelled_by_user_id = (
        "cancelled",
        moment,
        actor_id,
    )
    await db.execute(
        update(VpnBroadcastDelivery)
        .where(
            VpnBroadcastDelivery.campaign_id == row.id,
            or_(
                VpnBroadcastDelivery.state == "pending",
                and_(
                    VpnBroadcastDelivery.state == "claimed",
                    VpnBroadcastDelivery.sending_at.is_(None),
                ),
            ),
        )
        .values(
            state="cancelled", finished_at=moment, claim_token=None, lease_until=None
        )
    )
    await db.flush()
    await _audit(db, row, actor_id, "vpn_broadcast_cancelled")
    return row


async def broadcast_counts(db: AsyncSession, campaign_id: int) -> dict[str, int]:
    result = {
        state: 0
        for state in (
            "pending",
            "claimed",
            "sending",
            "sent",
            "failed",
            "skipped",
            "uncertain",
            "cancelled",
        )
    }
    rows = await db.execute(
        select(VpnBroadcastDelivery.state, func.count())
        .where(VpnBroadcastDelivery.campaign_id == campaign_id)
        .group_by(VpnBroadcastDelivery.state)
    )
    result.update(rows.all())
    return {"recipient_count": sum(result.values()), **result}
