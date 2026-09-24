"""Strict dependency-free protocol for read-only VPN node health checks."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from time import time_ns
from typing import Literal

from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    is_valid_vpn_endpoint_target,
)

VPN_NODE_HEALTH_VERSION = 1
MAX_HEALTH_PAYLOAD_BYTES = 16 * 1024
MAX_CLOCK_SKEW_MS = 5 * 60 * 1000

HealthState = Literal["healthy", "unhealthy"]
HealthRuntime = Literal["running", "stopped", "error"]


class VpnNodeHealthError(ValueError):
    def __init__(self) -> None:
        self.code = "vpn_node_health_invalid"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VpnNodeHealthRequest:
    worker_id: int
    target: VpnEndpointTarget = field(repr=False)
    checked_at_ms: int


@dataclass(frozen=True, slots=True)
class VpnNodeHealthReceipt:
    state: HealthState
    error_code: str | None
    runtime: HealthRuntime | None


_REQUEST_FIELDS = {
    "version",
    *(item.name for item in fields(VpnNodeHealthRequest)),
}
_TARGET_FIELDS = {item.name for item in fields(VpnEndpointTarget)}
_RECEIPT_FIELDS = {
    "version",
    *(item.name for item in fields(VpnNodeHealthReceipt)),
}
_VALID_RECEIPTS = frozenset(
    {
        ("healthy", None, "running"),
        ("unhealthy", "vpn_node_health_endpoint_missing", "running"),
        ("unhealthy", "vpn_node_health_endpoint_mismatch", "running"),
        ("unhealthy", "vpn_node_health_runtime_unavailable", "error"),
        ("unhealthy", "vpn_node_health_runtime_stopped", "stopped"),
        ("unhealthy", "vpn_node_health_listener_unavailable", "running"),
        ("unhealthy", "vpn_node_health_panel_unavailable", None),
        ("unhealthy", "vpn_node_health_internal", None),
    }
)
VPN_NODE_HEALTH_ERROR_CODES = frozenset(
    error_code for _, error_code, _ in _VALID_RECEIPTS if error_code is not None
)


class _MalformedPayload(ValueError):
    pass


def _invalid() -> None:
    error = VpnNodeHealthError()
    try:
        raise error from None
    finally:
        error.__context__ = None


def _positive_integer(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _MalformedPayload
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise _MalformedPayload


def _decode(raw: bytes) -> dict[str, object]:
    if type(raw) is not bytes or not raw or len(raw) > MAX_HEALTH_PAYLOAD_BYTES:
        _invalid()
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
        if type(value) is not dict or _canonical(value) != raw:
            raise _MalformedPayload
        return value
    except (
        UnicodeError,
        json.JSONDecodeError,
        _MalformedPayload,
        TypeError,
        ValueError,
        RecursionError,
    ):
        pass
    _invalid()


def _resolve_now_ms(now_ms: int | None) -> int:
    resolved = time_ns() // 1_000_000 if now_ms is None else now_ms
    if not _positive_integer(resolved):
        _invalid()
    return resolved


def _validate_request(request: object, now_ms: int | None) -> None:
    if not isinstance(request, VpnNodeHealthRequest):
        _invalid()
    resolved_now_ms = _resolve_now_ms(now_ms)
    if not (
        is_valid_vpn_endpoint_target(request.target)
        and _positive_integer(request.worker_id)
        and request.worker_id == request.target.worker_id
        and _positive_integer(request.checked_at_ms)
        and resolved_now_ms - MAX_CLOCK_SKEW_MS
        <= request.checked_at_ms
        <= resolved_now_ms + MAX_CLOCK_SKEW_MS
    ):
        _invalid()


def _validate_receipt(receipt: object) -> None:
    if not isinstance(receipt, VpnNodeHealthReceipt):
        _invalid()
    if not (
        type(receipt.state) is str
        and (receipt.error_code is None or type(receipt.error_code) is str)
        and (receipt.runtime is None or type(receipt.runtime) is str)
        and (receipt.state, receipt.error_code, receipt.runtime) in _VALID_RECEIPTS
    ):
        _invalid()


def serialize_node_health_request(
    request: VpnNodeHealthRequest,
    now_ms: int | None = None,
) -> bytes:
    _validate_request(request, now_ms)
    return _canonical(
        {
            "version": VPN_NODE_HEALTH_VERSION,
            "worker_id": request.worker_id,
            "target": asdict(request.target),
            "checked_at_ms": request.checked_at_ms,
        }
    )


def parse_node_health_request(
    raw: bytes,
    now_ms: int | None = None,
) -> VpnNodeHealthRequest:
    value = _decode(raw)
    if (
        set(value) != _REQUEST_FIELDS
        or type(value["version"]) is not int
        or value["version"] != VPN_NODE_HEALTH_VERSION
        or type(value["target"]) is not dict
        or set(value["target"]) != _TARGET_FIELDS
    ):
        _invalid()
    try:
        request = VpnNodeHealthRequest(
            worker_id=value["worker_id"],
            target=VpnEndpointTarget(**value["target"]),
            checked_at_ms=value["checked_at_ms"],
        )
    except (TypeError, ValueError, AttributeError):
        pass
    else:
        _validate_request(request, now_ms)
        return request
    _invalid()


def serialize_node_health_receipt(receipt: VpnNodeHealthReceipt) -> bytes:
    _validate_receipt(receipt)
    return _canonical(
        {
            "version": VPN_NODE_HEALTH_VERSION,
            "state": receipt.state,
            "error_code": receipt.error_code,
            "runtime": receipt.runtime,
        }
    )


def parse_node_health_receipt(raw: bytes) -> VpnNodeHealthReceipt:
    value = _decode(raw)
    if (
        set(value) != _RECEIPT_FIELDS
        or type(value["version"]) is not int
        or value["version"] != VPN_NODE_HEALTH_VERSION
    ):
        _invalid()
    try:
        receipt = VpnNodeHealthReceipt(
            state=value["state"],
            error_code=value["error_code"],
            runtime=value["runtime"],
        )
    except (TypeError, ValueError, AttributeError):
        pass
    else:
        _validate_receipt(receipt)
        return receipt
    _invalid()
