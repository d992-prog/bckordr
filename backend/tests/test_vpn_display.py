import base64
import json

import pytest

from app.services import vpn_display
from app.services.vpn_display import (
    InvalidVpnDisplay,
    connection_label,
    display_uri,
    validate_display_name,
)


UUID = "11111111-1111-4111-8111-111111111111"
NAME = "iPhone & работа"
LABEL = "Veltrix VPN · iPhone & работа"


def _vmess_uri(
    payload: dict[str, object], *, urlsafe: bool = False, padding: bool = True
) -> str:
    encoded = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    )
    if urlsafe:
        encoded = encoded.replace(b"+", b"-").replace(b"/", b"_")
    if not padding:
        encoded = encoded.rstrip(b"=")
    return "vmess://" + encoded.decode()


def _raw_vmess_uri(raw_json: str) -> str:
    return "vmess://" + base64.b64encode(raw_json.encode()).decode()


def test_validate_display_name_trims_and_accepts_padded_unicode_name():
    name = "  " + "я" * 64 + "  "

    assert validate_display_name(name) == "я" * 64


@pytest.mark.parametrize("value", ["", "  ", "a" * 65, "a\nb", "a\u202eb"])
def test_validate_display_name_rejects_invalid_values_without_echoing_input(value):
    with pytest.raises(InvalidVpnDisplay, match="^invalid_display_name$") as error:
        validate_display_name(value)

    assert not value or value not in str(error.value)


def test_connection_label_uses_validated_name_and_prefix():
    assert connection_label("  iPhone  ") == "Veltrix VPN · iPhone"


def test_display_uri_replaces_vless_fragment_and_preserves_original_prefix():
    uri = (
        "vless://11111111-1111-4111-8111-111111111111@vpn.example:8443"
        "?type=tcp&security=none#dropcatch-8-test1"
    )

    result = display_uri(uri, NAME)

    assert (
        result
        == uri.split("#", 1)[0]
        + "#Veltrix%20VPN%20%C2%B7%20iPhone%20%26%20%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0"
    )
    assert result.split("#", 1)[0] == uri.split("#", 1)[0]


def test_display_uri_vless_preserves_ipv6_path_query_and_existing_encoding():
    uri = (
        "vless://11111111-1111-4111-8111-111111111111@[2001:db8::1]:443"
        "?path=%2Fa%3Fb&type=grpc#old%20fragment"
    )

    result = display_uri(uri, "Phone")

    assert result == uri.split("#", 1)[0] + "#Veltrix%20VPN%20%C2%B7%20Phone"


@pytest.mark.parametrize("host", ["vpn example", r"vpn\example", " vpn.example"])
def test_display_uri_rejects_malformed_vless_hostname(host):
    uri = f"vless://{UUID}@{host}:443"

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(uri, "Phone")


def test_display_uri_rewrites_only_vmess_ps_and_keeps_unknown_fields():
    payload = {
        "v": "2",
        "ps": "old",
        "add": "vpn.example",
        "port": "443",
        "id": UUID,
        "net": "tcp",
        "tls": "tls",
        "unknown": {"enabled": True, "items": [1, "two"]},
    }

    result = display_uri(_vmess_uri(payload), NAME)
    encoded = result.removeprefix("vmess://")
    decoded = json.loads(base64.b64decode(encoded))

    assert decoded == {**payload, "ps": LABEL}
    assert result.startswith("vmess://")


@pytest.mark.parametrize(
    "urlsafe,padding", [(False, True), (False, False), (True, True), (True, False)]
)
def test_display_uri_accepts_vmess_base64_variants(urlsafe, padding):
    payload = {
        "v": "2",
        "ps": "old",
        "add": "vpn.example",
        "port": "443",
        "id": UUID,
        "x": '">',
    }
    input_uri = _vmess_uri(payload, urlsafe=urlsafe, padding=padding)
    input_payload = input_uri.removeprefix("vmess://")
    if urlsafe:
        assert "-" in input_payload or "_" in input_payload
    else:
        assert "+" in input_payload or "/" in input_payload

    result = display_uri(input_uri, "Phone")

    assert json.loads(base64.b64decode(result.removeprefix("vmess://"))) == {
        **payload,
        "ps": "Veltrix VPN · Phone",
    }


@pytest.mark.parametrize("address", ["vpn example", r"vpn\example", " "])
def test_display_uri_rejects_malformed_vmess_address(address):
    payload = {"v": "2", "ps": "old", "add": address, "port": "443", "id": UUID}

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(_vmess_uri(payload), "Phone")


@pytest.mark.parametrize(
    "raw_json",
    [
        '{"v":"2","ps":"old","add":"vpn.example","port":"443","id":null}',
        '{"v":"2","ps":"old","add":"vpn.example","port":true,"id":"' + UUID + '"}',
        '{"v":"2","ps":"old","add":"vpn.example","port":null,"id":"' + UUID + '"}',
        '{"v":"2","ps":"old","add":"vpn.example","port":443.0,"id":"' + UUID + '"}',
        '{"v":"2","ps":"old","port":"443","id":"' + UUID + '"}',
        '{"v":"2","ps":"old","add":"vpn.example","id":"' + UUID + '"}',
        '{"v":"2","ps":"old","add":"vpn.example","port":"443"}',
    ],
)
def test_display_uri_rejects_malformed_vmess_required_fields(raw_json):
    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(_raw_vmess_uri(raw_json), "Phone")


def test_display_uri_rejects_non_finite_unknown_vmess_field():
    raw_json = (
        '{"v":"2","ps":"old","add":"vpn.example","port":"443","id":"'
        + UUID
        + '","x":1e999}'
    )

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(_raw_vmess_uri(raw_json), "Phone")


def test_display_uri_rejects_deeply_nested_vmess_json(monkeypatch):
    monkeypatch.setattr(json.scanner, "make_scanner", json.scanner.py_make_scanner)
    monkeypatch.setattr(json.decoder, "scanstring", json.decoder.py_scanstring)

    nested = "0"
    for _ in range(2000):
        nested = "[" + nested + "]"
    raw_json = (
        '{"v":"2","ps":"old","add":"vpn.example","port":"443","id":"'
        + UUID
        + '","x":'
        + nested
        + "}"
    )

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(_raw_vmess_uri(raw_json), "Phone")


def test_display_uri_normalizes_json_recursion_error(monkeypatch):
    uri = _vmess_uri(
        {"v": "2", "ps": "old", "add": "vpn.example", "port": "443", "id": UUID}
    )
    calls = 0

    def raise_recursion_error(*args, **kwargs):
        nonlocal calls
        calls += 1
        raise RecursionError

    monkeypatch.setattr(vpn_display.json, "loads", raise_recursion_error)

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$") as error:
        display_uri(uri, "Phone")

    assert calls == 1
    assert error.value.__cause__ is None


@pytest.mark.parametrize(
    "uri",
    [
        "",
        "https://example.com",
        "vmess://bad",
        "vmess:bad",
        " vless://" + UUID + "@vpn.example:443",
        "vless://no-host",
        "vless://not-a-uuid@vpn.example:443",
        "vless://" + UUID + "@vpn.example:0",
        "vless://" + UUID + "@vpn.example:65536",
        "vless://" + UUID + ":secret@vpn.example:443",
        "vless://" + UUID + "@vpn.example",
        "vless://" + UUID + "@vpn.example:443\n",
    ],
)
def test_display_uri_rejects_invalid_configuration_without_echoing_uri(uri):
    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$") as error:
        display_uri(uri, "Phone")

    assert not uri or uri not in str(error.value)
    assert error.value.__cause__ is None


@pytest.mark.parametrize("suffix", ["#existing", "#", "?query", "?"])
def test_display_uri_rejects_vmess_fragments_and_queries(suffix):
    payload = {"v": "2", "ps": "old", "add": "vpn.example", "port": "443", "id": UUID}

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(_vmess_uri(payload) + suffix, "Phone")


def test_display_uri_rejects_missing_or_oversized_input():
    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri(None, "Phone")  # type: ignore[arg-type]

    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri("vless://" + "a" * 16384, "Phone")


def test_display_uri_rejects_control_characters_in_vmess_input():
    with pytest.raises(InvalidVpnDisplay, match="^invalid_configuration$"):
        display_uri("vmess://bad\u202evalue", "Phone")
