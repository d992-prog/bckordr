from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
from pathlib import Path
import stat
import subprocess
import sys
from types import SimpleNamespace
import zipfile

import asyncssh
import pytest

from app.services.vpn_node_bundle import IMPORT_PROBE_SENTINEL, build_node_bundle
from app.services.vpn_node_journal import initialize_node_journal
from app.services.vpn_node_transport import VpnNodeTransportSnapshot


BACKEND = Path(__file__).resolve().parents[1]


def _private_file(path: Path, raw: bytes) -> None:
    path.write_bytes(raw)
    path.chmod(0o600)


def _layout(tmp_path: Path):
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    code = tmp_path / "opt" / "veltrix-vpn" / "current"
    auth = tmp_path / "var" / "lib" / "veltrix-vpn" / "control-auth"
    state = tmp_path / "var" / "lib" / "veltrix-vpn"
    code.mkdir(parents=True)
    auth.mkdir(parents=True)
    for directory in (
        tmp_path,
        tmp_path / "opt",
        tmp_path / "opt" / "veltrix-vpn",
        code,
        tmp_path / "var",
        tmp_path / "var" / "lib",
        state,
        auth,
    ):
        directory.chmod(0o700)
    return deployment.NodeDeploymentLayout(
        active=code / "vpn-node.pyz",
        previous=code / "vpn-node.pyz.previous",
        state=state / "deployment-state.json",
        config=auth / "node.json",
        token=auth / "api-token",
        journal=state / "control-journal",
    )


def _bundle(tmp_path: Path) -> tuple[bytes, str]:
    target = tmp_path / "bundle.pyz"
    digest = build_node_bundle(BACKEND, target)
    return target.read_bytes(), digest


def _old_bundle(tmp_path: Path) -> bytes:
    raw, _digest = _bundle(tmp_path)
    target = tmp_path / "old-bundle.pyz"
    target.write_bytes(raw)
    with zipfile.ZipFile(target, "a") as archive:
        archive.comment = b"previous-reviewed-release"
    return target.read_bytes()


def _config(database: Path) -> bytes:
    return json.dumps(
        {
            "version": 1,
            "panel_url": "http://127.0.0.1:2053/secret-base/",
            "database_path": str(database),
        },
        separators=(",", ":"),
    ).encode("ascii")


def test_fresh_install_is_atomic_and_preserves_token(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    token = b"existing-service-token\n"
    _private_file(layout.token, token)
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    before = layout.token.stat()
    bundle, digest = _bundle(tmp_path)
    config = _config(database)

    result = deployment.install_node_release(
        bundle,
        expected_sha256=digest,
        node_config=config,
        layout=layout,
        python_executable=Path(sys.executable),
        owner_uid=before.st_uid,
        require_posix=False,
    )

    after = layout.token.stat()
    assert result == "installed"
    assert hashlib.sha256(layout.active.read_bytes()).hexdigest() == digest
    assert layout.config.read_bytes() == config
    assert layout.token.read_bytes() == token
    assert (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
        stat.S_IMODE(after.st_mode),
        after.st_uid,
    ) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
        stat.S_IMODE(before.st_mode),
        before.st_uid,
    )
    assert sorted(path.name for path in layout.journal.iterdir()) == [
        "gate.sqlite3",
        "operations.sqlite3",
    ]
    assert not layout.previous.exists()
    assert not layout.state.exists()
    probe = subprocess.run(
        [sys.executable, "-I", "-S", str(layout.active), "--import-probe"],
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert (probe.returncode, probe.stdout, probe.stderr) == (
        0,
        IMPORT_PROBE_SENTINEL,
        b"",
    )


def test_upgrade_keeps_previous_code_and_existing_private_state(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    config = _config(database)
    _private_file(layout.config, config)
    layout.journal.mkdir(mode=0o700)
    initialize_node_journal(layout.journal)
    old = _old_bundle(tmp_path)
    _private_file(layout.active, old)
    preserved = {
        path: (path.read_bytes(), path.stat())
        for path in (
            layout.token,
            layout.config,
            layout.journal / "gate.sqlite3",
            layout.journal / "operations.sqlite3",
        )
    }
    bundle, digest = _bundle(tmp_path)

    result = deployment.install_node_release(
        bundle,
        expected_sha256=digest,
        node_config=config,
        layout=layout,
        python_executable=Path(sys.executable),
        owner_uid=layout.token.stat().st_uid,
        require_posix=False,
    )

    assert result == "installed"
    assert layout.active.read_bytes() == bundle
    assert layout.previous.read_bytes() == old
    for path, (raw, before) in preserved.items():
        after = path.stat()
        assert path.read_bytes() == raw
        assert (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )


def test_failure_rolls_back_new_state_and_preserves_old_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    token = b"existing-service-token\n"
    _private_file(layout.token, token)
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    old = _old_bundle(tmp_path)
    _private_file(layout.active, old)
    bundle, digest = _bundle(tmp_path)
    replace = deployment.os.replace

    def fail_activation(source, destination, *args, **kwargs):
        destination_is_active = Path(destination) == layout.active or (
            kwargs.get("dst_dir_fd") is not None
            and destination == layout.active.name
        )
        if destination_is_active and Path(source).suffix == ".pyz":
            raise OSError("secret activation detail")
        return replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(deployment.os, "replace", fail_activation)

    with pytest.raises(deployment.NodeDeploymentError) as caught:
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=_config(database),
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert str(caught.value) == "vpn_node_deployment_failed"
    assert "secret" not in repr(caught.value)
    assert layout.active.read_bytes() == old
    assert layout.token.read_bytes() == token
    assert not layout.previous.exists()
    assert not layout.config.exists()
    assert not layout.journal.exists()
    assert not layout.state.exists()
    assert not list(layout.active.parent.glob(".candidate-*.pyz"))


def test_control_trust_is_exact_atomic_and_idempotent(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "vpn-node-15-known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    raw = b"64.188.64.159 " + key.export_public_key("openssh")
    owner_uid = parent.stat().st_uid

    assert deployment.install_control_known_hosts(
        raw,
        target=target,
        host="64.188.64.159",
        port=22,
        owner_uid=owner_uid,
        require_posix=False,
    ) == "installed"
    before = target.stat()
    assert target.read_bytes() == raw
    assert deployment.install_control_known_hosts(
        raw,
        target=target,
        host="64.188.64.159",
        port=22,
        owner_uid=owner_uid,
        require_posix=False,
    ) == "unchanged"
    after = target.stat()
    assert (after.st_dev, after.st_ino, after.st_mtime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_mtime_ns,
    )

    rsa = asyncssh.generate_private_key("ssh-rsa", key_size=3072)
    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            b"64.188.64.159 " + rsa.export_public_key("openssh"),
            target=parent / "wrong-known-hosts",
            host="64.188.64.159",
            port=22,
            owner_uid=owner_uid,
            require_posix=False,
        )


def test_deployment_helper_is_deterministic_and_runs_one_shot(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    bundle, bundle_digest = _bundle(tmp_path)
    first = tmp_path / "first-deploy.pyz"
    second = tmp_path / "second-deploy.pyz"
    options = {
        "source_root": BACKEND,
        "node_bundle": bundle,
        "expected_node_sha256": bundle_digest,
        "node_config": _config(database),
        "layout": layout,
        "python_executable": Path(sys.executable),
        "owner_uid": layout.token.stat().st_uid,
        "require_posix": False,
    }

    first_digest = deployment.build_node_deployment_helper(target=first, **options)
    second_digest = deployment.build_node_deployment_helper(target=second, **options)

    assert first_digest == second_digest
    assert first.read_bytes() == second.read_bytes()
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(first)],
        input=b"",
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert (result.returncode, result.stdout, result.stderr) == (
        0,
        b"vpn_node_deployment_installed\n",
        b"",
    )
    assert layout.active.read_bytes() == bundle


class _PasswordServer(asyncssh.SSHServer):
    def begin_auth(self, username):
        return True

    def password_auth_supported(self):
        return True

    def validate_password(self, username, password):
        return username == "root" and password == "deployment-password"


@pytest.mark.asyncio
async def test_one_shot_caller_uses_strict_pin_fixed_command_and_bounded_stdin() -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    invocations = []

    async def process_factory(process):
        request = await process.stdin.read()
        invocations.append((process.command, process.term_type, request))
        process.stdout.write(b"vpn_node_deployment_installed\n")
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=_PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    helper = b"synthetic-reviewed-deployment-helper"
    digest = hashlib.sha256(helper).hexdigest()
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=(
                f"[127.0.0.1]:{port} ".encode()
                + host_key.export_public_key("openssh")
            ),
            password="deployment-password",
        )

        assert await deployment.deploy_node_helper_over_ssh(
            snapshot,
            helper,
            expected_sha256=digest,
        ) == "installed"
    finally:
        server.close()
        await server.wait_closed()

    expected_stdin = digest.encode() + b"\n" + str(len(helper)).encode() + b"\n" + helper
    assert invocations == [
        (deployment.FIXED_DEPLOY_COMMAND, None, expected_stdin)
    ]
    assert helper not in deployment.FIXED_DEPLOY_COMMAND.encode()


def test_next_invocation_performs_rollback_only_after_abrupt_loss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    old = _old_bundle(tmp_path)
    _private_file(layout.active, old)
    bundle, digest = _bundle(tmp_path)
    config = _config(database)
    identity = deployment._identity
    token_checks = 0

    def interrupt_after_activation(*args, **kwargs):
        nonlocal token_checks
        result = identity(*args, **kwargs)
        if args[0] == layout.token:
            token_checks += 1
            if token_checks == 2:
                raise KeyboardInterrupt
        return result

    monkeypatch.setattr(deployment, "_identity", interrupt_after_activation)
    with pytest.raises(KeyboardInterrupt):
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=config,
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )
    monkeypatch.setattr(deployment, "_identity", identity)

    assert layout.active.read_bytes() == bundle
    assert layout.state.exists()
    assert deployment.install_node_release(
        bundle,
        expected_sha256=digest,
        node_config=config,
        layout=layout,
        python_executable=Path(sys.executable),
        owner_uid=layout.token.stat().st_uid,
        require_posix=False,
    ) == "recovered"
    assert layout.active.read_bytes() == old
    assert not layout.previous.exists()
    assert not layout.config.exists()
    assert not layout.journal.exists()
    assert not layout.state.exists()


@pytest.mark.asyncio
async def test_admin_caller_loads_pin_and_private_helper_before_transport(
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    known_hosts = tmp_path / "known_hosts"
    helper_path = tmp_path / "deploy.pyz"
    _private_file(known_hosts, b"literal-ed25519-pin")
    helper = b"reviewed-helper"
    _private_file(helper_path, helper)
    digest = hashlib.sha256(helper).hexdigest()
    worker = SimpleNamespace(id=15)
    snapshot = object()
    calls = []

    def load_snapshot(actual_worker, actual_path):
        calls.append(("snapshot", actual_worker, actual_path))
        return snapshot

    async def transport(actual_snapshot, raw, *, expected_sha256):
        calls.append(("transport", actual_snapshot, raw, expected_sha256))
        return "installed"

    assert await deployment.deploy_prebuilt_node_release(
        worker,
        known_hosts_path=known_hosts,
        helper_path=helper_path,
        expected_sha256=digest,
        owner_uid=helper_path.stat().st_uid,
        require_posix=False,
        snapshot_loader=load_snapshot,
        transport=transport,
    ) == "installed"
    assert calls == [
        ("snapshot", worker, known_hosts),
        ("transport", snapshot, helper, digest),
    ]


def test_concurrent_token_change_is_incomplete_and_keeps_recovery_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    old = _old_bundle(tmp_path)
    _private_file(layout.active, old)
    bundle, digest = _bundle(tmp_path)
    identity = deployment._identity
    token_checks = 0

    def change_token_before_final_check(*args, **kwargs):
        nonlocal token_checks
        if args[0] == layout.token:
            token_checks += 1
            if token_checks == 2:
                layout.token.write_bytes(b"external-token-change\n")
        return identity(*args, **kwargs)

    monkeypatch.setattr(deployment, "_identity", change_token_before_final_check)
    with pytest.raises(deployment.NodeDeploymentError) as caught:
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=_config(database),
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert caught.value.code == "vpn_node_deployment_incomplete"
    assert layout.active.read_bytes() == old
    assert layout.state.exists()


def test_control_trust_ancestors_may_be_root_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    target = (tmp_path / "known_hosts").resolve()
    real_lstat = Path.lstat

    def root_owned(path):
        real_lstat(path)
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | 0o755,
            st_uid=0,
        )

    monkeypatch.setattr(Path, "lstat", root_owned)

    deployment._secure_ancestors(target, 12345, require_posix=True)


@pytest.mark.asyncio
async def test_transport_timeout_after_stdin_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_factory(process):
        await process.stdin.read()
        await asyncio.sleep(1)
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=_PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=(
                f"[127.0.0.1]:{port} ".encode()
                + host_key.export_public_key("openssh")
            ),
            password="deployment-password",
        )
        helper = b"reviewed-helper"
        monkeypatch.setattr(deployment, "REMOTE_DEPLOY_TIMEOUT", 0.01)

        with pytest.raises(deployment.NodeDeploymentError) as caught:
            await deployment.deploy_node_helper_over_ssh(
                snapshot,
                helper,
                expected_sha256=hashlib.sha256(helper).hexdigest(),
            )

        assert caught.value.code == "vpn_node_deployment_incomplete"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_one_shot_caller_reports_rollback_only_recovery() -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    host_key = asyncssh.generate_private_key("ssh-ed25519")

    async def process_factory(process):
        await process.stdin.read()
        process.stdout.write(b"vpn_node_deployment_recovered\n")
        process.exit(0)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_factory=_PasswordServer,
        server_host_keys=[host_key],
        process_factory=process_factory,
        encoding=None,
    )
    try:
        port = server.get_port()
        snapshot = VpnNodeTransportSnapshot(
            host="127.0.0.1",
            port=port,
            username="root",
            known_hosts=(
                f"[127.0.0.1]:{port} ".encode()
                + host_key.export_public_key("openssh")
            ),
            password="deployment-password",
        )
        helper = b"reviewed-helper"

        assert await deployment.deploy_node_helper_over_ssh(
            snapshot,
            helper,
            expected_sha256=hashlib.sha256(helper).hexdigest(),
        ) == "recovered"
    finally:
        server.close()
        await server.wait_closed()


def test_existing_journal_must_be_a_valid_initialized_pair(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    layout.journal.mkdir(mode=0o700)
    _private_file(layout.journal / "gate.sqlite3", b"not-a-sqlite-journal")
    _private_file(layout.journal / "operations.sqlite3", b"not-a-sqlite-journal")
    bundle, digest = _bundle(tmp_path)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=_config(database),
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert not layout.active.exists()
    assert not layout.state.exists()


def test_config_database_must_exist_as_private_regular_file(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    bundle, digest = _bundle(tmp_path)
    missing = tmp_path / "missing-x-ui.db"

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=_config(missing),
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert not layout.state.exists()
    assert not layout.config.exists()


def test_config_rejects_duplicate_keys_before_mutation(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    config = (
        b'{"version":1,"version":1,"panel_url":"http://127.0.0.1:2053/",'
        + json.dumps("database_path").encode()
        + b":"
        + json.dumps(str(database)).encode()
        + b"}"
    )
    bundle, digest = _bundle(tmp_path)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=config,
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert not layout.state.exists()


def test_existing_active_must_pass_the_exact_import_probe(tmp_path: Path) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    _private_file(layout.active, b"not-a-reviewed-zipapp")
    bundle, digest = _bundle(tmp_path)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=_config(database),
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert layout.active.read_bytes() == b"not-a-reviewed-zipapp"
    assert not layout.state.exists()


def test_concurrent_existing_config_change_is_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    layout = _layout(tmp_path)
    _private_file(layout.token, b"existing-service-token\n")
    database = tmp_path / "x-ui.db"
    _private_file(database, b"sqlite-placeholder")
    config = _config(database)
    _private_file(layout.config, config)
    bundle, digest = _bundle(tmp_path)
    identity = deployment._identity
    token_checks = 0

    def change_config_after_activation(*args, **kwargs):
        nonlocal token_checks
        result = identity(*args, **kwargs)
        if args[0] == layout.token:
            token_checks += 1
            if token_checks == 2:
                layout.config.write_bytes(config + b" ")
        return result

    monkeypatch.setattr(deployment, "_identity", change_config_after_activation)
    with pytest.raises(deployment.NodeDeploymentError) as caught:
        deployment.install_node_release(
            bundle,
            expected_sha256=digest,
            node_config=config,
            layout=layout,
            python_executable=Path(sys.executable),
            owner_uid=layout.token.stat().st_uid,
            require_posix=False,
        )

    assert caught.value.code == "vpn_node_deployment_incomplete"
    assert not layout.active.exists()
    assert layout.state.exists()
