import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import ZoneRule, ZoneStrategy
from app.services.bootstrap import ensure_default_zone_strategies


@pytest.mark.asyncio
async def test_bootstrap_creates_discovered_zone_strategy_presets():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await ensure_default_zone_strategies(session_factory)

    async with session_factory() as session:
        strategies = (
            await session.execute(select(ZoneStrategy).order_by(ZoneStrategy.zone.asc()))
        ).scalars().all()
        rules = (await session.execute(select(ZoneRule).order_by(ZoneRule.name.asc()))).scalars().all()

    strategy_by_zone = {strategy.zone: strategy for strategy in strategies}
    assert set(strategy_by_zone) == {"com", "net", "org"}
    assert strategy_by_zone["com"].timezone_name == "UTC"
    assert strategy_by_zone["net"].name == "Verisign NET Drop Window"
    assert strategy_by_zone["org"].name == "PIR ORG Drop Window"

    rule_by_zone = {strategy.zone: rule for strategy in strategies for rule in rules if rule.zone_strategy_id == strategy.id}
    assert rule_by_zone["com"].schedule_type == "daily"
    assert rule_by_zone["com"].hour == 18
    assert rule_by_zone["com"].minute == 0
    assert rule_by_zone["com"].second == 0
    assert rule_by_zone["com"].window_duration_seconds == 2700
    assert rule_by_zone["net"].hour == 18
    assert rule_by_zone["net"].window_duration_seconds == 2700
    assert rule_by_zone["org"].hour == 15
    assert rule_by_zone["org"].minute == 14
    assert rule_by_zone["org"].second == 30
    assert rule_by_zone["org"].window_duration_seconds == 100

    await ensure_default_zone_strategies(session_factory)

    async with session_factory() as session:
        strategy_count = await session.scalar(select(func.count()).select_from(ZoneStrategy))
        rule_count = await session.scalar(select(func.count()).select_from(ZoneRule))

    assert strategy_count == 3
    assert rule_count == 3
    await engine.dispose()
