"""Real independent-session races in the validated synthetic PostgreSQL schema."""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_device_locations import NOW, seed_device_locations
from test_vpn_devices_postgres import wait_for_blocker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)

from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomerSession,
    VpnDevice,
)
from app.services import vpn_devices
from app.services.vpn_control_intents import (
    claim_next_vpn_control_operation,
    finalize_vpn_control_operation,
)
from app.services.vpn_devices import (
    lock_owned_device,
    select_device_location,
    stage_device_revoke,
)


async def environment(postgres_schema, monkeypatch, *, second_customer=False):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    monkeypatch.setattr(vpn_devices, "load_transport_snapshot", lambda *_: object())
    async with factory() as db:
        owner, _subscription, device, _key, endpoints = await seed_device_locations(db)
        ids = owner.id, device.id, [endpoint.id for endpoint in endpoints]
        other = None
        if second_customer:
            owner2, _, device2, _, _ = await seed_device_locations(db, identity="200")
            other = owner2.id, device2.id
    return factory, ids, other


async def choose(db, owner_id, device_id, endpoint_id):
    return await select_device_location(db, customer_id=owner_id, device_id=device_id, endpoint_id=endpoint_id,
                                        settings=Settings(_env_file=None), now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("same_endpoint", [True, False])
async def test_location_choices_serialize_one_child_and_last_pending(postgres_schema, monkeypatch, same_endpoint):
    factory, (owner_id, device_id, endpoints), _ = await environment(postgres_schema, monkeypatch)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        assert holder_pid != waiter_pid
        await lock_owned_device(holder, owner_id, device_id)
        target = endpoints[1] if same_endpoint else endpoints[2]
        async def waiting_choice():
            await choose(waiter, owner_id, device_id, target)
            await waiter.commit()
        task = asyncio.create_task(waiting_choice())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await choose(holder, owner_id, device_id, endpoints[1])
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        device = await db.get(VpnDevice, device_id)
        pending = await db.get(VpnAccessKey, device.pending_key_id)
        assert pending.endpoint_id == target
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == (2 if same_endpoint else 3)
        assert await db.scalar(select(func.count()).select_from(VpnControlOperation)) == (1 if same_endpoint else 2)
        if not same_endpoint:
            original_id, pending_id = device.preferred_key_id, device.pending_key_id
            token = uuid4()
            claimed = await claim_next_vpn_control_operation(db, claim_token=token, now=NOW + timedelta(seconds=1))
            # Equal timestamps use UUID order; claim both workers if the newer choice wins that tie.
            if claimed.endpoint_id != endpoints[1]:
                await db.commit()
                token = uuid4()
                claimed = await claim_next_vpn_control_operation(db, claim_token=token, now=NOW + timedelta(seconds=1))
            assert claimed.endpoint_id == endpoints[1]
            await db.commit()
            await finalize_vpn_control_operation(db, UUID(claimed.id), token, receipt_state="observed", error_code=None,
                                                  now=NOW + timedelta(seconds=2))
            await db.commit()
            await db.refresh(device)
            assert device.preferred_key_id == original_id and device.pending_key_id == pending_id


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_first", [True, False])
async def test_revoke_selection_race_never_leaves_a_new_usable_child(postgres_schema, monkeypatch, revoke_first):
    factory, (owner_id, device_id, endpoints), _ = await environment(postgres_schema, monkeypatch)
    async def revoke(db):
        _, _, device, keys = await lock_owned_device(db, owner_id, device_id)
        await stage_device_revoke(db, next(key for key in keys if key.device_id == device.id), now=NOW)
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await lock_owned_device(holder, owner_id, device_id)
        async def waiting_action():
            if revoke_first:
                with pytest.raises(ValueError, match="customer_location_unavailable"):
                    await choose(waiter, owner_id, device_id, endpoints[1])
            else:
                await revoke(waiter)
            await waiter.commit()
        task = asyncio.create_task(waiting_action())
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            if revoke_first:
                await revoke(holder)
            else:
                await choose(holder, owner_id, device_id, endpoints[1])
            await holder.commit()
            await asyncio.wait_for(task, 15)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    async with factory() as db:
        device = await db.get(VpnDevice, device_id)
        assert device.revoke_requested_at is not None and device.pending_key_id is None
        assert device.token_generation == 1
        assert {key.status for key in (await db.scalars(select(VpnAccessKey))).all()} == {"pending_revoke"}


@pytest.mark.asyncio
async def test_two_customers_one_endpoint_capacity_have_one_winner(postgres_schema, monkeypatch):
    from app.services import vpn_policy
    factory, (owner_id, device_id, endpoints), other = await environment(postgres_schema, monkeypatch, second_customer=True)
    allocated, release = asyncio.Event(), asyncio.Event()
    original = vpn_policy.select_public_vpn_endpoint
    async with factory() as holder, factory() as waiter:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        async def gate(db, **kwargs):
            selected = await original(db, **kwargs)
            if db is holder:
                allocated.set()
                await asyncio.wait_for(release.wait(), 20)
            return selected
        monkeypatch.setattr(vpn_policy, "select_public_vpn_endpoint", gate)
        async def selecting(db, customer_id, selected_device):
            await choose(db, customer_id, selected_device, endpoints[1])
            await db.commit()
        winner = asyncio.create_task(selecting(holder, owner_id, device_id))
        loser = None
        try:
            await asyncio.wait_for(allocated.wait(), 10)
            loser = asyncio.create_task(selecting(waiter, *other))
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, loser)
            release.set()
            await asyncio.wait_for(winner, 15)
            with pytest.raises(ValueError, match="customer_location_unavailable"):
                await asyncio.wait_for(loser, 15)
            await waiter.rollback()
        finally:
            release.set()
            for task in (winner, loser):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(*[task for task in (winner, loser) if task is not None], return_exceptions=True)
    async with factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey).where(VpnAccessKey.endpoint_id == endpoints[1])) == 1


@pytest.mark.asyncio
async def test_expired_captured_principal_cannot_mutate_after_customer_lock_wait(postgres_schema, monkeypatch):
    from app.api.routes.vpn_portal import _device_mutation_principal
    from app.services.vpn_portal_auth import issue_session, lookup_session
    factory, (owner_id, device_id, _), _ = await environment(postgres_schema, monkeypatch)
    settings = Settings(_env_file=None, VPN_PORTAL_ENABLED=True, VPN_PORTAL_ALLOWED_TELEGRAM_IDS="100")
    async with factory() as seed:
        raw = await issue_session(seed, owner_id, "100")
        await seed.commit()
    async with factory() as holder, factory() as waiter:
        principal = await lookup_session(waiter, raw, settings)
        assert principal is not None
        session = await holder.get(VpnCustomerSession, principal.session.token_hash)
        session.expires_at = utcnow() + timedelta(seconds=0.3)
        await holder.commit()
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        waiter_pid = await waiter.scalar(text("SELECT pg_backend_pid()"))
        await lock_owned_device(holder, owner_id, device_id)
        task = asyncio.create_task(_device_mutation_principal(waiter, principal, settings))
        try:
            await wait_for_blocker(postgres_schema.engine, waiter_pid, holder_pid, task)
            await asyncio.sleep(0.4)
            await holder.commit()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, 15)
            assert error.value.status_code == 401
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
