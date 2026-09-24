from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager, nullcontext
from dataclasses import replace as replace_config
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import app.operations.backup as backup_module
import pytest
from app.operations.backup import (
    BackupConfig,
    BackupError,
    create_validated_backup,
    main,
)

NOW = datetime(2026, 9, 24, 3, 10, 11, tzinfo=timezone.utc)
NONCE = "abcdef0123456789"
PASSWORD = "never-print-this-password"


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
        monkeypatch.setattr(backup_module, "_require_secure_platform", lambda: None)
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
    created = datetime.strptime(name[:15], "%Y%m%dT%H%M%S").replace(tzinfo=timezone.utc)
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

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        nonlocal replacement
        replacement = Path(command[3])
        replacement.unlink()
        replacement.write_bytes(b"replacement archive")
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
        assert stat.S_IMODE(replacement.stat().st_mode) == 0o600


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
        now=lambda: datetime(2026, 9, 20, 3, 10, 11, tzinfo=timezone.utc),
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


def test_main_fails_closed_without_echoing_invalid_environment(
    capsys: pytest.CaptureFixture[str],
) -> None:
    environment = {"DB_URL": PASSWORD}

    assert main(environment) == 1
    output = capsys.readouterr()
    assert output == ("", "")
    assert PASSWORD not in repr(output)


def test_main_catches_unexpected_secret_exception_without_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _config(tmp_path)
    environment = {
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
