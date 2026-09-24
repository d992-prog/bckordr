from __future__ import annotations

import base64
import hashlib
import importlib
import importlib.util
import io
import json
import sqlite3
import threading
from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from time import time_ns

import pytest

from app.services.vpn_endpoint_types import VpnEndpointTarget
from app.services.vpn_node_health import (
    VpnNodeHealthReceipt,
    VpnNodeHealthRequest,
    serialize_node_health_receipt,
    serialize_node_health_request,
)

SECRET = "synthetic-health-token-must-never-escape"
PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def test_node_health_entrypoint_module_available():
    assert (
        importlib.util.find_spec("app.services.vpn_node_health_entrypoint") is not None
    )


@pytest.fixture
def entrypoint():
    return importlib.import_module("app.services.vpn_node_health_entrypoint")


@pytest.fixture
def observation():
    return importlib.import_module("app.services.vpn_xui_node_observation")


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
def node(tmp_path, target):
    database = tmp_path / "x-ui.db"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.execute("INSERT INTO inbounds VALUES (?)", (target.inbound_id,))
    state = {
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
                    "id": target.inbound_id,
                    "protocol": "vless",
                    "listen": "",
                    "enable": True,
                    "port": target.port,
                    "settings": {
                        "decryption": "none",
                        "clients": [
                            {
                                "id": "11111111-2222-4333-8444-555555555555",
                                "email": SECRET,
                                "enable": True,
                                "flow": "xtls-rprx-vision",
                            }
                        ],
                    },
                    "streamSettings": {
                        "network": "tcp",
                        "security": "reality",
                        "realitySettings": {
                            "serverNames": ["example.test"],
                            "shortIds": ["abcd"],
                            "privateKey": SECRET,
                            "settings": {
                                "publicKey": PUBLIC_KEY,
                                "fingerprint": "chrome",
                                "serverName": "",
                            },
                        },
                    },
                }
            ],
        },
        "events": [],
        "mutations": [],
        "listener": True,
        "database": database,
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def _respond(self, body):
            encoded = json.dumps(body, separators=(",", ":")).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            assert self.headers["Authorization"] == "Bearer " + SECRET
            state["events"].append(("GET", self.path))
            body = {
                "/secret-base/panel/api/server/status": state["status"],
                "/secret-base/panel/api/inbounds/list": state["inbounds"],
            }[self.path]
            self._respond(body)

        def do_POST(self):
            state["mutations"].append(self.path)
            self._respond({"success": False, "obj": None})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.01},
    )
    thread.start()
    state["panel_url"] = f"http://127.0.0.1:{server.server_port}/secret-base/"
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def request_bytes(target: VpnEndpointTarget) -> bytes:
    now_ms = time_ns() // 1_000_000
    return serialize_node_health_request(
        VpnNodeHealthRequest(target.worker_id, target, now_ms),
        now_ms=now_ms,
    )


def install_trusted_inputs(monkeypatch, entrypoint, node, tmp_path):
    config = tmp_path / "node.json"
    token = tmp_path / "api-token"
    values = {
        config: json.dumps(
            {
                "version": 1,
                "panel_url": node["panel_url"],
                "database_path": str(node["database"]),
            },
            separators=(",", ":"),
        ).encode(),
        token: SECRET.encode(),
    }
    checked = []

    def read(path, *, limit):
        checked.append((path, limit))
        return values[path]

    monkeypatch.setattr(entrypoint, "_read_private_file", read)
    monkeypatch.setattr(
        entrypoint,
        "_validate_private_regular_file",
        lambda path: checked.append((path, "database")),
    )
    return config, token, checked


def run(
    entrypoint,
    raw,
    *,
    config,
    token,
    listener=lambda _port: True,
    uid=lambda: 0,
    observer=None,
):
    stdout = io.BytesIO()
    stderr = io.BytesIO()
    kwargs = {}
    if observer is not None:
        kwargs["observer"] = observer
    code = entrypoint.run_node_health_entrypoint(
        io.BytesIO(raw),
        stdout,
        stderr,
        config_path=config,
        token_path=token,
        effective_uid=uid,
        listener_probe=listener,
        **kwargs,
    )
    return code, stdout.getvalue(), stderr.getvalue()


def test_endpoint_observation_is_immutable_and_does_not_read_clients(
    observation, node, target
):
    calls = []

    class Panel:
        def request(self, method, route):
            calls.append((method, route))
            return {
                "panel/api/server/status": node["status"],
                "panel/api/inbounds/list": node["inbounds"],
            }[route]

    result = observation.observe_node_endpoint(
        Panel(),
        target=target,
        database_path=node["database"],
        listener_probe=lambda port: port == 443,
    )

    assert asdict(result) == {
        "state": "matched",
        "transport": "matched",
        "runtime": "running",
        "listening": True,
    }
    assert not hasattr(result, "__dict__")
    with pytest.raises(FrozenInstanceError):
        result.listening = False
    assert calls == [
        ("GET", "panel/api/server/status"),
        ("GET", "panel/api/inbounds/list"),
    ]
    assert SECRET not in repr(result)


@pytest.mark.parametrize(
    "case,error_code,runtime",
    [
        ("healthy", None, "running"),
        ("missing", "vpn_node_health_endpoint_missing", "running"),
        ("stopped", "vpn_node_health_runtime_stopped", "stopped"),
        ("transport", "vpn_node_health_endpoint_mismatch", "running"),
        ("protocol", "vpn_node_health_endpoint_mismatch", "running"),
        ("host", "vpn_node_health_endpoint_mismatch", "running"),
        ("listener", "vpn_node_health_listener_unavailable", "running"),
    ],
)
def test_exact_health_receipt_and_read_only_state(
    monkeypatch, entrypoint, node, target, tmp_path, case, error_code, runtime
):
    if case == "missing":
        node["inbounds"]["obj"] = []
        with sqlite3.connect(node["database"]) as connection:
            connection.execute("DELETE FROM inbounds")
    elif case == "stopped":
        node["status"]["obj"]["xray"]["state"] = "stop"
    elif case == "transport":
        node["inbounds"]["obj"][0]["streamSettings"]["network"] = "ws"
    elif case == "protocol":
        node["inbounds"]["obj"][0]["protocol"] = "trojan"
    elif case == "host":
        node["inbounds"]["obj"][0]["listen"] = "127.0.0.1"
    elif case == "listener":
        node["listener"] = False

    config, token, checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )
    database_hash = hashlib.sha256(node["database"].read_bytes()).digest()
    panel_payload = deepcopy([node["status"], node["inbounds"]])
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
        listener=lambda port: port == target.port and node["listener"],
    )
    expected = VpnNodeHealthReceipt(
        "healthy" if error_code is None else "unhealthy",
        error_code,
        runtime,
    )

    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(expected),
        b"",
    )
    assert stdout.count(b"{") == stdout.count(b"}") == 1
    assert b"\n" not in stdout
    assert hashlib.sha256(node["database"].read_bytes()).digest() == database_hash
    assert [node["status"], node["inbounds"]] == panel_payload
    assert node["mutations"] == []
    assert all(method == "GET" for method, _path in node["events"])
    assert all("clients" not in path for _method, path in node["events"])
    assert checked == [
        (config, entrypoint.MAX_CONFIG_BYTES),
        (node["database"], "database"),
        (token, entrypoint.MAX_TOKEN_BYTES),
    ]
    assert SECRET.encode() not in stdout + stderr


def test_database_and_panel_failures_map_to_closed_receipts(
    monkeypatch, entrypoint, node, target, tmp_path
):
    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )
    node["database"].write_bytes(b"not-sqlite " + SECRET.encode())
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)
        ),
        b"",
    )

    class UnavailablePanel:
        def __init__(self, *_args, **_kwargs):
            from app.services.vpn_xui_node_http import NodePanelError

            raise NodePanelError("vpn_xui_request_failed")

    monkeypatch.setattr(entrypoint, "NodePanelSession", UnavailablePanel)
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt(
                "unhealthy", "vpn_node_health_panel_unavailable", None
            )
        ),
        b"",
    )


@pytest.mark.parametrize("raw", [b"", b"[]", b"{", b'{"version":1}\n'])
def test_malformed_request_is_static_and_performs_no_privileged_io(
    monkeypatch, entrypoint, tmp_path, raw
):
    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    code, stdout, stderr = run(
        entrypoint,
        raw,
        config=tmp_path / "node.json",
        token=tmp_path / "token",
        uid=lambda: pytest.fail("uid checked"),
    )
    assert code == entrypoint.EXIT_INVALID_REQUEST
    assert stdout == stderr == b""
    assert touched == []


def test_request_is_bounded_to_health_protocol_limit(
    monkeypatch, entrypoint, tmp_path
):
    touched = []
    monkeypatch.setattr(
        entrypoint,
        "_read_private_file",
        lambda *args, **kwargs: touched.append((args, kwargs)),
    )
    code, stdout, stderr = run(
        entrypoint,
        b" " * (entrypoint.MAX_HEALTH_PAYLOAD_BYTES + 1),
        config=tmp_path / "node.json",
        token=tmp_path / "token",
    )
    assert code == entrypoint.EXIT_INVALID_REQUEST
    assert stdout == stderr == b""
    assert touched == []


@pytest.mark.parametrize(
    "failure",
    ["non_root", "config_file", "config_payload", "database", "token_file", "token_decode"],
)
def test_valid_request_maps_trusted_input_failures_to_one_static_receipt(
    monkeypatch, entrypoint, node, target, tmp_path, failure
):
    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )
    uid = lambda: 1000 if failure == "non_root" else 0

    if failure == "config_file":
        monkeypatch.setattr(
            entrypoint,
            "_read_private_file",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                entrypoint.NodeEntrypointError()
            ),
        )
    elif failure == "config_payload":
        monkeypatch.setattr(
            entrypoint,
            "_read_private_file",
            lambda path, *, limit: (
                json.dumps(
                    {
                        "version": 1,
                        "panel_url": node["panel_url"],
                        "database_path": f"relative/{SECRET}",
                    },
                    separators=(",", ":"),
                ).encode()
                if path == config
                else SECRET.encode()
            ),
        )
    elif failure == "database":
        monkeypatch.setattr(
            entrypoint,
            "_validate_private_regular_file",
            lambda path: (_ for _ in ()).throw(RuntimeError(f"{path}:{SECRET}")),
        )
    elif failure == "token_file":
        original_read = entrypoint._read_private_file

        def reject_token(path, *, limit):
            if path == token:
                raise entrypoint.NodeEntrypointError()
            return original_read(path, limit=limit)

        monkeypatch.setattr(entrypoint, "_read_private_file", reject_token)
    elif failure == "token_decode":
        original_read = entrypoint._read_private_file

        def invalid_token(path, *, limit):
            if path == token:
                return b"\xff" + SECRET.encode()
            return original_read(path, limit=limit)

        monkeypatch.setattr(entrypoint, "_read_private_file", invalid_token)

    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
        uid=uid,
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)
        ),
        b"",
    )
    assert SECRET.encode() not in stdout + stderr
    assert str(config).encode() not in stdout + stderr
    assert str(token).encode() not in stdout + stderr
    assert str(node["database"]).encode() not in stdout + stderr


def test_valid_request_process_interruption_remains_silent(
    monkeypatch, entrypoint, node, target, tmp_path
):
    class ProcessInterruption(BaseException):
        pass

    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )

    def interrupt(*_args, **_kwargs):
        raise ProcessInterruption(SECRET)

    monkeypatch.setattr(entrypoint, "_read_private_file", interrupt)
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
    )
    assert code == entrypoint.EXIT_INTERRUPTED
    assert stdout == stderr == b""


def test_unexpected_operational_failure_is_one_static_receipt(
    monkeypatch, entrypoint, node, target, tmp_path
):
    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )

    def fail(*_args, **_kwargs):
        raise RuntimeError(SECRET)

    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
        observer=fail,
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)
        ),
        b"",
    )
    assert SECRET.encode() not in stdout + stderr


@pytest.mark.parametrize(
    "observation",
    [
        ("matched", "matched", "unknown", True),
        ("matched", "matched", "running", 1),
    ],
)
def test_inexact_observer_result_fails_closed(
    monkeypatch, entrypoint, node, target, tmp_path, observation
):
    from app.services.vpn_xui_node_observation import NodeEndpointObservation

    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
        observer=lambda *_args, **_kwargs: NodeEndpointObservation(*observation),
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)
        ),
        b"",
    )


def test_listener_probe_requires_an_exact_boolean(
    monkeypatch, entrypoint, node, target, tmp_path
):
    config, token, _checked = install_trusted_inputs(
        monkeypatch, entrypoint, node, tmp_path
    )
    code, stdout, stderr = run(
        entrypoint,
        request_bytes(target),
        config=config,
        token=token,
        listener=lambda _port: 1,
    )
    assert (code, stdout, stderr) == (
        0,
        serialize_node_health_receipt(
            VpnNodeHealthReceipt("unhealthy", "vpn_node_health_internal", None)
        ),
        b"",
    )


@pytest.mark.parametrize(
    "name,body",
    [
        (
            "tcp",
            (
                "  sl  local_address rem_address   st\n"
                "   0: 00000000:01BB 00000000:0000 0A\n"
            ),
        ),
        (
            "tcp6",
            (
                "  sl  local_address                         remote_address                        st\n"
                "   0: 00000000000000000000000000000000:01BB "
                "00000000000000000000000000000000:0000 0A\n"
            ),
        ),
    ],
)
def test_listener_probe_supports_ipv4_and_ipv6(entrypoint, tmp_path, name, body):
    table = tmp_path / name
    table.write_text(body, encoding="ascii")
    assert entrypoint.public_port_is_listening(443, tables=(table,)) is True
    assert entrypoint.public_port_is_listening(8443, tables=(table,)) is False
