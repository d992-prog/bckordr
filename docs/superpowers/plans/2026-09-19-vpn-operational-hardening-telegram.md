# VPN Operational Hardening And Telegram MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a payment-free VPN service with safe 3x-UI provisioning, automatic subscription/key lifecycle, drop-catching protection, Telegram customer access, and complete admin visibility.

**Architecture:** Keep FastAPI and PostgreSQL as the single control plane. Add a focused VPN policy layer shared by admin APIs, lifecycle, and worker maintenance; extend the existing lifecycle scheduler; add an idempotent Telegram webhook service; preserve all access-key history instead of deleting rows.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, httpx, pytest/pytest-asyncio, React 18, TypeScript 5, Vite 6.

---

## File Structure

### New files

- `backend/app/services/vpn_policy.py` — subscription validation, active-attack detection, node eligibility, deterministic node selection.
- `backend/app/services/vpn_telegram.py` — Telegram update parsing, customer upsert, command rendering, outbound delivery.
- `backend/app/api/routes/vpn_telegram.py` — authenticated Telegram webhook.
- `backend/tests/test_vpn_policy.py` — policy and node-selection tests.
- `backend/tests/test_vpn_lifecycle.py` — expiration, provisioning retry, revocation retry, partial-failure tests.
- `backend/tests/test_vpn_telegram.py` — webhook authentication, commands, idempotency, and delivery failure tests.
- `docs/vpn-service.md` — deployment, configuration, smoke test, and recovery guide.

### Existing files to modify

- `backend/app/core/config.py` — lifecycle and Telegram settings.
- `backend/app/api/__init__.py` — Telegram router registration.
- `backend/app/api/routes/control.py` — subscription defaults, safe key actions, maintenance safety, lifecycle status, Telegram telemetry.
- `backend/app/db/models.py` — no destructive changes; reuse `AppSetting` and `VpnTelegramUpdate`.
- `backend/app/schemas/control.py` — node eligibility, lifecycle status, key compatibility response, Telegram telemetry schemas.
- `backend/app/services/app_settings.py` — lifecycle last-result persistence.
- `backend/app/services/control_runtime.py` — scheduled lifecycle cycle.
- `backend/app/services/vpn_lifecycle.py` — bounded retry orchestration.
- `backend/app/services/vpn_provisioning.py` — policy checks and idempotent revoke result handling.
- `backend/app/services/worker_maintenance.py` — no independent policy; API blocks unsafe mutations before jobs start.
- `backend/app/services/discovery_worker_runtime.py` — remove the existing unused import so backend Ruff is clean.
- `backend/tests/test_vpn_control_api.py` — API defaults, validation, retry, and retained-delete behavior.
- `backend/tests/test_worker_allowlist.py` — maintenance safety tests using existing API fixtures.
- `backend/tests/test_runtime_harness.py` — verify registration HTTP client is ready before a hot window.
- `worker/app/runner.py` — pre-create and reuse the registration client.
- `frontend/src/api.ts` — lifecycle, eligibility, Telegram telemetry, retained-key response types.
- `frontend/src/App.tsx` — VPN status, retry/revoke controls, lifecycle and Telegram diagnostics.
- `backend/.env.example` — new backend settings.
- `README.md` and `docs/current-state.md` — current VPN capabilities and limits.

---

### Task 1: Restore A Trustworthy Baseline And Pre-Warm Worker HTTP

**Files:**
- Modify: `worker/app/runner.py:91-506`
- Modify: `backend/tests/test_runtime_harness.py:401-706`
- Modify: `backend/app/services/discovery_worker_runtime.py:10`

- [ ] **Step 1: Strengthen the existing failing runtime regression**

In `backend/tests/test_runtime_harness.py`, keep `test_worker_runtime_preserves_long_http_error_body_samples` and add this assertion immediately after the runner is created:

```python
assert runner.registration_client is not None
```

In the same test, close the runner at the end:

```python
await runner.close()
```

Add a focused test:

```python
@pytest.mark.asyncio
async def test_worker_reuses_precreated_registration_client():
    settings = WorkerSettings(
        CONTROL_BASE_URL="http://control.test",
        WORKER_ID=1,
        CONTROL_TOKEN="worker-token",
    )
    runner = WorkerRunner(settings)
    first_client = runner.registration_client

    second_client = runner._make_registration_client()
    assert second_client is not first_client

    await second_client.aclose()
    await runner.close()
```

- [ ] **Step 2: Run the runtime tests and confirm RED**

Run:

```powershell
cd backend
python -m pytest tests/test_runtime_harness.py::test_worker_runtime_preserves_long_http_error_body_samples tests/test_runtime_harness.py::test_worker_reuses_precreated_registration_client -q -p no:cacheprovider
```

Expected: FAIL because `WorkerRunner` has no `registration_client`, and the short-window sample test may still miss all attempts.

- [ ] **Step 3: Pre-create and reuse one registration client**

In `worker/app/runner.py`, initialize a long-lived client:

```python
class WorkerRunner:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.control = ControlClient(settings)
        self.registration_client = self._make_registration_client()
        self._stop = False
        self._clock_offset_ms = 0
        self._current_rps = 0.0
        self._current_capacity_rps = 0.0
        self._simulate_random = random.Random(settings.simulate_random_seed)

    async def close(self) -> None:
        await self.registration_client.aclose()
        await self.control.close()
```

Replace the per-task context manager in `_execute_task` with:

```python
client = self.registration_client
dispatch_interval, concurrency_limit = self._runtime_limits(task.planned_rps)
```

Dedent the existing attack loop one level so it uses `client` without opening or closing it per task.

Remove `DiscoveryObservation` from the import in `backend/app/services/discovery_worker_runtime.py`:

```python
from app.db.models import DiscoveryDomain, DiscoveryWorkerTask, WorkerNode
```

- [ ] **Step 4: Verify GREEN and baseline quality**

Run:

```powershell
cd backend
python -m pytest tests/test_runtime_harness.py -q -p no:cacheprovider
python -m ruff check app tests
cd ..\worker
python -m ruff check app
```

Expected: all runtime harness tests pass; both Ruff commands report `All checks passed!`.

- [ ] **Step 5: Commit**

```powershell
git add worker/app/runner.py backend/tests/test_runtime_harness.py backend/app/services/discovery_worker_runtime.py
git commit -m "fix: prewarm worker registration client"
```

---

### Task 2: Add VPN Subscription And Node Policy

**Files:**
- Create: `backend/app/services/vpn_policy.py`
- Create: `backend/tests/test_vpn_policy.py`

- [ ] **Step 1: Write policy tests**

Create `backend/tests/test_vpn_policy.py` with an in-memory SQLite session fixture and these behaviors:

```python
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AttackRun, DropDomain, VpnAccessKey, VpnCustomer, VpnSubscription, WorkerNode, WorkerTask
from app.services.vpn_policy import (
    count_device_slots,
    evaluate_vpn_node,
    select_vpn_node,
    validate_subscription_access,
)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def test_validate_subscription_access_rejects_expired_subscription():
    now = datetime(2026, 9, 19, tzinfo=UTC)
    customer = VpnCustomer(status="active")
    subscription = VpnSubscription(
        status="active",
        starts_at=now - timedelta(days=2),
        expires_at=now - timedelta(seconds=1),
        max_devices=1,
    )

    assert validate_subscription_access(subscription, customer, device_slots=0, now=now) == "VPN subscription has expired"


def test_validate_subscription_access_enforces_device_limit():
    now = datetime(2026, 9, 19, tzinfo=UTC)
    customer = VpnCustomer(status="active")
    subscription = VpnSubscription(status="active", max_devices=1)

    assert validate_subscription_access(subscription, customer, device_slots=1, now=now) == "VPN device limit reached"


@pytest.mark.asyncio
async def test_select_vpn_node_excludes_worker_with_active_attack(session_factory):
    async with session_factory() as session:
        busy = WorkerNode(
            name="busy",
            status="ready",
            is_enabled=True,
            vpn_enabled=True,
            vpn_role="drop_worker+vpn_node",
            vpn_runtime_status="ready",
            vpn_public_host="busy.example",
            vpn_inbound_id=1,
            ssh_host="10.0.0.1",
            ssh_password="secret",
        )
        free = WorkerNode(
            name="free",
            status="ready",
            is_enabled=True,
            vpn_enabled=True,
            vpn_role="vpn_node",
            vpn_runtime_status="ready",
            vpn_public_host="free.example",
            vpn_inbound_id=1,
            ssh_host="10.0.0.2",
            ssh_password="secret",
        )
        session.add_all([busy, free])
        await session.flush()
        domain = DropDomain(fqdn="target.fr", zone="fr", drop_date=datetime.now(UTC).date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=datetime.now(UTC),
            planned_end_at=datetime.now(UTC) + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(WorkerTask(
            attack_run_id=run.id,
            domain_id=domain.id,
            worker_id=busy.id,
            status="running",
        ))
        await session.commit()

        selected = await select_vpn_node(session)

        assert selected is not None
        assert selected.id == free.id
        assert (await evaluate_vpn_node(session, busy)).eligible is False
```

- [ ] **Step 2: Run policy tests and confirm RED**

Run:

```powershell
cd backend
python -m pytest tests/test_vpn_policy.py -q -p no:cacheprovider
```

Expected: collection FAIL because `app.services.vpn_policy` does not exist.

- [ ] **Step 3: Implement the policy service**

Create `backend/app/services/vpn_policy.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import AttackRun, VpnAccessKey, VpnCustomer, VpnSubscription, WorkerNode, WorkerTask

DEVICE_SLOT_STATUSES = ("pending_sync", "syncing", "active", "pending_revoke")
VPN_MUTATION_ACTIONS = {
    "vpn_install",
    "vpn_update",
    "vpn_restart",
    "vpn_autoconfig",
    "vpn_create_inbound",
}


@dataclass(frozen=True, slots=True)
class VpnNodeEligibility:
    eligible: bool
    reasons: tuple[str, ...]


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def validate_subscription_access(
    subscription: VpnSubscription,
    customer: VpnCustomer | None,
    *,
    device_slots: int,
    now: datetime | None = None,
) -> str | None:
    current_time = _as_utc(now or utcnow())
    starts_at = _as_utc(subscription.starts_at)
    expires_at = _as_utc(subscription.expires_at)
    assert current_time is not None
    if customer is None or customer.status != "active":
        return "VPN customer is not active"
    if subscription.status not in {"active", "trial"}:
        return f"VPN subscription is {subscription.status}"
    if starts_at is not None and starts_at > current_time:
        return "VPN subscription has not started"
    if expires_at is not None and expires_at <= current_time:
        return "VPN subscription has expired"
    if device_slots >= subscription.max_devices:
        return "VPN device limit reached"
    return None


async def count_device_slots(session: AsyncSession, subscription_id: int, *, exclude_key_id: int | None = None) -> int:
    query = select(func.count(VpnAccessKey.id)).where(
        VpnAccessKey.subscription_id == subscription_id,
        VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
    )
    if exclude_key_id is not None:
        query = query.where(VpnAccessKey.id != exclude_key_id)
    return int(await session.scalar(query) or 0)


async def active_attack_worker_ids(session: AsyncSession) -> set[int]:
    result = await session.execute(
        select(WorkerTask.worker_id)
        .join(AttackRun, AttackRun.id == WorkerTask.attack_run_id)
        .where(
            WorkerTask.status.in_(("queued", "running")),
            AttackRun.status.in_(("planned", "running")),
        )
        .distinct()
    )
    return {int(worker_id) for worker_id in result.scalars().all()}


async def evaluate_vpn_node(session: AsyncSession, worker: WorkerNode) -> VpnNodeEligibility:
    reasons: list[str] = []
    if not worker.is_enabled or worker.status in {"offline", "disabled"}:
        reasons.append("Worker is not online and enabled")
    if not worker.vpn_enabled or worker.vpn_role == "none":
        reasons.append("VPN role is disabled")
    if worker.vpn_runtime_status != "ready":
        reasons.append("VPN runtime is not ready")
    if not worker.ssh_access_configured:
        reasons.append("Worker SSH access is not configured")
    if not worker.vpn_inbound_id:
        reasons.append("VPN inbound ID is not configured")
    if not worker.vpn_public_host:
        reasons.append("VPN public host is not configured")
    if worker.id in await active_attack_worker_ids(session):
        reasons.append("Worker is assigned to an active domain attack")
    return VpnNodeEligibility(eligible=not reasons, reasons=tuple(reasons))


async def select_vpn_node(session: AsyncSession, *, worker_id: int | None = None) -> WorkerNode | None:
    query = select(WorkerNode)
    if worker_id is not None:
        query = query.where(WorkerNode.id == worker_id)
    workers = (await session.execute(query.order_by(WorkerNode.id.asc()))).scalars().all()
    eligible: list[tuple[int, datetime | None, int, WorkerNode]] = []
    for worker in workers:
        result = await evaluate_vpn_node(session, worker)
        if not result.eligible:
            continue
        active_keys = int(await session.scalar(
            select(func.count(VpnAccessKey.id)).where(
                VpnAccessKey.worker_id == worker.id,
                VpnAccessKey.status == "active",
            )
        ) or 0)
        eligible.append((active_keys, worker.vpn_last_checked_at, worker.id, worker))
    eligible.sort(key=lambda item: (
        item[0],
        item[1] is not None,
        _as_utc(item[1]) or datetime.min.replace(tzinfo=timezone.utc),
        item[2],
    ))
    return eligible[0][3] if eligible else None
```

- [ ] **Step 4: Run policy tests and verify GREEN**

```powershell
cd backend
python -m pytest tests/test_vpn_policy.py -q -p no:cacheprovider
```

Expected: all policy tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/vpn_policy.py backend/tests/test_vpn_policy.py
git commit -m "feat: add vpn safety policy"
```

---

### Task 3: Enforce Subscription Defaults And Safe Key Issuance

**Files:**
- Modify: `backend/app/api/routes/control.py:2923-3120`
- Modify: `backend/app/schemas/control.py:603-674`
- Modify: `backend/tests/test_vpn_control_api.py`

- [ ] **Step 1: Add failing API tests**

Add tests that create a 30-day plan and assert omitted subscription values inherit from it:

```python
subscription_response = await client.post(
    "/control/vpn/subscriptions",
    json={"customer_id": customer_id, "plan_id": plan_id},
)
assert subscription_response.status_code == 201
subscription = subscription_response.json()
assert subscription["starts_at"] is not None
assert subscription["expires_at"] is not None
assert subscription["traffic_limit_gb"] == 100
assert subscription["max_devices"] == 2
```

Add tests asserting:

```python
assert expired_key_response.status_code == 409
assert second_device_response.status_code == 409
assert unsafe_explicit_worker_response.status_code == 409
```

Add an automatic-selection test with one busy and one free node:

```python
key_response = await client.post(
    "/control/vpn/access-keys",
    json={"subscription_id": subscription_id, "public_name": "phone"},
)
assert key_response.status_code == 201
assert key_response.json()["worker_id"] == free_worker_id
```

Replace the legacy deletion expectation with retained history:

```python
delete_response = await client.delete(f"/control/vpn/access-keys/{access_key_id}")
assert delete_response.status_code == 200
assert delete_response.json()["id"] == access_key_id
assert delete_response.json()["status"] in {"revoked", "pending_revoke"}

keys_after_delete = (await client.get("/control/vpn/access-keys")).json()
assert any(item["id"] == access_key_id for item in keys_after_delete)
```

- [ ] **Step 2: Run targeted API tests and confirm RED**

```powershell
cd backend
python -m pytest tests/test_vpn_control_api.py -q -p no:cacheprovider
```

Expected: new assertions fail because plans are not inherited, device limits are not enforced, and nodes are not selected automatically.

- [ ] **Step 3: Apply plan defaults on subscription creation**

In `create_vpn_subscription`, build values from fields explicitly supplied by the request:

```python
plan = await db.get(VpnPlan, payload.plan_id) if payload.plan_id is not None else None
if payload.plan_id is not None and plan is None:
    raise HTTPException(status_code=404, detail="VPN plan not found")

values = payload.model_dump(exclude_unset=True)
starts_at = values.get("starts_at") or utcnow()
values["starts_at"] = starts_at
if plan is not None:
    if "expires_at" not in payload.model_fields_set and plan.duration_days is not None:
        values["expires_at"] = starts_at + timedelta(days=plan.duration_days)
    if "traffic_limit_gb" not in payload.model_fields_set:
        values["traffic_limit_gb"] = plan.traffic_limit_gb
    if "max_devices" not in payload.model_fields_set:
        values["max_devices"] = plan.max_devices
subscription = VpnSubscription(**values)
```

Use these exact imports in `control.py`:

```python
from datetime import timedelta

from app.services.vpn_policy import (
    count_device_slots,
    evaluate_vpn_node,
    select_vpn_node,
    validate_subscription_access,
)
```

- [ ] **Step 4: Enforce validity, limits, and node policy for key actions**

Add a private route helper:

```python
async def _validate_vpn_key_issue(
    db: AsyncSession,
    subscription: VpnSubscription,
    *,
    exclude_key_id: int | None = None,
) -> None:
    customer = await db.get(VpnCustomer, subscription.customer_id)
    slots = await count_device_slots(db, subscription.id, exclude_key_id=exclude_key_id)
    error = validate_subscription_access(subscription, customer, device_slots=slots)
    if error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=error)
```

Use it from key creation and the `/provision` retry endpoint. On retry, pass `exclude_key_id=access_key.id` because the key already occupies its device slot. Reject provisioning retries unless the key is `pending_sync` or `failed`; revocation has its own safe-node path and must not require a valid subscription.

For a create request, resolve workers with:

```python
worker = await select_vpn_node(db, worker_id=payload.worker_id)
if payload.worker_id is not None and worker is None:
    requested_worker = await db.get(WorkerNode, payload.worker_id)
    if requested_worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    eligibility = await evaluate_vpn_node(db, requested_worker)
    raise HTTPException(status_code=409, detail="; ".join(eligibility.reasons))
```

When no automatic node is available, create `pending_sync` with:

```python
last_error="No safe VPN node is currently available"
```

For `/provision`, load the subscription, validate it, and resolve with the assigned worker when present or automatic selection otherwise:

```python
await _validate_vpn_key_issue(db, subscription, exclude_key_id=access_key.id)
worker = await select_vpn_node(db, worker_id=access_key.worker_id)
if access_key.worker_id is not None and worker is None:
    assigned_worker = await db.get(WorkerNode, access_key.worker_id)
    eligibility = await evaluate_vpn_node(db, assigned_worker) if assigned_worker else None
    detail = "; ".join(eligibility.reasons) if eligibility else "Assigned VPN node was not found"
    raise HTTPException(status_code=409, detail=detail)
if worker is None:
    access_key.status = "pending_sync"
    access_key.last_error = "No safe VPN node is currently available"
else:
    access_key.worker_id = worker.id
    await provision_vpn_access_key(db, access_key, subscription=subscription, worker=worker)
```

Before manual `/revoke`, evaluate the assigned worker. Return `409` during an active domain attack without invoking SSH; leave the key in `pending_revoke` so lifecycle can retry it.

Change the legacy delete route to `response_model=VpnAccessKeyResponse`. It calls `revoke_vpn_access_key`, commits, refreshes, and returns the retained key without `db.delete(access_key)`.

- [ ] **Step 5: Verify API GREEN**

```powershell
cd backend
python -m pytest tests/test_vpn_policy.py tests/test_vpn_control_api.py tests/test_vpn_provisioning.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/api/routes/control.py backend/app/schemas/control.py backend/tests/test_vpn_control_api.py
git commit -m "feat: enforce vpn subscription access"
```

---

### Task 4: Protect VPN Maintenance During Domain Attacks

**Files:**
- Modify: `backend/app/api/routes/control.py:2264-2681`
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/tests/test_worker_allowlist.py`
- Modify: `backend/tests/test_vpn_control_api.py`
- Test: `backend/tests/test_vpn_policy.py`

- [ ] **Step 1: Add failing maintenance tests**

Using the existing maintenance API test fixture, seed a running `AttackRun` and `WorkerTask` for a VPN worker. Assert:

```python
blocked = await client.post(f"/control/workers/{worker_id}/maintenance/vpn-restart")
assert blocked.status_code == 409
assert "active domain attack" in blocked.json()["detail"]

health_check = await client.post(f"/control/workers/{worker_id}/maintenance/vpn-check")
assert health_check.status_code == 202
```

Add a bulk test:

```python
response = await client.post("/control/workers/maintenance/vpn-update-all")
payload = response.json()
assert busy_worker_id in payload["skipped_worker_ids"]
assert free_worker_id not in payload["skipped_worker_ids"]
```

Add a read-only eligibility API assertion:

```python
response = await client.get("/control/vpn/nodes/eligibility")
assert response.status_code == 200
by_worker = {item["worker_id"]: item for item in response.json()}
assert by_worker[busy_worker_id] == {
    "worker_id": busy_worker_id,
    "eligible": False,
    "blocked_reasons": ["Worker is assigned to an active domain attack"],
}
assert by_worker[free_worker_id]["eligible"] is True
```

- [ ] **Step 2: Run tests and confirm RED**

```powershell
cd backend
python -m pytest tests/test_worker_allowlist.py -q -p no:cacheprovider
```

Expected: unsafe VPN mutation starts instead of returning `409`, or busy worker appears in bulk jobs.

- [ ] **Step 3: Reuse the VPN policy in maintenance endpoints**

In `_start_worker_maintenance_job`, before creating the job:

```python
if action in VPN_MUTATION_ACTIONS:
    eligibility = await evaluate_vpn_node(db, worker)
    active_attack_reason = next(
        (reason for reason in eligibility.reasons if "active domain attack" in reason),
        None,
    )
    if active_attack_reason:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=active_attack_reason)
```

Do not apply this gate to `vpn_check`.

For both bulk VPN loops, load `busy_worker_ids = await active_attack_worker_ids(db)` and include `worker.id in busy_worker_ids` in the skip condition.

Add this schema:

```python
class VpnNodeEligibilityResponse(BaseModel):
    worker_id: int
    eligible: bool
    blocked_reasons: list[str]
```

Expose the policy result without changing the existing worker response contract:

```python
@router.get("/vpn/nodes/eligibility", response_model=list[VpnNodeEligibilityResponse])
async def list_vpn_node_eligibility(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[VpnNodeEligibilityResponse]:
    del admin
    workers = (await db.execute(select(WorkerNode).order_by(WorkerNode.id.asc()))).scalars().all()
    response: list[VpnNodeEligibilityResponse] = []
    for worker in workers:
        eligibility = await evaluate_vpn_node(db, worker)
        response.append(VpnNodeEligibilityResponse(
            worker_id=worker.id,
            eligible=eligibility.eligible,
            blocked_reasons=list(eligibility.reasons),
        ))
    return response
```

- [ ] **Step 4: Verify GREEN**

```powershell
cd backend
python -m pytest tests/test_worker_allowlist.py tests/test_vpn_policy.py tests/test_vpn_control_api.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/api/routes/control.py backend/app/schemas/control.py backend/tests/test_worker_allowlist.py backend/tests/test_vpn_control_api.py
git commit -m "feat: protect attack workers from vpn maintenance"
```

---

### Task 5: Expand And Schedule VPN Lifecycle Maintenance

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/services/app_settings.py`
- Modify: `backend/app/services/vpn_lifecycle.py`
- Modify: `backend/app/services/control_runtime.py`
- Modify: `backend/app/api/routes/control.py`
- Modify: `backend/app/schemas/control.py`
- Create: `backend/tests/test_vpn_lifecycle.py`
- Modify: `backend/tests/test_vpn_control_api.py`

- [ ] **Step 1: Write failing lifecycle service tests**

Copy the exact `session_factory` fixture from Task 2 into `test_vpn_lifecycle.py`. Use a fixed timezone-aware `now` and concrete database rows for these four cases:

| Test | Seeded state | Service stub | Required assertions |
|---|---|---|---|
| `test_lifecycle_retries_pending_sync_key_on_safe_node` | Active customer and subscription, one `pending_sync` key without a worker, one fully configured eligible VPN worker | Monkeypatched `provision_vpn_access_key` assigns the worker and changes the key to `active` | `provisioned_keys == 1`, `checked_keys == 1`, persisted key is `active` |
| `test_lifecycle_keeps_revoke_pending_on_busy_node` | Expired subscription, one `pending_revoke` key on a fully configured worker, plus a running `AttackRun`/`WorkerTask` for that worker | Monkeypatched revoke raises immediately if invoked | `skipped_unsafe_keys == 1`, key remains `pending_revoke`, revoke stub is never entered |
| `test_lifecycle_continues_after_one_key_failure` | Expired subscription with two active keys on safe configured workers | Monkeypatched revoke raises for key `failure` and changes key `success` to `revoked` | `failed_keys == 1`, `revoked_keys == 1`, successful key is persisted as `revoked`, failed key has bounded `last_error` |
| `test_lifecycle_persists_last_result` | Active customer/subscription without keys | No network stub required | `json.loads(await get_app_setting(...))["ran_at"] == now.isoformat()` and all counters are present |
| `test_lifecycle_calls_are_serialized` | Two concurrent service calls against separate sessions and one retryable key | Provisioning stub pauses on an `asyncio.Event` while a second call is started | The second call does not enter its transition loop until the first call releases the event |

The provisioning and revoke stubs must mutate the passed key to the production terminal state; do not rely only on mock call counts.

- [ ] **Step 2: Run lifecycle tests and confirm RED**

```powershell
cd backend
python -m pytest tests/test_vpn_lifecycle.py -q -p no:cacheprovider
```

Expected: FAIL because batch retry counters, safe-node retry, and persisted status do not exist.

- [ ] **Step 3: Add lifecycle configuration and persisted status**

Add to `Settings`:

```python
vpn_lifecycle_enabled: bool = Field(default=True, alias="VPN_LIFECYCLE_ENABLED")
vpn_lifecycle_interval_seconds: float = Field(default=60.0, alias="VPN_LIFECYCLE_INTERVAL_SECONDS")
vpn_lifecycle_batch_size: int = Field(default=50, alias="VPN_LIFECYCLE_BATCH_SIZE")
vpn_telegram_bot_token: str = Field(default="", alias="VPN_TELEGRAM_BOT_TOKEN")
vpn_telegram_webhook_secret: str = Field(default="", alias="VPN_TELEGRAM_WEBHOOK_SECRET")
vpn_telegram_secret_token: str = Field(default="", alias="VPN_TELEGRAM_SECRET_TOKEN")
vpn_support_text: str = Field(
    default="Напишите администратору для подключения или продления VPN.",
    alias="VPN_SUPPORT_TEXT",
)
```

Add to `app_settings.py`:

```python
VPN_LIFECYCLE_LAST_RESULT_KEY = "vpn_lifecycle_last_result"


async def get_vpn_lifecycle_last_result(session: AsyncSession) -> dict:
    raw = await get_app_setting(session, VPN_LIFECYCLE_LAST_RESULT_KEY)
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}
```

Import `json`.

- [ ] **Step 4: Implement bounded provisioning and revocation retries**

Add a process-wide lock around the service itself so scheduled and manual invocations share the same guard:

```python
import asyncio

_vpn_lifecycle_lock = asyncio.Lock()


async def run_vpn_lifecycle_maintenance(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    batch_size: int = 50,
) -> dict[str, int | str]:
    async with _vpn_lifecycle_lock:
        return await _run_vpn_lifecycle_maintenance(
            db,
            now=now or utcnow(),
            batch_size=max(1, batch_size),
        )
```

The private `_run_vpn_lifecycle_maintenance` performs the work and returns exactly:

```python
result = {
    "ran_at": current_time.isoformat(),
    "expired_subscriptions": 0,
    "checked_keys": 0,
    "provisioned_keys": 0,
    "revoked_keys": 0,
    "pending_sync_keys": 0,
    "pending_revoke_keys": 0,
    "skipped_unsafe_keys": 0,
    "failed_keys": 0,
}
```

Process at most `batch_size` keys total, in stable key-ID order, one key at a time. For `pending_sync`, validate the subscription and select a safe node; for terminal subscriptions or elapsed key expiry, use safe-node eligibility then revoke. A never-provisioned key with no assigned worker is marked `revoked` locally because no remote client can exist. A skipped assigned worker creates a warning `VpnNodeEvent` with type `lifecycle_unsafe_skip`. Wrap each key transition in `try/except`, store a sanitized bounded error, increment `failed_keys`, and continue. Sequential processing plus the process lock prevents concurrent lifecycle SSH mutations against one node.

Use this helper before persisting an exception so credentials cannot enter `last_error` or events:

```python
def _bounded_key_error(exc: Exception, worker: WorkerNode | None) -> str:
    message = str(exc)
    if worker is not None:
        for secret in (worker.ssh_password, worker.vpn_panel_password):
            if secret:
                message = message.replace(secret, "<redacted>")
    return message[:2000]
```

Counter semantics are fixed: `checked_keys` counts every key taken into the bounded batch; `provisioned_keys` and `revoked_keys` count confirmed terminal transitions; `pending_sync_keys` and `pending_revoke_keys` count retryable outcomes after this cycle; `skipped_unsafe_keys` is also incremented when an assigned node is blocked by policy; `failed_keys` counts unexpected per-key exceptions. A key can therefore contribute to one pending counter and to `skipped_unsafe_keys`, but never to both a confirmed-transition counter and a pending counter.

Persist the final result:

```python
await set_app_setting(
    db,
    VPN_LIFECYCLE_LAST_RESULT_KEY,
    json.dumps(result, ensure_ascii=False, separators=(",", ":")),
)
```

- [ ] **Step 5: Schedule lifecycle from the control runtime**

In `ControlRuntimeOrchestrator.__init__`:

```python
self._vpn_lifecycle_enabled = settings.vpn_lifecycle_enabled if settings else True
self._vpn_lifecycle_interval_seconds = max(settings.vpn_lifecycle_interval_seconds, 1.0) if settings else 60.0
self._vpn_lifecycle_batch_size = max(settings.vpn_lifecycle_batch_size, 1) if settings else 50
self._last_vpn_lifecycle_at = None
```

In `run_cycle`, before commit:

```python
if (
    self._vpn_lifecycle_enabled
    and (
        self._last_vpn_lifecycle_at is None
        or (now - self._last_vpn_lifecycle_at).total_seconds() >= self._vpn_lifecycle_interval_seconds
    )
):
    await run_vpn_lifecycle_maintenance(
        session,
        now=now,
        batch_size=self._vpn_lifecycle_batch_size,
    )
    self._last_vpn_lifecycle_at = now
```

- [ ] **Step 6: Add lifecycle status API**

Add a response with safe defaults so the status endpoint works before the first cycle:

```python
class VpnLifecycleStatusResponse(BaseModel):
    ran_at: datetime | None = None
    expired_subscriptions: int = 0
    checked_keys: int = 0
    provisioned_keys: int = 0
    revoked_keys: int = 0
    pending_sync_keys: int = 0
    pending_revoke_keys: int = 0
    skipped_unsafe_keys: int = 0
    failed_keys: int = 0
```

Add the route:

```python
@router.get("/vpn/lifecycle/status", response_model=VpnLifecycleStatusResponse)
async def get_vpn_lifecycle_status(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VpnLifecycleStatusResponse:
    del admin
    return VpnLifecycleStatusResponse.model_validate(await get_vpn_lifecycle_last_result(db))
```

Update the manual endpoint to pass configured batch size and return the expanded result.

- [ ] **Step 7: Verify lifecycle GREEN**

```powershell
cd backend
python -m pytest tests/test_vpn_lifecycle.py tests/test_vpn_control_api.py tests/test_auto_scheduler.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```powershell
git add backend/app/core/config.py backend/app/services/app_settings.py backend/app/services/vpn_lifecycle.py backend/app/services/control_runtime.py backend/app/api/routes/control.py backend/app/schemas/control.py backend/tests/test_vpn_lifecycle.py backend/tests/test_vpn_control_api.py
git commit -m "feat: automate vpn lifecycle maintenance"
```

---

### Task 6: Add Idempotent Telegram Customer Webhook

**Files:**
- Create: `backend/app/services/vpn_telegram.py`
- Create: `backend/app/api/routes/vpn_telegram.py`
- Modify: `backend/app/api/__init__.py`
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Create: `backend/tests/test_vpn_telegram.py`

- [ ] **Step 1: Write Telegram webhook tests**

Build a FastAPI test app with `vpn_telegram.router`, database override, and monkeypatched delivery function. Cover:

```python
@pytest.mark.asyncio
async def test_webhook_rejects_wrong_path_secret(client):
    response = await client.post("/vpn-telegram/webhook/wrong", json={"update_id": 1})
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_start_creates_customer_once_and_duplicate_update_is_idempotent(client, session_factory):
    payload = telegram_message(10, 12345, "/start")
    first = await client.post("/vpn-telegram/webhook/correct", json=payload)
    second = await client.post("/vpn-telegram/webhook/correct", json=payload)
    assert first.status_code == 200
    assert second.status_code == 200
    async with session_factory() as session:
        assert await session.scalar(select(func.count(VpnCustomer.id))) == 1
        assert await session.scalar(select(func.count(VpnTelegramUpdate.id))) == 1


@pytest.mark.asyncio
async def test_keys_returns_only_active_keys_for_valid_subscription(client, seeded_customer):
    response = await client.post(
        "/vpn-telegram/webhook/correct",
        headers={"X-Telegram-Bot-Api-Secret-Token": "header-secret"},
        json=telegram_message(11, seeded_customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    assert "vless://active" in delivered_messages[-1]["text"]
    assert "vless://revoked" not in delivered_messages[-1]["text"]
```

Add these named cases with exact outcomes:

- `test_start_returns_current_subscription_status`: delivered text contains `VPN-бот готов` and the seeded subscription ID.
- `test_status_without_subscription_points_to_support`: delivered text contains `Активной VPN-подписки нет`.
- `test_disabled_customer_cannot_receive_keys`: even with an active subscription and key, delivered text contains `VPN-профиль отключён` and does not contain the config URI.
- `test_support_returns_configured_text`: delivered text equals the fixture's `VPN_SUPPORT_TEXT`.
- `test_webhook_rejects_wrong_header_secret`: response is `403` and both `VpnTelegramUpdate` and `VpnCustomer` counts stay zero.
- `test_webhook_without_bot_configuration_returns_503`: response is `503` before reading the request payload.
- `test_authenticated_malformed_update_is_acknowledged_and_recorded`: response is `200`, the update row exists, `processed_at` is null, and `error_message` contains `message identity`.
- `test_delivery_failure_is_persisted_without_rollback`: make the sender error contain the bot token; response is `200`, customer changes remain committed, the bounded `error_message` contains `<redacted>` but not the token, and a seeded assigned key produces a `telegram_delivery_failed` node event.

- [ ] **Step 2: Run Telegram tests and confirm RED**

```powershell
cd backend
python -m pytest tests/test_vpn_telegram.py -q -p no:cacheprovider
```

Expected: collection FAIL because the Telegram service and router do not exist.

- [ ] **Step 3: Implement Telegram service**

Create `vpn_telegram.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnNodeEvent, VpnSubscription, VpnTelegramUpdate

TelegramSender = Callable[[Settings, str, str], Awaitable[None]]


def sanitize_telegram_error(exc: Exception, settings: Settings) -> str:
    message = str(exc)
    if settings.vpn_telegram_bot_token:
        message = message.replace(settings.vpn_telegram_bot_token, "<redacted>")
    return message[:2000]


@dataclass(frozen=True, slots=True)
class TelegramMessage:
    update_id: str
    chat_id: str
    user_id: str
    username: str | None
    first_name: str | None
    last_name: str | None
    text: str


def parse_telegram_message(payload: dict) -> TelegramMessage:
    message = payload.get("message") or {}
    sender = message.get("from") or {}
    chat = message.get("chat") or {}
    if payload.get("update_id") is None or sender.get("id") is None or chat.get("id") is None:
        raise ValueError("Telegram update does not contain a message identity")
    return TelegramMessage(
        update_id=str(payload["update_id"]),
        chat_id=str(chat["id"]),
        user_id=str(sender["id"]),
        username=sender.get("username"),
        first_name=sender.get("first_name"),
        last_name=sender.get("last_name"),
        text=str(message.get("text") or "").strip(),
    )


async def send_telegram_message(settings: Settings, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{settings.vpn_telegram_bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {
            "keyboard": [[{"text": "Статус"}, {"text": "Ключи"}], [{"text": "Поддержка"}]],
            "resize_keyboard": True,
        },
    }
    async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
```

Use this exact command mapping and renderer:

```python
COMMANDS = {
    "/start": "start",
    "/status": "status",
    "статус": "status",
    "/keys": "keys",
    "ключи": "keys",
    "/support": "support",
    "поддержка": "support",
}


async def render_customer_response(
    session: AsyncSession,
    customer: VpnCustomer,
    command: str,
    settings: Settings,
) -> str:
    if command == "support":
        return settings.vpn_support_text
    if customer.status != "active":
        return "VPN-профиль отключён. Выберите «Поддержка» для связи с администратором."

    now = utcnow()
    valid_filter = (
        VpnSubscription.customer_id == customer.id,
        VpnSubscription.status.in_(("active", "trial")),
        or_(VpnSubscription.starts_at.is_(None), VpnSubscription.starts_at <= now),
        or_(VpnSubscription.expires_at.is_(None), VpnSubscription.expires_at > now),
    )
    subscriptions = (
        await session.execute(
            select(VpnSubscription)
            .where(*valid_filter)
            .order_by(VpnSubscription.expires_at.asc(), VpnSubscription.id.asc())
        )
    ).scalars().all()

    if command in {"start", "status"}:
        intro = "VPN-бот готов. Оплата пока не подключена.\n" if command == "start" else ""
        if not subscriptions:
            return intro + "Активной VPN-подписки нет. Выберите «Поддержка» для связи с администратором."
        lines = [intro + "Ваши активные VPN-подписки:"]
        for subscription in subscriptions:
            expires = subscription.expires_at.isoformat() if subscription.expires_at else "без срока"
            lines.append(f"#{subscription.id}: {subscription.status}, до {expires}")
        return "\n".join(lines)
    if command == "keys":
        keys = (
            await session.execute(
                select(VpnAccessKey)
                .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
                .where(
                    *valid_filter,
                    VpnAccessKey.status == "active",
                    VpnAccessKey.config_uri.is_not(None),
                )
                .order_by(VpnAccessKey.id.asc())
            )
        ).scalars().all()
        if not keys:
            return "Активных VPN-ключей нет."
        return "Ваши VPN-ключи:\n" + "\n".join(
            f"{key.public_name or f'Ключ #{key.id}'}\n{key.config_uri}" for key in keys
        )
    return "Доступны команды: /status, /keys, /support."
```

Implement the idempotent transaction boundary and delivery audit:

```python
async def process_telegram_update(
    session: AsyncSession,
    payload: dict,
    settings: Settings,
    sender: TelegramSender = send_telegram_message,
) -> dict[str, bool]:
    raw_update_id = payload.get("update_id")
    if raw_update_id is None:
        return {"processed": False, "duplicate": False}
    update_id = str(raw_update_id)
    existing = await session.scalar(
        select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == update_id)
    )
    if existing is not None:
        return {"processed": existing.processed_at is not None, "duplicate": True}

    update = VpnTelegramUpdate(update_id=update_id, payload=payload)
    session.add(update)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        return {"processed": False, "duplicate": True}

    try:
        message = parse_telegram_message(payload)
    except ValueError as exc:
        update.error_message = sanitize_telegram_error(exc, settings)
        return {"processed": False, "duplicate": False}

    customer = await session.scalar(
        select(VpnCustomer).where(VpnCustomer.telegram_user_id == message.user_id)
    )
    if customer is None:
        customer = VpnCustomer(telegram_user_id=message.user_id, status="active")
        session.add(customer)
    customer.telegram_username = message.username
    customer.first_name = message.first_name
    customer.last_name = message.last_name
    await session.flush()
    update.customer_id = customer.id

    command = COMMANDS.get(message.text.casefold(), "start")
    response_text = await render_customer_response(session, customer, command, settings)
    try:
        await sender(settings, message.chat_id, response_text)
    except Exception as exc:  # delivery is recorded; Telegram still receives HTTP 200
        update.error_message = str(exc)[:2000]
        key = await session.scalar(
            select(VpnAccessKey)
            .join(VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id)
            .where(
                VpnSubscription.customer_id == customer.id,
                VpnAccessKey.worker_id.is_not(None),
            )
            .order_by(VpnAccessKey.id.desc())
            .limit(1)
        )
        if key is not None and key.worker_id is not None:
            session.add(VpnNodeEvent(
                worker_id=key.worker_id,
                level="error",
                event_type="telegram_delivery_failed",
                message="Telegram VPN response delivery failed",
                details={"update_id": message.update_id, "error": update.error_message},
            ))
        return {"processed": False, "duplicate": False}

    update.processed_at = utcnow()
    update.error_message = None
    return {"processed": True, "duplicate": False}
```

- [ ] **Step 4: Implement authenticated webhook and admin telemetry**

Create `vpn_telegram.py` router:

```python
router = APIRouter(prefix="/vpn-telegram", tags=["vpn-telegram"])


@router.post("/webhook/{secret}")
async def vpn_telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    settings = get_settings()
    if not settings.vpn_telegram_bot_token or not settings.vpn_telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="VPN Telegram bot is not configured")
    if not secrets.compare_digest(secret, settings.vpn_telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if settings.vpn_telegram_secret_token and not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "",
        settings.vpn_telegram_secret_token,
    ):
        raise HTTPException(status_code=403, detail="Invalid Telegram secret token")
    try:
        payload = await request.json()
    except ValueError:
        return {"ok": True, "processed": False, "duplicate": False}
    result = await process_telegram_update(db, payload, settings)
    await db.commit()
    return {"ok": True, **result}
```

Register it in `backend/app/api/__init__.py`.

Add the exact admin telemetry contract:

```python
class VpnTelegramUpdateResponse(BaseModel):
    id: int
    update_id: str
    customer_id: int | None
    processed_at: datetime | None
    error_message: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
```

Add the route:

```python
@router.get("/vpn/telegram-updates", response_model=list[VpnTelegramUpdateResponse])
async def list_vpn_telegram_updates(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[VpnTelegramUpdate]:
    del admin
    return list((await db.execute(
        select(VpnTelegramUpdate)
        .order_by(VpnTelegramUpdate.created_at.desc(), VpnTelegramUpdate.id.desc())
        .limit(100)
    )).scalars().all())
```

- [ ] **Step 5: Verify Telegram GREEN**

```powershell
cd backend
python -m pytest tests/test_vpn_telegram.py tests/test_vpn_control_api.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/vpn_telegram.py backend/app/api/routes/vpn_telegram.py backend/app/api/__init__.py backend/app/schemas/control.py backend/app/api/routes/control.py backend/tests/test_vpn_telegram.py
git commit -m "feat: add vpn telegram customer bot"
```

---

### Task 7: Add VPN Lifecycle And Telegram Visibility To The UI

**Files:**
- Modify: `frontend/src/api.ts:366-538,715-1000`
- Modify: `frontend/src/App.tsx:770-835,1101-1160,2300-2440,4460-4820`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add exact frontend API contracts**

Add a separate policy response rather than changing the persisted worker contract:

```typescript
export type VpnNodeEligibility = {
  worker_id: number;
  eligible: boolean;
  blocked_reasons: string[];
};
```

Replace the lifecycle result with:

```typescript
export type VpnLifecycleStatus = {
  ran_at: string | null;
  expired_subscriptions: number;
  checked_keys: number;
  provisioned_keys: number;
  revoked_keys: number;
  pending_sync_keys: number;
  pending_revoke_keys: number;
  skipped_unsafe_keys: number;
  failed_keys: number;
};

export type VpnTelegramUpdate = {
  id: number;
  update_id: string;
  customer_id: number | null;
  processed_at: string | null;
  error_message: string | null;
  created_at: string;
};
```

Add API calls:

```typescript
getVpnLifecycleStatus: () => request<VpnLifecycleStatus>("/control/vpn/lifecycle/status"),
getVpnNodeEligibility: () => request<VpnNodeEligibility[]>("/control/vpn/nodes/eligibility"),
getVpnTelegramUpdates: () => request<VpnTelegramUpdate[]>("/control/vpn/telegram-updates"),
deleteVpnAccessKey: (id: number) =>
  request<VpnAccessKey>(`/control/vpn/access-keys/${id}`, { method: "DELETE" }),
```

- [ ] **Step 2: Update VPN state loading**

Add state:

```typescript
const [vpnLifecycleStatus, setVpnLifecycleStatus] = useState<VpnLifecycleStatus | null>(null);
const [vpnNodeEligibility, setVpnNodeEligibility] = useState<Record<number, VpnNodeEligibility>>({});
const [vpnTelegramUpdates, setVpnTelegramUpdates] = useState<VpnTelegramUpdate[]>([]);
```

Load all three resources in the existing authenticated `loadAll` `Promise.all` group. Store eligibility with:

```typescript
setVpnNodeEligibility(Object.fromEntries(
  eligibilityItems.map((item) => [item.worker_id, item]),
));
```

- [ ] **Step 3: Render operational status and safe actions**

On each VPN node row, resolve the issuance status and the narrower attack-maintenance gate:

```typescript
const eligibility = vpnNodeEligibility[worker.id];
const vpnMaintenanceBlocked = (eligibility?.blocked_reasons ?? []).some((reason) =>
  reason.includes("active domain attack"),
);
```

Render:

```tsx
<span className={eligibility?.eligible ? "status available" : "status error"}>
  {eligibility?.eligible ? "готова к выдаче" : "заблокирована"}
</span>
{(eligibility?.blocked_reasons ?? []).map((reason) => (
  <div className="row-hint" key={reason}>{reason}</div>
))}
```

Disable VPN install/update/restart/autoconfigure/inbound buttons only when `vpnMaintenanceBlocked`; health check always remains enabled. Do not disable setup actions merely because runtime, inbound, public host, or SSH configuration is not ready—those actions are how an operator makes the node eligible. Key issuance still uses the full `eligibility.eligible` policy on the backend.

Add a lifecycle summary card with the exact counters from `VpnLifecycleStatus`. Rename the destructive key button from `Удалить` to `Отозвать и сохранить`. The handler consumes the returned key and reports `revoked` versus `pending_revoke`; it never removes the row optimistically.

For each key, render `Повторить выдачу` only in `pending_sync` or `failed` and call the existing `/provision` API; render `Повторить отзыв` only in `pending_revoke` and call the existing `/revoke` API. Keep `last_error` visible beneath every non-terminal key so an operator can correct the node before retrying.

Add a `Telegram` table with update time, update ID, customer ID, processed state, and error.

- [ ] **Step 4: Build frontend**

```powershell
npm --prefix frontend run build
```

Expected: TypeScript and Vite build succeed.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css
git commit -m "feat: show vpn operational health"
```

---

### Task 8: Document Configuration And Safe Operations

**Files:**
- Modify: `backend/.env.example`
- Create: `docs/vpn-service.md`
- Modify: `README.md`
- Modify: `docs/current-state.md`

- [ ] **Step 1: Add concrete environment settings**

Append to `backend/.env.example`:

```dotenv
VPN_LIFECYCLE_ENABLED=true
VPN_LIFECYCLE_INTERVAL_SECONDS=60
VPN_LIFECYCLE_BATCH_SIZE=50
VPN_TELEGRAM_BOT_TOKEN=
VPN_TELEGRAM_WEBHOOK_SECRET=
VPN_TELEGRAM_SECRET_TOKEN=
VPN_SUPPORT_TEXT=Напишите администратору для подключения или продления VPN.
```

- [ ] **Step 2: Write the operations guide**

Create `docs/vpn-service.md` with these exact sections:

```markdown
# VPN Service Operations

## Current Scope

The service supports manual plans/subscriptions, automatic lifecycle, 3x-UI keys, and Telegram delivery. Payments are intentionally disabled.

## Safe Node Rollout

1. Enable VPN role on one non-critical worker.
2. Configure SSH, public host, 3x-UI panel, and inbound.
3. Run VPN check, install or autoconfigure, and create inbound when required.
4. Confirm the node says `готова к выдаче`.
5. Create one test customer, subscription, and key.
6. Test the VLESS/VMess URI on a client device.
7. Revoke the key and confirm it remains in history as `revoked`.

## Telegram Webhook

Configure the environment values, restart control, then call Telegram `setWebhook` with:

`https://CONTROL_HOST/api/vpn-telegram/webhook/VPN_TELEGRAM_WEBHOOK_SECRET`

Send `X-Telegram-Bot-Api-Secret-Token` through Telegram's `secret_token` webhook option.

## Recovery

- `pending_sync`: restore a safe ready node, then run lifecycle or press retry.
- `pending_revoke`: restore the assigned node, then run lifecycle or press revoke again.
- active domain attack: wait for the run to finish; existing VPN clients continue working.
- Telegram error: inspect the Telegram updates table and control logs, correct the token/network issue, then send a new command.

## Smoke Test

Document the exact admin and Telegram sequence and expected state after every step.
```

Expand the `Smoke Test` section with plan/customer/subscription/key creation, `/start`, `/status`, `/keys`, forced expiration, lifecycle run, and final `revoked` state.

- [ ] **Step 3: Refresh repository state documentation**

Update `README.md` and `docs/current-state.md` so they state:

- lifecycle runs automatically and manually;
- Telegram supports `/start`, `/status`, `/keys`, `/support`;
- payments remain out of scope;
- VPN mutations are blocked on active attack workers;
- key history is retained after revoke.

- [ ] **Step 4: Verify documentation and config references**

```powershell
rg -n "VPN_LIFECYCLE|VPN_TELEGRAM|pending_revoke|Telegram" backend/.env.example docs/vpn-service.md README.md docs/current-state.md
```

Expected: every setting and operational state appears in the intended files.

- [ ] **Step 5: Commit**

```powershell
git add backend/.env.example docs/vpn-service.md README.md docs/current-state.md
git commit -m "docs: add vpn service operations"
```

---

### Task 9: Full Verification And Regression Review

**Files:**
- Modify only files required by failures discovered in this task; every production fix must first add a focused failing test.

- [ ] **Step 1: Run the complete backend suite**

```powershell
cd backend
python -m pytest tests -q -p no:cacheprovider
```

Expected: all backend tests pass with zero failures.

- [ ] **Step 2: Run both Ruff checks**

```powershell
cd backend
python -m ruff check app tests
cd ..\worker
python -m ruff check app
```

Expected: both commands report `All checks passed!`.

- [ ] **Step 3: Run import smoke checks**

```powershell
cd backend
python -c "from app.main import app; print(app.title)"
cd ..\worker
python -c "from app.runner import WorkerRunner; from app.gandi import register_domain; print('worker-import-ok')"
```

Expected: control title and `worker-import-ok` are printed without tracebacks.

- [ ] **Step 4: Build the frontend**

```powershell
npm --prefix frontend run build
```

Expected: TypeScript and Vite production build succeed.

- [ ] **Step 5: Inspect repository state**

```powershell
git status --short
git log --oneline -12
```

Expected: only the pre-existing untracked CSV/reference data remains; implementation files are committed.

- [ ] **Step 6: Review acceptance criteria against evidence**

Record in the final handoff:

- exact backend test count;
- Ruff results;
- frontend build result;
- whether a real 3x-UI smoke test was possible;
- whether a real Telegram webhook smoke test was possible;
- any step requiring server credentials or external deployment.

- [ ] **Step 7: Commit any test-only verification adjustments**

If Step 1 exposed an environment-sensitive test and a focused test-first correction was required:

```powershell
git add backend/tests worker/app backend/app
git commit -m "test: stabilize vpn release verification"
```

If no tracked files changed, skip this commit.

---

## Plan Self-Review

**Spec coverage:** Tasks 2-5 cover subscription rules, node safety, lifecycle, retry behavior, and attack protection. Task 6 covers the payment-free Telegram MVP and idempotency. Task 7 covers admin visibility. Task 8 covers configuration and operations. Task 9 covers complete verification.

**Scope:** Payments, referrals, coupons, traffic billing, and customer web applications remain excluded. YadrenoVPN remains a reference and is not imported.

**Type consistency:** `VpnNodeEligibility`, `VpnLifecycleStatusResponse`, `VpnTelegramUpdateResponse`, `validate_subscription_access`, `select_vpn_node`, `run_vpn_lifecycle_maintenance`, and `process_telegram_update` are introduced before consumers use them.

**Safety:** No destructive migration or force-delete path is introduced. Revoked keys remain stored. VPN mutations are blocked on active domain workers, while read-only health checks and existing client traffic remain available.
