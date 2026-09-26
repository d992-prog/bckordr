# Veltrix Release Readiness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one authoritative, fail-closed release-readiness report and an admin panel that explains every blocked public-release gate without enabling public trial or payment.

**Architecture:** Extend `vpn_endpoints` with external-verification evidence, compute readiness in one backend service, expose it through authenticated control endpoints, and render the result in a focused React component. The existing `app_settings` release marker is written only after a locked recheck passes; environment flags remain untouched.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy/PostgreSQL, React 18, TypeScript, Node test runner.

---

### Task 1: Persist external endpoint verification evidence

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/vpn_endpoint_migrations.py`
- Modify: `backend/tests/test_vpn_endpoint_schema.py`
- Modify: `backend/tests/test_vpn_endpoint_migrations.py`

- [ ] **Step 1: Write failing metadata tests**

Add assertions that `vpn_endpoints` exposes nullable `external_verified_at` and
`external_config_fingerprint`, with the fingerprint bounded to 64 characters.

```python
columns = VpnEndpoint.__table__.c
assert columns.external_verified_at.nullable is True
assert columns.external_config_fingerprint.type.length == 64
```

- [ ] **Step 2: Run the focused tests and confirm failure**

Run:

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_endpoint_schema.py -q
```

Expected: failure because both columns are absent.

- [ ] **Step 3: Add the model fields and additive migrations**

Add to `VpnEndpoint`:

```python
external_verified_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
external_config_fingerprint: Mapped[str | None] = mapped_column(
    String(64), nullable=True
)
```

Append idempotent migrations:

```python
"ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS external_verified_at TIMESTAMPTZ NULL",
"ALTER TABLE vpn_endpoints ADD COLUMN IF NOT EXISTS external_config_fingerprint VARCHAR(64) NULL",
```

- [ ] **Step 4: Extend the real-PostgreSQL migration test**

Run the migrations twice and assert both columns exist and preserve an existing
endpoint row unchanged.

- [ ] **Step 5: Run focused schema tests**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_endpoint_schema.py backend/tests/test_vpn_endpoint_migrations.py -q
```

Expected: SQLite metadata passes; PostgreSQL cases pass when the safe test URL is configured and otherwise skip explicitly.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/db/models.py backend/app/db/vpn_endpoint_migrations.py backend/tests/test_vpn_endpoint_schema.py backend/tests/test_vpn_endpoint_migrations.py
git commit -m "feat(vpn): store endpoint external verification"
```

### Task 2: Build the pure release-readiness evaluator

**Files:**
- Create: `backend/app/services/vpn_release_readiness.py`
- Modify: `backend/app/services/vpn_endpoint_types.py`
- Create: `backend/tests/test_vpn_release_readiness.py`

- [ ] **Step 1: Write failing evaluator tests**

Cover at least: no endpoints, one endpoint, stale health, missing capacity, no
external proof, full capacity, invalid trial plan, queued/uncertain operations,
stale backup, two independent healthy endpoints, and payment/public flags.

```python
report = evaluate_release_snapshot(snapshot, now=NOW)
assert report.ready is False
assert {check.code for check in report.checks if check.state == "fail"} == {
    "vpn_second_node_missing",
    "vpn_capacity_unset",
}
```

- [ ] **Step 2: Confirm the tests fail because the service is absent**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_release_readiness.py -q
```

- [ ] **Step 3: Implement minimal immutable report types**

```python
@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    code: str
    state: Literal["pass", "warn", "fail"]
    message: str
    entity_id: int | None = None
    observed_at: datetime | None = None

@dataclass(frozen=True, slots=True)
class ReleaseReadiness:
    ready: bool
    checked_at: datetime
    checks: tuple[ReleaseCheck, ...]
```

Keep messages mapped from fixed codes; `entity_id` may contain only a database
integer used for an admin-panel anchor. Never include URI, UUID, IP, SSH output
or raw exceptions.

- [ ] **Step 4: Add a database snapshot loader**

Use grouped SQLAlchemy queries for plans, endpoints/workers, capacity occupancy,
active operations and maintenance jobs. Accept operational/backup observations
as small adapter values so the evaluator remains deterministic in tests. Missing
observations fail closed rather than silently passing.

- [ ] **Step 5: Add the endpoint public-configuration fingerprint**

Add `public_endpoint_fingerprint` beside `VpnEndpointTarget`. Hash canonical JSON
containing only the externally observable endpoint identity: inbound ID, host,
port, protocol, transport, security, server name, public key, short ID,
fingerprint and flow. Do not include database endpoint/worker IDs so ordinary
record maintenance cannot invalidate unchanged public identity.

```python
return hashlib.sha256(
    json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
```

- [ ] **Step 6: Run the evaluator tests**

Expected: every fail/warn/pass transition passes and exception strings never
appear in public messages.

- [ ] **Step 7: Commit**

```powershell
git add backend/app/services/vpn_release_readiness.py backend/app/services/vpn_endpoint_types.py backend/tests/test_vpn_release_readiness.py
git commit -m "feat(vpn): compute release readiness"
```

### Task 3: Expose authenticated readiness and external verification APIs

**Files:**
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Modify: `backend/app/services/app_settings.py`
- Create: `backend/tests/test_vpn_release_readiness_api.py`

- [ ] **Step 1: Write failing API tests**

Test authenticated GET, anonymous 401, external confirmation, fingerprint
mismatch invalidation, locked marker commit, blocked commit returning 409, and
that neither endpoint changes `VPN_PUBLIC_TRIAL_ENABLED`.

- [ ] **Step 2: Add bounded schemas**

```python
class VpnReleaseCheckResponse(BaseModel):
    code: str
    state: Literal["pass", "warn", "fail"]
    message: str
    observed_at: datetime | None = None

class VpnReleaseReadinessResponse(BaseModel):
    ready: bool
    checked_at: datetime
    checks: list[VpnReleaseCheckResponse]

class VpnEndpointExternalVerificationRequest(BaseModel):
    confirmed: Literal[True]
    model_config = ConfigDict(extra="forbid")
```

- [ ] **Step 3: Add GET `/control/vpn/release-readiness`**

Require `require_admin`, load the current report and set `Cache-Control: no-store`.

- [ ] **Step 4: Add POST external verification**

Lock the endpoint, compute its current public fingerprint server-side, set the
two evidence fields and add `vpn_endpoint_external_verification` to the existing
admin audit log. Do not accept a fingerprint from the browser.

- [ ] **Step 5: Add POST readiness commit**

Within one transaction, acquire the existing serialized VPN mutation lock,
recompute all hard checks, reject non-ready state with 409, write
`vpn_public_release_ready_v1` using the configured release ID, and audit the
action. Reject empty or malformed release IDs.

- [ ] **Step 6: Run API tests and Ruff**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_release_readiness_api.py -q
backend/.venv/Scripts/python -m ruff check backend/app backend/tests/test_vpn_release_readiness.py backend/tests/test_vpn_release_readiness_api.py
```

- [ ] **Step 7: Commit**

```powershell
git add backend/app/schemas/control.py backend/app/api/routes/control.py backend/app/services/app_settings.py backend/tests/test_vpn_release_readiness_api.py
git commit -m "feat(vpn): expose release readiness controls"
```

### Task 4: Add the focused admin panel

**Files:**
- Create: `frontend/src/VpnReleaseReadinessPanel.tsx`
- Create: `frontend/src/vpnReleaseReadiness.ts`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Create: `frontend/test/vpnReleaseReadiness.test.mjs`

- [ ] **Step 1: Write failing frontend state tests**

Test grouping, fixed Russian status labels, summary counts and commit-button
eligibility. Keep the pure helper independent of React.

```typescript
assert.deepEqual(summarizeReleaseChecks(checks), {
  pass: 2,
  warn: 1,
  fail: 1,
  canCommit: false,
});
```

- [ ] **Step 2: Add API types and methods**

Add `VpnReleaseCheck`, `VpnReleaseReadiness`, `getVpnReleaseReadiness`,
`confirmVpnEndpointExternalVerification` and `commitVpnReleaseReadiness`.

- [ ] **Step 3: Implement the panel**

Render one summary card and compact grouped checks. Show existing navigation
actions rather than duplicating node/capacity forms. Require a named confirmation
before the marker commit.

- [ ] **Step 4: Integrate with the existing VPN admin load**

Load readiness with other VPN overview requests, show a bounded inline error and
refresh after capacity or external-verification mutations.

- [ ] **Step 5: Run frontend tests and build**

```powershell
cd frontend
npm test
npm run build
```

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/VpnReleaseReadinessPanel.tsx frontend/src/vpnReleaseReadiness.ts frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/test/vpnReleaseReadiness.test.mjs
git commit -m "feat(vpn): show release readiness in admin"
```

### Task 5: Verify the readiness release slice

**Files:**
- Modify: `docs/vpn-service.md`
- Modify: `docs/current-state.md`

- [ ] **Step 1: Document fail-closed behavior and rollout**

State that a green panel writes only the database marker and never changes the
public-trial or payment flags.

- [ ] **Step 2: Run focused backend tests, full frontend tests and build**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_release_readiness.py backend/tests/test_vpn_release_readiness_api.py backend/tests/test_vpn_endpoint_schema.py -q
backend/.venv/Scripts/python -m ruff check backend/app backend/tests
cd frontend
npm test
npm run build
```

- [ ] **Step 3: Run `git diff --check` and inspect the final diff**

- [ ] **Step 4: Commit docs**

```powershell
git add docs/vpn-service.md docs/current-state.md
git commit -m "docs(vpn): document release readiness gate"
```
