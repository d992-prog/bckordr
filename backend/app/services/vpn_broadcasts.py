"""Explicitly confirmed plain-text campaigns; no network calls in draft workflow."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, insert, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnBotChat, VpnBroadcastCampaign, VpnBroadcastDelivery
from app.services.audit import add_audit_log
from app.services.vpn_bot_chats import configured_vpn_bot_id, registry_state
from app.services.vpn_bot_delivery import (
    TelegramDeliveryResult,
    bot_cooldown_remaining,
    extend_bot_cooldown,
    post_telegram_payload,
    reserve_bot_send_slot,
)
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


@dataclass(frozen=True, slots=True)
class BroadcastClaim:
    campaign_id: int
    delivery_id: int
    bot_id: str
    chat_id: str = field(repr=False)
    body: str = field(repr=False)
    token: str = field(repr=False)


async def _locked_pair(db, campaign_id, delivery_id, *, skip_locked=False):
    campaign = await db.scalar(
        select(VpnBroadcastCampaign)
        .where(VpnBroadcastCampaign.id == campaign_id)
        .with_for_update(skip_locked=skip_locked)
    )
    if campaign is None:
        return None, None
    job = await db.scalar(
        select(VpnBroadcastDelivery)
        .where(
            VpnBroadcastDelivery.id == delivery_id,
            VpnBroadcastDelivery.campaign_id == campaign_id,
        )
        .with_for_update(skip_locked=skip_locked)
    )
    return campaign, job


async def _complete(db, campaign):
    await db.flush()
    if campaign.state == "running" and not await db.scalar(
        select(VpnBroadcastDelivery.id)
        .where(
            VpnBroadcastDelivery.campaign_id == campaign.id,
            VpnBroadcastDelivery.state.in_(("pending", "claimed", "sending")),
        )
        .limit(1)
    ):
        campaign.state = "completed"


async def _recover(db, clock):
    candidates = (
        await db.execute(
            select(VpnBroadcastDelivery.campaign_id, VpnBroadcastDelivery.id)
            .where(
                VpnBroadcastDelivery.state.in_(("claimed", "sending")),
                VpnBroadcastDelivery.lease_until <= _as_utc(clock()),
            )
            .order_by(VpnBroadcastDelivery.lease_until, VpnBroadcastDelivery.id)
            .limit(20)
        )
    ).all()
    for campaign_id, delivery_id in candidates:
        campaign, job = await _locked_pair(
            db, campaign_id, delivery_id, skip_locked=True
        )
        moment = _as_utc(clock())
        if (
            job is None
            or job.state not in ("claimed", "sending")
            or job.lease_until is None
            or _as_utc(job.lease_until) > moment
        ):
            continue
        if job.state == "sending" or job.sending_at is not None:
            job.state, job.finished_at, job.error_code = (
                "uncertain",
                moment,
                "vpn_bot_delivery_uncertain",
            )
        elif campaign.state == "running":
            job.state, job.available_at = "pending", moment
        else:
            job.state, job.finished_at = "cancelled", moment
        job.claim_token = job.lease_until = None
        await _complete(db, campaign)


async def _claim_delivery(session_factory, settings, clock):
    async with session_factory() as db, db.begin():
        await _recover(db, clock)
        if await registry_state(db, settings) != "ready":
            return False, None
        bot_id, moment = configured_vpn_bot_id(settings), _as_utc(clock())
        due = (
            select(VpnBroadcastDelivery.id)
            .where(
                VpnBroadcastDelivery.campaign_id == VpnBroadcastCampaign.id,
                VpnBroadcastDelivery.state == "pending",
                VpnBroadcastDelivery.available_at <= moment,
            )
            .exists()
        )
        campaign = await db.scalar(
            select(VpnBroadcastCampaign)
            .where(
                VpnBroadcastCampaign.state == "running",
                VpnBroadcastCampaign.bot_id == bot_id,
                due,
            )
            .order_by(VpnBroadcastCampaign.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if campaign is None:
            return False, None
        job = await db.scalar(
            select(VpnBroadcastDelivery)
            .where(
                VpnBroadcastDelivery.campaign_id == campaign.id,
                VpnBroadcastDelivery.state == "pending",
                VpnBroadcastDelivery.available_at <= moment,
            )
            .order_by(VpnBroadcastDelivery.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False, None
        chat = await db.get(VpnBotChat, (job.bot_id, job.chat_id), with_for_update=True)
        if chat is None or chat.blocked_at is not None:
            job.state, job.finished_at, job.error_code = (
                "skipped",
                _as_utc(clock()),
                "vpn_bot_blocked",
            )
            await _complete(db, campaign)
            return True, None
        job.state, job.claim_token = "claimed", str(uuid4())
        job.lease_until = _as_utc(clock()) + timedelta(seconds=30)
        return True, BroadcastClaim(
            campaign.id, job.id, job.bot_id, job.chat_id, campaign.body, job.claim_token
        )


async def _release_rate_wait(session_factory, claim, delay, clock):
    async with session_factory() as db, db.begin():
        campaign, job = await _locked_pair(db, claim.campaign_id, claim.delivery_id)
        if (
            job is not None
            and job.claim_token == claim.token
            and job.state == "claimed"
            and campaign.state == "running"
        ):
            job.state, job.available_at = (
                "pending",
                _as_utc(clock()) + timedelta(seconds=delay),
            )
            job.claim_token = job.lease_until = None


async def _mark_sending(session_factory, settings, claim, clock):
    async with session_factory() as db, db.begin():
        campaign, job = await _locked_pair(db, claim.campaign_id, claim.delivery_id)
        moment = _as_utc(clock())
        if (
            job is None
            or job.claim_token != claim.token
            or job.state != "claimed"
            or campaign.state != "running"
            or job.lease_until is None
            or _as_utc(job.lease_until) <= moment
            or await registry_state(db, settings) != "ready"
            or configured_vpn_bot_id(settings) != claim.bot_id
        ):
            return False
        chat = await db.get(VpnBotChat, (job.bot_id, job.chat_id), with_for_update=True)
        moment = _as_utc(clock())
        if job.lease_until is None or _as_utc(job.lease_until) <= moment:
            return False
        if chat is None or chat.blocked_at is not None:
            job.state, job.finished_at, job.error_code = (
                "skipped",
                moment,
                "vpn_bot_blocked",
            )
            job.lease_until = None
            await _complete(db, campaign)
            return False
        job.state, job.sending_at = "sending", moment
        job.lease_until = moment + timedelta(seconds=30)
        job.attempt_count += 1
        return True


async def _finalize_delivery(session_factory, claim, result, clock):
    async with session_factory() as db, db.begin():
        campaign, job = await _locked_pair(db, claim.campaign_id, claim.delivery_id)
        if job is None or job.claim_token != claim.token or job.state != "sending":
            return
        moment = _as_utc(clock())
        job.error_code, job.lease_until = result.error_code, None
        if result.state == "retry" and campaign.state == "running":
            job.state, job.available_at = (
                "pending",
                moment + timedelta(seconds=result.retry_after or 30),
            )
            job.claim_token = job.sending_at = None
            return
        job.finished_at = moment
        if result.state == "sent":
            job.state, job.telegram_message_id = "sent", result.message_id
        elif result.state == "blocked":
            job.state = "skipped"
            chat = await db.get(
                VpnBotChat, (job.bot_id, job.chat_id), with_for_update=True
            )
            if chat is not None:
                chat.blocked_at = moment
        elif result.state == "failed":
            job.state = "failed"
        elif result.state == "retry":
            job.state = "cancelled"
        else:
            job.state = "uncertain"
        if result.error_code == "vpn_bot_auth_failed" and campaign.state == "running":
            campaign.state = "failed"
            await db.execute(
                update(VpnBroadcastDelivery)
                .where(
                    VpnBroadcastDelivery.campaign_id == campaign.id,
                    VpnBroadcastDelivery.state.in_(("pending", "claimed")),
                    VpnBroadcastDelivery.sending_at.is_(None),
                )
                .values(
                    state="cancelled",
                    finished_at=moment,
                    claim_token=None,
                    lease_until=None,
                    error_code="vpn_bot_auth_failed",
                )
            )
        await _complete(db, campaign)


async def dispatch_next_broadcast_delivery(
    session_factory, settings, *, sender=None, now=utcnow
) -> bool:
    processed, claim = await _claim_delivery(session_factory, settings, now)
    if claim is None:
        return processed
    delay = await reserve_bot_send_slot(
        session_factory,
        settings.vpn_telegram_bot_token,
        claim.chat_id,
        now=_as_utc(now()),
    )
    if delay:
        await _release_rate_wait(session_factory, claim, delay, now)
        return True
    if not await _mark_sending(session_factory, settings, claim, now):
        return True
    delay = await bot_cooldown_remaining(
        session_factory, settings.vpn_telegram_bot_token, now=_as_utc(now())
    )
    if delay:
        await _finalize_delivery(
            session_factory,
            claim,
            TelegramDeliveryResult(
                "retry",
                retry_after=max(1, int(delay + 1)),
                error_code="vpn_bot_rate_limited",
            ),
            now,
        )
        return True
    try:
        result = await asyncio.wait_for(
            (sender or post_telegram_payload)(
                settings.vpn_telegram_bot_token,
                {
                    "chat_id": claim.chat_id,
                    "text": claim.body,
                    "link_preview_options": {"is_disabled": True},
                },
            ),
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - a lost response is never evidence of non-delivery
        result = TelegramDeliveryResult(
            "uncertain", error_code="vpn_bot_delivery_uncertain"
        )
    if result.error_code == "vpn_bot_rate_limited":
        await extend_bot_cooldown(
            session_factory,
            settings.vpn_telegram_bot_token,
            result.retry_after,
            now=_as_utc(now()),
        )
    await _finalize_delivery(session_factory, claim, result, now)
    return True
