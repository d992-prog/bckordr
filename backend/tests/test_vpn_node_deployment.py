from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import os
import stat
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import asyncssh
import pytest

from app.services import vpn_node_transport
from app.services.vpn_node_bundle import IMPORT_PROBE_SENTINEL, build_node_bundle
from app.services.vpn_node_journal import initialize_node_journal
from app.services.vpn_node_transport import (
    MAX_KNOWN_HOSTS_BYTES,
    VpnNodeTransportSnapshot,
)

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
    reader_gid = parent.stat().st_gid

    assert deployment.install_control_known_hosts(
        raw,
        target=target,
        host="64.188.64.159",
        port=22,
        reader_gid=reader_gid,
        require_posix=False,
    ) == "installed"
    before = target.stat()
    assert target.read_bytes() == raw
    assert deployment.install_control_known_hosts(
        raw,
        target=target,
        host="64.188.64.159",
        port=22,
        reader_gid=reader_gid,
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
            reader_gid=reader_gid,
            require_posix=False,
        )


def test_control_trust_posix_install_uses_root_owner_reader_group_and_0640(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    target = tmp_path / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    raw = b"node-one " + key.export_public_key("openssh")
    secure_calls = []
    replace_calls = []

    class Lock:
        def __enter__(self):
            return 7

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr(
        deployment,
        "_secure_ancestors",
        lambda path, owner_uid, *, require_posix: secure_calls.append(
            (path, owner_uid, require_posix)
        ),
    )
    monkeypatch.setattr(
        deployment,
        "_control_known_hosts_lock",
        lambda *_args, **_kwargs: Lock(),
    )
    monkeypatch.setattr(
        deployment,
        "_replace_control_known_hosts",
        lambda *args, **kwargs: replace_calls.append((args, kwargs)),
    )

    assert deployment.install_control_known_hosts(
        raw,
        target=target,
        host="node-one",
        port=22,
        reader_gid=33,
    ) == "installed"

    assert secure_calls == [(target, 0, True)]
    assert replace_calls == [
        (
            (target, raw),
            {
                "reader_gid": 33,
                "require_posix": True,
                "directory_fd": 7,
                "existed": False,
            },
        )
    ]


def test_control_trust_posix_candidate_is_root_reader_group_0640(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    target = tmp_path / "known_hosts"
    ownership = []
    modes = []
    monkeypatch.setattr(deployment, "_write_exclusive", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment.os, "open", lambda *_args, **_kwargs: 9)
    monkeypatch.setattr(
        deployment.os, "fchown", lambda *args: ownership.append(args), raising=False
    )
    monkeypatch.setattr(deployment.os, "fchmod", lambda *args: modes.append(args))
    monkeypatch.setattr(deployment.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(deployment.os, "close", lambda _fd: None)
    monkeypatch.setattr(deployment.os, "replace", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(deployment, "_sync_directory", lambda _path: None)

    deployment._replace_control_known_hosts(
        target,
        b"pin\n",
        reader_gid=33,
        require_posix=True,
        directory_fd=None,
        existed=False,
    )

    assert ownership == [(9, 0, 33)]
    assert modes == [(9, 0o640)]


def test_control_trust_lock_is_secured_before_atomic_publication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    target = Path("C:/etc/veltrix/known_hosts")
    ownership = []
    modes = []
    opens = []
    links = []
    unlinks = []
    locks = []
    fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda fd, operation: locks.append((fd, operation)),
    )
    secure = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_uid=0,
        st_gid=0,
        st_dev=1,
        st_ino=2,
    )

    def open_file(path, flags, *args, **kwargs):
        opens.append((path, flags, args, kwargs))
        if len(opens) == 1:
            return 10
        if path == ".known_hosts.lock" and len(opens) == 2:
            raise FileNotFoundError
        if path != ".known_hosts.lock":
            assert flags & os.O_EXCL
            return 11
        return 12

    monkeypatch.setattr(deployment.os, "name", "posix")
    monkeypatch.setattr(deployment.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", fcntl)
    monkeypatch.setattr(deployment.os, "open", open_file)
    monkeypatch.setattr(
        deployment.os, "fchown", lambda *args: ownership.append(args), raising=False
    )
    monkeypatch.setattr(deployment.os, "fchmod", lambda *args: modes.append(args))
    monkeypatch.setattr(deployment.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(deployment.os, "fstat", lambda _fd: secure)
    monkeypatch.setattr(deployment.os, "stat", lambda *_args, **_kwargs: secure)
    monkeypatch.setattr(
        deployment.os,
        "link",
        lambda *args, **kwargs: links.append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(
        deployment.os,
        "unlink",
        lambda *args, **kwargs: unlinks.append((args, kwargs)),
    )
    monkeypatch.setattr(deployment.os, "close", lambda _fd: None)

    with deployment._control_known_hosts_lock(
        target,
        require_posix=True,
    ) as directory_fd:
        assert directory_fd == 10

    assert ownership == [(11, 0, 0)]
    assert modes == [(11, 0o600)]
    assert links == [
        (
            (opens[2][0], ".known_hosts.lock"),
            {"src_dir_fd": 10, "dst_dir_fd": 10, "follow_symlinks": False},
        )
    ]
    assert unlinks == [((opens[2][0],), {"dir_fd": 10})]
    assert locks == [(12, fcntl.LOCK_EX), (12, fcntl.LOCK_UN)]


def test_control_trust_lock_repairs_legacy_metadata_under_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    target = Path("C:/etc/veltrix/known_hosts")
    ownership = []
    modes = []
    opens = []
    locks = []
    fcntl = SimpleNamespace(
        LOCK_EX=1,
        LOCK_UN=2,
        flock=lambda fd, operation: locks.append((fd, operation)),
    )
    legacy = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o640,
        st_uid=33,
        st_gid=33,
        st_dev=1,
        st_ino=2,
    )
    repaired = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_uid=0,
        st_gid=0,
        st_dev=1,
        st_ino=2,
    )
    stats = [legacy, repaired]
    monkeypatch.setattr(deployment.os, "name", "posix")
    monkeypatch.setattr(deployment.os, "O_DIRECTORY", 0x10000, raising=False)
    monkeypatch.setitem(sys.modules, "fcntl", fcntl)
    monkeypatch.setattr(
        deployment.os,
        "open",
        lambda *_args, **_kwargs: opens.append(None) or (9 + len(opens)),
    )
    monkeypatch.setattr(
        deployment.os, "fchown", lambda *args: ownership.append(args), raising=False
    )
    monkeypatch.setattr(deployment.os, "fchmod", lambda *args: modes.append(args))
    monkeypatch.setattr(deployment.os, "fsync", lambda _fd: None)
    monkeypatch.setattr(deployment.os, "fstat", lambda _fd: stats.pop(0))
    monkeypatch.setattr(deployment.os, "stat", lambda *_args, **_kwargs: legacy)
    monkeypatch.setattr(deployment.os, "close", lambda _fd: None)

    with deployment._control_known_hosts_lock(target, require_posix=True):
        pass

    assert ownership == [(11, 0, 0)]
    assert modes == [(11, 0o600)]
    assert locks == [(11, fcntl.LOCK_EX), (11, fcntl.LOCK_UN)]


def test_control_trust_appends_distinct_target_and_preserves_existing_bytes(
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    first = b"node-one " + key.export_public_key("openssh").rstrip(b"\n")
    second = b"[node-two]:2222 " + key.export_public_key("openssh")
    _private_file(target, first)

    assert deployment.install_control_known_hosts(
        second,
        target=target,
        host="node-two",
        port=2222,
        reader_gid=parent.stat().st_gid,
        require_posix=False,
    ) == "installed"
    assert target.read_bytes() == first + b"\n" + second


def test_control_trust_same_pin_is_unchanged_and_rotation_is_refused(
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    original_key = asyncssh.generate_private_key("ssh-ed25519")
    replacement_key = asyncssh.generate_private_key("ssh-ed25519")
    original = b"node-one " + original_key.export_public_key("openssh").rstrip(b"\n")
    _private_file(target, original)
    before = target.stat()
    reader_gid = parent.stat().st_gid

    assert deployment.install_control_known_hosts(
        original + b"\n",
        target=target,
        host="node-one",
        port=22,
        reader_gid=reader_gid,
        require_posix=False,
    ) == "unchanged"
    assert target.read_bytes() == original
    assert target.stat().st_ino == before.st_ino

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            b"node-one " + replacement_key.export_public_key("openssh"),
            target=target,
            host="node-one",
            port=22,
            reader_gid=reader_gid,
            require_posix=False,
        )
    assert target.read_bytes() == original


def test_control_trust_rejects_multi_entry_input_and_oversize_merge_without_mutation(
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    reader_gid = parent.stat().st_gid
    first = b"node-one " + key.export_public_key("openssh")
    second = b"node-two " + key.export_public_key("openssh")

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            first + second,
            target=target,
            host="node-one",
            port=22,
            reader_gid=reader_gid,
            require_posix=False,
        )
    assert not target.exists()

    incoming = b"new-node " + key.export_public_key("openssh")
    suffix = b" " + key.export_public_key("openssh").rstrip(b"\n") + b"\n"
    existing = b""
    index = 0
    while True:
        for host_length in range(1, 254):
            final_size = len(existing) + host_length + len(suffix)
            if (
                MAX_KNOWN_HOSTS_BYTES - len(incoming)
                < final_size
                <= MAX_KNOWN_HOSTS_BYTES
            ):
                labels = []
                remaining = host_length
                while remaining > 63:
                    label_length = 62 if remaining == 64 else 63
                    labels.append("z" * label_length)
                    remaining -= label_length + 1
                labels.append("z" * remaining)
                existing += ".".join(labels).encode() + suffix
                break
        else:
            existing += f"node-{index}".encode() + suffix
            index += 1
            continue
        break
    assert len(existing) <= MAX_KNOWN_HOSTS_BYTES < len(existing) + len(incoming)
    _private_file(target, existing)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            incoming,
            target=target,
            host="new-node",
            port=22,
            reader_gid=reader_gid,
            require_posix=False,
        )
    assert target.read_bytes() == existing


def test_control_trust_replace_failure_preserves_existing_content(
    monkeypatch,
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    original = b"node-one " + key.export_public_key("openssh")
    incoming = b"node-two " + key.export_public_key("openssh")
    _private_file(target, original)
    replace_calls = []

    def fail_replace(*args, **kwargs):
        replace_calls.append((args, kwargs))
        raise OSError("synthetic replace failure")

    monkeypatch.setattr(deployment.os, "replace", fail_replace)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            incoming,
            target=target,
            host="node-two",
            port=22,
            reader_gid=parent.stat().st_gid,
            require_posix=False,
        )
    assert replace_calls
    assert target.read_bytes() == original
    assert not list(parent.glob(".known_hosts.*.tmp"))


@pytest.mark.parametrize("preexisting", [False, True])
def test_control_trust_post_replace_fsync_failure_rolls_back(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    preexisting: bool,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    original = b"node-one " + key.export_public_key("openssh")
    incoming = b"node-two " + key.export_public_key("openssh")
    if preexisting:
        _private_file(target, original)
    failed = False

    def fail_after_new_target_is_published(_directory: Path) -> None:
        nonlocal failed
        if target.exists() and b"node-two " in target.read_bytes() and not failed:
            failed = True
            raise OSError("synthetic post-replace fsync failure")

    monkeypatch.setattr(deployment, "_sync_directory", fail_after_new_target_is_published)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            incoming,
            target=target,
            host="node-two",
            port=22,
            reader_gid=parent.stat().st_gid,
            require_posix=False,
        )

    assert failed
    if preexisting:
        assert target.read_bytes() == original
    else:
        assert not target.exists()
    assert not list(parent.glob(".known_hosts.*"))


def test_control_trust_failed_rollback_retains_private_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    original = b"node-one " + key.export_public_key("openssh")
    incoming = b"node-two " + key.export_public_key("openssh")
    _private_file(target, original)
    real_replace = deployment.os.replace
    replace_calls = []
    fsync_failed = False

    def fail_rollback(source, destination, *args, **kwargs):
        replace_calls.append((source, destination))
        if str(source).endswith(".bak"):
            raise OSError("synthetic rollback failure")
        return real_replace(source, destination, *args, **kwargs)

    def fail_after_new_target_is_published(_directory: Path) -> None:
        nonlocal fsync_failed
        if target.exists() and b"node-two " in target.read_bytes() and not fsync_failed:
            fsync_failed = True
            raise OSError("synthetic post-replace fsync failure")

    monkeypatch.setattr(deployment.os, "replace", fail_rollback)
    monkeypatch.setattr(deployment, "_sync_directory", fail_after_new_target_is_published)

    with pytest.raises(deployment.NodeDeploymentError):
        deployment.install_control_known_hosts(
            incoming,
            target=target,
            host="node-two",
            port=22,
            reader_gid=parent.stat().st_gid,
            require_posix=False,
        )

    backups = list(parent.glob(".known_hosts.*.bak"))
    assert fsync_failed
    assert len(replace_calls) == 2
    assert len(backups) == 1
    assert backups[0].read_bytes() == original


@pytest.mark.skipif(os.name != "posix", reason="stdlib flock is POSIX-only")
def test_control_trust_concurrent_distinct_appends_retain_both(
    monkeypatch,
    tmp_path: Path,
) -> None:
    deployment = importlib.import_module("app.services.vpn_node_deployment")
    parent = tmp_path / "control-trust"
    parent.mkdir(mode=0o700)
    target = parent / "known_hosts"
    key = asyncssh.generate_private_key("ssh-ed25519")
    reader_gid = parent.stat().st_gid
    barrier = Barrier(2)
    monkeypatch.setattr(deployment, "_secure_ancestors", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        vpn_node_transport,
        "_validate_ancestors",
        lambda *_args, **_kwargs: None,
    )

    def install(host: str) -> str:
        raw = host.encode() + b" " + key.export_public_key("openssh")
        barrier.wait()
        return deployment.install_control_known_hosts(
            raw,
            target=target,
            host=host,
            port=22,
            reader_gid=reader_gid,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(install, ("node-one", "node-two")))

    assert results == ["installed", "installed"]
    assert set(target.read_bytes().splitlines()) == {
        b"node-one " + key.export_public_key("openssh").rstrip(b"\n"),
        b"node-two " + key.export_public_key("openssh").rstrip(b"\n"),
    }


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
