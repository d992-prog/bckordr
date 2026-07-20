from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AppSetting

DIAGNOSTIC_TELEGRAM_TOKEN_KEY = "diagnostic_telegram_token"
DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY = "diagnostic_telegram_chat_id"
DISCOVERY_RUNTIME_SETTING_PREFIX = "discovery_runtime_"


@dataclass(frozen=True)
class DiscoveryRuntimeSettings:
    discovery_enabled: bool
    discovery_worker_enabled: bool
    discovery_local_fallback_enabled: bool
    discovery_scheduler_interval_seconds: float
    discovery_batch_size: int
    discovery_concurrency: int
    discovery_timeout_seconds: float
    discovery_worker_task_stale_seconds: int
    worker_discovery_concurrency: int
    worker_discovery_poll_interval_seconds: float


async def get_app_setting(session: AsyncSession, key: str) -> str | None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def set_app_setting(session: AsyncSession, key: str, value: str | None) -> AppSetting:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = AppSetting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
    await session.flush()
    return setting


async def get_diagnostic_telegram_settings(session: AsyncSession) -> tuple[str | None, str | None]:
    token = await get_app_setting(session, DIAGNOSTIC_TELEGRAM_TOKEN_KEY)
    chat_id = await get_app_setting(session, DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY)
    return token, chat_id


def _setting_key(name: str) -> str:
    return f"{DISCOVERY_RUNTIME_SETTING_PREFIX}{name}"


def _parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_int(value: str | None, default: int, *, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except ValueError:
        return default
    return min(max(parsed, minimum), maximum)


def _parse_float(value: str | None, default: float, *, minimum: float, maximum: float) -> float:
    if value is None:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    return min(max(parsed, minimum), maximum)


async def get_discovery_runtime_settings(session: AsyncSession, settings: Settings) -> DiscoveryRuntimeSettings:
    keys = [
        "discovery_enabled",
        "discovery_worker_enabled",
        "discovery_local_fallback_enabled",
        "discovery_scheduler_interval_seconds",
        "discovery_batch_size",
        "discovery_concurrency",
        "discovery_timeout_seconds",
        "discovery_worker_task_stale_seconds",
        "worker_discovery_concurrency",
        "worker_discovery_poll_interval_seconds",
    ]
    result = await session.execute(select(AppSetting).where(AppSetting.key.in_([_setting_key(key) for key in keys])))
    values = {item.key.removeprefix(DISCOVERY_RUNTIME_SETTING_PREFIX): item.value for item in result.scalars().all()}
    return DiscoveryRuntimeSettings(
        discovery_enabled=_parse_bool(values.get("discovery_enabled"), settings.discovery_enabled),
        discovery_worker_enabled=_parse_bool(values.get("discovery_worker_enabled"), settings.discovery_worker_enabled),
        discovery_local_fallback_enabled=_parse_bool(
            values.get("discovery_local_fallback_enabled"),
            settings.discovery_local_fallback_enabled,
        ),
        discovery_scheduler_interval_seconds=_parse_float(
            values.get("discovery_scheduler_interval_seconds"),
            settings.discovery_scheduler_interval_seconds,
            minimum=0.25,
            maximum=3600.0,
        ),
        discovery_batch_size=_parse_int(
            values.get("discovery_batch_size"),
            settings.discovery_batch_size,
            minimum=1,
            maximum=1000,
        ),
        discovery_concurrency=_parse_int(
            values.get("discovery_concurrency"),
            settings.discovery_concurrency,
            minimum=1,
            maximum=500,
        ),
        discovery_timeout_seconds=_parse_float(
            values.get("discovery_timeout_seconds"),
            settings.discovery_timeout_seconds,
            minimum=0.25,
            maximum=60.0,
        ),
        discovery_worker_task_stale_seconds=_parse_int(
            values.get("discovery_worker_task_stale_seconds"),
            settings.discovery_worker_task_stale_seconds,
            minimum=10,
            maximum=3600,
        ),
        worker_discovery_concurrency=_parse_int(
            values.get("worker_discovery_concurrency"),
            settings.worker_discovery_concurrency,
            minimum=1,
            maximum=128,
        ),
        worker_discovery_poll_interval_seconds=_parse_float(
            values.get("worker_discovery_poll_interval_seconds"),
            settings.worker_discovery_poll_interval_seconds,
            minimum=0.1,
            maximum=60.0,
        ),
    )


async def set_discovery_runtime_settings(
    session: AsyncSession,
    settings: Settings,
    values: dict[str, object],
) -> DiscoveryRuntimeSettings:
    allowed_keys = {
        "discovery_enabled",
        "discovery_worker_enabled",
        "discovery_local_fallback_enabled",
        "discovery_scheduler_interval_seconds",
        "discovery_batch_size",
        "discovery_concurrency",
        "discovery_timeout_seconds",
        "discovery_worker_task_stale_seconds",
        "worker_discovery_concurrency",
        "worker_discovery_poll_interval_seconds",
    }
    for key, value in values.items():
        if key not in allowed_keys:
            continue
        await set_app_setting(session, _setting_key(key), str(value).lower() if isinstance(value, bool) else str(value))
    return await get_discovery_runtime_settings(session, settings)
