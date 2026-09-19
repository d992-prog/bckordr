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
    VpnSubscription,
    WorkerNode,
    WorkerTask,
)
from app.services.vpn_policy import (
    count_device_slots,
    evaluate_vpn_node,
    select_vpn_node,
    validate_subscription_access,
)


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
async def test_select_vpn_node_excludes_worker_with_active_attack(session_factory):
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
            status="running",
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
                status="running",
            )
        )
        await session.commit()

        selected = await select_vpn_node(session)
        busy_eligibility = await evaluate_vpn_node(session, busy)

        assert selected is not None
        assert selected.id == free.id
        assert busy_eligibility.eligible is False
        assert busy_eligibility.reasons == ("Worker is assigned to an active domain attack",)


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
