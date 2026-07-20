from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import DiscoveryDomain, DiscoveryObservation, DiscoveryWorkerTask, WorkerNode
from app.services.discovery_worker_runtime import (
    apply_discovery_worker_task_result,
    enqueue_due_discovery_worker_tasks,
)
from app.schemas.runtime import DiscoveryWorkerTaskResultRequest


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
async def test_enqueue_due_discovery_worker_tasks_spreads_domains_across_workers():
    engine, session_factory = await _make_test_session_factory()
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add_all(
            [
                WorkerNode(name="worker-1", control_token="token-1", status="ready", is_enabled=True),
                WorkerNode(name="worker-2", control_token="token-2", status="running", is_enabled=True),
                WorkerNode(name="offline", control_token="token-3", status="offline", is_enabled=True),
            ]
        )
        for index in range(4):
            session.add(
                DiscoveryDomain(
                    fqdn=f"example{index}.se",
                    zone="se",
                    status="tracking",
                    next_check_at=now - timedelta(seconds=1),
                )
            )
        await session.commit()

    async with session_factory() as session:
        created = await enqueue_due_discovery_worker_tasks(session, now=now, batch_size=10)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(DiscoveryWorkerTask).order_by(DiscoveryWorkerTask.id.asc()))
        tasks = list(result.scalars().all())

    assert created == 4
    assert len(tasks) == 4
    assert {task.worker_id for task in tasks} == {1, 2}
    assert [task.discovery_domain_id for task in tasks] == [1, 2, 3, 4]

    await engine.dispose()


@pytest.mark.asyncio
async def test_enqueue_due_discovery_worker_tasks_does_not_duplicate_active_domain_task():
    engine, session_factory = await _make_test_session_factory()
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(WorkerNode(name="worker-1", control_token="token-1", status="ready", is_enabled=True))
        session.add(DiscoveryDomain(fqdn="example.se", zone="se", status="tracking", next_check_at=now))
        await session.flush()
        session.add(
            DiscoveryWorkerTask(
                discovery_domain_id=1,
                worker_id=1,
                status="running",
                assigned_at=now,
            )
        )
        await session.commit()

    async with session_factory() as session:
        created = await enqueue_due_discovery_worker_tasks(session, now=now, batch_size=10)
        await session.commit()

    async with session_factory() as session:
        task_count = len((await session.execute(select(DiscoveryWorkerTask))).scalars().all())

    assert created == 0
    assert task_count == 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_discovery_worker_task_result_updates_domain_and_keeps_last_observations():
    engine, session_factory = await _make_test_session_factory()
    now = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(WorkerNode(name="worker-1", control_token="token-1", status="ready", is_enabled=True))
        session.add(DiscoveryDomain(fqdn="example.se", zone="se", status="tracking", next_check_at=now))
        await session.flush()
        for index in range(5):
            session.add(
                DiscoveryObservation(
                    discovery_domain_id=1,
                    source="rdap",
                    observed_at=now - timedelta(minutes=index + 1),
                    lifecycle_stage="registered",
                    availability_status="taken",
                )
            )
        task = DiscoveryWorkerTask(discovery_domain_id=1, worker_id=1, status="running", assigned_at=now)
        session.add(task)
        await session.commit()

    async with session_factory() as session:
        task = await session.get(DiscoveryWorkerTask, 1)
        assert task is not None
        await apply_discovery_worker_task_result(
            session,
            task,
            DiscoveryWorkerTaskResultRequest(
                worker_id=1,
                source="rdap",
                observed_at=now,
                http_status=404,
                latency_ms=120,
                lifecycle_stage="not_found",
                availability_status="available",
                status_codes=[],
                raw_response='{"title":"Not found"}',
                error=None,
            ),
            now=now,
        )
        await session.commit()

    async with session_factory() as session:
        domain = await session.get(DiscoveryDomain, 1)
        observations = (
            await session.execute(
                select(DiscoveryObservation).where(DiscoveryObservation.discovery_domain_id == 1)
            )
        ).scalars().all()
        task = await session.get(DiscoveryWorkerTask, 1)

    assert domain is not None
    assert domain.status == "available"
    assert domain.available_first_seen_at == now.replace(tzinfo=None)
    assert task is not None
    assert task.status == "completed"
    assert len(observations) == 5

    await engine.dispose()
