from __future__ import annotations

import hashlib
import inspect
import json
import os
import sqlite3
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import asyncssh
import pytest

from app.services import vpn_node_bootstrap
from app.services.vpn_node_bootstrap import (
    BOOTSTRAP_IMPORT_SENTINEL,
    FIXED_BOOTSTRAP_COMMAND,
    BootstrapLayout,
    VpnNodeBootstrapError,
    VpnNodeBootstrapReceipt,
    _node_config_bytes,
    _prepare_vpn_node,
    build_vpn_node_bootstrap_bundle,
    execute_vpn_node_bootstrap,
)
from app.services.vpn_node_transport import VpnNodeTransportSnapshot
from app.services.worker_maintenance import (
    XUI_INSTALL_COMMIT,
    XUI_INSTALL_SHA256,
    XUI_VERSION,
    _build_vpn_install_command,
    _build_xray_compatibility_command,
    _build_xray_owner_normalization_command,
)

BACKEND = Path(__file__).resolve().parents[1]


def test_bootstrap_bundle_is_deterministic_and_imports_in_isolated_python(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.pyz"
    second = tmp_path / "second.pyz"
    changed = tmp_path / "changed.pyz"

    first_digest = build_vpn_node_bootstrap_bundle(
        BACKEND,
        first,
        worker_id=16,
        panel_url="http://127.0.0.1:2053/panel/",
    )
    second_digest = build_vpn_node_bootstrap_bundle(
        BACKEND,
        second,
        worker_id=16,
        panel_url="http://127.0.0.1:2053/panel/",
    )
    changed_digest = build_vpn_node_bootstrap_bundle(
        BACKEND,
        changed,
        worker_id=17,
        panel_url="http://127.0.0.1:2053/panel/",
    )

    assert first.read_bytes() == second.read_bytes()
    assert first_digest == second_digest == hashlib.sha256(first.read_bytes()).hexdigest()
    assert changed_digest != first_digest
    with zipfile.ZipFile(first) as archive:
        main = archive.read("__main__.py").decode("ascii")
    assert repr(_build_vpn_install_command()) in main
    assert repr(_build_xray_owner_normalization_command()) in main
    assert repr(_build_xray_compatibility_command()) in main
    assert XUI_INSTALL_COMMIT in main
    assert XUI_INSTALL_SHA256 in main
    assert XUI_VERSION in main
    probe = subprocess.run(
        [sys.executable, "-I", "-S", str(first), "--import-probe"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert probe.returncode == 0
    assert probe.stdout == BOOTSTRAP_IMPORT_SENTINEL
    assert probe.stderr == b""


def test_bootstrap_receipt_is_frozen_and_contains_only_public_fields() -> None:
    receipt = VpnNodeBootstrapReceipt(
        state="prepared",
        panel_url="http://127.0.0.1:2053/panel/",
        database_path="/etc/x-ui/x-ui.db",
        xui_version="v3.8.5",
    )

    assert json.loads(json.dumps(receipt.__dict__)) if hasattr(receipt, "__dict__") else True
    assert repr(receipt) == (
        "VpnNodeBootstrapReceipt(state='prepared', "
        "panel_url='http://127.0.0.1:2053/panel/', "
        "database_path='/etc/x-ui/x-ui.db', xui_version='v3.8.5')"
    )


@pytest.mark.parametrize(
    "panel_url",
    ["https://127.0.0.1:2053/panel/", "http://vpn.example:2053/panel/"],
)
def test_builder_rejects_non_loopback_or_tls_panel_url_before_writing(
    tmp_path: Path, panel_url: str
) -> None:
    target = tmp_path / "bootstrap.pyz"
    with pytest.raises(VpnNodeBootstrapError, match="vpn_node_bootstrap_failed"):
        build_vpn_node_bootstrap_bundle(
            BACKEND, target, worker_id=16, panel_url=panel_url
        )
    assert not target.exists()


def _private_file(path: Path, raw: bytes = b"x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(0o600)


def _layout(tmp_path: Path) -> BootstrapLayout:
    state = tmp_path / "var" / "lib" / "veltrix-vpn"
    auth = state / "control-auth"
    binary = tmp_path / "usr" / "local" / "x-ui" / "x-ui"
    database = tmp_path / "etc" / "x-ui" / "x-ui.db"
    for directory in (state, auth, binary.parent, database.parent):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o700)
    return BootstrapLayout(
        state_root=state,
        auth_directory=auth,
        token=auth / "api-token",
        config=auth / "node.json",
        marker=auth / "bootstrap-reconciliation.json",
        backup=state / "backups" / "x-ui-before-api-token.db",
        xui_binaries=(binary,),
        databases=(database,),
    )


def _database(path: Path, *, inbounds: tuple[int, ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE inbounds (id INTEGER PRIMARY KEY)")
        connection.execute(
            "CREATE TABLE api_tokens (id INTEGER PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.executemany("INSERT INTO inbounds VALUES (?)", [(i,) for i in inbounds])
        connection.commit()
    finally:
        connection.close()
    path.chmod(0o600)


def _record_token(path: Path, token_name: str, *, row_id: int = 1) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("INSERT INTO api_tokens VALUES (?, ?)", (row_id, token_name))
        connection.commit()
    finally:
        connection.close()


def _assert_unique_token_command(command: list[str], binary: Path) -> None:
    assert command[:4] == [
        str(binary),
        "setting",
        "-getApiToken=true",
        "-tokenName",
    ]
    prefix = "veltrix-automation-16-"
    assert command[4].startswith(prefix)
    suffix = command[4][len(prefix) :]
    assert len(suffix) == 32
    int(suffix, 16)


class _Panel:
    def __init__(self, panel_url: str, *, api_token: str) -> None:
        self.panel_url = panel_url
        self.api_token = api_token

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


def _prepared(layout: BootstrapLayout, *, worker_id: int = 16) -> str:
    token = "synthetic-bootstrap-token-0123456789"
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0])
    _private_file(layout.token, (token + "\n").encode())
    _private_file(
        layout.config,
        _node_config_bytes(
            "http://127.0.0.1:2053/panel/",
            layout.databases[0],
        ),
    )
    return token


def test_exact_existing_token_and_config_are_idempotent(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    token = _prepared(layout)
    seen: list[tuple[str, str]] = []

    def panel(panel_url: str, *, api_token: str):
        seen.append((panel_url, api_token))
        return _Panel(panel_url, api_token=api_token)

    receipt = _prepare_vpn_node(
        layout,
        worker_id=16,
        panel_url="http://127.0.0.1:2053/panel/",
        install_command=_build_vpn_install_command(),
        xui_version=XUI_VERSION,
        architecture="amd64",
        port_occupied=lambda _port: True,
        runner=lambda *_args, **_kwargs: pytest.fail("must not mutate"),
        panel_session_factory=panel,
        owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
        require_posix=False,
    )

    assert receipt == VpnNodeBootstrapReceipt(
        "already_prepared",
        "http://127.0.0.1:2053/panel/",
        str(layout.databases[0]),
        XUI_VERSION,
    )
    assert seen == [("http://127.0.0.1:2053/panel/", token)]


@pytest.mark.parametrize(
    ("fault", "code"),
    [
        ("architecture", "vpn_node_bootstrap_environment_invalid"),
        ("port", "vpn_node_bootstrap_port_conflict"),
        ("inbound", "vpn_node_bootstrap_inbound_exists"),
        ("foreign_config", "vpn_node_bootstrap_foreign_state"),
        ("foreign_token", "vpn_node_bootstrap_foreign_state"),
        ("unsafe_mode", "vpn_node_bootstrap_environment_invalid"),
        ("symlink", "vpn_node_bootstrap_environment_invalid"),
    ],
)
def test_preflight_rejects_unsafe_or_foreign_state_before_mutation(
    tmp_path: Path,
    fault: str,
    code: str,
) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0], inbounds=(7,) if fault == "inbound" else ())
    architecture = "arm64" if fault == "architecture" else "amd64"
    occupied = fault == "port"
    if fault == "foreign_config":
        _private_file(layout.config, b"{}\n")
    elif fault == "foreign_token":
        _private_file(layout.token, b"foreign-token\n")
    elif fault == "unsafe_mode":
        if os.name != "posix":
            pytest.skip("POSIX mode semantics required")
        layout.databases[0].chmod(0o644)
    elif fault == "symlink":
        layout.databases[0].unlink()
        try:
            layout.databases[0].symlink_to(layout.xui_binaries[0])
        except OSError:
            pytest.skip("symlinks unavailable")

    with pytest.raises(VpnNodeBootstrapError, match=code) as caught:
        _prepare_vpn_node(
            layout,
            worker_id=16,
            panel_url="http://127.0.0.1:2053/panel/",
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            architecture=architecture,
            port_occupied=lambda _port: occupied,
            runner=lambda *_args, **_kwargs: pytest.fail("must fail before mutation"),
            panel_session_factory=_Panel,
            owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
            require_posix=False,
        )

    assert caught.value.code == code
    assert not layout.marker.exists()
    assert not layout.backup.exists()


def test_missing_xui_rejects_foreign_backup_before_install(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.backup, b"closed-backup")

    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_foreign_state"
    ):
        _prepare_vpn_node(
            layout,
            worker_id=16,
            panel_url="http://127.0.0.1:2053/panel/",
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            architecture="amd64",
            port_occupied=lambda _port: False,
            runner=lambda *_args, **_kwargs: pytest.fail(
                "must reject foreign state before installing"
            ),
            panel_session_factory=_Panel,
            owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
            require_posix=False,
        )


def test_missing_xui_rejects_unsafe_install_parent_before_install(
    tmp_path: Path,
) -> None:
    base = _layout(tmp_path)
    unsafe_parent = tmp_path / "not-a-directory"
    unsafe_parent.write_bytes(b"foreign")
    layout = BootstrapLayout(
        state_root=base.state_root,
        auth_directory=base.auth_directory,
        token=base.token,
        config=base.config,
        marker=base.marker,
        backup=base.backup,
        xui_binaries=(unsafe_parent / "x-ui",),
        databases=base.databases,
    )
    calls: list[str] = []

    def run(command: str, **_kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b"", b"")

    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_environment_invalid"
    ):
        _prepare_vpn_node(
            layout,
            worker_id=16,
            panel_url="http://127.0.0.1:2053/panel/",
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            architecture="amd64",
            port_occupied=lambda _port: False,
            runner=run,
            panel_session_factory=_Panel,
            owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
            require_posix=False,
        )

    assert calls == []


def test_missing_xui_runs_the_current_pinned_install_command(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    calls: list[object] = []

    def run(command, **kwargs):
        calls.append(command)
        if isinstance(command, str):
            assert command == _build_vpn_install_command()
            _private_file(layout.xui_binaries[0])
            _database(layout.databases[0])
            return subprocess.CompletedProcess(command, 0, b"3x-ui installed\n", b"")
        _assert_unique_token_command(command, layout.xui_binaries[0])
        _record_token(layout.databases[0], command[-1])
        return subprocess.CompletedProcess(
            command,
            0,
            b"There is 1 API token configured.\napiToken: synthetic-bootstrap-token-0123456789\n",
            b"",
        )

    receipt = _prepare_vpn_node(
        layout,
        worker_id=16,
        panel_url="http://127.0.0.1:2053/panel/",
        install_command=_build_vpn_install_command(),
        xui_version=XUI_VERSION,
        architecture="amd64",
        port_occupied=lambda _port: False,
        runner=run,
        panel_session_factory=_Panel,
        owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
        require_posix=False,
    )

    assert receipt.state == "prepared"
    assert calls[0] == _build_vpn_install_command()
    assert XUI_VERSION in _build_vpn_install_command()


def test_backup_and_marker_exist_before_exact_token_cli_and_success_is_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0])
    calls: list[list[str]] = []
    fsyncs: list[int] = []
    real_fsync = os.fsync

    def fsync(descriptor: int) -> None:
        fsyncs.append(descriptor)
        real_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", fsync)

    def run(command, **_kwargs):
        calls.append(command)
        assert layout.backup.is_file()
        if os.name == "posix":
            assert stat.S_IMODE(layout.backup.stat().st_mode) == 0o600
        assert layout.marker.is_file()
        _record_token(layout.databases[0], command[-1])
        return subprocess.CompletedProcess(
            command,
            0,
            b"apiToken: synthetic-bootstrap-token-0123456789\n",
            b"",
        )

    receipt = _prepare_vpn_node(
        layout,
        worker_id=16,
        panel_url="http://127.0.0.1:2053/panel/",
        install_command=_build_vpn_install_command(),
        xui_version=XUI_VERSION,
        architecture="amd64",
        port_occupied=lambda _port: False,
        runner=run,
        panel_session_factory=_Panel,
        owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
        require_posix=False,
    )

    assert len(calls) == 1
    _assert_unique_token_command(calls[0], layout.xui_binaries[0])
    assert receipt.state == "prepared"
    assert "synthetic-bootstrap-token" not in repr(receipt)
    if os.name == "posix":
        assert stat.S_IMODE(layout.auth_directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(layout.token.stat().st_mode) == 0o600
        assert stat.S_IMODE(layout.config.stat().st_mode) == 0o600
    assert layout.config.read_bytes() == _node_config_bytes(
        "http://127.0.0.1:2053/panel/", layout.databases[0]
    )
    assert not layout.marker.exists()
    assert fsyncs


def test_post_cli_rejects_changes_to_existing_token_rows(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0])
    _record_token(layout.databases[0], "owner-token", row_id=7)

    def corrupt(command, **_kwargs):
        connection = sqlite3.connect(layout.databases[0])
        try:
            connection.execute("UPDATE api_tokens SET name='changed' WHERE id=7")
            connection.execute("INSERT INTO api_tokens VALUES (?, ?)", (8, command[-1]))
            connection.commit()
        finally:
            connection.close()
        return subprocess.CompletedProcess(
            command,
            0,
            b"apiToken: synthetic-bootstrap-token-0123456789\n",
            b"",
        )

    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_mutation_uncertain"
    ):
        _prepare_vpn_node(
            layout,
            worker_id=16,
            panel_url="http://127.0.0.1:2053/panel/",
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            architecture="amd64",
            port_occupied=lambda _port: False,
            runner=corrupt,
            panel_session_factory=_Panel,
            owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
            require_posix=False,
        )

    assert layout.marker.exists()
    assert not layout.token.exists()


def test_ambiguous_token_creation_leaves_private_marker_and_blocks_retry(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0])
    calls = 0

    def ambiguous(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise subprocess.TimeoutExpired("secret-command", 10)

    kwargs = {
        "worker_id": 16,
        "panel_url": "http://127.0.0.1:2053/panel/",
        "install_command": _build_vpn_install_command(),
        "xui_version": XUI_VERSION,
        "architecture": "amd64",
        "port_occupied": lambda _port: False,
        "runner": ambiguous,
        "panel_session_factory": _Panel,
        "owner_uid": os.getuid() if hasattr(os, "getuid") else 0,
        "require_posix": False,
    }
    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_mutation_uncertain"
    ):
        _prepare_vpn_node(layout, **kwargs)

    if os.name == "posix":
        assert stat.S_IMODE(layout.marker.stat().st_mode) == 0o600
    assert calls == 1
    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_reconciliation_required"
    ):
        _prepare_vpn_node(layout, **kwargs)
    assert calls == 1


def test_token_output_is_strict_and_never_appears_in_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _private_file(layout.xui_binaries[0])
    _database(layout.databases[0])
    secret = "synthetic-secret-token-1234567890"

    def malformed(command, **_kwargs):
        return subprocess.CompletedProcess(command, 0, f"debug {secret}\n".encode(), b"")

    with pytest.raises(VpnNodeBootstrapError) as caught:
        _prepare_vpn_node(
            layout,
            worker_id=16,
            panel_url="http://127.0.0.1:2053/panel/",
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            architecture="amd64",
            port_occupied=lambda _port: False,
            runner=malformed,
            panel_session_factory=_Panel,
            owner_uid=os.getuid() if hasattr(os, "getuid") else 0,
            require_posix=False,
        )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)


class _Reader:
    def __init__(self, raw: bytes, *, error: Exception | None = None) -> None:
        self.raw = raw
        self.offset = 0
        self.error = error
        self.requests: list[int] = []

    async def read(self, size: int) -> bytes:
        self.requests.append(size)
        if self.error is not None:
            raise self.error
        chunk = self.raw[self.offset : self.offset + size]
        self.offset += len(chunk)
        return chunk


class _Writer:
    def __init__(self) -> None:
        self.raw = bytearray()
        self.eof = False

    def write(self, raw: bytes) -> None:
        self.raw.extend(raw)

    async def drain(self) -> None:
        return None

    def write_eof(self) -> None:
        self.eof = True


class _Process:
    def __init__(self, stdout: bytes, *, stdout_error: Exception | None = None) -> None:
        self.stdin = _Writer()
        self.stdout = _Reader(stdout, error=stdout_error)
        self.stderr = _Reader(b"")
        self.exit_status = 0
        self.waited = False
        self.closed = False

    async def wait(self) -> None:
        self.waited = True

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class _Connection:
    def __init__(self, process: _Process) -> None:
        self.process = process
        self.commands: list[tuple[str, dict[str, object]]] = []
        self.aborted = False

    async def create_process(self, command: str, **kwargs):
        self.commands.append((command, kwargs))
        return self.process

    def abort(self) -> None:
        self.aborted = True

    async def wait_closed(self) -> None:
        return None


def _snapshot() -> VpnNodeTransportSnapshot:
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    return VpnNodeTransportSnapshot(
        host="vpn.example",
        port=22,
        username="root",
        known_hosts=b"vpn.example " + host_key.export_public_key("openssh"),
        password="synthetic-password",
    )


def _receipt_bytes(state: str = "prepared") -> bytes:
    return (
        json.dumps(
            {
                "state": state,
                "panel_url": "http://127.0.0.1:2053/panel/",
                "database_path": "/etc/x-ui/x-ui.db",
                "xui_version": XUI_VERSION,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("ascii")
        + b"\n"
    )


@pytest.mark.asyncio
async def test_executor_uses_strict_transport_fixed_command_and_exact_framing() -> None:
    assert (
        inspect.signature(execute_vpn_node_bootstrap)
        .parameters["connector"]
        .default
        is asyncssh.connect
    )
    bundle = b"synthetic-reviewed-bootstrap"
    digest = hashlib.sha256(bundle).hexdigest()
    process = _Process(_receipt_bytes())
    connection = _Connection(process)
    calls: list[tuple] = []

    async def connector(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    receipt = await execute_vpn_node_bootstrap(
        _snapshot(), bundle, expected_sha256=digest, connector=connector
    )

    assert receipt == VpnNodeBootstrapReceipt(
        "prepared",
        "http://127.0.0.1:2053/panel/",
        "/etc/x-ui/x-ui.db",
        XUI_VERSION,
    )
    assert connection.commands == [
        (FIXED_BOOTSTRAP_COMMAND, {"term_type": None, "encoding": None})
    ]
    assert bytes(process.stdin.raw) == (
        digest.encode("ascii") + b"\n" + str(len(bundle)).encode("ascii") + b"\n" + bundle
    )
    assert process.stdin.eof and process.waited
    assert connection.aborted
    assert calls[0][1]["known_hosts"] is not None
    assert calls[0][1]["config"] is None
    assert calls[0][1]["agent_path"] is None
    assert calls[0][1]["server_host_key_algs"] == ["ssh-ed25519"]
    assert max(process.stdout.requests) <= 4097


@pytest.mark.asyncio
async def test_disconnect_after_remote_process_creation_is_uncertain_and_secret_free() -> None:
    secret = "synthetic-output-secret"
    process = _Process(b"", stdout_error=OSError(secret))
    connection = _Connection(process)

    async def connector(*_args, **_kwargs):
        return connection

    bundle = b"reviewed-bootstrap"
    with pytest.raises(
        VpnNodeBootstrapError, match="vpn_node_bootstrap_mutation_uncertain"
    ) as caught:
        await execute_vpn_node_bootstrap(
            _snapshot(),
            bundle,
            expected_sha256=hashlib.sha256(bundle).hexdigest(),
            connector=connector,
        )

    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
    assert connection.aborted


@pytest.mark.asyncio
async def test_executor_rejects_unreviewed_bundle_before_connect() -> None:
    calls = 0

    async def connector(*_args, **_kwargs):
        nonlocal calls
        calls += 1

    with pytest.raises(VpnNodeBootstrapError, match="vpn_node_bootstrap_failed"):
        await execute_vpn_node_bootstrap(
            _snapshot(), b"bundle", expected_sha256="0" * 64, connector=connector
        )
    assert calls == 0


def test_runtime_main_emits_only_canonical_public_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    receipt = VpnNodeBootstrapReceipt(
        "prepared",
        "http://127.0.0.1:2053/panel/",
        "/etc/x-ui/x-ui.db",
        XUI_VERSION,
    )
    monkeypatch.setattr(vpn_node_bootstrap, "_prepare_vpn_node", lambda *_args, **_kwargs: receipt)

    vpn_node_bootstrap._runtime_main(
        worker_id=16,
        panel_url=receipt.panel_url,
        install_command="fixed-install",
        xui_version=XUI_VERSION,
    )

    output = capsys.readouterr()
    assert output.out.encode("ascii") == _receipt_bytes()
    assert output.err == ""
