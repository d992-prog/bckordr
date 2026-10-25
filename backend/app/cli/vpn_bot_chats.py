"""Restore only operator-attributed private webhook history; never sends messages."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import AppSetting
from app.services.vpn_bot_chats import (
    REGISTRY_BOT_KEY,
    _positive_id,
    backfill_private_bot_chats,
    configured_vpn_bot_id,
)


def _identity_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(follow_redirects=False, timeout=10)


async def get_telegram_bot_id(settings: Settings) -> str:
    try:
        async with _identity_client() as client:
            response = await client.get(
                f"https://api.telegram.org/bot{settings.vpn_telegram_bot_token}/getMe"
            )
        if response.status_code != 200:
            raise ValueError
        data = response.json()
        result = (
            data.get("result")
            if isinstance(data, dict) and data.get("ok") is True
            else None
        )
        if (
            not isinstance(result, dict)
            or result.get("is_bot") is not True
            or type(result.get("id")) is not int
            or _positive_id(str(result["id"])) is None
        ):
            raise ValueError
        return str(result["id"])
    except (httpx.HTTPError, ValueError):
        raise ValueError("vpn_bot_registry_identity_unavailable") from None


async def _lock_marker(db: AsyncSession, bot_id: str) -> AppSetting:
    query = (
        select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY).with_for_update()
    )
    marker = await db.scalar(query.execution_options(populate_existing=True))
    if marker is None:
        try:
            async with db.begin_nested():
                marker = AppSetting(key=REGISTRY_BOT_KEY, value=f"backfilling:{bot_id}")
                db.add(marker)
                await db.flush()
        except IntegrityError:
            marker = await db.scalar(query.execution_options(populate_existing=True))
            if marker is None:
                raise
    if marker.value not in (bot_id, f"backfilling:{bot_id}"):
        raise ValueError("vpn_bot_registry_bot_changed")
    return marker


async def restore_registry(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    confirmed_bot_id: str,
    *,
    identity_reader: Callable[[Settings], Awaitable[str]] | None = None,
) -> int:
    bot_id = configured_vpn_bot_id(settings)
    if _positive_id(confirmed_bot_id) is None or confirmed_bot_id != bot_id:
        raise ValueError("vpn_bot_registry_identity_mismatch")
    if await (identity_reader or get_telegram_bot_id)(settings) != bot_id:
        raise ValueError("vpn_bot_registry_identity_mismatch")
    accepted, after_id = 0, 0
    while True:
        async with session_factory() as db:
            marker = await _lock_marker(db, bot_id)
            count, last_id = await backfill_private_bot_chats(
                db, bot_id, after_id=after_id
            )
            accepted += count
            if last_id is None:
                marker.value = bot_id
            await db.commit()
        if last_id is None:
            return accepted
        after_id = last_id


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Use only after confirming that all historical VPN webhook updates belong to this bot. This command does not send messages.",
    )
    result.add_argument("--confirmed-deployment-bot-id", required=True)
    return result


async def _run(confirmed_bot_id: str) -> int:
    from app.db.session import create_vpn_control_database
    from app.services.vpn_portal_logging import install_safe_logging

    install_safe_logging()
    settings = get_settings()
    database = create_vpn_control_database(settings)
    try:
        await restore_registry(database.session_factory, settings, confirmed_bot_id)
    except ValueError as exc:
        # All validation/network failures above use fixed, secret-free codes.
        print(str(exc))
        return 1
    finally:
        await database.engine.dispose()
    print("VPN bot recipient registry restored. No messages sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(_run(parser().parse_args().confirmed_deployment_bot_id))
    )
