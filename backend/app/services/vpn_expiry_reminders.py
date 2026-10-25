"""Opt-in expiry events, with durable claims committed before any Telegram HTTP."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import and_, case, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db.base import utcnow
from app.db.models import VpnBotChat, VpnCustomer, VpnExpiryReminder, VpnSubscription
from app.services.vpn_bot_chats import configured_vpn_bot_id, registry_state
from app.services.vpn_bot_delivery import (
    TelegramDeliveryResult,
    bot_cooldown_remaining,
    extend_bot_cooldown,
    post_telegram_payload,
    reserve_bot_send_slot,
)
from app.services.vpn_cabinet_settings import get_reminders_enabled_at
from app.services.vpn_policy import _as_utc
from app.services.vpn_portal_auth import identity_admitted
from app.services.vpn_telegram import telegram_cabinet_keyboard

_OFFSETS = {"3d": timedelta(days=3), "1d": timedelta(days=1), "expired": timedelta(0)}
_MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


def _event(expires_at, moment):
    if expires_at <= moment:
        return "expired"
    if expires_at <= moment + timedelta(days=1):
        return "1d"
    if expires_at <= moment + timedelta(days=3):
        return "3d"
    return None


async def _enqueue(db, settings, moment):
    enabled_at = _as_utc(await get_reminders_enabled_at(db))
    if enabled_at is None or await registry_state(db, settings) != "ready":
        return
    bot_id = configured_vpn_bot_id(settings)
    expiry = VpnSubscription.expires_at
    event = case(
        (expiry <= moment, "expired"),
        (expiry <= moment + timedelta(days=1), "1d"),
        else_="3d",
    )
    existing = (
        select(VpnExpiryReminder.id)
        .where(
            VpnExpiryReminder.subscription_id == VpnSubscription.id,
            VpnExpiryReminder.expires_at == expiry,
            VpnExpiryReminder.event_kind == event,
        )
        .exists()
    )
    rows = (
        await db.execute(
            select(VpnSubscription.id, expiry, VpnCustomer.telegram_user_id, event)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .join(
                VpnBotChat,
                and_(
                    VpnBotChat.bot_id == bot_id,
                    VpnBotChat.chat_id == VpnCustomer.telegram_user_id,
                ),
            )
            .where(
                VpnCustomer.status == "active",
                VpnCustomer.expiry_reminders.is_(True),
                VpnBotChat.blocked_at.is_(None),
                or_(
                    VpnSubscription.status.in_(("active", "trial")),
                    and_(VpnSubscription.status == "expired", expiry <= moment),
                ),
                or_(
                    and_(expiry <= moment, expiry >= enabled_at),
                    and_(
                        expiry > moment,
                        expiry <= moment + timedelta(days=1),
                        expiry >= enabled_at + timedelta(days=1),
                    ),
                    and_(
                        expiry > moment + timedelta(days=1),
                        expiry <= moment + timedelta(days=3),
                        expiry >= enabled_at + timedelta(days=3),
                    ),
                ),
                ~existing,
            )
            .order_by(expiry, VpnSubscription.id)
            .limit(20)
        )
    ).all()
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    for subscription_id, expires_at, chat_id, kind in rows:
        # Inbound updates lock chat before customer/subscription; do not invert it via FKs.
        chat = await db.scalar(
            select(VpnBotChat)
            .where(VpnBotChat.bot_id == bot_id, VpnBotChat.chat_id == chat_id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if chat is None or chat.blocked_at is not None:
            continue
        await db.execute(
            insert(VpnExpiryReminder)
            .values(
                subscription_id=subscription_id,
                expires_at=expires_at,
                event_kind=kind,
                enabled_at=enabled_at,
                bot_id=bot_id,
                chat_id=chat_id,
                state="pending",
                available_at=moment,
                attempt_count=0,
            )
            .on_conflict_do_nothing(
                index_elements=["subscription_id", "expires_at", "event_kind"]
            )
        )


def _finish(job, state, moment, error=None):
    job.state, job.finished_at, job.error_code = state, moment, error
    job.claim_token = job.lease_until = None


async def _recover(db, moment):
    jobs = (
        await db.scalars(
            select(VpnExpiryReminder)
            .where(
                VpnExpiryReminder.state.in_(("claimed", "sending")),
                VpnExpiryReminder.lease_until <= moment,
            )
            .order_by(VpnExpiryReminder.lease_until, VpnExpiryReminder.id)
            .with_for_update(skip_locked=True)
            .limit(20)
        )
    ).all()
    for job in jobs:
        # Even a stale claimed event is not automatically delivered after a restart.
        _finish(job, "uncertain", moment, "vpn_bot_delivery_uncertain")


@dataclass(frozen=True, slots=True)
class ReminderClaim:
    reminder_id: int
    bot_id: str
    chat_id: str = field(repr=False)
    token: str = field(repr=False)


async def _locked_reminder(db, reminder_id, *, skip_locked=False):
    owner = (
        await db.execute(
            select(
                VpnSubscription.customer_id,
                VpnSubscription.id,
                VpnExpiryReminder.bot_id,
                VpnExpiryReminder.chat_id,
            )
            .join(
                VpnExpiryReminder,
                VpnExpiryReminder.subscription_id == VpnSubscription.id,
            )
            .where(VpnExpiryReminder.id == reminder_id)
        )
    ).one_or_none()
    if owner is None:
        return None, None, None
    chat = await db.scalar(
        select(VpnBotChat)
        .where(VpnBotChat.bot_id == owner[2], VpnBotChat.chat_id == owner[3])
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if chat is None:
        return None, None, None
    customer = await db.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == owner[0])
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if customer is None:
        return None, None, None
    subscription = await db.scalar(
        select(VpnSubscription)
        .where(VpnSubscription.id == owner[1])
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if subscription is None:
        return None, None, None
    job = await db.scalar(
        select(VpnExpiryReminder)
        .where(VpnExpiryReminder.id == reminder_id)
        .with_for_update(skip_locked=skip_locked)
        .execution_options(populate_existing=True)
    )
    if (
        job is None
        or subscription.customer_id != customer.id
        or job.subscription_id != subscription.id
        or (job.bot_id, job.chat_id) != (chat.bot_id, chat.chat_id)
    ):
        return None, None, None
    return customer, subscription, job


async def _eligible(db, settings, customer, subscription, job, clock):
    chat = await db.get(
        VpnBotChat,
        (job.bot_id, job.chat_id),
        with_for_update=True,
        populate_existing=True,
    )
    enabled_at = _as_utc(await get_reminders_enabled_at(db))
    if enabled_at is None or await registry_state(db, settings) != "ready":
        return False
    moment = _as_utc(clock())
    expiry = _as_utc(subscription.expires_at)
    return (
        expiry is not None
        and expiry == _as_utc(job.expires_at)
        and subscription.customer_id == customer.id
        and subscription.status
        in (("active", "trial", "expired") if expiry <= moment else ("active", "trial"))
        and customer.status == "active"
        and customer.expiry_reminders
        and customer.telegram_user_id == job.chat_id
        and job.bot_id == configured_vpn_bot_id(settings)
        and chat is not None
        and chat.blocked_at is None
        and _as_utc(job.enabled_at) == enabled_at
        and expiry - _OFFSETS[job.event_kind] >= enabled_at
        and _event(expiry, moment) == job.event_kind
    )


async def _claim_reminder(session_factory, settings, clock):
    # Recovery and FK inserts each commit before owner locks to preserve lock order.
    async with session_factory() as db, db.begin():
        await _recover(db, _as_utc(clock()))
    async with session_factory() as db, db.begin():
        await _enqueue(db, settings, _as_utc(clock()))
    async with session_factory() as db, db.begin():
        if await get_reminders_enabled_at(db) is None:
            return False, None
        ids = (
            await db.scalars(
                select(VpnExpiryReminder.id)
                .where(
                    VpnExpiryReminder.state == "pending",
                    VpnExpiryReminder.available_at <= _as_utc(clock()),
                )
                .order_by(VpnExpiryReminder.available_at, VpnExpiryReminder.id)
                .limit(20)
            )
        ).all()
        processed = False
        for reminder_id in ids:
            customer, subscription, job = await _locked_reminder(
                db, reminder_id, skip_locked=True
            )
            if (
                job is None
                or job.state != "pending"
                or _as_utc(job.available_at) > _as_utc(clock())
            ):
                continue
            processed = True
            if not await _eligible(db, settings, customer, subscription, job, clock):
                _finish(job, "skipped", _as_utc(clock()), "vpn_reminder_ineligible")
                continue
            job.state, job.claim_token = "claimed", str(uuid4())
            job.lease_until = _as_utc(clock()) + timedelta(seconds=30)
            return True, ReminderClaim(job.id, job.bot_id, job.chat_id, job.claim_token)
        return processed, None


async def _release_rate_wait(session_factory, claim, delay, clock):
    async with session_factory() as db, db.begin():
        _, _, job = await _locked_reminder(db, claim.reminder_id)
        if (
            job is not None
            and job.claim_token == claim.token
            and job.state == "claimed"
        ):
            moment = _as_utc(clock())
            if job.lease_until is None or _as_utc(job.lease_until) <= moment:
                _finish(job, "uncertain", moment, "vpn_bot_delivery_uncertain")
                return
            job.state, job.available_at = "pending", moment + timedelta(seconds=delay)
            job.claim_token = job.lease_until = None


async def _mark_sending(session_factory, settings, claim, clock):
    async with session_factory() as db, db.begin():
        customer, subscription, job = await _locked_reminder(db, claim.reminder_id)
        if job is None or job.state != "claimed" or job.claim_token != claim.token:
            return False
        keyboard = telegram_cabinet_keyboard(
            settings,
            claim.chat_id,
            cabinet_allowed=await identity_admitted(
                db, settings, claim.chat_id, now=_as_utc(clock())
            ),
        )
        if keyboard is None:
            _finish(
                job, "skipped", _as_utc(clock()), "vpn_reminder_cabinet_unavailable"
            )
            return False
        if not await _eligible(db, settings, customer, subscription, job, clock):
            _finish(job, "skipped", _as_utc(clock()), "vpn_reminder_ineligible")
            return False
        moment = _as_utc(clock())
        if job.lease_until is None or _as_utc(job.lease_until) <= moment:
            return False
        expiry = _as_utc(job.expires_at)
        date = f"{expiry.day}\u00a0{_MONTHS[expiry.month - 1]} {expiry.year}, {expiry:%H:%M} UTC"
        text = (
            f"Срок подписки Veltrix VPN истек {date}"
            if job.event_kind == "expired"
            else f"Срок подписки Veltrix VPN истекает {date}"
        )
        payload = {
            "chat_id": claim.chat_id,
            "text": text,
            "reply_markup": keyboard,
            "link_preview_options": {"is_disabled": True},
        }
        job.state, job.sending_at = "sending", moment
        job.lease_until = moment + timedelta(seconds=30)
        job.attempt_count += 1
        return job.lease_until, payload


async def _finalize(session_factory, claim, result, clock):
    moment = _as_utc(clock())
    async with session_factory() as db, db.begin():
        _, _, job = await _locked_reminder(db, claim.reminder_id)
        if job is None or job.state != "sending" or job.claim_token != claim.token:
            return
        if result.state == "retry":
            job.state, job.available_at = (
                "pending",
                moment + timedelta(seconds=result.retry_after or 30),
            )
            job.claim_token = job.lease_until = job.sending_at = None
            job.error_code = result.error_code
            return
        state = (
            result.state
            if result.state in ("sent", "failed")
            else "skipped"
            if result.state == "blocked"
            else "uncertain"
        )
        _finish(job, state, moment, result.error_code)
        if result.state == "sent":
            job.telegram_message_id = result.message_id
        if result.state == "blocked":
            chat = await db.get(
                VpnBotChat,
                (job.bot_id, job.chat_id),
                with_for_update=True,
                populate_existing=True,
            )
            if chat is not None and _as_utc(chat.last_seen_at) <= moment:
                chat.blocked_at = moment


async def dispatch_next_expiry_reminder(
    session_factory, settings, *, sender=None, now=utcnow
):
    processed, claim = await _claim_reminder(session_factory, settings, now)
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
    delay = await bot_cooldown_remaining(
        session_factory, settings.vpn_telegram_bot_token, now=_as_utc(now())
    )
    if delay:
        await _release_rate_wait(session_factory, claim, delay, now)
        return True
    sending = await _mark_sending(session_factory, settings, claim, now)
    if not sending:
        return True
    deadline, payload = sending
    if deadline <= _as_utc(now()) + timedelta(seconds=10):
        await _finalize(
            session_factory,
            claim,
            TelegramDeliveryResult(
                "retry", retry_after=30, error_code="vpn_bot_rate_wait"
            ),
            now,
        )
        return True
    try:
        result = await asyncio.wait_for(
            (sender or post_telegram_payload)(settings.vpn_telegram_bot_token, payload),
            timeout=10,
        )
    except Exception:  # noqa: BLE001 - a lost response is not evidence of non-delivery
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
    await _finalize(session_factory, claim, result, now)
    return True
