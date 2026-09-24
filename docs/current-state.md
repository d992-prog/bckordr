# Current State

## Public-trial release-candidate checkpoint (2026-09-24, local only)

The product now has a release-shaped, non-payment public trial path, but this
checkpoint is deliberately fail-closed and **not deployed**. No production
database, Telegram webhook, VPN node or feature flag was changed. Payments are
still intentionally absent and remain the final integration stage.

### Customer, bot and provisioning behavior

- A Telegram identity can use at most one Veltrix trial across the bot, cabinet
  and historical friend-invitation path. A redeemed friend invitation consumes
  the same one-time right. A used, expired, suspended or revoked trial never
  becomes a second trial.
- The bot shows `Получить 7 дней` only while the public-trial flag is on.
  `/start` only offers the trial; it does not activate it. Activation is accepted
  only in a private chat and is committed before Telegram is answered. A retry
  after a delivery failure reuses its stored bounded result without creating
  another subscription, key or control operation. The stored replay context
  contains only an outcome and optional access-key ID, never a UUID, connection
  URI or raw internal error.
- The cabinet admits a new Telegram identity only while the public-trial flag is
  enabled. Its status endpoint is authenticated and activation additionally
  requires a CSRF token. They return only `disabled`, `available`,
  `capacity_paused`, `preparing`, `active` or `used`. After activation, the
  browser makes one status request at a time every two seconds for at most 30
  attempts; manual refresh remains available.
  A session-generation guard prevents an old poll from updating a new session.
  Turning the admission flag off denies fresh identities without stranding an
  existing, unexpired and non-revoked public trial while its valid release ID
  remains configured. OIDC admission is locked and rechecked before a session
  is issued.
- Activation atomically creates the existing seven-day subscription, one
  endpoint-bound VLESS key and one durable strict-dispatcher provision operation.
  The profile remains `preparing` until that operation succeeded and the active
  key has a connection URI. The URI is shown only through the existing protected
  cabinet profile flow.

### Capacity, notification and retry behavior

- Public allocation considers only a verified, ready REALITY endpoint with a
  positive explicit capacity and a fresh healthy worker. The worker must be
  enabled, unarchived, VPN-ready and free of existing safety blockers. Health is
  stale after the configured 300-second window.
- Capacity counts `pending_sync`, `syncing`, `active`, `pending_suspend`,
  `suspended`, `pending_revoke` and `failed`; `revoked` does not occupy a slot.
  Selection is deterministic by lowest utilization and endpoint ID. Activation
  locks the worker and endpoint in that order and revalidates health and capacity
  before commit, so concurrent users cannot oversubscribe a one-slot endpoint.
  Friend-invitation redemption uses the same locked selector and capacity pool.
- The strict dispatcher can run for a public-only release when the public flag,
  valid release ID and exact public readiness marker are present; it does not
  require the friend-beta flag. It still fails closed unless dispatch is enabled,
  public portal access is closed and an enabled release path has its exact marker.
- The admin VPN workspace lists occupancy and permits only the capacity limit
  and warning percentage to be edited. Limits are validated as 1..100000 or
  unset; warnings are 1..100. Changes produce numeric audit details and do not
  expose transport credentials.
- A ready notice is eligible only for an active key with a URI, an active and
  unexpired active/trial subscription, an active customer and a Telegram ID.
  Workers claim one row with `FOR UPDATE SKIP LOCKED` and commit the claim before
  sending. Success sets `ready_notified_at`; an ordinary failure clears the
  claim, records a bounded event and waits one minute before retry. Sending is
  bounded to 30 seconds, and a claim older than two minutes can be reclaimed.
  The runtime keeps at most one notification task. The message never contains a
  VPN URI.
- Delivery is intentionally at-least-once at the Telegram boundary: if Telegram
  accepted a message but the process died before recording success, the stale
  claim can be retried and the customer can receive a duplicate notification.

### Fail-closed defaults and production boundary

```env
VPN_PUBLIC_TRIAL_ENABLED=false
VPN_PUBLIC_TRIAL_RELEASE_ID=
VPN_PUBLIC_TRIAL_PLAN_SLUG=trial-7d
VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300
VPN_READY_NOTIFICATIONS_ENABLED=false
```

Production enablement requires all of the following in addition to reviewed
code: the exact `vpn_public_release_ready_v1` database marker matching a
lowercase 64-hex release ID, an active `trial-7d` plan with a seven-day duration
and one-device limit, configured endpoint capacity, a fresh healthy endpoint,
the strict dispatcher, a second externally verified production VPN node, a
reviewed migration backup and separate deployment approval. The public-trial and
ready-notification flags remain off; this checkpoint performs no rollout.

### Local release-gate evidence

- The desktop and mobile browser smoke used a disposable SQLite database, a fake
  seven-day plan and a fake ready endpoint. It exercised signed Telegram Mini
  App admission, the available card, one activation, preparing state, automatic
  and manual refresh, a capacity-paused second identity, and an admin capacity
  edit from 1/80% to 2/75%. No production node or Telegram webhook was reachable.
- The browser run observed bounded single-flight polling; the exact 30-attempt
  ceiling is covered by the frontend test suite rather than a 60-second visual
  wait.
- The final PostgreSQL evidence is a composite rather than one uninterrupted
  run. At the current code revision the full real-PostgreSQL run recorded 2,321
  passes and the one expected Windows skip for POSIX ownership/modes before the
  external SSH tunnel reset; exactly two Telegram/PostgreSQL tests then reported
  connection-loss setup errors. Both interrupted node IDs passed on a fresh
  disposable PostgreSQL cluster (2/2, no skips). Together these results cover all
  2,324 collected backend node IDs at the current revision, with no PostgreSQL
  failure or PostgreSQL skip and only the POSIX-only check excluded.
- The independent local backend run recorded 2,245 passes and 79 skips: 74
  real-PostgreSQL cases covered by the composite gate above, four Windows tests
  for unavailable symlink creation and the same POSIX ownership/mode check.
  Ruff passed for the complete backend application and test tree. The frontend
  recorded 72/72 passing tests, and the production Vite build produced both the
  admin and cabinet entries. `git diff --check` passed.
- Post-integrated-review verification was focused real PostgreSQL plus a fresh
  full local run, not another uninterrupted full environment run, so the honest
  composite evidence above remains unchanged. The complete public-trial
  PostgreSQL file passed 7/7 with no skip in 203.24 seconds, including public
  versus friend contention for one capacity slot. Independent cleanup confirmed
  the tunnel port closed, no matching temporary cluster directory and the
  control service active. The refreshed local backend passed 2,256 tests and
  skipped 80: 75 real-PostgreSQL cases, four unavailable Windows symlink cases
  and the POSIX-only ownership/mode check. Full Ruff, all 72 frontend tests, the
  two-entry Vite production build and `git diff --check` passed.
- Every disposable PostgreSQL run used a loopback-only remote listener through
  the SSH tunnel. After the transport reset, the single abandoned cluster was
  stopped and removed only after its directory, pidfile, port and process were
  validated. A separate final probe confirmed the temporary port closed, no
  matching temporary directory and the control service still active.

## Friend invitation closed-beta application checkpoint (2026-09-23, local only)

This is the preceding closed-beta checkpoint. Continue in
`.worktrees/veltrix-customer-portal`, branch `codex/veltrix-customer-portal`.
The approved design is `64a3972`, the execution plan is `1255aed`, and the
application implementation is the commit range `ebebebc..1899ef7`.

### Implemented and verified locally

- The admin can manage ten durable invitation slots. A raw Telegram link is
  returned only by create/rotate, held only in the immediate component state,
  and never returned by list. Redeemed and disabled slots are not reused.
- Telegram activation validates and redacts the payload before persistence,
  atomically creates one seven-day trial, one stable endpoint-bound key and one
  durable control operation, and safely reuses the same records on replay.
- Friend Mini App/OIDC/session admission now rechecks the exact invitation ->
  key -> subscription -> customer chain. Expiry, disable and customer archive
  close admission; an approved extension can restore only a subscription-suspended
  key, never an explicitly revoked key.
- The strict dispatcher has a lazy dedicated PostgreSQL pool, bounded database
  timeouts and one sequential runtime task. It runs only when every application
  and database-visible readiness gate is exact; uncertain mutation outcomes are
  never resent automatically.
- The Russian admin panel exposes slot state, identity, expiry, provisioning
  status, one-time copy, guarded retry and confirmed disable without retaining
  raw invitation links in the application root state.

### Final local evidence

- The focused invitation/Telegram/portal/lifecycle/control acceptance set passed
  **502 tests with zero skips in 246.75s** against a fresh isolated PostgreSQL
  14.23 cluster on `127.0.0.1:62747`.
- The complete backend passed **2095 tests with 5 skips in 462.66s** against the
  same cluster. The five skips are Windows-only journal limitations: four symlink
  cases and one POSIX owner/mode case; the isolated journal confirmation was **83
  passed, 5 skipped in 4.65s**. No required PostgreSQL case was skipped.
- Whole-backend Ruff passed. The frontend passed **61 tests with zero skips**,
  and its TypeScript/Vite production build succeeded. Whitespace checks passed.
- Full verification exposed and fixed two test-only assumptions: endpoint
  migrations must remain contiguous rather than forever be the final migrations,
  and invitation-link parsing must preserve a valid token containing internal
  `i_`. The PostgreSQL race now forces that token shape deterministically.
- Fresh independent specification and quality/security reviews approved the
  complete range. Review follow-up added an initial-claim PostgreSQL race which
  proves the loser replays a committed activation after winner delivery failure,
  prevented a late failure write from overwriting successful replay, rejected
  archived workers during readiness, and removed stale copied links from the UI.

### Production boundary and next gates

- This checkpoint is **not deployed**. No production schema or setting changed,
  no real invitation exists, and no friend was admitted. The owner's UUID,
  working 8443 link, Xray generation and payment state are unchanged.
- `VPN_FRIEND_BETA_ENABLED=false`, `VPN_CONTROL_DISPATCH_ENABLED=false` and
  `VPN_PORTAL_PUBLIC_ACCESS=false` remain the required defaults. The application
  therefore fails closed even if this code is deployed without rollout data.
- Before the first real link: deploy and verify the strict node zipapp/trust/config
  and journal; rehearse the migration on a closed production copy; complete the
  timeout and ambiguous-COMMIT reconciliation gates; create and externally accept
  the protected REALITY endpoint on TCP 443; then perform one controlled friend
  connection and individual revoke. Only after that proof may the remaining beta
  slots be used.
- Payments remain intentionally postponed until the VPN product is working and
  the closed beta has been accepted.

## Strict VPN dispatcher and reservation checkpoint (2026-09-22, local only)

This is a historical checkpoint, superseded by the current 2026-09-24
public-trial release-candidate checkpoint above. Its work remains in
`.worktrees/veltrix-customer-portal`, branch `codex/veltrix-customer-portal`.
The strict dispatcher is callable and fully tested, but it has no runtime,
startup, API or lifecycle caller and is **not deployed**. Do not enable enqueue
or scheduling from this branch.

### Implemented and verified locally

- A deterministic stdlib zipapp now contains the exact node-runner manifest.
  Its bounded entrypoint requires UID 0, strict private root-owned config/token/
  x-ui database/journal paths, exact JSON input and an exact receipt. It never
  initializes or replaces the durable journal implicitly.
- The control transport requires a literal raw Ed25519 pin from a private
  `known_hosts` snapshot. SSH config, agents, default keys, X.509 trust, PKCS11,
  GSS, host-based and keyboard-interactive fallbacks are disabled. The command
  is fixed, the request travels only on stdin, output is bounded concurrently,
  and failures never include raw remote output or secrets.
- The dispatcher commits the claim before SSH, closes that database session,
  performs network I/O without a transaction, then finalizes in a fresh
  transaction. Its current `finalize_timeout` bounds only the pre-COMMIT gate;
  once COMMIT starts, the dispatcher waits for the driver outcome and session
  close. Cancellation and a lost COMMIT response never trigger an automatic
  resend: the row may already be finalized or may remain claimed, so either
  result requires explicit reconciliation.
- Cross-system reservations now serialize VPN control against domain attacks,
  VPN maintenance, sensitive worker edits and decommission. PostgreSQL worker
  locks are acquired in ascending ID order and all losing paths recheck after
  the lock, including immediately before the legacy SSH callback.
- Independent final specification and quality reviews both approved the complete
  Tasks1-3 implementation with no significant findings. Ponytail review found
  no new dependency or speculative runtime abstraction.

### Final evidence and production boundary

- Commit `11cd172` made the router assertion use FastAPI's public OpenAPI result,
  after the first full server run exposed an internal-router compatibility change
  in FastAPI 0.138.2. The corrected test passed locally and in the full rerun.
- A fresh isolated PostgreSQL 14.24 cluster on loopback port 56680 and the control
  server's Python 3.11.0rc1 ran the complete backend suite: **1892 passed, 2
  skipped, 12 deprecation warnings in 482.20s**. Whole-backend Ruff passed. A
  separate Linux zipapp/entrypoint run passed **51 tests in 3.09s**. Local and
  commit-range whitespace checks passed.
- The exact temporary source archive, script, cluster and directory
  `/tmp/veltrix-task4-11cd172-16f2d13e` were removed; port 56680 closed and
  `domain-drop-control.service` remained active. The production database and VPN
  node were not used by the tests.
- Nothing from this strict-dispatcher checkpoint is deployed. No control database
  migration, node zipapp, trust/config file or new journal was installed. The
  current production runtime remains on its existing legacy path. Public TCP 443
  for the new path remains unopened, no protected REALITY inbound was created,
  and the owner's UUID, 8443 endpoint and working link are unchanged. Friend
  invitations, public admission and payments remain disabled.
- The next reviewed plan is
  `docs/superpowers/plans/2026-09-22-vpn-strict-node-deployment.md`. It permits
  only a one-shot strict-pinned code/config deployment and closed-copy migration
  rehearsal. Runtime scheduling remains blocked until PostgreSQL
  `statement_timeout`, the driver's command timeout and explicit ambiguous-COMMIT
  reconciliation have separate tests.

## Node-local API, journal and approved live repair (2026-09-21 continuation)

This section is a historical predecessor to the strict-dispatcher checkpoint
above. Its production observations remain relevant, but its list of missing local
components is superseded. The friend beta is still **not ready**: there is no
protected inbound, deployed strict path, invitation activation or reconciliation
workflow. Payments and public cabinet admission remain disabled. Do not remove
the bound-profile mutation barriers.

### Implemented locally

- `f393478`: stdlib node-local panel session with direct loopback networking,
  bounded responses/deadlines, strict envelopes, no retries and explicit mutation
  uncertainty. `b7a9096`: configuration observer checks exact panel identity,
  inbound coverage against read-only local SQLite and the expected transport.
  Full inbound responses, including REALITY private keys, stay on the node.
- `a05040e`: dedicated Bearer-token mode. No browser login/cookie/CSRF/logout or
  credential fallback in that mode; secrets cleared on context close. Status
  success alone is not proof of administrative scope or runtime application.
- `b449e52`, `vpn_node_journal.py`: separate SQLite gate and durable operation ledger,
  generation/permanent-revoke fences, no automatic replay, and node-wide blocking
  after uncertain sends. Real process-death and late-HTTP-handler tests pass.
  **Not anti-rollback storage:** restoring an old same-installation ledger can
  erase fences. Never restore/reinitialize ledger files as recovery. Explicit
  control-side reconciliation is a release gate, not implemented yet.
- `6067819`: the legacy attachment insert now uses its existing schema-aware timestamp
  helper: INTEGER/BIGINT gets Unix milliseconds, legacy TEXT stays compatible.
  Existing rows are never repaired implicitly. This prevention patch is still
  local; its deployed already-linked path does not rewrite the repaired row.

### Authorized production actions actually completed

- The owner permitted a dedicated administrative 3x-UI API token, stored only
  on the node at `/var/lib/veltrix-vpn/control-auth/api-token`, root-owned0600
  below private0700 directories. Creation used a fresh unique name after strict
  SSH, worker reservation, backup and private-copy CLI-initialization rehearsal.
  Existing tokens, password, clients and inbound configuration stayed unchanged.
  Do not rerun the installer: the CLI can rotate existing named tokens.
- Live global client listing initially failed because the owner's single
  `client_inbounds.created_at` held text although pinned 3x-UI expects int64
  milliseconds. After separate explicit owner permission, a fresh backup and
  private-copy rehearsal, exactly that field was converted. Full table/schema
  comparisons verified no other changes; Xray PID/start-time was unchanged.
  UUID, URI, expiry and counters were preserved; no restart was performed.
- Live token authentication, status, settings, inbound list and global client
  list now pass. The new node-local observer checked the actual existing owner
  candidate: matched, enabled, transport matched, runtime running. This is a
  read-only configuration observation, not endpoint binding or revocation proof.
- Backups and rehearsal copies remain private on the VPN node below
  `/var/lib/veltrix-vpn/control-auth/`, including `created-at-repair/`.
  Neither panel credentials nor raw panel responses were exported to chat.
- Permanent public TCP443 for the protected VPN was explicitly authorized, but
  **has not been opened**. Existing8443 and the owner's link are unchanged. No
  application deployment occurred; last recorded deployed revision is `e635f0b`.

### Verification and next gate

- Parent final full backend: **1311 passed, 13 skipped in 201.15s**. Eight require
  a separate PostgreSQL test URL; five are journal platform checks. Whole-backend
  Ruff and whitespace checks passed. No frontend source changed; preserve the
  existing generated `frontend/tsconfig.tsbuildinfo` modification.
- Independent specification review then quality review approved each component
  after fixes. Fourteen synthetic POSIX checks also passed on the control server's
  application Python3.11.0. Only private temporary synthetic files were used and
  removed; no working database or service was involved in those checks.
- Final integrated independent review approved this bounded checkpoint with no
  actionable findings; its targeted rerun passed 140 tests with five platform
  skips. This is not production-launch approval.
- Next: connected node mutation/runtime verification, durable control staging
  and worker reservations, strict SSH execution, recovery/reconciliation and
  real PostgreSQL race tests; then verified legacy import, protected inbound
  acceptance and one-use invitations. Keep current owner-only access throughout.
- Detailed evidence and limits are in the dated node-panel-session,
  node-observation, node-token-session, node-operation-journal and
  client-link-timestamp-repair plans. The old xui-remote-session plan is explicitly
  superseded; never forward full inbound responses to the control process.

## Pinned 3x-UI identity observation (2026-09-21, local only)

- `a823868` implements the next bounded component in
  `backend/app/services/vpn_xui_identity.py`.
  It compares the exact UUID/email pair, exclusive inbound attachment and enabled
  state across the pinned 3x-UI 3.8.5 global-client and inbound inventories.
  It rejects duplicate identities, stale/split matches, malformed responses,
  orphan/multi-attached clients and unsupported target types using static errors.
  Frozen observations contain no credentials or raw response. The UUID-parser
  regression proves secret-bearing underlying errors are suppressed in formatted
  exception chains; do not enable traceback-local capture for these payloads.
- This function is pure and **not wired into provisioning**. `matched` is not
  permission to mutate, and `not_observed` is not proof of global absence or
  completed revocation. User-scoped and non-atomic API lists remain an explicit
  limitation. No transport/REALITY validation or trusted full collector is implied.
  Existing bound-profile barriers remain unchanged; do not deploy this checkpoint.
- Plan: `docs/superpowers/plans/2026-09-21-veltrix-xui-identity-preflight.md`.
  TDD: 205 new tests failed against the stub while 87 existing tests passed;
  the final related slice passed all 292 tests. Independent specification and
  quality reviews and final integrated review approved the local component
  without findings. Parent reran
  all 205 new tests successfully after the final test-only secret assertion.
  Parent full backend on committed `a823868`: **990 passed, 8 skipped in 139.83
  seconds**, repeating the initial 164.10-second run. The skips
  require `VPN_PORTAL_TEST_PG_URL` (three migration and five portal PostgreSQL
  tests). No fresh PostgreSQL concurrency evidence is claimed. Whole-backend
  Ruff and whitespace checks pass. No frontend source changed or build was run.
- No control-server/VPN-node calls or changes, panel login, port opening or
  credential rotation occurred. Deployed state is not reverified here; the last
  recorded production revision remains `e635f0b`. Preserve the unrelated generated
  `frontend/tsconfig.tsbuildinfo` modification. Next: trusted collection and transport
  validation, durable endpoint-aware mutations, verified legacy import and protected
  inbound/runtime acceptance; invitation implementation still follows that work.

## Closed-beta disconnect policy approved (2026-09-21)

- After the owner's next continuation, official 3x-UI 3.8.5 release notes and
  its upstream disconnect clarification confirmed the release constraint below.
  The owner explicitly accepted shared reconnections on disable/expiry for the
  first ten-person closed test. The approved spec now records this narrow exception;
  effective removal must still stop already-authenticated traffic, not just reject
  a new handshake. Preserve existing UUIDs and links, and measure recovery.
- This choice does not authorize opening a new port or immediately changing the
  node. No production setting or VPN connection has changed in this continuation.
  IP bans are not profile-level revocation. Add/update hot-apply failures may also
  restart the core: do not promise unconditional continuity or an unmeasured outage.
  Details: `docs/veltrix-endpoint-api-findings.md`.

## Recorded endpoint resolution and safety barrier (2026-09-21, local only)

- Continue in `.worktrees/veltrix-customer-portal` on
  `codex/veltrix-customer-portal`; do not switch to the root checkout for this work.
  Plan: `docs/superpowers/plans/2026-09-21-veltrix-endpoint-resolution.md`.
  `541b2c3` adds the immutable recorded-endpoint resolver; `e8cee3e` and `a39dedd`
  strengthen its regression checks. Both task reviews approved the resolver;
  56 focused tests pass, including changed/cleared worker defaults, identity
  preservation, stale endpoint refresh and directly observed no-flush/no-commit.
- `221a835` adds the legacy-mutation/decommission barrier. Bound provision,
  suspension and revocation stay pending with static diagnostics and no SSH,
  UUID/URI replacement, issue/expiry/revoke/sync time rewrite, success event or worker-health
  mutation; only the requested pending status, error and key update time change.
  Direct service calls preserve permanent revocation. Node retirement refuses any non-revoked
  bound profile before clearing keys or credentials, including unknown statuses;
  confirmed-revoked history and existing unbound retirement remain compatible.
  Task specification/quality reviews and the independent final integrated review
  approved this local gate with no remaining findings.
  This is **not** a deployable endpoint-aware release:
  there is no remote adapter, durable operation intent, verified legacy import,
  endpoint-aware lifecycle selection, protected inbound or friend invitation yet.
  Do not bind production keys or deploy this intermediate checkpoint.
- Source research exposed an unresolved release constraint in installed 3x-UI
  3.8.5 / Xray 26.9.9: hot API changes can fall back to a shared-process restart,
  whereas credential removal alone need not end existing authenticated traffic.
  See `docs/veltrix-endpoint-api-findings.md` for pinned primary sources and caller
  integration gaps. Turning off restart-on-disable does not prove both effective
  revocation and uninterrupted operation. Resolve this before a live adapter;
  do not silently weaken the approved acceptance criteria.
- Parent full backend verification on committed `221a835`: **785 passed,
  8 skipped in 131.27 seconds**, repeating the initial 158.23-second successful run.
  The skips explicitly require a PostgreSQL test URL
  (three migration and five portal tests); no concurrency proof is inferred from
  SQLite. The implementer also ran 162 related regression tests successfully.
  Whole-backend Ruff and diff whitespace checks passed. No frontend source changed,
  so frontend tests/build were not rerun in this increment.
- This increment has made no control/VPN-node calls or changes. The last verified deployed
  revision remains `e635f0b`, as recorded below; no fresh production-health claim
  is made by the local test results. Closed owner-only admission and payment
  deferral remain unchanged. Preserve the existing generated
  `frontend/tsconfig.tsbuildinfo` modification; it is not part of this increment.

## Protected VPN endpoints: local storage gate (2026-09-21, not deployed)

- The owner approved continuing the protected-endpoint design before the friend
  beta. First group remains ten individual one-use invitations, seven days from
  confirmed readiness and one profile per participant; no payment integration or
  public admission. Invitations and the protected inbound are not implemented yet.
- `c8e78b8` adds endpoint storage and nullable key bindings, with no startup
  backfill, remote calls or runtime routing change. Both task specification reviews
  and the final integrated quality review approved this storage-only scope.
  `14fa67a` + `019bf99` add real PostgreSQL upgrade/repeat/fresh-schema proof;
  use `docs/superpowers/plans/2026-09-21-veltrix-vpn-endpoint-storage.md` for the ledger.
  Do not deploy this storage-only checkpoint as an endpoint-aware release.
- Parent verification: 706 backend tests passed with real PostgreSQL/no skips;
  after the last test-only review amendment, all 29 schema/migration tests passed.
  Configured backend Ruff clean; frontend 52 tests and TypeScript/Vite build passed.
  The synthetic cluster `/tmp/veltrix-portal-test-s7iqczfs` was stopped and removed;
  independent SSH inspection confirmed its exact directory absent and port closed.
  Control remains active on `e635f0b`; existing test1 passed certificate-valid
  HTTPS200 and UDP DNS. Public HTTPS health returned 200/ok. An initial probe with
  urllib's default User-Agent received Cloudflare 403; paired otherwise-identical
  requests using the prior httpx User-Agent returned 200. No edge/app settings changed.
- Read-only inspection of VPN worker 15 found 3x-UI **3.8.5**, Xray **26.9.9**,
  one VLESS/none inbound on 8443, and no TCP listener on 443 at inspection time.
  Firewall/external reachability of 443 was not established and no port was opened.
  Production remains `e635f0b`; no working VPN key or node setting was changed.
- The owner could not independently compare the SSH host fingerprint and explicitly
  authorized trusting the currently presented key. The inspection pins that key
  in local ignored `.pytest_cache/vpn-node-15-known-hosts`, with fingerprint
  `SHA256:YoFFLpJcKF3MTbHICP8hOV+HUBCUorQuAKCruYd13D0`. This is owner-authorized
  first-contact trust, **not** a hosting-console verification. Authenticated
  read-only inspection then passed against that pin. Preserve it; do not silently
  replace a changed host key. The existing application SSH helper still uses
  `known_hosts=None`; no claim is made that production worker SSH is now pinned.
  A durable strict-verification path is required before protected-endpoint mutation.
- The node has no explicit `restartXrayOnClientDisable` setting. The installed
  version's [setting source](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/setting.go)
  defaults missing values to true. Client operations must therefore be checked for
  restart/connection behavior before friend admission; merely calling the panel API
  does not establish uninterrupted operation. No panel setting or token was changed.

## Mini App launch correction (2026-09-21, supersedes release revision below)

- Owner has now confirmed the corrected real iPhone Mini App launch works
  ("проверил, уже работает"). This is user-reported live acceptance of the login
  correction, not a new automated check. Owner then confirmed copying the connection
  from the cabinet and using it in Happ works ("да, всё копируется и всё работает").
  Owner also confirmed renaming the profile, retaining its name after reopening the
  cabinet, and continued VPN operation. Owner then confirmed seeing the successful
  logout screen and automatic sign-in after closing/reopening Telegram and Mini App.
  The main flow, rename persistence and logout/re-entry are user-accepted. Wider
  pilot checks and final integration remain; this is not public-launch approval.
  No new server changes accompanied these confirmations.
- Production now runs `e635f0b078cb4ee91f9b4d1dd5e6ce54fd7ea1b2`. Owner reports
  browser sign-in completed; detailed subscription-data comparison remains unchecked.
  Real iPhone Mini App screenshot exposed an empty-initData launch from the bot's
  reply keyboard. Telegram documents that this launch type provides no WebAppInitData.
- Fixed only bot entry delivery: command reply keyboard remains, old WebApp row is
  replaced on the next reply, and one separately gated inline cabinet button follows
  all response chunks. Authentication validation and closed single-owner access remain
  unchanged. Owner followed the fresh `/start`/new inline-button retest and confirmed
  it works. Do not repeat BotFather or credential setup.
- Regression evidence: 2 launch tests failed before correction; final bot suite 50
  passed, including partial-delivery failure without replaying a delivered key.
  Broader portal/customer/bot checks before the last added test: 267 passed, 5 optional
  PostgreSQL concurrency checks skipped. Frontend 52 passed; backend Ruff clean.
  Independent source and deployment reviews completed. Backup-loop absent-file and
  active `verifying` job regressions passed (2 tests).
- Deployment backup: `/opt/backups/veltrix-miniapp-VsJUzooa`, including old source and
  explicit absence markers for newly added files. Fixed-base bundle fast-forward;
  only control restarted. Environment/unit/Nginx/frontend/buildcache hashes and
  metadata preserved; no database migration, node restart or unsolicited bot message.
  Post-restart public health, closed capabilities, anonymous rejection, PKCE redirect
  and cancellation of the probe's own attempt passed. VPN identity hashes unchanged;
  existing test1 passed certificate-valid HTTPS200 and UDP DNS after the restart.
- Fresh public Chromium check at 390px rendered the login link with no page errors
  or overflow. Cabinet assets, Telegram SDK and config returned 200, anonymous `/me`
  returned 401. Unrelated Cloudflare analytics DNS failure did not block rendering.
  This is deployment/UI evidence, not a completed user Mini App login.

## Initial release and activation checkpoint (historical, 2026-09-21)

- Post-release setup: user switched BotFather from legacy Login Widget to OIDC,
  then reported saving callback under Redirect URIs and origin under Trusted Origins.
  Hidden-entry helper staged at `/root/veltrix-portal-setup/portal_oidc_setup.py`:
  reviewed, 13 synthetic tests passed on server including POSIX permissions. Owner
  completed private entry; fresh inspection confirmed both OIDC settings present
  without printing values. Real Telegram authentication remains unverified.
- Closed pilot is now enabled for exactly the previously challenge-verified owner.
  `VPN_PORTAL_ENABLED=true`, `VPN_PORTAL_PUBLIC_ACCESS=false`; numeric identity stays
  private. Fresh read-only checks matched the historical private-chat challenge to
  the existing active customer owning key8. No ownership or VPN credentials changed.
  Config-only backup: `/opt/backups/veltrix-pilot-da5nmqms/env.before-pilot` (private,
  includes saved OIDC credentials). Shared config lock, atomic write, metadata checks
  and disabled-state rollback were reviewed. Only control restarted; no Nginx/node changes.
- Pilot checks passed: public health, all login capabilities available, unauthenticated
  `/me` rejected, Telegram redirect/callback URL + PKCE + secure binding cookie and
  cancellation of only the probe's own attempt. All prior VPN identities stayed
  unchanged; certificate-validated HTTPS200 and UDP DNS through test1 passed again
  after restart. No Telegram account was impersonated or session manufactured.
  Activation guard now includes domain attacks in `verifying` as well as planned/running;
  its focused regression, 6 config tests and synthetic actual-app access/keyboard checks pass.
- Production control at `/opt/domain-drop-catcher` now runs reviewed revision
  `46caa1010e452dc94d606dc2b7b2a59f18816ac7`. Tasks 1–12 are implemented and reviewed.
  Task 13's deployment and closed-pilot activation checks passed; real Telegram login
  and final integration remain pending. Work continues on `codex/veltrix-customer-portal`
  in `.worktrees/veltrix-customer-portal`; main checkout remains unchanged.
- Login is available at `/cabinet/` for the verified pilot only. Next: owner performs
  real browser and iPhone Mini App sign-in, then checks their subscription/profile.
  Do not treat mock SDK/browser tests as live Telegram evidence. Payments remain deferred.
- Final pre-release verification: `python -m pytest` — 664 passed in 139.42 seconds
  with real PostgreSQL and no skips; `python -m ruff check app tests` clean;
  `npm test` — 52 passed; `npm run build` passed. Independent auth, specification,
  quality and operational safety reviews approved their respective scopes.
- Initial disabled-release server probe passed all 20 checks: additive schema and names,
  all pre-existing VPN identity hashes unchanged, no-store/no-referrer, public health,
  disabled customer login, existing admin login/profile reads, admin/customer cookie
  isolation and logout of the probe's own admin session. Existing key8/test1 still
  transports certificate-validated HTTPS (200) and UDP DNS. No URI or credential printed.
- Before pilot activation, a browser check of the actual public `/cabinet/` at 390px loaded its assets
  and showed the disabled-login state, without page errors or horizontal overflow.
  This is live deployment/UI evidence, not a real Telegram login or iPhone-client test.
- PyJWT 2.14.0 was installed without unrelated runtime upgrades. Only the control
  service was restarted and Nginx reloaded; actual root service unit, worker allowlist,
  generated production build cache, VPN nodes and transport were preserved.
- Rollback backup retained: `/opt/backups/veltrix-cabinet-20260921-003811`, including
  full validated DB dump, original environment/unit/Nginx/frontend, VPN identity
  hashes and release marker. Do not restore the entire old DB over newer business data.
  Synthetic PostgreSQL `/tmp/veltrix-portal-test-h8t_gmwt` was stopped and removed;
  both real-snapshot rehearsal copies and isolated Nginx instances were already removed.
- Operational steps and remaining real-client checks:
  `docs/vpn-customer-portal-runbook.md`. No public launch or true end-to-end Telegram
  authentication claim is made. The production runtime's Python 3.11.0rc1 and VPN
  transport hardening remain separate pre-public-launch tasks.

## Veltrix implementation history (2026-09-20–21, before the release above)

The entries below are chronological implementation evidence, not the current
deployment state. The release checkpoint above supersedes earlier pending statuses.

- Fresh release backup created and validated on the managing server at
  `/opt/backups/veltrix-cabinet-20260921-003811`: full custom dump 1,133,002,179 bytes,
  required-table TOC plus config/previous frontend archives verified. No DB data was
  downloaded. Environment, actual service unit, generated build cache and VPN identity
  hashes are retained privately for rollback verification. At that checkpoint the
  production app was unchanged; deployment is recorded above.
  No active attack/worker/maintenance/zone-scan jobs; ordinary discovery work continues.
- During Task 12 implementation/review, a separate unprivileged, loopback-only
  Nginx 1.18 rehearsal passed callback/400/413/429/502, cookie flags, scoped no-store,
  cabinet no-referrer and secret-marker log checks. Its exact temporary process and
  directory were removed; production Nginx was not reloaded. Independent final static
  auth review of Tasks 1–11 found no important issue; live Telegram remains unverified.
- Task 11 is complete with specification and quality re-review approval. Bot and
  admin share safe profile names and entitlement rules; admin rename changes only
  display_name/updated_at and preserves VPN identity. Inline rename, full copy,
  independent per-profile pending state and authoritative response ordering were
  verified against the actual built admin UI, including external revoke and delayed
  PATCH/GET cases. Timestamp comparison preserves backend microseconds. Parent full
  backend run: 634 passed in 140.45 seconds with real PostgreSQL and no skips;
  subsequent two bot-copy changes passed all 37 Telegram tests. Final frontend:
  52 tests, TypeScript/Vite build and 390px browser regression passed; full Ruff clean.
  Private pilot keyboard is implemented but not enabled on production yet.
- Task 10 is implemented and independently reviewed: separate `/cabinet/` entry,
  five real hash tabs, subscription/profile presentation, full selectable links,
  rename/copy flows and Telegram-aware authentication states. Parent verification:
  37 frontend tests, TypeScript/Vite multi-page build, full HTTP-mocked browser QA
  at 320/390/768/1280 pixels in light/dark, and the actual FastAPI static route passed.
  The official downloaded Telegram SDK also passed synthetic launch/cache cleanup;
  this is not a live Telegram client or real login proof. No cabinet deployment yet.
  Reviews caught and fixed Mini App 401 guidance, dynamic system/content safe areas,
  preservation of unrelated URL fragments, parallel-request 401 handling and stale
  links after rename across tab remounts. Both specification and quality re-reviews
  approved; parent repeated the expanded browser suite after the final changes.
- Latest unchanged-backend rerun: 611 passed in 157.40 seconds with real PostgreSQL
  and no skips; full Ruff clean. Production public health returned 200/ok while the
  separately approved server-local snapshot rehearsal was restoring. The first full
  restore hit its 900-second deadline before migration. Automatic cleanup removed
  the private dump and cluster; an independent read-only check confirmed absence
  and 27.7 GiB free disk. No app restart or node changes.
- The user separately approved a temporary copy of the real production database on
  the same managing server for migration verification. The dump stays on that host;
  the disposable cluster has a private Unix socket, peer authentication and no TCP
  listener. Any retry must likewise stop/remove its exact directory after the probe,
  in addition to cleaning up the separate synthetic PostgreSQL test cluster when
  work finishes. The bounded 2700-second retry completed successfully: two migration
  passes preserved legacy VPN/node fields, identities and limits; display names and
  second-pass timestamps stayed stable; the previous ORM could read/write with rollback.
  The probe did not start application lifespan or perform node operations. Its dump
  and exact temporary cluster were removed; an independent read-only check confirmed
  absence, 28.2 GiB free disk and normal load. This is real-snapshot migration evidence,
  not live authentication proof or a retained deployment backup. A fresh validated
  backup is still required before deployment. Production access remained read-only.
- Browser OIDC credentials are still absent from the private server environment.
  The user has not yet configured BotFather Login Widget / Allowed URLs. Client
  Secret must be entered privately, not sent in chat. This blocks live browser-login
  verification, not the implementation or synthetic tests.
- Read-only deployment inspection: production remains at `2f288c0`, service active,
  Nginx configuration valid, and PyJWT is not installed (cryptography is 49.0.0).
  Six RSA/JWK success and negative-claim checks using the locally tested PyJWT 2.14.0
  public sources passed in server memory on Python 3.11.0rc1. No package, server file,
  database or service changed; this is compatibility evidence, not an installation
  or live OIDC proof. Install the verified dependency explicitly during rollout;
  do not bypass the project's stable-Python requirement or upgrade runtime implicitly.
- Closed-pilot ownership confirmed on 2026-09-21 Moscow: the user identified their
  Telegram account as Elo with no username, then sent a fresh one-time challenge to
  the existing bot's private chat. A read-only production query matched the processed
  webhook sender/chat ID to the existing active customer owning test1 (key 8,
  subscription 2). No ownership or account fields were changed. Use this confirmed
  numeric identity for the closed pilot, never infer ownership from a display name.
  The numeric ID remains in the private operational evidence, not this repository.
- Task 8 adds the separate customer HTTP surface and lifespan OIDC integration.
  Specification and quality reviews approved after negative-test improvements.
  Parent full backend: 611 passed in 140.61 seconds with real PostgreSQL and no skips;
  final API rerun after test-only changes: 17 passed in 7.70 seconds; full Ruff clean.
  Real admin/customer session coexistence, entitlement changes, HMAC/RSA auth paths,
  streamed input limits, CORS isolation and generic no-store errors are tested.
  Late-response exception log sanitization remains required in Task 12 before rollout.
  Current unchanged admin frontend also passes all 12 tests and TypeScript/Vite build.
- Task 9 adds independent typed portal requests, static Russian errors and status/date
  helpers, without admin imports or persistent client token storage. Both reviews
  approved. Parent independently ran all 22 frontend tests and TypeScript/Vite build;
  reviewer ran all 10 new focused tests. This is client infrastructure, not a completed
  cabinet screen or browser/Mini App production verification.
- User chose to include a customer cabinet immediately, alongside the Telegram bot,
  using Telegram sign-in without separate email/password registration. Both surfaces
  must share the existing VPN customers, subscriptions and keys; admin authentication
  remains separate. Payments remain explicitly deferred until the end.
- Visual concept compares Telegram-only and cabinet experiences. All mockup counts,
  traffic, durations and profile limits are demonstration data, not agreed tariffs
  or live measurements. Implementation progress below refers to the isolated branch;
  the cabinet has not yet been deployed to production.
- Written design approved by the user on 2026-09-20:
  `docs/superpowers/specs/2026-09-20-veltrix-customer-portal-design.md`.
  The first bounded delivery is real cabinet/authentication/profile presentation;
  statistics collection, published tariffs and public-launch hardening have separate
  follow-on specifications. The cabinet itself is not postponed behind a bot-only launch.
- Implementation plan prepared in
  `docs/superpowers/plans/2026-09-20-veltrix-customer-portal.md`.
  Isolated branch `codex/veltrix-customer-portal`, worktree
  `.worktrees/veltrix-customer-portal`, starts at `f111271`.
  Application implementation has started in that worktree; production configuration
  and deployment have not changed.
  User selected delegated implementation with controller review in this same task.
- Fresh worktree baseline: backend `332 passed` in 58.48 seconds, frontend `12 passed`,
  TypeScript/Vite production build passed. The old main backend virtualenv lacks pytest;
  checks used the available Python 3.14.4 with worktree/backend on PYTHONPATH.
  Frontend dependencies installed offline from the existing npm cache.
- Task 1 display helper passed specification and quality reviews. Focused suite:
  48 passed, no skips; full Ruff clean. A full backend run before the final additional
  parser-hardening tests had 363 passed. This helper is not yet connected to
  customer/admin/bot responses. Valid VLESS prefixes and VMess non-label fields are
  preserved; malformed inputs fail with safe errors.
- Separate worktree `backend/.venv` now uses Python 3.14.4 and inherited test packages;
  PyJWT 2.14.0 was installed only there. Main checkout dependencies remain unchanged.
- Task 2 adds nullable display_name, three separate portal auth tables, disabled-by-default
  portal settings and the PyJWT dependency. Both reviews approved; 77 focused/related
  tests passed and full Ruff is clean. No routes or login behavior enabled yet. The
  migration statement was checked, but an actual PostgreSQL upgrade rehearsal remains
  mandatory before rollout; SQLite tests are not proof of PostgreSQL upgrade behavior.
- Read-only compatibility inspection found production Python `3.11.0rc1` and PostgreSQL
  `14.24`. Six synthetic display-helper checks passed in a separate server Python
  process (valid VLESS/VMess, overflow, real deep nesting, malformed scheme, name
  trimming), with no file/DB/service changes. Runtime upgrade is a separate release
  risk to address before public launch, not an automatic part of the cabinet change.
- Task 3 assigns stable names to legacy and new profiles, with restart-safe batched
  startup backfill and customer-first issuance locks. Only nullable display_name is
  filled; existing public_name/UUID/URI and remote client identity remain unchanged.
  Both reviews approved; parent ran 145 profile/control/remote/display/Telegram tests
  successfully and full Ruff is clean. A subsequent two-connection PostgreSQL test
  observed the actual blocking PID on the customer row, then confirmed ordinals 2
  and 3 after the first transaction committed. This verifies the numbering helper's
  lock behavior, not the entire issuance/archive/lifecycle concurrency matrix.
- With explicit user approval, a disposable PostgreSQL 14 cluster was started for
  synthetic-data tests on the managing host, reachable only through a localhost SSH
  tunnel. It is separate from the production cluster, database and application.
  A pre-feature schema generated from revision f111271 passed two real PostgreSQL
  migration/backfill runs: legacy customer/subscription/key values (apart from normal
  updated_at changes) remained stable, names were stable, auth tables appeared,
  VARCHAR(64) was enforced, and the old ORM still read/wrote the extended schema.
  The rehearsal schema was removed. This is synthetic schema compatibility evidence,
  not a production-snapshot rehearsal, VPN traffic test or live authentication proof.
- Task 4 adds canonical Telegram identity, bounded/safe Mini App HMAC validation,
  canonical replay digests and savepoint-based customer resolution. Both reviews
  approved; parent independently ran 70 new/existing Telegram tests, including the
  actual PostgreSQL insert race: one customer, one recovered uniqueness conflict,
  and both outer writes preserved. Full Ruff is clean. No login routes enabled yet.
  Full backend suite after Tasks 1–4: 471 passed in 99.20 seconds, including the
  configured PostgreSQL integration test, with no skips.
- Task 5 (verified 2026-09-21 Moscow) adds separate customer sessions, origin/CSRF
  checks, closed-pilot policy, durable one-use login records, atomic Mini App exchange
  and isolated bounded cleanup. Both reviews approved. Parent full backend run:
  519 passed in 150.71 seconds, no skips; full Ruff clean. Reviewer independently
  ran 116 focused tests. Real PostgreSQL tests observed concurrent claim contention,
  a recovered duplicate digest, and a committed Telegram rebind between lookup and
  reuse. A stale session is rejected without persisting exchange-side changes even
  if the caller commits after the error. No login routes or UI are enabled yet.
  All 63 current app modules also parse with Python 3.11 grammar; this is syntax
  evidence, not a full production-runtime test. An optional IPv6-origin normalization
  edge is documented in the plan and does not affect the intended DNS origin.
- Task 6 adds the official Telegram OIDC code/PKCE client, strict RS256 identity
  verification and bounded shared JWKS cache. Both reviews approved. Parent ran
  111 OIDC/Telegram tests in 11.68 seconds with PostgreSQL enabled and no skips;
  final focused OIDC rerun: 56 passed in 1.06 seconds, full Ruff clean. Tests use
  synthetic RSA-signed tokens and mocked provider HTTP, not live user credentials.
  Distinct valid OIDC sub and Telegram id are supported; identity uses id. Network
  responses are capped during streaming, redirects are disabled, and key-cache
  rotation/outage/throttling behavior is covered. HTTP integration and live browser
  login remain pending. The service parses with Python 3.11 grammar; local execution
  is still Python 3.14.4, not a production-runtime verification.
  A subsequent read-only probe ran the actual new JWKS provider against Telegram's
  public endpoint and selected its current RSA/RS256 `oidc-1` key with no `use` field.
  This confirms compatibility with the live public key document, not a successful
  user login or verification of a real user's ID token. No client secrets were used.
- Task 7 adds explicit customer DTOs, SQL ownership filters, effective subscription
  state and entitlement checks, safe link export and display-name-only profile edits.
  Both reviews approved. Parent ran 19 view tests and 58 related tests; after repairing
  a pre-existing timing-sensitive lifecycle test, the full backend passed: 594 tests
  in 135.27 seconds, PostgreSQL tests enabled, no skips. Full Ruff is clean; all 65
  app modules parse as Python 3.11. View tests use SQLite and inspect PostgreSQL lock
  SQL, not real PostgreSQL rename concurrency. HTTP integration remains pending.
- The lifecycle test failure was reproduced with a 0.15-second first-checkpoint
  delay against the effective 0.1-second cycle budget. Only the test changed: it now
  places cancellation deterministically after a real first commit and checks second-key
  rollback/resumption from separate sessions. Production timeouts and VPN behavior
  were not changed. Independent review approved the test repair.
- Separate the visible profile name from the legacy `public_name`, which currently
  participates in 3x-UI client email generation. Never globally rename internal
  `dropcatch-*` identifiers or regenerate UUIDs merely to improve labels.

## Telegram connection checkpoint (2026-09-20)

- User created `@veltrix_vpn_official_bot` and entered its token directly in an
  interactive server terminal. Token identity was verified with Telegram `getMe`;
  no token was displayed in the task. Production `.env` is root-owned, mode 0600;
  the control systemd unit currently runs as root (empty `User=`), not www-data.
- Both webhook protection secrets are configured. Telegram accepted the HTTPS
  webhook with `allowed_updates=[message]`, four connections and no pending-update
  deletion. `/start`, `/status`, `/keys`, `/support` command menu was registered
  and read back. Webhook status reported zero pending updates and no delivery error.
- Local authenticated empty-payload probe succeeded without creating a customer;
  missing-header and public wrong-secret probes returned 403. Local/public health
  passed. Existing `test1` passed HTTP, certificate-validated HTTPS and UDP DNS
  from both server and operator computer after the control-only restart.
- Private configuration backup:
  `/opt/backups/telegram-webhook-25h0r4qc/env.before-webhook`.
- Subsequent read-only inspection found a real `/start` received at
  `2026-09-20T18:12:05Z` and processed without a recorded delivery error, followed
  by another successfully processed private update. This verifies the processing
  path, not the visitor's ownership of the existing test subscription.
- **Next identity step:** confirm the user's Telegram identity before associating
  any existing subscription; do not assign `test1` to an arbitrary recent bot visitor.
- No VPN-node configuration, access keys or payment settings changed in this step.

## VPN subscription synchronization (2026-09-20)

- Subscription policy edits atomically queue existing keys, preserving their valid
  UUID/link and assigned node. Used traffic is not reset by renewal or limit edits.
- Expiry and pause use `pending_suspend` / `suspended`; renewal restores these keys.
  Manual revoke, cancellation, customer archive and node removal remain permanent.
  Historical revoked keys are deliberately not auto-restored.
- UI separates saved policy from confirmed node application and shows suspension
  and synchronization states. Unavailable nodes remain pending with sanitized
  errors and scheduled retries. No billing integration was added.
- New coverage includes API staging, suspension/resumption, stale expiry, manual
  revoke serialization, inactive customers, retained device slots, legacy and
  normalized 3x-UI SQL preservation and confirmed remote restart behavior.
- Deployed release `f6a087c` to the control server; the service is active and
  local health plus public health from the operator computer both return `ok`.
- Full verification: backend `332 passed`, Ruff clean, frontend `12 passed`,
  production builds completed locally and on the server.
- Live temporary-key verification passed: renewal applied expiry/device/traffic
  limits without changing UUID/link/node; recorded usage remained `675` upload /
  `5413` download bytes. Pause blocked actual tunnel traffic, resume and renewal
  after automatic expiry restored certificate-validated HTTPS 200. Manual revoke
  remained final after renewal. Test traffic was repeated after node startup so
  the usage assertion used nonzero persisted counters, not a zero/zero comparison.
- `test1` identity and node policy were unchanged; HTTP 200, certificate-validated
  HTTPS 200 and UDP DNS succeeded through its saved link from both the control
  server and operator computer. Temporary keys 9/10/11 are revoked; temporary
  customers are archived and verification admin sessions revoked.
- Full PostgreSQL backup (879677908 bytes):
  `/opt/backups/vpn-subscription-sync-20260920-170125/control.dump`; archive listing
  validated. A private SQLite node backup was also created before each live test.
  The earlier 90-second backup attempt was incomplete and was not used for deploy.

## Project Summary

This repository is a `multizone domain drop catcher` rebuilt from an older checker-oriented project.

Current shape:
- `control server` stores all state, schedules attacks, allocates worker capacity, manages accounts/contacts/workers, and exposes the UI/API.
- `worker server` is a separate runtime that polls control, receives tasks, waits for the planned window, and executes registrar registration requests.
- pre-registration availability checks are removed from the combat path.

Primary live registrar target today:
- `Gandi`

Architecture goal:
- multi-zone
- multi-strategy
- multi-worker
- future multi-registrar

## Task Modes

- `inherit_zone`
  Domain inherits its effective rules/phases from the zone strategy.
- `manual_override`
  Domain uses its own `DomainRuleOverride` plus local rules/phases.

## Core Files

### Backend / control

- [backend/app/api/routes/control.py](/D:/паразитное%20seo/backorder/project/backend/app/api/routes/control.py)
  Main control API: domains, strategies, override CRUD, workers, accounts, contacts, attacks, dry-run.
- [backend/app/api/routes/worker_runtime.py](/D:/паразитное%20seo/backorder/project/backend/app/api/routes/worker_runtime.py)
  Worker heartbeat, task polling, progress, results.
- [backend/app/services/attack_runtime.py](/D:/паразитное%20seo/backorder/project/backend/app/services/attack_runtime.py)
  Attack planning, weighted allocation, rebalance, live phase refresh, worker supervision.
- [backend/app/services/strategy_runtime.py](/D:/паразитное%20seo/backorder/project/backend/app/services/strategy_runtime.py)
  Effective strategy resolution, due-today logic, window preview, phase evaluation.
- [backend/app/services/gandi_dry_run.py](/D:/паразитное%20seo/backorder/project/backend/app/services/gandi_dry_run.py)
  Control-side Gandi dry-run request builder and dry-run execution.
- [backend/app/services/registrars.py](/D:/паразитное%20seo/backorder/project/backend/app/services/registrars.py)
  Registrar account remote auth validation.
- [backend/app/services/vpn_policy.py](/D:/паразитное%20seo/backorder/project/backend/app/services/vpn_policy.py)
  Shared subscription, device-limit, safe-node, and active-attack policy.
- [backend/app/services/vpn_lifecycle.py](/D:/паразитное%20seo/backorder/project/backend/app/services/vpn_lifecycle.py)
  Serialized automatic/manual provisioning, expiration, and revoke retries.
- [backend/app/services/vpn_customer_lifecycle.py](/D:/паразитное%20seo/backorder/project/backend/app/services/vpn_customer_lifecycle.py)
  Atomic customer archive staging: disables usable subscriptions and marks usable keys for revocation while preserving history.
- [backend/app/services/vpn_telegram.py](/D:/паразитное%20seo/backorder/project/backend/app/services/vpn_telegram.py)
  Idempotent customer bot commands and audited Telegram delivery.
- [backend/app/db/models.py](/D:/паразитное%20seo/backorder/project/backend/app/db/models.py)
  SQLAlchemy models for domains, strategies, overrides, workers, runs, tasks, events, contacts, accounts.
- [backend/app/db/migrations.py](/D:/паразитное%20seo/backorder/project/backend/app/db/migrations.py)
  Startup ALTER/CREATE compatibility migrations for existing databases.
- [backend/app/main.py](/D:/паразитное%20seo/backorder/project/backend/app/main.py)
  FastAPI app bootstrap and control runtime orchestrator startup.

### Worker

- [worker/app/runner.py](/D:/паразитное%20seo/backorder/project/worker/app/runner.py)
  Worker main loop, simulate mode, live planned RPS updates, Gandi create-status follow-up.
- [worker/app/control_client.py](/D:/паразитное%20seo/backorder/project/worker/app/control_client.py)
  Control polling client and task dataclasses.
- [worker/app/gandi.py](/D:/паразитное%20seo/backorder/project/worker/app/gandi.py)
  Gandi request builder, `Dry-Run` support, `createstatus` polling, top-level/domain extra parameters parsing.
- [worker/app/config.py](/D:/паразитное%20seo/backorder/project/worker/app/config.py)
  Worker env-driven configuration, including simulate and Gandi status polling knobs.

### Frontend

- [frontend/src/App.tsx](/D:/паразитное%20seo/backorder/project/frontend/src/App.tsx)
  Main control panel UI.
- [frontend/src/api.ts](/D:/паразитное%20seo/backorder/project/frontend/src/api.ts)
  Frontend API types and HTTP helpers.
- [frontend/src/VpnCustomerWorkspacePanel.tsx](/D:/паразитное%20seo/backorder/project/frontend/src/VpnCustomerWorkspacePanel.tsx)
  Customer-centered VPN workspace for profile, subscription, and access-key operations.
- [frontend/src/vpnCustomerWorkspace.ts](/D:/паразитное%20seo/backorder/project/frontend/src/vpnCustomerWorkspace.ts)
  Tested customer filtering, operational status, extension, and safe status-transition rules.

### Docs

- [docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md](/D:/паразитное%20seo/backorder/project/docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md)
  Core design spec.
- [docs/load-testing.md](/D:/паразитное%20seo/backorder/project/docs/load-testing.md)
  Simulate-load guidance and failure testing.
- [docs/gandi-production.md](/D:/паразитное%20seo/backorder/project/docs/gandi-production.md)
  Current Gandi production behavior and caveats.
- [docs/vpn-service.md](/D:/паразитное%20seo/backorder/project/docs/vpn-service.md)
  VPN node rollout, Telegram webhook configuration, recovery, and smoke testing.

## What Is Implemented

- Legacy checker routes are removed from the active combat API.
- Multi-zone strategy model exists:
  - `ZoneStrategy`
  - `ZoneRule`
  - `ZoneRulePhase`
- Domain-level manual override exists:
  - `DomainRuleOverride`
  - `DomainOverrideRule`
  - `DomainOverridePhase`
- Domains store:
  - `drop_date`
  - `zone`
  - `timezone_name`
  - `strategy_mode`
  - `override_min_guaranteed_rps`
  - readiness reasons
  - `registration_extra_parameters`
  - dry-run result fields
- Control auto-assigns defaults on domain creation:
  - registrar account
  - contact profile
  - zone strategy by zone when available
- Control exposes CRUD for:
  - zone strategies
  - zone rules
  - zone rule phases
  - domain override settings
  - domain override rules
  - domain override phases
  - workers
  - registrar accounts
  - contact profiles
- Control exposes preview for:
  - zone strategy windows
  - domain override windows
- Control runtime implements:
  - auto-planning for due-today domains
  - weighted allocator
  - live phase transitions
  - rebalance
  - worker stall detection
  - worker offline failover and reassignment
- Runtime visibility is already exposed in API/UI:
  - `runtime_minimum_rps`
  - `runtime_desired_rps`
  - `runtime_allocated_rps`
  - `runtime_phase_name`
  - assigned worker count
- Worker runtime supports:
  - heartbeat
  - task polling
  - progress reporting
  - result reporting
  - simulate mode with latency/jitter/success-rate knobs
  - queued task acknowledgement now transitions task/run/domain into active runtime states
- Worker Gandi integration now supports:
  - `Authorization: Bearer <PAT>`
  - account-level or worker-level `api_base_url`
  - `Dry-Run: 1`
  - `POST /v5/domain/domains`
  - `createstatus` follow-up after `202 Accepted`
  - all four contact roles: `owner/admin/bill/tech`
  - top-level `extra_parameters`
  - contact-level `extra_parameters`
- Control-side domain dry-run now exists:
  - `POST /api/control/domains/{id}/dry-run`
  - `POST /api/control/domains/dry-run/batch`
  - persists dry-run status/message/http code/check timestamp on the domain
- Control-side contact prefill from Gandi now exists:
  - `POST /api/control/registrar-accounts/{id}/prefill-contact`
  - fetches a contact draft from Gandi `user-info`
  - optionally enriches from `organization/organizations/{sharing_id}`
  - supports both production and sandbox through `api_base_url`
- Control can now maintain a worker runtime IP allowlist:
  - uses worker `ip_address` values as allowlist entries
  - generates an nginx include file for `/api/worker-runtime/`
  - can optionally execute a configured reload command after worker CRUD changes
- Frontend now exposes:
  - Gandi account `api_base_url`
  - Gandi-specific contact fields
  - domain `registration_extra_parameters`
  - per-domain `Dry run` action
  - batch `Dry run due today` action
  - account-level `Prefill contact`
- VPN service now supports:
  - manual plans, customers, and subscriptions while payments remain out of scope
  - a customer-centered admin workspace with search and operational filters
  - customer profile creation/editing and safe archive/restore actions
  - atomic customer archive that disables active/trial subscriptions, revokes usable keys, preserves all history, and reports pending revokes
  - subscription creation/editing, quick `+7/+30/+90` extension, and immediate suspension workflow
  - access-key issue/retry/revoke controls grouped under the owning subscription
  - exact full `vless://` / `vmess://` link copying plus an expandable read-only fallback for manual copying
  - safe automatic 3x-UI node selection and per-subscription device limits
  - safe VPN-node decommissioning with archived worker history, local key revocation, credential clearing, and active-work conflict checks
  - scheduled and manual lifecycle runs for `pending_sync`, expiration, and `pending_revoke`
  - blocking VPN mutations on workers with an active domain attack while health checks remain available
  - revoke-and-retain access-key history instead of destructive deletion
  - idempotent Telegram `/start`, `/status`, `/keys`, and `/support` commands
  - delivery of active keys only for active customers with currently valid subscriptions
  - admin visibility for node eligibility, lifecycle counters, key retry state, and Telegram updates

## Current Limits

- No real live Gandi registration proof has been executed from this workspace yet.
- The system is architecturally multi-account and multi-registrar ready, but the main tested scenario is still:
  - one Gandi account
  - one default contact profile
- `owner/admin/bill/tech` are currently cloned from one contact profile, not managed separately.
- TLD-specific `extra_parameters` are supported as raw JSON text, not as a rich typed UI model.
- The Task 9 disposable local desktop/mobile browser smoke is complete. Pending
  verification is limited to the production Mini App through the live Telegram
  webhook and a real VPN client.
- Worker runtime IP allowlist enforcement is implemented on the control side, but nginx/origin deployment still must be configured on the server.
- VPN payments are intentionally not implemented yet.
- A real Telegram webhook smoke test requires a deployed HTTPS control URL and a BotFather token; automated webhook behavior is covered locally.

## Earlier Verified Checks (historical)

These 2026-09-20 checks are retained for history and are superseded by the
current 2026-09-24 public-trial release-gate section above.

VPN connectivity repair on 2026-09-20:
- Reproduced a plain VLESS connection that worked over node loopback but stalled on public port 443. Packet-header capture confirmed TLS payload reached the node while plain VLESS payload did not.
- Moved the affected existing inbound to 8443 after a successful temporary-port probe, retained its client credential, and updated the control-side port and access URI. A private node database backup was created before the change.
- Verified HTTP 200, certificate-validated HTTPS 200, and a UDP DNS answer through the saved VPN URI from both the control server and local computer. The iPhone needs the refreshed URI reimported.
- Auto-created plain VLESS inbounds now avoid port 443; existing TLS/REALITY inbounds are unaffected.
- `python -m pytest tests/test_worker_maintenance.py tests/test_vpn_provisioning.py tests/test_vpn_policy.py -q` -> `26 passed`; backend Ruff -> passed.

At the VPN customer workspace checkpoint on 2026-09-20:
- `python -m pytest` in `backend` -> `238 passed`
- `python -m ruff check app tests` in `backend` -> passed
- `python -m ruff check app` in `worker` -> passed
- `npm test` in `frontend` -> `9 passed`
- `npm run build` in `frontend` -> passed

## Fast Re-Entry

In a new chat, start with:

`Read docs/current-state.md and continue`

If deeper context is needed, read in this order:

1. [docs/current-state.md](/D:/паразитное%20seo/backorder/project/docs/current-state.md)
2. [docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md](/D:/паразитное%20seo/backorder/project/docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md)
3. [backend/app/services/attack_runtime.py](/D:/паразитное%20seo/backorder/project/backend/app/services/attack_runtime.py)
4. [backend/app/services/strategy_runtime.py](/D:/паразитное%20seo/backorder/project/backend/app/services/strategy_runtime.py)
5. [backend/app/api/routes/control.py](/D:/паразитное%20seo/backorder/project/backend/app/api/routes/control.py)
6. [worker/app/runner.py](/D:/паразитное%20seo/backorder/project/worker/app/runner.py)
7. [frontend/src/App.tsx](/D:/паразитное%20seo/backorder/project/frontend/src/App.tsx)
