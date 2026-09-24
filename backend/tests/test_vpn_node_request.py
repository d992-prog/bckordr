from __future__ import annotations

import base64
import importlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from hashlib import sha256
from uuid import UUID, uuid4

import pytest

from app.services.vpn_endpoint_types import VpnEndpointTarget


def test_request_module_exists():
    assert importlib.util.find_spec("app.services.vpn_node_request") is not None


@pytest.fixture
def api():
    assert importlib.util.find_spec("app.services.vpn_node_request") is not None
    return importlib.import_module("app.services.vpn_node_request")


@pytest.fixture
def request_value(api):
    return api.VpnNodeRequest(
        uuid4(),
        11,
        1,
        "provision",
        VpnEndpointTarget(
            1,
            2,
            3,
            "vpn.example.test",
            443,
            "vless",
            "tcp",
            "reality",
            "example.test",
            base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("="),
            "abcd",
            "chrome",
            "xtls-rprx-vision",
        ),
        UUID("11111111-2222-4333-8444-555555555555"),
        "private-email",
        "private-sub-id",
        0,
        0,
        1000,
        True,
        False,
    )


def test_roundtrip_exact_digest_detached_and_secret_free(api, request_value):
    value = api.serialize_node_request(request_value)
    assert value == dict(
        asdict(request_value),
        version=1,
        operation_id=str(request_value.operation_id),
        client_uuid=str(request_value.client_uuid),
    )
    result = api.parse_node_request(value)
    assert result == request_value
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.generation = 9
    assert (
        api.node_request_digest(result)
        == sha256(
            json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode()
        ).hexdigest()
    )
    value["target"]["port"] = 99
    assert result.target.port == 443
    for secret in (
        str(result.client_uuid),
        result.client_email,
        result.sub_id,
        result.target.public_key,
    ):
        assert secret not in repr(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("operation_id", "not-uuid"),
        ("client_uuid", "11111111222243338444555555555555"),
        ("access_key_id", True),
        ("access_key_id", 0),
        ("generation", 2**63),
        ("expires_at_ms", -1),
        ("traffic_limit_bytes", 1.0),
        ("created_at_ms", 0),
        ("allow_create", 1),
        ("allow_shared_restart", None),
        ("action", "delete"),
        ("client_email", "."),
        ("client_email", ".."),
        ("client_email", "a/b"),
        ("client_email", "кириллица"),
        ("sub_id", "x" * 65),
        ("sub_id", ""),
    ],
)
def test_invalid_snapshot_has_only_static_error(api, request_value, field, value):
    payload = api.serialize_node_request(request_value)
    payload[field] = value
    with pytest.raises(api.VpnNodeRequestError) as caught:
        api.parse_node_request(payload)
    assert str(caught.value) == "vpn_node_request_invalid"
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint_id", True),
        ("worker_id", 0),
        ("inbound_id", 2**63),
        ("port", 65536),
        ("public_host", "bad/path"),
        ("server_name", "bad name"),
        ("protocol", "trojan"),
        ("transport", "ws"),
        ("security", "tls"),
        ("flow", ""),
        ("public_key", "a" * 43),
        ("short_id", "ABCD"),
        ("fingerprint", "bad\n"),
    ],
)
def test_invalid_target(api, request_value, field, value):
    bad = replace(request_value, target=replace(request_value.target, **{field: value}))
    with pytest.raises(api.VpnNodeRequestError):
        api.serialize_node_request(bad)


@pytest.mark.parametrize("scope", ["request", "target"])
@pytest.mark.parametrize("kind", ["missing", "extra"])
def test_exact_fields(api, request_value, scope, kind):
    value = api.serialize_node_request(request_value)
    selected = value if scope == "request" else value["target"]
    if kind == "missing":
        selected.pop(next(iter(selected)))
    else:
        selected["secret-unexpected"] = "secret"
    with pytest.raises(api.VpnNodeRequestError):
        api.parse_node_request(value)


def test_each_field_changes_digest_and_legacy_policy(api, request_value):
    changes = dict(
        operation_id=uuid4(),
        access_key_id=12,
        generation=2,
        action="suspend",
        target=replace(request_value.target, port=444),
        client_uuid=uuid4(),
        client_email="other",
        sub_id="other",
        expires_at_ms=1,
        traffic_limit_bytes=1,
        created_at_ms=1001,
        allow_create=True,
        allow_shared_restart=True,
    )
    original = replace(request_value, allow_create=False)
    for field, value in changes.items():
        assert api.node_request_digest(
            replace(original, **{field: value})
        ) != api.node_request_digest(original)
    legacy = replace(
        original,
        sub_id="",
        target=replace(
            original.target,
            security="none",
            flow="",
            server_name=None,
            public_key=None,
            short_id=None,
            fingerprint=None,
        ),
    )
    assert api.parse_node_request(api.serialize_node_request(legacy)) == legacy
    with pytest.raises(api.VpnNodeRequestError):
        api.serialize_node_request(replace(legacy, allow_create=True))


def test_request_import_without_site_packages():
    result = subprocess.run(
        [sys.executable, "-S", "-c", "import app.services.vpn_node_request"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
