"""Read-only, node-local panel observations; never mutation authorization."""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.services.vpn_endpoint_types import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_xui_identity import inspect_xui_client_identity
from app.services.vpn_xui_node_http import NodePanelSession


@dataclass(frozen=True, slots=True)
class NodeClientObservation:
    state: Literal["matched", "not_observed"]
    record_id: int | None
    enabled: bool | None
    transport: Literal["matched", "mismatch", "unsupported"]
    runtime: Literal["running", "stop", "error"]


def observe_node_client(
    panel: NodePanelSession,
    *,
    target: VpnEndpointTarget,
    client_uuid: UUID,
    client_email: str,
    database_path: Path,
) -> NodeClientObservation:
    """Observe declared identity/transport and cached panel runtime on this node.

    Local ID coverage detects scoped inventories but is not atomic against a
    manual panel operator. ``not_observed`` is not global absence or permission
    to create/revoke. Panel identity omits Xray runtime credential normalization.
    Transport matching compares declared configuration, not cryptographic proof
    that REALITY private/public keys correspond. Cached runtime is not proof of
    application; an end-to-end release gate is still required. No observation
    authorizes mutation or removes the separate readiness/revocation barriers.
    """
    before = _local_inbound_ids(database_path)
    runtime = _runtime(panel.request("GET", "panel/api/server/status"))
    inbounds = panel.request("GET", "panel/api/inbounds/list")
    clients = panel.request("GET", "panel/api/clients/list")
    after = _local_inbound_ids(database_path)
    rows = _covered_rows(inbounds, before, after)
    identity = inspect_xui_client_identity(
        panel_version="3.8.5",
        target=target,
        client_uuid=client_uuid,
        client_email=client_email,
        inbound_response=inbounds,
        client_response=clients,
    )
    row = next(row for row in rows if row["id"] == target.inbound_id)
    flow = None
    if identity.state == "matched":
        flow = next(
            client.get("flow", "")
            for client in row["settings"]["clients"]
            if UUID(client["id"]) == client_uuid and client["email"] == client_email
        )
    transport = _transport(row, target, flow, identity.state == "matched")
    return NodeClientObservation(
        identity.state,
        identity.record_id,
        identity.enabled,
        transport,
        runtime,
    )


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _local_inbound_ids(path: Path) -> tuple[int, ...]:
    connection = None
    try:
        if (
            not isinstance(path, Path)
            or not path.is_absolute()
            or path.is_symlink()
            or not path.is_file()
            or any(parent.is_symlink() for parent in path.parents)
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        deadline = time.monotonic() + 2.0
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=2)
        connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
        connection.execute("PRAGMA query_only=ON")
        values = tuple(
            row[0]
            for row in connection.execute(
                "SELECT id FROM inbounds ORDER BY id LIMIT 10001"
            )
        )
        if (
            time.monotonic() >= deadline
            or len(values) > 10000
            or any(not _positive_int(value) for value in values)
            or len(set(values)) != len(values)
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        return values
    except (OSError, ValueError, sqlite3.Error):
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                raise VpnEndpointError("vpn_xui_inventory_unavailable") from None


def _runtime(response: object) -> Literal["running", "stop", "error"]:
    if not isinstance(response, dict) or response.get("success") is not True:
        raise VpnEndpointError("vpn_xui_status_invalid") from None
    obj = response.get("obj")
    if not isinstance(obj, dict):
        raise VpnEndpointError("vpn_xui_status_invalid") from None
    if obj.get("panelVersion") != "3.8.5":
        raise VpnEndpointError("vpn_xui_version_unsupported") from None
    xray = obj.get("xray")
    if (
        not isinstance(xray, dict)
        or xray.get("state") not in ("running", "stop", "error")
        or not isinstance(xray.get("errorMsg"), str)
    ):
        raise VpnEndpointError("vpn_xui_status_invalid") from None
    if xray["state"] == "running" and xray["errorMsg"]:
        return "error"
    return xray["state"]


def _covered_rows(
    response: object, before: tuple[int, ...], after: tuple[int, ...]
) -> list[dict]:
    if (
        not isinstance(response, dict)
        or response.get("success") is not True
        or response.get("nodePending", False) is not False
        or not isinstance(response.get("obj"), list)
    ):
        raise VpnEndpointError("vpn_xui_inventory_incomplete") from None
    rows = response["obj"]
    if any(
        not isinstance(row, dict) or not _positive_int(row.get("id")) for row in rows
    ):
        raise VpnEndpointError("vpn_xui_inventory_incomplete") from None
    ids = tuple(sorted(row["id"] for row in rows))
    if len(set(ids)) != len(ids) or before != ids or after != ids:
        raise VpnEndpointError("vpn_xui_inventory_incomplete") from None
    return rows


def _public_key(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        return False
    return (
        len(decoded) == 32
        and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value
    )


def _short_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"(?:[0-9a-f]{2}){1,8}", value) is not None
    )


def _fingerprint(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 32
        and all(0x20 <= ord(character) <= 0x7E for character in value)
    )


def _raw_options(stream: dict) -> bool:
    for key in ("tcpSettings", "rawSettings"):
        if key not in stream:
            continue
        options = stream[key]
        if not isinstance(options, dict):
            return False
        if options.get("acceptProxyProtocol", False) is not False:
            return False
        if "header" in options:
            header = options["header"]
            if not isinstance(header, dict) or header.get("type", "none") != "none":
                return False
    sockopt = stream.get("sockopt", {})
    return (
        isinstance(sockopt, dict) and sockopt.get("acceptProxyProtocol", False) is False
    )


def _reality_matches(stream: dict, target: VpnEndpointTarget) -> bool:
    reality = stream.get("realitySettings")
    if not isinstance(reality, dict) or not isinstance(reality.get("settings"), dict):
        return False
    settings = reality["settings"]
    names, ids = reality.get("serverNames"), reality.get("shortIds")
    return (
        isinstance(names, list)
        and bool(names)
        and all(isinstance(name, str) and bool(name) for name in names)
        and target.server_name in names
        and isinstance(ids, list)
        and bool(ids)
        and all(_short_id(value) for value in ids)
        and _short_id(target.short_id)
        and target.short_id in ids
        and _public_key(target.public_key)
        and settings.get("publicKey") == target.public_key
        and _fingerprint(target.fingerprint)
        and settings.get("fingerprint") == target.fingerprint
        and isinstance(reality.get("privateKey"), str)
        and bool(reality["privateKey"])
        and settings.get("serverName", "") in ("", target.server_name)
        and settings.get("spiderX", "") == ""
        and settings.get("mldsa65Verify", "") == ""
        and reality.get("spiderX", "") == ""
        and reality.get("mldsa65Verify", "") == ""
    )


def _transport(
    row: dict,
    target: VpnEndpointTarget,
    client_flow: object,
    matched: bool,
) -> Literal["matched", "mismatch", "unsupported"]:
    target_flow = "" if target.flow is None else target.flow
    if (
        target.transport not in ("tcp", "raw")
        or target.security not in ("none", "reality")
        or (target.security == "none" and target_flow != "")
        or (target.security == "reality" and target_flow != "xtls-rprx-vision")
    ):
        return "unsupported"
    stream = row.get("streamSettings")
    if (
        not _positive_int(row.get("port"))
        or not _positive_int(target.port)
        or row["port"] != target.port
        or not 1 <= target.port <= 65535
        or row.get("enable") is not True
        or not isinstance(stream, dict)
        or stream.get("network") not in ("tcp", "raw")
        or stream.get("security") != target.security
        or not _raw_options(stream)
        or row["settings"].get("decryption") != "none"
        or (matched and client_flow != target_flow)
    ):
        return "mismatch"
    if target.security == "reality" and not _reality_matches(stream, target):
        return "mismatch"
    return "matched"
