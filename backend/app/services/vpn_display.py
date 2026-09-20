"""Pure validation and display transformations for customer VPN data."""

import base64
import binascii
import json
import unicodedata
import uuid
from urllib.parse import quote, urlsplit


class InvalidVpnDisplay(ValueError):
    """A safe, user-facing validation failure for VPN display data."""


def _invalid_configuration() -> None:
    raise InvalidVpnDisplay("invalid_configuration") from None


def validate_display_name(value: str) -> str:
    """Return a normalized display name or raise without exposing its input."""
    if not isinstance(value, str):
        raise InvalidVpnDisplay("invalid_display_name") from None

    normalized = value.strip()
    if not 1 <= len(normalized) <= 64 or any(
        unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in normalized
    ):
        raise InvalidVpnDisplay("invalid_display_name") from None
    return normalized


def connection_label(name: str) -> str:
    """Build the stable label shown for a VPN connection."""
    return "Veltrix VPN · " + validate_display_name(name)


def _is_uuid(value: object) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, ValueError, TypeError):
        return False
    return str(parsed).lower() == value.lower()


def _display_vless(uri: str, label: str) -> str:
    prefix = uri.split("#", 1)[0]
    separator_index = prefix.find("://")
    if separator_index < 0 or prefix[:separator_index].lower() != "vless":
        _invalid_configuration()
    parsed = urlsplit(prefix)
    if parsed.scheme.lower() != "vless":
        _invalid_configuration()
    if (
        parsed.username is None
        or parsed.password is not None
        or not _is_uuid(parsed.username)
    ):
        _invalid_configuration()
    hostname = parsed.hostname
    if not hostname or any(
        character.isspace() or character == "\\" for character in hostname
    ):
        _invalid_configuration()
    port = parsed.port
    if port is None or not 1 <= port <= 65535:
        _invalid_configuration()
    return prefix + "#" + quote(label, safe="")


def _reject_json_constant(value: str) -> None:
    raise ValueError(value)


def _display_vmess(uri: str, label: str) -> str:
    parsed = urlsplit(uri)
    before_fragment, fragment_separator, _ = uri.partition("#")
    separator = "://"
    separator_index = before_fragment.find(separator)
    if (
        parsed.query
        or "?" in before_fragment
        or fragment_separator
        or separator_index < 0
        or before_fragment.count(separator) != 1
        or before_fragment[:separator_index].lower() != "vmess"
    ):
        _invalid_configuration()
    payload = before_fragment[separator_index + len(separator) :]
    if not payload:
        _invalid_configuration()

    padded_payload = payload + "=" * (-len(payload) % 4)
    decoded = base64.b64decode(
        padded_payload.encode("ascii"), altchars=b"-_", validate=True
    )
    data = json.loads(decoded.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(data, dict):
        _invalid_configuration()

    address = data.get("add")
    identifier = data.get("id")
    port = data.get("port")
    if (
        not isinstance(address, str)
        or not address
        or any(character.isspace() or character == "\\" for character in address)
    ):
        _invalid_configuration()
    if not _is_uuid(identifier):
        _invalid_configuration()
    if isinstance(port, bool):
        _invalid_configuration()
    if isinstance(port, int):
        port_number = port
    elif isinstance(port, str) and port:
        if not port.isascii() or not port.isdecimal():
            _invalid_configuration()
        port_number = int(port)
    else:
        _invalid_configuration()
    if not 1 <= port_number <= 65535:
        _invalid_configuration()

    data["ps"] = label
    encoded = json.dumps(
        data, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return "vmess://" + base64.b64encode(encoded).decode("ascii")


def display_uri(uri: str, name: str) -> str:
    """Apply the display label to a supported VLESS or VMess URI."""
    label = connection_label(name)
    if (
        not isinstance(uri, str)
        or not uri
        or len(uri) > 16384
        or any(
            unicodedata.category(character) in {"Cc", "Cf", "Cs"} for character in uri
        )
    ):
        _invalid_configuration()

    try:
        parsed = urlsplit(uri)
        scheme = parsed.scheme.lower()
        if scheme == "vless":
            return _display_vless(uri, label)
        if scheme == "vmess":
            return _display_vmess(uri, label)
    except (
        binascii.Error,
        RecursionError,
        UnicodeError,
        ValueError,
        TypeError,
        OverflowError,
    ):
        _invalid_configuration()
    _invalid_configuration()
