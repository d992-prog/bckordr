from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnNodeEvent, VpnSubscription


READY_TEXT = "Veltrix VPN\nПрофиль готов. Откройте личный кабинет, чтобы подключиться."
READY_NOTICE_SEND_TIMEOUT_SECONDS = 30.0
READY_NOTICE_CLAIM_STALE_AFTER = timedelta(minutes=2)
READY_NOTICE_FAILURE_BACKOFF = timedelta(minutes=1)
ReadyNoticeSender = Callable[[Settings, str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class ReadyNoticeClaim:
    access_key_id: int
    worker_id: int
    chat_id: str
    claimed_at: datetime


async def claim_ready_notice(
    db: AsyncSession,
    *,
    now: datetime | None = None,
) -> ReadyNoticeClaim | None:
    current_time = now or utcnow()
    recent_failure = exists(
        select(VpnNodeEvent.id).where(
            VpnNodeEvent.event_type == "telegram_ready_delivery_failed",
            VpnNodeEvent.created_at
            >= current_time - READY_NOTICE_FAILURE_BACKOFF,
            VpnNodeEvent.details["access_key_id"].as_integer() == VpnAccessKey.id,
        )
    )
    row = (
        await db.execute(
            select(VpnAccessKey, VpnCustomer.telegram_user_id)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(
                VpnAccessKey.status == "active",
                VpnAccessKey.config_uri.is_not(None),
                VpnAccessKey.config_uri != "",
                VpnAccessKey.worker_id.is_not(None),
                or_(
                    VpnAccessKey.ready_notice_claimed_at.is_(None),
                    VpnAccessKey.ready_notice_claimed_at
                    <= current_time - READY_NOTICE_CLAIM_STALE_AFTER,
                ),
                VpnAccessKey.ready_notified_at.is_(None),
                VpnSubscription.status.in_(("active", "trial")),
                or_(
                    VpnSubscription.starts_at.is_(None),
                    VpnSubscription.starts_at <= current_time,
                ),
                or_(
                    VpnSubscription.expires_at.is_(None),
                    VpnSubscription.expires_at > current_time,
                ),
                VpnCustomer.status == "active",
                VpnCustomer.telegram_user_id.is_not(None),
                VpnCustomer.telegram_user_id != "",
                ~recent_failure,
            )
            .order_by(VpnAccessKey.id)
            .limit(1)
            .with_for_update(skip_locked=True, of=VpnAccessKey)
        )
    ).first()
    if row is None:
        return None
    access_key, chat_id = row
    access_key.ready_notice_claimed_at = current_time
    await db.flush()
    return ReadyNoticeClaim(
        access_key_id=access_key.id,
        worker_id=access_key.worker_id,
        chat_id=chat_id,
        claimed_at=current_time,
    )


async def deliver_next_ready_notice(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    sender: ReadyNoticeSender,
    *,
    now: datetime | None = None,
) -> bool:
    current_time = now or utcnow()
    async with session_factory() as db:
        claim = await claim_ready_notice(db, now=current_time)
        if claim is None:
            await db.rollback()
            return False
        await db.commit()

    try:
        await asyncio.wait_for(
            sender(settings, claim.chat_id, READY_TEXT),
            timeout=READY_NOTICE_SEND_TIMEOUT_SECONDS,
        )
    except Exception:
        async with session_factory() as db:
            await db.execute(
                update(VpnAccessKey)
                .where(
                    VpnAccessKey.id == claim.access_key_id,
                    VpnAccessKey.ready_notice_claimed_at == claim.claimed_at,
                )
                .values(ready_notice_claimed_at=None)
            )
            db.add(
                VpnNodeEvent(
                    worker_id=claim.worker_id,
                    level="error",
                    event_type="telegram_ready_delivery_failed",
                    message="Telegram VPN ready notification delivery failed",
                    details={"access_key_id": claim.access_key_id},
                    created_at=current_time,
                )
            )
            await db.commit()
        return False

    async with session_factory() as db:
        await db.execute(
            update(VpnAccessKey)
            .where(
                VpnAccessKey.id == claim.access_key_id,
                VpnAccessKey.ready_notice_claimed_at == claim.claimed_at,
                VpnAccessKey.ready_notified_at.is_(None),
            )
            .values(
                ready_notice_claimed_at=None,
                ready_notified_at=current_time,
            )
        )
        await db.commit()
    return True
