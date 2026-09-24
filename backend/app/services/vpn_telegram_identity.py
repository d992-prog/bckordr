from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VpnCustomer


MAX_TELEGRAM_USER_ID = 2**52
MAX_RAW_TELEGRAM_USER_ID_LENGTH = 64


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def telegram_user_id(value: object) -> str:
    if isinstance(value, bool):
        raise ValueError("invalid_identity")
    if isinstance(value, int):
        if not 0 < value < MAX_TELEGRAM_USER_ID:
            raise ValueError("invalid_identity")
        return str(value)
    if not isinstance(value, str):
        raise ValueError("invalid_identity")

    raw = value
    if (
        not raw
        or len(raw) > MAX_RAW_TELEGRAM_USER_ID_LENGTH
        or not raw.isascii()
        or not raw.isdecimal()
    ):
        raise ValueError("invalid_identity")

    normalized = raw.lstrip("0")
    if not normalized:
        raise ValueError("invalid_identity")
    upper_bound = str(MAX_TELEGRAM_USER_ID)
    if len(normalized) > len(upper_bound) or (
        len(normalized) == len(upper_bound) and normalized >= upper_bound
    ):
        raise ValueError("invalid_identity")
    return normalized


def optional_text(value: object, length: int) -> str | None:
    return value[:length] if isinstance(value, str) and value else None


def identity_from_user(user: dict) -> TelegramIdentity:
    return TelegramIdentity(
        user_id=telegram_user_id(user.get("id")),
        username=optional_text(user.get("username"), 128),
        first_name=optional_text(user.get("first_name"), 128),
        last_name=optional_text(user.get("last_name"), 128),
    )


async def resolve_telegram_customer(
    db: AsyncSession,
    identity: TelegramIdentity,
) -> VpnCustomer:
    canonical_user_id = telegram_user_id(identity.user_id)
    statement = (
        select(VpnCustomer)
        .where(VpnCustomer.telegram_user_id == canonical_user_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    customer = await db.scalar(statement)
    if customer is None:
        try:
            async with db.begin_nested():
                customer = VpnCustomer(
                    telegram_user_id=canonical_user_id,
                    status="active",
                )
                db.add(customer)
                await db.flush()
        except IntegrityError:
            customer = await db.scalar(statement)
            if customer is None:
                raise

    customer.telegram_username = identity.username
    customer.first_name = identity.first_name
    customer.last_name = identity.last_name
    await db.flush()
    return customer
