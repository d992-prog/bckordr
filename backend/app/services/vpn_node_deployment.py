"""One-shot, node-local installation of the reviewed VPN runner."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
import sys
from uuid import uuid4
import zipfile

from app.services.vpn_node_bundle import IMPORT_PROBE_SENTINEL
from app.services.vpn_node_journal import initialize_node_journal


MAX_BUNDLE_BYTES = 4 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
MAX_DATABASE_BYTES = 16 * 1024 * 1024 * 1024
DEPLOYMENT_HELPER_SENTINEL = b"veltrix-vpn-deployer-import-ok-v1\n"
_HELPER_MEMBERS = (
    "app/__init__.py",
    "app/services/__init__.py",
    "app/services/vpn_node_bundle.py",
    "app/services/vpn_node_journal.py",
    "app/services/vpn_node_deployment.py",
)
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_REMOTE_BOOTSTRAP = r'''import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

MAX_HELPER = 8 * 1024 * 1024
FAILED = b"vpn_node_deployment_failed\n"
INCOMPLETE = b"vpn_node_deployment_incomplete\n"
INSTALLED = b"vpn_node_deployment_installed\n"
RECOVERED = b"vpn_node_deployment_recovered\n"

def emit(raw, code):
    sys.stdout.buffer.write(raw)
    raise SystemExit(code)

candidate = None
keep = False
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
    for path in (parent, *parent.parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or info.st_mode & 0o022):
            emit(FAILED, 1)
    candidate = parent / (".deployment-" + digest + ".pyz")
    if any(path != candidate for path in parent.glob(".deployment-*.pyz")):
        emit(INCOMPLETE, 1)
    if candidate.exists() or candidate.is_symlink():
        info = candidate.lstat()
        if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
                or info.st_uid != 0 or stat.S_IMODE(info.st_mode) != 0o600
                or candidate.stat().st_size != size
                or hashlib.sha256(candidate.read_bytes()).hexdigest() != digest):
            emit(INCOMPLETE, 1)
    else:
        descriptor = os.open(
            candidate,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW,
            0o600,
        )
        try:
            view = memoryview(raw)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    emit(INCOMPLETE, 1)
                view = view[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    result = subprocess.run(
        ["/usr/bin/python3", "-I", "-S", str(candidate)],
        input=b"",
        capture_output=True,
        timeout=90,
        check=False,
    )
    allowed = {
        (0, INSTALLED): INSTALLED,
        (0, RECOVERED): RECOVERED,
        (1, FAILED): FAILED,
        (1, INCOMPLETE): INCOMPLETE,
    }
    output = allowed.get((result.returncode, result.stdout))
    if output is None or result.stderr:
        keep = True
        emit(INCOMPLETE, 1)
    keep = output == INCOMPLETE
    emit(output, result.returncode)
except SystemExit:
    raise
except BaseException:
    keep = candidate is not None and candidate.exists()
    emit(INCOMPLETE if keep else FAILED, 1)
finally:
    if candidate is not None and candidate.exists() and not keep:
        try:
            candidate.unlink()
            directory = os.open(
                candidate.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
'''
FIXED_DEPLOY_COMMAND = (
    "/usr/bin/python3 -I -S -c 'import base64;exec(base64.b64decode(\""
    + base64.b64encode(_REMOTE_BOOTSTRAP.encode("ascii")).decode("ascii")
    + "\"))'"
)
REMOTE_DEPLOY_TIMEOUT = 120.0


class NodeDeploymentError(RuntimeError):
    def __init__(self, code: str = "vpn_node_deployment_failed") -> None:
        if code not in {"vpn_node_deployment_failed", "vpn_node_deployment_incomplete"}:
            code = "vpn_node_deployment_failed"
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class NodeDeploymentLayout:
    active: Path
    previous: Path
    state: Path
    config: Path
    token: Path
    journal: Path


DEFAULT_LAYOUT = NodeDeploymentLayout(
    active=Path("/opt/veltrix-vpn/current/vpn-node.pyz"),
    previous=Path("/opt/veltrix-vpn/current/vpn-node.pyz.previous"),
    state=Path("/var/lib/veltrix-vpn/deployment-state.json"),
    config=Path("/var/lib/veltrix-vpn/control-auth/node.json"),
    token=Path("/var/lib/veltrix-vpn/control-auth/api-token"),
    journal=Path("/var/lib/veltrix-vpn/control-journal"),
)


def _fail() -> None:
    raise NodeDeploymentError from None


def _sync_directory(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _private_regular(
    path: Path,
    owner_uid: int,
    *,
    bounded: int,
    enforce_metadata: bool = True,
) -> os.stat_result:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (enforce_metadata and before.st_uid != owner_uid)
            or (enforce_metadata and stat.S_IMODE(before.st_mode) != 0o600)
            or not 0 < before.st_size <= bounded
        ):
            _fail()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            _fail()
        return before
    except (OSError, TypeError, ValueError):
        _fail()


def _secure_ancestors(path: Path, owner_uid: int, *, require_posix: bool) -> None:
    if not path.is_absolute() or ".." in path.parts:
        _fail()
    if not require_posix:
        return
    try:
        for ancestor in reversed(path.parents):
            info = ancestor.lstat()
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or info.st_uid not in {0, owner_uid}
                or info.st_mode & 0o022
            ):
                _fail()
    except OSError:
        _fail()


def _config(raw: bytes) -> Path:
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CONFIG_BYTES:
        _fail()
    try:
        def exact_object(pairs):
            result = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError
                result[key] = item
            return result

        value = json.loads(
            raw.decode("ascii"),
            object_pairs_hook=exact_object,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
        )
        if (
            type(value) is not dict
            or set(value) != {"version", "panel_url", "database_path"}
            or type(value["version"]) is not int
            or value["version"] != 1
            or type(value["panel_url"]) is not str
            or type(value["database_path"]) is not str
        ):
            _fail()
        from urllib.parse import urlsplit

        panel = urlsplit(value["panel_url"])
        if (
            panel.scheme != "http"
            or not panel.hostname
            or not ipaddress.ip_address(panel.hostname).is_loopback
            or panel.username is not None
            or panel.password is not None
            or panel.query
            or panel.fragment
        ):
            _fail()
        database = Path(value["database_path"])
        if not database.is_absolute() or ".." in database.parts:
            _fail()
        return database
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
        _fail()


def _identity(
    path: Path,
    bounded: int,
    owner_uid: int,
    *,
    enforce_metadata: bool,
) -> tuple[object, ...]:
    descriptor = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or (enforce_metadata and before.st_uid != owner_uid)
            or (enforce_metadata and stat.S_IMODE(before.st_mode) != 0o600)
            or not 0 < before.st_size <= bounded
        ):
            _fail()
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_BINARY", 0),
        )
        opened = os.fstat(descriptor)
        baseline = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
            before.st_mode,
            before.st_uid,
        )
        def stable(info):
            result = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
            )
            if enforce_metadata:
                return result + (info.st_ctime_ns, info.st_mode, info.st_uid)
            return result

        if stable(before) != stable(opened):
            _fail()
        digest = hashlib.sha256()
        remaining = bounded + 1
        while remaining:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            digest.update(chunk)
            remaining -= len(chunk)
        if remaining == 0 and os.read(descriptor, 1):
            _fail()
        after = os.fstat(descriptor)
        linked = path.lstat()
        for info in (after, linked):
            if stable(before) != stable(info):
                _fail()
        return (*baseline, digest.hexdigest())
    except NodeDeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        _fail()
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail()


def _journal_identity(
    directory: Path,
    owner_uid: int,
    *,
    enforce_metadata: bool,
) -> tuple[tuple[object, ...], ...]:
    try:
        info = directory.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or (enforce_metadata and info.st_uid != owner_uid)
            or (enforce_metadata and info.st_mode & 0o077)
            or {path.name for path in directory.iterdir()}
            != {"gate.sqlite3", "operations.sqlite3"}
        ):
            _fail()
        identities = tuple(
            _identity(
                directory / name,
                MAX_BUNDLE_BYTES,
                owner_uid,
                enforce_metadata=enforce_metadata,
            )
            for name in ("gate.sqlite3", "operations.sqlite3")
        )
        from app.services import vpn_node_journal as journal

        connections = []
        try:
            for name in ("gate.sqlite3", "operations.sqlite3"):
                connection = sqlite3.connect(
                    (directory / name).as_uri() + "?mode=ro",
                    uri=True,
                    isolation_level=None,
                )
                connection.execute("PRAGMA query_only=ON")
                connections.append(connection)
            gate, operations = connections
            gate_id = journal._metadata(gate, journal._GATE_SCHEMA)
            operations_id = journal._metadata(operations, journal._JOURNAL_SCHEMA)
            journal._validate_rows(operations)
            if gate_id != operations_id:
                _fail()
        finally:
            for connection in connections:
                connection.close()
        return identities
    except NodeDeploymentError:
        raise
    except (OSError, TypeError, ValueError, sqlite3.Error, RuntimeError):
        _fail()


def _write_exclusive(
    path: Path,
    raw: bytes,
    *,
    directory_fd: int | None = None,
) -> None:
    name: str | Path = path.name if directory_fd is not None else path
    descriptor = os.open(
        name,
        os.O_CREAT
        | os.O_EXCL
        | os.O_WRONLY
        | getattr(os, "O_BINARY", 0),
        0o600,
        dir_fd=directory_fd,
    )
    try:
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail()
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _probe(path: Path, python_executable: Path, expected_sha256: str | None = None) -> None:
    result = subprocess.run(
        [str(python_executable), "-I", "-S", str(path), "--import-probe"],
        input=b"",
        capture_output=True,
        timeout=10,
        check=False,
    )
    if (
        result.returncode != 0
        or result.stdout != IMPORT_PROBE_SENTINEL
        or result.stderr
        or (
            expected_sha256 is not None
            and hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
        )
    ):
        _fail()


def _atomic_create(path: Path, raw: bytes) -> None:
    candidate = path.parent / f".{path.name}.{uuid4().hex}.tmp"
    directory_fd = None
    try:
        if os.name == "posix":
            directory_fd = os.open(
                path.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        _write_exclusive(candidate, raw, directory_fd=directory_fd)
        if directory_fd is None:
            os.replace(candidate, path)
            _sync_directory(path.parent)
        else:
            os.replace(
                candidate.name,
                path.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
    finally:
        try:
            if directory_fd is None:
                candidate.unlink(missing_ok=True)
            else:
                try:
                    os.unlink(candidate.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        if directory_fd is not None:
            os.close(directory_fd)


def _zip_entry(name: str) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, _ZIP_TIMESTAMP)
    entry.compress_type = zipfile.ZIP_DEFLATED
    entry.create_system = 3
    entry.external_attr = 0o100600 << 16
    return entry


def _path_literal(path: Path) -> str:
    return json.dumps(str(path), ensure_ascii=True)


def _helper_main(
    layout: NodeDeploymentLayout,
    expected_node_sha256: str,
    python_executable: Path,
    owner_uid: int,
    require_posix: bool,
) -> bytes:
    fields = ",\n        ".join(
        f"{name}=Path({_path_literal(getattr(layout, name))})"
        for name in ("active", "previous", "state", "config", "token", "journal")
    )
    return (
        "from pathlib import Path\n"
        "import sys\n"
        "import zipfile\n"
        "from app.services.vpn_node_deployment import (\n"
        "    DEPLOYMENT_HELPER_SENTINEL, NodeDeploymentError,\n"
        "    NodeDeploymentLayout, install_node_release,\n"
        ")\n"
        "if sys.argv[1:] == ['--import-probe']:\n"
        "    sys.stdout.buffer.write(DEPLOYMENT_HELPER_SENTINEL)\n"
        "    raise SystemExit(0)\n"
        "with zipfile.ZipFile(sys.argv[0]) as archive:\n"
        "    bundle = archive.read('payload/vpn-node.pyz')\n"
        "    config = archive.read('payload/node.json')\n"
        "layout = NodeDeploymentLayout(\n        "
        + fields
        + "\n)\n"
        "try:\n"
        "    result = install_node_release(\n"
        "        bundle,\n"
        f"        expected_sha256={expected_node_sha256!r},\n"
        "        node_config=config,\n"
        "        layout=layout,\n"
        f"        python_executable=Path({_path_literal(python_executable)}),\n"
        f"        owner_uid={owner_uid!r},\n"
        f"        require_posix={require_posix!r},\n"
        "    )\n"
        "except NodeDeploymentError as error:\n"
        "    sys.stdout.buffer.write((error.code + '\\n').encode('ascii'))\n"
        "    raise SystemExit(1)\n"
        "sys.stdout.buffer.write(('vpn_node_deployment_' + result + '\\n').encode('ascii'))\n"
        "raise SystemExit(0)\n"
    ).encode("ascii")


def build_node_deployment_helper(
    source_root: Path,
    target: Path,
    *,
    node_bundle: bytes,
    expected_node_sha256: str,
    node_config: bytes,
    layout: NodeDeploymentLayout = DEFAULT_LAYOUT,
    python_executable: Path = Path("/usr/bin/python3"),
    owner_uid: int = 0,
    require_posix: bool = True,
) -> str:
    """Build the deterministic one-shot helper sent over strict SSH stdin."""
    if (
        not source_root.is_dir()
        or not target.is_absolute()
        or not target.parent.is_dir()
        or not isinstance(node_bundle, bytes)
        or hashlib.sha256(node_bundle).hexdigest() != expected_node_sha256
    ):
        _fail()
    _config(node_config)
    members = []
    try:
        for name in _HELPER_MEMBERS:
            raw = (source_root / name).read_bytes()
            compile(raw, name, "exec", dont_inherit=True)
            members.append((name, raw))
        main = _helper_main(
            layout,
            expected_node_sha256,
            python_executable,
            owner_uid,
            require_posix,
        )
        compile(main, "__main__.py", "exec", dont_inherit=True)
        temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_CREAT
                | os.O_EXCL
                | os.O_WRONLY
                | getattr(os, "O_BINARY", 0),
                0o600,
            )
            os.close(descriptor)
            with zipfile.ZipFile(temporary, "w") as archive:
                archive.writestr(_zip_entry("__main__.py"), main)
                for name, raw in members:
                    archive.writestr(_zip_entry(name), raw)
                archive.writestr(_zip_entry("payload/vpn-node.pyz"), node_bundle)
                archive.writestr(_zip_entry("payload/node.json"), node_config)
            os.chmod(temporary, 0o600)
            probe = subprocess.run(
                [sys.executable, "-I", "-S", str(temporary), "--import-probe"],
                input=b"",
                capture_output=True,
                timeout=10,
                check=False,
            )
            if (
                probe.returncode != 0
                or probe.stdout != DEPLOYMENT_HELPER_SENTINEL
                or probe.stderr
            ):
                _fail()
            raw = temporary.read_bytes()
            digest = hashlib.sha256(raw).hexdigest()
            descriptor = os.open(
                temporary,
                os.O_RDWR | getattr(os, "O_BINARY", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, target)
            _sync_directory(target.parent)
            return digest
        finally:
            temporary.unlink(missing_ok=True)
    except NodeDeploymentError:
        raise
    except (OSError, SyntaxError, ValueError, subprocess.SubprocessError):
        _fail()


async def deploy_node_helper_over_ssh(
    snapshot,
    helper: bytes,
    *,
    expected_sha256: str,
    connector=None,
) -> str:
    """Send one reviewed helper over strict Ed25519-pinned SSH stdin."""
    import asyncssh

    from app.services.vpn_node_transport import _parse_known_hosts, _read_process_output

    if connector is None:
        connector = asyncssh.connect
    password_mode = type(snapshot.password) is str and bool(snapshot.password)
    key_mode = isinstance(snapshot.client_key, asyncssh.SSHKey)
    if (
        not isinstance(helper, bytes)
        or not 0 < len(helper) <= 8 * 1024 * 1024
        or _DIGEST.fullmatch(expected_sha256) is None
        or hashlib.sha256(helper).hexdigest() != expected_sha256
        or type(snapshot.host) is not str
        or not snapshot.host
        or type(snapshot.port) is not int
        or not 1 <= snapshot.port <= 65535
        or snapshot.username != "root"
        or password_mode == key_mode
    ):
        _fail()
    phase = "preflight"
    try:
        known_hosts = _parse_known_hosts(
            snapshot.known_hosts,
            snapshot.host,
            snapshot.port,
        )
        options = {
            "config": None,
            "known_hosts": known_hosts,
            "client_keys": None if password_mode else [snapshot.client_key],
            "password": snapshot.password if password_mode else None,
            "preferred_auth": ["password"] if password_mode else ["publickey"],
            "agent_path": None,
            "agent_forwarding": False,
            "pkcs11_provider": None,
            "x509_trusted_certs": None,
            "x509_trusted_cert_paths": [],
            "server_host_key_algs": ["ssh-ed25519"],
            "gss_host": None,
            "gss_kex": False,
            "gss_auth": False,
            "host_based_auth": False,
            "kbdint_auth": False,
            "public_key_auth": not password_mode,
            "password_auth": password_mode,
            "disable_trivial_auth": True,
            "connect_timeout": 10.0,
            "login_timeout": 10.0,
        }
        wire = (
            expected_sha256.encode("ascii")
            + b"\n"
            + str(len(helper)).encode("ascii")
            + b"\n"
            + helper
        )
        async with asyncio.timeout(20.0):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        async with asyncio.timeout(REMOTE_DEPLOY_TIMEOUT):
            async with connection:
                process = await connection.create_process(
                    FIXED_DEPLOY_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                phase = "mutation"
                process.stdin.write(wire)
                await process.stdin.drain()
                process.stdin.write_eof()
                stdout, stderr = await _read_process_output(process)
                await process.wait()
        outcomes = {
            (0, b"vpn_node_deployment_installed\n"): "installed",
            (0, b"vpn_node_deployment_recovered\n"): "recovered",
            (1, b"vpn_node_deployment_failed\n"): "failed",
            (1, b"vpn_node_deployment_incomplete\n"): "incomplete",
        }
        outcome = outcomes.get((process.exit_status, stdout))
        if outcome is None or stderr:
            _fail()
        return outcome
    except asyncio.CancelledError:
        raise
    except NodeDeploymentError:
        raise
    except Exception:
        raise NodeDeploymentError(
            "vpn_node_deployment_incomplete"
            if phase == "mutation"
            else "vpn_node_deployment_failed"
        ) from None


async def deploy_prebuilt_node_release(
    worker,
    *,
    known_hosts_path: Path,
    helper_path: Path,
    expected_sha256: str,
    owner_uid: int | None = None,
    require_posix: bool = True,
    snapshot_loader=None,
    transport=None,
) -> str:
    """Practical admin entrypoint for one prebuilt, reviewed deployment helper."""
    from app.services.vpn_node_transport import (
        _read_private_file,
        load_transport_snapshot,
    )

    if snapshot_loader is None:
        snapshot_loader = load_transport_snapshot
    if transport is None:
        transport = deploy_node_helper_over_ssh
    if owner_uid is None:
        owner_uid = getattr(os, "geteuid", lambda: -1)()
    try:
        _secure_ancestors(
            helper_path,
            owner_uid,
            require_posix=require_posix,
        )
        if require_posix:
            raw = _read_private_file(
                helper_path,
                limit=8 * 1024 * 1024,
                owner_uid=owner_uid,
            )
        else:
            _private_regular(
                helper_path,
                owner_uid,
                bounded=8 * 1024 * 1024,
                enforce_metadata=False,
            )
            raw = helper_path.read_bytes()
        if (
            _DIGEST.fullmatch(expected_sha256) is None
            or hashlib.sha256(raw).hexdigest() != expected_sha256
        ):
            _fail()
        snapshot = snapshot_loader(worker, known_hosts_path)
        outcome = await transport(
            snapshot,
            raw,
            expected_sha256=expected_sha256,
        )
        if outcome not in {"installed", "recovered", "failed", "incomplete"}:
            _fail()
        return outcome
    except asyncio.CancelledError:
        raise
    except NodeDeploymentError:
        raise
    except Exception:
        _fail()


def install_control_known_hosts(
    raw: bytes,
    *,
    target: Path,
    host: str,
    port: int,
    owner_uid: int,
    require_posix: bool = True,
) -> str:
    """Install one immutable literal Ed25519 pin for the control service."""
    from app.services.vpn_node_transport import VpnNodeTransportError, _parse_known_hosts

    try:
        _parse_known_hosts(raw, host, port)
        _secure_ancestors(target, owner_uid, require_posix=require_posix)
        if target.exists() or target.is_symlink():
            _private_regular(
                target,
                owner_uid,
                bounded=64 * 1024,
                enforce_metadata=require_posix,
            )
            if target.read_bytes() != raw:
                _fail()
            return "unchanged"

        candidate = target.parent / f".{target.name}.{uuid4().hex}.tmp"
        linked = False
        directory_fd = None
        try:
            if os.name == "posix":
                directory_fd = os.open(
                    target.parent,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                )
            _write_exclusive(candidate, raw, directory_fd=directory_fd)
            if require_posix:
                descriptor = os.open(
                    candidate.name if directory_fd is not None else candidate,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=directory_fd,
                )
                try:
                    os.fchown(descriptor, owner_uid, -1)
                    os.fchmod(descriptor, 0o600)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            if directory_fd is None:
                os.link(candidate, target, follow_symlinks=False)
                _sync_directory(target.parent)
            else:
                os.link(
                    candidate.name,
                    target.name,
                    src_dir_fd=directory_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
                os.fsync(directory_fd)
            linked = True
            candidate_info = candidate.lstat()
            target_info = _private_regular(
                target,
                owner_uid,
                bounded=64 * 1024,
                enforce_metadata=require_posix,
            )
            if (
                (candidate_info.st_dev, candidate_info.st_ino)
                != (target_info.st_dev, target_info.st_ino)
                or target.read_bytes() != raw
            ):
                _fail()
        except BaseException:
            if linked:
                try:
                    linked_info = target.lstat()
                    candidate_info = candidate.lstat()
                    if (linked_info.st_dev, linked_info.st_ino) == (
                        candidate_info.st_dev,
                        candidate_info.st_ino,
                    ):
                        target.unlink()
                        if directory_fd is None:
                            _sync_directory(target.parent)
                        else:
                            os.fsync(directory_fd)
                except OSError:
                    pass
            raise
        finally:
            if directory_fd is None:
                candidate.unlink(missing_ok=True)
            else:
                try:
                    os.unlink(candidate.name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
                os.close(directory_fd)
        return "installed"
    except NodeDeploymentError:
        raise
    except (OSError, TypeError, ValueError, VpnNodeTransportError):
        _fail()


def _remove_exact_file(path: Path, raw: bytes) -> None:
    if not path.exists() or path.is_symlink() or path.read_bytes() != raw:
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    path.unlink()
    _sync_directory(path.parent)


def _remove_new_journal(path: Path) -> None:
    if path.is_symlink() or not path.is_dir():
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    children = {child.name: child for child in path.iterdir()}
    allowed = {
        "gate.sqlite3",
        "operations.sqlite3",
        "gate.sqlite3-journal",
        "operations.sqlite3-journal",
    }
    if not set(children).issubset(allowed):
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    for child in children.values():
        if child.is_symlink() or not child.is_file():
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        child.unlink()
    path.rmdir()
    _sync_directory(path.parent)


def _rollback(
    *,
    layout: NodeDeploymentLayout,
    bundle: bytes,
    old_active: bytes | None,
    node_config: bytes,
    config_preexisting: bool,
    journal_preexisting: bool,
    marker: bytes,
    candidate: Path,
    remove_state: bool,
) -> None:
    if candidate.exists() or candidate.is_symlink():
        _remove_exact_file(candidate, bundle)
    if old_active is None:
        if layout.active.exists() or layout.active.is_symlink():
            _remove_exact_file(layout.active, bundle)
    elif layout.active.exists() and not layout.active.is_symlink():
        current = layout.active.read_bytes()
        if current == bundle:
            _remove_exact_file(layout.active, bundle)
            if not layout.previous.exists() or layout.previous.read_bytes() != old_active:
                raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
            os.replace(layout.previous, layout.active)
            _sync_directory(layout.active.parent)
        elif current == old_active:
            if layout.previous.exists() or layout.previous.is_symlink():
                _remove_exact_file(layout.previous, old_active)
        else:
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    elif layout.previous.exists() and layout.previous.read_bytes() == old_active:
        os.replace(layout.previous, layout.active)
        _sync_directory(layout.active.parent)
    else:
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None

    if not config_preexisting and (layout.config.exists() or layout.config.is_symlink()):
        _remove_exact_file(layout.config, node_config)
    if not journal_preexisting and (layout.journal.exists() or layout.journal.is_symlink()):
        _remove_new_journal(layout.journal)
    if remove_state and (layout.state.exists() or layout.state.is_symlink()):
        _remove_exact_file(layout.state, marker)


def _decode_state(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(raw.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    keys = {
        "version",
        "bundle_sha256",
        "old_sha256",
        "candidate",
        "config_preexisting",
        "config_identity",
        "journal_preexisting",
        "journal_identity",
        "token_identity",
    }
    if (
        type(value) is not dict
        or set(value) != keys
        or value["version"] != 1
        or _DIGEST.fullmatch(value["bundle_sha256"] or "") is None
        or (
            value["old_sha256"] is not None
            and _DIGEST.fullmatch(value["old_sha256"] or "") is None
        )
        or re.fullmatch(r"\.candidate-[0-9a-f]{32}\.pyz", value["candidate"] or "")
        is None
        or type(value["config_preexisting"]) is not bool
        or type(value["journal_preexisting"]) is not bool
        or (
            value["config_preexisting"]
            and (
                type(value["config_identity"]) is not list
                or len(value["config_identity"]) != 8
            )
        )
        or (not value["config_preexisting"] and value["config_identity"] is not None)
        or (
            value["journal_preexisting"]
            and (
                type(value["journal_identity"]) is not list
                or len(value["journal_identity"]) != 2
                or any(
                    type(item) is not list or len(item) != 8
                    for item in value["journal_identity"]
                )
            )
        )
        or (not value["journal_preexisting"] and value["journal_identity"] is not None)
        or type(value["token_identity"]) is not list
        or len(value["token_identity"]) != 8
    ):
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
    return value


def _recover_interrupted(
    *,
    layout: NodeDeploymentLayout,
    bundle: bytes,
    expected_sha256: str,
    node_config: bytes,
    owner_uid: int,
    require_posix: bool,
) -> str:
    try:
        raw = layout.state.read_bytes()
        state = _decode_state(raw)
        if state["bundle_sha256"] != expected_sha256:
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        token_identity = _identity(
            layout.token,
            MAX_TOKEN_BYTES,
            owner_uid,
            enforce_metadata=require_posix,
        )
        if list(token_identity) != state["token_identity"]:
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        if state["config_preexisting"]:
            if (
                layout.config.read_bytes() != node_config
                or list(
                    _identity(
                        layout.config,
                        MAX_CONFIG_BYTES,
                        owner_uid,
                        enforce_metadata=require_posix,
                    )
                )
                != state["config_identity"]
            ):
                raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        if state["journal_preexisting"]:
            current_journal = _journal_identity(
                layout.journal,
                owner_uid,
                enforce_metadata=require_posix,
            )
            if [list(item) for item in current_journal] != state["journal_identity"]:
                raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        old_digest = state["old_sha256"]
        old_active = None
        if old_digest is not None:
            for path in (layout.previous, layout.active):
                if path.exists() and not path.is_symlink():
                    candidate_raw = path.read_bytes()
                    if hashlib.sha256(candidate_raw).hexdigest() == old_digest:
                        old_active = candidate_raw
                        break
            if old_active is None:
                raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        candidate = layout.active.parent / str(state["candidate"])
        _rollback(
            layout=layout,
            bundle=bundle,
            old_active=old_active,
            node_config=node_config,
            config_preexisting=bool(state["config_preexisting"]),
            journal_preexisting=bool(state["journal_preexisting"]),
            marker=raw,
            candidate=candidate,
            remove_state=True,
        )
        return "recovered"
    except NodeDeploymentError:
        raise
    except (OSError, TypeError, ValueError):
        raise NodeDeploymentError("vpn_node_deployment_incomplete") from None


def install_node_release(
    bundle: bytes,
    *,
    expected_sha256: str,
    node_config: bytes,
    layout: NodeDeploymentLayout = DEFAULT_LAYOUT,
    python_executable: Path = Path("/usr/bin/python3"),
    owner_uid: int = 0,
    require_posix: bool = True,
) -> str:
    """Install one artifact locally; callers must supply a strictly pinned channel."""
    if require_posix and (os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0):
        _fail()
    if (
        not isinstance(bundle, bytes)
        or not 0 < len(bundle) <= MAX_BUNDLE_BYTES
        or not isinstance(expected_sha256, str)
        or len(expected_sha256) != 64
        or hashlib.sha256(bundle).hexdigest() != expected_sha256
        or not isinstance(layout, NodeDeploymentLayout)
    ):
        _fail()
    database_path = _config(node_config)
    for path in (
        layout.active,
        layout.previous,
        layout.state,
        layout.config,
        layout.token,
        layout.journal,
    ):
        _secure_ancestors(path, owner_uid, require_posix=require_posix)
    _secure_ancestors(database_path, owner_uid, require_posix=require_posix)
    _private_regular(
        database_path,
        owner_uid,
        bounded=MAX_DATABASE_BYTES,
        enforce_metadata=require_posix,
    )
    token_identity = _identity(
        layout.token,
        MAX_TOKEN_BYTES,
        owner_uid,
        enforce_metadata=require_posix,
    )
    if layout.state.exists() or layout.state.is_symlink():
        _private_regular(
            layout.state,
            owner_uid,
            bounded=MAX_CONFIG_BYTES,
            enforce_metadata=require_posix,
        )
        return _recover_interrupted(
            layout=layout,
            bundle=bundle,
            expected_sha256=expected_sha256,
            node_config=node_config,
            owner_uid=owner_uid,
            require_posix=require_posix,
        )
    if layout.previous.exists() or layout.previous.is_symlink():
        _fail()
    old_active = None
    if layout.active.exists() or layout.active.is_symlink():
        _private_regular(
            layout.active,
            owner_uid,
            bounded=MAX_BUNDLE_BYTES,
            enforce_metadata=require_posix,
        )
        _probe(layout.active, python_executable)
        old_active = layout.active.read_bytes()
    config_preexisting = layout.config.exists() or layout.config.is_symlink()
    config_identity = None
    if config_preexisting:
        if layout.config.read_bytes() != node_config:
            _fail()
        config_identity = _identity(
            layout.config,
            MAX_CONFIG_BYTES,
            owner_uid,
            enforce_metadata=require_posix,
        )
    journal_preexisting = layout.journal.exists() or layout.journal.is_symlink()
    journal_identity = None
    if journal_preexisting:
        journal_identity = _journal_identity(
            layout.journal,
            owner_uid,
            enforce_metadata=require_posix,
        )

    candidate = layout.active.parent / f".candidate-{uuid4().hex}.pyz"
    marker = json.dumps(
        {
            "version": 1,
            "bundle_sha256": expected_sha256,
            "old_sha256": (
                hashlib.sha256(old_active).hexdigest()
                if old_active is not None
                else None
            ),
            "candidate": candidate.name,
            "config_preexisting": config_preexisting,
            "config_identity": (
                list(config_identity) if config_identity is not None else None
            ),
            "journal_preexisting": journal_preexisting,
            "journal_identity": (
                [list(item) for item in journal_identity]
                if journal_identity is not None
                else None
            ),
            "token_identity": list(token_identity),
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("ascii")
    active_directory_fd = None
    try:
        if os.name == "posix":
            active_directory_fd = os.open(
                layout.active.parent,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            )
        _atomic_create(layout.state, marker)
        _write_exclusive(
            candidate,
            bundle,
            directory_fd=active_directory_fd,
        )
        _probe(candidate, python_executable, expected_sha256)
        if not layout.config.exists():
            _atomic_create(layout.config, node_config)
            if layout.config.read_bytes() != node_config:
                _fail()
            _identity(
                layout.config,
                MAX_CONFIG_BYTES,
                owner_uid,
                enforce_metadata=require_posix,
            )
        if journal_identity is None:
            layout.journal.mkdir(mode=0o700)
            initialize_node_journal(layout.journal)
            _journal_identity(
                layout.journal,
                owner_uid,
                enforce_metadata=require_posix,
            )
        if old_active is not None:
            _write_exclusive(
                layout.previous,
                old_active,
                directory_fd=active_directory_fd,
            )
            if active_directory_fd is None:
                _sync_directory(layout.previous.parent)
            else:
                os.fsync(active_directory_fd)
        if active_directory_fd is None:
            os.replace(candidate, layout.active)
            _sync_directory(layout.active.parent)
        else:
            os.replace(
                candidate.name,
                layout.active.name,
                src_dir_fd=active_directory_fd,
                dst_dir_fd=active_directory_fd,
            )
            os.fsync(active_directory_fd)
        if (
            _identity(
                layout.token,
                MAX_TOKEN_BYTES,
                owner_uid,
                enforce_metadata=require_posix,
            )
            != token_identity
        ):
            _fail()
        if journal_identity is not None and _journal_identity(
            layout.journal,
            owner_uid,
            enforce_metadata=require_posix,
        ) != journal_identity:
            _fail()
        if config_identity is not None and _identity(
            layout.config,
            MAX_CONFIG_BYTES,
            owner_uid,
            enforce_metadata=require_posix,
        ) != config_identity:
            _fail()
        layout.state.unlink()
        _sync_directory(layout.state.parent)
        return "installed"
    except (NodeDeploymentError, OSError, subprocess.SubprocessError, ValueError):
        try:
            protected_state_unchanged = (
                _identity(
                    layout.token,
                    MAX_TOKEN_BYTES,
                    owner_uid,
                    enforce_metadata=require_posix,
                )
                == token_identity
            )
            if config_identity is not None:
                protected_state_unchanged = protected_state_unchanged and (
                    _identity(
                        layout.config,
                        MAX_CONFIG_BYTES,
                        owner_uid,
                        enforce_metadata=require_posix,
                    )
                    == config_identity
                )
            if journal_identity is not None:
                protected_state_unchanged = protected_state_unchanged and (
                    _journal_identity(
                        layout.journal,
                        owner_uid,
                        enforce_metadata=require_posix,
                    )
                    == journal_identity
                )
        except (NodeDeploymentError, OSError, ValueError):
            protected_state_unchanged = False
        try:
            _rollback(
                layout=layout,
                bundle=bundle,
                old_active=old_active,
                node_config=node_config,
                config_preexisting=config_preexisting,
                journal_preexisting=journal_preexisting,
                marker=marker,
                candidate=candidate,
                remove_state=protected_state_unchanged,
            )
        except (NodeDeploymentError, OSError, ValueError):
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        if not protected_state_unchanged:
            raise NodeDeploymentError("vpn_node_deployment_incomplete") from None
        _fail()
    finally:
        try:
            if active_directory_fd is None:
                candidate.unlink(missing_ok=True)
            else:
                try:
                    os.unlink(candidate.name, dir_fd=active_directory_fd)
                except FileNotFoundError:
                    pass
        except OSError:
            pass
        if active_directory_fd is not None:
            os.close(active_directory_fd)
