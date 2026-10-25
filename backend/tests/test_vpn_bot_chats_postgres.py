"""Registry migration and real races confined to the allowlisted synthetic DB."""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_bot_chats import settings
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_friend_invitations import NOW

from app.cli.vpn_bot_chats import _lock_marker
from app.db.base import Base
from app.db.models import AppSetting, VpnBotChat
from app.db.vpn_broadcast_migrations import VPN_BOT_CHAT_MIGRATIONS
from app.services.vpn_bot_chats import _record_chat, registry_state


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_registry_migration_is_repeatable_before_or_after_metadata(
    postgres_schema, metadata_first
):
    async with postgres_schema.engine.begin() as connection:
        if metadata_first:
            await connection.run_sync(Base.metadata.create_all)
        for _ in range(2):
            for statement in VPN_BOT_CHAT_MIGRATIONS:
                await connection.execute(text(statement))
        rows = (
            await connection.execute(
                text(
                    "SELECT column_name,data_type FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='vpn_bot_chats'"
                )
            )
        ).all()
        assert dict(rows)["last_update_id"] == "bigint"
        assert len(rows) == 6
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with factory() as db:
        await _record_chat(db, "111", "12345", 2**40, NOW, unblock=True)
        await db.commit()
        assert (await db.get(VpnBotChat, ("111", "12345"))).last_update_id == 2**40


@pytest.mark.asyncio
async def test_two_first_private_updates_converge_without_losing_dates(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await _record_chat(holder, "111", "12345", 10, NOW, unblock=True)

        async def second_update():
            await _record_chat(
                waiter, "111", "12345", 9, NOW + timedelta(seconds=1), unblock=True
            )
            await waiter.commit()

        task = asyncio.create_task(second_update())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        chat = await db.get(VpnBotChat, ("111", "12345"))
        assert chat.first_seen_at == NOW and chat.last_seen_at == NOW + timedelta(
            seconds=1
        )
        assert chat.last_update_id == 9
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 1


@pytest.mark.asyncio
async def test_concurrent_cli_cannot_assign_history_to_two_bots(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await _lock_marker(holder, "111")

        async def competing_bot():
            with pytest.raises(ValueError, match="^vpn_bot_registry_bot_changed$"):
                await _lock_marker(waiter, "222")
            await waiter.rollback()

        task = asyncio.create_task(competing_bot())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert await registry_state(db, settings()) == "history_unverified"
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 1
