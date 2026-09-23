"""Real PostgreSQL locks and production VPN migrations; never SQLite concurrency."""

import asyncio
from datetime import timedelta
import re

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.base import Base
from app.db.migrations import MIGRATIONS
from app.db.models import (
    User,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnFriendInvitation,
    WorkerNode,
)
from app.services import vpn_friend_invitations as friends
from app.services import vpn_public_trial as trial
from app.services.vpn_control_intents import stage_vpn_control_operation
from app.services.vpn_policy import select_public_vpn_endpoint
from app.services.vpn_telegram_identity import TelegramIdentity
from test_vpn_endpoint_migrations import postgres_schema as postgres_schema
from test_vpn_public_trial import (
    NOW,
    endpoint,
    seed_trial,
    trial_counts,
    trial_settings,
    worker,
)


@pytest_asyncio.fixture
async def pg_trial(postgres_schema, monkeypatch):
    # Only unrelated support tables come from metadata. All VPN tables, indexes,
    # constraints and new columns are created by the actual startup migrations.
    required = {
        "users",
        "worker_nodes",
        "worker_tasks",
        "attack_runs",
        "worker_maintenance_jobs",
    }
    pending = list(required)
    while pending:
        for fk in Base.metadata.tables[pending.pop()].foreign_keys:
            dependency = fk.column.table.name
            if dependency not in required:
                required.add(dependency)
                pending.append(dependency)
    async with postgres_schema.engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name in required:
                await connection.run_sync(table.create)
        for statement in MIGRATIONS:
            if re.search(r"\b(?:vpn_\w+|app_settings)\b", statement):
                await connection.execute(text(statement))
    sessions = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    monkeypatch.setattr(friends, "load_transport_snapshot", lambda *_: object())
    async with sessions() as db:
        await seed_trial(db)
    return postgres_schema.engine, sessions


async def limits(db):
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(text("SET LOCAL statement_timeout = '10s'"))


def locking(statement, model):
    return (
        statement._for_update_arg is not None
        and statement.column_descriptions[0]["entity"] is model
    )


@pytest.mark.asyncio
async def test_same_new_identity_race_creates_one_generation_one_chain(pg_trial):
    engine, sessions = pg_trial
    both_missing = asyncio.Event()
    arrived = 0

    class RacingSession(AsyncSession):
        first = True

        async def scalar(self, statement, **kwargs):
            nonlocal arrived
            value = await super().scalar(statement, **kwargs)
            if self.first and locking(statement, VpnCustomer):
                self.first = False
                assert value is None
                arrived += 1
                if arrived == 2:
                    both_missing.set()
                await asyncio.wait_for(both_missing.wait(), 5)
            return value

    racing = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)

    async def contender(identity):
        async with racing() as db:
            await limits(db)
            result = await trial.activate_public_trial(
                db, trial_settings(), TelegramIdentity(identity), NOW
            )
            await db.commit()
            return result

    results = await asyncio.wait_for(
        asyncio.gather(contender("00222"), contender("222")), 15
    )
    assert results[0] == results[1]
    async with sessions() as db:
        assert await trial_counts(db) == [1, 1, 1]
        customer = await db.scalar(
            select(VpnCustomer).where(VpnCustomer.telegram_user_id == "222")
        )
        assert customer.trial_started_at == NOW
        operation = await db.scalar(select(VpnControlOperation))
        assert operation.generation == 1 and operation.state == "queued"


@pytest.mark.asyncio
async def test_two_identities_compete_for_one_capacity_slot(pg_trial):
    engine, sessions = pg_trial
    async with sessions() as db:
        (await db.get(VpnEndpoint, 1)).max_active_profiles = 1
        db.add_all(
            [VpnCustomer(telegram_user_id="222"), VpnCustomer(telegram_user_id="333")]
        )
        await db.commit()
    both_allocating = asyncio.Event()
    arrived = 0

    class RacingSession(AsyncSession):
        first = True

        async def scalar(self, statement, **kwargs):
            nonlocal arrived
            if self.first and locking(statement, WorkerNode):
                self.first = False
                arrived += 1
                if arrived == 2:
                    both_allocating.set()
                await asyncio.wait_for(both_allocating.wait(), 5)
            return await super().scalar(statement, **kwargs)

    racing = async_sessionmaker(engine, class_=RacingSession, expire_on_commit=False)

    async def contender(identity):
        async with racing() as db:
            await limits(db)
            try:
                await trial.activate_public_trial(
                    db, trial_settings(), TelegramIdentity(identity), NOW
                )
            except trial.PublicTrialUnavailable:
                await db.rollback()
                return identity, False
            await db.commit()
            return identity, True

    results = await asyncio.wait_for(
        asyncio.gather(contender("222"), contender("333")), 15
    )
    assert sum(won for _, won in results) == 1
    async with sessions() as db:
        assert await trial_counts(db) == [1, 1, 1]
        for identity, won in results:
            customer = await db.scalar(
                select(VpnCustomer).where(VpnCustomer.telegram_user_id == identity)
            )
            assert (customer.trial_started_at is not None) == won
            if not won:
                assert (
                    await trial.public_trial_status(db, trial_settings(), customer, NOW)
                ).state == "capacity_paused"


@pytest.mark.asyncio
async def test_activation_and_existing_staging_share_worker_without_deadlock(pg_trial):
    engine, sessions = pg_trial
    async with sessions() as db:
        existing = await trial.activate_public_trial(
            db, trial_settings(), TelegramIdentity("123"), NOW
        )
        await db.commit()
    public_has_worker = asyncio.Event()
    stage_waits_worker = asyncio.Event()
    stage_pid = None
    observed_block = False
    lock_sequences = {"public": [], "stage": []}

    class OrderedSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            nonlocal observed_block
            role = self.info["role"]
            if statement._for_update_arg is not None:
                model = statement.column_descriptions[0]["entity"]
                lock_sequences[role].append(model)
                if role == "stage" and model is WorkerNode:
                    stage_waits_worker.set()
            result = await super().scalar(statement, **kwargs)
            if (
                role == "public"
                and locking(statement, WorkerNode)
                and not public_has_worker.is_set()
            ):
                public_has_worker.set()
                await asyncio.wait_for(stage_waits_worker.wait(), 5)

                # Observe the database wait, not merely coroutine scheduling.
                async def observe_wait():
                    while True:
                        blockers = (
                            await self.execute(
                                text("SELECT pg_blocking_pids(:pid)"),
                                {"pid": stage_pid},
                            )
                        ).scalar_one()
                        if blockers:
                            return
                        await asyncio.sleep(0.01)

                await asyncio.wait_for(observe_wait(), 3)
                observed_block = True
            return result

        async def scalars(self, statement, **kwargs):
            if statement._for_update_arg is not None:
                lock_sequences[self.info["role"]].append(
                    statement.column_descriptions[0]["entity"]
                )
            return await super().scalars(statement, **kwargs)

    ordered = async_sessionmaker(engine, class_=OrderedSession, expire_on_commit=False)

    async def activate():
        async with ordered(info={"role": "public"}) as db:
            await limits(db)
            result = await trial.activate_public_trial(
                db, trial_settings(), TelegramIdentity("222"), NOW
            )
            await db.commit()
            return result

    async def stage():
        nonlocal stage_pid
        await asyncio.wait_for(public_has_worker.wait(), 5)
        async with ordered(info={"role": "stage"}) as db:
            await limits(db)
            stage_pid = (await db.execute(text("SELECT pg_backend_pid()"))).scalar_one()
            result = await stage_vpn_control_operation(
                db, existing.access_key_id, "provision", now=NOW
            )
            await db.commit()
            return result.id

    await asyncio.wait_for(asyncio.gather(activate(), stage()), 15)
    assert observed_block
    from app.db.models import VpnSubscription

    assert lock_sequences["stage"][:5] == [
        VpnCustomer,
        VpnSubscription,
        VpnAccessKey,
        WorkerNode,
        VpnEndpoint,
    ]
    public = lock_sequences["public"]
    assert (
        public.index(VpnCustomer) < public.index(WorkerNode) < public.index(VpnEndpoint)
    )
    async with sessions() as db:
        assert await trial_counts(db) == [2, 2, 2]


@pytest.mark.asyncio
async def test_rejected_candidate_releases_savepoint_locks_and_falls_through(pg_trial):
    engine, sessions = pg_trial
    async with sessions() as db:
        second = worker("fallback")
        db.add(second)
        await db.flush()
        db.add(endpoint(second))
        await db.commit()
    candidate_waiting = asyncio.Event()

    class CandidateSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if locking(statement, WorkerNode) and not candidate_waiting.is_set():
                candidate_waiting.set()
            return await super().scalar(statement, **kwargs)

    candidates = async_sessionmaker(
        engine, class_=CandidateSession, expire_on_commit=False
    )
    async with sessions() as blocker, candidates() as allocator:
        await limits(blocker)
        await limits(allocator)
        await blocker.scalar(
            select(WorkerNode).where(WorkerNode.id == 1).with_for_update()
        )

        async def allocate():
            return await select_public_vpn_endpoint(
                allocator, now=NOW, health_max_age_seconds=60, lock=True
            )

        task = asyncio.create_task(allocate())
        try:
            await asyncio.wait_for(candidate_waiting.wait(), 5)
            (await blocker.get(VpnEndpoint, 1)).status = "draining"
            await blocker.commit()
            selected = await asyncio.wait_for(task, 10)
            assert selected.endpoint.worker_id == second.id
            # Allocator still owns the accepted candidate, but the rejected
            # worker AND endpoint locks must be free in a third transaction.
            async with sessions() as probe:
                await limits(probe)
                assert await probe.scalar(
                    select(WorkerNode)
                    .where(WorkerNode.id == 1)
                    .with_for_update(nowait=True)
                )
                assert await probe.scalar(
                    select(VpnEndpoint)
                    .where(VpnEndpoint.id == 1)
                    .with_for_update(nowait=True)
                )
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await allocator.rollback()


@pytest.mark.asyncio
async def test_public_and_friend_race_share_one_trial_marker(pg_trial):
    _, sessions = pg_trial
    from app.db.models import AppSetting

    async with sessions() as db:
        db.add(
            User(
                id=1,
                username="owner",
                password_hash="hash",
                role="admin",
                status="active",
            )
        )
        db.add(AppSetting(key="vpn_friend_beta_release_ready_v1", value="a" * 64))
        await db.flush()
        db.add(
            VpnFriendInvitation(
                slot=1,
                created_by_user_id=1,
                token_digest=friends.digest_invite_token("A" * 43),
                redeem_expires_at=NOW + timedelta(days=1),
            )
        )
        await db.commit()
    start = asyncio.Event()

    async def public():
        await start.wait()
        async with sessions() as db:
            await limits(db)
            try:
                result = await trial.activate_public_trial(
                    db, trial_settings(), TelegramIdentity("123"), NOW
                )
            except trial.PublicTrialConflict:
                await db.rollback()
                customer = await db.get(VpnCustomer, 1)
                assert (
                    await trial.public_trial_status(db, trial_settings(), customer, NOW)
                ).state == "used"
                return None
            await db.commit()
            return result.access_key_id

    async def friend():
        await start.wait()
        async with sessions() as db:
            await limits(db)
            settings = trial_settings(
                VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64
            )
            try:
                result = await friends.redeem_friend_invitation(
                    db, settings, "A" * 43, TelegramIdentity("123"), NOW
                )
            except friends.FriendInvitationConflict:
                await db.rollback()
                return None
            await db.commit()
            return result.access_key_id

    public_task, friend_task = (
        asyncio.create_task(public()),
        asyncio.create_task(friend()),
    )
    start.set()
    public_id, friend_id = await asyncio.wait_for(
        asyncio.gather(public_task, friend_task), 15
    )
    assert (public_id is None) != (friend_id is None)
    async with sessions() as db:
        assert await trial_counts(db) == [1, 1, 1]
        assert (await db.get(VpnCustomer, 1)).trial_started_at == NOW
        only_key = await db.scalar(select(VpnAccessKey))
        assert only_key.id == (public_id if public_id is not None else friend_id)
        invitation = await db.get(VpnFriendInvitation, 1)
        if friend_id is not None:
            assert invitation.access_key_id == friend_id
            settings = trial_settings(
                VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64
            )
            replay = await friends.redeem_friend_invitation(
                db, settings, "A" * 43, TelegramIdentity("123"), NOW
            )
            assert replay.access_key_id == friend_id
        else:
            assert invitation.redeemed_at is None
            replay = await trial.activate_public_trial(
                db, trial_settings(), TelegramIdentity("123"), NOW
            )
            assert replay.access_key_id == public_id
