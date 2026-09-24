"""Real PostgreSQL coverage for the fleet-wide advisory probe lease."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.models import VpnEndpoint, WorkerNode
from app.services.vpn_fleet_health import probe_next_vpn_endpoint
from app.services.vpn_node_health import VpnNodeHealthReceipt

pytest_plugins = ("test_vpn_endpoint_migrations",)

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
KNOWN_HOSTS = Path("/etc/veltrix/known_hosts")


@pytest_asyncio.fixture
async def pg_factory(postgres_schema) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


def worker(name: str) -> WorkerNode:
    return WorkerNode(
        name=name,
        status="ready",
        is_enabled=True,
        vpn_enabled=True,
        vpn_role="vpn_node",
        vpn_runtime_status="error",
        vpn_public_host=f"{name}.example",
        vpn_inbound_id=10,
        ssh_host="192.0.2.10",
        ssh_username="root",
        ssh_password="transport-secret",
        vpn_last_checked_at=NOW - timedelta(hours=1),
    )


def endpoint(node: WorkerNode, inbound_id: int) -> VpnEndpoint:
    return VpnEndpoint(
        worker_id=node.id,
        inbound_id=inbound_id,
        public_host=f"{node.name}-{inbound_id}.example",
        port=443,
        protocol="vless",
        transport="raw",
        security="reality",
        server_name="www.example.com",
        public_key=PUBLIC_KEY,
        short_id=f"{inbound_id:016x}",
        fingerprint="chrome",
        flow="xtls-rprx-vision",
        status="ready",
        verified_at=NOW - timedelta(minutes=5),
    )


async def seed_two(factory) -> tuple[int, int]:
    async with factory() as session:
        first_worker = worker("pg-first")
        second_worker = worker("pg-second")
        session.add_all([first_worker, second_worker])
        await session.flush()
        first_endpoint = endpoint(first_worker, 31)
        second_endpoint = endpoint(second_worker, 32)
        session.add_all([first_endpoint, second_endpoint])
        await session.commit()
        return first_endpoint.id, second_endpoint.id


@pytest.mark.asyncio
async def test_postgres_global_lease_loser_returns_false_then_next_endpoint_progresses(
    pg_factory,
) -> None:
    expected = list(await seed_two(pg_factory))
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[int] = []

    async def transport(_snapshot, request, *, now_ms):
        del now_ms
        calls.append(request.target.endpoint_id)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return VpnNodeHealthReceipt("healthy", None, "running")

    winner = asyncio.create_task(
        probe_next_vpn_endpoint(
            pg_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda: NOW,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        loser = await asyncio.wait_for(
            probe_next_vpn_endpoint(
                pg_factory,
                KNOWN_HOSTS,
                snapshot_loader=lambda *_: object(),
                transport=transport,
                now=lambda: NOW,
            ),
            timeout=2,
        )
        assert loser is False
        assert calls == [expected[0]]
        release.set()
        assert await asyncio.wait_for(winner, timeout=5) is True
        assert await probe_next_vpn_endpoint(
            pg_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda: NOW + timedelta(seconds=1),
        ) is True
        assert calls == expected
    finally:
        release.set()
        if not winner.done():
            winner.cancel()
        await asyncio.gather(winner, return_exceptions=True)


@pytest.mark.asyncio
async def test_postgres_cancelled_winner_releases_lease_and_retries_oldest(
    pg_factory,
) -> None:
    oldest, _ = await seed_two(pg_factory)
    started = asyncio.Event()

    async def blocked_transport(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    winner = asyncio.create_task(
        probe_next_vpn_endpoint(
            pg_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=blocked_transport,
            now=lambda: NOW,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    winner.cancel()
    with pytest.raises(asyncio.CancelledError):
        await winner

    calls: list[int] = []

    async def healthy(_snapshot, request, *, now_ms):
        del now_ms
        calls.append(request.target.endpoint_id)
        return VpnNodeHealthReceipt("healthy", None, "running")

    assert await asyncio.wait_for(
        probe_next_vpn_endpoint(
            pg_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=healthy,
            now=lambda: NOW + timedelta(seconds=1),
        ),
        timeout=5,
    ) is True
    assert calls == [oldest]
