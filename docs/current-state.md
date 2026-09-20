# Current State

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
- Deployment/live-smoke results are recorded below after verification.

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
