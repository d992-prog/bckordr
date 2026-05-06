from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import _apply_domain_readiness, router as control_router
from app.db.base import Base
from app.db.models import ContactProfile, DropDomain, RegistrarAccount, ZoneRule, ZoneStrategy
from app.db.session import get_db
from app.schemas.control import (
    ContactProfileCreateRequest,
    DomainOverrideRuleCreateRequest,
    DomainOverrideRulePhaseCreateRequest,
    ZoneRuleCreateRequest,
    ZoneRulePhaseCreateRequest,
    ZoneStrategyCreateRequest,
)
from app.services.strategy_runtime import evaluate_domain_readiness


def test_zone_strategy_request_defaults_to_priority_resolution():
    payload = ZoneStrategyCreateRequest(zone="fr", name="France default", timezone_name="Europe/Paris")

    assert payload.rule_resolution_mode == "priority"


def test_domain_is_draft_without_strategy_or_account():
    domain = SimpleNamespace(
        registrar_account_id=None,
        contact_profile_id=None,
        drop_date=None,
        attack_enabled=True,
    )

    result = evaluate_domain_readiness(domain, effective_strategy=None)

    assert result.status == "draft"
    assert "strategy" in result.reasons[0]


def test_zone_rule_request_defaults_to_hourly_flat_mode():
    payload = ZoneRuleCreateRequest(name="FR hourly", schedule_type="hourly")

    assert payload.minute == 31
    assert payload.execution_profile_mode == "flat"


def test_zone_rule_phase_defaults_to_percent_mode():
    payload = ZoneRulePhaseCreateRequest(name="burst")

    assert payload.rps_mode == "percent"
    assert payload.rps_value == 100.0


def test_domain_override_rule_request_defaults_to_hourly_flat_mode():
    payload = DomainOverrideRuleCreateRequest(name="Manual FR hourly", schedule_type="hourly")

    assert payload.minute == 31
    assert payload.execution_profile_mode == "flat"


def test_domain_override_rule_phase_defaults_to_percent_mode():
    payload = DomainOverrideRulePhaseCreateRequest(name="burst")

    assert payload.rps_mode == "percent"
    assert payload.rps_value == 100.0


@pytest.mark.asyncio
async def test_domain_override_api_supports_create_rule_phase_and_preview():
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
        domain = DropDomain(
            fqdn="manual.fr",
            zone="fr",
            timezone_name="Europe/Paris",
            registrar_slug="gandi",
            drop_date=date(2026, 5, 5),
            strategy_mode="manual_override",
            registrar_account_id=1,
            contact_profile_id=1,
            attack_enabled=True,
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

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        override_response = await client.post(
            f"/control/domains/{domain_id}/override",
            json={
                "timezone_name": "Europe/Paris",
                "rule_resolution_mode": "priority",
                "default_min_guaranteed_rps": 2.0,
                "notes": "manual domain",
            },
        )
        assert override_response.status_code == 201
        override_id = override_response.json()["id"]

        rule_response = await client.post(
            f"/control/domains/{domain_id}/override/rules",
            json={
                "name": "Manual hourly",
                "schedule_type": "hourly",
                "minute": 31,
                "second": 59,
                "window_duration_seconds": 61,
                "priority": 100,
                "execution_profile_mode": "phased",
            },
        )
        assert rule_response.status_code == 201
        rule_id = rule_response.json()["id"]

        phase_response = await client.post(
            f"/control/domain-override-rules/{rule_id}/phases",
            json={
                "name": "burst",
                "sort_order": 0,
                "start_offset_seconds": 0,
                "duration_seconds": 61,
                "rps_mode": "fixed",
                "rps_value": 9.0,
                "stop_on_success": True,
            },
        )
        assert phase_response.status_code == 201
        assert phase_response.json()["domain_override_rule_id"] == rule_id

        preview_response = await client.get(
            f"/control/domains/{domain_id}/override/preview",
            params={"target_date": "2026-05-05"},
        )
        assert preview_response.status_code == 200
        payload = preview_response.json()
        assert payload["strategy_id"] == override_id
        assert payload["resolution_mode"] == "priority"
        assert payload["windows"][0]["rule_id"] == rule_id

    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_domain_readiness_marks_inherited_zone_strategy_ready_when_rule_exists():
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
            label="Default contact",
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
            name="Gandi account",
            registrar_slug="gandi",
            api_token="token",
            default_contact_profile_id=contact.id,
            is_active=True,
        )
        session.add(account)
        await session.flush()

        strategy = ZoneStrategy(
            zone="fr",
            name="France Default",
            timezone_name="Europe/Paris",
            rule_resolution_mode="priority",
            default_registrar_slug="gandi",
            is_active=True,
        )
        session.add(strategy)
        await session.flush()

        rule = ZoneRule(
            zone_strategy_id=strategy.id,
            name="FR hourly",
            schedule_type="hourly",
            minute=31,
            second=59,
            window_duration_seconds=61,
            priority=100,
            is_enabled=True,
        )
        session.add(rule)
        await session.flush()

        domain = DropDomain(
            fqdn="dryrun-20260506-01.fr",
            zone="fr",
            timezone_name="Europe/Paris",
            registrar_slug="gandi",
            zone_strategy_id=strategy.id,
            strategy_mode="inherit_zone",
            registrar_account_id=account.id,
            contact_profile_id=contact.id,
            drop_date=date(2026, 5, 6),
            attack_enabled=True,
        )
        session.add(domain)
        await session.flush()

        await _apply_domain_readiness(session, domain)

        assert domain.status == "ready"
        assert domain.readiness_reasons is None

    await engine.dispose()


def test_contact_profile_request_accepts_gandi_prefill_fields():
    payload = ContactProfileCreateRequest(
        label="Imported from Gandi",
        person_type="individual",
        given_name="Alice",
        family_name="Doe",
        email="alice@example.org",
        phone="+33123456789",
        mobile="+33987654321",
        fax="+33111111111",
        lang="fr",
        street_address="5 rue neuve",
        city="Paris",
        state="FR-IDF",
        zip_code="75001",
        country_code="FR",
        data_obfuscated=True,
        mail_obfuscated=False,
        icann_contract_accept=True,
        extra_parameters='{"local_presence":"fr"}',
    )

    assert payload.mobile == "+33987654321"
    assert payload.lang == "fr"
    assert payload.icann_contract_accept is True


@pytest.mark.asyncio
async def test_registrar_account_prefill_contact_returns_gandi_draft(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.db.models import RegistrarAccount

    async with session_factory() as session:
        account = RegistrarAccount(
            name="Gandi sandbox",
            registrar_slug="gandi",
            api_token="sandbox-token",
            api_base_url="https://api.sandbox.gandi.net/v5/domain/domains",
            sharing_id="org-123",
            is_active=True,
            supports_dry_run=True,
        )
        session.add(account)
        await session.commit()
        await session.refresh(account)
        account_id = account.id

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    async def fake_prefill(account, settings):
        return {
            "label": "Gandi import | Alice Doe",
            "person_type": "individual",
            "given_name": "Alice",
            "family_name": "Doe",
            "organization_name": None,
            "email": "alice@example.org",
            "phone": "+33123456789",
            "mobile": "+33987654321",
            "fax": None,
            "lang": "fr",
            "street_address": "5 rue neuve",
            "city": "Paris",
            "state": "FR-IDF",
            "zip_code": "75001",
            "country_code": "FR",
            "data_obfuscated": True,
            "mail_obfuscated": False,
            "icann_contract_accept": True,
            "extra_parameters": None,
            "is_default": False,
            "notes": "Imported from Gandi sandbox",
        }

    monkeypatch.setattr("app.api.routes.control.build_gandi_contact_prefill", fake_prefill)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/control/registrar-accounts/{account_id}/prefill-contact")
        assert response.status_code == 200
        payload = response.json()
        assert payload["label"] == "Gandi import | Alice Doe"
        assert payload["email"] == "alice@example.org"
        assert payload["mobile"] == "+33987654321"
        assert payload["lang"] == "fr"

    await engine.dispose()
