from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_vpn_usage_api import env as env  # noqa: PLC0414
from test_vpn_usage_api import seed

from app.api.deps import get_current_user, require_admin
from app.db import models as m

URL = "/api/control/vpn/abuse/incidents"
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
