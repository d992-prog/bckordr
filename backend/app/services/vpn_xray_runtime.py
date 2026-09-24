"""Node-only Xray 26.9.9 observations; no authorization or transport proof.

The API enumerates its email map, not anonymous dynamic users. Neither a match
nor ``not_observed`` proves global inventory coverage, applied transport, or
readiness/revocation. Raw configuration and accounts must stay on this node.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID


_INVALID = "vpn_xray_runtime_invalid"
_UNAVAILABLE = "vpn_xray_runtime_unavailable"
_CONFLICT = "vpn_xray_runtime_conflict"
_LIMIT = 2 * 1024 * 1024
_PROC = Path("/proc")


class XrayRuntimeError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code if code in (_INVALID, _UNAVAILABLE, _CONFLICT) else _INVALID
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class XrayRuntimeObservation:
    state: Literal["matched", "not_observed"]
    process_id: int
    process_start_ticks: int


def _require(condition: object, code: str = _UNAVAILABLE) -> None:
    if not condition:
        _fail(code)


def _fail(code: str) -> None:
    error = XrayRuntimeError(code)
    try:
        raise error from None
    finally:
        # from None hides context; clearing it also removes a caller's secrets.
        error.__context__ = None


def _port(value: object) -> bool:
    return type(value) is int and 1 <= value <= 65535


def _email(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9._@+-]{1,64}", value) is not None


def observe_xray_client(
    *, port: int, client_uuid: UUID, client_email: str, flow: str,
    executable_path: Path = Path("/usr/local/x-ui/bin/xray-linux-amd64"),
    config_path: Path = Path("/usr/local/x-ui/bin/config.json"),
    timeout_seconds: float = 8.0,
) -> XrayRuntimeObservation:
    """Observe one identity, failing closed on ambiguity, unsafe files or drift."""
    _require(
        _port(port) and isinstance(client_uuid, UUID) and _email(client_email)
        and isinstance(flow, str) and flow in ("", "xtls-rprx-vision")
        and type(timeout_seconds) in (int, float)
        and 0 < timeout_seconds <= 30 and math.isfinite(timeout_seconds)
        and all(isinstance(p, Path) and p.is_absolute() for p in (executable_path, config_path)),
        _INVALID,
    )
    try:
        return _observe(port, client_uuid, client_email, flow, executable_path, config_path, timeout_seconds)
    except XrayRuntimeError as exc:
        code = exc.code
    except Exception:
        code = _UNAVAILABLE
    _fail(code)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    _require(remaining > 0)
    return remaining


def _require_linux() -> None:
    _require(sys.platform == "linux" and _PROC.is_dir())


def _trusted_file(path: Path, deadline: float) -> tuple:
    # Check every component without resolving symlinks away first.
    _require(".." not in path.parts)
    for component in (*reversed(path.parents), path):
        _remaining(deadline)
        info = os.lstat(component)
        _require(info.st_uid == 0 and not info.st_mode & 0o022)
        _require(not getattr(info, "st_file_attributes", 0) & 1024)
        _require(stat.S_ISREG(info.st_mode) if component == path else stat.S_ISDIR(info.st_mode))
    _remaining(deadline)
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_uid, info.st_gid)


def _read(path: Path, deadline: float) -> bytes:
    _remaining(deadline)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags | getattr(os, "O_BINARY", 0))
    try:
        _require(stat.S_ISREG(os.fstat(descriptor).st_mode))
        result = bytearray()
        while True:
            _remaining(deadline)
            chunk = os.read(descriptor, min(65536, _LIMIT + 1 - len(result)))
            _remaining(deadline)
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            _require(len(result) <= _LIMIT)
    finally:
        os.close(descriptor)


def _readlink(path: Path, deadline: float) -> str:
    _remaining(deadline)
    result = os.readlink(path)
    _remaining(deadline)
    return result


def _entries(path: Path, deadline: float):
    _remaining(deadline)
    with os.scandir(path) as entries:
        for entry in entries:
            _remaining(deadline)
            yield Path(entry.path)


def _process(executable: Path, deadline: float) -> Path:
    matches = []
    for candidate in _entries(_PROC, deadline):
        if not re.fullmatch(r"[0-9]+", candidate.name):
            continue
        try:
            if _readlink(candidate / "exe", deadline) == str(executable):
                matches.append(candidate)
        except (FileNotFoundError, ProcessLookupError):
            continue
    _remaining(deadline)
    _require(len(matches) == 1)
    return matches[0]


def _start_ticks(process: Path, deadline: float) -> int:
    raw = _read(process / "stat", deadline)
    _remaining(deadline)
    fields = raw.rsplit(b")", 1)[1].split()
    _require(len(fields) > 19 and fields[19].isdigit())
    return int(fields[19])


def _config_argument(process: Path, config: Path, deadline: float) -> None:
    raw = _read(process / "cmdline", deadline)
    _remaining(deadline)
    _require(raw.endswith(b"\0"))
    arguments = raw[:-1].decode("utf-8").split("\0")
    selected = []
    for index, argument in enumerate(arguments[1:], 1):
        flag = argument.lstrip("-").split("=", 1)[0]
        _require(flag not in ("confdir", "config-dir"))
        if flag in ("c", "config"):
            _require(argument in ("-c", "-config", "--config") and index + 1 < len(arguments))
            selected.append(arguments[index + 1])
    _require(len(selected) == 1 and selected[0] and not selected[0].startswith("-"))
    path = Path(selected[0])
    if not path.is_absolute():
        cwd = Path(_readlink(process / "cwd", deadline))
        _require(cwd.is_absolute())
        path = cwd / path
    _require(path == config and ".." not in path.parts)


def _listener(process: Path, port: int, deadline: float) -> str:
    raw = _read(process / "net/tcp", deadline)
    _remaining(deadline)
    inodes = set()
    for line in raw.decode("ascii").splitlines()[1:]:
        _remaining(deadline)
        fields = line.split()
        _require(len(fields) >= 10)
        if fields[1] == f"0100007F:{port:04X}" and fields[3] == "0A":
            _require(fields[9].isdigit() and int(fields[9]) > 0)
            inodes.add(fields[9])
    _require(len(inodes) == 1)
    inode = inodes.pop()
    for descriptor in _entries(process / "fd", deadline):
        try:
            if _readlink(descriptor, deadline) == f"socket:[{inode}]":
                return inode
        except FileNotFoundError:
            continue
    _fail(_UNAVAILABLE)


def _network_namespace(process: Path, deadline: float) -> str:
    namespace = _readlink(process / "ns/net", deadline)
    _require(namespace == _readlink(_PROC / "thread-self/ns/net", deadline))
    return namespace


def _run(arguments: list[str], deadline: float) -> bytes:
    """Drain both pipes with fixed limits; own and reap only this CLI child."""
    _remaining(deadline)
    child = subprocess.Popen(arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, shell=False, close_fds=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    outputs = [bytearray(), bytearray()]
    interrupted = False
    try:
        streams = [child.stdout, child.stderr]
        for stream in streams:
            os.set_blocking(stream.fileno(), False)
        ended = set()
        while len(ended) != 2 or child.poll() is None:
            _remaining(deadline)
            for index, stream in enumerate(streams):
                if index in ended:
                    continue
                try:
                    chunk = os.read(stream.fileno(), min(65536, _LIMIT + 1 - len(outputs[index])))
                except BlockingIOError:
                    continue
                if not chunk:
                    ended.add(index)
                outputs[index].extend(chunk)
                _require(len(outputs[index]) <= _LIMIT)
            try:
                child.wait(timeout=min(_remaining(deadline), 0.02))
            except subprocess.TimeoutExpired:
                continue
            if len(ended) != 2:
                time.sleep(min(_remaining(deadline), 0.002))
        _remaining(deadline)
        _require(child.returncode == 0)
        return bytes(outputs[0])
    except (KeyboardInterrupt, SystemExit):
        interrupted = True
        raise
    finally:
        cleanup_interrupt = None
        try:
            if child.poll() is None:
                child.kill()
            while True:
                try:
                    child.wait()
                    break
                except (KeyboardInterrupt, SystemExit) as exc:
                    if cleanup_interrupt is None:
                        cleanup_interrupt = exc
        finally:
            try:
                child.stdout.close()
            finally:
                child.stderr.close()
        if cleanup_interrupt is not None and not interrupted:
            raise cleanup_interrupt


def _observe(port: int, uid: UUID, email: str, flow: str,
             executable: Path, config: Path, timeout: float) -> XrayRuntimeObservation:
    deadline = time.monotonic() + timeout
    _require_linux()
    executable_identity = _trusted_file(executable, deadline)
    config_identity = _trusted_file(config, deadline)
    process = _process(executable, deadline)
    ticks = _start_ticks(process, deadline)
    _config_argument(process, config, deadline)
    raw = _read(config, deadline)
    _remaining(deadline)
    digest = hashlib.sha256(raw).digest()
    api_port, tags, target = _configuration(_json_object(raw), port, uid, email, flow)
    namespace = _network_namespace(process, deadline)
    listener = _listener(process, api_port, deadline)
    version = _run([str(executable), "version"], deadline)
    _require(re.match(rb"Xray 26\.9\.9(?:\s|$)", version))
    matched = False
    for tag in tags:
        seconds = _remaining(deadline)
        raw = _run([str(executable), "api", "inbounduser", f"--server=127.0.0.1:{api_port}",
                    f"--timeout={math.ceil(seconds)}", f"-tag={tag}"], deadline)
        observed = _runtime_users(_json_object(raw), tag, target, uid, email, flow)
        matched = matched or observed
    _require(_process(executable, deadline) == process)
    _require(_start_ticks(process, deadline) == ticks)
    _config_argument(process, config, deadline)
    _require(_trusted_file(executable, deadline) == executable_identity)
    _require(_trusted_file(config, deadline) == config_identity)
    _require(hashlib.sha256(_read(config, deadline)).digest() == digest)
    _require(_network_namespace(process, deadline) == namespace)
    _require(_listener(process, api_port, deadline) == listener)
    _remaining(deadline)
    return XrayRuntimeObservation("matched" if matched else "not_observed", int(process.name), ticks)


def _json_object(raw: bytes) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result)
            result[key] = value
        return result
    def constant(value):
        _fail(_UNAVAILABLE)
    def finite_float(value):
        parsed = float(value)
        _require(math.isfinite(parsed))
        return parsed
    _require(len(raw) <= _LIMIT)
    result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                        parse_constant=constant, parse_float=finite_float)
    _require(isinstance(result, dict))
    return result


def _normalized(value: UUID) -> bytes:
    return value.bytes[:6] + b"\0\0" + value.bytes[8:]


def _clients(rows: object, tag: str, target: str, expected_uuid: UUID,
             expected_email: str, expected_flow: str, *, typed: bool) -> bool:
    _require(isinstance(rows, list))
    ids, emails = set(), set()
    matched = False
    for row in rows:
        _require(isinstance(row, dict) and _email(row.get("email")))
        account = row.get("account") if typed else row
        _require(isinstance(account, dict))
        if typed:
            _require(account.get("_TypedMessage_") == "xray.proxy.vless.Account")
        _require(isinstance(account.get("id"), str))
        uid, email, flow = UUID(account["id"]), row["email"], account.get("flow", "")
        _require(isinstance(flow, str) and flow in ("", "xtls-rprx-vision"))
        normalized, lowered = _normalized(uid), email.lower()
        _require(normalized not in ids and lowered not in emails, _CONFLICT)
        ids.add(normalized)
        emails.add(lowered)
        if normalized == _normalized(expected_uuid) or lowered == expected_email.lower():
            _require(tag == target and uid == expected_uuid and email == expected_email
                     and flow == expected_flow, _CONFLICT)
            matched = True
    return matched


def _configuration(value: dict, port: int, uid: UUID, email: str, flow: str) -> tuple[int, list[str], str]:
    api, inbounds = value.get("api"), value.get("inbounds")
    _require(isinstance(api, dict) and isinstance(inbounds, list))
    _require(isinstance(api.get("services"), list) and "HandlerService" in api["services"])
    seen, api_rows, vless, targets = set(), [], [], []
    for inbound in inbounds:
        _require(isinstance(inbound, dict))
        tag = inbound.get("tag")
        _require(isinstance(tag, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tag))
        _require(tag not in seen)
        seen.add(tag)
        if tag == api.get("tag"):
            api_rows.append(inbound)
        if inbound.get("protocol") == "vless":
            _require(_port(inbound.get("port")))
            vless.append(inbound)
            if inbound["port"] == port:
                targets.append(tag)
    _require(len(api_rows) == 1 and len(targets) == 1)
    api_row, target = api_rows[0], targets[0]
    _require(api_row.get("listen") == "127.0.0.1" and _port(api_row.get("port")))
    for inbound in vless:
        settings = inbound.get("settings")
        _require(isinstance(settings, dict))
        _clients(settings.get("clients"), inbound["tag"], target, uid, email, flow, typed=False)
    return api_row["port"], [row["tag"] for row in vless], target


def _runtime_users(value: dict, tag: str, target: str, uid: UUID, email: str, flow: str) -> bool:
    _require(not value or "users" in value)
    return _clients(value.get("users", []), tag, target, uid, email, flow, typed=True)
