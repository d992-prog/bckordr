from __future__ import annotations

import asyncio
import json
import stat
import subprocess
import tempfile
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from app.core.config import Settings
from app.operations import watchdog
from app.operations.watchdog import AlertState
from app.services.vpn_release_readiness import (
    BackupObservation,
    OperationalObservations,
    ReadinessObservation,
    ReleaseCheck,
)

ALERT_ENV = {
    "VPN_TELEGRAM_BOT_TOKEN": "private-bot-token",
    "VPN_ALERT_TELEGRAM_USER_ID": "123456789",
}
NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    values = {
        "VPN_PORTAL_PUBLIC_ORIGIN": "https://vpn.example.test",
        "VPN_BACKUP_ENABLED": True,
        "VPN_BACKUP_DIRECTORY": "/var/backups/domain-drop-catcher",
        "VPN_CONTROL_KNOWN_HOSTS_PATH": "/etc/veltrix/known_hosts",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _observation(state: str = "pass") -> ReadinessObservation:
    return ReadinessObservation(
        state=state,
        observed_at=NOW,
        max_age_seconds=600,
    )


def _observations(**overrides: ReadinessObservation) -> OperationalObservations:
    values = {
        "system": _observation(),
        "control": _observation(),
        "local": _observation(),
        "public": _observation(),
        "cabinet": _observation(),
        "disk": _observation(),
        "known_hosts": _observation(),
    }
    values.update(overrides)
    return OperationalObservations(**values)


def test_alert_state_is_immutable_and_slotted() -> None:
    state = AlertState(
        digest="a" * 64,
        failing_codes=("control_health",),
        notified=False,
    )

    assert state.digest == "a" * 64
    assert state.failing_codes == ("control_health",)
    assert state.notified is False
    assert not hasattr(state, "__dict__")
    with pytest.raises(FrozenInstanceError):
        state.notified = True  # type: ignore[misc]


def test_build_alert_state_validates_sorts_and_deduplicates_codes() -> None:
    first = watchdog.build_alert_state(
        ["public_health", "control_health", "public_health"],
        notified=False,
    )
    second = watchdog.build_alert_state(
        ["control_health", "public_health"],
        notified=False,
    )

    assert first.failing_codes == ("control_health", "public_health")
    assert first.digest == second.digest
    assert len(first.digest) == 64


@pytest.mark.parametrize(
    "unsafe_code",
    [
        "https://private.example/vless://secret",
        "00000000-0000-0000-0000-000000000001",
        "raw exception: token=secret",
    ],
)
def test_build_alert_state_rejects_non_static_codes_without_echoing_them(
    unsafe_code: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        watchdog.build_alert_state([unsafe_code], notified=False)

    assert str(caught.value) == "watchdog_check_code_invalid"
    assert unsafe_code not in str(caught.value)


def test_save_alert_state_is_atomic_private_and_round_trips(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.json"
    state = watchdog.build_alert_state(["control_health"], notified=True)
    calls: dict[str, object] = {}
    events: list[str] = []
    real_mkstemp = tempfile.mkstemp
    real_chmod = watchdog.os.chmod
    real_fsync = watchdog.os.fsync
    real_replace = watchdog.os.replace

    def tracked_mkstemp(*, prefix: str, dir: str):
        calls["temp_dir"] = Path(dir)
        return real_mkstemp(prefix=prefix, dir=dir)

    def tracked_fsync(fd: int) -> None:
        calls["fsync"] = True
        events.append("file_fsync")
        real_fsync(fd)

    def tracked_chmod(path: str | Path, mode: int) -> None:
        calls.setdefault("chmod", []).append(mode)
        real_chmod(path, mode)

    def tracked_replace(source: str, destination: Path) -> None:
        calls["replace"] = (Path(source), Path(destination))
        events.append("replace")
        real_replace(source, destination)

    def tracked_directory_fsync(directory: Path) -> None:
        calls["fsync_directory"] = directory
        events.append("directory_fsync")

    monkeypatch.setattr(watchdog.tempfile, "mkstemp", tracked_mkstemp)
    monkeypatch.setattr(watchdog.os, "chmod", tracked_chmod)
    monkeypatch.setattr(watchdog.os, "fsync", tracked_fsync)
    monkeypatch.setattr(watchdog.os, "replace", tracked_replace)
    monkeypatch.setattr(
        watchdog,
        "_fsync_directory",
        tracked_directory_fsync,
        raising=False,
    )

    watchdog.save_alert_state(state_path, state)

    assert calls["temp_dir"] == tmp_path
    assert calls["fsync"] is True
    source, destination = calls["replace"]
    assert source.parent == tmp_path
    assert destination == state_path
    assert calls["fsync_directory"] == tmp_path
    assert events == ["file_fsync", "replace", "directory_fsync"]
    assert calls["chmod"] == [0o600, 0o600]
    if watchdog.os.name != "nt":
        assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert watchdog.load_alert_state(state_path) == state
    assert list(tmp_path.iterdir()) == [state_path]


def test_load_alert_state_treats_missing_or_corrupt_state_as_healthy(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    healthy = watchdog.build_alert_state((), notified=True)

    assert watchdog.load_alert_state(state_path) == healthy

    state_path.write_text('{"digest":"forged","failing_codes":[]}', encoding="utf-8")

    assert watchdog.load_alert_state(state_path) == healthy


def test_load_alert_state_caps_input_before_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "state.json"
    read_sizes: list[int] = []

    class OversizedState:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            read_sizes.append(size)
            return b"x" * (watchdog.STATE_FILE_BYTE_LIMIT + 1)

    monkeypatch.setattr(
        Path,
        "open",
        lambda *_args, **_kwargs: OversizedState(),
    )

    assert watchdog.load_alert_state(state_path) == watchdog.build_alert_state(
        (),
        notified=True,
    )
    assert read_sizes == [watchdog.STATE_FILE_BYTE_LIMIT + 1]


def test_fsync_directory_uses_and_closes_posix_directory_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    directory_fd = 987
    directory_flag = 0x10000
    monkeypatch.setattr(watchdog.os, "name", "posix")
    monkeypatch.setattr(watchdog.os, "O_DIRECTORY", directory_flag, raising=False)
    monkeypatch.setattr(
        watchdog.os,
        "open",
        lambda path, flags: calls.append(("open", (str(path), flags)))
        or directory_fd,
    )
    monkeypatch.setattr(
        watchdog.os,
        "fsync",
        lambda fd: calls.append(("fsync", fd)),
    )
    monkeypatch.setattr(
        watchdog.os,
        "close",
        lambda fd: calls.append(("close", fd)),
    )

    watchdog._fsync_directory(tmp_path)

    assert calls == [
        ("open", (str(tmp_path), watchdog.os.O_RDONLY | directory_flag)),
        ("fsync", directory_fd),
        ("close", directory_fd),
    ]


def test_save_alert_state_redacts_directory_fsync_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_error = "vless://secret private-token"

    def fail_directory_fsync(_directory: Path) -> None:
        raise OSError(raw_error)

    monkeypatch.setattr(
        watchdog,
        "_fsync_directory",
        fail_directory_fsync,
        raising=False,
    )

    with pytest.raises(watchdog.AlertStateError) as caught:
        watchdog.save_alert_state(
            tmp_path / "state.json",
            watchdog.build_alert_state(["control_health"], notified=True),
        )

    assert str(caught.value) == "watchdog_state_write_failed"
    assert raw_error not in str(caught.value)


def test_save_alert_state_redacts_temporary_file_creation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_error = "private state path and token"

    def fail_mkstemp(*, prefix: str, dir: str):
        raise OSError(f"{raw_error}: {prefix} {dir}")

    monkeypatch.setattr(watchdog.tempfile, "mkstemp", fail_mkstemp)

    with pytest.raises(watchdog.AlertStateError) as caught:
        watchdog.save_alert_state(
            tmp_path / "state.json",
            watchdog.build_alert_state(["control_health"], notified=True),
        )

    assert str(caught.value) == "watchdog_state_write_failed"
    assert raw_error not in str(caught.value)


def test_first_failure_is_pending_and_silent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    pending = watchdog.run_alert_cycle(
        ["public_health", "control_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert pending == watchdog.build_alert_state(
        ["control_health", "public_health"], notified=False
    )
    assert messages == []
    assert watchdog.load_alert_state(state_path) == pending


def test_second_identical_failure_sends_clear_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for _ in range(2):
        state = watchdog.run_alert_cycle(
            ["control_health"],
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert state.notified is True
    assert messages == [
        (
            "Veltrix VPN: мониторинг обнаружил устойчивую проблему.\n"
            "Подключение может работать нестабильно. "
            "Проверьте раздел «Готовность» в панели.\n"
            "Коды проверки: control_health"
        )
    ]


def test_identical_notified_failure_is_silent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for codes in (
        ["control_health", "public_health"],
        ["public_health", "control_health", "control_health"],
        ["control_health", "public_health"],
    ):
        watchdog.run_alert_cycle(
            codes,
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert len(messages) == 1


def test_changed_pending_failure_restarts_confirmation(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for codes in (
        ["control_health"],
        ["public_health"],
        ["public_health"],
    ):
        watchdog.run_alert_cycle(
            codes,
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert len(messages) == 1
    assert "Коды проверки: public_health" in messages[0]


def test_changed_notified_failure_stays_silent_until_recovery(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    delivered: list[str] = []
    send = lambda _token, _user_id, message: delivered.append(message)

    for _ in range(2):
        watchdog.run_alert_cycle(
            ["control_health"],
            state_path=state_path,
            environ=ALERT_ENV,
            sender=send,
        )

    changed = watchdog.run_alert_cycle(
        ["public_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )

    assert changed == watchdog.build_alert_state(
        ["public_health"], notified=True
    )
    assert watchdog.load_alert_state(state_path) == changed
    assert len(delivered) == 1

    recovered = watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )
    watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )

    assert recovered == watchdog.build_alert_state((), notified=True)
    assert delivered == [
        (
            "Veltrix VPN: мониторинг обнаружил устойчивую проблему.\n"
            "Подключение может работать нестабильно. "
            "Проверьте раздел «Готовность» в панели.\n"
            "Коды проверки: control_health"
        ),
        (
            "Veltrix VPN: работа сервиса восстановлена.\n"
            "Проверки снова проходят. Дополнительных действий не требуется."
        ),
    ]


def test_pending_failure_recovers_without_message(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    watchdog.run_alert_cycle(
        ["endpoint_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )
    healthy = watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert healthy == watchdog.build_alert_state((), notified=True)
    assert messages == []


def test_recovery_after_notified_failure_sends_once(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    def send(_token: str, _user_id: str, message: str) -> None:
        messages.append(message)

    watchdog.run_alert_cycle(
        ["backup_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )
    watchdog.run_alert_cycle(
        ["backup_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )
    recovered = watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )
    watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=send,
    )

    assert messages[-1] == (
        "Veltrix VPN: работа сервиса восстановлена.\n"
        "Проверки снова проходят. Дополнительных действий не требуется."
    )
    assert len(messages) == 2
    assert recovered == watchdog.build_alert_state((), notified=True)


def test_corrupt_state_is_replaced_without_a_false_recovery(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")
    messages: list[str] = []

    state = watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert messages == []
    assert state == watchdog.build_alert_state((), notified=True)
    assert watchdog.load_alert_state(state_path) == state
    assert state_path.read_text(encoding="utf-8").startswith("{")


def test_telegram_failure_persists_unnotified_state_for_retry(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    unsafe_error = (
        "private-bot-token vless://secret "
        "00000000-0000-0000-0000-000000000001"
    )

    def fail(_token: str, _user_id: str, message: str) -> None:
        assert "private-bot-token" not in message
        assert "vless://secret" not in message
        assert "00000000-0000-0000-0000-000000000001" not in message
        raise RuntimeError(unsafe_error)

    first = watchdog.run_alert_cycle(
        ["backup_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=fail,
    )

    assert first.notified is False

    pending = watchdog.run_alert_cycle(
        ["backup_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=fail,
    )

    assert pending.notified is False
    assert watchdog.load_alert_state(state_path) == pending
    assert unsafe_error not in state_path.read_text(encoding="utf-8")

    messages: list[str] = []
    retried = watchdog.run_alert_cycle(
        ["backup_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert retried.notified is True
    assert len(messages) == 1


class _FakeTelegramResponse:
    status = 200

    def __init__(self, body: bytes = b'{"ok":true}') -> None:
        self.body = body
        self.read_sizes: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self, size: int) -> bytes:
        self.read_sizes.append(size)
        return self.body


class _FakeTelegramOpener:
    def __init__(self, response: _FakeTelegramResponse) -> None:
        self.response = response
        self.requests = []
        self.timeouts: list[float] = []

    def open(self, request, *, timeout: float):
        self.requests.append(request)
        self.timeouts.append(timeout)
        return self.response


def test_send_telegram_message_is_bounded_and_uses_form_post() -> None:
    response = _FakeTelegramResponse()
    opener = _FakeTelegramOpener(response)
    message = "Veltrix: тест безопасного сообщения."

    watchdog.send_telegram_message(
        "private-bot-token",
        "123456789",
        message,
        opener=opener,
    )

    request = opener.requests[0]
    assert request.full_url == (
        "https://api.telegram.org/botprivate-bot-token/sendMessage"
    )
    assert request.get_method() == "POST"
    assert parse_qs(request.data.decode()) == {
        "chat_id": ["123456789"],
        "text": [message],
    }
    assert opener.timeouts == [watchdog.TELEGRAM_TIMEOUT_SECONDS]
    assert watchdog.TELEGRAM_TIMEOUT_SECONDS <= 5
    assert response.read_sizes == [watchdog.TELEGRAM_RESPONSE_BYTE_LIMIT + 1]


def test_default_telegram_opener_disables_redirects() -> None:
    handler = watchdog._NoRedirectHandler()

    assert handler.redirect_request(None, None, 302, "redirect", {}, "https://unsafe") is None


@pytest.mark.parametrize("user_id", ["", "-123", "12.5", "user-123"])
def test_send_telegram_message_rejects_non_numeric_user_id(user_id: str) -> None:
    opener = _FakeTelegramOpener(_FakeTelegramResponse())

    with pytest.raises(watchdog.TelegramDeliveryError) as caught:
        watchdog.send_telegram_message(
            "private-bot-token",
            user_id,
            "safe message",
            opener=opener,
        )

    assert str(caught.value) == "watchdog_telegram_delivery_failed"
    assert opener.requests == []


def test_send_telegram_message_caps_response_and_redacts_transport_error() -> None:
    oversized = b"x" * (watchdog.TELEGRAM_RESPONSE_BYTE_LIMIT + 1)
    opener = _FakeTelegramOpener(_FakeTelegramResponse(oversized))

    with pytest.raises(watchdog.TelegramDeliveryError) as caught:
        watchdog.send_telegram_message(
            "private-bot-token",
            "123456789",
            "safe message",
            opener=opener,
        )

    assert str(caught.value) == "watchdog_telegram_delivery_failed"
    assert "private-bot-token" not in str(caught.value)


def test_run_alert_cycle_uses_environment_and_default_sender(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[tuple[str, str, str]] = []
    monkeypatch.setenv("VPN_TELEGRAM_BOT_TOKEN", "environment-token")
    monkeypatch.setenv("VPN_ALERT_TELEGRAM_USER_ID", "987654321")
    monkeypatch.setattr(
        watchdog,
        "send_telegram_message",
        lambda token, user_id, message: sent.append((token, user_id, message)),
    )

    state_path = tmp_path / "state.json"
    watchdog.run_alert_cycle(["system_health"], state_path=state_path)
    state = watchdog.run_alert_cycle(["system_health"], state_path=state_path)

    assert state.notified is True
    assert sent[0][:2] == ("environment-token", "987654321")


def test_collect_system_observations_uses_exact_independent_bounded_commands() -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object):
        calls.append((command, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(command, 3)
        return SimpleNamespace(returncode=0)

    system, control = watchdog.collect_system_observations(
        NOW,
        runner=runner,
    )

    assert system.state == "fail"
    assert control.state == "pass"
    assert calls == [
        (
            ["/usr/bin/systemctl", "is-system-running", "--quiet"],
            {
                "check": False,
                "shell": False,
                "timeout": 3.0,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        ),
        (
            [
                "/usr/bin/systemctl",
                "is-active",
                "--quiet",
                "domain-drop-control.service",
            ],
            {
                "check": False,
                "shell": False,
                "timeout": 3.0,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        ),
    ]
    assert system.observed_at == control.observed_at == NOW
    assert system.max_age_seconds == control.max_age_seconds == 600


class _HttpResponse:
    def __init__(self, status: int, *chunks: bytes) -> None:
        self.status_code = status
        self.chunks = chunks
        self.yielded = 0

    async def aiter_bytes(self):
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk


class _SlowHttpResponse(_HttpResponse):
    async def aiter_bytes(self):
        while True:
            await asyncio.sleep(0.02)
            self.yielded += 1
            yield b" "


class _HttpStream:
    def __init__(self, response: _HttpResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _HttpResponse:
        return self.response

    async def __aexit__(self, *_args) -> None:
        return None


class _HttpClient:
    def __init__(self, responses: dict[str, _HttpResponse]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args) -> None:
        return None

    def stream(self, method: str, url: str) -> _HttpStream:
        assert method == "GET"
        self.urls.append(url)
        response = self.responses[url]
        if isinstance(response, Exception):
            raise response
        return _HttpStream(response)


@pytest.mark.asyncio
async def test_collect_http_observations_uses_exact_urls_and_safe_client_options() -> None:
    local_url = "http://127.0.0.1:8000/api/health"
    public_url = "https://vpn.example.test/api/health"
    cabinet_url = "https://vpn.example.test/cabinet/"
    client = _HttpClient(
        {
            local_url: _HttpResponse(200, b'{"status":"ok"}'),
            public_url: _HttpResponse(200, b'{"status":', b'"ok"}'),
            cabinet_url: _HttpResponse(200, b"x" * 4096),
        }
    )
    options: list[dict[str, object]] = []

    def client_factory(**kwargs: object) -> _HttpClient:
        options.append(kwargs)
        return client

    local, public, cabinet = await watchdog.collect_http_observations(
        _settings(),
        NOW,
        client_factory=client_factory,
    )

    assert (local.state, public.state, cabinet.state) == ("pass", "pass", "pass")
    assert client.urls == [local_url, public_url, cabinet_url]
    assert options == [
        {"timeout": 3.0, "follow_redirects": False, "trust_env": False}
    ]
    assert all(item.max_age_seconds == 600 for item in (local, public, cabinet))


@pytest.mark.asyncio
async def test_collect_http_observations_rejects_redirects_oversize_and_bad_json_independently() -> None:
    local_url = "http://127.0.0.1:8000/api/health"
    public_url = "https://vpn.example.test/api/health"
    cabinet_url = "https://vpn.example.test/cabinet/"
    oversized = _HttpResponse(200, b"x" * 4096, b"x")
    client = _HttpClient(
        {
            local_url: _HttpResponse(200, b'{"status":"not-ok"}'),
            public_url: oversized,
            cabinet_url: _HttpResponse(302, b"redirect"),
        }
    )

    result = await watchdog.collect_http_observations(
        _settings(),
        NOW,
        client_factory=lambda **_kwargs: client,
    )

    assert tuple(item.state for item in result) == ("fail", "fail", "fail")
    assert client.urls == [local_url, public_url, cabinet_url]
    assert oversized.yielded == 2


@pytest.mark.asyncio
async def test_invalid_public_origin_does_not_skip_local_health() -> None:
    local_url = "http://127.0.0.1:8000/api/health"
    client = _HttpClient(
        {local_url: _HttpResponse(200, json.dumps({"status": "ok"}).encode())}
    )

    result = await watchdog.collect_http_observations(
        _settings(VPN_PORTAL_PUBLIC_ORIGIN="http://unsafe.example.test"),
        NOW,
        client_factory=lambda **_kwargs: client,
    )

    assert tuple(item.state for item in result) == ("pass", "fail", "fail")
    assert client.urls == [local_url]


@pytest.mark.asyncio
async def test_each_http_probe_has_overall_deadline_and_timeout_does_not_skip_rest() -> None:
    local_url = "http://127.0.0.1:8000/api/health"
    public_url = "https://vpn.example.test/api/health"
    cabinet_url = "https://vpn.example.test/cabinet/"
    slow = _SlowHttpResponse(200)
    client = _HttpClient(
        {
            local_url: slow,
            public_url: _HttpResponse(200, b'{"status":"ok"}'),
            cabinet_url: _HttpResponse(200, b"cabinet"),
        }
    )
    deadlines: list[float] = []

    def probe_timeout(seconds: float):
        deadlines.append(seconds)
        return asyncio.timeout(0.01)

    result = await watchdog.collect_http_observations(
        _settings(),
        NOW,
        client_factory=lambda **_kwargs: client,
        probe_timeout=probe_timeout,
    )

    assert tuple(item.state for item in result) == ("fail", "pass", "pass")
    assert client.urls == [local_url, public_url, cabinet_url]
    assert deadlines == [3.0, 3.0, 3.0]
    assert slow.yielded == 0


@pytest.mark.parametrize(
    ("total", "free", "expected"),
    ((100, 10, "pass"), (100, 9, "fail"), (0, 0, "fail")),
)
def test_disk_observation_enforces_ten_percent_boundary(
    total: int,
    free: int,
    expected: str,
) -> None:
    paths: list[str] = []

    def disk_usage(path: str):
        paths.append(path)
        return SimpleNamespace(total=total, free=free)

    observation = watchdog.collect_disk_observation(
        _settings(),
        NOW,
        disk_usage=disk_usage,
    )

    assert observation.state == expected
    assert paths == ["/var/backups/domain-drop-catcher"]


def test_known_hosts_observation_uses_explicit_control_service_group() -> None:
    runner_calls: list[tuple[list[str], dict[str, object]]] = []
    group_lookup_calls: list[str] = []
    validator_calls: list[tuple[Path, int]] = []

    def runner(command: list[str], **kwargs: object):
        runner_calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=b"www-data\nveltrix-control\n")

    def group_lookup(group_name: str):
        group_lookup_calls.append(group_name)
        return SimpleNamespace(gr_gid=44)

    def validator(path: Path, *, expected_reader_gid: int) -> None:
        validator_calls.append((path, expected_reader_gid))

    observation = watchdog.collect_known_hosts_observation(
        _settings(VPN_CONTROL_KNOWN_HOSTS_PATH="/etc/veltrix/known_hosts"),
        NOW,
        runner=runner,
        user_lookup=lambda _username: pytest.fail("user lookup called"),
        group_lookup=group_lookup,
        validator=validator,
    )

    assert observation.state == "pass"
    assert runner_calls == [
        (
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
            {
                "check": False,
                "shell": False,
                "timeout": 3.0,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.DEVNULL,
            },
        )
    ]
    assert group_lookup_calls == ["veltrix-control"]
    assert validator_calls == [(Path("/etc/veltrix/known_hosts"), 44)]


def test_known_hosts_observation_uses_service_users_primary_group() -> None:
    lookup_calls: list[str] = []
    validator_calls: list[tuple[Path, int]] = []

    observation = watchdog.collect_known_hosts_observation(
        _settings(VPN_CONTROL_KNOWN_HOSTS_PATH="/etc/veltrix/known_hosts"),
        NOW,
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=b"www-data\n\n",
        ),
        user_lookup=lambda username: (
            lookup_calls.append(username) or SimpleNamespace(pw_gid=33)
        ),
        group_lookup=lambda _group: pytest.fail("group lookup called"),
        validator=lambda path, *, expected_reader_gid: validator_calls.append(
            (path, expected_reader_gid)
        ),
    )

    assert observation.state == "pass"
    assert lookup_calls == ["www-data"]
    assert validator_calls == [(Path("/etc/veltrix/known_hosts"), 33)]


def test_known_hosts_observation_maps_empty_service_identity_to_root_group() -> None:
    lookup_calls: list[str] = []
    validator_calls: list[tuple[Path, int]] = []

    observation = watchdog.collect_known_hosts_observation(
        _settings(VPN_CONTROL_KNOWN_HOSTS_PATH="/etc/veltrix/known_hosts"),
        NOW,
        runner=lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout=b"\n\n",
        ),
        user_lookup=lambda username: (
            lookup_calls.append(username) or SimpleNamespace(pw_gid=0)
        ),
        group_lookup=lambda _group: pytest.fail("group lookup called"),
        validator=lambda path, *, expected_reader_gid: validator_calls.append(
            (path, expected_reader_gid)
        ),
    )

    assert observation.state == "pass"
    assert lookup_calls == ["root"]
    assert validator_calls == [(Path("/etc/veltrix/known_hosts"), 0)]


@pytest.mark.parametrize(
    ("path", "result", "user_lookup", "group_lookup", "validator"),
    (
        (
            "relative-known-hosts",
            SimpleNamespace(returncode=0, stdout=b"www-data\ncontrol\n"),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=1, stdout=b""),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"x" * 513),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"www-data\n"),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"www data\ncontrol\n"),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"missing\n\n"),
            lambda _username: (_ for _ in ()).throw(KeyError("missing")),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"www-data\nmissing\n"),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: (_ for _ in ()).throw(KeyError("missing")),
            lambda *_args, **_kwargs: None,
        ),
        (
            "/etc/veltrix/known_hosts",
            SimpleNamespace(returncode=0, stdout=b"www-data\ncontrol\n"),
            lambda _username: SimpleNamespace(pw_gid=33),
            lambda _group: SimpleNamespace(gr_gid=44),
            lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bad pin")),
        ),
    ),
)
def test_known_hosts_observation_fails_closed(
    path: str,
    result: object,
    user_lookup,
    group_lookup,
    validator,
) -> None:
    observation = watchdog.collect_known_hosts_observation(
        _settings(VPN_CONTROL_KNOWN_HOSTS_PATH=path),
        NOW,
        runner=lambda *_args, **_kwargs: result,
        user_lookup=user_lookup,
        group_lookup=group_lookup,
        validator=validator,
    )

    assert observation.state == "fail"


@pytest.mark.parametrize(
    ("enabled", "timestamp", "expected"),
    (
        (False, NOW, "fail"),
        (True, None, "fail"),
        (True, NOW, "pass"),
        (True, NOW - timedelta(hours=36), "pass"),
        (True, NOW - timedelta(hours=36, seconds=1), "fail"),
        (True, NOW + timedelta(microseconds=1), "fail"),
    ),
)
def test_backup_observation_requires_enabled_recent_nonfuture_validated_backup(
    enabled: bool,
    timestamp: datetime | None,
    expected: str,
) -> None:
    calls: list[Path] = []

    def latest(root: Path) -> datetime | None:
        calls.append(root)
        return timestamp

    observation = watchdog.collect_backup_observation(
        _settings(VPN_BACKUP_ENABLED=enabled),
        NOW,
        latest=latest,
    )

    assert observation.state == expected
    assert observation.max_age_seconds == 36 * 60 * 60
    assert calls == ([Path("/var/backups/domain-drop-catcher")] if enabled else [])


@pytest.mark.asyncio
async def test_database_failure_retains_local_failures_as_sorted_safe_codes() -> None:
    operational = _observations(disk=_observation("fail"))
    backup = BackupObservation("pass", NOW, 36 * 60 * 60)

    def fail_database(_settings: Settings):
        raise RuntimeError("postgresql://private:secret@host/database")

    checks = await watchdog.collect_failing_codes(
        _settings(),
        now=lambda: NOW,
        collect_observations=lambda *_args, **_kwargs: (operational, backup),
        database_factory=fail_database,
        evaluate_operations=lambda *_args, **_kwargs: (
            ReleaseCheck("disk_health", "fail", "safe"),
            ReleaseCheck("system_health", "pass", "safe"),
            ReleaseCheck("not_allowlisted", "fail", "unsafe"),
            ReleaseCheck("disk_health", "fail", "duplicate"),
        ),
    )

    assert checks == ("control_database_unavailable", "disk_health")


@pytest.mark.asyncio
async def test_database_orchestration_persists_observations_before_dispose() -> None:
    events: list[object] = []
    operational = _observations()
    backup = BackupObservation("pass", NOW, 36 * 60 * 60)

    class Session:
        async def __aenter__(self):
            events.append("session_enter")
            return self

        async def __aexit__(self, *_args) -> None:
            events.append("session_exit")

        async def commit(self) -> None:
            events.append("commit")

    class Engine:
        async def dispose(self) -> None:
            events.append("dispose")

    database = SimpleNamespace(engine=Engine(), session_factory=lambda: Session())

    async def load_snapshot(session, settings, *, operational, backup):
        events.append(("load", session, settings, operational, backup))
        return "snapshot"

    def evaluate(snapshot, *, now):
        events.append(("evaluate", snapshot, now))
        return SimpleNamespace(
            checks=(ReleaseCheck("release_id", "fail", "safe"),)
        )

    async def store(session, stored_operational, stored_backup):
        events.append(("store", session, stored_operational, stored_backup))

    class Timeout:
        async def __aenter__(self):
            events.append("timeout_enter")

        async def __aexit__(self, *_args) -> None:
            events.append("timeout_exit")

    timeout_values: list[float] = []

    def timeout(seconds: float) -> Timeout:
        timeout_values.append(seconds)
        return Timeout()

    checks = await watchdog.collect_failing_codes(
        _settings(),
        now=lambda: NOW,
        collect_observations=lambda *_args, **_kwargs: (operational, backup),
        database_factory=lambda _settings: database,
        snapshot_loader=load_snapshot,
        evaluate=evaluate,
        store_observations=store,
        timeout=timeout,
    )

    assert checks == ("release_id",)
    assert timeout_values == [10.0]
    assert events[0:2] == ["timeout_enter", "session_enter"]
    assert events[-4:] == ["commit", "session_exit", "timeout_exit", "dispose"]
    assert [item[0] for item in events if isinstance(item, tuple)] == [
        "load",
        "evaluate",
        "store",
    ]


@pytest.mark.asyncio
async def test_missing_failed_backup_timestamp_persists_without_database_failure() -> None:
    operational = _observations()
    backup = BackupObservation("fail", None, 36 * 60 * 60)
    events: list[str] = []

    class Result:
        def scalar_one_or_none(self):
            return None

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def execute(self, _statement):
            return Result()

        def add(self, _row) -> None:
            events.append("add")

        async def flush(self) -> None:
            events.append("flush")

        async def commit(self) -> None:
            events.append("commit")

    class Engine:
        async def dispose(self) -> None:
            events.append("dispose")

    async def load_snapshot(*_args, **_kwargs):
        return "snapshot"

    result = await watchdog.collect_failing_codes(
        _settings(),
        now=lambda: NOW,
        collect_observations=lambda *_args: (operational, backup),
        database_factory=lambda _settings: SimpleNamespace(
            engine=Engine(),
            session_factory=lambda: Session(),
        ),
        snapshot_loader=load_snapshot,
        evaluate=lambda _snapshot, *, now: SimpleNamespace(
            checks=(ReleaseCheck("backup_health", "fail", str(now)),)
        ),
    )

    assert result == ("backup_health",)
    assert events == ["add", "flush", "commit", "dispose"]


@pytest.mark.asyncio
@pytest.mark.parametrize("dispose_mode", ("raise", "hang"))
async def test_database_cleanup_failure_is_bounded_and_preserves_database_checks(
    dispose_mode: str,
) -> None:
    operational = _observations()
    backup = BackupObservation("pass", NOW, 36 * 60 * 60)
    deadlines: list[float] = []

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            return None

        async def commit(self) -> None:
            return None

    class Engine:
        async def dispose(self) -> None:
            if dispose_mode == "raise":
                raise RuntimeError("private database cleanup failure")
            await asyncio.sleep(60)

    async def load_snapshot(*_args, **_kwargs):
        return "snapshot"

    async def store(*_args) -> None:
        return None

    def cleanup_timeout(seconds: float):
        deadlines.append(seconds)
        return asyncio.timeout(0.01 if dispose_mode == "hang" else 1)

    result = await watchdog.collect_failing_codes(
        _settings(),
        now=lambda: NOW,
        collect_observations=lambda *_args: (operational, backup),
        database_factory=lambda _settings: SimpleNamespace(
            engine=Engine(),
            session_factory=lambda: Session(),
        ),
        snapshot_loader=load_snapshot,
        evaluate=lambda _snapshot, *, now: SimpleNamespace(
            checks=(ReleaseCheck("release_id", "fail", str(now)),)
        ),
        store_observations=store,
        evaluate_operations=lambda *_args: pytest.fail(
            "successful database checks were replaced"
        ),
        cleanup_timeout=cleanup_timeout,
    )

    assert result == ("control_database_unavailable", "release_id")
    assert deadlines == [3.0]


def test_main_is_fail_closed_silent_and_runs_one_enabled_cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_calls: list[Settings] = []
    alert_calls: list[tuple[tuple[str, ...], Path, object]] = []

    async def run(settings: Settings) -> tuple[str, ...]:
        run_calls.append(settings)
        return ("control_health",)

    monkeypatch.setattr(
        watchdog,
        "run_alert_cycle",
        lambda codes, *, state_path, environ: alert_calls.append(
            (codes, Path(state_path), environ)
        ),
    )
    def disabled_run(_settings):
        pytest.fail("disabled watchdog ran")
    assert watchdog.main({}, run=disabled_run) == 0
    assert watchdog.main({"VPN_WATCHDOG_ENABLED": "false"}, run=disabled_run) == 0
    assert watchdog.main({"VPN_WATCHDOG_ENABLED": "maybe"}, run=run) == 1
    state_path = tmp_path / "state.json"
    environment = {
        "VPN_WATCHDOG_ENABLED": "true",
        "VPN_WATCHDOG_STATE_PATH": str(state_path),
        "VPN_ALERT_TELEGRAM_USER_ID": "123456789",
        "VPN_TELEGRAM_BOT_TOKEN": "private-token",
        "PGSSLMODE": "disable",
        "PGHOST": "/var/run/postgresql",
        "DB_URL": "postgresql+asyncpg:///veltrix",
    }

    assert watchdog.main(environment, run=run) == 0

    assert len(run_calls) == 1
    assert alert_calls == [
        (("control_health",), state_path, environment)
    ]
    for overrides in (
        {"DB_URL": "postgresql+asyncpg://user:password@db.internal/veltrix"},
        {"DB_URL": "postgresql+asyncpg:///veltrix?host=db.internal"},
        {
            "DB_URL": (
                "postgresql+asyncpg:///veltrix"
                "?host=/var/run/postgresql,db.internal&port=5432,5432"
            )
        },
        {
            "DB_URL": (
                "postgresql+asyncpg://user:password@localhost/veltrix"
                "?sslmode=require"
            )
        },
        {
            "DB_URL": (
                "postgresql+asyncpg:///veltrix?dsn="
                "postgresql%3A%2F%2Fuser%3Apassword%40db.internal%2Fremote"
            )
        },
        {"DB_URL": "postgresql+asyncpg:///veltrix", "PGHOST": "db.internal"},
        {"DB_URL": "postgresql+asyncpg:///veltrix?host=", "PGHOST": "db.internal"},
    ):
        assert watchdog.main(environment | overrides, run=run) == 1
    assert len(run_calls) == 1
    assert capsys.readouterr() == ("", "")


def test_main_returns_one_silently_on_invalid_settings_or_state_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def run(_settings: Settings) -> tuple[str, ...]:
        return ("control_health",)

    invalid = {"VPN_WATCHDOG_ENABLED": "true"}
    assert watchdog.main(invalid, run=run) == 1

    monkeypatch.setattr(
        watchdog,
        "run_alert_cycle",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            watchdog.AlertStateError("watchdog_state_write_failed")
        ),
    )
    environment = {
        "VPN_WATCHDOG_ENABLED": "true",
        "VPN_WATCHDOG_STATE_PATH": str(tmp_path / "state.json"),
        "VPN_ALERT_TELEGRAM_USER_ID": "123456789",
        "VPN_TELEGRAM_BOT_TOKEN": "private-token",
    }
    assert watchdog.main(environment, run=run) == 1
    assert capsys.readouterr() == ("", "")
