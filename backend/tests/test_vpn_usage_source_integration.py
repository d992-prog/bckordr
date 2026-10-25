from __future__ import annotations

import inspect
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from test_vpn_usage import (
    KNOWN_HOSTS,
    NOW,
    claim,
    poll,
    receipt,
    seed,
)
from test_vpn_usage import (
    factory as factory,  # noqa: PLC0414
)

from app.db import models
from app.services import vpn_usage as usage
from app.services import vpn_usage_sources
from app.services.vpn_usage_sources import source_hash

SECRET = "synthetic-persistent-source-key-32-characters"
RAW_IP = "203.0.113.77"


def source_receipt(pending, *, state="ok", sources=(RAW_IP,), **kwargs):
    result = receipt(pending, **kwargs)
    return replace(
        result,
        keys=tuple(
            replace(key, source_state=state, sources=sources) for key in result.keys
        ),
    )


async def source_rows(factory):
    async with factory() as db:
        return (
            await db.scalars(
                select(models.VpnKeySourceSample).order_by(
                    models.VpnKeySourceSample.access_key_id,
                )
            )
        ).all()


@pytest.mark.asyncio
async def test_apply_hashes_and_sanitizes_before_first_database_call_and_sql_is_private(
    factory,
    caplog,
):
    worker_id, _, keys = await seed(factory)
    pending = await claim(usage, factory)
    parameters = []
    engine = factory.kw["bind"].sync_engine

    def checked_factory():
        frame = inspect.currentframe().f_back
        cleaned = frame.f_locals["receipt"]
        assert all(not key.sources for key in cleaned.keys)
        return factory()

    def capture(connection, cursor, statement, values, context, many):
        parameters.append(repr(values))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        assert await usage.apply_usage_poll(
            checked_factory,
            pending,
            source_receipt(pending),
            now=NOW,
            source_secret=SECRET,
        )
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    samples = await source_rows(factory)
    assert [sample.access_key_id for sample in samples] == keys
    for sample, binding in zip(samples, pending.bindings):
        assert sample.state == "ok" and len(sample.key_epoch) == 16
        assert sample.source_hashes == [source_hash(SECRET, binding[3], RAW_IP)]
        assert sample.minute_at == NOW.replace(tzinfo=None)
    assert samples[0].source_hashes != samples[1].source_hashes
    assert (await poll(factory, worker_id)).source_state == "ok"
    persisted = "".join(parameters) + caplog.text
    assert RAW_IP not in persisted and SECRET not in persisted
    assert "managed-" not in persisted and "4333-8444" not in persisted
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,sources,expected",
    [
        ("ok", (), []),
        ("unsupported", (), None),
        ("unavailable", (), None),
    ],
)
async def test_sources_preserve_measured_empty_versus_unsupported_or_unknown(
    factory,
    state,
    sources,
    expected,
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending, state=state, sources=sources),
        now=NOW,
        source_secret=SECRET,
    )
    rows = await source_rows(factory)
    assert len(rows) == 2
    assert all(row.state == state and row.source_hashes == expected for row in rows)
    assert all((row.key_epoch is not None) == (state == "ok") for row in rows)
    assert (await poll(factory, worker_id)).source_state == state


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "secret", ["", "change-me-session-secret", "x" * 31, "\ud800" * 32]
)
async def test_invalid_source_secret_preserves_ordinary_measured_traffic(
    factory, secret
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(factory, pending, receipt(pending), now=NOW)
    later = NOW + timedelta(minutes=1)
    pending = await claim(usage, factory, now=later)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending, up=150, down=390),
        now=later,
        source_secret=secret,
    )
    async with factory() as db:
        hours = (await db.scalars(select(models.VpnUsageHour))).all()
        assert len(hours) == 2 and sum(row.upload_bytes for row in hours) == 100
    rows = await source_rows(factory)
    assert all(row.state == "unavailable" and row.source_hashes is None for row in rows)
    assert all(row.key_epoch is None for row in rows)
    state = await poll(factory, worker_id)
    assert state.traffic_state == "ok" and state.source_state == "unavailable"


@pytest.mark.asyncio
async def test_source_binding_is_device_scoped_and_rotation_replaces_epoch(factory):
    await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending),
        now=NOW,
        source_secret=SECRET,
    )
    before = await source_rows(factory)
    later = NOW + timedelta(minutes=1)
    pending = await claim(usage, factory, now=later)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending),
        now=later,
        source_secret=SECRET + "rotated",
    )
    after = await source_rows(factory)
    assert len(after) == len(before) == 2
    for old, new in zip(before, after):
        assert old.key_epoch != new.key_epoch
        assert old.source_hashes != new.source_hashes
        assert new.observed_at > old.observed_at


@pytest.mark.asyncio
async def test_unbound_device_or_unverified_child_is_unknown_not_complete(factory):
    worker_id, _, keys = await seed(factory, unverified=True)
    async with factory() as db:
        key = await db.get(models.VpnAccessKey, keys[1])
        key.device_id = None
        await db.commit()
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending),
        now=NOW,
        source_secret=SECRET,
    )
    rows = await source_rows(factory)
    assert len(rows) == 1 and rows[0].access_key_id == keys[1]
    assert rows[0].state == "unavailable" and rows[0].source_hashes is None
    assert (await poll(factory, worker_id)).source_state == "unavailable"


@pytest.mark.asyncio
async def test_stale_or_out_of_order_source_receipt_cannot_replace_newer_evidence(
    factory,
):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    result = source_receipt(pending, observed=NOW + timedelta(seconds=20))
    assert await usage.apply_usage_poll(
        factory,
        pending,
        result,
        now=NOW + timedelta(seconds=20),
        source_secret=SECRET,
    )
    before = await source_rows(factory)
    later = NOW + timedelta(minutes=1)
    pending = await claim(usage, factory, now=later)
    # The latest row may already contain newer same-window evidence from another claimant.
    async with factory() as db:
        for row in await db.scalars(select(models.VpnKeySourceSample)):
            row.observed_at = later + timedelta(seconds=20)
        await db.commit()
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending, sources=("192.0.2.19",)),
        now=later,
        source_secret=SECRET,
    )
    after = await source_rows(factory)
    assert [row.source_hashes for row in after] == [row.source_hashes for row in before]
    assert all(
        row.observed_at == (later + timedelta(seconds=20)).replace(tzinfo=None)
        for row in after
    )
    assert (await poll(factory, worker_id)).source_state == "unavailable"


@pytest.mark.asyncio
async def test_collect_uses_persistent_secret_without_passing_it_to_node(
    factory, monkeypatch
):
    worker_id, _, _ = await seed(factory)
    monkeypatch.setattr(
        usage,
        "get_settings",
        lambda: SimpleNamespace(session_secret_key=SECRET),
        raising=False,
    )

    async def transport(snapshot, request, **kwargs):
        assert SECRET not in repr((snapshot, request, kwargs))
        assert set(kwargs) == {"now_ms"}
        return source_receipt(SimpleNamespace(request=request))

    assert await usage.collect_usage_poll(
        factory,
        KNOWN_HOSTS,
        now=lambda: NOW,
        snapshot_loader=lambda *_: object(),
        transport=transport,
    )
    assert (await poll(factory, worker_id)).source_state == "ok"
    assert all(row.source_hashes for row in await source_rows(factory))


@pytest.mark.asyncio
async def test_source_hash_failure_does_not_discard_valid_traffic(factory, monkeypatch):
    worker_id, _, _ = await seed(factory)
    pending = await claim(usage, factory)
    assert await usage.apply_usage_poll(factory, pending, receipt(pending), now=NOW)

    def invalid_hash(*args):
        raise ValueError("private source data must not escape")

    monkeypatch.setattr(vpn_usage_sources, "source_hash", invalid_hash)
    later = NOW + timedelta(minutes=1)
    pending = await claim(usage, factory, now=later)
    assert await usage.apply_usage_poll(
        factory,
        pending,
        source_receipt(pending, up=150, down=390),
        now=later,
        source_secret=SECRET,
    )
    async with factory() as db:
        assert (
            sum(
                row.upload_bytes
                for row in await db.scalars(select(models.VpnUsageHour))
            )
            == 100
        )
    assert (await poll(factory, worker_id)).source_state == "unavailable"
    assert all(row.source_hashes is None for row in await source_rows(factory))


@pytest.mark.asyncio
@pytest.mark.parametrize("elapsed,expected", [(31, True), (46, False)])
async def test_stale_receipt_or_claim_cannot_create_source_evidence(
    factory, elapsed, expected
):
    await seed(factory)
    pending = await claim(usage, factory)
    assert (
        await usage.apply_usage_poll(
            factory,
            pending,
            source_receipt(pending),
            now=NOW + timedelta(seconds=elapsed),
            source_secret=SECRET,
        )
        is expected
    )
    assert await source_rows(factory) == []
    async with factory() as db:
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None


@pytest.mark.asyncio
async def test_sources_use_one_batched_read_and_write_for_many_keys(factory):
    await seed(factory, count=50)
    source_queries = []
    engine = factory.kw["bind"].sync_engine

    def capture(connection, cursor, statement, values, context, many):
        if "vpn_key_source_samples" in statement:
            source_queries.append((statement, many))

    for minute in (NOW, NOW + timedelta(minutes=1)):
        pending = await claim(usage, factory, now=minute)
        source_queries.clear()
        event.listen(engine, "before_cursor_execute", capture)
        try:
            assert await usage.apply_usage_poll(
                factory,
                pending,
                source_receipt(pending),
                now=minute,
                source_secret=SECRET,
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        reads = [sql for sql, _ in source_queries if sql.lstrip().startswith("SELECT")]
        writes = [
            (sql, many)
            for sql, many in source_queries
            if not sql.lstrip().startswith("SELECT")
        ]
        assert len(reads) == len(writes) == 1
        assert writes[0][1] is True


@pytest.mark.asyncio
async def test_source_prefetch_locks_existing_rows_in_key_order_against_retention(
    factory,
):
    await seed(factory)
    pending = await claim(usage, factory)
    locks = []

    class SourceLockSession(AsyncSession):
        async def scalars(self, statement, **kwargs):
            if statement.column_descriptions[0]["entity"] is models.VpnKeySourceSample:
                lock = statement._for_update_arg
                assert lock is not None
                locks.append((lock.read, lock.skip_locked))
                assert list(statement._order_by_clauses) == [
                    models.VpnKeySourceSample.access_key_id
                ]
            return await super().scalars(statement, **kwargs)

    locked_factory = async_sessionmaker(
        factory.kw["bind"], class_=SourceLockSession, expire_on_commit=False
    )
    assert await usage.apply_usage_poll(
        locked_factory,
        pending,
        source_receipt(pending),
        now=NOW,
        source_secret=SECRET,
    )
    assert locks == [(False, False)]
