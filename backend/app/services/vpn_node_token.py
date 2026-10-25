"""Parse existing node API tokens without changing their stored bytes."""

from __future__ import annotations

import re

MAX_TOKEN_BYTES = 4096
_TOKEN = re.compile(rb"[A-Za-z0-9._~+/\-]+=*\Z", re.ASCII)


def parse_node_token_bytes(raw: bytes) -> str:
    if type(raw) is not bytes:
        raise ValueError("vpn_node_token_invalid") from None
    token = raw[:-1] if raw.endswith(b"\n") else raw
    if not 1 <= len(token) <= MAX_TOKEN_BYTES or _TOKEN.fullmatch(token) is None:
        raise ValueError("vpn_node_token_invalid") from None
    return token.decode("ascii")
