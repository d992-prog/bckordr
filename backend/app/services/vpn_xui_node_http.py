from __future__ import annotations

import http.client
import json
import math
import re
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from http.cookies import CookieError, SimpleCookie
from types import TracebackType
from typing import Any
from urllib.parse import unquote_to_bytes, urlencode, urlsplit


_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_ERROR_CODES = frozenset(
    {
        "vpn_xui_connection_invalid",
        "vpn_xui_request_invalid",
        "vpn_xui_session_not_authenticated",
        "vpn_xui_http_failed",
        "vpn_xui_response_invalid",
        "vpn_xui_request_failed",
        "vpn_xui_apply_pending",
        "vpn_xui_auth_failed",
    }
)
_BASE_SEGMENT = re.compile(r"[A-Za-z0-9_-]+\Z")
_POSITIVE_DECIMAL = re.compile(r"[1-9][0-9]*\Z")
_PERCENT_ESCAPE = re.compile(r"%[0-9A-Fa-f]{2}")
_DNS_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z")
_COOKIE_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_API_TOKEN = re.compile(r"[A-Za-z0-9._~+/\-]+=*\Z", re.ASCII)
_COOKIE_FIELD = re.compile(
    r"""
    [ ]*[!#$%&'*+.^_`|~0-9A-Za-z-]+
    (?:[ ]*=[ ]*(?:
        "(?:[^"\\\x00-\x1f\x7f]|\\[\x20-\x7e])*"
        |(?:[A-Za-z]{3,6}day|[A-Za-z]{3}),[ ]
            [A-Za-z0-9 -]{9,11}[ ][0-9:]{8}[ ]GMT
        |[A-Za-z0-9!#%&'~_`><@,:/$*+\-.^|)(?}{=\[\]]*
    ))?
    [ ]*(?:;|$)
    """,
    re.VERBOSE | re.ASCII,
)


class NodePanelError(ValueError):
    def __init__(self, code: str, *, mutation_uncertain: bool = False) -> None:
        self.code = code if code in _ERROR_CODES else "vpn_xui_response_invalid"
        self.mutation_uncertain = bool(mutation_uncertain)
        super().__init__(self.code)

    def __repr__(self) -> str:
        return (
            f"NodePanelError(code={self.code!r}, "
            f"mutation_uncertain={self.mutation_uncertain!r})"
        )


@dataclass(frozen=True, slots=True)
class _PanelAddress:
    scheme: str
    hostname: str
    port: int
    host_header: str
    base_path: str


@dataclass(frozen=True, slots=True)
class _PreparedRequest:
    method: str
    route: str
    encoded_body: bytes | None
    mutation: bool


def _raise(code: str, *, mutation_uncertain: bool = False) -> None:
    raise NodePanelError(code, mutation_uncertain=mutation_uncertain) from None


def _has_space_or_control(value: str) -> bool:
    return any(ord(character) <= 0x20 or ord(character) == 0x7F for character in value)


def _valid_hostname(hostname: str) -> bool:
    if not hostname or _has_space_or_control(hostname):
        return False
    try:
        ascii_hostname = hostname.encode("idna").decode("ascii")
    except UnicodeError:
        return False
    if len(ascii_hostname) > 253:
        return False
    if ":" in ascii_hostname:
        try:
            socket.inet_pton(socket.AF_INET6, ascii_hostname)
        except OSError:
            return False
        return True
    try:
        socket.inet_pton(socket.AF_INET, ascii_hostname)
    except OSError:
        labels = ascii_hostname.removesuffix(".").split(".")
        return bool(labels) and all(_DNS_LABEL.fullmatch(label) for label in labels)
    return True


def validate_panel_url(panel_url: object) -> _PanelAddress:
    if (
        not isinstance(panel_url, str)
        or not panel_url
        or _has_space_or_control(panel_url)
        or "\\" in panel_url
        or "?" in panel_url
        or "#" in panel_url
    ):
        _raise("vpn_xui_connection_invalid")
    try:
        parts = urlsplit(panel_url)
        port = parts.port
        hostname = parts.hostname
    except (TypeError, ValueError):
        _raise("vpn_xui_connection_invalid")
    if (
        parts.scheme not in {"http", "https"}
        or not parts.netloc
        or parts.netloc.endswith(":")
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or hostname is None
        or not _valid_hostname(hostname)
    ):
        _raise("vpn_xui_connection_invalid")
    resolved_port = port if port is not None else (443 if parts.scheme == "https" else 80)
    if not 1 <= resolved_port <= 65535:
        _raise("vpn_xui_connection_invalid")

    path = parts.path or "/"
    if "%" in path or "\\" in path or not path.startswith("/") or not path.endswith("/"):
        _raise("vpn_xui_connection_invalid")
    if path != "/":
        segments = path[1:-1].split("/")
        if not segments or any(not _BASE_SEGMENT.fullmatch(segment) for segment in segments):
            _raise("vpn_xui_connection_invalid")
    return _PanelAddress(parts.scheme, hostname, resolved_port, parts.netloc, path)


def validate_credentials(username: object, password: object) -> tuple[str, str]:
    if not isinstance(username, str) or not username:
        _raise("vpn_xui_connection_invalid")
    if not isinstance(password, str) or not password:
        _raise("vpn_xui_connection_invalid")
    return username, password


def validate_timeout(timeout_seconds: object) -> float:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 < timeout_seconds <= 60
        or not math.isfinite(timeout_seconds)
    ):
        _raise("vpn_xui_connection_invalid")
    return float(timeout_seconds)


def _validate_email_segment(segment: str) -> bool:
    if not segment or segment in {".", ".."}:
        return False
    cursor = 0
    while cursor < len(segment):
        if segment[cursor] == "%":
            if _PERCENT_ESCAPE.match(segment, cursor) is None:
                return False
            cursor += 3
        else:
            cursor += 1
    try:
        decoded = unquote_to_bytes(segment).decode("utf-8", errors="strict")
    except (UnicodeDecodeError, ValueError):
        return False
    if not decoded or decoded in {".", ".."} or "%" in decoded:
        return False
    if any(character in "/\\?#" or character.isspace() or ord(character) < 0x20 or ord(character) == 0x7F for character in decoded):
        return False
    try:
        segment.encode("ascii")
    except UnicodeEncodeError:
        return False
    return True


def validate_request(
    method: object,
    route: object,
    *,
    body: object = None,
    mutation: object = False,
) -> _PreparedRequest:
    if not isinstance(method, str) or method not in {"GET", "POST"}:
        _raise("vpn_xui_request_invalid")
    if not isinstance(mutation, bool):
        _raise("vpn_xui_request_invalid")
    if (
        not isinstance(route, str)
        or not route
        or route.startswith("/")
        or "\\" in route
        or "#" in route
        or _has_space_or_control(route)
    ):
        _raise("vpn_xui_request_invalid")
    try:
        parts = urlsplit(route)
    except ValueError:
        _raise("vpn_xui_request_invalid")
    if parts.scheme or parts.netloc or parts.fragment or parts.path != route.split("?", 1)[0]:
        _raise("vpn_xui_request_invalid")
    segments = parts.path.split("/")
    if any(not segment or segment in {".", ".."} for segment in segments):
        _raise("vpn_xui_request_invalid")

    fixed_get = {
        "panel/api/inbounds/list",
        "panel/api/clients/list",
        "panel/api/server/status",
    }
    fixed_post_read = {"panel/api/setting/all"}
    fixed_post_mutation = {
        "panel/api/clients/add",
        "panel/api/clients/bulkEnable",
        "panel/api/clients/bulkDisable",
        "panel/api/server/restartXrayService",
    }
    expected_mutation: bool | None = None
    query_allowed = False

    if method == "GET" and parts.path in fixed_get and not parts.query:
        expected_mutation = False
    elif (
        method == "GET"
        and len(segments) == 5
        and segments[:4] == ["panel", "api", "clients", "get"]
        and not parts.query
        and _validate_email_segment(segments[4])
    ):
        expected_mutation = False
    elif method == "POST" and parts.path in fixed_post_read and not parts.query:
        expected_mutation = False
    elif method == "POST" and parts.path in fixed_post_mutation and not parts.query:
        expected_mutation = True
    elif (
        method == "POST"
        and len(segments) == 5
        and segments[:4] == ["panel", "api", "clients", "update"]
        and _validate_email_segment(segments[4])
    ):
        expected_mutation = True
        query_allowed = True

    if expected_mutation is None or mutation is not expected_mutation:
        _raise("vpn_xui_request_invalid")
    if query_allowed:
        prefix = "inboundIds="
        if not parts.query.startswith(prefix) or not _POSITIVE_DECIMAL.fullmatch(parts.query[len(prefix) :]):
            _raise("vpn_xui_request_invalid")
    elif "?" in route:
        _raise("vpn_xui_request_invalid")

    if method == "GET":
        if body is not None:
            _raise("vpn_xui_request_invalid")
        encoded = None
    else:
        if not isinstance(body, dict):
            _raise("vpn_xui_request_invalid")
        try:
            encoded = json.dumps(
                body,
                allow_nan=False,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
        except (TypeError, ValueError, OverflowError, RecursionError):
            _raise("vpn_xui_request_invalid")
    return _PreparedRequest(method, route, encoded, mutation)


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    return remaining


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _reject_constant(_value: str) -> None:
    raise ValueError


def _finite_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError
    return parsed


def _valid_cookie_syntax(header: str) -> bool:
    # SimpleCookie silently accepts a parsed prefix if a later field cannot be
    # tokenized. Check every field first, retaining its quoted/date value syntax.
    candidate = header.strip(" ")
    cursor = 0
    while cursor < len(candidate):
        match = _COOKIE_FIELD.match(candidate, cursor)
        if match is None:
            return False
        cursor = match.end()
    return bool(candidate)


def _decode_envelope(data: bytes) -> dict[str, Any]:
    try:
        text = data.decode("utf-8", errors="strict")
        envelope = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_constant,
            parse_float=_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError):
        _raise("vpn_xui_response_invalid")
    if not isinstance(envelope, dict) or envelope.get("success") is not True:
        _raise("vpn_xui_response_invalid")
    if "nodePending" in envelope and envelope["nodePending"] is not False:
        _raise("vpn_xui_apply_pending")
    obj = envelope.get("obj")
    if isinstance(obj, dict) and "nodePending" in obj and obj["nodePending"] is not False:
        _raise("vpn_xui_apply_pending")
    return envelope


class NodePanelSession:
    def __init__(
        self,
        panel_url: str,
        username: str | None = None,
        password: str | None = None,
        *,
        api_token: str | None = None,
        timeout_seconds: float = 10,
    ) -> None:
        self._address = validate_panel_url(panel_url)
        token_mode = username is None and password is None and api_token is not None
        password_mode = api_token is None and username is not None and password is not None
        if token_mode:
            if (
                not isinstance(api_token, str)
                or not 1 <= len(api_token) <= 4096
                or _API_TOKEN.fullmatch(api_token) is None
            ):
                _raise("vpn_xui_connection_invalid")
            self._username = ""
            self._password = ""
            self._api_token: str | None = api_token
        elif password_mode:
            self._username, self._password = validate_credentials(username, password)
            self._api_token = None
        else:
            _raise("vpn_xui_connection_invalid")
        self._token_mode = token_mode
        self._timeout = validate_timeout(timeout_seconds)
        self._cookies: dict[str, str] = {}
        self._csrf_token: str | None = None
        self._entered = False
        self._logged_in = False
        self._closed = False

    def __repr__(self) -> str:
        return "NodePanelSession()"

    def __enter__(self) -> NodePanelSession:
        if self._closed or self._entered:
            _raise("vpn_xui_session_not_authenticated")
        try:
            if self._token_mode:
                try:
                    self._exchange(
                        "GET",
                        self._address.base_path + "panel/api/server/status",
                        data=None,
                        content_type=None,
                        csrf_token=None,
                        mutation=False,
                        api_token=self._api_token,
                    )
                except NodePanelError as exc:
                    if exc.code == "vpn_xui_request_failed":
                        raise
                    _raise("vpn_xui_auth_failed")
                self._entered = True
                return self
            first_token = self._fetch_csrf()
            self._login(first_token)
            self._logged_in = True
            self._csrf_token = first_token
            self._csrf_token = self._fetch_csrf()
            self._entered = True
            return self
        except BaseException:
            try:
                if self._logged_in:
                    self._best_effort_logout(suppress_interrupt=True)
            finally:
                self._clear_secrets()
            raise

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> bool:
        try:
            if self._logged_in:
                self._best_effort_logout(suppress_interrupt=_exc_type is not None)
        finally:
            self._clear_secrets()
        return False

    def request(
        self,
        method: str,
        route: str,
        *,
        body: dict[str, Any] | None = None,
        mutation: bool = False,
    ) -> dict[str, Any]:
        prepared = validate_request(method, route, body=body, mutation=mutation)
        if (
            not self._entered
            or self._closed
            or (self._token_mode and self._api_token is None)
            or (not self._token_mode and self._csrf_token is None)
        ):
            _raise("vpn_xui_session_not_authenticated")
        return self._exchange(
            prepared.method,
            self._address.base_path + prepared.route,
            data=prepared.encoded_body,
            content_type="application/json" if prepared.encoded_body is not None else None,
            csrf_token=self._csrf_token,
            mutation=prepared.mutation,
            api_token=self._api_token if self._token_mode else None,
        )

    def _fetch_csrf(self) -> str:
        try:
            envelope = self._exchange(
                "GET",
                self._address.base_path + "csrf-token",
                data=None,
                content_type=None,
                csrf_token=self._csrf_token,
                mutation=False,
            )
            token = envelope.get("obj")
            if (
                not isinstance(token, str)
                or not token
                or len(token) > 4096
                or any(ord(character) < 0x21 or ord(character) > 0x7E for character in token)
            ):
                _raise("vpn_xui_auth_failed")
            return token
        except NodePanelError as exc:
            if exc.code == "vpn_xui_request_failed":
                raise
            _raise("vpn_xui_auth_failed")

    def _login(self, csrf_token: str) -> None:
        try:
            data = urlencode(
                {"username": self._username, "password": self._password},
                doseq=False,
            ).encode("ascii")
        except (UnicodeError, ValueError):
            _raise("vpn_xui_auth_failed")
        try:
            self._exchange(
                "POST",
                self._address.base_path + "login",
                data=data,
                content_type="application/x-www-form-urlencoded",
                csrf_token=csrf_token,
                mutation=False,
            )
        except NodePanelError as exc:
            if exc.code == "vpn_xui_request_failed":
                raise
            _raise("vpn_xui_auth_failed")

    def _best_effort_logout(self, *, suppress_interrupt: bool = False) -> None:
        try:
            self._exchange(
                "POST",
                self._address.base_path + "logout",
                data=b"",
                content_type="application/x-www-form-urlencoded",
                csrf_token=self._csrf_token,
                mutation=False,
            )
        except (KeyboardInterrupt, SystemExit):
            if not suppress_interrupt:
                raise
        except Exception:
            pass

    def _clear_secrets(self) -> None:
        self._username = ""
        self._password = ""
        self._api_token = None
        self._cookies.clear()
        self._csrf_token = None
        self._entered = False
        self._logged_in = False
        self._closed = True

    def _cookie_header(self) -> str | None:
        if not self._cookies:
            return None
        parts: list[str] = []
        for name, value in self._cookies.items():
            cookie = SimpleCookie()
            cookie[name] = value
            parts.append(cookie[name].OutputString())
        return "; ".join(parts)

    def _update_cookies(self, headers: http.client.HTTPMessage) -> None:
        for raw_header in headers.get_all("Set-Cookie", []):
            if (
                not raw_header
                or _has_space_or_control(raw_header.replace(" ", ""))
                or not _valid_cookie_syntax(raw_header)
            ):
                _raise("vpn_xui_response_invalid")
            parsed = SimpleCookie()
            try:
                parsed.load(raw_header)
            except CookieError:
                _raise("vpn_xui_response_invalid")
            if not parsed:
                _raise("vpn_xui_response_invalid")
            for name, morsel in parsed.items():
                value = morsel.value
                if (
                    not _COOKIE_NAME.fullmatch(name)
                    or any(ord(character) < 0x20 or ord(character) > 0x7E for character in value)
                ):
                    _raise("vpn_xui_response_invalid")
                max_age = morsel["max-age"]
                if max_age:
                    try:
                        parsed_age = int(max_age, 10)
                    except ValueError:
                        _raise("vpn_xui_response_invalid")
                    if parsed_age == 0:
                        self._cookies.pop(name, None)
                        continue
                self._cookies[name] = value

    def _exchange(
        self,
        method: str,
        target: str,
        *,
        data: bytes | None,
        content_type: str | None,
        csrf_token: str | None,
        mutation: bool,
        api_token: str | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + self._timeout
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self._address.port,
            timeout=self._timeout,
        )
        may_have_sent = False
        completed = False
        deadline_timer: threading.Timer | None = None
        response: http.client.HTTPResponse | None = None
        try:
            connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            connection.sock.settimeout(_remaining(deadline))
            connection.sock.connect(("127.0.0.1", self._address.port))
            if self._address.scheme == "https":
                if connection.sock is None:
                    raise OSError
                context = ssl.create_default_context()
                connection.sock.settimeout(_remaining(deadline))
                connection.sock = context.wrap_socket(
                    connection.sock,
                    server_hostname=self._address.hostname,
                )

            wire_socket = connection.sock

            def abort_at_deadline() -> None:
                if wire_socket is None:
                    return
                try:
                    wire_socket.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    wire_socket.close()
                except OSError:
                    pass

            deadline_timer = threading.Timer(_remaining(deadline), abort_at_deadline)
            deadline_timer.daemon = True
            deadline_timer.start()

            headers = {"Host": self._address.host_header, "Connection": "close"}
            if content_type is not None:
                headers["Content-Type"] = content_type
            if api_token is not None:
                headers["Authorization"] = "Bearer " + api_token
            else:
                if csrf_token is not None:
                    headers["X-CSRF-Token"] = csrf_token
                cookie_header = self._cookie_header()
                if cookie_header is not None:
                    headers["Cookie"] = cookie_header

            if connection.sock is not None:
                connection.sock.settimeout(_remaining(deadline))
            may_have_sent = mutation
            connection.request(method, target, body=data, headers=headers)
            if connection.sock is not None:
                connection.sock.settimeout(_remaining(deadline))
            response = connection.getresponse()
            if response.status != 200:
                _raise("vpn_xui_http_failed", mutation_uncertain=may_have_sent)

            lengths = response.headers.get_all("Content-Length", [])
            expected_length: int | None = None
            if lengths:
                if (
                    len(lengths) != 1
                    or not lengths[0].isascii()
                    or not lengths[0].isdigit()
                    or len(lengths[0]) > 10
                ):
                    _raise("vpn_xui_response_invalid", mutation_uncertain=may_have_sent)
                expected_length = int(lengths[0])
                if expected_length > _MAX_RESPONSE_BYTES:
                    _raise("vpn_xui_response_invalid", mutation_uncertain=may_have_sent)

            chunks: list[bytes] = []
            size = 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(_remaining(deadline))
                chunk = response.read(min(65536, _MAX_RESPONSE_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    _raise("vpn_xui_response_invalid", mutation_uncertain=may_have_sent)
                chunks.append(chunk)
            _remaining(deadline)
            if expected_length is not None and size != expected_length:
                _raise("vpn_xui_response_invalid", mutation_uncertain=may_have_sent)
            if api_token is None:
                self._update_cookies(response.headers)
            envelope = _decode_envelope(b"".join(chunks))
            _remaining(deadline)
            completed = True
            return envelope
        except (KeyboardInterrupt, SystemExit):
            raise
        except NodePanelError as exc:
            if may_have_sent and not exc.mutation_uncertain:
                _raise(exc.code, mutation_uncertain=True)
            raise
        except http.client.IncompleteRead:
            _raise("vpn_xui_response_invalid", mutation_uncertain=may_have_sent)
        except (
            OSError,
            ValueError,
            UnicodeError,
            ssl.SSLError,
            http.client.HTTPException,
            TimeoutError,
        ):
            _raise("vpn_xui_request_failed", mutation_uncertain=may_have_sent)
        finally:
            cleanup_error: BaseException | None = None
            try:
                if deadline_timer is not None:
                    deadline_timer.cancel()
                    deadline_timer.join()
            except BaseException as exc:
                cleanup_error = exc
            for resource in (response, connection):
                try:
                    if resource is not None:
                        resource.close()
                except BaseException as exc:
                    if cleanup_error is None or (
                        isinstance(cleanup_error, Exception) and not isinstance(exc, Exception)
                    ):
                        cleanup_error = exc
            if completed and cleanup_error is not None:
                if not isinstance(cleanup_error, Exception):
                    raise cleanup_error from None
                _raise("vpn_xui_request_failed", mutation_uncertain=may_have_sent)
