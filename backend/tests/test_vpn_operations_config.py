from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)


def test_operations_defaults_are_disabled_and_fail_closed() -> None:
    settings = _settings()

    assert settings.vpn_backup_enabled is False
    assert settings.vpn_backup_directory == "/var/backups/domain-drop-catcher"
    assert settings.vpn_backup_retention == 7
    assert settings.vpn_watchdog_enabled is False
    assert settings.vpn_alert_telegram_user_id == ""
    assert settings.vpn_watchdog_state_path == "/var/lib/veltrix-watchdog/state.json"


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("VPN_BACKUP_DIRECTORY", "relative/backups"),
        ("VPN_BACKUP_DIRECTORY", ""),
        ("VPN_WATCHDOG_STATE_PATH", "relative/state.json"),
        ("VPN_WATCHDOG_STATE_PATH", ""),
    ),
)
def test_operations_paths_must_be_absolute(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(**{field: value})


@pytest.mark.parametrize("retention", (1, 32))
def test_backup_retention_is_bounded(retention: int) -> None:
    with pytest.raises(ValidationError):
        _settings(VPN_BACKUP_RETENTION=retention)


@pytest.mark.parametrize(
    "user_id",
    ("", "abc", "0", "-1", "12.3", "١٢٣"),
)
def test_enabled_watchdog_requires_positive_numeric_telegram_user_id(
    user_id: str,
) -> None:
    with pytest.raises(ValidationError):
        _settings(
            VPN_WATCHDOG_ENABLED=True,
            VPN_ALERT_TELEGRAM_USER_ID=user_id,
        )


def test_enabled_watchdog_accepts_positive_numeric_telegram_user_id() -> None:
    settings = _settings(
        VPN_WATCHDOG_ENABLED=True,
        VPN_ALERT_TELEGRAM_USER_ID="123456789",
    )

    assert settings.vpn_watchdog_enabled is True
    assert settings.vpn_alert_telegram_user_id == "123456789"
