"""Deterministic candidate bundle and strict fixed-command SSH execution."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path, PurePath, PurePosixPath
import subprocess
import sys
import zipfile
from uuid import uuid4

import asyncssh

from app.services.vpn_node_transport import (
    CONNECT_TIMEOUT,
    LOGIN_TIMEOUT,
    VpnNodeTransportSnapshot,
    _connection_options,
    _read_process_output,
)
from app.services.vpn_reality_endpoint_installer import (
    MAX_RECEIPT_BYTES,
    EndpointInstallError,
    EndpointInstallReceipt,
    EndpointInstallRequest,
    _json_object,
    encode_install_receipt,
    encode_install_request,
    parse_install_receipt,
)


ENDPOINT_BUNDLE_MEMBERS = (
    "app/__init__.py",
    "app/services/__init__.py",
    "app/services/vpn_endpoint_types.py",
    "app/services/vpn_xui_identity.py",
    "app/services/vpn_xui_node_http.py",
    "app/services/vpn_xui_node_observation.py",
    "app/services/vpn_xray_runtime.py",
    "app/services/vpn_reality_endpoint_installer.py",
)
ENDPOINT_IMPORT_PROBE_SENTINEL = b"veltrix-endpoint-installer-import-ok-v1\n"
ENDPOINT_CANDIDATE_PARENT = PurePosixPath("/var/lib/veltrix-vpn")
ENDPOINT_CANDIDATE_NAME = "endpoint-installer-candidate.pyz"
MAX_ENDPOINT_BUNDLE_BYTES = 512 * 1024
FIXED_ENDPOINT_INSTALLER_COMMAND = (
    "/usr/bin/python3 -I -S "
    f"{ENDPOINT_CANDIDATE_PARENT}/{ENDPOINT_CANDIDATE_NAME}"
)
ENDPOINT_OPERATION_TIMEOUT = 30.0
_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
_MAIN = (
    b"import sys\n"
    b"import app.services.vpn_endpoint_types\n"
    b"import app.services.vpn_xui_identity\n"
    b"import app.services.vpn_xui_node_http\n"
    b"import app.services.vpn_xui_node_observation\n"
    b"import app.services.vpn_xray_runtime\n"
    b"from app.services.vpn_reality_endpoint_installer import main\n"
    b"if sys.argv[1:] == ['--import-probe']:\n"
    b"    sys.stdout.buffer.write("
    + repr(ENDPOINT_IMPORT_PROBE_SENTINEL).encode("ascii")
    + b")\n"
    b"    raise SystemExit(0)\n"
    b"if sys.argv[1:]:\n"
    b"    raise SystemExit(2)\n"
    b"raise SystemExit(main())\n"
)


def _candidate_admin_source(parent: PurePath, python_executable: PurePath) -> str:
    if (
        not isinstance(parent, PurePath)
        or not parent.is_absolute()
        or not isinstance(python_executable, PurePath)
        or not python_executable.is_absolute()
    ):
        raise ValueError("invalid candidate admin configuration")
    return rf'''import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import sys

PARENT = Path({str(parent)!r})
TARGET = PARENT / {ENDPOINT_CANDIDATE_NAME!r}
PYTHON = {str(python_executable)!r}
MAX_BUNDLE = {MAX_ENDPOINT_BUNDLE_BYTES}
SENTINEL = {ENDPOINT_IMPORT_PROBE_SENTINEL!r}

def fail():
    raise RuntimeError("candidate administration failed")

def pairs(items):
    result = {{}}
    for key, value in items:
        if key in result:
            fail()
        result[key] = value
    return result

def identity(value):
    base = (value.st_dev, value.st_ino, value.st_size)
    if os.name != "posix":
        return base
    return base + (value.st_mtime_ns, value.st_ctime_ns, value.st_mode, value.st_uid)

def safe_parent():
    for path in (PARENT, *PARENT.parents):
        value = path.lstat()
        if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
            fail()
        if os.name == "posix" and (value.st_uid != 0 or value.st_mode & 0o022):
            fail()

def probe(path):
    result = subprocess.run(
        [PYTHON, "-I", "-S", str(path), "--import-probe"],
        input=b"", capture_output=True, check=False, timeout=10,
    )
    if result.returncode != 0 or result.stdout != SENTINEL or result.stderr:
        fail()

def exact_file(path, digest):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode)
            or not 0 < before.st_size <= MAX_BUNDLE):
        fail()
    if os.name == "posix" and (before.st_uid != 0 or stat.S_IMODE(before.st_mode) != 0o600):
        fail()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if identity(opened) != identity(before):
            fail()
        chunks = []
        size = 0
        while True:
            chunk = os.read(descriptor, min(65536, MAX_BUNDLE + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BUNDLE:
                fail()
        if identity(os.fstat(descriptor)) != identity(opened):
            fail()
    finally:
        os.close(descriptor)
    if identity(path.lstat()) != identity(before):
        fail()
    if hashlib.sha256(b"".join(chunks)).hexdigest() != digest:
        fail()
    probe(path)
    return True

def sync_parent():
    if os.name != "posix":
        return
    descriptor = os.open(PARENT, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                         | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def read_request():
    raw = sys.stdin.buffer.read(MAX_BUNDLE + 2049)
    if len(raw) > MAX_BUNDLE + 2048 or b"\n" not in raw:
        fail()
    line, body = raw.split(b"\n", 1)
    value = json.loads(line.decode("ascii"), object_pairs_hook=pairs)
    if type(value) is not dict or set(value) != {{"version", "action", "sha256", "size"}}:
        fail()
    if (value["version"] != 1 or type(value["version"]) is not int
            or value["action"] not in ("install", "remove")
            or type(value["sha256"]) is not str or len(value["sha256"]) != 64
            or any(item not in "0123456789abcdef" for item in value["sha256"])
            or type(value["size"]) is not int or value["size"] < 0):
        fail()
    canonical = json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    if canonical != line:
        fail()
    if value["action"] == "install":
        if not 0 < value["size"] <= MAX_BUNDLE or len(body) != value["size"]:
            fail()
        if hashlib.sha256(body).hexdigest() != value["sha256"]:
            fail()
    elif value["size"] != 0 or body:
        fail()
    return value, body

def install(value, body):
    digest = value["sha256"]
    if exact_file(TARGET, digest):
        return "already_present"
    temporary = None
    try:
        for _attempt in range(32):
            temporary = PARENT / (".endpoint-installer-" + secrets.token_hex(16) + ".tmp")
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_BINARY", 0),
                    0o600,
                )
                break
            except FileExistsError:
                temporary = None
        else:
            fail()
        try:
            cursor = 0
            while cursor < len(body):
                written = os.write(descriptor, body[cursor:])
                if written <= 0:
                    fail()
                cursor += written
            os.fchmod(descriptor, 0o600)
            if os.name == "posix":
                os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if not exact_file(temporary, digest):
            fail()
        try:
            if os.name == "posix":
                os.link(temporary, TARGET, follow_symlinks=False)
            else:
                os.link(temporary, TARGET)
        except FileExistsError:
            if not exact_file(TARGET, digest):
                fail()
        sync_parent()
        if not exact_file(TARGET, digest):
            fail()
        return "installed"
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
                sync_parent()
            except FileNotFoundError:
                pass

def remove(value):
    digest = value["sha256"]
    if not TARGET.exists() and not TARGET.is_symlink():
        return "absent"
    if not exact_file(TARGET, digest):
        fail()
    before = TARGET.lstat()
    if identity(TARGET.lstat()) != identity(before):
        fail()
    TARGET.unlink()
    sync_parent()
    return "removed"

def main():
    if os.name == "posix" and os.geteuid() != 0:
        fail()
    safe_parent()
    value, body = read_request()
    state = install(value, body) if value["action"] == "install" else remove(value)
    response = {{"version": 1, "state": state, "sha256": value["sha256"]}}
    sys.stdout.buffer.write(json.dumps(response, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()

try:
    main()
except BaseException:
    raise SystemExit(1) from None
'''


_CANDIDATE_ADMIN_SOURCE = _candidate_admin_source(
    ENDPOINT_CANDIDATE_PARENT,
    PurePosixPath("/usr/bin/python3"),
)
FIXED_ENDPOINT_CANDIDATE_ADMIN_COMMAND = (
    "/usr/bin/python3 -I -S -c 'import base64;exec(base64.b64decode(\""
    + base64.b64encode(_CANDIDATE_ADMIN_SOURCE.encode("utf-8")).decode("ascii")
    + "\"))'"
)


class EndpointInstallerDeploymentError(RuntimeError):
    def __init__(self, *, mutation_uncertain: bool = False) -> None:
        self.mutation_uncertain = bool(mutation_uncertain)
        code = (
            "vpn_endpoint_installer_mutation_uncertain"
            if self.mutation_uncertain
            else "vpn_endpoint_installer_transport_failed"
        )
        super().__init__(code)


def _fail(*, mutation_uncertain: bool = False) -> None:
    error = EndpointInstallerDeploymentError(mutation_uncertain=mutation_uncertain)
    try:
        raise error from None
    finally:
        error.__context__ = None


def _entry(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def build_endpoint_installer_bundle(source_root: Path, target: Path) -> str:
    if (
        not isinstance(source_root, Path)
        or not isinstance(target, Path)
        or not source_root.is_dir()
        or not target.is_absolute()
        or not target.parent.is_dir()
    ):
        _fail()
    sources: list[tuple[str, bytes]] = []
    try:
        for member in ENDPOINT_BUNDLE_MEMBERS:
            raw = (source_root / member).read_bytes()
            compile(raw, member, "exec", dont_inherit=True)
            sources.append((member, raw))
    except (OSError, SyntaxError, ValueError, TypeError):
        _fail()
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with zipfile.ZipFile(temporary, "x") as archive:
            archive.writestr(_entry("__main__.py"), _MAIN)
            for member, raw in sources:
                archive.writestr(_entry(member), raw)
        result = subprocess.run(
            [sys.executable, "-I", "-S", str(temporary), "--import-probe"],
            input=b"",
            capture_output=True,
            timeout=10,
            check=False,
        )
        if (
            result.returncode != 0
            or result.stdout != ENDPOINT_IMPORT_PROBE_SENTINEL
            or result.stderr
        ):
            _fail()
        raw_bundle = temporary.read_bytes()
        if not 0 < len(raw_bundle) <= MAX_ENDPOINT_BUNDLE_BYTES:
            _fail()
        digest = hashlib.sha256(raw_bundle).hexdigest()
        with temporary.open("r+b") as handle:
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return digest
    except EndpointInstallerDeploymentError:
        raise
    except (OSError, subprocess.SubprocessError, zipfile.BadZipFile):
        _fail()
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _encode_candidate_admin_request(
    action: str,
    digest: str,
    bundle: bytes = b"",
) -> bytes:
    if (
        action not in {"install", "remove"}
        or type(digest) is not str
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or type(bundle) is not bytes
        or (action == "install" and not 0 < len(bundle) <= MAX_ENDPOINT_BUNDLE_BYTES)
        or (action == "remove" and bundle)
        or (action == "install" and hashlib.sha256(bundle).hexdigest() != digest)
    ):
        _fail()
    header = {
        "version": 1,
        "action": action,
        "sha256": digest,
        "size": len(bundle),
    }
    return json.dumps(
        header,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n" + bundle


def _encode_candidate_admin_receipt(state: str, digest: str) -> bytes:
    value = {"version": 1, "state": state, "sha256": digest}
    return json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"


def _parse_candidate_admin_receipt(
    raw: bytes,
    *,
    action: str,
    digest: str,
) -> str:
    try:
        if len(raw) > 256 or not raw.endswith(b"\n"):
            _fail(mutation_uncertain=True)
        value = _json_object(raw[:-1])
        states = {
            "install": {"installed", "already_present"},
            "remove": {"removed", "absent"},
        }
        if (
            type(value) is not dict
            or set(value) != {"version", "state", "sha256"}
            or value.get("version") != 1
            or type(value.get("version")) is not int
            or value.get("state") not in states[action]
            or value.get("sha256") != digest
            or _encode_candidate_admin_receipt(value["state"], digest) != raw
        ):
            _fail(mutation_uncertain=True)
        return value["state"]
    except EndpointInstallerDeploymentError:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, KeyError):
        _fail(mutation_uncertain=True)


async def _execute_candidate_admin_over_ssh(
    snapshot: VpnNodeTransportSnapshot,
    request: bytes,
    *,
    action: str,
    digest: str,
    connector: Callable[..., object] = asyncssh.connect,
) -> str:
    attempted = False
    try:
        options = _connection_options(snapshot)
        async with asyncio.timeout(CONNECT_TIMEOUT + LOGIN_TIMEOUT):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        async with asyncio.timeout(ENDPOINT_OPERATION_TIMEOUT):
            async with connection:
                process = await connection.create_process(
                    FIXED_ENDPOINT_CANDIDATE_ADMIN_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                attempted = True
                process.stdin.write(request)
                await process.stdin.drain()
                process.stdin.write_eof()
                stdout, stderr = await _read_process_output(process)
                await process.wait()
                if process.exit_status != 0 or stderr:
                    _fail(mutation_uncertain=True)
                return _parse_candidate_admin_receipt(
                    stdout,
                    action=action,
                    digest=digest,
                )
    except asyncio.CancelledError:
        raise
    except EndpointInstallerDeploymentError:
        raise
    except Exception:
        _fail(mutation_uncertain=attempted)


async def install_endpoint_installer_over_ssh(
    snapshot: VpnNodeTransportSnapshot,
    bundle: bytes,
    *,
    expected_sha256: str,
    connector: Callable[..., object] = asyncssh.connect,
) -> str:
    if type(bundle) is not bytes or not 0 < len(bundle) <= MAX_ENDPOINT_BUNDLE_BYTES:
        _fail()
    digest = hashlib.sha256(bundle).hexdigest()
    if expected_sha256 != digest:
        _fail()
    request = _encode_candidate_admin_request("install", digest, bundle)
    await _execute_candidate_admin_over_ssh(
        snapshot,
        request,
        action="install",
        digest=digest,
        connector=connector,
    )
    return digest


async def remove_endpoint_installer_over_ssh(
    snapshot: VpnNodeTransportSnapshot,
    expected_sha256: str,
    *,
    inspect_request: EndpointInstallRequest,
    connector: Callable[..., object] = asyncssh.connect,
) -> EndpointInstallReceipt:
    if (
        not isinstance(inspect_request, EndpointInstallRequest)
        or inspect_request.action != "inspect"
    ):
        _fail()
    request = _encode_candidate_admin_request("remove", expected_sha256)
    observed = await execute_endpoint_installer_over_ssh(
        snapshot,
        inspect_request,
        connector=connector,
    )
    await _execute_candidate_admin_over_ssh(
        snapshot,
        request,
        action="remove",
        digest=expected_sha256,
        connector=connector,
    )
    return observed


def _parse_remote_receipt(
    raw: bytes,
    request: EndpointInstallRequest,
) -> EndpointInstallReceipt:
    try:
        if not raw.endswith(b"\n") or len(raw) > MAX_RECEIPT_BYTES:
            _fail(mutation_uncertain=request.action != "inspect")
        receipt = parse_install_receipt(_json_object(raw[:-1]))
        states = {
            "ensure": {"staged", "already_present"},
            "inspect": {"observed"},
            "remove": {"removed"},
            "add_acceptance_client": {"acceptance_client_present"},
            "remove_acceptance_client": {"acceptance_client_removed"},
        }
        if (
            encode_install_receipt(receipt) != raw
            or receipt.state not in states[request.action]
            or receipt.worker_id != request.worker_id
            or receipt.public_host != request.public_host
            or receipt.server_name != request.server_name
            or receipt.short_id != request.short_id
            or (request.inbound_id is not None and receipt.inbound_id != request.inbound_id)
            or (
                request.receipt_digest is not None
                and receipt.receipt_digest != request.receipt_digest
            )
        ):
            _fail(mutation_uncertain=request.action != "inspect")
        return receipt
    except EndpointInstallerDeploymentError:
        raise
    except (EndpointInstallError, KeyError, TypeError, ValueError):
        _fail(mutation_uncertain=request.action != "inspect")


async def execute_endpoint_installer_over_ssh(
    snapshot: VpnNodeTransportSnapshot,
    request: EndpointInstallRequest,
    *,
    connector: Callable[..., object] = asyncssh.connect,
) -> EndpointInstallReceipt:
    attempted = False
    try:
        raw_request = encode_install_request(request)
        options = _connection_options(snapshot)
        async with asyncio.timeout(CONNECT_TIMEOUT + LOGIN_TIMEOUT):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        async with asyncio.timeout(ENDPOINT_OPERATION_TIMEOUT):
            async with connection:
                process = await connection.create_process(
                    FIXED_ENDPOINT_INSTALLER_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                attempted = True
                process.stdin.write(raw_request)
                await process.stdin.drain()
                process.stdin.write_eof()
                stdout, stderr = await _read_process_output(process)
                await process.wait()
                if process.exit_status != 0 or stderr:
                    _fail(mutation_uncertain=request.action != "inspect")
                return _parse_remote_receipt(stdout, request)
    except asyncio.CancelledError:
        raise
    except EndpointInstallerDeploymentError:
        raise
    except Exception:
        _fail(
            mutation_uncertain=(
                attempted and isinstance(request, EndpointInstallRequest)
                and request.action != "inspect"
            )
        )
