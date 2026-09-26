"""Strict, single-shot SSH transport for the node control zipapp."""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import math
import os
import re
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import isawaitable
from pathlib import Path
from typing import Literal
from uuid import UUID

import asyncssh

from app.services.vpn_node_health import (
    MAX_HEALTH_PAYLOAD_BYTES,
    VpnNodeHealthReceipt,
    VpnNodeHealthRequest,
    parse_node_health_receipt,
    serialize_node_health_request,
)

FIXED_NODE_COMMAND = "/usr/bin/python3 -I -S /opt/veltrix-vpn/current/vpn-node.pyz"
FIXED_NODE_HEALTH_COMMAND = FIXED_NODE_COMMAND + " --health"
FIXED_NODE_RECEIPT_LOOKUP_COMMAND = FIXED_NODE_COMMAND + " --lookup-receipt"
MAX_KNOWN_HOSTS_BYTES = 64 * 1024
MAX_PRIVATE_KEY_BYTES = 64 * 1024
MAX_STDOUT_BYTES = 64 * 1024
MAX_STDERR_BYTES = 64 * 1024
CONNECT_TIMEOUT = 10.0
LOGIN_TIMEOUT = 10.0
REMOTE_OPERATION_TIMEOUT = 30.0
TRANSPORT_CLEANUP_TIMEOUT = 1.0

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
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_DNS_HOST = re.compile(
    r"(?=.{1,253}\Z)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)"
    r"(?:\.(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?))*\Z"
)
_DOTTED_QUAD = re.compile(r"(?:[0-9]+\.){3}[0-9]+\Z")


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


@dataclass(frozen=True)
class _KnownHostEntry:
    line: str
    key: bytes


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


def _canonical_known_hosts_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return (
            _DOTTED_QUAD.fullmatch(host) is None
            and _DNS_HOST.fullmatch(host) is not None
        )
    return str(address) == host


def _canonical_known_hosts_target(target: str) -> bool:
    if (
        not target
        or any(character in target for character in "*,?!|")
        or target.startswith("@")
        or any(character.isspace() for character in target)
    ):
        return False
    if target.startswith("["):
        match = re.fullmatch(r"\[([^\[\]]+)\]:([1-9][0-9]{0,4})", target)
        if match is None:
            return False
        host, port_text = match.groups()
        port = int(port_text)
        return (
            _canonical_known_hosts_host(host)
            and port != 22
            and port <= 65535
            and port_text == str(port)
        )
    if "[" in target or "]" in target:
        return False
    return _canonical_known_hosts_host(target)


def _known_hosts_target(host: str, port: int) -> str:
    if type(host) is not str or type(port) is not int or not 1 <= port <= 65535:
        _fail()
    target = host if port == 22 else f"[{host}]:{port}"
    if not _canonical_known_hosts_target(target):
        _fail()
    return target


def _parse_literal_known_hosts(raw: bytes) -> dict[str, _KnownHostEntry]:
    try:
        text = raw.decode("ascii", errors="strict")
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            _fail()
        entries: dict[str, _KnownHostEntry] = {}
        for line in lines:
            fields = line.split()
            if len(fields) != 3:
                _fail()
            target, key_type, encoded_key = fields[:3]
            if (
                not _canonical_known_hosts_target(target)
                or key_type != "ssh-ed25519"
                or target in entries
            ):
                _fail()
            decoded = base64.b64decode(encoded_key, validate=True)
            if not decoded:
                _fail()
            asyncssh.import_public_key(f"{key_type} {encoded_key}")
            entries[target] = _KnownHostEntry(line, decoded)
        return entries
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


def _parse_known_hosts(raw: bytes, host: str, port: int):
    entries = _parse_literal_known_hosts(raw)
    entry = entries.get(_known_hosts_target(host, port))
    if entry is None:
        _fail()
    return asyncssh.import_known_hosts(entry.line + "\n")


def validate_known_hosts_file(path: Path, *, owner_uid: int) -> None:
    raw = _read_private_file(
        Path(path),
        limit=MAX_KNOWN_HOSTS_BYTES,
        owner_uid=owner_uid,
    )
    _parse_literal_known_hosts(raw)


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


async def _bounded_read(
    reader, limit: int, phase: Literal["preflight", "mutation"] = "mutation"
) -> bytes:
    result = bytearray()
    while True:
        chunk = await reader.read(min(65536, limit + 1 - len(result)))
        if not isinstance(chunk, bytes):
            _fail(phase)
        if not chunk:
            return bytes(result)
        result.extend(chunk)
        if len(result) > limit:
            _fail(phase)


async def _read_process_output(
    process,
    phase: Literal["preflight", "mutation"] = "mutation",
    *,
    stdout_limit: int = MAX_STDOUT_BYTES,
) -> tuple[bytes, bytes]:
    tasks = (
        asyncio.create_task(_bounded_read(process.stdout, stdout_limit, phase)),
        asyncio.create_task(_bounded_read(process.stderr, MAX_STDERR_BYTES, phase)),
    )
    try:
        stdout, stderr = await asyncio.gather(*tasks)
        return stdout, stderr
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _connection_options(snapshot: VpnNodeTransportSnapshot) -> dict[str, object]:
    password_mode = type(snapshot.password) is str and bool(snapshot.password)
    key_mode = isinstance(snapshot.client_key, asyncssh.SSHKey)
    if (
        type(snapshot.host) is not str
        or not snapshot.host
        or type(snapshot.port) is not int
        or not 1 <= snapshot.port <= 65535
        or snapshot.username != "root"
        or password_mode == key_mode
    ):
        _fail()
    known_hosts = _parse_known_hosts(snapshot.known_hosts, snapshot.host, snapshot.port)
    return {
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
    if type(request) is not bytes or not request:
        _fail()
    options = _connection_options(snapshot)
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
    except Exception:  # noqa: BLE001 - collapse transport details into a safe code
        _fail(phase)


def _consume_task_result(task: asyncio.Task) -> None:
    try:
        task.exception()
    except BaseException:  # noqa: BLE001,S110 - cleanup must never escape
        pass


async def _execute_health_operation(
    connection,
    request_bytes: bytes,
    process_holder: list[object],
) -> VpnNodeHealthReceipt:
    async with connection:
        process = await connection.create_process(
            FIXED_NODE_HEALTH_COMMAND,
            term_type=None,
            encoding=None,
        )
        process_holder.append(process)
        process.stdin.write(request_bytes)
        await process.stdin.drain()
        process.stdin.write_eof()
        stdout, stderr = await _read_process_output(
            process,
            "preflight",
            stdout_limit=MAX_HEALTH_PAYLOAD_BYTES,
        )
        await process.wait()
        if process.exit_status != 0 or stderr:
            _fail()
        return parse_node_health_receipt(stdout)


async def _bounded_health_cleanup(
    connection,
    process,
    operation_task: asyncio.Task | None,
) -> None:
    if process is not None:
        try:
            process.close()
        except Exception:  # noqa: BLE001,S110 - best-effort secret-free cleanup
            pass
    try:
        connection.abort()
    except Exception:  # noqa: BLE001,S110 - best-effort secret-free cleanup
        pass

    tasks: set[asyncio.Task] = set()
    if operation_task is not None:
        tasks.add(operation_task)
        if not operation_task.done():
            operation_task.cancel()
    for owner in (process, connection):
        if owner is None:
            continue
        try:
            waiter = owner.wait_closed()
            if isawaitable(waiter):
                tasks.add(asyncio.ensure_future(waiter))
        except Exception:  # noqa: BLE001,S110 - best-effort secret-free cleanup
            pass

    pending = {task for task in tasks if not task.done()}
    slice_timeout = TRANSPORT_CLEANUP_TIMEOUT / 4
    try:
        for attempt in range(4):
            if not pending:
                break
            if attempt:
                for task in pending:
                    task.cancel()
            done, pending = await asyncio.wait(pending, timeout=slice_timeout)
            for task in done:
                _consume_task_result(task)
    finally:
        for task in tasks:
            if task.done():
                _consume_task_result(task)
            else:
                task.cancel()
                task.add_done_callback(_consume_task_result)


async def _cleanup_health_transport(
    connection,
    process,
    operation_task: asyncio.Task | None,
) -> None:
    cleanup_task = asyncio.create_task(
        _bounded_health_cleanup(connection, process, operation_task)
    )
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        cleanup_task.add_done_callback(_consume_task_result)
        raise
    except Exception:  # noqa: BLE001,S110 - cleanup never changes public failure
        pass


async def execute_vpn_node_health_over_ssh(
    snapshot: VpnNodeTransportSnapshot,
    request: VpnNodeHealthRequest,
    *,
    now_ms: int | None = None,
    connector: Callable[..., object] = asyncssh.connect,
) -> VpnNodeHealthReceipt:
    """Execute one read-only health observation through pinned strict SSH."""
    try:
        request_bytes = serialize_node_health_request(request, now_ms=now_ms)
    except Exception:  # noqa: BLE001 - collapse request details into a safe code
        _fail()
    options = _connection_options(snapshot)
    connection = None
    operation_task = None
    process_holder: list[object] = []
    try:
        async with asyncio.timeout(CONNECT_TIMEOUT + LOGIN_TIMEOUT):
            connection = await connector(
                snapshot.host,
                snapshot.port,
                username=snapshot.username,
                **options,
            )
        operation_task = asyncio.create_task(
            _execute_health_operation(connection, request_bytes, process_holder)
        )
        done, _pending = await asyncio.wait(
            {operation_task}, timeout=REMOTE_OPERATION_TIMEOUT
        )
        if not done:
            await _cleanup_health_transport(
                connection,
                process_holder[0] if process_holder else None,
                operation_task,
            )
            _fail()
        return operation_task.result()
    except asyncio.CancelledError:
        if connection is not None:
            await _cleanup_health_transport(
                connection,
                process_holder[0] if process_holder else None,
                operation_task,
            )
        raise
    except VpnNodeTransportError:
        raise
    except Exception:  # noqa: BLE001 - collapse transport details into a safe code
        if connection is not None:
            await _cleanup_health_transport(
                connection,
                process_holder[0] if process_holder else None,
                operation_task,
            )
        _fail()


def _parse_exact_lookup_receipt(
    raw: bytes, operation_id: UUID, request_digest: str
) -> NodeControlReceipt | None:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=lambda pairs: _lookup_pairs(pairs),
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            parse_float=lambda value: _lookup_float(value),
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        RecursionError,
    ):
        _fail()
    if (
        type(value) is not dict
        or set(value)
        != {
            "version",
            "operation_id",
            "request_digest",
            "found",
            "state",
            "error_code",
        }
        or value["version"] != 1
        or type(value["version"]) is not int
        or value["operation_id"] != str(operation_id)
        or value["request_digest"] != request_digest
        or type(value["found"]) is not bool
        or (value["found"] and (value["state"], value["error_code"]) not in _RECEIPTS)
        or (
            not value["found"]
            and (value["state"] is not None or value["error_code"] is not None)
        )
    ):
        _fail()
    canonical = (
        json.dumps(
            {
                "version": value["version"],
                "operation_id": value["operation_id"],
                "request_digest": value["request_digest"],
                "found": value["found"],
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
        _fail()
    if not value["found"]:
        return None
    return NodeControlReceipt(value["state"], value["error_code"])


def _lookup_pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            raise ValueError from None
        result[key] = value
    return result


def _lookup_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError from None
    return parsed


async def lookup_vpn_node_receipt(
    snapshot: VpnNodeTransportSnapshot,
    *,
    operation_id: UUID,
    request_digest: str,
    connector: Callable[..., object] = asyncssh.connect,
) -> NodeControlReceipt | None:
    """Fetch one exact journal receipt through a fixed read-only node command."""
    if (
        not isinstance(operation_id, UUID)
        or type(request_digest) is not str
        or _DIGEST.fullmatch(request_digest) is None
    ):
        _fail()
    options = _connection_options(snapshot)
    request = (
        json.dumps(
            {
                "version": 1,
                "operation_id": str(operation_id),
                "request_digest": request_digest,
            },
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )
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
                    FIXED_NODE_RECEIPT_LOOKUP_COMMAND,
                    term_type=None,
                    encoding=None,
                )
                process.stdin.write(request)
                await process.stdin.drain()
                process.stdin.write_eof()
                stdout, stderr = await _read_process_output(process, "preflight")
                await process.wait()
                if process.exit_status != 0 or stderr:
                    _fail()
                return _parse_exact_lookup_receipt(stdout, operation_id, request_digest)
    except asyncio.CancelledError:
        raise
    except VpnNodeTransportError:
        raise
    except Exception:  # noqa: BLE001 - collapse transport details into a safe code
        _fail()
