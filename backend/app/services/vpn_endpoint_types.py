"""Dependency-free endpoint values shared with node-local observation code."""

from __future__ import annotations

import hashlib
import json
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


def public_endpoint_fingerprint(target: VpnEndpointTarget) -> str:
    payload = {name: getattr(target, name) for name in _PUBLIC_IDENTITY_FIELDS}
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return hashlib.sha256(canonical).hexdigest()
