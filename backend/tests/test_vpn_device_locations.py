from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from test_vpn_public_trial import NOW, HookedSession, add_key, endpoint, worker

from app.api.deps import get_current_user, require_admin
from app.api.routes.control import router
from app.db.base import Base
from app.db.models import (
    AdminAuditLog,
    VpnAccessKey,
    VpnControlOperation,
    VpnEndpoint,
    WorkerNode,
)
from app.db.session import get_db
from app.services.vpn_policy import select_public_vpn_endpoint

PAYLOAD = {"is_published": True, "public_name": "  Нидерланды  ", "country_code": " nl "}


@pytest_asyncio.fixture
async def publication():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, class_=HookedSession, expire_on_commit=False)
    async with sessions() as db:
        node = worker("publication")
        db.add(node)
        await db.flush()
        target = endpoint(node)
        db.add(target)
        await db.flush()
        key = await add_key(db, target)
        await db.commit()
        target_id, key_id = target.id, key.id

    app = FastAPI()
    app.include_router(router)

    async def database():
        async with sessions() as db:
            yield db

    async def admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_admin] = admin
    try:
        yield app, sessions, target_id, key_id
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_new_endpoint_is_hidden(publication):
    _, sessions, target_id, _ = publication
    async with sessions() as db:
        target = await db.get(VpnEndpoint, target_id)
        assert target.is_published is False
        assert target.public_name is None and target.country_code is None


@pytest.mark.asyncio
@pytest.mark.parametrize("role,expected", [(None, 401), ("user", 403)])
async def test_publication_requires_admin(publication, role, expected):
    app, _, target_id, _ = publication
    app.dependency_overrides.pop(require_admin)
    if role:
        async def user():
            return SimpleNamespace(id=1, role=role)
        app.dependency_overrides[get_current_user] = user
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(f"/control/vpn/endpoints/{target_id}/publication", json=PAYLOAD,
                                      headers={"Origin": "http://testserver"})
    assert response.status_code == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://testserver.evil.example"])
async def test_publication_requires_exact_origin(publication, origin):
    app, _, target_id, _ = publication
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(f"/control/vpn/endpoints/{target_id}/publication", json=PAYLOAD,
                                      headers={"Origin": origin} if origin else {})
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"public_name": ""}, {"public_name": "x" * 65}, {"public_name": "bad\x00name"},
    {"public_name": "bad\u200bname"}, {"country_code": "NLD"}, {"country_code": "НЛ"},
    {"country_code": "N1"}, {"country_code": None}, {"worker_id": 2},
    {"public_key": "secret"}, {"status": "ready"},
])
async def test_publication_rejects_invalid_or_foreign_fields(publication, changes):
    app, _, target_id, _ = publication
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(f"/control/vpn/endpoints/{target_id}/publication", json=PAYLOAD | changes,
                                      headers={"Origin": "http://testserver"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_publish_and_hide_only_change_publication_and_audit(publication):
    app, sessions, target_id, key_id = publication
    async with sessions() as db:
        target = await db.get(VpnEndpoint, target_id)
        target.last_error_code = "vpn_node_health_unavailable"
        await db.commit()
        key = await db.get(VpnAccessKey, key_id)
        key_before = {column.name: getattr(key, column.name) for column in key.__table__.columns}
        endpoint_before = {column.name: getattr(target, column.name) for column in target.__table__.columns
                           if column.name not in {"is_published", "public_name", "country_code", "updated_at"}}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        for published in (True, False):
            response = await client.patch(f"/control/vpn/endpoints/{target_id}/publication",
                                          json=PAYLOAD | {"is_published": published},
                                          headers={"Origin": "http://testserver"})
            assert response.status_code == 200
            result = response.json()
            assert (result["is_published"], result["public_name"], result["country_code"]) == (
                published, "Нидерланды", "NL")
            assert result["occupied_profiles"] == 1
            assert "public_key" not in result and "config_uri" not in result
            async with sessions() as db:
                assert await select_public_vpn_endpoint(db, now=NOW, health_max_age_seconds=120) is None
        missing = await client.patch("/control/vpn/endpoints/999999/publication", json=PAYLOAD,
                                     headers={"Origin": "http://testserver"})
        assert missing.status_code == 404

    async with sessions() as db:
        key = await db.get(VpnAccessKey, key_id)
        target = await db.get(VpnEndpoint, target_id)
        assert {column.name: getattr(key, column.name) for column in key.__table__.columns} == key_before
        assert {name: getattr(target, name) for name in endpoint_before} == endpoint_before
        assert await db.scalar(select(func.count()).select_from(VpnControlOperation)) == 0
        audits = list(await db.scalars(select(AdminAuditLog).order_by(AdminAuditLog.id)))
        assert [(audit.action, audit.details) for audit in audits] == [
            ("vpn_endpoint_publication_update", f"endpoint_id={target_id} published=1"),
            ("vpn_endpoint_publication_update", f"endpoint_id={target_id} published=0"),
        ]


@pytest.mark.asyncio
async def test_publication_locks_worker_then_refreshes_endpoint_binding(publication):
    app, sessions, target_id, _ = publication
    async with sessions() as db:
        other = worker("other")
        db.add(other)
        await db.commit()
        other_id = other.id
    locks = []

    async def database():
        async with sessions() as db:
            original = db.scalar

            async def scalar(statement, **kwargs):
                if statement._for_update_arg is not None:
                    locks.append(statement.column_descriptions[0]["entity"])
                    if len(locks) == 1:
                        await db.execute(update(VpnEndpoint).where(VpnEndpoint.id == target_id).values(
                            worker_id=other_id).execution_options(synchronize_session=False))
                return await original(statement, **kwargs)
            db.scalar = scalar
            yield db
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(f"/control/vpn/endpoints/{target_id}/publication", json=PAYLOAD,
                                      headers={"Origin": "http://testserver"})
    assert response.status_code == 409
    assert locks == [WorkerNode, VpnEndpoint]
    async with sessions() as db:
        assert (await db.get(VpnEndpoint, target_id)).is_published is False
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog)) == 0
