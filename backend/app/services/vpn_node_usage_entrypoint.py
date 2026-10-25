"""Root-only read-only usage observation; no control journal or mutations."""

from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from time import time_ns
from typing import BinaryIO
from uuid import UUID

from app.services import vpn_xray_runtime as runtime
from app.services.vpn_node_entrypoint import (
    EXIT_FAILURE,
    EXIT_INTERRUPTED,
    EXIT_INVALID_REQUEST,
    MAX_CONFIG_BYTES,
    MAX_TOKEN_BYTES,
    NODE_CONFIG_PATH,
    NODE_TOKEN_PATH,
    NodeEntrypointError,
    _bounded_read,
    _effective_uid,
    _node_config,
    _read_private_file,
    _validate_private_regular_file,
)
from app.services.vpn_node_usage import (
    MAX_OBSERVATION_SECONDS,
    MAX_USAGE_REQUEST_BYTES,
    KeyUsage,
    UsageReceipt,
    UsageRequest,
    _fail,
    _integer,
    _require,
    parse_node_usage_request,
    serialize_node_usage_receipt,
    serialize_node_usage_request,
)
from app.services.vpn_xui_identity import (
    _attachments,
    _enabled,
    _inventory_rows,
    _positive_int,
    _uuid,
)
from app.services.vpn_xui_node_http import NodePanelSession
from app.services.vpn_xui_node_observation import (
    _covered_rows,
    _database_snapshot,
    _runtime,
)

_EXECUTABLE = Path("/usr/local/x-ui/bin/xray-linux-amd64")
_CONFIG = Path("/usr/local/x-ui/bin/config.json")
_INVENTORY_LIMIT = 10_000
_PARAMETER_BATCH = 128


def _runtime_identity(deadline: float) -> tuple:
    """Pin the same trusted runtime checks as control, without querying/resetting stats."""
    runtime._require_linux()
    executable = runtime._trusted_file(_EXECUTABLE, deadline)
    config = runtime._trusted_file(_CONFIG, deadline)
    process = runtime._process(_EXECUTABLE, deadline)
    _require(runtime._linked_identity(process / "exe", deadline) == executable[:2])
    ticks = runtime._start_ticks(process, deadline)
    runtime._config_argument(process, _CONFIG, deadline)
    raw = runtime._read(_CONFIG, deadline)
    value = runtime._json_object(raw)
    api = value.get("api")
    _require(isinstance(api, dict) and isinstance(api.get("services"), list))
    _require(
        "HandlerService" in api["services"] and isinstance(value.get("inbounds"), list)
    )
    api_rows = [
        row
        for row in value["inbounds"]
        if isinstance(row, dict) and row.get("tag") == api.get("tag")
    ]
    _require(
        len(api_rows) == 1
        and api_rows[0].get("listen") == "127.0.0.1"
        and runtime._port(api_rows[0].get("port"))
    )
    namespace = runtime._network_namespace(process, deadline)
    listener = runtime._listener(process, api_rows[0]["port"], deadline)
    _require(
        runtime._SUPPORTED_XRAY_VERSION.match(
            runtime._run([str(_EXECUTABLE), "version"], deadline)
        )
    )
    boot = runtime._read(runtime._PROC / "sys/kernel/random/boot_id", deadline).strip()
    _require(
        len(boot) == 36 and str(UUID(boot.decode("ascii"))).encode("ascii") == boot
    )
    epoch = hashlib.sha256(
        boot + b"\0" + process.name.encode("ascii") + b"\0" + str(ticks).encode("ascii")
    ).hexdigest()
    return (
        epoch,
        executable,
        config,
        hashlib.sha256(raw).digest(),
        process,
        ticks,
        namespace,
        listener,
    )


def _managed_inventory(
    inbounds: list[dict], clients: object, request: UsageRequest
) -> set[int]:
    rows = _inventory_rows(clients)
    _require(len(rows) <= _INVENTORY_LIMIT)
    by_email, by_uuid, by_record = (
        defaultdict(list),
        defaultdict(list),
        defaultdict(list),
    )
    for row in rows:
        record = _positive_int(row.get("id"))
        email = row.get("email")
        _require(runtime._email(email))
        uid = None if row.get("uuid") == "" else _uuid(row.get("uuid"))
        item = (record, uid, email, _enabled(row.get("enable")), _attachments(row))
        by_record[record].append(item)
        by_email[email.lower()].append(item)
        if uid is not None:
            by_uuid[runtime._normalized(uid)].append(item)
    embedded_email, embedded_uuid = defaultdict(list), defaultdict(list)
    for inbound in inbounds:
        if inbound.get("protocol") != "vless":
            continue
        _require(
            inbound.get("nodeId") is None and inbound.get("fallbackParent") is None
        )
        settings = inbound.get("settings")
        _require(
            isinstance(settings, dict) and isinstance(settings.get("clients"), list)
        )
        _require(len(settings["clients"]) <= _INVENTORY_LIMIT)
        for client in settings["clients"]:
            _require(isinstance(client, dict) and runtime._email(client.get("email")))
            uid = _uuid(client.get("id"))
            item = (inbound["id"], uid, client["email"], _enabled(client.get("enable")))
            embedded_email[client["email"].lower()].append(item)
            embedded_uuid[runtime._normalized(uid)].append(item)
    matched = set()
    for identity in request.identities:
        uid = UUID(identity.client_uuid)
        normalized, lowered = runtime._normalized(uid), identity.client_email.lower()
        global_rows = by_email[lowered]
        attached = embedded_email[lowered]
        if (
            len(global_rows) != 1
            or global_rows != by_uuid[normalized]
            or len(attached) != 1
            or attached != embedded_uuid[normalized]
        ):
            continue
        record, observed_uuid, email, enabled, attachments = global_rows[0]
        if (
            len(by_record[record]) == 1
            and observed_uuid == uid
            and email == identity.client_email
            and attachments == (identity.inbound_id,)
            and attached[0] == (identity.inbound_id, uid, email, enabled)
        ):
            matched.add(identity.access_key_id)
    return matched


def _counter_rows(
    connection, request: UsageRequest, deadline: float
) -> dict[str, list[tuple]]:
    _require(
        connection.execute(
            "SELECT type FROM sqlite_master WHERE name='client_traffics' LIMIT 2"
        ).fetchall()
        == [("table",)]
    )
    rows = defaultdict(list)
    emails = [identity.client_email for identity in request.identities]
    for offset in range(0, len(emails), _PARAMETER_BATCH):
        batch = emails[offset : offset + _PARAMETER_BATCH]
        placeholders = ",".join("?" for _ in batch)
        selected = connection.execute(
            "SELECT id,email,up,down,reset_count FROM client_traffics WHERE email COLLATE NOCASE IN ("
            + placeholders
            + ") LIMIT 10001",
            batch,
        ).fetchall()
        _require(len(selected) <= _INVENTORY_LIMIT and time.monotonic() < deadline)
        for row in selected:
            _require(type(row[1]) is str)
            rows[row[1].lower()].append(row)
    return rows


def observe_node_usage(
    panel: NodePanelSession,
    *,
    request: UsageRequest,
    database_path: Path,
    now_ms: Callable[[], int] = lambda: time_ns() // 1_000_000,
) -> UsageReceipt:
    connection = None
    try:
        serialize_node_usage_request(request, now_ms=now_ms())
        deadline = time.monotonic() + MAX_OBSERVATION_SECONDS
        before = _runtime_identity(deadline)
        _validate_private_regular_file(database_path)
        connection = _database_snapshot(database_path, deadline)
        connection.execute("PRAGMA query_only=ON")
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        inbound_ids = tuple(
            row[0]
            for row in connection.execute(
                "SELECT id FROM inbounds ORDER BY id LIMIT 10001"
            )
        )
        _require(
            len(inbound_ids) <= _INVENTORY_LIMIT
            and all(_integer(value, 1) for value in inbound_ids)
        )
        _require(len(set(inbound_ids)) == len(inbound_ids))
        _require(_runtime(panel.request("GET", "panel/api/server/status")) == "running")
        inbounds = _covered_rows(
            panel.request("GET", "panel/api/inbounds/list"), inbound_ids, inbound_ids
        )
        matched = _managed_inventory(
            inbounds, panel.request("GET", "panel/api/clients/list"), request
        )
        counters = _counter_rows(connection, request, deadline)
        record_counts = defaultdict(int)
        for values in counters.values():
            for row in values:
                if _integer(row[0], 1):
                    record_counts[row[0]] += 1
        keys = []
        for identity in request.identities:
            rows = counters[identity.client_email.lower()]
            row = rows[0] if len(rows) == 1 else None
            if (
                identity.access_key_id in matched
                and row is not None
                and _integer(row[0], 1)
                and record_counts[row[0]] == 1
                and row[1] == identity.client_email
                and all(_integer(value) for value in row[2:])
            ):
                keys.append(
                    KeyUsage(
                        identity.access_key_id,
                        "ok",
                        row[0],
                        row[4],
                        row[2],
                        row[3],
                        "unsupported",
                        (),
                    )
                )
            else:
                keys.append(
                    KeyUsage(
                        identity.access_key_id,
                        "unavailable",
                        None,
                        None,
                        None,
                        None,
                        "unsupported",
                        (),
                    )
                )
        connection.close()
        connection = None
        _validate_private_regular_file(database_path)
        _require(_runtime_identity(deadline) == before and time.monotonic() < deadline)
        receipt = UsageReceipt(
            1, request.poll_id, request.minute_ms, now_ms(), before[0], tuple(keys)
        )
        serialize_node_usage_receipt(receipt, request, now_ms=now_ms())
        return receipt
    except Exception:  # noqa: BLE001,S110 - discard raw node details at this boundary
        pass
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
    _fail()


def run_node_usage_entrypoint(
    stdin: BinaryIO,
    stdout: BinaryIO,
    _stderr: BinaryIO,
    *,
    config_path: Path = NODE_CONFIG_PATH,
    token_path: Path = NODE_TOKEN_PATH,
    effective_uid: Callable[[], int] = _effective_uid,
    observer: Callable[..., UsageReceipt] = observe_node_usage,
) -> int:
    try:
        request = parse_node_usage_request(
            _bounded_read(stdin, MAX_USAGE_REQUEST_BYTES)
        )
    except BaseException as error:  # noqa: BLE001 - contain process interruption
        return (
            EXIT_INVALID_REQUEST if isinstance(error, Exception) else EXIT_INTERRUPTED
        )
    try:
        _require(effective_uid() == 0)
        panel_url, database_path = _node_config(
            _read_private_file(config_path, limit=MAX_CONFIG_BYTES)
        )
        _validate_private_regular_file(database_path)
        token = _read_private_file(token_path, limit=MAX_TOKEN_BYTES + 1)
        _require(
            token.endswith(b"\n") and b"\n" not in token[:-1] and b"\r" not in token
        )
        with NodePanelSession(
            panel_url, api_token=token[:-1].decode("ascii"), timeout_seconds=3
        ) as panel:
            receipt = observer(panel, request=request, database_path=database_path)
        encoded = serialize_node_usage_receipt(receipt, request)
        if stdout.write(encoded) != len(encoded):
            raise NodeEntrypointError from None
        stdout.flush()
        return 0
    except BaseException as error:  # noqa: BLE001 - contain process interruption
        return EXIT_FAILURE if isinstance(error, Exception) else EXIT_INTERRUPTED


def main() -> int:
    return run_node_usage_entrypoint(
        sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer
    )


if __name__ == "__main__":
    raise SystemExit(main())
