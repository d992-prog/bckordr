"""Upgrade and lock races only in the validated synthetic PostgreSQL database."""

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_vpn_device_locations_postgres import environment
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import postgres_schema as postgres_schema  # noqa: PLC0414
from test_vpn_public_trial import NOW

from app.api.routes import control
from app.core.config import Settings
from app.db.models import VpnAccessKey, VpnControlOperation, VpnCustomer, VpnDevice, VpnDeviceOperation, VpnEndpoint, VpnSubscription, WorkerNode
from app.schemas.control import VpnAccessKeyCreateRequest
from app.services.vpn_control_intents import stage_vpn_control_operation
from app.services.vpn_device_operations import request_device_creation, request_device_operation, resume_device_operations
from app.services.vpn_devices import attach_initial_device, lock_owned_device, stage_device_revoke
from app.services.vpn_policy import count_device_slots


def test_device_operation_migrations_registered_after_backfill():
    from app.db.migrations import MIGRATIONS
    from app.db.vpn_device_migrations import VPN_DEVICE_MIGRATIONS
    from app.db.vpn_device_operations_migrations import VPN_DEVICE_OPERATIONS_MIGRATIONS
    assert MIGRATIONS.index(VPN_DEVICE_OPERATIONS_MIGRATIONS[0]) > MIGRATIONS.index(VPN_DEVICE_MIGRATIONS[-1])
    assert MIGRATIONS[-len(VPN_DEVICE_OPERATIONS_MIGRATIONS):] == VPN_DEVICE_OPERATIONS_MIGRATIONS


async def setup(postgres_schema, monkeypatch):
    factory, (owner_id, device_id, endpoints), _ = await environment(postgres_schema, monkeypatch)
    async with factory() as db:
        key = await db.scalar(select(VpnAccessKey).where(VpnAccessKey.device_id == device_id))
        key.verified_client_email, key.panel_sub_id = "vpn-key-11", uuid4().hex
        subscription_id, key_id = key.subscription_id, key.id
        await db.commit()
    return factory, SimpleNamespace(owner=owner_id, device=device_id, endpoints=endpoints,
                                     subscription=subscription_id, key=key_id, settings=Settings(_env_file=None))


async def replace(db, ids, request_id):
    return await request_device_operation(db, customer_id=ids.owner, device_id=ids.device,
                                         action="replace", request_id=request_id, settings=ids.settings, now=NOW)


@pytest.mark.asyncio
async def test_same_request_replays_after_independent_session_lock_wait(postgres_schema, monkeypatch):
    factory, ids = await setup(postgres_schema, monkeypatch)
    request_id = uuid4()
    async with factory() as holder, factory() as waiter:
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        first = await replace(holder, ids, request_id)
        async def replay():
            second = await replace(waiter, ids, request_id)
            await waiter.commit()
            return second.id
        task = asyncio.create_task(replay())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            assert await asyncio.wait_for(task, 15) == first.id
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnDeviceOperation)) == 1
        assert (await db.get(VpnDevice, ids.device)).token_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operator_first", [False, True])
async def test_operator_revoke_and_replace_serialize_without_resurrection(postgres_schema, monkeypatch, operator_first):
    factory, ids = await setup(postgres_schema, monkeypatch)
    async def revoke(db):
        _, _, _, keys = await lock_owned_device(db, ids.owner, ids.device)
        await stage_device_revoke(db, next(key for key in keys if key.id == ids.key), now=NOW)
        await db.commit()
    async with factory() as holder, factory() as waiter:
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        await lock_owned_device(holder, ids.owner, ids.device)
        async def waiting_action():
            if operator_first:
                with pytest.raises(ValueError, match="customer_device_subscription_unavailable"):
                    await replace(waiter, ids, uuid4())
                await waiter.rollback()
            else:
                await revoke(waiter)
        task = asyncio.create_task(waiting_action())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            if operator_first:
                await revoke(holder)
            else:
                await replace(holder, ids, uuid4())
                await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        await resume_device_operations(db, settings=ids.settings, now=NOW)
        device = await db.get(VpnDevice, ids.device)
        assert device.revoke_requested_at is not None and device.pending_key_id is None
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 1
        assert (await db.get(VpnAccessKey, ids.key)).status == "pending_revoke"


@pytest.mark.asyncio
async def test_admin_creation_cannot_take_replacement_slot_after_all_old_keys_revoked(postgres_schema, monkeypatch):
    factory, ids = await setup(postgres_schema, monkeypatch)
    monkeypatch.setattr(control, "utcnow", lambda: NOW)
    monkeypatch.setattr("app.services.vpn_policy.utcnow", lambda: NOW)
    async with factory() as holder, factory() as waiter:
        operation = await replace(holder, ids, uuid4())
        old = await holder.get(VpnAccessKey, ids.key)
        old.status, old.revoked_at = "revoked", NOW
        await holder.flush()
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        async def admin_create():
            with pytest.raises(HTTPException, match="VPN device limit reached"):
                await control.create_vpn_access_key(VpnAccessKeyCreateRequest(subscription_id=ids.subscription),
                                                    waiter, SimpleNamespace(id=1))
            await waiter.rollback()
        task = asyncio.create_task(admin_create())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert await count_device_slots(db, ids.subscription) == 1
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 1
        assert (await db.get(VpnDeviceOperation, operation.id)).stage == "revoking"


@pytest.mark.asyncio
async def test_internal_old_key_revoke_cannot_be_replayed_after_operator_revoke(postgres_schema, monkeypatch):
    factory, ids = await setup(postgres_schema, monkeypatch)
    async with factory() as db:
        operation = await replace(db, ids, uuid4())
        await db.commit()
    async with factory() as holder, factory() as waiter:
        # Cache the old operation before the operator obtains the owner lock.
        await waiter.get(VpnDeviceOperation, operation.id)
        _, _, _, keys = await lock_owned_device(holder, ids.owner, ids.device)
        await stage_device_revoke(holder, next(key for key in keys if key.id == ids.key), now=NOW)
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        async def internal_revoke():
            with pytest.raises(ValueError, match="vpn_control_device_operation_invalid"):
                await stage_vpn_control_operation(waiter, ids.key, "revoke", now=NOW,
                                                  device_operation_id=operation.id)
            await waiter.rollback()
        task = asyncio.create_task(internal_revoke())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_customer_creations_on_one_country_capacity_have_one_winner(postgres_schema, monkeypatch):
    factory, ids = await setup(postgres_schema, monkeypatch)
    async with factory() as db:
        subscription = await db.get(VpnSubscription, ids.subscription)
        subscription.max_devices = 2
        other = VpnCustomer(status="active")
        db.add(other)
        await db.flush()
        other_subscription = VpnSubscription(customer_id=other.id, status="active", max_devices=1)
        db.add(other_subscription)
        await db.commit()
        other_id, other_subscription_id = other.id, other_subscription.id
    async def create(db, customer_id, subscription_id):
        return await request_device_creation(db, customer_id=customer_id, subscription_id=subscription_id,
                                             endpoint_id=ids.endpoints[1], display_name="Laptop", request_id=uuid4(),
                                             settings=ids.settings, now=NOW)
    async with factory() as holder, factory() as waiter:
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        await create(holder, ids.owner, ids.subscription)
        async def losing_create():
            with pytest.raises(ValueError, match="customer_device_endpoint_unavailable"):
                await create(waiter, other_id, other_subscription_id)
            await waiter.rollback()
        task = asyncio.create_task(losing_create())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnDeviceOperation)) == 1
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey).where(VpnAccessKey.endpoint_id == ids.endpoints[1])) == 1


@pytest.mark.asyncio
async def test_delayed_lifecycle_candidate_refreshes_after_independent_replacement_completion(postgres_schema, monkeypatch):
    from app.services import vpn_lifecycle
    from test_vpn_device_operations import receipt, resume

    factory, ids = await setup(postgres_schema, monkeypatch)
    async with factory() as db:
        await replace(db, ids, uuid4())
        await db.commit()
    candidate_read, release = asyncio.Event(), asyncio.Event()
    original = vpn_lifecycle.lock_key_device_context
    async with factory() as holder, factory() as waiter:
        assert await holder.scalar(text("SELECT pg_backend_pid()")) != await waiter.scalar(text("SELECT pg_backend_pid()"))
        async def delayed_lock(db, key_id):
            if db is waiter and key_id == ids.key:
                candidate_read.set()
                await asyncio.wait_for(release.wait(), 20)
            return await original(db, key_id)
        monkeypatch.setattr(vpn_lifecycle, "lock_key_device_context", delayed_lock)
        task = asyncio.create_task(vpn_lifecycle.run_vpn_lifecycle_maintenance(waiter, now=NOW, settings=ids.settings))
        try:
            await asyncio.wait_for(candidate_read.wait(), 10)
            env = SimpleNamespace(db=holder, settings=ids.settings)
            await receipt(env)
            await resume(env)
            await receipt(env)
            await resume(env)
            release.set()
            await asyncio.wait_for(task, 15)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        device = await db.get(VpnDevice, ids.device)
        fresh = await db.get(VpnAccessKey, device.preferred_key_id)
        assert fresh.status == "active" and fresh.credential_generation == 1
        assert device.revoke_requested_at is None and device.token_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shared_worker", [False, True])
async def test_opposite_country_replacements_lock_shared_targets_without_deadlock(postgres_schema, monkeypatch, shared_worker):
    factory, first_ids = await setup(postgres_schema, monkeypatch)
    async with factory() as db:
        device = await db.get(VpnDevice, first_ids.device)
        subscription = await db.get(VpnSubscription, first_ids.subscription)
        first, second = [await db.get(VpnEndpoint, identity) for identity in first_ids.endpoints[:2]]
        first.max_active_profiles = second.max_active_profiles = 4
        if shared_worker:
            second.worker_id, second.inbound_id = first.worker_id, 2
        await db.flush()
        def key(identity, owner_subscription, endpoint, device_id=None):
            return VpnAccessKey(id=identity, subscription_id=owner_subscription.id, device_id=device_id,
                                endpoint_id=endpoint.id, worker_id=endpoint.worker_id, status="active",
                                external_uuid=str(uuid4()), panel_sub_id=uuid4().hex,
                                verified_client_email=f"vpn-key-{identity}", config_uri="vless://safe-fake@vpn.example:443")
        db.add(key(12, subscription, second, device.id))
        device.preferred_key_id = 12
        other = VpnCustomer(status="active", telegram_user_id="200")
        db.add(other)
        await db.flush()
        other_subscription = VpnSubscription(customer_id=other.id, status="active", max_devices=1,
                                             expires_at=subscription.expires_at)
        db.add(other_subscription)
        await db.flush()
        other_first = key(21, other_subscription, second)
        db.add(other_first)
        await db.flush()
        other_device = await attach_initial_device(db, other_first)
        db.add(key(22, other_subscription, first, other_device.id))
        await db.flush()
        other_device.preferred_key_id = 22
        await db.commit()
        second_ids = SimpleNamespace(owner=other.id, device=other_device.id, settings=first_ids.settings)
    first_worker_locked, release = asyncio.Event(), asyncio.Event()
    async with factory() as holder, factory() as waiter:
        holder_pid, waiter_pid = await holder.scalar(text("SELECT pg_backend_pid()")), await waiter.scalar(text("SELECT pg_backend_pid()"))
        original = holder.scalar
        async def gate(statement, **kwargs):
            result = await original(statement, **kwargs)
            if (statement._for_update_arg is not None and statement.column_descriptions[0].get("entity") is WorkerNode
                    and not first_worker_locked.is_set()):
                first_worker_locked.set()
                await asyncio.wait_for(release.wait(), 20)
            return result
        monkeypatch.setattr(holder, "scalar", gate)
        async def replacing(db, ids):
            operation = await replace(db, ids, uuid4())
            await db.commit()
            return operation.id
        winner = asyncio.create_task(replacing(holder, first_ids))
        contender = None
        try:
            await asyncio.wait_for(first_worker_locked.wait(), 10)
            contender = asyncio.create_task(replacing(waiter, second_ids))
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, contender)
            release.set()
            await asyncio.wait_for(asyncio.gather(winner, contender), 20)
        finally:
            release.set()
            for task in (winner, contender):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (winner, contender) if task is not None], return_exceptions=True)
    async with factory() as db:
        operations = (await db.scalars(select(VpnDeviceOperation))).all()
        assert len(operations) == 2 and all(operation.stage == "revoking" for operation in operations)
        assert await db.scalar(select(func.count()).select_from(VpnControlOperation)) == 4
        for ids in (first_ids, second_ids):
            device = await db.get(VpnDevice, ids.device)
            assert device.revoke_requested_at is None and device.token_generation == 1


@pytest.mark.asyncio
async def test_old_startup_backfill_and_generation_upgrade_are_repeatable_with_history(postgres_schema, monkeypatch):
    factory, ids = await setup(postgres_schema, monkeypatch)
    from app.db.vpn_device_migrations import VPN_DEVICE_MIGRATIONS
    from app.db.vpn_device_operations_migrations import VPN_DEVICE_OPERATIONS_MIGRATIONS
    async with postgres_schema.engine.begin() as conn:
        original = (await conn.execute(text("SELECT id,external_uuid,config_uri FROM vpn_access_keys ORDER BY id"))).all()
        await conn.execute(text("DROP TABLE vpn_device_operations"))
        await conn.execute(text("ALTER TABLE vpn_access_keys DROP CONSTRAINT uq_vpn_access_key_device_generation_endpoint"))
        await conn.execute(text("ALTER TABLE vpn_access_keys DROP COLUMN credential_generation"))
        await conn.execute(text("ALTER TABLE vpn_devices DROP COLUMN credential_generation"))
        await conn.execute(text("ALTER TABLE vpn_access_keys ADD CONSTRAINT uq_vpn_access_key_device_endpoint UNIQUE(device_id,endpoint_id)"))
    for pass_number in range(2):
        async with postgres_schema.engine.begin() as conn:
            for statement in (*VPN_DEVICE_MIGRATIONS, *VPN_DEVICE_OPERATIONS_MIGRATIONS):
                await conn.execute(text(statement))
            assert (await conn.execute(text("SELECT id,external_uuid,config_uri FROM vpn_access_keys WHERE credential_generation=0 ORDER BY id"))).all() == original
            if pass_number == 0:
                await conn.execute(text("""INSERT INTO vpn_access_keys
                    (subscription_id,device_id,worker_id,endpoint_id,credential_generation)
                    SELECT subscription_id,device_id,worker_id,endpoint_id,1 FROM vpn_access_keys WHERE id=:key
                """), {"key": ids.key})
            constraints = set((await conn.execute(text("SELECT conname FROM pg_constraint WHERE conrelid='vpn_access_keys'::regclass"))).scalars())
            assert "uq_vpn_access_key_device_endpoint" not in constraints
            assert "uq_vpn_access_key_device_generation_endpoint" in constraints
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 2
        assert await count_device_slots(db, ids.subscription) == 1
    async with postgres_schema.engine.begin() as conn:
        for sql, expected in (
            ("UPDATE vpn_devices SET credential_generation=-1", "ck_vpn_device_credential_generation"),
            ("UPDATE vpn_access_keys SET credential_generation=-1", "ck_vpn_access_key_credential_generation"),
            ("UPDATE vpn_access_keys SET credential_generation=0", "uq_vpn_access_key_device_generation_endpoint"),
        ):
            async with conn.begin_nested() as savepoint:
                with pytest.raises(IntegrityError, match=expected):
                    await conn.execute(text(sql))
                await savepoint.rollback()
