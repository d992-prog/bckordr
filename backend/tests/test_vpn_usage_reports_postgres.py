"""Report queries against a disposable schema, never the application database."""

from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_usage_api import (
    CUSTOMER,
    NODES,
    assert_boundary_interval_cannot_hide_internal_reset_gap,
    assert_mutable_current_hour_preserves_known_bytes,
    hour,
    poll,
    report_app,
    sample,
    seed,
)

from app.db.base import Base


@pytest_asyncio.fixture
async def pg_env(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    app = report_app(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(sessions=sessions, client=client)


@pytest.mark.asyncio
async def test_report_grouping_and_scoped_subqueries_execute_on_postgres(pg_env):
    ids = await seed(pg_env, count=2)
    async with pg_env.sessions() as db:
        for key, worker in zip(ids.keys, ids.workers):
            db.add_all(
                [
                    hour(key, worker, ids.now),
                    sample(key, worker, ids.now),
                    poll(worker, ids.now),
                ]
            )
        await db.commit()
    nodes = await pg_env.client.get(NODES)
    customer = await pg_env.client.get(CUSTOMER.format(ids.customer))
    assert nodes.status_code == customer.status_code == 200
    assert [row["upload_bytes"] for row in nodes.json()] == [10, 10]
    assert customer.json()["upload_bytes"] == 20
    assert len(customer.json()["devices"]) == 1
    assert customer.json()["devices"][0]["download_bytes"] == 40


@pytest.mark.asyncio
async def test_mutable_current_hour_after_request_clock_keeps_known_totals_on_postgres(
    pg_env, monkeypatch
):
    await assert_mutable_current_hour_preserves_known_bytes(pg_env, monkeypatch)


@pytest.mark.asyncio
async def test_boundary_minute_cannot_hide_reset_gap_on_postgres(pg_env, monkeypatch):
    await assert_boundary_interval_cannot_hide_internal_reset_gap(pg_env, monkeypatch)
