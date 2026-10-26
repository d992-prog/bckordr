"""Device serialization proof, exclusively in the validated synthetic PostgreSQL schema."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)

from app.api.routes import control
from app.db.base import Base, utcnow
from app.db.models import (
    User,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnDevice,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.schemas.control import VpnAccessKeyCreateRequest
from app.services.vpn_devices import (
    attach_initial_device,
    lock_key_device_context,
    stage_device_revoke,
)
from app.services.vpn_policy import count_device_slots
from app.services.vpn_subscription_sync import stage_subscription_update


async def seed(postgres_schema, *, extra_child=False):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with factory() as db:
        customer = VpnCustomer(status="active")
        db.add_all([customer, User(id=1, username="safe-fake", password_hash="safe-fake", role="admin")])
        await db.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=2,
                                       expires_at=utcnow() + timedelta(days=1))
        db.add(subscription)
        await db.flush()
        key = VpnAccessKey(subscription_id=subscription.id, status="active", external_uuid=str(uuid4()),
                           config_uri="vless://safe-fake@vpn.example:443#stored")
        db.add(key)
        await db.flush()
        device = await attach_initial_device(db, key)
        if extra_child:
            db.add(VpnAccessKey(subscription_id=subscription.id, device_id=device.id, status="suspended",
                                external_uuid=str(uuid4()), config_uri="vless://safe-fake-child@vpn.example:443#stored"))
        await db.commit()
    return factory


async def wait_for_blocker(engine, waiter_pid, holder_pid, task):
    async with engine.connect() as monitor:
        async with asyncio.timeout(10):
            while True:
                if holder_pid in await monitor.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": waiter_pid}):
                    return
                assert not task.done(), "Writer did not wait for the customer's database lock"
                await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_two_creators_one_remaining_device_slot_have_one_winner(postgres_schema, monkeypatch):
    factory = await seed(postgres_schema)
    from test_vpn_public_trial import worker as ready_worker

    async with factory() as db:
        node = ready_worker("safe-fake-create", vpn_last_checked_at=utcnow())
        db.add(node)
        await db.flush()
        db.add(VpnEndpoint(worker_id=node.id, inbound_id=1, public_host="vpn.example", port=443,
                           security="reality", transport="raw", status="ready", verified_at=utcnow(),
                           max_active_profiles=10, is_published=True, server_name="cdn.example",
                           public_key="A" * 43, short_id="aabbccdd", fingerprint="chrome", flow="xtls-rprx-vision"))
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    original = control._validate_vpn_key_issue
    async with factory() as first, factory() as second:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        assert first_pid != second_pid
        async def gate(db, subscription, **kwargs):
            await original(db, subscription, **kwargs)
            if db is first:
                entered.set()
                await asyncio.wait_for(release.wait(), 20)
        monkeypatch.setattr(control, "_validate_vpn_key_issue", gate)
        payload = VpnAccessKeyCreateRequest(subscription_id=1)
        winner = asyncio.create_task(control.create_vpn_access_key(payload, first, SimpleNamespace(id=1)))
        loser = None
        try:
            await asyncio.wait_for(entered.wait(), 10)
            loser = asyncio.create_task(control.create_vpn_access_key(payload, second, SimpleNamespace(id=1)))
            await wait_for_blocker(postgres_schema.engine, second_pid, first_pid, loser)
            release.set()
            assert (await asyncio.wait_for(winner, 15)).device_id is not None
            with pytest.raises(HTTPException, match="VPN device limit reached") as error:
                await asyncio.wait_for(loser, 15)
            assert error.value.status_code == 409
            await second.rollback()
        finally:
            release.set()
            for task in (winner, loser):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (winner, loser) if task is not None], return_exceptions=True)
    async with factory() as db:
        assert await count_device_slots(db, 1) == 2
        assert await db.scalar(select(func.count()).select_from(VpnDevice)) == 2
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("first_action", ["revoke", "renew"])
async def test_revoke_and_renewal_are_serialized_and_permanent_revoke_wins(postgres_schema, first_action):
    factory = await seed(postgres_schema, extra_child=True)
    async def act(db, action):
        key = await lock_key_device_context(db, 1)
        subscription = await db.get(VpnSubscription, key.subscription_id)
        if action == "revoke":
            await stage_device_revoke(db, key)
        else:
            await stage_subscription_update(db, subscription, {"expires_at": utcnow() + timedelta(days=30)})
        await db.commit()
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        cached_parent = await waiter.get(VpnDevice, 1)
        assert cached_parent is not None and cached_parent.token_generation == 0
        await lock_key_device_context(holder, 1)
        task = asyncio.create_task(act(waiter, "renew" if first_action == "revoke" else "revoke"))
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await act(holder, first_action)
            await asyncio.wait_for(task, 15)
            assert (await waiter.get(VpnDevice, 1, populate_existing=True)).token_generation == 1
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        device = await db.get(VpnDevice, 1)
        assert device.revoke_requested_at is not None and device.token_generation == 1
        assert [key.status for key in (await db.scalars(select(VpnAccessKey).order_by(VpnAccessKey.id))).all()] == ["pending_revoke"] * 2
        assert await count_device_slots(db, 1) == 1


@pytest.mark.asyncio
async def test_existing_overlimit_resync_restores_all_children(postgres_schema, monkeypatch):
    from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
    factory = await seed(postgres_schema, extra_child=True)
    async def transport(*args):
        return "DROPCATCH_VPN_CLIENT_STATUS=provisioned\nDROPCATCH_VPN_CLIENT_URL=vless://safe-fake@vpn.example:443"
    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", transport)
    async with factory() as db:
        worker = WorkerNode(name="safe-fake", status="ready", vpn_enabled=True, vpn_role="vpn_node",
                            vpn_runtime_status="ready", vpn_public_host="vpn.example", vpn_inbound_id=1,
                            ssh_host="192.0.2.1", ssh_password="safe-fake")
        db.add(worker)
        await db.flush()
        subscription = await db.get(VpnSubscription, 1)
        key = VpnAccessKey(subscription_id=1, status="suspended", external_uuid=str(uuid4()))
        db.add(key)
        await db.flush()
        device = await attach_initial_device(db, key)
        db.add(VpnAccessKey(subscription_id=1, device_id=device.id, status="suspended", external_uuid=str(uuid4())))
        await db.flush()
        for child in (await db.scalars(select(VpnAccessKey))).all():
            child.worker_id, child.status = worker.id, "suspended"
        subscription.max_devices = 1
        await db.commit()
        result = await run_vpn_lifecycle_maintenance(db)
        assert result["provisioned_keys"] == 4
        assert await count_device_slots(db, 1) == 2
        assert {key.status for key in (await db.scalars(select(VpnAccessKey))).all()} == {"active"}


@pytest.mark.asyncio
async def test_uncertain_child_keeps_one_logical_slot_reserved(postgres_schema):
    factory = await seed(postgres_schema, extra_child=True)
    async with factory() as db:
        worker = WorkerNode(name="safe-fake")
        db.add(worker)
        await db.flush()
        endpoint = VpnEndpoint(worker_id=worker.id, inbound_id=1, public_host="vpn.example", port=443)
        db.add(endpoint)
        await db.flush()
        children = (await db.scalars(select(VpnAccessKey).order_by(VpnAccessKey.id))).all()
        children[0].status = "revoked"
        children[1].status = "pending_revoke"  # The uncertain revoke has not acknowledged removal.
        children[1].worker_id, children[1].endpoint_id = worker.id, endpoint.id
        children[1].operation_generation = 1
        db.add(VpnControlOperation(id=str(uuid4()), access_key_id=children[1].id, worker_id=worker.id,
                                   endpoint_id=endpoint.id, generation=1, action="revoke", state="uncertain",
                                   request_snapshot="{}", request_digest="a" * 64))
        await db.commit()
    async with factory() as independent:
        assert await count_device_slots(independent, 1) == 1
        child = await independent.get(VpnAccessKey, 2)
        child.status = "revoked"
        await independent.commit()
        assert await count_device_slots(independent, 1) == 0
