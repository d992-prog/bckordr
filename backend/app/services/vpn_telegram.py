from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Awaitable, Callable

import httpx
from sqlalchemy import select
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
from app.services.vpn_customer_view import (
    UnavailableCustomerConnection,
    customer_connection,
    list_customer_profiles,
    list_customer_subscriptions,
)
from app.services.vpn_portal_auth import identity_allowed, public_origin
from app.services.vpn_portal_http import portal_capabilities
from app.services.vpn_telegram_identity import (
    TelegramIdentity,
    identity_from_user,
    resolve_telegram_customer,
    telegram_user_id,
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
    chat_type: str | None
    user_id: str
    username: str | None
    first_name: str | None
    last_name: str | None
    text: str

    @property
    def identity(self) -> TelegramIdentity:
        return TelegramIdentity(
            user_id=self.user_id,
            username=self.username,
            first_name=self.first_name,
            last_name=self.last_name,
        )


def parse_telegram_message(payload: dict) -> TelegramMessage:
    message = payload.get("message")
    if not isinstance(message, dict):
        raise ValueError("Telegram update does not contain a message identity")
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        raise ValueError("Telegram update does not contain a message identity")
    if payload.get("update_id") is None or sender.get("id") is None or chat.get("id") is None:
        raise ValueError("Telegram update does not contain a message identity")
    identity = identity_from_user(sender)
    chat_id = str(chat["id"])
    chat_type = chat.get("type") if isinstance(chat.get("type"), str) else None
    if chat_type == "private" and telegram_user_id(chat.get("id")) != identity.user_id:
        raise ValueError("invalid_identity")
    return TelegramMessage(
        update_id=str(payload["update_id"]),
        chat_id=chat_id,
        chat_type=chat_type,
        user_id=identity.user_id,
        username=identity.username,
        first_name=identity.first_name,
        last_name=identity.last_name,
        text=str(message.get("text") or "").strip(),
    )


def telegram_keyboard(settings: Settings, chat_id: str) -> dict:
    """Return a command keyboard with a safely gated Mini App entry point."""
    keyboard: list[list[dict[str, object]]] = [
        [{"text": "Моя подписка"}, {"text": "Мои профили"}],
        [{"text": "Помощь"}],
    ]
    try:
        private_chat_id = int(chat_id)
    except (TypeError, ValueError):
        private_chat_id = 0
    if (
        private_chat_id > 0
        and portal_capabilities(settings)["mini_app_enabled"]
        and identity_allowed(settings, chat_id)
    ):
        keyboard.append(
            [
                {
                    "text": "Личный кабинет",
                    "web_app": {"url": public_origin(settings) + "/cabinet/"},
                }
            ]
        )
    return {"keyboard": keyboard, "resize_keyboard": True}


async def send_telegram_message(settings: Settings, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.vpn_telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        for chunk in split_telegram_text(text):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "reply_markup": telegram_keyboard(settings, chat_id),
            }
            response = await client.post(url, json=payload)
            response.raise_for_status()


def split_telegram_text(text: str, *, limit: int = 4000) -> list[str]:
    if limit < 1:
        raise ValueError("Telegram message limit must be positive")
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            split_at = limit
        else:
            split_at += 1
        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


COMMANDS = {
    "/start": "start",
    "/status": "status",
    "статус": "status",
    "/keys": "keys",
    "ключи": "keys",
    "/support": "support",
    "поддержка": "support",
    "моя подписка": "status",
    "мои профили": "keys",
    "помощь": "support",
}


_SUBSCRIPTION_LABELS = {
    "active": "активна",
    "trial": "пробная",
    "scheduled": "ещё не началась",
    "expired": "истекла",
    "disabled": "приостановлена",
    "cancelled": "отменена",
    "unavailable": "недоступна",
}
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


def _friendly_date(value: datetime | None) -> str:
    if value is None:
        return "без ограничения по сроку"
    current = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return f"{current.day} {_MONTHS[current.month - 1]} {current.year}, {current:%H:%M} UTC"


async def render_customer_response(
    session: AsyncSession,
    customer: VpnCustomer,
    command: str,
    settings: Settings,
) -> str:
    if command == "support":
        return settings.vpn_support_text
    if customer.status != "active":
        return "VPN-профиль отключён. Выберите «Помощь» для связи с администратором."

    subscriptions = await list_customer_subscriptions(session, customer.id)

    if command in {"start", "status"}:
        if not subscriptions:
            return (
                "VeltrixVPN\nАктивной VPN-подписки нет. "
                "Выберите «Помощь» для связи с администратором."
            )
        lines = ["VeltrixVPN"]
        for subscription in subscriptions:
            lines.extend(
                [
                    f"Статус: {_SUBSCRIPTION_LABELS.get(subscription.state, 'недоступна')}",
                    f"Действует до: {_friendly_date(subscription.expires_at)}",
                ]
            )
        return "\n".join(lines)

    if command == "keys":
        profiles = await list_customer_profiles(session, customer)
        connections: list[str] = []
        for profile in profiles:
            if not profile.can_connect:
                continue
            try:
                connection = await customer_connection(session, customer, profile.id)
            except UnavailableCustomerConnection:
                continue
            connections.append(f"{profile.display_name}\n{connection.uri}")
        if not connections:
            return "Сейчас нет доступных профилей VeltrixVPN."
        return "Ваши профили VeltrixVPN:\n\n" + "\n\n".join(connections)

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
    # Claim the update durably before any outbound side effect. If the process
    # dies after Telegram accepts a message, a retry sees this row and does not
    # deliver the same credential twice.
    await session.commit()

    try:
        message = parse_telegram_message(payload)
    except ValueError as exc:
        update.error_message = sanitize_telegram_error(exc, settings)
        return {"processed": False, "duplicate": False}

    active_sender = sender or send_telegram_message
    if message.chat_type != "private":
        try:
            await active_sender(
                settings,
                message.chat_id,
                "VPN-ключи и данные подписки доступны только в личном чате с ботом.",
            )
        except Exception as exc:
            update.error_message = sanitize_telegram_error(exc, settings)
            return {"processed": False, "duplicate": False}
        update.processed_at = utcnow()
        update.error_message = None
        return {"processed": True, "duplicate": False}

    customer = await resolve_telegram_customer(session, message.identity)
    update.customer_id = customer.id

    command = COMMANDS.get(message.text.casefold(), "start")
    response_text = await render_customer_response(session, customer, command, settings)
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
