from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.services.vpn_customer_lifecycle import (
    VpnCustomerArchiveConflictError,
    stage_vpn_customer_archive,
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


async def seed_customer(
    session: AsyncSession,
) -> tuple[
    VpnCustomer,
    VpnSubscription,
    VpnSubscription,
    VpnSubscription,
    VpnAccessKey,
    VpnAccessKey,
]:
    customer = VpnCustomer(telegram_user_id="archive-me", status="active")
    session.add(customer)
    await session.flush()
    active = VpnSubscription(customer_id=customer.id, status="active", max_devices=2)
    trial = VpnSubscription(customer_id=customer.id, status="trial", max_devices=1)
    expired = VpnSubscription(customer_id=customer.id, status="expired", max_devices=1)
    session.add_all([active, trial, expired])
    await session.flush()
    active_key = VpnAccessKey(
        subscription_id=active.id,
        status="active",
        config_uri="vless://active",
    )
    revoked_key = VpnAccessKey(
        subscription_id=expired.id,
        status="revoked",
        config_uri=None,
    )
    session.add_all([active_key, revoked_key])
    await session.commit()
    return customer, active, trial, expired, active_key, revoked_key


@pytest.mark.asyncio
async def test_stage_archive_blocks_customer_subscriptions_and_usable_keys(session_factory):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        customer, active, trial, expired, active_key, revoked_key = await seed_customer(session)

        result = await stage_vpn_customer_archive(session, customer.id, now=now)
        await session.commit()

        assert customer.status == "archived"
        assert active.status == "disabled"
        assert trial.status == "disabled"
        assert expired.status == "expired"
        assert active_key.status == "pending_revoke"
        assert revoked_key.status == "revoked"
        assert result.disabled_subscription_count == 2
        assert [item.id for item in result.access_keys] == [active_key.id]


@pytest.mark.asyncio
async def test_stage_archive_rejects_an_already_archived_customer(session_factory):
    async with session_factory() as session:
        customer, *_ = await seed_customer(session)
        customer.status = "archived"
        await session.commit()

        with pytest.raises(VpnCustomerArchiveConflictError, match="already archived"):
            await stage_vpn_customer_archive(session, customer.id)
