"""One-shot, node-local preparation of a new 3x-UI VPN node."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import platform
import re
import signal
import sqlite3
import stat
import subprocess
import sys
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal
from uuid import uuid4

try:
    import asyncssh
except ModuleNotFoundError as error:
    if error.name != "asyncssh":
        raise
    asyncssh = None

BOOTSTRAP_IMPORT_SENTINEL = b"veltrix-vpn-bootstrap-import-ok-v1\n"
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_BUNDLE_MEMBERS = (
    "app/__init__.py",
    "app/services/__init__.py",
    "app/services/vpn_xui_node_http.py",
    "app/services/vpn_node_bootstrap.py",
)
_TOKEN = re.compile(rb"[A-Za-z0-9._~+/\-]{1,4096}=*\Z", re.ASCII)
_TOKEN_LINE = re.compile(
    rb"^apiToken: ([A-Za-z0-9._~+/\-]{1,4096}=*)$", re.ASCII | re.MULTILINE
)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_REMOTE_OUTPUT_BYTES = 4096
MAX_LOCAL_OUTPUT_BYTES = 64 * 1024
BOOTSTRAP_TIMEOUT = 900.0
XUI_START_COMMAND = (
    "systemctl enable --now x-ui.service || systemctl enable --now x-ui || "
    "systemctl enable --now 3x-ui.service"
)
_ERROR_CODES = frozenset(
    {
        "vpn_node_bootstrap_failed",
        "vpn_node_bootstrap_environment_invalid",
        "vpn_node_bootstrap_port_conflict",
        "vpn_node_bootstrap_inbound_exists",
        "vpn_node_bootstrap_foreign_state",
        "vpn_node_bootstrap_mutation_uncertain",
        "vpn_node_bootstrap_reconciliation_required",
    }
)
_REMOTE_ROOT_SOURCE = r'''def prepare_private_root(path, owner_uid, require_posix):
    for ancestor in path.parents:
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or (require_posix and info.st_uid != owner_uid)
                or (require_posix and info.st_mode & 0o022)):
            raise ValueError
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or (require_posix and info.st_uid != owner_uid)
            or (require_posix and stat.S_IMODE(info.st_mode) != 0o700)):
        raise ValueError
'''
_REMOTE_BOUNDED_SOURCE = r'''def run_bounded(command, timeout, limit):
    import os
    import signal
    import threading

    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    buffers = [bytearray(), bytearray()]
    overflow = threading.Event()
    errors = []

    def terminate():
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except OSError:
            pass

    def collect(pipe, target):
        try:
            while True:
                remaining = limit - len(target)
                chunk = pipe.read(min(65536, remaining + 1))
                if not chunk:
                    return
                if len(chunk) > remaining:
                    overflow.set()
                    terminate()
                    return
                target.extend(chunk)
        except BaseException as error:
            errors.append(error)
            terminate()

    threads = [
        threading.Thread(target=collect, args=(process.stdout, buffers[0]), daemon=True),
        threading.Thread(target=collect, args=(process.stderr, buffers[1]), daemon=True),
    ]
    for thread in threads:
        thread.start()
    timed_out = False
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate()
        process.wait()
    except BaseException:
        terminate()
        process.wait()
        raise
    for thread in threads:
        thread.join(timeout=1)
    if any(thread.is_alive() for thread in threads):
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        raise TimeoutError
    for pipe in (process.stdout, process.stderr):
        pipe.close()
    if errors or timed_out:
        raise TimeoutError
    if overflow.is_set():
        raise OverflowError
    return subprocess.CompletedProcess(command, process.returncode, bytes(buffers[0]), bytes(buffers[1]))
'''
_REMOTE_BOOTSTRAP = r'''import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import threading

MAX_HELPER = 8 * 1024 * 1024
MAX_OUTPUT = 4096
FAILED = b"vpn_node_bootstrap_failed\n"
UNCERTAIN = b"vpn_node_bootstrap_mutation_uncertain\n"
RECONCILE = b"vpn_node_bootstrap_reconciliation_required\n"
ALLOWED_ERRORS = {
    FAILED,
    b"vpn_node_bootstrap_environment_invalid\n",
    b"vpn_node_bootstrap_port_conflict\n",
    b"vpn_node_bootstrap_inbound_exists\n",
    b"vpn_node_bootstrap_foreign_state\n",
    UNCERTAIN,
    RECONCILE,
}

''' + _REMOTE_ROOT_SOURCE + _REMOTE_BOUNDED_SOURCE + r'''

def emit(raw, code):
    sys.stdout.buffer.write(raw)
    raise SystemExit(code)

candidate = None
started = False
try:
    digest_line = sys.stdin.buffer.readline(66)
    size_line = sys.stdin.buffer.readline(16)
    if not re.fullmatch(rb"[0-9a-f]{64}\n", digest_line):
        emit(FAILED, 1)
    if not re.fullmatch(rb"[1-9][0-9]{0,7}\n", size_line):
        emit(FAILED, 1)
    digest = digest_line[:-1].decode("ascii")
    size = int(size_line)
    if size > MAX_HELPER:
        emit(FAILED, 1)
    raw = sys.stdin.buffer.read(size + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        emit(FAILED, 1)
    if os.geteuid() != 0:
        emit(FAILED, 1)
    parent = Path("/var/lib/veltrix-vpn")
    prepare_private_root(parent, 0, True)
    candidate = parent / (".bootstrap-" + digest + ".pyz")
    if candidate.exists() or candidate.is_symlink():
        emit(RECONCILE, 1)
    descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                emit(FAILED, 1)
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    started = True
    try:
        result = run_bounded(
            ["/usr/bin/python3", "-I", "-S", str(candidate)],
            900,
            MAX_OUTPUT,
        )
    except (TimeoutError, OverflowError):
        emit(UNCERTAIN, 1)
    output = result.stdout
    if result.stderr:
        emit(UNCERTAIN, 1)
    if result.returncode == 1 and output in ALLOWED_ERRORS:
        emit(output, 1)
    if result.returncode != 0:
        emit(UNCERTAIN, 1)
    try:
        value = json.loads(output.decode("ascii"))
        if (type(value) is not dict
                or set(value) != {"state", "panel_url", "database_path", "xui_version"}
                or value["state"] not in {"prepared", "already_prepared"}
                or any(type(value[key]) is not str for key in ("state", "panel_url", "database_path", "xui_version"))
                or output != (json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii") + b"\n")):
            raise ValueError
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        emit(UNCERTAIN, 1)
    emit(output, 0)
except SystemExit:
    raise
except BaseException:
    emit(UNCERTAIN if started else FAILED, 1)
finally:
    if candidate is not None:
        try:
            candidate.unlink(missing_ok=True)
        except OSError:
            pass
'''
FIXED_BOOTSTRAP_COMMAND = (
    "/usr/bin/python3 -I -S -B -c 'import base64;exec(base64.b64decode(\""
    + base64.b64encode(_REMOTE_BOOTSTRAP.encode("ascii")).decode("ascii")
    + "\"))'"
)


class VpnNodeBootstrapError(RuntimeError):
    """A fixed, secret-free bootstrap failure."""

    def __init__(self, code: str = "vpn_node_bootstrap_failed") -> None:
        self.code = code if code in _ERROR_CODES else "vpn_node_bootstrap_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VpnNodeBootstrapReceipt:
    state: Literal["prepared", "already_prepared"]
    panel_url: str
    database_path: str
    xui_version: str


@dataclass(frozen=True, slots=True)
class BootstrapLayout:
    state_root: Path
    auth_directory: Path
    token: Path
    config: Path
    provenance: Path
    marker: Path
    backup: Path
    xui_binaries: tuple[Path, ...]
    databases: tuple[Path, ...]


DEFAULT_LAYOUT = BootstrapLayout(
    state_root=Path("/var/lib/veltrix-vpn"),
    auth_directory=Path("/var/lib/veltrix-vpn/control-auth"),
    token=Path("/var/lib/veltrix-vpn/control-auth/api-token"),
    config=Path("/var/lib/veltrix-vpn/control-auth/node.json"),
    provenance=Path("/var/lib/veltrix-vpn/control-auth/bootstrap-state.json"),
    marker=Path("/var/lib/veltrix-vpn/control-auth/bootstrap-reconciliation.json"),
    backup=Path("/var/lib/veltrix-vpn/backups/x-ui-before-api-token.db"),
    xui_binaries=(Path("/usr/local/x-ui/x-ui"), Path("/etc/x-ui/x-ui")),
    databases=(
        Path("/etc/x-ui/x-ui.db"),
        Path("/usr/local/x-ui/bin/x-ui.db"),
        Path("/usr/local/x-ui/x-ui.db"),
    ),
)


def _fail(code: str = "vpn_node_bootstrap_failed") -> None:
    raise VpnNodeBootstrapError(code) from None


def _run_bounded_subprocess(
    command,
    *,
    shell: bool,
    timeout: float,
    failure_code: str,
) -> subprocess.CompletedProcess:
    process = None
    try:
        process = subprocess.Popen(
            command,
            shell=shell,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        buffers = [bytearray(), bytearray()]
        overflow = threading.Event()
        reader_failed = threading.Event()

        def terminate() -> None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
            except OSError:
                pass

        def collect(pipe, target: bytearray) -> None:
            try:
                while True:
                    remaining = MAX_LOCAL_OUTPUT_BYTES - len(target)
                    chunk = pipe.read(min(65536, remaining + 1))
                    if not chunk:
                        return
                    if len(chunk) > remaining:
                        overflow.set()
                        terminate()
                        return
                    target.extend(chunk)
            except (OSError, ValueError):
                reader_failed.set()
                terminate()

        threads = (
            threading.Thread(
                target=collect,
                args=(process.stdout, buffers[0]),
                daemon=True,
            ),
            threading.Thread(
                target=collect,
                args=(process.stderr, buffers[1]),
                daemon=True,
            ),
        )
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            terminate()
            process.wait()
        except BaseException:
            terminate()
            process.wait()
            raise
        for thread in threads:
            thread.join(timeout=1)
        if any(thread.is_alive() for thread in threads):
            for pipe in (process.stdout, process.stderr):
                pipe.close()
            _fail(failure_code)
        for pipe in (process.stdout, process.stderr):
            pipe.close()
        if reader_failed.is_set() or overflow.is_set() or timed_out:
            _fail(failure_code)
        return subprocess.CompletedProcess(
            command,
            process.returncode,
            bytes(buffers[0]),
            bytes(buffers[1]),
        )
    except VpnNodeBootstrapError:
        raise
    except BaseException as error:
        if process is not None and process.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.SubprocessError):
                pass
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _fail(failure_code)


def _invoke_command(
    runner,
    command,
    *,
    shell: bool,
    timeout: float,
    failure_code: str,
) -> subprocess.CompletedProcess:
    if runner is None:
        return _run_bounded_subprocess(
            command,
            shell=shell,
            timeout=timeout,
            failure_code=failure_code,
        )
    try:
        result = runner(
            command,
            shell=shell,
            input=b"",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        if (
            type(result.returncode) is not int
            or type(result.stdout) is not bytes
            or type(result.stderr) is not bytes
            or len(result.stdout) > MAX_LOCAL_OUTPUT_BYTES
            or len(result.stderr) > MAX_LOCAL_OUTPUT_BYTES
        ):
            _fail(failure_code)
        return result
    except VpnNodeBootstrapError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _fail(failure_code)


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory(
    path: Path,
    owner_uid: int,
    *,
    create: bool = False,
    require_posix: bool,
) -> None:
    try:
        if create and not path.exists():
            path.mkdir(mode=0o700)
        info = path.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (require_posix and stat.S_IMODE(info.st_mode) != 0o700)
            or (require_posix and info.st_uid != owner_uid)
        ):
            _fail("vpn_node_bootstrap_environment_invalid")
    except VpnNodeBootstrapError:
        raise
    except OSError:
        _fail("vpn_node_bootstrap_environment_invalid")


def _secure_file(
    path: Path,
    owner_uid: int,
    *,
    mode: int | None,
    limit: int,
    require_posix: bool,
    read_mode: Literal["content", "digest", "metadata"] = "content",
) -> bytes:
    descriptor = None
    try:
        if require_posix:
            for ancestor in path.parents:
                ancestor_info = ancestor.lstat()
                if (
                    not stat.S_ISDIR(ancestor_info.st_mode)
                    or stat.S_ISLNK(ancestor_info.st_mode)
                    or ancestor_info.st_uid != owner_uid
                    or ancestor_info.st_mode & 0o022
                ):
                    _fail("vpn_node_bootstrap_environment_invalid")
        if read_mode not in {"content", "digest", "metadata"}:
            _fail("vpn_node_bootstrap_environment_invalid")
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (require_posix and mode is not None and stat.S_IMODE(before.st_mode) != mode)
            or (require_posix and mode is None and before.st_mode & 0o022)
            or (require_posix and before.st_uid != owner_uid)
            or not 0 < before.st_size <= limit
        ):
            _fail("vpn_node_bootstrap_environment_invalid")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0),
        )
        opened = os.fstat(descriptor)
        def identity(info):
            value = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if require_posix:
                return value + (
                    info.st_ctime_ns,
                    info.st_mode,
                    info.st_uid,
                    info.st_gid,
                )
            return value
        if identity(opened) != identity(before):
            _fail("vpn_node_bootstrap_environment_invalid")
        raw = bytearray()
        digest = hashlib.sha256()
        size = 0
        if read_mode != "metadata":
            while True:
                chunk = os.read(descriptor, min(65536, limit + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    _fail("vpn_node_bootstrap_environment_invalid")
                if read_mode == "content":
                    raw.extend(chunk)
                else:
                    digest.update(chunk)
        after = os.fstat(descriptor)
        linked = path.lstat()
        if identity(after) != identity(before) or identity(linked) != identity(before):
            _fail("vpn_node_bootstrap_environment_invalid")
        if read_mode == "digest":
            return digest.hexdigest().encode("ascii")
        return bytes(raw)
    except VpnNodeBootstrapError:
        raise
    except OSError:
        _fail("vpn_node_bootstrap_environment_invalid")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail("vpn_node_bootstrap_environment_invalid")


def _secure_existing_ancestors(
    path: Path, owner_uid: int, *, require_posix: bool
) -> None:
    try:
        for ancestor in path.parents:
            if not ancestor.exists() and not ancestor.is_symlink():
                continue
            info = ancestor.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or (require_posix and info.st_uid != owner_uid)
                or (require_posix and info.st_mode & 0o022)
            ):
                _fail("vpn_node_bootstrap_environment_invalid")
    except VpnNodeBootstrapError:
        raise
    except OSError:
        _fail("vpn_node_bootstrap_environment_invalid")


def _atomic_private(path: Path, raw: bytes) -> None:
    candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            candidate,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("vpn_node_bootstrap_mutation_uncertain")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.link(candidate, path, follow_symlinks=False)
        candidate.unlink()
        _sync_directory(path.parent)
    except VpnNodeBootstrapError:
        raise
    except OSError:
        _fail("vpn_node_bootstrap_mutation_uncertain")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        candidate.unlink(missing_ok=True)


def _node_config_bytes(
    panel_url: str,
    database_path: Path,
) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "panel_url": panel_url,
                "database_path": str(database_path),
            },
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _bootstrap_state_bytes(backup_path: Path, backup_sha256: str) -> bytes:
    return (
        json.dumps(
            {
                "version": 1,
                "backup_path": str(backup_path),
                "backup_sha256": backup_sha256,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _validate_panel_url(panel_url: str) -> None:
    from app.services.vpn_xui_node_http import validate_panel_url

    address = validate_panel_url(panel_url)
    try:
        if address.scheme != "http" or not ipaddress.ip_address(
            address.hostname
        ).is_loopback:
            _fail()
    except ValueError:
        _fail()


def _single_existing(paths: tuple[Path, ...], owner_uid: int, *, database: bool, require_posix: bool) -> Path | None:
    present = [path for path in paths if path.exists() or path.is_symlink()]
    if len(present) > 1:
        _fail("vpn_node_bootstrap_environment_invalid")
    if not present:
        return None
    path = present[0]
    _secure_file(
        path,
        owner_uid,
        mode=0o600 if database else None,
        limit=16 * 1024 * 1024 * 1024 if database else 64 * 1024 * 1024,
        require_posix=require_posix,
        read_mode="metadata",
    )
    return path


def _database_state(
    database: Path,
    token_name: str,
    *,
    target_must_exist: bool,
) -> tuple[tuple[str, ...], tuple[tuple[object, ...], ...]]:
    try:
        connection = sqlite3.connect(
            database.as_uri() + "?mode=ro", uri=True, isolation_level=None
        )
        try:
            connection.execute("PRAGMA query_only=ON")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if "inbounds" not in tables:
                _fail("vpn_node_bootstrap_environment_invalid")
            if connection.execute("SELECT 1 FROM inbounds LIMIT 1").fetchone() is not None:
                _fail("vpn_node_bootstrap_inbound_exists")
            columns: tuple[str, ...] = ()
            preserved: tuple[tuple[object, ...], ...] = ()
            target_count = 0
            if "api_tokens" in tables:
                columns = tuple(
                    row[1]
                    for row in connection.execute("PRAGMA table_info(api_tokens)")
                )
                if "name" not in columns:
                    _fail("vpn_node_bootstrap_environment_invalid")
                name_index = columns.index("name")
                rows = tuple(connection.execute("SELECT * FROM api_tokens ORDER BY rowid"))
                target_count = sum(row[name_index] == token_name for row in rows)
                preserved = tuple(row for row in rows if row[name_index] != token_name)
            if target_count != (1 if target_must_exist else 0):
                _fail(
                    "vpn_node_bootstrap_mutation_uncertain"
                    if target_must_exist
                    else "vpn_node_bootstrap_foreign_state"
                )
            return columns, preserved
        finally:
            connection.close()
    except VpnNodeBootstrapError:
        raise
    except (OSError, sqlite3.Error, TypeError, ValueError):
        _fail(
            "vpn_node_bootstrap_mutation_uncertain"
            if target_must_exist
            else "vpn_node_bootstrap_environment_invalid"
        )


def _backup_database(
    database: Path,
    backup: Path,
    owner_uid: int,
    *,
    require_posix: bool,
) -> None:
    try:
        _secure_directory(
            backup.parent,
            owner_uid,
            create=True,
            require_posix=require_posix,
        )
        descriptor = os.open(
            backup,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
            0o600,
        )
        os.close(descriptor)
        source = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
        destination = sqlite3.connect(backup)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(backup, 0o600)
        descriptor = os.open(backup, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _sync_directory(backup.parent)
        _secure_file(
            backup,
            owner_uid,
            mode=0o600,
            limit=16 * 1024 * 1024 * 1024,
            require_posix=require_posix,
        )
    except FileExistsError:
        _fail("vpn_node_bootstrap_foreign_state")
    except VpnNodeBootstrapError:
        raise
    except (OSError, sqlite3.Error):
        _fail("vpn_node_bootstrap_failed")


def _linux_port_occupied(port: int) -> bool:
    encoded = f"{port:04X}"
    try:
        for name in ("/proc/net/tcp", "/proc/net/tcp6"):
            try:
                with open(name, encoding="ascii") as stream:
                    next(stream, None)
                    for line in stream:
                        fields = line.split()
                        if len(fields) >= 4 and fields[3] == "0A" and fields[1].endswith(":" + encoded):
                            return True
            except FileNotFoundError:
                continue
        return False
    except (OSError, UnicodeError):
        _fail("vpn_node_bootstrap_environment_invalid")


def _default_panel_session(panel_url: str, *, api_token: str):
    from app.services.vpn_xui_node_http import NodePanelSession

    return NodePanelSession(panel_url, api_token=api_token)


def _prepare_vpn_node(
    layout: BootstrapLayout,
    *,
    worker_id: int,
    panel_url: str,
    install_command: str,
    xui_version: str,
    architecture: str,
    stop_command: str | None = None,
    start_command: str | None = None,
    ready_command: str | None = None,
    port_occupied=_linux_port_occupied,
    runner=None,
    panel_session_factory=_default_panel_session,
    owner_uid: int = 0,
    require_posix: bool = True,
    pre_install_commands: tuple[str, ...] = (),
    post_install_commands: tuple[str, ...] = (),
) -> VpnNodeBootstrapReceipt:
    """Prepare one node; any token ambiguity is durable and never retried."""
    try:
        if (
            not isinstance(layout, BootstrapLayout)
            or type(worker_id) is not int
            or worker_id <= 0
            or architecture != "amd64"
            or type(install_command) is not str
            or not install_command
            or type(xui_version) is not str
            or not xui_version
            or (require_posix and (owner_uid != 0 or os.geteuid() != 0))
        ):
            _fail("vpn_node_bootstrap_environment_invalid")
        _validate_panel_url(panel_url)
        _secure_directory(layout.state_root, owner_uid, require_posix=require_posix)
        if layout.auth_directory.exists() or layout.auth_directory.is_symlink():
            _secure_directory(layout.auth_directory, owner_uid, require_posix=require_posix)
        if layout.marker.exists() or layout.marker.is_symlink():
            _secure_file(
                layout.marker,
                owner_uid,
                mode=0o600,
                limit=4096,
                require_posix=require_posix,
            )
            _fail("vpn_node_bootstrap_reconciliation_required")
        has_backup = layout.backup.exists() or layout.backup.is_symlink()
        if layout.backup.parent.exists() or layout.backup.parent.is_symlink():
            _secure_directory(
                layout.backup.parent,
                owner_uid,
                require_posix=require_posix,
            )
        for candidate in (*layout.xui_binaries, *layout.databases):
            _secure_existing_ancestors(
                candidate,
                owner_uid,
                require_posix=require_posix,
            )

        has_token = layout.token.exists() or layout.token.is_symlink()
        has_config = layout.config.exists() or layout.config.is_symlink()
        has_provenance = layout.provenance.exists() or layout.provenance.is_symlink()
        if (
            layout.provenance.parent != layout.auth_directory
            or has_token != has_config
            or has_token != has_provenance
        ):
            _fail("vpn_node_bootstrap_foreign_state")
        database = _single_existing(
            layout.databases, owner_uid, database=True, require_posix=require_posix
        )
        binary = _single_existing(
            layout.xui_binaries, owner_uid, database=False, require_posix=require_posix
        )
        if has_token and has_config:
            if database is None or binary is None or not has_backup:
                _fail("vpn_node_bootstrap_foreign_state")
            backup_sha256 = _secure_file(
                layout.backup,
                owner_uid,
                mode=0o600,
                limit=16 * 1024 * 1024 * 1024,
                require_posix=require_posix,
                read_mode="digest",
            ).decode("ascii")
            token_raw = _secure_file(
                layout.token,
                owner_uid,
                mode=0o600,
                limit=4097,
                require_posix=require_posix,
            )
            config_raw = _secure_file(
                layout.config,
                owner_uid,
                mode=0o600,
                limit=65536,
                require_posix=require_posix,
            )
            provenance_raw = _secure_file(
                layout.provenance,
                owner_uid,
                mode=0o600,
                limit=4096,
                require_posix=require_posix,
            )
            if not token_raw.endswith(b"\n") or not _TOKEN.fullmatch(token_raw[:-1]):
                _fail("vpn_node_bootstrap_foreign_state")
            if config_raw != _node_config_bytes(
                panel_url,
                database,
            ) or provenance_raw != _bootstrap_state_bytes(
                layout.backup, backup_sha256
            ):
                _fail("vpn_node_bootstrap_foreign_state")
            token = token_raw[:-1].decode("ascii")
            with panel_session_factory(panel_url, api_token=token):
                pass
            return VpnNodeBootstrapReceipt(
                "already_prepared", panel_url, str(database), xui_version
            )

        if has_backup:
            _fail("vpn_node_bootstrap_foreign_state")

        token_name = f"veltrix-automation-{worker_id}-{uuid4().hex}"
        token_state = None
        if binary is not None and database is not None:
            token_state = _database_state(
                database, token_name, target_must_exist=False
            )
        if port_occupied(443):
            _fail("vpn_node_bootstrap_port_conflict")
        if any(
            type(command) is not str or not command
            for command in (stop_command, start_command, ready_command)
        ):
            _fail("vpn_node_bootstrap_environment_invalid")
        if (binary is None) != (database is None):
            _fail("vpn_node_bootstrap_environment_invalid")
        if binary is None:
            for command in pre_install_commands:
                result = _invoke_command(
                    runner,
                    command,
                    shell=True,
                    timeout=720,
                    failure_code="vpn_node_bootstrap_failed",
                )
                if result.returncode != 0 or result.stderr:
                    _fail("vpn_node_bootstrap_failed")
            result = _invoke_command(
                runner,
                install_command,
                shell=True,
                timeout=720,
                failure_code="vpn_node_bootstrap_failed",
            )
            if result.returncode != 0 or result.stderr:
                _fail("vpn_node_bootstrap_failed")
            for command in post_install_commands:
                result = _invoke_command(
                    runner,
                    command,
                    shell=True,
                    timeout=720,
                    failure_code="vpn_node_bootstrap_failed",
                )
                if result.returncode != 0 or result.stderr:
                    _fail("vpn_node_bootstrap_failed")
            binary = _single_existing(
                layout.xui_binaries, owner_uid, database=False, require_posix=require_posix
            )
            database = _single_existing(
                layout.databases, owner_uid, database=True, require_posix=require_posix
            )
            if binary is None or database is None:
                _fail("vpn_node_bootstrap_failed")
        assert binary is not None and database is not None

        if token_state is None:
            token_state = _database_state(
                database, token_name, target_must_exist=False
            )
        _secure_directory(
            layout.auth_directory,
            owner_uid,
            create=True,
            require_posix=require_posix,
        )
        _atomic_private(
            layout.marker,
            json.dumps(
                {"version": 1, "state": "token_creation_attempted", "name": token_name},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            + b"\n",
        )
        try:
            result = _invoke_command(
                runner,
                stop_command,
                shell=True,
                timeout=30,
                failure_code="vpn_node_bootstrap_mutation_uncertain",
            )
            if result.returncode != 0 or result.stderr:
                _fail("vpn_node_bootstrap_mutation_uncertain")
            _backup_database(
                database,
                layout.backup,
                owner_uid,
                require_posix=require_posix,
            )
            backup_sha256 = _secure_file(
                layout.backup,
                owner_uid,
                mode=0o600,
                limit=16 * 1024 * 1024 * 1024,
                require_posix=require_posix,
                read_mode="digest",
            ).decode("ascii")
            result = _invoke_command(
                runner,
                [
                    str(binary),
                    "setting",
                    "-getApiToken=true",
                    "-tokenName",
                    token_name,
                ],
                shell=False,
                timeout=30,
                failure_code="vpn_node_bootstrap_mutation_uncertain",
            )
            matches = (
                _TOKEN_LINE.findall(result.stdout)
                if result.returncode == 0
                and not result.stderr
                and len(result.stdout) <= 64 * 1024
                else []
            )
            if len(matches) != 1:
                _fail("vpn_node_bootstrap_mutation_uncertain")
            token_raw = matches[0]
            if _database_state(
                database, token_name, target_must_exist=True
            ) != token_state:
                _fail("vpn_node_bootstrap_mutation_uncertain")
            token = token_raw.decode("ascii")
            for command in (start_command, ready_command):
                result = _invoke_command(
                    runner,
                    command,
                    shell=True,
                    timeout=30,
                    failure_code="vpn_node_bootstrap_mutation_uncertain",
                )
                if result.returncode != 0 or result.stderr:
                    _fail("vpn_node_bootstrap_mutation_uncertain")
            with panel_session_factory(panel_url, api_token=token):
                pass
            _atomic_private(layout.token, token_raw + b"\n")
            _atomic_private(
                layout.config,
                _node_config_bytes(
                    panel_url,
                    database,
                ),
            )
            _atomic_private(
                layout.provenance,
                _bootstrap_state_bytes(layout.backup, backup_sha256),
            )
            layout.marker.unlink()
            _sync_directory(layout.auth_directory)
        except BaseException as exc:
            try:
                _invoke_command(
                    runner,
                    stop_command,
                    shell=True,
                    timeout=30,
                    failure_code="vpn_node_bootstrap_mutation_uncertain",
                )
            except VpnNodeBootstrapError:
                pass
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if isinstance(exc, VpnNodeBootstrapError):
                raise
            _fail("vpn_node_bootstrap_mutation_uncertain")
        return VpnNodeBootstrapReceipt("prepared", panel_url, str(database), xui_version)
    except VpnNodeBootstrapError:
        raise
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _fail("vpn_node_bootstrap_environment_invalid")


def _zip_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    entry.compress_type = zipfile.ZIP_STORED
    entry.create_system = 3
    entry.external_attr = 0o100600 << 16
    return entry


def _main_source(
    *,
    worker_id: int,
    panel_url: str,
    install_command: str,
    xui_version: str,
    stop_command: str,
    start_command: str,
    ready_command: str,
    pre_install_commands: tuple[str, ...],
    post_install_commands: tuple[str, ...],
) -> bytes:
    return (
        "import sys\n"
        "from app.services.vpn_node_bootstrap import (\n"
        "    BOOTSTRAP_IMPORT_SENTINEL, _runtime_main,\n"
        ")\n"
        "if sys.argv[1:] == ['--import-probe']:\n"
        "    sys.stdout.buffer.write(BOOTSTRAP_IMPORT_SENTINEL)\n"
        "    raise SystemExit(0)\n"
        "_runtime_main(\n"
        f"    worker_id={worker_id!r},\n"
        f"    panel_url={panel_url!r},\n"
        f"    install_command={install_command!r},\n"
        f"    xui_version={xui_version!r},\n"
        f"    stop_command={stop_command!r},\n"
        f"    start_command={start_command!r},\n"
        f"    ready_command={ready_command!r},\n"
        f"    pre_install_commands={pre_install_commands!r},\n"
        f"    post_install_commands={post_install_commands!r},\n"
        ")\n"
    ).encode("ascii")


def build_vpn_node_bootstrap_bundle(
    source_root: Path,
    target: Path,
    *,
    worker_id: int,
    panel_url: str,
) -> str:
    """Build a deterministic stdlib-only zipapp for one reviewed node."""
    from app.services.vpn_xui_node_http import NodePanelError
    from app.services.worker_maintenance import (
        XUI_VERSION,
        _build_vpn_install_command,
        _build_vpn_ready_command,
        _build_xray_compatibility_command,
        _build_xray_owner_normalization_command,
        _build_xui_stop_command,
    )

    source_root = Path(source_root)
    target = Path(target)
    try:
        if (
            not source_root.is_dir()
            or not target.is_absolute()
            or not target.parent.is_dir()
            or type(worker_id) is not int
            or worker_id <= 0
        ):
            _fail()
        _validate_panel_url(panel_url)
        members = []
        for name in _BUNDLE_MEMBERS:
            raw = (source_root / name).read_bytes()
            compile(raw, name, "exec", dont_inherit=True)
            members.append((name, raw))
        main = _main_source(
            worker_id=worker_id,
            panel_url=panel_url,
            install_command=_build_vpn_install_command(),
            xui_version=XUI_VERSION,
            stop_command=_build_xui_stop_command(),
            start_command=XUI_START_COMMAND,
            ready_command=_build_vpn_ready_command(),
            pre_install_commands=(
                "apt-get update",
                "apt-get install -y curl socat jq tar",
            ),
            post_install_commands=(
                _build_xray_owner_normalization_command(),
                _build_xray_compatibility_command(),
            ),
        )
        compile(main, "__main__.py", "exec", dont_inherit=True)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.close(descriptor)
            with zipfile.ZipFile(temporary, "w") as archive:
                archive.writestr(_zip_entry("__main__.py"), main)
                for name, raw in members:
                    archive.writestr(_zip_entry(name), raw)
            os.chmod(temporary, 0o600)
            probe = _run_bounded_subprocess(
                [sys.executable, "-I", "-S", str(temporary), "--import-probe"],
                shell=False,
                timeout=10,
                failure_code="vpn_node_bootstrap_failed",
            )
            if (
                probe.returncode != 0
                or probe.stdout != BOOTSTRAP_IMPORT_SENTINEL
                or probe.stderr
            ):
                _fail()
            raw = temporary.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            descriptor = os.open(temporary, os.O_RDWR | getattr(os, "O_BINARY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            return digest
        finally:
            temporary.unlink(missing_ok=True)
    except VpnNodeBootstrapError:
        raise
    except (NodePanelError, OSError, SyntaxError, TypeError, ValueError, subprocess.SubprocessError):
        _fail()


def _receipt_bytes(receipt: VpnNodeBootstrapReceipt) -> bytes:
    return (
        json.dumps(
            {
                "state": receipt.state,
                "panel_url": receipt.panel_url,
                "database_path": receipt.database_path,
                "xui_version": receipt.xui_version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _runtime_main(
    *,
    worker_id: int,
    panel_url: str,
    install_command: str,
    xui_version: str,
    stop_command: str,
    start_command: str,
    ready_command: str,
    pre_install_commands: tuple[str, ...] = (),
    post_install_commands: tuple[str, ...] = (),
) -> None:
    architecture = {"x86_64": "amd64", "amd64": "amd64"}.get(
        platform.machine().lower(), platform.machine().lower()
    )
    try:
        receipt = _prepare_vpn_node(
            DEFAULT_LAYOUT,
            worker_id=worker_id,
            panel_url=panel_url,
            install_command=install_command,
            xui_version=xui_version,
            architecture=architecture,
            stop_command=stop_command,
            start_command=start_command,
            ready_command=ready_command,
            pre_install_commands=pre_install_commands,
            post_install_commands=post_install_commands,
        )
    except VpnNodeBootstrapError as error:
        sys.stdout.buffer.write((error.code + "\n").encode("ascii"))
        raise SystemExit(1) from None
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        sys.stdout.buffer.write(b"vpn_node_bootstrap_failed\n")
        raise SystemExit(1) from None
    sys.stdout.buffer.write(_receipt_bytes(receipt))


def _json_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail("vpn_node_bootstrap_mutation_uncertain")
        result[key] = value
    return result


def _parse_receipt(raw: bytes) -> VpnNodeBootstrapReceipt:
    from app.services.vpn_xui_node_http import NodePanelError

    try:
        value = json.loads(
            raw.decode("ascii", errors="strict"),
            object_pairs_hook=_json_pairs,
            parse_constant=lambda _value: _fail(
                "vpn_node_bootstrap_mutation_uncertain"
            ),
        )
        if (
            type(value) is not dict
            or set(value)
            != {"state", "panel_url", "database_path", "xui_version"}
            or value["state"] not in {"prepared", "already_prepared"}
            or any(
                type(value[key]) is not str
                for key in ("state", "panel_url", "database_path", "xui_version")
            )
            or not value["xui_version"]
        ):
            _fail("vpn_node_bootstrap_mutation_uncertain")
        _validate_panel_url(value["panel_url"])
        database = PurePosixPath(value["database_path"])
        if (
            not database.is_absolute()
            or ".." in database.parts
            or str(database) != value["database_path"]
        ):
            _fail("vpn_node_bootstrap_mutation_uncertain")
        receipt = VpnNodeBootstrapReceipt(
            value["state"],
            value["panel_url"],
            value["database_path"],
            value["xui_version"],
        )
        if raw != _receipt_bytes(receipt):
            _fail("vpn_node_bootstrap_mutation_uncertain")
        return receipt
    except VpnNodeBootstrapError:
        raise
    except (NodePanelError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        _fail("vpn_node_bootstrap_mutation_uncertain")


async def execute_vpn_node_bootstrap(
    snapshot,
    bundle: bytes,
    *,
    expected_sha256: str,
    connector=asyncssh.connect if asyncssh is not None else None,
) -> VpnNodeBootstrapReceipt:
    """Execute one reviewed bootstrap bundle over strict pinned SSH."""
    from app.services.vpn_node_transport import (
        _cleanup_health_transport,
        _connection_options,
        _read_process_output,
    )

    if (
        type(bundle) is not bytes
        or not 0 < len(bundle) <= MAX_BUNDLE_BYTES
        or type(expected_sha256) is not str
        or _DIGEST.fullmatch(expected_sha256) is None
        or hashlib.sha256(bundle).hexdigest() != expected_sha256
        or connector is None
    ):
        _fail()
    connection = None
    process = None
    phase: Literal["preflight", "mutation"] = "preflight"
    try:
        options = _connection_options(snapshot)
        async with asyncio.timeout(20.0):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        async with asyncio.timeout(BOOTSTRAP_TIMEOUT):
            process = await connection.create_process(
                FIXED_BOOTSTRAP_COMMAND,
                term_type=None,
                encoding=None,
            )
            phase = "mutation"
            wire = (
                expected_sha256.encode("ascii")
                + b"\n"
                + str(len(bundle)).encode("ascii")
                + b"\n"
                + bundle
            )
            process.stdin.write(wire)
            await process.stdin.drain()
            process.stdin.write_eof()
            stdout, stderr = await _read_process_output(
                process,
                stdout_limit=MAX_REMOTE_OUTPUT_BYTES,
            )
            await process.wait()
        if stderr:
            _fail("vpn_node_bootstrap_mutation_uncertain")
        if process.exit_status == 0:
            return _parse_receipt(stdout)
        if process.exit_status == 1:
            try:
                code = stdout.decode("ascii", errors="strict").removesuffix("\n")
            except UnicodeError:
                _fail("vpn_node_bootstrap_mutation_uncertain")
            if stdout == (code + "\n").encode("ascii") and code in _ERROR_CODES:
                _fail(code)
        _fail("vpn_node_bootstrap_mutation_uncertain")
    except asyncio.CancelledError:
        raise
    except VpnNodeBootstrapError:
        raise
    except BaseException as error:
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            raise
        _fail(
            "vpn_node_bootstrap_mutation_uncertain"
            if phase == "mutation"
            else "vpn_node_bootstrap_failed"
        )
    finally:
        await _cleanup_health_transport(connection, process, None)
