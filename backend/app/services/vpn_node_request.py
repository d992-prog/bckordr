"""Immutable, versioned node request; validation and hashing perform no IO."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, fields
from uuid import UUID

from app.services.vpn_endpoint_types import EndpointOperation, VpnEndpointTarget
from app.services.vpn_xui_node_http import _valid_hostname
from app.services.vpn_xui_node_observation import _fingerprint, _public_key, _short_id


class VpnNodeRequestError(ValueError):
    def __init__(self) -> None:
        self.code = "vpn_node_request_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VpnNodeRequest:
    operation_id: UUID
    access_key_id: int
    generation: int
    action: EndpointOperation
    target: VpnEndpointTarget = field(repr=False)
    client_uuid: UUID = field(repr=False)
    client_email: str = field(repr=False)
    sub_id: str = field(repr=False)
    expires_at_ms: int
    traffic_limit_bytes: int
    created_at_ms: int
    allow_create: bool
    allow_shared_restart: bool


def _invalid() -> None:
    error = VpnNodeRequestError()
    try:
        raise error from None
    finally:
        error.__context__ = None


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= 2**63 - 1


def _validate(request: object) -> None:
    if not isinstance(request, VpnNodeRequest):
        _invalid()
    target = request.target
    if not (
        isinstance(request.operation_id, UUID)
        and isinstance(request.client_uuid, UUID)
        and _integer(request.access_key_id, 1)
        and _integer(request.generation, 1)
        and type(request.action) is str
        and request.action in ("provision", "suspend", "revoke")
        and _integer(request.expires_at_ms)
        and _integer(request.traffic_limit_bytes)
        and _integer(request.created_at_ms, 1)
        and type(request.allow_create) is bool
        and type(request.allow_shared_restart) is bool
        and type(request.client_email) is str
        and re.fullmatch(r"[A-Za-z0-9._@+-]{1,64}", request.client_email)
        and request.client_email not in (".", "..")
        and type(request.sub_id) is str
        and re.fullmatch(r"[A-Za-z0-9_-]{0,64}", request.sub_id)
        and isinstance(target, VpnEndpointTarget)
    ):
        _invalid()
    if not (
        all(
            _integer(value, 1)
            for value in (target.endpoint_id, target.worker_id, target.inbound_id)
        )
        and _integer(target.port, 1)
        and target.port <= 65535
        and type(target.public_host) is str
        and _valid_hostname(target.public_host)
        and target.protocol == "vless"
        and target.transport in ("tcp", "raw")
        and target.security in ("none", "reality")
        and all(
            type(value) is str
            for value in (target.protocol, target.transport, target.security)
        )
    ):
        _invalid()
    if target.security == "none":
        if any(
            value not in (None, "")
            for value in (
                target.flow,
                target.server_name,
                target.public_key,
                target.short_id,
                target.fingerprint,
            )
        ):
            _invalid()
    elif not (
        target.flow == "xtls-rprx-vision"
        and _public_key(target.public_key)
        and _short_id(target.short_id)
        and _fingerprint(target.fingerprint)
        and type(target.server_name) is str
        and _valid_hostname(target.server_name)
    ):
        _invalid()
    if request.allow_create and not (
        request.action == "provision"
        and target.security == "reality"
        and request.sub_id
    ):
        _invalid()


def serialize_node_request(request: VpnNodeRequest) -> dict:
    _validate(request)
    value = asdict(request)
    value.update(
        version=1,
        operation_id=str(request.operation_id),
        client_uuid=str(request.client_uuid),
    )
    return value


def parse_node_request(value: object) -> VpnNodeRequest:
    if (
        type(value) is not dict
        or set(value) != {"version", *(item.name for item in fields(VpnNodeRequest))}
        or type(value["version"]) is not int
        or value["version"] != 1
        or type(value["target"]) is not dict
        or set(value["target"]) != {item.name for item in fields(VpnEndpointTarget)}
    ):
        _invalid()
    try:
        copied = {key: item for key, item in value.items() if key != "version"}
        for name in ("operation_id", "client_uuid"):
            if type(copied[name]) is not str:
                _invalid()
            parsed = UUID(copied[name])
            if str(parsed) != copied[name]:
                _invalid()
            copied[name] = parsed
        copied["target"] = VpnEndpointTarget(**copied["target"])
        request = VpnNodeRequest(**copied)
        _validate(request)
        return request
    except (TypeError, ValueError, AttributeError):
        pass
    _invalid()


def node_request_digest(request: VpnNodeRequest) -> str:
    encoded = json.dumps(
        serialize_node_request(request),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
