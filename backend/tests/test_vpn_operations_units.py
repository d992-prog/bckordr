from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DEPLOY = ROOT / "deploy"
BACKEND = "/opt/domain-drop-catcher/backend"
PYTHON = f"{BACKEND}/.venv/bin/python"
ENV_FILE = f"{BACKEND}/.env"


def _unit(name: str) -> ConfigParser:
    parser = ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    with (DEPLOY / name).open(encoding="utf-8") as source:
        parser.read_file(source)
    return parser


@pytest.mark.parametrize(
    ("name", "module", "runtime", "read_write_path"),
    (
        (
            "veltrix-backup.service",
            "app.operations.backup",
            "75min",
            "/var/backups/domain-drop-catcher",
        ),
        (
            "veltrix-watchdog.service",
            "app.operations.watchdog",
            "2min",
            "/var/lib/veltrix-watchdog",
        ),
    ),
)
def test_operations_services_are_root_only_hardened_oneshots(
    name: str,
    module: str,
    runtime: str,
    read_write_path: str,
) -> None:
    service = _unit(name)["Service"]

    assert service["Type"] == "oneshot"
    assert service["User"] == "root"
    assert service["Group"] == "root"
    assert service["WorkingDirectory"] == BACKEND
    assert service["EnvironmentFile"] == ENV_FILE
    assert service["ExecStart"] == f"{PYTHON} -m {module}"
    assert service["UMask"] == "0077"
    assert service["RuntimeMaxSec"] == runtime
    assert service["NoNewPrivileges"] == "true"
    assert service["ProtectSystem"] == "strict"
    assert service["ProtectHome"] == "true"
    assert service["PrivateTmp"] == "true"
    assert service["PrivateDevices"] == "true"
    assert service["ProtectKernelTunables"] == "true"
    assert service["ProtectKernelModules"] == "true"
    assert service["ProtectControlGroups"] == "true"
    assert service["RestrictSUIDSGID"] == "true"
    assert service["LockPersonality"] == "true"
    assert service["RestrictRealtime"] == "true"
    assert service["ReadWritePaths"] == read_write_path
    assert "/bin/sh" not in service["ExecStart"]
    assert "-c" not in service["ExecStart"].split()
    assert "TELEGRAM" not in service["ExecStart"]
    assert "DB_URL" not in service["ExecStart"]


def test_backup_service_exposes_only_required_backup_paths() -> None:
    service = _unit("veltrix-backup.service")["Service"]

    assert service["ReadOnlyPaths"].split() == [
        ENV_FILE,
        "/etc/systemd/system/domain-drop-control.service",
        "/etc/nginx",
        "/opt/domain-drop-catcher/frontend/dist",
    ]
    assert service["Environment"].split() == [
        "VPN_BACKUP_DIRECTORY=/var/backups/domain-drop-catcher",
        f"VPN_BACKUP_ENV_FILE={ENV_FILE}",
        "VPN_BACKUP_SYSTEMD_UNIT=/etc/systemd/system/domain-drop-control.service",
        "VPN_BACKUP_NGINX_DIRECTORY=/etc/nginx",
        "VPN_BACKUP_FRONTEND_DIST=/opt/domain-drop-catcher/frontend/dist",
    ]


def test_watchdog_service_owns_only_its_private_state_directory() -> None:
    service = _unit("veltrix-watchdog.service")["Service"]

    assert service["StateDirectory"] == "veltrix-watchdog"
    assert service["StateDirectoryMode"] == "0700"
    assert service["Environment"] == (
        "VPN_WATCHDOG_STATE_PATH=/var/lib/veltrix-watchdog/state.json"
    )
    assert service["ReadOnlyPaths"].split() == [
        ENV_FILE,
        "-/var/backups/domain-drop-catcher",
    ]


@pytest.mark.parametrize(
    ("name", "service", "calendar", "jitter"),
    (
        (
            "veltrix-backup.timer",
            "veltrix-backup.service",
            "*-*-* 03:10:00 UTC",
            "20min",
        ),
        (
            "veltrix-watchdog.timer",
            "veltrix-watchdog.service",
            "*-*-* *:00/5:00 UTC",
            "30s",
        ),
    ),
)
def test_operations_timers_are_persistent_jittered_and_non_overlapping(
    name: str,
    service: str,
    calendar: str,
    jitter: str,
) -> None:
    timer = _unit(name)["Timer"]

    assert timer["Unit"] == service
    assert timer["OnCalendar"] == calendar
    assert timer["RandomizedDelaySec"] == jitter
    assert timer["Persistent"] == "true"
    assert _unit(name)["Install"]["WantedBy"] == "timers.target"
    assert _unit(service)["Service"]["Type"] == "oneshot"
