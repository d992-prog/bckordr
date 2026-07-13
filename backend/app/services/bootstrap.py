from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import User, ZoneRule, ZoneStrategy
from app.services.security import hash_password


async def ensure_owner_account(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not settings.owner_login or not settings.owner_password:
        return

    async with session_factory() as session:
        result = await session.execute(select(User).where(User.username == settings.owner_login))
        owner = result.scalar_one_or_none()
        if owner is None:
            session.add(
                User(
                    username=settings.owner_login,
                    password_hash=hash_password(settings.owner_password),
                    role="owner",
                    status="approved",
                    language="ru",
                )
            )
        else:
            owner.role = "owner"
            owner.status = "approved"
            owner.deleted_at = None
            owner.status_message = None
        await session.commit()


DEFAULT_ZONE_STRATEGY_PRESETS = (
    {
        "zone": "fr",
        "name": "FRNIC FR Hourly Drop Window",
        "timezone_name": "Europe/Paris",
        "notes": "Preset from observed FRNIC drops: .fr domains drop around minute 32 of an hour; safe window starts at 31:59 local zone time.",
        "rule": {
            "name": "FR hourly 31:59",
            "schedule_type": "hourly",
            "hour": None,
            "minute": 31,
            "second": 59,
            "window_duration_seconds": 61,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: every hour at 31:59 + 61s Europe/Paris.",
        },
    },
    {
        "zone": "com",
        "name": "Verisign COM Drop Window",
        "timezone_name": "UTC",
        "notes": "Preset from DropCatch RDAP registration analysis: .com registrations cluster in 18:00-18:43 UTC; safe window 17:59:30-18:45 UTC.",
        "rule": {
            "name": "COM Verisign 18:00 UTC",
            "schedule_type": "daily",
            "hour": 18,
            "minute": 0,
            "second": 0,
            "window_duration_seconds": 2700,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 18:00:00-18:45:00 UTC.",
        },
    },
    {
        "zone": "net",
        "name": "Verisign NET Drop Window",
        "timezone_name": "UTC",
        "notes": "Preset from DropCatch RDAP registration analysis: .net registrations cluster in 18:00-18:43 UTC; safe window 17:59:30-18:45 UTC.",
        "rule": {
            "name": "NET Verisign 18:00 UTC",
            "schedule_type": "daily",
            "hour": 18,
            "minute": 0,
            "second": 0,
            "window_duration_seconds": 2700,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 18:00:00-18:45:00 UTC.",
        },
    },
    {
        "zone": "org",
        "name": "PIR ORG Drop Window",
        "timezone_name": "UTC",
        "notes": "Preset from DropCatch RDAP registration analysis: .org registrations cluster inside 15:15 UTC; safe window 15:14:30-15:16:10 UTC.",
        "rule": {
            "name": "ORG PIR 15:15 UTC",
            "schedule_type": "daily",
            "hour": 15,
            "minute": 14,
            "second": 30,
            "window_duration_seconds": 100,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 15:14:30-15:16:10 UTC.",
        },
    },
)


def get_zone_strategy_preset(zone: str) -> dict | None:
    normalized_zone = zone.strip().lower().lstrip(".")
    return next((preset for preset in DEFAULT_ZONE_STRATEGY_PRESETS if preset["zone"] == normalized_zone), None)


async def ensure_zone_strategy_preset(session: AsyncSession, zone: str) -> ZoneStrategy:
    preset = get_zone_strategy_preset(zone)
    if preset is None:
        raise ValueError(f"No preset configured for zone {zone}")

    existing = await session.scalar(
        select(ZoneStrategy).where(
            ZoneStrategy.zone == preset["zone"],
            ZoneStrategy.is_active.is_(True),
        )
    )
    if existing is not None:
        rules = (
            await session.execute(
                select(ZoneRule).where(
                    ZoneRule.zone_strategy_id == existing.id,
                    ZoneRule.is_enabled.is_(True),
                )
            )
        ).scalars().all()
        if rules:
            return existing

    strategy = existing
    if strategy is None:
        strategy = ZoneStrategy(
            zone=preset["zone"],
            name=preset["name"],
            timezone_name=preset["timezone_name"],
            rule_resolution_mode="priority",
            default_registrar_slug="gandi",
            is_active=True,
            notes=preset["notes"],
        )
        session.add(strategy)
        await session.flush()

    rule = preset["rule"]
    session.add(
        ZoneRule(
            zone_strategy_id=strategy.id,
            name=rule["name"],
            schedule_type=rule["schedule_type"],
            hour=rule["hour"],
            minute=rule["minute"],
            second=rule["second"],
            window_duration_seconds=rule["window_duration_seconds"],
            priority=rule["priority"],
            execution_profile_mode=rule["execution_profile_mode"],
            notes=rule["notes"],
        )
    )
    await session.flush()
    return strategy


async def ensure_default_zone_strategies(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for preset in DEFAULT_ZONE_STRATEGY_PRESETS:
            await ensure_zone_strategy_preset(session, preset["zone"])
        await session.commit()
