from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import WorkerNode
from app.db.session import get_db
from app.services.worker_allowlist import render_worker_runtime_allowlist


def test_render_worker_runtime_allowlist_includes_worker_ips_and_deny_all():
    workers = [
        SimpleNamespace(ip_address="2.27.40.3", is_enabled=True),
        SimpleNamespace(ip_address="203.0.113.9", is_enabled=True),
        SimpleNamespace(ip_address=None, is_enabled=True),
        SimpleNamespace(ip_address="2.27.40.3", is_enabled=False),
    ]

    rendered = render_worker_runtime_allowlist(workers)

    assert "allow 2.27.40.3;" in rendered
    assert "allow 203.0.113.9;" in rendered
    assert rendered.strip().endswith("deny all;")
    assert rendered.count("allow 2.27.40.3;") == 1


@pytest.mark.asyncio
async def test_worker_crud_triggers_allowlist_sync(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    calls: list[int] = []

    async def fake_sync(session, settings):
        del settings
        worker_count = len((await session.execute(WorkerNode.__table__.select())).all())
        calls.append(worker_count)
        return False

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)

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
        create_response = await client.post(
            "/control/workers",
            json={
                "name": "worker-1",
                "registrar_slug": "gandi",
                "control_token": "token-1",
                "status": "ready",
                "ip_address": "2.27.40.3",
                "max_rps": 16,
                "target_rps": 16,
                "current_rps": 0,
                "current_capacity_rps": 0,
                "cpu_load": 0,
                "ram_usage_percent": 0,
                "clock_drift_ms": 0,
                "is_enabled": True,
            },
        )
        assert create_response.status_code == 201
        worker_id = create_response.json()["id"]

        update_response = await client.patch(
            f"/control/workers/{worker_id}",
            json={"ip_address": "2.27.40.4"},
        )
        assert update_response.status_code == 200

        delete_response = await client.delete(f"/control/workers/{worker_id}")
        assert delete_response.status_code == 200

    assert len(calls) == 3
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_setup_endpoint_generates_runtime_commands(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def fake_sync(session, settings):
        del session, settings
        return False

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)

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
        create_response = await client.post(
            "/control/workers",
            json={
                "name": "worker-setup",
                "registrar_slug": "gandi",
                "control_token": "token-setup",
                "status": "ready",
                "ip_address": "2.27.20.255",
                "max_rps": 16,
                "target_rps": 16,
                "current_rps": 0,
                "current_capacity_rps": 0,
                "cpu_load": 0,
                "ram_usage_percent": 0,
                "clock_drift_ms": 0,
                "is_enabled": True,
            },
        )
        assert create_response.status_code == 201
        worker_id = create_response.json()["id"]

        setup_response = await client.get(
            f"/control/workers/{worker_id}/setup",
            params={"simulate_mode": "false", "runtime_base_url": "http://2.27.21.88:8080"},
        )

    assert setup_response.status_code == 200
    payload = setup_response.json()
    assert payload["worker_id"] == worker_id
    assert payload["runtime_base_url"] == "http://2.27.21.88:8080"
    assert payload["simulate_mode"] is False
    assert "WORKER_ID=1" in payload["write_env_command"]
    assert "CONTROL_TOKEN=token-setup" in payload["write_env_command"]
    assert "SIMULATE_MODE=false" in payload["write_env_command"]
    assert any("systemctl enable --now domain-drop-worker.service" in command for command in payload["full_install_commands"])
    assert any("SIMULATE_MODE=true" in command for command in payload["switch_to_test_commands"])
    assert any("SIMULATE_MODE=false" in command for command in payload["switch_to_live_commands"])
    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_create_accepts_ssh_access_without_exposing_password(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def fake_sync(session, settings):
        del session, settings
        return False

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)

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
        create_response = await client.post(
            "/control/workers",
            json={
                "name": "worker-with-ssh",
                "registrar_slug": "gandi",
                "status": "ready",
                "ip_address": "2.27.20.255",
                "region": "DE",
                "ssh_host": "2.27.20.255",
                "ssh_port": 22,
                "ssh_username": "root",
                "ssh_password": "temporary-root-password",
                "max_rps": 16,
                "target_rps": 16,
                "is_enabled": True,
            },
        )

        assert create_response.status_code == 201
        payload = create_response.json()
        assert payload["ssh_host"] == "2.27.20.255"
        assert payload["ssh_port"] == 22
        assert payload["ssh_username"] == "root"
        assert payload["ssh_access_configured"] is True
        assert "ssh_password" not in payload

        stored = await client.get("/control/workers")
        assert stored.status_code == 200
        listed = stored.json()[0]
        assert listed["ssh_access_configured"] is True
        assert "ssh_password" not in listed

    await engine.dispose()


@pytest.mark.asyncio
async def test_worker_update_job_can_be_started_from_control_panel(monkeypatch: pytest.MonkeyPatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async def fake_sync(session, settings):
        del session, settings
        return False

    started_jobs: list[int] = []

    async def fake_run_job(job_id: int) -> None:
        started_jobs.append(job_id)

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", fake_sync)
    monkeypatch.setattr("app.api.routes.control.run_worker_maintenance_job", fake_run_job)

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
        create_response = await client.post(
            "/control/workers",
            json={
                "name": "worker-update-job",
                "registrar_slug": "gandi",
                "ip_address": "2.27.20.255",
                "ssh_host": "2.27.20.255",
                "ssh_username": "root",
                "ssh_password": "temporary-root-password",
                "max_rps": 16,
                "target_rps": 16,
            },
        )
        worker_id = create_response.json()["id"]

        job_response = await client.post(f"/control/workers/{worker_id}/maintenance/update")

    assert job_response.status_code == 202
    payload = job_response.json()
    assert payload["worker_id"] == worker_id
    assert payload["action"] == "update"
    assert payload["status"] == "queued"
    assert started_jobs == [payload["id"]]
    await engine.dispose()
