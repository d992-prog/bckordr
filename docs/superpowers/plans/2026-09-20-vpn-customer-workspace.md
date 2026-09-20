# VPN Customer Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the read-only VPN customer tables with a customer-centric workspace that edits customers and subscriptions, safely archives customers, and exposes complete copyable access links without adding payment behavior.

**Architecture:** Keep the existing VPN data model and typed API, add one focused backend lifecycle service plus one archive endpoint for the only multi-row operation, and move the VPN customer UI out of the large `App.tsx` into a dedicated React component. Pure TypeScript helpers own filtering, expiry classification, primary-subscription selection, and date extension so the UX rules are unit tested independently of React.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, pytest/pytest-asyncio, React 18, TypeScript 5, Node test runner, CSS, Vite 6.

---

## File Map

- Create `backend/app/services/vpn_customer_lifecycle.py`: row-lock and stage the non-destructive customer archive transition.
- Create `backend/tests/test_vpn_customer_archive.py`: service and HTTP coverage for archive, revoke outcomes, audit, missing rows, and repeated archive.
- Modify `backend/app/schemas/control.py`: typed archive response.
- Modify `backend/app/api/routes/control.py`: authenticated archive endpoint and best-effort remote key revocation.
- Create `frontend/src/vpnCustomerWorkspace.ts`: pure customer filtering, status, selection, and extension-date helpers.
- Create `frontend/test/vpnCustomerWorkspace.test.mjs`: Node unit tests for those helpers.
- Create `frontend/src/VpnCustomerWorkspace.tsx`: customer-centric UI and all customer/subscription/key interactions.
- Modify `frontend/src/api.ts`: archive response type and API call.
- Modify `frontend/src/App.tsx`: remove the permanent customer/subscription/key forms and read-only tables, delegate the feature to the new component, and retain tariffs/nodes/lifecycle panels.
- Modify `frontend/src/styles.css`: two-pane workspace, responsive stacking, cards, form actions, and full-URI presentation.
- Modify `docs/current-state.md`: record the shipped admin capabilities and current verification totals.

No database migration is required.

### Task 1: Add Tested Customer-Workspace Domain Helpers

**Files:**
- Create: `frontend/src/vpnCustomerWorkspace.ts`
- Create: `frontend/test/vpnCustomerWorkspace.test.mjs`

- [ ] **Step 1: Write the failing helper tests**

Create `frontend/test/vpnCustomerWorkspace.test.mjs` with fixtures that prove search, filters, primary-subscription selection, expiry classification, and extension dates:

```js
import test from "node:test";
import assert from "node:assert/strict";

import {
  calculateExtendedExpiration,
  classifyVpnCustomer,
  filterVpnCustomers,
  selectPrimarySubscription,
} from "../src/vpnCustomerWorkspace.ts";

const now = new Date("2026-09-20T12:00:00.000Z");
const customers = [
  { id: 1, first_name: "Anna", last_name: "Kuznetsova", telegram_username: "anna", telegram_user_id: "100", status: "active", notes: null, created_at: now.toISOString(), updated_at: now.toISOString() },
  { id: 2, first_name: "Max", last_name: null, telegram_username: "max_s", telegram_user_id: "200", status: "active", notes: null, created_at: now.toISOString(), updated_at: now.toISOString() },
  { id: 3, first_name: "Old", last_name: "Client", telegram_username: "old", telegram_user_id: "300", status: "archived", notes: null, created_at: now.toISOString(), updated_at: now.toISOString() },
];
const subscriptions = [
  { id: 10, customer_id: 1, plan_id: 1, status: "active", starts_at: null, expires_at: "2026-09-24T12:00:00.000Z", traffic_limit_gb: 100, max_devices: 3, notes: null, created_at: now.toISOString(), updated_at: now.toISOString() },
  { id: 11, customer_id: 1, plan_id: 1, status: "expired", starts_at: null, expires_at: "2026-08-01T00:00:00.000Z", traffic_limit_gb: 100, max_devices: 3, notes: null, created_at: now.toISOString(), updated_at: "2026-08-01T00:00:00.000Z" },
  { id: 20, customer_id: 2, plan_id: null, status: "disabled", starts_at: null, expires_at: null, traffic_limit_gb: null, max_devices: 1, notes: null, created_at: now.toISOString(), updated_at: now.toISOString() },
];

test("selects a usable subscription before historical rows", () => {
  assert.equal(selectPrimarySubscription(1, subscriptions)?.id, 10);
});

test("classifies expiring, suspended, and archived customers", () => {
  assert.equal(classifyVpnCustomer(customers[0], subscriptions, now), "expiring");
  assert.equal(classifyVpnCustomer(customers[1], subscriptions, now), "suspended");
  assert.equal(classifyVpnCustomer(customers[2], subscriptions, now), "archived");
});

test("searches identity fields and applies the selected operational filter", () => {
  assert.deepEqual(filterVpnCustomers(customers, subscriptions, "all", "@ANNA", now).map((item) => item.id), [1]);
  assert.deepEqual(filterVpnCustomers(customers, subscriptions, "all", "200", now).map((item) => item.id), [2]);
  assert.deepEqual(filterVpnCustomers(customers, subscriptions, "expiring", "", now).map((item) => item.id), [1]);
  assert.deepEqual(filterVpnCustomers(customers, subscriptions, "archived", "", now).map((item) => item.id), [3]);
});

test("extends from the later of current expiry and now", () => {
  assert.equal(calculateExtendedExpiration("2026-10-01T12:00:00.000Z", 30, now), "2026-10-31T12:00:00.000Z");
  assert.equal(calculateExtendedExpiration("2026-09-01T12:00:00.000Z", 7, now), "2026-09-27T12:00:00.000Z");
  assert.equal(calculateExtendedExpiration(null, 30, now), "2026-10-20T12:00:00.000Z");
});
```

- [ ] **Step 2: Run the test and verify the missing module failure**

Run from the repository root:

```powershell
npm --prefix frontend test -- --test-name-pattern="selects|classifies|searches|extends"
```

Expected: FAIL because `frontend/src/vpnCustomerWorkspace.ts` does not exist.

- [ ] **Step 3: Implement the pure helpers**

Create `frontend/src/vpnCustomerWorkspace.ts`:

```ts
import type { VpnCustomer, VpnSubscription } from "./api";

export type VpnCustomerFilter = "all" | "active" | "expiring" | "suspended" | "archived";
export type VpnCustomerOperationalStatus = Exclude<VpnCustomerFilter, "all">;

const USABLE_SUBSCRIPTION_STATUSES = new Set(["active", "trial"]);
const SUSPENDED_SUBSCRIPTION_STATUSES = new Set(["disabled", "cancelled", "expired"]);
const EXPIRING_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

function timestamp(value: string | null | undefined) {
  if (!value) return Number.NEGATIVE_INFINITY;
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

export function selectPrimarySubscription(customerId: number, subscriptions: VpnSubscription[]) {
  return subscriptions
    .filter((subscription) => subscription.customer_id === customerId)
    .sort((left, right) => {
      const usableDelta = Number(USABLE_SUBSCRIPTION_STATUSES.has(right.status)) - Number(USABLE_SUBSCRIPTION_STATUSES.has(left.status));
      if (usableDelta !== 0) return usableDelta;
      return timestamp(right.updated_at) - timestamp(left.updated_at);
    })[0] ?? null;
}

export function classifyVpnCustomer(
  customer: VpnCustomer,
  subscriptions: VpnSubscription[],
  now = new Date(),
): VpnCustomerOperationalStatus {
  if (customer.status === "archived") return "archived";
  const primary = selectPrimarySubscription(customer.id, subscriptions);
  if (!primary || SUSPENDED_SUBSCRIPTION_STATUSES.has(primary.status)) return "suspended";
  if (USABLE_SUBSCRIPTION_STATUSES.has(primary.status) && primary.expires_at) {
    const remaining = timestamp(primary.expires_at) - now.getTime();
    if (remaining >= 0 && remaining <= EXPIRING_WINDOW_MS) return "expiring";
  }
  return customer.status === "active" && USABLE_SUBSCRIPTION_STATUSES.has(primary.status) ? "active" : "suspended";
}

export function filterVpnCustomers(
  customers: VpnCustomer[],
  subscriptions: VpnSubscription[],
  filter: VpnCustomerFilter,
  query: string,
  now = new Date(),
) {
  const needle = query.trim().replace(/^@/, "").toLocaleLowerCase("ru");
  return customers.filter((customer) => {
    const identity = [customer.first_name, customer.last_name, customer.telegram_username, customer.telegram_user_id, String(customer.id)]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase("ru");
    const matchesQuery = !needle || identity.includes(needle);
    const matchesFilter = filter === "all" || classifyVpnCustomer(customer, subscriptions, now) === filter;
    return matchesQuery && matchesFilter;
  });
}

export function calculateExtendedExpiration(expiresAt: string | null, days: number, now = new Date()) {
  const currentExpiry = timestamp(expiresAt);
  const base = Math.max(Number.isFinite(currentExpiry) ? currentExpiry : now.getTime(), now.getTime());
  return new Date(base + days * 24 * 60 * 60 * 1000).toISOString();
}
```

- [ ] **Step 4: Run all frontend helper tests**

```powershell
npm --prefix frontend test
```

Expected: all Node tests PASS.

- [ ] **Step 5: Commit the helper slice**

```powershell
git add frontend/src/vpnCustomerWorkspace.ts frontend/test/vpnCustomerWorkspace.test.mjs
git commit -m "test: define vpn customer workspace rules"
```

### Task 2: Stage Customer Archive Atomically in a Backend Service

**Files:**
- Create: `backend/app/services/vpn_customer_lifecycle.py`
- Create: `backend/tests/test_vpn_customer_archive.py`

- [ ] **Step 1: Write failing service tests**

Create `backend/tests/test_vpn_customer_archive.py` with an in-memory SQLite fixture and these two tests. Seed one active, one trial, and one expired subscription; attach active and already-revoked keys.

```python
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.services.vpn_customer_lifecycle import (
    VpnCustomerArchiveConflictError,
    stage_vpn_customer_archive,
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


async def seed_customer(session: AsyncSession):
    customer = VpnCustomer(telegram_user_id="archive-me", status="active")
    session.add(customer)
    await session.flush()
    active = VpnSubscription(customer_id=customer.id, status="active", max_devices=2)
    trial = VpnSubscription(customer_id=customer.id, status="trial", max_devices=1)
    expired = VpnSubscription(customer_id=customer.id, status="expired", max_devices=1)
    session.add_all([active, trial, expired])
    await session.flush()
    active_key = VpnAccessKey(subscription_id=active.id, status="active", config_uri="vless://active")
    revoked_key = VpnAccessKey(subscription_id=expired.id, status="revoked", config_uri=None)
    session.add_all([active_key, revoked_key])
    await session.commit()
    return customer, active, trial, expired, active_key, revoked_key


@pytest.mark.asyncio
async def test_stage_archive_blocks_customer_subscriptions_and_usable_keys(session_factory):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        customer, active, trial, expired, active_key, revoked_key = await seed_customer(session)
        result = await stage_vpn_customer_archive(session, customer.id, now=now)
        await session.commit()
        assert customer.status == "archived"
        assert active.status == "disabled"
        assert trial.status == "disabled"
        assert expired.status == "expired"
        assert active_key.status == "pending_revoke"
        assert revoked_key.status == "revoked"
        assert result.disabled_subscription_count == 2
        assert [item.id for item in result.access_keys] == [active_key.id]


@pytest.mark.asyncio
async def test_stage_archive_rejects_an_already_archived_customer(session_factory):
    async with session_factory() as session:
        customer, *_ = await seed_customer(session)
        customer.status = "archived"
        await session.commit()
        with pytest.raises(VpnCustomerArchiveConflictError, match="already archived"):
            await stage_vpn_customer_archive(session, customer.id)
```

- [ ] **Step 2: Verify the tests fail for the missing service**

Run from `backend`:

```powershell
python -m pytest tests/test_vpn_customer_archive.py -q -p no:cacheprovider
```

Expected: collection FAIL because `app.services.vpn_customer_lifecycle` does not exist.

- [ ] **Step 3: Implement the archive staging service**

Create `backend/app/services/vpn_customer_lifecycle.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription


ARCHIVABLE_SUBSCRIPTION_STATUSES = ("active", "trial")
REVOKABLE_KEY_STATUSES = ("active", "pending_sync", "syncing", "pending_revoke")


class VpnCustomerArchiveNotFoundError(LookupError):
    pass


class VpnCustomerArchiveConflictError(RuntimeError):
    pass


@dataclass(slots=True)
class StagedVpnCustomerArchive:
    customer: VpnCustomer
    disabled_subscription_count: int
    access_keys: list[VpnAccessKey]


async def stage_vpn_customer_archive(
    session: AsyncSession,
    customer_id: int,
    *,
    now: datetime | None = None,
) -> StagedVpnCustomerArchive:
    changed_at = now or utcnow()
    customer = await session.scalar(
        select(VpnCustomer).where(VpnCustomer.id == customer_id).with_for_update()
    )
    if customer is None:
        raise VpnCustomerArchiveNotFoundError("VPN customer not found")
    if customer.status == "archived":
        raise VpnCustomerArchiveConflictError("VPN customer is already archived")

    subscriptions = list(
        (
            await session.scalars(
                select(VpnSubscription)
                .where(VpnSubscription.customer_id == customer_id)
                .with_for_update()
            )
        ).all()
    )
    subscription_ids = [subscription.id for subscription in subscriptions]
    access_keys = []
    if subscription_ids:
        access_keys = list(
            (
                await session.scalars(
                    select(VpnAccessKey)
                    .where(
                        VpnAccessKey.subscription_id.in_(subscription_ids),
                        VpnAccessKey.status.in_(REVOKABLE_KEY_STATUSES),
                    )
                    .order_by(VpnAccessKey.id.asc())
                    .with_for_update()
                )
            ).all()
        )

    customer.status = "archived"
    customer.updated_at = changed_at
    disabled_count = 0
    for subscription in subscriptions:
        if subscription.status in ARCHIVABLE_SUBSCRIPTION_STATUSES:
            subscription.status = "disabled"
            subscription.updated_at = changed_at
            disabled_count += 1
    for access_key in access_keys:
        access_key.status = "pending_revoke"
        access_key.updated_at = changed_at

    return StagedVpnCustomerArchive(
        customer=customer,
        disabled_subscription_count=disabled_count,
        access_keys=access_keys,
    )
```

- [ ] **Step 4: Run the focused service tests**

```powershell
python -m pytest tests/test_vpn_customer_archive.py -q -p no:cacheprovider
```

Expected: `2 passed`.

- [ ] **Step 5: Commit the atomic archive service**

```powershell
git add backend/app/services/vpn_customer_lifecycle.py backend/tests/test_vpn_customer_archive.py
git commit -m "feat: stage vpn customer archive"
```

### Task 3: Expose the Safe Archive Endpoint and Revoke Keys

**Files:**
- Modify: `backend/app/schemas/control.py:579-680`
- Modify: `backend/app/api/routes/control.py:20-180,2944-2998`
- Modify: `backend/tests/test_vpn_customer_archive.py`

- [ ] **Step 1: Add failing HTTP archive tests**

Extend `backend/tests/test_vpn_customer_archive.py` with an ASGI client fixture. Import `httpx`, `FastAPI`, `SimpleNamespace`, `select`, `require_admin`, `control_router`, `get_db`, `AdminAuditLog`, and `WorkerNode`. Add a ready node to the seed data and attach two usable keys. Patch `app.api.routes.control.revoke_vpn_access_key` so one key becomes `revoked` and one stays `pending_revoke`.

The success test must assert this exact contract:

```python
response = await api_client.post(f"/control/vpn/customers/{customer_id}/archive")
assert response.status_code == 200
assert response.json()["customer"]["status"] == "archived"
assert response.json()["disabled_subscriptions"] == 2
assert response.json()["revoked_keys"] == 1
assert response.json()["pending_revoke_keys"] == 1

async with session_factory() as session:
    audit = await session.scalar(
        select(AdminAuditLog).where(AdminAuditLog.action == "vpn_customer_archive")
    )
    assert audit is not None
    assert f"customer_id={customer_id}" in (audit.details or "")
```

Add separate assertions for `404` on an unknown ID and `409` on a second archive request. Add a restore test that archives a customer, calls `PATCH /control/vpn/customers/{customer_id}` with `{"status": "active"}`, and then asserts through the database that the customer is active while its subscriptions remain `disabled` and its old keys remain `revoked` or `pending_revoke`.

- [ ] **Step 2: Run the endpoint tests and verify 404 route failures**

```powershell
python -m pytest tests/test_vpn_customer_archive.py -q -p no:cacheprovider
```

Expected: service tests PASS and new endpoint tests FAIL because the route is absent.

- [ ] **Step 3: Add the typed response schema**

Immediately after `VpnCustomerResponse` in `backend/app/schemas/control.py`, add:

```python
class VpnCustomerArchiveResponse(BaseModel):
    customer: VpnCustomerResponse
    disabled_subscriptions: int
    revoked_keys: int
    pending_revoke_keys: int
```

Import this response in `backend/app/api/routes/control.py`.

- [ ] **Step 4: Add the archive route**

Import the new service errors and `stage_vpn_customer_archive`. Add the route after `update_vpn_customer`:

```python
@router.post("/vpn/customers/{customer_id}/archive", response_model=VpnCustomerArchiveResponse)
async def archive_vpn_customer(
    customer_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> VpnCustomerArchiveResponse:
    try:
        staged = await stage_vpn_customer_archive(db, customer_id)
    except VpnCustomerArchiveNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except VpnCustomerArchiveConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    for access_key in staged.access_keys:
        worker = await db.get(WorkerNode, access_key.worker_id) if access_key.worker_id else None
        if worker is None and not access_key.config_uri:
            access_key.status = "revoked"
            access_key.revoked_at = utcnow()
            access_key.last_error = None
            continue
        if worker is not None:
            locked_worker = await lock_vpn_worker(db, worker.id)
            eligibility = await evaluate_vpn_node(db, locked_worker) if locked_worker is not None else None
            if eligibility is None or not eligibility.eligible:
                access_key.status = "pending_revoke"
                access_key.last_error = "; ".join(eligibility.reasons) if eligibility is not None else "VPN node was not found"
                continue
            worker = locked_worker
        await revoke_vpn_access_key(db, access_key, worker=worker)

    revoked_keys = sum(key.status == "revoked" for key in staged.access_keys)
    pending_revoke_keys = sum(key.status != "revoked" for key in staged.access_keys)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="vpn_customer_archive",
        details=(
            f"customer_id={customer_id} "
            f"disabled_subscriptions={staged.disabled_subscription_count} "
            f"revoked_keys={revoked_keys} pending_revoke_keys={pending_revoke_keys}"
        ),
    )
    await db.commit()
    await db.refresh(staged.customer)
    return VpnCustomerArchiveResponse(
        customer=VpnCustomerResponse.model_validate(staged.customer),
        disabled_subscriptions=staged.disabled_subscription_count,
        revoked_keys=revoked_keys,
        pending_revoke_keys=pending_revoke_keys,
    )
```

- [ ] **Step 5: Run archive and existing VPN API tests**

```powershell
python -m pytest tests/test_vpn_customer_archive.py tests/test_vpn_control_api.py -q -p no:cacheprovider
```

Expected: all selected tests PASS.

- [ ] **Step 6: Run Ruff on the new backend slice**

```powershell
python -m ruff check app/services/vpn_customer_lifecycle.py app/schemas/control.py app/api/routes/control.py tests/test_vpn_customer_archive.py
```

Expected: PASS with no diagnostics.

- [ ] **Step 7: Commit the endpoint**

```powershell
git add backend/app/schemas/control.py backend/app/api/routes/control.py backend/tests/test_vpn_customer_archive.py
git commit -m "feat: archive vpn customers safely"
```

### Task 4: Add the Typed Frontend Archive API

**Files:**
- Modify: `frontend/src/api.ts:476-520,994-1032`

- [ ] **Step 1: Add the response type and method**

After `VpnCustomer`, add:

```ts
export type VpnCustomerArchiveResult = {
  customer: VpnCustomer;
  disabled_subscriptions: number;
  revoked_keys: number;
  pending_revoke_keys: number;
};
```

After `updateVpnCustomer`, add:

```ts
archiveVpnCustomer: (id: number) =>
  request<VpnCustomerArchiveResult>(`/control/vpn/customers/${id}/archive`, {
    method: "POST",
  }),
```

- [ ] **Step 2: Run the strict frontend build**

```powershell
npm --prefix frontend run build
```

Expected: TypeScript and Vite build PASS.

- [ ] **Step 3: Commit the API contract**

```powershell
git add frontend/src/api.ts frontend/tsconfig.tsbuildinfo
git commit -m "feat: add vpn customer archive client"
```

### Task 5: Build the Customer List and Editable Profile

**Files:**
- Create: `frontend/src/VpnCustomerWorkspace.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Create the component shell and explicit props**

Create `frontend/src/VpnCustomerWorkspace.tsx` with these public inputs and local state:

```tsx
import { FormEvent, useEffect, useMemo, useState } from "react";

import { api, VpnAccessKey, VpnCustomer, VpnPlan, VpnSubscription, WorkerNode } from "./api";
import {
  calculateExtendedExpiration,
  classifyVpnCustomer,
  filterVpnCustomers,
  selectPrimarySubscription,
  VpnCustomerFilter,
} from "./vpnCustomerWorkspace";

type NoticeType = "success" | "error";

type Props = {
  customers: VpnCustomer[];
  subscriptions: VpnSubscription[];
  accessKeys: VpnAccessKey[];
  plans: VpnPlan[];
  workers: WorkerNode[];
  reload: () => Promise<void>;
  notify: (type: NoticeType, text: string) => void;
};

const EMPTY_CUSTOMER_FORM = {
  telegramUserId: "",
  telegramUsername: "",
  firstName: "",
  lastName: "",
  status: "active",
  notes: "",
};

export function VpnCustomerWorkspace({ customers, subscriptions, accessKeys, plans, workers, reload, notify }: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<VpnCustomerFilter>("all");
  const [selectedCustomerId, setSelectedCustomerId] = useState<number | null>(null);
  const [customerForm, setCustomerForm] = useState(EMPTY_CUSTOMER_FORM);
  const [editingCustomer, setEditingCustomer] = useState(false);
  const [creatingCustomer, setCreatingCustomer] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);

  const filteredCustomers = useMemo(
    () => filterVpnCustomers(customers, subscriptions, filter, query),
    [customers, subscriptions, filter, query],
  );
  const selectedCustomer = customers.find((item) => item.id === selectedCustomerId) ?? null;

  useEffect(() => {
    if (selectedCustomerId && customers.some((item) => item.id === selectedCustomerId)) return;
    setSelectedCustomerId(filteredCustomers[0]?.id ?? customers[0]?.id ?? null);
  }, [customers, filteredCustomers, selectedCustomerId]);

  function beginCustomerEdit(customer: VpnCustomer) {
    setCustomerForm({
      telegramUserId: customer.telegram_user_id ?? "",
      telegramUsername: customer.telegram_username ?? "",
      firstName: customer.first_name ?? "",
      lastName: customer.last_name ?? "",
      status: customer.status,
      notes: customer.notes ?? "",
    });
    setEditingCustomer(true);
  }

  async function saveCustomer(event: FormEvent) {
    event.preventDefault();
    setBusyAction(selectedCustomer ? "customer-update" : "customer-create");
    try {
      const payload = {
        telegram_user_id: customerForm.telegramUserId.trim() || null,
        telegram_username: customerForm.telegramUsername.replace(/^@/, "").trim() || null,
        first_name: customerForm.firstName.trim() || null,
        last_name: customerForm.lastName.trim() || null,
        status: customerForm.status,
        notes: customerForm.notes.trim() || null,
      };
      const saved = selectedCustomer && editingCustomer
        ? await api.updateVpnCustomer(selectedCustomer.id, payload)
        : await api.createVpnCustomer(payload);
      await reload();
      setSelectedCustomerId(saved.id);
      setEditingCustomer(false);
      setCreatingCustomer(false);
      setCustomerForm(EMPTY_CUSTOMER_FORM);
      notify("success", selectedCustomer && editingCustomer ? "Данные клиента сохранены" : "VPN клиент добавлен");
    } catch (error) {
      notify("error", error instanceof Error ? error.message : "Не удалось сохранить клиента");
    } finally {
      setBusyAction(null);
    }
  }
```

Close the initial component with a searchable list and read-only selected-customer pane. Add these constants above the component:

```tsx
const CUSTOMER_FILTERS: Array<{ value: VpnCustomerFilter; label: string }> = [
  { value: "all", label: "Все" },
  { value: "active", label: "Активные" },
  { value: "expiring", label: "Скоро истекут" },
  { value: "suspended", label: "Приостановлены" },
  { value: "archived", label: "Архив" },
];

function customerName(customer: VpnCustomer) {
  const name = [customer.first_name, customer.last_name].filter(Boolean).join(" ").trim();
  if (name) return name;
  if (customer.telegram_username) return `@${customer.telegram_username}`;
  if (customer.telegram_user_id) return `Telegram ID ${customer.telegram_user_id}`;
  return `клиент #${customer.id}`;
}
```

Add this return value after `saveCustomer`:

```tsx
  return (
    <div className="vpn-customer-workspace">
      <aside className="vpn-customer-sidebar">
        <div className="vpn-workspace-toolbar">
          <strong>Клиенты</strong>
          <button
            type="button"
            onClick={() => {
              setCustomerForm(EMPTY_CUSTOMER_FORM);
              setEditingCustomer(false);
              setCreatingCustomer(true);
            }}
          >
            Новый клиент
          </button>
        </div>
        <label>
          <span>Поиск</span>
          <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Имя, @username или Telegram ID" />
        </label>
        <div className="actions" aria-label="Фильтр клиентов">
          {CUSTOMER_FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={filter === item.value ? "secondary active-chip" : "ghost"}
              onClick={() => setFilter(item.value)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="vpn-customer-list">
          {filteredCustomers.map((customer) => {
            const primary = selectPrimarySubscription(customer.id, subscriptions);
            const state = classifyVpnCustomer(customer, subscriptions);
            return (
              <button
                key={customer.id}
                type="button"
                className={`vpn-customer-row ${selectedCustomerId === customer.id ? "active-chip" : ""}`}
                onClick={() => {
                  setSelectedCustomerId(customer.id);
                  setCreatingCustomer(false);
                  setEditingCustomer(false);
                }}
              >
                <span>
                  <strong>{customerName(customer)}</strong>
                  <span className="row-hint">
                    {customer.telegram_username ? `@${customer.telegram_username}` : customer.telegram_user_id ?? "без Telegram"}
                  </span>
                  <span className="row-hint">{primary ? `подписка #${primary.id}` : "без подписки"}</span>
                </span>
                <span className={`status ${state === "active" ? "available" : state === "expiring" ? "checking" : "inactive"}`}>{state}</span>
              </button>
            );
          })}
          {filteredCustomers.length === 0 ? <p className="empty">По этому фильтру клиентов нет.</p> : null}
        </div>
      </aside>
      <section className="vpn-customer-detail">
        {creatingCustomer ? (
          <form className="form" onSubmit={saveCustomer}>
            <h3>Новый клиент</h3>
            <input value={customerForm.firstName} onChange={(event) => setCustomerForm((current) => ({ ...current, firstName: event.target.value }))} placeholder="Имя" />
            <input value={customerForm.telegramUsername} onChange={(event) => setCustomerForm((current) => ({ ...current, telegramUsername: event.target.value }))} placeholder="@username" />
            <div className="actions">
              <button type="submit" disabled={busyAction === "customer-create"}>Создать</button>
              <button type="button" className="ghost" onClick={() => setCreatingCustomer(false)}>Отмена</button>
            </div>
          </form>
        ) : selectedCustomer ? (
          <div className="vpn-workspace-stack">
            <div className="vpn-workspace-section">
              <h3>{customerName(selectedCustomer)}</h3>
              <p className="muted">{selectedCustomer.notes || "Заметок нет"}</p>
            </div>
          </div>
        ) : (
          <p className="empty">Выберите клиента или создайте нового.</p>
        )}
      </section>
    </div>
  );
}
```

- [ ] **Step 2: Add profile edit, archive, and restore handlers**

Add these handlers inside the component:

```tsx
  async function archiveCustomer(customer: VpnCustomer) {
    if (!window.confirm(`Перенести ${customer.first_name || customer.telegram_username || `клиента #${customer.id}`} в архив? Подписки будут отключены, а ключи отправлены на отзыв.`)) return;
    setBusyAction("customer-archive");
    try {
      const result = await api.archiveVpnCustomer(customer.id);
      await reload();
      const pending = result.pending_revoke_keys > 0 ? `, ожидают отзыва: ${result.pending_revoke_keys}` : "";
      notify("success", `Клиент перенесён в архив. Отозвано ключей: ${result.revoked_keys}${pending}`);
    } catch (error) {
      notify("error", error instanceof Error ? error.message : "Не удалось архивировать клиента");
    } finally {
      setBusyAction(null);
    }
  }

  async function restoreCustomer(customer: VpnCustomer) {
    setBusyAction("customer-restore");
    try {
      await api.updateVpnCustomer(customer.id, { status: "active" });
      await reload();
      notify("success", "Клиент восстановлен. Подписки и ключи нужно активировать отдельно.");
    } catch (error) {
      notify("error", error instanceof Error ? error.message : "Не удалось восстановить клиента");
    } finally {
      setBusyAction(null);
    }
  }
```

Add one field renderer inside the component and use it in both create and edit forms:

```tsx
  function renderCustomerFields() {
    return (
      <>
        <div className="form two-columns">
          <label><span>Telegram ID</span><input value={customerForm.telegramUserId} onChange={(event) => setCustomerForm((current) => ({ ...current, telegramUserId: event.target.value }))} /></label>
          <label><span>Telegram username</span><input value={customerForm.telegramUsername} onChange={(event) => setCustomerForm((current) => ({ ...current, telegramUsername: event.target.value }))} placeholder="@username" /></label>
          <label><span>Имя</span><input value={customerForm.firstName} onChange={(event) => setCustomerForm((current) => ({ ...current, firstName: event.target.value }))} /></label>
          <label><span>Фамилия</span><input value={customerForm.lastName} onChange={(event) => setCustomerForm((current) => ({ ...current, lastName: event.target.value }))} /></label>
          <label>
            <span>Статус</span>
            <select value={customerForm.status} onChange={(event) => setCustomerForm((current) => ({ ...current, status: event.target.value }))}>
              <option value="active">Активен</option>
              <option value="blocked">Заблокирован</option>
              <option value="archived">Архив</option>
            </select>
          </label>
        </div>
        <label><span>Заметки</span><textarea value={customerForm.notes} onChange={(event) => setCustomerForm((current) => ({ ...current, notes: event.target.value }))} /></label>
      </>
    );
  }
```

Replace the two abbreviated inputs in the creation form with `{renderCustomerFields()}`. Replace the read-only selected-customer block with:

```tsx
<div className="vpn-workspace-stack">
  <section className="vpn-workspace-section">
    <div className="vpn-workspace-section-head">
      <div>
        <h3>{customerName(selectedCustomer)}</h3>
        <p className="muted">
          {selectedCustomer.telegram_username ? `@${selectedCustomer.telegram_username}` : selectedCustomer.telegram_user_id ?? "Telegram не указан"}
        </p>
      </div>
      <div className="actions">
        {!editingCustomer ? <button type="button" className="ghost" onClick={() => beginCustomerEdit(selectedCustomer)}>Изменить</button> : null}
        {selectedCustomer.status === "archived" ? (
          <button type="button" className="ghost" disabled={busyAction === "customer-restore"} onClick={() => void restoreCustomer(selectedCustomer)}>Восстановить</button>
        ) : (
          <button type="button" className="danger" disabled={busyAction === "customer-archive"} onClick={() => void archiveCustomer(selectedCustomer)}>В архив</button>
        )}
      </div>
    </div>
    {editingCustomer ? (
      <form className="form" onSubmit={saveCustomer}>
        {renderCustomerFields()}
        <div className="actions">
          <button type="submit" disabled={busyAction === "customer-update"}>Сохранить</button>
          <button type="button" className="ghost" onClick={() => setEditingCustomer(false)}>Отмена</button>
        </div>
      </form>
    ) : (
      <p>{selectedCustomer.notes || "Заметок нет"}</p>
    )}
  </section>
</div>
```

The selected-customer subscriptions and keys from Task 6 are appended to the same `vpn-workspace-stack` after this profile section.

- [ ] **Step 3: Add base workspace styles**

Append product-native styles to `frontend/src/styles.css`:

```css
.vpn-customer-workspace {
  display: grid;
  grid-template-columns: minmax(260px, 0.8fr) minmax(0, 2fr);
  gap: 18px;
}

.vpn-customer-sidebar,
.vpn-customer-detail,
.vpn-workspace-section {
  border: 1px solid var(--border);
  border-radius: 20px;
  background: var(--panel-strong);
}

.vpn-customer-sidebar,
.vpn-customer-detail {
  padding: 16px;
}

.vpn-customer-list,
.vpn-workspace-stack {
  display: grid;
  gap: 10px;
}

.vpn-customer-row {
  width: 100%;
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  gap: 10px;
  text-align: left;
  background: transparent;
  color: var(--text);
  border: 1px solid var(--border);
}

.vpn-customer-row.active-chip {
  background: rgba(183, 93, 45, 0.1);
}

.vpn-workspace-section {
  padding: 16px;
}

.vpn-workspace-section-head,
.vpn-workspace-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

@media (max-width: 980px) {
  .vpn-customer-workspace {
    grid-template-columns: 1fr;
  }
}
```

- [ ] **Step 4: Run tests and TypeScript build**

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: tests and build PASS.

- [ ] **Step 5: Commit the customer/profile UI**

```powershell
git add frontend/src/VpnCustomerWorkspace.tsx frontend/src/styles.css frontend/tsconfig.tsbuildinfo
git commit -m "feat: add vpn customer workspace"
```

### Task 6: Add Subscription Management and Complete Access-Key Controls

**Files:**
- Modify: `frontend/src/VpnCustomerWorkspace.tsx`
- Modify: `frontend/src/styles.css`

- [ ] **Step 1: Add selected-customer subscription and key derivations**

Inside `VpnCustomerWorkspace`, derive data without duplicating server state:

```tsx
  const selectedSubscriptions = useMemo(
    () => subscriptions
      .filter((item) => item.customer_id === selectedCustomerId)
      .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at)),
    [selectedCustomerId, subscriptions],
  );
  const selectedSubscriptionIds = useMemo(
    () => new Set(selectedSubscriptions.map((item) => item.id)),
    [selectedSubscriptions],
  );
  const selectedAccessKeys = useMemo(
    () => accessKeys.filter((item) => selectedSubscriptionIds.has(item.subscription_id)),
    [accessKeys, selectedSubscriptionIds],
  );
  const planMap = useMemo(() => new Map(plans.map((item) => [item.id, item])), [plans]);
  const workerMap = useMemo(() => new Map(workers.map((item) => [item.id, item])), [workers]);
```

- [ ] **Step 2: Add subscription create/edit and quick actions**

Use one local subscription draft with `planId`, `status`, `startsAt`, `expiresAt`, `trafficLimitGb`, `maxDevices`, and `notes`. Add `saveSubscription`, `extendSubscription`, and `suspendSubscription` handlers.

The extension handler must use the tested helper and show the calculated date before writing:

```tsx
  async function extendSubscription(subscription: VpnSubscription, days: 7 | 30 | 90) {
    const expiresAt = calculateExtendedExpiration(subscription.expires_at, days);
    const formatted = new Date(expiresAt).toLocaleString("ru-RU");
    if (!window.confirm(`Продлить подписку #${subscription.id} на ${days} дней, до ${formatted}, и активировать её?`)) return;
    setBusyAction(`subscription-${subscription.id}`);
    try {
      await api.updateVpnSubscription(subscription.id, { expires_at: expiresAt, status: "active" });
      await reload();
      notify("success", `Подписка продлена до ${formatted}`);
    } catch (error) {
      notify("error", error instanceof Error ? error.message : "Не удалось продлить подписку");
    } finally {
      setBusyAction(null);
    }
  }

  async function suspendSubscription(subscription: VpnSubscription) {
    if (!window.confirm(`Приостановить подписку #${subscription.id}? Активные ключи будут отозваны обслуживанием VPN.`)) return;
    setBusyAction(`subscription-${subscription.id}`);
    try {
      await api.updateVpnSubscription(subscription.id, { status: "disabled" });
      await api.runVpnLifecycleMaintenance();
      await reload();
      notify("success", "Подписка приостановлена, ключи отправлены на отзыв");
    } catch (error) {
      notify("error", error instanceof Error ? error.message : "Не удалось приостановить подписку");
    } finally {
      setBusyAction(null);
    }
  }
```

The subscription form status options are exactly `active`, `trial`, `disabled`, `expired`, and `cancelled`. Creating a subscription always sends `customer_id: selectedCustomer.id`; editing never changes the customer ID.

- [ ] **Step 3: Add key issue, retry, revoke, expand, and copy controls**

Group `selectedAccessKeys` under each subscription. Keep a `Set<number>` of expanded key IDs in component state. Render the full URI only inside this read-only textarea:

```tsx
{expandedKeyIds.has(accessKey.id) && accessKey.config_uri ? (
  <textarea
    id={`vpn-key-uri-${accessKey.id}`}
    className="vpn-key-uri"
    value={accessKey.config_uri}
    readOnly
    spellCheck={false}
    aria-label={`Полная ссылка ключа ${accessKey.public_name ?? accessKey.id}`}
  />
) : null}
```

Copy the original value and provide a manual fallback:

```tsx
  async function copyAccessKey(accessKey: VpnAccessKey) {
    if (!accessKey.config_uri) return;
    try {
      await navigator.clipboard.writeText(accessKey.config_uri);
      notify("success", "Полная VPN-ссылка скопирована");
    } catch {
      setExpandedKeyIds((current) => new Set(current).add(accessKey.id));
      window.requestAnimationFrame(() => {
        const field = document.getElementById(`vpn-key-uri-${accessKey.id}`) as HTMLTextAreaElement | null;
        field?.focus();
        field?.select();
      });
      notify("error", "Буфер обмена недоступен. Полная ссылка выделена — скопируйте её вручную.");
    }
  }
```

Use `api.createVpnAccessKey`, `api.provisionVpnAccessKey`, and `api.revokeVpnAccessKey` directly. Preserve existing confirmation wording for revoke, display `last_error` for non-revoked keys, and never include `config_uri` in a toast or error message.

- [ ] **Step 4: Add subscription/key styles and narrow-screen behavior**

Append:

```css
.vpn-subscription-card,
.vpn-key-card {
  display: grid;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 16px;
  background: rgba(255, 250, 242, 0.58);
}

.vpn-subscription-meta,
.vpn-key-meta {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(130px, 1fr));
  gap: 10px;
}

.vpn-key-preview {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: "Courier New", monospace;
}

.vpn-key-uri {
  min-height: 96px;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
  font-family: "Courier New", monospace;
}

@media (max-width: 720px) {
  .vpn-workspace-section-head,
  .vpn-workspace-toolbar {
    align-items: stretch;
    flex-direction: column;
  }
}
```

- [ ] **Step 5: Run frontend tests and build**

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all helper tests PASS and the production build PASS.

- [ ] **Step 6: Commit the complete workspace interactions**

```powershell
git add frontend/src/VpnCustomerWorkspace.tsx frontend/src/styles.css frontend/tsconfig.tsbuildinfo
git commit -m "feat: manage vpn subscriptions and keys"
```

### Task 7: Integrate the Workspace and Remove the Read-Only Duplicate UI

**Files:**
- Modify: `frontend/src/App.tsx:1-35,250-285,1100-1160,2350-2460,4510-4665,4790-4905`

- [ ] **Step 1: Import and render the dedicated component**

Add:

```tsx
import { VpnCustomerWorkspace } from "./VpnCustomerWorkspace";
```

After the tariff card/table and before the VPN-node card, render:

```tsx
<div className="card full-span">
  <div className="card-head">
    <div>
      <h2>Клиенты и доступ</h2>
      <p className="muted">Управление клиентом, подписками и ключами в одном месте.</p>
    </div>
  </div>
  <VpnCustomerWorkspace
    customers={vpnCustomers}
    subscriptions={vpnSubscriptions}
    accessKeys={vpnAccessKeys}
    plans={vpnPlans}
    workers={vpnNodes}
    reload={() => loadAll()}
    notify={(type, text) => setToast({ type, text })}
  />
</div>
```

- [ ] **Step 2: Remove obsolete App-owned customer controls**

Delete `DEFAULT_VPN_CUSTOMER_FORM`, `DEFAULT_VPN_SUBSCRIPTION_FORM`, `DEFAULT_VPN_ACCESS_KEY_FORM`, their three `useState` calls, `submitVpnCustomer`, `submitVpnSubscription`, `submitVpnAccessKey`, `provisionVpnAccessKey`, `revokeVpnAccessKey`, and `deleteVpnAccessKey` from `App.tsx` after confirming the new component owns every call.

Delete the permanent `Новый клиент`, `Новая подписка`, and `Новый ключ` cards. Keep `Новый тариф`. Delete the old `Клиенты и подписки` and `Ключи доступа` read-only tables so there is one source of UI truth.

- [ ] **Step 3: Add the missing Russian archive label**

Add `archived: "в архиве"` and `pending_revoke: "ожидает отзыва"` to `formatStatusLabel`. Ensure `statusClass("archived")` continues to use the inactive style.

- [ ] **Step 4: Run tests and production build**

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all tests PASS, TypeScript reports no unused form symbols, and Vite completes.

- [ ] **Step 5: Perform a local browser smoke check**

Run:

```powershell
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5175
```

Verify at desktop and narrow widths:

- selecting a customer changes the right pane;
- search matches name, username, and Telegram ID;
- all five filters produce the expected rows;
- customer edit save/cancel works;
- archive confirmation names the customer and archive remains visible under `Архив`;
- restore does not reactivate old subscriptions;
- subscription edit and +7/+30/+90 actions show calculated dates;
- suspend triggers lifecycle maintenance and exposes pending revoke errors;
- full URI wraps, copies exactly, and remains absent from toast text;
- no customer, no search results, no subscriptions, and no keys each have distinct empty states;
- at narrow width the customer list stacks above details with no clipped buttons.

Stop the dev server after the check.

- [ ] **Step 6: Commit the integration cleanup**

```powershell
git add frontend/src/App.tsx frontend/tsconfig.tsbuildinfo
git commit -m "refactor: integrate vpn customer workspace"
```

### Task 8: Full Verification and Documentation

**Files:**
- Modify: `docs/current-state.md`

- [ ] **Step 1: Run the complete backend test suite**

From `backend`:

```powershell
python -m pytest tests -q -p no:cacheprovider
```

Expected: all tests PASS.

- [ ] **Step 2: Run backend and worker Ruff checks**

From `backend`:

```powershell
python -m ruff check app tests
```

From `worker`:

```powershell
python -m ruff check app
```

Expected: both commands PASS.

- [ ] **Step 3: Run final frontend verification**

From the repository root:

```powershell
npm --prefix frontend test
npm --prefix frontend run build
```

Expected: all Node tests PASS and the TypeScript/Vite production build PASS.

- [ ] **Step 4: Update the current-state record**

In `docs/current-state.md`, add a dated entry stating that the VPN admin now has a customer-centric workspace, editable customers/subscriptions, safe archive/restore, quick extension actions, and complete copyable access URIs. Record the actual test counts and verification commands from Steps 1-3; do not copy older counts.

- [ ] **Step 5: Review the final diff for secrets and scope**

```powershell
git diff --check
git status --short
git diff -- backend/app/services/vpn_customer_lifecycle.py backend/app/schemas/control.py backend/app/api/routes/control.py backend/tests/test_vpn_customer_archive.py frontend/src/api.ts frontend/src/vpnCustomerWorkspace.ts frontend/test/vpnCustomerWorkspace.test.mjs frontend/src/VpnCustomerWorkspace.tsx frontend/src/App.tsx frontend/src/styles.css docs/current-state.md
```

Expected: no whitespace errors, no real `vless://` production URI, no server credentials, no payment integration, and no unrelated CSV or `.tmp-yadrenovpn` files staged.

- [ ] **Step 6: Commit verification documentation**

```powershell
git add docs/current-state.md
git commit -m "docs: record vpn customer workspace"
```

- [ ] **Step 7: Prepare deployment without exposing access keys**

After code review, push the tested commits, pull them on `/opt/domain-drop-catcher`, run `npm ci && npm run build` in `/opt/domain-drop-catcher/frontend`, restart `domain-drop-control.service`, and verify local/public health plus authenticated customer/archive API behavior. Browser smoke checks may confirm that a real key copies correctly, but commands, logs, screenshots, and chat output must never print the URI.
