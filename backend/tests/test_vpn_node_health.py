from __future__ import annotations

import base64
import importlib
import json
import subprocess
import sys
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from traceback import format_exception

import pytest

from app.services.vpn_endpoint_types import VpnEndpointTarget

NOW_MS = 1_700_000_000_000
SECRET = "private-panel-token-must-never-escape"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


@pytest.fixture
def api():
    assert importlib.util.find_spec("app.services.vpn_node_health") is not None
    return importlib.import_module("app.services.vpn_node_health")


@pytest.fixture
def target() -> VpnEndpointTarget:
    return VpnEndpointTarget(
        endpoint_id=1,
        worker_id=2,
        inbound_id=3,
        public_host="vpn.example.test",
        port=443,
        protocol="vless",
        transport="tcp",
        security="reality",
        server_name="example.test",
        public_key=PUBLIC_KEY,
        short_id="abcd",
        fingerprint="chrome",
        flow="xtls-rprx-vision",
    )


@pytest.fixture
def health_request(api, target):
    return api.VpnNodeHealthRequest(
        worker_id=target.worker_id,
        target=target,
        checked_at_ms=NOW_MS,
    )


@pytest.fixture
def receipt(api):
    return api.VpnNodeHealthReceipt(
        state="healthy",
        error_code=None,
        runtime="running",
    )


def test_health_values_are_frozen_slotted_and_secret_free(api, health_request, receipt):
    assert not hasattr(health_request, "__dict__")
    assert not hasattr(receipt, "__dict__")
    with pytest.raises(FrozenInstanceError):
        health_request.worker_id = 9
    with pytest.raises(FrozenInstanceError):
        receipt.runtime = "stopped"
    assert "target=" not in repr(health_request)
    assert PUBLIC_KEY not in repr(health_request)


def test_request_serialization_is_exact_canonical_and_roundtrips(api, health_request):
    value = {
        "version": 1,
        "worker_id": health_request.worker_id,
        "target": asdict(health_request.target),
        "checked_at_ms": health_request.checked_at_ms,
    }
    encoded = api.serialize_node_health_request(health_request, now_ms=NOW_MS)

    assert encoded == canonical(value)
    assert encoded == api.serialize_node_health_request(health_request, now_ms=NOW_MS)
    assert encoded.endswith(b"}")
    assert b"\n" not in encoded
    assert api.parse_node_health_request(encoded, now_ms=NOW_MS) == health_request
    assert all(
        secret not in encoded
        for secret in (b'"client_uuid"', b'"uri"', b'"panel_token"')
    )


def test_request_uses_current_time_when_now_is_not_supplied(
    api, health_request, monkeypatch
):
    monkeypatch.setattr(api, "time_ns", lambda: NOW_MS * 1_000_000)
    encoded = api.serialize_node_health_request(health_request)
    assert api.parse_node_health_request(encoded) == health_request


def test_receipt_serialization_is_exact_canonical_and_roundtrips(api, receipt):
    encoded = api.serialize_node_health_receipt(receipt)
    assert encoded == canonical(
        {
            "version": 1,
            "state": "healthy",
            "error_code": None,
            "runtime": "running",
        }
    )
    assert encoded == api.serialize_node_health_receipt(receipt)
    assert api.parse_node_health_receipt(encoded) == receipt


@pytest.mark.parametrize("message", ["request", "receipt"])
@pytest.mark.parametrize("defect", ["unknown", "missing", "duplicate", "noncanonical"])
def test_envelopes_reject_unknown_missing_duplicate_and_noncanonical_fields(
    api, health_request, receipt, message, defect
):
    if message == "request":
        raw = api.serialize_node_health_request(health_request, now_ms=NOW_MS)
        parse = lambda value: api.parse_node_health_request(value, now_ms=NOW_MS)
    else:
        raw = api.serialize_node_health_receipt(receipt)
        parse = api.parse_node_health_receipt

    if defect in ("unknown", "missing"):
        payload = json.loads(raw)
        if defect == "unknown":
            payload["unknown"] = SECRET
        else:
            payload.pop("version")
        raw = canonical(payload)
    elif defect == "duplicate":
        raw = raw.replace(b'"version":1', b'"version":1,"version":1', 1)
    else:
        raw += b"\n"

    with pytest.raises(api.VpnNodeHealthError) as caught:
        parse(raw)
    assert caught.value.code == "vpn_node_health_invalid"
    assert str(caught.value) == "vpn_node_health_invalid"
    assert caught.value.__context__ is None
    assert SECRET not in "".join(format_exception(caught.value))


@pytest.mark.parametrize("defect", ["unknown", "missing", "duplicate"])
def test_target_rejects_unknown_missing_and_duplicate_fields(
    api, health_request, defect
):
    raw = api.serialize_node_health_request(health_request, now_ms=NOW_MS)
    if defect in ("unknown", "missing"):
        payload = json.loads(raw)
        if defect == "unknown":
            payload["target"]["unknown"] = SECRET
        else:
            payload["target"].pop("endpoint_id")
        raw = canonical(payload)
    else:
        raw = raw.replace(b'"endpoint_id":1', b'"endpoint_id":1,"endpoint_id":1', 1)
    with pytest.raises(api.VpnNodeHealthError):
        api.parse_node_health_request(raw, now_ms=NOW_MS)


@pytest.mark.parametrize("message", ["request", "receipt"])
@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"[]",
        b"{",
        b"\xff",
        b'{"version":NaN}',
        bytearray(b"{}"),
    ],
)
def test_parsers_reject_invalid_bytes_utf8_json_and_nonobjects(api, message, raw):
    parse = (
        (lambda value: api.parse_node_health_request(value, now_ms=NOW_MS))
        if message == "request"
        else api.parse_node_health_receipt
    )
    with pytest.raises(api.VpnNodeHealthError, match="^vpn_node_health_invalid$"):
        parse(raw)


@pytest.mark.parametrize("message", ["request", "receipt"])
def test_parsers_reject_payloads_over_16_kib(api, message):
    raw = b"{" + b" " * (16 * 1024) + b"}"
    parse = (
        (lambda value: api.parse_node_health_request(value, now_ms=NOW_MS))
        if message == "request"
        else api.parse_node_health_receipt
    )
    with pytest.raises(api.VpnNodeHealthError):
        parse(raw)


@pytest.mark.parametrize(
    "offset,valid",
    [
        (-300_001, False),
        (-300_000, True),
        (300_000, True),
        (300_001, False),
    ],
)
def test_request_clock_window_has_exact_past_and_future_boundaries(
    api, health_request, offset, valid
):
    candidate = replace(health_request, checked_at_ms=NOW_MS + offset)
    if valid:
        raw = api.serialize_node_health_request(candidate, now_ms=NOW_MS)
        assert api.parse_node_health_request(raw, now_ms=NOW_MS) == candidate
    else:
        with pytest.raises(api.VpnNodeHealthError):
            api.serialize_node_health_request(candidate, now_ms=NOW_MS)


@pytest.mark.parametrize(
    "field,value",
    [
        ("worker_id", True),
        ("worker_id", 0),
        ("worker_id", 2**63),
        ("checked_at_ms", True),
        ("checked_at_ms", 0),
        ("checked_at_ms", 2**63),
    ],
)
def test_request_rejects_bool_and_out_of_range_integers(
    api, health_request, field, value
):
    with pytest.raises(api.VpnNodeHealthError):
        api.serialize_node_health_request(
            replace(health_request, **{field: value}), now_ms=NOW_MS
        )


def test_request_rejects_invalid_now_and_worker_mismatch(api, health_request):
    with pytest.raises(api.VpnNodeHealthError):
        api.serialize_node_health_request(health_request, now_ms=True)
    with pytest.raises(api.VpnNodeHealthError):
        api.serialize_node_health_request(
            replace(health_request, worker_id=health_request.worker_id + 1),
            now_ms=NOW_MS,
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("endpoint_id", True),
        ("worker_id", 0),
        ("inbound_id", 0),
        ("public_host", "bad/path"),
        ("port", True),
        ("protocol", "trojan"),
        ("transport", "ws"),
        ("security", "tls"),
        ("server_name", "bad name"),
        ("public_key", SECRET),
        ("short_id", "ABCD"),
        ("fingerprint", "bad\n"),
        ("flow", ""),
    ],
)
def test_request_rejects_every_invalid_target_field(api, health_request, field, value):
    candidate = replace(
        health_request,
        target=replace(health_request.target, **{field: value}),
    )
    with pytest.raises(api.VpnNodeHealthError) as caught:
        api.serialize_node_health_request(candidate, now_ms=NOW_MS)
    assert SECRET not in repr(caught.value)


def test_request_delegates_target_validation_to_canonical_validator(
    api, health_request, monkeypatch
):
    seen = []

    def reject(value):
        seen.append(value)
        return False

    monkeypatch.setattr(api, "is_valid_vpn_endpoint_target", reject)
    with pytest.raises(api.VpnNodeHealthError):
        api.serialize_node_health_request(health_request, now_ms=NOW_MS)
    assert seen == [health_request.target]


VALID_RECEIPTS = [
    ("healthy", None, "running"),
    ("unhealthy", "vpn_node_health_endpoint_missing", "running"),
    ("unhealthy", "vpn_node_health_endpoint_mismatch", "running"),
    ("unhealthy", "vpn_node_health_runtime_unavailable", "error"),
    ("unhealthy", "vpn_node_health_runtime_stopped", "stopped"),
    ("unhealthy", "vpn_node_health_listener_unavailable", "running"),
    ("unhealthy", "vpn_node_health_panel_unavailable", None),
    ("unhealthy", "vpn_node_health_internal", None),
]


@pytest.mark.parametrize("state,error_code,runtime", VALID_RECEIPTS)
def test_receipt_accepts_only_documented_combinations(api, state, error_code, runtime):
    receipt = api.VpnNodeHealthReceipt(state, error_code, runtime)
    raw = api.serialize_node_health_receipt(receipt)
    assert api.parse_node_health_receipt(raw) == receipt


@pytest.mark.parametrize("state", ["healthy", "unhealthy"])
@pytest.mark.parametrize(
    "error_code",
    [None, *(item[1] for item in VALID_RECEIPTS if item[1]), "arbitrary-secret"],
)
@pytest.mark.parametrize("runtime", [None, "running", "stopped", "error", "unknown"])
def test_receipt_rejects_every_other_combination(api, state, error_code, runtime):
    combination = (state, error_code, runtime)
    if combination in VALID_RECEIPTS:
        return
    with pytest.raises(api.VpnNodeHealthError) as caught:
        api.serialize_node_health_receipt(
            api.VpnNodeHealthReceipt(state, error_code, runtime)
        )
    assert "arbitrary-secret" not in repr(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("state", True),
        ("error_code", True),
        ("runtime", True),
    ],
)
def test_receipt_parser_rejects_bool_values(api, receipt, field, value):
    payload = json.loads(api.serialize_node_health_receipt(receipt))
    payload[field] = value
    with pytest.raises(api.VpnNodeHealthError):
        api.parse_node_health_receipt(canonical(payload))


def test_health_protocol_imports_under_isolated_python_without_site_packages():
    backend = Path(__file__).resolve().parents[1]
    code = (
        f"import sys; sys.path.insert(0, {str(backend)!r}); "
        "import app.services.vpn_node_health; "
        "assert not any(name in sys.modules for name in "
        "('sqlalchemy', 'fastapi', 'httpx', 'app.core.config', 'app.db.models'))"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
