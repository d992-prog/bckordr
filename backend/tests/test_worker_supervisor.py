from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import AttackEvent, AttackRun, ContactProfile, DropDomain, RegistrarAccount, WorkerNode, WorkerTask
from app.services.attack_runtime import (
    finalize_expired_attack_runs,
    rebalance_worker_pool,
    recompute_run_statistics,
    supervise_worker_pool,
)


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
