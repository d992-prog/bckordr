from __future__ import annotations

import json
import http.client
import logging
import _thread
import socket
import sqlite3
import ssl
import threading
import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from traceback import format_exception
from urllib.parse import parse_qs
from uuid import UUID

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.services.vpn_portal_logging import install_safe_logging
from app.services.vpn_endpoint_types import VpnEndpointTarget
from app.services.vpn_xui_node_http import (
    NodePanelError,
    NodePanelSession,
    validate_credentials,
    validate_panel_url,
    validate_request,
    validate_timeout,
)
from app.services.vpn_xui_node_observation import observe_node_client


SECRET = "synthetic-node-panel-secret"
MAX_BODY = 2 * 1024 * 1024


class PanelState:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []
        self.mode = "ok"
        self.inventory_status = 200
        self.inventory_body = b'{"success":true,"obj":[{"id":7}]}'
        self.inventory_headers: dict[str, str] = {}
        self.token_status = 200
        self.token_status_path = "/panel/api/server/status"
        self.token_body = b'{"success":true,"obj":{"panelVersion":"3.8.5"}}'
        self.token_headers: dict[str, str] = {}
        self.route_bodies: dict[str, bytes] = {}
        self.trickle_delay = 0.0
        self.requests = 0
        self.peer_closed = threading.Event()


class PanelHandler(BaseHTTPRequestHandler):
    server: "PanelServer"
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _record(self, body: bytes) -> None:
        state = self.server.state
        state.requests += 1
        state.events.append(
            {
                "method": self.command,
                "path": self.path,
                "host": self.headers.get("Host"),
                "cookie": self.headers.get("Cookie"),
                "csrf": self.headers.get("X-CSRF-Token"),
                "authorization": self.headers.get("Authorization"),
                "content_type": self.headers.get("Content-Type"),
                "body": body,
            }
        )

    def _send(
        self,
        status: int,
        body: bytes,
        *,
        headers: dict[str, str] | None = None,
        declared_length: int | None = None,
    ) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(declared_length if declared_length is not None else len(body)))
        self.end_headers()
        if self.server.state.trickle_delay:
            for byte in body:
                try:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(self.server.state.trickle_delay)
        else:
            self.wfile.write(body)

    def _dispatch(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self._record(body)
        state = self.server.state
        path = self.path

        if path == "/csrf-token":
            csrf_count = sum(event["path"] == path for event in state.events)
            if state.mode == "first_csrf_fail" and csrf_count == 1:
                self._send(200, b'{"success":true,"obj":""}')
                return
            if state.mode == "second_csrf_fail" and csrf_count == 2:
                self._send(200, b'{"success":false,"msg":"' + SECRET.encode() + b'"}')
                return
            token = b"pre-login-token" if csrf_count == 1 else b"fresh-token"
            headers = {"Set-Cookie": "sid=abc; Path=/; HttpOnly"} if csrf_count == 1 else {}
            if state.mode == "delete_cookie" and csrf_count == 2:
                headers = {"Set-Cookie": "sid=gone; Max-Age=0; Path=/"}
            if state.mode == "malformed_cookie" and csrf_count == 1:
                headers = {"Set-Cookie": 'sid="unterminated'}
            self._send(200, b'{"success":true,"obj":"' + token + b'"}', headers=headers)
            return

        if path == "/login":
            parsed = parse_qs(body.decode("ascii"), strict_parsing=True)
            valid = (
                parsed == {"username": ["node-user"], "password": [SECRET]}
                and self.headers.get("X-CSRF-Token") == "pre-login-token"
                and self.headers.get("Cookie") == "sid=abc"
            )
            if state.mode == "login_fail" or not valid:
                self._send(200, b'{"success":false,"msg":"' + SECRET.encode() + b'"}')
            else:
                self._send(200, b'{"success":true,"obj":null}')
            return

        if path == "/logout":
            if state.mode == "logout_stall":
                time.sleep(0.3)
            status = 500 if state.mode == "logout_status" else 200
            self._send(status, b'{"success":true,"obj":null}')
            return

        if path == state.token_status_path and self.headers.get("Authorization"):
            self._send(
                state.token_status,
                state.token_body,
                headers=state.token_headers,
            )
            return

        if path == "/panel/api/inbounds/list":
            if state.mode in {"custom_length", "unframed_stall"}:
                self.send_response(200)
                self.send_header("Connection", "close")
                for name, value in state.inventory_headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(state.inventory_body)
                self.wfile.flush()
                if state.mode == "unframed_stall":
                    time.sleep(0.5)
                return
            if state.mode == "error_wait_for_close":
                self.send_response(500)
                self.send_header("Connection", "close")
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.flush()
                self.connection.settimeout(1)
                try:
                    if not self.connection.recv(1):
                        state.peer_closed.set()
                except OSError:
                    pass
                return
            if state.mode == "trickle_headers":
                wire = (
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                    + b"Content-Length: "
                    + str(len(state.inventory_body)).encode("ascii")
                    + b"\r\n\r\n"
                    + state.inventory_body
                )
                for byte in wire:
                    try:
                        self.connection.sendall(bytes([byte]))
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        break
                    time.sleep(state.trickle_delay)
                self.close_connection = True
                return
            if state.mode == "redirect":
                self._send(302, b"", headers={"Location": "/panel/api/inbounds/list"})
                return
            if state.mode == "truncate":
                self._send(200, b'{"success":true', declared_length=100)
                self.close_connection = True
                return
            if state.mode == "stall":
                time.sleep(0.3)
            self._send(
                state.inventory_status,
                state.inventory_body,
                headers=state.inventory_headers,
            )
            return

        if path in {
            "/panel/api/clients/list",
            "/panel/api/server/status",
            "/panel/api/setting/all",
            "/panel/api/clients/add",
            "/panel/api/clients/bulkEnable",
            "/panel/api/clients/bulkDisable",
            "/panel/api/server/restartXrayService",
        } or path.startswith("/panel/api/clients/get/") or path.startswith(
            "/panel/api/clients/update/"
        ):
            if state.mode == "mutation_invalid":
                self._send(200, b"not-json")
            elif state.mode == "mutation_pending":
                self._send(200, b'{"success":true,"nodePending":true}')
            elif state.mode == "mutation_status":
                self._send(500, b'{"success":false}')
            elif state.mode == "mutation_bad_cookie":
                self._send(
                    200,
                    b'{"success":true,"obj":null}',
                    headers={"Set-Cookie": 'sid="unterminated'},
                )
            else:
                self._send(
                    200,
                    state.route_bodies.get(
                        path, b'{"success":true,"obj":{"ok":true}}'
                    ),
                    headers=state.inventory_headers,
                )
            return

        self._send(404, b'{"success":false}')

    do_GET = _dispatch
    do_POST = _dispatch


class PanelServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, state: PanelState) -> None:
        super().__init__(("127.0.0.1", 0), PanelHandler)
        self.state = state


@contextmanager
def running_panel(state: PanelState | None = None):
    panel_state = state or PanelState()
    server = PanelServer(panel_state)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        yield panel_state, f"http://panel.example:{port}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def make_session(url: str, **kwargs: object) -> NodePanelSession:
    return NodePanelSession(url, "node-user", SECRET, **kwargs)


def assert_error(code: str, call, *, uncertain: bool = False) -> NodePanelError:
    with pytest.raises(NodePanelError) as caught:
        call()
    assert caught.value.code == code
    assert caught.value.mutation_uncertain is uncertain
    rendered = "".join(format_exception(caught.value))
    assert SECRET not in rendered
    assert str(caught.value) == code
    assert SECRET not in repr(caught.value)
    return caught.value


@pytest.mark.parametrize(
    "url",
    [
        "ftp://panel.example/",
        "http://user@panel.example/",
        "http://panel.example:0/",
        "http://panel.example:65536/",
        "http://panel.example/a//b/",
        "http://panel.example/a/../b/",
        "http://panel.example/a%2fb/",
        "http://panel.example/base",
        "http://panel.example/?query=1",
        "http://panel.example/#fragment",
        "http://panel.example/?",
        "http://panel.example/#",
        "http://panel.example:/",
        "http://panel.example/has space/",
    ],
)
def test_panel_url_validation_rejects_noncanonical_values(url: str) -> None:
    assert_error("vpn_xui_connection_invalid", lambda: validate_panel_url(url))


@pytest.mark.parametrize("value", ["", None, 7])
def test_credentials_are_nonempty_strings(value: object) -> None:
    assert_error(
        "vpn_xui_connection_invalid",
        lambda: validate_credentials(value, SECRET),
    )


@pytest.mark.parametrize(
    "value", [True, 0, -1, 61, 10**400, -(10**400), float("nan"), float("inf"), "2"]
)
def test_timeout_is_finite_bounded_number(value: object) -> None:
    assert_error("vpn_xui_connection_invalid", lambda: validate_timeout(value))


def test_construction_has_no_io_and_repr_is_secret_free() -> None:
    session = NodePanelSession("https://panel.example:9443/base/", "node-user", SECRET)
    assert repr(session) == "NodePanelSession()"
    assert SECRET not in repr(session)
    assert validate_credentials(" user\t", " password with spaces ") == (
        " user\t",
        " password with spaces ",
    )


@pytest.mark.parametrize(
    ("username", "password", "api_token"),
    [
        (None, None, None),
        ("node-user", None, None),
        (None, SECRET, None),
        ("node-user", SECRET, "token"),
        (None, None, ""),
        (None, None, " token"),
        (None, None, "token "),
        (None, None, "token\nvalue"),
        (None, None, "token=value.more"),
        (None, None, "tøken"),
        (None, None, "a" * 4097),
        (None, None, 7),
    ],
)
def test_authentication_mode_and_api_token_are_validated_before_io(
    monkeypatch, username: object, password: object, api_token: object
) -> None:
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: pytest.fail("I/O"))
    assert_error(
        "vpn_xui_connection_invalid",
        lambda: NodePanelSession(
            "http://panel.example/",
            username,
            password,
            api_token=api_token,
        ),
    )


@pytest.mark.parametrize(
    "token",
    ["a", "A9._~+/-", "header.payload.signature", "base64+/==", "a" * 4096],
)
def test_api_token_mode_accepts_only_the_bounded_bearer_grammar(token: str) -> None:
    session = NodePanelSession("http://panel.example/", api_token=token)
    assert repr(session) == "NodePanelSession()"


@pytest.mark.parametrize(
    ("method", "route", "body", "mutation"),
    [
        ("PUT", "panel/api/inbounds/list", None, False),
        ([], "panel/api/inbounds/list", None, False),
        ("GET", "/panel/api/inbounds/list", None, False),
        ("GET", "//evil.example/path", None, False),
        ("GET", "panel/api/inbounds//list", None, False),
        ("GET", "panel/api/inbounds/../list", None, False),
        ("GET", "panel/api/inbounds\\list", None, False),
        ("GET", "panel/api/clients/get/a%2Fb", None, False),
        ("GET", "panel/api/clients/get/a%252Fb", None, False),
        ("GET", "panel/api/clients/get/a%", None, False),
        ("GET", "panel/api/clients/get/\ud800", None, False),
        ("GET", "panel/api/inbounds/list?", None, False),
        ("GET", "panel/api/clients/get/a?x=1", None, False),
        ("GET", "panel/api/csrf-token", None, False),
        ("GET", "panel/api/inbounds/list", {}, False),
        ("GET", "panel/api/inbounds/list", None, True),
        ("POST", "panel/api/setting/all", {}, True),
        ("POST", "panel/api/clients/add", {}, False),
        ("POST", "panel/api/clients/update/a?inboundIds=0", {}, True),
        ("POST", "panel/api/clients/update/a?inboundIds=01", {}, True),
        ("POST", "panel/api/clients/update/a?inboundIds=1&x=2", {}, True),
        ("POST", "panel/api/clients/add", None, True),
        ("POST", "panel/api/clients/add", [], True),
        ("POST", "panel/api/clients/add", {"value": float("nan")}, True),
        ("POST", "panel/api/clients/add", {}, 1),
    ],
)
def test_request_validation_rejects_before_network(
    method: object,
    route: object,
    body: object,
    mutation: object,
) -> None:
    assert_error(
        "vpn_xui_request_invalid",
        lambda: validate_request(method, route, body=body, mutation=mutation),
    )


@pytest.mark.parametrize(
    ("method", "route", "body", "mutation"),
    [
        ("GET", "panel/api/inbounds/list", None, False),
        ("GET", "panel/api/clients/list", None, False),
        ("GET", "panel/api/server/status", None, False),
        ("GET", "panel/api/clients/get/user%40example.test", None, False),
        ("POST", "panel/api/setting/all", {}, False),
        ("POST", "panel/api/clients/add", {"id": 1}, True),
        ("POST", "panel/api/clients/bulkEnable", {}, True),
        ("POST", "panel/api/clients/bulkDisable", {}, True),
        ("POST", "panel/api/clients/update/user%40example.test?inboundIds=12", {}, True),
        ("POST", "panel/api/server/restartXrayService", {}, True),
    ],
)
def test_allowlisted_requests_use_authenticated_wire_contract(
    method: str,
    route: str,
    body: dict | None,
    mutation: bool,
) -> None:
    with running_panel() as (state, url), make_session(url) as session:
        result = session.request(method, route, body=body, mutation=mutation)
        assert result["success"] is True
        event = state.events[-1]
        assert event["method"] == method
        assert event["path"] == "/" + route
        assert event["host"] == f"panel.example:{url.rsplit(':', 1)[1][:-1]}"
        assert event["csrf"] == "fresh-token"
        assert event["cookie"] == "sid=abc"
        assert event["authorization"] is None
        if method == "POST":
            assert event["content_type"] == "application/json"
            assert json.loads(event["body"]) == body


def test_context_authenticates_rotates_csrf_and_logs_out() -> None:
    with running_panel() as (state, url):
        session = make_session(url)
        with session:
            payload = session.request("GET", "panel/api/inbounds/list")
            assert payload == {"success": True, "obj": [{"id": 7}]}

        assert [event["path"] for event in state.events] == [
            "/csrf-token",
            "/login",
            "/csrf-token",
            "/panel/api/inbounds/list",
            "/logout",
        ]
        assert state.events[-1]["csrf"] == "fresh-token"
        assert_error(
            "vpn_xui_session_not_authenticated",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )
        assert_error("vpn_xui_session_not_authenticated", session.__enter__)


def test_token_context_uses_one_status_probe_and_authorization_only() -> None:
    token = "synthetic-token.secret_+/=="
    state = PanelState()
    state.token_headers = {"Set-Cookie": 'sid="unterminated'}
    state.inventory_headers = {"Set-Cookie": 'other="unterminated'}
    with running_panel(state) as (state, url):
        session = NodePanelSession(url, api_token=token)
        with session:
            session.request("GET", "panel/api/inbounds/list")
            session.request("POST", "panel/api/setting/all", body={})
            session.request("POST", "panel/api/clients/add", body={}, mutation=True)

        assert [event["path"] for event in state.events] == [
            "/panel/api/server/status",
            "/panel/api/inbounds/list",
            "/panel/api/setting/all",
            "/panel/api/clients/add",
        ]
        assert all(
            event["authorization"] == f"Bearer {token}"
            and event["cookie"] is None
            and event["csrf"] is None
            for event in state.events
        )
        assert state.events[0]["body"] == b""
        assert state.events[0]["content_type"] is None
        assert session._api_token is None
        assert_error(
            "vpn_xui_session_not_authenticated",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )
        assert_error("vpn_xui_session_not_authenticated", session.__enter__)


def test_token_entry_status_probe_uses_the_validated_panel_base_path() -> None:
    state = PanelState()
    state.token_status_path = "/base/panel/api/server/status"
    with running_panel(state) as (state, url):
        with NodePanelSession(url + "base/", api_token="synthetic-base-token"):
            pass
        assert [event["path"] for event in state.events] == [
            "/base/panel/api/server/status"
        ]


def test_token_session_composes_with_read_only_node_observation(tmp_path) -> None:
    client_uuid = UUID("11111111-2222-4333-8444-555555555555")
    email = "synthetic-token-profile"
    database = tmp_path / "panel.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE inbounds (id)")
        connection.execute("INSERT INTO inbounds VALUES (7)")

    state = PanelState()
    state.token_body = json.dumps(
        {
            "success": True,
            "obj": {
                "panelVersion": "3.8.5",
                "xray": {"state": "running", "errorMsg": ""},
            },
        }
    ).encode()
    state.inventory_body = json.dumps(
        {
            "success": True,
            "obj": [
                {
                    "id": 7,
                    "protocol": "vless",
                    "enable": True,
                    "port": 443,
                    "settings": {
                        "decryption": "none",
                        "clients": [
                            {
                                "id": str(client_uuid),
                                "email": email,
                                "enable": True,
                            }
                        ],
                    },
                    "streamSettings": {"network": "raw", "security": "none"},
                }
            ],
        }
    ).encode()
    state.route_bodies["/panel/api/clients/list"] = json.dumps(
        {
            "success": True,
            "obj": [
                {
                    "id": 19,
                    "uuid": str(client_uuid),
                    "email": email,
                    "enable": True,
                    "inboundIds": [7],
                }
            ],
        }
    ).encode()
    state.token_headers = state.inventory_headers = {
        "Set-Cookie": 'ignored="unterminated'
    }
    token = "synthetic-observer-token"
    with running_panel(state) as (state, url):
        with NodePanelSession(url, api_token=token) as session:
            result = observe_node_client(
                session,
                target=VpnEndpointTarget(
                    1,
                    1,
                    7,
                    "vpn.example.test",
                    443,
                    "vless",
                    "raw",
                    "none",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                client_uuid=client_uuid,
                client_email=email,
                database_path=database,
            )
        assert (
            result.state,
            result.record_id,
            result.enabled,
            result.transport,
            result.runtime,
        ) == ("matched", 19, True, "matched", "running")
        assert [event["path"] for event in state.events] == [
            "/panel/api/server/status",
            "/panel/api/server/status",
            "/panel/api/inbounds/list",
            "/panel/api/clients/list",
        ]
        assert all(
            event["authorization"] == f"Bearer {token}"
            and event["cookie"] is None
            and event["csrf"] is None
            for event in state.events
        )


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (401, b'{"success":false}'),
        (403, b'{"success":false}'),
        (200, b'{"success":false}'),
        (200, b"not-json"),
    ],
)
def test_token_entry_rejection_is_static_without_fallback_or_retry(
    status: int, body: bytes
) -> None:
    token = "synthetic-rejected-token"
    state = PanelState()
    state.token_status = status
    state.token_body = body
    with running_panel(state) as (state, url):
        session = NodePanelSession(url, api_token=token)
        error = assert_error("vpn_xui_auth_failed", session.__enter__)
        assert [event["path"] for event in state.events] == [
            "/panel/api/server/status"
        ]
        assert state.events[0]["authorization"] == f"Bearer {token}"
        assert token not in str(error) + repr(error) + "".join(format_exception(error))
        assert session._api_token is None
        assert_error("vpn_xui_session_not_authenticated", session.__enter__)


def test_token_entry_preserves_transport_failure_and_clears_secret() -> None:
    with running_panel() as (_state, url):
        port = int(url.rsplit(":", 1)[1][:-1])
    token = "synthetic-transport-token"
    session = NodePanelSession(
        f"http://panel.example:{port}/",
        api_token=token,
        timeout_seconds=0.1,
    )
    error = assert_error("vpn_xui_request_failed", session.__enter__)
    assert token not in str(error) + repr(error) + "".join(format_exception(error))
    assert session._api_token is None


@pytest.mark.parametrize(
    ("mode", "code"),
    [
        ("mutation_status", "vpn_xui_http_failed"),
        ("mutation_invalid", "vpn_xui_response_invalid"),
    ],
)
def test_token_post_failures_preserve_read_certainty_and_mutation_uncertainty(
    mode: str, code: str
) -> None:
    token = "synthetic-post-token"
    with running_panel() as (state, url):
        with NodePanelSession(url, api_token=token) as session:
            state.mode = mode
            assert_error(
                code,
                lambda: session.request("POST", "panel/api/setting/all", body={}),
            )
            assert_error(
                code,
                lambda: session.request(
                    "POST", "panel/api/clients/add", body={}, mutation=True
                ),
                uncertain=True,
            )
        assert [event["path"] for event in state.events] == [
            "/panel/api/server/status",
            "/panel/api/setting/all",
            "/panel/api/clients/add",
        ]
        assert all(event["authorization"] == f"Bearer {token}" for event in state.events)


def test_token_caller_interrupt_propagates_after_local_cleanup() -> None:
    token = "synthetic-caller-interrupt-token"
    interruption = KeyboardInterrupt()
    with running_panel() as (state, url):
        session = NodePanelSession(url, api_token=token)
        with pytest.raises(KeyboardInterrupt) as caught:
            with session:
                raise interruption
        assert caught.value is interruption
        assert session._api_token is None
        assert [event["path"] for event in state.events] == [
            "/panel/api/server/status"
        ]


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_token_entry_interrupt_clears_secret_and_does_not_fallback(
    monkeypatch, interruption_type
) -> None:
    token = "synthetic-interrupted-token"
    interruption = interruption_type()

    def interrupt(_connection):
        raise interruption

    with running_panel() as (state, url):
        session = NodePanelSession(url, api_token=token)
        monkeypatch.setattr(http.client.HTTPConnection, "getresponse", interrupt)
        with pytest.raises(interruption_type) as caught:
            session.__enter__()
        assert caught.value is interruption
        assert session._api_token is None
        assert all(
            event["path"] not in {"/csrf-token", "/login", "/logout"}
            for event in state.events
        )


@pytest.mark.parametrize(
    ("mode", "expected_paths"),
    [
        ("first_csrf_fail", ["/csrf-token"]),
        ("login_fail", ["/csrf-token", "/login"]),
        (
            "second_csrf_fail",
            [
                "/csrf-token",
                "/login",
                "/csrf-token",
                "/logout",
            ],
        ),
        ("malformed_cookie", ["/csrf-token"]),
    ],
)
def test_auth_failures_are_static_and_cleanup_login_when_possible(
    mode: str,
    expected_paths: list[str],
) -> None:
    state = PanelState()
    state.mode = mode
    with running_panel(state) as (state, url):
        session = make_session(url)
        assert_error("vpn_xui_auth_failed", session.__enter__)
        assert [event["path"] for event in state.events] == expected_paths
        assert_error(
            "vpn_xui_session_not_authenticated",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


def test_deleted_cookie_is_not_sent_after_rotation() -> None:
    state = PanelState()
    state.mode = "delete_cookie"
    with running_panel(state) as (state, url), make_session(url) as session:
        session.request("GET", "panel/api/inbounds/list")
        assert state.events[-1]["cookie"] is None


@pytest.mark.parametrize(
    "cookie",
    ['sid=abc; Max-Age="unterminated', 'sid=abc; Path="unterminated', "sid=abc; [garbage"],
)
@pytest.mark.parametrize("mutation", [False, True])
def test_cookie_validation_rejects_unparsed_suffixes(cookie: str, mutation: bool) -> None:
    with running_panel() as (state, url), make_session(url) as session:
        state.inventory_headers = {"Set-Cookie": cookie}
        method = "POST" if mutation else "GET"
        route = "panel/api/clients/add" if mutation else "panel/api/inbounds/list"
        assert_error(
            "vpn_xui_response_invalid",
            lambda: session.request(method, route, body={} if mutation else None, mutation=mutation),
            uncertain=mutation,
        )


@pytest.mark.parametrize(
    ("cookie", "expected"),
    [
        (
            "sid=rotated; Path=/; Max-Age=3600; HttpOnly; Secure; SameSite=Lax; "
            "Expires=Wed, 09 Jun 2038 10:18:14 GMT",
            "sid=rotated",
        ),
        ('sid="quoted value"; Path="/"; HttpOnly', 'sid="quoted value"'),
        (r'sid="escaped\"value"; Path=/', r'sid="escaped\"value"'),
    ],
)
def test_cookie_validation_preserves_attributes_quotes_and_escapes(
    cookie: str, expected: str
) -> None:
    with running_panel() as (state, url), make_session(url) as session:
        state.inventory_headers = {"Set-Cookie": cookie}
        session.request("GET", "panel/api/inbounds/list")
        state.inventory_headers = {}
        session.request("GET", "panel/api/inbounds/list")
        assert state.events[-1]["cookie"] == expected


def test_context_does_not_mask_caller_error_when_logout_fails() -> None:
    with running_panel() as (state, url):
        with pytest.raises(RuntimeError, match="caller failure"):
            with make_session(url):
                state.mode = "logout_status"
                raise RuntimeError("caller failure")


def test_fresh_keyboard_interrupt_during_normal_logout_propagates_after_cleanup() -> None:
    state = PanelState()
    with running_panel(state) as (_state, url):
        session = make_session(url)
        session.__enter__()
        state.mode = "logout_stall"
        timer = threading.Timer(0.05, _thread.interrupt_main)
        timer.start()
        try:
            with pytest.raises(KeyboardInterrupt):
                session.__exit__(None, None, None)
        finally:
            timer.cancel()
            timer.join()
        assert_error(
            "vpn_xui_session_not_authenticated",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (b"<html>" + SECRET.encode(), "vpn_xui_response_invalid"),
        (b"\xff", "vpn_xui_response_invalid"),
        (b'{"success":true,"success":true}', "vpn_xui_response_invalid"),
        (b'{"success":true,"obj":NaN}', "vpn_xui_response_invalid"),
        (b'{"success":true,"obj":1e999}', "vpn_xui_response_invalid"),
        (b'{"success":false,"msg":"' + SECRET.encode() + b'"}', "vpn_xui_response_invalid"),
        (b'{"success":1}', "vpn_xui_response_invalid"),
    ],
)
def test_invalid_responses_are_rejected_without_secret_leak(body: bytes, code: str) -> None:
    state = PanelState()
    state.inventory_body = body
    with running_panel(state) as (_state, url), make_session(url) as session:
        assert_error(code, lambda: session.request("GET", "panel/api/inbounds/list"))


@pytest.mark.parametrize(
    "body",
    [
        b'{"success":true,"nodePending":true}',
        b'{"success":true,"nodePending":null}',
        b'{"success":true,"obj":{"nodePending":true}}',
    ],
)
def test_pending_responses_have_static_error(body: bytes) -> None:
    state = PanelState()
    state.inventory_body = body
    with running_panel(state) as (_state, url), make_session(url) as session:
        assert_error(
            "vpn_xui_apply_pending",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


def test_redirect_is_not_followed_and_request_is_not_retried() -> None:
    state = PanelState()
    state.mode = "redirect"
    with running_panel(state) as (state, url), make_session(url) as session:
        before = state.requests
        assert_error(
            "vpn_xui_http_failed",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )
        assert state.requests == before + 1


def test_unread_error_response_closes_even_while_exception_is_retained() -> None:
    with running_panel() as (state, url), make_session(url) as session:
        state.mode = "error_wait_for_close"
        error = assert_error(
            "vpn_xui_http_failed",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )
        assert error.__traceback__ is not None
        assert state.peer_closed.wait(0.5)


@pytest.mark.parametrize("mode", ["truncate", "stall"])
def test_truncated_or_stalled_response_fails_and_connection_closes(mode: str) -> None:
    state = PanelState()
    state.mode = mode
    timeout = 0.1 if mode == "stall" else 1.0
    with running_panel(state) as (_state, url), make_session(url, timeout_seconds=timeout) as session:
        started = time.monotonic()
        expected = "vpn_xui_request_failed" if mode == "stall" else "vpn_xui_response_invalid"
        assert_error(expected, lambda: session.request("GET", "panel/api/inbounds/list"))
        assert time.monotonic() - started < 0.8


def test_response_size_is_bounded() -> None:
    state = PanelState()
    state.inventory_body = b" " * (MAX_BODY + 1)
    with running_panel(state) as (_state, url), make_session(url) as session:
        assert_error(
            "vpn_xui_response_invalid",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


@pytest.mark.parametrize("length", ["\u00b2", "1" * 5000], ids=["unicode-digit", "oversized-integer"])
def test_malformed_content_length_has_static_error(length: str) -> None:
    with running_panel() as (state, url), make_session(url) as session:
        state.mode = "custom_length"
        state.inventory_headers = {"Content-Length": length}
        assert_error(
            "vpn_xui_response_invalid",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


def test_deadline_does_not_accept_valid_unframed_json_when_peer_stalls() -> None:
    with running_panel() as (state, url), make_session(url, timeout_seconds=0.12) as session:
        state.mode = "unframed_stall"
        assert_error(
            "vpn_xui_request_failed",
            lambda: session.request("GET", "panel/api/inbounds/list"),
        )


def test_deadline_spans_trickled_body_not_each_socket_read() -> None:
    state = PanelState()
    state.inventory_body = b'{"success":true,"obj":"abcdefghijklmnop"}'
    with running_panel(state) as (_state, url):
        with make_session(url, timeout_seconds=0.12) as session:
            state.trickle_delay = 0.02
            started = time.monotonic()
            assert_error(
                "vpn_xui_request_failed",
                lambda: session.request("GET", "panel/api/inbounds/list"),
            )
            assert time.monotonic() - started < 0.7
            state.trickle_delay = 0


def test_deadline_spans_trickled_headers() -> None:
    state = PanelState()
    with running_panel(state) as (_state, url):
        with make_session(url, timeout_seconds=0.12) as session:
            state.mode = "trickle_headers"
            state.trickle_delay = 0.02
            started = time.monotonic()
            assert_error(
                "vpn_xui_request_failed",
                lambda: session.request("GET", "panel/api/inbounds/list"),
            )
            assert time.monotonic() - started < 0.7
            state.mode = "ok"
            state.trickle_delay = 0


def test_mutation_uncertainty_starts_only_after_request_may_be_sent() -> None:
    state = PanelState()
    with running_panel(state) as (state, url):
        session = make_session(url)
        session.__enter__()

        state.mode = "mutation_status"
        assert_error(
            "vpn_xui_http_failed",
            lambda: session.request(
                "POST", "panel/api/clients/add", body={}, mutation=True
            ),
            uncertain=True,
        )

        session.__exit__(None, None, None)
        port = int(url.rsplit(":", 1)[1][:-1])

    dead = NodePanelSession(
        f"http://panel.example:{port}/", "node-user", SECRET, timeout_seconds=0.1
    )
    # Authentication itself is never a caller mutation and therefore remains certain.
    assert_error("vpn_xui_request_failed", dead.__enter__, uncertain=False)


def test_mutation_parse_and_pending_failures_are_uncertain() -> None:
    for mode, code in [
        ("mutation_invalid", "vpn_xui_response_invalid"),
        ("mutation_pending", "vpn_xui_apply_pending"),
        ("mutation_bad_cookie", "vpn_xui_response_invalid"),
    ]:
        state = PanelState()
        with running_panel(state) as (state, url), make_session(url) as session:
            route = "panel/api/clients/add"
            state.mode = mode
            assert_error(
                code,
                lambda: session.request("POST", route, body={}, mutation=True),
                uncertain=True,
            )


@pytest.mark.parametrize("after_send", [False, True])
def test_transport_value_error_is_static_with_correct_mutation_uncertainty(
    monkeypatch, after_send: bool
) -> None:
    private_value = "synthetic-private-panel-value"

    def fail_with_private_value(*_args, **_kwargs):
        raise ValueError(private_value)

    with running_panel() as (_state, url), make_session(url) as session:
        if after_send:
            monkeypatch.setattr(http.client.HTTPConnection, "getresponse", fail_with_private_value)
        else:
            monkeypatch.setattr(socket.socket, "connect", fail_with_private_value)
        error = assert_error(
            "vpn_xui_request_failed",
            lambda: session.request("POST", "panel/api/clients/add", body={}, mutation=True),
            uncertain=after_send,
        )
        assert private_value not in str(error)
        assert private_value not in repr(error)
        assert private_value not in "".join(format_exception(error))


@pytest.mark.parametrize("close_site", ["response", "connection"])
@pytest.mark.parametrize("outcome", ["success", "http_error", "keyboard_interrupt", "system_exit"])
def test_cleanup_failures_preserve_original_error_and_close_both_resources(
    monkeypatch, close_site: str, outcome: str
) -> None:
    private_value = "synthetic-private-close-value"
    closed: list[str] = []
    response_close = http.client.HTTPResponse.close
    connection_close = http.client.HTTPConnection.close
    getresponse = http.client.HTTPConnection.getresponse
    interruption = KeyboardInterrupt() if outcome == "keyboard_interrupt" else SystemExit(9)

    def close_response(response):
        response_close(response)
        closed.append("response")
        if close_site == "response":
            raise OSError(private_value)

    def close_connection(connection):
        try:
            connection_close(connection)
        finally:
            closed.append("connection")
        if close_site == "connection":
            raise OSError(private_value)

    def interrupted_response(connection):
        getresponse(connection)
        raise interruption

    with running_panel() as (state, url), make_session(url) as session:
        if outcome == "http_error":
            state.mode = "mutation_status"
        with monkeypatch.context() as patches:
            patches.setattr(http.client.HTTPResponse, "close", close_response)
            patches.setattr(http.client.HTTPConnection, "close", close_connection)
            if outcome in {"keyboard_interrupt", "system_exit"}:
                patches.setattr(http.client.HTTPConnection, "getresponse", interrupted_response)
                with pytest.raises(type(interruption)) as caught:
                    session.request("POST", "panel/api/clients/add", body={}, mutation=True)
                assert caught.value is interruption
                error = caught.value
            else:
                error = assert_error(
                    "vpn_xui_http_failed" if outcome == "http_error" else "vpn_xui_request_failed",
                    lambda: session.request("POST", "panel/api/clients/add", body={}, mutation=True),
                    uncertain=True,
                )
            assert {"response", "connection"}.issubset(closed)
            assert private_value not in "".join(format_exception(error))


@pytest.mark.parametrize("interruption_type", [KeyboardInterrupt, SystemExit])
def test_fresh_interrupt_during_cleanup_propagates_after_both_closes(
    monkeypatch, interruption_type
) -> None:
    closed: list[str] = []
    response_close = http.client.HTTPResponse.close
    connection_close = http.client.HTTPConnection.close
    interruption = interruption_type()

    def close_response(response):
        response_close(response)
        closed.append("response")
        raise interruption

    def close_connection(connection):
        try:
            connection_close(connection)
        finally:
            closed.append("connection")

    with running_panel() as (_state, url), make_session(url) as session:
        with monkeypatch.context() as patches:
            patches.setattr(http.client.HTTPResponse, "close", close_response)
            patches.setattr(http.client.HTTPConnection, "close", close_connection)
            with pytest.raises(interruption_type) as caught:
                session.request("POST", "panel/api/clients/add", body={}, mutation=True)
            assert caught.value is interruption
            assert {"response", "connection"}.issubset(closed)


def test_request_validation_happens_before_authenticated_socket_use() -> None:
    with running_panel() as (state, url), make_session(url) as session:
        before = state.requests
        assert_error(
            "vpn_xui_request_invalid",
            lambda: session.request("POST", "panel/api/clients/add", body=None, mutation=True),
        )
        assert state.requests == before


def test_unicode_encoding_failures_are_static() -> None:
    with running_panel() as (_state, url):
        bad_credentials = NodePanelSession(url, "\ud800", SECRET)
        assert_error("vpn_xui_auth_failed", bad_credentials.__enter__)

        port = url.rsplit(":", 1)[1][:-1]
        unicode_host = NodePanelSession(
            f"http://панель.рф:{port}/", "node-user", SECRET
        )
        assert_error("vpn_xui_request_failed", unicode_host.__enter__)


def _write_self_signed_certificate(tmp_path) -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "panel.example")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("panel.example")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_path = tmp_path / "panel-cert.pem"
    key_path = tmp_path / "panel-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return str(cert_path), str(key_path)


def test_https_uses_real_certificate_verification_and_closes_listener(tmp_path) -> None:
    state = PanelState()
    server = PanelServer(state)
    cert_path, key_path = _write_self_signed_certificate(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        session = make_session(f"https://panel.example:{port}/", timeout_seconds=0.5)
        assert_error("vpn_xui_request_failed", session.__enter__)
        assert state.events == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


@pytest.mark.parametrize("hostname", ["panel.example", "wrong.example"])
def test_https_trusted_certificate_checks_original_hostname_and_sni(
    tmp_path, monkeypatch, hostname: str
) -> None:
    state = PanelState()
    server = PanelServer(state)
    cert_path, key_path = _write_self_signed_certificate(tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    received_names: list[str | None] = []
    server_context.set_servername_callback(
        lambda _socket, server_name, _context: received_names.append(server_name)
    )
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    client_context = ssl.create_default_context(cafile=cert_path)
    monkeypatch.setattr(ssl, "create_default_context", lambda: client_context)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        session = make_session(f"https://{hostname}:{port}/", timeout_seconds=1)
        if hostname == "panel.example":
            with session:
                assert session.request("GET", "panel/api/inbounds/list") == {
                    "success": True,
                    "obj": [{"id": 7}],
                }
            assert len(state.events) == 5
            assert all(event["host"] == f"{hostname}:{port}" for event in state.events)
            assert received_names == [hostname] * 5
        else:
            assert_error("vpn_xui_request_failed", session.__enter__)
            assert state.events == []
            assert received_names == [hostname]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not thread.is_alive()


def test_safe_logging_and_all_normal_formatting_hide_secrets(caplog) -> None:
    install_safe_logging()
    state = PanelState()
    state.inventory_body = b'{"success":false,"msg":"' + SECRET.encode() + b'"}'
    with running_panel(state) as (_state, url), make_session(url) as session:
        with caplog.at_level(logging.WARNING):
            error = assert_error(
                "vpn_xui_response_invalid",
                lambda: session.request("GET", "panel/api/inbounds/list"),
            )
            logging.getLogger("app.services.vpn_portal").exception("node failed", exc_info=error)
    assert SECRET not in caplog.text


def test_system_exit_from_caller_propagates_after_logout() -> None:
    with running_panel() as (state, url):
        with pytest.raises(SystemExit):
            with make_session(url):
                raise SystemExit(9)
        assert state.events[-1]["path"] == "/logout"


def test_loopback_transport_does_not_call_name_resolution(monkeypatch) -> None:
    def unexpected_lookup(*_args, **_kwargs):
        pytest.fail("Loopback transport must not resolve hostnames")

    with running_panel() as (_state, url):
        monkeypatch.setattr(socket, "getaddrinfo", unexpected_lookup)
        with make_session(url) as session:
            assert session.request("GET", "panel/api/inbounds/list")["success"] is True
