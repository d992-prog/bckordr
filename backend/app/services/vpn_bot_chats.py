"""Known private bot chats; browser login is deliberately not a source."""

from __future__ import annotations

import re
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import AppSetting, VpnBotChat, VpnTelegramUpdate
from app.services.vpn_policy import _as_utc

if TYPE_CHECKING:
    from app.services.vpn_telegram import TelegramMessage

_BOT_TOKEN = re.compile(r"([1-9][0-9]*):[A-Za-z0-9_-]+", re.ASCII)
REGISTRY_BOT_KEY = "vpn_bot_registry_bot_id_v1"


class BotRegistryConfigurationError(ValueError):
    pass


def _positive_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdecimal()
        or len(value) > 16
    ):
        return None
    number = int(value)
    return value if 1 <= number <= 2**52 - 1 and str(number) == value else None


def configured_vpn_bot_id(settings: Settings) -> str:
    match = _BOT_TOKEN.fullmatch(settings.vpn_telegram_bot_token)
    if match is None or _positive_id(match[1]) is None:
        raise BotRegistryConfigurationError("vpn_bot_unconfigured")
    return match[1]


async def registry_state(db: AsyncSession, settings: Settings) -> str:
    try:
        bot_id = configured_vpn_bot_id(settings)
    except BotRegistryConfigurationError:
        return "unconfigured"
    value = await db.scalar(
        select(AppSetting.value).where(AppSetting.key == REGISTRY_BOT_KEY)
    )
    if value == bot_id:
        return "ready"
    if value is None or value == f"backfilling:{bot_id}":
        return "history_unverified"
    return "bot_changed"


async def _record_chat(
    db: AsyncSession,
    bot_id: str,
    chat_id: str,
    update_id: int,
    seen_at: datetime,
    *,
    unblock: bool,
) -> None:
    query = select(VpnBotChat).where(
        VpnBotChat.bot_id == bot_id, VpnBotChat.chat_id == chat_id
    )
    row = await db.scalar(
        query.with_for_update().execution_options(populate_existing=True)
    )
    if row is None:
        try:
            async with db.begin_nested():
                row = VpnBotChat(
                    bot_id=bot_id,
                    chat_id=chat_id,
                    first_seen_at=seen_at,
                    last_seen_at=seen_at,
                    last_update_id=update_id,
                )
                db.add(row)
                await db.flush()
            return
        except IntegrityError:
            row = await db.scalar(
                query.with_for_update().execution_options(populate_existing=True)
            )
            if row is None:
                raise
    row.first_seen_at = min(_as_utc(row.first_seen_at), seen_at)
    if seen_at >= _as_utc(row.last_seen_at):
        if unblock and row.last_update_id != update_id:
            row.blocked_at = None
        row.last_seen_at, row.last_update_id = seen_at, update_id
    await db.flush()


async def record_private_bot_chat(
    db: AsyncSession,
    settings: Settings,
    message: TelegramMessage,
    *,
    now: datetime | None = None,
) -> bool:
    if (
        message.chat_type != "private"
        or _positive_id(message.chat_id) is None
        or message.chat_id != message.user_id
    ):
        return False
    bot_id = configured_vpn_bot_id(settings)
    raw_id = message.update_id
    if (
        not isinstance(raw_id, str)
        or not raw_id.isascii()
        or not raw_id.isdecimal()
        or len(raw_id) > 19
    ):
        return False
    update_id = int(raw_id)
    if str(update_id) != raw_id or update_id > 2**63 - 1:
        return False
    await _record_chat(
        db, bot_id, message.chat_id, update_id, _as_utc(now or utcnow()), unblock=True
    )
    return True


async def backfill_private_bot_chats(
    db: AsyncSession,
    bot_id: str,
    *,
    after_id: int = 0,
    limit: int = 500,
) -> tuple[int, int | None]:
    """Return accepted records and last scanned ID for one bounded historical page."""
    if (
        _positive_id(bot_id) is None
        or type(limit) is not int
        or not 1 <= limit <= 500
        or type(after_id) is not int
        or after_id < 0
    ):
        raise ValueError("vpn_bot_registry_invalid_page")
    updates = (
        await db.scalars(
            select(VpnTelegramUpdate)
            .where(VpnTelegramUpdate.id > after_id)
            .order_by(VpnTelegramUpdate.id)
            .limit(limit)
        )
    ).all()
    accepted = 0
    for update in updates:
        payload = update.payload
        if (
            not isinstance(payload, dict)
            or type(raw_id := payload.get("update_id")) is not int
            or not 0 <= raw_id <= 2**63 - 1
            or str(raw_id) != update.update_id
        ):
            continue
        message = payload.get("message")
        if not isinstance(message, dict):
            continue
        sender, chat = message.get("from"), message.get("chat")
        if (
            not isinstance(sender, dict)
            or not isinstance(chat, dict)
            or chat.get("type") != "private"
        ):
            continue
        sender_id, chat_id = sender.get("id"), chat.get("id")
        if (
            type(sender_id) is not int
            or type(chat_id) is not int
            or sender_id != chat_id
            or not 1 <= chat_id <= 2**52 - 1
        ):
            continue
        seen_at = _as_utc(update.created_at)
        if seen_at is None:
            continue
        await _record_chat(db, bot_id, str(chat_id), raw_id, seen_at, unblock=False)
        accepted += 1
    return accepted, updates[-1].id if updates else None
