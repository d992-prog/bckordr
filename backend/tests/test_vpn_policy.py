from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnControlOperation,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
)
from app.services.vpn_policy import (
    active_vpn_mutation_worker_ids,
    count_device_slots,
    evaluate_vpn_node,
    lock_vpn_subscription,
    lock_vpn_worker,
    select_vpn_node,
    validate_subscription_access,
)
from app.services.attack_runtime import load_attack_available_workers


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def _vpn_worker(name: str, *, checked_at: datetime | None = None) -> WorkerNode:
    return WorkerNode(
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
        vpn_last_checked_at=checked_at,
    )


def test_validate_subscription_access_rejects_expired_naive_timestamp():
    now = datetime(2026, 9, 20, tzinfo=UTC)
    customer = VpnCustomer(status="active")
    subscription = VpnSubscription(
        status="active",
        starts_at=(now - timedelta(days=2)).replace(tzinfo=None),
        expires_at=(now - timedelta(seconds=1)).replace(tzinfo=None),
        max_devices=1,
    )

    assert validate_subscription_access(subscription, customer, device_slots=0, now=now) == (
        "VPN subscription has expired"
    )


def test_validate_subscription_access_enforces_device_limit():
    now = datetime(2026, 9, 20, tzinfo=UTC)
    customer = VpnCustomer(status="active")
    subscription = VpnSubscription(status="active", max_devices=1)

    assert validate_subscription_access(subscription, customer, device_slots=1, now=now) == (
        "VPN device limit reached"
    )


@pytest.mark.asyncio
async def test_vpn_resource_locks_use_for_update():
    captured = []
    subscription = VpnSubscription(id=7, customer_id=1, status="active", max_devices=1)
    worker = _vpn_worker("locked")
    worker.id = 9

    class FakeSession:
        async def scalar(self, statement):
            captured.append(statement)
            return subscription if len(captured) == 1 else worker

    fake_session = FakeSession()
    assert await lock_vpn_subscription(fake_session, 7) is subscription
    assert await lock_vpn_worker(fake_session, 9) is worker
    assert all(statement._for_update_arg is not None for statement in captured)


@pytest.mark.asyncio
async def test_active_vpn_mutation_worker_ids_uses_queued_and_running_jobs(session_factory):
    async with session_factory() as session:
        queued = _vpn_worker("queued-mutation")
        running = _vpn_worker("running-mutation")
        health = _vpn_worker("health-check")
        finished = _vpn_worker("finished-mutation")
        session.add_all([queued, running, health, finished])
        await session.flush()
        session.add_all(
            [
                WorkerMaintenanceJob(worker_id=queued.id, action="vpn_update", status="queued"),
                WorkerMaintenanceJob(worker_id=running.id, action="vpn_restart", status="running"),
                WorkerMaintenanceJob(worker_id=health.id, action="vpn_check", status="running"),
                WorkerMaintenanceJob(worker_id=finished.id, action="vpn_update", status="succeeded"),
            ]
        )
        await session.commit()

        assert await active_vpn_mutation_worker_ids(session) == {queued.id, running.id}


@pytest.mark.asyncio
async def test_active_vpn_mutation_worker_ids_includes_control_reservations(session_factory):
    async with session_factory() as session:
        workers = [_vpn_worker(state) for state in ("queued", "claimed", "uncertain", "done")]
        session.add_all(workers)
        await session.flush()
        for index, (worker, state) in enumerate(
            zip(workers, ("queued", "claimed", "uncertain", "succeeded")),
            1,
        ):
            session.add(
                VpnControlOperation(
                    id=f"40000000-0000-4000-8000-{index:012d}",
                    access_key_id=index,
                    worker_id=worker.id,
                    endpoint_id=index,
                    generation=1,
                    action="provision",
                    request_snapshot={},
                    request_digest=f"{index:064x}",
                    state=state,
                    claim_token=(
                        f"50000000-0000-4000-8000-{index:012d}"
                        if state in {"claimed", "uncertain"}
                        else None
                    ),
                )
            )
        await session.commit()

        assert await active_vpn_mutation_worker_ids(session) == {
            workers[0].id,
            workers[1].id,
            workers[2].id,
        }


@pytest.mark.asyncio
async def test_attack_worker_loader_excludes_active_vpn_mutation_lease(session_factory):
    async with session_factory() as session:
        leased = _vpn_worker("leased")
        free = _vpn_worker("available")
        session.add_all([leased, free])
        await session.flush()
        session.add(
            WorkerMaintenanceJob(worker_id=leased.id, action="vpn_update", status="queued")
        )
        await session.commit()

        available = await load_attack_available_workers(session)

    assert [worker.id for worker in available] == [free.id]


@pytest.mark.asyncio
async def test_count_device_slots_includes_retryable_states_and_can_exclude_key(session_factory):
    async with session_factory() as session:
        customer = VpnCustomer(status="active")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=5)
        session.add(subscription)
        await session.flush()
        keys = [
            VpnAccessKey(subscription_id=subscription.id, status=status)
            for status in ("pending_sync", "syncing", "active", "pending_revoke", "revoked")
        ]
        session.add_all(keys)
        await session.commit()

        assert await count_device_slots(session, subscription.id) == 4
        assert await count_device_slots(session, subscription.id, exclude_key_id=keys[0].id) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status,task_status,blocked", [
    ("running", "running", True), ("planned", "planned", True), ("running", "planned", True),
    ("verifying", "cancelled", True), ("verifying", "succeeded", True), ("verifying", "failed", True),
    ("success", "running", False), ("failed", "queued", False), ("stopped", "planned", False),
])
async def test_vpn_node_selection_respects_domain_work_phase(
    session_factory, run_status, task_status, blocked,
):
    async with session_factory() as session:
        busy = _vpn_worker("busy")
        busy.vpn_role = "drop_worker+vpn_node"
        free = _vpn_worker("free")
        session.add_all([busy, free])
        await session.flush()
        domain = DropDomain(fqdn="target.fr", zone="fr", drop_date=datetime.now(UTC).date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status=run_status,
            planned_start_at=datetime.now(UTC),
            planned_end_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=busy.id,
                status=task_status,
            )
        )
        await session.commit()

        selected = await select_vpn_node(session)
        busy_eligibility = await evaluate_vpn_node(session, busy)

        assert selected is not None
        assert selected.id == (free.id if blocked else busy.id)
        assert busy_eligibility.eligible is not blocked
        assert busy_eligibility.reasons == (
            ("Worker is assigned to an active domain attack",) if blocked else ()
        )


@pytest.mark.asyncio
async def test_select_vpn_node_prefers_fewest_active_keys_then_oldest_check(session_factory):
    now = datetime(2026, 9, 20, tzinfo=UTC)
    async with session_factory() as session:
        loaded = _vpn_worker("loaded", checked_at=now - timedelta(days=2))
        recent = _vpn_worker("recent", checked_at=now)
        oldest = _vpn_worker("oldest", checked_at=now - timedelta(days=1))
        session.add_all([loaded, recent, oldest])
        await session.flush()
        customer = VpnCustomer(status="active")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=2)
        session.add(subscription)
        await session.flush()
        session.add(
            VpnAccessKey(
                subscription_id=subscription.id,
                worker_id=loaded.id,
                status="active",
            )
        )
        await session.commit()

        selected = await select_vpn_node(session)

        assert selected is not None
        assert selected.id == oldest.id
