from __future__ import annotations

import asyncio
import base64
import gc
import weakref
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
import pytest_asyncio
from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnNodeEvent,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
)
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    public_endpoint_fingerprint,
)
from app.services.vpn_fleet_health import (
    _LOCAL_PROBE_LOCKS,
    _local_probe_lock,
    probe_next_vpn_endpoint,
)
from app.services.vpn_node_health import VpnNodeHealthReceipt, VpnNodeHealthRequest
from app.services.vpn_node_transport import VpnNodeTransportError
from app.services.vpn_policy import VPN_MUTATION_ACTIONS
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")
KNOWN_HOSTS = Path("C:/veltrix/known_hosts")


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class RecordingSession(AsyncSession):
    lock_entities: ClassVar[list[type]] = []
    on_worker_lock = None

    async def scalar(self, statement, **kwargs):
        if statement._for_update_arg is not None:
            entity = statement.column_descriptions[0]["entity"]
            self.lock_entities.append(entity)
            if entity is WorkerNode and type(self).on_worker_lock is not None:
                hook, type(self).on_worker_lock = type(self).on_worker_lock, None
                await hook(self)
        return await super().scalar(statement, **kwargs)


@pytest_asyncio.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
        class_=RecordingSession,
    )
    RecordingSession.lock_entities = []
    RecordingSession.on_worker_lock = None
    try:
        yield factory
    finally:
        await engine.dispose()


def worker(name: str, **changes: object) -> WorkerNode:
    values = {
        "name": name,
        "status": "ready",
        "is_enabled": True,
        "vpn_enabled": True,
        "vpn_role": "vpn_node",
        "vpn_runtime_status": "error",
        "vpn_public_host": f"{name}.example",
        "vpn_inbound_id": 10,
        "ssh_host": "192.0.2.10",
        "ssh_username": "root",
        "ssh_password": "transport-secret",
        "vpn_last_checked_at": NOW - timedelta(hours=1),
        "vpn_last_error": "old-safe-code",
    }
    values.update(changes)
    return WorkerNode(**values)


def endpoint(node: WorkerNode, *, inbound_id: int, **changes: object) -> VpnEndpoint:
    values = {
        "worker_id": node.id,
        "inbound_id": inbound_id,
        "public_host": f"{node.name}-{inbound_id}.example",
        "port": 443,
        "protocol": "vless",
        "transport": "raw",
        "security": "reality",
        "server_name": "www.example.com",
        "public_key": PUBLIC_KEY,
        "short_id": f"{inbound_id:016x}",
        "fingerprint": "chrome",
        "flow": "xtls-rprx-vision",
        "status": "ready",
        "verified_at": NOW - timedelta(minutes=5),
    }
    values.update(changes)
    return VpnEndpoint(**values)


async def seed_endpoint(
    factory: async_sessionmaker[AsyncSession],
    *,
    name: str = "node",
    worker_changes: dict[str, object] | None = None,
    endpoint_changes: dict[str, object] | None = None,
    inbound_id: int = 10,
) -> tuple[int, int]:
    async with factory() as session:
        node = worker(name, **(worker_changes or {}))
        session.add(node)
        await session.flush()
        target = endpoint(node, inbound_id=inbound_id, **(endpoint_changes or {}))
        session.add(target)
        await session.commit()
        return node.id, target.id


def healthy_transport(calls: list[VpnNodeHealthRequest]):
    async def execute(_snapshot, request, *, now_ms):
        calls.append(request)
        assert isinstance(request, VpnNodeHealthRequest)
        assert request.checked_at_ms == now_ms == int(NOW.timestamp() * 1000)
        assert request.worker_id == request.target.worker_id
        return VpnNodeHealthReceipt("healthy", None, "running")

    return execute


@pytest.mark.asyncio
async def test_probe_orders_endpoint_attempts_nulls_first_then_worker_and_endpoint(
    session_factory,
) -> None:
    async with session_factory() as session:
        first_worker = worker("first", vpn_last_checked_at=None)
        second_worker = worker("second", vpn_last_checked_at=NOW - timedelta(days=2))
        session.add_all([first_worker, second_worker])
        await session.flush()
        oldest_non_null = endpoint(
            first_worker,
            inbound_id=11,
            health_checked_at=NOW - timedelta(days=3),
        )
        first_null = endpoint(first_worker, inbound_id=12, health_checked_at=None)
        second_null = endpoint(second_worker, inbound_id=13, health_checked_at=None)
        session.add_all([oldest_non_null, first_null, second_null])
        await session.commit()
        expected = [first_null.id, second_null.id, oldest_non_null.id]

    calls: list[VpnNodeHealthRequest] = []
    snapshots: list[tuple[int, Path]] = []

    def snapshot_loader(node: WorkerNode, path: Path) -> object:
        snapshots.append((node.id, path))
        return object()

    for endpoint_id in expected:
        assert await probe_next_vpn_endpoint(
            session_factory,
            KNOWN_HOSTS,
            snapshot_loader=snapshot_loader,
            transport=healthy_transport(calls),
            now=lambda: NOW,
        ) is True
        assert calls[-1].target.endpoint_id == endpoint_id

    assert [request.target.endpoint_id for request in calls] == expected
    assert snapshots == [
        (calls[0].worker_id, KNOWN_HOSTS),
        (calls[1].worker_id, KNOWN_HOSTS),
        (calls[2].worker_id, KNOWN_HOSTS),
    ]
    assert RecordingSession.lock_entities == [
        WorkerNode,
        VpnEndpoint,
        WorkerNode,
        VpnEndpoint,
        WorkerNode,
        VpnEndpoint,
    ]


@pytest.mark.asyncio
async def test_probe_rechecks_candidate_attempt_timestamp_after_worker_lock(
    session_factory,
) -> None:
    _, endpoint_id = await seed_endpoint(session_factory)
    calls: list[VpnNodeHealthRequest] = []

    async def mark_attempted(session: AsyncSession) -> None:
        await session.execute(
            update(VpnEndpoint)
            .where(VpnEndpoint.id == endpoint_id)
            .values(health_checked_at=NOW - timedelta(seconds=1))
            .execution_options(synchronize_session=False)
        )

    RecordingSession.on_worker_lock = mark_attempted

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=healthy_transport(calls),
        now=lambda: NOW,
    ) is False
    assert calls == []
    assert RecordingSession.lock_entities == [WorkerNode, VpnEndpoint]


@pytest.mark.asyncio
async def test_sqlite_process_lease_prevents_overlapping_or_duplicate_probes(
    session_factory,
) -> None:
    async with session_factory() as session:
        first_worker = worker("lease-first")
        second_worker = worker("lease-second")
        session.add_all([first_worker, second_worker])
        await session.flush()
        first_endpoint = endpoint(first_worker, inbound_id=21)
        second_endpoint = endpoint(second_worker, inbound_id=22)
        session.add_all([first_endpoint, second_endpoint])
        await session.commit()
        expected = [first_endpoint.id, second_endpoint.id]

    first_started = asyncio.Event()
    duplicate_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[int] = []

    async def transport(_snapshot, request, *, now_ms):
        del now_ms
        calls.append(request.target.endpoint_id)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        else:
            duplicate_started.set()
        return VpnNodeHealthReceipt("healthy", None, "running")

    first = asyncio.create_task(
        probe_next_vpn_endpoint(
            session_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda: NOW,
        )
    )
    await asyncio.wait_for(first_started.wait(), timeout=1)
    second = asyncio.create_task(
        probe_next_vpn_endpoint(
            session_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda: NOW + timedelta(seconds=1),
        )
    )
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(duplicate_started.wait(), timeout=0.1)
        assert calls == [expected[0]]
        assert second.done() is False
        release_first.set()
        assert await asyncio.gather(first, second) == [True, True]
        assert calls == expected
    finally:
        release_first.set()
        for task in (first, second):
            if not task.done():
                task.cancel()
        await asyncio.gather(first, second, return_exceptions=True)


def test_local_probe_lock_registry_releases_closed_contended_event_loops() -> None:
    async def contend() -> None:
        lock = _local_probe_lock()
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        assert waiter.done() is False
        lock.release()
        await waiter
        lock.release()

    gc.collect()
    initial_registry_size = len(_LOCAL_PROBE_LOCKS)
    loop_refs = []
    for _ in range(3):
        loop = asyncio.new_event_loop()
        loop_refs.append(weakref.ref(loop))
        try:
            loop.run_until_complete(contend())
        finally:
            loop.close()
        del loop

    gc.collect()

    assert all(loop_ref() is None for loop_ref in loop_refs)
    assert len(_LOCAL_PROBE_LOCKS) == initial_registry_size


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_changes", "endpoint_changes"),
    [
        ({"archived_at": NOW}, {}),
        ({"is_enabled": False}, {}),
        ({"status": "offline"}, {}),
        ({"status": "disabled"}, {}),
        ({"vpn_enabled": False}, {}),
        ({"vpn_role": "none"}, {}),
        ({}, {"status": "disabled"}),
    ],
)
async def test_probe_skips_ineligible_workers_and_endpoints(
    session_factory,
    worker_changes,
    endpoint_changes,
) -> None:
    await seed_endpoint(
        session_factory,
        worker_changes=worker_changes,
        endpoint_changes=endpoint_changes,
    )
    calls: list[VpnNodeHealthRequest] = []

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=healthy_transport(calls),
        now=lambda: NOW,
    ) is False
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["staged", "ready", "draining"])
async def test_probe_accepts_every_non_disabled_endpoint_status(
    session_factory,
    status: str,
) -> None:
    await seed_endpoint(
        session_factory,
        endpoint_changes={"status": status, "verified_at": None if status == "staged" else NOW},
    )
    calls: list[VpnNodeHealthRequest] = []

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=healthy_transport(calls),
        now=lambda: NOW,
    ) is True
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [*sorted(VPN_MUTATION_ACTIONS), "vpn_check"])
@pytest.mark.parametrize("status", ["queued", "running"])
async def test_probe_skips_worker_with_busy_maintenance(
    session_factory,
    action: str,
    status: str,
) -> None:
    worker_id, _ = await seed_endpoint(session_factory)
    async with session_factory() as session:
        session.add(WorkerMaintenanceJob(worker_id=worker_id, action=action, status=status))
        await session.commit()

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=lambda *_args, **_kwargs: pytest.fail("transport must not run"),
        now=lambda: NOW,
    ) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["queued", "claimed", "uncertain"])
async def test_probe_skips_worker_with_active_control_operation(
    session_factory,
    state: str,
) -> None:
    worker_id, endpoint_id = await seed_endpoint(session_factory)
    async with session_factory() as session:
        session.add(
            VpnControlOperation(
                id=f"40000000-0000-4000-8000-{len(state):012d}",
                access_key_id=1,
                worker_id=worker_id,
                endpoint_id=endpoint_id,
                generation=1,
                action="provision",
                request_snapshot={},
                request_digest="a" * 64,
                state=state,
                claim_token=(
                    f"50000000-0000-4000-8000-{len(state):012d}"
                    if state in {"claimed", "uncertain"}
                    else None
                ),
            )
        )
        await session.commit()

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=lambda *_args, **_kwargs: pytest.fail("transport must not run"),
        now=lambda: NOW,
    ) is False


@pytest.mark.asyncio
async def test_transport_failure_records_safe_transition_then_next_endpoint_progresses(
    session_factory,
) -> None:
    async with session_factory() as session:
        first = worker("first")
        second = worker("second")
        session.add_all([first, second])
        await session.flush()
        first_endpoint = endpoint(first, inbound_id=11)
        second_endpoint = endpoint(second, inbound_id=12)
        session.add_all([first_endpoint, second_endpoint])
        await session.commit()
        first_id, second_id = first_endpoint.id, second_endpoint.id

    calls: list[int] = []

    async def failing(_snapshot, request, *, now_ms):
        del now_ms
        calls.append(request.target.endpoint_id)
        raise VpnNodeTransportError("preflight")

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=failing,
        now=lambda: NOW,
    ) is True
    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=failing,
        now=lambda: NOW + timedelta(seconds=1),
    ) is True
    assert calls == [first_id, second_id]

    async with session_factory() as session:
        first_row = await session.get(VpnEndpoint, first_id)
        first_worker = await session.get(WorkerNode, first_row.worker_id)
        assert as_utc(first_row.health_checked_at) == NOW
        assert first_row.last_error_code == "vpn_node_health_transport_failed"
        assert first_worker.vpn_runtime_status == "error"
        assert as_utc(first_worker.vpn_last_checked_at) == NOW
        assert first_worker.vpn_last_error == "vpn_node_health_transport_failed"


@pytest.mark.asyncio
async def test_success_updates_only_health_and_preserves_matching_external_proof(
    session_factory,
) -> None:
    worker_id, endpoint_id = await seed_endpoint(session_factory)
    async with session_factory() as session:
        node = await session.get(WorkerNode, worker_id)
        target = await session.get(VpnEndpoint, endpoint_id)
        target.last_error_code = "vpn_node_health_internal"
        target.external_verified_at = NOW - timedelta(days=1)
        current_target = VpnEndpointTarget(
            endpoint_id=target.id,
            worker_id=target.worker_id,
            inbound_id=target.inbound_id,
            public_host=target.public_host,
            port=target.port,
            protocol=target.protocol,
            transport=target.transport,
            security=target.security,
            server_name=target.server_name,
            public_key=target.public_key,
            short_id=target.short_id,
            fingerprint=target.fingerprint,
            flow=target.flow,
        )
        target.external_config_fingerprint = public_endpoint_fingerprint(current_target)
        customer = VpnCustomer(status="active")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=node.id,
            endpoint_id=target.id,
            external_uuid="11111111-1111-4111-8111-111111111111",
            config_uri="vless://client-secret",
            status="active",
        )
        session.add(access_key)
        await session.commit()
        proof_at = target.external_verified_at
        identity = (target.public_host, target.port, target.public_key, target.status)
        access_key_id = access_key.id

    calls: list[VpnNodeHealthRequest] = []
    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=healthy_transport(calls),
        now=lambda: NOW,
    ) is True

    async with session_factory() as session:
        node = await session.get(WorkerNode, worker_id)
        target = await session.get(VpnEndpoint, endpoint_id)
        access_key = await session.get(VpnAccessKey, access_key_id)
        assert (node.vpn_runtime_status, as_utc(node.vpn_last_checked_at), node.vpn_last_error) == (
            "ready",
            NOW,
            None,
        )
        assert (
            as_utc(target.verified_at),
            as_utc(target.health_checked_at),
            target.last_error_code,
        ) == (
            NOW,
            NOW,
            None,
        )
        assert as_utc(target.external_verified_at) == as_utc(proof_at)
        assert target.external_config_fingerprint == public_endpoint_fingerprint(calls[0].target)
        assert (target.public_host, target.port, target.public_key, target.status) == identity
        assert access_key.external_uuid == "11111111-1111-4111-8111-111111111111"
        assert access_key.config_uri == "vless://client-secret"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("proof_at", "fingerprint"),
    [
        (NOW, "0" * 64),
        (NOW, None),
        (None, "current"),
    ],
)
async def test_probe_clears_mismatched_or_one_sided_external_proof(
    session_factory,
    proof_at,
    fingerprint,
) -> None:
    _, endpoint_id = await seed_endpoint(session_factory)
    async with session_factory() as session:
        target = await session.get(VpnEndpoint, endpoint_id)
        target.external_verified_at = proof_at
        if fingerprint == "current":
            fingerprint = public_endpoint_fingerprint(
                VpnEndpointTarget(
                    endpoint_id=target.id,
                    worker_id=target.worker_id,
                    inbound_id=target.inbound_id,
                    public_host=target.public_host,
                    port=target.port,
                    protocol=target.protocol,
                    transport=target.transport,
                    security=target.security,
                    server_name=target.server_name,
                    public_key=target.public_key,
                    short_id=target.short_id,
                    fingerprint=target.fingerprint,
                    flow=target.flow,
                )
            )
        target.external_config_fingerprint = fingerprint
        await session.commit()

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=lambda *_: object(),
        transport=healthy_transport([]),
        now=lambda: NOW,
    ) is True

    async with session_factory() as session:
        target = await session.get(VpnEndpoint, endpoint_id)
        assert target.external_verified_at is None
        assert target.external_config_fingerprint is None


@pytest.mark.asyncio
async def test_failure_preserves_last_success_identity_and_clients_and_dedupes_events(
    session_factory,
) -> None:
    worker_id, endpoint_id = await seed_endpoint(session_factory)
    async with session_factory() as session:
        target = await session.get(VpnEndpoint, endpoint_id)
        original_identity = (
            target.public_host,
            target.port,
            target.public_key,
            target.status,
        )
        proof_at = NOW - timedelta(days=1)
        proof_hash = public_endpoint_fingerprint(
            VpnEndpointTarget(
                endpoint_id=target.id,
                worker_id=target.worker_id,
                inbound_id=target.inbound_id,
                public_host=target.public_host,
                port=target.port,
                protocol=target.protocol,
                transport=target.transport,
                security=target.security,
                server_name=target.server_name,
                public_key=target.public_key,
                short_id=target.short_id,
                fingerprint=target.fingerprint,
                flow=target.flow,
            )
        )
        target.external_verified_at = proof_at
        target.external_config_fingerprint = proof_hash
        customer = VpnCustomer(status="active")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker_id,
            endpoint_id=endpoint_id,
            external_uuid="22222222-2222-4222-8222-222222222222",
            config_uri="vless://preserved-secret",
            status="active",
        )
        session.add(access_key)
        await session.commit()
        access_key_id = access_key.id

    outcomes = [
        VpnNodeHealthReceipt(
            "unhealthy",
            "vpn_node_health_endpoint_mismatch",
            "running",
        ),
        VpnNodeHealthReceipt(
            "unhealthy",
            "vpn_node_health_endpoint_mismatch",
            "running",
        ),
        VpnNodeHealthReceipt("healthy", None, "running"),
        VpnNodeHealthReceipt(
            "unhealthy",
            "vpn_node_health_endpoint_mismatch",
            "running",
        ),
    ]

    async def transport(_snapshot, _request, *, now_ms):
        del now_ms
        return outcomes.pop(0)

    for offset in range(4):
        assert await probe_next_vpn_endpoint(
            session_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda offset=offset: NOW + timedelta(seconds=offset),
        ) is True

    async with session_factory() as session:
        target = await session.get(VpnEndpoint, endpoint_id)
        access_key = await session.get(VpnAccessKey, access_key_id)
        events = list(
            await session.scalars(
                select(VpnNodeEvent).order_by(VpnNodeEvent.id)
            )
        )
        assert as_utc(target.verified_at) == NOW + timedelta(seconds=2)
        assert (target.public_host, target.port, target.public_key, target.status) == original_identity
        assert target.last_error_code == "vpn_node_health_endpoint_mismatch"
        assert as_utc(target.external_verified_at) == proof_at
        assert target.external_config_fingerprint == proof_hash
        assert access_key.external_uuid == "22222222-2222-4222-8222-222222222222"
        assert access_key.config_uri == "vless://preserved-secret"
        assert len(events) == 2
        assert all(event.level == "error" for event in events)
        assert len({event.event_type for event in events}) == 1
        assert len({event.message for event in events}) == 1
        assert all(
            event.details
            == {
                "endpoint_id": endpoint_id,
                "error_code": "vpn_node_health_endpoint_mismatch",
            }
            for event in events
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_code", "unsafe_text"),
    [
        (VpnNodeTransportError("preflight"), "vpn_node_health_transport_failed", None),
        (
            RuntimeError("uri://host/token/request secret"),
            "vpn_node_health_internal",
            "uri://host/token/request secret",
        ),
    ],
)
async def test_probe_never_persists_or_emits_exception_text(
    session_factory,
    failure: Exception,
    expected_code: str,
    unsafe_text: str | None,
) -> None:
    worker_id, endpoint_id = await seed_endpoint(session_factory)

    def snapshot_loader(*_args):
        raise failure

    assert await probe_next_vpn_endpoint(
        session_factory,
        KNOWN_HOSTS,
        snapshot_loader=snapshot_loader,
        transport=healthy_transport([]),
        now=lambda: NOW,
    ) is True

    async with session_factory() as session:
        node = await session.get(WorkerNode, worker_id)
        target = await session.get(VpnEndpoint, endpoint_id)
        event = await session.scalar(select(VpnNodeEvent))
        assert node.vpn_last_error == expected_code
        assert target.last_error_code == expected_code
        persisted = repr((node.vpn_last_error, target.last_error_code, event.message, event.details))
        if unsafe_text is not None:
            assert unsafe_text not in persisted
        assert set(event.details) == {"endpoint_id", "error_code"}


@pytest.mark.asyncio
async def test_probe_cancellation_rolls_back_and_releases_without_an_event(
    session_factory,
) -> None:
    worker_id, endpoint_id = await seed_endpoint(session_factory)
    started = asyncio.Event()

    async def transport(*_args, **_kwargs):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        probe_next_vpn_endpoint(
            session_factory,
            KNOWN_HOSTS,
            snapshot_loader=lambda *_: object(),
            transport=transport,
            now=lambda: NOW,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with session_factory() as session:
        node = await session.get(WorkerNode, worker_id)
        target = await session.get(VpnEndpoint, endpoint_id)
        assert as_utc(node.vpn_last_checked_at) == NOW - timedelta(hours=1)
        assert node.vpn_last_error == "old-safe-code"
        assert target.health_checked_at is None
        assert await session.scalar(select(VpnNodeEvent)) is None
