"""Dependency-free endpoint values shared with node-local observation code."""

from __future__ import annotations

import base64
import binascii
import hashlib
import ipaddress
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

EndpointOperation = Literal["provision", "suspend", "revoke"]


class VpnEndpointError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class VpnEndpointTarget:
    endpoint_id: int
    worker_id: int
    inbound_id: int
    public_host: str
    port: int
    protocol: str
    transport: str
    security: str
    server_name: str | None
    public_key: str | None
    short_id: str | None
    fingerprint: str | None
    flow: str | None


_PUBLIC_IDENTITY_FIELDS = (
    "inbound_id",
    "public_host",
    "port",
    "protocol",
    "transport",
    "security",
    "server_name",
    "public_key",
    "short_id",
    "fingerprint",
    "flow",
)

_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")


def _positive_integer(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _valid_host(value: object) -> bool:
    if (
        type(value) is not str
        or not value
        or any(
            character.isspace()
            or unicodedata.category(character).startswith("C")
            for character in value
        )
    ):
        return False
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_value) > 253:
        return False
    try:
        ipaddress.ip_address(ascii_value)
    except ValueError:
        labels = ascii_value.removesuffix(".").split(".")
        return bool(labels) and all(_DNS_LABEL.fullmatch(label) for label in labels)
    return True


def _valid_public_key(value: object) -> bool:
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is None:
        return False
    try:
        decoded = base64.urlsafe_b64decode(value + "=")
    except (ValueError, binascii.Error):
        return False
    return (
        len(decoded) == 32
        and base64.urlsafe_b64encode(decoded).decode().rstrip("=") == value
    )


def is_valid_vpn_endpoint_target(target: object) -> bool:
    if not isinstance(target, VpnEndpointTarget):
        return False
    if not (
        all(
            _positive_integer(value)
            for value in (target.endpoint_id, target.worker_id, target.inbound_id)
        )
        and _positive_integer(target.port)
        and target.port <= 65535
        and _valid_host(target.public_host)
        and type(target.protocol) is str
        and target.protocol == "vless"
        and type(target.transport) is str
        and target.transport in {"tcp", "raw"}
        and type(target.security) is str
        and target.security in {"none", "reality"}
    ):
        return False
    identity = (
        target.server_name,
        target.public_key,
        target.short_id,
        target.fingerprint,
        target.flow,
    )
    if target.security == "none":
        return all(value in (None, "") for value in identity)
    return bool(
        target.flow == "xtls-rprx-vision"
        and _valid_host(target.server_name)
        and _valid_public_key(target.public_key)
        and type(target.short_id) is str
        and re.fullmatch(r"(?:[0-9a-f]{2}){1,8}", target.short_id)
        and type(target.fingerprint) is str
        and 1 <= len(target.fingerprint) <= 32
        and all(0x20 <= ord(character) <= 0x7E for character in target.fingerprint)
    )


def public_endpoint_fingerprint(target: VpnEndpointTarget) -> str:
    payload = {name: getattr(target, name) for name in _PUBLIC_IDENTITY_FIELDS}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
