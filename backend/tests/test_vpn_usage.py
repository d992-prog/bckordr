from __future__ import annotations

import asyncio
import importlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.services.vpn_node_usage import KeyUsage, UsageReceipt

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
KNOWN_HOSTS = Path("C:/synthetic/known_hosts")


@pytest.fixture
def usage():
    return importlib.import_module("app.services.vpn_usage")


def sample(second, epoch, record, reset, up, down):
    return SimpleNamespace(
        sampled_at=datetime.fromtimestamp(second, UTC),
        counter_epoch=epoch,
        record_id=record,
        reset_count=reset,
        upload_bytes=up,
        download_bytes=down,
    )


@pytest.mark.parametrize(
    "previous,current,expected",
    [
        (None, sample(60, "a", 1, 0, 100, 300), None),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 0, 150, 390), (50, 90)),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "b", 1, 0, 150, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 1, 2, 4), None),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 2, 0, 150, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 0, 99, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 0, 150, 299), None),
        (sample(60, "a", 1, 0, 100, 300), sample(60, "a", 1, 0, 150, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(59, "a", 1, 0, 150, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(181, "a", 1, 0, 150, 390), None),
        (sample(60, "a", 1, 0, 100, 300), sample(180, "a", 1, 0, 150, 390), (50, 90)),
        (sample(60, "a", 1, 0, 100, 300), sample(120, "a", 1, 0, 100, 300), (0, 0)),
    ],
)
def test_conservative_traffic_delta(usage, previous, current, expected):
    assert usage.traffic_delta(previous, current) == expected


@pytest.mark.parametrize("value", [True, -1, 2**63, 1.5, None])
def test_delta_rejects_invalid_counter_values(usage, value):
    with pytest.raises(ValueError, match="vpn_usage_counter_invalid"):
        usage.traffic_delta(
            sample(60, "a", 1, 0, 0, 0), sample(120, "a", 1, 0, value, 0)
        )


def test_usage_metadata_has_six_private_tables_and_scoped_open_incident_index():
    names = {
        "vpn_node_usage_polls",
        "vpn_key_usage_samples",
        "vpn_usage_hours",
        "vpn_key_source_samples",
        "vpn_device_abuse_state",
        "vpn_abuse_incidents",
    }
    assert names <= set(Base.metadata.tables)
    for name in names:
        table = Base.metadata.tables[name]
        assert not {"client_uuid", "client_email", "ip_address", "config_uri"} & set(
            table.c.keys()
        )
        assert all(
            foreign_key.ondelete == "RESTRICT" for foreign_key in table.foreign_keys
        )
    incident = Base.metadata.tables["vpn_abuse_incidents"]
    index = next(
        index
        for index in incident.indexes
        if index.name == "uq_vpn_abuse_incidents_open_device"
    )
    assert (
        index.unique
        and str(index.dialect_options["postgresql"]["where"])
        == str(index.dialect_options["sqlite"]["where"])
        == "acknowledged_at IS NULL"
    )


@pytest_asyncio.fixture
async def factory():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed(factory, *, unverified=False, count=2):
    async with factory() as db:
        worker = models.WorkerNode(
            name="usage-node",
            status="ready",
            vpn_enabled=True,
            vpn_role="vpn_node",
            ssh_host="192.0.2.1",
            ssh_username="root",
            ssh_password="private",
        )
        customer = models.VpnCustomer(
            telegram_user_id="usage-customer", status="active"
        )
        db.add_all([worker, customer])
        await db.flush()
        subscription = models.VpnSubscription(
            customer_id=customer.id,
            status="active",
            starts_at=NOW,
            expires_at=NOW + timedelta(days=30),
        )
        db.add(subscription)
        await db.flush()
        endpoint = models.VpnEndpoint(
            worker_id=worker.id,
            inbound_id=7,
            public_host="vpn.example.test",
            port=443,
            protocol="vless",
            transport="tcp",
            security="none",
            verified_at=NOW,
        )
        db.add(endpoint)
        await db.flush()
        keys = []
        for index in range(count):
            device = models.VpnDevice(
                subscription_id=subscription.id, display_name=f"Device {index}"
            )
            db.add(device)
            await db.flush()
            key = models.VpnAccessKey(
                subscription_id=subscription.id,
                device_id=device.id,
                worker_id=worker.id,
                endpoint_id=endpoint.id,
                status="active",
                protocol="vless",
                external_uuid=f"{index + 1:08x}-2222-4333-8444-555555555555",
                verified_client_email=None
                if unverified and index == 0
                else f"managed-{index}",
                panel_sub_id="verified",
            )
            db.add(key)
            await db.flush()
            keys.append(key.id)
        await db.commit()
        return worker.id, endpoint.id, keys


async def claim(usage, factory, now=NOW):
    return await usage.claim_usage_poll(
        factory, KNOWN_HOSTS, now=now, snapshot_loader=lambda *_: object()
    )


def receipt(claim, *, up=100, down=300, epoch="a" * 64, observed=None):
    return UsageReceipt(
        1,
        claim.request.poll_id,
        claim.request.minute_ms,
        claim.request.minute_ms + 10
        if observed is None
        else int(observed.timestamp() * 1000),
        epoch,
        tuple(
            KeyUsage(
                identity.access_key_id,
                "ok",
                identity.access_key_id + 90,
                0,
                up,
                down,
                "unsupported",
                (),
            )
            for identity in claim.request.identities
        ),
    )


async def poll(factory, worker_id):
    async with factory() as db:
        return await db.get(models.VpnNodeUsagePoll, worker_id)


@pytest.mark.asyncio
async def test_first_baseline_then_measured_zero_and_increment(usage, factory):
    worker_id, _, keys = await seed(factory)
    first = await claim(usage, factory)
    assert first is not None
    assert await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=1)
    )
    async with factory() as db:
        assert await db.scalar(select(models.VpnUsageHour)) is None
        assert (await db.get(models.VpnKeyUsageSample, keys[0])).upload_bytes == 100
    second = await claim(usage, factory, NOW + timedelta(minutes=1))
    assert await usage.apply_usage_poll(
        factory, second, receipt(second), now=NOW + timedelta(minutes=1, seconds=1)
    )
    third = await claim(usage, factory, NOW + timedelta(minutes=2))
    assert await usage.apply_usage_poll(
        factory,
        third,
        receipt(third, up=150, down=390),
        now=NOW + timedelta(minutes=2, seconds=1),
    )
    async with factory() as db:
        rows = (await db.scalars(select(models.VpnUsageHour))).all()
        assert len(rows) == 2
        assert all(
            (row.upload_bytes, row.download_bytes, row.sample_count) == (50, 90, 2)
            for row in rows
        )
        assert all(row.hour_start.replace(tzinfo=UTC) == NOW for row in rows)
        node = await db.get(models.WorkerNode, worker_id)
        assert (
            node.vpn_last_checked_at is None
            and node.vpn_runtime_status == "not_installed"
        )


@pytest.mark.asyncio
async def test_duplicate_expired_and_stale_claims_do_not_apply(usage, factory):
    worker_id, _, _ = await seed(factory)
    first = await claim(usage, factory)
    assert await claim(usage, factory) is None
    assert not await usage.apply_usage_poll(
        factory,
        replace(first, claim_token="wrong"),
        receipt(first),
        now=NOW + timedelta(seconds=1),
    )
    assert not await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=46)
    )
    next_claim = await claim(usage, factory, NOW + timedelta(minutes=1))
    assert await usage.apply_usage_poll(
        factory,
        next_claim,
        receipt(next_claim),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    assert not await usage.apply_usage_poll(
        factory,
        next_claim,
        receipt(next_claim),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    assert (await poll(factory, worker_id)).protocol_version == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["identity", "endpoint", "transport", "child"])
async def test_changed_binding_during_ssh_discards_batch(usage, factory, change):
    worker_id, endpoint_id, keys = await seed(factory)
    pending = await claim(usage, factory)
    async with factory() as db:
        if change == "identity":
            (
                await db.get(models.VpnAccessKey, keys[0])
            ).verified_client_email = "changed"
        elif change == "endpoint":
            (await db.get(models.VpnEndpoint, endpoint_id)).port = 8443
        elif change == "transport":
            (await db.get(models.WorkerNode, worker_id)).ssh_host = "192.0.2.99"
        else:
            (await db.get(models.VpnAccessKey, keys[0])).worker_id = None
            (await db.get(models.VpnAccessKey, keys[0])).endpoint_id = None
        await db.commit()
    assert not await usage.apply_usage_poll(
        factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
    )
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
    assert (
        await poll(factory, worker_id)
    ).last_error_code == "vpn_usage_binding_changed"


@pytest.mark.asyncio
async def test_failure_preserves_baseline_and_totals_with_safe_error(usage, factory):
    worker_id, _, keys = await seed(factory)
    first = await claim(usage, factory)
    await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=1)
    )
    pending = await claim(usage, factory, NOW + timedelta(minutes=1))
    assert await usage.apply_usage_poll(
        factory,
        pending,
        None,
        error_code="private@example.test",
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    async with factory() as db:
        assert (await db.get(models.VpnKeyUsageSample, keys[0])).upload_bytes == 100
        assert await db.scalar(select(models.VpnUsageHour)) is None
    state = await poll(factory, worker_id)
    assert (
        state.traffic_state == "unavailable"
        and state.last_error_code == "vpn_usage_unavailable"
    )


@pytest.mark.asyncio
async def test_unverified_child_is_partial_coverage_without_guessed_email(
    usage, factory
):
    worker_id, _, keys = await seed(factory, unverified=True)
    pending = await claim(usage, factory)
    assert [identity.access_key_id for identity in pending.request.identities] == [
        keys[1]
    ]
    await usage.apply_usage_poll(
        factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
    )
    assert (await poll(factory, worker_id)).traffic_state == "partial"
    async with factory() as db:
        assert await db.get(models.VpnKeyUsageSample, keys[0]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["epoch", "record", "reset", "up", "down", "gap", "missing"]
)
async def test_resets_gaps_and_missing_samples_never_create_historical_bytes(
    usage, factory, change
):
    worker_id, _, keys = await seed(factory)
    first = await claim(usage, factory)
    await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=1)
    )
    later = NOW + timedelta(minutes=3 if change == "gap" else 1)
    pending = await claim(usage, factory, later)
    result = receipt(pending)
    key_changes = {
        "epoch": {},
        "record": {"record_id": 999},
        "reset": {"reset_count": 1},
        "up": {"upload_bytes": 99},
        "down": {"download_bytes": 299},
        "gap": {},
        "missing": {
            "traffic_state": "unavailable",
            "record_id": None,
            "reset_count": None,
            "upload_bytes": None,
            "download_bytes": None,
        },
    }[change]
    result = replace(
        result,
        counter_epoch="b" * 64 if change == "epoch" else result.counter_epoch,
        keys=tuple(replace(key, **key_changes) for key in result.keys),
    )
    assert await usage.apply_usage_poll(
        factory, pending, result, now=later + timedelta(seconds=1)
    )
    async with factory() as db:
        assert await db.scalar(select(models.VpnUsageHour)) is None
        baseline = await db.get(models.VpnKeyUsageSample, keys[0])
        assert baseline.sampled_at.replace(tzinfo=UTC) == (
            NOW if change == "missing" else later
        ) + timedelta(milliseconds=10)
    assert (await poll(factory, worker_id)).traffic_state == (
        "unavailable" if change == "missing" else "partial"
    )


@pytest.mark.asyncio
async def test_out_of_order_receipt_does_not_move_baseline_backwards(usage, factory):
    _, _, keys = await seed(factory)
    first = await claim(usage, factory)
    await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=1)
    )
    async with factory() as db:
        (await db.get(models.VpnKeyUsageSample, keys[0])).sampled_at = NOW + timedelta(
            minutes=2
        )
        await db.commit()
    pending = await claim(usage, factory, NOW + timedelta(minutes=1))
    await usage.apply_usage_poll(
        factory,
        pending,
        receipt(pending, up=500),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    async with factory() as db:
        baseline = await db.get(models.VpnKeyUsageSample, keys[0])
        assert baseline.sampled_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=2)
        assert baseline.upload_bytes == 100


@pytest.mark.asyncio
async def test_aggregate_overflow_retains_measured_totals_and_marks_gap(usage, factory):
    worker_id, _, keys = await seed(factory)
    first = await claim(usage, factory)
    await usage.apply_usage_poll(
        factory, first, receipt(first), now=NOW + timedelta(seconds=1)
    )
    async with factory() as db:
        db.add(
            models.VpnUsageHour(
                access_key_id=keys[0],
                worker_id=worker_id,
                hour_start=NOW,
                upload_bytes=2**63 - 1,
                download_bytes=1,
                sample_count=1,
                first_sample_at=NOW - timedelta(seconds=60),
                last_sample_at=NOW,
            )
        )
        await db.commit()
    pending = await claim(usage, factory, NOW + timedelta(minutes=1))
    await usage.apply_usage_poll(
        factory,
        pending,
        receipt(pending, up=101),
        now=NOW + timedelta(minutes=1, seconds=1),
    )
    async with factory() as db:
        aggregate = await db.get(models.VpnUsageHour, (keys[0], worker_id, NOW))
        assert aggregate.upload_bytes == 2**63 - 1 and aggregate.sample_count == 1
        assert (await db.get(models.VpnKeyUsageSample, keys[0])).upload_bytes == 101
    assert (
        await poll(factory, worker_id)
    ).last_error_code == "vpn_usage_counter_overflow"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["maintenance", "control", "disabled", "archived"])
async def test_busy_or_ineligible_workers_are_not_claimed(usage, factory, kind):
    worker_id, endpoint_id, keys = await seed(factory)
    async with factory() as db:
        if kind == "maintenance":
            db.add(
                models.WorkerMaintenanceJob(
                    worker_id=worker_id, action="vpn_check", status="running"
                )
            )
        elif kind == "control":
            db.add(
                models.VpnControlOperation(
                    id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
                    access_key_id=keys[0],
                    worker_id=worker_id,
                    endpoint_id=endpoint_id,
                    generation=1,
                    action="provision",
                    request_snapshot={},
                    request_digest="a" * 64,
                    state="queued",
                )
            )
        elif kind == "disabled":
            (await db.get(models.WorkerNode, worker_id)).vpn_enabled = False
        else:
            (await db.get(models.WorkerNode, worker_id)).archived_at = NOW
        await db.commit()
    assert await claim(usage, factory) is None


@pytest.mark.asyncio
async def test_all_children_above_admin_list_limit_are_claimed(usage, factory):
    _, _, keys = await seed(factory, count=1001)
    pending = await claim(usage, factory)
    assert len(pending.request.identities) == 1001
    assert {key.access_key_id for key in pending.request.identities} == set(keys)


@pytest.mark.asyncio
async def test_batch_overflow_is_explicit_unavailable_and_never_transported(
    usage, factory
):
    worker_id, endpoint_id, keys = await seed(factory)
    async with factory() as db:
        existing = await db.get(models.VpnAccessKey, keys[0])
        db.add_all(
            models.VpnAccessKey(
                subscription_id=existing.subscription_id,
                worker_id=worker_id,
                endpoint_id=endpoint_id,
                status="suspended",
                protocol="vless",
                external_uuid=f"{index:08x}-2222-4333-8444-555555555555",
                verified_client_email=f"managed-{index}",
            )
            for index in range(3, 2050)
        )
        await db.commit()
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append(1)
        raise AssertionError("overflow must not reach SSH")

    assert not await usage.collect_usage_poll(
        factory,
        KNOWN_HOSTS,
        now=lambda: NOW,
        snapshot_loader=lambda *_: object(),
        transport=forbidden,
    )
    state = await poll(factory, worker_id)
    assert (
        state.last_error_code == "vpn_usage_overflow"
        and state.traffic_state == "unavailable"
    )
    assert state.protocol_version is None and state.claim_token is None and calls == []


@pytest.mark.asyncio
async def test_unknown_receipt_and_raw_sources_never_persist_credentials(
    usage, factory
):
    from sqlalchemy import event

    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    parameters = []
    engine = factory.kw["bind"].sync_engine

    def capture(connection, cursor, statement, values, context, many):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE")):
            parameters.append(repr(values))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        result = receipt(pending)
        result = replace(
            result,
            keys=tuple(
                replace(key, source_state="ok", sources=("203.0.113.77",))
                for key in result.keys
            ),
        )
        assert await usage.apply_usage_poll(
            factory, pending, result, now=NOW + timedelta(seconds=1)
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    persisted = "".join(parameters)
    assert "203.0.113.77" not in persisted and "managed-" not in persisted
    assert "4333-8444" not in persisted and "private" not in persisted
    assert (await poll(factory, worker_id)).source_state == "unavailable"


@pytest.mark.asyncio
async def test_invalid_receipt_keeps_protocol_unknown_without_baseline(usage, factory):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        replace(receipt(pending), keys=()),
        now=NOW + timedelta(seconds=1),
    )
    state = await poll(factory, worker_id)
    assert (
        state.protocol_version is None
        and state.last_error_code == "vpn_usage_receipt_invalid"
    )
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None


@pytest.mark.asyncio
async def test_apply_prefetches_baselines_and_hours_in_bounded_queries(usage, factory):
    from sqlalchemy import event

    await seed(factory, count=50)
    pending = await claim(usage, factory)
    engine = factory.kw["bind"].sync_engine
    reads = []

    def capture(connection, cursor, statement, values, context, many):
        if statement.lstrip().upper().startswith("SELECT") and (
            "FROM vpn_key_usage_samples" in statement
            or "FROM vpn_usage_hours" in statement
        ):
            reads.append(statement)

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await usage.apply_usage_poll(
            factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert len(reads) <= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [models.WorkerNode, models.VpnNodeUsagePoll])
async def test_apply_cannot_wait_past_lease_with_a_prelock_timestamp(
    usage, factory, locked
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    attempted, release = asyncio.Event(), asyncio.Event()
    clock = SimpleNamespace(now=NOW + timedelta(seconds=20))
    lock_order = []

    class ContendedSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            lock = statement._for_update_arg
            if lock is not None:
                entity = statement.column_descriptions[0]["entity"]
                lock_order.append(entity)
                if entity is locked:
                    attempted.set()
                    if lock.skip_locked:
                        return None
                    await release.wait()
            return await super().scalar(statement, **kwargs)

    contended_factory = async_sessionmaker(
        factory.kw["bind"], class_=ContendedSession, expire_on_commit=False
    )
    task = asyncio.create_task(
        usage.apply_usage_poll(
            contended_factory, pending, receipt(pending), now=clock.now
        )
    )
    await asyncio.wait_for(attempted.wait(), 1)
    clock.now = NOW + timedelta(seconds=50)
    assert clock.now > pending.claimed_at + timedelta(seconds=usage.USAGE_LEASE_SECONDS)
    release.set()
    assert not await task
    assert lock_order == (
        [models.WorkerNode]
        if locked is models.WorkerNode
        else [models.WorkerNode, models.VpnNodeUsagePoll]
    )
    assert (await poll(factory, worker_id)).claim_token == pending.claim_token
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_point", ["connection", "counter_read"])
async def test_apply_refreshes_lease_after_connection_and_read_waits(
    usage, factory, monkeypatch, wait_point
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(usage, "monotonic", lambda: clock.elapsed, raising=False)

    class DelayedSession(AsyncSession):
        async def __aenter__(self):
            session = await super().__aenter__()
            if wait_point == "connection":
                clock.elapsed = 30.0
            return session

        async def scalars(self, statement, **kwargs):
            result = await super().scalars(statement, **kwargs)
            if (
                wait_point == "counter_read"
                and statement.column_descriptions[0]["entity"] is models.VpnUsageHour
            ):
                clock.elapsed = 30.0
            return result

    delayed_factory = async_sessionmaker(
        factory.kw["bind"], class_=DelayedSession, expire_on_commit=False
    )
    assert not await usage.apply_usage_poll(
        delayed_factory, pending, receipt(pending), now=NOW + timedelta(seconds=20)
    )
    assert (await poll(factory, worker_id)).claim_token == pending.claim_token
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None


@pytest.mark.asyncio
async def test_receipt_freshness_expires_during_connection_wait_before_lease(
    usage, factory, monkeypatch
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(usage, "monotonic", lambda: clock.elapsed, raising=False)

    class DelayedSession(AsyncSession):
        async def __aenter__(self):
            session = await super().__aenter__()
            clock.elapsed = 15.0
            return session

    delayed_factory = async_sessionmaker(
        factory.kw["bind"], class_=DelayedSession, expire_on_commit=False
    )
    assert await usage.apply_usage_poll(
        delayed_factory, pending, receipt(pending), now=NOW + timedelta(seconds=20)
    )
    state = await poll(factory, worker_id)
    assert (
        state.traffic_state == "unavailable"
        and state.last_error_code == "vpn_usage_receipt_invalid"
    )
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None


@pytest.mark.asyncio
async def test_apply_locks_all_bound_key_references_before_worker_and_poll(
    usage, factory
):
    await seed(factory, unverified=True)
    pending = await claim(usage, factory)
    locked = []

    class RecordingSession(AsyncSession):
        def record(self, statement):
            lock = statement._for_update_arg
            if lock is not None:
                locked.append(
                    (
                        statement.column_descriptions[0]["entity"],
                        lock.read,
                        lock.key_share,
                        lock.skip_locked,
                    )
                )

        async def scalar(self, statement, **kwargs):
            self.record(statement)
            return await super().scalar(statement, **kwargs)

        async def scalars(self, statement, **kwargs):
            self.record(statement)
            result = await super().scalars(statement, **kwargs)
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"] is models.VpnAccessKey
            ):
                key_ids = [binding[0] for binding in pending.bindings]
                assert key_ids in statement.compile().params.values()
                assert len(key_ids) == 2 and len(pending.request.identities) == 1
            return result

    recording_factory = async_sessionmaker(
        factory.kw["bind"], class_=RecordingSession, expire_on_commit=False
    )
    assert await usage.apply_usage_poll(
        recording_factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
    )
    assert locked == [
        (models.VpnAccessKey, True, True, True),
        (models.WorkerNode, False, False, True),
        (models.VpnNodeUsagePoll, False, False, True),
        (models.VpnKeySourceSample, False, False, False),
    ]


@pytest.mark.asyncio
async def test_collect_commits_claim_before_network_and_only_apply_uses_new_session(
    usage, factory
):
    worker_id, _, _ = await seed(factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(snapshot, request, **kwargs):
        async with factory() as db:
            assert (
                await db.get(models.VpnNodeUsagePoll, worker_id)
            ).claim_token == request.poll_id
        entered.set()
        await release.wait()
        return UsageReceipt(
            1,
            request.poll_id,
            request.minute_ms,
            request.minute_ms + 10,
            "a" * 64,
            tuple(
                KeyUsage(
                    i.access_key_id,
                    "ok",
                    i.access_key_id + 90,
                    0,
                    1,
                    2,
                    "unsupported",
                    (),
                )
                for i in request.identities
            ),
        )

    task = asyncio.create_task(
        usage.collect_usage_poll(
            factory,
            KNOWN_HOSTS,
            now=lambda: NOW,
            snapshot_loader=lambda *_: object(),
            transport=transport,
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        async with factory() as db:
            node = await db.get(models.WorkerNode, worker_id)
            node.notes = "issuance can progress"
            await asyncio.wait_for(db.commit(), 1)
    finally:
        release.set()
    assert await task


@pytest.mark.asyncio
async def test_cleanup_expires_only_old_hours_and_sources_and_preserves_baseline(
    usage, factory
):
    worker_id, _, keys = await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(factory, pending, receipt(pending), now=NOW)
    async with factory() as db:
        for offset in (timedelta(days=90, seconds=1), timedelta(days=90)):
            hour = NOW - offset
            db.add(
                models.VpnUsageHour(
                    access_key_id=keys[0],
                    worker_id=worker_id,
                    hour_start=hour,
                    upload_bytes=3,
                    download_bytes=7,
                    sample_count=1,
                    first_sample_at=hour,
                    last_sample_at=hour + timedelta(seconds=1),
                )
            )
        for key_id, age in zip(keys, (timedelta(days=1, seconds=1), timedelta(days=1))):
            observed = NOW - age
            await db.merge(
                models.VpnKeySourceSample(
                    access_key_id=key_id,
                    worker_id=worker_id,
                    minute_at=observed,
                    observed_at=observed,
                    state="ok",
                    key_epoch="synthetic",
                    source_hashes=[],
                )
            )
        worker = await db.get(models.WorkerNode, worker_id)
        worker.status = "offline"
        await db.commit()
    assert await usage.cleanup_usage_retention(factory, now=NOW) == 2
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnUsageHour)) == 1
        )
        assert (
            await db.scalars(select(models.VpnKeySourceSample.access_key_id))
        ).all() == [keys[1]]
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnKeyUsageSample))
            == 2
        )


@pytest.mark.asyncio
async def test_cleanup_has_one_shared_1000_row_budget_and_prioritizes_sources(
    usage, factory
):
    worker_id, _, keys = await seed(factory, count=1001)
    expired = NOW - timedelta(days=91)
    async with factory() as db:
        db.add_all(
            models.VpnKeySourceSample(
                access_key_id=key_id,
                worker_id=worker_id,
                minute_at=expired,
                observed_at=expired,
                state="unsupported",
                key_epoch=None,
                source_hashes=None,
            )
            for key_id in keys
        )
        db.add(
            models.VpnUsageHour(
                access_key_id=keys[0],
                worker_id=worker_id,
                hour_start=expired,
                upload_bytes=0,
                download_bytes=0,
                sample_count=1,
                first_sample_at=expired,
                last_sample_at=expired + timedelta(seconds=1),
            )
        )
        await db.commit()
    assert await usage.cleanup_usage_retention(factory, now=NOW) == 1000
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnKeySourceSample))
            == 1
        )
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnUsageHour)) == 1
        )
    assert await usage.cleanup_usage_retention(factory, now=NOW) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("late_reason", ["deadline", "cancelled"])
async def test_collect_never_applies_late_transport_even_if_cancellation_is_swallowed(
    usage,
    factory,
    monkeypatch,
    late_reason,
):
    worker_id, _, _ = await seed(factory)
    clock = [100.0]
    monkeypatch.setattr(usage, "monotonic", lambda: clock[0])
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(snapshot, request, **kwargs):
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            pass
        if late_reason == "deadline":
            clock[0] += 36
        return receipt(SimpleNamespace(request=request))

    task = asyncio.create_task(
        usage.collect_usage_poll(
            factory,
            KNOWN_HOSTS,
            now=lambda: NOW,
            deadline=135.0,
            snapshot_loader=lambda *_: object(),
            transport=transport,
        )
    )
    await asyncio.wait_for(entered.wait(), 2)
    if late_reason == "cancelled":
        task.cancel()
    else:
        release.set()
    assert not await asyncio.wait_for(task, 2)
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None
        assert (
            await db.get(models.VpnNodeUsagePoll, worker_id)
        ).claim_token is not None


@pytest.mark.asyncio
async def test_operation_deadline_is_rechecked_after_apply_connection_wait(
    usage,
    factory,
    monkeypatch,
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    clock = [100.0]
    monkeypatch.setattr(usage, "monotonic", lambda: clock[0])

    class DelayedSession(AsyncSession):
        async def __aenter__(self):
            session = await super().__aenter__()
            clock[0] = 106.0
            return session

    delayed_factory = async_sessionmaker(
        factory.kw["bind"],
        class_=DelayedSession,
        expire_on_commit=False,
    )
    assert not await usage.apply_usage_poll(
        delayed_factory,
        pending,
        receipt(pending),
        now=NOW,
        deadline=105.0,
    )
    assert (await poll(factory, worker_id)).claim_token == pending.claim_token
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
