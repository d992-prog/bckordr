"""One-shot root-only installer for one protected REALITY endpoint."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict, dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, BinaryIO, Literal, Protocol
from urllib.parse import quote, unquote
from uuid import UUID

from app.services.vpn_xray_runtime import _run as _run_xray
from app.services.vpn_xray_runtime import _trusted_file
from app.services.vpn_xui_node_observation import (
    _covered_rows,
    _local_inbound_ids,
    _runtime,
)
from app.services.vpn_xui_node_http import (
    NodePanelError,
    NodePanelSession,
    validate_panel_url,
)


PORT = 443
PROTOCOL = "vless"
TRANSPORT = "raw"
SECURITY = "reality"
FINGERPRINT = "chrome"
FLOW = "xtls-rprx-vision"
CONTROLLED_WORKER_ID = 15
REMARK = "veltrix-protected-reality-v1"
TAG = "veltrix-reality-443"
XRAY_EXECUTABLE = Path("/usr/local/x-ui/bin/xray-linux-amd64")
MAX_REQUEST_BYTES = 128 * 1024
MAX_RECEIPT_BYTES = 2048
MAX_CONFIG_BYTES = 64 * 1024
MAX_TOKEN_BYTES = 4096
POST_MUTATION_SETTLE_SECONDS = 10.0
POST_MUTATION_POLL_SECONDS = 0.05
NODE_CONFIG_PATH = Path("/var/lib/veltrix-vpn/control-auth/node.json")
NODE_TOKEN_PATH = Path("/var/lib/veltrix-vpn/control-auth/api-token")
EXIT_FAILURE = 1
EXIT_INVALID_REQUEST = 2
EXIT_INTERRUPTED = 3

_HEX = re.compile(r"[0-9a-f]+\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_KEY = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class EndpointInstallError(ValueError):
    def __init__(self, code: str = "vpn_endpoint_install_failed") -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    error = EndpointInstallError(code)
    try:
        raise error from None
    finally:
        error.__context__ = None


@dataclass(frozen=True, slots=True)
class EndpointInstallRequest:
    action: Literal[
        "ensure",
        "inspect",
        "remove",
        "add_acceptance_client",
        "remove_acceptance_client",
    ]
    worker_id: int
    public_host: str
    server_name: str
    short_id: str
    inbound_id: int | None = None
    receipt_digest: str | None = None
    acceptance_uuid: UUID | None = None
    acceptance_email: str | None = None


@dataclass(frozen=True, slots=True)
class EndpointInstallReceipt:
    version: int
    state: Literal[
        "staged",
        "already_present",
        "observed",
        "removed",
        "acceptance_client_present",
        "acceptance_client_removed",
    ]
    worker_id: int
    inbound_id: int
    public_host: str
    port: int
    protocol: str
    transport: str
    security: str
    server_name: str
    public_key: str
    short_id: str
    fingerprint: str
    flow: str
    receipt_digest: str


def _pairs(values: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in values:
        if key in result:
            _fail("vpn_endpoint_install_request_invalid")
        result[key] = value
    return result


def _constant(_value: str) -> None:
    _fail("vpn_endpoint_install_request_invalid")


def _float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        _fail("vpn_endpoint_install_request_invalid")
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
        _fail("vpn_endpoint_install_request_invalid")
    if type(value) is not dict:
        _fail("vpn_endpoint_install_request_invalid")
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
        _fail("vpn_endpoint_install_environment_invalid")
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
                _fail("vpn_endpoint_install_environment_invalid")
    except OSError:
        _fail("vpn_endpoint_install_environment_invalid")


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
        if not _private_regular(before) or not 0 < before.st_size <= limit:
            _fail("vpn_endpoint_install_environment_invalid")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if not _private_regular(opened) or _identity(opened) != _identity(before):
            _fail("vpn_endpoint_install_environment_invalid")
        result = bytearray()
        while True:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
            if len(result) > limit:
                _fail("vpn_endpoint_install_environment_invalid")
        if _identity(os.fstat(descriptor)) != _identity(opened):
            _fail("vpn_endpoint_install_environment_invalid")
        if _identity(os.lstat(path)) != _identity(opened):
            _fail("vpn_endpoint_install_environment_invalid")
        return bytes(result)
    except EndpointInstallError:
        raise
    except (OSError, TypeError, ValueError):
        _fail("vpn_endpoint_install_environment_invalid")
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                _fail("vpn_endpoint_install_environment_invalid")


def _validate_private_regular_file(path: Path) -> None:
    _validate_ancestors(path)
    try:
        if not _private_regular(os.lstat(path)):
            _fail("vpn_endpoint_install_environment_invalid")
    except OSError:
        _fail("vpn_endpoint_install_environment_invalid")


def _node_config(raw: bytes) -> tuple[str, Path]:
    value = _json_object(raw)
    if set(value) != {"version", "panel_url", "database_path"}:
        _fail("vpn_endpoint_install_environment_invalid")
    if value["version"] != 1 or type(value["version"]) is not int:
        _fail("vpn_endpoint_install_environment_invalid")
    panel_url, database_value = value["panel_url"], value["database_path"]
    if type(panel_url) is not str or type(database_value) is not str:
        _fail("vpn_endpoint_install_environment_invalid")
    try:
        validate_panel_url(panel_url)
        database_path = Path(database_value)
    except (ValueError, TypeError, OSError):
        _fail("vpn_endpoint_install_environment_invalid")
    if not database_path.is_absolute() or ".." in database_path.parts:
        _fail("vpn_endpoint_install_environment_invalid")
    return panel_url, database_path


def _effective_uid() -> int:
    getter = getattr(os, "geteuid", None)
    return getter() if getter is not None else -1


def _host(value: object) -> str:
    if (
        type(value) is not str
        or not value
        or value != value.lower()
        or value.endswith(".")
        or len(value) > 253
    ):
        _fail("vpn_endpoint_install_request_invalid")
    if any(character.isspace() or ord(character) < 0x21 for character in value):
        _fail("vpn_endpoint_install_request_invalid")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError:
        labels = value.split(".")
        if not labels or any(_DNS_LABEL.fullmatch(label) is None for label in labels):
            _fail("vpn_endpoint_install_request_invalid")
        return value
    if parsed.version == 6:
        _fail("vpn_endpoint_install_request_invalid")
    return value


def _short_id(value: object) -> str:
    if type(value) is not str or not 2 <= len(value) <= 16 or len(value) % 2 or _HEX.fullmatch(value) is None:
        _fail("vpn_endpoint_install_request_invalid")
    return value


def _positive_int(value: object) -> int:
    if type(value) is not int or value <= 0:
        _fail("vpn_endpoint_install_request_invalid")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or _DIGEST.fullmatch(value) is None:
        _fail("vpn_endpoint_install_request_invalid")
    return value


def _public_key(value: object) -> str:
    if type(value) is not str or _KEY.fullmatch(value) is None:
        _fail("vpn_endpoint_install_receipt_invalid")
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        _fail("vpn_endpoint_install_receipt_invalid")
    if len(decoded) != 32 or base64.urlsafe_b64encode(decoded).decode().rstrip("=") != value:
        _fail("vpn_endpoint_install_receipt_invalid")
    return value


def _uuid(value: object) -> UUID:
    if type(value) is not str:
        _fail("vpn_endpoint_install_request_invalid")
    try:
        parsed = UUID(value)
    except (ValueError, TypeError, AttributeError):
        _fail("vpn_endpoint_install_request_invalid")
    if str(parsed) != value or parsed.version != 4:
        _fail("vpn_endpoint_install_request_invalid")
    return parsed


def _email(value: object) -> str:
    if (
        type(value) is not str
        or not 1 <= len(value) <= 128
        or "@" not in value
        or any(ord(character) < 0x21 or ord(character) > 0x7E for character in value)
        or any(character in "/\\?#" for character in value)
    ):
        _fail("vpn_endpoint_install_request_invalid")
    return value


def parse_install_request(value: object) -> EndpointInstallRequest:
    if type(value) is not dict:
        _fail("vpn_endpoint_install_request_invalid")
    action = value.get("action")
    common = {"version", "action", "worker_id", "public_host", "server_name", "short_id"}
    exact_actions = {"remove", "add_acceptance_client", "remove_acceptance_client"}
    client_actions = {"add_acceptance_client", "remove_acceptance_client"}
    expected = common
    if action in exact_actions:
        expected |= {"inbound_id", "receipt_digest"}
    if action in client_actions:
        expected |= {"acceptance_uuid", "acceptance_email"}
    if set(value) != expected or value.get("version") != 1 or type(value.get("version")) is not int:
        _fail("vpn_endpoint_install_request_invalid")
    if action not in ("ensure", "inspect", "remove", "add_acceptance_client", "remove_acceptance_client"):
        _fail("vpn_endpoint_install_request_invalid")
    inbound_id = _positive_int(value["inbound_id"]) if action in exact_actions else None
    receipt_digest = _digest(value["receipt_digest"]) if action in exact_actions else None
    acceptance_uuid = _uuid(value["acceptance_uuid"]) if action in client_actions else None
    acceptance_email = _email(value["acceptance_email"]) if action in client_actions else None
    worker_id = _positive_int(value["worker_id"])
    if worker_id != CONTROLLED_WORKER_ID:
        _fail("vpn_endpoint_install_request_invalid")
    return EndpointInstallRequest(
        action,
        worker_id,
        _host(value["public_host"]),
        _host(value["server_name"]),
        _short_id(value["short_id"]),
        inbound_id,
        receipt_digest,
        acceptance_uuid,
        acceptance_email,
    )


def encode_install_request(request: EndpointInstallRequest) -> bytes:
    if not isinstance(request, EndpointInstallRequest):
        _fail("vpn_endpoint_install_request_invalid")
    value: dict[str, object] = {
        "version": 1,
        "action": request.action,
        "worker_id": request.worker_id,
        "public_host": request.public_host,
        "server_name": request.server_name,
        "short_id": request.short_id,
    }
    if request.inbound_id is not None:
        value["inbound_id"] = request.inbound_id
        value["receipt_digest"] = request.receipt_digest
    if request.acceptance_uuid is not None:
        value["acceptance_uuid"] = str(request.acceptance_uuid)
        value["acceptance_email"] = request.acceptance_email
    parsed = parse_install_request(value)
    if parsed != request:
        _fail("vpn_endpoint_install_request_invalid")
    encoded = json.dumps(
        value,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii") + b"\n"
    if len(encoded) > MAX_REQUEST_BYTES:
        _fail("vpn_endpoint_install_request_invalid")
    return encoded


def _receipt_payload(**values: object) -> dict[str, object]:
    return {
        "version": 1,
        "state": values["state"],
        "worker_id": values["worker_id"],
        "inbound_id": values["inbound_id"],
        "public_host": values["public_host"],
        "port": PORT,
        "protocol": PROTOCOL,
        "transport": TRANSPORT,
        "security": SECURITY,
        "server_name": values["server_name"],
        "public_key": values["public_key"],
        "short_id": values["short_id"],
        "fingerprint": FINGERPRINT,
        "flow": FLOW,
    }


def _payload_digest(payload: dict[str, object]) -> str:
    identity = payload | {}
    identity.pop("state", None)
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def make_endpoint_receipt(
    *,
    state: Literal[
        "staged",
        "already_present",
        "observed",
        "removed",
        "acceptance_client_present",
        "acceptance_client_removed",
    ],
    worker_id: int,
    inbound_id: int,
    public_host: str,
    server_name: str,
    public_key: str,
    short_id: str,
) -> EndpointInstallReceipt:
    if state not in (
        "staged",
        "already_present",
        "observed",
        "removed",
        "acceptance_client_present",
        "acceptance_client_removed",
    ):
        _fail("vpn_endpoint_install_receipt_invalid")
    payload = _receipt_payload(
        state=state,
        worker_id=_positive_int(worker_id),
        inbound_id=_positive_int(inbound_id),
        public_host=_host(public_host),
        server_name=_host(server_name),
        public_key=_public_key(public_key),
        short_id=_short_id(short_id),
    )
    return EndpointInstallReceipt(**payload, receipt_digest=_payload_digest(payload))


def parse_install_receipt(value: object) -> EndpointInstallReceipt:
    if type(value) is not dict or set(value) != set(EndpointInstallReceipt.__dataclass_fields__):
        _fail("vpn_endpoint_install_receipt_invalid")
    try:
        receipt = make_endpoint_receipt(
            state=value["state"],
            worker_id=value["worker_id"],
            inbound_id=value["inbound_id"],
            public_host=value["public_host"],
            server_name=value["server_name"],
            public_key=value["public_key"],
            short_id=value["short_id"],
        )
    except EndpointInstallError:
        _fail("vpn_endpoint_install_receipt_invalid")
    fixed = {
        "version": 1,
        "port": PORT,
        "protocol": PROTOCOL,
        "transport": TRANSPORT,
        "security": SECURITY,
        "fingerprint": FINGERPRINT,
        "flow": FLOW,
        "receipt_digest": receipt.receipt_digest,
    }
    if any(value.get(key) != expected for key, expected in fixed.items()):
        _fail("vpn_endpoint_install_receipt_invalid")
    return receipt


def encode_install_receipt(receipt: EndpointInstallReceipt) -> bytes:
    parsed = parse_install_receipt(asdict(receipt))
    encoded = json.dumps(asdict(parsed), separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii") + b"\n"
    if len(encoded) > MAX_RECEIPT_BYTES:
        _fail("vpn_endpoint_install_receipt_invalid")
    return encoded


class _Panel(Protocol):
    def request(
        self,
        method: str,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        mutation: bool = False,
    ) -> dict[str, Any]: ...


PanelFactory = Callable[[], AbstractContextManager[_Panel]]


class EndpointAdminPanelSession(NodePanelSession):
    """Token-only extension used only by the disposable endpoint candidate."""

    def __init__(self, panel_url: str, *, api_token: str, timeout_seconds: float = 10) -> None:
        super().__init__(
            panel_url,
            username=None,
            password=None,
            api_token=api_token,
            timeout_seconds=timeout_seconds,
        )

    def request(
        self,
        method: str,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        mutation: bool = False,
    ) -> dict[str, Any]:
        if method == "GET":
            return super().request(method, route, body=body, mutation=mutation)
        valid = method == "POST" and mutation is True and isinstance(body, dict)
        if route in {"panel/api/inbounds/add", "panel/api/clients/add"}:
            valid = valid
        elif re.fullmatch(r"panel/api/inbounds/del/[1-9][0-9]*", route):
            valid = valid and body == {}
        elif route.startswith("panel/api/clients/del/"):
            encoded_email = route.removeprefix("panel/api/clients/del/")
            decoded_email = unquote(encoded_email, errors="strict")
            try:
                valid = valid and quote(_email(decoded_email), safe="") == encoded_email and body == {}
            except EndpointInstallError:
                valid = False
        else:
            valid = False
        if (
            not valid
            or not self._entered
            or self._closed
            or not self._token_mode
            or self._api_token is None
        ):
            raise NodePanelError("vpn_xui_request_invalid") from None
        try:
            encoded = json.dumps(
                body,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        except (TypeError, ValueError, OverflowError, RecursionError):
            raise NodePanelError("vpn_xui_request_invalid") from None
        return self._exchange(
            method,
            self._address.base_path + route,
            data=encoded,
            content_type="application/json",
            csrf_token=None,
            mutation=True,
            api_token=self._api_token,
        )


def _inbound_payload(
    *,
    server_name: str,
    short_id: str,
    private_key: str,
    public_key: str,
) -> dict[str, Any]:
    return {
        "up": 0,
        "down": 0,
        "total": 0,
        "remark": REMARK,
        "enable": True,
        "expiryTime": 0,
        "trafficReset": "never",
        "trafficResetDay": 1,
        "lastTrafficResetTime": 0,
        "listen": "",
        "port": PORT,
        "protocol": PROTOCOL,
        "settings": {"clients": [], "decryption": "none", "fallbacks": []},
        "streamSettings": {
            "network": TRANSPORT,
            "security": SECURITY,
            "externalProxy": [],
            "realitySettings": {
                "show": False,
                "xver": 0,
                "target": f"{server_name}:443",
                "serverNames": [server_name],
                "privateKey": private_key,
                "minClientVer": "",
                "maxClientVer": "",
                "maxTimediff": 0,
                "shortIds": [short_id],
                "mldsa65Seed": "",
                "settings": {
                    "publicKey": public_key,
                    "fingerprint": FINGERPRINT,
                    "serverName": "",
                    "spiderX": "/",
                    "mldsa65Verify": "",
                },
            },
        },
        "tag": TAG,
        "sniffing": {
            "enabled": True,
            "destOverride": ["http", "tls", "quic", "fakedns"],
            "metadataOnly": False,
            "routeOnly": False,
        },
        "shareAddrStrategy": "node",
        "shareAddr": "",
        "disableFlow": False,
    }


def _parse_x25519_output(raw: bytes) -> tuple[str, str]:
    try:
        text = raw.decode("ascii", errors="strict")
        match = re.fullmatch(
            r"PrivateKey: ([A-Za-z0-9_-]{43})\n"
            r"Password \(PublicKey\): ([A-Za-z0-9_-]{43})\n"
            r"Hash32: ([A-Za-z0-9_-]{43})\n",
            text,
        )
        if match is None:
            _fail("vpn_endpoint_install_keygen_failed")
        private_key, public_key, hash_value = match.groups()
        _public_key(private_key)
        _public_key(public_key)
        _public_key(hash_value)
        return private_key, public_key
    except EndpointInstallError:
        raise
    except (UnicodeDecodeError, ValueError, TypeError):
        _fail("vpn_endpoint_install_keygen_failed")


def generate_x25519_keypair(
    *,
    executable: Path = XRAY_EXECUTABLE,
    timeout_seconds: float = 5.0,
) -> tuple[str, str]:
    try:
        deadline = time.monotonic() + timeout_seconds
        _trusted_file(executable, deadline)
        return _parse_x25519_output(_run_xray([str(executable), "x25519"], deadline))
    except EndpointInstallError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _fail("vpn_endpoint_install_keygen_failed")


def _response_rows(value: object, code: str) -> list[dict[str, Any]]:
    if (
        not isinstance(value, dict)
        or value.get("success") is not True
        or value.get("nodePending", False) is not False
        or not isinstance(value.get("obj"), list)
        or any(not isinstance(row, dict) for row in value["obj"])
    ):
        _fail(code)
    return value["obj"]


def _client_rows(value: object) -> list[dict[str, Any]]:
    rows = _response_rows(value, "vpn_endpoint_install_inventory_invalid")
    if len(rows) > 10000:
        _fail("vpn_endpoint_install_inventory_invalid")
    seen_ids: set[int] = set()
    seen_uuids: set[str] = set()
    seen_emails: set[str] = set()
    for row in rows:
        record_id = row.get("id")
        inbound_ids = row.get("inboundIds")
        if (
            type(record_id) is not int
            or record_id <= 0
            or record_id in seen_ids
            or type(row.get("uuid")) is not str
            or type(row.get("email")) is not str
            or (row["uuid"] and row["uuid"] in seen_uuids)
            or row["email"] in seen_emails
            or type(row.get("enable")) is not bool
            or (inbound_ids is not None and (
                not isinstance(inbound_ids, list)
                or any(type(item) is not int or item <= 0 for item in inbound_ids)
                or len(set(inbound_ids)) != len(inbound_ids)
            ))
        ):
            _fail("vpn_endpoint_install_inventory_invalid")
        seen_ids.add(record_id)
        if row["uuid"]:
            seen_uuids.add(row["uuid"])
        seen_emails.add(row["email"])
    return rows


def _snapshot(panel: _Panel, database_path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        before = _local_inbound_ids(database_path)
        if _runtime(panel.request("GET", "panel/api/server/status")) != "running":
            _fail("vpn_endpoint_install_runtime_unavailable")
        inbound_response = panel.request("GET", "panel/api/inbounds/list")
        client_response = panel.request("GET", "panel/api/clients/list")
        after = _local_inbound_ids(database_path)
        return _covered_rows(inbound_response, before, after), _client_rows(client_response)
    except EndpointInstallError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _fail("vpn_endpoint_install_inventory_invalid")


def _contains_expected(actual: object, expected: object) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _contains_expected(actual[key], value)
            for key, value in expected.items()
        )
    return actual == expected


def _matching_endpoint(
    rows: list[dict[str, Any]], request: EndpointInstallRequest
) -> tuple[dict[str, Any] | None, str | None]:
    candidates = [row for row in rows if row.get("port") == PORT]
    if not candidates:
        return None, None
    if len(candidates) != 1:
        _fail("vpn_endpoint_install_conflict")
    row = candidates[0]
    settings = row.get("settings")
    stream = row.get("streamSettings")
    reality = stream.get("realitySettings") if isinstance(stream, dict) else None
    client_settings = reality.get("settings") if isinstance(reality, dict) else None
    private_key = reality.get("privateKey") if isinstance(reality, dict) else None
    public_key = client_settings.get("publicKey") if isinstance(client_settings, dict) else None
    expected = _inbound_payload(
        server_name=request.server_name,
        short_id=request.short_id,
        private_key=private_key,
        public_key=public_key,
    )
    for telemetry_key in ("up", "down", "total", "lastTrafficResetTime"):
        expected.pop(telemetry_key)
    expected["settings"].pop("clients")
    exact = (
        type(row.get("id")) is int
        and row["id"] > 0
        and isinstance(settings, dict)
        and isinstance(settings.get("clients"), list)
        and isinstance(private_key, str)
        and isinstance(client_settings, dict)
        and isinstance(public_key, str)
        and _contains_expected(row, expected)
    )
    if not exact:
        _fail("vpn_endpoint_install_conflict")
    try:
        _public_key(private_key)
        return row, _public_key(public_key)
    except EndpointInstallError:
        _fail("vpn_endpoint_install_conflict")


def _endpoint_receipt(
    request: EndpointInstallRequest,
    row: dict[str, Any],
    public_key: str,
    state: str,
) -> EndpointInstallReceipt:
    return make_endpoint_receipt(
        state=state,
        worker_id=request.worker_id,
        inbound_id=row["id"],
        public_host=request.public_host,
        server_name=request.server_name,
        public_key=public_key,
        short_id=request.short_id,
    )


def _attached_clients(
    row: dict[str, Any], clients: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    inbound_id = row["id"]
    return [client for client in clients if inbound_id in (client.get("inboundIds") or [])]


def _require_receipt_identity(
    request: EndpointInstallRequest, row: dict[str, Any], public_key: str
) -> None:
    observed = _endpoint_receipt(request, row, public_key, "observed")
    if request.inbound_id != row["id"] or request.receipt_digest != observed.receipt_digest:
        _fail("vpn_endpoint_install_identity_mismatch")


def _acceptance_matches(
    request: EndpointInstallRequest,
    row: dict[str, Any],
    clients: list[dict[str, Any]],
) -> bool:
    assert request.acceptance_uuid is not None and request.acceptance_email is not None
    embedded = [
        client
        for client in row["settings"]["clients"]
        if isinstance(client, dict)
        and (client.get("id") == str(request.acceptance_uuid) or client.get("email") == request.acceptance_email)
    ]
    global_rows = [
        client
        for client in clients
        if client.get("uuid") == str(request.acceptance_uuid) or client.get("email") == request.acceptance_email
    ]
    return (
        len(embedded) == 1
        and len(global_rows) == 1
        and embedded[0].get("id") == str(request.acceptance_uuid)
        and embedded[0].get("email") == request.acceptance_email
        and embedded[0].get("enable") is True
        and embedded[0].get("flow") == FLOW
        and global_rows[0].get("uuid") == str(request.acceptance_uuid)
        and global_rows[0].get("email") == request.acceptance_email
        and global_rows[0].get("enable") is True
        and global_rows[0].get("inboundIds") == [row["id"]]
    )


def _acceptance_client_payload(request: EndpointInstallRequest) -> dict[str, Any]:
    if request.acceptance_uuid is None or request.acceptance_email is None:
        _fail("vpn_endpoint_install_request_invalid")
    return {
        "keepAlive": 0,
        "limitIp": 0,
        "limitHwid": 0,
        "totalGB": 0,
        "expiryTime": 0,
        "reset": 0,
        "resetDay": 0,
        "resetMax": 0,
        "trafficResetDay": 1,
        "email": request.acceptance_email,
        "subId": "",
        "password": "",
        "auth": "",
        "flow": FLOW,
        "security": "",
        "privateKey": "",
        "publicKey": "",
        "allowedIPs": [],
        "preSharedKey": "",
        "forwardedPorts": "",
        "secret": "",
        "adTag": "",
        "group": "",
        "comment": "",
        "trafficReset": "never",
        "reverse": None,
        "enable": True,
        "tgId": 0,
        "id": str(request.acceptance_uuid),
        "created_at": 0,
        "updated_at": 0,
    }


def _wait_for_acceptance_state(
    panel: _Panel,
    database_path: Path,
    request: EndpointInstallRequest,
    *,
    present: bool,
) -> tuple[dict[str, Any], str]:
    deadline = time.monotonic() + POST_MUTATION_SETTLE_SECONDS
    while True:
        try:
            rows, clients = _snapshot(panel, database_path)
            row, public_key = _matching_endpoint(rows, request)
            if row is not None and public_key is not None:
                if present and _acceptance_matches(request, row, clients):
                    return row, public_key
                if not present and not row["settings"]["clients"] and not _attached_clients(row, clients):
                    return row, public_key
        except EndpointInstallError as error:
            if error.code not in {
                "vpn_endpoint_install_runtime_unavailable",
                "vpn_endpoint_install_inventory_invalid",
            }:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _fail("vpn_endpoint_install_postcondition_failed")
        time.sleep(min(POST_MUTATION_POLL_SECONDS, remaining))


def execute_endpoint_action(
    request: EndpointInstallRequest,
    *,
    database_path: Path,
    panel_factory: PanelFactory,
    key_generator: Callable[[], tuple[str, str]] = generate_x25519_keypair,
) -> EndpointInstallReceipt:
    if not isinstance(request, EndpointInstallRequest) or not isinstance(database_path, Path):
        _fail("vpn_endpoint_install_request_invalid")
    try:
        with panel_factory() as panel:
            rows, clients = _snapshot(panel, database_path)
            row, public_key = _matching_endpoint(rows, request)
            if request.action == "ensure":
                if row is not None:
                    if row["settings"]["clients"] or _attached_clients(row, clients):
                        _fail("vpn_endpoint_install_conflict")
                    return _endpoint_receipt(request, row, public_key, "already_present")
                private_key, generated_public_key = key_generator()
                _public_key(private_key)
                _public_key(generated_public_key)
                panel.request(
                    "POST",
                    "panel/api/inbounds/add",
                    body=_inbound_payload(
                        server_name=request.server_name,
                        short_id=request.short_id,
                        private_key=private_key,
                        public_key=generated_public_key,
                    ),
                    mutation=True,
                )
                rows, clients = _snapshot(panel, database_path)
                row, public_key = _matching_endpoint(rows, request)
                if row is None or row["settings"]["clients"] or _attached_clients(row, clients):
                    _fail("vpn_endpoint_install_postcondition_failed")
                return _endpoint_receipt(request, row, public_key, "staged")
            if row is None or public_key is None:
                _fail("vpn_endpoint_install_missing")
            if request.action == "inspect":
                if row["settings"]["clients"] or _attached_clients(row, clients):
                    _fail("vpn_endpoint_install_clients_present")
                return _endpoint_receipt(request, row, public_key, "observed")
            _require_receipt_identity(request, row, public_key)
            if request.action == "remove":
                if row["settings"]["clients"] or _attached_clients(row, clients):
                    _fail("vpn_endpoint_install_clients_present")
                panel.request("POST", f"panel/api/inbounds/del/{row['id']}", body={}, mutation=True)
                rows, _clients = _snapshot(panel, database_path)
                if any(candidate.get("id") == row["id"] or candidate.get("port") == PORT for candidate in rows):
                    _fail("vpn_endpoint_install_postcondition_failed")
                return _endpoint_receipt(request, row, public_key, "removed")
            assert request.acceptance_uuid is not None and request.acceptance_email is not None
            matches = _acceptance_matches(request, row, clients)
            if request.action == "add_acceptance_client":
                if matches:
                    return _endpoint_receipt(request, row, public_key, "acceptance_client_present")
                if row["settings"]["clients"] or _attached_clients(row, clients):
                    _fail("vpn_endpoint_install_clients_present")
                if any(
                    client.get("uuid") == str(request.acceptance_uuid)
                    or client.get("email") == request.acceptance_email
                    for client in clients
                ):
                    _fail("vpn_endpoint_install_identity_mismatch")
                panel.request(
                    "POST",
                    "panel/api/clients/add",
                    body={
                        "client": _acceptance_client_payload(request),
                        "inboundIds": [row["id"]],
                    },
                    mutation=True,
                )
                row, public_key = _wait_for_acceptance_state(
                    panel, database_path, request, present=True
                )
                return _endpoint_receipt(request, row, public_key, "acceptance_client_present")
            if not matches or len(row["settings"]["clients"]) != 1 or len(_attached_clients(row, clients)) != 1:
                _fail("vpn_endpoint_install_identity_mismatch")
            route_email = quote(request.acceptance_email, safe="")
            panel.request("POST", f"panel/api/clients/del/{route_email}", body={}, mutation=True)
            row, public_key = _wait_for_acceptance_state(
                panel, database_path, request, present=False
            )
            return _endpoint_receipt(request, row, public_key, "acceptance_client_removed")
    except EndpointInstallError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _fail("vpn_endpoint_install_failed")


def _bounded_read(stream: BinaryIO) -> bytes:
    result = bytearray()
    try:
        while True:
            chunk = stream.read(min(65536, MAX_REQUEST_BYTES + 1 - len(result)))
            if not isinstance(chunk, bytes):
                _fail("vpn_endpoint_install_request_invalid")
            if not chunk:
                return bytes(result)
            result.extend(chunk)
            if len(result) > MAX_REQUEST_BYTES:
                _fail("vpn_endpoint_install_request_invalid")
    except (OSError, ValueError, TypeError):
        _fail("vpn_endpoint_install_request_invalid")


def _execute_node_request(
    request: EndpointInstallRequest,
    *,
    config_path: Path = NODE_CONFIG_PATH,
    token_path: Path = NODE_TOKEN_PATH,
    effective_uid: Callable[[], int] = _effective_uid,
) -> EndpointInstallReceipt:
    try:
        if effective_uid() != 0:
            _fail("vpn_endpoint_install_privilege_required")
        panel_url, database_path = _node_config(
            _read_private_file(config_path, limit=MAX_CONFIG_BYTES)
        )
        _validate_private_regular_file(database_path)
        token = _read_private_file(token_path, limit=MAX_TOKEN_BYTES).decode(
            "ascii", errors="strict"
        )

        def panel_factory() -> EndpointAdminPanelSession:
            return EndpointAdminPanelSession(panel_url, api_token=token)

        return execute_endpoint_action(
            request,
            database_path=database_path,
            panel_factory=panel_factory,
        )
    except EndpointInstallError:
        raise
    except BaseException as error:
        if not isinstance(error, Exception):
            raise
        _fail("vpn_endpoint_install_environment_invalid")


def run_endpoint_installer(
    stdin: BinaryIO,
    stdout: BinaryIO,
    _stderr: BinaryIO,
    *,
    executor: Callable[[EndpointInstallRequest], EndpointInstallReceipt] | None = None,
) -> int:
    try:
        request = parse_install_request(_json_object(_bounded_read(stdin)))
    except BaseException as error:
        return EXIT_INVALID_REQUEST if isinstance(error, Exception) else EXIT_INTERRUPTED
    try:
        receipt = _execute_node_request(request) if executor is None else executor(request)
        encoded = encode_install_receipt(receipt)
        if stdout.write(encoded) != len(encoded):
            _fail("vpn_endpoint_install_failed")
        stdout.flush()
        return 0
    except BaseException as error:
        return EXIT_FAILURE if isinstance(error, Exception) else EXIT_INTERRUPTED


def main() -> int:
    return run_endpoint_installer(sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
