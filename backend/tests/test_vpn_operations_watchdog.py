from __future__ import annotations

import stat
import tempfile
from dataclasses import FrozenInstanceError
from pathlib import Path
from urllib.parse import parse_qs

import pytest
from app.operations import watchdog
from app.operations.watchdog import AlertState

ALERT_ENV = {
    "VPN_TELEGRAM_BOT_TOKEN": "private-bot-token",
    "VPN_ALERT_TELEGRAM_USER_ID": "123456789",
}


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


def test_first_failure_sends_fixed_safe_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    sent: list[tuple[str, str, str]] = []

    state = watchdog.run_alert_cycle(
        ["public_health", "control_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda token, user_id, message: sent.append(
            (token, user_id, message)
        ),
    )

    assert state.notified is True
    assert state.failing_codes == ("control_health", "public_health")
    assert sent == [
        (
            "private-bot-token",
            "123456789",
            (
                "Veltrix: готовность к релизу нарушена.\n"
                "Коды: control_health, public_health\n"
                "Действие: проверьте экран готовности."
            ),
        )
    ]
    assert watchdog.load_alert_state(state_path) == state


def test_identical_notified_failure_is_silent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for codes in (
        ["control_health", "public_health"],
        ["public_health", "control_health", "control_health"],
    ):
        watchdog.run_alert_cycle(
            codes,
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert len(messages) == 1


def test_changed_failure_reason_sends_a_new_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for codes in (["control_health"], ["public_health"]):
        watchdog.run_alert_cycle(
            codes,
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert len(messages) == 2
    assert "Коды: control_health" in messages[0]
    assert "Коды: public_health" in messages[1]


def test_failed_changed_alert_preserves_notified_failure_until_recovery(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    delivered: list[str] = []
    first = watchdog.run_alert_cycle(
        ["control_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: delivered.append(message),
    )

    def fail_changed(_token: str, _user_id: str, _message: str) -> None:
        raise RuntimeError("private changed-alert failure")

    after_failure = watchdog.run_alert_cycle(
        ["public_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=fail_changed,
    )

    assert after_failure == first
    assert watchdog.load_alert_state(state_path) == first

    recovered = watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: delivered.append(message),
    )
    watchdog.run_alert_cycle(
        [],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: delivered.append(message),
    )

    assert recovered == watchdog.build_alert_state((), notified=True)
    assert delivered == [
        (
            "Veltrix: готовность к релизу нарушена.\n"
            "Коды: control_health\n"
            "Действие: проверьте экран готовности."
        ),
        (
            "Veltrix: готовность к релизу восстановлена.\n"
            "Действие: проверьте экран готовности."
        ),
    ]


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
        "Veltrix: готовность к релизу восстановлена.\n"
        "Действие: проверьте экран готовности."
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

    state = watchdog.run_alert_cycle(
        ["system_health"],
        state_path=tmp_path / "state.json",
    )

    assert state.notified is True
    assert sent[0][:2] == ("environment-token", "987654321")
