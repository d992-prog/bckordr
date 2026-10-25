from __future__ import annotations

import pytest


@pytest.mark.parametrize("ending", [b"", b"\n"])
@pytest.mark.parametrize("token", [b"a", b"Legacy_./+~-token==", b"a" * 4096])
def test_node_token_accepts_exact_legacy_or_single_lf_bytes(token, ending):
    from app.services.vpn_node_token import parse_node_token_bytes

    raw = token + ending
    assert parse_node_token_bytes(raw) == token.decode("ascii")
    assert raw == token + ending


@pytest.mark.parametrize(
    "raw",
    [
        b"", b"\n", b"\r", b"token\r\n", b"token\r", b"token\n\n",
        b"first\nsecond", b"first\nsecond\n", b"token\x00", b"token\x1f",
        b"token\x7f", b"token\xff", b" token", b"token ", b"token\t",
        b"token:invalid", b"=", b"tok=en", b"a" * 4097, b"a" * 4097 + b"\n",
    ],
)
def test_node_token_rejects_invalid_bytes_without_echoing_them(raw):
    from app.services.vpn_node_token import parse_node_token_bytes

    with pytest.raises(ValueError, match="^vpn_node_token_invalid$") as caught:
        parse_node_token_bytes(raw)
    assert caught.value.__context__ is None


@pytest.mark.parametrize("raw", [None, "token", bytearray(b"token")])
def test_node_token_requires_bytes(raw):
    from app.services.vpn_node_token import parse_node_token_bytes

    with pytest.raises(ValueError, match="^vpn_node_token_invalid$"):
        parse_node_token_bytes(raw)
