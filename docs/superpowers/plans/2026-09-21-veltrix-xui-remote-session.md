# Veltrix strict SSH panel session implementation plan

**Superseded before implementation:** full inbound API responses contain REALITY
private keys. Forwarding those responses to the control process violates the
approved node-only private-key boundary. No code was written for this plan.
Implement node-local HTTP processing and export only an allowlisted operation
result via strict SSH instead. The authenticated HTTP/error requirements below
remain research context, not an instruction to implement the obsolete tunnel.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provide a real, bounded, strictly host-key-verified HTTP session to the node's loopback panel, including CSRF login and explicit uncertainty reporting.

**Architecture:** AsyncSSH forwards a temporary control-host loopback port to node loopback only; HTTPX uses that channel without environment proxies or redirects. HTTPS retains certificate and original-hostname verification. A small session owns cookies/login/logout and returns parsed envelopes or static errors; it does not itself authorize VPN operations. Existing legacy mutation barriers remain until the durable runner and complete adapter are verified.

**Tech Stack:** Python 3.11+, existing AsyncSSH and HTTPX, pytest-asyncio, real local SSH/HTTP test servers.

---

## Scope and interfaces

Part of the already approved protected-endpoint design, using the existing worktree.
No production calls, port opening, token generation, DB binding or deployment in
this task. Temporary test listeners bind only `127.0.0.1`. No `known_hosts=None`,
autoaccept callback, arbitrary shell command, external proxy, insecure TLS fallback
or automatic retry. A lost mutation response cannot be presented as failure-before-write.

Files:

- Create `backend/app/services/vpn_xui_session.py`: safe connection/configuration,
  HTTP envelope handling and authenticated session lifetime only.
- Create `backend/tests/test_vpn_xui_session.py`: fake transport tests for wire/error
  contracts plus actual local SSH forwarding and host-key rejection.

Public contract:

```python
@dataclass(frozen=True, slots=True)
class XuiConnectionConfig:
    ssh_host: str
    ssh_port: int
    ssh_username: str
    known_hosts: str = field(repr=False)
    panel_url: str = field(repr=False)
    panel_username: str = field(repr=False)
    panel_password: str = field(repr=False)
    ssh_password: str | None = field(default=None, repr=False)
    ssh_key_path: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

class XuiSessionError(VpnEndpointError):
    def __init__(self, code: str, *, mutation_uncertain: bool = False):
        super().__init__(code)
        self.mutation_uncertain = mutation_uncertain

class XuiSession:
    async def request(self, method: str, route: str, *,
                      body: dict | None = None,
                      mutation: bool = False) -> dict: ...

@asynccontextmanager
async def open_xui_session(config: XuiConnectionConfig) -> AsyncIterator[XuiSession]: ...
```

`request` returns the complete success envelope for the identity observer. It may
contain secrets: callers must extract needed values, never log/serialize it or retain
it in DB/events. Session/config repr must not expose credentials, cookies, panel path
or raw responses. The implementation uses private helpers for login form and logout.
The declaration above defines signatures, not a production stub to retain.

## Task 1: Implement and prove the authenticated transport

- [ ] Add failing tests using `httpx.MockTransport` only at the network boundary:

```python
@pytest.mark.asyncio
async def test_successful_envelope_is_returned_without_mutating_input():
    envelope = {"success": True, "obj": []}
    async def handler(request):
        return httpx.Response(200, json=envelope)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        session = XuiSession(http, "csrf-synthetic")
        assert await session.request("GET", "panel/api/clients/list") == envelope
```

Public constructor accepts an owned HTTPX client and CSRF token for composition;
it performs no I/O itself. The real context factory owns resource cleanup.
Test every unsafe route, malformed/non-200 response, redirects (no second request),
HTML/null/list, false/nonboolean success, top/nested nodePending, oversized response,
duplicate JSON keys, invalid UTF-8, missing CSRF, login failure and secret-bearing
exceptions. Verify no retries and uncertainty true after a mutation request may
have been submitted. Nonmutation errors remain uncertainty false. Validation before
request never sets uncertainty. Test POST wire body/CSRF, cookie propagation through
login, logout, and closure on success/error/cancellation. Assert plain logs at the
application's normal logging configuration do not contain password/cookie/path.

- [ ] Verify RED from backend using a unique scratch directory:

```powershell
$vpnSessionTemp = Join-Path (Get-Location).Path ('.pytest_cache/xui-session-red-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $vpnSessionTemp) { throw 'Scratch exists' }
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_xui_session.py -q --basetemp $vpnSessionTemp
```

- [ ] Implement the following algorithm, keeping the file focused. Every validation
  branch must have the failing test before the corresponding implementation.

```python
# Request validation (before network):
if method not in {"GET", "POST"}:
    raise XuiSessionError("vpn_xui_request_invalid")
if not isinstance(route, str) or not route or route.startswith("/"):
    raise XuiSessionError("vpn_xui_request_invalid")
parts = urlsplit(route)
if parts.scheme or parts.netloc or parts.fragment or "\\" in route:
    raise XuiSessionError("vpn_xui_request_invalid")
if any(ord(c) < 33 or ord(c) == 127 for c in route):
    raise XuiSessionError("vpn_xui_request_invalid")
# Permit only canonical safe ASCII route segments and decimal inboundIds query.
# Caller can percent-encode an email, but decoding must not introduce separators,
# traversal, whitespace, query/fragment syntax or control characters.
if any(segment in {"", ".", ".."} for segment in parts.path.split("/")):
    raise XuiSessionError("vpn_xui_request_invalid")
decoded = unquote(parts.path)
if any(c in decoded for c in "\\?#") or decoded.count("/") != parts.path.count("/"):
    raise XuiSessionError("vpn_xui_request_invalid")
if any(segment in {"", ".", ".."} for segment in decoded.split("/")):
    raise XuiSessionError("vpn_xui_request_invalid")
if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in decoded):
    raise XuiSessionError("vpn_xui_request_invalid")
if parts.query and not re.fullmatch(r"inboundIds=[1-9][0-9]*", parts.query):
    raise XuiSessionError("vpn_xui_request_invalid")
```

Use an explicit route/method allowlist: GET `panel/api/inbounds/list`,
`panel/api/clients/list`, `panel/api/server/status`, and `panel/api/clients/get/<email>`;
POST `panel/api/clients/add`, `panel/api/clients/bulkEnable`,
`panel/api/clients/bulkDisable`, `panel/api/clients/update/<email>?inboundIds=<id>`.
No general panel settings/write API exposed. Login/logout/CSRF are private helpers.
Mutation must be literal bool and must agree with the POST client action; GET
cannot be tagged mutation, a mutation POST cannot omit the uncertainty policy.
Body must be JSON-serializable dict, POST requires one, GET body must be None.
Serialize with `allow_nan=False` before sending; errors are static request_invalid.

HTTP implementation core:

```python
try:
    async with http.stream(method, route, content=encoded_body,
                           headers={"X-CSRF-Token": csrf_token,
                                    "Content-Type": "application/json"}) as response:
        if response.status_code != 200:
            raise XuiSessionError("vpn_xui_http_failed", mutation_uncertain=mutation)
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 2 * 1024 * 1024:
                raise XuiSessionError("vpn_xui_response_invalid", mutation_uncertain=mutation)
        envelope = json.loads(bytes(chunks).decode("utf-8"),
                              object_pairs_hook=reject_duplicate_keys,
                              parse_constant=reject_nonfinite)
except XuiSessionError:
    raise
except (httpx.HTTPError, OSError, ValueError, UnicodeError):
    raise XuiSessionError("vpn_xui_request_failed", mutation_uncertain=mutation) from None
if not isinstance(envelope, dict) or envelope.get("success") is not True:
    raise XuiSessionError("vpn_xui_response_invalid", mutation_uncertain=mutation)
if envelope.get("nodePending", False) is not False or (
    isinstance(envelope.get("obj"), dict)
    and envelope["obj"].get("nodePending", False) is not False
):
    raise XuiSessionError("vpn_xui_apply_pending", mutation_uncertain=mutation)
return envelope
```

`reject_duplicate_keys` builds a dictionary, raises `ValueError` on a repeated key;
`reject_nonfinite` always raises `ValueError`. Do not embed keys/values in errors.
No response message is copied to exceptions. Preserve asyncio.CancelledError;
caller treats cancellation during mutation as unresolved, not rolled back.

Connection validation/creation:

```python
panel = urlsplit(config.panel_url)
# Require http or https, hostname, no userinfo/query/fragment, valid 1..65535 port;
# path is '/' or '/<ASCII alnum/_/->/' with no empty/dot/traversal segments.
# Require positive nonboolean SSH port, nonempty username/host/credentials,
# explicit existing readable nonempty known_hosts file, and finite timeout 0<t<=60.
# At least one explicit password or key path; no ambient SSH agent/config.
async with asyncssh.connect(
    config.ssh_host, port=config.ssh_port, username=config.ssh_username,
    password=config.ssh_password,
    client_keys=[config.ssh_key_path] if config.ssh_key_path else [],
    known_hosts=config.known_hosts, config=None, agent_path=None,
    connect_timeout=config.timeout_seconds, login_timeout=config.timeout_seconds,
) as connection:
    listener = await connection.forward_local_port("127.0.0.1", 0, "127.0.0.1", panel.port or (443 if panel.scheme == "https" else 80))
    try:
        async def set_tls_hostname(request):
            request.extensions["sni_hostname"] = panel.hostname
        async with httpx.AsyncClient(
            base_url=f"{panel.scheme}://127.0.0.1:{listener.get_port()}{panel.path or '/'}",
            headers={"Host": panel.netloc},
            verify=True, trust_env=False, follow_redirects=False,
            timeout=config.timeout_seconds,
            event_hooks={"request": [set_tls_hostname]},
        ) as http:
            # GET csrf-token success envelope's obj is a nonempty printable token.
            # POST login with form username/password, same CSRF header/cookie jar.
            # GET csrf-token again after login: session rotation may change token.
            # yield XuiSession(http, fresh_csrf); logout best-effort in finally.
            # Clear cookies unconditionally. Logout failure never masks the caller's
            # result/error and never claims session invalidation succeeded.
            yield authenticated_session
    finally:
        listener.close()
        await listener.wait_closed()
```

Catch only connection/setup errors outside yield and replace with static
`vpn_xui_connection_failed` from None; don't relabel caller exceptions as connection
failures. Validate configuration before connecting. Bound startup/login separately
from operation timeouts; cleanup occurs on cancellation as well. HTTPS must use real
certificate verification with original hostname; never downgrade to HTTP.
Host-key mismatch tests must prove password/panel requests were not sent.
Use existing `install_safe_logging` guard for application execution; do not weaken
global logging. AsyncSSH debug data must not be enabled by this component.

- [ ] Add actual local SSH integration tests: ephemeral Ed25519 host key written
  only into tmp_path known_hosts, SSH server permitting loopback forwarding, simple
  HTTP test server implementing CSRF/login/inventory/logout and tracking calls.
  Successful test must pass through `open_xui_session` without mocking AsyncSSH or
  HTTPX. Wrong/missing pin must fail before panel login. Assert listeners close and
  a synthetic password never appears in captured logs/errors. Use actual local TLS
  server with untrusted certificate to prove rejection; valid TLS SNI wiring can be
  asserted at HTTP transport seam without disabling certificate verification.
- [ ] Run GREEN focused session + existing identity/endpoint/barrier tests from
  backend with new local basetemp, then backend Ruff and whitespace checks.
- [ ] Specification review, quality review, fixes/re-review as needed; parent commit
  only the two owned files with `feat(vpn): add pinned SSH panel sessions`.

## Follow-on execution within the user's continuation

This task supplies the transport used by the next connected adapter. Next write
and execute the operation/policy contract against the pinned API, then add durable
staging/draining and real PostgreSQL recovery tests before removing the barrier.
No successful HTTP response proves runtime revocation; ambiguous remote writes
must keep the worker reserved until reconciled, and a later revoke must not be
overtaken by a late enable. Do not close the overall continuation merely because
this transport component passes its tests.
