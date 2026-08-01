from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnPlan,
    VpnSubscription,
    VpnTelegramUpdate,
    WorkerNode,
)


async def _make_test_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, session_factory


@pytest.mark.asyncio
async def test_vpn_models_store_subscription_key_and_node_metadata():
    engine, session_factory = await _make_test_session_factory()
    now = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    async with session_factory() as session:
        worker = WorkerNode(
            name="vpn-node-1",
            control_token="worker-token",
            status="ready",
            vpn_role="drop_worker+vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
            vpn_public_host="de-1.example.net",
            vpn_panel_url="https://de-1.example.net:2053",
            vpn_inbound_id=7,
        )
        plan = VpnPlan(
            slug="basic-month",
            name="Basic month",
            duration_days=30,
            traffic_limit_gb=150,
            max_devices=2,
            price_amount=990,
            currency="RUB",
        )
        customer = VpnCustomer(
            telegram_user_id="123456",
            telegram_username="customer",
            first_name="Test",
            last_name="User",
        )
        session.add_all([worker, plan, customer])
        await session.flush()

        subscription = VpnSubscription(
            customer_id=customer.id,
            plan_id=plan.id,
            status="active",
            starts_at=now,
            expires_at=now + timedelta(days=30),
            traffic_limit_gb=150,
            max_devices=2,
        )
        session.add(subscription)
        await session.flush()

        key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            protocol="vless",
            public_name="Customer phone",
            external_uuid="vpn-client-uuid",
            config_uri="vless://vpn-client-uuid@example.net",
            status="active",
            issued_at=now,
        )
        event = VpnNodeEvent(
            worker_id=worker.id,
            level="info",
            event_type="key_created",
            message="VPN key created",
            details={"subscription_id": subscription.id},
        )
        update = VpnTelegramUpdate(
            update_id="tg-update-1",
            customer_id=customer.id,
            payload={"message": {"text": "/start"}},
            processed_at=now,
        )
        session.add_all([key, event, update])
        await session.commit()

    async with session_factory() as session:
        saved_key = (
            await session.execute(select(VpnAccessKey).where(VpnAccessKey.external_uuid == "vpn-client-uuid"))
        ).scalar_one()
        saved_worker = await session.get(WorkerNode, saved_key.worker_id)
        saved_subscription = await session.get(VpnSubscription, saved_key.subscription_id)
        saved_customer = await session.get(VpnCustomer, saved_subscription.customer_id)
        saved_event = (await session.execute(select(VpnNodeEvent))).scalar_one()
        saved_update = (await session.execute(select(VpnTelegramUpdate))).scalar_one()

    assert saved_worker is not None
    assert saved_worker.vpn_enabled is True
    assert saved_worker.vpn_role == "drop_worker+vpn_node"
    assert saved_worker.vpn_inbound_id == 7
    assert saved_subscription.status == "active"
    assert saved_customer.telegram_user_id == "123456"
    assert saved_key.config_uri.startswith("vless://")
    assert saved_event.details == {"subscription_id": saved_subscription.id}
    assert saved_update.update_id == "tg-update-1"

    await engine.dispose()
