"""Real PostgreSQL serialization checks for VPN node onboarding."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, FastAPI
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request
from test_vpn_endpoint_migrations import PRE_TASK1_MIGRATIONS

from app.api.deps import require_admin
from app.api.routes import control
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.migrations import MIGRATIONS
from app.db.models import (
    AdminAuditLog,
    User,
    VpnAccessKey,
    VpnCustomer,
    VpnEndpoint,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
)
from app.db.session import get_db
from app.schemas.control import VpnEndpointCapacityUpdateRequest
from app.services.vpn_control_intents import (
    VpnControlIntentError,
    stage_vpn_control_operation,
)
from app.services.vpn_fleet_health import probe_next_vpn_endpoint
from app.services.vpn_node_health import VpnNodeHealthReceipt
from app.services.vpn_reality_endpoint_installer import make_endpoint_receipt
from app.services.vpn_reality_endpoint_registration import (
    ExternalEndpointAcceptance,
    promote_protected_endpoint,
)

pytest_plugins = ("test_vpn_endpoint_migrations",)

NOW = datetime(2026, 10, 15, 12, tzinfo=UTC)
HEALTH_NOW = NOW + timedelta(minutes=1)
FINGERPRINT = "SHA256:" + "A" * 43
HOST_KEY = "192.0.2.44 ssh-ed25519 " + "A" * 44
PUBLIC_KEY = "A" * 43
SHORT_ID = "0011223344556677"
RELEASE_ID = "b" * 64
ONBOARDING_MIGRATIONS = (
    "ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS ssh_host_key TEXT NULL",
    (
        "ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS "
        "ssh_host_key_fingerprint VARCHAR(64) NULL"
    ),
)


@pytest_asyncio.fixture
async def pg_sessions(
    postgres_schema,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )


def _worker(**changes: object) -> WorkerNode:
    values: dict[str, object] = {
        "name": f"polish-server-{uuid4().hex[:8]}",
        "status": "ready",
        "is_enabled": True,
        "vpn_role": "vpn_node",
        "vpn_enabled": True,
        "vpn_runtime_status": "needs_config",
        "vpn_public_host": "192.0.2.44",
        "ssh_host": "192.0.2.44",
        "ssh_port": 22,
        "ssh_username": "root",
        "ssh_password": "test-only-password",
        "ssh_host_key": HOST_KEY,
        "ssh_host_key_fingerprint": FINGERPRINT,
    }
    values.update(changes)
    return WorkerNode(**values)


def _endpoint(worker_id: int, **changes: object) -> VpnEndpoint:
    values: dict[str, object] = {
        "worker_id": worker_id,
        "inbound_id": 27,
        "public_host": "192.0.2.44",
        "port": 443,
        "protocol": "vless",
        "transport": "tcp",
        "security": "reality",
        "server_name": "gateway.icloud.com",
        "public_key": PUBLIC_KEY,
        "short_id": SHORT_ID,
        "fingerprint": "chrome",
        "flow": "xtls-rprx-vision",
        "status": "staged",
        "health_checked_at": NOW,
        "max_active_profiles": 20,
        "capacity_warning_percent": 80,
    }
    values.update(changes)
    return VpnEndpoint(**values)


def _same_origin_request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": ((b"host", b"testserver"), (b"origin", b"http://testserver")),
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
    )


async def _seed_admin(session: AsyncSession) -> User:
    admin = User(
        username=f"owner-{uuid4().hex[:8]}",
        password_hash="test-only-hash",
        role="admin",
        status="active",
    )
    session.add(admin)
    await session.flush()
    return admin


def _locks_worker(statement) -> bool:
    return (
        getattr(statement, "_for_update_arg", None) is not None
        and statement.column_descriptions
        and statement.column_descriptions[0].get("entity") is WorkerNode
    )


async def _wait_until_postgres_session_is_blocked(pg_sessions, pid: int) -> bool:
    async with pg_sessions() as observer:
        while True:
            blocked = await observer.scalar(
                text("SELECT cardinality(pg_blocking_pids(:pid)) > 0"),
                {"pid": pid},
            )
            if blocked:
                return True
            await asyncio.sleep(0.01)


async def _cancel_and_await(*tasks: asyncio.Task | None) -> None:
    present = [task for task in tasks if task is not None]
    for task in present:
        if not task.done():
            task.cancel()
    await asyncio.gather(*present, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_start_requests_create_one_active_job(
    pg_sessions, postgres_schema, monkeypatch
) -> None:
    first_locked = asyncio.Event()
    second_attempted = asyncio.Event()
    release_first = asyncio.Event()
    second_pid: dict[str, int] = {}

    class StartSession(AsyncSession):
        held_worker = False

        async def scalar(self, statement, **kwargs):
            if not self.held_worker and _locks_worker(statement):
                self.held_worker = True
                pid = int(await super().scalar(text("SELECT pg_backend_pid()")))
                if self.info["role"] == "first":
                    result = await super().scalar(statement, **kwargs)
                    first_locked.set()
                    await asyncio.wait_for(release_first.wait(), timeout=5)
                    return result
                second_pid["value"] = pid
                second_attempted.set()
            return await super().scalar(statement, **kwargs)

    start_sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=StartSession,
    )
    async with pg_sessions() as session:
        admin = await _seed_admin(session)
        worker = _worker()
        session.add(worker)
        await session.commit()
        worker_id, admin_id = worker.id, admin.id

    app = FastAPI()
    app.include_router(control_router)
    roles = iter(("first", "second"))

    async def override_get_db():
        async with start_sessions(info={"role": next(roles)}) as session:
            yield session

    async def override_admin():
        return SimpleNamespace(id=admin_id, role="admin")

    async def noop_runner(_job_id: int) -> None:
        return None

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = override_admin
    monkeypatch.setattr(control, "run_worker_maintenance_job", noop_runner)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
        headers={"Origin": "http://testserver"},
    ) as client:
        first_request = asyncio.create_task(
            client.post(f"/control/workers/{worker_id}/vpn-onboarding/start")
        )
        second_request = None
        try:
            await asyncio.wait_for(first_locked.wait(), timeout=5)
            second_request = asyncio.create_task(
                client.post(f"/control/workers/{worker_id}/vpn-onboarding/start")
            )
            await asyncio.wait_for(second_attempted.wait(), timeout=5)
            assert await asyncio.wait_for(
                _wait_until_postgres_session_is_blocked(
                    pg_sessions,
                    second_pid["value"],
                ),
                timeout=3,
            )
            assert not first_request.done()
            assert not second_request.done()
            release_first.set()
            first, second = await asyncio.wait_for(
                asyncio.gather(first_request, second_request), timeout=10
            )
        finally:
            release_first.set()
            await _cancel_and_await(first_request, second_request)

    assert first.status_code == second.status_code == 202
    assert first.json()["job_id"] == second.json()["job_id"]
    async with pg_sessions() as session:
        jobs = list(
            (
                await session.scalars(
                    select(WorkerMaintenanceJob).where(
                        WorkerMaintenanceJob.worker_id == worker_id,
                        WorkerMaintenanceJob.status.in_(("queued", "running")),
                    )
                )
            ).all()
        )
        audits = list(
            (
                await session.scalars(
                    select(AdminAuditLog).where(
                        AdminAuditLog.action == "vpn_node_onboarding_start"
                    )
                )
            ).all()
        )
    assert [(job.action, job.status) for job in jobs] == [("vpn_onboard", "queued")]
    assert len(audits) == 1


@pytest.mark.asyncio
async def test_onboarding_and_customer_provisioning_cannot_share_worker(
    pg_sessions,
    postgres_schema,
) -> None:
    worker_locked = asyncio.Event()
    provisioning_attempted = asyncio.Event()
    release_onboarding = asyncio.Event()
    provisioning_pid: dict[str, int] = {}

    class OnboardingSession(AsyncSession):
        held_worker = False

        async def scalar(self, statement, **kwargs):
            result = await super().scalar(statement, **kwargs)
            if not self.held_worker and _locks_worker(statement):
                self.held_worker = True
                worker_locked.set()
                await asyncio.wait_for(release_onboarding.wait(), timeout=5)
            return result

    class ProvisioningSession(AsyncSession):
        attempted_worker = False

        async def scalar(self, statement, **kwargs):
            if not self.attempted_worker and _locks_worker(statement):
                self.attempted_worker = True
                provisioning_pid["value"] = int(
                    await super().scalar(text("SELECT pg_backend_pid()"))
                )
                provisioning_attempted.set()
            return await super().scalar(statement, **kwargs)

    onboarding_sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=OnboardingSession,
    )
    provisioning_sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=ProvisioningSession,
    )
    async with pg_sessions() as session:
        admin = await _seed_admin(session)
        worker = _worker()
        session.add(worker)
        await session.flush()
        endpoint = _endpoint(worker.id, status="disabled")
        session.add(endpoint)
        await session.flush()
        customer = VpnCustomer(status="active")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(
            customer_id=customer.id,
            status="active",
            starts_at=NOW,
            expires_at=NOW + timedelta(days=30),
            max_devices=1,
        )
        session.add(subscription)
        await session.flush()
        key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            endpoint_id=endpoint.id,
            external_uuid=str(uuid4()),
            verified_client_email="customer@local",
            panel_sub_id="customer-sub-id",
            status="pending_sync",
        )
        session.add(key)
        await session.commit()
        worker_id, access_key_id, admin_id = worker.id, key.id, admin.id

    async def start_onboarding():
        async with onboarding_sessions() as session:
            return await control.start_vpn_node_onboarding(
                worker_id,
                BackgroundTasks(),
                _same_origin_request(
                    f"/control/workers/{worker_id}/vpn-onboarding/start"
                ),
                session,
                SimpleNamespace(id=admin_id, role="admin"),
            )

    async def provision_customer() -> str:
        async with provisioning_sessions() as session:
            try:
                await stage_vpn_control_operation(
                    session,
                    access_key_id,
                    "provision",
                    now=NOW,
                )
            except VpnControlIntentError as error:
                await session.rollback()
                return error.code
            raise AssertionError("customer provisioning shared an onboarding worker")

    onboarding = asyncio.create_task(start_onboarding())
    provisioning = None
    try:
        await asyncio.wait_for(worker_locked.wait(), timeout=5)
        provisioning = asyncio.create_task(provision_customer())
        await asyncio.wait_for(provisioning_attempted.wait(), timeout=5)
        assert await asyncio.wait_for(
            _wait_until_postgres_session_is_blocked(
                pg_sessions,
                provisioning_pid["value"],
            ),
            timeout=3,
        )
        assert not onboarding.done()
        assert not provisioning.done()
        release_onboarding.set()
        response, error_code = await asyncio.wait_for(
            asyncio.gather(onboarding, provisioning), timeout=10
        )
    finally:
        release_onboarding.set()
        await _cancel_and_await(onboarding, provisioning)

    assert response.job_id is not None
    assert error_code == "vpn_control_worker_busy"
    async with pg_sessions() as session:
        assert (
            await session.scalar(
                select(WorkerMaintenanceJob.id).where(
                    WorkerMaintenanceJob.worker_id == worker_id,
                    WorkerMaintenanceJob.status.in_(("queued", "running")),
                )
            )
            is not None
        )


@pytest.mark.asyncio
async def test_promotion_with_capacity_and_health_updates_preserves_exact_endpoint(
    pg_sessions,
    postgres_schema,
) -> None:
    promotion_has_worker = asyncio.Event()
    release_promotion = asyncio.Event()
    health_attempted_worker = asyncio.Event()
    health_pid: dict[str, int] = {}

    class PromotionSession(AsyncSession):
        held_worker = False

        async def scalar(self, statement, **kwargs):
            result = await super().scalar(statement, **kwargs)
            if (
                not self.held_worker
                and statement._for_update_arg is not None
                and statement.column_descriptions
                and statement.column_descriptions[0].get("entity") is WorkerNode
            ):
                self.held_worker = True
                promotion_has_worker.set()
                await release_promotion.wait()
            return result

    class HealthSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if _locks_worker(statement):
                health_pid["value"] = int(
                    await super().scalar(text("SELECT pg_backend_pid()"))
                )
                health_attempted_worker.set()
            return await super().scalar(statement, **kwargs)

    engine = postgres_schema.engine
    promotion_sessions = async_sessionmaker(
        engine, expire_on_commit=False, class_=PromotionSession
    )
    health_sessions = async_sessionmaker(
        engine, expire_on_commit=False, class_=HealthSession
    )
    async with pg_sessions() as session:
        admin = await _seed_admin(session)
        worker = _worker(
            vpn_runtime_status="awaiting_test",
            vpn_inbound_id=27,
            vpn_inbound_port=443,
            vpn_inbound_protocol="vless",
            vpn_inbound_transport="tcp",
            vpn_inbound_security="reality",
            vpn_listener_status="listening",
            vpn_last_checked_at=NOW,
        )
        session.add(worker)
        await session.flush()
        endpoint = _endpoint(worker.id)
        session.add(endpoint)
        await session.commit()
        worker_id, endpoint_id, admin_id = worker.id, endpoint.id, admin.id

    receipt = make_endpoint_receipt(
        state="acceptance_client_removed",
        worker_id=worker_id,
        inbound_id=27,
        public_host="192.0.2.44",
        server_name="gateway.icloud.com",
        public_key=PUBLIC_KEY,
        short_id=SHORT_ID,
    )
    acceptance = ExternalEndpointAcceptance(
        release_id=RELEASE_ID,
        receipt_digest=receipt.receipt_digest,
        evidence_digest="c" * 64,
        checked_at=NOW,
    )

    async def promote() -> None:
        async with promotion_sessions() as session:
            promoted = await promote_protected_endpoint(
                session,
                receipt,
                acceptance,
                controlled_worker_id=worker_id,
                now=NOW,
            )
            assert promoted.id == endpoint_id
            await session.commit()

    async def update_capacity() -> None:
        await asyncio.wait_for(promotion_has_worker.wait(), timeout=5)
        async with pg_sessions() as session:
            response = await control.update_vpn_endpoint_capacity(
                endpoint_id,
                VpnEndpointCapacityUpdateRequest(
                    max_active_profiles=37,
                    capacity_warning_percent=73,
                ),
                session,
                SimpleNamespace(id=admin_id, role="admin"),
            )
            assert response.endpoint_id == endpoint_id

    async def healthy_transport(_snapshot, request, *, now_ms):
        assert request.target.endpoint_id == endpoint_id
        assert now_ms == int(HEALTH_NOW.timestamp() * 1000)
        return VpnNodeHealthReceipt("healthy", None, "running")

    promotion = asyncio.create_task(promote())
    capacity = None
    health = None
    try:
        await asyncio.wait_for(promotion_has_worker.wait(), timeout=5)
        capacity = asyncio.create_task(update_capacity())
        health = asyncio.create_task(
            probe_next_vpn_endpoint(
                health_sessions,
                Path("C:/veltrix/known_hosts"),
                snapshot_loader=lambda *_: object(),
                transport=healthy_transport,
                now=lambda: HEALTH_NOW,
            )
        )
        await asyncio.wait_for(capacity, timeout=5)
        await asyncio.wait_for(health_attempted_worker.wait(), timeout=5)
        assert await asyncio.wait_for(
            _wait_until_postgres_session_is_blocked(
                pg_sessions,
                health_pid["value"],
            ),
            timeout=3,
        )
        release_promotion.set()
        await asyncio.wait_for(promotion, timeout=10)
        assert await asyncio.wait_for(health, timeout=10) is True
    finally:
        release_promotion.set()
        await _cancel_and_await(promotion, capacity, health)

    async with pg_sessions() as session:
        endpoint = await session.get(VpnEndpoint, endpoint_id)
        worker = await session.get(WorkerNode, worker_id)
    assert endpoint is not None and worker is not None
    assert (
        endpoint.worker_id,
        endpoint.inbound_id,
        endpoint.public_host,
        endpoint.port,
        endpoint.protocol,
        endpoint.transport,
        endpoint.security,
        endpoint.server_name,
        endpoint.public_key,
        endpoint.short_id,
        endpoint.fingerprint,
        endpoint.flow,
    ) == (
        worker_id,
        27,
        "192.0.2.44",
        443,
        "vless",
        "tcp",
        "reality",
        "gateway.icloud.com",
        PUBLIC_KEY,
        SHORT_ID,
        "chrome",
        "xtls-rprx-vision",
    )
    assert endpoint.status == "ready"
    assert endpoint.max_active_profiles == 37
    assert endpoint.capacity_warning_percent == 73
    assert endpoint.verified_at == HEALTH_NOW
    assert endpoint.health_checked_at == HEALTH_NOW
    assert endpoint.last_error_code is None
    assert worker.vpn_runtime_status == "ready"


@pytest.mark.asyncio
async def test_onboarding_migrations_twice_preserve_pre_onboarding_data(
    postgres_schema,
) -> None:
    worker_query = text(
        """
        SELECT id, name, region, status, is_enabled, ssh_host, ssh_port,
               ssh_username, ssh_password, ssh_key_path, vpn_role, vpn_enabled,
               vpn_runtime_status, vpn_public_host, vpn_inbound_id,
               vpn_inbound_port, vpn_inbound_protocol, vpn_inbound_transport,
               vpn_inbound_security, vpn_listener_status, vpn_last_checked_at,
               vpn_last_error, archived_at, created_at, updated_at
        FROM worker_nodes WHERE id = 7
        """
    )
    endpoint_query = text(
        """
        SELECT id, worker_id, inbound_id, public_host, port, protocol,
               transport, security, server_name, public_key, short_id,
               fingerprint, flow, status, verified_at, health_checked_at,
               external_verified_at, external_config_fingerprint,
               last_error_code, max_active_profiles, capacity_warning_percent,
               created_at, updated_at
        FROM vpn_endpoints WHERE id = 11
        """
    )

    async with postgres_schema.engine.begin() as connection:
        await connection.execute(
            text(
                """
                CREATE TABLE worker_nodes (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(128) NOT NULL,
                    region VARCHAR(128) NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'provisioning',
                    is_enabled BOOLEAN NOT NULL DEFAULT true,
                    ssh_host VARCHAR(64) NULL,
                    ssh_port INTEGER NOT NULL DEFAULT 22,
                    ssh_username VARCHAR(64) NULL,
                    ssh_password TEXT NULL,
                    ssh_key_path VARCHAR(255) NULL,
                    vpn_role VARCHAR(32) NOT NULL DEFAULT 'none',
                    vpn_enabled BOOLEAN NOT NULL DEFAULT false,
                    vpn_runtime_status VARCHAR(32) NOT NULL DEFAULT 'not_installed',
                    vpn_public_host VARCHAR(255) NULL,
                    vpn_inbound_id INTEGER NULL,
                    vpn_inbound_port INTEGER NULL,
                    vpn_inbound_protocol VARCHAR(32) NULL,
                    vpn_inbound_transport VARCHAR(32) NULL,
                    vpn_inbound_security VARCHAR(32) NULL,
                    vpn_listener_status VARCHAR(32) NULL,
                    vpn_last_checked_at TIMESTAMPTZ NULL,
                    vpn_last_error TEXT NULL,
                    archived_at TIMESTAMPTZ NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    CONSTRAINT uq_worker_nodes_name UNIQUE (name)
                )
                """
            )
        )
        await connection.execute(
            text(
                "CREATE INDEX ix_worker_nodes_archived_at ON worker_nodes(archived_at)"
            )
        )
        for statement in PRE_TASK1_MIGRATIONS[:3]:
            await connection.execute(text(statement))
        await connection.execute(
            text(
                """
                INSERT INTO worker_nodes (
                    id, name, region, status, is_enabled, ssh_host, ssh_port,
                    ssh_username, ssh_password, ssh_key_path, vpn_role,
                    vpn_enabled, vpn_runtime_status, vpn_public_host,
                    vpn_inbound_id, vpn_inbound_port, vpn_inbound_protocol,
                    vpn_inbound_transport, vpn_inbound_security,
                    vpn_listener_status, vpn_last_checked_at, vpn_last_error,
                    archived_at, created_at, updated_at
                ) VALUES (
                    7, 'migration-polish-server', 'pl-waw', 'ready', true,
                    '192.0.2.44', 22, 'root', 'old-password', '/root/.ssh/id_ed25519',
                    'vpn_node', true, 'awaiting_test', '192.0.2.44', 27, 443,
                    'vless', 'tcp', 'reality', 'listening', :checked_at,
                    'old-safe-code', NULL, :created_at, :updated_at
                )
                """
            ),
            {
                "checked_at": NOW,
                "created_at": NOW - timedelta(days=2),
                "updated_at": NOW - timedelta(days=1),
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO vpn_endpoints (
                    id, worker_id, inbound_id, public_host, port, protocol,
                    transport, security, server_name, public_key, short_id,
                    fingerprint, flow, status, verified_at, health_checked_at,
                    external_verified_at, external_config_fingerprint,
                    last_error_code, max_active_profiles,
                    capacity_warning_percent, created_at, updated_at
                ) VALUES (
                    11, 7, 27, '192.0.2.44', 443, 'vless', 'tcp', 'reality',
                    'gateway.icloud.com', :public_key, :short_id, 'chrome',
                    'xtls-rprx-vision', 'staged', NULL, :checked_at, NULL, NULL,
                    'vpn_node_health_transport_failed', 41, 67,
                    :created_at, :updated_at
                )
                """
            ),
            {
                "public_key": PUBLIC_KEY,
                "short_id": SHORT_ID,
                "checked_at": NOW,
                "created_at": NOW - timedelta(days=2),
                "updated_at": NOW - timedelta(days=1),
            },
        )
        missing_columns = set(
            await connection.scalars(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'worker_nodes'
                      AND column_name IN ('ssh_host_key', 'ssh_host_key_fingerprint')
                    """
                )
            )
        )
        assert missing_columns == set()
        before_worker = dict((await connection.execute(worker_query)).mappings().one())
        before_endpoint = dict(
            (await connection.execute(endpoint_query)).mappings().one()
        )
        before_constraints = set(
            await connection.scalars(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'vpn_endpoints'::regclass
                    """
                )
            )
        )
        before_indexes = set(
            await connection.scalars(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'vpn_endpoints'
                    """
                )
            )
        )

    for _ in range(2):
        async with postgres_schema.engine.begin() as connection:
            for statement in MIGRATIONS:
                if statement in ONBOARDING_MIGRATIONS:
                    await connection.execute(text(statement))

    async with postgres_schema.engine.connect() as connection:
        after_worker = dict((await connection.execute(worker_query)).mappings().one())
        after_endpoint = dict(
            (await connection.execute(endpoint_query)).mappings().one()
        )
        new_columns = {
            row["column_name"]: (
                row["is_nullable"],
                row["data_type"],
                row["character_maximum_length"],
            )
            for row in (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT column_name, is_nullable, data_type,
                                   character_maximum_length
                            FROM information_schema.columns
                            WHERE table_schema = current_schema()
                              AND table_name = 'worker_nodes'
                              AND column_name IN (
                                  'ssh_host_key', 'ssh_host_key_fingerprint'
                              )
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
        }
        added_values = (
            await connection.execute(
                text(
                    """
                    SELECT ssh_host_key, ssh_host_key_fingerprint
                    FROM worker_nodes WHERE id = 7
                    """
                )
            )
        ).one()
        after_constraints = set(
            await connection.scalars(
                text(
                    """
                    SELECT conname FROM pg_constraint
                    WHERE conrelid = 'vpn_endpoints'::regclass
                    """
                )
            )
        )
        after_indexes = set(
            await connection.scalars(
                text(
                    """
                    SELECT indexname FROM pg_indexes
                    WHERE schemaname = current_schema()
                      AND tablename = 'vpn_endpoints'
                    """
                )
            )
        )

    assert after_worker == before_worker
    assert after_endpoint == before_endpoint
    assert new_columns == {
        "ssh_host_key": ("YES", "text", None),
        "ssh_host_key_fingerprint": ("YES", "character varying", 64),
    }
    assert tuple(added_values) == (None, None)
    assert after_constraints == before_constraints
    assert {
        "uq_vpn_endpoint_worker_inbound",
        "uq_vpn_endpoint_id_worker",
        "ck_vpn_endpoint_inbound",
        "ck_vpn_endpoint_port",
        "ck_vpn_endpoint_capacity",
        "ck_vpn_endpoint_capacity_warning",
        "ck_vpn_endpoint_status",
        "ck_vpn_endpoint_security",
        "ck_vpn_endpoint_ready",
    } <= after_constraints
    assert after_indexes == before_indexes
    assert {
        "ix_vpn_endpoints_worker_id",
        "ix_vpn_endpoints_status",
    } <= after_indexes
