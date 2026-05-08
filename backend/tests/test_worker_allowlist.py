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
