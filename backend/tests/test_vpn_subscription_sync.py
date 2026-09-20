import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.deps import require_admin
from app.api.routes.control import router
from app.db.base import Base, utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription, WorkerNode
from app.db.session import get_db
from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance


UUID = "11111111-1111-4111-8111-111111111111"
URI = f"vless://{UUID}@vpn.example:8443?security=none&type=tcp"


@pytest_asyncio.fixture
async def env(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'sync.db').as_posix()}")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    app = FastAPI()
    app.include_router(router)

    async def database():
        async with factory() as session:
            yield session

    async def admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_admin] = admin
    commands = []

    async def ssh(worker, incoming):
        commands.extend(incoming)
        return (
            "DROPCATCH_VPN_CLIENT_STATUS=provisioned\n"
            f"DROPCATCH_VPN_CLIENT_URL={URI}\n"
            "DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended\n"
            "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked\n"
        )

    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", ssh)
    async with factory() as db:
        customer = VpnCustomer(status="active")
        worker = WorkerNode(
            name="sync-test", status="ready", vpn_enabled=True, vpn_role="vpn_node",
            vpn_runtime_status="ready", vpn_public_host="vpn.example", vpn_inbound_id=1,
            vpn_inbound_port=8443, ssh_host="192.0.2.1", ssh_password="test-password",
        )
        db.add_all([customer, worker])
        await db.flush()
        subscription = VpnSubscription(
            customer_id=customer.id, status="active", starts_at=utcnow() - timedelta(days=30),
            expires_at=utcnow() + timedelta(days=1), max_devices=5, traffic_limit_gb=10,
        )
        db.add(subscription)
        await db.flush()
        key = VpnAccessKey(
            subscription_id=subscription.id, worker_id=worker.id, status="active",
            external_uuid=UUID, config_uri=URI, expires_at=subscription.expires_at,
        )
        db.add(key)
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield SimpleNamespace(factory=factory, client=client, commands=commands)
    await engine.dispose()


async def key_snapshot(env):
    async with env.factory() as db:
        return await db.get(VpnAccessKey, 1)


async def cycle(env, **kwargs):
    async with env.factory() as db:
        return await run_vpn_lifecycle_maintenance(db, **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"expires_at": "2028-01-01T00:00:00Z"}, {"expires_at": None},
    {"traffic_limit_gb": 30}, {"traffic_limit_gb": None}, {"max_devices": 4},
])
async def test_policy_edit_queues_existing_identity(env, updates):
    response = await env.client.patch("/control/vpn/subscriptions/1", json=updates)
    assert response.status_code == 200
    key = await key_snapshot(env)
    assert key.status == "pending_sync"
    assert (key.external_uuid, key.config_uri, key.worker_id) == (UUID, URI, 1)
    result = await cycle(env)
    assert result["provisioned_keys"] == 1
    assert (await key_snapshot(env)).status == "active"
    if "expires_at" in updates:
        expected = updates["expires_at"]
        assert key.expires_at == (datetime.fromisoformat(expected).replace(tzinfo=None) if expected else None)


@pytest.mark.asyncio
async def test_notes_only_edit_does_not_sync(env):
    assert (await env.client.patch("/control/vpn/subscriptions/1", json={"notes": "note"})).status_code == 200
    assert (await key_snapshot(env)).status == "active"
    assert (await cycle(env))["checked_keys"] == 0
    assert env.commands == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["disabled", "expired"])
async def test_suspend_then_renew_restores_same_link(env, status):
    response = await env.client.patch("/control/vpn/subscriptions/1", json={"status": status})
    assert response.status_code == 200
    assert (await key_snapshot(env)).status == "pending_suspend"
    assert (await cycle(env))["suspended_keys"] == 1
    suspended = await key_snapshot(env)
    assert suspended.status == "suspended"
    assert suspended.revoked_at is None
    response = await env.client.patch("/control/vpn/subscriptions/1", json={
        "status": "active", "expires_at": "2028-01-01T00:00:00Z",
    })
    assert response.status_code == 200
    assert (await cycle(env))["provisioned_keys"] == 1
    restored = await key_snapshot(env)
    assert restored.status == "active"
    assert (restored.external_uuid, restored.config_uri, restored.worker_id) == (UUID, URI, 1)


@pytest.mark.asyncio
async def test_renewal_overrides_stale_expiry_and_pending_suspension(env):
    async with env.factory() as db:
        key = await db.get(VpnAccessKey, 1)
        key.expires_at = utcnow() - timedelta(days=1)
        key.status = "pending_suspend"
        await db.commit()
    response = await env.client.patch("/control/vpn/subscriptions/1", json={"expires_at": "2028-01-01T00:00:00Z"})
    assert response.status_code == 200
    assert (await cycle(env))["provisioned_keys"] == 1
    assert (await key_snapshot(env)).status == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["revoked", "pending_revoke"])
async def test_renewal_never_restores_permanent_revoke(env, status):
    async with env.factory() as db:
        (await db.get(VpnAccessKey, 1)).status = status
        await db.commit()
    await env.client.patch("/control/vpn/subscriptions/1", json={"expires_at": "2028-01-01T00:00:00Z"})
    assert (await key_snapshot(env)).status == status
    await cycle(env)
    assert (await key_snapshot(env)).status == "revoked"


@pytest.mark.asyncio
async def test_cancel_permanently_revokes_even_suspended_key(env):
    async with env.factory() as db:
        (await db.get(VpnAccessKey, 1)).status = "suspended"
        await db.commit()
    await env.client.patch("/control/vpn/subscriptions/1", json={"status": "cancelled"})
    assert (await key_snapshot(env)).status == "pending_revoke"
    await cycle(env)
    assert (await key_snapshot(env)).status == "revoked"


@pytest.mark.asyncio
async def test_expiry_and_future_start_use_reversible_suspension(env):
    future_start = utcnow() + timedelta(hours=1)
    response = await env.client.patch("/control/vpn/subscriptions/1", json={"starts_at": future_start.isoformat()})
    assert response.status_code == 200
    await cycle(env)
    assert (await key_snapshot(env)).status == "suspended"
    assert (await cycle(env))["checked_keys"] == 0
    assert (await cycle(env, now=future_start + timedelta(seconds=1)))["provisioned_keys"] == 1
    async with env.factory() as db:
        (await db.get(VpnSubscription, 1)).expires_at = utcnow() - timedelta(seconds=1)
        await db.commit()
    assert (await cycle(env))["suspended_keys"] == 1


@pytest.mark.asyncio
async def test_reject_device_reduction_below_retained_keys(env):
    async with env.factory() as db:
        db.add(VpnAccessKey(subscription_id=1, status="suspended"))
        await db.commit()
    response = await env.client.patch("/control/vpn/subscriptions/1", json={"max_devices": 1})
    assert response.status_code == 409
    async with env.factory() as db:
        assert (await db.get(VpnSubscription, 1)).max_devices == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"status": None}, {"max_devices": None}, {"status": "nonsense"},
                                          {"expires_at": "2020-01-01T00:00:00Z"}])
async def test_reject_invalid_policy(env, updates):
    response = await env.client.patch("/control/vpn/subscriptions/1", json=updates)
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("customer_status", ["blocked", "archived"])
async def test_cannot_renew_inactive_customer(env, customer_status):
    async with env.factory() as db:
        (await db.get(VpnCustomer, 1)).status = customer_status
        await db.commit()
    response = await env.client.patch("/control/vpn/subscriptions/1", json={"expires_at": "2028-01-01T00:00:00Z"})
    assert response.status_code == 409


@pytest.mark.asyncio
async def test_failed_suspend_remains_pending_then_renewal_applies_latest_policy(env, monkeypatch):
    async def offline(*args):
        raise RuntimeError("offline test-password")

    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", offline)
    await env.client.patch("/control/vpn/subscriptions/1", json={"status": "disabled"})
    await cycle(env)
    key = await key_snapshot(env)
    assert key.status == "pending_suspend"
    assert "test-password" not in key.last_error
    await env.client.patch("/control/vpn/subscriptions/1", json={"status": "active", "max_devices": 6})
    await cycle(env)
    assert (await key_snapshot(env)).status == "pending_sync"


@pytest.mark.asyncio
async def test_manual_revoke_waits_for_inflight_lifecycle_sync(env, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    remote_revoke = asyncio.Event()

    async def delayed_ssh(worker, commands):
        if "DROPCATCH_VPN_CLIENT_PAYLOAD=" in commands[0]:
            entered.set()
            await release.wait()
            return f"DROPCATCH_VPN_CLIENT_STATUS=provisioned\nDROPCATCH_VPN_CLIENT_URL={URI}\n"
        remote_revoke.set()
        return "DROPCATCH_VPN_CLIENT_REVOKE_STATUS=revoked\n"

    monkeypatch.setattr("app.services.vpn_provisioning.execute_worker_ssh_commands", delayed_ssh)
    async with env.factory() as db:
        (await db.get(VpnAccessKey, 1)).status = "pending_sync"
        await db.commit()
    cycling = asyncio.create_task(cycle(env))
    await asyncio.wait_for(entered.wait(), 2)
    revoking = asyncio.create_task(env.client.post("/control/vpn/access-keys/1/revoke"))
    try:
        await asyncio.sleep(0.05)
        assert not revoking.done()
        assert not remote_revoke.is_set()
    finally:
        release.set()
        await cycling
        response = await revoking
    assert response.status_code == 200
    assert (await key_snapshot(env)).status == "revoked"


@pytest.mark.asyncio
@pytest.mark.parametrize("manual", [False, True])
async def test_previously_issued_key_with_missing_node_never_moves_automatically(env, manual):
    async with env.factory() as db:
        key = await db.get(VpnAccessKey, 1)
        key.worker_id = None
        key.status = "pending_sync"
        await db.commit()
    if manual:
        response = await env.client.post("/control/vpn/access-keys/1/provision")
        assert response.status_code == 409
    else:
        await cycle(env)
    key = await key_snapshot(env)
    assert key.worker_id is None
    assert key.status == "pending_sync"
    assert key.config_uri == URI
    assert env.commands == []
