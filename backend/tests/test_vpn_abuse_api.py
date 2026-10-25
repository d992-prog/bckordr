from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import insert, select
from test_vpn_usage_api import env as env  # noqa: PLC0414
from test_vpn_usage_api import seed

from app.api.deps import get_current_user, require_admin
from app.db import models as m

URL = "/api/control/vpn/abuse/incidents"
CONTEXT = "/api/control/vpn/abuse/devices/{}/context"
FIELDS = {
    "id",
    "device_id",
    "customer_id",
    "display_name",
    "started_at",
    "last_observed_at",
    "peak_source_count",
    "acknowledged_at",
}


async def incident_seed(env):
    ids = await seed(env)
    async with env.sessions() as db, db.begin():
        db.add(
            m.User(
                id=1,
                username="synthetic-admin",
                password_hash="not-a-password",
                role="admin",
            )
        )
        db.add(
            m.VpnDeviceAbuseState(
                device_id=ids.device,
                last_minute_at=ids.now,
                consecutive_windows=5,
                coverage="ok",
                key_epoch="a" * 16,
            )
        )
        rows = [
            m.VpnAbuseIncident(
                device_id=ids.device,
                started_at=ids.now - timedelta(minutes=4),
                last_observed_at=ids.now,
                peak_source_count=3,
                acknowledged_at=ids.now if index == 0 else None,
            )
            for index in range(2)
        ]
        db.add_all(rows)
        await db.flush()
        return ids, [row.id for row in rows]


@pytest.mark.asyncio
async def test_incident_reports_require_admin_and_have_exact_private_safe_fields(env):
    ids, rows = await incident_seed(env)
    response = await env.client.get(URL)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert len(result) == 1 and set(result[0]) == FIELDS
    assert result[0]["id"] == rows[1] and result[0]["customer_id"] == ids.customer
    assert (
        result[0]["device_id"] == ids.device and result[0]["display_name"] == "Laptop"
    )
    assert "private" not in response.text and "source_hashes" not in response.text
    env.app.dependency_overrides.pop(require_admin)
    assert (await env.client.get(URL)).status_code == 401
    assert (
        await env.client.post(f"{URL}/{rows[1]}/acknowledge", json={})
    ).status_code == 401

    async def ordinary():
        return SimpleNamespace(id=2, role="user")

    env.app.dependency_overrides[get_current_user] = ordinary
    assert (await env.client.get(URL)).status_code == 403
    assert (
        await env.client.post(f"{URL}/{rows[1]}/acknowledge", json={})
    ).status_code == 403


@pytest.mark.asyncio
async def test_incident_pagination_filter_and_payload_are_strict(env):
    _, rows = await incident_seed(env)
    result = await env.client.get(
        URL, params={"state": "acknowledged", "after_id": 0, "limit": 1}
    )
    assert result.status_code == 200 and [row["id"] for row in result.json()] == [
        rows[0]
    ]
    assert (await env.client.get(URL, params={"after_id": rows[1]})).json() == []
    for query in ({"state": "all"}, {"limit": 101}, {"limit": 0}, {"after_id": -1}):
        assert (await env.client.get(URL, params=query)).status_code == 422
    path = f"{URL}/{rows[1]}/acknowledge"
    for origin in (None, "http://foreign.example", "http://testserver.evil"):
        headers = {} if origin is None else {"Origin": origin}
        assert (
            await env.client.post(path, json={}, headers=headers)
        ).status_code == 403
    assert (
        await env.client.post(
            path, json={"revoke": True}, headers={"Origin": "http://testserver"}
        )
    ).status_code == 422
    missing = await env.client.post(
        f"{URL}/9999/acknowledge", json={}, headers={"Origin": "http://testserver"}
    )
    assert (
        missing.status_code == 404
        and missing.json()["detail"] == "vpn_abuse_incident_not_found"
    )


@pytest.mark.asyncio
async def test_acknowledge_persists_actor_and_audits_ids_without_access_changes(env):
    ids, rows = await incident_seed(env)
    async with env.sessions() as db:
        before = (await db.get(m.VpnAccessKey, ids.keys[0])).status
    response = await env.client.post(
        f"{URL}/{rows[1]}/acknowledge", json={}, headers={"Origin": "http://testserver"}
    )
    assert response.status_code == 200 and set(response.json()) == FIELDS
    assert response.json()["acknowledged_at"] is not None
    async with env.sessions() as db:
        row = await db.get(m.VpnAbuseIncident, rows[1])
        assert row.acknowledged_by_user_id == 1
        assert (
            await db.get(m.VpnDeviceAbuseState, ids.device)
        ).consecutive_windows == 0
        assert (await db.get(m.VpnAccessKey, ids.keys[0])).status == before
        assert (await db.get(m.VpnDevice, ids.device)).revoke_requested_at is None
        assert await db.scalar(select(m.VpnControlOperation)) is None
        assert (
            await db.scalar(select(m.AdminAuditLog))
        ).details == f"incident_id={rows[1]};device_id={ids.device}"
    assert (await env.client.get(URL)).json() == []


@pytest.mark.asyncio
async def test_application_error_boundaries_are_bounded_generic_and_no_store(env):
    import httpx

    from app.main import create_app

    _, rows = await incident_seed(env)
    app = create_app()
    app.dependency_overrides.update(env.app.dependency_overrides)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for path in (
            f"{URL}?after_id={2**80}",
            f"{URL}?after_id=-1",
            f"{URL}?state=private-secret",
            f"{URL}/0/acknowledge",
            f"{URL}/{2**80}/acknowledge",
        ):
            response = await (
                client.post(path, json={}, headers={"Origin": "http://testserver"})
                if path.endswith("acknowledge")
                else client.get(path)
            )
            assert response.status_code == 422
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"detail": "vpn_abuse_invalid_request"}
        path = f"{URL}/{rows[1]}/acknowledge"
        response = await client.post(
            path,
            json={"source_hashes": ["private-secret"]},
            headers={"Origin": "http://testserver"},
        )
        assert response.status_code == 422 and response.json() == {
            "detail": "vpn_abuse_invalid_request"
        }
        response = await client.post(
            path, json={}, headers={"Origin": "http://foreign.example"}
        )
        assert (
            response.status_code == 403
            and response.headers["cache-control"] == "no-store"
        )
        app.dependency_overrides.pop(require_admin)
        for response in (await client.get(URL), await client.post(path, json={})):
            assert (
                response.status_code == 401
                and response.headers["cache-control"] == "no-store"
            )


@pytest.mark.asyncio
async def test_device_context_is_bounded_and_independent_of_global_list_caps(env):
    from app.schemas.control import (
        VpnAccessKeyResponse,
        VpnCustomerResponse,
        VpnSubscriptionResponse,
    )

    ids = await seed(env)
    async with env.sessions() as db, db.begin():
        await db.execute(insert(m.VpnCustomer), [{"first_name": "Filler"}] * 501)
        await db.execute(
            insert(m.VpnSubscription), [{"customer_id": ids.foreign}] * 501
        )
        foreign_subscription = await db.scalar(
            select(m.VpnSubscription.id)
            .where(m.VpnSubscription.customer_id == ids.foreign)
            .limit(1)
        )
        await db.execute(
            insert(m.VpnAccessKey), [{"subscription_id": foreign_subscription}] * 1001
        )
    for path, missing_id in (
        ("/api/control/vpn/customers", ids.customer),
        ("/api/control/vpn/subscriptions", ids.subscription),
        ("/api/control/vpn/access-keys", ids.keys[0]),
    ):
        rows = (await env.client.get(path)).json()
        assert missing_id not in {row["id"] for row in rows}
    response = await env.client.get(
        CONTEXT.format(ids.device), params={"customer_id": ids.customer}
    )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"device_id", "customer", "subscription", "access_key"}
    assert result["device_id"] == ids.device
    assert result["customer"]["id"] == ids.customer
    assert result["subscription"]["id"] == ids.subscription
    assert result["access_key"]["id"] == ids.keys[0]
    assert result["access_key"]["device_id"] == ids.device
    assert result["access_key"]["config_uri"] is None, (
        "reuse safe access-key URI serializer"
    )
    for name, schema in (
        ("customer", VpnCustomerResponse),
        ("subscription", VpnSubscriptionResponse),
        ("access_key", VpnAccessKeyResponse),
    ):
        assert set(result[name]) == set(schema.model_fields)


@pytest.mark.asyncio
async def test_device_context_prefers_nonrevoked_preferred_then_deterministic_child(
    env,
):
    ids = await seed(env, count=3)
    path = CONTEXT.format(ids.device)
    query = {"customer_id": ids.customer}
    async with env.sessions() as db, db.begin():
        (await db.get(m.VpnDevice, ids.device)).preferred_key_id = ids.keys[2]
    assert (await env.client.get(path, params=query)).json()["access_key"][
        "id"
    ] == ids.keys[2]
    async with env.sessions() as db, db.begin():
        (await db.get(m.VpnAccessKey, ids.keys[2])).status = "revoked"
    assert (await env.client.get(path, params=query)).json()["access_key"][
        "id"
    ] == ids.keys[0]
    async with env.sessions() as db, db.begin():
        (await db.get(m.VpnAccessKey, ids.keys[0])).revoked_at = ids.now
    assert (await env.client.get(path, params=query)).json()["access_key"][
        "id"
    ] == ids.keys[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_device", [False, True])
async def test_device_context_never_uses_child_of_another_device_or_subscription(
    env, wrong_device
):
    ids = await seed(env)
    async with env.sessions() as db, db.begin():
        foreign_subscription = m.VpnSubscription(customer_id=ids.foreign)
        db.add(foreign_subscription)
        await db.flush()
        sibling = m.VpnDevice(subscription_id=ids.subscription, display_name="Other")
        db.add(sibling)
        await db.flush()
        foreign_child = m.VpnAccessKey(
            subscription_id=ids.subscription
            if wrong_device
            else foreign_subscription.id,
            device_id=sibling.id if wrong_device else ids.device,
            status="active",
        )
        db.add(foreign_child)
        await db.flush()
        (await db.get(m.VpnDevice, ids.device)).preferred_key_id = foreign_child.id
    response = await env.client.get(
        CONTEXT.format(ids.device), params={"customer_id": ids.customer}
    )
    assert response.status_code == 200
    assert response.json()["access_key"]["id"] == ids.keys[0]


@pytest.mark.asyncio
async def test_device_context_handles_no_children_and_keeps_revocation_sticky(env):
    ids = await seed(env, count=2)
    async with env.sessions() as db, db.begin():
        empty = m.VpnDevice(subscription_id=ids.subscription, display_name="Empty")
        db.add(empty)
        device = await db.get(m.VpnDevice, ids.device)
        device.revoke_requested_at = ids.now
        device.preferred_key_id = ids.keys[1]
        device.token_generation = 4
        for key_id in ids.keys:
            key = await db.get(m.VpnAccessKey, key_id)
            key.status = "revoked"
            key.revoked_at = key.revoke_requested_at = ids.now
        await db.flush()
        empty_id = empty.id
    response = await env.client.get(
        CONTEXT.format(empty_id), params={"customer_id": ids.customer}
    )
    assert response.status_code == 200 and response.json()["access_key"] is None
    for _ in range(2):
        response = await env.client.get(
            CONTEXT.format(ids.device), params={"customer_id": ids.customer}
        )
        assert response.status_code == 200
        assert response.json()["access_key"]["id"] == ids.keys[0]
        assert response.json()["access_key"]["status"] == "revoked"
    async with env.sessions() as db:
        device = await db.get(m.VpnDevice, ids.device)
        assert device.revoke_requested_at is not None and device.token_generation == 4
        for key_id in ids.keys:
            key = await db.get(m.VpnAccessKey, key_id)
            assert (
                key.status == "revoked"
                and key.revoked_at is not None
                and key.revoke_requested_at is not None
            )
        assert await db.scalar(select(m.VpnControlOperation)) is None
        assert await db.scalar(select(m.AdminAuditLog)) is None


@pytest.mark.asyncio
async def test_device_context_auth_owner_bounds_and_errors_are_private_no_store(env):
    import httpx

    from app.main import create_app

    ids = await seed(env)
    app = create_app()
    app.dependency_overrides.update(env.app.dependency_overrides)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        for device_id, customer_id in (
            (ids.device, ids.foreign),
            (ids.device, 2**31 - 1),
            (2**31 - 1, ids.customer),
        ):
            response = await client.get(
                CONTEXT.format(device_id), params={"customer_id": customer_id}
            )
            assert response.status_code == 404
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"detail": "vpn_abuse_device_not_found"}
        for device_id, customer_id in (
            (0, ids.customer),
            (-1, ids.customer),
            (2**31, ids.customer),
            ("private-secret", ids.customer),
            (ids.device, 0),
            (ids.device, -1),
            (ids.device, 2**31),
            (ids.device, "private-secret"),
            (ids.device, None),
        ):
            query = {} if customer_id is None else {"customer_id": customer_id}
            response = await client.get(CONTEXT.format(device_id), params=query)
            assert response.status_code == 422
            assert response.headers["cache-control"] == "no-store"
            assert response.json() == {"detail": "vpn_abuse_invalid_request"}
        app.dependency_overrides.pop(require_admin)
        response = await client.get(
            CONTEXT.format(ids.device), params={"customer_id": ids.customer}
        )
        assert (
            response.status_code == 401
            and response.headers["cache-control"] == "no-store"
        )

        async def ordinary():
            return SimpleNamespace(id=2, role="user")

        app.dependency_overrides[get_current_user] = ordinary
        response = await client.get(
            CONTEXT.format(ids.device), params={"customer_id": ids.customer}
        )
        assert (
            response.status_code == 403
            and response.headers["cache-control"] == "no-store"
        )
