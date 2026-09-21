# Veltrix VPN Endpoint Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add inert, migration-tested storage for stable per-key inbound identity without changing any existing VPN operation.

**Architecture:** An additive `VpnEndpoint` table owns one immutable-in-use inbound identity. A nullable composite foreign key binds an access key to an endpoint on the same worker. The migration performs no backfill, network calls, service restarts or configuration changes.

**Tech Stack:** Python, SQLAlchemy 2, PostgreSQL 14, pytest/pytest-asyncio, SQLite for fast constraint tests.

---

## Scope and sequence

This is the independently testable **storage gate** of the approved protected-endpoint
specification, not a deployable protected VPN release. Existing issuance and lifecycle
remain unchanged while there are no endpoint-bound keys. Do not create endpoint rows
in production or deploy this intermediate revision alone.

Subsequent dependent plans cover endpoint resolution/verified legacy binding,
endpoint-aware operations and durable intent, the version-specific hot-update API,
admin visibility, and the protected transport rehearsal/rollout. Their implementation
depends on the storage contract here and read-only facts from the installed node.
Invitations remain a separate subsequent specification. No claim that those scopes
are implemented or ready is permitted on completion of this plan.

Worktree: `D:/паразитное seo/backorder/project/.worktrees/veltrix-customer-portal`.
Use its `backend/.venv/Scripts/python.exe`; no package upgrades. Initial code baseline
`74f852f`; 66 existing policy/provisioning/lifecycle/subscription-sync tests passed.
Three pre-existing dirty documentation files are parent-owned checkpoints; preserve them.

## File responsibilities

- `backend/app/db/models.py`: the endpoint model and access-key database constraints;
  retain the existing `VpnAccessKey.worker` relationship and all old fields.
- `backend/app/db/vpn_endpoint_migrations.py`: bounded additive PostgreSQL statements.
- `backend/app/db/migrations.py`: import and append those statements to `MIGRATIONS`.
- `backend/tests/test_vpn_endpoint_schema.py`: fresh-schema defaults, constraints,
  legacy compatibility and absence of credential columns.
- `backend/tests/test_vpn_endpoint_migrations.py`: real PostgreSQL upgrade/repeat and
  fresh-metadata-then-migration tests with synthetic data in a private random schema.

## Task 1: Inert model and additive migration

- [ ] **1. RED: add schema tests, then run them before editing application code.**

Create `backend/tests/test_vpn_endpoint_schema.py`. The first test must fail on the
missing table, not on an import error:

```python
from app.db.base import Base
from app.db import models


def test_endpoint_table_is_registered():
    assert "vpn_endpoints" in Base.metadata.tables


def test_existing_key_does_not_require_endpoint():
    key = models.VpnAccessKey(subscription_id=1, worker_id=7,
                              external_uuid="legacy", config_uri="vless://legacy")
    assert hasattr(key, "endpoint_id")
    assert key.endpoint_id is None
    assert key.external_uuid == "legacy"
    assert key.config_uri == "vless://legacy"
```

Run from `backend`: `.\.venv\Scripts\python.exe -m pytest tests/test_vpn_endpoint_schema.py -q`.
Expected: assertion failures for missing endpoint table/column.

- [ ] **2. GREEN: add the model and migration.**

Add `CheckConstraint` and `ForeignKeyConstraint` to the SQLAlchemy imports in
`models.py`. Insert the following model immediately before `VpnAccessKey`:

```python
class VpnEndpoint(Base):
    __tablename__ = "vpn_endpoints"
    __table_args__ = (
        UniqueConstraint("worker_id", "inbound_id", name="uq_vpn_endpoint_worker_inbound"),
        UniqueConstraint("id", "worker_id", name="uq_vpn_endpoint_id_worker"),
        CheckConstraint("inbound_id > 0", name="ck_vpn_endpoint_inbound"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="ck_vpn_endpoint_port"),
        CheckConstraint("status IN ('staged', 'ready', 'draining', 'disabled')",
                        name="ck_vpn_endpoint_status"),
        CheckConstraint("security IN ('none', 'tls', 'reality')",
                        name="ck_vpn_endpoint_security"),
        CheckConstraint("status <> 'ready' OR (security IN ('tls', 'reality') "
                        "AND verified_at IS NOT NULL)", name="ck_vpn_endpoint_ready"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    worker_id: Mapped[int] = mapped_column(
        ForeignKey("worker_nodes.id", ondelete="RESTRICT"), index=True)
    inbound_id: Mapped[int] = mapped_column(Integer)
    public_host: Mapped[str] = mapped_column(String(255))
    port: Mapped[int] = mapped_column(Integer)
    protocol: Mapped[str] = mapped_column(String(32), default="vless", server_default="vless")
    transport: Mapped[str] = mapped_column(String(32), default="tcp", server_default="tcp")
    security: Mapped[str] = mapped_column(String(32), default="none", server_default="none")
    server_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    public_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    short_id: Mapped[str | None] = mapped_column(String(16), nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(32), nullable=True)
    flow: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="staged", server_default="staged", index=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow,
                                              onupdate=utcnow, server_default=func.now())
```

Add inside `VpnAccessKey`, without changing its existing worker foreign key:

```python
__table_args__ = (
    ForeignKeyConstraint(
        ["endpoint_id", "worker_id"], ["vpn_endpoints.id", "vpn_endpoints.worker_id"],
        name="fk_vpn_access_key_endpoint_worker", ondelete="RESTRICT",
    ),
    CheckConstraint("endpoint_id IS NULL OR worker_id IS NOT NULL",
                    name="ck_vpn_access_key_endpoint_worker"),
)
endpoint_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
```

Create `vpn_endpoint_migrations.py` with `VPN_ENDPOINT_MIGRATIONS`, in this order:

```python
VPN_ENDPOINT_MIGRATIONS = (
    """CREATE TABLE IF NOT EXISTS vpn_endpoints (
        id SERIAL PRIMARY KEY,
        worker_id INTEGER NOT NULL REFERENCES worker_nodes(id) ON DELETE RESTRICT,
        inbound_id INTEGER NOT NULL, public_host VARCHAR(255) NOT NULL, port INTEGER NOT NULL,
        protocol VARCHAR(32) NOT NULL DEFAULT 'vless',
        transport VARCHAR(32) NOT NULL DEFAULT 'tcp', security VARCHAR(32) NOT NULL DEFAULT 'none',
        server_name VARCHAR(255) NULL, public_key VARCHAR(128) NULL, short_id VARCHAR(16) NULL,
        fingerprint VARCHAR(32) NULL, flow VARCHAR(32) NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'staged', verified_at TIMESTAMPTZ NULL,
        last_error_code VARCHAR(64) NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        CONSTRAINT uq_vpn_endpoint_worker_inbound UNIQUE (worker_id, inbound_id),
        CONSTRAINT uq_vpn_endpoint_id_worker UNIQUE (id, worker_id),
        CONSTRAINT ck_vpn_endpoint_inbound CHECK (inbound_id > 0),
        CONSTRAINT ck_vpn_endpoint_port CHECK (port BETWEEN 1 AND 65535),
        CONSTRAINT ck_vpn_endpoint_status CHECK (status IN ('staged','ready','draining','disabled')),
        CONSTRAINT ck_vpn_endpoint_security CHECK (security IN ('none','tls','reality')),
        CONSTRAINT ck_vpn_endpoint_ready CHECK
            (status <> 'ready' OR (security IN ('tls','reality') AND verified_at IS NOT NULL))
    )""",
    "CREATE INDEX IF NOT EXISTS ix_vpn_endpoints_worker_id ON vpn_endpoints(worker_id)",
    "CREATE INDEX IF NOT EXISTS ix_vpn_endpoints_status ON vpn_endpoints(status)",
    "ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS endpoint_id INTEGER NULL",
    """DO $$ BEGIN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE
            conrelid = 'vpn_access_keys'::regclass AND conname = 'fk_vpn_access_key_endpoint_worker') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT fk_vpn_access_key_endpoint_worker
            FOREIGN KEY (endpoint_id, worker_id) REFERENCES vpn_endpoints(id, worker_id) ON DELETE RESTRICT;
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE
            conrelid = 'vpn_access_keys'::regclass AND conname = 'ck_vpn_access_key_endpoint_worker') THEN
            ALTER TABLE vpn_access_keys ADD CONSTRAINT ck_vpn_access_key_endpoint_worker
            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL);
        END IF;
    END $$""",
    "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_endpoint_id ON vpn_access_keys(endpoint_id)",
)
```

In `migrations.py` import `VPN_ENDPOINT_MIGRATIONS` from the new module and append
it to the existing `MIGRATIONS` tuple. Do not modify older statements or startup.

- [ ] **3. RED/GREEN: extend schema tests with actual FK enforcement.**

Use real SQLite foreign-key enforcement. Add these imports, fixture and tests
to the Task 1 test file (keep the two initial RED tests):

```python
import pytest
import pytest_asyncio
from datetime import UTC, datetime
from sqlalchemy import delete, event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload


@pytest_asyncio.fixture
async def endpoint_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add_all([models.WorkerNode(id=1, name="one"),
                             models.WorkerNode(id=2, name="two"),
                             models.VpnCustomer(id=1)])
            await session.flush()
            session.add(models.VpnSubscription(id=1, customer_id=1))
            await session.commit()
            yield session, 1
    finally:
        await engine.dispose()


def endpoint(**values):
    data = dict(worker_id=1, inbound_id=1, public_host="vpn.example", port=443)
    data.update(values)
    return models.VpnEndpoint(**data)


@pytest.mark.parametrize("values", [
    {"inbound_id": 0}, {"port": 0}, {"port": 65536},
    {"status": "unknown"}, {"security": "unknown"},
    {"status": "ready", "security": "none"},
    {"status": "ready", "security": "reality", "verified_at": None},
])
@pytest.mark.asyncio
async def test_invalid_endpoint_is_rejected(endpoint_session, values):
    session, worker_id = endpoint_session
    data = dict(worker_id=worker_id, inbound_id=1, public_host="vpn.example", port=443)
    data.update(values)
    session.add(models.VpnEndpoint(**data))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.asyncio
async def test_defaults_and_no_private_credentials(endpoint_session):
    session, _ = endpoint_session
    row = endpoint()
    session.add(row)
    await session.commit()
    assert row.status == "staged" and row.verified_at is None
    assert row.created_at and row.updated_at
    assert set(models.VpnEndpoint.__table__.columns.keys()) == {
        "id", "worker_id", "inbound_id", "public_host", "port", "protocol",
        "transport", "security", "server_name", "public_key", "short_id",
        "fingerprint", "flow", "status", "verified_at", "last_error_code",
        "created_at", "updated_at",
    }


@pytest.mark.parametrize("second_worker", [1, 2])
@pytest.mark.asyncio
async def test_inbound_uniqueness_is_per_worker(endpoint_session, second_worker):
    session, _ = endpoint_session
    session.add(endpoint())
    await session.commit()
    session.add(endpoint(worker_id=second_worker))
    if second_worker == 1:
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()
    else:
        await session.commit()


@pytest.mark.parametrize("worker_id, endpoint_id", [(2, 1), (None, 1), (1, 999)])
@pytest.mark.asyncio
async def test_invalid_key_binding_rejected(endpoint_session, worker_id, endpoint_id):
    session, _ = endpoint_session
    session.add(endpoint(id=1))
    await session.commit()
    session.add(models.VpnAccessKey(subscription_id=1, worker_id=worker_id,
                                    endpoint_id=endpoint_id))
    with pytest.raises(IntegrityError):
        await session.commit()
    await session.rollback()


@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.asyncio
async def test_key_roundtrip_and_worker_relationship(endpoint_session, bound):
    session, _ = endpoint_session
    session.add(endpoint(id=1))
    await session.commit()
    instant = datetime(2026, 9, 21, tzinfo=UTC)
    row = models.VpnAccessKey(subscription_id=1, worker_id=1,
        endpoint_id=1 if bound else None, external_uuid="synthetic-old",
        config_uri="vless://synthetic-old", status="active", issued_at=instant)
    session.add(row)
    await session.commit()
    session.expunge_all()
    result = (await session.scalars(select(models.VpnAccessKey).options(
        selectinload(models.VpnAccessKey.worker)))).one()
    assert result.worker.id == 1
    assert result.endpoint_id == (1 if bound else None)
    assert result.external_uuid == "synthetic-old"
    assert result.config_uri == "vless://synthetic-old"
    assert result.status == "active"
    assert result.issued_at.replace(tzinfo=UTC) == instant


@pytest.mark.parametrize("model", [models.WorkerNode, "endpoint"])
@pytest.mark.asyncio
async def test_referenced_identity_cannot_be_deleted(endpoint_session, model):
    session, _ = endpoint_session
    session.add(endpoint(id=1))
    await session.commit()
    session.add(models.VpnAccessKey(subscription_id=1, worker_id=1, endpoint_id=1))
    await session.commit()
    target = models.VpnEndpoint if model == "endpoint" else model
    with pytest.raises(IntegrityError):
        await session.execute(delete(target).where(target.id == 1))
        await session.commit()
    await session.rollback()
```

These are storage checks, not runtime validation. Keep RED evidence for the
missing schema; constraint tests can be introduced together before the model
patch by importing the model through `models` inside tests/fixtures.

- [ ] **4. Verify and commit only Task 1 files.**

Run `.\.venv\Scripts\python.exe -m pytest tests/test_vpn_endpoint_schema.py tests/test_vpn_portal_schema.py tests/test_vpn_policy.py -q`
and `.\.venv\Scripts\python.exe -m ruff check app/db tests/test_vpn_endpoint_schema.py`.
Expected: all pass. Review spec compliance, then code quality; fix/review issues.
Commit message: `feat(vpn): add stable endpoint identity storage`.

## Task 2: Actual PostgreSQL repeat-migration rehearsal

- [ ] **1. Create an optional PostgreSQL fixture using `VPN_PORTAL_TEST_PG_URL`,
  the existing isolated-test environment variable.**

Validate `make_url(url)` is PostgreSQL+asyncpg, its hostname is `127.0.0.1`,
database is exactly `veltrix_portal_test`, and the port is explicit and not 5432.
Without the variable, skip with a clear reason; with an unsafe URL, fail before
connecting. Use random identifier `vpn_endpoint_test_` + `uuid4().hex`, create
only that schema, set per-connection `search_path`, and remove only that exact
schema in `finally`. Set SQLAlchemy `hide_parameters=True`; no URL logging.
Create `backend/tests/test_vpn_endpoint_migrations.py` with:

```python
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.db.base import Base
from app.db import models
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS


@pytest_asyncio.fixture
async def migration_engine():
    raw = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not raw:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for PostgreSQL migration proof")
    url = make_url(raw)
    assert url.drivername == "postgresql+asyncpg", "Unsafe test database driver"
    assert url.host == "127.0.0.1", "Test database must use a loopback SSH tunnel"
    assert url.database == "veltrix_portal_test", "Unexpected test database"
    assert url.port and url.port != 5432, "Unexpected test database port"
    schema = "vpn_endpoint_test_" + uuid4().hex
    base = create_async_engine(url, hide_parameters=True)
    engine = create_async_engine(url, hide_parameters=True,
        connect_args={"server_settings": {"search_path": schema}})
    created = False
    try:
        async with base.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        created = True
        yield engine
    finally:
        await engine.dispose()
        if created:
            async with base.begin() as connection:
                await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await base.dispose()


async def repeat_migration(engine):
    for _ in range(2):
        async with engine.begin() as connection:
            for statement in VPN_ENDPOINT_MIGRATIONS:
                await connection.execute(text(statement))
```

- [ ] **2. Test the old-schema upgrade, both constraint enforcement and preservation.**

Create minimal legacy tables in the isolated schema and use these statements:

```sql
CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY);
CREATE TABLE vpn_access_keys (
    id SERIAL PRIMARY KEY, worker_id INTEGER REFERENCES worker_nodes(id),
    external_uuid VARCHAR(128), config_uri TEXT, status VARCHAR(32),
    issued_at TIMESTAMPTZ, expires_at TIMESTAMPTZ
);
INSERT INTO worker_nodes (id) VALUES (1), (2);
INSERT INTO vpn_access_keys (worker_id, external_uuid, config_uri, status)
VALUES (1, 'synthetic-legacy', 'vless://synthetic-legacy', 'active');
```

Snapshot the old row as a dict before migration. Execute every exported
`VPN_ENDPOINT_MIGRATIONS` statement twice inside real transactions. Assert each
old value is unchanged, `endpoint_id` is null, and endpoints are empty. Insert an
endpoint for worker 1, bind the old key, and verify mismatched worker 2, null worker,
duplicate inbound and deletion of the referenced endpoint fail in savepoints.
Do not catch broad exceptions; require SQLAlchemy `IntegrityError`.
The test code (execute the individual SQL strings above, not a multi-statement
asyncpg prepared statement):

```python
@pytest.mark.asyncio
async def test_legacy_upgrade_preserves_rows_and_enforces_bindings(migration_engine):
    engine = migration_engine
    statements = [
        "CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)",
        "CREATE TABLE vpn_access_keys (id SERIAL PRIMARY KEY, "
        "worker_id INTEGER REFERENCES worker_nodes(id), external_uuid VARCHAR(128), "
        "config_uri TEXT, status VARCHAR(32), issued_at TIMESTAMPTZ, expires_at TIMESTAMPTZ)",
        "INSERT INTO worker_nodes (id) VALUES (1), (2)",
        "INSERT INTO vpn_access_keys (worker_id, external_uuid, config_uri, status) "
        "VALUES (1, 'synthetic-legacy', 'vless://synthetic-legacy', 'active')",
    ]
    async with engine.begin() as connection:
        for statement in statements:
            await connection.execute(text(statement))
        before = dict((await connection.execute(text("SELECT * FROM vpn_access_keys"))).mappings().one())
    await repeat_migration(engine)
    async with engine.begin() as connection:
        after = dict((await connection.execute(text("SELECT * FROM vpn_access_keys"))).mappings().one())
        assert after.pop("endpoint_id") is None
        assert before == after
        assert await connection.scalar(text("SELECT count(*) FROM vpn_endpoints")) == 0
        await connection.execute(text("INSERT INTO vpn_endpoints "
            "(id, worker_id, inbound_id, public_host, port) VALUES (1, 1, 1, 'vpn.example', 443)"))
        await connection.execute(text("UPDATE vpn_access_keys SET endpoint_id=1 WHERE id=1"))
        for statement in [
            "UPDATE vpn_access_keys SET worker_id=2 WHERE id=1",
            "UPDATE vpn_access_keys SET worker_id=NULL WHERE id=1",
            "INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port) "
            "VALUES (2, 1, 1, 'vpn.example', 443)",
            "DELETE FROM vpn_endpoints WHERE id=1",
        ]:
            with pytest.raises(IntegrityError):
                async with connection.begin_nested():
                    await connection.execute(text(statement))
```

- [ ] **3. Test the application's fresh-schema startup order.**

In a second empty schema run `Base.metadata.create_all`, then run the endpoint
statements twice. Inspect named FK/CHECK/UNIQUE constraints and assert the two
access-key constraints each exist exactly once. This catches metadata/SQL drift.
No application lifespan or network/VPN services are started by either test.

```python
@pytest.mark.asyncio
async def test_fresh_metadata_then_repeat_migration(migration_engine):
    assert models.VpnEndpoint.__table__.name == "vpn_endpoints"
    async with migration_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await repeat_migration(migration_engine)
    async with migration_engine.connect() as connection:
        names = (await connection.execute(text("SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'vpn_access_keys'::regclass"))).scalars().all()
    assert names.count("fk_vpn_access_key_endpoint_worker") == 1
    assert names.count("ck_vpn_access_key_endpoint_worker") == 1
```

- [ ] **4. Run against a separately authorized disposable PostgreSQL cluster.**

Use the reviewed private test-cluster helper, not the production database. Parent
owns provisioning/cleanup of the fixture. Record real test results, stop the
temporary cluster and independently verify its exact directory is removed.
Run the full backend suite with the same test URL and Ruff before final checkpoint.
Commit message: `test(vpn): rehearse endpoint storage upgrades on PostgreSQL`.

## Acceptance and next boundary

Storage tests passing, real PostgreSQL twice-upgrade/fresh-start proof, zero legacy
data mutation, and both review stages constitute completion of this storage plan.
No VPN transport, admission, invitation or deployment completion is implied.
Next plan connects the stable identity to verified binding and runtime operations;
production remains on its existing working version until that release is ready.
