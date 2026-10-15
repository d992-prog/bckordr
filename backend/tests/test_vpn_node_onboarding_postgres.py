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
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from starlette.requests import Request

from app.api.deps import require_admin
from app.api.routes import control
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.migrations import run_startup_migrations
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


@pytest.mark.asyncio
async def test_concurrent_start_requests_create_one_active_job(
    pg_sessions, monkeypatch
) -> None:
    async with pg_sessions() as session:
        admin = await _seed_admin(session)
        worker = _worker()
        session.add(worker)
        await session.commit()
        worker_id, admin_id = worker.id, admin.id

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with pg_sessions() as session:
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
        first, second = await asyncio.wait_for(
            asyncio.gather(
                client.post(f"/control/workers/{worker_id}/vpn-onboarding/start"),
                client.post(f"/control/workers/{worker_id}/vpn-onboarding/start"),
            ),
            timeout=10,
        )

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
    release_onboarding = asyncio.Event()

    class OnboardingSession(AsyncSession):
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
                worker_locked.set()
                await asyncio.wait_for(release_onboarding.wait(), timeout=5)
            return result

    onboarding_sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=OnboardingSession,
    )
    async with pg_sessions() as session:
        admin = await _seed_admin(session)
        worker = _worker()
        session.add(worker)
        await session.flush()
        endpoint = _endpoint(worker.id)
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
        await asyncio.wait_for(worker_locked.wait(), timeout=5)
        async with pg_sessions() as session:
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
    provisioning = asyncio.create_task(provision_customer())
    await asyncio.wait_for(worker_locked.wait(), timeout=5)
    await asyncio.sleep(0)
    release_onboarding.set()
    response, error_code = await asyncio.wait_for(
        asyncio.gather(onboarding, provisioning), timeout=10
    )

    assert response.state == "queued"
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
                await asyncio.wait_for(release_promotion.wait(), timeout=5)
            return result

    class HealthSession(AsyncSession):
        async def scalar(self, statement, **kwargs):
            if (
                statement._for_update_arg is not None
                and statement.column_descriptions
                and statement.column_descriptions[0].get("entity") is WorkerNode
            ):
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
    release_promotion.set()
    await asyncio.wait_for(promotion, timeout=10)
    assert await asyncio.wait_for(health, timeout=10) is True

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
async def test_startup_migrations_twice_preserve_worker_and_endpoint_data(
    pg_sessions,
    postgres_schema,
) -> None:
    async with pg_sessions() as session:
        worker = _worker(
            name="migration-polish-server",
            region="pl-waw",
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
        endpoint = _endpoint(
            worker.id,
            health_checked_at=NOW,
            last_error_code="vpn_node_health_transport_failed",
            max_active_profiles=41,
            capacity_warning_percent=67,
        )
        session.add(endpoint)
        await session.commit()
        worker_id, endpoint_id = worker.id, endpoint.id

    async def snapshot() -> tuple[dict[str, object], dict[str, object]]:
        async with pg_sessions() as session:
            worker = await session.get(WorkerNode, worker_id)
            endpoint = await session.get(VpnEndpoint, endpoint_id)
            assert worker is not None and endpoint is not None
            return (
                {
                    "id": worker.id,
                    "name": worker.name,
                    "region": worker.region,
                    "ssh_host": worker.ssh_host,
                    "ssh_port": worker.ssh_port,
                    "ssh_username": worker.ssh_username,
                    "ssh_host_key": worker.ssh_host_key,
                    "ssh_host_key_fingerprint": worker.ssh_host_key_fingerprint,
                    "vpn_runtime_status": worker.vpn_runtime_status,
                    "vpn_public_host": worker.vpn_public_host,
                    "vpn_inbound_id": worker.vpn_inbound_id,
                    "vpn_inbound_port": worker.vpn_inbound_port,
                    "vpn_inbound_protocol": worker.vpn_inbound_protocol,
                    "vpn_inbound_transport": worker.vpn_inbound_transport,
                    "vpn_inbound_security": worker.vpn_inbound_security,
                    "vpn_listener_status": worker.vpn_listener_status,
                    "vpn_last_checked_at": worker.vpn_last_checked_at,
                },
                {
                    "id": endpoint.id,
                    "worker_id": endpoint.worker_id,
                    "inbound_id": endpoint.inbound_id,
                    "public_host": endpoint.public_host,
                    "port": endpoint.port,
                    "protocol": endpoint.protocol,
                    "transport": endpoint.transport,
                    "security": endpoint.security,
                    "server_name": endpoint.server_name,
                    "public_key": endpoint.public_key,
                    "short_id": endpoint.short_id,
                    "fingerprint": endpoint.fingerprint,
                    "flow": endpoint.flow,
                    "status": endpoint.status,
                    "health_checked_at": endpoint.health_checked_at,
                    "last_error_code": endpoint.last_error_code,
                    "max_active_profiles": endpoint.max_active_profiles,
                    "capacity_warning_percent": endpoint.capacity_warning_percent,
                },
            )

    before = await snapshot()
    await run_startup_migrations(postgres_schema.engine)
    await run_startup_migrations(postgres_schema.engine)
    assert await snapshot() == before
