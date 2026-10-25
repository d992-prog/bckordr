"""Uses the same isolated, validated local PostgreSQL schema as other VPN tests."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_expiry_reminders import (
    immediate_gate as immediate_gate,  # noqa: PLC0414
)
from test_vpn_expiry_reminders import (
    seed,
    service,
    settings,
)
from test_vpn_friend_invitations import NOW

from app.db import models
from app.db.base import Base, utcnow
from app.services.vpn_bot_delivery import TelegramDeliveryResult


async def environment(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    ids = await seed(factory)
    return factory, ids


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_reminder_migration_repeats_preserves_existing_customer_and_subscription(
    postgres_schema, metadata_first
):
    from app.db.migrations import MIGRATIONS
    from app.db.vpn_reminder_migrations import VPN_REMINDER_MIGRATIONS

    assert all(statement in MIGRATIONS for statement in VPN_REMINDER_MIGRATIONS)
    async with postgres_schema.engine.begin() as connection:
        if metadata_first:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(
                text("ALTER TABLE vpn_customers DROP COLUMN expiry_reminders")
            )
        else:
            await connection.execute(
                text(
                    "CREATE TABLE vpn_customers (id SERIAL PRIMARY KEY, telegram_user_id VARCHAR(64))"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE vpn_subscriptions (id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES vpn_customers(id), expires_at TIMESTAMPTZ)"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE vpn_bot_chats (bot_id VARCHAR(64), chat_id VARCHAR(64), PRIMARY KEY(bot_id,chat_id))"
                )
            )
            await connection.execute(
                text(
                    "CREATE TABLE vpn_access_keys (id SERIAL PRIMARY KEY, subscription_id INTEGER REFERENCES vpn_subscriptions(id), config_uri TEXT)"
                )
            )
        await connection.execute(
            text(
                "INSERT INTO vpn_customers (id, telegram_user_id) VALUES (71, '12345')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO vpn_subscriptions (id, customer_id, expires_at) VALUES (81, 71, :expiry)"
            ),
            {"expiry": NOW},
        )
        await connection.execute(
            text(
                "INSERT INTO vpn_access_keys (id, subscription_id, config_uri) VALUES (91, 81, 'synthetic-key-preserved')"
            )
        )
        for _ in range(2):
            for statement in VPN_REMINDER_MIGRATIONS:
                await connection.execute(text(statement))
        assert (
            await connection.execute(
                text("SELECT expiry_reminders FROM vpn_customers WHERE id=71")
            )
        ).scalar_one() is True
        assert (
            await connection.execute(
                text("SELECT customer_id FROM vpn_subscriptions WHERE id=81")
            )
        ).scalar_one() == 71
        assert (
            await connection.execute(
                text("SELECT config_uri FROM vpn_access_keys WHERE id=91")
            )
        ).scalar_one() == "synthetic-key-preserved"
        columns = (
            (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='vpn_expiry_reminders'"
                    )
                )
            )
            .scalars()
            .all()
        )
        assert set(columns) == set(models.VpnExpiryReminder.__table__.columns.keys())


@pytest.mark.asyncio
async def test_concurrent_dispatchers_persist_one_event_before_network(
    postgres_schema, immediate_gate
):
    factory, _ = await environment(postgres_schema)
    calls = []

    async def sender(*_args):
        calls.append(True)
        async with factory() as db:
            # A different session must see committed sending state before HTTP.
            job = await db.scalar(select(models.VpnExpiryReminder))
            assert job.state == "sending"
        return TelegramDeliveryResult("sent", message_id=71)

    await asyncio.gather(
        *[
            service().dispatch_next_expiry_reminder(
                factory, settings(), sender=sender, now=lambda: NOW
            )
            for _ in range(3)
        ]
    )
    assert calls == [True]
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnExpiryReminder))
            == 1
        )
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "sent"


@pytest.mark.asyncio
async def test_renewal_committed_before_send_lock_invalidates_claim(
    postgres_schema, immediate_gate
):
    factory, (_, subscription_id) = await environment(postgres_schema)
    _, claim = await service()._claim_reminder(factory, settings(), lambda: NOW)
    async with factory() as db:
        (
            await db.get(models.VpnSubscription, subscription_id, with_for_update=True)
        ).expires_at = NOW + timedelta(days=30)
        await db.commit()
    assert (
        await service()._mark_sending(factory, settings(), claim, lambda: NOW) is False
    )
    async with factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "skipped"


@pytest.mark.asyncio
async def test_customer_lock_wait_must_not_turn_stale_claim_into_sending(
    postgres_schema, immediate_gate, monkeypatch
):
    factory, (customer_id, _) = await environment(postgres_schema)
    clock = [NOW]
    _, claim = await service()._claim_reminder(factory, settings(), lambda: clock[0])
    original = service()._locked_reminder
    waiter_pid = asyncio.get_running_loop().create_future()

    async def tracked(db, *args, **kwargs):
        waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(service(), "_locked_reminder", tracked)
    async with factory() as holder:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        await holder.get(models.VpnCustomer, customer_id, with_for_update=True)
        task = asyncio.create_task(
            service()._mark_sending(factory, settings(), claim, lambda: clock[0])
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


@pytest.mark.asyncio
async def test_recovery_releases_old_event_before_enqueue_and_late_finalize(
    postgres_schema, immediate_gate, monkeypatch
):
    factory, _ = await environment(postgres_schema)
    _, old = await service()._claim_reminder(factory, settings(), lambda: NOW)
    assert await service()._mark_sending(factory, settings(), old, lambda: NOW)
    recovered, release = asyncio.Event(), asyncio.Event()
    original_enqueue = service()._enqueue

    async def paused_enqueue(*args):
        recovered.set()
        await release.wait()
        return await original_enqueue(*args)

    monkeypatch.setattr(service(), "_enqueue", paused_enqueue)
    clock = lambda: NOW + timedelta(days=3)
    task = asyncio.create_task(service()._claim_reminder(factory, settings(), clock))
    try:
        await asyncio.wait_for(recovered.wait(), 5)
        # Finalization locks customer/subscription before the old event. It must not
        # wait on recovery while discovery may need an FK lock on that subscription.
        await asyncio.wait_for(
            service()._finalize(
                factory, old, TelegramDeliveryResult("sent", message_id=71), clock
            ),
            5,
        )
        release.set()
        _, fresh = await asyncio.wait_for(task, 10)
        assert fresh is not None
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        jobs = (
            await db.scalars(
                select(models.VpnExpiryReminder).order_by(models.VpnExpiryReminder.id)
            )
        ).all()
        assert [(job.event_kind, job.state) for job in jobs] == [
            ("3d", "uncertain"),
            ("expired", "claimed"),
        ]


@pytest.mark.asyncio
async def test_preference_patch_waits_for_customer_then_rejects_concurrent_logout(
    postgres_schema,
):
    from app.api.routes.vpn_portal import update_preferences
    from app.schemas.vpn_portal import PortalPreferences
    from app.services.vpn_portal_auth import (
        issue_session,
        lookup_session,
        revoke_session,
    )

    factory, (customer_id, _) = await environment(postgres_schema)
    configuration = settings()
    async with factory() as db:
        raw = await issue_session(db, customer_id, "12345")
        await db.commit()
    async with factory() as holder, factory() as waiter:
        principal = await lookup_session(waiter, raw, configuration)
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await holder.get(models.VpnCustomer, customer_id, with_for_update=True)
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(settings=configuration))
        )
        task = asyncio.create_task(
            update_preferences(
                PortalPreferences(expiry_reminders=False),
                request,
                Response(),
                principal,
                waiter,
            )
        )
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            async with factory() as logout:
                await revoke_session(logout, principal, now=utcnow())
                await logout.commit()
            await holder.commit()
            with pytest.raises(HTTPException) as rejected:
                await asyncio.wait_for(task, 10)
            assert rejected.value.status_code == 401
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (await db.get(models.VpnCustomer, customer_id)).expiry_reminders is True


@pytest.mark.asyncio
async def test_inbound_chat_lock_precedes_reminder_owner_locks(
    postgres_schema, immediate_gate, monkeypatch
):
    factory, (customer_id, subscription_id) = await environment(postgres_schema)
    _, claim = await service()._claim_reminder(factory, settings(), lambda: NOW)
    original = service()._locked_reminder
    waiter_pid = asyncio.get_running_loop().create_future()

    async def tracked(db, *args, **kwargs):
        waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(service(), "_locked_reminder", tracked)
    async with factory() as inbound:
        holder_pid = await inbound.scalar(text("SELECT pg_backend_pid()"))
        await inbound.get(models.VpnBotChat, ("111", "12345"), with_for_update=True)
        task = asyncio.create_task(
            service()._mark_sending(factory, settings(), claim, lambda: NOW)
        )
        try:
            pid = await asyncio.wait_for(waiter_pid, 5)
            await wait_for_blocker(postgres_schema.engine, pid, holder_pid, task)
            # A real webhook next resolves the customer and may update a subscription.
            assert (
                await inbound.get(
                    models.VpnCustomer, customer_id, with_for_update={"nowait": True}
                )
                is not None
            )
            assert (
                await inbound.get(
                    models.VpnSubscription,
                    subscription_id,
                    with_for_update={"nowait": True},
                )
                is not None
            )
            await inbound.commit()
            assert await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_enqueue_skips_webhook_chat_before_taking_subscription_fk(
    postgres_schema,
):
    factory, (customer_id, subscription_id) = await environment(postgres_schema)

    async def discover():
        async with factory() as db, db.begin():
            await service()._enqueue(db, settings(), NOW)

    async with factory() as inbound:
        await inbound.get(models.VpnBotChat, ("111", "12345"), with_for_update=True)
        task = asyncio.create_task(discover())
        try:
            await asyncio.wait_for(task, 5)
            assert (
                await inbound.get(
                    models.VpnCustomer, customer_id, with_for_update={"nowait": True}
                )
                is not None
            )
            assert (
                await inbound.get(
                    models.VpnSubscription,
                    subscription_id,
                    with_for_update={"nowait": True},
                )
                is not None
            )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnExpiryReminder))
            == 0
        )


@pytest.mark.asyncio
async def test_403_chat_wait_preserves_newer_inbound_message(
    postgres_schema, immediate_gate, monkeypatch
):
    from app.services.vpn_bot_chats import _record_chat

    factory, _ = await environment(postgres_schema)
    _, claim = await service()._claim_reminder(factory, settings(), lambda: NOW)
    assert await service()._mark_sending(factory, settings(), claim, lambda: NOW)
    original = service()._locked_reminder
    waiter_pid = asyncio.get_running_loop().create_future()
    clock = [NOW]

    async def tracked(db, *args, **kwargs):
        waiter_pid.set_result(await db.scalar(text("SELECT pg_backend_pid()")))
        return await original(db, *args, **kwargs)

    monkeypatch.setattr(service(), "_locked_reminder", tracked)
    async with factory() as inbound:
        holder_pid = await inbound.scalar(text("SELECT pg_backend_pid()"))
        await inbound.get(models.VpnBotChat, ("111", "12345"), with_for_update=True)
        task = asyncio.create_task(
            service()._finalize(
                factory,
                claim,
                TelegramDeliveryResult("blocked", error_code="vpn_bot_blocked"),
                lambda: clock[0],
            )
        )
        try:
            pid = await asyncio.wait_for(waiter_pid, 5)
            await wait_for_blocker(postgres_schema.engine, pid, holder_pid, task)
            clock[0] = NOW + timedelta(seconds=1)
            await _record_chat(inbound, "111", "12345", 7, clock[0], unblock=True)
            await inbound.commit()
            await asyncio.wait_for(task, 10)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        chat = await db.get(models.VpnBotChat, ("111", "12345"))
        assert chat.last_update_id == 7 and chat.blocked_at is None
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "skipped"
