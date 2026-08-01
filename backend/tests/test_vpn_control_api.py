from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import WorkerNode
from app.db.session import get_db


@pytest.mark.asyncio
async def test_vpn_control_api_creates_plan_customer_subscription_and_key():
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
            json={"customer_id": customer_id, "plan_id": plan_id, "max_devices": 3},
        )
        assert subscription_response.status_code == 201
        subscription_id = subscription_response.json()["id"]

        key_response = await client.post(
            "/control/vpn/access-keys",
            json={"subscription_id": subscription_id, "worker_id": 1, "public_name": "phone"},
        )
        assert key_response.status_code == 201
        key_payload = key_response.json()
        assert key_payload["status"] == "pending_sync"
        assert key_payload["external_uuid"]
        assert key_payload["worker_id"] == 1

        overview_response = await client.get("/control/vpn/overview")

    assert overview_response.status_code == 200
    overview = overview_response.json()
    assert overview["enabled_nodes"] == 1
    assert overview["ready_nodes"] == 1
    assert overview["active_customers"] == 1
    assert overview["active_subscriptions"] == 1
    assert overview["active_keys"] == 0
    await engine.dispose()
