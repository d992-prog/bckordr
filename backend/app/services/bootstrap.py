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
        "notes": "Preset from observed FRNIC drops: .fr domains drop around minute 32 of an hour; safe window starts at 31:30 local zone time.",
        "rule": {
            "name": "FR hourly 31:30",
            "schedule_type": "hourly",
            "hour": None,
            "minute": 31,
            "second": 30,
            "window_duration_seconds": 95,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: every hour at 31:30 + 95s Europe/Paris.",
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
    {
        "zone": "ae",
        "name": "TDRA AE Daily Drop Window",
        "timezone_name": "Asia/Dubai",
        "notes": "Preset from discovery availability export: .ae domains became available at 03:33:01-03:33:46 Asia/Dubai; safe window starts at 03:32:30.",
        "rule": {
            "name": "AE daily 03:32:30 Asia/Dubai",
            "schedule_type": "daily",
            "hour": 3,
            "minute": 32,
            "second": 30,
            "window_duration_seconds": 100,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 03:32:30-03:34:10 Asia/Dubai.",
        },
    },
    {
        "zone": "se",
        "name": "IIS SE Daily Drop Window",
        "timezone_name": "Europe/Stockholm",
        "notes": "Preset from discovery availability export: .se domains became available at 06:01:18-06:03:42 Europe/Stockholm; safe window starts at 06:00:45.",
        "rule": {
            "name": "SE daily 06:00:45 Europe/Stockholm",
            "schedule_type": "daily",
            "hour": 6,
            "minute": 0,
            "second": 45,
            "window_duration_seconds": 210,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 06:00:45-06:04:15 Europe/Stockholm.",
        },
    },
    {
        "zone": "bg",
        "name": "Register.BG BG Daily Drop Window",
        "timezone_name": "Europe/Sofia",
        "notes": "Preset from discovery availability export: .bg domains became available at 01:43:59-01:49:52 Europe/Sofia; safe window starts at 01:43:30.",
        "rule": {
            "name": "BG daily 01:43:30 Europe/Sofia",
            "schedule_type": "daily",
            "hour": 1,
            "minute": 43,
            "second": 30,
            "window_duration_seconds": 420,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 01:43:30-01:50:30 Europe/Sofia.",
        },
    },
    {
        "zone": "hr",
        "name": "CARNet HR Daily Drop Window",
        "timezone_name": "Europe/Zagreb",
        "notes": "Preset from discovery availability export: .hr domains became available at 05:30:05-05:55:42 Europe/Zagreb; wide safe window until more data is collected.",
        "rule": {
            "name": "HR daily 05:29:30 Europe/Zagreb",
            "schedule_type": "daily",
            "hour": 5,
            "minute": 29,
            "second": 30,
            "window_duration_seconds": 1620,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 05:29:30-05:56:30 Europe/Zagreb.",
        },
    },
    {
        "zone": "ee",
        "name": "EIS EE Daily Drop Window",
        "timezone_name": "Europe/Tallinn",
        "notes": "Preset from discovery availability export: .ee domains became available at 00:05:08-00:06:23 Europe/Tallinn; safe window starts at 00:04:30.",
        "rule": {
            "name": "EE daily 00:04:30 Europe/Tallinn",
            "schedule_type": "daily",
            "hour": 0,
            "minute": 4,
            "second": 30,
            "window_duration_seconds": 140,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 00:04:30-00:06:50 Europe/Tallinn.",
        },
    },
    {
        "zone": "rs",
        "name": "RNIDS RS Daily Drop Window",
        "timezone_name": "Europe/Belgrade",
        "notes": "Preset from discovery availability export: .rs domains became available at 20:15:50-20:16:24 Europe/Belgrade; safe window starts at 20:15:30.",
        "rule": {
            "name": "RS daily 20:15:30 Europe/Belgrade",
            "schedule_type": "daily",
            "hour": 20,
            "minute": 15,
            "second": 30,
            "window_duration_seconds": 80,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 20:15:30-20:16:50 Europe/Belgrade.",
        },
    },
    {
        "zone": "nl",
        "name": "SIDN NL Daily Drop Window",
        "timezone_name": "Europe/Amsterdam",
        "notes": "Preset from discovery availability export: .nl domains mostly became available at 02:00-02:04 Europe/Amsterdam, with one later outlier; safe window starts at 01:59:30.",
        "rule": {
            "name": "NL daily 01:59:30 Europe/Amsterdam",
            "schedule_type": "daily",
            "hour": 1,
            "minute": 59,
            "second": 30,
            "window_duration_seconds": 300,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 01:59:30-02:04:30 Europe/Amsterdam.",
        },
    },
    {
        "zone": "no",
        "name": "Norid NO Daily Drop Window",
        "timezone_name": "Europe/Oslo",
        "notes": "Preset from discovery availability export: .no domains became available at 03:17:02-03:19:44 Europe/Oslo; safe window starts at 03:16:30.",
        "rule": {
            "name": "NO daily 03:16:30 Europe/Oslo",
            "schedule_type": "daily",
            "hour": 3,
            "minute": 16,
            "second": 30,
            "window_duration_seconds": 225,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 03:16:30-03:20:15 Europe/Oslo.",
        },
    },
    {
        "zone": "me",
        "name": "DoMEn ME Daily Drop Window",
        "timezone_name": "Europe/Podgorica",
        "notes": "Preset from discovery availability export: .me domains became available at 19:00:04-19:00:55 Europe/Podgorica; safe window starts at 18:59:30.",
        "rule": {
            "name": "ME daily 18:59:30 Europe/Podgorica",
            "schedule_type": "daily",
            "hour": 18,
            "minute": 59,
            "second": 30,
            "window_duration_seconds": 120,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 18:59:30-19:01:30 Europe/Podgorica.",
        },
    },
    {
        "zone": "mk",
        "name": "MARnet MK Daily Drop Window",
        "timezone_name": "Europe/Skopje",
        "notes": "Preset from discovery availability export: .mk domains became available inside 22:00-22:14 Europe/Skopje; safe window starts at 21:59:30.",
        "rule": {
            "name": "MK daily 21:59:30 Europe/Skopje",
            "schedule_type": "daily",
            "hour": 21,
            "minute": 59,
            "second": 30,
            "window_duration_seconds": 960,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 21:59:30-22:15:30 Europe/Skopje.",
        },
    },
    {
        "zone": "sk",
        "name": "SK-NIC SK Daily Drop Window",
        "timezone_name": "Europe/Bratislava",
        "notes": "Preset from discovery availability export: .sk domains became available inside 02:01-02:13 Europe/Bratislava; safe window starts at 01:59:30.",
        "rule": {
            "name": "SK daily 01:59:30 Europe/Bratislava",
            "schedule_type": "daily",
            "hour": 1,
            "minute": 59,
            "second": 30,
            "window_duration_seconds": 900,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 01:59:30-02:14:30 Europe/Bratislava.",
        },
    },
    {
        "zone": "tr",
        "name": "TRABIS TR Daily Drop Window",
        "timezone_name": "Europe/Istanbul",
        "notes": "Preset from discovery availability export: .tr domains became available at 00:49:47-00:50:29 Europe/Istanbul; safe window starts at 00:49:30.",
        "rule": {
            "name": "TR daily 00:49:30 Europe/Istanbul",
            "schedule_type": "daily",
            "hour": 0,
            "minute": 49,
            "second": 30,
            "window_duration_seconds": 100,
            "priority": 100,
            "execution_profile_mode": "flat",
            "notes": "Observed safe window: 00:49:30-00:51:10 Europe/Istanbul.",
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
