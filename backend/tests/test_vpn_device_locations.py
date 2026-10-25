from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from test_vpn_public_trial import NOW, HookedSession, add_key, worker

from app.api.deps import get_current_user, require_admin
from app.api.routes.control import router
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    AdminAuditLog,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnDevice,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.db.session import get_db
from app.services import vpn_devices as devices
from app.services.vpn_devices import attach_initial_device
from app.services.vpn_policy import select_public_vpn_endpoint


async def seed_device_locations(db, *, now=NOW, identity="100"):
    customer = VpnCustomer(status="active", telegram_user_id=identity)
    db.add(customer)
    await db.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1,
                                   expires_at=now + timedelta(days=30))
    db.add(subscription)
    endpoints = []
    for code in ("NL", "PL", "DE"):
        node = worker(f"private-{identity}-{code}", vpn_last_checked_at=now)
        db.add(node)
        await db.flush()
        target = VpnEndpoint(worker_id=node.id, inbound_id=1, public_host=f"{code.lower()}.example", port=443,
                             security="reality", transport="raw", status="ready", verified_at=now,
                             max_active_profiles=1, is_published=True, public_name=code, country_code=code,
                             server_name="cdn.example", public_key="A" * 43, short_id="aabbccdd",
                             fingerprint="chrome", flow="xtls-rprx-vision")
        db.add(target)
        endpoints.append(target)
    await db.flush()
    key = VpnAccessKey(id=11 if identity == "100" else None,
                       subscription_id=subscription.id, worker_id=endpoints[0].worker_id,
                       endpoint_id=endpoints[0].id, status="active", display_name="Phone",
                       external_uuid=str(uuid4()), issued_at=now, expires_at=subscription.expires_at,
                       config_uri="vless://11111111-1111-4111-8111-111111111111@nl.example:443?security=reality#stored")
    db.add(key)
    await db.flush()
    device = await attach_initial_device(db, key)
    await db.commit()
    return customer, subscription, device, key, endpoints


@pytest_asyncio.fixture
async def location_env(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(devices, "load_transport_snapshot", lambda *_: object(), raising=False)
    config = Settings(_env_file=None, VPN_CONTROL_KNOWN_HOSTS_PATH="safe-fake")
    try:
        async with sessions() as db:
            seeded = await seed_device_locations(db)
            yield SimpleNamespace(db=db, sessions=sessions, settings=config, customer=seeded[0],
                                  subscription=seeded[1], device=seeded[2], key=seeded[3], endpoints=seeded[4])
    finally:
        await engine.dispose()


async def choose(env, endpoint_id):
    selector = getattr(devices, "select_device_location", None)
    assert callable(selector), "location selection service is missing"
    return await selector(env.db, customer_id=env.customer.id, device_id=env.device.id,
                          endpoint_id=endpoint_id, settings=env.settings, now=NOW)


@pytest.mark.asyncio
async def test_select_country_keeps_current_then_promotes_and_reuses_old_uuid(location_env):
    env = location_env
    old_identity = env.key.external_uuid, env.key.config_uri
    await choose(env, env.endpoints[1].id)
    assert env.device.preferred_key_id == env.key.id
    pending = await env.db.get(VpnAccessKey, env.device.pending_key_id)
    assert pending.device_id == env.device.id and pending.status == "pending_sync"
    assert (env.key.external_uuid, env.key.config_uri) == old_identity
    assert await env.db.scalar(select(func.count()).select_from(VpnDevice)) == 1
    await choose(env, env.endpoints[1].id)
    assert await env.db.scalar(select(func.count()).select_from(VpnControlOperation)) == 1
    pending.status, pending.config_uri = "active", old_identity[1].replace("nl.example", "pl.example")
    await devices.promote_prepared_device_key(env.db, pending, now=NOW)
    assert env.device.preferred_key_id == pending.id and env.device.pending_key_id is None
    await env.db.flush()
    await choose(env, env.endpoints[0].id)
    assert env.device.preferred_key_id == env.key.id and env.device.pending_key_id is None
    assert env.key.external_uuid == old_identity[0]
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey)) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "uncertain"])
async def test_failed_or_uncertain_country_never_replaces_good_current(location_env, state):
    env = location_env
    await choose(env, env.endpoints[1].id)
    operation = await env.db.scalar(select(VpnControlOperation))
    operation.state = state
    operation.error_code = "vpn_node_preflight_failed" if state == "failed" else "vpn_node_mutation_uncertain"
    await env.db.flush()
    if state == "uncertain":
        with pytest.raises(ValueError, match="customer_location_verification_required"):
            await choose(env, env.endpoints[1].id)
    else:
        await choose(env, env.endpoints[1].id)
    assert env.device.preferred_key_id == env.key.id


@pytest.mark.asyncio
async def test_late_result_cannot_override_new_pending_choice(location_env):
    env = location_env
    await choose(env, env.endpoints[1].id)
    earlier = await env.db.get(VpnAccessKey, env.device.pending_key_id)
    await choose(env, env.endpoints[2].id)
    later_id = env.device.pending_key_id
    earlier.status, earlier.config_uri = "active", env.key.config_uri
    await devices.promote_prepared_device_key(env.db, earlier, now=NOW)
    assert env.device.preferred_key_id == env.key.id and env.device.pending_key_id == later_id


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["hidden", "unhealthy", "full", "unsupported", "customer", "subscription", "expired", "revoke", "transport", "child_revoke"])
async def test_rejected_selection_does_not_change_credential_or_pending(location_env, monkeypatch, change):
    env = location_env
    target = env.endpoints[1]
    if change == "hidden":
        target.is_published = False
    elif change == "unhealthy":
        target.verified_at = NOW - timedelta(days=1)
    elif change == "full":
        env.db.add(VpnAccessKey(subscription_id=env.subscription.id, endpoint_id=target.id,
                                worker_id=target.worker_id, status="pending_sync"))
    elif change == "unsupported":
        target.protocol = "vmess"
    elif change == "customer":
        env.customer.status = "blocked"
    elif change == "subscription":
        env.subscription.status = "disabled"
    elif change == "expired":
        env.subscription.expires_at = NOW
    elif change == "revoke":
        env.device.revoke_requested_at = NOW
    elif change == "child_revoke":
        env.db.add(VpnAccessKey(subscription_id=env.subscription.id, device_id=env.device.id,
                                endpoint_id=target.id, worker_id=target.worker_id, status="revoked"))
    else:
        from app.services.vpn_node_transport import VpnNodeTransportError
        def reject(*_):
            raise VpnNodeTransportError("vpn_node_transport_unavailable")
        monkeypatch.setattr(devices, "load_transport_snapshot", reject)
    await env.db.commit()
    count = await env.db.scalar(select(func.count()).select_from(VpnAccessKey))
    with pytest.raises(ValueError, match="customer_location_unavailable"):
        await choose(env, target.id)
    assert env.device.pending_key_id is None
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey)) == count


@pytest.mark.asyncio
async def test_selection_failure_rolls_back_new_child_pending_and_operation(location_env, monkeypatch):
    env = location_env
    selector = getattr(devices, "select_device_location", None)
    assert callable(selector), "location selection service is missing"
    from app.services.vpn_control_intents import VpnControlIntentError
    async def reject(*args, **kwargs):
        raise VpnControlIntentError("vpn_control_worker_busy")
    monkeypatch.setattr("app.services.vpn_control_intents.stage_vpn_control_operation", reject)
    with pytest.raises(ValueError, match="customer_location_unavailable"):
        await choose(env, env.endpoints[1].id)
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey)) == 1
    assert await env.db.scalar(select(func.count()).select_from(VpnControlOperation)) == 0
    await env.db.refresh(env.device)
    assert env.device.pending_key_id is None

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
        target = VpnEndpoint(worker_id=node.id, inbound_id=1, public_host="publication.example", port=443,
                             security="reality", status="ready", verified_at=NOW, max_active_profiles=4,
                             server_name="cdn.example", public_key="A" * 43, short_id="aabbccdd",
                             fingerprint="chrome", flow="xtls-rprx-vision")
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
            stale_endpoint = await db.get(VpnEndpoint, target_id)
            assert stale_endpoint is not None
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


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_fallback", [False, True])
async def test_automatic_hidden_only_issuance_does_not_allocate_or_fall_back(publication, monkeypatch, legacy_fallback):
    app, sessions, target_id, key_id = publication
    monkeypatch.setattr("app.api.routes.control.utcnow", lambda: NOW)
    async with sessions() as db:
        subscription_id = (await db.get(VpnAccessKey, key_id)).subscription_id
        if legacy_fallback:
            (await db.get(VpnEndpoint, target_id)).status = "draining"
            db.add(worker("legacy"))
            await db.commit()
    legacy_calls = []

    async def legacy_provision(_db, key, **_kwargs):
        legacy_calls.append(key.id)
        return key
    monkeypatch.setattr("app.api.routes.control.provision_vpn_access_key", legacy_provision)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/control/vpn/access-keys", json={"subscription_id": subscription_id})
    assert response.status_code == 409
    assert response.json()["detail"] == "No ready VPN endpoint is currently available"
    assert legacy_calls == []
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 1
        assert await db.scalar(select(func.count()).select_from(VpnDevice)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnControlOperation)) == 0


@pytest.mark.asyncio
async def test_explicit_admin_worker_can_allocate_hidden_endpoint(publication, monkeypatch):
    app, sessions, target_id, key_id = publication
    monkeypatch.setattr("app.api.routes.control.utcnow", lambda: NOW)
    async with sessions() as db:
        subscription_id = (await db.get(VpnAccessKey, key_id)).subscription_id
        worker_id = (await db.get(VpnEndpoint, target_id)).worker_id
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/control/vpn/access-keys", json={
            "subscription_id": subscription_id, "worker_id": worker_id})
    assert response.status_code == 201
    async with sessions() as db:
        key = await db.get(VpnAccessKey, response.json()["id"])
        assert key.endpoint_id == target_id and key.status == "pending_sync"
        assert (await db.get(VpnEndpoint, target_id)).is_published is False
        assert await db.scalar(select(func.count()).select_from(VpnControlOperation)) == 1


@pytest.mark.asyncio
async def test_existing_hidden_connection_remains_readable_and_resync_keeps_binding(publication):
    from app.services.vpn_customer_view import customer_connection

    app, sessions, target_id, key_id = publication
    identity = "00000000-0000-4000-8000-000000000001"
    uri = f"vless://{identity}@publication.example:443?type=tcp&security=tls#original"
    async with sessions() as db:
        key = await db.get(VpnAccessKey, key_id)
        key.external_uuid, key.config_uri, key.display_name = identity, uri, "Laptop"
        await db.commit()
        subscription = await db.get(VpnSubscription, key.subscription_id)
        customer = await db.get(VpnCustomer, subscription.customer_id)
        assert (await customer_connection(db, customer, key_id, now=NOW)).uri.startswith(f"vless://{identity}@")
        key.status = "failed"
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(f"/control/vpn/access-keys/{key_id}/provision")
    assert response.status_code == 200
    async with sessions() as db:
        key = await db.get(VpnAccessKey, key_id)
        assert (key.external_uuid, key.config_uri, key.endpoint_id) == (identity, uri, target_id)
        assert key.revoke_requested_at is None and key.revoked_at is None
        assert (await db.get(VpnEndpoint, target_id)).is_published is False


@pytest.mark.asyncio
async def test_committed_provision_can_finish_after_hiding(publication, monkeypatch):
    from app.services.vpn_control_intents import (
        claim_next_vpn_control_operation,
        finalize_vpn_control_operation,
    )

    app, sessions, target_id, key_id = publication
    monkeypatch.setattr("app.api.routes.control.utcnow", lambda: NOW)
    async with sessions() as db:
        subscription_id = (await db.get(VpnAccessKey, key_id)).subscription_id
        (await db.get(VpnEndpoint, target_id)).is_published = True
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        created = await client.post("/control/vpn/access-keys", json={"subscription_id": subscription_id})
        assert created.status_code == 201
        hidden = await client.patch(f"/control/vpn/endpoints/{target_id}/publication",
                                    json=PAYLOAD | {"is_published": False}, headers={"Origin": "http://testserver"})
        assert hidden.status_code == 200
    token = uuid4()
    async with sessions() as db:
        operation = await claim_next_vpn_control_operation(db, claim_token=token, now=NOW)
        assert operation is not None
        operation_id = UUID(operation.id)
        await db.commit()
    async with sessions() as db:
        operation = await finalize_vpn_control_operation(
            db, operation_id, token, receipt_state="observed", error_code=None, now=NOW)
        assert operation.state == "succeeded"
        key = await db.get(VpnAccessKey, created.json()["id"])
        assert key.status == "active" and key.config_uri
        assert (await db.get(VpnEndpoint, target_id)).is_published is False
