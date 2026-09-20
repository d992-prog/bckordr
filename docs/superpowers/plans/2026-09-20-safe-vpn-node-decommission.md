# Safe VPN Node Decommission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe panel action that archives an obsolete worker/VPN node, retires its local VPN credentials, preserves audit history, and removes it from all active scheduling and VPN views.

**Architecture:** Add `WorkerNode.archived_at` and a focused `worker_decommission` service that owns locking, conflict checks, key retirement, credential clearing, and the audit event. Keep the existing worker DELETE endpoint but change it from physical deletion to the service-backed archive operation, then expose that same operation from both the worker and VPN tables.

**Tech Stack:** FastAPI, SQLAlchemy asyncio, PostgreSQL/SQLite test database, pytest, React 18, TypeScript, Vite.

---

## File Structure

- Create `backend/app/services/worker_decommission.py`: transactional decommission domain logic and typed errors/result.
- Create `backend/tests/test_worker_decommission.py`: service and API regression coverage.
- Modify `backend/app/db/models.py`: add the archive timestamp.
- Modify `backend/app/db/migrations.py`: make the archive column available on existing PostgreSQL databases.
- Modify `backend/app/api/routes/control.py`: route integration, active-list filtering, archived-worker guards, audit, and allowlist sync.
- Modify `backend/app/services/vpn_policy.py`: exclude archived workers from VPN selection.
- Modify `frontend/src/App.tsx`: confirmation handler and delete buttons in both node views.
- Modify `docs/vpn-service.md`: operator-facing decommission behavior and warning.
- Modify `docs/current-state.md`: record the completed capability and verification checkpoint.

### Task 1: Add the Worker Archive State

**Files:**
- Modify: `backend/app/db/models.py:571-601`
- Modify: `backend/app/db/migrations.py:20-55`
- Create: `backend/tests/test_worker_decommission.py`

- [ ] **Step 1: Write the failing model test**

Create the test database fixture and prove the desired ORM field is persisted:

```python
from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import WorkerNode


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


@pytest.mark.asyncio
async def test_worker_archive_timestamp_is_persisted(session_factory):
    archived_at = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = WorkerNode(name="retired-node", archived_at=archived_at)
        session.add(worker)
        await session.commit()
        await session.refresh(worker)

        assert worker.archived_at is not None
        assert worker.archived_at.replace(tzinfo=UTC) == archived_at
```

- [ ] **Step 2: Run the test and verify RED**

Run:

```powershell
cd backend
python -m pytest tests/test_worker_decommission.py::test_worker_archive_timestamp_is_persisted -q -p no:cacheprovider
```

Expected: FAIL because `archived_at` is not a valid `WorkerNode` field.

- [ ] **Step 3: Add the model and compatibility migration**

Add to `WorkerNode`:

```python
archived_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True),
    nullable=True,
    index=True,
)
```

Add to `MIGRATIONS`:

```python
"ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ NULL",
"CREATE INDEX IF NOT EXISTS ix_worker_nodes_archived_at ON worker_nodes(archived_at)",
```

- [ ] **Step 4: Run the model test and verify GREEN**

Run the Step 2 command again. Expected: `1 passed`.

- [ ] **Step 5: Run schema regression checks**

Run:

```powershell
python -m pytest tests/test_runtime_schema.py tests/test_vpn_models.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/db/models.py backend/app/db/migrations.py backend/tests/test_worker_decommission.py
git commit -m "feat: add worker archive state"
```

### Task 2: Implement Transactional Decommissioning

**Files:**
- Create: `backend/app/services/worker_decommission.py`
- Modify: `backend/tests/test_worker_decommission.py`

- [ ] **Step 1: Write failing service tests**

Extend the test file with helpers that seed a worker, customer, subscription, and active key. Add three tests:

```python
from datetime import timedelta

from sqlalchemy import select

from app.db.models import (
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerTask,
)
from app.services.worker_decommission import (
    WorkerDecommissionConflictError,
    decommission_worker,
)


async def seed_vpn_node(session: AsyncSession) -> tuple[WorkerNode, VpnCustomer, VpnSubscription, VpnAccessKey]:
    worker = WorkerNode(
        name="obsolete-vpn-node",
        status="ready",
        is_enabled=True,
        control_token="worker-token",
        ip_address="203.0.113.10",
        ssh_host="203.0.113.10",
        ssh_username="root",
        ssh_password="ssh-secret",
        ssh_key_path="/root/.ssh/id_ed25519",
        vpn_role="drop_worker_vpn",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_public_host="obsolete.example",
        vpn_panel_url="https://obsolete.example:2053",
        vpn_panel_username="admin",
        vpn_panel_password="panel-secret",
        vpn_inbound_id=1,
    )
    customer = VpnCustomer(status="active")
    session.add_all([worker, customer])
    await session.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
    session.add(subscription)
    await session.flush()
    access_key = VpnAccessKey(
        subscription_id=subscription.id,
        worker_id=worker.id,
        status="active",
        config_uri="vless://secret-client-uri",
    )
    session.add(access_key)
    await session.commit()
    return worker, customer, subscription, access_key


@pytest.mark.asyncio
async def test_decommission_worker_archives_node_and_retires_attached_keys(session_factory):
    now = datetime(2026, 9, 20, 13, 30, tzinfo=UTC)
    async with session_factory() as session:
        worker, customer, subscription, access_key = await seed_vpn_node(session)

        result = await decommission_worker(session, worker.id, now=now)
        await session.commit()

        await session.refresh(worker)
        await session.refresh(access_key)
        assert result.retired_key_count == 1
        assert worker.archived_at is not None
        assert worker.archived_at.replace(tzinfo=UTC) == now
        assert worker.status == "archived"
        assert worker.is_enabled is False
        assert worker.control_token is None
        assert worker.ssh_password is None
        assert worker.ssh_key_path is None
        assert worker.vpn_panel_password is None
        assert worker.vpn_enabled is False
        assert worker.vpn_role == "none"
        assert worker.vpn_runtime_status == "decommissioned"
        assert access_key.status == "revoked"
        assert access_key.revoked_at is not None
        assert access_key.revoked_at.replace(tzinfo=UTC) == now
        assert access_key.config_uri is None
        assert access_key.worker_id == worker.id
        assert "remote revoke was not confirmed" in (access_key.last_error or "")
        assert await session.get(VpnCustomer, customer.id) is not None
        assert await session.get(VpnSubscription, subscription.id) is not None
        event = await session.scalar(
            select(VpnNodeEvent).where(VpnNodeEvent.worker_id == worker.id)
        )
        assert event is not None
        assert event.event_type == "node_decommissioned"
        assert event.details["retired_key_count"] == 1


@pytest.mark.asyncio
async def test_decommission_worker_rejects_active_attack_without_partial_changes(session_factory):
    async with session_factory() as session:
        worker, _, _, access_key = await seed_vpn_node(session)
        now = datetime.now(UTC)
        domain = DropDomain(fqdn="busy.example", zone="example", drop_date=now.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now,
            planned_end_at=now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="running",
            )
        )
        await session.commit()

        with pytest.raises(WorkerDecommissionConflictError, match="active domain attack"):
            await decommission_worker(session, worker.id, now=now)
        await session.rollback()

        await session.refresh(worker)
        await session.refresh(access_key)
        assert worker.archived_at is None
        assert worker.control_token == "worker-token"
        assert access_key.status == "active"
        assert access_key.config_uri == "vless://secret-client-uri"


@pytest.mark.asyncio
async def test_decommission_worker_rejects_active_maintenance(session_factory):
    async with session_factory() as session:
        worker, _, _, _ = await seed_vpn_node(session)
        session.add(WorkerMaintenanceJob(worker_id=worker.id, action="vpn_update", status="queued"))
        await session.commit()

        with pytest.raises(WorkerDecommissionConflictError, match="active maintenance"):
            await decommission_worker(session, worker.id)
```

- [ ] **Step 2: Run the tests and verify RED**

Run:

```powershell
python -m pytest tests/test_worker_decommission.py -q -p no:cacheprovider
```

Expected: collection FAIL because `app.services.worker_decommission` does not exist.

- [ ] **Step 3: Implement the focused service**

Create `backend/app/services/worker_decommission.py` with:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    AttackRun,
    VpnAccessKey,
    VpnNodeEvent,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
)
from app.services.vpn_policy import DEVICE_SLOT_STATUSES


class WorkerDecommissionNotFoundError(ValueError):
    pass


class WorkerDecommissionConflictError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WorkerDecommissionResult:
    worker: WorkerNode
    retired_key_count: int


async def decommission_worker(
    session: AsyncSession,
    worker_id: int,
    *,
    now: datetime | None = None,
) -> WorkerDecommissionResult:
    archived_at = now or utcnow()
    worker = await session.scalar(
        select(WorkerNode)
        .where(WorkerNode.id == worker_id, WorkerNode.archived_at.is_(None))
        .with_for_update()
    )
    if worker is None:
        raise WorkerDecommissionNotFoundError("Worker not found")

    active_attack_task_id = await session.scalar(
        select(WorkerTask.id)
        .join(AttackRun, AttackRun.id == WorkerTask.attack_run_id)
        .where(
            WorkerTask.worker_id == worker_id,
            WorkerTask.status.in_(("queued", "planned", "running")),
            AttackRun.status.in_(("planned", "running")),
        )
        .limit(1)
    )
    if active_attack_task_id is not None:
        raise WorkerDecommissionConflictError("Worker is assigned to an active domain attack")

    active_maintenance_id = await session.scalar(
        select(WorkerMaintenanceJob.id)
        .where(
            WorkerMaintenanceJob.worker_id == worker_id,
            WorkerMaintenanceJob.status.in_(("queued", "running")),
        )
        .limit(1)
    )
    if active_maintenance_id is not None:
        raise WorkerDecommissionConflictError("Worker has active maintenance")

    keys = list(
        (
            await session.scalars(
                select(VpnAccessKey)
                .where(
                    VpnAccessKey.worker_id == worker_id,
                    VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
                )
                .with_for_update()
            )
        ).all()
    )
    for access_key in keys:
        access_key.status = "revoked"
        access_key.revoked_at = archived_at
        access_key.config_uri = None
        access_key.last_error = "Node was decommissioned; remote revoke was not confirmed"
        access_key.updated_at = archived_at

    previous_status = worker.status
    previous_vpn_status = worker.vpn_runtime_status
    worker.archived_at = archived_at
    worker.is_enabled = False
    worker.status = "archived"
    worker.control_token = None
    worker.ssh_password = None
    worker.ssh_key_path = None
    worker.vpn_enabled = False
    worker.vpn_role = "none"
    worker.vpn_runtime_status = "decommissioned"
    worker.vpn_panel_password = None
    worker.updated_at = archived_at
    session.add(
        VpnNodeEvent(
            worker_id=worker.id,
            level="warning",
            event_type="node_decommissioned",
            message="Worker and VPN node were removed from active use",
            details={
                "retired_key_count": len(keys),
                "previous_status": previous_status,
                "previous_vpn_status": previous_vpn_status,
                "remote_revoke_confirmed": False,
            },
        )
    )
    await session.flush()
    return WorkerDecommissionResult(worker=worker, retired_key_count=len(keys))
```

- [ ] **Step 4: Run the service tests and verify GREEN**

Run the Step 2 command again. Expected: all tests in `test_worker_decommission.py` pass.

- [ ] **Step 5: Run VPN policy and lifecycle regressions**

```powershell
python -m pytest tests/test_vpn_policy.py tests/test_vpn_lifecycle.py tests/test_vpn_provisioning.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/worker_decommission.py backend/tests/test_worker_decommission.py
git commit -m "feat: safely decommission vpn nodes"
```

### Task 3: Wire the API and Exclude Archived Workers

**Files:**
- Modify: `backend/app/api/routes/control.py:2230-2240,2648-2785,2815-2835`
- Modify: `backend/app/services/vpn_policy.py:150-165`
- Modify: `backend/tests/test_worker_decommission.py`
- Modify: `backend/tests/test_worker_allowlist.py:25-110`

- [ ] **Step 1: Write failing API tests**

Add an ASGI test app with `get_db` and `require_admin` overrides:

```python
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.session import get_db


@pytest_asyncio.fixture
async def api_client(session_factory) -> AsyncIterator[httpx.AsyncClient]:
    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_delete_worker_archives_and_hides_it_from_active_apis(api_client, session_factory, monkeypatch):
    allowlist_worker_counts: list[int] = []

    async def fake_sync(session, settings):
        del settings
        active_count = len(
            (
                await session.scalars(
                    select(WorkerNode).where(WorkerNode.is_enabled.is_(True))
                )
            ).all()
        )
        allowlist_worker_counts.append(active_count)
        return True

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)
    async with session_factory() as session:
        worker, _, _, _ = await seed_vpn_node(session)
        worker_id = worker.id

    response = await api_client.delete(f"/control/workers/{worker_id}")
    assert response.status_code == 200
    assert response.json()["detail"] == "Worker decommissioned"
    assert (await api_client.get("/control/workers")).json() == []
    assert (await api_client.get("/control/vpn/nodes/eligibility")).json() == []
    assert allowlist_worker_counts == [0]


@pytest.mark.asyncio
async def test_delete_worker_returns_409_for_active_attack(api_client, session_factory):
    async with session_factory() as session:
        worker, _, _, access_key = await seed_vpn_node(session)
        worker_id = worker.id
        key_id = access_key.id
        now = datetime.now(UTC)
        domain = DropDomain(fqdn="api-busy.example", zone="example", drop_date=now.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now,
            planned_end_at=now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker_id,
                status="running",
            )
        )
        await session.commit()

    response = await api_client.delete(f"/control/workers/{worker_id}")
    assert response.status_code == 409
    assert response.json()["detail"] == "Worker is assigned to an active domain attack"

    async with session_factory() as session:
        worker = await session.get(WorkerNode, worker_id)
        access_key = await session.get(VpnAccessKey, key_id)
        assert worker is not None
        assert worker.archived_at is None
        assert worker.control_token == "worker-token"
        assert access_key is not None
        assert access_key.status == "active"


@pytest.mark.asyncio
async def test_archived_worker_cannot_be_updated_or_maintained(api_client, session_factory):
    async with session_factory() as session:
        worker, _, _, _ = await seed_vpn_node(session)
        worker_id = worker.id
        await decommission_worker(session, worker_id)
        await session.commit()

    update = await api_client.patch(f"/control/workers/{worker_id}", json={"is_enabled": True})
    maintenance = await api_client.post(f"/control/workers/{worker_id}/maintenance/vpn-check")
    setup = await api_client.get(f"/control/workers/{worker_id}/setup")
    assert update.status_code == 404
    assert maintenance.status_code == 404
    assert setup.status_code == 404
```

These requests stop before scheduling background work. The tests inspect database state instead of mocked return values.

- [ ] **Step 2: Run the API tests and verify RED**

Run:

```powershell
python -m pytest tests/test_worker_decommission.py tests/test_worker_allowlist.py::test_worker_crud_triggers_allowlist_sync -q -p no:cacheprovider
```

Expected: FAIL because DELETE physically removes the row, active lists do not filter archive state, and archived-worker guards are absent.

- [ ] **Step 3: Integrate the decommission service in the route**

Import the service types, replace physical deletion, and preserve a single commit:

```python
from app.services.worker_decommission import (
    WorkerDecommissionConflictError,
    WorkerDecommissionNotFoundError,
    decommission_worker,
)


@router.delete("/workers/{worker_id}", response_model=MessageResponse)
async def delete_worker(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    try:
        result = await decommission_worker(db, worker_id)
    except WorkerDecommissionNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except WorkerDecommissionConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="worker_decommission",
        details=f"worker_id={worker_id} retired_keys={result.retired_key_count}",
    )
    await db.commit()
    await sync_worker_runtime_allowlist(db, get_settings())
    return MessageResponse(detail="Worker decommissioned")
```

- [ ] **Step 4: Filter active lists and VPN selection**

Apply `WorkerNode.archived_at.is_(None)` to `list_workers`, `list_vpn_node_eligibility`, and the base query in `select_vpn_node`:

```python
select(WorkerNode)
.where(WorkerNode.archived_at.is_(None))
.order_by(WorkerNode.name.asc())
```

For ID-specific selection, combine the archive predicate with `WorkerNode.id == worker_id`.

- [ ] **Step 5: Guard setup, update, and maintenance operations**

Use `lock_vpn_worker` for both worker updates and every single-node maintenance action. Immediately after retrieval, reject either missing or archived workers:

```python
worker = await lock_vpn_worker(db, worker_id)
if worker is None or worker.archived_at is not None:
    raise HTTPException(status_code=404, detail="Worker not found")
```

For the read-only setup endpoint, the existing `db.get` can remain, followed by the same archived check. This serializes maintenance creation against decommissioning and prevents direct API reactivation.

- [ ] **Step 6: Update the existing allowlist test expectation**

The current CRUD test counts all physical rows. Change its fake synchronizer to count `WorkerNode.is_enabled.is_(True)` rows. Expected call sequence remains `[1, 1, 0]`, proving the archive removes the node from allowlist input without deleting it.

- [ ] **Step 7: Run API and policy tests and verify GREEN**

```powershell
python -m pytest tests/test_worker_decommission.py tests/test_worker_allowlist.py tests/test_vpn_policy.py tests/test_vpn_control_api.py -q -p no:cacheprovider
```

Expected: all selected tests pass.

- [ ] **Step 8: Commit**

```powershell
git add backend/app/api/routes/control.py backend/app/services/vpn_policy.py backend/tests/test_worker_decommission.py backend/tests/test_worker_allowlist.py
git commit -m "feat: expose safe worker decommission api"
```

### Task 4: Add the VPN-Panel Delete Action

**Files:**
- Modify: `frontend/src/App.tsx:2858-2880,4365-4375,4705-4725`

- [ ] **Step 1: Add a dedicated confirmation handler**

Remove `worker` from the generic `deleteItem` union and add:

```tsx
async function decommissionWorker(worker: WorkerNode) {
  const confirmed = window.confirm(
    `Удалить ноду ${worker.name} из активной системы?\n\n` +
      "Она перестанет использоваться для drop-задач и VPN. Активные ключи будут отозваны локально, " +
      "история сохранится. Удаленный VPS и 3x-UI не изменяются — сервер нужно отдельно удалить или защитить у хостера.",
  );
  if (!confirmed) {
    return;
  }
  try {
    await api.deleteWorker(worker.id);
    await loadAll();
    setToast({ type: "success", text: "Нода удалена из активной системы" });
  } catch (error) {
    setToast({
      type: "error",
      text: error instanceof Error ? error.message : "Ошибка удаления ноды",
    });
  }
}
```

- [ ] **Step 2: Use the handler in both views**

Change the worker-card button to:

```tsx
<button type="button" className="danger" onClick={() => void decommissionWorker(worker)}>
  Удалить
</button>
```

Add to the VPN-node action group:

```tsx
<button type="button" className="danger" onClick={() => void decommissionWorker(worker)}>
  Удалить ноду
</button>
```

- [ ] **Step 3: Run the frontend production build**

The repository has no frontend component-test runner, as recorded in the approved design. Use the strict TypeScript/Vite build for this small UI-only change:

```powershell
npm --prefix frontend run build
```

Expected: TypeScript and Vite complete with exit code 0.

- [ ] **Step 4: Commit**

```powershell
git add frontend/src/App.tsx
git commit -m "feat: add vpn node delete action"
```

### Task 5: Document Operations and Run Full Verification

**Files:**
- Modify: `docs/vpn-service.md`
- Modify: `docs/current-state.md`

- [ ] **Step 1: Document decommission behavior**

Add a `Node Decommission` section to `docs/vpn-service.md` stating:

```markdown
## Node Decommission

Use `Удалить ноду` when a VPS is no longer owned or reachable. The control server archives the worker, clears its control/SSH/3x-UI credentials, removes it from active allocation and the worker allowlist, and locally revokes attached keys while preserving history.

Deletion is blocked during an active domain attack or maintenance job. The action does not contact or destroy the remote VPS. Cancel or destroy the server at the hosting provider separately; local revocation cannot prove removal from a still-running 3x-UI instance.
```

Update `docs/current-state.md` to list safe worker/VPN decommissioning under implemented VPN capabilities and include the final verification commands/results.

- [ ] **Step 2: Run formatting and static checks**

```powershell
cd backend
python -m ruff check app tests
cd ..\worker
python -m ruff check .
cd ..
git diff --check
```

Expected: all commands exit 0 with no findings.

- [ ] **Step 3: Run the full backend suite**

```powershell
cd backend
python -m pytest tests -q -p no:cacheprovider
```

Expected: all tests pass with zero failures.

- [ ] **Step 4: Rebuild the frontend**

```powershell
cd ..
npm --prefix frontend run build
```

Expected: production build exits 0.

- [ ] **Step 5: Commit documentation**

```powershell
git add docs/vpn-service.md docs/current-state.md
git commit -m "docs: document safe vpn node removal"
```

### Task 6: Review, Push, Deploy, and Decommission `automatic-ivory`

**Files:**
- Verify all changed files; no new implementation files in this task.

- [ ] **Step 1: Review the complete branch**

```powershell
git status --short
git diff aea8af5..HEAD --check
git log --oneline -8
```

Expected: only the user's pre-existing untracked CSV and `.tmp-yadrenovpn` items remain outside Git; no whitespace errors.

- [ ] **Step 2: Push the tested commits**

```powershell
git push origin main
```

Expected: `origin/main` advances to the final implementation commit.

- [ ] **Step 3: Run production preflight**

Over SSH, verify:

```bash
git -C /opt/domain-drop-catcher status --short --branch
sudo -u postgres psql -d dropcatcher -Atc "select count(1) from attack_runs where status in ('planned','running');"
systemctl is-active domain-drop-control.service
```

Expected: no tracked server changes, attack count `0`, service `active`. Preserve the existing stash and the untracked `backend/domain_drop_catcher_control.egg-info/` and `gandi-probes/` directories.

- [ ] **Step 4: Deploy application changes**

```bash
git -C /opt/domain-drop-catcher pull --ff-only origin main
cd /opt/domain-drop-catcher/frontend && npm ci && npm run build
systemctl restart domain-drop-control.service
```

Expected: pull fast-forwards, build exits 0, service restarts successfully, startup migration adds `worker_nodes.archived_at`.

- [ ] **Step 5: Verify production before mutation**

```bash
systemctl is-active domain-drop-control.service
curl -fsS http://127.0.0.1:8000/api/health
curl -fsS https://veltrix.qzz.io/api/health
sudo -u postgres psql -d dropcatcher -Atc "select id,name,archived_at from worker_nodes where name='automatic-ivory';"
sudo -u postgres psql -d dropcatcher -Atc "select count(1) from worker_maintenance_jobs where worker_id=(select id from worker_nodes where name='automatic-ivory') and status in ('queued','running');"
```

Expected: both health responses are `ok`, `automatic-ivory` is not yet archived, active maintenance count is `0`, and a repeated attack-count check is also `0`.

- [ ] **Step 6: Archive through the authenticated control API**

Use the normal authenticated admin session in the panel and click `VPN -> VPN ноды -> automatic-ivory -> Удалить ноду`, then accept the confirmation. Do not issue direct SQL updates because the API owns key retirement, audit events, and allowlist synchronization.

Expected: success toast `Нода удалена из активной системы` and the row disappears from both worker and VPN-node views.

- [ ] **Step 7: Verify the production result**

Run read-only checks without selecting secret values:

```bash
sudo -u postgres psql -d dropcatcher -Atc "select name,status,is_enabled,vpn_enabled,vpn_role,vpn_runtime_status,archived_at,control_token is null,ssh_password is null,vpn_panel_password is null from worker_nodes where name='automatic-ivory';"
sudo -u postgres psql -d dropcatcher -Atc "select status,config_uri is null,worker_id,last_error from vpn_access_keys where worker_id=(select id from worker_nodes where name='automatic-ivory') order by id;"
sudo -u postgres psql -d dropcatcher -Atc "select event_type,details->>'retired_key_count' from vpn_node_events where worker_id=(select id from worker_nodes where name='automatic-ivory') order by id desc limit 1;"
curl -fsS https://veltrix.qzz.io/api/health
```

Expected: worker is archived/disabled/decommissioned with cleared credentials, attached key is revoked with no URI, latest event is `node_decommissioned`, and public health remains `ok`.

## Plan Self-Review

- Spec coverage: data retention, credential clearing, key retirement, conflict gates, UI, audit, deployment, and the named production node are each covered by a task.
- Placeholder scan: the plan contains no deferred implementation placeholders; fixtures and database seed steps are written explicitly.
- Type consistency: `archived_at`, `WorkerDecommissionResult.retired_key_count`, error names, endpoint response, statuses, and UI handler names are consistent across tasks.
