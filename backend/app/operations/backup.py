from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import stat
import subprocess
import time
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - exercised by the platform guard
    fcntl = None  # type: ignore[assignment]

try:
    import resource
except ImportError:  # pragma: no cover - resource is POSIX-only
    resource = None  # type: ignore[assignment]

_PG_DUMP = "/usr/bin/pg_dump"
_PG_RESTORE = "/usr/bin/pg_restore"
_DATABASE_SCHEMES = {
    "postgres",
    "postgres+asyncpg",
    "postgresql",
    "postgresql+asyncpg",
}
_SET_NAME = re.compile(r"\A\d{8}T\d{6}\.\d{6}Z-[a-z0-9]{8,64}\Z")
_NONCE = re.compile(r"\A[a-z0-9]{8,64}\Z")
_HEX_DIGEST = re.compile(r"\A[0-9a-f]{64}\Z")
_HOSTNAME = re.compile(r"\A[0-9A-Za-z._:-]+\Z")
_REPARSE_POINT = 0x400
_CHUNK_SIZE = 1024 * 1024
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024


class BackupError(RuntimeError):
    """A deliberately detail-free operational backup failure."""


@dataclass(frozen=True, slots=True)
class BackupConfig:
    backup_root: Path
    env_file: Path
    systemd_unit: Path
    nginx_directory: Path
    frontend_dist: Path
    retention: int = 7
    command_timeout_seconds: float = 900.0
    deadline_seconds: float = 3600.0
    max_files: int = 50_000
    max_depth: int = 64
    max_file_bytes: int = 8 * 1024 * 1024 * 1024
    max_total_bytes: int = 32 * 1024 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in (
            "backup_root",
            "env_file",
            "systemd_unit",
            "nginx_directory",
            "frontend_dist",
        ):
            path = Path(getattr(self, field))
            if not path.is_absolute():
                raise ValueError(f"{field} must be absolute")
            object.__setattr__(self, field, path)
        if not 2 <= self.retention <= 31:
            raise ValueError("retention must be between 2 and 31")
        if (
            not math.isfinite(self.command_timeout_seconds)
            or not math.isfinite(self.deadline_seconds)
            or self.command_timeout_seconds <= 0
            or self.deadline_seconds <= 0
            or self.max_files <= 0
            or not 1 <= self.max_depth <= 256
            or self.max_file_bytes <= 0
            or self.max_total_bytes <= 0
        ):
            raise ValueError("backup bounds must be positive")


@dataclass(frozen=True, slots=True)
class BackupResult:
    directory: Path
    created_at: str
    manifest_sha256: str
    deleted_sets: int


@dataclass(slots=True)
class _Budget:
    config: BackupConfig
    monotonic: Callable[[], float]
    started_at: float
    files: int = 0
    directories: int = 0
    total_bytes: int = 0

    def check_deadline(self) -> None:
        if self.monotonic() - self.started_at > self.config.deadline_seconds:
            raise BackupError("backup_deadline_exceeded")

    def command_timeout(self) -> float:
        elapsed = self.monotonic() - self.started_at
        remaining = self.config.deadline_seconds - elapsed
        if remaining <= 0:
            raise BackupError("backup_deadline_exceeded")
        return min(self.config.command_timeout_seconds, remaining)

    def add_file(self, size: int) -> None:
        self.check_deadline()
        if size < 0 or size > self.config.max_file_bytes:
            raise BackupError("backup_limits_exceeded")
        self.files += 1
        self.total_bytes += size
        if (
            self.files > self.config.max_files
            or self.total_bytes > self.config.max_total_bytes
        ):
            raise BackupError("backup_limits_exceeded")

    def add_directory(self) -> None:
        self.check_deadline()
        self.directories += 1
        if self.directories > self.config.max_files:
            raise BackupError("backup_limits_exceeded")

    def check_copy_size(self, expected: int, copied: int) -> None:
        self.check_deadline()
        if (
            copied > self.config.max_file_bytes
            or self.total_bytes + max(0, copied - expected)
            > self.config.max_total_bytes
        ):
            raise BackupError("backup_limits_exceeded")


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validated_path(path: Path, *, directory: bool) -> os.stat_result:
    try:
        absolute = Path(os.path.abspath(path))
        resolved = path.resolve(strict=True)
        info = path.lstat()
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_source_invalid") from None
    expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    if not _same_path(absolute, resolved) or _is_reparse(info) or not expected:
        raise BackupError("backup_source_invalid")
    return info


def _validate_root_ancestors(path: Path) -> None:
    if os.name != "posix":
        return
    effective_uid = os.geteuid()
    try:
        for ancestor in reversed(Path(os.path.abspath(path)).parents):
            info = ancestor.lstat()
            mode = stat.S_IMODE(info.st_mode)
            sticky_system_temp = (
                info.st_uid == 0
                and bool(mode & stat.S_ISVTX)
                and bool(mode & 0o002)
            )
            if (
                not stat.S_ISDIR(info.st_mode)
                or _is_reparse(info)
                or info.st_uid not in {0, effective_uid}
                or (mode & 0o022 and not sticky_system_temp)
            ):
                raise BackupError("backup_root_invalid")
    except BackupError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_root_invalid") from None


def _validated_root_details(path: Path) -> tuple[Path, os.stat_result]:
    _validate_root_ancestors(path)
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_root_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or _is_reparse(info)
        or not _same_path(Path(os.path.abspath(path)), resolved)
        or info.st_uid != getattr(os, "geteuid", lambda: info.st_uid)()
        or (os.name == "posix" and stat.S_IMODE(info.st_mode) & 0o077 != 0)
    ):
        raise BackupError("backup_root_invalid")
    try:
        path.chmod(0o700)
        after = path.lstat()
    except OSError:
        raise BackupError("backup_root_invalid") from None
    if (
        _file_identity(after) != _file_identity(info)
        or not stat.S_ISDIR(after.st_mode)
        or _is_reparse(after)
    ):
        raise BackupError("backup_root_invalid")
    return resolved, after


def _validated_root(path: Path) -> Path:
    return _validated_root_details(path)[0]


def _validate_bound_root(
    root: Path, descriptor: int, expected: os.stat_result
) -> None:
    _validate_root_ancestors(root)
    try:
        opened = os.fstat(descriptor)
        linked = root.lstat()
        effective_uid = os.geteuid()
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_root_invalid") from None
    if (
        not stat.S_ISDIR(opened.st_mode)
        or not stat.S_ISDIR(linked.st_mode)
        or _is_reparse(opened)
        or _is_reparse(linked)
        or _file_identity(opened) != _file_identity(expected)
        or _file_identity(linked) != _file_identity(expected)
        or opened.st_uid != effective_uid
        or linked.st_uid != effective_uid
        or stat.S_IMODE(opened.st_mode) & 0o077
        or stat.S_IMODE(linked.st_mode) & 0o077
    ):
        raise BackupError("backup_root_invalid")


@contextmanager
def _bound_backup_root(path: Path):
    root, expected = _validated_root_details(path)
    descriptor = -1
    try:
        descriptor = os.open(
            root,
            os.O_RDONLY
            | os.O_DIRECTORY
            | os.O_NOFOLLOW
            | getattr(os, "O_CLOEXEC", 0),
        )
        validate = partial(_validate_bound_root, root, descriptor, expected)
        validate()
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_root_invalid") from None
    try:
        yield root, validate
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                raise BackupError("backup_root_invalid") from None


def _reject_source_overlap(config: BackupConfig, root: Path) -> None:
    try:
        files = (
            config.env_file.resolve(strict=True),
            config.systemd_unit.resolve(strict=True),
        )
        directories = (
            config.nginx_directory.resolve(strict=True),
            config.frontend_dist.resolve(strict=True),
        )
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_source_invalid") from None
    if any(path.is_relative_to(root) for path in files) or any(
        path == root or path.is_relative_to(root) or root.is_relative_to(path)
        for path in directories
    ):
        raise BackupError("backup_source_invalid")


def _decode_component(value: str | None, *, required: bool) -> str | None:
    if value is None:
        if required:
            raise ValueError
        return None
    decoded = unquote(value)
    if (
        (required and not decoded)
        or "\x00" in decoded
        or any(character in "\r\n" for character in decoded)
    ):
        raise ValueError
    return decoded


def _postgres_environment(db_url: str) -> tuple[str, dict[str, str]]:
    try:
        parsed = urlsplit(db_url)
        if (
            parsed.scheme.lower() not in _DATABASE_SCHEMES
            or parsed.query
            or parsed.fragment
            or not parsed.netloc
            or not parsed.path.startswith("/")
            or parsed.path.startswith("//")
        ):
            raise ValueError
        username = _decode_component(parsed.username, required=True)
        password = _decode_component(parsed.password, required=False)
        hostname = _decode_component(parsed.hostname, required=True)
        database = _decode_component(parsed.path[1:], required=True)
        if not _HOSTNAME.fullmatch(hostname) or "/" in database:
            raise ValueError
        port = parsed.port or 5432
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        raise BackupError("backup_database_url_invalid") from None

    environment = {
        "LC_ALL": "C",
        "PGHOST": hostname,
        "PGPORT": str(port),
        "PGUSER": username,
        "PGDATABASE": database,
    }
    if password is not None:
        environment["PGPASSWORD"] = password
    return database, environment


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _sync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError as error:
        if os.name == "nt" and error.errno in {13, 22}:
            return
        raise BackupError("backup_write_failed") from None
    try:
        os.fsync(descriptor)
    except OSError as error:
        if not (os.name == "nt" and error.errno in {9, 13, 22}):
            raise BackupError("backup_write_failed") from None
    finally:
        os.close(descriptor)


def _set_file_size_limit(limit: int) -> None:
    assert resource is not None
    _soft, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
    bounded = limit if hard == resource.RLIM_INFINITY else min(limit, hard)
    resource.setrlimit(resource.RLIMIT_FSIZE, (bounded, bounded))


def _run_command(
    runner: Callable[..., Any],
    command: list[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    error_code: str,
    file_size_limit: int | None = None,
) -> None:
    try:
        options: dict[str, object] = {
            "env": dict(environment),
            "timeout": timeout,
            "shell": False,
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if file_size_limit is not None:
            if os.name != "posix" or resource is None:
                raise BackupError(error_code)
            options["preexec_fn"] = partial(_set_file_size_limit, file_size_limit)
        completed = runner(command, **options)
        if completed.returncode != 0:
            raise BackupError(error_code)
    except BackupError:
        raise
    # An injected runner is untrusted here; its exception text may contain the env.
    except Exception:  # noqa: BLE001
        raise BackupError(error_code) from None


def _file_identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def _create_private_dump(path: Path) -> tuple[int, int]:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError
        return _file_identity(info)
    except OSError:
        raise BackupError("backup_dump_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _secure_dump_after_command(
    path: Path, identity: tuple[int, int]
) -> tuple[int, os.stat_result]:
    descriptor = -1
    try:
        flags = os.O_RDWR | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _file_identity(info) != identity:
            raise OSError
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        return descriptor, os.fstat(descriptor)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise BackupError("backup_dump_failed") from None


_DIR_FD_SUPPORTED = (
    os.name == "posix"
    and hasattr(os, "O_DIRECTORY")
    and hasattr(os, "O_NOFOLLOW")
    and os.open in os.supports_dir_fd
    and os.stat in os.supports_dir_fd
    and os.stat in os.supports_follow_symlinks
    and os.scandir in os.supports_fd
)


def _require_secure_platform() -> None:
    if not _DIR_FD_SUPPORTED or fcntl is None:
        raise BackupError("backup_platform_unsupported")


@contextmanager
def _exclusive_backup_root(root: Path):
    descriptor = -1
    try:
        descriptor = os.open(
            root / ".backup.lock",
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or _is_reparse(info):
            raise OSError
        os.fchmod(descriptor, 0o600)
        assert fcntl is not None
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if descriptor >= 0:
            os.close(descriptor)
        raise BackupError("backup_busy") from None
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise BackupError("backup_lock_failed") from None
    try:
        yield
    finally:
        os.close(descriptor)


def _matches_identity(
    info: os.stat_result, expected: os.stat_result, *, directory: bool
) -> bool:
    expected_type = (
        stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    )
    if expected.st_ino or expected.st_dev:
        same_object = _file_identity(info) == _file_identity(expected)
    else:
        same_object = info.st_ctime_ns == expected.st_ctime_ns
    stable_directory = not directory or (
        info.st_mtime_ns == expected.st_mtime_ns
        and (os.name != "posix" or info.st_ctime_ns == expected.st_ctime_ns)
    )
    return (
        expected_type and not _is_reparse(info) and same_object and stable_directory
    )


def _matches_stable_file(info: os.stat_result, expected: os.stat_result) -> bool:
    return (
        _matches_identity(info, expected, directory=False)
        and info.st_size == expected.st_size
        and info.st_mtime_ns == expected.st_mtime_ns
        and (os.name != "posix" or info.st_ctime_ns == expected.st_ctime_ns)
    )


def _copy_descriptor(
    source_fd: int,
    destination: Path,
    relative: Path,
    expected: os.stat_result,
    budget: _Budget,
) -> dict[str, object]:
    destination_fd = -1
    digest = hashlib.sha256()
    copied = 0
    try:
        opened = os.fstat(source_fd)
        if (
            not _matches_identity(opened, expected, directory=False)
            or opened.st_size != expected.st_size
        ):
            raise BackupError("backup_source_changed")
        _mkdir_private(destination.parent)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        while chunk := os.read(source_fd, _CHUNK_SIZE):
            copied += len(chunk)
            budget.check_copy_size(expected.st_size, copied)
            digest.update(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                if written <= 0:
                    raise OSError
                view = view[written:]
        if copied != expected.st_size or not _matches_stable_file(
            os.fstat(source_fd), expected
        ):
            raise BackupError("backup_source_changed")
        os.fchmod(destination_fd, 0o600)
        os.fsync(destination_fd)
        destination.chmod(0o600)
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_copy_failed") from None
    finally:
        if destination_fd >= 0:
            os.close(destination_fd)
    return {
        "path": relative.as_posix(),
        "sha256": digest.hexdigest(),
        "size": copied,
    }


def _open_absolute_directory(path: Path, budget: _Budget) -> tuple[int, os.stat_result]:
    expected_path = _validated_path(path, directory=True)
    absolute = Path(os.path.abspath(path))
    descriptor = -1
    try:
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        descriptor = os.open(absolute.anchor, flags)
        opened = os.fstat(descriptor)
        for component in absolute.parts[1:]:
            budget.check_deadline()
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
                raise BackupError("backup_source_invalid")
            child = os.open(component, flags, dir_fd=descriptor)
            child_info = os.fstat(child)
            if not _matches_identity(child_info, before, directory=True):
                os.close(child)
                raise BackupError("backup_source_changed")
            os.close(descriptor)
            descriptor = child
            opened = child_info
        after = path.lstat()
        if not _matches_identity(
            opened, expected_path, directory=True
        ) or not _matches_identity(after, expected_path, directory=True):
            raise BackupError("backup_source_changed")
        return descriptor, opened
    except BackupError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except (OSError, ValueError):
        if descriptor >= 0:
            os.close(descriptor)
        raise BackupError("backup_source_invalid") from None


def _scan_directory_fd(
    descriptor: int, depth: int, budget: _Budget
) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                budget.check_deadline()
                info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
                    raise BackupError("backup_source_invalid")
                if stat.S_ISDIR(info.st_mode):
                    if depth + 1 > budget.config.max_depth:
                        raise BackupError("backup_limits_exceeded")
                    budget.add_directory()
                elif stat.S_ISREG(info.st_mode):
                    budget.add_file(info.st_size)
                else:
                    raise BackupError("backup_source_invalid")
                entries.append((entry.name, info))
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_source_invalid") from None
    return sorted(entries, key=lambda item: item[0])


@dataclass(slots=True)
class _DirectoryFrame:
    descriptor: int
    source: Path
    destination: Path
    relative: Path
    depth: int
    expected: os.stat_result
    parent_name: str | None = None
    entries: list[tuple[str, os.stat_result]] | None = None
    index: int = 0


def _copy_posix_tree(
    source: Path,
    destination: Path,
    relative: Path,
    budget: _Budget,
) -> list[dict[str, object]]:
    descriptor, expected = _open_absolute_directory(source, budget)
    budget.add_directory()
    frames = [_DirectoryFrame(descriptor, source, destination, relative, 0, expected)]
    records: list[dict[str, object]] = []
    try:
        while frames:
            frame = frames[-1]
            budget.check_deadline()
            if frame.entries is None:
                _mkdir_private(frame.destination)
                frame.entries = _scan_directory_fd(
                    frame.descriptor, frame.depth, budget
                )
            if frame.index >= len(frame.entries):
                if not _matches_identity(
                    os.fstat(frame.descriptor), frame.expected, directory=True
                ):
                    raise BackupError("backup_source_changed")
                if len(frames) > 1:
                    parent = frames[-2]
                    linked = os.stat(
                        frame.parent_name,
                        dir_fd=parent.descriptor,
                        follow_symlinks=False,
                    )
                    if not _matches_identity(linked, frame.expected, directory=True):
                        raise BackupError("backup_source_changed")
                else:
                    linked = frame.source.lstat()
                    if not _matches_identity(linked, frame.expected, directory=True):
                        raise BackupError("backup_source_changed")
                os.close(frame.descriptor)
                frames.pop()
                continue

            name, entry_info = frame.entries[frame.index]
            frame.index += 1
            child_destination = frame.destination / name
            child_relative = frame.relative / name
            if stat.S_ISDIR(entry_info.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=frame.descriptor,
                )
                opened = os.fstat(child_fd)
                if not _matches_identity(opened, entry_info, directory=True):
                    os.close(child_fd)
                    raise BackupError("backup_source_changed")
                frames.append(
                    _DirectoryFrame(
                        child_fd,
                        frame.source / name,
                        child_destination,
                        child_relative,
                        frame.depth + 1,
                        entry_info,
                        name,
                    )
                )
                continue

            source_fd = -1
            try:
                source_fd = os.open(
                    name,
                    os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
                    dir_fd=frame.descriptor,
                )
                records.append(
                    _copy_descriptor(
                        source_fd,
                        child_destination,
                        child_relative,
                        entry_info,
                        budget,
                    )
                )
                linked = os.stat(name, dir_fd=frame.descriptor, follow_symlinks=False)
                if not _matches_stable_file(linked, entry_info):
                    raise BackupError("backup_source_changed")
            except BackupError:
                raise
            except OSError:
                raise BackupError("backup_source_invalid") from None
            finally:
                if source_fd >= 0:
                    os.close(source_fd)
    finally:
        for frame in frames:
            try:
                os.close(frame.descriptor)
            except OSError:
                pass
    return records


def _copy_posix_file(
    source: Path,
    destination: Path,
    relative: Path,
    budget: _Budget,
) -> dict[str, object]:
    parent_fd, parent_info = _open_absolute_directory(source.parent, budget)
    source_fd = -1
    try:
        expected = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if _is_reparse(expected) or not stat.S_ISREG(expected.st_mode):
            raise BackupError("backup_source_invalid")
        budget.add_file(expected.st_size)
        source_fd = os.open(
            source.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
            dir_fd=parent_fd,
        )
        record = _copy_descriptor(source_fd, destination, relative, expected, budget)
        linked = os.stat(source.name, dir_fd=parent_fd, follow_symlinks=False)
        if not _matches_stable_file(linked, expected):
            raise BackupError("backup_source_changed")
        if not _matches_identity(source.parent.lstat(), parent_info, directory=True):
            raise BackupError("backup_source_changed")
        return record
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_source_invalid") from None
    finally:
        if source_fd >= 0:
            os.close(source_fd)
        os.close(parent_fd)


def _copy_sources(
    config: BackupConfig, partial: Path, budget: _Budget
) -> list[dict[str, object]]:
    try:
        if not _DIR_FD_SUPPORTED:
            raise BackupError("backup_platform_unsupported")
        records = [
            _copy_posix_file(
                config.env_file,
                partial / "environment" / ".env",
                Path("environment") / ".env",
                budget,
            ),
            _copy_posix_file(
                config.systemd_unit,
                partial / "systemd" / config.systemd_unit.name,
                Path("systemd") / config.systemd_unit.name,
                budget,
            ),
        ]
        records.extend(
            _copy_posix_tree(
                config.nginx_directory,
                partial / "nginx",
                Path("nginx"),
                budget,
            )
        )
        records.extend(
            _copy_posix_tree(
                config.frontend_dist,
                partial / "frontend",
                Path("frontend"),
                budget,
            )
        )
        return records
    except BackupError:
        raise
    except (OSError, RecursionError):
        raise BackupError("backup_source_invalid") from None


def _record_dump(
    descriptor: int,
    name: str,
    expected: os.stat_result,
    budget: _Budget,
) -> dict[str, object]:
    try:
        if expected.st_size == 0:
            raise BackupError("backup_dump_failed")
        budget.add_file(expected.st_size)
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(descriptor, _CHUNK_SIZE):
            size += len(chunk)
            budget.check_copy_size(expected.st_size, size)
            digest.update(chunk)
        if size != expected.st_size or not _matches_stable_file(
            os.fstat(descriptor), expected
        ):
            raise BackupError("backup_source_changed")
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_dump_failed") from None
    return {"path": name, "sha256": digest.hexdigest(), "size": size}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _capped_json_bytes(value: object, max_bytes: int, error_code: str) -> bytes:
    output = bytearray()
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"))
    for chunk in encoder.iterencode(value):
        encoded = chunk.encode("utf-8")
        if len(output) + len(encoded) + 1 > max_bytes:
            raise BackupError(error_code)
        output.extend(encoded)
    output.append(0x0A)
    return bytes(output)


def _write_private_file(path: Path, content: bytes) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError
            view = view[written:]
        os.fchmod(descriptor, 0o600)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        path.chmod(0o600)
    except OSError:
        if descriptor >= 0:
            os.close(descriptor)
        raise BackupError("backup_write_failed") from None


def _trusted_marker_bytes(raw: bytes, root: Path) -> bool:
    try:
        metadata = json.loads(raw)
        set_name = metadata.get("set_name")
        return bool(
            set(metadata)
            == {"created_at", "manifest_sha256", "set_name", "status", "version"}
            and isinstance(set_name, str)
            and _SET_NAME.fullmatch(set_name)
            and _trusted_success(root / set_name, root)
        )
    except (UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        return False


def _read_existing_marker(path: Path, root: Path) -> bytes | None:
    descriptor = -1
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise BackupError("backup_publish_failed") from None
    if (
        _is_reparse(before)
        or not stat.S_ISREG(before.st_mode)
        or before.st_size > 16_384
    ):
        raise BackupError("backup_publish_failed")
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        if not _matches_stable_file(opened, before):
            raise BackupError("backup_publish_failed")
        chunks: list[bytes] = []
        size = 0
        while chunk := os.read(descriptor, min(_CHUNK_SIZE, 16_385 - size)):
            chunks.append(chunk)
            size += len(chunk)
            if size > 16_384:
                raise BackupError("backup_publish_failed")
        if size != opened.st_size or not _matches_stable_file(path.lstat(), opened):
            raise BackupError("backup_publish_failed")
        raw = b"".join(chunks)
        if not _trusted_marker_bytes(raw, root):
            raise BackupError("backup_publish_failed")
        return raw
    except BackupError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError, TypeError):
        raise BackupError("backup_publish_failed") from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _remove_private_temp(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _restore_marker(
    *,
    root: Path,
    marker: Path,
    previous: bytes | None,
    published: bytes,
    nonce: str,
    replace: Callable[[Path, Path], None],
) -> bool:
    rollback = root / f".latest-success.{nonce}.rollback"
    try:
        try:
            current = _read_existing_marker(marker, root)
        except BackupError:
            return False
        if current == previous:
            _sync_directory(root)
            return True
        if current != published:
            return False
        if previous is None:
            marker.unlink(missing_ok=True)
        else:
            if not _trusted_marker_bytes(previous, root):
                return False
            _write_private_file(rollback, previous)
            replace(rollback, marker)
        _sync_directory(root)
        return True
    except (BackupError, OSError, RecursionError):
        _remove_private_temp(rollback)
        return False


def _publish_backup(
    *,
    root: Path,
    partial: Path,
    final: Path,
    marker: Path,
    marker_temp: Path,
    metadata: bytes,
    nonce: str,
    replace: Callable[[Path, Path], None],
) -> None:
    promoted = False
    marker_replaced = False
    previous: bytes | None = None
    try:
        previous = _read_existing_marker(marker, root)
        _write_private_file(marker_temp, metadata)
        promoted = True
        replace(partial, final)
        _sync_directory(root)
        marker_replaced = True
        replace(marker_temp, marker)
        _sync_directory(root)
        return
    except (BackupError, OSError, RecursionError):
        marker_restored = not marker_replaced or _restore_marker(
            root=root,
            marker=marker,
            previous=previous,
            published=metadata,
            nonce=nonce,
            replace=replace,
        )
        if promoted and marker_restored:
            try:
                replace(final, partial)
                _sync_directory(root)
            except (BackupError, OSError, RecursionError):
                pass
        _remove_private_temp(marker_temp)
        raise BackupError("backup_publish_failed") from None


def _tree_is_safe(directory: Path) -> bool:
    try:
        for root, directories, files in os.walk(
            directory, topdown=True, followlinks=False
        ):
            root_info = Path(root).lstat()
            if _is_reparse(root_info) or not stat.S_ISDIR(root_info.st_mode):
                return False
            for name in [*directories, *files]:
                info = (Path(root) / name).lstat()
                if _is_reparse(info) or not (
                    stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)
                ):
                    return False
    except OSError:
        return False
    return True


def _trusted_success(directory: Path, root: Path) -> bool:
    if not _SET_NAME.fullmatch(directory.name):
        return False
    try:
        info = directory.lstat()
        if (
            _is_reparse(info)
            or not stat.S_ISDIR(info.st_mode)
            or directory.resolve(strict=True).parent != root
            or not _tree_is_safe(directory)
        ):
            return False
        metadata_path = directory / "backup.json"
        manifest_path = directory / "manifest.json"
        for path in (metadata_path, manifest_path):
            file_info = path.lstat()
            if _is_reparse(file_info) or not stat.S_ISREG(file_info.st_mode):
                return False
        if (
            metadata_path.stat().st_size > 16_384
            or manifest_path.stat().st_size > _MAX_MANIFEST_BYTES
        ):
            return False
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        created_at = datetime.strptime(
            str(metadata.get("created_at")), "%Y-%m-%dT%H:%M:%SZ"
        ).replace(tzinfo=timezone.utc)
        return (
            set(metadata)
            == {"created_at", "manifest_sha256", "set_name", "status", "version"}
            and metadata.get("version") == 1
            and metadata.get("status") == "successful"
            and metadata.get("set_name") == directory.name
            and created_at.strftime("%Y%m%dT%H%M%S") == directory.name[:15]
            and _HEX_DIGEST.fullmatch(str(metadata.get("manifest_sha256"))) is not None
            and metadata.get("manifest_sha256") == digest
        )
    except (
        OSError,
        UnicodeError,
        ValueError,
        json.JSONDecodeError,
        AttributeError,
        TypeError,
    ):
        return False


def _remove_safe_tree(directory: Path, root: Path) -> bool:
    if not _trusted_success(directory, root):
        return False
    try:
        entries = sorted(
            directory.rglob("*"), key=lambda path: len(path.parts), reverse=True
        )
        if any(path.is_symlink() or _is_reparse(path.lstat()) for path in entries):
            return False
        for path in entries:
            if path.is_dir():
                path.rmdir()
            elif path.is_file():
                path.unlink()
            else:
                return False
        directory.rmdir()
    except OSError:
        return False
    return True


def _apply_retention(root: Path, retention: int, current: str) -> int:
    try:
        candidates = sorted(
            (child for child in root.iterdir() if _trusted_success(child, root)),
            key=lambda path: path.name,
            reverse=True,
        )
    except OSError:
        raise BackupError("backup_retention_failed") from None
    current_path = next(
        (path for path in candidates if path.name == current),
        None,
    )
    if current_path is None:
        raise BackupError("backup_retention_failed")
    try:
        marker = _read_existing_marker(root / "latest-success.json", root)
        current_metadata = (current_path / "backup.json").read_bytes()
    except (BackupError, OSError):
        raise BackupError("backup_retention_failed") from None
    if marker is None or marker != current_metadata:
        raise BackupError("backup_retention_failed")
    others = [path.name for path in candidates if path.name != current]
    keep = {current, *others[: retention - 1]}
    deleted = 0
    for path in candidates:
        if path.name in keep:
            continue
        if not _remove_safe_tree(path, root):
            raise BackupError("backup_retention_failed")
        deleted += 1
    if deleted:
        try:
            _sync_directory(root)
        except BackupError:
            raise BackupError("backup_retention_failed") from None
    return deleted


def _format_created_at(value: datetime) -> tuple[str, str]:
    if value.tzinfo is None or value.utcoffset() is None:
        raise BackupError("backup_clock_invalid")
    utc = value.astimezone(timezone.utc)
    return (
        utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        utc.strftime("%Y%m%dT%H%M%S.%fZ"),
    )


def _default_runner(
    command: list[str], **kwargs: object
) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, **kwargs)


def create_validated_backup(
    config: BackupConfig,
    db_url: str,
    *,
    runner: Callable[..., Any] = _default_runner,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    nonce: Callable[[], str] = lambda: secrets.token_hex(8),
    monotonic: Callable[[], float] = time.monotonic,
    replace: Callable[[Path, Path], None] | None = None,
) -> BackupResult:
    """Create one validated set without exposing database credentials."""
    database, pg_environment = _postgres_environment(db_url)
    _require_secure_platform()
    with (
        _bound_backup_root(config.backup_root) as (root, validate_root),
        _exclusive_backup_root(root),
    ):
        validate_root()
        try:
            return _create_validated_backup_locked(
                config,
                database,
                pg_environment,
                root,
                validate_root=validate_root,
                runner=runner,
                now=now,
                nonce=nonce,
                monotonic=monotonic,
                replace=replace,
            )
        finally:
            validate_root()


def _create_validated_backup_locked(
    config: BackupConfig,
    database: str,
    pg_environment: Mapping[str, str],
    root: Path,
    *,
    validate_root: Callable[[], None],
    runner: Callable[..., Any],
    now: Callable[[], datetime],
    nonce: Callable[[], str],
    monotonic: Callable[[], float],
    replace: Callable[[Path, Path], None] | None,
) -> BackupResult:
    created_at, timestamp = _format_created_at(now())
    safe_nonce = nonce()
    if not _NONCE.fullmatch(safe_nonce):
        raise BackupError("backup_nonce_invalid")
    set_name = f"{timestamp}-{safe_nonce}"
    partial = root / f"{set_name}.partial"
    final = root / set_name
    budget = _Budget(config, monotonic, monotonic())
    try:
        partial.mkdir(mode=0o700, exist_ok=False)
        partial.chmod(0o700)
    except FileExistsError:
        raise BackupError("backup_set_collision") from None
    except OSError:
        raise BackupError("backup_write_failed") from None
    try:
        final.lstat()
    except FileNotFoundError:
        pass
    except OSError:
        raise BackupError("backup_set_collision") from None
    else:
        raise BackupError("backup_set_collision")

    marker = root / "latest-success.json"
    marker_temp = root / f".latest-success.{safe_nonce}.tmp"
    atomic_replace = os.replace if replace is None else replace
    try:
        _reject_source_overlap(config, root)
        dump = partial / "database.dump"
        dump_identity = _create_private_dump(dump)
        command_error: BackupError | None = None
        try:
            _run_command(
                runner,
                [
                    _PG_DUMP,
                    "--format=custom",
                    "--file",
                    str(dump),
                    "--dbname",
                    database,
                ],
                environment=pg_environment,
                timeout=budget.command_timeout(),
                error_code="backup_dump_failed",
                file_size_limit=(
                    min(config.max_file_bytes, config.max_total_bytes)
                    if runner is _default_runner
                    else None
                ),
            )
        except BackupError as error:
            command_error = error
        dump_fd, dump_info = _secure_dump_after_command(dump, dump_identity)
        try:
            if command_error is not None:
                raise command_error
            dump_record = _record_dump(dump_fd, dump.name, dump_info, budget)
            _run_command(
                runner,
                [_PG_RESTORE, "--file", os.devnull, str(dump)],
                environment=pg_environment,
                timeout=budget.command_timeout(),
                error_code="backup_validation_failed",
            )
            if not _matches_stable_file(
                os.fstat(dump_fd), dump_info
            ) or not _matches_stable_file(dump.lstat(), dump_info):
                raise BackupError("backup_source_changed")
        finally:
            os.close(dump_fd)

        records = [dump_record, *_copy_sources(config, partial, budget)]

        records.sort(key=lambda item: str(item["path"]))
        manifest_raw = _capped_json_bytes(
            {"files": records, "version": 1},
            _MAX_MANIFEST_BYTES,
            "backup_limits_exceeded",
        )
        manifest_digest = hashlib.sha256(manifest_raw).hexdigest()
        _write_private_file(partial / "manifest.json", manifest_raw)
        metadata = {
            "created_at": created_at,
            "manifest_sha256": manifest_digest,
            "set_name": set_name,
            "status": "successful",
            "version": 1,
        }
        metadata_raw = _json_bytes(metadata)
        _write_private_file(partial / "backup.json", metadata_raw)
        for directory in sorted(
            (path for path in partial.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o700)
            _sync_directory(directory)
        _sync_directory(partial)
        validate_root()
        _publish_backup(
            root=root,
            partial=partial,
            final=final,
            marker=marker,
            marker_temp=marker_temp,
            metadata=metadata_raw,
            nonce=safe_nonce,
            replace=atomic_replace,
        )
    except BackupError:
        raise
    except (OSError, RecursionError):
        raise BackupError("backup_write_failed") from None

    validate_root()
    deleted = _apply_retention(root, config.retention, set_name)
    validate_root()
    return BackupResult(final, created_at, manifest_digest, deleted)


def main(
    environment: Mapping[str, str] | None = None,
    *,
    create: Callable[[BackupConfig, str], object] = create_validated_backup,
) -> int:
    """Run one backup from the root-private systemd environment."""
    values = os.environ if environment is None else environment
    enabled = values.get("VPN_BACKUP_ENABLED", "false").strip().lower()
    if enabled in {"", "0", "false", "no", "off"}:
        return 0
    if enabled not in {"1", "true", "yes", "on"}:
        return 1
    try:
        config = BackupConfig(
            backup_root=Path(values["VPN_BACKUP_DIRECTORY"]),
            env_file=Path(values["VPN_BACKUP_ENV_FILE"]),
            systemd_unit=Path(values["VPN_BACKUP_SYSTEMD_UNIT"]),
            nginx_directory=Path(values["VPN_BACKUP_NGINX_DIRECTORY"]),
            frontend_dist=Path(values["VPN_BACKUP_FRONTEND_DIST"]),
            retention=int(values.get("VPN_BACKUP_RETENTION", "7")),
        )
        create(config, values["DB_URL"])
    except Exception:  # noqa: BLE001 - the CLI must never print secret exception text
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the systemd unit
    raise SystemExit(main())
