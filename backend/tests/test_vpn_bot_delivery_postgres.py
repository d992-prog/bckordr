import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_friend_invitations import NOW

from app.db.base import Base
from app.db.models import VpnBotRateGate
from app.services.vpn_bot_delivery import (
    _global_gate,
    extend_bot_cooldown,
    reserve_bot_send_slot,
    token_fingerprint,
)


async def environment(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(postgres_schema.engine, expire_on_commit=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_chat", [False, True])
async def test_simultaneous_gate_reservations_share_global_and_chat_limits(
    postgres_schema, same_chat
):
    factory = await environment(postgres_schema)
    values = await asyncio.gather(
        reserve_bot_send_slot(factory, "111:synthetic", "12345", now=NOW),
        reserve_bot_send_slot(
            factory, "111:synthetic", "12345" if same_chat else "67890", now=NOW
        ),
    )
    assert sorted(values) == [0, 1 if same_chat else 0.05]
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnBotRateGate)) == 1


@pytest.mark.asyncio
async def test_other_token_does_not_wait_for_locked_vpn_gate(postgres_schema):
    factory = await environment(postgres_schema)
    async with factory() as holder:
        await _global_gate(holder, token_fingerprint("111:synthetic"))
        assert (
            await asyncio.wait_for(
                reserve_bot_send_slot(factory, "222:operations", "12345", now=NOW), 5
            )
            == 0
        )
        await holder.rollback()


@pytest.mark.asyncio
async def test_cooldown_is_rechecked_after_waiting_for_global_gate_lock(
    postgres_schema,
):
    factory = await environment(postgres_schema)
    async with factory() as holder:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        row = await _global_gate(holder, token_fingerprint("111:synthetic"))
        row.cooldown_until = NOW + timedelta(seconds=30)
        await holder.flush()
        async with factory() as waiter:
            waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))

            async def wait_and_reserve():
                await _global_gate(waiter, token_fingerprint("111:synthetic"))
                await waiter.rollback()
                return await reserve_bot_send_slot(
                    factory, "111:synthetic", "12345", now=NOW
                )

            task = asyncio.create_task(wait_and_reserve())
            try:
                await wait_for_blocker(
                    postgres_schema.engine, waiter_pid, holder_pid, task
                )
                await holder.commit()
                assert await asyncio.wait_for(task, 15) == 30
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    await extend_bot_cooldown(factory, "111:synthetic", 5, now=NOW)
    assert await reserve_bot_send_slot(factory, "111:synthetic", "67890", now=NOW) == 30


@pytest.mark.asyncio
async def test_database_clock_reservation_and_cooldown_need_no_process_clock(
    postgres_schema,
):
    factory = await environment(postgres_schema)
    assert await reserve_bot_send_slot(factory, "111:synthetic", "12345") == 0
    await extend_bot_cooldown(factory, "111:synthetic", 37)
    delay = await reserve_bot_send_slot(factory, "111:synthetic", "67890")
    assert 30 < delay <= 37
