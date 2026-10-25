import base64
from datetime import timedelta
from types import SimpleNamespace
from typing import Annotated

import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from sqlalchemy import event, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool
from test_vpn_device_locations import seed_device_locations
from test_vpn_device_subscriptions import SECRET

from app.api.routes import vpn_portal as portal
from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import VpnAccessKey, VpnCustomerSession, VpnDevice
from app.db.session import get_db
from app.services import vpn_devices
from app.services.vpn_portal_auth import SESSION_COOKIE, csrf_token, issue_session
from app.services.vpn_portal_http import PortalHttpMiddleware

ORIGIN = "https://portal.example"


@pytest_asyncio.fixture
async def device_api(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(vpn_devices, "load_transport_snapshot", lambda *_: object())
    app = FastAPI()
    app.include_router(portal.router, prefix="/api")
    app.state.settings = Settings(_env_file=None, VPN_PORTAL_ENABLED=True, VPN_PORTAL_PUBLIC_ORIGIN=ORIGIN,
                                  VPN_PORTAL_ALLOWED_TELEGRAM_IDS="100,200", SESSION_SECRET_KEY=SECRET)
    app.add_middleware(PortalHttpMiddleware, portal_prefix="/api/vpn-portal")
    async def database():
        async with sessions() as db:
            yield db
    app.dependency_overrides[get_db] = database
    async with sessions() as db:
        customer, subscription, device, key, endpoints = await seed_device_locations(db, now=utcnow())
        child = VpnAccessKey(subscription_id=subscription.id, device_id=device.id, status="suspended")
        db.add(child)
        await db.flush()
        raw = await issue_session(db, customer.id, customer.telegram_user_id)
        ids = SimpleNamespace(customer=customer.id, device=device.id, key=key.id, child=child.id,
                              subscription=subscription.id, endpoints=[endpoint.id for endpoint in endpoints])
        await db.commit()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=ORIGIN) as client:
        client.cookies.set(SESSION_COOKIE, raw)
        yield SimpleNamespace(app=app, sessions=sessions, client=client, ids=ids,
                              headers={"Origin": ORIGIN, "X-CSRF-Token": csrf_token(raw)})
    await engine.dispose()


@pytest.mark.asyncio
async def test_v2_lists_one_logical_device_without_secrets_or_get_writes(device_api):
    env = device_api
    assert env.ids.device != env.ids.key
    mutations = []
    def capture(_conn, statement, *_args):
        if getattr(statement, "is_insert", False) or getattr(statement, "is_update", False) or getattr(statement, "is_delete", False):
            mutations.append(statement)
    event.listen(env.sessions.kw["bind"].sync_engine, "before_execute", capture)
    try:
        response = await env.client.get("/api/vpn-portal/v2/devices")
        locations = await env.client.get(f"/api/vpn-portal/v2/devices/{env.ids.device}/locations")
    finally:
        event.remove(env.sessions.kw["bind"].sync_engine, "before_execute", capture)
    assert response.status_code == locations.status_code == 200
    assert response.json() == [{"id": env.ids.device, "subscription_id": env.ids.subscription,
                                "display_name": "Phone", "state": "active", "can_connect": True,
                                "legacy_profile_id": env.ids.key}]
    assert not mutations
    text = response.text + locations.text
    assert "vless://" not in text and "subscription/" not in text and "private-" not in text
    assert locations.json()["current"]["endpoint_id"] == env.ids.endpoints[0]


@pytest.mark.asyncio
async def test_portal_usage_is_owned_read_only_and_keeps_unknown_measurements(device_api):
    env = device_api
    mutations = []
    def capture(_conn, statement, *_args):
        if any(getattr(statement, flag, False) for flag in ("is_insert", "is_update", "is_delete")):
            mutations.append(statement)
    event.listen(env.sessions.kw["bind"].sync_engine, "before_execute", capture)
    try:
        response = await env.client.get("/api/vpn-portal/usage?days=7&customer_id=999999")
    finally:
        event.remove(env.sessions.kw["bind"].sync_engine, "before_execute", capture)
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"upload_bytes", "download_bytes", "measured_since", "last_sample_at", "coverage", "days", "devices"}
    assert result["days"] == 7
    assert result["upload_bytes"] is None and result["download_bytes"] is None
    assert result["coverage"] == "none"
    assert [row["device_id"] for row in result["devices"]] == [env.ids.device]
    assert response.headers["cache-control"] == "no-store"
    assert not mutations
    assert "customer_id" not in response.text and "vless://" not in response.text
    for days in ("0", "2", "31", "1.0", "abc"):
        assert (await env.client.get(f"/api/vpn-portal/usage?days={days}")).status_code == 422
    env.client.cookies.clear()
    assert (await env.client.get("/api/vpn-portal/usage")).status_code == 401


@pytest.mark.asyncio
async def test_connection_returns_stable_trusted_url_and_public_feed_one_uri(device_api):
    env = device_api
    path = f"/api/vpn-portal/v2/devices/{env.ids.device}/connection"
    response = await env.client.get(path, headers={"Host": "evil.example"})
    assert response.status_code == 200
    result = response.json()
    assert set(result) == {"subscription_url", "direct_uri"}
    assert result["subscription_url"].startswith(ORIGIN + "/api/vpn-portal/subscription/")
    rename = await env.client.patch(f"/api/vpn-portal/v2/devices/{env.ids.device}",
                                    json={"display_name": "Laptop"}, headers=env.headers)
    assert rename.status_code == 200 and rename.json()["display_name"] == "Laptop"
    renamed = (await env.client.get(path)).json()
    assert renamed["subscription_url"] == result["subscription_url"]
    assert renamed["direct_uri"] != result["direct_uri"]
    env.client.cookies.clear()
    feed = await env.client.get(result["subscription_url"])
    assert feed.status_code == 200
    assert base64.b64decode(feed.text).decode() == renamed["direct_uri"] + "\n"
    assert feed.headers["cache-control"] == "no-store"
    assert feed.headers["referrer-policy"] == "no-referrer"
    assert feed.headers["x-content-type-options"] == "nosniff"
    invalid = await env.client.get("/api/vpn-portal/subscription/private-invalid-token")
    assert invalid.status_code == 404 and "private-invalid-token" not in invalid.text
    assert invalid.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_select_location_is_pending_and_old_key_id_routes_keep_working(device_api):
    env = device_api
    connection = (await env.client.get(f"/api/vpn-portal/profiles/{env.ids.key}/connection")).json()["uri"]
    response = await env.client.post(f"/api/vpn-portal/v2/devices/{env.ids.device}/location",
                                    json={"endpoint_id": env.ids.endpoints[1]}, headers=env.headers)
    assert response.status_code == 202
    assert response.json()["selection_state"] == "preparing"
    assert response.json()["pending_endpoint_id"] == env.ids.endpoints[1]
    assert response.json()["current"]["endpoint_id"] == env.ids.endpoints[0]
    assert (await env.client.get(f"/api/vpn-portal/profiles/{env.ids.key}/connection")).json()["uri"] == connection
    assert (await env.client.get(f"/api/vpn-portal/v2/devices/{env.ids.key}/connection")).status_code == 404
    renamed = await env.client.patch(f"/api/vpn-portal/profiles/{env.ids.child}",
                                     json={"display_name": "All children"}, headers=env.headers)
    assert renamed.status_code == 200
    assert (await env.client.get("/api/vpn-portal/v2/devices")).json()[0]["display_name"] == "All children"


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{}, {"Origin": ORIGIN}, {"Origin": "https://evil.example"},
                                     {"Origin": ORIGIN + "/"}])
async def test_v2_mutation_requires_csrf_and_exact_origin(device_api, headers):
    env = device_api
    response = await env.client.post(f"/api/vpn-portal/v2/devices/{env.ids.device}/location",
                                    json={"endpoint_id": env.ids.endpoints[1]}, headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "revoked"])
@pytest.mark.parametrize("method", ["rename", "select"])
async def test_captured_session_is_revalidated_before_device_mutation(device_api, change, method):
    env = device_api
    async def captured(request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
        principal = await portal.current_customer(request, db)
        value = {"expires_at": utcnow() - timedelta(seconds=1)} if change == "expired" else {"revoked_at": utcnow()}
        await db.execute(update(VpnCustomerSession).where(
            VpnCustomerSession.token_hash == principal.session.token_hash,
        ).values(**value).execution_options(synchronize_session=False))
        await db.flush()
        return principal
    env.app.dependency_overrides[portal.current_customer] = captured
    if method == "rename":
        response = await env.client.patch(f"/api/vpn-portal/v2/devices/{env.ids.device}",
                                          json={"display_name": "Forbidden"}, headers=env.headers)
    else:
        response = await env.client.post(f"/api/vpn-portal/v2/devices/{env.ids.device}/location",
                                         json={"endpoint_id": env.ids.endpoints[1]}, headers=env.headers)
    assert response.status_code == 401
    async with env.sessions() as db:
        device = await db.get(VpnDevice, env.ids.device)
        assert device.display_name == "Phone" and device.pending_key_id is None


@pytest.mark.asyncio
async def test_hidden_current_is_readable_but_never_a_selectable_option(device_api):
    from app.db.models import VpnEndpoint
    env = device_api
    async with env.sessions() as db:
        endpoint = await db.get(VpnEndpoint, env.ids.endpoints[0])
        endpoint.is_published, endpoint.public_name, endpoint.country_code = False, None, None
        await db.commit()
    response = await env.client.get(f"/api/vpn-portal/v2/devices/{env.ids.device}/locations")
    assert response.status_code == 200
    assert response.json()["current"] == {"endpoint_id": env.ids.endpoints[0], "name": "Ранее выданное подключение",
                                           "country_code": None, "is_published": False}
    assert env.ids.endpoints[0] not in [row["endpoint_id"] for row in response.json()["available"]]
    assert (await env.client.get(f"/api/vpn-portal/v2/devices/{env.ids.device}/connection")).status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["", "/locations", "/location", "/connection"])
async def test_foreign_logical_device_is_generic_404(device_api, suffix):
    env = device_api
    async with env.sessions() as db:
        _, _, foreign, _, _ = await seed_device_locations(db, now=utcnow(), identity="200")
        foreign_id = foreign.id
    path = f"/api/vpn-portal/v2/devices/{foreign_id}{suffix}"
    if suffix == "":
        response = await env.client.patch(path, json={"display_name": "Foreign rename"}, headers=env.headers)
    elif suffix == "/location":
        response = await env.client.post(path, json={"endpoint_id": env.ids.endpoints[1]}, headers=env.headers)
    else:
        response = await env.client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": "customer_device_not_found"}


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["failed", "uncertain"])
async def test_failed_and_uncertain_selection_keep_old_connection_and_safe_metadata(device_api, state):
    from app.db.models import VpnControlOperation
    env = device_api
    path = f"/api/vpn-portal/v2/devices/{env.ids.device}"
    before = (await env.client.get(path + "/connection")).json()
    assert (await env.client.post(path + "/location", json={"endpoint_id": env.ids.endpoints[1]},
                                  headers=env.headers)).status_code == 202
    async with env.sessions() as db:
        operation = await db.scalar(select(VpnControlOperation))
        operation.state = state
        operation.error_code = "vpn_node_preflight_failed" if state == "failed" else "vpn_node_mutation_uncertain"
        await db.commit()
    view = (await env.client.get(path + "/locations")).json()
    assert view["selection_state"] == ("failed" if state == "failed" else "verification_required")
    assert (await env.client.get(path + "/connection")).json() == before
    if state == "uncertain":
        response = await env.client.post(path + "/location", json={"endpoint_id": env.ids.endpoints[1]}, headers=env.headers)
        assert response.status_code == 409
        assert response.json() == {"detail": "customer_location_verification_required"}


@pytest.mark.asyncio
async def test_connection_and_feed_fail_closed_for_weak_secret_and_permanent_revoke(device_api):
    from app.services.vpn_devices import lock_key_device_context, stage_device_revoke
    env = device_api
    path = f"/api/vpn-portal/v2/devices/{env.ids.device}/connection"
    original = (await env.client.get(path)).json()
    env.app.state.settings.session_secret_key = "weak"
    assert (await env.client.get(path)).status_code == 409
    assert (await env.client.get(original["subscription_url"])).status_code == 404
    env.app.state.settings.session_secret_key = SECRET
    async with env.sessions() as db:
        key = await lock_key_device_context(db, env.ids.key)
        await stage_device_revoke(db, key)
        await db.commit()
    assert (await env.client.get(path)).status_code == 409
    revoked_feed = await env.client.get(original["subscription_url"])
    assert revoked_feed.status_code == 404
    assert original["subscription_url"] not in revoked_feed.text and original["direct_uri"] not in revoked_feed.text
    assert (await env.client.get("/api/vpn-portal/v2/devices")).json()[0]["state"] == "pending_revoke"
