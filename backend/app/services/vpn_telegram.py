from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
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
    VpnFriendInvitation,
    VpnNodeEvent,
    VpnSubscription,
    VpnTelegramUpdate,
)
from app.services.vpn_customer_view import (
    MissingCustomerProfile,
    UnavailableCustomerConnection,
    customer_connection,
    list_customer_profiles,
    list_customer_subscriptions,
)
from app.services.vpn_friend_invitations import (
    FriendInvitationConflict,
    FriendInvitationUnavailable,
    redeem_friend_invitation,
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

INVITE_START = re.compile(r"/start i_([A-Za-z0-9_-]{43})", re.ASCII)
INVITATION_REJECTED_TEXT = (
    "Приглашение недействительно. Попросите владельца создать новую ссылку."
)
INVITATION_UNAVAILABLE_TEXT = (
    "Сервис временно недоступен. Попробуйте открыть приглашение позже."
)
INVITATION_PREPARING_TEXT = (
    "Veltrix VPN\nДоступ готовится. Это может занять несколько минут."
)
_REDACTED_START_PAYLOAD = "<redacted-start-payload>"
_REDACTED_MESSAGE = "<redacted-message>"
_MAX_SIGNED_BIGINT = 2**63 - 1
_MAX_MESSAGE_ID = 2**31 - 1
_MAX_TELEGRAM_ID = 2**52 - 1
_CHAT_TYPES = frozenset({"private", "group", "supergroup", "channel"})
_PERSISTED_COMMANDS = frozenset(
    {
        "/start",
        "/status",
        "/keys",
        "/support",
        "статус",
        "ключи",
        "поддержка",
        "моя подписка",
        "мои профили",
        "помощь",
    }
)


def _bounded_int(value: object, minimum: int, maximum: int) -> int | None:
    return value if type(value) is int and minimum <= value <= maximum else None


def _telegram_chat_id(value: object) -> int | None:
    candidate = _bounded_int(value, -_MAX_TELEGRAM_ID, _MAX_TELEGRAM_ID)
    return candidate if candidate != 0 else None


def friend_invite_token(text: str) -> str | None:
    match = INVITE_START.fullmatch(text)
    return match.group(1) if match else None


def _has_start_payload(text: str) -> bool:
    return text.startswith("/start") and text != "/start"


def sanitized_telegram_text(text: str) -> str:
    if _has_start_payload(text):
        return _REDACTED_START_PAYLOAD
    if text in _PERSISTED_COMMANDS:
        return text
    return _REDACTED_MESSAGE


def sanitized_telegram_payload(payload: dict, text: str) -> dict:
    update_id = _bounded_int(payload.get("update_id"), 0, _MAX_SIGNED_BIGINT)
    message = payload.get("message")
    if not isinstance(message, dict):
        return {"update_id": update_id}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    chat_type = chat.get("type")
    return {
        "update_id": update_id,
        "message": {
            "message_id": _bounded_int(message.get("message_id"), 1, _MAX_MESSAGE_ID),
            "date": _bounded_int(message.get("date"), 0, _MAX_SIGNED_BIGINT),
            "from": {
                "id": _bounded_int(sender.get("id"), 1, _MAX_TELEGRAM_ID),
            },
            "chat": {
                "id": _telegram_chat_id(chat.get("id")),
                "type": (
                    chat_type
                    if type(chat_type) is str and chat_type in _CHAT_TYPES
                    else None
                ),
            },
            "text": sanitized_telegram_text(text),
        },
    }


def _with_invitation_context(
    payload: dict,
    outcome: str,
    access_key_id: int | None = None,
) -> dict:
    context: dict[str, object] = {"outcome": outcome}
    if outcome == "redeemed":
        context["access_key_id"] = access_key_id
    return {**payload, "friend_invitation": context}


def _invitation_context(update: VpnTelegramUpdate) -> tuple[str, int | None] | None:
    payload = update.payload
    if not isinstance(payload, dict):
        return None
    context = payload.get("friend_invitation")
    if not isinstance(context, dict):
        return None
    outcome = context.get("outcome")
    if outcome in {"rejected", "unavailable"}:
        return (outcome, None) if set(context) == {"outcome"} else None
    access_key_id = context.get("access_key_id")
    if (
        outcome != "redeemed"
        or set(context) != {"outcome", "access_key_id"}
        or type(access_key_id) is not int
        or not 1 <= access_key_id <= 2**63 - 1
    ):
        return None
    return outcome, access_key_id


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
        text=str(message.get("text") or ""),
    )


def telegram_keyboard(settings: Settings, chat_id: str) -> dict:
    """Keep commands available and replace any previously sent WebApp keyboard."""
    return {
        "keyboard": [
            [{"text": "Моя подписка"}, {"text": "Мои профили"}],
            [{"text": "Помощь"}],
        ],
        "resize_keyboard": True,
    }


def telegram_cabinet_keyboard(settings: Settings, chat_id: str) -> dict | None:
    """Use an inline launch: reply-keyboard WebApps receive no signed initData."""
    try:
        private_chat_id = int(chat_id)
    except (TypeError, ValueError):
        private_chat_id = 0
    if (
        private_chat_id > 0
        and portal_capabilities(settings)["mini_app_enabled"]
        and identity_allowed(settings, chat_id)
    ):
        return {
            "inline_keyboard": [
                [{
                    "text": "Личный кабинет",
                    "web_app": {"url": public_origin(settings) + "/cabinet/"},
                }],
            ]
        }
    return None


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
        cabinet_keyboard = telegram_cabinet_keyboard(settings, chat_id)
        if cabinet_keyboard is not None:
            # Telegram permits only one reply_markup type per message. Keep the
            # command keyboard above and add exactly one authenticated launch.
            response = await client.post(url, json={
                "chat_id": chat_id,
                "text": "Подписка, VPN-профили и инструкции по подключению — в личном кабинете.",
                "reply_markup": cabinet_keyboard,
            })
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


def _raw_message_text(payload: dict) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    text = message.get("text")
    return text if isinstance(text, str) else ""


def _stored_invite_update(update: VpnTelegramUpdate) -> bool:
    payload = update.payload
    if not isinstance(payload, dict):
        return False
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    text = message.get("text")
    return text == _REDACTED_START_PAYLOAD or (
        isinstance(text, str) and _has_start_payload(text)
    )


async def _invitation_response(
    session: AsyncSession,
    update: VpnTelegramUpdate,
    message: TelegramMessage,
) -> str:
    context = _invitation_context(update)
    if context is None:
        return INVITATION_REJECTED_TEXT
    outcome, access_key_id = context
    if update.customer_id is None:
        if outcome == "unavailable":
            return INVITATION_UNAVAILABLE_TEXT
        return INVITATION_REJECTED_TEXT
    if outcome != "redeemed" or access_key_id is None:
        return INVITATION_REJECTED_TEXT
    row = (
        await session.execute(
            select(VpnCustomer, VpnSubscription, VpnAccessKey, VpnFriendInvitation)
            .select_from(VpnFriendInvitation)
            .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(
                VpnCustomer.id == update.customer_id,
                VpnAccessKey.id == access_key_id,
                VpnCustomer.telegram_user_id == message.user_id,
                VpnFriendInvitation.telegram_user_id == message.user_id,
                VpnFriendInvitation.revoked_at.is_(None),
            )
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        return INVITATION_REJECTED_TEXT
    customer, _subscription, access_key, _invitation = row
    try:
        connection = await customer_connection(session, customer, access_key.id)
    except (MissingCustomerProfile, UnavailableCustomerConnection):
        return INVITATION_PREPARING_TEXT
    return f"Ваш профиль VeltrixVPN:\n\n{connection.uri}"


async def _record_delivery_failure(
    session: AsyncSession,
    update: VpnTelegramUpdate,
) -> None:
    update.error_message = "telegram_delivery_failed"
    if update.customer_id is None:
        return
    payload = update.payload if isinstance(update.payload, dict) else {}
    if "friend_invitation" in payload:
        context = _invitation_context(update)
        if context is None or context[0] != "redeemed" or context[1] is None:
            return
        key = await session.scalar(
            select(VpnAccessKey)
            .select_from(VpnFriendInvitation)
            .join(VpnAccessKey, VpnAccessKey.id == VpnFriendInvitation.access_key_id)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .where(
                VpnAccessKey.id == context[1],
                VpnCustomer.id == update.customer_id,
                VpnFriendInvitation.telegram_user_id == VpnCustomer.telegram_user_id,
                VpnAccessKey.worker_id.is_not(None),
            )
        )
    else:
        key = await session.scalar(
            select(VpnAccessKey)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .where(
                VpnSubscription.customer_id == update.customer_id,
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
                details={"update_id": update.update_id, "error": update.error_message},
            )
        )


async def process_telegram_update(
    session: AsyncSession,
    payload: dict,
    settings: Settings,
    sender: TelegramSender | None = None,
) -> dict[str, bool]:
    raw_update_id = payload.get("update_id")
    canonical_update_id = _bounded_int(raw_update_id, 0, _MAX_SIGNED_BIGINT)
    if canonical_update_id is None:
        return {"processed": False, "duplicate": False}
    update_id = str(canonical_update_id)
    active_sender = sender or send_telegram_message
    existing = await session.scalar(
        select(VpnTelegramUpdate)
        .where(VpnTelegramUpdate.update_id == update_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if existing is not None:
        if existing.processed_at is not None:
            return {"processed": True, "duplicate": True}
        if _stored_invite_update(existing):
            try:
                message = parse_telegram_message(existing.payload or {})
            except ValueError:
                return {"processed": False, "duplicate": True}
            response_text = await _invitation_response(session, existing, message)
            try:
                await active_sender(settings, message.chat_id, response_text)
            except Exception:
                await _record_delivery_failure(session, existing)
                return {"processed": False, "duplicate": True}
            existing.processed_at = utcnow()
            existing.error_message = None
            return {"processed": True, "duplicate": True}
        return {"processed": existing.processed_at is not None, "duplicate": True}

    text = _raw_message_text(payload)
    sanitized_payload = sanitized_telegram_payload(payload, text)
    message: TelegramMessage | None
    parse_error: str | None = None
    try:
        message = parse_telegram_message(payload)
    except ValueError as exc:
        message = None
        parse_error = sanitize_telegram_error(exc, settings)
    update = VpnTelegramUpdate(
        update_id=update_id,
        payload=sanitized_payload,
        error_message=parse_error,
    )
    session.add(update)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"processed": False, "duplicate": True}
    if message is None:
        await session.commit()
        return {"processed": False, "duplicate": False}

    if message.chat_type == "private" and _has_start_payload(message.text):
        token = friend_invite_token(message.text)
        if token is None:
            update.error_message = "friend_invitation_rejected"
            update.payload = _with_invitation_context(update.payload, "rejected")
        else:
            try:
                redeemed = await redeem_friend_invitation(
                    session,
                    settings,
                    token,
                    message.identity,
                    utcnow(),
                )
            except FriendInvitationConflict:
                update.error_message = "friend_invitation_rejected"
                update.payload = _with_invitation_context(update.payload, "rejected")
            except FriendInvitationUnavailable:
                update.error_message = "friend_beta_unavailable"
                update.payload = _with_invitation_context(update.payload, "unavailable")
            else:
                update.customer_id = redeemed.customer_id
                update.error_message = None
                update.payload = _with_invitation_context(
                    update.payload,
                    "redeemed",
                    redeemed.access_key_id,
                )

        # The update claim and any successful activation become durable together,
        # before Telegram can observe the response.
        await session.commit()
        response_text = await _invitation_response(session, update, message)
        try:
            await active_sender(settings, message.chat_id, response_text)
        except Exception:
            await _record_delivery_failure(session, update)
            return {"processed": False, "duplicate": False}
        update.processed_at = utcnow()
        update.error_message = None
        return {"processed": True, "duplicate": False}

    # Generic updates keep their existing durable reservation before delivery.
    await session.commit()
    if message.chat_type != "private":
        try:
            await active_sender(
                settings,
                message.chat_id,
                "VPN-ключи и данные подписки доступны только в личном чате с ботом.",
            )
        except Exception:
            await _record_delivery_failure(session, update)
            return {"processed": False, "duplicate": False}
        update.processed_at = utcnow()
        update.error_message = None
        return {"processed": True, "duplicate": False}

    customer = await resolve_telegram_customer(session, message.identity)
    update.customer_id = customer.id

    command = COMMANDS.get(message.text.strip().casefold(), "start")
    response_text = await render_customer_response(session, customer, command, settings)
    try:
        await active_sender(settings, message.chat_id, response_text)
    except Exception:  # Telegram retries are prevented by the HTTP 200 acknowledgement.
        await _record_delivery_failure(session, update)
        return {"processed": False, "duplicate": False}

    update.processed_at = utcnow()
    update.error_message = None
    return {"processed": True, "duplicate": False}
