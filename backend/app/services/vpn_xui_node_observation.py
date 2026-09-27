"""Read-only, node-local panel observations; never mutation authorization."""

from __future__ import annotations

import base64
import binascii
import os
import re
import sqlite3
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from app.services.vpn_endpoint_types import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_xray_runtime import xray_public_listener_is_bound
from app.services.vpn_xui_identity import inspect_xui_client_identity
from app.services.vpn_xui_node_http import NodePanelSession

_DATABASE_LIMIT = 64 * 1024 * 1024
_DATABASE_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class NodeClientObservation:
    state: Literal["matched", "not_observed"]
    record_id: int | None
    enabled: bool | None
    transport: Literal["matched", "mismatch", "unsupported"]
    runtime: Literal["running", "stop", "error"]


@dataclass(frozen=True, slots=True)
class NodeEndpointObservation:
    state: Literal["matched", "not_observed"]
    transport: Literal["matched", "mismatch", "unsupported"] | None
    runtime: Literal["running", "stop", "error"]
    listening: bool


def xray_endpoint_is_listening(row: dict, target: VpnEndpointTarget) -> bool:
    listen = row.get("listen", "")
    return isinstance(listen, str) and xray_public_listener_is_bound(
        port=target.port,
        listen=listen,
    )


def observe_node_endpoint(
    panel: NodePanelSession,
    *,
    target: VpnEndpointTarget,
    database_path: Path,
    listener_probe: Callable[[dict, VpnEndpointTarget], bool] = (
        xray_endpoint_is_listening
    ),
) -> NodeEndpointObservation:
    """Observe one endpoint without reading or changing client identities."""
    before = _local_inbound_ids(database_path)
    runtime = _runtime(panel.request("GET", "panel/api/server/status"))
    inbounds = panel.request("GET", "panel/api/inbounds/list")
    after = _local_inbound_ids(database_path)
    rows = _covered_rows(inbounds, before, after)
    row = next((item for item in rows if item["id"] == target.inbound_id), None)
    listening = False if row is None else listener_probe(row, target)
    if type(listening) is not bool:
        raise TypeError from None
    return NodeEndpointObservation(
        "matched" if row is not None else "not_observed",
        _transport(row, target, None, False) if row is not None else None,
        runtime,
        listening,
    )


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


def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
    )


def _reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 1024)


def _wal_sidecars_are_safe(path: Path, database: os.stat_result) -> None:
    found = []
    for suffix in ("-wal", "-shm"):
        try:
            found.append(os.lstat(str(path) + suffix))
        except FileNotFoundError:
            pass
    try:
        os.lstat(str(path) + "-journal")
    except FileNotFoundError:
        pass
    else:
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    if len(found) not in (0, 2) or any(
        not stat.S_ISREG(info.st_mode)
        or stat.S_ISLNK(info.st_mode)
        or _reparse(info)
        or info.st_uid != database.st_uid
        or stat.S_IMODE(info.st_mode) != stat.S_IMODE(database.st_mode)
        or not 0 <= info.st_size <= _DATABASE_LIMIT
        for info in found
    ):
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None


def _database_snapshot(path: Path, deadline: float) -> sqlite3.Connection:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    source = None
    snapshot = None
    try:
        for parent in path.parents:
            info = os.lstat(parent)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _reparse(info):
                raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        directory_before = os.lstat(path.parent)
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _reparse(before)
            or not 100 <= before.st_size <= _DATABASE_LIMIT
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        _wal_sidecars_are_safe(path, before)
        if time.monotonic() >= deadline:
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None

        source = sqlite3.connect(
            f"{path.as_uri()}?mode=ro", uri=True, timeout=_DATABASE_TIMEOUT_SECONDS
        )
        source.execute("PRAGMA query_only=ON")
        snapshot = sqlite3.connect(":memory:", timeout=_DATABASE_TIMEOUT_SECONDS)

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() >= deadline:
                raise VpnEndpointError("vpn_xui_inventory_unavailable") from None

        source.backup(snapshot, pages=64, progress=progress, sleep=0.01)
        after = os.lstat(path)
        directory_after = os.lstat(path.parent)
        _wal_sidecars_are_safe(path, after)
        if (
            time.monotonic() >= deadline
            or not _same_file(before, after)
            or not _same_file(directory_before, directory_after)
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        opened = source
        source = None
        opened.close()
        result = snapshot
        snapshot = None
        return result
    except VpnEndpointError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    finally:
        if source is not None:
            try:
                source.close()
            except sqlite3.Error:
                pass
        if snapshot is not None:
            try:
                snapshot.close()
            except sqlite3.Error:
                pass


def _local_inbound_ids(path: Path) -> tuple[int, ...]:
    connection = None
    try:
        deadline = time.monotonic() + _DATABASE_TIMEOUT_SECONDS
        connection = _database_snapshot(path, deadline)
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
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
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
        and settings.get("spiderX", "") in ("", "/")
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
    settings = row.get("settings")
    if (
        row.get("protocol") != target.protocol
        or row.get("listen", "") not in ("", "0.0.0.0", "::", target.public_host)
        or not _positive_int(row.get("port"))
        or not _positive_int(target.port)
        or row["port"] != target.port
        or not 1 <= target.port <= 65535
        or row.get("enable") is not True
        or not isinstance(stream, dict)
        or stream.get("network") not in ("tcp", "raw")
        or stream.get("security") != target.security
        or not _raw_options(stream)
        or not isinstance(settings, dict)
        or settings.get("decryption") != "none"
        or (matched and client_flow != target_flow)
    ):
        return "mismatch"
    if target.security == "reality" and not _reality_matches(stream, target):
        return "mismatch"
    return "matched"
