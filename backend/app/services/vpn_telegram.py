from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    VpnTelegramUpdate,
)

TelegramSender = Callable[[Settings, str, str], Awaitable[None]]


def sanitize_telegram_error(exc: Exception, settings: Settings) -> str:
    message = str(exc)
    if settings.vpn_telegram_bot_token:
        message = message.replace(settings.vpn_telegram_bot_token, "<redacted>")
    return message[:2000]


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    update_id: str
    chat_id: str
    user_id: str
    username: str | None
    first_name: str | None
    last_name: str | None
    text: str


def parse_telegram_message(payload: dict) -> TelegramMessage:
    message = payload.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if payload.get("update_id") is None or sender.get("id") is None or chat.get("id") is None:
        raise ValueError("Telegram update does not contain a message identity")
    return TelegramMessage(
        update_id=str(payload["update_id"]),
        chat_id=str(chat["id"]),
        user_id=str(sender["id"]),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        text=str(message.get("text") or "").strip(),
    )


async def send_telegram_message(settings: Settings, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.vpn_telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {
            "keyboard": [
                [{"text": "Статус"}, {"text": "Ключи"}],
                [{"text": "Поддержка"}],
            ],
            "resize_keyboard": True,
        },
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()


COMMANDS = {
    "/start": "start",
    "/status": "status",
    "статус": "status",
    "/keys": "keys",
    "ключи": "keys",
    "/support": "support",
    "поддержка": "support",
}


async def render_customer_response(
    session: AsyncSession,
    customer: VpnCustomer,
    command: str,
    settings: Settings,
) -> str:
    if command == "support":
        return settings.vpn_support_text
    if customer.status != "active":
        return "VPN-профиль отключён. Выберите «Поддержка» для связи с администратором."

    now = utcnow()
    valid_filter = (
        VpnSubscription.customer_id == customer.id,
        VpnSubscription.status.in_(("active", "trial")),
        or_(VpnSubscription.starts_at.is_(None), VpnSubscription.starts_at <= now),
        or_(VpnSubscription.expires_at.is_(None), VpnSubscription.expires_at > now),
    )
    subscriptions = (
        (
            await session.execute(
                select(VpnSubscription)
                .where(*valid_filter)
                .order_by(VpnSubscription.expires_at.asc(), VpnSubscription.id.asc())
            )
        )
        .scalars()
        .all()
    )

    if command in {"start", "status"}:
        intro = "VPN-бот готов. Оплата пока не подключена.\n" if command == "start" else ""
        if not subscriptions:
            return intro + (
                "Активной VPN-подписки нет. "
                "Выберите «Поддержка» для связи с администратором."
            )
        lines = [intro + "Ваши активные VPN-подписки:"]
        for subscription in subscriptions:
            expires = subscription.expires_at.isoformat() if subscription.expires_at else "без срока"
            lines.append(f"#{subscription.id}: {subscription.status}, до {expires}")
        return "\n".join(lines)

    if command == "keys":
        keys = (
            (
                await session.execute(
                    select(VpnAccessKey)
                    .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
                    .where(
                        *valid_filter,
                        VpnAccessKey.status == "active",
                        VpnAccessKey.config_uri.is_not(None),
                    )
                    .order_by(VpnAccessKey.id.asc())
                )
            )
            .scalars()
            .all()
        )
        if not keys:
            return "Активных VPN-ключей нет."
        return "Ваши VPN-ключи:\n" + "\n".join(
            f"{key.public_name or f'Ключ #{key.id}'}\n{key.config_uri}" for key in keys
        )

    return "Доступны команды: /status, /keys, /support."


async def process_telegram_update(
    session: AsyncSession,
    payload: dict,
    settings: Settings,
    sender: TelegramSender | None = None,
) -> dict[str, bool]:
    raw_update_id = payload.get("update_id")
    if raw_update_id is None:
        return {"processed": False, "duplicate": False}
    update_id = str(raw_update_id)
    existing = await session.scalar(
        select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == update_id)
    )
    if existing is not None:
        return {"processed": existing.processed_at is not None, "duplicate": True}

    update = VpnTelegramUpdate(update_id=update_id, payload=payload)
    session.add(update)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"processed": False, "duplicate": True}

    try:
        message = parse_telegram_message(payload)
    except ValueError as exc:
        update.error_message = sanitize_telegram_error(exc, settings)
        return {"processed": False, "duplicate": False}

    customer = await session.scalar(
        select(VpnCustomer).where(VpnCustomer.telegram_user_id == message.user_id)
    )
    if customer is None:
        customer = VpnCustomer(telegram_user_id=message.user_id, status="active")
        session.add(customer)
    customer.telegram_username = message.username
    customer.first_name = message.first_name
    customer.last_name = message.last_name
    await session.flush()
    update.customer_id = customer.id

    command = COMMANDS.get(message.text.casefold(), "start")
    response_text = await render_customer_response(session, customer, command, settings)
    active_sender = sender or send_telegram_message
    try:
        await active_sender(settings, message.chat_id, response_text)
    except Exception as exc:  # Telegram retries are prevented by the HTTP 200 acknowledgement.
        update.error_message = sanitize_telegram_error(exc, settings)
        key = await session.scalar(
            select(VpnAccessKey)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .where(
                VpnSubscription.customer_id == customer.id,
                VpnAccessKey.worker_id.is_not(None),
            )
            .order_by(VpnAccessKey.id.desc())
            .limit(1)
        )
        if key is not None and key.worker_id is not None:
            session.add(
                VpnNodeEvent(
                    worker_id=key.worker_id,
                    level="error",
                    event_type="telegram_delivery_failed",
                    message="Telegram VPN response delivery failed",
                    details={"update_id": message.update_id, "error": update.error_message},
                )
            )
        return {"processed": False, "duplicate": False}

    update.processed_at = utcnow()
    update.error_message = None
    return {"processed": True, "duplicate": False}
