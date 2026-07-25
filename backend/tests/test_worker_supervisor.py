from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AttackEvent, AttackRun, ContactProfile, DropDomain, RegistrarAccount, WorkerNode, WorkerTask
from app.services import attack_runtime as attack_runtime_module
from app.services.attack_runtime import (
    autoplan_due_attack_runs,
    finalize_expired_attack_runs,
    rebalance_worker_pool,
    recompute_run_statistics,
    supervise_worker_pool,
)
from app.services.discovery import DiscoveryObservationInput


async def _make_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, session_factory


@pytest.mark.asyncio
async def test_autoplan_disables_domain_when_preflight_confirms_it_is_registered(monkeypatch):
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 7, 16, 4, 30, 45, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            contact = ContactProfile(
                label="Default FR",
                given_name="Jean",
                family_name="Dupont",
                email="ops@example.com",
                phone="+33100000000",
                street_address="1 Rue Test",
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
            )
            session.add(account)
            await session.flush()

            worker = WorkerNode(
                name="worker-primary",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                status="ready",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
                last_heartbeat_at=now,
            )
            domain = DropDomain(
                fqdn="anfas.fr",
                zone="fr",
                timezone_name="Europe/Paris",
                registrar_slug="gandi",
                registrar_account_id=account.id,
                contact_profile_id=contact.id,
                drop_date=now.date(),
                priority=200,
                status="queued",
                attack_enabled=True,
                auto_start_enabled=True,
                auto_start_lead_seconds=90,
                window_start_minute=31,
                window_start_second=59,
                window_duration_seconds=61,
            )
            session.add_all([worker, domain])
            await session.commit()

            async def fake_check(*_args, **_kwargs):
                return DiscoveryObservationInput(
                    source="rdap",
                    observed_at=now,
                    http_status=200,
                    lifecycle_stage="registered",
                    availability_status="taken",
                    status_codes=["ACTIVE", "addPeriod"],
                )

            monkeypatch.setattr(attack_runtime_module, "check_discovery_domain_rdap", fake_check)

            created_runs = await autoplan_due_attack_runs(session, now=now)
            await session.commit()

        async with session_factory() as session:
            domain = (await session.execute(select(DropDomain).where(DropDomain.fqdn == "anfas.fr"))).scalar_one()
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.id.asc()))
            ).scalars().all()

            assert created_runs == []
            assert domain.attack_enabled is False
            assert domain.status == "failed"
            assert domain.readiness_reasons == "Pre-start RDAP safety check confirmed domain is already registered"
            assert [event.event_type for event in events] == ["pre_start_domain_taken"]
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_supervise_worker_pool_marks_stalled_worker_offline_and_rebalances_domain():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            contact = ContactProfile(
                label="Default FR",
                given_name="Jean",
                family_name="Dupont",
                email="ops@example.com",
                phone="+33100000000",
                street_address="1 Rue Test",
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
                supports_dry_run=False,
            )
            session.add(account)
            await session.flush()

            stalled_worker = WorkerNode(
                name="worker-stalled",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                status="running",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
                last_heartbeat_at=now - timedelta(seconds=120),
            )
            spare_worker = WorkerNode(
                name="worker-spare",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                status="ready",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
                last_heartbeat_at=now,
            )
            session.add_all([stalled_worker, spare_worker])
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
                status="attacking",
                attack_enabled=True,
                window_start_minute=31,
                window_start_second=59,
                window_duration_seconds=61,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=now - timedelta(seconds=11),
                planned_end_at=now + timedelta(seconds=49),
                started_at=now - timedelta(seconds=11),
                assigned_worker_count=1,
                planned_rps=16.0,
                current_rps=8.0,
                max_rps=16.0,
            )
            session.add(run)
            await session.flush()

            stale_task = WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=stalled_worker.id,
                status="running",
                planned_rps=16.0,
                actual_rps=8.0,
                assigned_at=now - timedelta(seconds=11),
                acknowledged_at=now - timedelta(seconds=10),
                started_at=now - timedelta(seconds=10),
            )
            session.add(stale_task)
            await session.commit()

            affected = await supervise_worker_pool(session, now=now, stall_threshold_seconds=45)
            assert affected == 1
            created = await rebalance_worker_pool(session, now=now)
            await recompute_run_statistics(session)
            await session.commit()

            assert created == 1

        async with session_factory() as session:
            stalled_worker = (
                await session.execute(select(WorkerNode).where(WorkerNode.name == "worker-stalled"))
            ).scalar_one()
            spare_worker = (
                await session.execute(select(WorkerNode).where(WorkerNode.name == "worker-spare"))
            ).scalar_one()
            tasks = (
                await session.execute(select(WorkerTask).where(WorkerTask.domain_id == domain.id).order_by(WorkerTask.id.asc()))
            ).scalars().all()
            run = await session.get(AttackRun, run.id)
            domain = await session.get(DropDomain, domain.id)
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.created_at.asc()))
            ).scalars().all()

            assert stalled_worker.status == "offline"
            assert stalled_worker.current_rps == 0.0
            assert stalled_worker.current_capacity_rps == 0.0
            assert spare_worker.status == "ready"

            assert len(tasks) == 2
            assert tasks[0].worker_id == stalled_worker.id
            assert tasks[0].status == "failed"
            assert tasks[0].stop_reason == "Worker heartbeat stalled"

            assert tasks[1].worker_id == spare_worker.id
            assert tasks[1].status == "running"
            assert tasks[1].planned_rps > 0

            assert run is not None and run.status == "running"
            assert run.assigned_worker_count == 1
            assert run.planned_rps > 0
            assert domain is not None and domain.status == "attacking"

            event_types = {event.event_type for event in events}
            assert "worker_task_stalled" in event_types
            assert "worker_rebalanced" in event_types
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_expired_attack_runs_disables_domain_after_repeated_registered_rdap_checks():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            contact = ContactProfile(
                label="Default COM",
                given_name="Jean",
                family_name="Dupont",
                email="ops@example.com",
                phone="+33100000000",
                street_address="1 Rue Test",
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
                supports_dry_run=False,
            )
            session.add(account)
            await session.flush()

            worker = WorkerNode(
                name="worker-primary",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                status="running",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
                last_heartbeat_at=now,
            )
            session.add(worker)
            await session.flush()

            domain = DropDomain(
                fqdn="lost-example.com",
                zone="com",
                timezone_name="UTC",
                registrar_slug="gandi",
                registrar_account_id=account.id,
                contact_profile_id=contact.id,
                drop_date=now.date(),
                priority=200,
                status="attacking",
                attack_enabled=True,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=now - timedelta(seconds=90),
                planned_end_at=now - timedelta(seconds=10),
                started_at=now - timedelta(seconds=90),
                assigned_worker_count=1,
                planned_rps=16.0,
                current_rps=16.0,
                max_rps=16.0,
            )
            session.add(run)
            await session.flush()

            task = WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="running",
                planned_rps=16.0,
                actual_rps=16.0,
                assigned_at=now - timedelta(seconds=90),
                acknowledged_at=now - timedelta(seconds=89),
                started_at=now - timedelta(seconds=89),
            )
            session.add(task)
            await session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                if str(request.url) == "https://data.iana.org/rdap/dns.json":
                    return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
                if str(request.url) == "https://rdap.registry.example/domain/lost-example.com":
                    return httpx.Response(200, json={"status": ["client transfer prohibited"]})
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                for offset in (0, 61, 122):
                    await finalize_expired_attack_runs(
                        session,
                        now=now + timedelta(seconds=offset),
                        client=client,
                        confirmation_threshold=3,
                        check_interval_seconds=60,
                    )
                    await session.commit()

        async with session_factory() as session:
            domain = (await session.execute(select(DropDomain).where(DropDomain.fqdn == "lost-example.com"))).scalar_one()
            run = (await session.execute(select(AttackRun).where(AttackRun.domain_id == domain.id))).scalar_one()
            task = (await session.execute(select(WorkerTask).where(WorkerTask.domain_id == domain.id))).scalar_one()
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.id.asc()))
            ).scalars().all()

            assert domain.attack_enabled is False
            assert domain.status == "failed"
            assert domain.readiness_reasons == "Post-window RDAP safety check confirmed domain is already registered"
            assert run.status == "failed"
            assert run.stop_reason == "Post-window RDAP safety check confirmed domain is already registered"
            assert task.status == "cancelled"
            assert task.stop_reason == "Attack window expired; post-window RDAP verification started"
            assert [event.event_type for event in events].count("post_window_rdap_registered") == 3
            assert any(event.event_type == "post_window_domain_taken" for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_expired_attack_runs_marks_success_when_gandi_account_owns_registered_domain():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 7, 25, 3, 33, 10, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            contact = ContactProfile(
                label="Default SK",
                given_name="Jean",
                family_name="Dupont",
                email="ops@example.com",
                phone="+33100000000",
                street_address="1 Rue Test",
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
                supports_dry_run=False,
            )
            session.add(account)
            await session.flush()

            worker = WorkerNode(
                name="worker-primary",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                status="running",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
                last_heartbeat_at=now,
            )
            session.add(worker)
            await session.flush()

            domain = DropDomain(
                fqdn="bbcenter.sk",
                zone="sk",
                timezone_name="Europe/Bratislava",
                registrar_slug="gandi",
                registrar_account_id=account.id,
                contact_profile_id=contact.id,
                drop_date=now.date(),
                priority=200,
                status="attacking",
                attack_enabled=True,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=now - timedelta(seconds=95),
                planned_end_at=now - timedelta(seconds=5),
                started_at=now - timedelta(seconds=95),
                assigned_worker_count=1,
                planned_rps=240.0,
                current_rps=0.0,
                max_rps=240.0,
            )
            session.add(run)
            await session.flush()

            task = WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="running",
                planned_rps=16.0,
                actual_rps=0.0,
                assigned_at=now - timedelta(seconds=95),
                acknowledged_at=now - timedelta(seconds=94),
                started_at=now - timedelta(seconds=94),
            )
            session.add(task)
            await session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                if str(request.url) == "https://data.iana.org/rdap/dns.json":
                    return httpx.Response(200, json={"services": [[["sk"], ["https://rdap.registry.example/"]]]})
                if str(request.url) == "https://rdap.registry.example/domain/bbcenter.sk":
                    return httpx.Response(200, json={"status": ["active"]})
                if str(request.url) == "https://api.gandi.net/v5/domain/domains/bbcenter.sk":
                    assert request.headers["Authorization"] == "Bearer gandi-token"
                    return httpx.Response(200, json={"fqdn": "bbcenter.sk"})
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await finalize_expired_attack_runs(
                    session,
                    now=now,
                    client=client,
                    confirmation_threshold=1,
                    check_interval_seconds=60,
                )
                await session.commit()

        async with session_factory() as session:
            domain = (await session.execute(select(DropDomain).where(DropDomain.fqdn == "bbcenter.sk"))).scalar_one()
            run = (await session.execute(select(AttackRun).where(AttackRun.domain_id == domain.id))).scalar_one()
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.id.asc()))
            ).scalars().all()

            assert domain.status == "success"
            assert domain.success_at is not None
            assert run.status == "success"
            assert run.stop_reason == "Gandi ownership check confirmed domain was registered in this account"
            assert any(event.event_type == "post_window_domain_owned" for event in events)
            assert not any(event.event_type == "post_window_domain_taken" for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_expired_attack_runs_uses_fr_whois_when_rdap_is_stale_pending_delete():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 7, 16, 22, 33, 5, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            domain = DropDomain(
                fqdn="lesmotsdetrop.fr",
                zone="fr",
                timezone_name="Europe/Paris",
                registrar_slug="gandi",
                drop_date=now.date(),
                priority=200,
                status="attacking",
                attack_enabled=True,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=now - timedelta(seconds=95),
                planned_end_at=now - timedelta(seconds=5),
                started_at=now - timedelta(seconds=95),
                assigned_worker_count=0,
                planned_rps=80.0,
                current_rps=0.0,
                max_rps=80.0,
            )
            session.add(run)
            await session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                if str(request.url) == "https://data.iana.org/rdap/dns.json":
                    return httpx.Response(200, json={"services": [[["fr"], ["https://rdap.nic.fr/"]]]})
                if str(request.url) == "https://rdap.nic.fr/domain/lesmotsdetrop.fr":
                    return httpx.Response(200, json={"status": ["pendingDelete"]})
                return httpx.Response(404)

            async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
                assert fqdn == "lesmotsdetrop.fr"
                assert server == "whois.nic.fr"
                assert timeout_seconds == 5.0
                return """
domain:                        lesmotsdetrop.fr
status:                        ACTIVE
status:                        addPeriod
eppstatus:                     active
registrar:                     FUNCALL BV
created:                       2026-07-16T22:32:02.759659Z
source:                        FRNIC
"""

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                await finalize_expired_attack_runs(
                    session,
                    now=now,
                    client=client,
                    confirmation_threshold=1,
                    check_interval_seconds=60,
                    whois_lookup=whois_lookup,
                )
                await session.commit()

        async with session_factory() as session:
            domain = (await session.execute(select(DropDomain).where(DropDomain.fqdn == "lesmotsdetrop.fr"))).scalar_one()
            run = (await session.execute(select(AttackRun).where(AttackRun.domain_id == domain.id))).scalar_one()
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.id.asc()))
            ).scalars().all()

            assert domain.attack_enabled is False
            assert domain.status == "failed"
            assert run.status == "failed"
            assert any(event.event_type == "post_window_rdap_registered" for event in events)
            assert any("source=whois_fallback" in event.message for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_finalize_expired_attack_runs_keeps_domain_enabled_when_rdap_is_not_conclusive():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    try:
        async with session_factory() as session:
            domain = DropDomain(
                fqdn="uncertain-example.com",
                zone="com",
                timezone_name="UTC",
                registrar_slug="gandi",
                drop_date=now.date(),
                priority=200,
                status="attacking",
                attack_enabled=True,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=now - timedelta(seconds=90),
                planned_end_at=now - timedelta(seconds=10),
                started_at=now - timedelta(seconds=90),
                assigned_worker_count=0,
                planned_rps=16.0,
                current_rps=0.0,
                max_rps=16.0,
            )
            session.add(run)
            await session.commit()

            def handler(request: httpx.Request) -> httpx.Response:
                if str(request.url) == "https://data.iana.org/rdap/dns.json":
                    return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
                if str(request.url) == "https://rdap.registry.example/domain/uncertain-example.com":
                    return httpx.Response(500, text="registry temporary error")
                return httpx.Response(404)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                for offset in (0, 61, 122):
                    await finalize_expired_attack_runs(
                        session,
                        now=now + timedelta(seconds=offset),
                        client=client,
                        confirmation_threshold=3,
                        check_interval_seconds=60,
                    )
                    await session.commit()

        async with session_factory() as session:
            domain = (
                await session.execute(select(DropDomain).where(DropDomain.fqdn == "uncertain-example.com"))
            ).scalar_one()
            run = (await session.execute(select(AttackRun).where(AttackRun.domain_id == domain.id))).scalar_one()
            events = (
                await session.execute(select(AttackEvent).where(AttackEvent.domain_id == domain.id).order_by(AttackEvent.id.asc()))
            ).scalars().all()

            assert domain.attack_enabled is True
            assert domain.status == "queued"
            assert run.status == "failed"
            assert run.stop_reason == "Post-window RDAP safety checks were inconclusive; domain remains enabled"
            assert not any(event.event_type == "post_window_domain_taken" for event in events)
            assert [event.event_type for event in events].count("post_window_rdap_inconclusive") == 3
    finally:
        await engine.dispose()
