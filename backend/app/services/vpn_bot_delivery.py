"""One HTTP attempt with explicit uncertainty and a bot-wide database rate gate."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import VpnBotChatRateGate, VpnBotRateGate
from app.services.vpn_policy import _as_utc

_delivery_database = None


def _delivery_sessions(settings: Settings):
    from app.db.session import create_vpn_control_database

    global _delivery_database
    if _delivery_database is None:
        # Callers may hold a request connection; a separate bounded pool avoids starvation.
        _delivery_database = create_vpn_control_database(settings)
    return _delivery_database.session_factory


async def close_bot_delivery_database() -> None:
    global _delivery_database
    database, _delivery_database = _delivery_database, None
    if database is not None:
        await database.engine.dispose()


async def _database_now(db: AsyncSession) -> datetime:
    clock = (
        func.clock_timestamp() if db.bind.dialect.name == "postgresql" else func.now()
    )
    return _as_utc(await db.scalar(select(clock)))


@dataclass(frozen=True, slots=True)
class TelegramDeliveryResult:
    state: Literal["sent", "retry", "blocked", "failed", "uncertain"]
    message_id: int | None = None
    retry_after: int | None = None
    error_code: str | None = None


class VpnBotDeliveryError(RuntimeError):
    pass


def _telegram_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, timeout=10)


async def post_telegram_payload(token: str, payload: dict) -> TelegramDeliveryResult:
    uncertain = TelegramDeliveryResult(
        "uncertain", error_code="vpn_bot_delivery_uncertain"
    )
    try:
        async with _telegram_client() as client:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage", json=payload
            )
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
        return TelegramDeliveryResult(
            "retry", retry_after=30, error_code="vpn_bot_unreachable"
        )
    except (httpx.HTTPError, httpx.InvalidURL):
        return uncertain
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        return uncertain
    try:
        data = response.json()
    except (ValueError, RecursionError):
        return uncertain
    if not isinstance(data, dict):
        return uncertain
    if 200 <= response.status_code < 300 and data.get("ok") is True:
        result = data.get("result")
        if isinstance(result, dict):
            message_id, chat = result.get("message_id"), result.get("chat")
            if (
                type(message_id) is int
                and 0 < message_id <= 2**63 - 1
                and isinstance(chat, dict)
                and type(chat.get("id")) is int
                and str(chat["id"]) == str(payload.get("chat_id"))
            ):
                return TelegramDeliveryResult("sent", message_id=message_id)
        return uncertain
    if data.get("ok") is not False or type(data.get("error_code")) is not int:
        return uncertain
    code = data["error_code"]
    if code == 429:
        parameters = data.get("parameters")
        delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
        if type(delay) is not int or not 1 <= delay <= 86400:
            delay = 60
        return TelegramDeliveryResult(
            "retry", retry_after=delay, error_code="vpn_bot_rate_limited"
        )
    if code == 403:
        return TelegramDeliveryResult("blocked", error_code="vpn_bot_blocked")
    if code == 400:
        return TelegramDeliveryResult("failed", error_code="vpn_bot_request_rejected")
    if code == 401:
        return TelegramDeliveryResult("failed", error_code="vpn_bot_auth_failed")
    return uncertain


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _global_gate(db: AsyncSession, fingerprint: str) -> VpnBotRateGate:
    query = (
        select(VpnBotRateGate)
        .where(VpnBotRateGate.token_fingerprint == fingerprint)
        .with_for_update()
    )
    row = await db.scalar(query.execution_options(populate_existing=True))
    if row is None:
        try:
            async with db.begin_nested():
                row = VpnBotRateGate(token_fingerprint=fingerprint)
                db.add(row)
                await db.flush()
        except IntegrityError:
            row = await db.scalar(query.execution_options(populate_existing=True))
            if row is None:
                raise
    return row


async def reserve_bot_send_slot(
    session_factory: async_sessionmaker[AsyncSession],
    token: str,
    chat_id: str,
    *,
    now: datetime | None = None,
) -> float:
    async with session_factory() as db, db.begin():
        fingerprint = token_fingerprint(token)
        gate = await _global_gate(db, fingerprint)
        moment = _as_utc(now) if now is not None else await _database_now(db)
        chat = await db.get(
            VpnBotChatRateGate, (fingerprint, chat_id), with_for_update=True
        )
        if chat is None:
            chat = VpnBotChatRateGate(token_fingerprint=fingerprint, chat_id=chat_id)
            db.add(chat)
        due = max(
            [
                moment,
                *[
                    _as_utc(value)
                    for value in (
                        gate.next_send_at,
                        gate.cooldown_until,
                        chat.next_send_at,
                    )
                    if value is not None
                ],
            ]
        )
        if due > moment:
            return (due - moment).total_seconds()
        gate.next_send_at = moment + timedelta(milliseconds=50)
        chat.next_send_at = moment + timedelta(seconds=1)
        return 0


async def extend_bot_cooldown(
    session_factory: async_sessionmaker[AsyncSession],
    token: str,
    delay: int,
    *,
    now: datetime | None = None,
) -> None:
    if type(delay) is not int or not 1 <= delay <= 86400:
        delay = 60
    async with session_factory() as db, db.begin():
        gate = await _global_gate(db, token_fingerprint(token))
        moment = _as_utc(now) if now is not None else await _database_now(db)
        gate.cooldown_until = max(
            _as_utc(gate.cooldown_until) or moment, moment + timedelta(seconds=delay)
        )


async def bot_cooldown_remaining(
    session_factory, token: str, *, now: datetime
) -> float:
    async with session_factory() as db:
        value = await db.scalar(
            select(VpnBotRateGate.cooldown_until).where(
                VpnBotRateGate.token_fingerprint == token_fingerprint(token),
            )
        )
        return (
            max(0, (_as_utc(value) - _as_utc(now)).total_seconds())
            if value is not None
            else 0
        )


async def send_gated_payload(
    settings: Settings,
    payload: dict,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> TelegramDeliveryResult:
    if session_factory is None:
        session_factory = _delivery_sessions(settings)
    deadline = time.monotonic() + 10
    while True:
        delay = await reserve_bot_send_slot(
            session_factory, settings.vpn_telegram_bot_token, str(payload["chat_id"])
        )
        if not delay:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return TelegramDeliveryResult(
                "retry", retry_after=max(1, int(delay)), error_code="vpn_bot_rate_wait"
            )
        await asyncio.sleep(min(delay, remaining, 1))
    result = await post_telegram_payload(settings.vpn_telegram_bot_token, payload)
    if result.error_code == "vpn_bot_rate_limited":
        await extend_bot_cooldown(
            session_factory, settings.vpn_telegram_bot_token, result.retry_after
        )
    return result


def require_delivered(result: TelegramDeliveryResult) -> None:
    if result.state != "sent":
        raise VpnBotDeliveryError(result.error_code or "vpn_bot_delivery_uncertain")
