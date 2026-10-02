from __future__ import annotations

import asyncio
import hashlib
import importlib
import inspect
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

import httpx
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db.session import create_vpn_control_database
from app.operations.backup import validated_latest_success_at
from app.services.app_settings import set_vpn_watchdog_observations
from app.services.vpn_node_transport import validate_known_hosts_file
from app.services.vpn_portal_auth import public_origin
from app.services.vpn_release_readiness import (
    BackupObservation,
    OperationalObservations,
    ReadinessObservation,
    evaluate_operational_checks,
    evaluate_release_readiness,
    load_release_readiness_snapshot,
)

SAFE_CHECK_CODES = frozenset(
    {
        "aggregate_capacity",
        "backup_health",
        "cabinet_health",
        "control_database_unavailable",
        "control_health",
        "control_operations",
        "disk_health",
        "dispatch",
        "endpoint_capacity",
        "endpoint_configuration",
        "endpoint_external_proof",
        "endpoint_health",
        "endpoint_redundancy",
        "known_hosts",
        "local_health",
        "maintenance",
        "payment_disabled",
        "portal_public_access",
        "public_health",
        "public_trial_disabled",
        "public_trial_plan",
        "release_id",
        "system_health",
        "worker_active",
        "worker_health",
    }
)
TELEGRAM_TIMEOUT_SECONDS = 3.0
TELEGRAM_RESPONSE_BYTE_LIMIT = 4096
STATE_FILE_BYTE_LIMIT = 4096
CHECK_TIMEOUT_SECONDS = 3.0
HTTP_RESPONSE_BYTE_LIMIT = 4096
OPERATIONAL_MAX_AGE_SECONDS = 600
BACKUP_MAX_AGE_SECONDS = 36 * 60 * 60
DATABASE_TIMEOUT_SECONDS = 10.0
SERVICE_IDENTITY_BYTE_LIMIT = 512


class AlertStateError(RuntimeError):
    pass


class TelegramDeliveryError(RuntimeError):
    pass


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req,
        fp,
        code,
        msg,
        headers,
        newurl,
    ) -> None:
        return None


@dataclass(frozen=True, slots=True)
class AlertState:
    digest: str
    failing_codes: tuple[str, ...]
    notified: bool


def build_alert_state(
    failing_codes: Iterable[str],
    *,
    notified: bool,
) -> AlertState:
    try:
        codes = tuple(sorted(set(failing_codes)))
    except (TypeError, ValueError):
        raise ValueError("watchdog_check_code_invalid") from None
    if any(not isinstance(code, str) or code not in SAFE_CHECK_CODES for code in codes):
        raise ValueError("watchdog_check_code_invalid")
    digest = hashlib.sha256("\n".join(codes).encode()).hexdigest()
    return AlertState(digest=digest, failing_codes=codes, notified=notified)


def _read_alert_state(path: str | Path) -> tuple[AlertState, bool]:
    healthy = build_alert_state((), notified=True)
    try:
        with Path(path).open("rb") as state_file:
            raw_state = state_file.read(STATE_FILE_BYTE_LIMIT + 1)
        if len(raw_state) > STATE_FILE_BYTE_LIMIT:
            raise ValueError("watchdog_state_invalid")
        payload = json.loads(raw_state)
        codes = payload["failing_codes"]
        notified = payload["notified"]
        if not isinstance(codes, list) or not isinstance(notified, bool):
            raise TypeError("watchdog_state_invalid")
        state = build_alert_state(codes, notified=notified)
        if payload["digest"] != state.digest:
            raise ValueError("watchdog_state_invalid")
        return state, True
    except (KeyError, OSError, TypeError, ValueError):
        return healthy, False


def load_alert_state(path: str | Path) -> AlertState:
    return _read_alert_state(path)[0]


def _fsync_directory(directory: Path) -> None:
    if os.name != "posix":
        return
    directory_fd = os.open(
        directory,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def save_alert_state(path: str | Path, state: AlertState) -> None:
    destination = Path(path)
    payload = json.dumps(
        {
            "digest": state.digest,
            "failing_codes": list(state.failing_codes),
            "notified": state.notified,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    fd: int | None = None
    temporary_name: str | None = None
    try:
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            dir=str(destination.parent),
        )
        os.chmod(temporary_name, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        os.chmod(destination, 0o600)
        _fsync_directory(destination.parent)
    except BaseException as error:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if temporary_name is not None:
            try:
                os.unlink(temporary_name)
            except OSError:
                pass
        if isinstance(error, Exception):
            raise AlertStateError("watchdog_state_write_failed") from None
        raise


def _valid_user_id(user_id: str) -> bool:
    return user_id.isascii() and user_id.isdigit() and int(user_id) > 0


def _telegram_credentials(environ: Mapping[str, str]) -> tuple[str, str]:
    token = environ.get("VPN_TELEGRAM_BOT_TOKEN", "")
    user_id = environ.get("VPN_ALERT_TELEGRAM_USER_ID", "")
    if not token or not _valid_user_id(user_id):
        raise ValueError("watchdog_telegram_credentials_invalid")
    return token, user_id


def send_telegram_message(
    token: str,
    user_id: str,
    message: str,
    *,
    opener: OpenerDirector | None = None,
) -> None:
    try:
        if not token or not _valid_user_id(user_id):
            raise ValueError("watchdog_telegram_credentials_invalid")
        request = Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=urlencode({"chat_id": user_id, "text": message}).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        client = opener if opener is not None else build_opener(_NoRedirectHandler())
        with client.open(request, timeout=TELEGRAM_TIMEOUT_SECONDS) as response:
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            body = response.read(TELEGRAM_RESPONSE_BYTE_LIMIT + 1)
        if status != 200 or len(body) > TELEGRAM_RESPONSE_BYTE_LIMIT:
            raise ValueError("watchdog_telegram_response_invalid")
        payload = json.loads(body)
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise ValueError("watchdog_telegram_response_invalid")
    except Exception:  # noqa: BLE001 - redact every transport failure.
        raise TelegramDeliveryError("watchdog_telegram_delivery_failed") from None


def _failure_message(codes: tuple[str, ...]) -> str:
    return (
        "Veltrix VPN: мониторинг обнаружил устойчивую проблему.\n"
        "Подключение может работать нестабильно. "
        "Проверьте раздел «Готовность» в панели.\n"
        f"Коды проверки: {', '.join(codes)}"
    )


_RECOVERY_MESSAGE = (
    "Veltrix VPN: работа сервиса восстановлена.\n"
    "Проверки снова проходят. Дополнительных действий не требуется."
)


def run_alert_cycle(
    failing_codes: Iterable[str],
    *,
    state_path: str | Path,
    environ: Mapping[str, str] | None = None,
    sender: Callable[[str, str, str], None] | None = None,
) -> AlertState:
    environment = os.environ if environ is None else environ
    delivery = send_telegram_message if sender is None else sender
    previous, state_valid = _read_alert_state(state_path)
    current = build_alert_state(failing_codes, notified=False)

    if current.failing_codes:
        if state_valid and previous.digest == current.digest:
            if previous.notified:
                return previous
            pending = current
            message = _failure_message(current.failing_codes)
        elif state_valid and previous.notified and previous.failing_codes:
            reported = build_alert_state(current.failing_codes, notified=True)
            save_alert_state(state_path, reported)
            return reported
        else:
            save_alert_state(state_path, current)
            return current
    elif previous.failing_codes:
        healthy = build_alert_state((), notified=True)
        if not previous.notified:
            save_alert_state(state_path, healthy)
            return healthy
        pending = build_alert_state((), notified=False)
        message = _RECOVERY_MESSAGE
    elif not previous.notified:
        pending = previous
        message = _RECOVERY_MESSAGE
    else:
        healthy = build_alert_state((), notified=True)
        if not state_valid:
            save_alert_state(state_path, healthy)
        return healthy

    save_alert_state(state_path, pending)
    try:
        token, user_id = _telegram_credentials(environment)
        delivery(token, user_id, message)
    except Exception:  # noqa: BLE001 - any sender failure must remain retryable.
        return pending

    notified = build_alert_state(current.failing_codes, notified=True)
    save_alert_state(state_path, notified)
    return notified


def _readiness_observation(state: str, observed_at: datetime) -> ReadinessObservation:
    return ReadinessObservation(
        state=state,  # type: ignore[arg-type]
        observed_at=observed_at,
        max_age_seconds=OPERATIONAL_MAX_AGE_SECONDS,
    )


def _require_local_database_when_tls_disabled(
    settings: Settings,
    environ: Mapping[str, str],
) -> None:
    if environ.get("PGSSLMODE", "").strip().lower() != "disable":
        return
    url = make_url(settings.db_url)
    query_host = url.query.get("host")
    local_hosts = {"localhost", "127.0.0.1", "::1"}

    def local_host(value: object) -> bool:
        return bool(
            isinstance(value, str)
            and value
            and "," not in value
            and (value.startswith("/") or value in local_hosts)
        )

    if (
        not url.drivername.startswith("postgresql")
        or url.host not in {None, "", *local_hosts}
        or ("host" in url.query and not local_host(query_host))
        or (
            "host" not in url.query
            and url.host in {None, ""}
            and not local_host(environ.get("PGHOST"))
        )
        or any(key.lower() not in {"host", "port"} for key in url.query)
    ):
        raise ValueError("watchdog_database_transport_invalid")


def collect_system_observations(
    observed_at: datetime,
    *,
    runner: Callable[..., Any] = subprocess.run,
) -> tuple[ReadinessObservation, ReadinessObservation]:
    commands = (
        ["/usr/bin/systemctl", "is-system-running", "--quiet"],
        [
            "/usr/bin/systemctl",
            "is-active",
            "--quiet",
            "domain-drop-control.service",
        ],
    )
    observations: list[ReadinessObservation] = []
    for command in commands:
        try:
            result = runner(
                command,
                check=False,
                shell=False,
                timeout=CHECK_TIMEOUT_SECONDS,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            state = "pass" if result.returncode == 0 else "fail"
        except Exception:  # noqa: BLE001 - every local failure is a static state.
            state = "fail"
        observations.append(_readiness_observation(state, observed_at))
    return observations[0], observations[1]


async def _http_observation(
    client: httpx.AsyncClient,
    url: str,
    observed_at: datetime,
    *,
    require_health_json: bool,
    probe_timeout: Callable[[float], Any] = asyncio.timeout,
) -> ReadinessObservation:
    state = "fail"
    try:
        async with probe_timeout(CHECK_TIMEOUT_SECONDS):
            body = bytearray()
            async with client.stream("GET", url) as response:
                if response.status_code != 200:
                    return _readiness_observation(state, observed_at)
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > HTTP_RESPONSE_BYTE_LIMIT:
                        return _readiness_observation(state, observed_at)
            if not require_health_json:
                state = "pass"
            else:
                payload = json.loads(body)
                if isinstance(payload, dict) and payload.get("status") == "ok":
                    state = "pass"
    except Exception:  # noqa: BLE001 - every probe failure is a static state.
        state = "fail"
    return _readiness_observation(state, observed_at)


async def collect_http_observations(
    settings: Settings,
    observed_at: datetime,
    *,
    client_factory: Callable[..., Any] = httpx.AsyncClient,
    probe_timeout: Callable[[float], Any] = asyncio.timeout,
) -> tuple[ReadinessObservation, ReadinessObservation, ReadinessObservation]:
    local_url = f"http://127.0.0.1:8000{settings.api_prefix}/health"
    failed = _readiness_observation("fail", observed_at)
    origin: str | None = None
    try:
        candidate = public_origin(settings)
        if not candidate.startswith("https://"):
            raise ValueError("public_origin_must_use_https")
        origin = candidate
    except Exception:  # noqa: BLE001 - configuration becomes a static failure.
        origin = None

    async with client_factory(
        timeout=CHECK_TIMEOUT_SECONDS,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        local = await _http_observation(
            client,
            local_url,
            observed_at,
            require_health_json=True,
            probe_timeout=probe_timeout,
        )
        if origin is None:
            return local, failed, failed
        public = await _http_observation(
            client,
            f"{origin}{settings.api_prefix}/health",
            observed_at,
            require_health_json=True,
            probe_timeout=probe_timeout,
        )
        cabinet = await _http_observation(
            client,
            f"{origin}/cabinet/",
            observed_at,
            require_health_json=False,
            probe_timeout=probe_timeout,
        )
    return local, public, cabinet


def collect_disk_observation(
    settings: Settings,
    observed_at: datetime,
    *,
    disk_usage: Callable[[str], Any] = shutil.disk_usage,
) -> ReadinessObservation:
    try:
        usage = disk_usage(settings.vpn_backup_directory)
        passed = usage.total > 0 and usage.free * 100 >= usage.total * 10
    except Exception:  # noqa: BLE001 - every local failure is a static state.
        passed = False
    return _readiness_observation("pass" if passed else "fail", observed_at)


def collect_known_hosts_observation(
    settings: Settings,
    observed_at: datetime,
    *,
    runner: Callable[..., Any] = subprocess.run,
    user_lookup: Callable[[str], Any] | None = None,
    group_lookup: Callable[[str], Any] | None = None,
    validator: Callable[..., None] = validate_known_hosts_file,
) -> ReadinessObservation:
    try:
        configured = settings.vpn_control_known_hosts_path.strip()
        candidate = Path(configured)
        if not (
            PurePosixPath(configured).is_absolute()
            or PureWindowsPath(configured).is_absolute()
        ):
            raise ValueError("known_hosts_path_invalid")
        result = runner(
            [
                "/usr/bin/systemctl",
                "show",
                "-p",
                "User",
                "-p",
                "Group",
                "--value",
                "domain-drop-control.service",
            ],
            check=False,
            shell=False,
            timeout=CHECK_TIMEOUT_SECONDS,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw_user = result.stdout
        if (
            result.returncode != 0
            or type(raw_user) is not bytes
            or len(raw_user) > SERVICE_IDENTITY_BYTE_LIMIT
            or not raw_user.endswith(b"\n")
        ):
            raise ValueError("control_service_identity_invalid")
        identity = (
            raw_user.decode("ascii", errors="strict").removesuffix("\n").split("\n")
        )
        if len(identity) != 2 or any(
            any(character.isspace() for character in value) for value in identity
        ):
            raise ValueError("control_service_identity_invalid")
        username, group_name = identity
        if group_name:
            lookup_group = (
                importlib.import_module("grp").getgrnam
                if group_lookup is None
                else group_lookup
            )
            reader_gid = lookup_group(group_name).gr_gid
        else:
            lookup_user = (
                importlib.import_module("pwd").getpwnam
                if user_lookup is None
                else user_lookup
            )
            reader_gid = lookup_user(username or "root").pw_gid
        if type(reader_gid) is not int or reader_gid < 0:
            raise ValueError("control_service_identity_invalid")
        validator(candidate, expected_reader_gid=reader_gid)
        passed = True
    except Exception:  # noqa: BLE001 - every local failure is a static state.
        passed = False
    return _readiness_observation("pass" if passed else "fail", observed_at)


def collect_backup_observation(
    settings: Settings,
    observed_at: datetime,
    *,
    latest: Callable[[Path], datetime | None] = validated_latest_success_at,
) -> BackupObservation:
    timestamp: datetime | None = None
    passed = False
    if settings.vpn_backup_enabled:
        try:
            timestamp = latest(Path(settings.vpn_backup_directory))
            passed = bool(
                timestamp is not None
                and timestamp.tzinfo is not None
                and timestamp.utcoffset() is not None
                and observed_at - timedelta(seconds=BACKUP_MAX_AGE_SECONDS)
                <= timestamp
                <= observed_at
            )
        except Exception:  # noqa: BLE001 - every local failure is a static state.
            timestamp = None
    return BackupObservation(
        state="pass" if passed else "fail",
        observed_at=timestamp,
        max_age_seconds=BACKUP_MAX_AGE_SECONDS,
    )


async def collect_watchdog_observations(
    settings: Settings,
    observed_at: datetime,
    *,
    system_collector: Callable[..., Any] = collect_system_observations,
    http_collector: Callable[..., Any] = collect_http_observations,
    disk_collector: Callable[..., Any] = collect_disk_observation,
    known_hosts_collector: Callable[..., Any] = collect_known_hosts_observation,
    backup_collector: Callable[..., Any] = collect_backup_observation,
) -> tuple[OperationalObservations, BackupObservation]:
    failed = _readiness_observation("fail", observed_at)
    try:
        system, control = system_collector(observed_at)
    except Exception:  # noqa: BLE001 - collectors remain independent.
        system, control = failed, failed
    try:
        local, public, cabinet = await http_collector(settings, observed_at)
    except Exception:  # noqa: BLE001 - collectors remain independent.
        local, public, cabinet = failed, failed, failed
    try:
        disk = disk_collector(settings, observed_at)
    except Exception:  # noqa: BLE001 - collectors remain independent.
        disk = failed
    try:
        known_hosts = known_hosts_collector(settings, observed_at)
    except Exception:  # noqa: BLE001 - collectors remain independent.
        known_hosts = failed
    try:
        backup = backup_collector(settings, observed_at)
    except Exception:  # noqa: BLE001 - collectors remain independent.
        backup = BackupObservation(
            state="fail",
            observed_at=None,
            max_age_seconds=BACKUP_MAX_AGE_SECONDS,
        )
    return (
        OperationalObservations(
            system=system,
            control=control,
            local=local,
            public=public,
            cabinet=cabinet,
            disk=disk,
            known_hosts=known_hosts,
        ),
        backup,
    )


async def _resolve(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def collect_failing_codes(
    settings: Settings,
    *,
    now: Callable[[], datetime] | None = None,
    collect_observations: Callable[..., Any] = collect_watchdog_observations,
    database_factory: Callable[..., Any] = create_vpn_control_database,
    snapshot_loader: Callable[..., Any] = load_release_readiness_snapshot,
    evaluate: Callable[..., Any] = evaluate_release_readiness,
    store_observations: Callable[..., Any] = set_vpn_watchdog_observations,
    evaluate_operations: Callable[..., Any] = evaluate_operational_checks,
    timeout: Callable[[float], Any] = asyncio.timeout,
    cleanup_timeout: Callable[[float], Any] = asyncio.timeout,
) -> tuple[str, ...]:
    current = datetime.now(UTC) if now is None else now()
    operational, backup = await _resolve(collect_observations(settings, current))
    database = None
    transaction_failed = False
    cleanup_failed = False
    checks: Iterable[Any] = ()
    try:
        database = database_factory(settings)
        async with (
            timeout(DATABASE_TIMEOUT_SECONDS),
            database.session_factory() as session,
        ):
            snapshot = await snapshot_loader(
                session,
                settings,
                operational=operational,
                backup=backup,
            )
            readiness = evaluate(snapshot, now=current)
            checks = readiness.checks
            await store_observations(session, operational, backup)
            await session.commit()
    except Exception:  # noqa: BLE001 - database details must never escape.
        transaction_failed = True
    finally:
        if database is not None:
            try:
                async with cleanup_timeout(CHECK_TIMEOUT_SECONDS):
                    await database.engine.dispose()
            except Exception:  # noqa: BLE001 - disposal failure is fail-closed.
                cleanup_failed = True

    if transaction_failed:
        try:
            checks = evaluate_operations(
                operational,
                backup,
                bool(settings.vpn_control_known_hosts_path.strip()),
                current,
            )
        except Exception:  # noqa: BLE001 - keep the explicit database failure.
            checks = ()
    codes = {
        check.code
        for check in checks
        if check.state == "fail" and check.code in SAFE_CHECK_CODES
    }
    if transaction_failed or cleanup_failed:
        codes.add("control_database_unavailable")
    return tuple(sorted(codes))


def main(
    environment: Mapping[str, str] | None = None,
    *,
    run: Callable[[Settings], Any] = collect_failing_codes,
) -> int:
    environ = os.environ if environment is None else environment
    enabled = environ.get("VPN_WATCHDOG_ENABLED", "").strip().lower()
    if enabled in {"", "0", "false", "no", "off"}:
        return 0
    if enabled not in {"1", "true", "yes", "on"}:
        return 1
    try:
        settings = Settings.model_validate(dict(environ))
        _require_local_database_when_tls_disabled(settings, environ)
        failing_codes = asyncio.run(run(settings))
        run_alert_cycle(
            failing_codes,
            state_path=settings.vpn_watchdog_state_path,
            environ=environ,
        )
    except Exception:  # noqa: BLE001 - watchdog CLI is intentionally silent.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
