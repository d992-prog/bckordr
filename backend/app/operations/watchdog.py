from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

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
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
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
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        dir=str(destination.parent),
    )
    try:
        os.chmod(temporary_name, 0o600)
        with os.fdopen(fd, "wb") as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_name, destination)
        os.chmod(destination, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
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
        "Veltrix: готовность к релизу нарушена.\n"
        f"Коды: {', '.join(codes)}\n"
        "Действие: проверьте экран готовности."
    )


_RECOVERY_MESSAGE = (
    "Veltrix: готовность к релизу восстановлена.\n"
    "Действие: проверьте экран готовности."
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
        if (
            state_valid
            and previous.digest == current.digest
            and previous.notified
        ):
            return previous
        pending = current
        message = _failure_message(current.failing_codes)
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
