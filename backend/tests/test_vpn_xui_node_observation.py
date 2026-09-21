from __future__ import annotations

import base64
import importlib
import json
import sqlite3
import subprocess
import sys
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from traceback import format_exception
from uuid import UUID

import pytest

from app.services.vpn_endpoints import VpnEndpointError, VpnEndpointTarget
from app.services.vpn_xui_node_http import NodePanelError, NodePanelSession


SECRET = "synthetic_observation_secret_never_export"
CLIENT_UUID = UUID("11111111-2222-4333-8444-555555555555")
EMAIL = "synthetic-profile"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def test_node_observation_module_available():
    assert importlib.util.find_spec("app.services.vpn_xui_node_observation") is not None


@pytest.fixture
def observation():
    spec = importlib.util.find_spec("app.services.vpn_xui_node_observation")
    assert spec is not None, "node observation module must exist"
    return importlib.import_module("app.services.vpn_xui_node_observation")


@pytest.fixture
def data(tmp_path):
    database = tmp_path / "panel.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.executemany("INSERT INTO inbounds VALUES (?)", [(2,), (7,)])
    target = VpnEndpointTarget(
        1,
        1,
        2,
        "vpn.example.test",
        443,
        "vless",
        "tcp",
        "reality",
        "example.test",
        PUBLIC_KEY,
        "abcd",
        "chrome",
        "xtls-rprx-vision",
    )
    embedded = {
        "id": str(CLIENT_UUID),
        "email": EMAIL,
        "enable": True,
        "flow": "xtls-rprx-vision",
    }
    reality = {
        "serverNames": ["example.test"],
        "shortIds": ["abcd"],
        "privateKey": SECRET,
        "settings": {
            "publicKey": PUBLIC_KEY,
            "fingerprint": "chrome",
            "serverName": "",
        },
    }
    return {
        "database": database,
        "target": target,
        "status": {
            "success": True,
            "obj": {
                "panelVersion": "3.8.5",
                "xray": {"state": "running", "errorMsg": ""},
            },
        },
        "inbounds": {
            "success": True,
            "obj": [
                {
                    "id": 2,
                    "protocol": "vless",
                    "enable": True,
                    "port": 443,
                    "settings": {"decryption": "none", "clients": [embedded]},
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": reality,
                    },
                },
                {
                    "id": 7,
                    "protocol": "vless",
                    "enable": True,
                    "port": 8443,
                    "settings": {"decryption": "none", "clients": []},
                    "streamSettings": {"network": "raw", "security": "none"},
                },
            ],
        },
        "clients": {
            "success": True,
            "obj": [
                {
                    "id": 19,
                    "uuid": str(CLIENT_UUID),
                    "email": EMAIL,
                    "enable": True,
                    "inboundIds": [2],
                }
            ],
        },
        "events": [],
        "on_clients": None,
    }


@pytest.fixture
def panel(data):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            data["events"].append(("GET", self.path))
            if self.path == "/csrf-token":
                body = {"success": True, "obj": "synthetic-token"}
            else:
                key = {
                    "/panel/api/server/status": "status",
                    "/panel/api/inbounds/list": "inbounds",
                    "/panel/api/clients/list": "clients",
                }[self.path]
                if key == "clients" and data["on_clients"]:
                    data["on_clients"]()
                body = data[key]
            self.respond(body)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            data["events"].append(("POST", self.path))
            assert self.path in ("/login", "/logout")
            self.respond({"success": True, "obj": None})

        def respond(self, body):
            encoded = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}
    )
    thread.start()
    try:
        with NodePanelSession(
            f"http://127.0.0.1:{server.server_port}/", "test", SECRET
        ) as session:
            yield session
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def observe(observation, panel, data):
    return observation.observe_node_client(
        panel,
        target=data["target"],
        client_uuid=CLIENT_UUID,
        client_email=EMAIL,
        database_path=data["database"],
    )


def assert_error(observation, panel, data, code, error_type=VpnEndpointError):
    with pytest.raises(error_type) as caught:
        observe(observation, panel, data)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert SECRET not in "".join(format_exception(caught.value))


def test_complete_observation_is_small_frozen_and_read_only(
    observation, panel, data, capsys, caplog
):
    before = data["database"].read_bytes()
    payloads = deepcopy([data["status"], data["inbounds"], data["clients"]])
    result = observe(observation, panel, data)
    assert asdict(result) == {
        "state": "matched",
        "record_id": 19,
        "enabled": True,
        "transport": "matched",
        "runtime": "running",
    }
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.runtime = "error"
    assert SECRET not in repr(result) + repr(asdict(result))
    assert data["database"].read_bytes() == before
    assert payloads == [data["status"], data["inbounds"], data["clients"]]
    assert data["events"][-3:] == [
        ("GET", "/panel/api/server/status"),
        ("GET", "/panel/api/inbounds/list"),
        ("GET", "/panel/api/clients/list"),
    ]
    assert capsys.readouterr() == ("", "")
    assert caplog.text == ""


@pytest.mark.parametrize("state", ["absent", "disabled", "wrong_identity"])
def test_identity_cases(observation, panel, data, state):
    if state == "absent":
        data["inbounds"]["obj"][0]["settings"]["clients"] = []
        data["clients"]["obj"] = []
        result = observe(observation, panel, data)
        assert (result.state, result.record_id, result.enabled) == (
            "not_observed",
            None,
            None,
        )
    elif state == "disabled":
        data["inbounds"]["obj"][0]["settings"]["clients"][0]["enable"] = False
        data["clients"]["obj"][0]["enable"] = False
        assert observe(observation, panel, data).enabled is False
    else:
        data["clients"]["obj"][0]["email"] = "wrong"
        assert_error(observation, panel, data, "vpn_xui_identity_conflict")


@pytest.mark.parametrize(
    "ids", [[2], [2, 7, 8], [2, 2], [2, True], [2, "7"], [2, 7.0], [2, 0]]
)
def test_api_coverage_must_equal_local_inventory(observation, panel, data, ids):
    data["inbounds"]["obj"] = [
        dict(data["inbounds"]["obj"][0], id=value) for value in ids
    ]
    assert_error(observation, panel, data, "vpn_xui_inventory_incomplete")


def test_database_drift_between_reads_rejects(observation, panel, data):
    def drift():
        with sqlite3.connect(data["database"]) as connection:
            connection.execute("INSERT INTO inbounds VALUES (8)")

    data["on_clients"] = drift
    assert_error(observation, panel, data, "vpn_xui_inventory_incomplete")


@pytest.mark.parametrize(
    "kind", ["missing", "relative", "string", "directory", "corrupt", "missing_table"]
)
def test_database_failure_precedes_api_and_never_creates(
    observation, panel, data, kind, tmp_path
):
    if kind == "missing":
        data["database"] = tmp_path / "not-created.sqlite"
    elif kind == "relative":
        data["database"] = Path("relative.sqlite")
    elif kind == "string":
        data["database"] = str(data["database"])
    elif kind == "directory":
        data["database"] = tmp_path
    elif kind == "corrupt":
        data["database"].write_text(SECRET)
    else:
        with sqlite3.connect(data["database"]) as connection:
            connection.execute("DROP TABLE inbounds")
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events
    assert not (tmp_path / "not-created.sqlite").exists()


@pytest.mark.parametrize("version", [None, "3.8.4", "3.8.5 ", 3.85])
def test_version_validation_stops_before_inventory(observation, panel, data, version):
    data["status"]["obj"]["panelVersion"] = version
    assert_error(observation, panel, data, "vpn_xui_version_unsupported")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "xray",
    [
        None,
        {},
        {"state": "unknown", "errorMsg": ""},
        {"state": "running", "errorMsg": None},
    ],
)
def test_malformed_runtime_stops_before_inventory(observation, panel, data, xray):
    data["status"]["obj"]["xray"] = xray
    assert_error(observation, panel, data, "vpn_xui_status_invalid")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "state,message,expected",
    [("stop", "", "stop"), ("error", SECRET, "error"), ("running", SECRET, "error")],
)
def test_runtime_is_cached_observation_without_message(
    observation, panel, data, state, message, expected
):
    data["status"]["obj"]["xray"] = {"state": state, "errorMsg": message}
    result = observe(observation, panel, data)
    assert result.runtime == expected
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("port", True),
        ("port", "443"),
        ("port", 8443),
        ("enable", False),
        ("enable", 1),
        ("streamSettings", None),
    ],
)
def test_transport_shape_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0][field] = value
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "transport,security,flow",
    [
        ("ws", "reality", "xtls-rprx-vision"),
        ("tcp", "tls", ""),
        ("tcp", "none", "vision"),
        ("tcp", "reality", ""),
    ],
)
def test_unsupported_target(observation, panel, data, transport, security, flow):
    data["target"] = replace(
        data["target"], transport=transport, security=security, flow=flow
    )
    assert observe(observation, panel, data).transport == "unsupported"


@pytest.mark.parametrize("legacy_flow", [None, ""])
def test_legacy_and_raw_alias(observation, panel, data, legacy_flow):
    data["target"] = replace(
        data["target"], inbound_id=7, port=8443, security="none", flow=legacy_flow
    )
    embedded = data["inbounds"]["obj"][0]["settings"]["clients"].pop()
    embedded.pop("flow")
    data["inbounds"]["obj"][1]["settings"]["clients"].append(embedded)
    data["clients"]["obj"][0]["inboundIds"] = [7]
    assert observe(observation, panel, data).transport == "matched"


def test_embedded_flow_not_global_override(observation, panel, data):
    data["clients"]["obj"][0]["flow"] = "wrong-global-flow"
    assert observe(observation, panel, data).transport == "matched"
    data["inbounds"]["obj"][0]["settings"]["clients"][0]["flow"] = ""
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("serverNames", []),
        ("serverNames", ["wrong"]),
        ("shortIds", ["ABCD"]),
        ("shortIds", ["abc"]),
        ("privateKey", ""),
        ("privateKey", 1),
        ("settings", None),
    ],
)
def test_reality_shape_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"][field] = value
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "field,value",
    [
        ("publicKey", PUBLIC_KEY + "="),
        ("fingerprint", "wrong"),
        ("fingerprint", "bad\n"),
        ("serverName", "wrong"),
        ("spiderX", "/"),
        ("mldsa65Verify", SECRET),
    ],
)
def test_reality_nested_settings_mismatch(observation, panel, data, field, value):
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"]["settings"][
        field
    ] = value
    assert observe(observation, panel, data).transport == "mismatch"


@pytest.mark.parametrize(
    "key,value",
    [
        ("tcpSettings", {"header": {"type": "http"}}),
        ("rawSettings", {"acceptProxyProtocol": True}),
    ],
)
def test_unrepresentable_raw_options_reject(observation, panel, data, key, value):
    data["inbounds"]["obj"][0]["streamSettings"][key] = value
    assert observe(observation, panel, data).transport == "mismatch"


def test_panel_error_propagates(observation, panel, data):
    data["inbounds"]["nodePending"] = True
    assert_error(observation, panel, data, "vpn_xui_apply_pending", NodePanelError)


@pytest.mark.parametrize("fingerprint", ["custom fingerprint", " ", "a" * 32])
def test_fingerprint_contract_allows_printable_ascii(
    observation, panel, data, fingerprint
):
    data["target"] = replace(data["target"], fingerprint=fingerprint)
    data["inbounds"]["obj"][0]["streamSettings"]["realitySettings"]["settings"][
        "fingerprint"
    ] = fingerprint
    assert observe(observation, panel, data).transport == "matched"


@pytest.mark.parametrize(
    "ids",
    [[2, 2], [2, None], [2, "7"], [2, 7.5], [2, 0], [2, -7], list(range(1, 10002))],
)
def test_invalid_or_oversized_local_ids_fail_before_api(observation, panel, data, ids):
    with sqlite3.connect(data["database"]) as connection:
        connection.execute("DELETE FROM inbounds")
        connection.executemany(
            "INSERT INTO inbounds VALUES (?)", [(value,) for value in ids]
        )
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events


def test_local_reads_select_only_ids_and_use_read_only_connection(
    observation, panel, data, monkeypatch
):
    real_connect = sqlite3.connect
    opened, statements, closed, progress = [], [], [], []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

        def set_progress_handler(self, callback, steps):
            progress.append((callback, steps))
            super().set_progress_handler(callback, steps)

    def connect(database, **kwargs):
        opened.append((database, kwargs))
        connection = real_connect(database, factory=Connection, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(observation.sqlite3, "connect", connect)
    observe(observation, panel, data)
    assert len(opened) == len(closed) == len(progress) == 2
    assert all(
        uri == data["database"].as_uri() + "?mode=ro"
        and kwargs == {"uri": True, "timeout": 2}
        for uri, kwargs in opened
    )
    assert (
        statements
        == ["PRAGMA query_only=ON", "SELECT id FROM inbounds ORDER BY id LIMIT 10001"]
        * 2
    )
    assert all(steps > 0 for _, steps in progress)


def test_sqlite_deadline_error_is_static_and_connection_closes(
    observation, panel, data, monkeypatch
):
    real_connect = sqlite3.connect
    closed = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        observation.sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(*args, factory=Connection, **kwargs),
    )
    ticks = iter([0.0, 3.0])
    monkeypatch.setattr(observation.time, "monotonic", lambda: next(ticks))
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert closed == [True]


def test_symlink_database_is_rejected_before_api(observation, panel, data, monkeypatch):
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == data["database"] or real_is_symlink(path),
    )
    events = list(data["events"])
    assert_error(observation, panel, data, "vpn_xui_inventory_unavailable")
    assert data["events"] == events


@pytest.mark.parametrize("obj", [None, [], SECRET])
def test_status_object_shape_is_validated_immediately(observation, panel, data, obj):
    data["status"]["obj"] = obj
    assert_error(observation, panel, data, "vpn_xui_status_invalid")
    assert data["events"][-1] == ("GET", "/panel/api/server/status")


@pytest.mark.parametrize(
    "change,expected",
    [
        ("decryption", "mismatch"),
        ("protocol", "vpn_xui_inbound_unsupported"),
        ("non_target_transport", "matched"),
    ],
)
def test_identity_protocol_defects_and_transport_scope(
    observation, panel, data, change, expected
):
    if change == "decryption":
        data["inbounds"]["obj"][0]["settings"]["decryption"] = "wrong"
    elif change == "protocol":
        data["inbounds"]["obj"][0]["protocol"] = "trojan"
    else:
        data["inbounds"]["obj"][1]["streamSettings"] = SECRET
    if expected.startswith("vpn_"):
        assert_error(observation, panel, data, expected)
    else:
        assert observe(observation, panel, data).transport == expected


@pytest.mark.parametrize(
    "envelope",
    [
        {"success": 1, "obj": []},
        {"success": True, "nodePending": 0, "obj": []},
        {"success": True, "obj": {}},
        {"success": True, "obj": [None]},
    ],
)
def test_coverage_envelope_is_strict_even_without_http_normalization(
    observation, envelope
):
    with pytest.raises(VpnEndpointError, match="^vpn_xui_inventory_incomplete$"):
        observation._covered_rows(envelope, (), ())


def test_types_reexport_same_objects_and_import_without_site_packages(observation):
    from app.services import vpn_endpoint_types, vpn_endpoints

    for name in ("VpnEndpointError", "VpnEndpointTarget", "EndpointOperation"):
        assert getattr(vpn_endpoint_types, name) is getattr(vpn_endpoints, name)
    code = """
import sys, sqlite3, socket, ssl, pathlib, builtins
def forbidden(*args, **kwargs):
    raise AssertionError('import performed I/O')
sqlite3.connect = socket.socket = pathlib.Path.open = builtins.open = forbidden
import app.services.vpn_xui_node_observation
assert not any(name in sys.modules for name in ('sqlalchemy', 'httpx', 'app.core.config', 'app.db.models'))
"""
    result = subprocess.run(
        [sys.executable, "-S", "-c", code],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[1],
        check=False,
    )
    assert (result.returncode, result.stdout, result.stderr) == (0, "", "")
