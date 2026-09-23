from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
    WorkerTask,
)
from app.services.vpn_policy import NODE_CAPACITY_STATUSES, select_public_vpn_endpoint


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


class HookedSession(AsyncSession):
    on_lock = None

    async def scalar(self, statement, **kwargs):
        if statement._for_update_arg is not None and self.on_lock is not None:
            hook, self.on_lock = self.on_lock, None
            await hook(self)
        return await super().scalar(statement, **kwargs)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _allow_invalid_rows(dbapi_connection, _connection_record):
        # Exercise policy checks even for values normally rejected by DB constraints.
        dbapi_connection.execute("PRAGMA ignore_check_constraints=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=HookedSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def worker(name, **changes):
    values = dict(
        name=name,
        status="ready",
        is_enabled=True,
        vpn_enabled=True,
        vpn_role="vpn_node",
        vpn_runtime_status="ready",
        vpn_public_host=f"{name}.example",
        vpn_inbound_id=1,
        ssh_host="10.0.0.1",
        ssh_password="secret",
        vpn_last_checked_at=NOW,
    )
    values.update(changes)
    return WorkerNode(**values)


def endpoint(node, **changes):
    values = dict(
        worker_id=node.id,
        inbound_id=1,
        public_host=f"{node.name}.example",
        port=443,
        protocol="vless",
        transport="raw",
        security="reality",
        status="ready",
        verified_at=NOW,
        max_active_profiles=4,
    )
    values.update(changes)
    return VpnEndpoint(**values)


async def add_key(session, target, status="active"):
    customer = VpnCustomer(status="active")
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=20)
    session.add(subscription)
    await session.flush()
    key = VpnAccessKey(
        subscription_id=subscription.id,
        worker_id=target.worker_id,
        endpoint_id=target.id,
        status=status,
    )
    session.add(key)
    await session.flush()
    return key


@pytest.mark.asyncio
async def test_selects_least_utilized_ratio_then_lowest_id(session_factory):
    async with session_factory() as session:
        nodes = [worker(str(i)) for i in range(3)]
        session.add_all(nodes)
        await session.flush()
        targets = [endpoint(nodes[0], max_active_profiles=2), endpoint(nodes[1]), endpoint(nodes[2])]
        session.add_all(targets)
        await session.flush()
        await add_key(session, targets[0])
        await add_key(session, targets[1])
        await add_key(session, targets[2])
        selected = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert selected is not None
        assert selected.endpoint.id == targets[1].id
        assert selected.occupied_profiles == 1
        assert selected.max_active_profiles == 4
        assert selected.utilization == 0.25
        with pytest.raises(FrozenInstanceError):
            selected.occupied_profiles = 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "draining"},
        {"verified_at": None},
        {"security": "tls"},
        {"max_active_profiles": None},
        {"max_active_profiles": 0},
        {"archived_at": NOW},
        {"vpn_enabled": False},
        {"vpn_last_checked_at": None},
        {"vpn_last_checked_at": NOW - timedelta(seconds=61)},
    ],
)
async def test_excludes_invalid_endpoint_or_worker(session_factory, change):
    async with session_factory() as session:
        node = worker("excluded")
        fallback_node = worker("fallback")
        for field, value in change.items():
            if hasattr(node, field):
                setattr(node, field, value)
        session.add_all([node, fallback_node])
        await session.flush()
        invalid = endpoint(node)
        for field, value in change.items():
            if hasattr(invalid, field):
                setattr(invalid, field, value)
        fallback = endpoint(fallback_node)
        session.add_all([invalid, fallback])
        await session.flush()
        selected = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert selected is not None
        assert selected.endpoint.id == fallback.id


@pytest.mark.asyncio
async def test_full_endpoint_is_excluded(session_factory):
    async with session_factory() as session:
        first, second = worker("full"), worker("free")
        session.add_all([first, second])
        await session.flush()
        full, free = endpoint(first, max_active_profiles=1), endpoint(second)
        session.add_all([full, free])
        await session.flush()
        await add_key(session, full)
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None and result.endpoint.id == free.id


@pytest.mark.asyncio
async def test_active_attack_excludes_worker(session_factory):
    async with session_factory() as session:
        busy, free = worker("busy"), worker("free")
        session.add_all([busy, free])
        await session.flush()
        busy_endpoint, free_endpoint = endpoint(busy), endpoint(free)
        session.add_all([busy_endpoint, free_endpoint])
        domain = DropDomain(fqdn="target.fr", zone="fr", drop_date=NOW.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=NOW,
            planned_end_at=NOW + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(WorkerTask(attack_run_id=run.id, domain_id=domain.id, worker_id=busy.id, status="running"))
        await session.flush()
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None and result.endpoint.id == free_endpoint.id


@pytest.mark.asyncio
async def test_all_capacity_statuses_count_but_revoked_does_not(session_factory):
    statuses = (
        "pending_sync", "syncing", "active", "pending_suspend", "suspended", "pending_revoke", "failed",
    )
    assert NODE_CAPACITY_STATUSES == statuses
    async with session_factory() as session:
        node = worker("statuses")
        session.add(node)
        await session.flush()
        target = endpoint(node, max_active_profiles=8)
        session.add(target)
        await session.flush()
        for status in (*statuses, "revoked"):
            await add_key(session, target, status)
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None
        assert result.occupied_profiles == 7
        assert result.utilization == 7 / 8


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age", [0, -30])
async def test_health_age_is_clamped_to_one_second(session_factory, max_age):
    async with session_factory() as session:
        recent = worker("recent", vpn_last_checked_at=NOW - timedelta(seconds=1))
        stale = worker("stale", vpn_last_checked_at=NOW - timedelta(seconds=2))
        session.add_all([recent, stale])
        await session.flush()
        session.add_all([endpoint(stale), endpoint(recent)])
        await session.flush()
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=max_age)
        assert result is not None and result.endpoint.worker_id == recent.id


@pytest.mark.asyncio
async def test_lock_recounts_full_first_candidate_and_tries_next(session_factory):
    async with session_factory() as session:
        first, second = worker("first"), worker("second")
        session.add_all([first, second])
        await session.flush()
        first_endpoint, second_endpoint = endpoint(first, max_active_profiles=1), endpoint(second)
        session.add_all([first_endpoint, second_endpoint])
        await session.flush()
        events = []

        @event.listens_for(session.sync_session, "after_transaction_create")
        def trace_savepoint(_session, transaction):
            if transaction.nested:
                events.append("savepoint")

        @event.listens_for(session.sync_session, "after_soft_rollback")
        def trace_rollback(_session, transaction):
            if transaction.nested:
                events.append("rollback")

        async def fill_first(current_session):
            await add_key(current_session, first_endpoint)

        session.on_lock = fill_first
        original_scalar = session.scalar

        async def record_locks(statement, **kwargs):
            if statement._for_update_arg is not None:
                entity = statement.column_descriptions[0]["entity"]
                events.append((entity, next(iter(statement.compile().params.values()))))
            return await original_scalar(statement, **kwargs)

        session.scalar = record_locks
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60, lock=True)
        assert result is not None and result.endpoint.id == second_endpoint.id
        assert events[:7] == [
            "savepoint",
            (WorkerNode, first.id),
            (VpnEndpoint, first_endpoint.id),
            "rollback",
            "savepoint",
            (WorkerNode, second.id),
            (VpnEndpoint, second_endpoint.id),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,changes",
    [
        (VpnEndpoint, {"status": "draining"}),
        (VpnEndpoint, {"security": "tls"}),
        (VpnEndpoint, {"verified_at": None}),
        (VpnEndpoint, {"max_active_profiles": None}),
        (WorkerNode, {"vpn_enabled": False}),
        (WorkerNode, {"vpn_last_checked_at": NOW - timedelta(minutes=2)}),
    ],
)
async def test_lock_revalidates_and_returns_none_without_alternative(
    session_factory, model, changes,
):
    async with session_factory() as session:
        node = worker("only")
        session.add(node)
        await session.flush()
        target = endpoint(node)
        session.add(target)
        await session.flush()

        async def invalidate(current_session):
            await current_session.execute(
                update(model)
                .where(model.id == (target.id if model is VpnEndpoint else node.id))
                .values(**changes)
                .execution_options(synchronize_session=False)
            )

        session.on_lock = invalidate
        assert await select_public_vpn_endpoint(
            session, now=NOW, health_max_age_seconds=60, lock=True
        ) is None


@pytest.mark.asyncio
async def test_lock_rejects_pending_orm_mutation_without_flushing_it(session_factory):
    async with session_factory() as session:
        node = worker("only")
        session.add(node)
        await session.flush()
        session.add(endpoint(node))
        await session.flush()
        pending = worker("pending")
        session.add(pending)

        with pytest.raises(ValueError, match="pending ORM changes"):
            await select_public_vpn_endpoint(
                session, now=NOW, health_max_age_seconds=60, lock=True
            )
        assert pending in session.new


@pytest.mark.asyncio
async def test_no_candidate_returns_none(session_factory):
    async with session_factory() as session:
        assert await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60) is None
