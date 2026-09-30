# Veltrix Public Release Shell Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` to execute each task with TDD,
> specification review and quality review.

**Goal:** Finish the customer-facing non-payment release layer: a safe public
tariff catalog, polished cabinet plans/help, local QR generation, a public
`/vpn/` page, and native Nginx request throttling, while leaving payment and
public-trial enablement off.

**Architecture:** Extend the existing `VpnPlan` model with only the two public
catalog controls it lacks, expose one read-only safe catalog through the existing
VPN portal API, and reuse that response in both the cabinet and a new Vite
multi-page `/vpn/` entry. Generate QR codes only in the authenticated browser so
the VPN URI never reaches a third party or a new server endpoint. Apply public
request throttling at the existing Nginx boundary. Do not change VPN keys,
endpoint assignment, trial policy, dispatcher behavior or payment state.

**Approved design:**
`docs/superpowers/specs/2026-09-23-veltrix-release-candidate-design.md`,
especially sections 4-6, 11-12 and 14-17.

**Tech stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, PostgreSQL additive
migrations, React 18, TypeScript, Vite, Nginx, Node built-in tests, pytest.

## Release boundary

- Payment remains absent and every paid plan says that purchase is coming soon.
- `VPN_PUBLIC_TRIAL_ENABLED`, `VPN_PORTAL_PUBLIC_ACCESS` and
  `VPN_READY_NOTIFICATIONS_ENABLED` remain off during deployment.
- Existing subscriptions, UUIDs, URIs, invitation slots and endpoints are not
  rewritten.
- Traffic telemetry, admin pagination/search and the second-node production
  acceptance remain follow-on release gates because they touch the node runtime
  or production topology, not this customer-facing shell.
- The new public page may be deployed while public trial admission remains off;
  its main action opens the official Telegram bot.

## Task 1: Add an explicit safe public tariff catalog

**Files:**

- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/migrations.py`
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/schemas/vpn_portal.py`
- Modify: `backend/app/api/routes/control.py`
- Modify: `backend/app/api/routes/vpn_portal.py`
- Modify: `backend/tests/test_vpn_portal_schema.py`
- Modify: `backend/tests/test_vpn_portal_api.py`
- Modify: `backend/tests/test_vpn_control_api.py`

1. Write failing tests proving that plans default to private, public ordering is
   non-negative, the migration is additive/idempotent, and the anonymous catalog
   returns only active public plans in `(display_order, id)` order.
2. Add `is_public BOOLEAN NOT NULL DEFAULT false` and
   `display_order INTEGER NOT NULL DEFAULT 0` with a non-negative check. Preserve
   all existing plan rows as private.
3. Extend admin create/update/response contracts and audit-covered endpoints so
   the owner can publish, hide and order plans.
4. Add `PortalPlan` containing only `id`, customer-facing name/description,
   duration, traffic limit, device limit, price/currency and derived `is_trial`.
   Never expose slug, timestamps or internal flags.
5. Add anonymous `GET /api/vpn-portal/plans`; select only `is_active=true` and
   `is_public=true`, derive `is_trial` from the configured trial slug, and keep
   the response safe when the portal or public trial is disabled.
6. Run focused schema/API/control tests and Ruff, then commit.

## Task 2: Publish and manage tariffs in the existing admin UI

**Files:**

- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/test/vpnCustomerApi.test.mjs` or add a focused plan test

1. Write failing tests for the two new fields and exact PATCH payload used by a
   publish/hide action.
2. Add `is_public` and `display_order` to the existing plan type and creation
   form. Do not create a second tariff editor.
3. Add a compact public/private status and a guarded publish/hide button to the
   existing tariff table. Keep delete behavior unchanged.
4. Reload the existing VPN datasets after a successful change and show the
   existing toast result.
5. Run frontend tests and TypeScript build, then commit.

## Task 3: Replace the cabinet tariff placeholder with the real catalog

**Files:**

- Modify: `frontend/src/vpn-portal/types.ts`
- Modify: `frontend/src/vpn-portal/api.ts`
- Create: `frontend/src/vpn-portal/PlanCatalog.tsx`
- Modify: `frontend/src/vpn-portal/Portal.tsx`
- Modify: `frontend/src/vpn-portal/portal.css`
- Modify: `frontend/test/vpnPortalApi.test.mjs`
- Add: `frontend/test/vpnPortalPlans.test.mjs`

1. Write failing tests for the anonymous catalog request, safe fields, Russian
   duration/device/traffic labels and the exact disabled purchase wording.
2. Load the catalog with the existing portal bootstrap/data flow and bounded
   generic error handling. Catalog failure must not sign the user out or hide
   working profiles.
3. Render accessible plan cards under `Тарифы`. Trial plans link to the existing
   trial card/status; all non-trial purchase controls are disabled and say
   `Покупка скоро будет доступна`.
4. Reuse existing visual tokens and mobile breakpoints. Do not add checkout,
   order, promo or payment abstractions.
5. Run portal tests and production build, then commit.

## Task 4: Add local QR display for an already revealed profile

**Files:**

- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/vpn-portal/ProfileCard.tsx`
- Modify: `frontend/src/vpn-portal/portal.css`
- Add: `frontend/src/vpn-portal/qr.ts`
- Add: `frontend/test/vpnPortalQr.test.mjs`

1. Write failing tests for a QR helper that accepts only a revealed URI, produces
   a local data URL, and exposes no network API.
2. Use one established browser QR dependency; no external image service and no
   backend QR endpoint. Dynamically import it only after the user asks to show
   the QR so the normal cabinet bundle stays small.
3. Add `Показать QR-код` beside copy after reveal. Provide clear busy/error/hide
   states, an image alt label, and clear the QR whenever session/profile/version
   state clears the URI.
4. Ensure the URI remains in authenticated memory only and is absent from URL,
   storage, logs and analytics.
5. Run dependency audit, focused tests and production build, then commit.

## Task 5: Add the public `/vpn/` product page and honest policy copy

**Files:**

- Create: `frontend/vpn/index.html`
- Create: `frontend/src/vpn-site/main.tsx`
- Create: `frontend/src/vpn-site/site.css`
- Modify: `frontend/vite.config.ts`
- Modify: `backend/app/api/routes/vpn_portal.py`
- Modify: `frontend/src/vpn-portal/types.ts`
- Modify: `frontend/src/vpn-portal/Portal.tsx`
- Add: `frontend/test/vpnPublicSite.test.mjs`
- Modify: `frontend/test/vpnPortalEntry.test.mjs`

1. Write failing entry/build/content tests for the third Vite page and safe bot
   CTA. Add the normalized public Telegram bot URL to portal config; expose no
   bot token, internal release ID or admin route.
2. Build a small semantic Russian page with hero, how it works, verified Happ
   platforms, public plan catalog, seven-day trial explanation, privacy summary,
   acceptable-use rules, support and footer links.
3. Be explicit that payment is not connected and paid plans cannot yet be
   purchased. Do not promise 24/7 support, total anonymity, unlimited capacity or
   collection of browsing history.
4. Link cabinet Help to `/vpn/#privacy` and `/vpn/#terms`; keep support text from
   server configuration.
5. Reuse Veltrix tokens and favicon, verify mobile and desktop layout, then run
   frontend tests/build and commit.

## Task 6: Apply native public-edge throttling and release headers

**Files:**

- Modify: `deploy/nginx-vpn-portal-http.conf`
- Modify: `deploy/nginx-vpn-portal-locations.conf`
- Modify: `backend/tests/test_vpn_portal_logging.py` or add a focused deployment
  configuration test
- Modify: `docs/vpn-customer-portal-runbook.md`

1. Write failing static configuration tests for an HTTP-context
   `limit_req_zone`, a bounded portal API limit, HTTP 429, `/vpn/` routing and
   security/cache headers.
2. Rate-limit the existing portal API location by client IP at a value compatible
   with the current two-second trial polling. Do not rate-limit the Telegram
   webhook with the customer policy.
3. Add explicit no-sniff/frame/referrer policy and correct cache policy for
   `/vpn/`; preserve `no-store` on customer API and cabinet pages.
4. Document install, `nginx -t`, rollback and 429 verification. No new daemon or
   application-side limiter.
5. Run focused tests and, where available, container/POSIX Nginx validation, then
   commit.

## Task 7: Integrated release verification and disabled production rollout

**Files:**

- Modify: `docs/current-state.md`
- Modify: `docs/vpn-service.md`

1. Run all backend tests affected by plans, portal, migrations, auth, trial and
   release readiness; run scoped Ruff.
2. Run the complete frontend test suite, production TypeScript/Vite build,
   dependency audit and `git diff --check`.
3. Run browser smoke on mobile and desktop for `/vpn/`, cabinet catalog, reveal,
   copy and local QR. Test that no VPN URI leaves the browser.
4. Request final specification and quality/security review; fix and re-run until
   there are no Critical or Important findings.
5. Deploy backend/frontend/Nginx with a fresh validated backup and all public
   admission/payment/ready-notification flags still off. Run migrations, Nginx
   syntax validation, local/public health checks and rollback rehearsal.
6. Confirm `/vpn/` is public, `/cabinet/` still works for admitted users, public
   trial remains unavailable, the bot remains functional and existing VPN links
   are unchanged.
7. Update current-state evidence and open/attach a PR. Do not enable public trial
   until the separate production topology and readiness gates pass.

## Acceptance criteria

- The owner can mark an active tariff public/private and control its order.
- Anonymous and cabinet users see only explicitly public active plans.
- Paid plan controls cannot create an order or imply that payment works.
- An authenticated user can reveal, copy and locally render a QR for a working
  profile without disclosing the URI outside the browser.
- `/vpn/` is a polished responsive Russian entry page with bot CTA, plans, help,
  privacy and terms.
- Public portal requests receive bounded Nginx throttling; secrets remain
  `no-store` and out of logs.
- Existing VPN data and access continue unchanged, and all public admission and
  payment flags remain off after rollout.
