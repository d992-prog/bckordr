"""Synthetic PostgreSQL races and migration parity; never application DB_URL."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import inspect, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_usage import KNOWN_HOSTS, NOW, claim, receipt, seed

from app.db import models
from app.db.base import Base
from app.db.migrations import MIGRATIONS, run_startup_migrations
from app.db.vpn_device_migrations import VPN_DEVICE_MIGRATIONS
from app.db.vpn_usage_migrations import VPN_USAGE_MIGRATIONS
from app.services import vpn_usage as usage

TABLES = (
    "vpn_node_usage_polls",
    "vpn_key_usage_samples",
    "vpn_usage_hours",
    "vpn_key_source_samples",
    "vpn_device_abuse_state",
    "vpn_abuse_incidents",
)


@pytest_asyncio.fixture
async def pg_factory(postgres_schema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return async_sessionmaker(postgres_schema.engine, expire_on_commit=False)


def test_usage_migrations_are_registered_additively_after_device_schema():
    start = MIGRATIONS.index(VPN_USAGE_MIGRATIONS[0])
    assert MIGRATIONS[start : start + len(VPN_USAGE_MIGRATIONS)] == VPN_USAGE_MIGRATIONS
    assert start > MIGRATIONS.index(VPN_DEVICE_MIGRATIONS[-1])


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_first", [False, True])
async def test_usage_startup_migrations_are_idempotent_and_match_metadata(
    postgres_schema, metadata_first
):
    tables = [
        table
        for table in Base.metadata.sorted_tables
        if metadata_first or table.name not in TABLES
    ]
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    await run_startup_migrations(postgres_schema.engine)
    await run_startup_migrations(postgres_schema.engine)
    async with postgres_schema.engine.connect() as connection:

        def catalog(sync):
            inspector = inspect(sync)
            for name in TABLES:
                table = Base.metadata.tables[name]
                assert {
                    column["name"] for column in inspector.get_columns(name)
                } == set(table.c.keys())
                expected = {
                    constraint.name
                    for constraint in table.constraints
                    if constraint.name
                }
                actual = [
                    constraint["name"]
                    for constraint in inspector.get_check_constraints(name)
                ]
                assert expected <= set(actual)
                assert all(actual.count(name) == 1 for name in expected)
                assert {index.name for index in table.indexes} <= {
                    index["name"] for index in inspector.get_indexes(name)
                }
                assert all(
                    constraint["options"]["ondelete"] == "RESTRICT"
                    for constraint in inspector.get_foreign_keys(name)
                )

        await connection.run_sync(catalog)
        predicate = await connection.scalar(
            text("""
            SELECT pg_get_expr(i.indpred, i.indrelid) FROM pg_index i
            JOIN pg_class c ON c.oid=i.indexrelid JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname='uq_vpn_abuse_incidents_open_device'
        """)
        )
        assert predicate == "(acknowledged_at IS NULL)"


@pytest.mark.asyncio
async def test_independent_claimants_get_one_claim_and_one_aggregate_increment(
    pg_factory,
):
    worker_id, _, keys = await seed(pg_factory)
    claims = await asyncio.gather(*(claim(usage, pg_factory) for _ in range(2)))
    assert len([value for value in claims if value is not None]) == 1
    winner = next(value for value in claims if value is not None)
    assert await usage.apply_usage_poll(
        pg_factory, winner, receipt(winner), now=NOW + timedelta(seconds=1)
    )
    pending = await claim(usage, pg_factory, NOW + timedelta(minutes=1))
    results = await asyncio.gather(
        *(
            usage.apply_usage_poll(
                pg_factory,
                pending,
                receipt(pending, up=150, down=390),
                now=NOW + timedelta(minutes=1, seconds=1),
            )
            for _ in range(2)
        )
    )
    assert sorted(results) == [False, True]
    async with pg_factory() as db:
        rows = (await db.scalars(select(models.VpnUsageHour))).all()
        assert {row.access_key_id for row in rows} == set(keys)
        assert all(
            (row.upload_bytes, row.download_bytes, row.sample_count) == (50, 90, 1)
            for row in rows
        )
        assert (await db.get(models.VpnNodeUsagePoll, worker_id)).claim_token is None


@pytest.mark.asyncio
async def test_locked_worker_and_poll_are_skipped_without_waiting(pg_factory):
    worker_id, _, _ = await seed(pg_factory)
    async with pg_factory() as owner, owner.begin():
        await owner.scalar(
            select(models.WorkerNode)
            .where(models.WorkerNode.id == worker_id)
            .with_for_update()
        )
        assert await asyncio.wait_for(claim(usage, pg_factory), 1) is None
    pending = await claim(usage, pg_factory)
    await usage.apply_usage_poll(
        pg_factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
    )
    async with pg_factory() as owner, owner.begin():
        await owner.scalar(
            select(models.VpnNodeUsagePoll)
            .where(models.VpnNodeUsagePoll.worker_id == worker_id)
            .with_for_update()
        )
        assert (
            await asyncio.wait_for(
                claim(usage, pg_factory, NOW + timedelta(minutes=1)), 1
            )
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [models.WorkerNode, models.VpnNodeUsagePoll])
async def test_apply_does_not_wait_on_a_row_lock_beyond_the_claim_lease(
    pg_factory, locked
):
    worker_id, _, _ = await seed(pg_factory)
    pending = await claim(usage, pg_factory)
    attempted = asyncio.Event()
    clock = SimpleNamespace(now=NOW + timedelta(seconds=20))

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"] is locked
            ):
                attempted.set()
            return await super().scalar(statement, **kwargs)

    applying = async_sessionmaker(
        pg_factory.kw["bind"], class_=ObservedSession, expire_on_commit=False
    )
    async with pg_factory() as owner:
        column = locked.id if locked is models.WorkerNode else locked.worker_id
        await owner.scalar(select(locked).where(column == worker_id).with_for_update())
        task = asyncio.create_task(
            usage.apply_usage_poll(applying, pending, receipt(pending), now=clock.now)
        )
        try:
            await asyncio.wait_for(attempted.wait(), 1)
            clock.now = NOW + timedelta(seconds=50)
            assert clock.now > pending.claimed_at + timedelta(
                seconds=usage.USAGE_LEASE_SECONDS
            )
            finished, _ = await asyncio.wait({task}, timeout=0.2)
        finally:
            await owner.rollback()
            result = await asyncio.wait_for(task, 2)
        assert result is False
        assert finished == {task}, (
            "apply must finish while the contended row remains locked"
        )
    async with pg_factory() as db:
        assert (
            await db.get(models.VpnNodeUsagePoll, worker_id)
        ).claim_token == pending.claim_token
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None


@pytest.mark.asyncio
async def test_apply_rechecks_lease_after_waiting_for_the_connection_pool(
    pg_factory, postgres_schema, monkeypatch
):
    worker_id, _, _ = await seed(pg_factory)
    pending = await claim(usage, pg_factory)
    clock = SimpleNamespace(elapsed=0.0)
    monkeypatch.setattr(usage, "monotonic", lambda: clock.elapsed, raising=False)
    requested = asyncio.Event()

    class ObservedSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if statement._for_update_arg is not None:
                requested.set()
            return await super().scalar(statement, **kwargs)

    engine = create_async_engine(
        postgres_schema.engine.url,
        pool_size=1,
        max_overflow=0,
        connect_args={"server_settings": {"search_path": postgres_schema.name}},
        hide_parameters=True,
    )
    applying = async_sessionmaker(
        engine, class_=ObservedSession, expire_on_commit=False
    )
    task = None
    try:
        async with engine.connect():
            task = asyncio.create_task(
                usage.apply_usage_poll(
                    applying, pending, receipt(pending), now=NOW + timedelta(seconds=20)
                )
            )
            await asyncio.wait_for(requested.wait(), 1)
            clock.elapsed = 30.0
        assert not await asyncio.wait_for(task, 2)
        async with pg_factory() as db:
            assert (
                await db.get(models.VpnNodeUsagePoll, worker_id)
            ).claim_token == pending.claim_token
            assert await db.scalar(select(models.VpnKeyUsageSample)) is None
            assert await db.scalar(select(models.VpnUsageHour)) is None
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await engine.dispose()


@pytest.mark.asyncio
async def test_network_observation_holds_no_worker_customer_subscription_or_key_lock(
    pg_factory,
):
    worker_id, _, keys = await seed(pg_factory)
    entered, release = asyncio.Event(), asyncio.Event()

    async def transport(snapshot, request, **kwargs):
        entered.set()
        await release.wait()
        return receipt(type("Claim", (), {"request": request})())

    task = asyncio.create_task(
        usage.collect_usage_poll(
            pg_factory,
            KNOWN_HOSTS,
            now=lambda: NOW,
            snapshot_loader=lambda *_: object(),
            transport=transport,
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    try:
        async with pg_factory() as issuing, issuing.begin():
            await issuing.execute(text("SET LOCAL lock_timeout='500ms'"))
            worker = await issuing.scalar(
                select(models.WorkerNode)
                .where(models.WorkerNode.id == worker_id)
                .with_for_update()
            )
            key = await issuing.scalar(
                select(models.VpnAccessKey)
                .where(models.VpnAccessKey.id == keys[0])
                .with_for_update()
            )
            subscription = await issuing.scalar(
                select(models.VpnSubscription)
                .where(models.VpnSubscription.id == key.subscription_id)
                .with_for_update()
            )
            customer = await issuing.scalar(
                select(models.VpnCustomer)
                .where(models.VpnCustomer.id == subscription.customer_id)
                .with_for_update()
            )
            worker.notes = "issuance proceeded during SSH"
            assert customer.id
    finally:
        release.set()
        await asyncio.gather(task)
    assert task.result()


@pytest.mark.asyncio
async def test_usage_foreign_keys_restrict_and_open_incidents_are_unique(pg_factory):
    from sqlalchemy.exc import IntegrityError

    _, _, keys = await seed(pg_factory)
    async with pg_factory() as db:
        key = await db.get(models.VpnAccessKey, keys[0])
        device_id = key.device_id
        db.add(
            models.VpnAbuseIncident(
                device_id=device_id,
                started_at=NOW,
                last_observed_at=NOW,
                peak_source_count=3,
            )
        )
        await db.commit()
        db.add(
            models.VpnAbuseIncident(
                device_id=device_id,
                started_at=NOW,
                last_observed_at=NOW,
                peak_source_count=4,
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()
        await db.execute(
            text("UPDATE vpn_abuse_incidents SET acknowledged_at=:at"), {"at": NOW}
        )
        db.add(
            models.VpnAbuseIncident(
                device_id=device_id,
                started_at=NOW,
                last_observed_at=NOW,
                peak_source_count=4,
            )
        )
        await db.commit()
