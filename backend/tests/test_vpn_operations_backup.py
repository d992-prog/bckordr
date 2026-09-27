from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import replace as replace_config
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.operations.backup as backup_module
from app.operations.backup import (
    BackupConfig,
    BackupError,
    create_validated_backup,
    main,
)

NOW = datetime(2026, 9, 24, 3, 10, 11, tzinfo=UTC)
NONCE = "abcdef0123456789"
PASSWORD = "never-print-this-password"
_BOUND_BACKUP_ROOT = backup_module._bound_backup_root
POSIX_SNAPSHOT_ONLY = pytest.mark.skipif(
    not backup_module._DIR_FD_SUPPORTED,
    reason="descriptor-anchored snapshot inspection is POSIX-only",
)


def _copy_test_file(
    source: Path,
    destination: Path,
    relative: Path,
    budget: backup_module._Budget,
    expected: os.stat_result | None = None,
) -> dict[str, object]:
    before = (
        backup_module._validated_path(source, directory=False)
        if expected is None
        else expected
    )
    if expected is None:
        budget.add_file(before.st_size)
    descriptor = os.open(source, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        record = backup_module._copy_descriptor(
            descriptor, destination, relative, before, budget
        )
    finally:
        os.close(descriptor)
    if not backup_module._matches_stable_file(source.lstat(), before):
        raise BackupError("backup_source_changed")
    return record


def _copy_test_tree(
    source: Path,
    destination: Path,
    relative: Path,
    budget: backup_module._Budget,
) -> list[dict[str, object]]:
    root_info = backup_module._validated_path(source, directory=True)
    budget.add_directory()
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    records: list[dict[str, object]] = []
    for child in sorted(source.rglob("*")):
        budget.check_deadline()
        info = child.lstat()
        if backup_module._is_reparse(info) or stat.S_ISLNK(info.st_mode):
            raise BackupError("backup_source_invalid")
        child_relative = child.relative_to(source)
        target = destination / child_relative
        if stat.S_ISDIR(info.st_mode):
            if len(child_relative.parts) > budget.config.max_depth:
                raise BackupError("backup_limits_exceeded")
            budget.add_directory()
            target.mkdir(mode=0o700, parents=True, exist_ok=True)
        elif stat.S_ISREG(info.st_mode):
            budget.add_file(info.st_size)
            records.append(
                _copy_test_file(
                    child,
                    target,
                    relative / child_relative,
                    budget,
                    expected=info,
                )
            )
        else:
            raise BackupError("backup_source_invalid")
    if not backup_module._matches_identity(
        source.lstat(), root_info, directory=True
    ):
        raise BackupError("backup_source_changed")
    return records


def _copy_sources_for_test(
    config: BackupConfig, partial: Path, budget: backup_module._Budget
) -> list[dict[str, object]]:
    records = [
        _copy_test_file(
            config.env_file,
            partial / "environment" / ".env",
            Path("environment") / ".env",
            budget,
        ),
        _copy_test_file(
            config.systemd_unit,
            partial / "systemd" / config.systemd_unit.name,
            Path("systemd") / config.systemd_unit.name,
            budget,
        ),
    ]
    records.extend(
        _copy_test_tree(
            config.nginx_directory, partial / "nginx", Path("nginx"), budget
        )
    )
    records.extend(
        _copy_test_tree(
            config.frontend_dist, partial / "frontend", Path("frontend"), budget
        )
    )
    return records


@pytest.fixture(autouse=True)
def _allow_test_platform(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    if (
        request.node.name != "test_backup_fails_before_writes_without_secure_platform"
        and not backup_module._DIR_FD_SUPPORTED
    ):
        @contextmanager
        def bound_test_root(path: Path, *, stable: bool = False):
            del stable
            yield backup_module._validated_root(path), -1, lambda: None

        monkeypatch.setattr(backup_module, "_require_secure_platform", lambda: None)
        monkeypatch.setattr(backup_module, "_bound_backup_root", bound_test_root)
        monkeypatch.setattr(
            backup_module, "_exclusive_backup_root", lambda _root: nullcontext()
        )
        monkeypatch.setattr(backup_module, "_copy_sources", _copy_sources_for_test)


class FakeRunner:
    def __init__(self, *, fail: str | None = None, after_restore=None) -> None:
        self.fail = fail
        self.after_restore = after_restore
        self.calls: list[tuple[list[str], dict[str, object]]] = []

    def __call__(
        self, command: list[str], **kwargs: object
    ) -> subprocess.CompletedProcess:
        self.calls.append((command, kwargs))
        if command[0] == "/usr/bin/pg_dump":
            if self.fail != "dump":
                Path(command[3]).write_bytes(b"complete custom postgres archive")
            return subprocess.CompletedProcess(command, 1 if self.fail == "dump" else 0)
        assert command[0] == "/usr/bin/pg_restore"
        Path(command[-1]).read_bytes()  # The fake models a full archive read.
        if self.after_restore is not None:
            self.after_restore()
        return subprocess.CompletedProcess(command, 1 if self.fail == "restore" else 0)


def _sources(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "source"
    nginx = source / "nginx"
    frontend = source / "dist"
    nginx.mkdir(parents=True)
    frontend.mkdir()
    env_file = source / ".env"
    unit = source / "veltrix-control.service"
    env_file.write_text("SESSION_SECRET=source-only\n", encoding="utf-8")
    unit.write_text("[Service]\nExecStart=/opt/veltrix\n", encoding="utf-8")
    (nginx / "veltrix.conf").write_text("server {}\n", encoding="utf-8")
    assets = frontend / "assets"
    assets.mkdir()
    (assets / "app.js").write_bytes(b"built frontend")
    return {
        "env_file": env_file,
        "systemd_unit": unit,
        "nginx_directory": nginx,
        "frontend_dist": frontend,
    }


def _config(tmp_path: Path, **changes: object) -> BackupConfig:
    values: dict[str, object] = {
        "backup_root": tmp_path / "backups",
        **_sources(tmp_path),
        "retention": 7,
        "command_timeout_seconds": 91.0,
        "deadline_seconds": 600.0,
        "max_files": 50,
        "max_file_bytes": 1024 * 1024,
        "max_total_bytes": 4 * 1024 * 1024,
    }
    values.update(changes)
    backup_root = Path(values["backup_root"])
    backup_root.mkdir(mode=0o700)
    return BackupConfig(**values)


def _run(config: BackupConfig, runner: FakeRunner, **kwargs: object):
    return create_validated_backup(
        config,
        f"postgresql+asyncpg://backup:{PASSWORD}@db.internal:5433/veltrix",
        runner=runner,
        now=lambda: NOW,
        nonce=lambda: NONCE,
        **kwargs,
    )


def _read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _assert_marker_target_exists(root: Path) -> None:
    marker = _read_json(root / "latest-success.json")
    assert (root / marker["set_name"]).is_dir()


def _write_success(root: Path, name: str) -> Path:
    directory = root / name
    directory.mkdir(mode=0o700)
    manifest = b'{"files":[],"version":1}\n'
    (directory / "manifest.json").write_bytes(manifest)
    digest = hashlib.sha256(manifest).hexdigest()
    created = datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=UTC)
    metadata = {
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "manifest_sha256": digest,
        "set_name": name,
        "status": "successful",
        "version": 1,
    }
    (directory / "backup.json").write_text(
        json.dumps(metadata, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    for path in directory.iterdir():
        path.chmod(0o600)
    return directory


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_returns_utc_without_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    before_entries = {path.name for path in config.backup_root.iterdir()}
    before_root = config.backup_root.stat()

    created_at = backup_module.validated_latest_success_at(config.backup_root)

    assert created_at == datetime(2026, 9, 24, 3, 10, 11, tzinfo=UTC)
    assert created_at.tzinfo is UTC
    assert {path.name for path in config.backup_root.iterdir()} == before_entries
    after_root = config.backup_root.stat()
    assert stat.S_IMODE(after_root.st_mode) == stat.S_IMODE(before_root.st_mode)
    assert after_root.st_ctime_ns == before_root.st_ctime_ns
    assert not (config.backup_root / ".backup.lock").exists()


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_returns_none_when_marker_is_missing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    assert backup_module.validated_latest_success_at(config.backup_root) is None


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_returns_future_timestamp_for_caller_policy(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20990101T000000.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())

    assert backup_module.validated_latest_success_at(config.backup_root) == datetime(
        2099, 1, 1, tzinfo=UTC
    )


@pytest.mark.parametrize(
    "tamper",
    ["forged", "oversized", "failed", "mismatched", "partial"],
)
@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rejects_untrusted_marker_or_set(
    tmp_path: Path, tamper: str
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    metadata_path = success / "backup.json"
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes(metadata_path.read_bytes())

    if tamper == "forged":
        marker.write_text('{"set_name":"not-a-backup"}\n', encoding="utf-8")
    elif tamper == "oversized":
        marker.write_bytes(b"x" * 16_385)
    elif tamper == "failed":
        metadata = _read_json(metadata_path)
        metadata["status"] = "failed"
        failed = backup_module._json_bytes(metadata)
        metadata_path.write_bytes(failed)
        marker.write_bytes(failed)
    elif tamper == "mismatched":
        metadata = _read_json(marker)
        metadata["status"] = "failed"
        marker.write_bytes(backup_module._json_bytes(metadata))
    else:
        metadata = _read_json(marker)
        metadata["set_name"] = f"{success.name}.partial"
        marker.write_bytes(backup_module._json_bytes(metadata))

    assert backup_module.validated_latest_success_at(config.backup_root) is None


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rejects_unstable_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    marker_identity = backup_module._file_identity(marker.stat())
    real_read = os.read
    changed = False

    def changing_read(descriptor: int, size: int) -> bytes:
        nonlocal changed
        chunk = real_read(descriptor, size)
        if (
            chunk
            and not changed
            and backup_module._file_identity(os.fstat(descriptor)) == marker_identity
        ):
            changed = True
            with marker.open("ab") as stream:
                stream.write(b" ")
        return chunk

    monkeypatch.setattr(backup_module.os, "read", changing_read)

    assert backup_module.validated_latest_success_at(config.backup_root) is None
    assert changed


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rejects_root_swap_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    original = config.backup_root.with_name("backups-original")
    swapped = False

    real_read_file = backup_module._read_bounded_file_at

    def swapping_read_file(
        directory_fd: int, name: str, limit: int
    ) -> backup_module._SnapshotFile:
        nonlocal swapped
        snapshot = real_read_file(directory_fd, name, limit)
        if name == "latest-success.json" and not swapped:
            swapped = True
            config.backup_root.rename(original)
            shutil.copytree(original, config.backup_root)
        return snapshot

    monkeypatch.setattr(
        backup_module, "_read_bounded_file_at", swapping_read_file, raising=False
    )

    assert backup_module.validated_latest_success_at(config.backup_root) is None
    assert swapped


@pytest.mark.parametrize("symlink_kind", ["marker", "set"])
@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rejects_symlinked_marker_and_set(
    tmp_path: Path, symlink_kind: str
) -> None:
    config = _config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    success = _write_success(outside, "20260924T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    if symlink_kind == "marker":
        marker.symlink_to(success / "backup.json")
    else:
        marker.write_bytes((success / "backup.json").read_bytes())
        (config.backup_root / success.name).symlink_to(
            success, target_is_directory=True
        )

    assert backup_module.validated_latest_success_at(config.backup_root) is None


def test_trusted_snapshot_parser_validates_exact_metadata_and_manifest(
    tmp_path: Path,
) -> None:
    root = tmp_path / "backups"
    root.mkdir()
    success = _write_success(root, "20260924T031011.000000Z-aaaaaaaa")
    metadata = (success / "backup.json").read_bytes()
    manifest = (success / "manifest.json").read_bytes()

    snapshot = backup_module._parse_snapshot_metadata(metadata)
    created_at = backup_module._validated_snapshot_timestamp(
        snapshot, metadata, metadata, manifest
    )

    assert created_at == datetime(2026, 9, 24, 3, 10, 11, tzinfo=UTC)
    with pytest.raises(BackupError, match="^backup_snapshot_invalid$"):
        backup_module._validated_snapshot_timestamp(
            snapshot, metadata, metadata + b" ", manifest
        )
    with pytest.raises(BackupError, match="^backup_snapshot_invalid$"):
        backup_module._validated_snapshot_timestamp(
            snapshot, metadata, metadata, manifest + b" "
        )


@pytest.mark.parametrize(
    ("file_name", "mutation"),
    [
        ("backup.json", "grow"),
        ("backup.json", "replace"),
        ("manifest.json", "grow"),
        ("manifest.json", "replace"),
    ],
)
@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rejects_file_race_during_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    mutation: str,
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    target = success / file_name
    identity = backup_module._file_identity(target.stat())
    original = target.read_bytes()
    real_read = os.read
    mutated = False

    def racing_read(descriptor: int, size: int) -> bytes:
        nonlocal mutated
        chunk = real_read(descriptor, size)
        if (
            chunk
            and not mutated
            and backup_module._file_identity(os.fstat(descriptor)) == identity
        ):
            mutated = True
            if mutation == "grow":
                with target.open("ab") as stream:
                    stream.write(b" ")
            else:
                replacement = target.with_name(f".{target.name}.replacement")
                replacement.write_bytes(original)
                os.replace(replacement, target)
        return chunk

    monkeypatch.setattr(backup_module.os, "read", racing_read)

    assert backup_module.validated_latest_success_at(config.backup_root) is None
    assert mutated


@pytest.mark.parametrize(
    "file_name", ["latest-success.json", "backup.json", "manifest.json"]
)
@pytest.mark.parametrize("mutation", ["grow", "replace"])
@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_rechecks_files_after_all_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    file_name: str,
    mutation: str,
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    target = marker if file_name == marker.name else success / file_name
    original = target.read_bytes()
    real_read_file = backup_module._read_bounded_file_at
    real_open = backup_module.os.open
    real_close = backup_module.os.close
    open_descriptors: set[int] = set()
    mutated = False

    def tracked_open(path, flags, mode=0o777, *, dir_fd=None):
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        open_descriptors.add(descriptor)
        return descriptor

    def tracked_close(descriptor: int) -> None:
        real_close(descriptor)
        open_descriptors.discard(descriptor)

    def mutate_after_final_read(
        directory_fd: int, name: str, limit: int
    ) -> backup_module._SnapshotFile:
        nonlocal mutated
        snapshot = real_read_file(directory_fd, name, limit)
        if name == "manifest.json" and not mutated:
            mutated = True
            if mutation == "grow":
                with target.open("ab") as stream:
                    stream.write(b" ")
            else:
                replacement = target.with_name(f".{target.name}.post-read")
                replacement.write_bytes(original)
                os.replace(replacement, target)
        return snapshot

    monkeypatch.setattr(
        backup_module, "_read_bounded_file_at", mutate_after_final_read
    )
    monkeypatch.setattr(backup_module.os, "open", tracked_open)
    monkeypatch.setattr(backup_module.os, "close", tracked_close)

    assert backup_module.validated_latest_success_at(config.backup_root) is None
    assert mutated
    assert not open_descriptors


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_does_not_walk_backup_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())

    def reject_tree_walk(*_args: object, **_kwargs: object):
        raise AssertionError("snapshot inspection must not walk the backup tree")

    monkeypatch.setattr(backup_module, "_trusted_success", reject_tree_walk)
    monkeypatch.setattr(backup_module.os, "walk", reject_tree_walk)

    assert backup_module.validated_latest_success_at(config.backup_root) == datetime(
        2026, 9, 24, 3, 10, 11, tzinfo=UTC
    )


@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_does_not_rewalk_root_after_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    real_validate_ancestors = backup_module._validate_root_ancestors
    path_walks = 0

    def reject_rewalk(path: Path) -> None:
        nonlocal path_walks
        path_walks += 1
        if path_walks > 1:
            raise AssertionError("root path was walked after descriptor binding")
        real_validate_ancestors(path)

    monkeypatch.setattr(
        backup_module, "_validate_root_ancestors", reject_rewalk
    )

    assert backup_module.validated_latest_success_at(config.backup_root) == datetime(
        2026, 9, 24, 3, 10, 11, tzinfo=UTC
    )
    assert path_walks <= 1


@pytest.mark.parametrize("swap", ["root", "set"])
@POSIX_SNAPSHOT_ONLY
def test_validated_latest_success_is_anchored_during_transient_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, swap: str
) -> None:
    config = _config(tmp_path)
    success = _write_success(
        config.backup_root, "20260924T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((success / "backup.json").read_bytes())
    parked_root = config.backup_root.with_name("backups-parked")
    parked_set = success.with_name(f"{success.name}.parked")
    real_read_file = backup_module._read_bounded_file_at
    swapped = False
    restored = False

    def swapping_read_file(
        directory_fd: int, name: str, limit: int
    ) -> backup_module._SnapshotFile:
        nonlocal swapped, restored
        snapshot = real_read_file(directory_fd, name, limit)
        if not swapped and (
            (swap == "root" and name == "latest-success.json")
            or (swap == "set" and name == "backup.json")
        ):
            swapped = True
            if swap == "root":
                config.backup_root.rename(parked_root)
                config.backup_root.mkdir(mode=0o700)
            else:
                success.rename(parked_set)
                success.mkdir(mode=0o700)
        elif swapped and not restored and name == "manifest.json":
            restored = True
            if swap == "root":
                config.backup_root.rmdir()
                parked_root.rename(config.backup_root)
            else:
                success.rmdir()
                parked_set.rename(success)
        return snapshot

    monkeypatch.setattr(backup_module, "_read_bounded_file_at", swapping_read_file)

    created_at = backup_module.validated_latest_success_at(config.backup_root)

    assert created_at is None
    assert swapped and restored


def test_snapshot_cleanup_attempts_every_close_after_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_name = "20260924T031011.000000Z-aaaaaaaa"
    manifest = b'{"files":[],"version":1}\n'
    metadata = backup_module._json_bytes(
        {
            "created_at": "2026-09-24T03:10:11Z",
            "manifest_sha256": hashlib.sha256(manifest).hexdigest(),
            "set_name": set_name,
            "status": "successful",
            "version": 1,
        }
    )
    close_attempts: list[str | int] = []

    def snapshot(name: str, raw: bytes) -> SimpleNamespace:
        def close() -> None:
            close_attempts.append(name)
            if name == "manifest":
                raise OSError("synthetic close failure")

        return SimpleNamespace(raw=raw, validate=lambda: None, close=close)

    files = iter(
        [
            snapshot("marker", metadata),
            snapshot("metadata", metadata),
            snapshot("manifest", manifest),
        ]
    )
    directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_dev=1,
        st_ino=2,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )
    monkeypatch.setattr(
        backup_module, "_read_bounded_file_at", lambda *_args: next(files)
    )
    monkeypatch.setattr(backup_module.os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "stat", lambda *_args, **_kwargs: directory)
    monkeypatch.setattr(backup_module.os, "open", lambda *_args, **_kwargs: 40)
    monkeypatch.setattr(backup_module.os, "fstat", lambda _descriptor: directory)
    monkeypatch.setattr(
        backup_module.os,
        "close",
        lambda descriptor: close_attempts.append(descriptor),
    )

    with pytest.raises(BackupError, match="^backup_snapshot_invalid$"):
        backup_module._inspect_latest_success_fd(10)

    assert close_attempts == ["manifest", "metadata", "marker", 40]


def test_success_uses_exact_bounded_commands_and_private_pg_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    runner = FakeRunner()
    monkeypatch.setenv("PATH", str(tmp_path / "attacker-bin"))
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path / "attacker-libs"))

    result = _run(config, runner)

    expected = config.backup_root / f"20260924T031011.000000Z-{NONCE}"
    partial_dump = config.backup_root / f"{expected.name}.partial" / "database.dump"
    assert result.directory == expected
    assert result.created_at == "2026-09-24T03:10:11Z"
    assert [call[0] for call in runner.calls] == [
        [
            "/usr/bin/pg_dump",
            "--format=custom",
            "--file",
            str(partial_dump),
            "--dbname",
            "veltrix",
        ],
        ["/usr/bin/pg_restore", "--file", os.devnull, str(partial_dump)],
    ]
    for _command, kwargs in runner.calls:
        assert kwargs["shell"] is False
        assert kwargs["stdin"] == subprocess.DEVNULL
        assert kwargs["stdout"] == subprocess.DEVNULL
        assert kwargs["stderr"] == subprocess.DEVNULL
        assert 0 < float(kwargs["timeout"]) <= config.command_timeout_seconds
        env = kwargs["env"]
        assert isinstance(env, dict)
        assert env["PGHOST"] == "db.internal"
        assert env["PGPORT"] == "5433"
        assert env["PGUSER"] == "backup"
        assert env["PGPASSWORD"] == PASSWORD
        assert env["PGDATABASE"] == "veltrix"
        assert env["LC_ALL"] == "C"
        assert set(env) == {
            "LC_ALL",
            "PGHOST",
            "PGPORT",
            "PGUSER",
            "PGPASSWORD",
            "PGDATABASE",
        }
    assert not (config.backup_root / f"{expected.name}.partial").exists()
    assert PASSWORD not in repr(result)


def test_manifest_is_deterministic_and_hashes_every_payload_file(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    result = _run(config, FakeRunner())

    manifest_raw = (result.directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_raw)
    paths = [item["path"] for item in manifest["files"]]
    assert paths == sorted(paths)
    assert paths == [
        "database.dump",
        "environment/.env",
        "frontend/assets/app.js",
        "nginx/veltrix.conf",
        "systemd/veltrix-control.service",
    ]
    for item in manifest["files"]:
        raw = (result.directory / item["path"]).read_bytes()
        assert item == {
            "path": item["path"],
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    assert result.manifest_sha256 == hashlib.sha256(manifest_raw).hexdigest()
    assert (
        _read_json(result.directory / "backup.json")["manifest_sha256"]
        == result.manifest_sha256
    )


def test_oversized_manifest_is_bounded_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _write_success(
        config.backup_root, "20260920T031011.000000Z-aaaaaaaa"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((previous / "backup.json").read_bytes())
    before = marker.read_bytes()
    real_dumps = json.dumps

    def reject_one_shot_manifest(value: object, *args: object, **kwargs: object) -> str:
        if isinstance(value, dict) and "files" in value:
            raise AssertionError("manifest must use the capped streaming encoder")
        return real_dumps(value, *args, **kwargs)

    def many_long_records(
        _config: BackupConfig,
        _partial: Path,
        _budget: backup_module._Budget,
    ) -> list[dict[str, object]]:
        return [
            {
                "path": f"frontend/{index:04d}-{'x' * 120}.js",
                "sha256": "0" * 64,
                "size": 1,
            }
            for index in range(20)
        ]

    monkeypatch.setattr(backup_module, "_MAX_MANIFEST_BYTES", 512, raising=False)
    monkeypatch.setattr(backup_module.json, "dumps", reject_one_shot_manifest)
    monkeypatch.setattr(backup_module, "_copy_sources", many_long_records)

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        _run(config, FakeRunner())

    assert marker.read_bytes() == before
    assert previous.is_dir()
    assert not any(
        child.is_dir() and not child.name.endswith(".partial") and child != previous
        for child in config.backup_root.iterdir()
    )


def test_root_marker_contains_only_safe_metadata_and_no_secrets(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = FakeRunner()

    result = _run(config, runner)

    marker_raw = (config.backup_root / "latest-success.json").read_text(
        encoding="utf-8"
    )
    assert json.loads(marker_raw) == {
        "created_at": "2026-09-24T03:10:11Z",
        "manifest_sha256": result.manifest_sha256,
        "set_name": result.directory.name,
        "status": "successful",
        "version": 1,
    }
    all_json = (
        marker_raw
        + (result.directory / "manifest.json").read_text()
        + (result.directory / "backup.json").read_text()
    )
    assert PASSWORD not in all_json
    assert all(
        PASSWORD not in argument
        for command, _kwargs in runner.calls
        for argument in command
    )


@pytest.mark.skipif(
    os.name == "nt", reason="Windows chmod does not expose POSIX mode bits"
)
def test_every_created_directory_and_file_is_private(tmp_path: Path) -> None:
    config = _config(tmp_path)

    result = _run(config, FakeRunner())

    for path in [result.directory, *result.directory.rglob("*")]:
        expected = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(path.stat().st_mode) == expected
    assert (
        stat.S_IMODE((config.backup_root / "latest-success.json").stat().st_mode)
        == 0o600
    )


@pytest.mark.parametrize(
    ("failure", "code"),
    [("dump", "backup_dump_failed"), ("restore", "backup_validation_failed")],
)
def test_command_failure_is_static_and_preserves_diagnostic_partial(
    tmp_path: Path, failure: str, code: str
) -> None:
    config = _config(tmp_path)

    with pytest.raises(BackupError, match=f"^{code}$") as raised:
        _run(config, FakeRunner(fail=failure))

    assert PASSWORD not in repr(raised.value)
    assert not (config.backup_root / "latest-success.json").exists()
    names = {path.name for path in config.backup_root.iterdir()}
    assert f"20260924T031011.000000Z-{NONCE}.partial" in names
    assert names <= {
        ".backup.lock",
        f"20260924T031011.000000Z-{NONCE}.partial",
    }


def test_dump_is_precreated_privately_and_stays_private_after_failure(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    dump_path: Path | None = None

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        nonlocal dump_path
        dump_path = Path(command[3])
        assert dump_path.is_file()
        if os.name != "nt":
            assert stat.S_IMODE(dump_path.stat().st_mode) == 0o600
            dump_path.chmod(0o644)
        return subprocess.CompletedProcess(command, 1)

    with pytest.raises(BackupError, match="^backup_dump_failed$"):
        create_validated_backup(
            config,
            f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
            runner=runner,
            now=lambda: NOW,
            nonce=lambda: NONCE,
        )

    assert dump_path is not None and dump_path.is_file()
    if os.name != "nt":
        assert stat.S_IMODE(dump_path.stat().st_mode) == 0o600


def test_dump_replacement_is_rejected_even_when_command_succeeds(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    replacement: Path | None = None
    replacement_mode: int | None = None

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        nonlocal replacement, replacement_mode
        replacement = Path(command[3])
        replacement.unlink()
        replacement.write_bytes(b"replacement archive")
        replacement_mode = stat.S_IMODE(replacement.stat().st_mode)
        return subprocess.CompletedProcess(command, 0)

    with pytest.raises(BackupError, match="^backup_dump_failed$"):
        create_validated_backup(
            config,
            f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
            runner=runner,
            now=lambda: NOW,
            nonce=lambda: NONCE,
        )

    assert replacement is not None and replacement.is_file()
    if os.name != "nt":
        # A replacement inode is untrusted and must not be chmod'd. The private
        # partial directory is the confidentiality boundary after rejection.
        assert stat.S_IMODE(replacement.stat().st_mode) == replacement_mode
        assert stat.S_IMODE(replacement.parent.stat().st_mode) == 0o700


def test_unexpected_runner_exception_cannot_expose_database_secret(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    def runner(_command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        raise RuntimeError(PASSWORD)

    with pytest.raises(BackupError, match="^backup_dump_failed$") as raised:
        create_validated_backup(
            config,
            f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
            runner=runner,
            now=lambda: NOW,
            nonce=lambda: NONCE,
        )

    assert PASSWORD not in repr(raised.value)


def test_failed_copy_preserves_previous_success_marker_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    previous_marker = (previous / "backup.json").read_bytes()
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes(previous_marker)
    real_write = os.write

    def fail_source_copy(descriptor: int, content) -> int:
        if bytes(content).startswith(b"SESSION_SECRET="):
            raise OSError("synthetic copy failure")
        return real_write(descriptor, content)

    monkeypatch.setattr(backup_module.os, "write", fail_source_copy)

    with pytest.raises(BackupError, match="^backup_copy_failed$"):
        _run(config, FakeRunner())

    assert marker.read_bytes() == previous_marker
    assert previous.is_dir()
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


@POSIX_SNAPSHOT_ONLY
def test_approved_nginx_symlinks_are_materialized_as_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    sites_available = config.nginx_directory / "sites-available"
    sites_enabled = config.nginx_directory / "sites-enabled"
    modules_enabled = config.nginx_directory / "modules-enabled"
    module_root = tmp_path / "modules-available"
    for directory in (sites_available, sites_enabled, modules_enabled, module_root):
        directory.mkdir()
    site_bytes = b"server { listen 80; }\n"
    module_bytes = b"load_module modules/ngx_http_test_module.so;\n"
    (sites_available / "site.conf").write_bytes(site_bytes)
    (module_root / "module.conf").write_bytes(module_bytes)
    (sites_enabled / "site.conf").symlink_to("../sites-available/site.conf")
    (modules_enabled / "module.conf").symlink_to(module_root / "module.conf")
    monkeypatch.setattr(backup_module, "_NGINX_MODULES_DIRECTORY", module_root)

    result = _run(config, FakeRunner())
    expected = {
        "nginx/sites-enabled/site.conf": site_bytes,
        "nginx/modules-enabled/module.conf": module_bytes,
    }
    records = {
        item["path"]: item
        for item in _read_json(result.directory / "manifest.json")["files"]
    }
    for relative, content in expected.items():
        copied = result.directory / relative
        assert copied.read_bytes() == content
        assert stat.S_ISREG(copied.lstat().st_mode)
        assert not copied.is_symlink()
        assert records[relative] == {
            "path": relative,
            "sha256": hashlib.sha256(content).hexdigest(),
            "size": len(content),
        }


@pytest.mark.parametrize(
    "case",
    ["missing_parent", "trailing_slash", "file_parent", "symlink_parent"],
)
@POSIX_SNAPSHOT_ONLY
def test_nginx_symlink_rejects_normalization_that_changes_target_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    config = _config(tmp_path)
    sites_available = config.nginx_directory / "sites-available"
    sites_enabled = config.nginx_directory / "sites-enabled"
    module_root = tmp_path / "modules-available"
    for directory in (sites_available, sites_enabled, module_root):
        directory.mkdir()
    (sites_available / "site.conf").write_bytes(b"server safe;\n")
    (module_root / "site.conf").write_bytes(b"load_module safe;\n")
    outside = tmp_path / "outside"
    (outside / "nested").mkdir(parents=True)
    (outside / "site.conf").write_bytes(b"outside secret\n")
    (module_root / "redirect").symlink_to(outside / "nested", target_is_directory=True)
    link_text = {
        "missing_parent": "../missing/../sites-available/site.conf",
        "trailing_slash": "../sites-available/site.conf/",
        "file_parent": "../sites-available/site.conf/../site.conf",
        "symlink_parent": f"{module_root}/redirect/../site.conf",
    }[case]
    normalized = Path(os.path.normpath(os.path.join(sites_enabled, link_text)))
    assert normalized.is_file()
    (sites_enabled / "invalid.conf").symlink_to(link_text)
    monkeypatch.setattr(backup_module, "_NGINX_MODULES_DIRECTORY", module_root)

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not (partial / "nginx/sites-enabled/invalid.conf").exists()
    assert not (config.backup_root / "latest-success.json").exists()


@pytest.mark.parametrize(
    "link_text",
    [
        "../missing/../sites-available/site.conf",
        "../sites-available/site.conf/",
        "../sites-available/site.conf/.",
        "../sites-available/site.conf/..",
        "../sites-available/site.conf/../site.conf",
        "../sites-available/redirect/../site.conf",
        "../sites-available/site.conf/./site.conf",
        "absolute_with_cancellation",
    ],
)
def test_approved_symlink_target_rejects_semantic_cancellation(
    tmp_path: Path, link_text: str
) -> None:
    config = _config(tmp_path)
    parent = config.nginx_directory / "sites-enabled"
    if link_text == "absolute_with_cancellation":
        link_text = (
            config.nginx_directory
            / "sites-available"
            / "missing"
            / ".."
            / "site.conf"
        ).as_posix()
    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        backup_module._approved_symlink_target(
            parent, link_text, (config.nginx_directory,)
        )


def test_approved_symlink_target_keeps_direct_relative_and_absolute_paths(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    parent = config.nginx_directory / "sites-enabled"
    target = config.nginx_directory / "sites-available" / "site.conf"
    for link_text in ("../sites-available/site.conf", str(target)):
        assert backup_module._approved_symlink_target(
            parent, link_text, (config.nginx_directory,)
        ) == target


@pytest.mark.parametrize("target_kind", ["broken", "directory", "link-to-link"])
@POSIX_SNAPSHOT_ONLY
def test_approved_nginx_symlink_rejects_unsupported_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target_kind: str
) -> None:
    config = _config(tmp_path)
    modules_enabled = config.nginx_directory / "modules-enabled"
    module_root = tmp_path / "modules-available"
    modules_enabled.mkdir()
    module_root.mkdir()
    target = module_root / "module.conf"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind == "link-to-link":
        (module_root / "real.conf").write_text("load_module test;\n", encoding="utf-8")
        target.symlink_to("real.conf")
    (modules_enabled / "module.conf").symlink_to(target)
    monkeypatch.setattr(backup_module, "_NGINX_MODULES_DIRECTORY", module_root)

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not (partial / "nginx/modules-enabled/module.conf").exists()
    assert not (config.backup_root / "latest-success.json").exists()


@POSIX_SNAPSHOT_ONLY
def test_relative_nginx_symlink_cannot_escape_approved_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sites_enabled = config.nginx_directory / "sites-enabled"
    sites_enabled.mkdir()
    secret = tmp_path / "outside-secret"
    secret_bytes = b"secret outside approved nginx roots\n"
    secret.write_bytes(secret_bytes)
    link = sites_enabled / "outside.conf"
    link.symlink_to("../../../outside-secret")
    assert link.resolve(strict=True) == secret

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not (partial / "nginx/sites-enabled/outside.conf").exists()
    assert all(
        secret_bytes not in path.read_bytes()
        for path in partial.rglob("*")
        if path.is_file()
    )
    assert not (config.backup_root / "latest-success.json").exists()


@POSIX_SNAPSHOT_ONLY
def test_frontend_symlink_remains_invalid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (config.frontend_dist / "real.js").write_text("real asset\n", encoding="utf-8")
    (config.frontend_dist / "linked.js").symlink_to("real.js")

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    assert not (config.backup_root / "latest-success.json").exists()


@POSIX_SNAPSHOT_ONLY
def test_nginx_symlink_retarget_during_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    sites_available = config.nginx_directory / "sites-available"
    sites_enabled = config.nginx_directory / "sites-enabled"
    sites_available.mkdir()
    sites_enabled.mkdir()
    (sites_available / "original.conf").write_text("server original;\n", encoding="utf-8")
    (sites_available / "replacement.conf").write_text(
        "server replacement;\n", encoding="utf-8"
    )
    link = sites_enabled / "veltrix.conf"
    link.symlink_to("../sites-available/original.conf")
    real_copy = backup_module._copy_descriptor
    retargeted = False

    def copy_then_retarget(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal retargeted
        record = real_copy(*args, **kwargs)
        if record["path"] == "nginx/sites-enabled/veltrix.conf" and not retargeted:
            retargeted = True
            link.unlink()
            link.symlink_to("../sites-available/replacement.conf")
        return record

    monkeypatch.setattr(backup_module, "_copy_descriptor", copy_then_retarget)

    with pytest.raises(BackupError, match="^backup_source_changed$"):
        _run(config, FakeRunner())

    assert retargeted
    assert not (config.backup_root / "latest-success.json").exists()


def _mock_approved_symlink_source(
    monkeypatch: pytest.MonkeyPatch, target: Path
) -> tuple[os.stat_result, os.stat_result, os.stat_result]:
    target_info = target.lstat()
    parent_info = target.parent.lstat()
    link_info = SimpleNamespace(
        st_mode=stat.S_IFLNK | 0o777,
        st_dev=1,
        st_ino=2,
        st_size=len(str(target)),
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )
    monkeypatch.setattr(
        backup_module, "_open_absolute_directory", lambda *_: (42, parent_info)
    )
    monkeypatch.setattr(
        backup_module.os, "readlink", lambda *_, **__: str(target)
    )
    monkeypatch.setattr(
        backup_module.os,
        "stat",
        lambda *_, dir_fd=None, **__: link_info if dir_fd == 41 else target_info,
    )
    monkeypatch.setattr(backup_module.os, "O_NOFOLLOW", 0x10000000, raising=False)
    monkeypatch.setattr(backup_module.os, "O_NONBLOCK", 0x20000000, raising=False)
    return link_info, target_info, parent_info


def test_nginx_symlink_scan_bounds_global_file_count_before_sorting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace_config(_config(tmp_path), max_files=1)
    budget = backup_module._Budget(config, lambda: 0.0, 0.0)
    scanned: list[str] = []
    entries = (SimpleNamespace(name=f"link-{index}") for index in range(1000))
    link_info = SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)

    def fake_stat(name: str, **_kwargs: object) -> SimpleNamespace:
        scanned.append(name)
        return link_info

    with monkeypatch.context() as patch:
        patch.setattr(backup_module.os, "scandir", lambda _fd: nullcontext(entries))
        patch.setattr(backup_module.os, "stat", fake_stat)
        with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
            backup_module._scan_directory_fd(41, 0, budget, allow_symlinks=True)
    assert scanned == ["link-0", "link-1"]


def test_one_nginx_symlink_is_counted_once_and_charged_target_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = replace_config(_config(tmp_path), max_files=1)
    target = config.nginx_directory / "veltrix.conf"
    budget = backup_module._Budget(config, lambda: 0.0, 0.0)
    with monkeypatch.context() as patch:
        link_info, target_info, parent_info = _mock_approved_symlink_source(
            patch, target
        )
        patch.setattr(
            backup_module.os,
            "scandir",
            lambda _fd: nullcontext(iter([SimpleNamespace(name="linked")])),
        )
        patch.setattr(backup_module.os, "open", lambda *_args, **_kwargs: 43)
        patch.setattr(
            backup_module.os,
            "fstat",
            lambda descriptor: parent_info if descriptor == 42 else target_info,
        )
        patch.setattr(backup_module.os, "close", lambda _fd: None)
        patch.setattr(backup_module, "_copy_descriptor", lambda *_: {})
        entries = backup_module._scan_directory_fd(
            41, 0, budget, allow_symlinks=True
        )
        assert entries == [("linked", link_info)]
        backup_module._copy_approved_symlink(
            41,
            target.parent,
            "linked",
            link_info,
            tmp_path / "copied",
            Path("nginx/linked"),
            (config.nginx_directory,),
            budget,
        )
    assert budget.files == 1
    assert budget.total_bytes == target.stat().st_size


def test_approved_nginx_target_open_is_nonblocking_and_rejects_nonregular_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = config.nginx_directory / "veltrix.conf"
    budget = backup_module._Budget(config, lambda: 0.0, 0.0)
    with monkeypatch.context() as patch:
        link_info, _, _ = _mock_approved_symlink_source(patch, target)
        opened_flags: list[int] = []
        closed: list[int] = []

        def open_target(_name: str, flags: int, *, dir_fd: int) -> int:
            assert dir_fd == 42
            opened_flags.append(flags)
            return 43

        patch.setattr(backup_module.os, "open", open_target)
        patch.setattr(
            backup_module.os,
            "fstat",
            lambda _fd: SimpleNamespace(st_mode=stat.S_IFIFO, st_dev=3, st_ino=4),
        )
        patch.setattr(backup_module.os, "read", lambda *_: pytest.fail("FIFO read"))
        patch.setattr(backup_module.os, "close", closed.append)
        with pytest.raises(BackupError, match="^backup_source_changed$"):
            backup_module._copy_approved_symlink(
                41,
                target.parent,
                "linked",
                link_info,
                tmp_path / "copied",
                Path("nginx/linked"),
                (config.nginx_directory,),
                budget,
            )
        assert opened_flags[0] & backup_module.os.O_NONBLOCK
        assert closed == [43, 42]


def test_approved_nginx_cleanup_closes_both_fds_after_first_close_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = config.nginx_directory / "veltrix.conf"
    budget = backup_module._Budget(config, lambda: 0.0, 0.0)
    with monkeypatch.context() as patch:
        link_info, _, _ = _mock_approved_symlink_source(patch, target)
        closed: list[int] = []
        patch.setattr(backup_module.os, "open", lambda *_args, **_kwargs: 43)

        def failed_copy(*_args: object) -> dict[str, object]:
            raise BackupError("backup_source_changed")

        def close(descriptor: int) -> None:
            closed.append(descriptor)
            if descriptor == 43:
                raise OSError("synthetic close failure")

        patch.setattr(backup_module, "_copy_descriptor", failed_copy)
        patch.setattr(backup_module.os, "close", close)
        with pytest.raises(BackupError, match="^backup_source_changed$"):
            backup_module._copy_approved_symlink(
                41,
                target.parent,
                "linked",
                link_info,
                tmp_path / "copied",
                Path("nginx/linked"),
                (config.nginx_directory,),
                budget,
            )
        assert closed == [43, 42]


def test_approved_nginx_cleanup_failure_uses_static_backup_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    target = config.nginx_directory / "veltrix.conf"
    budget = backup_module._Budget(config, lambda: 0.0, 0.0)
    with monkeypatch.context() as patch:
        link_info, target_info, parent_info = _mock_approved_symlink_source(
            patch, target
        )
        closed: list[int] = []
        patch.setattr(backup_module.os, "open", lambda *_args, **_kwargs: 43)
        patch.setattr(
            backup_module.os,
            "fstat",
            lambda descriptor: parent_info if descriptor == 42 else target_info,
        )
        patch.setattr(backup_module, "_copy_descriptor", lambda *_: {})

        def close(descriptor: int) -> None:
            closed.append(descriptor)
            if descriptor == 43:
                raise OSError("synthetic secret close failure")

        patch.setattr(backup_module.os, "close", close)
        with pytest.raises(BackupError, match="^backup_copy_failed$") as raised:
            backup_module._copy_approved_symlink(
                41,
                target.parent,
                "linked",
                link_info,
                tmp_path / "copied",
                Path("nginx/linked"),
                (config.nginx_directory,),
                budget,
            )
        assert "synthetic" not in repr(raised.value)
        assert closed == [43, 42]


def test_symlink_source_is_rejected_without_following_it(tmp_path: Path) -> None:
    config = _config(tmp_path)
    secret = tmp_path / "outside-secret"
    secret.write_text("must not be copied", encoding="utf-8")
    link = config.nginx_directory / "linked-secret"
    try:
        link.symlink_to(secret)
    except OSError as error:
        pytest.skip(f"symlink unavailable: {error}")

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not any(path.name == "linked-secret" for path in partial.rglob("*"))


def test_backup_root_cannot_overlap_a_source_tree(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config = replace_config(config, nginx_directory=tmp_path)

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_backup_fails_before_writes_without_secure_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    monkeypatch.setattr(backup_module, "_DIR_FD_SUPPORTED", False)

    with pytest.raises(BackupError, match="^backup_platform_unsupported$"):
        _run(config, FakeRunner())

    assert list(config.backup_root.iterdir()) == []


def test_backup_root_must_be_owned_by_effective_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)
    monkeypatch.setattr(
        backup_module.os,
        "geteuid",
        lambda: root.stat().st_uid + 1,
        raising=False,
    )

    with pytest.raises(BackupError, match="^backup_root_invalid$"):
        backup_module._validated_root(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode validation")
def test_backup_root_rejects_group_or_world_access(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o750)

    with pytest.raises(BackupError, match="^backup_root_invalid$"):
        backup_module._validated_root(root)


@pytest.mark.skipif(os.name != "posix", reason="POSIX ancestor mode validation")
def test_backup_root_rejects_writable_non_sticky_ancestor(tmp_path: Path) -> None:
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o700)
    config = _config(tmp_path, backup_root=unsafe / "backups")
    unsafe.chmod(0o777)

    with pytest.raises(BackupError, match="^backup_root_invalid$"):
        _run(config, FakeRunner())

    assert list(config.backup_root.iterdir()) == []


def test_bound_root_closes_unowned_child_when_fstat_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(Path.cwd().anchor) / "parent" / "backups"
    descriptors = iter([10, 11])
    open_attempts: list[int] = []
    close_attempts: list[int] = []
    directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_uid=1000,
        st_dev=1,
        st_ino=10,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )

    def fstat(descriptor: int) -> SimpleNamespace:
        if descriptor == 11:
            raise OSError("synthetic fstat failure")
        return directory

    def open_descriptor(*_args, **_kwargs) -> int:
        descriptor = next(descriptors)
        open_attempts.append(descriptor)
        return descriptor

    monkeypatch.setattr(backup_module.os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(backup_module, "_same_path", lambda *_args: True)
    monkeypatch.setattr(backup_module.os, "open", open_descriptor)
    monkeypatch.setattr(backup_module.os, "stat", lambda *_args, **_kwargs: directory)
    monkeypatch.setattr(backup_module.os, "fstat", fstat)
    monkeypatch.setattr(backup_module.os, "close", close_attempts.append)

    with (
        pytest.raises(BackupError, match="^backup_root_invalid$"),
        _BOUND_BACKUP_ROOT(root, stable=True),
    ):
        pytest.fail("root binding unexpectedly succeeded")

    assert (open_attempts, close_attempts) == ([10, 11], [11, 10])


def test_bound_root_does_not_retry_failed_close_and_closes_owned_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(Path.cwd().anchor) / "parent" / "backups"
    descriptors = iter([10, 11])
    owned_descriptors: set[int] = set()
    reused_descriptors: set[int] = set()
    close_attempts: list[int] = []
    directory = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_uid=1000,
        st_dev=1,
        st_ino=10,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )

    def open_descriptor(*_args, **_kwargs) -> int:
        descriptor = next(descriptors)
        owned_descriptors.add(descriptor)
        return descriptor

    def close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        if descriptor == 10 and close_attempts.count(descriptor) == 1:
            owned_descriptors.remove(descriptor)
            reused_descriptors.add(descriptor)
            raise OSError("synthetic close failure")
        if descriptor in owned_descriptors:
            owned_descriptors.remove(descriptor)
        elif descriptor in reused_descriptors:
            reused_descriptors.remove(descriptor)
        else:
            raise AssertionError("descriptor closed more than once")

    monkeypatch.setattr(backup_module.os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(backup_module, "_same_path", lambda *_args: True)
    monkeypatch.setattr(backup_module.os, "open", open_descriptor)
    monkeypatch.setattr(backup_module.os, "stat", lambda *_args, **_kwargs: directory)
    monkeypatch.setattr(backup_module.os, "fstat", lambda _descriptor: directory)
    monkeypatch.setattr(backup_module.os, "close", close)

    with (
        pytest.raises(BackupError, match="^backup_root_invalid$"),
        _BOUND_BACKUP_ROOT(root, stable=True),
    ):
        pytest.fail("root binding unexpectedly succeeded")

    assert close_attempts == [10, 11]
    assert owned_descriptors == set()
    assert reused_descriptors == {10}


def test_bound_root_cleanup_attempts_every_close_after_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(Path.cwd().anchor) / "backups"
    descriptors = iter([10, 11])
    open_attempts: list[int] = []
    close_attempts: list[int] = []
    parent = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_uid=1000,
        st_dev=1,
        st_ino=10,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )
    bound = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_uid=1000,
        st_dev=1,
        st_ino=11,
        st_mtime_ns=1,
        st_ctime_ns=1,
        st_file_attributes=0,
    )

    def close(descriptor: int) -> None:
        close_attempts.append(descriptor)
        if descriptor == 11:
            raise OSError("synthetic close failure")

    def open_descriptor(*_args, **_kwargs) -> int:
        descriptor = next(descriptors)
        open_attempts.append(descriptor)
        return descriptor

    monkeypatch.setattr(backup_module.os, "O_DIRECTORY", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "O_NOFOLLOW", 0, raising=False)
    monkeypatch.setattr(backup_module.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setattr(backup_module, "_same_path", lambda *_args: True)
    monkeypatch.setattr(backup_module.os, "open", open_descriptor)
    monkeypatch.setattr(
        backup_module.os,
        "stat",
        lambda *_args, **_kwargs: bound,
    )
    monkeypatch.setattr(
        backup_module.os,
        "fstat",
        lambda descriptor: parent if descriptor == 10 else bound,
    )
    monkeypatch.setattr(backup_module.os, "close", close)

    with (
        pytest.raises(BackupError, match="^backup_root_invalid$"),
        _BOUND_BACKUP_ROOT(root, stable=True),
    ):
        pass

    assert (open_attempts, close_attempts) == ([10, 11], [11, 10])


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_DIRECTORY"),
    reason="retained root descriptors are a POSIX guarantee",
)
def test_backup_root_swap_after_lock_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = config.backup_root.with_name("backups-original")

    @contextmanager
    def swapping_lock(root: Path):
        root.rename(original)
        root.mkdir(mode=0o700)
        yield

    monkeypatch.setattr(backup_module, "_exclusive_backup_root", swapping_lock)

    with pytest.raises(BackupError, match="^backup_root_invalid$"):
        _run(config, FakeRunner())

    assert list(config.backup_root.iterdir()) == []


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_DIRECTORY"),
    reason="secure dir-fd traversal is a POSIX guarantee",
)
def test_source_root_swap_cannot_copy_from_replacement_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    original = config.nginx_directory.with_name("nginx-original")
    outside = tmp_path / "outside-nginx"
    outside.mkdir()
    secret = b"outside-secret-must-not-be-copied"
    (outside / "veltrix.conf").write_bytes(secret)
    real_open = os.open
    swapped = False

    def swapping_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal swapped
        descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
        if (
            not swapped
            and path == config.nginx_directory.name
            and flags & os.O_DIRECTORY
            and dir_fd is not None
        ):
            swapped = True
            config.nginx_directory.rename(original)
            config.nginx_directory.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(backup_module.os, "open", swapping_open)

    with pytest.raises(BackupError, match="^backup_source_(invalid|changed)$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert secret not in b"".join(
        path.read_bytes() for path in partial.rglob("*") if path.is_file()
    )


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO creation is POSIX-only")
def test_special_source_file_is_rejected(tmp_path: Path) -> None:
    config = _config(tmp_path)
    os.mkfifo(config.frontend_dist / "unexpected-pipe")

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())


@pytest.mark.parametrize(
    "change",
    [
        {"max_files": 4},
        {"max_file_bytes": 10},
        {"max_total_bytes": 30},
    ],
)
def test_file_count_and_size_bounds_fail_closed(
    tmp_path: Path, change: dict[str, int]
) -> None:
    config = _config(tmp_path, **change)

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        _run(config, FakeRunner())


def test_enumeration_file_limit_fails_before_publication(tmp_path: Path) -> None:
    config = _config(tmp_path, max_files=5)
    for index in range(6):
        (config.frontend_dist / f"extra-{index}.js").write_bytes(b"x")
    runner = FakeRunner()

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        _run(config, runner)

    assert [command[0] for command, _kwargs in runner.calls] == [
        "/usr/bin/pg_dump",
        "/usr/bin/pg_restore",
    ]
    assert not (config.backup_root / "latest-success.json").exists()


def test_source_tree_depth_is_bounded_before_publication(tmp_path: Path) -> None:
    config = _config(tmp_path)
    nested = config.frontend_dist
    for name in ("one", "two", "three"):
        nested /= name
        nested.mkdir()
    runner = FakeRunner()

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        _run(replace_config(config, max_depth=2), runner)

    assert [command[0] for command, _kwargs in runner.calls] == [
        "/usr/bin/pg_dump",
        "/usr/bin/pg_restore",
    ]
    assert not (config.backup_root / "latest-success.json").exists()


def test_oversized_dump_is_rejected_before_restore_reads_it(tmp_path: Path) -> None:
    config = _config(tmp_path, max_file_bytes=10)
    runner = FakeRunner()

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        _run(config, runner)

    assert [command[0] for command, _kwargs in runner.calls] == ["/usr/bin/pg_dump"]


def test_dump_growth_is_bounded_while_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, max_file_bytes=10)
    dump_identity: tuple[int, int] | None = None
    calls: list[str] = []

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        nonlocal dump_identity
        calls.append(command[0])
        if command[0] == "/usr/bin/pg_dump":
            dump = Path(command[3])
            dump.write_bytes(b"x")
            dump_identity = backup_module._file_identity(dump.stat())
        return subprocess.CompletedProcess(command, 0)

    real_read = os.read
    injected = False

    def growing_read(descriptor: int, size: int) -> bytes:
        nonlocal injected
        if dump_identity == backup_module._file_identity(os.fstat(descriptor)):
            chunk = real_read(descriptor, size)
            if not chunk and not injected:
                injected = True
                return b"y" * 20
            return chunk
        return real_read(descriptor, size)

    monkeypatch.setattr(backup_module.os, "read", growing_read)

    with pytest.raises(BackupError, match="^backup_limits_exceeded$"):
        create_validated_backup(
            config,
            f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
            runner=runner,
            now=lambda: NOW,
            nonce=lambda: NONCE,
        )

    assert calls == ["/usr/bin/pg_dump"]


@pytest.mark.skipif(
    os.name != "posix" or getattr(backup_module, "resource", None) is None,
    reason="RLIMIT_FSIZE is POSIX-only",
)
def test_default_runner_caps_dump_during_child_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    limit = 4096
    config = _config(
        tmp_path,
        max_file_bytes=limit * 2,
        max_total_bytes=limit,
    )
    writer = tmp_path / "bounded-pg-dump"
    writer.write_text(
        f"#!{sys.executable}\n"
        "import pathlib, sys\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('--file') + 1])\n"
        "state = output.parent / 'writer.state'\n"
        "state.write_text('started', encoding='utf-8')\n"
        "output.write_bytes(b'x' * 65536)\n"
        "state.write_text('finished', encoding='utf-8')\n",
        encoding="utf-8",
    )
    writer.chmod(0o700)
    monkeypatch.setattr(backup_module, "_PG_DUMP", str(writer))

    with pytest.raises(BackupError, match="^backup_dump_failed$"):
        _run(config, backup_module._default_runner)

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert (partial / "writer.state").read_text(encoding="utf-8") == "started"
    assert (partial / "database.dump").stat().st_size <= limit


def test_stable_file_check_includes_posix_ctime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o600,
        st_dev=1,
        st_ino=2,
        st_size=3,
        st_mtime_ns=4,
        st_ctime_ns=5,
        st_file_attributes=0,
    )
    changed = SimpleNamespace(**vars(expected))
    changed.st_ctime_ns = 6
    monkeypatch.setattr(backup_module.os, "name", "posix")

    assert not backup_module._matches_stable_file(changed, expected)


def test_directory_identity_includes_mtime_and_posix_ctime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = SimpleNamespace(
        st_mode=stat.S_IFDIR | 0o700,
        st_dev=1,
        st_ino=2,
        st_size=0,
        st_mtime_ns=4,
        st_ctime_ns=5,
        st_file_attributes=0,
    )
    changed = SimpleNamespace(**vars(expected))
    changed.st_mtime_ns = 6
    changed.st_ctime_ns = 7
    monkeypatch.setattr(backup_module.os, "name", "posix")

    assert not backup_module._matches_identity(changed, expected, directory=True)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "O_DIRECTORY"),
    reason="stable directory traversal is a POSIX guarantee",
)
def test_source_directory_addition_during_traversal_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    real_copy = backup_module._copy_descriptor
    mutated = False

    def copy_then_mutate(*args: object, **kwargs: object) -> dict[str, object]:
        nonlocal mutated
        record = real_copy(*args, **kwargs)
        if record["path"] == "nginx/veltrix.conf":
            mutated = True
            (config.nginx_directory / "added.conf").write_text(
                "server_name added;\n", encoding="utf-8"
            )
        return record

    monkeypatch.setattr(backup_module, "_copy_descriptor", copy_then_mutate)

    with pytest.raises(BackupError, match="^backup_source_changed$"):
        _run(config, FakeRunner())

    assert mutated
    assert not (config.backup_root / "latest-success.json").exists()


def test_deadline_is_enforced_during_copy(tmp_path: Path) -> None:
    config = _config(tmp_path, deadline_seconds=1.0)
    ticks = iter([0.0, 0.1, 0.2, 0.3, 1.1])

    with pytest.raises(BackupError, match="^backup_deadline_exceeded$"):
        _run(config, FakeRunner(), monotonic=lambda: next(ticks, 1.1))


@pytest.mark.parametrize(
    "url",
    [
        "sqlite:///tmp/app.db",
        "postgresql+unknown://user:pass@db:5432/name",
        "postgresql://user:pass@:5432/name",
        "postgresql://:pass@db:5432/name",
        "postgresql://user:pass@db:99999/name",
        "postgresql://user:pass@db:5432/",
        "postgresql://user:pass@db:5432/name?sslmode=require",
        "postgresql://user:pass@db:5432/name#fragment",
        "postgresql://user:pass@db:5432/name/extra",
        "postgresql://user:pass@db%2Finternal:5432/name",
    ],
)
def test_unsafe_database_urls_are_rejected_before_creating_a_set(
    tmp_path: Path, url: str
) -> None:
    config = _config(tmp_path)

    with pytest.raises(BackupError, match="^backup_database_url_invalid$") as raised:
        create_validated_backup(config, url, runner=FakeRunner(), now=lambda: NOW)

    assert url not in repr(raised.value)
    assert list(config.backup_root.iterdir()) == []


def test_database_url_components_are_percent_decoded_exactly_once(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    runner = FakeRunner()

    create_validated_backup(
        config,
        "postgres://user%2520name:p%2540ss@db.internal/db%252Fname",
        runner=runner,
        now=lambda: NOW,
        nonce=lambda: NONCE,
    )

    command, kwargs = runner.calls[0]
    assert command[-1] == "db%2Fname"
    assert kwargs["env"]["PGUSER"] == "user%20name"
    assert kwargs["env"]["PGPASSWORD"] == "p%40ss"
    assert kwargs["env"]["PGPORT"] == "5432"


def test_exclusive_set_creation_never_overwrites_a_collision(tmp_path: Path) -> None:
    config = _config(tmp_path)
    collision = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    collision.mkdir()
    sentinel = collision / "keep"
    sentinel.write_text("existing", encoding="utf-8")

    with pytest.raises(BackupError, match="^backup_set_collision$"):
        _run(config, FakeRunner())

    assert sentinel.read_text(encoding="utf-8") == "existing"


def test_exclusive_lock_is_held_through_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    active = False

    @contextmanager
    def lock(_root: Path):
        nonlocal active
        active = True
        try:
            yield
        finally:
            active = False

    real_retention = backup_module._apply_retention

    def apply_retention(root: Path, retention: int, current: str) -> int:
        assert active
        return real_retention(root, retention, current)

    monkeypatch.setattr(backup_module, "_exclusive_backup_root", lock, raising=False)
    monkeypatch.setattr(backup_module, "_apply_retention", apply_retention)

    _run(config, FakeRunner())

    assert not active


@pytest.mark.skipif(
    not backup_module._DIR_FD_SUPPORTED,
    reason="production flock is POSIX-only",
)
def test_overlapping_backup_lock_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "backups"
    root.mkdir(mode=0o700)

    with (
        backup_module._exclusive_backup_root(root),
        pytest.raises(BackupError, match="^backup_busy$"),
        backup_module._exclusive_backup_root(root),
    ):
        raise AssertionError("overlapping backup acquired the lock")


def test_marker_replace_failure_rolls_promoted_set_back_to_partial(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    prior = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((prior / "backup.json").read_bytes())
    before = marker.read_bytes()

    def replace(source: Path, target: Path) -> None:
        if Path(target) == marker:
            raise OSError("synthetic marker failure containing no useful detail")
        os.replace(source, target)

    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner(), replace=replace)

    assert marker.read_bytes() == before
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_final_rename_failure_preserves_previous_marker_and_partial(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    prior = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((prior / "backup.json").read_bytes())
    before = marker.read_bytes()
    final = config.backup_root / f"20260924T031011.000000Z-{NONCE}"

    def replace(source: Path, target: Path) -> None:
        if Path(target) == final:
            raise OSError("synthetic final rename failure")
        os.replace(source, target)

    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner(), replace=replace)

    assert marker.read_bytes() == before
    _assert_marker_target_exists(config.backup_root)
    assert final.with_name(final.name + ".partial").is_dir()


def test_existing_marker_is_snapshotted_through_one_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((previous / "backup.json").read_bytes())
    real_read_bytes = Path.read_bytes

    def reject_marker_reopen(path: Path) -> bytes:
        if path == marker:
            raise AssertionError("marker path was reopened after validation")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_marker_reopen)

    result = _run(config, FakeRunner())

    assert result.directory.is_dir()


def test_directory_fsync_after_promotion_preserves_previous_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    prior = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((prior / "backup.json").read_bytes())
    before = marker.read_bytes()
    real_sync = backup_module._sync_directory
    failed = False

    def fail_first_root_sync(path: Path) -> None:
        nonlocal failed
        if Path(path) == config.backup_root and not failed:
            failed = True
            raise BackupError("backup_write_failed")
        real_sync(path)

    monkeypatch.setattr(backup_module, "_sync_directory", fail_first_root_sync)
    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner())

    assert marker.read_bytes() == before
    _assert_marker_target_exists(config.backup_root)
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_marker_temp_write_failure_preserves_previous_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    prior = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((prior / "backup.json").read_bytes())
    before = marker.read_bytes()
    real_write = backup_module._write_private_file

    def fail_marker_temp(path: Path, content: bytes) -> None:
        if path.name.startswith(".latest-success"):
            raise BackupError("backup_write_failed")
        real_write(path, content)

    monkeypatch.setattr(backup_module, "_write_private_file", fail_marker_temp)
    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner())

    assert marker.read_bytes() == before
    _assert_marker_target_exists(config.backup_root)
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_success_never_chmods_marker_after_atomic_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    marker = config.backup_root / "latest-success.json"
    real_chmod = Path.chmod

    def chmod(path: Path, mode: int) -> None:
        if path == marker:
            raise OSError("post-replace chmod must not happen")
        real_chmod(path, mode)

    monkeypatch.setattr(Path, "chmod", chmod)

    result = _run(config, FakeRunner())

    assert _read_json(marker)["set_name"] == result.directory.name
    assert result.directory.is_dir()


def test_failed_marker_restoration_never_demotes_its_live_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((previous / "backup.json").read_bytes())
    before = marker.read_bytes()
    real_sync = backup_module._sync_directory
    real_replace = os.replace

    def fail_after_marker_replace(path: Path) -> None:
        if Path(path) == config.backup_root and marker.read_bytes() != before:
            raise BackupError("backup_write_failed")
        real_sync(path)

    def fail_rollback_replace(source: Path, target: Path) -> None:
        if Path(source).name.endswith(".rollback"):
            raise OSError("synthetic rollback failure")
        real_replace(source, target)

    monkeypatch.setattr(backup_module, "_sync_directory", fail_after_marker_replace)
    monkeypatch.setattr(backup_module.os, "replace", fail_rollback_replace)

    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner())

    live = _read_json(marker)["set_name"]
    assert (config.backup_root / live).is_dir()


def test_marker_rollback_never_clobbers_a_concurrent_success(tmp_path: Path) -> None:
    config = _config(tmp_path)
    previous = _write_success(
        config.backup_root, "20260920T031011.000000Z-aaaaaaaa"
    )
    concurrent = _write_success(
        config.backup_root, "20260922T031011.000000Z-cccccccc"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((previous / "backup.json").read_bytes())
    final = _write_success(
        config.backup_root, f"20260921T031011.000000Z-{NONCE}"
    )
    partial = final.with_name(final.name + ".partial")
    final.rename(partial)
    marker_temp = config.backup_root / f".latest-success.{NONCE}.tmp"
    metadata = (partial / "backup.json").read_bytes()

    def racing_replace(source: Path, target: Path) -> None:
        os.replace(source, target)
        if Path(target) == marker and Path(source) == marker_temp:
            marker.write_bytes((concurrent / "backup.json").read_bytes())
            shutil.rmtree(previous)
            raise OSError("another successful publisher won")

    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        backup_module._publish_backup(
            root=config.backup_root,
            partial=partial,
            final=final,
            marker=marker,
            marker_temp=marker_temp,
            metadata=metadata,
            nonce=NONCE,
            replace=racing_replace,
        )

    assert _read_json(marker)["set_name"] == concurrent.name
    assert concurrent.is_dir()
    assert final.is_dir()
    assert not partial.exists()


def test_fsync_failure_leaves_partial_and_previous_marker_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    marker = config.backup_root / "latest-success.json"
    marker.write_text('{"previous":true}\n', encoding="utf-8")
    before = marker.read_bytes()

    def fail_fsync(_fd: int) -> None:
        raise OSError("synthetic fsync output must not escape")

    monkeypatch.setattr("app.operations.backup.os.fsync", fail_fsync)
    with pytest.raises(BackupError, match="^backup_dump_failed$") as raised:
        _run(config, FakeRunner())

    assert "synthetic" not in repr(raised.value)
    assert marker.read_bytes() == before
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_marker_directory_fsync_failure_restores_previous_marker_and_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    previous = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((previous / "backup.json").read_bytes())
    before = marker.read_bytes()
    real_sync = backup_module._sync_directory

    def fail_after_marker_replace(path: Path) -> None:
        if Path(path) == config.backup_root and marker.read_bytes() != before:
            raise BackupError("backup_write_failed")
        real_sync(path)

    monkeypatch.setattr(backup_module, "_sync_directory", fail_after_marker_replace)
    with pytest.raises(BackupError, match="^backup_publish_failed$"):
        _run(config, FakeRunner())

    assert marker.read_bytes() == before
    assert previous.is_dir()
    assert (config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial").is_dir()


def test_retention_deletes_only_old_trusted_successes(tmp_path: Path) -> None:
    config = _config(tmp_path, retention=2)
    oldest = _write_success(config.backup_root, "20260920T031011.000000Z-aaaaaaaa")
    middle = _write_success(config.backup_root, "20260921T031011.000000Z-bbbbbbbb")
    newest = _write_success(config.backup_root, "20260922T031011.000000Z-cccccccc")
    unrelated = config.backup_root / "operator-notes"
    unrelated.mkdir()
    partial = config.backup_root / "20260919T031011.000000Z-dddddddd.partial"
    partial.mkdir()
    forged = config.backup_root / "20260918T031011.000000Z-eeeeeeee"
    forged.mkdir()
    (forged / "backup.json").write_text('{"status":"successful"}', encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_sentinel = outside / "keep"
    outside_sentinel.write_text("safe", encoding="utf-8")
    symlink = config.backup_root / "20260917T031011.000000Z-ffffffff"
    try:
        symlink.symlink_to(outside, target_is_directory=True)
    except OSError:
        symlink = None

    result = _run(config, FakeRunner())

    assert not oldest.exists()
    assert not middle.exists()
    assert newest.is_dir()
    assert result.directory.is_dir()
    assert unrelated.is_dir()
    assert partial.is_dir()
    assert forged.is_dir()
    if symlink is not None:
        assert symlink.is_symlink()
    assert outside_sentinel.read_text(encoding="utf-8") == "safe"


def test_retention_keeps_current_marker_target_if_clock_moves_backward(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, retention=2)
    first = _write_success(config.backup_root, "20260922T031011.000000Z-aaaaaaaa")
    second = _write_success(config.backup_root, "20260923T031011.000000Z-bbbbbbbb")

    result = create_validated_backup(
        config,
        f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
        runner=FakeRunner(),
        now=lambda: datetime(2026, 9, 20, 3, 10, 11, tzinfo=UTC),
        nonce=lambda: NONCE,
    )

    assert not first.exists()
    assert second.is_dir()
    assert result.directory.is_dir()
    assert len(
        [
            child
            for child in config.backup_root.iterdir()
            if backup_module._trusted_success(child, config.backup_root)
        ]
    ) == config.retention
    assert _read_json(config.backup_root / "latest-success.json")["set_name"] == (
        result.directory.name
    )


def test_retention_failure_is_reported_after_successful_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, retention=2)
    for name in (
        "20260920T031011.000000Z-aaaaaaaa",
        "20260921T031011.000000Z-bbbbbbbb",
        "20260922T031011.000000Z-cccccccc",
    ):
        _write_success(config.backup_root, name)
    monkeypatch.setattr(backup_module, "_remove_safe_tree", lambda _path, _root: False)

    with pytest.raises(BackupError, match="^backup_retention_failed$"):
        _run(config, FakeRunner())

    marker = _read_json(config.backup_root / "latest-success.json")
    assert marker["set_name"] == f"20260924T031011.000000Z-{NONCE}"
    assert (config.backup_root / marker["set_name"]).is_dir()


@pytest.mark.parametrize("tamper", ["manifest", "marker"])
def test_retention_never_deletes_when_current_is_not_trusted(
    tmp_path: Path, tamper: str
) -> None:
    config = _config(tmp_path, retention=2)
    old = [
        _write_success(config.backup_root, name)
        for name in (
            "20260920T031011.000000Z-aaaaaaaa",
            "20260921T031011.000000Z-bbbbbbbb",
            "20260922T031011.000000Z-cccccccc",
        )
    ]
    current = _write_success(
        config.backup_root, "20260924T031011.000000Z-dddddddd"
    )
    marker = config.backup_root / "latest-success.json"
    marker.write_bytes((current / "backup.json").read_bytes())
    if tamper == "manifest":
        (current / "manifest.json").write_bytes(b"tampered\n")
    else:
        marker.write_bytes((old[-1] / "backup.json").read_bytes())

    with pytest.raises(BackupError, match="^backup_retention_failed$"):
        backup_module._apply_retention(
            config.backup_root, config.retention, current.name
        )

    assert all(path.is_dir() for path in [*old, current])


def test_config_rejects_relative_paths_and_invalid_retention(tmp_path: Path) -> None:
    sources = _sources(tmp_path)

    with pytest.raises(ValueError, match="absolute"):
        BackupConfig(backup_root=Path("relative"), **sources)
    with pytest.raises(ValueError, match="retention"):
        BackupConfig(backup_root=tmp_path / "backups", retention=1, **sources)
    with pytest.raises(ValueError, match="bounds"):
        BackupConfig(
            backup_root=tmp_path / "backups",
            command_timeout_seconds=float("inf"),
            **sources,
        )


def test_main_builds_backup_from_explicit_environment_without_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    environment = {
        "VPN_BACKUP_ENABLED": "true",
        "DB_URL": f"postgresql://backup:{PASSWORD}@db.internal:5432/veltrix",
        "VPN_BACKUP_DIRECTORY": str(config.backup_root),
        "VPN_BACKUP_ENV_FILE": str(config.env_file),
        "VPN_BACKUP_SYSTEMD_UNIT": str(config.systemd_unit),
        "VPN_BACKUP_NGINX_DIRECTORY": str(config.nginx_directory),
        "VPN_BACKUP_FRONTEND_DIST": str(config.frontend_dist),
        "VPN_BACKUP_RETENTION": "7",
    }
    captured: dict[str, object] = {}

    def create(received: BackupConfig, db_url: str) -> object:
        captured.update(config=received, db_url=db_url)
        return object()

    assert main(environment, create=create) == 0
    assert captured["db_url"] == environment["DB_URL"]
    received = captured["config"]
    assert isinstance(received, BackupConfig)
    assert (
        received.backup_root,
        received.env_file,
        received.systemd_unit,
        received.nginx_directory,
        received.frontend_dist,
        received.retention,
    ) == (
        config.backup_root,
        config.env_file,
        config.systemd_unit,
        config.nginx_directory,
        config.frontend_dist,
        7,
    )
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("enabled", [None, "", "false", "0", "off", "no"])
def test_main_disabled_is_a_silent_noop(
    enabled: str | None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {} if enabled is None else {"VPN_BACKUP_ENABLED": enabled}
    called = False

    def create(_config: BackupConfig, _db_url: str) -> object:
        nonlocal called
        called = True
        return object()

    assert main(environment, create=create) == 0
    assert called is False
    assert capsys.readouterr() == ("", "")


def test_main_invalid_enabled_value_fails_closed_without_running(
    capsys: pytest.CaptureFixture[str],
) -> None:
    called = False

    def create(_config: BackupConfig, _db_url: str) -> object:
        nonlocal called
        called = True
        return object()

    assert main({"VPN_BACKUP_ENABLED": "perhaps"}, create=create) == 1
    assert called is False
    assert capsys.readouterr() == ("", "")


def test_main_fails_closed_without_echoing_invalid_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {"VPN_BACKUP_ENABLED": "true", "DB_URL": PASSWORD}

    assert main(environment) == 1
    output = capsys.readouterr()
    assert output == ("", "")
    assert PASSWORD not in repr(output)


def test_main_catches_unexpected_secret_exception_without_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    environment = {
        "VPN_BACKUP_ENABLED": "true",
        "DB_URL": PASSWORD,
        "VPN_BACKUP_DIRECTORY": str(config.backup_root),
        "VPN_BACKUP_ENV_FILE": str(config.env_file),
        "VPN_BACKUP_SYSTEMD_UNIT": str(config.systemd_unit),
        "VPN_BACKUP_NGINX_DIRECTORY": str(config.nginx_directory),
        "VPN_BACKUP_FRONTEND_DIST": str(config.frontend_dist),
    }

    def create(_config: BackupConfig, _db_url: str) -> object:
        raise RuntimeError(PASSWORD)

    assert main(environment, create=create) == 1
    output = capsys.readouterr()
    assert output == ("", "")
    assert PASSWORD not in repr(output)
