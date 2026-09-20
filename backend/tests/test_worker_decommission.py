from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
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
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
)
from app.db.session import get_db
from app.services.worker_decommission import (
    WorkerDecommissionConflictError,
    decommission_worker,
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


@pytest.mark.asyncio
async def test_worker_archive_timestamp_is_persisted(session_factory):
    archived_at = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = WorkerNode(name="retired-node", archived_at=archived_at)
        session.add(worker)
        await session.commit()
        await session.refresh(worker)

        assert worker.archived_at is not None
        assert worker.archived_at.replace(tzinfo=UTC) == archived_at


async def seed_vpn_node(
    session: AsyncSession,
) -> tuple[WorkerNode, VpnCustomer, VpnSubscription, VpnAccessKey]:
    worker = WorkerNode(
        name="obsolete-vpn-node",
        status="ready",
        is_enabled=True,
        control_token="worker-token",
        ip_address="203.0.113.10",
        ssh_host="203.0.113.10",
        ssh_username="root",
        ssh_password="ssh-secret",
        ssh_key_path="/root/.ssh/id_ed25519",
        vpn_role="drop_worker_vpn",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_public_host="obsolete.example",
        vpn_panel_url="https://obsolete.example:2053",
        vpn_panel_username="admin",
        vpn_panel_password="panel-secret",
        vpn_inbound_id=1,
    )
    customer = VpnCustomer(status="active")
    session.add_all([worker, customer])
    await session.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
    session.add(subscription)
    await session.flush()
    access_key = VpnAccessKey(
        subscription_id=subscription.id,
        worker_id=worker.id,
        status="active",
        config_uri="vless://secret-client-uri",
    )
    session.add(access_key)
    await session.commit()
    return worker, customer, subscription, access_key


@pytest.mark.asyncio
async def test_decommission_worker_archives_node_and_retires_attached_keys(session_factory):
    now = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    async with session_factory() as session:
        worker, customer, subscription, access_key = await seed_vpn_node(session)

        result = await decommission_worker(session, worker.id, now=now)
        await session.commit()

        await session.refresh(worker)
        await session.refresh(access_key)
        assert result.retired_key_count == 1
        assert worker.archived_at is not None
        assert worker.archived_at.replace(tzinfo=UTC) == now
        assert worker.status == "archived"
        assert worker.is_enabled is False
        assert worker.control_token is None
        assert worker.ssh_password is None
        assert worker.ssh_key_path is None
        assert worker.vpn_panel_password is None
        assert worker.vpn_enabled is False
        assert worker.vpn_role == "none"
        assert worker.vpn_runtime_status == "decommissioned"
        assert access_key.status == "revoked"
        assert access_key.revoked_at is not None
        assert access_key.revoked_at.replace(tzinfo=UTC) == now
        assert access_key.config_uri is None
        assert access_key.worker_id == worker.id
        assert "remote revoke was not confirmed" in (access_key.last_error or "")
        assert await session.get(VpnCustomer, customer.id) is not None
        assert await session.get(VpnSubscription, subscription.id) is not None
        event = await session.scalar(
            select(VpnNodeEvent).where(VpnNodeEvent.worker_id == worker.id)
        )
        assert event is not None
        assert event.event_type == "node_decommissioned"
        assert event.details is not None
        assert event.details["retired_key_count"] == 1


@pytest.mark.asyncio
async def test_decommission_worker_rejects_active_attack_without_partial_changes(session_factory):
    async with session_factory() as session:
        worker, _, _, access_key = await seed_vpn_node(session)
        now = datetime.now(UTC)
        domain = DropDomain(fqdn="busy.example", zone="example", drop_date=now.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now,
            planned_end_at=now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="running",
            )
        )
        await session.commit()

        with pytest.raises(WorkerDecommissionConflictError, match="active domain attack"):
            await decommission_worker(session, worker.id, now=now)
        await session.rollback()

        await session.refresh(worker)
        await session.refresh(access_key)
        assert worker.archived_at is None
        assert worker.control_token == "worker-token"
        assert access_key.status == "active"
        assert access_key.config_uri == "vless://secret-client-uri"


@pytest.mark.asyncio
async def test_decommission_worker_rejects_active_maintenance(session_factory):
    async with session_factory() as session:
        worker, _, _, _ = await seed_vpn_node(session)
        session.add(WorkerMaintenanceJob(worker_id=worker.id, action="vpn_update", status="queued"))
        await session.commit()

        with pytest.raises(WorkerDecommissionConflictError, match="active maintenance"):
            await decommission_worker(session, worker.id)


@pytest.mark.asyncio
async def test_delete_worker_archives_and_hides_it_from_active_apis(
    api_client,
    session_factory,
    monkeypatch,
):
    allowlist_worker_counts: list[int] = []

    async def fake_sync(session, settings):
        del settings
        active_count = len(
            (
                await session.scalars(
                    select(WorkerNode).where(WorkerNode.is_enabled.is_(True))
                )
            ).all()
        )
        allowlist_worker_counts.append(active_count)
        return True

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)
    async with session_factory() as session:
        worker, _, _, access_key = await seed_vpn_node(session)
        worker_id = worker.id
        key_id = access_key.id

    response = await api_client.delete(f"/control/workers/{worker_id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "Worker decommissioned"
    assert (await api_client.get("/control/workers")).json() == []
    assert (await api_client.get("/control/vpn/nodes/eligibility")).json() == []
    assert allowlist_worker_counts == [0]

    async with session_factory() as session:
        worker = await session.get(WorkerNode, worker_id)
        access_key = await session.get(VpnAccessKey, key_id)
        assert worker is not None
        assert worker.archived_at is not None
        assert access_key is not None
        assert access_key.status == "revoked"


@pytest.mark.asyncio
async def test_delete_worker_returns_409_for_active_attack(api_client, session_factory):
    async with session_factory() as session:
        worker, _, _, access_key = await seed_vpn_node(session)
        worker_id = worker.id
        key_id = access_key.id
        now = datetime.now(UTC)
        domain = DropDomain(fqdn="api-busy.example", zone="example", drop_date=now.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now,
            planned_end_at=now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker_id,
                status="running",
            )
        )
        await session.commit()

    response = await api_client.delete(f"/control/workers/{worker_id}")
    assert response.status_code == 409
    assert response.json()["detail"] == "Worker is assigned to an active domain attack"

    async with session_factory() as session:
        worker = await session.get(WorkerNode, worker_id)
        access_key = await session.get(VpnAccessKey, key_id)
        assert worker is not None
        assert worker.archived_at is None
        assert worker.control_token == "worker-token"
        assert access_key is not None
        assert access_key.status == "active"


@pytest.mark.asyncio
async def test_archived_worker_cannot_be_updated_or_maintained(api_client, session_factory):
    async with session_factory() as session:
        worker, _, _, _ = await seed_vpn_node(session)
        worker_id = worker.id
        await decommission_worker(session, worker_id)
        await session.commit()

    update = await api_client.patch(f"/control/workers/{worker_id}", json={"is_enabled": True})
    maintenance = await api_client.post(f"/control/workers/{worker_id}/maintenance/vpn-check")
    setup = await api_client.get(f"/control/workers/{worker_id}/setup")
    assert update.status_code == 404
    assert maintenance.status_code == 404
    assert setup.status_code == 404
