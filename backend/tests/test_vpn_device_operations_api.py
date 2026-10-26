from datetime import timedelta
from typing import Annotated
from uuid import uuid4

import pytest
from fastapi import Depends, Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from test_vpn_device_api import device_api as device_api  # noqa: PLC0414
from test_vpn_device_api import ORIGIN
from test_vpn_device_locations import seed_device_locations

from app.api.routes import vpn_portal as portal
from app.db.base import utcnow
from app.db.models import VpnCustomer, VpnCustomerSession, VpnDevice, VpnDeviceOperation, VpnSubscription
from app.db.session import get_db


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "revoke", "replace"])
async def test_device_operations_require_authentication_and_csrf(device_api, action):
    env = device_api
    path = "/api/vpn-portal/v2/devices" + (f"/{env.ids.device}/{action}" if action != "create" else "")
    payload = {"request_id": str(uuid4())}
    if action == "create":
        payload |= dict(subscription_id=env.ids.subscription, endpoint_id=env.ids.endpoints[1], display_name="Laptop")
    for headers in ({}, {"Origin": ORIGIN}, {"Origin": "https://evil.example", "X-CSRF-Token": env.headers["X-CSRF-Token"]}):
        assert (await env.client.post(path, json=payload, headers=headers)).status_code == 403
    env.client.cookies.clear()
    assert (await env.client.post(path, json=payload, headers=env.headers)).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "revoke", "replace"])
@pytest.mark.parametrize("expired", [False, True])
async def test_device_operation_revalidates_captured_principal_before_writes(device_api, action, expired):
    env = device_api
    async def captured(request: Request, db: Annotated[AsyncSession, Depends(get_db)]):
        principal = await portal.current_customer(request, db)
        values = {"expires_at": utcnow() - timedelta(seconds=1)} if expired else {"revoked_at": utcnow()}
        await db.execute(update(VpnCustomerSession).where(
            VpnCustomerSession.token_hash == principal.session.token_hash,
        ).values(**values).execution_options(synchronize_session=False))
        await db.flush()
        return principal
    env.app.dependency_overrides[portal.current_customer] = captured
    payload = {"request_id": str(uuid4())}
    path = "/api/vpn-portal/v2/devices"
    if action == "create":
        payload |= dict(subscription_id=env.ids.subscription, endpoint_id=env.ids.endpoints[1], display_name="Laptop")
    else:
        path += f"/{env.ids.device}/{action}"
    assert (await env.client.post(path, json=payload, headers=env.headers)).status_code == 401
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VpnDeviceOperation)) == 0
        device = await db.get(VpnDevice, env.ids.device)
        assert device.token_generation == 0 and device.revoke_requested_at is None


@pytest.mark.asyncio
async def test_owned_create_operation_replay_payload_conflict_and_safe_get(device_api):
    env = device_api
    async with env.sessions() as db:
        subscription = await db.get(VpnSubscription, env.ids.subscription)
        subscription.max_devices = 2
        await db.commit()
    payload = dict(request_id=str(uuid4()), subscription_id=env.ids.subscription,
                   endpoint_id=env.ids.endpoints[1], display_name="Laptop")
    response = await env.client.post("/api/vpn-portal/v2/devices", json=payload, headers=env.headers)
    assert response.status_code == 202
    operation = response.json()
    assert operation["stage"] == "provisioning" and operation["action"] == "create"
    replay = await env.client.post("/api/vpn-portal/v2/devices", json=payload, headers=env.headers)
    assert replay.status_code == 202 and replay.json() == operation
    changed = await env.client.post("/api/vpn-portal/v2/devices", json=payload | {"display_name": "Other"}, headers=env.headers)
    assert changed.status_code == 409
    fetched = await env.client.get("/api/vpn-portal/v2/device-operations/" + operation["id"])
    assert fetched.status_code == 200 and fetched.json() == operation
    assert not any(secret in fetched.text for secret in ("vless://", "request_snapshot", "request_identity", "private-", "customer_id"))
    listed = (await env.client.get("/api/vpn-portal/v2/devices")).json()
    created = next(device for device in listed if device["id"] == operation["device_id"])
    assert created["operation"] == operation and not created["can_connect"]
    assert created["credential_generation"] == 0
    env.client.cookies.clear()
    assert (await env.client.get("/api/vpn-portal/v2/device-operations/" + operation["id"])).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["revoke", "replace"])
async def test_foreign_device_and_operation_are_generic_not_found(device_api, action):
    env = device_api
    async with env.sessions() as db:
        customer, _, foreign, key, _ = await seed_device_locations(db, now=utcnow(), identity="200")
        operation = VpnDeviceOperation(id=str(uuid4()), customer_id=customer.id, device_id=foreign.id,
                                       request_id=str(uuid4()), action="revoke", stage="revoking",
                                       credential_generation=0, request_identity={})
        db.add(operation)
        await db.commit()
        foreign_id, operation_id = foreign.id, operation.id
    response = await env.client.post(f"/api/vpn-portal/v2/devices/{foreign_id}/{action}",
                                     json={"request_id": str(uuid4())}, headers=env.headers)
    assert response.status_code == 404 and response.json() == {"detail": "customer_device_not_found"}
    response = await env.client.get("/api/vpn-portal/v2/device-operations/" + operation_id)
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_revoke_invalidates_old_feed_and_legacy_connection_immediately(device_api):
    env = device_api
    original = (await env.client.get(f"/api/vpn-portal/v2/devices/{env.ids.device}/connection")).json()
    payload = {"request_id": str(uuid4())}
    path = f"/api/vpn-portal/v2/devices/{env.ids.device}/revoke"
    response = await env.client.post(path, json=payload, headers=env.headers)
    assert response.status_code == 202
    replay = await env.client.post(path, json=payload, headers=env.headers)
    assert replay.json()["id"] == response.json()["id"]
    assert (await env.client.get(original["subscription_url"])).status_code == 404
    assert (await env.client.get(f"/api/vpn-portal/profiles/{env.ids.key}/connection")).status_code == 409
    changed = await env.client.post(path.replace("/revoke", "/replace"), json=payload, headers=env.headers)
    assert changed.status_code == 409
    async with env.sessions() as db:
        device = await db.get(VpnDevice, env.ids.device)
        assert device.token_generation == 1


@pytest.mark.asyncio
async def test_first_device_locations_are_owned_published_available_and_read_only(device_api):
    from app.services.vpn_portal_auth import SESSION_COOKIE, issue_session
    env = device_api
    async with env.sessions() as db:
        customer = VpnCustomer(status="active", telegram_user_id="200")
        db.add(customer)
        await db.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
        db.add(subscription)
        await db.flush()
        subscription_id = subscription.id
        raw = await issue_session(db, customer.id, "200")
        await db.commit()
    env.client.cookies.set(SESSION_COOKIE, raw)
    assert (await env.client.get("/api/vpn-portal/v2/devices")).json() == []
    response = await env.client.get(f"/api/vpn-portal/v2/locations?subscription_id={subscription_id}")
    assert response.status_code == 200
    assert [row["endpoint_id"] for row in response.json()] == env.ids.endpoints[1:]
    assert "private-" not in response.text and "public_key" not in response.text and "vless://" not in response.text
    assert (await env.client.get(f"/api/vpn-portal/v2/locations?subscription_id={env.ids.subscription}")).status_code == 404
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VpnDeviceOperation)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnDevice)) == 1


@pytest.mark.asyncio
async def test_session_expiring_while_staging_rolls_back_entire_creation(device_api, monkeypatch):
    env = device_api
    async with env.sessions() as db:
        subscription = await db.get(VpnSubscription, env.ids.subscription)
        subscription.max_devices = 2
        await db.commit()
    original = portal.request_device_creation
    async def captured(db, **kwargs):
        operation = await original(db, **kwargs)
        await db.execute(update(VpnCustomerSession).where(
            VpnCustomerSession.customer_id == env.ids.customer,
        ).values(expires_at=utcnow() - timedelta(seconds=1)).execution_options(synchronize_session=False))
        await db.flush()
        return operation
    monkeypatch.setattr(portal, "request_device_creation", captured)
    response = await env.client.post("/api/vpn-portal/v2/devices", json=dict(
        subscription_id=env.ids.subscription, endpoint_id=env.ids.endpoints[1],
        display_name="Laptop", request_id=str(uuid4())), headers=env.headers)
    assert response.status_code == 401
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(VpnDeviceOperation)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnDevice)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_id", [0, -1, 2**31, 10**60])
async def test_new_numeric_identifiers_are_bounded_before_database_binding(device_api, invalid_id):
    env = device_api
    response = await env.client.post("/api/vpn-portal/v2/devices", json=dict(
        subscription_id=invalid_id, endpoint_id=env.ids.endpoints[1], display_name="Laptop", request_id=str(uuid4())),
        headers=env.headers)
    assert response.status_code == 422
    response = await env.client.post(f"/api/vpn-portal/v2/devices/{invalid_id}/revoke",
                                     json={"request_id": str(uuid4())}, headers=env.headers)
    assert response.status_code == 422
    response = await env.client.get(f"/api/vpn-portal/v2/locations?subscription_id={invalid_id}")
    assert response.status_code == 422
