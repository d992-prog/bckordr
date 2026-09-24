from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import AppSetting
from app.services.vpn_release_readiness import (
    BackupObservation,
    OperationalObservations,
    ReadinessObservation,
)

DIAGNOSTIC_TELEGRAM_TOKEN_KEY = "diagnostic_telegram_token"
DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY = "diagnostic_telegram_chat_id"
DISCOVERY_RUNTIME_SETTING_PREFIX = "discovery_runtime_"
VPN_LIFECYCLE_LAST_RESULT_KEY = "vpn_lifecycle_last_result"
VPN_WATCHDOG_OBSERVATIONS_KEY = "vpn_watchdog_observations_v1"
_VPN_FRIEND_BETA_RELEASE_READY_KEY = "vpn_friend_beta_release_ready_v1"
_VPN_PUBLIC_RELEASE_READY_KEY = "vpn_public_release_ready_v1"
_VPN_WATCHDOG_OBSERVATIONS_MAX_BYTES = 4_096
_VPN_WATCHDOG_OBSERVATIONS_VERSION = 1
_VPN_WATCHDOG_OPERATIONAL_MAX_AGE_SECONDS = 600
_VPN_WATCHDOG_BACKUP_MAX_AGE_SECONDS = 36 * 60 * 60
_VPN_WATCHDOG_OPERATIONAL_KEYS = (
    "system",
    "control",
    "local",
    "public",
    "cabinet",
    "disk",
    "known_hosts",
)
_VPN_WATCHDOG_STATES = {"pass", "warn", "fail"}


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


async def set_vpn_public_release_ready(
    session: AsyncSession,
    release_id: str,
) -> AppSetting:
    return await set_app_setting(session, _VPN_PUBLIC_RELEASE_READY_KEY, release_id)


def _watchdog_timestamp(value: datetime | None) -> str:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("vpn_watchdog_observation_invalid")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _watchdog_observation_payload(
    value: ReadinessObservation | BackupObservation | None,
) -> dict[str, str]:
    if value is None or value.state not in _VPN_WATCHDOG_STATES:
        raise ValueError("vpn_watchdog_observation_invalid")
    return {
        "state": value.state,
        "observed_at": _watchdog_timestamp(value.observed_at),
    }


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("vpn_watchdog_observation_invalid")
        value[key] = item
    return value


def _parse_watchdog_observation(
    value: object,
    *,
    max_age_seconds: int,
) -> ReadinessObservation:
    if not isinstance(value, dict) or set(value) != {"state", "observed_at"}:
        raise ValueError("vpn_watchdog_observation_invalid")
    state = value["state"]
    observed_at = value["observed_at"]
    if state not in _VPN_WATCHDOG_STATES or not isinstance(observed_at, str):
        raise ValueError("vpn_watchdog_observation_invalid")
    if not observed_at.endswith("Z"):
        raise ValueError("vpn_watchdog_observation_invalid")
    timestamp = datetime.fromisoformat(f"{observed_at[:-1]}+00:00")
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("vpn_watchdog_observation_invalid")
    return ReadinessObservation(
        state=state,
        observed_at=timestamp,
        max_age_seconds=max_age_seconds,
    )


async def get_vpn_watchdog_observations(
    session: AsyncSession,
) -> tuple[OperationalObservations | None, BackupObservation | None]:
    with session.no_autoflush:
        raw = await get_app_setting(session, VPN_WATCHDOG_OBSERVATIONS_KEY)
    if raw is None:
        return None, None
    try:
        if len(raw.encode("utf-8")) > _VPN_WATCHDOG_OBSERVATIONS_MAX_BYTES:
            return None, None
        payload = json.loads(raw, object_pairs_hook=_unique_json_object)
        if not isinstance(payload, dict) or set(payload) != {
            "version",
            "operational",
            "backup",
        }:
            return None, None
        if type(payload["version"]) is not int or payload["version"] != 1:
            return None, None
        operational_payload = payload["operational"]
        if not isinstance(operational_payload, dict) or set(
            operational_payload
        ) != set(_VPN_WATCHDOG_OPERATIONAL_KEYS):
            return None, None
        operational_values = {
            name: _parse_watchdog_observation(
                operational_payload[name],
                max_age_seconds=_VPN_WATCHDOG_OPERATIONAL_MAX_AGE_SECONDS,
            )
            for name in _VPN_WATCHDOG_OPERATIONAL_KEYS
        }
        parsed_backup = _parse_watchdog_observation(
            payload["backup"],
            max_age_seconds=_VPN_WATCHDOG_BACKUP_MAX_AGE_SECONDS,
        )
    except (TypeError, UnicodeError, ValueError):
        return None, None
    return OperationalObservations(**operational_values), BackupObservation(
        state=parsed_backup.state,
        observed_at=parsed_backup.observed_at,
        max_age_seconds=parsed_backup.max_age_seconds,
    )


async def set_vpn_watchdog_observations(
    session: AsyncSession,
    operational: OperationalObservations,
    backup: BackupObservation,
) -> AppSetting:
    payload = {
        "version": _VPN_WATCHDOG_OBSERVATIONS_VERSION,
        "operational": {
            name: _watchdog_observation_payload(getattr(operational, name))
            for name in _VPN_WATCHDOG_OPERATIONAL_KEYS
        },
        "backup": _watchdog_observation_payload(backup),
    }
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    if len(raw.encode("utf-8")) > _VPN_WATCHDOG_OBSERVATIONS_MAX_BYTES:
        raise ValueError("vpn_watchdog_observation_invalid")
    return await set_app_setting(session, VPN_WATCHDOG_OBSERVATIONS_KEY, raw)


async def get_vpn_lifecycle_last_result(session: AsyncSession) -> dict[str, object]:
    raw = await get_app_setting(session, VPN_LIFECYCLE_LAST_RESULT_KEY)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


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
