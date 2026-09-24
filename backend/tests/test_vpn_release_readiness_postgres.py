from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.models import VpnEndpoint, VpnPlan, WorkerMaintenanceJob, WorkerNode
from app.services.vpn_release_readiness import lock_release_readiness_tables

if TYPE_CHECKING:
    from test_vpn_endpoint_migrations import PostgresSchema

pytest_plugins = ("test_vpn_endpoint_migrations",)


@dataclass(frozen=True)
class PostgresReadiness:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]
    endpoint_id: int
    plan_id: int
    maintenance_id: int


@pytest_asyncio.fixture
async def postgres_readiness(postgres_schema: PostgresSchema) -> PostgresReadiness:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    now = datetime.now(UTC)
    async with sessions() as session:
        worker = WorkerNode(
            name="readiness-barrier-worker",
            status="ready",
            is_enabled=True,
            vpn_enabled=True,
            vpn_role="vpn_node",
            vpn_runtime_status="ready",
        )
        plan = VpnPlan(
            slug="trial-7d",
            name="Trial",
            is_active=True,
            duration_days=7,
            max_devices=1,
        )
        session.add_all([worker, plan])
        await session.flush()
        endpoint = VpnEndpoint(
            worker_id=worker.id,
            inbound_id=1,
            public_host="vpn.example",
            port=443,
            protocol="vless",
            transport="raw",
            security="reality",
            status="ready",
            verified_at=now,
            max_active_profiles=10,
        )
        maintenance = WorkerMaintenanceJob(
            worker_id=worker.id,
            action="vpn_update",
            status="finished",
        )
        session.add_all([endpoint, maintenance])
        await session.commit()
        result = PostgresReadiness(
            engine=postgres_schema.engine,
            sessions=sessions,
            endpoint_id=endpoint.id,
            plan_id=plan.id,
            maintenance_id=maintenance.id,
        )
    return result


async def _wait_until_locking(engine: AsyncEngine, pid: int) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with engine.connect() as connection:
            waiting = await connection.scalar(
                text(
                    "SELECT wait_event_type = 'Lock' "
                    "FROM pg_stat_activity WHERE pid = :pid"
                ),
                {"pid": pid},
            )
        if waiting is True:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("database session did not wait for a table lock")


def _case_statements(control: PostgresReadiness, case: str):
    if case == "capacity":
        return (
            update(VpnEndpoint)
            .where(VpnEndpoint.id == control.endpoint_id)
            .values(max_active_profiles=7),
            select(VpnEndpoint.max_active_profiles).where(
                VpnEndpoint.id == control.endpoint_id
            ),
            update(VpnEndpoint)
            .where(VpnEndpoint.id == control.endpoint_id)
            .values(max_active_profiles=8),
            7,
            8,
        )
    if case == "plan":
        return (
            update(VpnPlan)
            .where(VpnPlan.id == control.plan_id)
            .values(is_active=False),
            select(VpnPlan.is_active).where(VpnPlan.id == control.plan_id),
            update(VpnPlan)
            .where(VpnPlan.id == control.plan_id)
            .values(is_active=True),
            False,
            True,
        )
    return (
        update(WorkerMaintenanceJob)
        .where(WorkerMaintenanceJob.id == control.maintenance_id)
        .values(status="queued"),
        select(WorkerMaintenanceJob.status).where(
            WorkerMaintenanceJob.id == control.maintenance_id
        ),
        update(WorkerMaintenanceJob)
        .where(WorkerMaintenanceJob.id == control.maintenance_id)
        .values(status="running"),
        "queued",
        "running",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["capacity", "plan", "maintenance"])
async def test_release_readiness_table_barrier_orders_existing_and_new_writers(
    postgres_readiness: PostgresReadiness,
    case: str,
) -> None:
    writer_statement, read_statement, late_statement, expected, late_expected = (
        _case_statements(postgres_readiness, case)
    )
    barrier_task = None
    late_task = None
    async with (
        postgres_readiness.sessions() as writer,
        postgres_readiness.sessions() as marker,
    ):
        try:
            await writer.execute(writer_statement)
            marker_pid = int(await marker.scalar(text("SELECT pg_backend_pid()")))
            barrier_task = asyncio.create_task(lock_release_readiness_tables(marker))
            await _wait_until_locking(postgres_readiness.engine, marker_pid)
            assert barrier_task.done() is False

            await writer.commit()
            await asyncio.wait_for(barrier_task, timeout=5)
            assert await marker.scalar(read_statement) == expected

            late_pid_ready = asyncio.get_running_loop().create_future()

            async def late_write() -> None:
                async with postgres_readiness.sessions() as late:
                    late_pid_ready.set_result(
                        int(await late.scalar(text("SELECT pg_backend_pid()")))
                    )
                    await late.execute(late_statement)
                    await late.commit()

            late_task = asyncio.create_task(late_write())
            late_pid = await asyncio.wait_for(late_pid_ready, timeout=5)
            await _wait_until_locking(postgres_readiness.engine, late_pid)
            assert late_task.done() is False

            await marker.commit()
            await asyncio.wait_for(late_task, timeout=5)
        finally:
            await writer.rollback()
            await marker.rollback()
            for task in (barrier_task, late_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (barrier_task, late_task) if task is not None),
                return_exceptions=True,
            )

    async with postgres_readiness.sessions() as observer:
        assert await observer.scalar(read_statement) == late_expected
