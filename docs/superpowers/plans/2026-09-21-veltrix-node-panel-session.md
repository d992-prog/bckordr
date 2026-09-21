# Veltrix node-local panel session implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Execute authenticated, bounded panel HTTP requests on the VPN node so private inbound data never needs to leave it.

**Architecture:** A standard-library synchronous session is packaged with the forthcoming node executor. It connects only to loopback; preserves original Host/SNI and certificate verification for HTTPS; owns cookies, CSRF and login/logout. Typed static errors distinguish a request rejected before send from a potentially applied mutation. Only the later executor's allowlisted summary is exported to the control host, not this session's raw responses.

**Tech Stack:** Python 3.11 stdlib http.client, ssl, socket, http.cookies, json; pytest and real loopback HTTP/TLS servers.

---

## Design correction and pinned reference

The rejected forwarding plan would bring inbound private keys into the control
process. This plan keeps raw data in node memory. No service daemon or new public
port is needed: a bounded Python zipapp executor will be invoked over strict SSH.
This is implementation of the existing approved API/privacy requirements, not
permission to change production or issue invitations.

Pinned source commit: `7ef22f94c950ff09f0870e2295fa65ad5968742c` (3x-UI v3.8.5).
[Authentication](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/internal/web/controller/index.go)
uses GET csrf-token, POST login form, POST logout and the X-CSRF-Token header.
[Client operations](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/internal/web/controller/client.go)
use the normalized client API, not old inbound addClient routes.

## Task 1: Node-only authenticated HTTP transport

**Files:**
- Create `backend/app/services/vpn_xui_node_http.py` (stdlib only, import performs no I/O).
- Create `backend/tests/test_vpn_xui_node_http.py`.

- [x] Add RED tests before implementation. Representative actual-loopback test:

```python
def test_authenticated_inventory_uses_cookie_and_csrf(panel_server):
    with NodePanelSession(panel_server.url, "synthetic-user", "synthetic-password") as panel:
        assert panel.request("GET", "panel/api/clients/list") == {"success": True, "obj": []}
    assert panel_server.events == ["csrf", "login", "csrf", "inventory", "logout"]
```

The test fixture is a local ThreadingHTTPServer on 127.0.0.1 with synthetic cookies,
CSRF and payloads. No external service dependency, production credentials or mocks
of the session. Use monkeypatch only for pre-network rejection and unavoidable
socket/TLS seams; prove real HTTP cookie, CSRF, bounds and cleanup behavior.

- [x] Run RED from backend with unique `.pytest_cache/node-http-red-<guid>` basetemp.
- [x] Implement the public signatures and closed request contract below. No stub or
  test-only production method may remain:

```python
class NodePanelError(ValueError):
    def __init__(self, code: str, *, mutation_uncertain: bool = False):
        super().__init__(code)
        self.code = code
        self.mutation_uncertain = mutation_uncertain

class NodePanelSession:
    def __init__(self, panel_url: str, username: str, password: str, *, timeout_seconds: float = 10.0):
        # Validate only; no socket or login on construction.
        self._url = validate_panel_url(panel_url)
        self._username, self._password = validate_credentials(username, password)
        self._timeout = validate_timeout(timeout_seconds)
        self._cookies = {}
        self._csrf = None

    def __repr__(self):
        return "NodePanelSession()"

    def __enter__(self):
        try:
            self._csrf = self._fetch_csrf()
            self._exchange("POST", "login", form={"username": self._username, "password": self._password})
            self._csrf = self._fetch_csrf()
            return self
        except BaseException:
            self._clear_secrets()
            raise

    def __exit__(self, exc_type, exc, traceback):
        try:
            if self._csrf is not None:
                self._exchange("POST", "logout")
        except NodePanelError:
            pass
        finally:
            self._clear_secrets()

    def request(self, method: str, route: str, *, body: dict | None = None, mutation: bool = False) -> dict:
        validate_request(method, route, body, mutation)
        if self._csrf is None:
            raise NodePanelError("vpn_xui_session_not_authenticated")
        return self._exchange(method, route, body=body, mutation=mutation)
```

Implement named helpers exactly within the owned module:

- `validate_panel_url`: str, HTTP/HTTPS only; hostname; no username/password/query/
  fragment; strict valid port; base path `/` or slash-separated ASCII letters,
  numbers, `_`, `-`, with trailing slash. Reject control/space, dot segments,
  backslashes, percent escapes, internal empty segments before network.
- `validate_credentials`: nonempty strings, returned unchanged; never log or format.
- `validate_timeout`: finite numeric `0 < t <= 60`, excluding bool.
- `validate_request`: literal GET/POST; canonical relative path; no traversal,
  absolute/network URL, whitespace/control/DEL, encoded separators, malformed or
  nested percent escapes. Single safely percent-encoded email segment supported.
  Query is absent or `inboundIds=<positive decimal id>` only on client update.
  Literal bool mutation must be true for mutation routes and false for read routes.
  GET body None; POST dictionary serialized before connect with `allow_nan=False`.
  Reject serialization errors with static `vpn_xui_request_invalid`.

Allowlist:

| Method | Route | Mutation |
|---|---|---|
| GET | panel/api/inbounds/list | false |
| GET | panel/api/clients/list | false |
| GET | panel/api/server/status | false |
| GET | panel/api/clients/get/<email> | false |
| POST | panel/api/setting/all | false |
| POST | panel/api/clients/add | true |
| POST | panel/api/clients/bulkEnable | true |
| POST | panel/api/clients/bulkDisable | true |
| POST | panel/api/clients/update/<email>?inboundIds=<id> | true |
| POST | panel/api/server/restartXrayService | true |

Private `_exchange` handles auth routes too; public request cannot invoke arbitrary
routes. Every exchange creates/closes its own connection in `finally`, carrying
session cookies in a controlled dictionary. Never use proxy environment variables.
Before creating a socket, encode either JSON with `allow_nan=False` or urlencoded
login form; set the correct Content-Type. Request path is validated base+route,
Host is original URL authority, X-CSRF-Token is current printable ASCII token
(max 4096); Cookie header comes only from strictly parsed response cookies.

Connection implementation:

```python
connection = http.client.HTTPConnection("127.0.0.1", panel_port, timeout=timeout)
connection.connect()
if scheme == "https":
    connection.sock = ssl.create_default_context().wrap_socket(
        connection.sock, server_hostname=original_hostname
    )
try:
    connection.request(method, path, body=encoded_body, headers=headers)
    response = connection.getresponse()
    if response.status != 200:
        raise NodePanelError("vpn_xui_http_failed", mutation_uncertain=mutation)
    payload = response.read(2 * 1024 * 1024 + 1)
    if len(payload) > 2 * 1024 * 1024:
        raise NodePanelError("vpn_xui_response_invalid", mutation_uncertain=mutation)
    envelope = json.loads(payload.decode("utf-8"), object_pairs_hook=reject_duplicate_keys,
                          parse_constant=reject_nonfinite)
finally:
    connection.close()
```

Place connect/TLS inside cleanup scope as well. Do not send HTTP before successful
TLS handshake. Do not disable hostname or certificate checks. Suppress underlying
OSError/ssl/HTTPException/ValueError/UnicodeError from public exception chains.
Socket/TLS failures before `request` mean uncertainty false; once a mutating request
may be sent, all network/parse/status/pending failures mean uncertainty true. No
automatic retry or redirect. KeyboardInterrupt/SystemExit propagate after cleanup.
Use monotonic deadline accounting so an indefinitely trickling response cannot hold
the process forever: deadline spans request+headers+body; refresh socket timeout from
remaining budget and read in bounded chunks; test the bound.

Success envelope must be dict, success IS True; no top/nested obj.nodePending except
absent or literal false. JSON duplicate keys, NaN/infinity and malformed bytes are
errors. Never include `msg`, raw body, URL, cookie, username or key in exceptions.
Static errors: connection_invalid, request_invalid, session_not_authenticated,
http_failed, response_invalid, request_failed, apply_pending, auth_failed with
`vpn_xui_` prefix. Session returns raw envelope **only inside node-local code**.

`_fetch_csrf` calls private GET csrf-token and validates obj as printable token.
Parse Set-Cookie using stdlib SimpleCookie, reject malformed/control characters,
retain cookie values across requests; zero Max-Age removes a cookie. No raw headers
are logged. `_clear_secrets` clears cookies, CSRF, username and password; the session
cannot be reused after close. A failed second CSRF fetch must attempt logout after
successful login without masking the original error. Logout is best effort, not a
claim that remote session destruction is confirmed.

- [x] Prove full test matrix: real successful auth/cookie/CSRF/GET/logout; no public
  login/settings mutation bypass; configuration/route/body/boolean/type checks before
  connection; redirect no follow; malformed/HTML/truncated/oversize/duplicate/nonfinite
  JSON; pending flags; exact mutation uncertainty phases; no replay; credentials never
  in repr/str/formatted chains/logs; login and post-login-CSRF failures; resources
  close on success/error; real untrusted local TLS rejection, original Host preserved;
  stalled/trickled response bounded. All test listeners close in fixture cleanup.
- [x] GREEN: new tests + 205 existing identity tests, backend Ruff, diff check.
- [x] Specification review then quality review; parent preserves unrelated files.

## Verified implementation checkpoint

The initial missing-module RED and subsequent behavior regressions were observed
before implementation/fixes. Parent's final targeted run: **326 passed in 52.09s**
(121 HTTP-session cases + 205 existing identity cases). Backend Ruff and diff check
passed. A full backend run before the final cleanup-only correction produced
**1101 passed, 8 skipped in 275.28s**; it is not presented as the final revision's
full-suite result. The skips require a separately configured PostgreSQL database.

Specification review required sanitizing ValueError and rejecting malformed cookie
suffixes; both were fixed and independently rechecked. Quality review required
preserving original exceptions and mutation uncertainty when cleanup itself fails;
its 12 targeted rechecks passed. Parent also verified stdlib-only import and Python
3.11 syntax compatibility. Actual production runtime verification is still pending.

No remote panel login, API mutation, restart, endpoint binding, or deployment was
performed for this component. A separate read-only SSH inspection refreshed node
versions/listeners. The owner's new TCP443 permission is recorded in the main spec
and has not been exercised. This checkpoint does not make the beta deployable.

## Continuation

After this component, build the node-local operation executor using the existing
identity logic plus transport/policy checks. Its response is an allowlisted safe
summary and its local durable ledger serializes requests, fences stale generations
and keeps ambiguous writes unresolved. Then connect a DB-backed control queue and
drainer; do not remove legacy barriers before all dependencies and recovery tests
exist. This local transport is not a deployable endpoint-aware release by itself.
