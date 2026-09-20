# Current State

## Veltrix customer product direction (2026-09-20)

- User chose to include a customer cabinet immediately, alongside the Telegram bot,
  using Telegram sign-in without separate email/password registration. Both surfaces
  must share the existing VPN customers, subscriptions and keys; admin authentication
  remains separate. Payments remain explicitly deferred until the end.
- Visual concept compares Telegram-only and cabinet experiences. All mockup counts,
  traffic, durations and profile limits are demonstration data, not agreed tariffs
  or live measurements. No product code/server changes have been made for this phase.
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
- The VPN customer workspace is operational; visual browser smoke testing still needs to be repeated in an environment where the desktop browser runner is available.
- Worker runtime IP allowlist enforcement is implemented on the control side, but nginx/origin deployment still must be configured on the server.
- VPN payments are intentionally not implemented yet.
- A real Telegram webhook smoke test requires a deployed HTTPS control URL and a BotFather token; automated webhook behavior is covered locally.

## Most Recent Verified Checks

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
