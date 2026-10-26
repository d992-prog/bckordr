import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_vpn_endpoint_migrations import (
    postgres_schema as postgres_schema,  # noqa: PLC0414
)
from test_vpn_usage_api import (
    env as env,  # noqa: PLC0414 -- pytest fixture registration
)
from test_vpn_usage_api import hour, poll, report_app, sample, seed

from app.api.deps import get_current_user, require_admin
from app.api.routes.vpn_portal import current_customer
from app.core.config import Settings
from app.db import models as m
from app.db.base import Base, utcnow
from app.db.models import AdminAuditLog, AppSetting

PATH = "/api/control/vpn/cabinet-settings"
DEFAULTS = {
    "revision": 0,
    "support_contact": None,
    "notice": None,
    "reminders_enabled": False,
}
ORIGIN = {"Origin": "http://testserver"}


@pytest.mark.asyncio
async def test_settings_route_is_admin_only_and_default_get_does_not_write(env):
    response = await env.client.get(PATH)
    assert response.status_code == 200
    assert response.json() == DEFAULTS
    assert response.headers["cache-control"] == "no-store"
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 0
    env.app.dependency_overrides.pop(require_admin)
    assert (await env.client.get(PATH)).status_code == 401
    assert (
        await env.client.put(PATH, json=DEFAULTS, headers=ORIGIN)
    ).status_code == 401

    async def customer():
        return SimpleNamespace(role="user")

    env.app.dependency_overrides[get_current_user] = customer
    assert (await env.client.get(PATH)).status_code == 403
    assert (
        await env.client.put(PATH, json=DEFAULTS, headers=ORIGIN)
    ).status_code == 403


@pytest.mark.asyncio
async def test_settings_update_is_revision_checked_audited_and_same_origin(env):
    for headers in ({}, {"Origin": "https://attacker.example"}):
        assert (
            await env.client.put(PATH, json=DEFAULTS, headers=headers)
        ).status_code == 403
    payload = {
        **DEFAULTS,
        "support_contact": "@veltrix_support",
        "reminders_enabled": True,
    }
    response = await env.client.put(PATH, json=payload, headers=ORIGIN)
    assert response.status_code == 200
    expected = {
        **payload,
        "revision": 1,
        "support_contact": "https://t.me/veltrix_support",
    }
    assert response.json() == expected
    assert response.headers["cache-control"] == "no-store"
    assert (await env.client.get(PATH)).json() == expected
    stale = await env.client.put(PATH, json=payload, headers=ORIGIN)
    assert stale.status_code == 409
    assert (await env.client.get(PATH)).json() == expected
    async with env.sessions() as db:
        logs = (await db.scalars(select(AdminAuditLog))).all()
        assert len(logs) == 1
        assert logs[0].action == "vpn_cabinet_settings_updated"
        assert logs[0].actor_user_id == 1
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "patch",
    [
        {"revision": -1},
        {"revision": True},
        {"support_contact": "https://evil.example"},
        {"reminders_enabled": "yes"},
        {"reminders_enabled_at": "2026-10-26T00:00:00Z"},
        {
            "notice": {
                "title": "Unsafe\x00",
                "body": "text",
                "starts_at": "2026-10-26T01:00:00Z",
                "ends_at": "2026-10-26T02:00:00Z",
            }
        },
    ],
)
async def test_invalid_settings_are_rejected_without_writes(env, patch):
    assert (
        await env.client.put(PATH, json={**DEFAULTS, **patch}, headers=ORIGIN)
    ).status_code == 422
    async with env.sessions() as db:
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 0
        assert await db.scalar(select(func.count()).select_from(AdminAuditLog)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "window, active", [("active", True), ("future", False), ("expired", False)]
)
async def test_public_config_contains_only_active_notice_and_public_settings(
    env, monkeypatch, window, active
):
    from app.api.routes import vpn_portal

    now = utcnow()
    env.app.state.settings = Settings(_env_file=None)
    monkeypatch.setattr(vpn_portal, "utcnow", lambda: now, raising=False)
    start, end = {
        "active": (now, now + timedelta(hours=1)),
        "future": (now + timedelta(hours=1), now + timedelta(hours=2)),
        "expired": (now - timedelta(hours=1), now),
    }[window]
    notice = {
        "title": "Работы",
        "body": "Короткое сообщение",
        "starts_at": start.isoformat(),
        "ends_at": end.isoformat(),
    }
    response = await env.client.put(
        PATH,
        headers=ORIGIN,
        json={
            **DEFAULTS,
            "support_contact": "@veltrix_support",
            "notice": notice,
            "reminders_enabled": True,
        },
    )
    assert response.status_code == 200
    result = await env.client.get("/api/vpn-portal/config")
    assert result.status_code == 200
    assert result.json()["support_url"] == "https://t.me/veltrix_support"
    assert result.json()["reminders_enabled"] is True
    assert (result.json()["notice"] is not None) is active
    assert "revision" not in result.json() and "reminders_enabled_at" not in result.text
    if active:
        assert result.json()["notice"]["body"] == notice["body"]


@pytest.mark.asyncio
async def test_public_config_get_never_writes_default_settings(env):
    env.app.state.settings = Settings(_env_file=None)
    mutations = []

    def capture(_conn, statement, *_args):
        if any(
            getattr(statement, flag, False)
            for flag in ("is_insert", "is_update", "is_delete")
        ):
            mutations.append(statement)

    event.listen(env.engine.sync_engine, "before_execute", capture)
    try:
        response = await env.client.get("/api/vpn-portal/config")
    finally:
        event.remove(env.engine.sync_engine, "before_execute", capture)
    assert response.status_code == 200
    assert response.json()["support_url"] is None
    assert response.json()["notice"] is None
    assert response.json()["reminders_enabled"] is False
    assert not mutations


@pytest.mark.asyncio
async def test_concurrent_initial_puts_on_postgres_have_one_conflict(postgres_schema):
    async with postgres_schema.engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(postgres_schema.engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(
            m.User(
                id=1, username="synthetic-admin", password_hash="unused", role="admin"
            )
        )
        await db.commit()
    app = report_app(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first, second = await asyncio.gather(
            client.put(PATH, json=DEFAULTS, headers=ORIGIN),
            client.put(PATH, json=DEFAULTS, headers=ORIGIN),
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        assert (await client.get(PATH)).json()["revision"] == 1


@pytest.mark.asyncio
async def test_portal_usage_reuses_safe_totals_and_omits_foreign_devices(env):
    ids = await seed(env)
    async with env.sessions() as db:
        foreign_subscription = m.VpnSubscription(customer_id=ids.foreign)
        db.add(foreign_subscription)
        await db.flush()
        foreign_device = m.VpnDevice(
            subscription_id=foreign_subscription.id,
            display_name="Foreign secret device",
        )
        db.add(foreign_device)
        await db.flush()
        foreign_key = m.VpnAccessKey(
            subscription_id=foreign_subscription.id,
            device_id=foreign_device.id,
            worker_id=ids.workers[0],
            status="active",
        )
        db.add(foreign_key)
        await db.flush()
        db.add_all(
            [
                hour(ids.keys[0], ids.workers[0], ids.now, up=10, down=20),
                sample(ids.keys[0], ids.workers[0], ids.now),
                poll(ids.workers[0], ids.now),
                hour(foreign_key.id, ids.workers[0], ids.now, up=99999, down=99999),
            ]
        )
        await db.commit()

    async def principal():
        return SimpleNamespace(customer=SimpleNamespace(id=ids.customer))

    env.app.dependency_overrides[current_customer] = principal
    response = await env.client.get(
        f"/api/vpn-portal/usage?days=30&customer_id={ids.foreign}"
    )
    assert response.status_code == 200
    result = response.json()
    assert result["upload_bytes"] == 10 and result["download_bytes"] == 20
    assert result["coverage"] == "partial"
    assert result["measured_since"] is not None and result["last_sample_at"] is not None
    assert [row["device_id"] for row in result["devices"]] == [ids.device]
    assert result["devices"][0]["upload_bytes"] == 10
    assert (
        "Foreign secret device" not in response.text
        and "customer_id" not in response.text
    )
    assert "private-" not in response.text and "vless://" not in response.text
