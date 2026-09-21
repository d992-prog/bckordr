# Veltrix connected node executor implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Work in the existing customer-portal worktree; use the checkboxes below.

**Goal:** Apply one durable, identity-scoped client policy through the installed panel and verify the running Xray before returning an observed receipt.

**Architecture:** Compose the existing loopback panel session, configuration/identity checks and durable node journal. A node-only runtime reader pins the running process and its loopback HandlerService. The executor accepts a typed immutable request; only safe receipts leave the node. Control-side staging/dispatch remains a separate required integration, not an automatic side effect of importing these modules.

**Tech Stack:** Python3.11 stdlib on the node, pinned 3x-UI3.8.5 and Xray26.9.9, real SQLite/HTTP/subprocess tests, existing pytest/Ruff.

---

## Execution checklist

- [x] Task1: runtime reader, RED/GREEN, spec review, quality review, parent real-node read-only proof.
- [x] Task2: typed request/policy preservation and journalled executor, RED/GREEN, spec review, quality review.
- [x] Task3: real local HTTP+SQLite integration with late/partial failures and preserved other-client policy.
- [x] Parent full backend verification and committed evidence.
- [ ] Next release stage: durable control integration and transport acceptance; beta is not ready yet.

Task1 evidence: final focused suite125passed, Ruff clean, independent spec and
quality reviews approved. Actual final module twice matched the existing owner
on the pinned node with an unchanged process generation. Full backend run during
the final review passed1432tests with13known skips; that run collected121runtime
cases, followed by the final125-case run including four namespace regressions.
No application deployment or new public port accompanies this checkpoint.

Task2/3 evidence:133request/executor tests pass, independent spec and quality
reviews approved, stdlib-only imports and Ruff pass. Parent guarded live no-op
proof passed after fixing the real signed-int64 tgId contract; it passed again
after inbound-policy preservation was added. It attempted zero HTTP writes,
observed the same running owner process and removed its private scratch journal.
Final parent backend run with the authorized isolated synthetic PostgreSQL:
**1601passed,1skipped in397.58s**. The skip is the POSIX ownership/mode test on
Windows; the journal's separate Linux proof is recorded at its earlier checkpoint.
Whole-backend Ruff passed from `backend/`. The exact temporary cluster
`/tmp/veltrix-portal-test-18gp4z3o` was stopped and removed, its port45065 closed,
and the control service remained active. No live client creation, suspension or
revocation was performed by the proof or tests.

## Task1: node-only runtime reader

Create `backend/app/services/vpn_xray_runtime.py` and
`backend/tests/test_vpn_xray_runtime.py`. No SQLAlchemy, application settings,
SSH, panel auth or network mutation imports. No installation or production writes.

```python
@dataclass(frozen=True, slots=True)
class XrayRuntimeObservation:
    state: Literal["matched", "not_observed"]
    process_id: int
    process_start_ticks: int

class XrayRuntimeError(ValueError):
    code: str  # allowlisted static codes only

def observe_xray_client(*, port: int, client_uuid: UUID,
                        client_email: str, flow: str,
                        executable_path: Path = Path("/usr/local/x-ui/bin/xray-linux-amd64"),
                        config_path: Path = Path("/usr/local/x-ui/bin/config.json"),
                        timeout_seconds: float = 8.0) -> XrayRuntimeObservation:
    ...
```

Validate all inputs before IO: nonbool integer port1..65535, UUID object,
canonical printable ASCII email1..64 with no whitespace/control/URL separators,
flow empty or `xtls-rprx-vision`, finite nonbool timeout0<t<=30, absolute Paths.
Wrong input raises `vpn_xray_runtime_invalid`. Runtime incompatibility/unavailable
and identity conflict use `vpn_xray_runtime_unavailable` and
`vpn_xray_runtime_conflict`; raw filenames/commands/stdout/stderr/UUID never escape.

Pinned primary source uses `api inbounduser --server=127.0.0.1:PORT --timeout=N
-tag=TAG`, JSON `users` with each account `_TypedMessage_` equal to
`xray.proxy.vless.Account`, `id` and optional empty `flow`. Missing `users` is
the marshaler's empty-list representation. Missing inbound is nonzero exit,
never absence. All raw data remain local. Source references:

- https://github.com/XTLS/Xray-core/blob/v26.9.9/main/commands/all/api/inbound_user.go
- https://github.com/XTLS/Xray-core/blob/v26.9.9/common/reflect/marshal.go
- https://github.com/XTLS/Xray-core/blob/v26.9.9/proxy/vless/validator.go

Algorithm:

1. Validate executable/config are existing regular files, no symlink/reparse or
   symlink ancestor. On POSIX require root owner and no group/other write bits.
   Require Linux `/proc`; absence fails unavailable, not a synthetic success.
2. Find exactly one process whose `/proc/PID/exe` resolves to the expected
   executable. Read start ticks from `/proc/PID/stat` using text after its last
   `)`. Verify cmdline contains exactly one `-c`/`-config`/`--config` pair resolving
   through `/proc/PID/cwd` to the expected config. Refuse extra config-directory
   arguments. Pin executable file identity and process PID/start ticks throughout.
3. Bounded strict UTF8 JSON config read (2MiB, duplicate keys/nonfinite rejected).
   Require API `HandlerService`, exactly one API-tagged inbound listening on
   `127.0.0.1`, valid integer port, unique inbound tags, exactly one VLESS inbound
   at requested public port. Tags match `[A-Za-z0-9_-]{1,128}`. Every configured
   VLESS client must have a valid UUID and nonempty canonical email. Reject
   duplicate normalized UUIDs/emails within an inbound. VLESS UUID normalization
   zeros bytes6/7; email comparison is lowercase. An expected identity alias on
   another inbound is conflict, even if it differs in UUID bytes6/7 or email case.
4. Verify loopback API listener inode belongs to the pinned process using
   `/proc/PID/net/tcp` and `/proc/PID/fd` (LISTEN state0A, IPv4 127.0.0.1 and exact
   port). Before CLI execution require the process network namespace to equal
   `/proc/thread-self/ns/net`; pin and recheck both afterward. Otherwise loopback
   could address another namespace's service. No `ss` dependency in production
   module, no remote/DNS API addresses.
5. Execute exact binary `version` and require26.9.9. Query named users for every
   configured VLESS inbound via that binary's read-only command, using a single
   total deadline. Never shell=True or external command strings. Bound both output
   streams (2MiB each); on timeout/overflow terminate only the owned read-only
   child, reap it, close pipes. Preserve KeyboardInterrupt/SystemExit with cleanup.
6. Strict parse JSON and all user/account rows. Reject anonymous/malformed users,
   unsupported accounts and normalized collisions. Expected UUID or lowercase
   email must identify the same exact UUID/email pair on the target inbound only;
   different pair or inbound is conflict. Matching client flow must equal expected.
   No match returns not_observed, not an assertion of global credential absence.
7. Recheck process identity, executable/config file identity and config content
   digest, plus listener ownership after observations. Drift fails unavailable.
   Return only the three immutable safe fields above.

Anonymous users dynamically added outside managed operations are not enumerable
by this Xray API; even GetCount enumerates only the email map. The reader does not
claim to rule them out. A fresh protected inbound plus exclusive managed changes
and release acceptance establish the operational boundary. Configuration-file
consistency alone is not proof of applied transport; external acceptance remains.

Tests first, including exact typed account shape captured as synthetic data;
normalized collision and case alias; unsupported version/account; empty users
versus nonzero CLI; missing/multiple processes, wrong config/loopback/listener;
drift between reads; malformed/duplicate/nonfinite JSON and bounded output;
real slow/oversized subprocess cleanup; invalid input before any subprocess;
secret-free repr/errors; Python -S import. Filesystem/process inspection may be
mocked at the OS boundary on Windows, but parser and subprocess tests run real code.
Then parent runs the actual module read-only on the pinned VPN node; no test key
or restart is needed for that validation.

Commands from worktree/backend, unique `.pytest_cache` basetemp:

```powershell
$runtimeScratch = Join-Path (Get-Location).Path ('.pytest_cache/xray-runtime-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $runtimeScratch) { throw 'Scratch exists' }
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_xray_runtime.py -q --basetemp $runtimeScratch
.\.venv\Scripts\python.exe -m ruff check app/services/vpn_xray_runtime.py tests/test_vpn_xray_runtime.py
```

## Task2: typed durable request and connected policy executor

Create `backend/app/services/vpn_node_request.py`,
`backend/app/services/vpn_xui_node_executor.py` and matching test files.
No API route or lifecycle wiring until the durable control dispatcher is complete.

```python
@dataclass(frozen=True, slots=True)
class VpnNodeRequest:
    operation_id: UUID
    access_key_id: int
    generation: int
    action: Literal["provision", "suspend", "revoke"]
    target: VpnEndpointTarget  # repr=False
    client_uuid: UUID          # repr=False
    client_email: str          # repr=False
    sub_id: str                # repr=False; existing legacy empty value allowed
    expires_at_ms: int         # zero unlimited, never negative first-use durations
    traffic_limit_bytes: int   # zero unlimited
    created_at_ms: int         # persisted creation time for new client only
    allow_create: bool
    allow_shared_restart: bool

def serialize_node_request(request: VpnNodeRequest) -> dict: ...
def parse_node_request(value: object) -> VpnNodeRequest: ...
def node_request_digest(request: VpnNodeRequest) -> str: ...

def execute_node_client_operation(
    request: VpnNodeRequest, *, journal_directory: Path, database_path: Path,
    panel_factory: Callable[[], AbstractContextManager[NodePanelSession]],
    runtime_reader: Callable[..., XrayRuntimeObservation] = observe_xray_client,
) -> NodeOperationReceipt: ...
```

Strictly reject missing/unknown snapshot keys and invalid types before IO. The
serialized dictionary has `version:1` plus exactly the dataclass fields above;
UUIDs are canonical lowercase hyphenated strings, target is the exact complete
`VpnEndpointTarget` field set. IDs/generation1..2^63-1, numeric policy0..2^63-1,
created_at_ms1..2^63-1, booleans exactbool, canonical ASCII client_email as Task1,
sub_id `[A-Za-z0-9_-]{0,64}`. Only local VLESS RAW/TCP with security none/reality;
none has empty flow and no crypto fields, REALITY requires canonical32byte public
key, shortid, SNI/fingerprint and Vision. `allow_create` requires provision plus
REALITY and a nonempty sub_id; nonprovision requests have allow_create false. Shared restart is used
only for suspend/revoke. No NodePanelSession or journal access on invalid input.
Use static `vpn_node_request_invalid`, never echo values.

Compute digest using SHA256 of UTF8 JSON, sort_keys=True, separators=(',', ':'),
ensure_ascii=True, allow_nan=False. Roundtrip produces identical bytes/digest and
detaches nested containers. The executor creates NodeOperation from this digest;
it never accepts an unrelated caller-supplied digest. Wire data may contain the
persisted client credential, but repr/errors/logs and journal remain secret-free.

The trusted journal callback opens the panel session inside the gate, so a failed
authentication still leaves accepted revoke intent durable. Before every mutating
send, invoke mark_mutating; no retry after any possible send, no panel SQL writes.

### Pinned policy contract

Use 3x-UI3.8.5 SHA `7ef22f94c950ff09f0870e2295fa65ad5968742c` source. Complete
ClientRecord keys (all emitted, including zero/empty/null):

```python
CLIENT_FIELDS = {
    'id', 'email', 'subId', 'uuid', 'password', 'auth', 'flow', 'security',
    'reverse', 'privateKey', 'publicKey', 'allowedIPs', 'preSharedKey', 'keepAlive',
    'forwardedPorts', 'secret', 'adTag', 'limitIp', 'limitHwid', 'totalGB',
    'expiryTime', 'enable', 'tgId', 'group', 'comment', 'reset', 'resetDay',
    'resetMax', 'trafficReset', 'trafficResetDay', 'createdAt', 'updatedAt',
}
```

Record id is numeric; wire id is credential UUID. Rename uuid→id,
createdAt→created_at, updatedAt→updated_at; allowedIPs CSV→array; copy all other
fields exactly. No implicit partial-update semantics. Do not send list-only
inboundIds/traffic or get-only externalLinks/usedTraffic/tunnelAllowedIPs.

Neutral supported policy requires reset=resetDay=resetMax=limitHwid=keepAlive=0,
trafficReset='never', trafficResetDay=1, group='', reverse=null and empty other
protocol strings(auth/privateKey/publicKey/preSharedKey/secret/adTag/
forwardedPorts/allowedIPs). Existing nonnegative limitIp is preserved (legacy
owner uses it); new clients use0. Security '' or 'auto'. Exact effective/canonical
flow must match expected supported flow. Canonical createdAt/updatedAt may be0
for legacy rows; preserve creation time. Existing comment, tgId, subId and every
untouched value are preserved. No externalLinks or tunnelAllowedIPs mappings.

Live no-op composition caught a synthetic-fixture error: `tgId` is a signed
int64 in both pinned ClientRecord and wire Client, not a string. Preserve the
integer exactly (including negative chat IDs); new clients use0. Reject bool,
strings and values outside signed64. It is not a nonnegative policy counter.

Read-only live preflight found an existing nonempty password on the exclusively
VLESS owner record. Preserve this string unchanged; new VLESS clients use ''.
The pinned Xray VLESS account has no password field, and the panel carries the
value through updates. Do not clear an unrelated existing credential.

Important upstream limitation: 3x-UI3.8.5 full update generates a new subId if
both supplied and stored values are empty. For such a legacy record only no-op,
bulk enable and bulk disable are allowed; refuse a required full update before
mutation. Migration to a new subId is not authorized by this task. All new
managed profiles have a persisted nonempty subId. Source:
https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go#L572

Inbound trafficReset must explicitly be 'never'; client-level never alone is
insufficient because inbound reset jobs act even when the inbound is disabled.
Refuse unsupported/reset/HWID/tunnel policy before any mutation. Revoke is not
durable if upstream auto-renew can later set enable=true; do not silently alter
those advanced policies to make the check pass.

### Execution algorithm

1. Under the journal gate, collect complete local-ID-covered inbound and global
   client lists and pinned server status. Reuse existing identity/transport
   validation; retain raw responses node-only. Extend by strict canonical policy
   parsing and normalized UUID/email collision checks across the full inventory,
   including disabled and orphan records. Compare readonly local inbound IDs
   before/after; no scoped partial inventory can authorize a write.
2. Matched identity must be one exact UUID/email pair attached only to the expected
   local inbound; canonical subId equals request sub_id. GET clients/get/<email>
   confirms that same record/attachment and no external/tunnel mappings. Runtime
   reader runs before mutation. For provision require transport matched and desired
   nonzero expiry still in the future on the node. Existing missing client with
   allow_create false fails; it is never recreated on another inbound.
3. A missing client may be created only with allow_create and protected REALITY.
   Both complete canonical inventory and runtime named-user observation must show
   no target identity/normalized alias. New payload uses neutral defaults, durable
   UUID/email/nonempty subId/creation time and desired expiry/quota/flow. POST
   clients/add body is {'client': complete_wire, 'inboundIds': [N]}.
4. Existing provision: preserve full canonical fields; change only totalGB,
   expiryTime and enable=true. If all equal and runtime matched, no write. If only
   enable differs, use bulkEnable. Otherwise POST clients/update/<email>?inboundIds=N
   with full mapped wire object, including unchanged limitHwid. No delete/recreate.
5. Suspend/revoke: require exact present canonical identity (missing remains a
   safe preflight failure, not false revoke success). Require shared-restart
   permission in request before mutation. POST bulkDisable only when enabled;
   preserve all policy. Bulk body {'emails':[email]}, require changed exactint1
   and skipped absent/empty. Permanent revoke fence is the journal's sticky bit.
6. Capture readonly local client_traffics(id,email,up,down,reset_count) before and
   after for existing rows; no disappearing rows/identity change/decreasing counters
   or changed reset count. Use local SQLite, not master-overlaid list counters. Do not
   require exact byte equality while traffic is active. Any ambiguity after a send
   stays uncertain. No counter adjustment/reset writes.
7. After write, freshly collect canonical identity/policy/attachment, verify desired
   fields and unchanged untouched fields (updatedAt may change); then actual runtime
   must match enabled policy. For suspend/revoke require old Xray process gone and
   a new healthy process without that identity. Allow bounded polling of reads for
   an asynchronous restart; if the old generation persists, one allowlisted POST
   server/restartXrayService may be sent under the same marked operation, then
   bounded fresh runtime reads. No repeated forced restarts. Failure is uncertain.
8. Before returning, compare all unrelated canonical client policies/attachments
   unchanged and fresh transport observation for provision. Return callback success
   only after postconditions. Observed receipt is historical, never current policy
   authority; control finalization must revalidate generation and intent.

No raw record/panel exception is persisted or returned. Replay/stale/uncertain
journal handling remains exactly the existing journal contract. Reconciliation
cannot clear a possibly running panel handler merely because SSH timed out.

### Task3 integration proof

Use real loopback HTTP server implementing pinned response shapes plus actual
temporary SQLite panel ID/counter tables and the real journal; substitute only
the OS runtime-reader boundary in executor integration tests. Exercise:

- Create→suspend→resume→revoke, exact identity/URI ingredients, preserved counters
  and unrelated client/inbound policy; lower generation and post-revoke enable
  cannot send any HTTP mutation.
- Existing legacy preserved limitIp/subId/createdAt; unsupported renew/HWID/tunnel
  policy sends nothing; same request replay sends nothing.
- Canonical stale/split/alias/orphan identity, missing attachment/coverage, changed
  transport/version or runtime mismatch are not successful mutation results.
- Auth/preflight failure durable revoke; server writes then false/malformed reply;
  lost response; post-read drift; runtime never applies; old process never stops.
  After possibly sent mutation, every later write stays blocked.
- Bulk skipped/bool changed, empty success objects, top/nested nodePending; no
  automatic retry or fallback. Verify journal mutating committed before server sees
  the first mutating byte/request. Partial add is never adopted on a new identity.
- Secrets in synthetic exception/body never appear in receipt/repr/formatted error,
  stdout/log capture or journal contents.

Task2/3 must pass independent spec then quality review and parent test run before
control queue wiring. One implementation agent at a time; parent works on control
contract and test environment while node code is developed.
