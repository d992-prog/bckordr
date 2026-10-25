"""Bounded dependency-free wire protocol for read-only node usage."""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import asdict, dataclass, field, fields
from time import time_ns
from typing import Literal
from uuid import UUID

from app.services.vpn_xray_runtime import _email, _normalized

VPN_NODE_USAGE_VERSION = 1
# shortcut: usage batches cap at 2048 managed credentials per node; add bounded pagination before raising this limit
MAX_USAGE_IDENTITIES = 2048
MAX_USAGE_SOURCES = 64
MAX_USAGE_REQUEST_BYTES = 512 * 1024
MAX_USAGE_RECEIPT_BYTES = 4 * 1024 * 1024
MAX_CLOCK_SKEW_MS = 30_000
MAX_OBSERVATION_SECONDS = 15.0
MAX_INT64 = 2**63 - 1


class VpnNodeUsageError(ValueError):
    def __init__(self) -> None:
        self.code = "vpn_node_usage_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class UsageIdentity:
    access_key_id: int
    inbound_id: int
    client_uuid: str = field(repr=False)
    client_email: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class UsageRequest:
    version: int
    poll_id: str
    minute_ms: int
    identities: tuple[UsageIdentity, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class KeyUsage:
    access_key_id: int
    traffic_state: Literal["ok", "unavailable"]
    record_id: int | None
    reset_count: int | None
    upload_bytes: int | None
    download_bytes: int | None
    source_state: Literal["ok", "unsupported", "unavailable"]
    sources: tuple[str, ...] = field(repr=False)


@dataclass(frozen=True, slots=True)
class UsageReceipt:
    version: int
    poll_id: str
    minute_ms: int
    observed_ms: int
    counter_epoch: str
    keys: tuple[KeyUsage, ...] = field(repr=False)


def _fail() -> None:
    error = VpnNodeUsageError()
    try:
        raise error from None
    finally:
        error.__context__ = None


def _require(value: object) -> None:
    if not value:
        _fail()


def _integer(value: object, minimum: int = 0) -> bool:
    return type(value) is int and minimum <= value <= MAX_INT64


def _uuid(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False


def _now(now_ms: int | None) -> int:
    value = time_ns() // 1_000_000 if now_ms is None else now_ms
    _require(_integer(value, 1))
    return value


def _validate_request(request: UsageRequest, now_ms: int | None) -> None:
    _require(isinstance(request, UsageRequest))
    _require(type(request.version) is int and request.version == VPN_NODE_USAGE_VERSION)
    _require(_uuid(request.poll_id) and _integer(request.minute_ms, 1))
    _require(
        request.minute_ms % 60_000 == 0
        and abs(_now(now_ms) - request.minute_ms) <= MAX_CLOCK_SKEW_MS
    )
    _require(
        type(request.identities) is tuple
        and 0 < len(request.identities) <= MAX_USAGE_IDENTITIES
    )
    ids, emails, uuids = set(), set(), set()
    for identity in request.identities:
        _require(isinstance(identity, UsageIdentity))
        _require(
            _integer(identity.access_key_id, 1) and _integer(identity.inbound_id, 1)
        )
        _require(_uuid(identity.client_uuid) and _email(identity.client_email))
        normalized = _normalized(UUID(identity.client_uuid))
        lowered = identity.client_email.lower()
        _require(
            identity.access_key_id not in ids
            and normalized not in uuids
            and lowered not in emails
        )
        ids.add(identity.access_key_id)
        uuids.add(normalized)
        emails.add(lowered)


def _source(value: object) -> bool:
    if type(value) is not str or not 1 <= len(value) <= 64 or "%" in value:
        return False
    try:
        raw = value[1:-1] if value.startswith("[") and value.endswith("]") else value
        address = ipaddress.ip_address(raw)
        return raw == value or isinstance(address, ipaddress.IPv6Address)
    except ValueError:
        return False


def _validate_receipt(
    receipt: UsageReceipt, request: UsageRequest, now_ms: int | None
) -> None:
    _validate_request(request, now_ms)
    _require(isinstance(receipt, UsageReceipt))
    _require(type(receipt.version) is int and receipt.version == request.version)
    _require(
        receipt.poll_id == request.poll_id
        and type(receipt.minute_ms) is int
        and receipt.minute_ms == request.minute_ms
    )
    _require(
        _integer(receipt.observed_ms, 1)
        and abs(receipt.observed_ms - _now(now_ms)) <= MAX_CLOCK_SKEW_MS
    )
    _require(abs(receipt.observed_ms - request.minute_ms) <= MAX_CLOCK_SKEW_MS)
    _require(
        type(receipt.counter_epoch) is str
        and re.fullmatch(r"[0-9a-f]{64}", receipt.counter_epoch)
    )
    _require(
        type(receipt.keys) is tuple and len(receipt.keys) == len(request.identities)
    )
    seen = set()
    for key in receipt.keys:
        _require(isinstance(key, KeyUsage) and _integer(key.access_key_id, 1))
        _require(key.access_key_id not in seen)
        seen.add(key.access_key_id)
        _require(
            type(key.traffic_state) is str
            and key.traffic_state in ("ok", "unavailable")
        )
        counters = (
            key.record_id,
            key.reset_count,
            key.upload_bytes,
            key.download_bytes,
        )
        if key.traffic_state == "ok":
            _require(
                _integer(key.record_id, 1)
                and all(_integer(item) for item in counters[1:])
            )
        else:
            _require(all(item is None for item in counters))
        _require(
            type(key.source_state) is str
            and key.source_state in ("ok", "unsupported", "unavailable")
        )
        _require(type(key.sources) is tuple and len(key.sources) <= MAX_USAGE_SOURCES)
        _require(all(_source(value) for value in key.sources))
        _require(len(set(key.sources)) == len(key.sources))
        _require(key.source_state == "ok" or not key.sources)
    _require(seen == {identity.access_key_id for identity in request.identities})


def _encode(value: object, limit: int) -> bytes:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")
    _require(len(raw) <= limit)
    return raw


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        _require(key not in result)
        result[key] = value
    return result


def _reject(_value: str) -> None:
    _fail()


def _decode(raw: bytes, limit: int) -> dict:
    _require(type(raw) is bytes and 0 < len(raw) <= limit)
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_reject
    )
    _require(type(value) is dict and _encode(value, limit) == raw)
    return value


def _exact(value: object, kind: type) -> None:
    _require(type(value) is dict and set(value) == {item.name for item in fields(kind)})


def serialize_node_usage_request(
    request: UsageRequest, now_ms: int | None = None
) -> bytes:
    try:
        _validate_request(request, now_ms)
        return _encode(asdict(request), MAX_USAGE_REQUEST_BYTES)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        pass
    _fail()


def parse_node_usage_request(raw: bytes, now_ms: int | None = None) -> UsageRequest:
    try:
        value = _decode(raw, MAX_USAGE_REQUEST_BYTES)
        _exact(value, UsageRequest)
        _require(
            type(value["identities"]) is list
            and len(value["identities"]) <= MAX_USAGE_IDENTITIES
        )
        for identity in value["identities"]:
            _exact(identity, UsageIdentity)
        value["identities"] = tuple(
            UsageIdentity(**item) for item in value["identities"]
        )
        request = UsageRequest(**value)
        _validate_request(request, now_ms)
        return request
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        pass
    _fail()


def serialize_node_usage_receipt(
    receipt: UsageReceipt, request: UsageRequest, now_ms: int | None = None
) -> bytes:
    try:
        _validate_receipt(receipt, request, now_ms)
        return _encode(asdict(receipt), MAX_USAGE_RECEIPT_BYTES)
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        pass
    _fail()


def parse_node_usage_receipt(
    raw: bytes, request: UsageRequest, now_ms: int | None = None
) -> UsageReceipt:
    try:
        value = _decode(raw, MAX_USAGE_RECEIPT_BYTES)
        _exact(value, UsageReceipt)
        _require(
            type(value["keys"]) is list and len(value["keys"]) <= MAX_USAGE_IDENTITIES
        )
        for key in value["keys"]:
            _exact(key, KeyUsage)
            _require(type(key["sources"]) is list)
            key["sources"] = tuple(key["sources"])
        value["keys"] = tuple(KeyUsage(**key) for key in value["keys"])
        receipt = UsageReceipt(**value)
        _validate_receipt(receipt, request, now_ms)
        return receipt
    except (ValueError, TypeError, AttributeError, RecursionError, OverflowError):
        pass
    _fail()
