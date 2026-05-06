# Current State

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

### Docs

- [docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md](/D:/паразитное%20seo/backorder/project/docs/superpowers/specs/2026-04-28-domain-drop-catcher-multizone-design.md)
  Core design spec.
- [docs/load-testing.md](/D:/паразитное%20seo/backorder/project/docs/load-testing.md)
  Simulate-load guidance and failure testing.
- [docs/gandi-production.md](/D:/паразитное%20seo/backorder/project/docs/gandi-production.md)
  Current Gandi production behavior and caveats.

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
- Frontend now exposes:
  - Gandi account `api_base_url`
  - Gandi-specific contact fields
  - domain `registration_extra_parameters`
  - per-domain `Dry run` action
  - batch `Dry run due today` action
  - account-level `Prefill contact`

## Current Limits

- No real live Gandi registration proof has been executed from this workspace yet.
- The system is architecturally multi-account and multi-registrar ready, but the main tested scenario is still:
  - one Gandi account
  - one default contact profile
- `owner/admin/bill/tech` are currently cloned from one contact profile, not managed separately.
- TLD-specific `extra_parameters` are supported as raw JSON text, not as a rich typed UI model.
- UI is operational and usable, but still not a final polished admin product.

## Most Recent Verified Checks

At the latest verified point:
- `python -m pytest tests -q -p no:cacheprovider` in `backend` -> `47 passed`
- `npm run build` in `frontend` -> passed
- `python -c "from app.main import app; print(app.title)"` in `backend` -> passed
- `python -c "from app.runner import WorkerRunner; from app.gandi import build_registration_request, register_domain; print('worker-import-ok')"` in `worker` -> passed

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
