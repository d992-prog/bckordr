"""Journalled local 3x-UI 3.8.5 writes with canonical and Xray postconditions.

Raw inventory stays in this callback. An observed receipt is historical evidence,
not continuing policy authority. Uncertain writes require external reconciliation.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from app.services.vpn_node_journal import (
    NodeOperation,
    NodeOperationReceipt,
    execute_node_operation,
)
from app.services.vpn_node_request import VpnNodeRequest, _integer, node_request_digest
from app.services.vpn_xray_runtime import (
    XrayRuntimeError,
    XrayRuntimeObservation,
    _normalized,
    observe_xray_client,
)
from app.services.vpn_xui_identity import _attachments, inspect_xui_client_identity
from app.services.vpn_xui_node_http import NodePanelSession
from app.services.vpn_xui_node_observation import (
    _covered_rows,
    _local_inbound_ids,
    _runtime,
    _transport,
)


_NUMBERS = {
    "id",
    "keepAlive",
    "limitIp",
    "limitHwid",
    "totalGB",
    "expiryTime",
    "reset",
    "resetDay",
    "resetMax",
    "trafficResetDay",
    "createdAt",
    "updatedAt",
}
_STRINGS = {
    "email",
    "subId",
    "uuid",
    "password",
    "auth",
    "flow",
    "security",
    "privateKey",
    "publicKey",
    "allowedIPs",
    "preSharedKey",
    "forwardedPorts",
    "secret",
    "adTag",
    "group",
    "comment",
    "trafficReset",
}
_CLIENT_FIELDS = _NUMBERS | _STRINGS | {"reverse", "enable", "tgId"}


def _require(condition: object) -> None:
    if not condition:
        raise ValueError("vpn_node_preflight_failed") from None


def _record(value: object, *, listing: bool = False) -> dict:
    _require(isinstance(value, dict))
    extras = {"inboundIds"} if listing else set()
    _require(
        set(value) - {"traffic"} == _CLIENT_FIELDS | extras
        if listing
        else set(value) == _CLIENT_FIELDS
    )
    _require(all(type(value[name]) is str for name in _STRINGS))
    _require(
        all(
            type(value[name]) is int and -(2**63) <= value[name] < 2**63
            for name in _NUMBERS | {"tgId"}
        )
    )
    _require(_integer(value["id"], 1) and type(value["enable"]) is bool)
    _require(value["reverse"] is None or isinstance(value["reverse"], dict))
    _require(re.fullmatch(r"[A-Za-z0-9._@+-]{1,64}", value["email"]))
    if value["uuid"]:
        _require(str(UUID(value["uuid"])) == value["uuid"])
    result = {name: value[name] for name in _CLIENT_FIELDS}
    if listing:
        result["inboundIds"] = list(_attachments(value))
    return result


def _supported(record: dict, request: VpnNodeRequest) -> None:
    _require(all(_integer(record[name]) for name in _NUMBERS))
    _require(
        all(
            record[name] == 0
            for name in ("reset", "resetDay", "resetMax", "limitHwid", "keepAlive")
        )
    )
    _require(record["trafficReset"] == "never" and record["trafficResetDay"] == 1)
    _require(record["reverse"] is None and record["security"] in ("", "auto"))
    _require(
        all(
            record[name] == ""
            for name in (
                "auth",
                "privateKey",
                "publicKey",
                "preSharedKey",
                "secret",
                "adTag",
                "forwardedPorts",
                "allowedIPs",
                "group",
            )
        )
    )
    _require(
        record["flow"] == (request.target.flow or "")
        and record["subId"] == request.sub_id
    )


def _wire(record: dict) -> dict:
    result = {
        name: record[name]
        for name in _CLIENT_FIELDS - {"id", "uuid", "createdAt", "updatedAt"}
    }
    result.update(
        id=record["uuid"],
        created_at=record["createdAt"],
        updated_at=record["updatedAt"],
        allowedIPs=[
            value.strip() for value in record["allowedIPs"].split(",") if value.strip()
        ],
    )
    return result


def _snapshot(
    panel: NodePanelSession, request: VpnNodeRequest, database: Path
) -> tuple[dict[int, dict], dict | None, dict[int, str]]:
    before = _local_inbound_ids(database)
    if _runtime(panel.request("GET", "panel/api/server/status")) != "running":
        raise XrayRuntimeError("vpn_xray_runtime_unavailable") from None
    inbounds = panel.request("GET", "panel/api/inbounds/list")
    clients = panel.request("GET", "panel/api/clients/list")
    rows = _covered_rows(inbounds, before, _local_inbound_ids(database))
    identity = inspect_xui_client_identity(
        panel_version="3.8.5",
        target=request.target,
        client_uuid=request.client_uuid,
        client_email=request.client_email,
        inbound_response=inbounds,
        client_response=clients,
    )
    _require(len(clients["obj"]) <= 10000)
    records, normalized_ids, emails = {}, set(), set()
    by_email = {}
    for value in clients["obj"]:
        record = _record(value, listing=True)
        email = record["email"].lower()
        _require(email not in emails)
        emails.add(email)
        if record["uuid"]:
            normalized = _normalized(UUID(record["uuid"]))
            _require(normalized not in normalized_ids)
            normalized_ids.add(normalized)
            if (
                normalized == _normalized(request.client_uuid)
                or email == request.client_email.lower()
            ):
                _require(
                    record["uuid"] == str(request.client_uuid)
                    and record["email"] == request.client_email
                    and record["inboundIds"] == [request.target.inbound_id]
                )
        else:
            _require(email != request.client_email.lower())
        _require(set(record["inboundIds"]) <= set(before))
        records[record["id"]] = record
        by_email[record["email"]] = record
    # Check all VLESS attachments, including disabled clients, against this same
    # canonical inventory. A split or alias cannot hide on another inbound.
    attached = set()
    for inbound in rows:
        if inbound["protocol"] != "vless":
            continue
        for client in inbound["settings"]["clients"]:
            record = by_email.get(client["email"])
            _require(
                record is not None
                and client["id"] == record["uuid"]
                and client["enable"] is record["enable"]
                and inbound["id"] in record["inboundIds"]
            )
            pair = (record["id"], inbound["id"])
            _require(pair not in attached)
            attached.add(pair)
    vless_ids = {row["id"] for row in rows if row["protocol"] == "vless"}
    _require(
        attached
        == {
            (record["id"], inbound_id)
            for record in records.values()
            for inbound_id in record["inboundIds"]
            if inbound_id in vless_ids
        }
    )
    target_inbound = next(row for row in rows if row["id"] == request.target.inbound_id)
    _require(target_inbound.get("trafficReset") == "never")
    selected = records.get(identity.record_id)
    flow = None
    if selected is not None:
        _supported(selected, request)
        obj = panel.request(
            "GET", "panel/api/clients/get/" + quote(request.client_email, safe="")
        )["obj"]
        _require(
            isinstance(obj, dict)
            and set(obj)
            == {
                "client",
                "inboundIds",
                "externalLinks",
                "usedTraffic",
                "tunnelAllowedIPs",
            }
        )
        _require(
            obj["externalLinks"] in (None, []) and obj["tunnelAllowedIPs"] in (None, {})
        )
        _require(
            _record(obj["client"]) == {name: selected[name] for name in _CLIENT_FIELDS}
        )
        _require(_attachments(obj) == (request.target.inbound_id,))
        flow = next(
            client.get("flow", "")
            for client in target_inbound["settings"]["clients"]
            if client["email"] == request.client_email
        )
        _require(flow == (request.target.flow or ""))
    if request.action == "provision":
        _require(
            _transport(target_inbound, request.target, flow, selected is not None)
            == "matched"
        )
    inbound_policy = {}
    for row in rows:
        # Pinned Inbound.up/down/clientStats are telemetry. Inbound.total is a
        # quota; reset timestamps, transport and all other fields stay compared.
        policy = {
            name: value
            for name, value in row.items()
            if name not in {"up", "down", "clientStats"}
        }
        if row["id"] == request.target.inbound_id:
            policy["settings"] = dict(row["settings"])
            policy["settings"]["clients"] = [
                client
                for client in row["settings"]["clients"]
                if not (
                    client["id"] == str(request.client_uuid)
                    and client["email"] == request.client_email
                )
            ]
        # JSON preserves scalar types (unlike Python's True == 1) and detaches
        # these node-only policy snapshots without exporting their contents.
        inbound_policy[row["id"]] = json.dumps(
            policy,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        )
    return records, selected, inbound_policy


def _counter(database: Path, email: str) -> tuple | None:
    connection = None
    try:
        _require(
            isinstance(database, Path) and database.is_absolute() and database.is_file()
        )
        _require(
            all(
                not path.is_symlink()
                and not getattr(path.lstat(), "st_file_attributes", 0) & 0x400
                for path in (database, *database.parents)
            )
        )
        deadline = time.monotonic() + 2.0
        connection = sqlite3.connect(
            database.as_uri() + "?mode=ro", uri=True, timeout=2
        )
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("PRAGMA query_only=ON")
        _require(
            connection.execute(
                "SELECT type FROM sqlite_master WHERE name='client_traffics' LIMIT 2"
            ).fetchall()
            == [("table",)]
        )
        rows = connection.execute(
            "SELECT id,email,up,down,reset_count FROM client_traffics WHERE email = ? COLLATE NOCASE LIMIT 2",
            (email,),
        ).fetchall()
        _require(time.monotonic() < deadline and len(rows) <= 1)
        if not rows:
            return None
        row = rows[0]
        _require(
            _integer(row[0], 1)
            and row[1] == email
            and all(_integer(value) for value in row[2:])
        )
        return row
    finally:
        if connection is not None:
            connection.close()


def execute_node_client_operation(
    request: VpnNodeRequest,
    *,
    journal_directory: Path,
    database_path: Path,
    panel_factory: Callable[[], AbstractContextManager[NodePanelSession]],
    runtime_reader: Callable[..., XrayRuntimeObservation] = observe_xray_client,
) -> NodeOperationReceipt:
    digest = node_request_digest(request)  # No journal or auth IO before validation.
    operation = NodeOperation(
        request.operation_id,
        request.access_key_id,
        request.generation,
        request.action,
        digest,
    )

    def perform(mark_mutating: Callable[[], None]) -> None:
        with panel_factory() as panel:
            records, current, inbound_policy = _snapshot(panel, request, database_path)
            counter = _counter(database_path, request.client_email)

            def runtime(timeout: float = 8.0) -> XrayRuntimeObservation:
                result = runtime_reader(
                    port=request.target.port,
                    client_uuid=request.client_uuid,
                    client_email=request.client_email,
                    flow=request.target.flow or "",
                    timeout_seconds=timeout,
                )
                _require(
                    isinstance(result, XrayRuntimeObservation)
                    and result.state in ("matched", "not_observed")
                    and _integer(result.process_id, 1)
                    and _integer(result.process_start_ticks, 1)
                )
                return result

            initial = runtime()
            if request.action == "provision":
                _require(
                    request.expires_at_ms == 0
                    or request.expires_at_ms > int(time.time() * 1000)
                )
            else:
                _require(request.allow_shared_restart and current is not None)
            if current is None:
                _require(
                    request.allow_create
                    and counter is None
                    and initial.state == "not_observed"
                )
                _require(
                    all(
                        record["subId"] != request.sub_id for record in records.values()
                    )
                )
                desired = {name: "" for name in _STRINGS}
                desired.update({name: 0 for name in _NUMBERS})
                desired.update(
                    uuid=str(request.client_uuid),
                    email=request.client_email,
                    subId=request.sub_id,
                    flow=request.target.flow or "",
                    reverse=None,
                    tgId=0,
                    enable=True,
                    trafficReset="never",
                    trafficResetDay=1,
                    createdAt=request.created_at_ms,
                    totalGB=request.traffic_limit_bytes,
                    expiryTime=request.expires_at_ms,
                )
                route, body = (
                    "panel/api/clients/add",
                    {
                        "client": _wire(desired),
                        "inboundIds": [request.target.inbound_id],
                    },
                )
            else:
                _require(counter is not None)
                desired = dict(current)
                desired["enable"] = request.action == "provision"
                if request.action == "provision":
                    desired.update(
                        totalGB=request.traffic_limit_bytes,
                        expiryTime=request.expires_at_ms,
                    )
                policy_changed = any(
                    desired[name] != current[name] for name in ("totalGB", "expiryTime")
                )
                if policy_changed:
                    # 3.8.5 fills an empty subId during full update. Such a
                    # migration would silently change an existing subscription.
                    _require(bool(current["subId"]))
                    route = (
                        "panel/api/clients/update/"
                        + quote(request.client_email, safe="")
                        + f"?inboundIds={request.target.inbound_id}"
                    )
                    body = _wire(desired)
                elif desired["enable"] != current["enable"]:
                    route = "panel/api/clients/bulk" + (
                        "Enable" if desired["enable"] else "Disable"
                    )
                    body = {"emails": [request.client_email]}
                else:
                    route, body = None, None
                    if request.action == "provision":
                        # Explicit reconciliation is deliberately separate; a
                        # matching database row alone cannot authorize success.
                        _require(initial.state == "matched")
            if route is not None:
                mark_mutating()
                response = panel.request("POST", route, body=body, mutation=True)
                if "/bulk" in route:
                    obj = response.get("obj")
                    _require(
                        isinstance(obj, dict)
                        and type(obj.get("changed")) is int
                        and obj["changed"] == 1
                        and obj.get("skipped", []) == []
                    )

            def verify() -> None:
                latest, selected, latest_inbound_policy = _snapshot(
                    panel, request, database_path
                )
                _require(selected is not None)
                _require(latest_inbound_policy == inbound_policy)
                ignored = (
                    {"updatedAt"} if current else {"id", "updatedAt", "inboundIds"}
                )
                _require(
                    all(
                        selected[name] == value
                        for name, value in desired.items()
                        if name not in ignored
                    )
                )
                previous_others = {
                    key: value
                    for key, value in records.items()
                    if current is None or key != current["id"]
                }
                latest_others = {
                    key: value for key, value in latest.items() if key != selected["id"]
                }
                _require(previous_others == latest_others)
                updated = _counter(database_path, request.client_email)
                _require(updated is not None)
                if counter is not None:
                    _require(
                        updated[:2] == counter[:2]
                        and updated[2] >= counter[2]
                        and updated[3] >= counter[3]
                        and updated[4] == counter[4]
                    )

            original_generation = (initial.process_id, initial.process_start_ticks)
            # Only readonly observations may repeat. If the original process is
            # still positively observed after this grace period, one restart is
            # authorized. A changed or unavailable process is never a retry cue.
            for forced in (False, True):
                provisioning = request.action == "provision"
                deadline = time.monotonic() + (5.0 if forced or provisioning else 3.0)
                last = None
                while time.monotonic() < deadline:
                    try:
                        observed = runtime(
                            min(8.0, max(0.001, deadline - time.monotonic()))
                        )
                        generation = (observed.process_id, observed.process_start_ticks)
                        ready = (
                            observed.state == "matched"
                            if provisioning
                            else generation != original_generation
                        )
                        if ready:
                            if not provisioning:
                                _require(observed.state == "not_observed")
                            verify()
                            _require(
                                not provisioning
                                or request.expires_at_ms == 0
                                or request.expires_at_ms > int(time.time() * 1000)
                            )
                            return
                        last = observed
                    except XrayRuntimeError as error:
                        _require(error.code == "vpn_xray_runtime_unavailable")
                        last = None
                    time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
                _require(not provisioning and not forced and last is not None)
                verify()
                mark_mutating()
                panel.request(
                    "POST",
                    "panel/api/server/restartXrayService",
                    body={},
                    mutation=True,
                )
            _require(False)

    return execute_node_operation(journal_directory, operation, perform)
