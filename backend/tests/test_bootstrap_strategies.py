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
    assert set(strategy_by_zone) == {
        "ae",
        "bg",
        "com",
        "ee",
        "fr",
        "hr",
        "me",
        "mk",
        "net",
        "nl",
        "no",
        "org",
        "rs",
        "se",
        "sk",
        "tr",
        "us",
    }
    assert strategy_by_zone["ae"].timezone_name == "Asia/Dubai"
    assert strategy_by_zone["bg"].timezone_name == "Europe/Sofia"
    assert strategy_by_zone["com"].timezone_name == "UTC"
    assert strategy_by_zone["ee"].timezone_name == "Europe/Tallinn"
    assert strategy_by_zone["fr"].timezone_name == "Europe/Paris"
    assert strategy_by_zone["hr"].timezone_name == "Europe/Zagreb"
    assert strategy_by_zone["me"].timezone_name == "Europe/Podgorica"
    assert strategy_by_zone["mk"].timezone_name == "Europe/Skopje"
    assert strategy_by_zone["net"].name == "Verisign NET Drop Window"
    assert strategy_by_zone["nl"].timezone_name == "Europe/Amsterdam"
    assert strategy_by_zone["no"].timezone_name == "Europe/Oslo"
    assert strategy_by_zone["org"].name == "PIR ORG Drop Window"
    assert strategy_by_zone["rs"].timezone_name == "Europe/Belgrade"
    assert strategy_by_zone["se"].timezone_name == "Europe/Stockholm"
    assert strategy_by_zone["sk"].timezone_name == "Europe/Bratislava"
    assert strategy_by_zone["tr"].timezone_name == "Europe/Istanbul"
    assert strategy_by_zone["us"].timezone_name == "UTC"

    rule_by_zone = {strategy.zone: rule for strategy in strategies for rule in rules if rule.zone_strategy_id == strategy.id}
    assert rule_by_zone["ae"].schedule_type == "daily"
    assert rule_by_zone["ae"].hour == 3
    assert rule_by_zone["ae"].minute == 32
    assert rule_by_zone["ae"].second == 30
    assert rule_by_zone["ae"].window_duration_seconds == 100
    assert rule_by_zone["bg"].hour == 1
    assert rule_by_zone["bg"].minute == 43
    assert rule_by_zone["bg"].second == 30
    assert rule_by_zone["bg"].window_duration_seconds == 420
    assert rule_by_zone["com"].schedule_type == "daily"
    assert rule_by_zone["com"].hour == 18
    assert rule_by_zone["com"].minute == 0
    assert rule_by_zone["com"].second == 0
    assert rule_by_zone["com"].window_duration_seconds == 2700
    assert rule_by_zone["ee"].hour == 0
    assert rule_by_zone["ee"].minute == 4
    assert rule_by_zone["ee"].second == 30
    assert rule_by_zone["ee"].window_duration_seconds == 140
    assert rule_by_zone["fr"].schedule_type == "hourly"
    assert rule_by_zone["fr"].hour is None
    assert rule_by_zone["fr"].minute == 31
    assert rule_by_zone["fr"].second == 30
    assert rule_by_zone["fr"].window_duration_seconds == 95
    assert rule_by_zone["hr"].hour == 5
    assert rule_by_zone["hr"].minute == 29
    assert rule_by_zone["hr"].second == 30
    assert rule_by_zone["hr"].window_duration_seconds == 1620
    assert rule_by_zone["me"].hour == 18
    assert rule_by_zone["me"].minute == 59
    assert rule_by_zone["me"].second == 30
    assert rule_by_zone["me"].window_duration_seconds == 120
    assert rule_by_zone["mk"].hour == 21
    assert rule_by_zone["mk"].minute == 59
    assert rule_by_zone["mk"].second == 30
    assert rule_by_zone["mk"].window_duration_seconds == 960
    assert rule_by_zone["net"].hour == 18
    assert rule_by_zone["net"].window_duration_seconds == 2700
    assert rule_by_zone["nl"].hour == 1
    assert rule_by_zone["nl"].minute == 59
    assert rule_by_zone["nl"].second == 30
    assert rule_by_zone["nl"].window_duration_seconds == 300
    assert rule_by_zone["no"].hour == 3
    assert rule_by_zone["no"].minute == 16
    assert rule_by_zone["no"].second == 30
    assert rule_by_zone["no"].window_duration_seconds == 225
    assert rule_by_zone["org"].hour == 15
    assert rule_by_zone["org"].minute == 14
    assert rule_by_zone["org"].second == 30
    assert rule_by_zone["org"].window_duration_seconds == 100
    assert rule_by_zone["rs"].hour == 20
    assert rule_by_zone["rs"].minute == 15
    assert rule_by_zone["rs"].second == 30
    assert rule_by_zone["rs"].window_duration_seconds == 80
    assert rule_by_zone["se"].schedule_type == "daily"
    assert rule_by_zone["se"].hour == 6
    assert rule_by_zone["se"].minute == 0
    assert rule_by_zone["se"].second == 45
    assert rule_by_zone["se"].window_duration_seconds == 210
    assert rule_by_zone["sk"].hour == 1
    assert rule_by_zone["sk"].minute == 59
    assert rule_by_zone["sk"].second == 30
    assert rule_by_zone["sk"].window_duration_seconds == 900
    assert rule_by_zone["tr"].hour == 0
    assert rule_by_zone["tr"].minute == 49
    assert rule_by_zone["tr"].second == 30
    assert rule_by_zone["tr"].window_duration_seconds == 100
    assert rule_by_zone["us"].hour == 0
    assert rule_by_zone["us"].minute == 1
    assert rule_by_zone["us"].second == 0
    assert rule_by_zone["us"].window_duration_seconds == 120

    await ensure_default_zone_strategies(session_factory)

    async with session_factory() as session:
        strategy_count = await session.scalar(select(func.count()).select_from(ZoneStrategy))
        rule_count = await session.scalar(select(func.count()).select_from(ZoneRule))

    assert strategy_count == 17
    assert rule_count == 17
    await engine.dispose()
