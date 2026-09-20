from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

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
