from datetime import UTC, datetime
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import (
    AdminAuditLog,
    VpnAccessKey,
    VpnCustomer,
    VpnSubscription,
    WorkerNode,
)
from app.db.session import get_db
from app.services.vpn_customer_lifecycle import (
    VpnCustomerArchiveConflictError,
    stage_vpn_customer_archive,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


async def seed_customer(
    session: AsyncSession,
) -> tuple[
    VpnCustomer,
    VpnSubscription,
    VpnSubscription,
    VpnSubscription,
    VpnAccessKey,
    VpnAccessKey,
]:
    customer = VpnCustomer(telegram_user_id="archive-me", status="active")
    session.add(customer)
    await session.flush()
    active = VpnSubscription(customer_id=customer.id, status="active", max_devices=2)
    trial = VpnSubscription(customer_id=customer.id, status="trial", max_devices=1)
    expired = VpnSubscription(customer_id=customer.id, status="expired", max_devices=1)
    session.add_all([active, trial, expired])
    await session.flush()
    active_key = VpnAccessKey(
        subscription_id=active.id,
        status="active",
        config_uri="vless://active",
    )
    revoked_key = VpnAccessKey(
        subscription_id=expired.id,
        status="revoked",
        config_uri=None,
    )
    session.add_all([active_key, revoked_key])
    await session.commit()
    return customer, active, trial, expired, active_key, revoked_key


@pytest.mark.asyncio
async def test_stage_archive_blocks_customer_subscriptions_and_usable_keys(session_factory):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        customer, active, trial, expired, active_key, revoked_key = await seed_customer(session)

        result = await stage_vpn_customer_archive(session, customer.id, now=now)
        await session.commit()

        assert customer.status == "archived"
        assert active.status == "disabled"
        assert trial.status == "disabled"
        assert expired.status == "expired"
        assert active_key.status == "pending_revoke"
        assert revoked_key.status == "revoked"
        assert result.disabled_subscription_count == 2
        assert [item.id for item in result.access_keys] == [active_key.id]


@pytest.mark.asyncio
async def test_stage_archive_rejects_an_already_archived_customer(session_factory):
    async with session_factory() as session:
        customer, *_ = await seed_customer(session)
        customer.status = "archived"
        await session.commit()

        with pytest.raises(VpnCustomerArchiveConflictError, match="already archived"):
            await stage_vpn_customer_archive(session, customer.id)


async def seed_archive_api_customer(session: AsyncSession) -> tuple[int, int, int]:
    worker = WorkerNode(
        name="customer-archive-node",
        status="ready",
        is_enabled=True,
        vpn_role="vpn_node",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_public_host="vpn.example.test",
        vpn_inbound_id=1,
        ssh_host="192.0.2.20",
        ssh_password="test-secret",
    )
    session.add(worker)
    await session.flush()
    customer, active, trial, _, active_key, _ = await seed_customer(session)
    active_key.worker_id = worker.id
    active_key.public_name = "revoked-on-archive"
    pending_key = VpnAccessKey(
        subscription_id=trial.id,
        worker_id=worker.id,
        public_name="pending-on-archive",
        status="active",
        config_uri="vless://pending",
    )
    session.add(pending_key)
    await session.commit()
    return customer.id, active_key.id, pending_key.id


@pytest.mark.asyncio
async def test_archive_endpoint_disables_access_and_reports_revoke_outcomes(
    api_client,
    session_factory,
    monkeypatch,
):
    async with session_factory() as session:
        customer_id, revoked_key_id, pending_key_id = await seed_archive_api_customer(session)

    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        if access_key.id == pending_key_id:
            access_key.status = "pending_revoke"
            access_key.last_error = "test node is temporarily unavailable"
        else:
            access_key.status = "revoked"
            access_key.revoked_at = datetime.now(UTC)
            access_key.last_error = None
        return access_key

    monkeypatch.setattr("app.api.routes.control.revoke_vpn_access_key", fake_revoke)

    response = await api_client.post(f"/control/vpn/customers/{customer_id}/archive")

    assert response.status_code == 200
    assert response.json()["customer"]["status"] == "archived"
    assert response.json()["disabled_subscriptions"] == 2
    assert response.json()["revoked_keys"] == 1
    assert response.json()["pending_revoke_keys"] == 1
    async with session_factory() as session:
        assert (await session.get(VpnAccessKey, revoked_key_id)).status == "revoked"
        assert (await session.get(VpnAccessKey, pending_key_id)).status == "pending_revoke"
        audit = await session.scalar(
            select(AdminAuditLog).where(AdminAuditLog.action == "vpn_customer_archive")
        )
        assert audit is not None
        assert f"customer_id={customer_id}" in (audit.details or "")


@pytest.mark.asyncio
async def test_archive_endpoint_returns_not_found_and_conflict(
    api_client,
    session_factory,
    monkeypatch,
):
    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        access_key.status = "revoked"
        access_key.revoked_at = datetime.now(UTC)
        return access_key

    monkeypatch.setattr("app.api.routes.control.revoke_vpn_access_key", fake_revoke)
    missing_response = await api_client.post("/control/vpn/customers/999/archive")
    assert missing_response.status_code == 404

    async with session_factory() as session:
        customer_id, _, _ = await seed_archive_api_customer(session)
    first_response = await api_client.post(f"/control/vpn/customers/{customer_id}/archive")
    repeated_response = await api_client.post(f"/control/vpn/customers/{customer_id}/archive")

    assert first_response.status_code == 200
    assert repeated_response.status_code == 409
    assert "already archived" in repeated_response.json()["detail"]


@pytest.mark.asyncio
async def test_restoring_customer_does_not_restore_subscriptions_or_keys(
    api_client,
    session_factory,
    monkeypatch,
):
    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        access_key.status = "revoked"
        access_key.revoked_at = datetime.now(UTC)
        return access_key

    monkeypatch.setattr("app.api.routes.control.revoke_vpn_access_key", fake_revoke)
    async with session_factory() as session:
        customer_id, revoked_key_id, pending_key_id = await seed_archive_api_customer(session)

    archive_response = await api_client.post(f"/control/vpn/customers/{customer_id}/archive")
    restore_response = await api_client.patch(
        f"/control/vpn/customers/{customer_id}",
        json={"status": "active"},
    )

    assert archive_response.status_code == 200
    assert restore_response.status_code == 200
    assert restore_response.json()["status"] == "active"
    async with session_factory() as session:
        subscriptions = list(
            (
                await session.scalars(
                    select(VpnSubscription).where(VpnSubscription.customer_id == customer_id)
                )
            ).all()
        )
        assert {item.status for item in subscriptions} == {"disabled", "expired"}
        assert (await session.get(VpnAccessKey, revoked_key_id)).status == "revoked"
        assert (await session.get(VpnAccessKey, pending_key_id)).status == "revoked"
