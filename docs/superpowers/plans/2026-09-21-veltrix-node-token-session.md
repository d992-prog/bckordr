# Veltrix node-local API token authentication plan

> **For agentic workers:** Use superpowers:subagent-driven-development with TDD,
> specification review, then quality review. This extends the reviewed session;
> it does not enable endpoint mutations or remove existing barriers.

**Goal:** Authenticate the node-local HTTP session with a dedicated, owner-approved
3x-UI API token without moving that credential off the VPN node.

**Reason:** Worker 15 has no saved panel password and its owner does not know it.
The owner explicitly approved a separate administrative API token, root-only
node-local storage, unchanged password/existing tokens, and no public panel port.

**Pinned contract:** 3x-UI 3.8.5 commit
`7ef22f94c950ff09f0870e2295fa65ad5968742c` accepts
`Authorization: Bearer <token>` on base-path `panel/api/*`. Authentication sets a
request-local first-user identity, not a browser login session. API-token requests
are exempt from browser CSRF; privileges still depend on token scope. A successful
status call proves only that this read succeeded, not administrative capability.

## Task 1: Session token mode

Files: modify `backend/app/services/vpn_xui_node_http.py` and
`backend/tests/test_vpn_xui_node_http.py` only.

- [x] Write failing tests before implementation for this backwards-compatible API:

```python
NodePanelSession(panel_url, username=None, password=None, *,
                 api_token=None, timeout_seconds=10.0)
```

Exactly one mode is allowed: nonempty username+password with `api_token is None`,
or a nonempty string token with both username/password `None`. Mixed/incomplete
modes fail `vpn_xui_connection_invalid` before I/O. Token must be ASCII Bearer
token syntax `[A-Za-z0-9._~+/-]+=*`, length 1..4096; reject whitespace, control,
Unicode and header injection, rather than stripping/changing the value. Password
mode retains its original behavior and positional-call compatibility.

- [x] In token mode `__enter__` performs one bounded read-only GET to
  `panel/api/server/status`, using the token. Mark entered only after a successful
  envelope. Map rejected/invalid authentication responses to static
  `vpn_xui_auth_failed`; preserve `vpn_xui_request_failed` for transport failure.
  No login, CSRF, cookie or logout requests in this mode; no password fallback.
- [x] Every token-mode exchange sends only `Authorization: Bearer ...` for auth:
  never Cookie or X-CSRF-Token. Ignore response cookies, do not retain or replay
  them. No redirect/retry and no token in URL/body/log/exception/repr. The existing
  loopback socket, TLS original-host verification, request allowlist, deadlines,
  bounded JSON parsing, uncertainty and cleanup contracts remain unchanged.
- [x] Clear the token reference with all existing secrets after context exit or
  failed/interrupting entry. Closed sessions cannot be reused. Context exit does
  not revoke the durable token or claim server-session invalidation.
- [x] Real loopback tests cover exact wire headers/paths, no session endpoints,
  synthetic cookie ignored, status401/403/false/invalid response, no retry/fallback,
  POST read-only and mutation, uncertainty after malformed mutation response,
  secret-free exceptions/logs/repr, cleanup/interrupts and invalid/mixed input.
  Existing password-mode tests must all remain green. Add a successful observer
  composition test without altering the observer's implementation.
- [x] Run focused HTTP+observer+identity+endpoint tests, Python3.11 syntax and
  stdlib import check, Ruff, then independent spec/quality reviews and parent run.

### Verified local checkpoint

Initial RED: 17 constructor/grammar failures, then 9 wire/auth/cleanup failures.
Implementer: 495 passed in 61.97s. Independent spec reviewer: 495 passed in 62.25s;
Python3.11 syntax, stdlib import, Ruff passed. Independent post-spec quality review
approved with no actionable findings. Parent: **495 passed in 62.13s**. The prior
observation checkpoint's full backend suite passed **1194 tests, 8 skipped** in
192.80s before the token-mode extension. PostgreSQL-only skips are not live proof.

## Operational follow-up, separate from local implementation

Before token creation, strict SSH must match saved node trust. Confirm installed
CLI `setting -h` has `tokenName` (read-only check already passed), exact panel
version and root/private storage. A closed SQLite backup is kept on the node.

The CLI has no exclusive-create option: with any existing tokens it regenerates
the selected name. Choose a unique `veltrix-automation-<random>` name, verify no
matching name exists, and never reuse it as an automatic retry. Use
`setting -getApiToken=true -tokenName <name>`: **not** `-getApiToken true`, which
stops Go flag parsing before the name. Capture subprocess output only in node
memory, parse the single token, write exclusively to a root-only file, fsync,
and discard output. Never return token/hash/raw CLI output to the control host.
Verify all pre-existing token rows and credentials/inbounds remain unchanged;
only one new expected token row is allowed. On ambiguous failure stop, do not
rotate/recreate/retry automatically. No direct panel SQL writes or password reset.

Run a node-local read-only session probe against the actual panel using that
file. Export only version-match booleans, state/counts and static errors. Token
creation and read-only verification do not authorize port opening, client changes,
server restart or application release. The approved TCP443 rollout still requires
the complete adapter, backup and its acceptance gates.

### Actual operational checkpoint

The owner-approved named token was created after independent installer review,
strict SSH validation, worker reservation, private backup and a private-copy CLI
initialization rehearsal. Rehearsal and postchecks compared users, settings,
existing tokens, full normalized clients/attachments, inbound configuration,
hosts/fallbacks/nodes and other client policy tables; only documented live-counter
columns were excluded. All protected data remained unchanged. Token storage is
`/var/lib/veltrix-vpn/control-auth/api-token`, root-owned mode0600 below mode0700
directories; the token did not leave the node. The verified backup and rehearsal
copy remain private on the node at this checkpoint.

The first installer call returned an ambiguous control error. Read-only recovery
confirmed zero automation tokens, no installation files and no other node
operation processes. Full preflight passed before the separately initiated
successful invocation; no automatic creation/rotation retry was used.
The precise cause of that first control error was not established.

Live token auth/status, settings read and inbound list passed. The panel reported
3x-UI3.8.5, Xray26.9.9 running without an error, restart-on-disable=true and one
inbound. **Global client listing returned HTTP200, success:false, obj:null.** The
session correctly rejected it; its message/body was not exported. This upstream
read error was subsequently traced to a legacy text timestamp in the single
client attachment. The separately approved one-field repair is documented in
`2026-09-21-veltrix-client-link-timestamp-repair.md`: after private-copy rehearsal
and backup, the timestamp was converted to milliseconds with no other row/schema
changes and no Xray restart. Live global client listing now passes, as does the
new node-local observer for the existing owner's exact legacy configuration.
No client policy mutation, restart, port change, endpoint binding or application
deployment occurred as part of these checkpoints.
