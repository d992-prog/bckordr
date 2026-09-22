"""Strict, single-shot SSH transport for the node control zipapp."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat
from typing import Literal
from uuid import UUID

import asyncssh


FIXED_NODE_COMMAND = "/usr/bin/python3 -I -S /opt/veltrix-vpn/current/vpn-node.pyz"
MAX_KNOWN_HOSTS_BYTES = 64 * 1024
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 64 * 1024
CONNECT_TIMEOUT = 10.0
LOGIN_TIMEOUT = 10.0
REMOTE_OPERATION_TIMEOUT = 30.0

_RECEIPTS = frozenset(
    {
        ("observed", None),
        ("failed", "vpn_node_preflight_failed"),
        ("failed", "vpn_node_interrupted_before_mutation"),
        ("uncertain", "vpn_node_mutation_uncertain"),
        ("stale", "vpn_node_operation_stale"),
        ("blocked", "vpn_node_reconciliation_required"),
        ("blocked", "vpn_node_key_revoked"),
    }
)


class VpnNodeTransportError(RuntimeError):
    """A secret-free classification of a failed one-shot transport."""

    def __init__(self, phase: Literal["preflight", "mutation"]) -> None:
        self.phase = phase
        super().__init__("vpn_node_transport_failed")


@dataclass(frozen=True)
class NodeControlReceipt:
    state: str
    error_code: str | None


@dataclass(frozen=True)
class VpnNodeTransportSnapshot:
    """All connection material detached before the claim transaction commits."""

    host: str
    port: int
    username: str
    known_hosts: bytes = field(repr=False)
    password: str | None = field(default=None, repr=False)
    client_key: object | None = field(default=None, repr=False)


def _fail(phase: Literal["preflight", "mutation"] = "preflight") -> None:
    error = VpnNodeTransportError(phase)
    try:
        raise error from None
    finally:
        error.__context__ = None


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else -1


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 1024)


def _identity(info: os.stat_result) -> tuple[object, ...]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_mode,
        info.st_uid,
    )


def _validate_ancestors(path: Path, owner_uid: int) -> None:
    if not path.is_absolute() or ".." in path.parts:
        _fail()
    try:
        for ancestor in reversed(path.parents):
            info = os.lstat(ancestor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _reparse(info)
                or info.st_uid not in {0, owner_uid}
                or info.st_mode & 0o022
            ):
                _fail()
    except OSError:
        _fail()


def _read_private_file(path: Path, *, limit: int, owner_uid: int) -> bytes:
    _validate_ancestors(path, owner_uid)
    descriptor = None
    try:
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _reparse(before)
            or before.st_uid != owner_uid
            or stat.S_IMODE(before.st_mode) != 0o600
            or not 0 < before.st_size <= limit
        ):
            _fail()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            _fail()
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > limit:
                _fail()
        if _identity(os.fstat(descriptor)) != _identity(opened):
            _fail()
        if _identity(os.lstat(path)) != _identity(opened):
            _fail()
        return bytes(result)
    except (OSError, TypeError, ValueError):
        _fail()
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail()


def _parse_known_hosts(raw: bytes, host: str, port: int):
    try:
        text = raw.decode("ascii", errors="strict")
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) != 1:
            _fail()
        fields = lines[0].split()
        if len(fields) < 3 or fields[0].startswith("@"):
            _fail()
        expected = host if port == 22 else f"[{host}]:{port}"
        hostname, key_type, encoded_key = fields[:3]
        if (
            hostname != expected
            or any(character in hostname for character in "*,?!")
            or hostname.startswith("|")
            or key_type != "ssh-ed25519"
        ):
            _fail()
        decoded = base64.b64decode(encoded_key, validate=True)
        if not decoded:
            _fail()
        return asyncssh.import_known_hosts(text)
    except VpnNodeTransportError:
        raise
    except (
        AttributeError,
        UnicodeDecodeError,
        ValueError,
        TypeError,
        asyncssh.KeyImportError,
    ):
        _fail()


def load_transport_snapshot(worker, known_hosts_path: Path) -> VpnNodeTransportSnapshot:
    """Read and import the one allowed credential while the worker row is locked."""
    host = worker.ssh_host or worker.ip_address
    port = 22 if worker.ssh_port is None else worker.ssh_port
    username = worker.ssh_username
    password = worker.ssh_password
    key_value = worker.ssh_key_path
    password_mode = type(password) is str and bool(password)
    key_mode = type(key_value) is str and bool(key_value)
    if (
        type(host) is not str
        or not host
        or type(port) is not int
        or not 1 <= port <= 65535
        or username != "root"
        or password_mode == key_mode
    ):
        _fail()
    owner_uid = _effective_uid()
    if owner_uid < 0:
        _fail()
    known_hosts_raw = _read_private_file(
        Path(known_hosts_path), limit=MAX_KNOWN_HOSTS_BYTES, owner_uid=owner_uid
    )
    _parse_known_hosts(known_hosts_raw, host, port)
    if password_mode:
        return VpnNodeTransportSnapshot(
            host, port, username, known_hosts_raw, password=password
        )
    assert type(key_value) is str
    key_path = Path(key_value)
    key_bytes = _read_private_file(
        key_path, limit=MAX_PRIVATE_KEY_BYTES, owner_uid=owner_uid
    )
    try:
        client_key = asyncssh.import_private_key(key_bytes)
    except (asyncssh.KeyImportError, ValueError, TypeError):
        _fail()
    return VpnNodeTransportSnapshot(
        host, port, username, known_hosts_raw, client_key=client_key
    )


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail("mutation")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    _fail("mutation")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail("mutation")
    return parsed


def _parse_exact_receipt(
    raw: bytes, operation_id: UUID, request_digest: str
) -> NodeControlReceipt:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except VpnNodeTransportError:
        raise
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        _fail("mutation")
    if (
        type(value) is not dict
        or set(value)
        != {"version", "operation_id", "request_digest", "state", "error_code"}
        or value["version"] != 1
        or type(value["version"]) is not int
        or value["operation_id"] != str(operation_id)
        or value["request_digest"] != request_digest
        or (value["state"], value["error_code"]) not in _RECEIPTS
    ):
        _fail("mutation")
    canonical = (
        json.dumps(
            {
                "version": value["version"],
                "operation_id": value["operation_id"],
                "request_digest": value["request_digest"],
                "state": value["state"],
                "error_code": value["error_code"],
            },
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
    if raw != canonical:
        _fail("mutation")
    return NodeControlReceipt(value["state"], value["error_code"])


async def _bounded_read(reader, limit: int) -> bytes:
    result = bytearray()
    while True:
        chunk = await reader.read(min(65536, limit + 1 - len(result)))
        if not isinstance(chunk, bytes):
            _fail("mutation")
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            _fail("mutation")


async def _read_process_output(process) -> tuple[bytes, bytes]:
    tasks = (
        asyncio.create_task(_bounded_read(process.stdout, MAX_STDOUT_BYTES)),
        asyncio.create_task(_bounded_read(process.stderr, MAX_STDERR_BYTES)),
    )
    try:
        stdout, stderr = await asyncio.gather(*tasks)
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def execute_vpn_node_request(
    snapshot: VpnNodeTransportSnapshot,
    request: bytes,
    *,
    operation_id: UUID,
    request_digest: str,
    phase_observer: Callable[[str, NodeControlReceipt | None], None] | None = None,
    connector: Callable[..., object] = asyncssh.connect,
) -> NodeControlReceipt:
    """Execute exactly once. The caller, not this function, owns durable retry policy."""
    phase: Literal["preflight", "mutation"] = "preflight"
    observer = phase_observer or (lambda _phase, _receipt: None)
    password_mode = type(snapshot.password) is str and bool(snapshot.password)
    key_mode = isinstance(snapshot.client_key, asyncssh.SSHKey)
    if (
        type(snapshot.host) is not str
        or not snapshot.host
        or type(snapshot.port) is not int
        or not 1 <= snapshot.port <= 65535
        or snapshot.username != "root"
        or type(request) is not bytes
        or not request
        or password_mode == key_mode
    ):
        _fail()
    known_hosts = _parse_known_hosts(snapshot.known_hosts, snapshot.host, snapshot.port)
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
        "connect_timeout": CONNECT_TIMEOUT,
        "login_timeout": LOGIN_TIMEOUT,
    }
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT + LOGIN_TIMEOUT):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        async with asyncio.timeout(REMOTE_OPERATION_TIMEOUT):
            async with connection:
                process = await connection.create_process(
                    FIXED_NODE_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                observer("prewrite", None)
                phase = "mutation"
                observer("stdin_write_attempted", None)
                process.stdin.write(request)
                await process.stdin.drain()
                process.stdin.write_eof()
                stdout, stderr = await _read_process_output(process)
                await process.wait()
                if process.exit_status != 0 or stderr:
                    _fail("mutation")
                receipt = _parse_exact_receipt(stdout, operation_id, request_digest)
                observer("receipt_validated", receipt)
                return receipt
    except asyncio.CancelledError:
        raise
    except VpnNodeTransportError:
        raise
    except Exception:
        _fail(phase)
