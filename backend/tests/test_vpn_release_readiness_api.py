from __future__ import annotations

import asyncio
import base64
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes import control as control_routes
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    AdminAuditLog,
    AppSetting,
    VpnAccessKey,
    VpnCustomer,
    VpnEndpoint,
    VpnPlan,
    VpnSubscription,
    WorkerNode,
)
from app.db.session import get_db
from app.schemas.control import VpnEndpointExternalVerificationRequest
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    public_endpoint_fingerprint,
)
from app.services.vpn_release_readiness import (
    BackupObservation,
    OperationalObservations,
    ReadinessObservation,
)

RELEASE_ID = "a" * 64
READY_MARKER_KEY = "vpn_public_release_ready_v1"
VALID_PUBLIC_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode().rstrip("=")


def _settings(*, release_id: str = RELEASE_ID) -> Settings:
    return Settings(
        _env_file=None,
        VPN_PUBLIC_TRIAL_RELEASE_ID=release_id,
        VPN_PUBLIC_TRIAL_PLAN_SLUG="trial-7d",
        VPN_PUBLIC_TRIAL_ENABLED=False,
        VPN_PORTAL_PUBLIC_ACCESS=False,
        VPN_CONTROL_DISPATCH_ENABLED=True,
        VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
        VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300,
    )


def _passing_observations(now: datetime) -> tuple[OperationalObservations, BackupObservation]:
    def observation() -> ReadinessObservation:
        return ReadinessObservation(state="pass", observed_at=now, max_age_seconds=300)

    return (
        OperationalObservations(
            system=observation(),
            control=observation(),
            local=observation(),
            public=observation(),
            cabinet=observation(),
            disk=observation(),
            known_hosts=observation(),
        ),
        BackupObservation(state="pass", observed_at=now, max_age_seconds=300),
    )


def _target(endpoint: VpnEndpoint) -> VpnEndpointTarget:
    return VpnEndpointTarget(
        endpoint_id=endpoint.id,
        worker_id=endpoint.worker_id,
        inbound_id=endpoint.inbound_id,
        public_host=endpoint.public_host,
        port=endpoint.port,
        protocol=endpoint.protocol,
        transport=endpoint.transport,
        security=endpoint.security,
        server_name=endpoint.server_name,
        public_key=endpoint.public_key,
        short_id=endpoint.short_id,
        fingerprint=endpoint.fingerprint,
        flow=endpoint.flow,
    )


async def _add_ready_endpoint(
    session: AsyncSession,
    *,
    index: int,
    now: datetime,
    externally_verified: bool,
) -> VpnEndpoint:
    worker = WorkerNode(
        name=f"release-node-{index}",
        status="ready",
        is_enabled=True,
        vpn_role="vpn_node",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_public_host=f"vpn-{index}.example",
        vpn_inbound_id=100 + index,
        ssh_host=f"10.0.0.{index}",
        ssh_password=f"worker-secret-{index}",
        vpn_last_checked_at=now,
    )
    session.add(worker)
    await session.flush()
    endpoint = VpnEndpoint(
        worker_id=worker.id,
        inbound_id=100 + index,
        public_host=f"vpn-{index}.example",
        port=443,
        protocol="vless",
        transport="raw",
        security="reality",
        server_name="www.example.com",
        public_key=VALID_PUBLIC_KEY,
        short_id=f"aabbcc{index:02d}",
        fingerprint="chrome",
        flow="xtls-rprx-vision",
        status="ready",
        verified_at=now,
        max_active_profiles=10,
        capacity_warning_percent=80,
    )
    session.add(endpoint)
    await session.flush()
    if externally_verified:
        endpoint.external_verified_at = now
        endpoint.external_config_fingerprint = public_endpoint_fingerprint(_target(endpoint))
    return endpoint


async def _seed_ready_release(
    session: AsyncSession,
    *,
    now: datetime,
) -> tuple[VpnEndpoint, VpnEndpoint]:
    session.add(
        VpnPlan(
            slug="trial-7d",
            name="Seven day trial",
            is_active=True,
            duration_days=7,
            max_devices=1,
        )
    )
    first = await _add_ready_endpoint(
        session,
        index=1,
        now=now,
        externally_verified=True,
    )
    second = await _add_ready_endpoint(
        session,
        index=2,
        now=now,
        externally_verified=True,
    )
    await session.commit()
    return first, second


@pytest_asyncio.fixture
async def api_context(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = _settings()
    now = datetime.now(UTC)
    monkeypatch.setattr(control_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(control_routes, "utcnow", lambda: now)

    async def override_get_db():
        async with factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=7, role="owner")

    app = FastAPI()
    app.include_router(control_routes.router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    anonymous_app = FastAPI()
    anonymous_app.include_router(control_routes.router)
    anonymous_app.dependency_overrides[get_db] = override_get_db

    yield SimpleNamespace(
        engine=engine,
        factory=factory,
        settings=settings,
        app=app,
        anonymous_app=anonymous_app,
        now=now,
    )
    await engine.dispose()


def test_external_verification_request_accepts_only_literal_true() -> None:
    assert VpnEndpointExternalVerificationRequest(confirmed=True).confirmed is True
    for payload in (
        {"confirmed": False},
        {"confirmed": True, "public_host": "must-not-be-accepted.example"},
        {"confirmed": True, "fingerprint": "must-not-be-accepted"},
    ):
        with pytest.raises(ValidationError):
            VpnEndpointExternalVerificationRequest(**payload)


@pytest.mark.asyncio
async def test_release_readiness_endpoints_require_admin(api_context) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.anonymous_app),
        base_url="http://testserver",
    ) as client:
        responses = (
            await client.get("/control/vpn/release-readiness"),
            await client.post(
                "/control/vpn/endpoints/1/external-verification",
                json={"confirmed": True},
            ),
            await client.post("/control/vpn/release-readiness/commit"),
        )
    assert [response.status_code for response in responses] == [401, 401, 401]


@pytest.mark.asyncio
async def test_get_release_readiness_is_fail_closed_bounded_and_no_store(
    api_context,
) -> None:
    async with api_context.factory() as session:
        first, _second = await _seed_ready_release(session, now=api_context.now)
        first_id = first.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        response = await client.get("/control/vpn/release-readiness")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["ready"] is False
    assert set(body) == {"ready", "checked_at", "checks"}
    assert body["checks"]
    assert all(
        set(check) == {"code", "state", "message", "entity_id", "observed_at"}
        for check in body["checks"]
    )
    assert all(check["state"] in {"pass", "warn", "fail"} for check in body["checks"])
    external_check = next(
        check
        for check in body["checks"]
        if check["code"] == "endpoint_external_proof" and check["entity_id"] == first_id
    )
    assert external_check["state"] == "pass"
    serialized = response.text
    for secret in (
        "vpn-1.example",
        VALID_PUBLIC_KEY,
        "aabbcc01",
        "worker-secret-1",
        "fingerprint",
        "config_uri",
    ):
        assert secret not in serialized

    async with api_context.factory() as session:
        endpoint = await session.get(VpnEndpoint, first_id)
        assert endpoint is not None
        endpoint.public_host = "changed.example"
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        changed = await client.get("/control/vpn/release-readiness")
    changed_check = next(
        check
        for check in changed.json()["checks"]
        if check["code"] == "endpoint_external_proof" and check["entity_id"] == first_id
    )
    assert changed_check["state"] == "fail"
    assert "changed.example" not in changed.text


@pytest.mark.asyncio
async def test_external_verification_is_server_computed_safe_and_audited(
    api_context,
) -> None:
    async with api_context.factory() as session:
        endpoint = await _add_ready_endpoint(
            session,
            index=1,
            now=api_context.now,
            externally_verified=False,
        )
        stale = await _add_ready_endpoint(
            session,
            index=3,
            now=api_context.now - timedelta(seconds=301),
            externally_verified=False,
        )
        unsuitable = VpnEndpoint(
            worker_id=endpoint.worker_id,
            inbound_id=999,
            public_host="staged-secret.example",
            port=443,
            protocol="vless",
            transport="raw",
            security="reality",
            server_name="www.example.com",
            public_key=VALID_PUBLIC_KEY,
            short_id="00112233",
            fingerprint="chrome",
            flow="xtls-rprx-vision",
            status="staged",
            verified_at=None,
        )
        plain = VpnEndpoint(
            worker_id=endpoint.worker_id,
            inbound_id=1000,
            public_host="plain.example",
            port=443,
            protocol="vless",
            transport="raw",
            security="none",
            server_name=None,
            public_key=None,
            short_id=None,
            fingerprint=None,
            flow=None,
            status="ready",
            verified_at=api_context.now,
        )
        plain.id = 999999
        assert (
            control_routes._external_verification_target(
                plain,
                now=api_context.now,
                health_max_age_seconds=300,
            )
            is None
        )
        customer = VpnCustomer(telegram_user_id="identity-owner")
        session.add_all([unsuitable, customer])
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=endpoint.worker_id,
            endpoint_id=endpoint.id,
            status="active",
            external_uuid="11111111-1111-4111-8111-111111111111",
            config_uri="vless://identity-secret",
        )
        session.add(access_key)
        await session.commit()
        endpoint_id = endpoint.id
        stale_id = stale.id
        unsuitable_id = unsuitable.id
        access_key_id = access_key.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        for payload in (
            {"confirmed": False},
            {"confirmed": True, "public_host": "attacker.example"},
            {"confirmed": True, "fingerprint": "attacker-controlled"},
        ):
            assert (
                await client.post(
                    f"/control/vpn/endpoints/{endpoint_id}/external-verification",
                    json=payload,
                )
            ).status_code == 422

        missing = await client.post(
            "/control/vpn/endpoints/999999/external-verification",
            json={"confirmed": True},
        )
        unsuitable_response = await client.post(
            f"/control/vpn/endpoints/{unsuitable_id}/external-verification",
            json={"confirmed": True},
        )
        stale_response = await client.post(
            f"/control/vpn/endpoints/{stale_id}/external-verification",
            json={"confirmed": True},
        )
        assert missing.status_code == 404
        assert missing.json() == {"detail": "VPN endpoint not found"}
        assert unsuitable_response.status_code == 409
        assert unsuitable_response.json() == {"detail": "VPN endpoint is not eligible for confirmation"}
        assert stale_response.status_code == 409
        assert stale_response.json() == {"detail": "VPN endpoint is not eligible for confirmation"}

        confirmed = await client.post(
            f"/control/vpn/endpoints/{endpoint_id}/external-verification",
            json={"confirmed": True},
        )
        assert confirmed.status_code == 200
        assert set(confirmed.json()) == {"endpoint_id", "confirmed", "confirmed_at"}
        assert confirmed.json()["endpoint_id"] == endpoint_id
        assert confirmed.json()["confirmed"] is True
        first_confirmed_at = datetime.fromisoformat(confirmed.json()["confirmed_at"])
        repeated = await client.post(
            f"/control/vpn/endpoints/{endpoint_id}/external-verification",
            json={"confirmed": True},
        )
        assert repeated.status_code == 200
        assert datetime.fromisoformat(repeated.json()["confirmed_at"]) >= first_confirmed_at

    for secret in (
        "vpn-1.example",
        VALID_PUBLIC_KEY,
        "aabbcc01",
        "worker-secret-1",
        "identity-secret",
    ):
        assert secret not in confirmed.text
        assert secret not in repeated.text

    async with api_context.factory() as session:
        stored = await session.get(VpnEndpoint, endpoint_id)
        assert stored is not None
        assert stored.external_verified_at is not None
        assert stored.external_config_fingerprint == public_endpoint_fingerprint(_target(stored))
        assert len(stored.external_config_fingerprint) == 64
        retained_key = await session.get(VpnAccessKey, access_key_id)
        assert retained_key is not None
        assert retained_key.external_uuid == "11111111-1111-4111-8111-111111111111"
        assert retained_key.config_uri == "vless://identity-secret"
        audits = list(
            await session.scalars(
                select(AdminAuditLog)
                .where(AdminAuditLog.action == "vpn_endpoint_external_verification")
                .order_by(AdminAuditLog.id)
            )
        )
        assert len(audits) == 2
        assert {row.details for row in audits} == {f"endpoint_id={endpoint_id}"}
        for row in audits:
            assert row.target_user_id is None
            assert "vpn-1.example" not in (row.details or "")
            assert "public-key" not in (row.details or "")

    assert api_context.settings.vpn_public_trial_enabled is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("public_key", "A" * 42),
        ("short_id", "ABCD"),
        ("server_name", "bad name"),
        ("fingerprint", "bad\n"),
        ("flow", "vision"),
    ),
)
async def test_external_verification_rejects_malformed_reality_target_without_sealing(
    api_context,
    field: str,
    value: str,
) -> None:
    async with api_context.factory() as session:
        endpoint = await _add_ready_endpoint(
            session,
            index=1,
            now=api_context.now,
            externally_verified=False,
        )
        setattr(endpoint, field, value)
        await session.commit()
        endpoint_id = endpoint.id

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/control/vpn/endpoints/{endpoint_id}/external-verification",
            json={"confirmed": True},
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "VPN endpoint is not eligible for confirmation"}
    assert value not in response.text
    async with api_context.factory() as session:
        stored = await session.get(VpnEndpoint, endpoint_id)
        assert stored is not None
        assert stored.external_verified_at is None
        assert stored.external_config_fingerprint is None
        assert (
            await session.scalar(
                select(AdminAuditLog).where(
                    AdminAuditLog.action == "vpn_endpoint_external_verification"
                )
            )
        ) is None


@pytest.mark.asyncio
async def test_blocked_and_invalid_release_commits_do_not_change_marker(
    api_context,
    monkeypatch,
) -> None:
    async with api_context.factory() as session:
        session.add(AppSetting(key=READY_MARKER_KEY, value="b" * 64))
        await session.commit()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        blocked = await client.post("/control/vpn/release-readiness/commit")
    assert blocked.status_code == 409
    assert blocked.json() == {"detail": "VPN release is not ready"}

    async with api_context.factory() as session:
        marker = await session.scalar(select(AppSetting).where(AppSetting.key == READY_MARKER_KEY))
        assert marker is not None
        assert marker.value == "b" * 64
        assert (
            await session.scalar(
                select(AdminAuditLog).where(AdminAuditLog.action == "vpn_release_readiness_commit")
            )
        ) is None
        await _seed_ready_release(session, now=api_context.now)

    monkeypatch.setattr(
        control_routes,
        "_release_readiness_observations",
        lambda: _passing_observations(api_context.now),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        for invalid_release_id in ("", "A" * 64, "g" * 64):
            api_context.settings.vpn_public_trial_release_id = invalid_release_id
            invalid = await client.post("/control/vpn/release-readiness/commit")
            assert invalid.status_code == 409
            assert invalid.json() == {"detail": "VPN release is not ready"}

    async with api_context.factory() as session:
        marker = await session.scalar(select(AppSetting).where(AppSetting.key == READY_MARKER_KEY))
        assert marker is not None
        assert marker.value == "b" * 64
        assert (
            await session.scalar(
                select(AdminAuditLog).where(AdminAuditLog.action == "vpn_release_readiness_commit")
            )
        ) is None


@pytest.mark.asyncio
async def test_ready_commit_writes_exact_marker_audits_and_serializes(
    api_context,
    monkeypatch,
) -> None:
    async with api_context.factory() as session:
        first, _second = await _seed_ready_release(session, now=api_context.now)
        customer = VpnCustomer(telegram_user_id="release-owner")
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=first.worker_id,
            endpoint_id=first.id,
            status="active",
            external_uuid="22222222-2222-4222-8222-222222222222",
            config_uri="vless://release-identity-secret",
        )
        session.add(access_key)
        await session.commit()
        access_key_id = access_key.id

    monkeypatch.setattr(
        control_routes,
        "_release_readiness_observations",
        lambda: _passing_observations(api_context.now),
    )
    barrier_calls = 0
    active_barriers = 0
    max_active_barriers = 0
    sequence: list[str] = []

    async def tracked_barrier(db):
        del db
        nonlocal barrier_calls, active_barriers, max_active_barriers
        barrier_calls += 1
        active_barriers += 1
        max_active_barriers = max(max_active_barriers, active_barriers)
        sequence.append("barrier")
        try:
            await asyncio.sleep(0.01)
        finally:
            active_barriers -= 1

    monkeypatch.setattr(
        control_routes,
        "lock_release_readiness_tables",
        tracked_barrier,
    )
    original_loader = control_routes._load_vpn_release_readiness_report
    active_loaders = 0
    max_active_loaders = 0
    loader_calls = 0

    async def tracked_loader(db, settings):
        nonlocal active_loaders, max_active_loaders, loader_calls
        assert sequence[-1] == "barrier"
        sequence.append("loader")
        loader_calls += 1
        active_loaders += 1
        max_active_loaders = max(max_active_loaders, active_loaders)
        try:
            await asyncio.sleep(0.01)
            return await original_loader(db, settings)
        finally:
            active_loaders -= 1

    monkeypatch.setattr(
        control_routes,
        "_load_vpn_release_readiness_report",
        tracked_loader,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_context.app),
        base_url="http://testserver",
    ) as client:
        first_response, second_response = await asyncio.gather(
            client.post("/control/vpn/release-readiness/commit"),
            client.post("/control/vpn/release-readiness/commit"),
        )

    assert [first_response.status_code, second_response.status_code] == [200, 200]
    assert first_response.json() == {"detail": "VPN release readiness committed"}
    assert second_response.json() == {"detail": "VPN release readiness committed"}
    assert loader_calls == 2
    assert max_active_loaders == 1
    assert barrier_calls == 2
    assert max_active_barriers == 1
    assert sequence == ["barrier", "loader", "barrier", "loader"]

    async with api_context.factory() as session:
        markers = list(
            await session.scalars(select(AppSetting).where(AppSetting.key == READY_MARKER_KEY))
        )
        assert len(markers) == 1
        assert markers[0].value == RELEASE_ID
        audits = list(
            await session.scalars(
                select(AdminAuditLog)
                .where(AdminAuditLog.action == "vpn_release_readiness_commit")
                .order_by(AdminAuditLog.id)
            )
        )
        assert len(audits) == 2
        assert all(row.details is None for row in audits)
        retained_key = await session.get(VpnAccessKey, access_key_id)
        assert retained_key is not None
        assert retained_key.external_uuid == "22222222-2222-4222-8222-222222222222"
        assert retained_key.config_uri == "vless://release-identity-secret"

    assert api_context.settings.vpn_public_trial_enabled is False
    assert api_context.settings.vpn_public_trial_release_id == RELEASE_ID
