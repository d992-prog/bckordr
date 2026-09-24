"""Dependency-free endpoint values shared with node-local observation code."""

from __future__ import annotations

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
