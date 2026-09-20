from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import AttackRun, DropDomain, VpnAccessKey, VpnSubscription, WorkerNode, WorkerTask
from app.db.session import get_db


@pytest.mark.asyncio
async def test_vpn_control_api_creates_plan_customer_subscription_and_key(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add(
            WorkerNode(
                name="vpn-node-1",
                registrar_slug="gandi",
                status="ready",
                is_enabled=True,
                ip_address="2.27.20.255",
                max_rps=16,
                target_rps=16,
                vpn_role="drop_worker+vpn_node",
                vpn_enabled=True,
                vpn_runtime_status="ready",
                vpn_public_host="de-1.example.net",
                vpn_inbound_id=1,
                ssh_host="2.27.20.255",
                ssh_password="secret",
            ),
        )
        await session.commit()

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        access_key.config_uri = f"vless://test-{access_key.id}"
        return access_key

    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        access_key.status = "revoked"
        access_key.revoked_at = datetime.now(UTC)
        return access_key

    monkeypatch.setattr("app.api.routes.control.provision_vpn_access_key", fake_provision)
    monkeypatch.setattr("app.api.routes.control.revoke_vpn_access_key", fake_revoke)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        plan_response = await client.post(
            "/control/vpn/plans",
            json={
                "slug": "monthly",
                "name": "Monthly",
                "duration_days": 30,
                "traffic_limit_gb": 100,
                "max_devices": 3,
                "price_amount": 490,
                "currency": "RUB",
            },
        )
        assert plan_response.status_code == 201
        plan_id = plan_response.json()["id"]

        customer_response = await client.post(
            "/control/vpn/customers",
            json={"telegram_user_id": "12345", "telegram_username": "client"},
        )
        assert customer_response.status_code == 201
        customer_id = customer_response.json()["id"]

        subscription_response = await client.post(
            "/control/vpn/subscriptions",
            json={"customer_id": customer_id, "plan_id": plan_id},
        )
        assert subscription_response.status_code == 201
        subscription_payload = subscription_response.json()
        subscription_id = subscription_payload["id"]
        starts_at = datetime.fromisoformat(subscription_payload["starts_at"])
        expires_at = datetime.fromisoformat(subscription_payload["expires_at"])
        assert timedelta(days=29, hours=23) < expires_at - starts_at < timedelta(days=30, minutes=1)
        assert subscription_payload["traffic_limit_gb"] == 100
        assert subscription_payload["max_devices"] == 3

        key_response = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": subscription_id, "worker_id": 1, "public_name": "phone"},
        )
        assert key_response.status_code == 201
        key_payload = key_response.json()
        assert key_payload["status"] == "active"
        assert key_payload["external_uuid"]
        assert key_payload["worker_id"] == 1
        key_id = key_payload["id"]

        delete_key_response = await client.delete(f"/control/vpn/access-keys/{key_id}")
        assert delete_key_response.status_code == 200
        assert delete_key_response.json()["id"] == key_id
        assert delete_key_response.json()["status"] == "revoked"

        keys_after_delete_response = await client.get("/control/vpn/access-keys")
        assert keys_after_delete_response.status_code == 200
        assert [item["id"] for item in keys_after_delete_response.json()] == [key_id]
        assert keys_after_delete_response.json()[0]["status"] == "revoked"

        overview_response = await client.get("/control/vpn/overview")

    assert overview_response.status_code == 200
    overview = overview_response.json()
    assert overview["enabled_nodes"] == 1
    assert overview["ready_nodes"] == 1
    assert overview["active_customers"] == 1
    assert overview["active_subscriptions"] == 1
    assert overview["active_keys"] == 0
    await engine.dispose()


@pytest.mark.asyncio
async def test_vpn_access_key_api_enforces_subscription_and_selects_safe_node(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        busy = WorkerNode(
            name="busy-vpn-node",
            status="ready",
            is_enabled=True,
            vpn_role="drop_worker+vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
            vpn_public_host="busy.example.net",
            vpn_inbound_id=1,
            ssh_host="10.0.0.1",
            ssh_password="secret",
        )
        free = WorkerNode(
            name="free-vpn-node",
            status="ready",
            is_enabled=True,
            vpn_role="vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
            vpn_public_host="free.example.net",
            vpn_inbound_id=1,
            ssh_host="10.0.0.2",
            ssh_password="secret",
        )
        unready = WorkerNode(
            name="unready-vpn-node",
            status="ready",
            is_enabled=True,
            vpn_role="vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="not_installed",
            vpn_public_host="unready.example.net",
            vpn_inbound_id=1,
            ssh_host="10.0.0.3",
            ssh_password="secret",
        )
        session.add_all([busy, free, unready])
        await session.flush()
        busy_worker_id = busy.id
        free_worker_id = free.id
        domain = DropDomain(fqdn="busy-target.fr", zone="fr", drop_date=datetime.now(UTC).date())
        session.add(domain)
        await session.flush()
        attack = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=datetime.now(UTC) - timedelta(minutes=1),
            planned_end_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        session.add(attack)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=attack.id,
                domain_id=domain.id,
                worker_id=busy.id,
                status="running",
            )
        )
        await session.commit()

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        access_key.config_uri = f"vless://test-{access_key.id}"
        return access_key

    revoke_attempts: list[int] = []

    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        revoke_attempts.append(access_key.id)
        access_key.status = "revoked"
        return access_key

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin
    monkeypatch.setattr("app.api.routes.control.provision_vpn_access_key", fake_provision)
    monkeypatch.setattr("app.api.routes.control.revoke_vpn_access_key", fake_revoke)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        plan = (
            await client.post(
                "/control/vpn/plans",
                json={"slug": "one", "name": "One device", "duration_days": 30, "max_devices": 1},
            )
        ).json()
        customer = (
            await client.post("/control/vpn/customers", json={"telegram_user_id": "safe-select"})
        ).json()
        subscription = (
            await client.post(
                "/control/vpn/subscriptions",
                json={"customer_id": customer["id"], "plan_id": plan["id"]},
            )
        ).json()

        automatic = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": subscription["id"], "public_name": "phone"},
        )
        assert automatic.status_code == 201
        assert automatic.json()["worker_id"] == free_worker_id
        assert automatic.json()["status"] == "active"

        second_device = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": subscription["id"], "worker_id": free_worker_id, "public_name": "tablet"},
        )
        assert second_device.status_code == 409
        assert second_device.json()["detail"] == "VPN device limit reached"

        expired_customer = (
            await client.post("/control/vpn/customers", json={"telegram_user_id": "expired"})
        ).json()
        expired_subscription = (
            await client.post(
                "/control/vpn/subscriptions",
                json={
                    "customer_id": expired_customer["id"],
                    "starts_at": (datetime.now(UTC) - timedelta(days=2)).isoformat(),
                    "expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                },
            )
        ).json()
        expired_key = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": expired_subscription["id"], "worker_id": free_worker_id},
        )
        assert expired_key.status_code == 409
        assert expired_key.json()["detail"] == "VPN subscription has expired"

        busy_customer = (
            await client.post("/control/vpn/customers", json={"telegram_user_id": "busy"})
        ).json()
        busy_subscription = (
            await client.post(
                "/control/vpn/subscriptions",
                json={"customer_id": busy_customer["id"], "plan_id": plan["id"]},
            )
        ).json()
        unsafe_worker = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": busy_subscription["id"], "worker_id": busy_worker_id},
        )
        assert unsafe_worker.status_code == 409
        assert "active domain attack" in unsafe_worker.json()["detail"]

        async with session_factory() as session:
            blocked_key = VpnAccessKey(
                subscription_id=busy_subscription["id"],
                worker_id=busy_worker_id,
                status="active",
                external_uuid="11111111-1111-1111-1111-111111111111",
                config_uri="vless://busy-key",
            )
            session.add(blocked_key)
            await session.commit()
            blocked_key_id = blocked_key.id

        blocked_revoke = await client.post(f"/control/vpn/access-keys/{blocked_key_id}/revoke")
        blocked_delete = await client.delete(f"/control/vpn/access-keys/{blocked_key_id}")
        assert blocked_revoke.status_code == 409
        assert blocked_delete.status_code == 409
        assert revoke_attempts == []
        async with session_factory() as session:
            retained_key = await session.get(VpnAccessKey, blocked_key_id)
            assert retained_key is not None
            assert retained_key.status == "pending_revoke"

        async with session_factory() as session:
            free_worker = await session.get(WorkerNode, free_worker_id)
            assert free_worker is not None
            free_worker.status = "offline"
            await session.commit()

        pending_customer = (
            await client.post("/control/vpn/customers", json={"telegram_user_id": "pending"})
        ).json()
        pending_subscription = (
            await client.post(
                "/control/vpn/subscriptions",
                json={"customer_id": pending_customer["id"], "plan_id": plan["id"]},
            )
        ).json()
        pending_key = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": pending_subscription["id"], "public_name": "waiting"},
        )
        assert pending_key.status_code == 201
        assert pending_key.json()["worker_id"] is None
        assert pending_key.json()["status"] == "pending_sync"
        assert pending_key.json()["last_error"] == "No safe VPN node is currently available"

        async with session_factory() as session:
            free_worker = await session.get(WorkerNode, free_worker_id)
            assert free_worker is not None
            free_worker.status = "ready"
            await session.commit()

        retry = await client.post(f"/control/vpn/access-keys/{pending_key.json()['id']}/provision")
        assert retry.status_code == 200
        assert retry.json()["worker_id"] == free_worker_id
        assert retry.json()["status"] == "active"

    await engine.dispose()


@pytest.mark.asyncio
async def test_vpn_lifecycle_endpoint_expires_subscription_and_marks_key_pending_suspend(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        session.add(
            WorkerNode(
                name="vpn-node-1",
                registrar_slug="gandi",
                status="ready",
                is_enabled=True,
                ip_address="2.27.20.255",
                max_rps=16,
                target_rps=16,
                vpn_role="drop_worker+vpn_node",
                vpn_enabled=True,
                vpn_runtime_status="ready",
                vpn_public_host="de-1.example.net",
                vpn_inbound_id=1,
                ssh_host="2.27.20.255",
                ssh_password="secret",
            ),
        )
        await session.commit()

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        access_key.config_uri = f"vless://test-{access_key.id}"
        return access_key

    async def fake_suspend(db, access_key, *, worker=None):
        del db, worker
        access_key.status = "pending_suspend"
        access_key.last_error = "simulated unavailable node"
        return access_key

    monkeypatch.setattr("app.api.routes.control.provision_vpn_access_key", fake_provision)
    monkeypatch.setattr("app.services.vpn_lifecycle.suspend_vpn_access_key", fake_suspend)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        plan_response = await client.post(
            "/control/vpn/plans",
            json={
                "slug": "daily",
                "name": "Daily",
                "duration_days": 1,
                "traffic_limit_gb": None,
                "max_devices": 1,
                "price_amount": 0,
                "currency": "RUB",
            },
        )
        plan_id = plan_response.json()["id"]

        customer_response = await client.post(
            "/control/vpn/customers",
            json={"telegram_user_id": "12345", "telegram_username": "client"},
        )
        customer_id = customer_response.json()["id"]

        subscription_response = await client.post(
            "/control/vpn/subscriptions",
            json={"customer_id": customer_id, "plan_id": plan_id, "max_devices": 1},
        )
        subscription_id = subscription_response.json()["id"]

        key_response = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": subscription_id, "worker_id": 1, "public_name": "phone"},
        )
        assert key_response.status_code == 201

        async with session_factory() as session:
            subscription = await session.get(VpnSubscription, subscription_id)
            assert subscription is not None
            subscription.expires_at = datetime.now(UTC) - timedelta(minutes=1)
            await session.commit()

        lifecycle_response = await client.post("/control/vpn/lifecycle/run")
        assert lifecycle_response.status_code == 200
        lifecycle_payload = lifecycle_response.json()
        assert lifecycle_payload["expired_subscriptions"] == 1
        assert lifecycle_payload["checked_keys"] == 1
        assert lifecycle_payload["pending_suspend_keys"] == 1

        status_response = await client.get("/control/vpn/lifecycle/status")
        assert status_response.status_code == 200
        assert status_response.json()["ran_at"] is not None
        assert status_response.json()["expired_subscriptions"] == 1
        assert status_response.json()["pending_suspend_keys"] == 1

        keys_response = await client.get("/control/vpn/access-keys")
        assert keys_response.status_code == 200
        keys = keys_response.json()
        assert keys[0]["status"] == "pending_suspend"

        subscriptions_response = await client.get("/control/vpn/subscriptions")
        assert subscriptions_response.status_code == 200
        subscriptions = subscriptions_response.json()
        assert subscriptions[0]["status"] == "expired"

    await engine.dispose()
