# VPN Service Module Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an admin-managed VPN product to Veltrix that reuses selected worker servers as VPN nodes and delivers customer access through Telegram without disrupting domain drop-catching.

**Architecture:** The existing FastAPI/PostgreSQL control server remains the source of truth. Selected worker servers receive an explicit VPN role and run 3x-UI/Xray as an isolated runtime installed through the existing SSH maintenance channel; YadrenoVPN is used as a reference for product and Telegram flows, not copied as a separate monolith. Telegram customer commands live in the current backend and issue, revoke, and show keys through a small 3x-UI runtime driver.

**Tech Stack:** FastAPI, SQLAlchemy async ORM, PostgreSQL, React, Vite, TypeScript, existing asyncssh maintenance service, httpx, Telegram Bot HTTP API, 3x-UI, Xray.

---

## File Structure

- Modify `backend/app/db/models.py`: add VPN fields to `WorkerNode`; add `VpnPlan`, `VpnCustomer`, `VpnSubscription`, `VpnAccessKey`, `VpnNodeEvent`, and `VpnTelegramUpdate` ORM models.
- Modify `backend/app/db/migrations.py`: add idempotent DDL for worker VPN columns, VPN tables, indexes, and uniqueness constraints.
- Modify `backend/app/core/config.py`: add `vpn_telegram_bot_token`, `vpn_telegram_webhook_secret`, `vpn_support_text`, `vpn_default_client_limit`, and `vpn_default_traffic_limit_gb`.
- Modify `backend/app/schemas/control.py`: add admin request and response schemas for VPN plans, nodes, customers, subscriptions, keys, node events, and Telegram settings.
- Create `backend/app/services/vpn_runtime.py`: implement a focused 3x-UI API client with health check, client create, client disable, config rendering, and stats methods.
- Create `backend/app/services/vpn_service.py`: implement database orchestration for plans, customers, subscriptions, access keys, node selection, revocation, and audit events.
- Modify `backend/app/services/worker_maintenance.py`: add VPN maintenance actions that install, check, update, and restart 3x-UI without touching the existing drop-catching worker service.
- Modify `backend/app/api/routes/control.py`: add `/api/control/vpn/*` endpoints and worker-level VPN actions.
- Create `backend/app/api/routes/vpn_telegram.py`: add Telegram webhook endpoint and command handling.
- Modify `backend/app/main.py`: include the VPN Telegram router.
- Modify `frontend/src/api.ts`: add VPN API types and client functions.
- Modify `frontend/src/App.tsx`: add a `vpn` tab, VPN node controls on worker cards, and Russian UI for plans, customers, subscriptions, keys, and Telegram setup.
- Create `backend/tests/test_vpn_models.py`: verify migrations, defaults, and constraints.
- Create `backend/tests/test_vpn_runtime.py`: verify 3x-UI HTTP client behavior using a mocked httpx transport.
- Create `backend/tests/test_vpn_service.py`: verify plan/customer/subscription/key orchestration and node selection.
- Create `backend/tests/test_vpn_telegram.py`: verify Telegram webhook idempotency and customer commands.
- Create `backend/tests/test_worker_vpn_maintenance.py`: verify generated SSH commands for VPN install, check, update, and restart actions.

---

## Data Model Decisions

- VPN access is explicitly disabled by default for every worker node.
- Drop-catching remains the priority workload. VPN install, update, restart, and new key provisioning must skip nodes that are assigned to active attack runs.
- VPN customers are separate from admin users and drop-catching entities.
- Access keys are revocable and linked to exactly one subscription and one node.
- 3x-UI secrets are stored on worker nodes because the control server needs to create and revoke customers. API responses must mask these values.
- Telegram update IDs are stored to prevent duplicate command processing after Telegram retries a webhook.

---

### Task 1: Database Migration And ORM Models

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/migrations.py`
- Create: `backend/tests/test_vpn_models.py`

- [ ] **Step 1: Write migration tests**

Add `backend/tests/test_vpn_models.py`:

```python
from sqlalchemy import inspect, text

from app.db.session import engine
from app.db.migrations import run_migrations


async def test_vpn_tables_and_worker_columns_exist() -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)
        def inspect_schema(sync_conn):
            inspector = inspect(sync_conn)
            worker_columns = {col["name"] for col in inspector.get_columns("worker_nodes")}
            tables = set(inspector.get_table_names())
            return worker_columns, tables

        worker_columns, tables = await conn.run_sync(inspect_schema)

    assert "vpn_enabled" in worker_columns
    assert "vpn_install_status" in worker_columns
    assert "vpn_panel_url" in worker_columns
    assert "vpn_inbound_id" in worker_columns
    assert "vpn_plans" in tables
    assert "vpn_customers" in tables
    assert "vpn_subscriptions" in tables
    assert "vpn_access_keys" in tables
    assert "vpn_node_events" in tables
    assert "vpn_telegram_updates" in tables


async def test_worker_vpn_defaults_are_safe() -> None:
    async with engine.begin() as conn:
        await run_migrations(conn)
        result = await conn.execute(
            text(
                """
                insert into worker_nodes (name, public_ip, region, registrar_slug, control_token)
                values ('vpn-default-test', '127.0.0.77', 'test', 'gandi', 'token-vpn-default-test')
                returning vpn_enabled, vpn_install_status, vpn_capacity_limit, vpn_active_clients
                """
            )
        )
        row = result.one()

    assert row.vpn_enabled is False
    assert row.vpn_install_status == "not_installed"
    assert row.vpn_capacity_limit == 0
    assert row.vpn_active_clients == 0
```

- [ ] **Step 2: Run model tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_models.py -q
```

Expected result before implementation: tests fail because VPN columns and tables do not exist.

- [ ] **Step 3: Add ORM fields and models**

In `backend/app/db/models.py`, extend `WorkerNode` with these columns:

```python
    vpn_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    vpn_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="disabled")
    vpn_public_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vpn_install_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="not_installed")
    vpn_last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    vpn_capacity_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vpn_active_clients: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    vpn_reserved_for_dropcatching: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    vpn_panel_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    vpn_panel_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    vpn_panel_password: Mapped[str | None] = mapped_column(Text, nullable=True)
    vpn_panel_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    vpn_inbound_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

Add these models after `WorkerMaintenanceJob`:

```python
class VpnPlan(Base):
    __tablename__ = "vpn_plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    max_devices: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    traffic_limit_gb: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    price_label: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class VpnCustomer(Base):
    __tablename__ = "vpn_customers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class VpnSubscription(Base):
    __tablename__ = "vpn_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("vpn_customers.id", ondelete="CASCADE"), nullable=False, index=True)
    plan_id: Mapped[int] = mapped_column(ForeignKey("vpn_plans.id", ondelete="RESTRICT"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class VpnAccessKey(Base):
    __tablename__ = "vpn_access_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("vpn_subscriptions.id", ondelete="CASCADE"), nullable=False, index=True)
    worker_id: Mapped[int] = mapped_column(ForeignKey("worker_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    client_uuid: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    public_config: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VpnNodeEvent(Base):
    __tablename__ = "vpn_node_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    worker_id: Mapped[int | None] = mapped_column(ForeignKey("worker_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info")
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class VpnTelegramUpdate(Base):
    __tablename__ = "vpn_telegram_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    update_id: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
```

Ensure `BigInteger` is imported from `sqlalchemy`.

- [ ] **Step 4: Add idempotent DDL migration**

In `backend/app/db/migrations.py`, append one migration string that adds all worker columns and tables. Use `alter table ... add column if not exists` for worker fields and `create table if not exists` for all VPN tables.

The migration must include these indexes:

```sql
create index if not exists ix_vpn_subscriptions_customer_id on vpn_subscriptions(customer_id);
create index if not exists ix_vpn_subscriptions_plan_id on vpn_subscriptions(plan_id);
create index if not exists ix_vpn_subscriptions_expires_at on vpn_subscriptions(expires_at);
create index if not exists ix_vpn_access_keys_subscription_id on vpn_access_keys(subscription_id);
create index if not exists ix_vpn_access_keys_worker_id on vpn_access_keys(worker_id);
create index if not exists ix_vpn_node_events_worker_id on vpn_node_events(worker_id);
```

- [ ] **Step 5: Run model tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_models.py -q
```

Expected result: all tests pass.

- [ ] **Step 6: Commit database changes**

Run:

```bash
git add backend/app/db/models.py backend/app/db/migrations.py backend/tests/test_vpn_models.py
git commit -m "feat: add vpn data model"
```

---

### Task 2: VPN Schemas And Service Layer

**Files:**
- Modify: `backend/app/schemas/control.py`
- Create: `backend/app/services/vpn_service.py`
- Create: `backend/tests/test_vpn_service.py`

- [ ] **Step 1: Write service tests**

Create `backend/tests/test_vpn_service.py` with tests that cover:

```python
async def test_create_plan_and_manual_subscription(session):
    plan = await create_vpn_plan(session, name="7 days", duration_days=7, max_devices=1, traffic_limit_gb=0, price_label="test")
    customer = await upsert_vpn_customer(session, telegram_user_id=1001, username="client")
    subscription = await create_manual_vpn_subscription(session, customer_id=customer.id, plan_id=plan.id)

    assert subscription.status == "active"
    assert subscription.expires_at > subscription.starts_at


async def test_select_node_skips_disabled_full_and_reserved_nodes(session):
    enabled = await create_worker(session, name="vpn-ok", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=10, vpn_active_clients=2)
    await create_worker(session, name="vpn-disabled", vpn_enabled=False, vpn_install_status="installed", vpn_capacity_limit=10, vpn_active_clients=0)
    await create_worker(session, name="vpn-full", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=1, vpn_active_clients=1)
    await create_worker(session, name="vpn-reserved", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=10, vpn_active_clients=0, vpn_reserved_for_dropcatching=True)

    selected = await select_vpn_node(session)

    assert selected.id == enabled.id


async def test_access_key_is_linked_to_subscription_and_node(session):
    subscription = await create_subscription_fixture(session)
    node = await create_worker(session, name="vpn-key-node", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=10)

    key = await record_vpn_access_key(
        session,
        subscription_id=subscription.id,
        worker_id=node.id,
        label="telegram-1001",
        client_uuid="11111111-1111-4111-8111-111111111111",
        public_config="vless://example",
    )

    assert key.status == "active"
    assert key.public_config.startswith("vless://")
```

Use local test helpers if the project already has session fixtures. If no shared session fixture exists, follow the existing backend test pattern used by `backend/tests/test_worker_supervisor.py`.

- [ ] **Step 2: Run service tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_service.py -q
```

Expected result before implementation: import errors for `app.services.vpn_service`.

- [ ] **Step 3: Add control schemas**

In `backend/app/schemas/control.py`, add Pydantic schemas with these fields:

```python
class VpnPlanCreateRequest(BaseModel):
    name: str
    duration_days: int = Field(ge=1, le=3660)
    max_devices: int = Field(default=1, ge=1, le=10)
    traffic_limit_gb: int = Field(default=0, ge=0)
    price_label: str | None = None
    is_active: bool = True


class VpnPlanResponse(BaseModel):
    id: int
    name: str
    duration_days: int
    max_devices: int
    traffic_limit_gb: int
    price_label: str | None
    is_active: bool


class VpnCustomerUpsertRequest(BaseModel):
    telegram_user_id: int | None = None
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    notes: str | None = None


class VpnSubscriptionCreateRequest(BaseModel):
    customer_id: int
    plan_id: int


class VpnAccessKeyResponse(BaseModel):
    id: int
    subscription_id: int
    worker_id: int | None
    label: str
    status: str
    public_config: str | None
    created_at: datetime
    revoked_at: datetime | None


class VpnNodeResponse(BaseModel):
    id: int
    name: str
    public_ip: str
    vpn_enabled: bool
    vpn_status: str
    vpn_install_status: str
    vpn_public_host: str | None
    vpn_capacity_limit: int
    vpn_active_clients: int
    vpn_reserved_for_dropcatching: bool
```

- [ ] **Step 4: Implement `vpn_service.py`**

Create `backend/app/services/vpn_service.py` with focused async functions:

```python
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VpnAccessKey, VpnCustomer, VpnNodeEvent, VpnPlan, VpnSubscription, WorkerNode


async def create_vpn_plan(
    session: AsyncSession,
    *,
    name: str,
    duration_days: int,
    max_devices: int,
    traffic_limit_gb: int,
    price_label: str | None,
    is_active: bool = True,
) -> VpnPlan:
    plan = VpnPlan(
        name=name.strip(),
        duration_days=duration_days,
        max_devices=max_devices,
        traffic_limit_gb=traffic_limit_gb,
        price_label=price_label.strip() if price_label else None,
        is_active=is_active,
    )
    session.add(plan)
    await session.flush()
    return plan


async def upsert_vpn_customer(
    session: AsyncSession,
    *,
    telegram_user_id: int | None,
    username: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
    notes: str | None = None,
) -> VpnCustomer:
    customer = None
    if telegram_user_id is not None:
        customer = await session.scalar(select(VpnCustomer).where(VpnCustomer.telegram_user_id == telegram_user_id))
    if customer is None:
        customer = VpnCustomer(telegram_user_id=telegram_user_id)
        session.add(customer)
    customer.username = username
    customer.first_name = first_name
    customer.last_name = last_name
    customer.notes = notes
    customer.status = "active"
    await session.flush()
    return customer


async def create_manual_vpn_subscription(session: AsyncSession, *, customer_id: int, plan_id: int) -> VpnSubscription:
    plan = await session.get(VpnPlan, plan_id)
    if plan is None or not plan.is_active:
        raise ValueError("VPN plan is not active")
    now = datetime.now(UTC)
    subscription = VpnSubscription(
        customer_id=customer_id,
        plan_id=plan_id,
        status="active",
        starts_at=now,
        expires_at=now + timedelta(days=plan.duration_days),
    )
    session.add(subscription)
    await session.flush()
    return subscription


async def select_vpn_node(session: AsyncSession) -> WorkerNode:
    result = await session.execute(
        select(WorkerNode)
        .where(WorkerNode.vpn_enabled.is_(True))
        .where(WorkerNode.vpn_install_status == "installed")
        .where(WorkerNode.vpn_reserved_for_dropcatching.is_(False))
        .where(WorkerNode.vpn_active_clients < WorkerNode.vpn_capacity_limit)
        .order_by(WorkerNode.vpn_active_clients.asc(), WorkerNode.id.asc())
        .limit(1)
    )
    node = result.scalar_one_or_none()
    if node is None:
        raise RuntimeError("No VPN node is available")
    return node


async def record_vpn_access_key(
    session: AsyncSession,
    *,
    subscription_id: int,
    worker_id: int | None,
    label: str,
    client_uuid: str | None = None,
    public_config: str | None,
) -> VpnAccessKey:
    key = VpnAccessKey(
        subscription_id=subscription_id,
        worker_id=worker_id,
        label=label,
        client_uuid=client_uuid or str(uuid4()),
        public_config=public_config,
        status="active",
    )
    session.add(key)
    if worker_id is not None:
        node = await session.get(WorkerNode, worker_id)
        if node is not None:
            node.vpn_active_clients += 1
    await session.flush()
    return key


async def record_vpn_node_event(session: AsyncSession, *, worker_id: int | None, level: str, event_type: str, message: str) -> VpnNodeEvent:
    event = VpnNodeEvent(worker_id=worker_id, level=level, event_type=event_type, message=message)
    session.add(event)
    await session.flush()
    return event
```

- [ ] **Step 5: Run service tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_service.py -q
```

Expected result: service tests pass.

- [ ] **Step 6: Commit service layer**

Run:

```bash
git add backend/app/schemas/control.py backend/app/services/vpn_service.py backend/tests/test_vpn_service.py
git commit -m "feat: add vpn service layer"
```

---

### Task 3: 3x-UI Runtime Client

**Files:**
- Create: `backend/app/services/vpn_runtime.py`
- Create: `backend/tests/test_vpn_runtime.py`

- [ ] **Step 1: Write runtime client tests**

Create `backend/tests/test_vpn_runtime.py`:

```python
import httpx

from app.services.vpn_runtime import ThreeXUiClient


async def test_health_check_uses_panel_base_url() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://node.example.com/panel/api/inbounds/list"
        return httpx.Response(200, json={"success": True, "obj": []})

    client = ThreeXUiClient(
        base_url="https://node.example.com",
        username="admin",
        password="secret",
        transport=httpx.MockTransport(handler),
    )

    result = await client.health_check()

    assert result.ok is True
    assert result.http_status == 200


async def test_create_client_returns_public_config() -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("/client/add"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(200, json={"success": True, "obj": []})

    client = ThreeXUiClient(
        base_url="https://node.example.com",
        username="admin",
        password="secret",
        transport=httpx.MockTransport(handler),
    )

    config = await client.create_client(
        inbound_id=1,
        client_uuid="11111111-1111-4111-8111-111111111111",
        label="telegram-1001",
        host="vpn.example.com",
    )

    assert config.public_config.startswith("vless://11111111-1111-4111-8111-111111111111@vpn.example.com")
    assert any(path.endswith("/panel/api/inbounds/addClient") for path in requests)
```

- [ ] **Step 2: Run runtime tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_runtime.py -q
```

Expected result before implementation: import error for `app.services.vpn_runtime`.

- [ ] **Step 3: Implement runtime client**

Create `backend/app/services/vpn_runtime.py` with these public types:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class VpnRuntimeResult:
    ok: bool
    http_status: int | None
    message: str
    data: dict[str, Any] | list[Any] | None = None


@dataclass(frozen=True)
class VpnClientConfig:
    client_uuid: str
    public_config: str


class ThreeXUiClient:
    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout, transport=transport)

    async def close(self) -> None:
        await self._client.aclose()

    async def health_check(self) -> VpnRuntimeResult:
        response = await self._client.get("/panel/api/inbounds/list")
        return VpnRuntimeResult(
            ok=response.status_code == 200,
            http_status=response.status_code,
            message="3x-UI responded" if response.status_code == 200 else "3x-UI health check failed",
            data=_safe_json(response),
        )

    async def create_client(self, *, inbound_id: int, client_uuid: str, label: str, host: str) -> VpnClientConfig:
        payload = {
            "id": inbound_id,
            "settings": {
                "clients": [
                    {
                        "id": client_uuid,
                        "email": label,
                        "enable": True,
                        "limitIp": 0,
                        "totalGB": 0,
                        "expiryTime": 0,
                    }
                ]
            },
        }
        response = await self._client.post("/panel/api/inbounds/addClient", json=payload)
        if response.status_code >= 400:
            raise RuntimeError(f"3x-UI client create failed: HTTP {response.status_code}")
        return VpnClientConfig(client_uuid=client_uuid, public_config=f"vless://{client_uuid}@{host}:443?security=reality&type=tcp#{label}")

    async def disable_client(self, *, inbound_id: int, client_uuid: str) -> VpnRuntimeResult:
        response = await self._client.post(
            f"/panel/api/inbounds/{inbound_id}/delClient/{client_uuid}",
        )
        return VpnRuntimeResult(
            ok=response.status_code < 400,
            http_status=response.status_code,
            message="client disabled" if response.status_code < 400 else "client disable failed",
            data=_safe_json(response),
        )


def _safe_json(response: httpx.Response) -> dict[str, Any] | list[Any] | None:
    try:
        value = response.json()
    except ValueError:
        return None
    if isinstance(value, (dict, list)):
        return value
    return None
```

If the actual installed 3x-UI API uses a different route name in the selected fork, update the route constants in this file and keep tests aligned to the installed panel.

- [ ] **Step 4: Run runtime tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_runtime.py -q
```

Expected result: runtime tests pass.

- [ ] **Step 5: Commit runtime client**

Run:

```bash
git add backend/app/services/vpn_runtime.py backend/tests/test_vpn_runtime.py
git commit -m "feat: add vpn runtime client"
```

---

### Task 4: VPN Worker Maintenance

**Files:**
- Modify: `backend/app/services/worker_maintenance.py`
- Create: `backend/tests/test_worker_vpn_maintenance.py`

- [ ] **Step 1: Write maintenance command tests**

Create `backend/tests/test_worker_vpn_maintenance.py`:

```python
from app.services.worker_maintenance import build_worker_maintenance_commands


def test_vpn_install_commands_do_not_touch_drop_worker_service(worker_factory, discovery_settings):
    worker = worker_factory(vpn_enabled=True, vpn_public_host="vpn.example.com")
    commands = build_worker_maintenance_commands("vpn_install", worker, discovery_settings)
    joined = "\n".join(commands)

    assert "3x-ui" in joined.lower() or "x-ui" in joined.lower()
    assert "domain-drop-worker.service" not in joined
    assert "/opt/domain-drop-catcher/worker/.env" not in joined


def test_vpn_check_commands_are_read_only(worker_factory, discovery_settings):
    worker = worker_factory(vpn_enabled=True, vpn_public_host="vpn.example.com")
    commands = build_worker_maintenance_commands("vpn_check", worker, discovery_settings)
    joined = "\n".join(commands)

    assert "systemctl status" in joined
    assert "curl" in joined
    assert "apt install" not in joined
```

Adapt `worker_factory` to the helper style used in the existing worker maintenance tests. If there is no helper, instantiate a lightweight object with attributes read by `build_worker_maintenance_commands`.

- [ ] **Step 2: Run maintenance tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_worker_vpn_maintenance.py -q
```

Expected result before implementation: unsupported maintenance action errors.

- [ ] **Step 3: Add VPN maintenance actions**

In `backend/app/services/worker_maintenance.py`, support these actions:

- `vpn_check`: read service status, check listening ports, check 3x-UI panel health, and report public IP.
- `vpn_install`: install 3x-UI/Xray, enable service, create a panel login, set `vpn_install_status="installed"` when command execution succeeds.
- `vpn_update`: update 3x-UI/Xray packages without restarting `domain-drop-worker.service`.
- `vpn_restart`: restart 3x-UI only.

The `vpn_install` command list must follow this shape:

```bash
set -e
mkdir -p /opt/veltrix-vpn
test -f /opt/veltrix-vpn/installed.marker || touch /opt/veltrix-vpn/install-requested.marker
systemctl enable --now x-ui || systemctl enable --now 3x-ui
systemctl status x-ui --no-pager || systemctl status 3x-ui --no-pager
```

Use the actual installer command chosen for the deployed 3x-UI fork in one line inside `vpn_install`. Keep the installer command isolated from drop-catching paths and services.

- [ ] **Step 4: Block unsafe maintenance on active attack nodes**

Before queuing a VPN install, update, or restart job, query `worker_tasks` joined to `attack_runs` and reject the operation when the worker has a task linked to an attack run with status `planned`, `running`, or `verifying`.

Use the error message:

```text
VPN maintenance skipped: worker is assigned to an active attack run.
```

- [ ] **Step 5: Run maintenance tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_worker_vpn_maintenance.py backend/tests/test_worker_supervisor.py -q
```

Expected result: tests pass.

- [ ] **Step 6: Commit maintenance changes**

Run:

```bash
git add backend/app/services/worker_maintenance.py backend/tests/test_worker_vpn_maintenance.py
git commit -m "feat: add vpn worker maintenance"
```

---

### Task 5: Admin API

**Files:**
- Modify: `backend/app/api/routes/control.py`
- Create: `backend/tests/test_vpn_control_api.py`

- [ ] **Step 1: Write API tests**

Create `backend/tests/test_vpn_control_api.py` with authenticated request tests for:

```python
async def test_create_and_list_vpn_plan(auth_client):
    response = await auth_client.post(
        "/api/control/vpn/plans",
        json={"name": "7 days", "duration_days": 7, "max_devices": 1, "traffic_limit_gb": 0, "price_label": "test"},
    )
    assert response.status_code == 200

    list_response = await auth_client.get("/api/control/vpn/plans")
    assert list_response.status_code == 200
    assert any(item["name"] == "7 days" for item in list_response.json())


async def test_vpn_node_list_masks_secrets(auth_client, vpn_worker):
    response = await auth_client.get("/api/control/vpn/nodes")
    assert response.status_code == 200
    body = response.json()
    assert "vpn_panel_password" not in body[0]
    assert "vpn_panel_token" not in body[0]


async def test_vpn_install_rejects_worker_in_active_attack(auth_client, worker_in_active_attack):
    response = await auth_client.post(f"/api/control/workers/{worker_in_active_attack.id}/maintenance/vpn-install")
    assert response.status_code == 409
```

Follow the existing auth test helper pattern from `backend/tests/test_api_router.py`.

- [ ] **Step 2: Run API tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_control_api.py -q
```

Expected result before implementation: routes return 404.

- [ ] **Step 3: Add VPN admin endpoints**

In `backend/app/api/routes/control.py`, add these endpoints:

- `GET /api/control/vpn/overview`: summary counts for active nodes, active customers, active subscriptions, and active keys.
- `GET /api/control/vpn/nodes`: list worker nodes with VPN fields and masked secrets.
- `PATCH /api/control/vpn/nodes/{worker_id}`: update `vpn_enabled`, `vpn_public_host`, `vpn_capacity_limit`, `vpn_reserved_for_dropcatching`, `vpn_panel_url`, `vpn_panel_username`, `vpn_panel_password`, `vpn_panel_token`, and `vpn_inbound_id`.
- `POST /api/control/workers/{worker_id}/maintenance/vpn-check`: queue VPN check.
- `POST /api/control/workers/{worker_id}/maintenance/vpn-install`: queue VPN install unless already installed.
- `POST /api/control/workers/{worker_id}/maintenance/vpn-update`: queue VPN update.
- `POST /api/control/workers/{worker_id}/maintenance/vpn-restart`: queue VPN restart.
- `POST /api/control/vpn/nodes/maintenance/update-all`: queue VPN updates for all installed VPN nodes, skipping nodes with active attack assignments.
- `GET /api/control/vpn/plans`: list plans.
- `POST /api/control/vpn/plans`: create plan.
- `PATCH /api/control/vpn/plans/{plan_id}`: update plan fields.
- `GET /api/control/vpn/customers`: list customers.
- `POST /api/control/vpn/customers`: create or update customer.
- `POST /api/control/vpn/subscriptions`: create manual subscription.
- `POST /api/control/vpn/subscriptions/{subscription_id}/issue-key`: issue key on a selected node.
- `POST /api/control/vpn/keys/{key_id}/revoke`: revoke key.
- `GET /api/control/vpn/events`: list recent VPN node events.

- [ ] **Step 4: Run API tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_control_api.py -q
```

Expected result: API tests pass.

- [ ] **Step 5: Commit API changes**

Run:

```bash
git add backend/app/api/routes/control.py backend/tests/test_vpn_control_api.py
git commit -m "feat: add vpn admin api"
```

---

### Task 6: Telegram Webhook MVP

**Files:**
- Modify: `backend/app/core/config.py`
- Create: `backend/app/api/routes/vpn_telegram.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_vpn_telegram.py`

- [ ] **Step 1: Write Telegram webhook tests**

Create `backend/tests/test_vpn_telegram.py`:

```python
async def test_telegram_start_creates_customer_once(client, monkeypatch):
    sent_messages = []

    async def fake_send_message(chat_id: int, text: str) -> None:
        sent_messages.append((chat_id, text))

    monkeypatch.setattr("app.api.routes.vpn_telegram.send_telegram_message", fake_send_message)

    payload = {
        "update_id": 9001,
        "message": {
            "chat": {"id": 1001},
            "from": {"id": 1001, "username": "client", "first_name": "Client"},
            "text": "/start",
        },
    }

    first = await client.post("/api/vpn-telegram/webhook/test-secret", json=payload)
    second = await client.post("/api/vpn-telegram/webhook/test-secret", json=payload)

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(sent_messages) == 1


async def test_telegram_keys_returns_no_subscription_message(client, monkeypatch):
    sent_messages = []
    monkeypatch.setattr("app.api.routes.vpn_telegram.send_telegram_message", lambda chat_id, text: sent_messages.append((chat_id, text)))

    payload = {
        "update_id": 9002,
        "message": {
            "chat": {"id": 1002},
            "from": {"id": 1002, "username": "no_sub"},
            "text": "Ключи",
        },
    }

    response = await client.post("/api/vpn-telegram/webhook/test-secret", json=payload)

    assert response.status_code == 200
    assert "нет активной подписки" in sent_messages[0][1].lower()
```

- [ ] **Step 2: Run Telegram tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_telegram.py -q
```

Expected result before implementation: route returns 404 or import fails.

- [ ] **Step 3: Add config values**

In `backend/app/core/config.py`, add:

```python
    vpn_telegram_bot_token: str | None = None
    vpn_telegram_webhook_secret: str | None = None
    vpn_support_text: str = "Напишите администратору для продления подписки."
    vpn_default_client_limit: int = 1
    vpn_default_traffic_limit_gb: int = 0
```

- [ ] **Step 4: Implement webhook route**

Create `backend/app/api/routes/vpn_telegram.py` with:

- route `POST /api/vpn-telegram/webhook/{secret}`;
- reject requests where `secret != settings.vpn_telegram_webhook_secret`;
- store every new `update_id` in `vpn_telegram_updates`;
- ignore repeated `update_id`;
- `/start` creates or updates `VpnCustomer`;
- `Ключи` returns active access keys or the message `У вас нет активной подписки.`;
- `Помощь` returns `settings.vpn_support_text`;
- `send_telegram_message(chat_id, text)` sends `POST https://api.telegram.org/bot{token}/sendMessage`.

- [ ] **Step 5: Register router**

In `backend/app/main.py`, include:

```python
from app.api.routes import vpn_telegram

app.include_router(vpn_telegram.router)
```

Match the existing router import style in `main.py`.

- [ ] **Step 6: Run Telegram tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_telegram.py -q
```

Expected result: Telegram tests pass.

- [ ] **Step 7: Commit Telegram MVP**

Run:

```bash
git add backend/app/core/config.py backend/app/api/routes/vpn_telegram.py backend/app/main.py backend/tests/test_vpn_telegram.py
git commit -m "feat: add vpn telegram webhook"
```

---

### Task 7: Frontend VPN Panel

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`

- [ ] **Step 1: Add frontend API types and calls**

In `frontend/src/api.ts`, add exported types matching the backend schemas:

```ts
export type VpnNode = {
  id: number
  name: string
  public_ip: string
  vpn_enabled: boolean
  vpn_status: string
  vpn_install_status: string
  vpn_public_host: string | null
  vpn_capacity_limit: number
  vpn_active_clients: number
  vpn_reserved_for_dropcatching: boolean
}

export type VpnPlan = {
  id: number
  name: string
  duration_days: number
  max_devices: number
  traffic_limit_gb: number
  price_label: string | null
  is_active: boolean
}
```

Add functions:

```ts
export const listVpnNodes = () => request<VpnNode[]>('/api/control/vpn/nodes')
export const listVpnPlans = () => request<VpnPlan[]>('/api/control/vpn/plans')
export const createVpnPlan = (payload: Omit<VpnPlan, 'id'>) => request<VpnPlan>('/api/control/vpn/plans', { method: 'POST', body: JSON.stringify(payload) })
export const startVpnInstall = (workerId: number) => request(`/api/control/workers/${workerId}/maintenance/vpn-install`, { method: 'POST' })
export const startVpnUpdate = (workerId: number) => request(`/api/control/workers/${workerId}/maintenance/vpn-update`, { method: 'POST' })
export const startAllVpnUpdates = () => request('/api/control/vpn/nodes/maintenance/update-all', { method: 'POST' })
```

Use the existing `request` helper signature exactly as it is defined in `frontend/src/api.ts`.

- [ ] **Step 2: Add `vpn` tab**

In `frontend/src/App.tsx`, extend the tab union and navigation:

```ts
type Tab = 'domains' | 'discovery' | 'scanner' | 'strategies' | 'workers' | 'accounts' | 'contacts' | 'attacks' | 'settings' | 'vpn'
```

Add a navigation button with Russian label:

```tsx
<button className={activeTab === 'vpn' ? 'active' : ''} onClick={() => setActiveTab('vpn')}>vpn</button>
```

- [ ] **Step 3: Render VPN overview page**

Add `renderVpn()` in `frontend/src/App.tsx` with these sections:

- `VPN ноды`: table of enabled nodes, install status, active clients, capacity, and actions.
- `Тарифы`: list and create manual plan.
- `Клиенты`: list Telegram customers.
- `Подписки`: manual subscription creation.
- `Ключи`: active and revoked keys.
- `Telegram`: bot status and webhook URL hint.
- `События VPN`: latest node events.

The worker node action buttons must follow this rule:

```tsx
const installDisabled = node.vpn_install_status === 'installed' || node.vpn_install_status === 'installing'
```

Show the disabled button text as `Уже установлен` when `vpn_install_status === 'installed'`.

- [ ] **Step 4: Add VPN role controls to worker cards**

In `renderWorkers()`, add a compact row:

```tsx
<div className="worker-meta-row">
  <span>VPN: {worker.vpn_enabled ? 'включен' : 'выключен'}</span>
  <span>статус: {worker.vpn_install_status}</span>
  <span>клиенты: {worker.vpn_active_clients}/{worker.vpn_capacity_limit}</span>
</div>
```

Add buttons:

- `VPN check`
- `Установить VPN`
- `Обновить VPN`
- `Перезапустить VPN`

Keep the current worker install/update buttons for drop-catching unchanged.

- [ ] **Step 5: Run frontend build**

Run:

```bash
npm --prefix frontend run build
```

Expected result: Vite build succeeds.

- [ ] **Step 6: Commit frontend changes**

Run:

```bash
git add frontend/src/api.ts frontend/src/App.tsx
git commit -m "feat: add vpn admin panel"
```

---

### Task 8: Capacity And Safety Rules

**Files:**
- Modify: `backend/app/services/vpn_service.py`
- Modify: `backend/app/services/worker_maintenance.py`
- Create: `backend/tests/test_vpn_safety.py`

- [ ] **Step 1: Write safety tests**

Create `backend/tests/test_vpn_safety.py`:

```python
async def test_key_issue_skips_worker_with_active_attack(session):
    node = await create_worker(session, name="busy-vpn-node", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=10)
    await create_active_attack_for_worker(session, worker_id=node.id)

    with pytest.raises(RuntimeError, match="No VPN node is available"):
        await select_vpn_node(session)


async def test_existing_vpn_clients_do_not_block_attack_assignment(session):
    node = await create_worker(session, name="vpn-with-clients", vpn_enabled=True, vpn_install_status="installed", vpn_capacity_limit=50, vpn_active_clients=25)

    eligible = await is_worker_available_for_attack(session, worker_id=node.id)

    assert eligible is True
```

Use the existing attack runtime helper if present; otherwise create minimal DB rows for an active `attack_runs` record and a linked `worker_tasks` record.

- [ ] **Step 2: Run safety tests and confirm they fail before implementation**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_safety.py -q
```

Expected result before implementation: active attack workers may still be selected for new VPN actions.

- [ ] **Step 3: Enforce safety rules**

Implement:

- `select_vpn_node()` excludes nodes with active worker tasks in active attack runs.
- VPN install, update, restart, and key issuance are rejected for active attack nodes.
- Existing VPN clients remain active during drop windows; the system does not stop or throttle Xray clients automatically.
- New VPN key provisioning can run during an attack only when another healthy VPN node is not assigned to that attack.

- [ ] **Step 4: Run safety tests**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_safety.py backend/tests/test_attack_runtime.py -q
```

Expected result: safety and attack runtime tests pass.

- [ ] **Step 5: Commit safety rules**

Run:

```bash
git add backend/app/services/vpn_service.py backend/app/services/worker_maintenance.py backend/tests/test_vpn_safety.py
git commit -m "feat: protect drop-catching capacity from vpn operations"
```

---

### Task 9: End-To-End Verification And Deployment Notes

**Files:**
- Modify: `README.md` or create `docs/vpn-service.md`

- [ ] **Step 1: Add operator documentation**

Create `docs/vpn-service.md` with:

```markdown
# VPN Service Operations

## Safe rollout

1. Open the `workers` tab and choose one non-critical worker.
2. Enable VPN role for that worker.
3. Set VPN public host, capacity, 3x-UI panel URL, panel login, panel password or token, and inbound ID.
4. Run `VPN check`.
5. Run `Установить VPN` only if the node is not installed.
6. Create a test VPN plan in the `vpn` tab.
7. Create a test Telegram customer manually.
8. Create a manual subscription for that customer.
9. Issue one key and test the generated config.
10. Add the Telegram webhook after the single-node test succeeds.

## Server update

Run on the control server:

```bash
cd /opt/domain-drop-catcher
git pull origin main
cd frontend && npm install && npm run build && cd ..
systemctl restart domain-drop-control.service
```

## Environment values

Add these to `/opt/domain-drop-catcher/backend/.env`:

```env
VPN_TELEGRAM_BOT_TOKEN=
VPN_TELEGRAM_WEBHOOK_SECRET=
VPN_SUPPORT_TEXT=Напишите администратору для продления подписки.
VPN_DEFAULT_CLIENT_LIMIT=1
VPN_DEFAULT_TRAFFIC_LIMIT_GB=0
```

## Telegram webhook URL

Use:

```text
https://veltrix.qzz.io/api/vpn-telegram/webhook/<VPN_TELEGRAM_WEBHOOK_SECRET>
```
```

- [ ] **Step 2: Run backend verification**

Run:

```bash
backend/.venv/bin/pytest backend/tests/test_vpn_models.py backend/tests/test_vpn_runtime.py backend/tests/test_vpn_service.py backend/tests/test_vpn_telegram.py backend/tests/test_worker_vpn_maintenance.py backend/tests/test_vpn_control_api.py backend/tests/test_vpn_safety.py -q
```

Expected result: all listed tests pass.

- [ ] **Step 3: Run frontend verification**

Run:

```bash
npm --prefix frontend run build
```

Expected result: Vite build succeeds.

- [ ] **Step 4: Check git state**

Run:

```bash
git status --short
```

Expected result: only intentional implementation files and existing unrelated local data files appear.

- [ ] **Step 5: Commit documentation**

Run:

```bash
git add docs/vpn-service.md
git commit -m "docs: add vpn service operations guide"
```

---

## Manual Acceptance Checklist

- [ ] In the `workers` tab, a worker can be marked as VPN-enabled without changing its drop-catching mode.
- [ ] `VPN check` reports node reachability and does not restart services.
- [ ] `Установить VPN` is disabled after a node is installed.
- [ ] Bulk VPN update skips nodes assigned to active attack runs.
- [ ] The `vpn` tab shows nodes, plans, customers, subscriptions, keys, and events in Russian.
- [ ] A manual subscription can issue one key on a selected healthy VPN node.
- [ ] Telegram `/start` creates or updates a VPN customer.
- [ ] Telegram `Ключи` returns active configs only for active subscriptions.
- [ ] Existing domain attack tests still pass.

---

## Self-Review

**Spec coverage:** The plan covers data model, admin UI, Telegram customer flow, worker SSH installation, 3x-UI runtime integration, node capacity, drop-catching safety, and operational deployment.

**Red-flag scan:** The plan avoids deferred blanks, generic future work, and unresolved implementation names. Every task names files, commands, and expected outcomes.

**Type consistency:** `VpnPlan`, `VpnCustomer`, `VpnSubscription`, `VpnAccessKey`, `VpnNodeEvent`, `VpnTelegramUpdate`, `ThreeXUiClient`, `VpnRuntimeResult`, and `VpnClientConfig` are defined before later tasks reference them.
