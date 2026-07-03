from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import DiscoveryDomain
from app.db.session import get_db
from app.services.discovery import (
    DiscoveryObservationInput,
    apply_discovery_observation,
    calculate_next_check_at,
    check_discovery_domain_rdap,
    normalize_lifecycle_stage,
    process_due_discovery_domains,
    resolve_rdap_domain_url,
)


def test_discovery_normalizes_epp_statuses():
    assert normalize_lifecycle_stage(["clientTransferProhibited", "redemptionPeriod"]) == "redemption"
    assert normalize_lifecycle_stage(["pendingDelete"]) == "pending_delete"
    assert normalize_lifecycle_stage([], http_status=404) == "not_found"


def test_discovery_resolves_rdap_url_from_iana_bootstrap():
    bootstrap = {
        "services": [
            [["org"], ["https://rdap.publicinterestregistry.example/rdap/"]],
            [["com", "net"], ["https://rdap.verisign.example/com/v1/"]],
        ]
    }

    assert (
        resolve_rdap_domain_url("example.com", bootstrap)
        == "https://rdap.verisign.example/com/v1/domain/example.com"
    )


@pytest.mark.asyncio
async def test_discovery_rdap_check_updates_domain_and_records_observation():
    previous_seen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="example.com", zone="com", last_checked_at=previous_seen)

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(
                200,
                json={"services": [[["com"], ["https://rdap.registry.example/"]]]},
            )
        if str(request.url) == "https://rdap.registry.example/domain/example.com":
            return httpx.Response(200, json={"status": ["pendingDelete"]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client)

    apply_discovery_observation(domain, observation)

    assert observation.source == "rdap"
    assert observation.http_status == 200
    assert observation.lifecycle_stage == "pending_delete"
    assert domain.status == "pending_delete"
    assert domain.predicted_drop_start_at == previous_seen + timedelta(days=5)


def test_pending_delete_observation_creates_drop_range():
    previous_seen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 6, 1, 12, 15, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="sample.com", zone="com", last_checked_at=previous_seen)

    apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=200,
            lifecycle_stage="pending_delete",
            status_codes=["pendingDelete"],
            raw_response='{"status":["pendingDelete"]}',
        ),
    )

    assert domain.status == "pending_delete"
    assert domain.first_seen_pending_delete_at == observed_at
    assert domain.pending_delete_previous_seen_at == previous_seen
    assert domain.predicted_drop_start_at == previous_seen + timedelta(days=5)
    assert domain.predicted_drop_end_at == observed_at + timedelta(days=5)


def test_discovery_uses_ten_second_interval_on_predicted_drop_day():
    now = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        status="pending_delete",
        predicted_drop_start_at=datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc),
        predicted_drop_end_at=datetime(2026, 6, 6, 23, 59, tzinfo=timezone.utc),
    )

    assert calculate_next_check_at(domain, now) == now + timedelta(seconds=10)


def test_discovery_uses_fifteen_minute_interval_for_redemption_domains():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        status="redemption",
        last_lifecycle_stage="redemption",
    )

    assert calculate_next_check_at(domain, now) == now + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_discovery_api_imports_and_lists_domains():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/control/discovery/domains/import",
            json={"domains": ["Example.COM", "example.com", "test.org"], "notes": "seed"},
        )
        assert response.status_code == 201
        assert response.json()["skipped"] == ["example.com"]

        list_response = await client.get("/control/discovery/domains")
        assert list_response.status_code == 200
        payload = list_response.json()
        assert [item["fqdn"] for item in payload] == ["example.com", "test.org"]
        assert payload[0]["zone"] == "com"
        assert payload[0]["status"] == "tracking"
        first_check = datetime.fromisoformat(payload[0]["next_check_at"])
        second_check = datetime.fromisoformat(payload[1]["next_check_at"])
        assert abs((second_check - first_check).total_seconds()) >= 200

    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_discovery_domains_persists_observation_and_notification():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(DiscoveryDomain(fqdn="example.com", zone="com", next_check_at=now))
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        if str(request.url) == "https://rdap.registry.example/domain/example.com":
            return httpx.Response(200, json={"status": ["pendingDelete"]})
        return httpx.Response(404)

    notifications: list[str] = []

    async with session_factory() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            processed = await process_due_discovery_domains(
                session,
                now=now,
                batch_size=5,
                client=client,
                notify=notifications.append,
            )
        await session.commit()

    async with session_factory() as session:
        domain = await session.get(DiscoveryDomain, 1)

    assert processed == 1
    assert domain is not None
    assert domain.status == "pending_delete"
    assert domain.next_check_at == (now + timedelta(minutes=5)).replace(tzinfo=None)
    assert notifications
    assert "pendingDelete" in notifications[0]

    await engine.dispose()
