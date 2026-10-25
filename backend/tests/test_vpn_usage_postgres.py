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

        async def scalars(self, statement, **kwargs):
            if statement._for_update_arg is not None:
                requested.set()
            return await super().scalars(statement, **kwargs)

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
@pytest.mark.parametrize("interval", ["first_baseline", "new_hour"])
async def test_usage_foreign_key_writes_do_not_deadlock_control_key_then_worker_locks(
    pg_factory, interval
):
    from app.services.vpn_control_intents import stage_vpn_control_operation

    worker_id, endpoint_id, keys = await seed(pg_factory)
    async with pg_factory() as db:
        (await db.get(models.VpnEndpoint, endpoint_id)).status = "disabled"
        await db.commit()
    at = NOW
    if interval == "new_hour":
        first = await claim(usage, pg_factory, NOW + timedelta(minutes=59))
        assert await usage.apply_usage_poll(
            pg_factory,
            first,
            receipt(first),
            now=NOW + timedelta(minutes=59, seconds=1),
        )
        at = NOW + timedelta(hours=1)
    pending = await claim(usage, pg_factory, at)
    keys_locked, allow_stage_worker = asyncio.Event(), asyncio.Event()
    usage_worker_locked, continue_usage = asyncio.Event(), asyncio.Event()

    class StagingSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"] is models.WorkerNode
            ):
                keys_locked.set()
                await allow_stage_worker.wait()
            return await super().scalar(statement, **kwargs)

    class ApplyingSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            result = await super().scalar(statement, **kwargs)
            if (
                result is not None
                and statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"] is models.WorkerNode
            ):
                usage_worker_locked.set()
                await continue_usage.wait()
            return result

    staging_factory = async_sessionmaker(
        pg_factory.kw["bind"], class_=StagingSession, expire_on_commit=False
    )
    applying_factory = async_sessionmaker(
        pg_factory.kw["bind"], class_=ApplyingSession, expire_on_commit=False
    )

    async def stage():
        async with staging_factory() as db, db.begin():
            operation = await stage_vpn_control_operation(db, keys[0], "revoke", now=at)
            return operation.action

    stage_task = asyncio.create_task(stage())
    apply_task, worker_signal = None, None
    try:
        await asyncio.wait_for(keys_locked.wait(), 2)
        apply_task = asyncio.create_task(
            usage.apply_usage_poll(
                applying_factory,
                pending,
                receipt(pending, up=150, down=390),
                now=at + timedelta(seconds=1),
            )
        )
        worker_signal = asyncio.create_task(usage_worker_locked.wait())
        finished, _ = await asyncio.wait(
            {apply_task, worker_signal}, timeout=2, return_when=asyncio.FIRST_COMPLETED
        )
        assert finished
        allow_stage_worker.set()
        continue_usage.set()
        results = await asyncio.wait_for(
            asyncio.gather(apply_task, stage_task, return_exceptions=True), 5
        )
        assert results == [False, "revoke"]
        async with pg_factory() as db:
            samples = (await db.scalars(select(models.VpnKeyUsageSample))).all()
            assert len(samples) == (2 if interval == "new_hour" else 0)
            assert all(sample.upload_bytes == 100 for sample in samples)
            assert await db.scalar(select(models.VpnUsageHour)) is None
            assert (
                await db.get(models.VpnNodeUsagePoll, worker_id)
            ).claim_token == pending.claim_token
    finally:
        allow_stage_worker.set()
        continue_usage.set()
        tasks = [
            task for task in (stage_task, apply_task, worker_signal) if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_busy_unverified_child_prevents_partial_key_prelocks_from_applying(
    pg_factory,
):
    worker_id, _, keys = await seed(pg_factory, unverified=True)
    pending = await claim(usage, pg_factory)
    assert len(pending.bindings) == 2 and len(pending.request.identities) == 1
    async with pg_factory() as owner, owner.begin():
        await owner.scalar(
            select(models.VpnAccessKey)
            .where(models.VpnAccessKey.id == keys[0])
            .with_for_update()
        )
        assert not await asyncio.wait_for(
            usage.apply_usage_poll(
                pg_factory, pending, receipt(pending), now=NOW + timedelta(seconds=1)
            ),
            1,
        )
    async with pg_factory() as db:
        assert (
            await db.get(models.VpnNodeUsagePoll, worker_id)
        ).claim_token == pending.claim_token
        assert await db.scalar(select(models.VpnKeyUsageSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("locked", [models.VpnKeySourceSample, models.VpnUsageHour])
async def test_retention_skips_busy_rows_without_deleting_traffic_baselines(
    pg_factory,
    locked,
):
    worker_id, _, keys = await seed(pg_factory)
    pending = await claim(usage, pg_factory)
    assert await usage.apply_usage_poll(pg_factory, pending, receipt(pending), now=NOW)
    expired = NOW - timedelta(days=91)
    async with pg_factory() as db:
        for key_id in keys:
            source = await db.get(models.VpnKeySourceSample, key_id)
            source.minute_at = source.observed_at = expired
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
    async with pg_factory() as owner, owner.begin():
        await owner.scalar(
            select(locked)
            .where(
                locked.access_key_id == keys[0],
            )
            .with_for_update()
        )
        assert (
            await asyncio.wait_for(
                usage.cleanup_usage_retention(pg_factory, now=NOW),
                1,
            )
            == 2
        )
    assert await usage.cleanup_usage_retention(pg_factory, now=NOW) == 1
    async with pg_factory() as db:
        assert len((await db.scalars(select(models.VpnKeyUsageSample))).all()) == 2
        assert await db.scalar(select(models.VpnKeySourceSample)) is None
        assert await db.scalar(select(models.VpnUsageHour)) is None


@pytest.mark.asyncio
async def test_apply_source_prefetch_prevents_retention_from_losing_valid_traffic(
    pg_factory,
):
    worker_id, _, _ = await seed(pg_factory)
    pending = await claim(usage, pg_factory)
    assert await usage.apply_usage_poll(pg_factory, pending, receipt(pending), now=NOW)
    async with pg_factory() as db:
        for source in await db.scalars(select(models.VpnKeySourceSample)):
            source.observed_at = NOW - timedelta(days=2)
        await db.commit()
    later = NOW + timedelta(minutes=1)
    pending = await claim(usage, pg_factory, now=later)
    prefetched, continue_apply = asyncio.Event(), asyncio.Event()

    class PausedSourceSession(AsyncSession):
        async def scalars(self, statement, **kwargs):
            rows = await super().scalars(statement, **kwargs)
            if statement.column_descriptions[0]["entity"] is models.VpnKeySourceSample:
                prefetched.set()
                await continue_apply.wait()
            return rows

    paused_factory = async_sessionmaker(
        pg_factory.kw["bind"],
        class_=PausedSourceSession,
        expire_on_commit=False,
    )
    apply_task = asyncio.create_task(
        usage.apply_usage_poll(
            paused_factory,
            pending,
            receipt(pending, up=150, down=390),
            now=later,
        )
    )
    try:
        await asyncio.wait_for(prefetched.wait(), 3)
        assert (
            await asyncio.wait_for(
                usage.cleanup_usage_retention(pg_factory, now=later), 1
            )
            == 0
        )
        continue_apply.set()
        assert await asyncio.wait_for(apply_task, 3)
        async with pg_factory() as db:
            assert len((await db.scalars(select(models.VpnKeySourceSample))).all()) == 2
            assert len((await db.scalars(select(models.VpnKeyUsageSample))).all()) == 2
            hours = (await db.scalars(select(models.VpnUsageHour))).all()
            assert sum(row.upload_bytes for row in hours) == 100
            assert sum(row.download_bytes for row in hours) == 180
            assert (
                await db.get(models.VpnNodeUsagePoll, worker_id)
            ).traffic_state == "ok"
    finally:
        continue_apply.set()
        if not apply_task.done():
            apply_task.cancel()
        await asyncio.gather(apply_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_abuse_concurrent_fifth_window_creates_one_open_incident(pg_factory):
    from test_vpn_abuse import device_seed, evaluate, incidents, samples, state, windows

    from app.services import vpn_abuse as abuse

    ids = await device_seed(pg_factory)
    await windows(abuse, pg_factory, ids, count=4)
    minute = NOW + timedelta(minutes=4)
    await samples(pg_factory, ids, minute)
    barrier = asyncio.Barrier(2)

    async def contender():
        await barrier.wait()
        await evaluate(abuse, pg_factory, ids, minute)

    await asyncio.wait_for(asyncio.gather(contender(), contender()), 5)
    assert len(await incidents(pg_factory)) == 1
    assert (await state(pg_factory, ids)).consecutive_windows == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("ack_first", [False, True])
async def test_abuse_evaluation_and_acknowledgement_share_state_then_incident_order(
    pg_factory, ack_first
):
    from test_vpn_abuse import device_seed, evaluate, incidents, samples, state, windows

    from app.services import vpn_abuse as abuse

    ids = await device_seed(pg_factory)
    await windows(abuse, pg_factory, ids)
    incident_id = (await incidents(pg_factory))[0].id
    minute = NOW + timedelta(minutes=5)
    await samples(pg_factory, ids, minute)
    locked, release, second_started = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class PausedStateSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            result = await super().scalar(statement, **kwargs)
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"]
                is models.VpnDeviceAbuseState
            ):
                locked.set()
                await release.wait()
            return result

    class SignalledSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            second_started.set()
            return await super().execute(statement, *args, **kwargs)

    paused = async_sessionmaker(
        pg_factory.kw["bind"], class_=PausedStateSession, expire_on_commit=False
    )
    signalled = async_sessionmaker(
        pg_factory.kw["bind"], class_=SignalledSession, expire_on_commit=False
    )

    async def ack(factory):
        async with factory() as db, db.begin():
            await abuse.acknowledge_incident(
                db, incident_id, actor_id=None, now=minute + timedelta(seconds=25)
            )

    first = asyncio.create_task(
        ack(paused) if ack_first else evaluate(abuse, paused, ids, minute)
    )
    second = None
    try:
        await asyncio.wait_for(locked.wait(), 3)
        second = asyncio.create_task(
            evaluate(abuse, signalled, ids, minute) if ack_first else ack(signalled)
        )
        await asyncio.wait_for(second_started.wait(), 3)
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 5)
        rows = await incidents(pg_factory)
        assert len(rows) == 1 and rows[0].acknowledged_at is not None
        assert (await state(pg_factory, ids)).consecutive_windows == 0
    finally:
        release.set()
        for task in (first, second):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(
            *(task for task in (first, second) if task is not None),
            return_exceptions=True,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["new_child", "revoke"])
async def test_abuse_child_snapshot_is_taken_after_waiting_for_state_lock(
    pg_factory, change
):
    from test_vpn_abuse import device_seed, evaluate, incidents, samples, state, windows

    from app.services import vpn_abuse as abuse

    ids = await device_seed(pg_factory)
    await windows(abuse, pg_factory, ids, count=4)
    minute = NOW + timedelta(minutes=4)
    await samples(pg_factory, ids, minute)
    entered = asyncio.Event()

    class SignalledSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            entered.set()
            return await super().execute(statement, *args, **kwargs)

    waiting = async_sessionmaker(
        pg_factory.kw["bind"], class_=SignalledSession, expire_on_commit=False
    )
    task = None
    try:
        async with pg_factory() as owner, owner.begin():
            await owner.scalar(
                select(models.VpnDeviceAbuseState)
                .where(models.VpnDeviceAbuseState.device_id == ids.device)
                .with_for_update()
            )
            task = asyncio.create_task(evaluate(abuse, waiting, ids, minute))
            await asyncio.wait_for(entered.wait(), 3)
            async with pg_factory() as writer, writer.begin():
                parent = await writer.get(models.VpnDevice, ids.device)
                if change == "revoke":
                    parent.revoke_requested_at = minute
                else:
                    writer.add(
                        models.VpnAccessKey(
                            subscription_id=parent.subscription_id,
                            device_id=ids.device,
                            worker_id=ids.workers[0],
                            status="pending_sync",
                            created_at=minute,
                        )
                    )
        await asyncio.wait_for(task, 5)
        assert (await state(pg_factory, ids)).consecutive_windows == 0
        assert await incidents(pg_factory) == []
    finally:
        if task is not None:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_abuse_state_lock_and_implicit_fk_do_not_block_credential_mutations(
    pg_factory,
):
    from test_vpn_abuse import device_seed, evaluate, samples

    from app.services import vpn_abuse as abuse

    ids = await device_seed(pg_factory)
    await samples(pg_factory, ids, NOW)
    locked, release = asyncio.Event(), asyncio.Event()

    class PausedStateSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            result = await super().scalar(statement, **kwargs)
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions[0]["entity"]
                is models.VpnDeviceAbuseState
            ):
                locked.set()
                await release.wait()
            return result

    paused = async_sessionmaker(
        pg_factory.kw["bind"], class_=PausedStateSession, expire_on_commit=False
    )
    task = asyncio.create_task(evaluate(abuse, paused, ids, NOW))
    try:
        await asyncio.wait_for(locked.wait(), 3)
        async with pg_factory() as db, db.begin():
            await db.execute(text("SET LOCAL lock_timeout='500ms'"))
            customer_id = await db.scalar(select(models.VpnCustomer.id))
            await db.scalar(
                select(models.VpnCustomer)
                .where(models.VpnCustomer.id == customer_id)
                .with_for_update()
            )
            await db.scalars(
                select(models.VpnSubscription)
                .where(models.VpnSubscription.customer_id == customer_id)
                .order_by(models.VpnSubscription.id)
                .with_for_update()
            )
            await db.scalars(
                select(models.VpnAccessKey)
                .where(models.VpnAccessKey.device_id == ids.device)
                .order_by(models.VpnAccessKey.id)
                .with_for_update()
            )
            await db.scalar(
                select(models.WorkerNode)
                .where(models.WorkerNode.id == ids.workers[0])
                .with_for_update()
            )
            await db.scalar(
                select(models.VpnEndpoint)
                .where(models.VpnEndpoint.id == ids.endpoints[0])
                .with_for_update()
            )
            (
                await db.get(models.VpnDevice, ids.device)
            ).display_name = "Rename does not wait for detector"
        release.set()
        await asyncio.wait_for(task, 5)
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_abuse_expiry_skips_busy_state_without_source_or_worker_locks(pg_factory):
    from test_vpn_abuse import device_seed, windows

    from app.services import vpn_abuse as abuse

    ids = await device_seed(pg_factory)
    await windows(abuse, pg_factory, ids, count=1)
    async with pg_factory() as owner, owner.begin():
        await owner.scalar(
            select(models.VpnDeviceAbuseState)
            .where(models.VpnDeviceAbuseState.device_id == ids.device)
            .with_for_update()
        )
        assert (
            await asyncio.wait_for(
                abuse.expire_abuse_windows(pg_factory, now=NOW + timedelta(minutes=3)),
                1,
            )
            == 0
        )
    assert (
        await abuse.expire_abuse_windows(pg_factory, now=NOW + timedelta(minutes=3))
        == 1
    )
