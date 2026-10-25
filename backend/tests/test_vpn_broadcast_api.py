from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select
from test_vpn_broadcasts import seed
from test_vpn_friend_invitations import (
    session_factory as session_factory,  # noqa: PLC0414
)

from app.api.deps import get_current_user, require_admin
from app.core.config import Settings
from app.db.models import (
    AdminAuditLog,
    AppSetting,
    User,
    VpnBotChat,
    VpnBroadcastDelivery,
)
from app.db.session import get_db
from app.main import create_app
from app.services.vpn_bot_chats import REGISTRY_BOT_KEY

BASE = "/api/control/vpn/broadcasts"
ORIGIN = {"Origin": "http://testserver"}


@pytest_asyncio.fixture
async def env(session_factory, monkeypatch):
    from app.api.routes import vpn_broadcasts

    settings = Settings(
        _env_file=None,
        VPN_TELEGRAM_BOT_TOKEN="111:synthetic-only",
        VPN_BROADCASTS_ENABLED=True,
    )
    monkeypatch.setattr(vpn_broadcasts, "get_settings", lambda: settings)
    app = create_app()

    async def database():
        async with session_factory() as db:
            yield db

    async def admin():
        return SimpleNamespace(id=1, role="admin")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_admin] = admin
    async with session_factory() as db:
        db.add(
            User(
                id=1,
                username="synthetic-admin",
                password_hash="unused",
                role="admin",
                status="active",
            )
        )
        await db.commit()
        await seed(db)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(
            app=app, factory=session_factory, client=client, settings=settings
        )


async def draft(env, text="<b>Новости</b>\r\nНовая страна"):
    result = await env.client.post(BASE, headers=ORIGIN, json={"body": text})
    assert result.status_code == 201, result.text
    return result.json()


@pytest.mark.asyncio
async def test_news_requires_admin_for_reads_and_mutations(env):
    env.app.dependency_overrides.pop(require_admin)
    for method, path, body in [
        ("GET", "/config", None),
        ("GET", "", None),
        ("POST", "", {"body": "Новость"}),
        ("GET", "/1", None),
    ]:
        response = await env.client.request(
            method, BASE + path, headers=ORIGIN, json=body
        )
        assert response.status_code == 401
        assert response.headers["cache-control"] == "no-store"

    async def ordinary_user():
        return SimpleNamespace(id=2, role="user")

    env.app.dependency_overrides[get_current_user] = ordinary_user
    for response in (
        await env.client.get(BASE + "/config"),
        await env.client.post(BASE, headers=ORIGIN, json={"body": "Новость"}),
    ):
        assert response.status_code == 403
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", [None, "https://foreign.example"])
async def test_every_mutation_requires_same_origin_before_writes(env, origin):
    row = await draft(env)
    headers = {} if origin is None else {"Origin": origin}
    requests = [
        ("POST", "", {"body": "Другая"}),
        ("PATCH", f"/{row['id']}", {"body": "Другая"}),
        ("POST", f"/{row['id']}/preview", {}),
        (
            "POST",
            f"/{row['id']}/confirm",
            {"expected_revision": 1, "expected_recipient_count": 1, "confirm": True},
        ),
        ("POST", f"/{row['id']}/cancel", {}),
    ]
    for method, path, payload in requests:
        assert (
            await env.client.request(method, BASE + path, headers=headers, json=payload)
        ).status_code == 403
    current = (await env.client.get(BASE + f"/{row['id']}")).json()
    assert current["state"] == "draft" and current["revision"] == 1
    assert (
        current["preview_revision"] is None
        and current["counts"]["recipient_count"] == 0
    )


@pytest.mark.asyncio
async def test_plain_text_draft_preview_confirm_and_cancel_without_network(
    env, monkeypatch
):
    async def forbidden_http(*_args, **_kwargs):
        pytest.fail("API workflow must never send Telegram HTTP")

    monkeypatch.setattr(
        "app.services.vpn_broadcasts.post_telegram_payload", forbidden_http
    )
    config = await env.client.get(BASE + "/config")
    assert (
        config.json()["registry_state"] == "ready"
        and config.json()["eligible_recipients"] == 1
    )
    row = await draft(env)
    url = BASE + f"/{row['id']}"
    assert row["body"] == "<b>Новости</b>\nНовая страна"
    assert row["state"] == "draft" and row["counts"]["recipient_count"] == 0
    preview = (await env.client.post(url + "/preview", headers=ORIGIN, json={})).json()
    assert preview["body"] == row["body"] and preview["preview_recipient_count"] == 1
    changed = await env.client.patch(
        url, headers=ORIGIN, json={"body": "Изменённая новость"}
    )
    assert (
        changed.json()["revision"] == 2 and changed.json()["preview_revision"] is None
    )
    stale = await env.client.post(
        url + "/confirm",
        headers=ORIGIN,
        json={"expected_revision": 1, "expected_recipient_count": 1, "confirm": True},
    )
    assert (
        stale.status_code == 409
        and stale.json()["detail"] == "vpn_broadcast_preview_changed"
    )
    preview = (await env.client.post(url + "/preview", headers=ORIGIN, json={})).json()
    payload = {
        "expected_revision": preview["revision"],
        "expected_recipient_count": 1,
        "confirm": True,
    }
    for _ in range(2):
        confirmed = await env.client.post(
            url + "/confirm", headers=ORIGIN, json=payload
        )
        assert (
            confirmed.status_code == 202 and confirmed.json()["counts"]["pending"] == 1
        )
    cancelled = await env.client.post(url + "/cancel", headers=ORIGIN, json={})
    assert (
        cancelled.json()["state"] == "cancelled"
        and cancelled.json()["counts"]["cancelled"] == 1
    )
    assert (await env.client.post(url + "/cancel", headers=ORIGIN, json={})).json()[
        "counts"
    ] == cancelled.json()["counts"]
    for response in (config, changed, stale, confirmed, cancelled):
        assert response.headers["cache-control"] == "no-store"
    async with env.factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 1
        )
        assert "Изменённая новость" not in str(
            (await db.scalars(select(AdminAuditLog.details))).all()
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"body": "private-news", "chat_id": 12345},
        {"body": "private-news", "parse_mode": "HTML"},
        {"body": " "},
        {"body": "private-news" * 500},
        {"body": 7},
    ],
)
async def test_invalid_body_is_fixed_error_without_echo(env, payload):
    response = await env.client.post(BASE, headers=ORIGIN, json=payload)
    assert response.status_code == 422
    assert response.json() == {"detail": "vpn_broadcast_invalid_request"}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation", [False, 1, "true", None])
async def test_explicit_confirmation_is_strict_true(env, confirmation):
    row = await draft(env)
    response = await env.client.post(
        BASE + f"/{row['id']}/confirm",
        headers=ORIGIN,
        json={
            "expected_revision": 1,
            "expected_recipient_count": 1,
            "confirm": confirmation,
        },
    )
    assert response.status_code == 422 and response.json() == {
        "detail": "vpn_broadcast_invalid_request"
    }


@pytest.mark.asyncio
async def test_registry_and_send_switch_are_visible_gates(env):
    row = await draft(env)
    env.settings.vpn_broadcasts_enabled = False
    assert (await env.client.get(BASE + "/config")).json()["enabled"] is False
    assert (
        await env.client.post(
            BASE + f"/{row['id']}/confirm",
            headers=ORIGIN,
            json={
                "expected_revision": 1,
                "expected_recipient_count": 1,
                "confirm": True,
            },
        )
    ).json()["detail"] == "vpn_broadcasts_disabled"
    env.settings.vpn_broadcasts_enabled = True
    async with env.factory() as db:
        await db.delete(
            await db.scalar(
                select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
            )
        )
        await db.commit()
    config = (await env.client.get(BASE + "/config")).json()
    assert config["registry_state"] == "history_unverified"
    assert config["eligible_recipients"] is None and config["explanation"]
    response = await env.client.post(
        BASE + f"/{row['id']}/preview", headers=ORIGIN, json={}
    )
    assert (
        response.status_code == 409
        and response.json()["detail"] == "vpn_broadcast_registry_unavailable"
    )


@pytest.mark.asyncio
async def test_summary_cursor_omits_body_and_detail_counts_are_authoritative(env):
    first, second = (
        await draft(env, "private-first-news"),
        await draft(env, "private-second-news"),
    )
    listed = await env.client.get(BASE, params={"limit": 1})
    assert len(listed.json()) == 1 and listed.json()[0]["id"] == second["id"]
    assert "body" not in listed.json()[0] and "private-second-news" not in listed.text
    older = await env.client.get(BASE, params={"before_id": second["id"]})
    assert [item["id"] for item in older.json()] == [first["id"]]
    for query in ({"limit": 51}, {"before_id": 0}):
        assert (await env.client.get(BASE, params=query)).status_code == 422
    assert (await env.client.get(BASE + "/999999")).status_code == 404


@pytest.mark.asyncio
async def test_changed_audience_requires_preview_and_creates_no_jobs(env):
    row = await draft(env)
    url = BASE + f"/{row['id']}"
    await env.client.post(url + "/preview", headers=ORIGIN, json={})
    async with env.factory() as db, db.begin():
        chat = await db.get(VpnBotChat, ("111", "12345"))
        chat.blocked_at = chat.last_seen_at
    response = await env.client.post(
        url + "/confirm",
        headers=ORIGIN,
        json={
            "expected_revision": 1,
            "expected_recipient_count": 1,
            "confirm": True,
        },
    )
    assert (
        response.status_code == 409
        and response.json()["detail"] == "vpn_broadcast_audience_changed"
    )
    assert (await env.client.get(url)).json()["counts"]["recipient_count"] == 0
    preview = (await env.client.post(url + "/preview", headers=ORIGIN, json={})).json()
    assert preview["preview_recipient_count"] == 0


@pytest.mark.asyncio
async def test_validation_never_echoes_invalid_unicode_or_extra_confirmation_fields(
    env,
):
    response = await env.client.post(
        BASE,
        headers={**ORIGIN, "Content-Type": "application/json"},
        content=b'{"body":"\\ud800"}',
    )
    assert response.status_code == 422 and response.json() == {
        "detail": "vpn_broadcast_invalid_request"
    }
    row = await draft(env)
    url = BASE + f"/{row['id']}"
    for path, payload in [
        ("/preview", {"body": "private-news"}),
        ("/cancel", {"recipient_ids": [12345]}),
        (
            "/confirm",
            {"expected_revision": True, "expected_recipient_count": 1, "confirm": True},
        ),
        (
            "/confirm",
            {
                "expected_revision": 1,
                "expected_recipient_count": 1,
                "confirm": True,
                "token": "private-secret",
            },
        ),
    ]:
        response = await env.client.post(url + path, headers=ORIGIN, json=payload)
        assert response.status_code == 422 and response.json() == {
            "detail": "vpn_broadcast_invalid_request"
        }
