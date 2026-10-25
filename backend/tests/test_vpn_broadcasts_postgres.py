import asyncio

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_bot_chats import settings
from test_vpn_broadcasts import immediate_gate as immediate_gate  # noqa: PLC0414
from test_vpn_broadcasts import seed
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_friend_invitations import NOW

from app.db.base import Base
from app.db.models import VpnBotChat, VpnBroadcastCampaign, VpnBroadcastDelivery
from app.db.vpn_broadcast_migrations import (
    VPN_BOT_CHAT_MIGRATIONS,
    VPN_BROADCAST_MIGRATIONS,
)
from app.services.vpn_bot_delivery import TelegramDeliveryResult
from app.services.vpn_broadcasts import (
    cancel_broadcast,
    confirm_broadcast,
    create_broadcast_draft,
    dispatch_next_broadcast_delivery,
    preview_broadcast,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_broadcast_migrations_repeat_and_match_metadata(
    postgres_schema, metadata_first
):
    async with postgres_schema.engine.begin() as connection:
        if metadata_first:
            await connection.run_sync(Base.metadata.create_all)
        else:
            await connection.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY)"))
        for _ in range(2):
            for statement in VPN_BOT_CHAT_MIGRATIONS + VPN_BROADCAST_MIGRATIONS:
                await connection.execute(text(statement))
        for model in (VpnBroadcastCampaign, VpnBroadcastDelivery):
            columns = (
                (
                    await connection.execute(
                        text(
                            "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name=:name"
                        ),
                        {"name": model.__tablename__},
                    )
                )
                .scalars()
                .all()
            )
            assert set(columns) == set(model.__table__.columns.keys())


async def environment(postgres_schema, *, confirmed=False):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(
            db, settings(), "Не отправлять: синтетический тест", actor_id=None, now=NOW
        )
        await preview_broadcast(db, settings(), draft.id, now=NOW)
        if confirmed:
            await confirm(db, draft.id)
        await db.commit()
        return factory, draft.id


async def confirm(db, campaign_id):
    return await confirm_broadcast(
        db,
        settings(),
        campaign_id,
        expected_revision=1,
        expected_recipient_count=1,
        actor_id=None,
        now=NOW,
    )


@pytest.mark.asyncio
async def test_two_simultaneous_confirmations_create_one_recipient_snapshot(
    postgres_schema,
):
    factory, campaign_id = await environment(postgres_schema)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await holder.scalar(
            select(VpnBroadcastCampaign)
            .where(VpnBroadcastCampaign.id == campaign_id)
            .with_for_update()
        )

        async def waiting_confirmation():
            await confirm(waiter, campaign_id)
            await waiter.commit()

        task = asyncio.create_task(waiting_confirmation())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await confirm(holder, campaign_id)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "running"
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_confirm_cancel_race_cannot_recreate_cancelled_queue(
    postgres_schema, cancel_first
):
    factory, campaign_id = await environment(postgres_schema)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await holder.scalar(
            select(VpnBroadcastCampaign)
            .where(VpnBroadcastCampaign.id == campaign_id)
            .with_for_update()
        )

        async def waiting_action():
            if cancel_first:
                await confirm(waiter, campaign_id)
            else:
                await cancel_broadcast(waiter, campaign_id, actor_id=None, now=NOW)
            await waiter.commit()

        task = asyncio.create_task(waiting_action())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            if cancel_first:
                await cancel_broadcast(holder, campaign_id, actor_id=None, now=NOW)
            else:
                await confirm(holder, campaign_id)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "cancelled"
        states = (await db.scalars(select(VpnBroadcastDelivery.state))).all()
        assert states == ([] if cancel_first else ["cancelled"])


@pytest.mark.asyncio
async def test_parallel_dispatchers_send_one_recipient_once(
    postgres_schema, immediate_gate
):
    factory, campaign_id = await environment(postgres_schema, confirmed=True)
    calls = []

    async def sender(*_args):
        calls.append(True)
        async with factory() as db:
            job = await db.scalar(
                select(VpnBroadcastDelivery).with_for_update(nowait=True)
            )
            assert job.state == "sending"
        return TelegramDeliveryResult("sent", message_id=71)

    await asyncio.gather(
        *[
            dispatch_next_broadcast_delivery(
                factory, settings(), sender=sender, now=lambda: NOW
            )
            for _ in range(2)
        ]
    )
    assert calls == [True]
    async with factory() as db:
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "completed"
        job = await db.scalar(select(VpnBroadcastDelivery))
        assert job.state == "sent" and job.attempt_count == 1


@pytest.mark.asyncio
async def test_cancel_commits_during_http_and_preserves_its_outcome(
    postgres_schema, immediate_gate
):
    factory, campaign_id = await environment(postgres_schema, confirmed=True)
    started, release = asyncio.Event(), asyncio.Event()

    async def sender(*_args):
        started.set()
        await release.wait()
        return TelegramDeliveryResult("sent", message_id=71)

    task = asyncio.create_task(
        dispatch_next_broadcast_delivery(
            factory, settings(), sender=sender, now=lambda: NOW
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 10)
        async with factory() as db:
            await asyncio.wait_for(
                cancel_broadcast(db, campaign_id, actor_id=None, now=NOW), 5
            )
            await db.commit()
        release.set()
        await asyncio.wait_for(task, 10)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "cancelled"
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "sent"


@pytest.mark.asyncio
async def test_recipient_lock_wait_cannot_turn_expired_claim_into_sending(
    postgres_schema, monkeypatch
):
    from datetime import timedelta

    from app.services import vpn_broadcasts

    factory, _ = await environment(postgres_schema, confirmed=True)
    clock = [NOW]
    _, claim = await vpn_broadcasts._claim_delivery(
        factory, settings(), lambda: clock[0]
    )
    assert claim is not None
    original_pair = vpn_broadcasts._locked_pair
    waiter_pid = asyncio.get_running_loop().create_future()

    async def tracked_pair(db, *args, **kwargs):
        result = await original_pair(db, *args, **kwargs)
        if not waiter_pid.done():
            waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
        return result

    monkeypatch.setattr(vpn_broadcasts, "_locked_pair", tracked_pair)
    async with factory() as holder:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        await holder.get(VpnBotChat, ("111", "12345"), with_for_update=True)
        task = asyncio.create_task(
            vpn_broadcasts._mark_sending(factory, settings(), claim, lambda: clock[0])
        )
        try:
            pid = await asyncio.wait_for(waiter_pid, 5)
            await wait_for_blocker(postgres_schema.engine, pid, holder_pid, task)
            clock[0] = NOW + timedelta(seconds=31)
            await holder.commit()
            assert await asyncio.wait_for(task, 10) is False
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "claimed"


@pytest.mark.asyncio
async def test_sending_lease_recovers_uncertain_not_pending(
    postgres_schema, immediate_gate
):
    from datetime import timedelta

    factory, _ = await environment(postgres_schema, confirmed=True)
    calls = []

    async def sender(*_args):
        calls.append(True)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_broadcast_delivery(
            factory, settings(), sender=sender, now=lambda: NOW
        )
    await dispatch_next_broadcast_delivery(
        factory, settings(), sender=sender, now=lambda: NOW + timedelta(seconds=31)
    )
    assert calls == [True]
    async with factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "uncertain"
