from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import MethodType

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.routes.worker_runtime import router as worker_runtime_router
from app.db.base import Base
from app.db.models import AttackEvent, AttackRun, ContactProfile, DropDomain, RegistrarAccount, WorkerNode, WorkerTask
from app.db.session import get_db


def _load_worker_runtime_classes():
    worker_app_dir = Path(__file__).resolve().parents[2] / "worker" / "app"
    saved_modules = {name: sys.modules.get(name) for name in ("app", "app.config", "app.control_client", "app.gandi", "app.runner")}

    worker_pkg = types.ModuleType("app")
    worker_pkg.__path__ = [str(worker_app_dir)]
    sys.modules["app"] = worker_pkg

    loaded = {}
    try:
        for module_name in ("config", "control_client", "gandi", "runner"):
            module_path = worker_app_dir / f"{module_name}.py"
            spec = importlib.util.spec_from_file_location(f"app.{module_name}", module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"app.{module_name}"] = module
            spec.loader.exec_module(module)
            loaded[module_name] = module
    finally:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return loaded["config"].WorkerSettings, loaded["runner"].WorkerRunner


WorkerSettings, WorkerRunner = _load_worker_runtime_classes()


async def _make_test_session_factory():
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


def _make_test_app(session_factory: async_sessionmaker[AsyncSession]) -> FastAPI:
    app = FastAPI()
    app.include_router(worker_runtime_router, prefix="/api")

    async def override_get_db():
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    return app


async def _seed_runtime_state(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    now = datetime.now(timezone.utc)
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

        worker_primary = WorkerNode(
            name="worker-primary",
            registrar_slug="gandi",
            assigned_registrar_account_id=account.id,
            control_token="worker-token",
            status="ready",
            is_enabled=True,
            target_rps=16.0,
            max_rps=16.0,
        )
        worker_sibling = WorkerNode(
            name="worker-sibling",
            registrar_slug="gandi",
            assigned_registrar_account_id=account.id,
            control_token="worker-token-2",
            status="ready",
            is_enabled=True,
            target_rps=16.0,
            max_rps=16.0,
        )
        session.add_all([worker_primary, worker_sibling])
        await session.flush()

        domain = DropDomain(
            fqdn="alpha.fr",
            zone="fr",
            timezone_name="Europe/Paris",
            registrar_slug="gandi",
            registrar_account_id=account.id,
            contact_profile_id=contact.id,
            drop_date=now.astimezone(timezone.utc).date(),
            priority=200,
            status="attacking",
            attack_enabled=True,
        )
        session.add(domain)
        await session.flush()

        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now - timedelta(seconds=1),
            planned_end_at=now + timedelta(seconds=5),
            started_at=now - timedelta(seconds=1),
            assigned_worker_count=2,
            planned_rps=2.0,
            current_rps=0.0,
            max_rps=32.0,
        )
        session.add(run)
        await session.flush()

        primary_task = WorkerTask(
            attack_run_id=run.id,
            domain_id=domain.id,
            worker_id=worker_primary.id,
            status="running",
            planned_rps=1.0,
            assigned_at=now,
            acknowledged_at=now,
            started_at=now,
        )
        sibling_task = WorkerTask(
            attack_run_id=run.id,
            domain_id=domain.id,
            worker_id=worker_sibling.id,
            status="queued",
            planned_rps=1.0,
            assigned_at=now,
        )
        session.add_all([primary_task, sibling_task])
        await session.commit()

        return {
            "contact_id": contact.id,
            "account_id": account.id,
            "worker_primary_id": worker_primary.id,
            "worker_sibling_id": worker_sibling.id,
            "domain_id": domain.id,
            "run_id": run.id,
            "primary_task_id": primary_task.id,
            "sibling_task_id": sibling_task.id,
        }


@pytest.mark.asyncio
async def test_worker_runtime_harness_covers_live_rps_update_and_success_flow():
    engine, session_factory = await _make_test_session_factory()
    try:
        ids = await _seed_runtime_state(session_factory)
        app = _make_test_app(session_factory)

        settings = WorkerSettings(
            CONTROL_BASE_URL="http://control.test",
            WORKER_ID=ids["worker_primary_id"],
            CONTROL_TOKEN="worker-token",
            POLL_INTERVAL_SECONDS=0.05,
            HEARTBEAT_INTERVAL_SECONDS=0.05,
            REQUEST_TIMEOUT_SECONDS=2.0,
            CONNECT_TIMEOUT_SECONDS=1.0,
            SIMULATE_MODE=False,
        )
        runner = WorkerRunner(settings)
        await runner.control.client.aclose()
        runner.control.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://control.test",
            headers={"X-Worker-Token": "worker-token"},
            timeout=httpx.Timeout(2.0, connect=1.0),
        )

        await runner._heartbeat(status="ready")
        task = await runner.control.next_task()
        assert task is not None
        assert task.task_id == ids["primary_task_id"]
        await runner.control.acknowledge_task(task.task_id)

        async with session_factory() as session:
            acknowledged_task = await session.get(WorkerTask, ids["primary_task_id"])
            assert acknowledged_task is not None
            assert acknowledged_task.status == "running"
            assert acknowledged_task.acknowledged_at is not None

        observed_planned_rps: list[float] = []

        async def fake_attempt(self, client, live_task):
            observed_planned_rps.append(live_task.planned_rps)
            if len(observed_planned_rps) == 1:
                await asyncio.sleep(1.1)
                return 503, 12.0, "retry"
            await asyncio.sleep(0.05)
            return 200, 8.0, "registered"

        runner._attempt_register = MethodType(fake_attempt, runner)

        async def bump_task_rps():
            await asyncio.sleep(0.05)
            async with session_factory() as session:
                primary_task = await session.get(WorkerTask, ids["primary_task_id"])
                assert primary_task is not None
                primary_task.planned_rps = 4.0
                await session.commit()

        bump_task = asyncio.create_task(bump_task_rps())
        await runner._execute_task(task)
        await bump_task
        await runner.control.client.aclose()

        assert observed_planned_rps[0] == 1.0
        assert any(value >= 4.0 for value in observed_planned_rps[1:])

        async with session_factory() as session:
            domain = await session.get(DropDomain, ids["domain_id"])
            run = await session.get(AttackRun, ids["run_id"])
            primary_task = await session.get(WorkerTask, ids["primary_task_id"])
            sibling_task = await session.get(WorkerTask, ids["sibling_task_id"])
            worker = await session.get(WorkerNode, ids["worker_primary_id"])
            events = (
                await session.execute(
                    select(AttackEvent).where(AttackEvent.domain_id == ids["domain_id"]).order_by(AttackEvent.created_at.asc())
                )
            ).scalars().all()

            assert domain is not None and domain.status == "success"
            assert run is not None and run.status == "success"
            assert primary_task is not None and primary_task.status == "success"
            assert sibling_task is not None and sibling_task.status == "cancelled"
            assert worker is not None and worker.last_seen_at is not None
            assert any(event.event_type == "task_ack" for event in events)
            assert any(event.event_type == "domain_registered" for event in events)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_acknowledge_transitions_queued_task_to_running():
    engine, session_factory = await _make_test_session_factory()
    try:
        now = datetime.now(timezone.utc)
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

            worker = WorkerNode(
                name="worker-primary",
                registrar_slug="gandi",
                assigned_registrar_account_id=account.id,
                control_token="worker-token",
                status="ready",
                is_enabled=True,
                target_rps=16.0,
                max_rps=16.0,
            )
            session.add(worker)
            await session.flush()

            domain = DropDomain(
                fqdn="queued.fr",
                zone="fr",
                timezone_name="Europe/Paris",
                registrar_slug="gandi",
                registrar_account_id=account.id,
                contact_profile_id=contact.id,
                drop_date=now.date(),
                priority=200,
                status="scheduled",
                attack_enabled=True,
            )
            session.add(domain)
            await session.flush()

            run = AttackRun(
                domain_id=domain.id,
                status="planned",
                planned_start_at=now + timedelta(seconds=30),
                planned_end_at=now + timedelta(seconds=90),
                assigned_worker_count=1,
                planned_rps=16.0,
                current_rps=0.0,
                max_rps=16.0,
            )
            session.add(run)
            await session.flush()

            task = WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="queued",
                planned_rps=16.0,
                assigned_at=now,
            )
            session.add(task)
            await session.commit()
            task_id = task.id
            worker_id = worker.id

        app = _make_test_app(session_factory)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver/api",
            headers={"X-Worker-Token": "worker-token"},
        ) as client:
            response = await client.post(
                f"/worker-runtime/tasks/{task_id}/ack",
                json={"worker_id": worker_id},
            )
            assert response.status_code == 200

        async with session_factory() as session:
            task = await session.get(WorkerTask, task_id)
            assert task is not None
            assert task.status == "running"
            assert task.acknowledged_at is not None
            assert task.started_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_simulate_mode_supports_controlled_failure_and_success_rates():
    settings = WorkerSettings(
        CONTROL_BASE_URL="http://control.test",
        WORKER_ID=1,
        CONTROL_TOKEN="worker-token",
        SIMULATE_MODE=True,
        SIMULATE_LATENCY_MS=1,
        SIMULATE_JITTER_MS=0,
        SIMULATE_SUCCESS_RATE=0.0,
        SIMULATE_SUCCESS_STATUS_CODE=200,
        SIMULATE_FAILURE_STATUS_CODE=503,
        SIMULATE_RANDOM_SEED=7,
    )
    runner = WorkerRunner(settings)
    task = types.SimpleNamespace()
    try:
        status_code, _latency_ms, body = await runner._attempt_register(client=None, task=task)
        assert status_code == 503
        assert body == "simulated failure"

        runner.settings.simulate_success_rate = 1.0
        status_code, _latency_ms, body = await runner._attempt_register(client=None, task=task)
        assert status_code == 200
        assert body == "simulated success"
    finally:
        await runner.close()
