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
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

_ALLOWED_ENVIRONMENT = (
    "PATH",
    "SystemRoot",
    "SYSTEMROOT",
    "WINDIR",
    "PATHEXT",
    "LD_LIBRARY_PATH",
    "LANG",
    "LC_ALL",
)
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


def _validated_root(path: Path) -> Path:
    try:
        info = path.lstat()
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_root_invalid") from None
    if (
        not stat.S_ISDIR(info.st_mode)
        or _is_reparse(info)
        or not _same_path(Path(os.path.abspath(path)), resolved)
    ):
        raise BackupError("backup_root_invalid")
    try:
        path.chmod(0o700)
    except OSError:
        raise BackupError("backup_root_invalid") from None
    return resolved


def _snapshot_directory(
    source: Path, destination: Path
) -> list[tuple[Path, Path, bool]]:
    _validated_path(source, directory=True)
    items = [(source, destination, True)]

    def visit(directory: Path, target: Path) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError:
            raise BackupError("backup_source_invalid") from None
        for entry in entries:
            path = Path(entry.path)
            child_target = target / entry.name
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                raise BackupError("backup_source_invalid") from None
            if _is_reparse(info) or stat.S_ISLNK(info.st_mode):
                raise BackupError("backup_source_invalid")
            if stat.S_ISDIR(info.st_mode):
                items.append((path, child_target, True))
                visit(path, child_target)
            elif stat.S_ISREG(info.st_mode):
                items.append((path, child_target, False))
            else:
                raise BackupError("backup_source_invalid")

    visit(source, destination)
    return items


def _snapshot_sources(config: BackupConfig) -> list[tuple[Path, Path, bool]]:
    _validated_path(config.env_file, directory=False)
    _validated_path(config.systemd_unit, directory=False)
    items = [
        (config.env_file, Path("environment") / ".env", False),
        (
            config.systemd_unit,
            Path("systemd") / config.systemd_unit.name,
            False,
        ),
    ]
    items.extend(_snapshot_directory(config.nginx_directory, Path("nginx")))
    items.extend(_snapshot_directory(config.frontend_dist, Path("frontend")))
    return items


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
        key: value for key, value in os.environ.items() if key in _ALLOWED_ENVIRONMENT
    }
    environment.update(
        {
            "PGHOST": hostname,
            "PGPORT": str(port),
            "PGUSER": username,
            "PGDATABASE": database,
        }
    )
    if password is not None:
        environment["PGPASSWORD"] = password
    return database, environment


def _mkdir_private(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)


def _sync_regular(path: Path) -> None:
    try:
        # Windows only permits fsync on a descriptor opened for writing.
        with path.open("r+b") as handle:
            os.fsync(handle.fileno())
    except OSError:
        raise BackupError("backup_write_failed") from None


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


def _run_command(
    runner: Callable[..., Any],
    command: list[str],
    *,
    environment: Mapping[str, str],
    timeout: float,
    error_code: str,
) -> None:
    try:
        completed = runner(
            command,
            env=dict(environment),
            timeout=timeout,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if completed.returncode != 0:
            raise BackupError(error_code)
    except BackupError:
        raise
    # An injected runner is untrusted here; its exception text may contain the env.
    except Exception:  # noqa: BLE001
        raise BackupError(error_code) from None


def _copy_file(source: Path, destination: Path, budget: _Budget) -> dict[str, object]:
    budget.check_deadline()
    try:
        before = source.lstat()
        if _is_reparse(before) or not stat.S_ISREG(before.st_mode):
            raise BackupError("backup_source_invalid")
        budget.add_file(before.st_size)
        _mkdir_private(destination.parent)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(source, flags)
        destination_fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0),
            0o600,
        )
        digest = hashlib.sha256()
        copied = 0
        try:
            opened = os.fstat(source_fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != before.st_dev
                or opened.st_ino != before.st_ino
            ):
                raise BackupError("backup_source_invalid")
            while chunk := os.read(source_fd, _CHUNK_SIZE):
                budget.check_deadline()
                copied += len(chunk)
                if copied > budget.config.max_file_bytes:
                    raise BackupError("backup_limits_exceeded")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    if written <= 0:
                        raise OSError
                    view = view[written:]
            if copied != before.st_size:
                raise BackupError("backup_source_changed")
            os.fchmod(destination_fd, 0o600)
            os.fsync(destination_fd)
        finally:
            os.close(source_fd)
            os.close(destination_fd)
        destination.chmod(0o600)
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_copy_failed") from None
    return {
        "path": destination.as_posix(),
        "sha256": digest.hexdigest(),
        "size": copied,
    }


def _record_dump(path: Path, budget: _Budget) -> dict[str, object]:
    try:
        info = path.lstat()
        if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size == 0:
            raise BackupError("backup_dump_failed")
        path.chmod(0o600)
        _sync_regular(path)
        budget.add_file(info.st_size)
        digest = hashlib.sha256()
        size = 0
        with path.open("rb") as handle:
            while chunk := handle.read(_CHUNK_SIZE):
                budget.check_deadline()
                digest.update(chunk)
                size += len(chunk)
        if size != info.st_size:
            raise BackupError("backup_source_changed")
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_dump_failed") from None
    return {"path": path.name, "sha256": digest.hexdigest(), "size": size}


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


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


def _read_existing_marker(path: Path, root: Path) -> bytes | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        raise BackupError("backup_publish_failed") from None
    if _is_reparse(info) or not stat.S_ISREG(info.st_mode) or info.st_size > 16_384:
        raise BackupError("backup_publish_failed")
    try:
        if path.resolve(strict=True).parent != root:
            raise BackupError("backup_publish_failed")
        return path.read_bytes()
    except BackupError:
        raise
    except OSError:
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
            or manifest_path.stat().st_size > 16_777_216
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
        return 0
    keep = {path.name for path in candidates[:retention]}
    keep.add(current)
    return sum(
        _remove_safe_tree(path, root) for path in candidates if path.name not in keep
    )


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
    replace: Callable[[Path, Path], None] = os.replace,
) -> BackupResult:
    """Create one validated set without exposing database credentials."""
    database, pg_environment = _postgres_environment(db_url)
    root = _validated_root(config.backup_root)
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
    promoted = False
    marker_replaced = False
    previous_marker: bytes | None = None
    try:
        _reject_source_overlap(config, root)
        sources = _snapshot_sources(config)
        dump = partial / "database.dump"
        _run_command(
            runner,
            [
                "pg_dump",
                "--format=custom",
                "--file",
                str(dump),
                "--dbname",
                database,
            ],
            environment=pg_environment,
            timeout=budget.command_timeout(),
            error_code="backup_dump_failed",
        )
        if not dump.exists():
            raise BackupError("backup_dump_failed")
        dump.chmod(0o600)
        dump_record = _record_dump(dump, budget)
        _run_command(
            runner,
            ["pg_restore", "--file", os.devnull, str(dump)],
            environment=pg_environment,
            timeout=budget.command_timeout(),
            error_code="backup_validation_failed",
        )

        records = [dump_record]
        for source, relative, is_directory in sources:
            destination = partial / relative
            if is_directory:
                _mkdir_private(destination)
                continue
            try:
                record = _copy_file(source, destination, budget)
            except BackupError as error:
                if error.args[0] in {
                    "backup_limits_exceeded",
                    "backup_deadline_exceeded",
                    "backup_source_invalid",
                    "backup_source_changed",
                }:
                    raise
                raise BackupError("backup_copy_failed") from None
            record["path"] = relative.as_posix()
            records.append(record)

        records.sort(key=lambda item: str(item["path"]))
        manifest_raw = _json_bytes({"files": records, "version": 1})
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
        previous_marker = _read_existing_marker(marker, root)
        _write_private_file(marker_temp, metadata_raw)
        replace(partial, final)
        promoted = True
        _sync_directory(root)
        replace(marker_temp, marker)
        marker_replaced = True
        marker.chmod(0o600)
        _sync_directory(root)
    except BackupError:
        if promoted:
            if marker_replaced:
                try:
                    if previous_marker is None:
                        marker.unlink(missing_ok=True)
                    else:
                        rollback = root / f".latest-success.{safe_nonce}.rollback"
                        _write_private_file(rollback, previous_marker)
                        os.replace(rollback, marker)
                except (OSError, BackupError):
                    pass
            try:
                os.replace(final, partial)
            except OSError:
                pass
        try:
            marker_temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except OSError:
        if promoted:
            try:
                os.replace(final, partial)
            except OSError:
                pass
        try:
            marker_temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise BackupError("backup_publish_failed") from None

    deleted = _apply_retention(root, config.retention, set_name)
    return BackupResult(final, created_at, manifest_digest, deleted)


def main(
    environment: Mapping[str, str] | None = None,
    *,
    create: Callable[[BackupConfig, str], object] = create_validated_backup,
) -> int:
    """Run one backup from the root-private systemd environment."""
    values = os.environ if environment is None else environment
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
    except (BackupError, KeyError, TypeError, ValueError):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by the systemd unit
    raise SystemExit(main())
