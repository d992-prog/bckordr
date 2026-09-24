# Protected-endpoint adapter: source findings

Date: 2026-09-21. Preparatory source review, **not a live API acceptance test**.
The preceding read-only node inspection reported 3x-UI 3.8.5 and Xray 26.9.9.
This review made no node calls, created no panel session/token and changed no setting.

## API identity and scope

The version-pinned client controller exposes `/clients/add`, `/clients/update/:email`,
`/clients/bulkDisable`, `/clients/del/:email` and `/clients/:email/detach` under the
panel API prefix. An implementation must follow this version's request schemas,
not assume older inbound-scoped `addClient`/`delClient` routes. See the
[3.8.5 client controller](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/controller/client.go).

An inbound filter on update does not isolate every field: the service also updates
the global client record, including enable and other settings. Credential omission
has preservation logic, but that does not establish patch semantics for every
field. Global deletion and selective detachment are different operations. Inspect
the complete verified record and all its attachments before any mutation; never
infer identity from email alone. See the
[3.8.5 client service](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/client_crud.go).

## Restart and effective revocation remain a release gate

`RestartXray(false)` first attempts a hot application, but failure can lead to
stopping and starting the shared process. The hot path also deliberately declines
user removal when the restart-on-disable setting requires dropping live sessions.
Therefore changing that setting to false would not prove either a no-restart
guarantee or complete immediate revocation. See
[3.8.5 Xray reconciliation](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/xray.go).

In Xray 26.9.9, VLESS `RemoveUser` removes the credential from the validator (and
handles reverse-proxy state); it does not explicitly close ordinary existing
connections. The panel source also documents this distinction. The inference for
our acceptance tests is that failure of a **new** login alone is insufficient:
already-authenticated traffic must be tested separately. See the
[26.9.9 VLESS handler](https://raw.githubusercontent.com/XTLS/Xray-core/v26.9.9/proxy/vless/inbound/inbound.go).

The original design required both effective suspension/revocation and no routine
interruption of another client's control connection. The owner has now accepted
shared reconnections on disable/expiry for the ten-person closed beta (see below).
A plain API wrapper still does not prove effective removal or successful recovery.
Do not silently extend the exception, patch/replace the installed panel, change
its settings, or declare a best-effort hot update sufficient.

## Current safe increment

The recorded-endpoint resolver and legacy mutation barrier are local intermediate
work. Bound profiles must stay pending with a static diagnostic until the adapter
and durable intents exist. Existing unbound behavior is temporary compatibility,
not the final migration policy. Do not deploy this checkpoint or create production
endpoint bindings. Next work also needs verified legacy import, strict SSH trust,
remote identity checks, and real PostgreSQL concurrency/recovery evidence.

## Pinned inventory contract for identity preflight

The next local helper consumes decoded responses without fetching or retaining
raw data. It is configuration observation only; it does not authorize mutation.
The pinned [global list implementation](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_lookup.go#L171)
lists normalized client records without user filtering or pagination. The
[response type](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client.go#L19)
flattens those fields and adds `inboundIds` (possibly null for an orphan).
Record `id` is numeric; `uuid` is the credential. Per-inbound flow can override
the global value, so this identity-only check must not compare global flow.

In contrast, the [inbound list](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/inbound.go#L180)
is scoped to the authenticated panel user. Its stored `settings.clients` may
contain stale identities, while normalized records supply runtime configuration.
Require the exact UUID/email pair, exclusive attachment and consistent enabled
state in both lists. Refuse ambiguity instead of repairing it automatically.
The helper's `not_observed` is deliberately weaker than absence: two non-atomic,
potentially incomplete responses cannot establish safe creation or revocation.

Valid [inbound JSON serialization](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/database/model/model.go#L165)
emits nested settings objects. Malformed saved settings can become strings and
must be rejected by the pinned parser. No generic string-decoding fallback is
needed for this contract. Unknown panel versions require separate validation.
The [mutation pending response](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/controller/util.go#L175)
has `obj: {nodePending: true}` and is not an inventory. Missing this flag in a
list response does not prove the running core matches the panel's database.

The later transport checker must use the actual nested
[REALITY schema](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/frontend/src/schemas/protocols/security/reality.ts#L3):
`realitySettings.serverNames`, `shortIds` and `settings.publicKey/fingerprint`.
Identity comparison alone checks none of these and must not mark an endpoint
ready. A trusted full collector, SSH pin enforcement, complete transport validation,
durable intents and runtime postconditions remain separate release requirements.

## Application integration still required before release

Source audit at `a39dedd` found these caller boundaries; the local barrier does not
claim to replace their current authorization or transaction policy:

- `vpn_provisioning.py` is the shared mutation service for manual admin actions,
  customer archive and lifecycle processing. Its old scripts operate on normalized
  client records/attachments globally. Merely changing a payload's inbound ID is
  not an endpoint-aware implementation.
- `vpn_lifecycle.py` and the admin retry route still use worker-default eligibility
  and node selection. Bound records must later use endpoint-aware eligibility,
  preserve the assigned worker, and refresh locked state before remote actions.
  The existing attack queries in `vpn_policy.py` and `worker_decommission.py`
  include planned/running attack runs but not verifying; extend and test that
  protection before endpoint-aware operations are enabled. The rollout helper's
  broader attack guard is not evidence that every application path has it.
- Subscription synchronization stages desired policy. Manual revoke must remain
  final across retries; callers must not overwrite a newer revoke with stale
  provision/suspend completion.
- Existing `flush()` before SSH is not a durable commit. A later operation-intent
  design must cover a crash after remote success and before result persistence,
  with real independent PostgreSQL connections and bounded recovery.
- Decommission must retain node credentials while bound revocation is incomplete.
  Missing-node/no-URI shortcut branches in lifecycle/archive also need explicit
  endpoint-aware review during final integration, even though valid persisted
  bindings currently require a matching non-null worker through DB constraints.

Do not turn the temporary unbound compatibility path into a permanent fallback.
No control/lifecycle refactor, remote import or endpoint selection was performed
as part of this source audit.

## Follow-up: owner-approved closed-beta disconnect exception

On the next continuation, the parent checked the official
[3.8.5 release notes](https://github.com/MHSanaei/3x-ui/releases/tag/v3.8.5), which
explicitly describe manual disable/delete as well as expiry/quota disabling under
the restart-on-client-disable setting. They distinguish credential removal from
ending established traffic. This confirms that the prior source finding is an
upstream-described behavior, not merely an inferred application bug.

The upstream [disconnect clarification](https://github.com/MHSanaei/3x-ui/pull/6551)
also explains that the IP-limit helper's temporary credential removal is not a
per-client live-session shutdown; its enforcement relies on fail2ban at the IP
layer. A public-IP ban is not a suitable substitute for our profile-level revoke:
users can share an address behind NAT, and an account can reconnect elsewhere.
No IP ban, panel login, setting change, restart or empirical node test was performed.

The owner explicitly answered: “Да, для закрытого теста допускаем переподключение
(рекомендую)”. This permits shared reconnections on disabling access or subscription
expiry for the first ten-person closed beta; it is not a general restart policy
for public release. The amended design still requires identity preservation,
effective removal of established traffic and recovery of the control profile.
Add/update failure paths may also restart the core; report and test these rather
than promising unconditional continuity. This design choice does not authorize
opening a port or immediately modifying the running node. No reconnection duration
has been measured. Public-release continuity remains a separate design decision.

## Node-local execution and runtime evidence (2026-09-21 continuation)

The panel's raw inbound response includes REALITY private keys. The approved
private-key boundary therefore rules out a control-side tunnel HTTP client that
downloads that response. The new transport and collector execute on the node;
only an explicit safe observation is eligible to cross SSH. Do not serialize the
session's raw response or arbitrary exceptions into remote command output.

The installed Xray's [read-only inbound-user CLI](https://github.com/XTLS/Xray-core/blob/v26.9.9/main/commands/all/api/inbound_user.go)
can query its local HandlerService using an inbound tag. Omitting email requests
the named-user inventory; a missing inbound is an RPC error, not proof that a
credential is absent. This offers runtime evidence independent of the panel's
cached server/status. It still does not replace external TLS/UDP acceptance or
prove that previously established traffic stopped.

Critically, the pinned [VLESS validator](https://github.com/XTLS/Xray-core/blob/v26.9.9/proxy/vless/validator.go)
normalizes credential bytes 6 and 7 to zero for runtime lookup and lowercases
email identities. Exact panel UUID uniqueness alone therefore does not exclude
runtime collisions. Its named-user enumeration also omits anonymous users. The
later runtime/mutation checker must handle these distinctions; the existing pure
panel identity observation deliberately makes no runtime authorization claim.
No production credentials were inspected or changed for this source finding.

For the pinned panel, create uses a wrapped `client` plus `inboundIds` list, while
update takes flat client fields and a scoped inbound query. `limitHwid` belongs
inside the creation wrapper but beside the flat update fields. Both success
responses may have null obj, not a refreshed record. Canonical policy must be
reread and preserved; success does not permit interpreting the request as a
partial patch. References: [client payloads](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/internal/web/service/client.go),
[controller](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/internal/web/controller/client.go).

A separate node-local durable journal is planned for interruption safety. Its
gate stays held for the execution, while intent/receipts commit independently.
An uncertain write blocks subsequent writes even after the SSH process dies;
there is no claim that a timeout cancels an already running panel handler. The
journal's initial creation must be explicit: losing an initialized journal must
never silently erase permanent-revoke or uncertain-operation history.

## Live authentication preflight (2026-09-21)

A strict-known-hosts, read-only control-database check found that worker 15 has
a panel URL and username but no `vpn_panel_password`. The node-local session
probe stopped at that check, before panel login. No password reset, new API token,
client change, panel database write, restart or firewall change was performed.
The owner was asked to save the known panel password through the existing
"Пароль 3x-UI" field, not send it in chat. Local implementation/tests continue
without treating this unavailable live check as a pass.

The old maintenance helper's `setting -getApiToken true` must not be used as a
read-only diagnostic. In the pinned upstream [GetApiToken implementation](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/main.go#L467),
it creates a token or regenerates a named fallback token, invalidating the prior
one. Existing tokens are hashes, not retrievable plaintext. Running that helper
would create/change privileged access and is not a substitute for authorized
authentication. The helper was inspected but not invoked.

## Interrupted-run recovery and policy compatibility

The synthetic PostgreSQL rehearsal left `/tmp/veltrix-portal-test-k0y567tf`
running after the local tool session was lost. A fresh strict-SSH recovery
verified its exact data directory, PID command line and port60301, stopped it,
then deleted only that temporary directory. The directory is absent, its listener
is closed and `domain-drop-control.service` is active. Synthetic data were
removed; no production database restore or migration was performed.

Fresh read-only panel observations confirmed one owner client and one inbound,
both reset policies `never`, neutral renewal/HWID settings, no external or tunnel
attachments, and the expected local counter schema. The owner has an empty subId
and a nonempty password field. Neither value was exported or changed.

The pinned [full-update implementation](https://github.com/MHSanaei/3x-ui/blob/v3.8.5/internal/web/service/client_crud.go#L572)
generates a subId if both old and requested values are empty. An executor cannot
promise full-update identity preservation for that legacy record. No-op and bulk
enable/disable remain possible; an expiry/quota change requiring full update must
fail before sending until a separate migration is authorized. New managed
profiles must have a nonempty persisted subId from their first request.

An existing password string can be preserved for the independently verified
exclusive VLESS attachment: the [VLESS account schema](https://github.com/XTLS/Xray-core/blob/v26.9.9/proxy/vless/account.proto)
does not use it. This does not establish why it was originally populated.
Deleting it merely to fit a neutral test fixture would be an unauthorized change.

## Durable control integration audit checkpoint

The existing per-event-loop mutation lock is not a PostgreSQL concurrency fence.
Keep the endpoint-bound mutation barrier until staging, claim, execution and
finalization are connected. The minimum persisted data are key generation,
sticky revoke intent, verified client email/subId, and an operation row containing
the exact request snapshot/digest and claim identity. Reuse existing subscription,
worker and endpoint records; do not add a second policy engine.

Required integration corrections:

- Commit staged policy and operation before SSH; never perform remote IO inside
  the staging transaction. Claims reserve the worker durably before execution.
- Use one lock order: customer, subscriptions by ID, keys by ID, workers by ID,
  endpoint, operation. Refresh locked ORM state before applying caller changes;
  do not overwrite already staged unflushed changes with a later refresh.
- Lifecycle currently marks expired subscriptions before locking. Recheck the
  expiration under a fresh lock so a concurrent extension is not overwritten.
- Archive, subscription edits, manual retry/revoke, compatibility DELETE and
  worker decommission must route through the same bound-key intent staging.
- Compare claim identity, generation and current policy during finalization.
  A historical provision receipt cannot overwrite a newer revoke or suspension.
- A timed-out claim with possible remote writes remains reserved and requires
  reconciliation; lease expiry alone cannot authorize a competing mutation.
- Domain protection must include planned tasks and every task associated with
  a verifying run (its tasks may already be cancelled). Recheck reservations
  after worker row locks, not only before them. Include queued operations to
  protect the interval before dispatch and between superseding policies.
- Maintenance and decommission must respect reservations and retain SSH/panel
  credentials while revocation remains unresolved. Do not bypass this through
  an empty config URI or missing-node shortcut for a bound key.

These are remaining implementation requirements, not implemented behavior.
Real PostgreSQL tests must cover rollback, two claimers, stale ORM reads,
provision versus revoke/archive/expiry, extension versus expiry, reservation
races, uncertain claims, endpoint disable and migration idempotency. The earlier
57-pass PostgreSQL run covered existing endpoint migrations and portal auth;
it did not test this still-unimplemented operation queue.

## Runtime trust prerequisite and authorized ownership repair

The actual new runtime reader initially refused to run: `/usr/local/x-ui`,
`/usr/local/x-ui/bin` and `/usr/local/x-ui/bin/xray-linux-amd64` belonged to
UID/GID1001. A read-only check found no account with that UID; both the panel
service and running Xray use root. The configuration file already belonged to
root. Trust checks were not weakened to accept this installation state.

Automatic safety review rejected changing production file ownership without
specific consent. The owner then explicitly authorized exactly these three
paths. Only their UID was changed to root; GID, permission bits, inode/device,
binary content and configuration content were preserved. The same PID/start
generation remained running. No VPN restart occurred. Original ownership and
mode metadata are saved root-only at
`/var/lib/veltrix-vpn/control-auth/xray-archive-owner-before.json`.

Afterward, two calls of the actual runtime module on the node both matched the
owner's existing credential and observed the same process generation. This is
read-only runtime evidence, not external connection acceptance, transport
readiness, a completed mutation executor or readiness to invite friends.

## Connected executor and lifecycle regression checkpoint

The node journal is now composed with typed, digested requests, loopback panel
operations, canonical policy/counter verification and the actual Xray reader.
The new modules remain disconnected from application routes and the lifecycle
dispatcher until durable control intent/claim/finalization is implemented.

A guarded actual-node no-op proof explicitly rejected every non-GET or mutation
attempt and used only a disposable private journal. It caught a fixture error:
`tgId` is signed int64 in both pinned ClientRecord and wire Client, not a string.
The corrected parser preserves it unchanged, uses0 for new clients and rejects
bool/string/out-of-range values. This was checked against the pinned
[model source](https://github.com/MHSanaei/3x-ui/blob/7ef22f94c950ff09f0870e2295fa65ad5968742c/internal/database/model/model.go).

Independent spec review also demonstrated false success when unrelated inbound
policy changed. Fresh node-only snapshots now compare all inbound policy,
including other effective clients. Only live up/down/clientStats telemetry and
the exact managed UUID/email entry on the target inbound are excluded. Fifteen
drift regressions failed before this correction; request/executor tests now
pass133cases. The actual corrected composition returns observed with zero write
attempts, an unchanged process generation and owner runtime matched; its scratch
journal was removed. This is **not** a production mutation/connection acceptance
test and does not make the protected endpoint or invitations ready.

Two earlier control-audit findings have separate bounded corrections:

- Shared domain protection includes planned work and every task status on a
  verifying run; decommission uses the same predicate (`a67f25a`).
- Expiration selects IDs then locks and refreshes each subscription before
  rechecking its status/date. This prevents a candidate from overwriting a
  concurrently committed extension or cancellation. Four actual PostgreSQL
  tests observe the blocking editor connection and verify extension, unlimited,
  cancellation and still-due outcomes. This is not a global concurrency or
  durable-operation-queue proof.

The unfinished control-integration requirements above still apply otherwise.
No application deployment, endpoint binding, firewall opening, invitation issue,
owner UUID/link change or VPN restart accompanies these local corrections.

Final verification:1601backend tests passed with1POSIX-only skip on Windows,
including the real isolated-PostgreSQL tests; whole-backend Ruff passed. Both
independent executor reviews and the bounded-expiry review approved. The exact
synthetic cluster `/tmp/veltrix-portal-test-18gp4z3o` was stopped and removed,
its port45065 was closed and the control service remained active. The earlier
short-lived rehearsal `/tmp/veltrix-portal-test-tpum14jb` also cleaned itself on
stdin EOF before the interactive tunnel was started; neither cluster remains.

## Durable control queue final checkpoint (2026-09-22)

The application now has a locally implemented durable endpoint-control queue:
idempotent schema migration, immutable request staging, PostgreSQL-safe claim and
worker reservation, generation/token compare-and-set finalization, sticky revoke
intent and explicit uncertain-state retention. A stale operation is superseded
without changing its key, including when policy/identity changed after staging;
claim continues to the next valid operation in the same call. Generation values
are rejected before flush when the next value would exceed PostgreSQL `INTEGER`.

Independent final spec review approved the full schema/stage/claim/finalize
behavior. Independent quality review found and re-approved fixes for stale-first
queue traversal and the generation boundary. The final complete backend run at
`3bfe59d` passed `1727` tests with `5` expected platform skips against a fresh
isolated PostgreSQL. Whole-backend Ruff and `git diff --check` passed. The exact
synthetic cluster `/tmp/veltrix-portal-test-g3qdsfi3` was stopped and removed,
its listener closed and `domain-drop-control.service` remained active.

This is a local integration checkpoint, not a production cutover. No dispatcher
currently transports claimed requests; strict host-key-pinned SSH is not wired
to the queue, the node-local modules are not deployed by this application branch,
and lifecycle/API/routes still use the legacy production behavior. The production
database was not migrated and this branch was not deployed. TCP 443 remains
closed for the new path, no protected REALITY inbound was created, and the
owner's legacy endpoint, UUID, key and working link were not changed. Friend
invitations, public beta access and payments remain disabled.

The next implementation stage must add the strict host-key-pinned dispatcher and
deploy/verify the node-local modules before any production path is allowed to
enqueue or execute these operations. API/lifecycle cutover, protected endpoint
creation, connection acceptance and invitation release stay behind later gates.

## Strict dispatcher and reservation final checkpoint (2026-09-22)

The strict control-to-node path is now implemented as a callable local component:
an exact stdlib zipapp and root-only node entrypoint, literal Ed25519-pinned
AsyncSSH transport with ambient trust/auth disabled, transaction-separated
claim/network/finalize dispatch, and cross-system worker reservations covering
attacks, maintenance, sensitive edits and decommission. It has no application
runtime, startup, API or lifecycle caller.

Independent final specification and quality reviews approved the complete change.
After a FastAPI 0.138.2 test-compatibility correction in `11cd172`, a fresh
isolated PostgreSQL 14.24 cluster on the control server passed the complete backend
suite: `1892 passed, 2 skipped`. Whole-backend Ruff passed, and the server's
Python 3.11.0rc1 separately passed all `51` zipapp/entrypoint tests. The temporary
cluster, source and script were deleted, its listener closed, and the production
service remained active.

This is still not a production cutover. The control database was not migrated,
the node bundle/trust/config/journal were not installed, and no request can be
scheduled through the new dispatcher. TCP 443, protected REALITY inbound creation,
the owner UUID/link, invitations, public access and payments remain unchanged.
The next stage is limited to the reviewed one-shot deployment plan in
`docs/superpowers/plans/2026-09-22-vpn-strict-node-deployment.md`; runtime remains
blocked on tested database/server timeouts and ambiguous-COMMIT reconciliation.
