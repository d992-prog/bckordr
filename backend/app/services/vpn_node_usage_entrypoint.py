"""Root-only read-only usage observation; no control journal or mutations."""

from __future__ import annotations

import hashlib
import re
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
from app.services.vpn_node_token import parse_node_token_bytes
from app.services.vpn_node_usage import (
    MAX_OBSERVATION_SECONDS,
    MAX_USAGE_REQUEST_BYTES,
    MAX_USAGE_SOURCES,
    KeyUsage,
    UsageReceipt,
    UsageRequest,
    _fail,
    _integer,
    _require,
    _source,
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
_FINAL_RUNTIME_RESERVE_SECONDS = 3.0
# shortcut: source probes cover at most 64 VLESS inbounds; paginate before raising this bound
_SOURCE_TAG_LIMIT = 64


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


def _source_command(command: str, argument: str, port: int, deadline: float) -> dict:
    raw = runtime._run(
        [
            str(_EXECUTABLE),
            "api",
            command,
            f"--server=127.0.0.1:{port}",
            "--timeout=3",
            argument,
        ],
        min(deadline, time.monotonic() + 3),
    )
    return runtime._json_object(raw)


def _source_listener(port: int, deadline: float) -> tuple:
    process = runtime._process(_EXECUTABLE, deadline)
    listeners = []
    for name in ("tcp", "tcp6"):
        try:
            raw = runtime._read(process / f"net/{name}", deadline)
        except FileNotFoundError:
            continue
        for line in raw.decode("ascii").splitlines()[1:]:
            runtime._remaining(deadline)
            fields = line.split()
            _require(len(fields) >= 10)
            address, number = fields[1].split(":")
            if int(number, 16) == port and fields[3] == "0A":
                _require(runtime._proc_address(address).is_loopback)
                _require(fields[9].isdigit() and int(fields[9]) > 0)
                listeners.append((address, fields[9]))
    _require(listeners)
    return tuple(sorted(listeners))


def _source_users(
    inbounds: list[dict], config: dict, port: int, deadline: float
) -> dict:
    tags = {
        row["id"]: row.get("tag") for row in inbounds if row.get("protocol") == "vless"
    }
    _require(
        0 < len(tags) <= _SOURCE_TAG_LIMIT
        and all(
            type(tag) is str and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", tag)
            for tag in tags.values()
        )
        and len(set(tags.values())) == len(tags)
        and {
            row.get("tag")
            for row in config["inbounds"]
            if isinstance(row, dict) and row.get("protocol") == "vless"
        }
        <= set(tags.values())
    )
    users, uuids = {}, set()
    for inbound_id, tag in tags.items():
        value = _source_command("inbounduser", f"-tag={tag}", port, deadline)
        _require(set(value) <= {"users"})
        rows = value.get("users", [])
        _require(type(rows) is list and len(rows) <= _INVENTORY_LIMIT - len(users))
        for row in rows:
            _require(isinstance(row, dict) and runtime._email(row.get("email")))
            account, level = row.get("account"), row.get("level", 0)
            _require(
                isinstance(account, dict)
                and account.get("_TypedMessage_") == "xray.proxy.vless.Account"
                and _integer(level)
                and level <= 2**32 - 1
            )
            uid = _uuid(account.get("id"))
            email, normalized = row["email"], runtime._normalized(uid)
            _require(email.lower() not in users and normalized not in uuids)
            users[email.lower()] = (inbound_id, uid, email, level)
            uuids.add(normalized)
    return users


def _online_sources(value: dict) -> dict:
    _require(set(value) <= {"users"})
    rows = value.get("users", [])
    _require(type(rows) is list and len(rows) <= _INVENTORY_LIMIT)
    result = {}
    for row in rows:
        _require(
            isinstance(row, dict)
            and set(row) <= {"email", "ips"}
            and runtime._email(row.get("email"))
        )
        email, ips = row["email"], row.get("ips", [])
        _require(email.lower() not in result)
        _require(type(ips) is list and len(ips) <= MAX_USAGE_SOURCES)
        sources = []
        for item in ips:
            _require(
                isinstance(item, dict)
                and set(item) <= {"ip", "lastSeen"}
                and _source(item.get("ip"))
            )
            seen = item.get("lastSeen", "0")
            if type(seen) is str:
                _require(re.fullmatch(r"0|[1-9][0-9]{0,18}", seen))
                seen = int(seen)
            _require(_integer(seen) and item["ip"] not in sources)
            sources.append(item["ip"])
        result[email.lower()] = (email, tuple(sources))
    return result


def _source_samples(
    request: UsageRequest,
    matched: set[int],
    inbounds: list[dict],
    config_hash: bytes,
    deadline: float,
) -> dict[int, tuple[str, tuple[str, ...]]]:
    unavailable = {
        identity.access_key_id: ("unavailable", ()) for identity in request.identities
    }
    try:
        raw = runtime._read(_CONFIG, deadline)
        _require(hashlib.sha256(raw).digest() == config_hash)
        config = runtime._json_object(raw)
        api, policy = config.get("api"), config.get("policy")
        if not (
            isinstance(config.get("stats"), dict)
            and isinstance(api, dict)
            and isinstance(api.get("services"), list)
            and "StatsService" in api["services"]
            and isinstance(policy, dict)
            and isinstance(policy.get("levels"), dict)
        ):
            return {key: ("unsupported", ()) for key in unavailable}
        api_rows = [
            row
            for row in config["inbounds"]
            if isinstance(row, dict) and row.get("tag") == api.get("tag")
        ]
        _require(
            len(api_rows) == 1
            and api_rows[0].get("listen") == "127.0.0.1"
            and runtime._port(api_rows[0].get("port"))
        )
        port = api_rows[0]["port"]
        listener = _source_listener(port, deadline)
        users = _source_users(inbounds, config, port, deadline)
        online = _online_sources(
            _source_command("statsonlineiplist", "-all", port, deadline)
        )
        _require(_source_listener(port, deadline) == listener)
        for identity in request.identities:
            user = users.get(identity.client_email.lower())
            if (
                identity.access_key_id not in matched
                or user is None
                or user[:3]
                != (
                    identity.inbound_id,
                    UUID(identity.client_uuid),
                    identity.client_email,
                )
            ):
                continue
            level = policy["levels"].get(str(user[3]))
            if not isinstance(level, dict) or level.get("statsUserOnline") is not True:
                unavailable[identity.access_key_id] = ("unsupported", ())
                continue
            sample = online.get(identity.client_email.lower())
            if sample is None:
                unavailable[identity.access_key_id] = ("ok", ())
            elif sample[0] == identity.client_email:
                unavailable[identity.access_key_id] = ("ok", sample[1])
        return unavailable
    except Exception:  # noqa: BLE001 - source failures never discard measured traffic
        return unavailable


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
        # Optional source probes cannot spend the mandatory final trust-check budget.
        sources = _source_samples(
            request,
            matched,
            inbounds,
            before[3],
            deadline - _FINAL_RUNTIME_RESERVE_SECONDS,
        )
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
                        *sources[identity.access_key_id],
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
                        *sources[identity.access_key_id],
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
        token = parse_node_token_bytes(
            _read_private_file(token_path, limit=MAX_TOKEN_BYTES + 1)
        )
        with NodePanelSession(
            panel_url, api_token=token, timeout_seconds=3
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
