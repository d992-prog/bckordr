"""Stable customer-visible names for VPN access profiles."""

from __future__ import annotations

import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription


_LEGACY_TEST_NAME = re.compile(r"test\d*", re.IGNORECASE)
_REMOVED_CATEGORIES = {"Cc", "Cf", "Cs"}


def initial_display_name(public_name: str | None, ordinal: int) -> str:
    """Derive a safe initial label without changing the legacy public name."""
    if public_name is None:
        return f"Профиль {ordinal}"

    cleaned = "".join(
        character
        for character in public_name.strip()
        if unicodedata.category(character) not in _REMOVED_CATEGORIES
    )
    cleaned = cleaned[:64].strip()
    if (
        not cleaned
        or _LEGACY_TEST_NAME.fullmatch(cleaned)
        or cleaned.casefold().startswith("dropcatch-")
    ):
        return f"Профиль {ordinal}"
    return cleaned


async def initialize_customer_names(session: AsyncSession, customer_id: int) -> int:
    """Fill missing profile names under the customer's serialization lock."""
    customer = await session.scalar(
        select(VpnCustomer)
        .where(VpnCustomer.id == customer_id)
        .with_for_update()
    )
    if customer is None:
        raise LookupError("VPN customer not found")

    access_keys = list(
        (
            await session.scalars(
                select(VpnAccessKey)
                .join(
                    VpnSubscription,
                    VpnSubscription.id == VpnAccessKey.subscription_id,
                )
                .where(VpnSubscription.customer_id == customer_id)
                .order_by(VpnAccessKey.id.asc())
            )
        ).all()
    )
    for ordinal, access_key in enumerate(access_keys, start=1):
        if access_key.display_name is None:
            access_key.display_name = initial_display_name(
                access_key.public_name, ordinal
            )
    await session.flush()
    return len(access_keys) + 1


async def backfill_profile_names(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Backfill profile names in restart-safe customer batches."""
    last_customer_id = 0
    while True:
        async with session_factory() as session:
            customer_ids = list(
                (
                    await session.scalars(
                        select(VpnCustomer.id)
                        .where(VpnCustomer.id > last_customer_id)
                        .order_by(VpnCustomer.id.asc())
                        .limit(100)
                    )
                ).all()
            )
            if not customer_ids:
                return
            for customer_id in customer_ids:
                await initialize_customer_names(session, customer_id)
            await session.commit()
        last_customer_id = customer_ids[-1]
