# Veltrix Public Trial and Capacity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a disabled-by-default public seven-day Telegram trial that can be activated once, provisions through the existing durable VPN queue, and never allocates beyond an explicitly configured endpoint capacity.

**Architecture:** Extend the existing customer, endpoint and access-key rows instead of introducing parallel account or provisioning models. A focused `vpn_public_trial` service owns trial policy and atomically creates the existing `VpnSubscription`/`VpnAccessKey` chain after locking both the Telegram customer and selected endpoint; bot and portal call that same service. The existing control runtime delivers a readiness notification after the strict dispatcher has produced a usable URI, while all public behavior remains behind a fail-closed feature flag and release marker.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, PostgreSQL concurrency tests, SQLite unit/API tests, React 18, TypeScript, Vite, Node's built-in test runner.

---

## Delivery boundary

This is the first independently releasable subsystem from
`docs/superpowers/specs/2026-09-23-veltrix-release-candidate-design.md`.
It delivers public trial admission, endpoint capacity and readiness notification
behind disabled flags. It does not publish the marketing page, add QR codes,
collect traffic counters, redesign the full admin workspace, configure the second
production node or enable the public flag in production. Those are separate plans
because each has its own data flow and release risks.

The implementation must preserve all existing UUIDs, invitation slots, friend
subscriptions, the owner's 8443 link and every active endpoint. A redeemed friend
trial is backfilled as already used. No task may call the legacy direct
`provision_vpn_access_key()` path for endpoint-bound profiles.

## File map

- `backend/app/db/models.py`: trial marker, endpoint capacity and notification claim columns.
- `backend/app/db/migrations.py`: additive idempotent DDL and backfill for existing trial users.
- `backend/app/db/vpn_endpoint_migrations.py`: fresh endpoint-table DDL matching the ORM.
- `backend/app/core/config.py`: disabled public-trial and ready-notification settings.
- `backend/app/services/app_settings.py`: public-release readiness marker name.
- `backend/app/services/vpn_policy.py`: endpoint occupancy and deterministic capacity-aware selection.
- `backend/app/services/vpn_public_trial.py`: sole trial status and activation policy.
- `backend/app/services/vpn_friend_invitations.py`: mark invitation trials as consumed by the same policy.
- `backend/app/services/vpn_portal_auth.py`: admit a new Telegram identity only while the public-trial gate is enabled.
- `backend/app/schemas/vpn_portal.py`: safe customer trial contracts.
- `backend/app/api/routes/vpn_portal.py`: authenticated trial status and activation endpoints.
- `backend/app/services/vpn_telegram.py`: trial command, button and commit-before-reply flow.
- `backend/app/services/vpn_ready_notifications.py`: claim and deliver one profile-ready message.
- `backend/app/services/control_runtime.py`: schedule readiness delivery beside existing VPN maintenance.
- `backend/app/schemas/control.py`: capacity list/update contracts.
- `backend/app/api/routes/control.py`: audited endpoint-capacity administration.
- `frontend/src/vpn-portal/types.ts`: trial response types.
- `frontend/src/vpn-portal/api.ts`: typed status/activation calls.
- `frontend/src/vpn-portal/TrialCard.tsx`: trial call-to-action and state rendering.
- `frontend/src/vpn-portal/Portal.tsx`: load and poll trial/profile state.
- `frontend/src/vpn-portal/portal.css`: responsive trial card states using existing tokens.
- `frontend/src/api.ts`: typed capacity administration calls.
- `frontend/src/VpnEndpointCapacityPanel.tsx`: small focused capacity editor.
- `frontend/src/App.tsx`: load and render the new capacity panel in the VPN workspace.
- `frontend/src/styles.css`: capacity table/card layout.
- `backend/tests/test_vpn_public_trial_schema.py`: model, migration, config and backfill coverage.
- `backend/tests/test_vpn_public_trial.py`: policy and capacity unit coverage.
- `backend/tests/test_vpn_public_trial_postgres.py`: real concurrent activation and allocation races.
- `backend/tests/test_vpn_portal_api.py`: public identity admission and trial route boundaries.
- `backend/tests/test_vpn_telegram.py`: bot activation and replay behavior.
- `backend/tests/test_vpn_ready_notifications.py`: notification claim/delivery coverage.
- `backend/tests/test_vpn_control_api.py`: capacity admin API and audit coverage.
- `frontend/test/vpnPortalApi.test.mjs`: exact customer API calls.
- `frontend/test/vpnPortalTrial.test.mjs`: trial card and bounded polling behavior.
- `frontend/test/vpnEndpointCapacity.test.mjs`: admin capacity UI behavior.
- `docs/current-state.md`: implemented behavior, disabled flags and remaining release gates.

### Task 1: Add the additive trial, capacity and notification schema

**Files:**
- Modify: `backend/app/db/models.py:669-879`
- Modify: `backend/app/db/migrations.py`
- Modify: `backend/app/db/vpn_endpoint_migrations.py:1-35`
- Modify: `backend/app/core/config.py:46-90`
- Create: `backend/tests/test_vpn_public_trial_schema.py`

- [ ] **Step 1: Write failing model and fail-closed setting tests**

Create `backend/tests/test_vpn_public_trial_schema.py` with the exact contract:

```python
from app.core.config import Settings
from app.db.models import VpnAccessKey, VpnCustomer, VpnEndpoint


def test_public_trial_schema_is_additive() -> None:
    assert "trial_started_at" in VpnCustomer.__table__.columns
    assert "max_active_profiles" in VpnEndpoint.__table__.columns
    assert "capacity_warning_percent" in VpnEndpoint.__table__.columns
    assert "ready_notice_claimed_at" in VpnAccessKey.__table__.columns
    assert "ready_notified_at" in VpnAccessKey.__table__.columns


def test_public_trial_defaults_fail_closed() -> None:
    settings = Settings(_env_file=None)
    assert settings.vpn_public_trial_enabled is False
    assert settings.vpn_public_trial_release_id == ""
    assert settings.vpn_public_trial_plan_slug == "trial-7d"
    assert settings.vpn_endpoint_health_max_age_seconds == 300
    assert settings.vpn_ready_notifications_enabled is False
```

- [ ] **Step 2: Run the schema test and verify the missing-column failure**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_public_trial_schema.py -q
```

Expected: FAIL because the five ORM columns and five settings do not exist.

- [ ] **Step 3: Add the minimal ORM columns and settings**

Add these mapped columns without changing existing defaults or relationships:

```python
# VpnCustomer
trial_started_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True, index=True
)

# VpnEndpoint
max_active_profiles: Mapped[int | None] = mapped_column(Integer, nullable=True)
capacity_warning_percent: Mapped[int] = mapped_column(
    Integer, default=80, server_default="80", nullable=False
)

# VpnAccessKey
ready_notice_claimed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
ready_notified_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

Add endpoint constraints to `VpnEndpoint.__table_args__`:

```python
CheckConstraint(
    "max_active_profiles IS NULL OR max_active_profiles > 0",
    name="ck_vpn_endpoint_capacity",
),
CheckConstraint(
    "capacity_warning_percent BETWEEN 1 AND 100",
    name="ck_vpn_endpoint_capacity_warning",
),
```

Add settings beside the existing VPN flags:

```python
vpn_public_trial_enabled: bool = Field(default=False, alias="VPN_PUBLIC_TRIAL_ENABLED")
vpn_public_trial_release_id: str = Field(default="", alias="VPN_PUBLIC_TRIAL_RELEASE_ID")
vpn_public_trial_plan_slug: str = Field(default="trial-7d", alias="VPN_PUBLIC_TRIAL_PLAN_SLUG")
vpn_endpoint_health_max_age_seconds: int = Field(
    default=300, alias="VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS"
)
vpn_ready_notifications_enabled: bool = Field(
    default=False, alias="VPN_READY_NOTIFICATIONS_ENABLED"
)
```

- [ ] **Step 4: Add idempotent PostgreSQL DDL and existing-trial backfill tests**

Append additive migration statements:

```sql
ALTER TABLE vpn_customers
    ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ NULL;
CREATE INDEX IF NOT EXISTS ix_vpn_customers_trial_started_at
    ON vpn_customers(trial_started_at);
ALTER TABLE vpn_endpoints
    ADD COLUMN IF NOT EXISTS max_active_profiles INTEGER NULL;
ALTER TABLE vpn_endpoints
    ADD COLUMN IF NOT EXISTS capacity_warning_percent INTEGER NOT NULL DEFAULT 80;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'vpn_endpoints'::regclass
          AND conname = 'ck_vpn_endpoint_capacity'
    ) THEN
        ALTER TABLE vpn_endpoints ADD CONSTRAINT ck_vpn_endpoint_capacity
            CHECK (max_active_profiles IS NULL OR max_active_profiles > 0);
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'vpn_endpoints'::regclass
          AND conname = 'ck_vpn_endpoint_capacity_warning'
    ) THEN
        ALTER TABLE vpn_endpoints ADD CONSTRAINT ck_vpn_endpoint_capacity_warning
            CHECK (capacity_warning_percent BETWEEN 1 AND 100);
    END IF;
END;
$$;
ALTER TABLE vpn_access_keys
    ADD COLUMN IF NOT EXISTS ready_notice_claimed_at TIMESTAMPTZ NULL;
ALTER TABLE vpn_access_keys
    ADD COLUMN IF NOT EXISTS ready_notified_at TIMESTAMPTZ NULL;
UPDATE vpn_customers AS customer
SET trial_started_at = source.started_at
FROM (
    SELECT subscription.customer_id, MIN(subscription.starts_at) AS started_at
    FROM vpn_subscriptions AS subscription
    WHERE subscription.status = 'trial' AND subscription.starts_at IS NOT NULL
    GROUP BY subscription.customer_id
) AS source
WHERE customer.id = source.customer_id
  AND customer.trial_started_at IS NULL;
UPDATE vpn_access_keys
SET ready_notified_at = COALESCE(last_synced_at, issued_at, created_at)
WHERE ready_notified_at IS NULL
  AND status = 'active'
  AND config_uri IS NOT NULL;
```

Add `max_active_profiles`, `capacity_warning_percent` and both named CHECK
constraints to the original `CREATE TABLE vpn_endpoints` statement in
`vpn_endpoint_migrations.py`, so a fresh database and an upgraded database have
the same endpoint schema.

Use the repository's existing PostgreSQL migration fixture to create a pre-change
schema with one ordinary customer, one redeemed friend trial and one active key.
Assert that the friend receives `trial_started_at`, the old active key is marked as
already notified, ordinary customer data is unchanged, constraints reject zero
capacity and warning values outside 1..100, and a second migration run is a no-op.

- [ ] **Step 5: Run schema checks and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_public_trial_schema.py -q
.venv/Scripts/python.exe -m ruff check app/db/models.py app/db/migrations.py app/db/vpn_endpoint_migrations.py app/core/config.py tests/test_vpn_public_trial_schema.py
```

Expected: all selected tests pass and Ruff reports no errors.

Commit:

```powershell
git add backend/app/db/models.py backend/app/db/migrations.py backend/app/db/vpn_endpoint_migrations.py backend/app/core/config.py backend/tests/test_vpn_public_trial_schema.py
git commit -m "feat(vpn): add public trial capacity schema"
```

### Task 2: Add deterministic endpoint capacity selection

**Files:**
- Modify: `backend/app/services/vpn_policy.py:21-200`
- Create: `backend/tests/test_vpn_public_trial.py`

- [ ] **Step 1: Write failing selection and occupancy tests**

Create tests that build three endpoint/worker pairs and prove the exact policy:

```python
selected = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=300)
assert selected.endpoint.id == least_utilized_ready_endpoint.id
assert selected.occupied_profiles == 2
assert selected.max_active_profiles == 10
```

The same file must assert that selection excludes an endpoint when it is
`draining`, unverified, non-REALITY, missing a limit, full, attached to an archived
or non-ready worker, or has `vpn_last_checked_at < NOW - 300 seconds`. Count
`pending_sync`, `syncing`, `active`, `pending_suspend`, `suspended`,
`pending_revoke` and `failed`; do not count `revoked`.

- [ ] **Step 2: Run the focused test and verify the missing-selector failure**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_public_trial.py -q
```

Expected: FAIL importing `select_public_vpn_endpoint`.

- [ ] **Step 3: Implement the occupancy view and locked selector**

Add to `vpn_policy.py`:

```python
NODE_CAPACITY_STATUSES = (
    "pending_sync",
    "syncing",
    "active",
    "pending_suspend",
    "suspended",
    "pending_revoke",
    "failed",
)


@dataclass(frozen=True, slots=True)
class VpnEndpointCapacity:
    endpoint: VpnEndpoint
    occupied_profiles: int
    max_active_profiles: int

    @property
    def utilization(self) -> float:
        return self.occupied_profiles / self.max_active_profiles
```

Implement `select_public_vpn_endpoint()` with this sequence:

```python
async def select_public_vpn_endpoint(
    session: AsyncSession,
    *,
    now: datetime,
    health_max_age_seconds: int,
    lock: bool = False,
) -> VpnEndpointCapacity | None:
    current = _as_utc(now)
    assert current is not None
    healthy_after = current - timedelta(seconds=max(health_max_age_seconds, 1))
    candidates = (
        await session.execute(
            select(VpnEndpoint, WorkerNode)
            .join(WorkerNode, WorkerNode.id == VpnEndpoint.worker_id)
            .where(
                VpnEndpoint.status == "ready",
                VpnEndpoint.security == "reality",
                VpnEndpoint.verified_at.is_not(None),
                VpnEndpoint.max_active_profiles.is_not(None),
                VpnEndpoint.max_active_profiles > 0,
                WorkerNode.archived_at.is_(None),
                WorkerNode.is_enabled.is_(True),
                WorkerNode.vpn_enabled.is_(True),
                WorkerNode.vpn_runtime_status == "ready",
                WorkerNode.vpn_last_checked_at >= healthy_after,
            )
            .order_by(VpnEndpoint.id.asc())
        )
    ).all()
    ranked: list[tuple[float, int]] = []
    snapshots: dict[int, VpnEndpointCapacity] = {}
    for endpoint, worker in candidates:
        if not (await evaluate_vpn_node(session, worker)).eligible:
            continue
        occupied = int(await session.scalar(
            select(func.count(VpnAccessKey.id)).where(
                VpnAccessKey.endpoint_id == endpoint.id,
                VpnAccessKey.status.in_(NODE_CAPACITY_STATUSES),
            )
        ) or 0)
        assert endpoint.max_active_profiles is not None
        if occupied < endpoint.max_active_profiles:
            ranked.append((occupied / endpoint.max_active_profiles, endpoint.id))
            snapshots[endpoint.id] = VpnEndpointCapacity(
                endpoint, occupied, endpoint.max_active_profiles
            )
    if not lock:
        return snapshots[sorted(ranked)[0][1]] if ranked else None
    for _ratio, endpoint_id in sorted(ranked):
        locked = await session.scalar(
            select(VpnEndpoint).where(VpnEndpoint.id == endpoint_id).with_for_update()
        )
        if locked is None or locked.status != "ready" or locked.max_active_profiles is None:
            continue
        worker = await session.get(WorkerNode, locked.worker_id)
        if worker is None or not (await evaluate_vpn_node(session, worker)).eligible:
            continue
        occupied = int(await session.scalar(
            select(func.count(VpnAccessKey.id)).where(
                VpnAccessKey.endpoint_id == locked.id,
                VpnAccessKey.status.in_(NODE_CAPACITY_STATUSES),
            )
        ) or 0)
        if occupied < locked.max_active_profiles:
            return VpnEndpointCapacity(locked, occupied, locked.max_active_profiles)
    return None
```

Import `timedelta` and `VpnEndpoint`; reuse the existing `_as_utc()` helper. Do
not add a new allocator class or dependency. Status reads use the default
`lock=False`; activation passes `lock=True` and keeps the endpoint row lock until
its access key is flushed.

- [ ] **Step 4: Run selector tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_public_trial.py -q
.venv/Scripts/python.exe -m ruff check app/services/vpn_policy.py tests/test_vpn_public_trial.py
```

Expected: all selected tests pass.

Commit:

```powershell
git add backend/app/services/vpn_policy.py backend/tests/test_vpn_public_trial.py
git commit -m "feat(vpn): select endpoints within capacity"
```

### Task 3: Implement one-time public trial policy

**Files:**
- Modify: `backend/app/services/app_settings.py`
- Create: `backend/app/services/vpn_public_trial.py`
- Modify: `backend/app/services/vpn_friend_invitations.py:782-863`
- Modify: `backend/tests/test_vpn_public_trial.py`
- Create: `backend/tests/test_vpn_public_trial_postgres.py`

- [ ] **Step 1: Write failing status, activation and friend-compatibility tests**

Define expected status values and assertions:

```python
assert (await public_trial_status(db, settings, customer, NOW)).state == "available"
activated = await activate_public_trial(db, settings, identity, NOW)
assert activated.state == "preparing"
assert activated.subscription_id is not None
assert activated.access_key_id is not None
assert activated.expires_at == NOW + timedelta(days=7)
assert customer.trial_started_at == NOW
```

Also assert:

- disabled flag, bad release marker, missing active `trial-7d` plan and no-capacity
  state never create rows;
- a prior or expired trial returns `used`, not a new subscription;
- replay during the same preparing/active trial returns the same subscription and
  key;
- an already redeemed friend is `used`;
- redeeming a friend invitation sets `trial_started_at` and a user with a public
  trial cannot redeem another seven-day invitation;
- the profile uses the selected endpoint, UUID, panel sub-ID, `pending_sync` and
  the existing `stage_vpn_control_operation()` path.

- [ ] **Step 2: Add the readiness marker and minimal service contracts**

In `app_settings.py` add:

```python
_VPN_PUBLIC_RELEASE_READY_KEY = "vpn_public_release_ready_v1"
```

Create `vpn_public_trial.py` with these public types:

```python
TrialState = Literal[
    "disabled", "available", "capacity_paused", "preparing", "active", "used"
]


@dataclass(frozen=True, slots=True)
class PublicTrialView:
    state: TrialState
    duration_days: int = 7
    profile_limit: int = 1
    subscription_id: int | None = None
    access_key_id: int | None = None
    expires_at: datetime | None = None


class PublicTrialUnavailable(RuntimeError):
    pass


class PublicTrialConflict(RuntimeError):
    pass
```

Keep `_RELEASE_ID = re.compile(r"[0-9a-f]{64}")` local. Read the marker through
`AppSetting`, require `vpn_public_trial_enabled`, the strict dispatcher flag, an
exact marker match, an active plan whose slug equals
`settings.vpn_public_trial_plan_slug`, `duration_days == 7` and
`max_devices == 1`, then call `select_public_vpn_endpoint()`.

- [ ] **Step 3: Implement status lookup without exposing secrets**

`public_trial_status()` must lock nothing and return only IDs/dates:

```python
async def public_trial_status(
    db: AsyncSession,
    settings: Settings,
    customer: VpnCustomer,
    now: datetime,
) -> PublicTrialView:
    chain = await _existing_trial_chain(db, customer.id)
    if chain is not None:
        subscription, access_key, operation = chain
        return _view_existing(subscription, access_key, operation, now)
    if customer.trial_started_at is not None:
        return PublicTrialView("used")
    if not settings.vpn_public_trial_enabled:
        return PublicTrialView("disabled")
    try:
        await require_public_trial_readiness(db, settings, now)
    except PublicTrialUnavailable:
        return PublicTrialView("capacity_paused")
    return PublicTrialView("available")
```

`_view_existing()` maps queued/claimed or a key without URI to `preparing`, a
succeeded provision with active key and unexpired subscription to `active`, and an
expired/disabled/revoked chain to `used`. It never returns `config_uri`.

- [ ] **Step 4: Implement atomic activation through the durable queue**

Use `resolve_telegram_customer()` and then re-lock that exact customer row. Under
the lock, return the existing chain if it exists; reject when
`trial_started_at is not None`; acquire a capacity-locked endpoint; copy plan
limits into the new subscription; create one key and stage one operation:

```python
subscription = VpnSubscription(
    customer_id=customer.id,
    plan_id=plan.id,
    status="trial",
    starts_at=current,
    expires_at=current + timedelta(days=plan.duration_days),
    traffic_limit_gb=plan.traffic_limit_gb,
    max_devices=plan.max_devices,
)
db.add(subscription)
await db.flush()
access_key = VpnAccessKey(
    subscription_id=subscription.id,
    worker_id=capacity.endpoint.worker_id,
    endpoint_id=capacity.endpoint.id,
    protocol="vless",
    external_uuid=str(uuid4()),
    verified_client_email=f"veltrix-trial-{customer.id}",
    panel_sub_id=uuid4().hex,
    display_name="Veltrix VPN",
    status="pending_sync",
    issued_at=current,
    expires_at=subscription.expires_at,
)
db.add(access_key)
await db.flush()
await stage_vpn_control_operation(db, access_key.id, "provision", now=current)
customer.trial_started_at = current
await db.flush()
```

Translate `VpnControlIntentError` into `PublicTrialUnavailable` so the surrounding
transaction rolls back the customer marker, subscription and key together.

- [ ] **Step 5: Make friend trials consume the same one-time right**

Immediately after `resolve_telegram_customer()` in `redeem_friend_invitation()`:

```python
if customer.trial_started_at is not None:
    _rejected()
customer.trial_started_at = current
```

The nested transaction already protects the invitation and customer chain. Update
friend invitation tests to assert rollback clears the marker whenever provisioning
staging fails.

- [ ] **Step 6: Prove real PostgreSQL concurrency**

In `test_vpn_public_trial_postgres.py`, use two independent sessions and an
`asyncio.Event` barrier to make the same Telegram identity activate concurrently.
Assert exactly one trial subscription, one access key, one generation-1 control
operation and one `trial_started_at`. Add a second race with two identities and a
capacity of one; assert one activation succeeds and the other returns
`capacity_paused` without consuming its trial right.

- [ ] **Step 7: Run service and concurrency tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_public_trial.py tests/test_vpn_public_trial_postgres.py tests/test_vpn_friend_invitations.py tests/test_vpn_friend_invitations_postgres.py -q
.venv/Scripts/python.exe -m ruff check app/services/app_settings.py app/services/vpn_public_trial.py app/services/vpn_friend_invitations.py tests/test_vpn_public_trial.py tests/test_vpn_public_trial_postgres.py
```

Expected: all selected tests pass; PostgreSQL concurrency tests must run, not skip,
before production rollout.

Commit:

```powershell
git add backend/app/services/app_settings.py backend/app/services/vpn_public_trial.py backend/app/services/vpn_friend_invitations.py backend/tests/test_vpn_public_trial.py backend/tests/test_vpn_public_trial_postgres.py
git commit -m "feat(vpn): activate one-time public trials"
```

### Task 4: Expose safe portal trial status and activation

**Files:**
- Modify: `backend/app/services/vpn_portal_auth.py:135-194`
- Modify: `backend/app/services/vpn_portal_http.py:28-42`
- Modify: `backend/app/schemas/vpn_portal.py`
- Modify: `backend/app/api/routes/vpn_portal.py`
- Modify: `backend/tests/test_vpn_portal_auth.py`
- Modify: `backend/tests/test_vpn_portal_api.py`

- [ ] **Step 1: Write failing public admission and route tests**

Add API tests with a fresh Telegram identity:

```python
status_response = client.get("/api/vpn-portal/trial")
assert status_response.status_code == 200
assert status_response.json() == {
    "state": "available",
    "duration_days": 7,
    "profile_limit": 1,
    "subscription_id": None,
    "access_key_id": None,
    "expires_at": None,
}
activated = client.post(
    "/api/vpn-portal/trial/activate",
    headers={"X-CSRF-Token": csrf, "Origin": "https://veltrix.qzz.io"},
)
assert activated.status_code == 200
assert activated.json()["state"] == "preparing"
```

Assert unauthenticated is 401, missing/wrong CSRF is 403, a disabled trial does not
admit a new identity, response headers are `no-store`, replay returns the same IDs,
and capacity exhaustion returns HTTP 409 with static detail
`public_trial_capacity_unavailable` without leaking node data.

- [ ] **Step 2: Admit public identities only under the new flag**

Extend `identity_allowed()` with one fail-closed branch:

```python
if settings.vpn_public_trial_enabled:
    return True
```

Extend `portal_capabilities()` so `access_configured` treats that flag as a valid
admission method. Do not turn on `VPN_PORTAL_PUBLIC_ACCESS`, weaken Telegram
signature checks or bypass the database session checks.

- [ ] **Step 3: Add exact schemas and endpoints**

Add:

```python
class PortalTrial(BaseModel):
    state: Literal[
        "disabled", "available", "capacity_paused", "preparing", "active", "used"
    ]
    duration_days: int
    profile_limit: int
    subscription_id: int | None
    access_key_id: int | None
    expires_at: datetime | None
```

Expose:

```python
@router.get("/trial", response_model=PortalTrial)
async def trial_status(
    request: Request,
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
) -> PortalTrial:
    view = await public_trial_status(db, _settings(request), principal.customer, datetime.now(UTC))
    return PortalTrial.model_validate(asdict(view))


@router.post("/trial/activate", response_model=PortalTrial)
async def activate_trial(
    request: Request,
    principal: PortalPrincipal = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> PortalTrial:
    identity = TelegramIdentity(
        user_id=principal.session.telegram_user_id,
        username=principal.customer.telegram_username,
        first_name=principal.customer.first_name,
        last_name=principal.customer.last_name,
    )
    try:
        view = await activate_public_trial(db, _settings(request), identity, datetime.now(UTC))
    except PublicTrialUnavailable:
        await db.rollback()
        raise HTTPException(409, "public_trial_capacity_unavailable") from None
    except PublicTrialConflict:
        await db.rollback()
        raise HTTPException(409, "public_trial_already_used") from None
    await db.commit()
    return PortalTrial.model_validate(asdict(view))
```

Use the existing portal middleware for generic safe error mapping and `no-store`.

- [ ] **Step 4: Run portal boundary tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_portal_auth.py tests/test_vpn_portal_api.py -q
.venv/Scripts/python.exe -m ruff check app/services/vpn_portal_auth.py app/services/vpn_portal_http.py app/schemas/vpn_portal.py app/api/routes/vpn_portal.py
```

Expected: all selected tests pass and existing friend/OIDC/Mini App cases remain
green.

Commit:

```powershell
git add backend/app/services/vpn_portal_auth.py backend/app/services/vpn_portal_http.py backend/app/schemas/vpn_portal.py backend/app/api/routes/vpn_portal.py backend/tests/test_vpn_portal_auth.py backend/tests/test_vpn_portal_api.py
git commit -m "feat(vpn): expose public trial in customer portal"
```

### Task 5: Add bot activation through the shared trial service

**Files:**
- Modify: `backend/app/services/vpn_telegram.py:46-75,220-320,355-399,576-713`
- Modify: `backend/tests/test_vpn_telegram.py`

- [ ] **Step 1: Write failing bot activation and replay tests**

Add tests asserting:

```python
assert "Получить 7 дней" in reply_keyboard_texts
assert sent_text == "Veltrix VPN\nДоступ готовится. Мы сообщим, когда профиль будет готов."
assert await count_trial_subscriptions(db, telegram_user_id="123") == 1
assert await count_control_operations(db, telegram_user_id="123") == 1
```

Cover disabled flag (no trial button), capacity pause, an already-used trial,
non-private chat rejection, duplicate Telegram `update_id`, delivery failure after
commit and retry. No response or stored payload may contain UUID, URI or raw
internal exception text.

- [ ] **Step 2: Add the conditional keyboard command**

Add `"получить 7 дней": "trial"` to `COMMANDS` and append one row only while the
public flag is enabled:

```python
rows = [
    [{"text": "Моя подписка"}, {"text": "Мои профили"}],
    [{"text": "Помощь"}],
]
if settings.vpn_public_trial_enabled:
    rows.insert(1, [{"text": "Получить 7 дней"}])
return {"keyboard": rows, "resize_keyboard": True}
```

For `/start` without subscriptions, render a short explanation that the user can
activate seven days with the button. Do not activate implicitly on `/start`.

- [ ] **Step 3: Add commit-before-reply activation flow**

Before the generic-update commit, branch on a private `trial` command. Resolve the
identity through `activate_public_trial()`, set `update.customer_id`, commit the
update and trial chain together, then send a static response. Map unavailable and
used states to bounded Russian messages. On duplicate update, reuse the recorded
outcome and never call the activation service again, following the invitation
context pattern with a separate `public_trial` payload key containing only
`outcome` and `access_key_id`.

- [ ] **Step 4: Run Telegram regression tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_telegram.py -q
.venv/Scripts/python.exe -m ruff check app/services/vpn_telegram.py tests/test_vpn_telegram.py
```

Expected: all Telegram tests pass, including friend invitation crash/replay cases.

Commit:

```powershell
git add backend/app/services/vpn_telegram.py backend/tests/test_vpn_telegram.py
git commit -m "feat(vpn): activate public trial from bot"
```

### Task 6: Deliver one readiness notification after provisioning

**Files:**
- Create: `backend/app/services/vpn_ready_notifications.py`
- Modify: `backend/app/services/control_runtime.py`
- Create: `backend/tests/test_vpn_ready_notifications.py`
- Modify: `backend/tests/test_vpn_control_runtime.py`

- [ ] **Step 1: Write failing claim, success and failure tests**

Tests must assert that only an active key with `config_uri`, an active/trial
unexpired subscription, active customer and Telegram ID is eligible. Two concurrent
workers may claim it once. A successful send sets `ready_notified_at`; an ordinary
send failure clears the claim for retry and records a bounded `VpnNodeEvent`;
existing backfilled keys and a second run never send again.

- [ ] **Step 2: Implement transaction-separated claim and delivery**

Create:

```python
READY_TEXT = "Veltrix VPN\nПрофиль готов. Откройте личный кабинет, чтобы подключиться."


@dataclass(frozen=True, slots=True)
class ReadyNoticeClaim:
    access_key_id: int
    worker_id: int
    chat_id: str
    claimed_at: datetime
```

`claim_ready_notice()` selects one eligible key with
`ready_notified_at IS NULL` and `ready_notice_claimed_at IS NULL`, uses
`FOR UPDATE SKIP LOCKED`, writes `ready_notice_claimed_at=now` and returns the
claim. `deliver_next_ready_notice(session_factory, settings, sender)` commits the
claim before the network call, sends `READY_TEXT` without the connection URI, then
opens a new transaction and sets `ready_notified_at` only when the key still owns
that exact `claimed_at`. On a caught send exception, a new transaction clears the
matching claim and adds `VpnNodeEvent(event_type="telegram_ready_delivery_failed")`.
Do not introduce an outbox framework for this single notification type.

- [ ] **Step 3: Schedule delivery without blocking the strict dispatcher**

In `ControlRuntimeOrchestrator`, store at most one readiness task and start it after
the normal lifecycle scheduling branch when
`settings.vpn_ready_notifications_enabled` is true. Inject the sender in tests;
production uses `send_telegram_message`. Cancel and await the task during shutdown,
matching the lifecycle/dispatcher task pattern.

- [ ] **Step 4: Run notification/runtime tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_ready_notifications.py tests/test_vpn_control_runtime.py -q
.venv/Scripts/python.exe -m ruff check app/services/vpn_ready_notifications.py app/services/control_runtime.py tests/test_vpn_ready_notifications.py tests/test_vpn_control_runtime.py
```

Expected: all selected tests pass; the dispatcher scheduling tests remain green.

Commit:

```powershell
git add backend/app/services/vpn_ready_notifications.py backend/app/services/control_runtime.py backend/tests/test_vpn_ready_notifications.py backend/tests/test_vpn_control_runtime.py
git commit -m "feat(vpn): notify customers when profiles are ready"
```

### Task 7: Add the portal trial card and bounded preparation polling

**Files:**
- Modify: `frontend/src/vpn-portal/types.ts`
- Modify: `frontend/src/vpn-portal/api.ts`
- Create: `frontend/src/vpn-portal/TrialCard.tsx`
- Modify: `frontend/src/vpn-portal/Portal.tsx`
- Modify: `frontend/src/vpn-portal/portal.css`
- Modify: `frontend/test/vpnPortalApi.test.mjs`
- Create: `frontend/test/vpnPortalTrial.test.mjs`

- [ ] **Step 1: Write failing API and component source-contract tests**

Extend the existing Node source-contract pattern to assert exact endpoints and
safe rendering:

```javascript
assert.equal(await api.trial().then((value) => value.state), "available");
assert.equal(await api.activateTrial("csrf").then((value) => value.state), "preparing");
assert.match(trialCardSource, /Получить 7 дней/);
assert.match(trialCardSource, /Новые подключения временно приостановлены/);
assert.doesNotMatch(trialCardSource, /config_uri|external_uuid|worker_id/);
```

Use fake timers around the extracted `nextTrialPoll()` helper and assert exactly 30
two-second polls, then no additional timer and a visible manual refresh action.

- [ ] **Step 2: Add exact frontend types and API methods**

```typescript
export type PortalTrialState =
  | "disabled"
  | "available"
  | "capacity_paused"
  | "preparing"
  | "active"
  | "used";

export interface PortalTrial {
  state: PortalTrialState;
  duration_days: number;
  profile_limit: number;
  subscription_id: number | null;
  access_key_id: number | null;
  expires_at: string | null;
}
```

Add `trial()` as `GET /trial` and `activateTrial(csrf)` as `POST
/trial/activate` with `X-CSRF-Token`, using the existing `portalRequest()` helper.

- [ ] **Step 3: Implement a state-only TrialCard component**

`TrialCard` receives `trial`, `busy`, `error`, `onActivate` and `onRefresh`. It
renders:

- `available`: seven-day description and enabled activation button;
- `capacity_paused`: no activation button, capacity message and refresh;
- `preparing`: progress status and manual refresh;
- `active`: success status and link to `#profiles`;
- `used`: no promise of a second trial and link to `#plans`;
- `disabled`: nothing.

Keep network state in `Portal.tsx`; do not add a state library.

- [ ] **Step 4: Load and poll boundedly in Portal**

Load `portalApi.trial()` with subscriptions/profiles. After successful activation,
replace local trial state immediately and refresh private data. While state is
`preparing`, schedule one two-second timeout at a time and stop after 30 attempts,
logout/session-generation change, active/used state or component unmount. Each poll
reloads trial, subscriptions and profiles through the existing generation guard.

- [ ] **Step 5: Add responsive styling and run frontend tests**

Reuse `.card`, `.button`, `.message` and existing color variables. Add only
`.trial-card`, `.trial-card__meta` and `.trial-card__actions`; at `max-width: 640px`
make actions full-width. Do not introduce a UI package.

Run:

```powershell
cd frontend
npm test
npm run build
```

Expected: all Node tests pass and Vite builds both admin and cabinet entries.

Commit:

```powershell
git add frontend/src/vpn-portal/types.ts frontend/src/vpn-portal/api.ts frontend/src/vpn-portal/TrialCard.tsx frontend/src/vpn-portal/Portal.tsx frontend/src/vpn-portal/portal.css frontend/test/vpnPortalApi.test.mjs frontend/test/vpnPortalTrial.test.mjs
git commit -m "feat(vpn): add public trial portal flow"
```

### Task 8: Add capacity administration to the VPN workspace

**Files:**
- Modify: `backend/app/schemas/control.py:540-580`
- Modify: `backend/app/api/routes/control.py:3037-3095`
- Modify: `backend/tests/test_vpn_control_api.py`
- Modify: `frontend/src/api.ts`
- Create: `frontend/src/VpnEndpointCapacityPanel.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Create: `frontend/test/vpnEndpointCapacity.test.mjs`

- [ ] **Step 1: Write failing admin API tests**

Test exact list/update behavior:

```python
response = client.get("/api/control/vpn/endpoints/capacity", headers=admin_headers)
assert response.json()[0]["occupied_profiles"] == 2
updated = client.patch(
    f"/api/control/vpn/endpoints/{endpoint.id}/capacity",
    headers=admin_headers,
    json={"max_active_profiles": 50, "capacity_warning_percent": 80},
)
assert updated.json()["max_active_profiles"] == 50
```

Assert unauthenticated requests fail, 0/negative capacity and warning outside
1..100 return 422, a missing endpoint is 404, URI/public key/short ID are absent,
and the audit log stores only endpoint ID and numeric limits.

- [ ] **Step 2: Add safe admin contracts and routes**

```python
class VpnEndpointCapacityUpdateRequest(BaseModel):
    max_active_profiles: int | None = Field(default=None, ge=1, le=100_000)
    capacity_warning_percent: int = Field(default=80, ge=1, le=100)


class VpnEndpointCapacityResponse(BaseModel):
    endpoint_id: int
    worker_id: int
    label: str
    status: str
    occupied_profiles: int
    max_active_profiles: int | None
    capacity_warning_percent: int
```

List all endpoints with one grouped occupancy query. Build `label` from public host
and port but do not return REALITY material. PATCH the two fields, add
`vpn_endpoint_capacity_update` audit entry, commit and return the recalculated view.

- [ ] **Step 3: Add one focused React panel**

Add typed `listVpnEndpointCapacities()` and `updateVpnEndpointCapacity()` calls to
`api.ts`. `VpnEndpointCapacityPanel` renders endpoint label/status, occupied/max,
percentage and two native number inputs. `App.tsx` loads it with the existing VPN
workspace data and updates the returned row in local state. Use the existing error
banner and button styles; no form or table dependency.

- [ ] **Step 4: Run admin backend/frontend tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_control_api.py -q
.venv/Scripts/python.exe -m ruff check app/schemas/control.py app/api/routes/control.py tests/test_vpn_control_api.py
cd ../frontend
npm test
npm run build
```

Expected: all selected backend tests, the full frontend suite and production build
pass.

Commit:

```powershell
git add backend/app/schemas/control.py backend/app/api/routes/control.py backend/tests/test_vpn_control_api.py frontend/src/api.ts frontend/src/VpnEndpointCapacityPanel.tsx frontend/src/App.tsx frontend/src/styles.css frontend/test/vpnEndpointCapacity.test.mjs
git commit -m "feat(vpn): manage endpoint capacity"
```

### Task 9: Run the complete non-production release gate and document flags

**Files:**
- Modify: `docs/current-state.md`
- Modify: `docs/vpn-service.md`

- [ ] **Step 1: Run backend, frontend and static verification**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check app tests
cd ../frontend
npm test
npm run build
cd ..
git diff --check
```

Expected: the full backend suite passes with the required PostgreSQL tests executed,
Ruff reports no errors, all frontend tests pass, both Vite entries build, and Git
reports no whitespace errors.

- [ ] **Step 2: Perform a local browser smoke without enabling production**

Start the local backend/frontend with public trial flags enabled against a disposable
database containing one `trial-7d` plan and one capacity-configured fake endpoint.
Verify at mobile and desktop widths: login, available card, one activation,
preparing state, refresh limit, capacity-paused state and admin capacity edit. Do
not point the disposable run at the production node or Telegram webhook.

- [ ] **Step 3: Document the disabled rollout contract**

Record these exact defaults and gates:

```env
VPN_PUBLIC_TRIAL_ENABLED=false
VPN_PUBLIC_TRIAL_RELEASE_ID=
VPN_PUBLIC_TRIAL_PLAN_SLUG=trial-7d
VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300
VPN_READY_NOTIFICATIONS_ENABLED=false
```

Document that production enablement additionally requires an exact
`vpn_public_release_ready_v1` database marker, active seven-day plan, configured
capacity, fresh healthy endpoint, strict dispatcher, second externally verified
production node, reviewed migration backup and a separate deployment approval.

- [ ] **Step 4: Commit documentation**

```powershell
git add docs/current-state.md docs/vpn-service.md
git commit -m "docs(vpn): document public trial release gates"
```

## Completion state

After this plan, the code can admit a new Telegram identity, offer one trial in bot
and cabinet, allocate within configured endpoint capacity, provision through the
strict queue and notify readiness. Production remains unchanged until the separate
release-operations plan configures the second node, writes the readiness marker and
enables both flags.

The next independent plans, in dependency order, are:

1. customer release UI: tariff catalog, QR, Happ instructions, visual system,
   support/legal pages and `/vpn/` landing;
2. traffic usage and operations: node counters, 90-day aggregates, customer usage
   and administrative dashboard/search/filtering;
3. release operations: second node, backup/restore rehearsal, monitoring, alerts,
   security checks, load/failure tests and guarded production rollout;
4. payment integration: only after the non-payment release candidate is accepted.
