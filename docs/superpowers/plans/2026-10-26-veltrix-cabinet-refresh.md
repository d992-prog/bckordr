# Veltrix Cabinet Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver the approved compact Veltrix customer cabinet with self-service devices, connection actions, measured traffic and configurable assistance on the existing backend.

**Architecture:** Extend existing owned-device APIs, transaction locks, control intents and hourly reports. Keep customer presentation, operator settings, device lifecycle and notification delivery in focused modules; do not replace the VPN runtime or transfer Mikan implementation code. Customer credentials remain unavailable until the relevant existing control operation has confirmed success.

**Tech Stack:** FastAPI, SQLAlchemy async, PostgreSQL; React 18, TypeScript, Vite; existing pytest and Node test runners. No new runtime dependencies.

**Approved specification:** `docs/superpowers/specs/2026-10-26-veltrix-cabinet-refresh-design.md` (user approved 2026-10-26).

---

## Progress

- [x] Task 1: server contracts and baseline repair; independent specification and quality reviews passed. Local new settings/API tests: 92 passed, 1 PostgreSQL skip; requested regression: 330 passed, 3 PostgreSQL skips; frontend: 204 passed and production build passed.
- [x] Task 1 PostgreSQL follow-up: the new concurrent initial-save API test passed on isolated PostgreSQL 14. Combined run: 109 passed, 1 unrelated older device-race fixture failed because it creates no issuable VPN endpoint. Task 2 repairs that connected fixture and reruns it. Temporary database and remote source copy were removed.
- [x] Visual choice: user approved variant 3 (`.impeccable/mocks/cabinet-refresh-c.png`); composition measurements and existing plate checks prepared. Application UI has not yet been rewritten.
- [x] Task 2: device lifecycle and credential generations — implemented through `b8096b5`; independent specification and quality reviews passed. Fixed stale lifecycle candidates, cross-owner worker/endpoint lock inversion (including archive), and unbound-UUID archive confirmation. Local operation/archive review: 48 passed. Isolated PostgreSQL snapshot `9d3ae0a`: 236 passed, including independent-session opposite-country replacements. Final application source also passed the connected `e29720d` batch (300 successful tests; an unrelated runtime-test teardown warning remained) and the clean `24d3008` follow-up below. Every temporary database/source copy was removed.
- [x] Task 3: gated expiry reminders — implemented through `e29720d`, test-isolation follow-up `24d3008`; independent specification and quality reviews passed. Clean isolated PostgreSQL/server-Python acceptance: 81 passed with warnings as errors, covering reminder service/runtime, all reminder concurrency scenarios, device-operation PostgreSQL scenarios and migration registration. The earlier connected batch had 300 successful tests plus a runtime-fixture thread warning: the two reminder tests unintentionally started the real news dispatcher. The test-only follow-up injects and awaits a fake dispatcher, with identity/call assertions; its complete runtime file passed three independent local processes. No production code was changed for that warning. Operator gate remains off by default, all senders/recipients were synthetic, and remote temporary data/source copy was removed.
- [ ] Task 4: compact customer UI — plate placement recorded; approved-comp first viewport and connection behavior in progress.
- [ ] Task 5: operator settings UI.
- [ ] Task 6: integrated review and release handoff.

## Working rules and verification commands

Worktree: `D:\паразитное seo\backorder\project\.worktrees\veltrix-cabinet-refresh`.

Backend interpreter, from its `backend` directory:

```powershell
& '..\..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest <test-paths> -q -W error
```

Frontend, from its `frontend` directory:

```powershell
npm ci --prefer-offline --no-audit --no-fund
npm test
npm run build
```

Use `apply_patch`. Keep the previous release branch and production untouched. No real Telegram messages, no production database changes and no new host trust. Commit only the task's explicit paths. Each task follows failing test → implementation → green test, then independent specification and quality reviews. Do not start the next task with open material findings.

## File boundaries and execution order

1. Cabinet settings and customer usage contracts: independent settings service/schema/admin router; thin additions to existing public configuration and owned-customer reads.
2. Device credential generations and lifecycle operations: migrations, models and focused operation service integrated with existing locks and control queue.
3. Reminder delivery: persistent deduplication records and a bounded runner integrated with existing bot delivery/runtime.
4. Customer cabinet: compact four-section presentation using the new contracts; reuse connection and country selection.
5. Operator configuration UI: one settings section for support, notice and reminder activation.
6. Integrated checks, visual review, documentation and release handoff.

## Task 1: Baseline, operator cabinet settings and owned usage API

**Files:**
- Create `backend/app/schemas/vpn_cabinet.py`, `backend/app/services/vpn_cabinet_settings.py`, `backend/app/api/routes/vpn_cabinet.py`.
- Modify `backend/app/api/__init__.py`, `backend/app/api/routes/vpn_portal.py`, `backend/app/services/vpn_telegram.py`.
- Create `backend/tests/test_vpn_cabinet_settings.py`, `backend/tests/test_vpn_cabinet_api.py`.
- Extend `backend/tests/test_vpn_device_api.py`, `backend/tests/test_vpn_portal_api.py`, `backend/tests/test_vpn_telegram.py` as their response contracts require.
- Fix `frontend/test/vpnAbuse.test.mjs` source reader to tolerate Windows CRLF without changing the component.

- [ ] Reproduce the existing CRLF-sensitive source test and normalize only the source reader (`.then(source => source.replace(/\r\n/g, "\n"))`). Run `npm test` with dependencies installed. Baseline backend device/usage tests currently pass (69 tests); frontend currently has one real baseline source-reader failure, plus an initially missing `uqr` dependency now installed.
- [ ] Add settings tests before the service exists. Test empty support, valid username/link, forbidden schemes/hosts/paths, plain-text notice limits, aware bounded notice dates, and disabled reminders by default. The supported normalized contact contract is:

```python
assert normalize_support_contact(None) is None
assert normalize_support_contact("@veltrix_support") == "https://t.me/veltrix_support"
assert normalize_support_contact("https://t.me/veltrix_support") == "https://t.me/veltrix_support"
with pytest.raises(ValueError):
    normalize_support_contact("javascript:alert(1)")
with pytest.raises(ValueError):
    normalize_support_contact("https://t.me.evil.example/support")
```

- [ ] Run the new test files; verify failure is missing requested behavior. Implement bounded Pydantic schemas and one JSON `AppSetting` value (reuse `get_app_setting`/`set_app_setting`). Invalid persisted settings fail closed, not to unsafe links. Use an optimistic integer revision checked under the settings-row transaction lock so an old form cannot silently replace a newer configuration. Initial revision is 0; save creates/locks the same setting key safely under concurrent first writes.
- [ ] Define admin `GET` and `PUT /api/control/vpn/cabinet-settings`, using `require_admin`, existing admin mutation/origin protections and audit. Request/response fields: `revision`, `support_contact`, `notice` (nullable object with `title`, `body`, `starts_at`, `ends_at`), `reminders_enabled`. Return 409 for stale revisions, 422 for invalid input. Persist only a canonical HTTPS Telegram username URL. Empty support removes the contact. Notice title 1–100 chars, body 1–1000, no control characters except newline/tab, valid aware dates with end strictly after start. Empty notice is `null`; do not implement HTML or external notice links.
- [ ] Extend portal configuration additively with `support_url`, currently active `notice` and `reminders_enabled`. Include only notices where `starts_at <= now < ends_at`. Preserve existing support text and login fields. Do not expose settings revision or scheduled notices publicly. Update exact-response fixtures.
- [ ] Add `GET /api/vpn-portal/usage?days=1|7|30`, authenticated using `current_customer`. Reuse the existing report and exclude the admin-only customer identity field from the response:

```python
report = await get_customer_usage(db, principal.customer.id, days=int(days), now=utcnow())
return report.model_dump(exclude={"customer_id"})
```

  Use a dedicated response model for the safe totals and device rows. Set `Cache-Control: no-store`. Reject invalid periods. Test logged-out 401, foreign-device omission, coverage/null preservation and no writes or leaked config URIs.
- [ ] Render configured support in bot help using the same service, not duplicated URL parsing; an empty setting keeps existing text. Verify with synthetic bot calls only. Preserve bot identity restrictions and safe errors.
- [ ] Run new settings/API tests plus device API, usage API, portal and Telegram tests. Run frontend suite and `git diff --check`. Commit the task. Obtain spec review, then quality review.

## Task 2: Persistent self-service device creation, revocation and replacement

**Files:**
- Modify `backend/app/db/models.py`, `backend/app/db/migrations.py`, `backend/app/services/vpn_devices.py`, `backend/app/services/vpn_device_view.py`, `backend/app/services/vpn_device_subscriptions.py`, `backend/app/services/vpn_policy.py`, `backend/app/services/vpn_control_intents.py`, `backend/app/services/vpn_lifecycle.py`, `backend/app/api/routes/vpn_portal.py`, `backend/app/schemas/vpn_portal.py`.
- Create `backend/app/db/vpn_device_operations_migrations.py`, `backend/app/services/vpn_device_operations.py`, `backend/tests/test_vpn_device_operations.py`, `backend/tests/test_vpn_device_operations_postgres.py`.
- Extend existing device, subscription, provisioning/control-intent, lifecycle and migration tests for generation-aware behavior.

- [ ] Write red tests for per-device replacement, stable owned identity, operation replay, stale sessions, foreign IDs, slot reservations between revoke/provision, country isolation and old-feed invalidation. A representative assertion is:

```python
assert result.id == replay.id
assert replacement.device_id == device.id
assert await count_device_slots(db, subscription.id) == 1
assert other_device.token_generation == other_generation
with pytest.raises(ValueError):
    await subscription_connection_uri(db, settings, old_token)
```

- [ ] Add nonnegative `credential_generation` to devices and keys (default 0); replace unique `(device_id, endpoint_id)` with `(device_id, credential_generation, endpoint_id)`. Keep all old rows/UUIDs. Add persistent operation record with unique `(customer_id, request_id)`, device/action, explicit stage, generation, chosen endpoint, creation/update times and bounded public error. Actions: `create`, `revoke`, `replace`; stages: `revoking`, `provisioning`, `completed`, `failed`, `uncertain`. Store original request identity fields so a replay with another payload is 409, not a new mutation. Migrations are repeatable and coexist with old startup backfills.
- [ ] Implement owned creation under existing customer → subscriptions → keys lock order. Validate named owned subscription, slot count, published healthy endpoint and transport readiness; create logical device/key and stage provision using existing helper patterns. A request timeout is recovered by replaying the same UUID. Provide `POST /v2/devices` with `subscription_id`, `endpoint_id`, `display_name`, `request_id`; status 202 until ready.
- [ ] Provide read-only `GET /v2/locations?subscription_id=<owned-id>` so a customer with no existing device can choose their first country. Return only public ready selectable locations, apply existing capacity checks, reserve no slot and expose no credentials. Foreign subscriptions are 404; unavailable capacity returns an empty list.
- [ ] Implement `POST /v2/devices/{id}/revoke` and `/replace` with `{request_id}` and existing CSRF/session revalidation. Persist operations before remote work. Revoke invalidates feed then queues every child key for verified removal. Replacement advances feed generation once, revokes all old keys, then creates new-generation credentials only after every old key is confirmed revoked and the subscription remains valid. Never clear sticky revoke on an old key. Uncertain removals block replacement and slot release. New credentials stay on the original logical device.
- [ ] Add owned `GET /v2/device-operations/{id}` for refresh/recovery. Response is safe operation metadata, never request snapshots or credentials. Resume progress through the existing bounded lifecycle/runtime tick, not a new daemon. Throttle completed independent replacements to one per device/24 hours; retries of one operation remain allowed.
- [ ] Include `operation` metadata in device responses (nullable) and `credential_generation` only if required for stale-response guards; update exact fixtures. While an operation is active, block conflicting location changes and derive pending UI states. Restrict all preferred/pending/connection/select queries to matching current generation; revoke remains all-generation. Count unfinished replacement as one occupied device even when no active old key remains. Preserve admin slot limits and existing legacy endpoint checks.
- [ ] Run SQLite service/API regressions and PostgreSQL-only migration/concurrency tests on an isolated authorized database. If no database exists, keep those tests runnable and request isolated-test permission before executing remotely; never claim their execution without evidence. Commit and obtain spec then quality review.

## Task 3: Opt-in, operator-gated expiry reminders

**Files:**
- Create `backend/app/services/vpn_expiry_reminders.py`, `backend/app/db/vpn_reminder_migrations.py`, `backend/tests/test_vpn_expiry_reminders.py`, `backend/tests/test_vpn_expiry_reminders_postgres.py`.
- Modify `backend/app/db/models.py`, `backend/app/db/migrations.py`, `backend/app/services/control_runtime.py`, `backend/app/api/routes/vpn_portal.py`, `backend/app/schemas/vpn_portal.py` and existing runtime tests.

- [ ] Red tests: disabled default sends nothing; customer disabled preference sends nothing; each 3-day/1-day/expired event at most once; renewed expiry invalidates queued old work; blocked bot skipped; 429 delays; uncertain outcome is not blindly retried; runtime restart reuses persistent deduplication; newly enabling does not replay historical intervals.
- [ ] Add customer reminder preference (default true but inert while operator gate is false), gate activation timestamp, persistent unique `(subscription_id, expires_at, event_kind)` records and bounded claim states. Reuse current bot registry, send-slot/cooldown helpers and delivery-result classification. Claim transaction commits before network; stale claimed outcome becomes uncertain. Before actual send, revalidate subscription expiry, preference and operator gate. No payment text.
- [ ] Define preference reads/changes as `GET/PATCH /api/vpn-portal/preferences` with `{expiry_reminders: boolean}`, using owned session and CSRF. Use existing runtime tick with bounded candidates/work per cycle; no separate scheduler or Telegram token source.
- [ ] Verify event text includes the correct subscription date and cabinet entry but no VPN secret. Run new tests and existing Telegram/broadcast/runtime/lifecycle tests. Commit and obtain spec then quality review.

## Task 4: Compact four-section customer interface

**Files:**
- Modify `frontend/src/vpn-portal/Portal.tsx`, `PortalHome.tsx`, `PortalAccount.tsx`, `ProfileCard.tsx`, `DeviceLocationPicker.tsx`, `navigation.ts`, `types.ts`, `api.ts`, `portal.css`.
- Create `frontend/src/vpn-portal/PortalHelp.tsx`, `PortalUsage.tsx`, `ConnectDevice.tsx`, `DeviceActions.tsx`, `clientApps.ts`, `deviceOperations.ts`.
- Extend `frontend/test/vpnPortalApi.test.mjs`, `vpnPortalHome.test.mjs`, `vpnPortalNavigation.test.mjs`, `vpnPortalConnection.test.mjs`, `vpnPortalCompatibility.test.mjs`, `browser/portal-ui.qa.mjs`; create focused app-import and operation tests.
- Update the existing cabinet surface brief with the approved layout contract before UI edits.

- [ ] Read Impeccable craft floor, existing design/surface guidance and Sasha UI references. Resolve approved reference composition against the existing comp-led preference before visual implementation, not by silently switching the build path. Reuse Veltrix assets; do not make a logo or new identity.
- [ ] Write failing client-contract tests for owned usage/settings/device operations and safe bounded errors. Example:

```javascript
await portalApi.usage(7);
assert.equal(calls[0].url, "/api/vpn-portal/usage?days=7");
assert.equal(calls[0].init.cache, "no-store");
assert.equal(calls[0].init.headers["X-CSRF-Token"], undefined);
```

- [ ] Implement types and exact API methods from Tasks 1–3. Keep credentials only in existing connection responses. Retain request UUID per submitted payload until definitive result; after a network timeout retry/recover the same operation. Scope every async completion and prepared URI to the current session/device/generation. Clear them on logout, replacement and country change.
- [ ] Implement `home/devices/help/account` view mapping, preserving `#profiles` alias and existing anchors. Compact home: access/remaining days, real usage, occupied-device count, device summary, connect block. Multiple subscriptions retain explicit selection for creation. No unconditional unavailable-statistics label or fake remaining traffic.
- [ ] Implement platform selector and official download/import links. Verify Happ/Hiddify URL schemes from official documentation during implementation. Build only allowlisted schemes from canonical prepared subscription URLs with correct URL encoding. Do not use telemetry or a third-party bridge. Keep manual copy/QR actions visible, and do not infer installed app/success from timeout.
- [ ] Implement creation, rename, country choice, revoke and replacement with synchronous action guards, native accessible fields, named confirmations and clear queued/uncertain states. A pending remote action cannot be represented as completed. Preserve the iOS gesture-safe prepared-link copy implementation.
- [ ] Implement real traffic totals/periods and per-device rows using existing formatters, unknown/null states and coverage labels. Contextual help and configured contact appear without invented destinations. Account includes reminder preference and legal links; no unpublished-price placeholder dominates the screen.
- [ ] Implement compact matte cards and glass navigation in existing tokens; 44px controls, safe areas, keyboard focus, reduced motion, readable fallback without blur. Check 320/390px mobile and desktop; avoid CSS features unsupported by the compatibility baseline without fallbacks. Run frontend tests/build. Commit and obtain spec then quality review.

## Task 5: Operator configuration panel

**Files:**
- Create `frontend/src/VpnCabinetSettingsPanel.tsx`, `frontend/src/vpnCabinetSettings.ts`, `frontend/test/vpnCabinetSettings.test.mjs`.
- Modify `frontend/src/api.ts`, `frontend/src/App.tsx`, `frontend/src/vpnAdminView.ts`, `frontend/src/VpnAdminNavigation.tsx` and relevant navigation/API tests.

- [ ] Red tests for exact settings routes, explicit operator activation, stale revisions and no saves on mounting. Verify a late result from one admin session cannot update another.
- [ ] Add one «Кабинет» admin tab; mount only when active. Read current settings, expose contact, notice title/body/start/end, and explicit reminder switch. Save whole known revision and show a conflict recovery action without silently replacing fresh server values. Notice text is plain text; empty contact removes support CTA.
- [ ] Explain that publishing an in-app notice does not send a bot message; enabling reminders affects future events. Saving settings must not call broadcast confirmation or create a campaign.
- [ ] Run new tests, navigation tests, entire frontend suite/build and settings API regressions. Commit and obtain spec then quality review.

## Task 6: Integrated verification and release handoff

**Files:**
- Update `docs/vpn-customer-portal-runbook.md`, `docs/vpn-service.md`, applicable cabinet surface brief and the plan completion checkboxes.
- For approved durable composition changes, use the Impeccable documenter to align `DESIGN.md` and `.impeccable/design.json` without repairing unrelated pre-existing drift.

- [ ] Run owned API, lifecycle, control, migration, reports, bot and runtime regression suites with warnings as errors. Verify isolated PostgreSQL concurrency and upgrade/re-run behavior before claiming release readiness. No production database experiments.
- [ ] Run `npm test`, `npm run build`, `git diff --check`. Batch mobile/desktop screenshots once, fix the combined material findings, capture at most one confirmation round before returning unresolved design choices to the user. Preserve actual screenshots as evidence, not mocked success assertions.
- [ ] Dispatch independent full spec/security review and Impeccable visual finish review with source paths, approved composition, screenshots and known limits; do not self-certify skipped device classes.
- [ ] Document settings, customer operations, replacement failure recovery, migration/rollback boundaries and disabled reminder defaults. No real broadcast or reminder send is part of acceptance testing.
- [ ] Report actual checks and any remaining limitations. Present branch/PR/deployment choices through finishing-a-development-branch; do not push, merge, install on production or enable messages without the relevant user decision.

## Self-review

All nine acceptance conditions in the specification map to Tasks 1–6. Device generation and subscription token generation are distinct. Usage stays an account/device report rather than a cross-node quota balance. The support destination is configurable and nullable; reminders and real Telegram sends do not become active as a side effect of installation. Existing keys change only after an explicit owned revoke/replace request.
