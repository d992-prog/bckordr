from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import ContactProfile, DropDomain, RegistrarAccount
from app.db.session import get_db
from app.services.gandi_dry_run import GandiDryRunResult


@pytest.mark.asyncio
async def test_domain_dry_run_endpoint_persists_gandi_result(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        contact = ContactProfile(
            label="Gandi default",
            person_type="individual",
            given_name="Alice",
            family_name="Doe",
            email="alice@example.org",
            phone="+33123456789",
            street_address="5 rue neuve",
            city="Paris",
            zip_code="75001",
            country_code="FR",
            lang="fr",
            icann_contract_accept=True,
            is_default=True,
        )
        session.add(contact)
        await session.flush()

        account = RegistrarAccount(
            name="Gandi main",
            registrar_slug="gandi",
            api_token="gandi-token",
            sharing_id="org-123",
            default_contact_profile_id=contact.id,
            is_active=True,
            supports_dry_run=True,
        )
        session.add(account)
        await session.flush()

        domain = DropDomain(
            fqdn="alpha.fr",
            zone="fr",
            timezone_name="Europe/Paris",
            registrar_slug="gandi",
            registrar_account_id=account.id,
            contact_profile_id=contact.id,
            drop_date=date(2026, 5, 5),
            priority=200,
            attack_enabled=True,
            status="ready",
            registration_extra_parameters='{"fr_lock": true}',
        )
        session.add(domain)
        await session.commit()
        await session.refresh(domain)
        domain_id = domain.id

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async def fake_run_gandi_domain_dry_run(domain, account, contact, settings):
        assert domain.registration_extra_parameters == '{"fr_lock": true}'
        assert account.api_token == "gandi-token"
        assert contact.lang == "fr"
        return GandiDryRunResult(
            status="ready",
            http_status=200,
            message='{"message":"Dry-run passed"}',
            checked_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        )

    monkeypatch.setattr("app.api.routes.control.run_gandi_domain_dry_run", fake_run_gandi_domain_dry_run)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/control/domains/{domain_id}/dry-run")
        assert response.status_code == 200
        payload = response.json()
        assert payload["domain_id"] == domain_id
        assert payload["status"] == "ready"
        assert payload["http_status"] == 200
        assert "Dry-run passed" in payload["message"]

    async with session_factory() as session:
        stored = await session.get(DropDomain, domain_id)
        assert stored is not None
        assert stored.dry_run_checked_at is not None
        assert stored.dry_run_status == "ready"
        assert stored.dry_run_http_status == 200
        assert "Dry-run passed" in (stored.dry_run_message or "")

    await engine.dispose()


@pytest.mark.asyncio
async def test_domain_dry_run_batch_endpoint_returns_summary(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        contact = ContactProfile(
            label="Gandi default",
            person_type="individual",
            given_name="Alice",
            family_name="Doe",
            email="alice@example.org",
            phone="+33123456789",
            street_address="5 rue neuve",
            city="Paris",
            zip_code="75001",
            country_code="FR",
            is_default=True,
        )
        session.add(contact)
        await session.flush()

        account = RegistrarAccount(
            name="Gandi main",
            registrar_slug="gandi",
            api_token="gandi-token",
            default_contact_profile_id=contact.id,
            is_active=True,
            supports_dry_run=True,
        )
        session.add(account)
        await session.flush()

        session.add_all(
            [
                DropDomain(
                    fqdn="alpha.fr",
                    zone="fr",
                    timezone_name="Europe/Paris",
                    registrar_slug="gandi",
                    registrar_account_id=account.id,
                    contact_profile_id=contact.id,
                    drop_date=date(2026, 5, 5),
                    priority=200,
                    attack_enabled=True,
                    status="ready",
                ),
                DropDomain(
                    fqdn="beta.fr",
                    zone="fr",
                    timezone_name="Europe/Paris",
                    registrar_slug="gandi",
                    registrar_account_id=account.id,
                    contact_profile_id=contact.id,
                    drop_date=date(2026, 5, 5),
                    priority=150,
                    attack_enabled=True,
                    status="ready",
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    async def fake_run_gandi_domain_dry_run(domain, account, contact, settings):
        status = "ready" if domain.fqdn == "alpha.fr" else "invalid"
        return GandiDryRunResult(
            status=status,
            http_status=200 if status == "ready" else 409,
            message=f"{domain.fqdn} -> {status}",
            checked_at=datetime(2026, 5, 5, 12, 30, tzinfo=timezone.utc),
        )

    monkeypatch.setattr("app.api.routes.control.run_gandi_domain_dry_run", fake_run_gandi_domain_dry_run)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/control/domains/dry-run/batch", json={"only_ready": True})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 2
        assert payload["ready"] == 1
        assert payload["invalid"] == 1
        assert payload["error"] == 0
        assert [item["domain_id"] for item in payload["results"]] == [1, 2]

    await engine.dispose()
