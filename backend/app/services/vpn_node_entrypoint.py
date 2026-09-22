"""Bounded stdin/stdout boundary for the node-local VPN executor."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import sys
from collections.abc import Callable
from typing import BinaryIO

from app.services.vpn_node_journal import NodeOperationReceipt
from app.services.vpn_node_request import (
    node_request_digest,
    parse_node_request,
)
from app.services.vpn_xui_node_executor import execute_node_client_operation
from app.services.vpn_xui_node_http import NodePanelSession, validate_panel_url


NODE_CONFIG_PATH = Path("/var/lib/veltrix-vpn/control-auth/node.json")
NODE_TOKEN_PATH = Path("/var/lib/veltrix-vpn/control-auth/api-token")
NODE_JOURNAL_DIRECTORY = Path("/var/lib/veltrix-vpn/control-journal")
MAX_REQUEST_BYTES = 128 * 1024
MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
EXIT_FAILURE = 1
EXIT_INVALID_REQUEST = 2
EXIT_INTERRUPTED = 3

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


class NodeEntrypointError(ValueError):
    def __init__(self) -> None:
        self.code = "vpn_node_entrypoint_failed"
        super().__init__(self.code)


def _fail() -> None:
    error = NodeEntrypointError()
    try:
        raise error from None
    finally:
        error.__context__ = None


def _bounded_read(stream: BinaryIO, limit: int) -> bytes:
    result = bytearray()
    try:
        while True:
            chunk = stream.read(min(65536, limit + 1 - len(result)))
            if not isinstance(chunk, bytes):
                _fail()
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > limit:
                _fail()
    except (OSError, ValueError, TypeError):
        _fail()


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail()
        result[key] = value
    return result


def _constant(_value: str) -> None:
    _fail()


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail()
    return parsed


def _json_object(raw: bytes) -> dict[str, object]:
    try:
        value = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
            parse_float=_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        _fail()
    if type(value) is not dict:
        _fail()
    return value


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


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 1024)


def _validate_ancestors(path: Path) -> None:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        _fail()
    try:
        for ancestor in reversed(path.parents):
            info = os.lstat(ancestor)
            if (
                not stat.S_ISDIR(info.st_mode)
                or stat.S_ISLNK(info.st_mode)
                or _reparse(info)
                or info.st_uid != 0
                or info.st_mode & 0o022
            ):
                _fail()
    except OSError:
        _fail()


def _private_regular(info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and not stat.S_ISLNK(info.st_mode)
        and not _reparse(info)
        and info.st_uid == 0
        and stat.S_IMODE(info.st_mode) == 0o600
    )


def _read_private_file(path: Path, *, limit: int) -> bytes:
    _validate_ancestors(path)
    descriptor = None
    try:
        before = os.lstat(path)
        if not _private_regular(before) or before.st_size > limit:
            _fail()
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not _private_regular(opened) or _identity(opened) != _identity(before):
            _fail()
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > limit:
                _fail()
        after = os.fstat(descriptor)
        linked = os.lstat(path)
        if _identity(after) != _identity(opened) or _identity(linked) != _identity(opened):
            _fail()
        return bytes(result)
    except (OSError, ValueError, TypeError):
        _fail()
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail()


def _validate_private_regular_file(path: Path) -> None:
    _validate_ancestors(path)
    try:
        if not _private_regular(os.lstat(path)):
            _fail()
    except OSError:
        _fail()


def _validate_journal(directory: Path) -> None:
    _validate_ancestors(directory)
    try:
        info = os.lstat(directory)
        if (
            not stat.S_ISDIR(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _reparse(info)
            or info.st_uid != 0
            or info.st_mode & 0o077
        ):
            _fail()
        for name in ("gate.sqlite3", "operations.sqlite3"):
            if not _private_regular(os.lstat(directory / name)):
                _fail()
    except OSError:
        _fail()


def _node_config(raw: bytes) -> tuple[str, Path]:
    value = _json_object(raw)
    if set(value) != {"version", "panel_url", "database_path"}:
        _fail()
    if value["version"] != 1 or type(value["version"]) is not int:
        _fail()
    panel_url = value["panel_url"]
    database_value = value["database_path"]
    if type(panel_url) is not str or type(database_value) is not str:
        _fail()
    try:
        validate_panel_url(panel_url)
        database_path = Path(database_value)
    except (ValueError, TypeError, OSError):
        _fail()
    if not database_path.is_absolute() or ".." in database_path.parts:
        _fail()
    return panel_url, database_path


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else -1


def run_node_entrypoint(
    stdin: BinaryIO,
    stdout: BinaryIO,
    _stderr: BinaryIO,
    *,
    config_path: Path = NODE_CONFIG_PATH,
    token_path: Path = NODE_TOKEN_PATH,
    journal_directory: Path = NODE_JOURNAL_DIRECTORY,
    effective_uid: Callable[[], int] = _effective_uid,
    executor: Callable[..., NodeOperationReceipt] = execute_node_client_operation,
) -> int:
    try:
        request = parse_node_request(_json_object(_bounded_read(stdin, MAX_REQUEST_BYTES)))
    except (KeyboardInterrupt, SystemExit):
        return EXIT_INTERRUPTED
    except Exception:
        return EXIT_INVALID_REQUEST
    try:
        if effective_uid() != 0:
            _fail()
        panel_url, database_path = _node_config(
            _read_private_file(config_path, limit=MAX_CONFIG_BYTES)
        )
        _validate_private_regular_file(database_path)
        _validate_journal(journal_directory)
        token = _read_private_file(token_path, limit=MAX_TOKEN_BYTES).decode(
            "ascii", errors="strict"
        )

        def panel_factory() -> NodePanelSession:
            return NodePanelSession(
                panel_url,
                username=None,
                password=None,
                api_token=token,
            )

        receipt = executor(
            request,
            journal_directory=journal_directory,
            database_path=database_path,
            panel_factory=panel_factory,
        )
        if (
            not isinstance(receipt, NodeOperationReceipt)
            or (receipt.state, receipt.error_code) not in _RECEIPTS
        ):
            _fail()
        encoded = (
            json.dumps(
                {
                    "version": 1,
                    "operation_id": str(request.operation_id),
                    "request_digest": node_request_digest(request),
                    "state": receipt.state,
                    "error_code": receipt.error_code,
                },
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("ascii")
            + b"\n"
        )
        if stdout.write(encoded) != len(encoded):
            _fail()
        stdout.flush()
        return 0
    except (KeyboardInterrupt, SystemExit):
        return EXIT_INTERRUPTED
    except Exception:
        return EXIT_FAILURE


def main() -> int:
    return run_node_entrypoint(sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
