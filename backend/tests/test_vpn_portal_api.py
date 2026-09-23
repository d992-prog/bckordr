from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from starlette.requests import Request
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import (
    AppSetting,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnCustomerSession,
    VpnEndpoint,
    VpnFriendInvitation,
    VpnPlan,
    VpnSubscription,
    User,
    UserSession,
    WorkerNode,
)
from app.db.session import get_db
from app.api.routes.vpn_portal import telegram_callback
from app.services.vpn_portal_auth import (
    BINDING_COOKIE,
    SESSION_COOKIE,
    create_login_attempt,
    csrf_token,
    digest_token,
    issue_session,
)
from app.services.vpn_portal_http import PortalHttpMiddleware
from app.services.vpn_portal_telegram import (
    TELEGRAM_ISSUER,
    TELEGRAM_JWKS_URL,
    TELEGRAM_TOKEN_URL,
    TelegramJWKSProvider,
)
from app.services.security import hash_session_token
from app.services.vpn_telegram_identity import TelegramIdentity


PORTAL_ORIGIN = "https://portal.example"


def mini_app_data(user_id: int, bot_token: str, *, auth_date: int | None = None) -> str:
    fields = {
        "auth_date": str(auth_date or int(time.time())),
        "query_id": f"query-{user_id}-{time.time_ns()}",
        "user": json.dumps(
            {"id": user_id, "first_name": f"Mini {user_id}"},
            separators=(",", ":"),
        ),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


async def seed_invited_api_identity(
    session: AsyncSession,
    user_id: str,
    *,
    slot: int = 1,
) -> VpnFriendInvitation:
    now = utcnow()
    customer = VpnCustomer(
        telegram_user_id=user_id,
        first_name=f"Friend {user_id}",
        status="active",
    )
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(
        customer_id=customer.id,
        status="trial",
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=7),
        max_devices=1,
    )
    session.add(subscription)
    await session.flush()
    key = VpnAccessKey(subscription_id=subscription.id, status="pending")
    session.add(key)
    await session.flush()
    invitation = VpnFriendInvitation(
        slot=slot,
        token_digest=f"{slot:064x}",
        created_at=now,
        redeem_expires_at=now + timedelta(days=7),
        redeemed_at=now,
        telegram_user_id=user_id,
        access_key_id=key.id,
    )
    session.add(invitation)
    await session.commit()
    return invitation


class ChunkedBody(httpx.AsyncByteStream):
    def __init__(self, *chunks: bytes) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_disconnected_mini_app_body_stops_without_invoking_route() -> None:
    inner_called = False
    sent: list[dict] = []
    settings = Settings(
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN=PORTAL_ORIGIN,
        VPN_PORTAL_PUBLIC_ACCESS=True,
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
    )

    async def inner(scope, receive, send) -> None:
        del scope, receive, send
        nonlocal inner_called
        inner_called = True

    messages = iter(
        [
            {"type": "http.request", "body": b'{"init_data":"partial', "more_body": True},
            {"type": "http.disconnect"},
        ]
    )

    async def receive() -> dict:
        message = next(messages, {"type": "http.disconnect"})
        if message["type"] == "http.disconnect":
            await asyncio.sleep(0)
        return message

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(inner, portal_prefix="/api/vpn-portal")
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/vpn-portal/auth/mini-app",
        "headers": [
            (b"origin", PORTAL_ORIGIN.encode()),
            (b"content-type", b"application/json"),
        ],
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
    }
    await asyncio.wait_for(middleware(scope, receive, send), timeout=0.2)

    assert inner_called is False
    assert next(message for message in sent if message["type"] == "http.response.start")[
        "status"
    ] == 400


@pytest.mark.asyncio
async def test_portal_unexpected_error_is_generic_no_store_and_scoped(caplog) -> None:
    async def failing(scope, receive, send) -> None:
        del scope, receive, send
        raise RuntimeError("database-password=must-not-leak")

    settings = Settings()
    portal_scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/vpn-portal/me",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(settings=settings)),
    }
    sent: list[dict] = []

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(failing, portal_prefix="/api/vpn-portal")
    with caplog.at_level("ERROR", logger="app.services.vpn_portal_http"):
        await middleware(portal_scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    assert start["status"] == 500
    assert (b"cache-control", b"no-store") in start["headers"]
    assert json.loads(body) == {"detail": "customer_service_unavailable"}
    assert b"database-password" not in body
    assert [record.getMessage() for record in caplog.records] == [
        "Unhandled VPN portal request failure"
    ]
    assert all(record.exc_info is None for record in caplog.records)
    assert "database-password" not in caplog.text

    sent.clear()
    callback_scope = {
        **portal_scope,
        "path": "/api/vpn-portal/auth/telegram/callback",
    }
    await middleware(callback_scope, receive, send)
    callback_start = next(
        message for message in sent if message["type"] == "http.response.start"
    )
    callback_headers = dict(callback_start["headers"])
    assert b"veltrix_login_binding=" in callback_headers[b"set-cookie"]
    assert b"Max-Age=0" in callback_headers[b"set-cookie"]

    outside_scope = {**portal_scope, "path": "/api/auth/me"}
    with pytest.raises(RuntimeError, match="must-not-leak"):
        await middleware(outside_scope, receive, send)


@pytest.mark.asyncio
async def test_portal_error_after_response_start_is_not_double_sent() -> None:
    sent: list[dict] = []

    async def started_then_failed(scope, receive, send) -> None:
        del scope, receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        raise RuntimeError("late failure")

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    middleware = PortalHttpMiddleware(
        started_then_failed, portal_prefix="/api/vpn-portal"
    )
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/vpn-portal/me",
        "headers": [],
        "app": SimpleNamespace(state=SimpleNamespace(settings=Settings())),
    }
    await middleware(scope, receive, send)

    starts = [message for message in sent if message["type"] == "http.response.start"]
    assert len(starts) == 1
    assert (b"cache-control", b"no-store") in starts[0]["headers"]
    assert sent[-1] == {
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    }


@pytest_asyncio.fixture
async def portal_app(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    settings = Settings(
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN=PORTAL_ORIGIN,
        VPN_PORTAL_PUBLIC_ACCESS=False,
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="100,200",
        VPN_PORTAL_OIDC_CLIENT_ID="portal-client",
        VPN_PORTAL_OIDC_CLIENT_SECRET="portal-secret",
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        CORS_ORIGINS="*",
    )

    async with factory() as session:
        alice = VpnCustomer(
            telegram_user_id="100",
            telegram_username="alice_private",
            first_name="Alice",
            last_name="Customer",
            status="active",
            notes="never expose this",
        )
        bob = VpnCustomer(telegram_user_id="200", first_name="Bob", status="active")
        session.add_all([alice, bob])
        await session.flush()
        subscription = VpnSubscription(
            customer_id=alice.id,
            status="active",
            starts_at=utcnow() - timedelta(days=1),
            expires_at=utcnow() + timedelta(days=30),
            max_devices=2,
        )
        foreign_subscription = VpnSubscription(
            customer_id=bob.id,
            status="active",
            expires_at=utcnow() + timedelta(days=30),
            max_devices=1,
        )
        session.add_all([subscription, foreign_subscription])
        await session.flush()
        own_profile = VpnAccessKey(
            subscription_id=subscription.id,
            display_name="Phone",
            status="active",
            config_uri=(
                "vless://11111111-1111-4111-8111-111111111111@vpn.example:443"
                "?type=tcp&security=tls#internal"
            ),
        )
        foreign_profile = VpnAccessKey(
            subscription_id=foreign_subscription.id,
            display_name="Foreign",
            status="active",
        )
        session.add_all([own_profile, foreign_profile])
        await session.flush()
        alice_raw = await issue_session(session, alice.id, alice.telegram_user_id)
        bob_raw = await issue_session(session, bob.id, bob.telegram_user_id)
        await session.commit()
        ids = SimpleNamespace(
            alice=alice.id,
            bob=bob.id,
            own_profile=own_profile.id,
            foreign_profile=foreign_profile.id,
            subscription=subscription.id,
            foreign_subscription=foreign_subscription.id,
            alice_raw=alice_raw,
            bob_raw=bob_raw,
        )

    async def override_get_db():
        async with factory() as session:
            yield session

    from app import main as main_module

    monkeypatch.setattr(main_module, "settings", settings)
    app = main_module.create_app()
    app.dependency_overrides[get_db] = override_get_db

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=PORTAL_ORIGIN,
        follow_redirects=False,
    ) as client:
        yield SimpleNamespace(
            app=app,
            client=client,
            factory=factory,
            ids=ids,
            settings=settings,
        )

    await engine.dispose()


async def seed_ready_public_trial(portal_app, monkeypatch) -> None:
    from app.services import vpn_public_trial

    settings = portal_app.settings
    settings.vpn_public_trial_enabled = True
    settings.vpn_public_trial_release_id = "a" * 64
    settings.vpn_control_dispatch_enabled = True
    monkeypatch.setattr(vpn_public_trial, "load_transport_snapshot", lambda *_: object())
    async with portal_app.factory() as session:
        worker = WorkerNode(
            name="portal-trial",
            status="ready",
            is_enabled=True,
            vpn_enabled=True,
            vpn_role="vpn_node",
            vpn_runtime_status="ready",
            vpn_public_host="vpn.example",
            vpn_inbound_id=1,
            ssh_host="10.0.0.1",
            ssh_password="private transport marker",
            vpn_last_checked_at=utcnow(),
        )
        session.add_all([
            worker,
            VpnPlan(slug="trial-7d", name="Portal trial", duration_days=7, max_devices=1),
            AppSetting(key="vpn_public_release_ready_v1", value="a" * 64),
        ])
        await session.flush()
        session.add(VpnEndpoint(
            worker_id=worker.id,
            inbound_id=1,
            public_host="vpn.example",
            port=443,
            security="reality",
            transport="raw",
            server_name="cdn.example.test",
            public_key="A" * 43,
            short_id="0123456789abcdef",
            fingerprint="chrome",
            flow="xtls-rprx-vision",
            status="ready",
            verified_at=utcnow(),
            max_active_profiles=4,
        ))
        await session.commit()


@pytest.mark.asyncio
async def test_public_trial_fresh_signed_mini_app_can_read_activate_and_replay(portal_app, monkeypatch):
    await seed_ready_public_trial(portal_app, monkeypatch)
    portal_app.settings.vpn_portal_allowed_telegram_ids = ""
    config = await portal_app.client.get("/api/vpn-portal/config")
    assert config.json()["enabled"] is True
    assert config.json()["browser_login_enabled"] is True
    assert config.json()["mini_app_enabled"] is True
    assert "access-control-allow-origin" not in config.headers

    fresh = httpx.AsyncClient(transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN)
    try:
        path = "/api/vpn-portal/trial"
        denied = await fresh.get(path)
        assert denied.status_code == 401
        assert denied.headers["cache-control"] == "no-store"
        login = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": mini_app_data(400, portal_app.settings.vpn_telegram_bot_token)},
        )
        assert login.status_code == 200
        assert login.headers["cache-control"] == "no-store"
        assert (await fresh.get("/api/vpn-portal/me")).status_code == 200
        available = await fresh.get(path)
        assert available.status_code == 200
        assert available.json() == {
            "state": "available", "duration_days": 7, "profile_limit": 1,
            "subscription_id": None, "access_key_id": None, "expires_at": None,
        }
        assert available.headers["cache-control"] == "no-store"

        csrf = login.json()["csrf_token"]
        for headers in ({}, {"Origin": PORTAL_ORIGIN}, {"X-CSRF-Token": csrf},
                        {"Origin": "https://foreign.example", "X-CSRF-Token": csrf}):
            rejected = await fresh.post(path + "/activate", headers=headers)
            assert (rejected.status_code, rejected.json()) == (403, {"detail": "customer_request_rejected"})
            assert rejected.headers["cache-control"] == "no-store"
        headers = {"Origin": PORTAL_ORIGIN, "X-CSRF-Token": csrf}
        activated = await fresh.post(path + "/activate", headers=headers)
        assert activated.status_code == 200, activated.json()
        payload = activated.json()
        assert payload == {
            "state": "preparing", "duration_days": 7, "profile_limit": 1,
            "subscription_id": payload["subscription_id"],
            "access_key_id": payload["access_key_id"],
            "expires_at": payload["expires_at"],
        }
        assert isinstance(payload["subscription_id"], int)
        assert isinstance(payload["access_key_id"], int)
        assert payload["expires_at"] is not None
        assert activated.headers["cache-control"] == "no-store"
        assert "private transport marker" not in activated.text
        assert "config_uri" not in activated.text
        replay = await fresh.post(path + "/activate", headers=headers)
        assert replay.status_code == 200 and replay.json() == payload
        assert (await fresh.get(path)).json() == payload
        async with portal_app.factory() as session:
            assert await session.scalar(select(func.count(VpnControlOperation.id))) == 1
            customer = await session.scalar(select(VpnCustomer).where(VpnCustomer.telegram_user_id == "400"))
            assert customer is not None and customer.trial_started_at is not None
        logout = await fresh.post("/api/vpn-portal/logout", headers=headers)
        assert logout.status_code == 200
        assert (await fresh.get(path)).status_code == 401
    finally:
        await fresh.aclose()


@pytest.mark.asyncio
async def test_public_trial_unavailable_and_used_errors_are_static(portal_app):
    portal_app.settings.vpn_public_trial_enabled = True
    portal_app.settings.vpn_portal_allowed_telegram_ids = ""
    fresh = httpx.AsyncClient(transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN)
    try:
        login = await fresh.post(
            "/api/vpn-portal/auth/mini-app", headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": mini_app_data(401, portal_app.settings.vpn_telegram_bot_token)},
        )
        assert login.status_code == 200
        headers = {"Origin": PORTAL_ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]}
        paused = await fresh.get("/api/vpn-portal/trial")
        assert paused.json()["state"] == "capacity_paused"
        unavailable = await fresh.post("/api/vpn-portal/trial/activate", headers=headers)
        assert (unavailable.status_code, unavailable.json()) == (409, {"detail": "public_trial_capacity_unavailable"})
        assert unavailable.headers["cache-control"] == "no-store"
        async with portal_app.factory() as session:
            customer = await session.scalar(select(VpnCustomer).where(VpnCustomer.telegram_user_id == "401"))
            assert customer.trial_started_at is None
            assert await session.scalar(select(func.count(VpnControlOperation.id))) == 0
            customer.trial_started_at = utcnow()
            await session.commit()
        used = await fresh.post("/api/vpn-portal/trial/activate", headers=headers)
        assert (used.status_code, used.json()) == (409, {"detail": "public_trial_already_used"})
        assert used.headers["cache-control"] == "no-store"
    finally:
        await fresh.aclose()


@pytest.mark.asyncio
async def test_disabled_public_trial_does_not_admit_fresh_mini_app_identity(portal_app):
    portal_app.settings.vpn_portal_allowed_telegram_ids = ""
    portal_app.settings.vpn_public_trial_enabled = False
    denied = await portal_app.client.post(
        "/api/vpn-portal/auth/mini-app", headers={"Origin": PORTAL_ORIGIN},
        json={"init_data": mini_app_data(402, portal_app.settings.vpn_telegram_bot_token)},
    )
    assert denied.status_code == 401
    assert denied.headers["cache-control"] == "no-store"
    async with portal_app.factory() as session:
        assert await session.scalar(select(VpnCustomer).where(VpnCustomer.telegram_user_id == "402")) is None


@pytest.mark.asyncio
async def test_public_trial_allows_fresh_oidc_identity_through_existing_callback(portal_app, monkeypatch):
    portal_app.settings.vpn_portal_allowed_telegram_ids = ""
    portal_app.settings.vpn_public_trial_enabled = True

    async def verified_identity(*args, **kwargs) -> TelegramIdentity:
        del args, kwargs
        return TelegramIdentity(user_id="403", first_name="OIDC")

    monkeypatch.setattr("app.api.routes.vpn_portal.exchange_authorization_code", verified_identity)
    portal_app.app.state.vpn_portal_http_client = None
    portal_app.app.state.vpn_portal_jwks_provider = None
    async with portal_app.factory() as session:
        state, binding, _verifier = await create_login_attempt(session)
        await session.commit()
        callback = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/vpn-portal/auth/telegram/callback",
            "query_string": urlencode({"state": state, "code": "provider-code"}).encode(),
            "headers": [(b"cookie", f"{BINDING_COOKIE}={binding}".encode())],
            "app": portal_app.app,
        })
        response = await telegram_callback(callback, session)
    assert response.headers["location"] == "/cabinet/"
    async with portal_app.factory() as session:
        customer = await session.scalar(select(VpnCustomer).where(VpnCustomer.telegram_user_id == "403"))
        assert customer is not None and customer.first_name == "OIDC"


@pytest.mark.asyncio
async def test_public_config_is_safe_fail_closed_and_never_cors_enabled(portal_app) -> None:
    response = await portal_app.client.get(
        "/api/vpn-portal/config",
        headers={"Origin": "https://attacker.example"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "enabled": True,
        "browser_login_enabled": True,
        "mini_app_enabled": True,
        "login_path": "/api/vpn-portal/auth/telegram/start",
        "support_text": "Обратитесь к администратору VPN.",
    }
    assert response.headers["cache-control"] == "no-store"
    assert "access-control-allow-origin" not in response.headers
    assert "portal-secret" not in response.text
    assert "bot-token" not in response.text

    portal_app.settings.vpn_portal_allowed_telegram_ids = ""
    disabled = await portal_app.client.get("/api/vpn-portal/config")
    assert disabled.json()["enabled"] is False
    assert disabled.json()["browser_login_enabled"] is False
    assert disabled.json()["mini_app_enabled"] is False


@pytest.mark.asyncio
async def test_customer_session_is_distinct_and_me_has_only_friendly_fields(portal_app) -> None:
    unauthenticated = await portal_app.client.get("/api/vpn-portal/me")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "no-store"

    portal_app.client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
    response = await portal_app.client.get("/api/vpn-portal/me")

    assert response.status_code == 200
    assert response.json()["display_name"] == "Alice Customer"
    assert set(response.json()) == {"display_name", "csrf_token"}
    assert len(response.json()["csrf_token"]) == 64
    assert "alice_private" not in response.text

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnCustomer)
            .where(VpnCustomer.id == portal_app.ids.alice)
            .values(first_name=None, last_name=None, telegram_username=None)
        )
        await session.commit()
    fallback = await portal_app.client.get("/api/vpn-portal/me")
    assert fallback.json()["display_name"] == "Клиент Veltrix VPN"


@pytest.mark.asyncio
async def test_portal_validation_is_static_no_store_but_outside_behavior_is_preserved(
    portal_app, caplog
) -> None:
    portal_app.client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
    forbidden_marker = "FORBIDDEN-CREDENTIAL-MARKER-3f99"
    malformed_marker = "MALFORMED-INIT-DATA-MARKER-6a21"
    oversized_marker = "OVERSIZED-INIT-DATA-MARKER-77c4"
    with caplog.at_level("INFO"):
        forbidden = await portal_app.client.patch(
            f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}",
            headers={
                "Origin": PORTAL_ORIGIN,
                "X-CSRF-Token": csrf_token(portal_app.ids.alice_raw),
            },
            json={
                "display_name": "Phone",
                "config_uri": f"vless://{forbidden_marker}",
                "status": forbidden_marker,
            },
        )
        malformed = await portal_app.client.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN, "Content-Type": "application/json"},
            content=f'{{"init_data":"{malformed_marker}'.encode(),
        )
        oversized_request = portal_app.client.build_request(
            "POST",
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN, "Content-Type": "application/json"},
            content=ChunkedBody(
                f'{{"init_data":"{oversized_marker}'.encode(),
                b"x" * 16_384,
                b'"}',
            ),
        )
        oversized = await portal_app.client.send(oversized_request)
        outside = await portal_app.client.post("/api/auth/login", json={})

    for response in (forbidden, malformed):
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_customer_request"}
        assert response.headers["cache-control"] == "no-store"
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "customer_request_rejected"}
    assert oversized.headers["cache-control"] == "no-store"

    response_bodies = " ".join(
        response.text for response in (forbidden, malformed, oversized)
    )
    for marker in (forbidden_marker, malformed_marker, oversized_marker):
        assert marker not in response_bodies
        assert marker not in caplog.text

    assert outside.status_code == 422
    assert isinstance(outside.json()["detail"], list)
    missing_locations = {tuple(error["loc"]) for error in outside.json()["detail"]}
    assert ("body", "username") in missing_locations
    assert ("body", "password") in missing_locations
    assert "cache-control" not in outside.headers


@pytest.mark.asyncio
async def test_portal_cors_bypass_is_exact_and_preserves_legacy_cors(portal_app) -> None:
    portal_preflight = await portal_app.client.options(
        "/api/vpn-portal/me",
        headers={
            "Origin": "https://legacy.example",
            "Access-Control-Request-Method": "GET",
        },
    )
    lookalike = await portal_app.client.options(
        "/api/vpn-portal-lookalike",
        headers={
            "Origin": "https://legacy.example",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert "access-control-allow-origin" not in portal_preflight.headers
    assert "access-control-allow-credentials" not in portal_preflight.headers
    assert lookalike.headers["access-control-allow-origin"] == "*"


@pytest.mark.asyncio
async def test_owned_views_mutation_guards_and_logout_are_customer_scoped(portal_app) -> None:
    portal_app.client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
    portal_app.client.cookies.set("frdm_session", "admin-cookie-must-survive")
    csrf = csrf_token(portal_app.ids.alice_raw)

    subscriptions = await portal_app.client.get("/api/vpn-portal/subscriptions")
    profiles = await portal_app.client.get("/api/vpn-portal/profiles")
    connection = await portal_app.client.get(
        f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}/connection"
    )
    assert subscriptions.status_code == profiles.status_code == connection.status_code == 200
    assert all("uri" not in profile for profile in profiles.json())
    assert connection.json()["uri"].startswith("vless://")

    hidden = []
    for profile_id in (portal_app.ids.foreign_profile, 999_999):
        response = await portal_app.client.get(
            f"/api/vpn-portal/profiles/{profile_id}/connection"
        )
        hidden.append((response.status_code, response.json()))
    assert hidden[0] == hidden[1] == (404, {"detail": "customer_profile_not_found"})

    guard_cases = (
        ("missing_csrf", {"Origin": PORTAL_ORIGIN}),
        (
            "other_session_csrf",
            {
                "Origin": PORTAL_ORIGIN,
                "X-CSRF-Token": csrf_token(portal_app.ids.bob_raw),
            },
        ),
        ("missing_origin", {"X-CSRF-Token": csrf}),
        (
            "foreign_origin",
            {"Origin": "https://foreign.example", "X-CSRF-Token": csrf},
        ),
    )
    mutation_targets = (
        (
            "PATCH",
            f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}",
        ),
        ("POST", "/api/vpn-portal/logout"),
    )
    for method, path in mutation_targets:
        for case_name, headers in guard_cases:
            request_arguments: dict[str, object] = {"headers": headers}
            if method == "PATCH":
                request_arguments["json"] = {"display_name": f"Rejected {case_name}"}
            rejected = await portal_app.client.request(
                method,
                path,
                **request_arguments,
            )
            assert rejected.status_code == 403, (method, case_name)
            assert rejected.json() == {"detail": "customer_request_rejected"}
            assert rejected.headers["cache-control"] == "no-store"
            assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 200

    async with portal_app.factory() as session:
        unchanged_profile = await session.get(VpnAccessKey, portal_app.ids.own_profile)
        unchanged_session = await session.get(
            VpnCustomerSession,
            digest_token(portal_app.ids.alice_raw),
        )
        assert unchanged_profile is not None and unchanged_profile.display_name == "Phone"
        assert unchanged_session is not None and unchanged_session.revoked_at is None

    missing_results = []
    for profile_id in (portal_app.ids.foreign_profile, 999_999):
        response = await portal_app.client.patch(
            f"/api/vpn-portal/profiles/{profile_id}",
            headers={"Origin": PORTAL_ORIGIN, "X-CSRF-Token": csrf},
            json={"display_name": "Hidden"},
        )
        missing_results.append((response.status_code, response.json()))
    assert missing_results[0] == missing_results[1] == (
        404,
        {"detail": "customer_profile_not_found"},
    )

    renamed = await portal_app.client.patch(
        f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}",
        headers={"Origin": PORTAL_ORIGIN, "X-CSRF-Token": csrf},
        json={"display_name": "  Travel laptop  "},
    )
    assert renamed.status_code == 200
    assert renamed.json()["display_name"] == "Travel laptop"

    logout = await portal_app.client.post(
        "/api/vpn-portal/logout",
        headers={"Origin": PORTAL_ORIGIN, "X-CSRF-Token": csrf},
    )
    assert logout.status_code == 200
    assert logout.json() == {"logged_out": True}
    assert portal_app.client.cookies.get("frdm_session") == "admin-cookie-must-survive"
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 401


@pytest.mark.asyncio
async def test_current_session_rechecks_archive_binding_expiry_and_pilot_access(portal_app) -> None:
    portal_app.client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 200

    portal_app.settings.vpn_portal_allowed_telegram_ids = "200"
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 401
    portal_app.settings.vpn_portal_allowed_telegram_ids = "100,200"
    portal_app.settings.vpn_portal_enabled = False
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 401
    assert (await portal_app.client.get("/api/vpn-portal/config")).json()["enabled"] is False
    portal_app.settings.vpn_portal_enabled = True

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnCustomer)
            .where(VpnCustomer.id == portal_app.ids.alice)
            .values(status="archived")
        )
        await session.commit()
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 401

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnCustomer)
            .where(VpnCustomer.id == portal_app.ids.alice)
            .values(status="active", telegram_user_id="300")
        )
        await session.commit()
    portal_app.settings.vpn_portal_allowed_telegram_ids = "100,200,300"
    assert (await portal_app.client.get("/api/vpn-portal/me")).status_code == 401


@pytest.mark.asyncio
async def test_independent_customer_jars_hide_foreign_profiles_and_admin_boundary(portal_app) -> None:
    bob = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN
    )
    admin_only = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN
    )
    try:
        bob.cookies.set(SESSION_COOKIE, portal_app.ids.bob_raw)
        bob_csrf = csrf_token(portal_app.ids.bob_raw)
        foreign_read = await bob.get(
            f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}/connection"
        )
        foreign_rename = await bob.patch(
            f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}",
            headers={"Origin": PORTAL_ORIGIN, "X-CSRF-Token": bob_csrf},
            json={"display_name": "Cannot rename"},
        )
        missing_read = await bob.get("/api/vpn-portal/profiles/999999/connection")
        assert (foreign_read.status_code, foreign_read.json()) == (
            missing_read.status_code,
            missing_read.json(),
        )
        assert foreign_rename.status_code == 404

        control = await bob.get("/api/control/vpn/customers")
        assert control.status_code == 401
        forbidden_rename = await bob.patch(
            f"/api/control/vpn/access-keys/{portal_app.ids.own_profile}/display-name",
            json={"display_name": "Customer must not rename admin profile"},
        )
        assert forbidden_rename.status_code == 401
        admin_only.cookies.set("frdm_session", "not-a-customer-session")
        assert (await admin_only.get("/api/vpn-portal/me")).status_code == 401

        async with portal_app.factory() as session:
            customer_session = await session.get(
                VpnCustomerSession, digest_token(portal_app.ids.bob_raw)
            )
            assert customer_session is not None
            customer_session.expires_at = utcnow() - timedelta(seconds=1)
            await session.commit()
        assert (await bob.get("/api/vpn-portal/me")).status_code == 401
    finally:
        await bob.aclose()
        await admin_only.aclose()


@pytest.mark.asyncio
async def test_real_admin_session_coexists_and_survives_customer_logout(
    portal_app, monkeypatch
) -> None:
    admin_raw = "admin-session-token-that-is-long-and-random-enough"
    now = utcnow()
    async with portal_app.factory() as session:
        admin = User(
            username="portal_admin",
            password_hash="unused",
            role="admin",
            status="approved",
        )
        session.add(admin)
        await session.flush()
        admin_session = UserSession(
            user_id=admin.id,
            token_hash=hash_session_token(admin_raw),
            remember_me=False,
            expires_at=now + timedelta(hours=1),
            last_used_at=now,
        )
        session.add(admin_session)
        await session.commit()
        admin_session_id = admin_session.id

    # SQLite drops timezone metadata; normalize only this fixture's clock while
    # exercising the real get_current_user/require_admin dependency chain.
    monkeypatch.setattr("app.api.deps.utcnow", lambda: utcnow().replace(tzinfo=None))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN
    )
    try:
        client.cookies.set("frdm_session", admin_raw)
        client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
        before = await client.get("/api/control/vpn/customers")
        assert before.status_code == 200
        assert (await client.get("/api/vpn-portal/me")).status_code == 200

        renamed = await client.patch(
            f"/api/control/vpn/access-keys/{portal_app.ids.own_profile}/display-name",
            json={"display_name": "Личный iPhone"},
        )
        assert renamed.status_code == 200
        assert renamed.json()["display_name"] == "Личный iPhone"
        assert "Veltrix%20VPN%20%C2%B7%20%D0%9B%D0%B8%D1%87%D0%BD%D1%8B%D0%B9%20iPhone" in renamed.json()["config_uri"]

        logout = await client.post(
            "/api/vpn-portal/logout",
            headers={
                "Origin": PORTAL_ORIGIN,
                "X-CSRF-Token": csrf_token(portal_app.ids.alice_raw),
            },
        )
        assert logout.status_code == 200
        assert client.cookies.get("frdm_session") == admin_raw
        assert (await client.get("/api/control/vpn/customers")).status_code == 200
        assert (await client.get("/api/vpn-portal/me")).status_code == 401
    finally:
        await client.aclose()

    async with portal_app.factory() as session:
        stored_admin_session = await session.get(UserSession, admin_session_id)
        assert stored_admin_session is not None
        assert stored_admin_session.revoked_at is None


@pytest.mark.asyncio
async def test_connection_rechecks_subscription_key_and_ownership_after_prior_fetch(
    portal_app,
) -> None:
    portal_app.client.cookies.set(SESSION_COOKIE, portal_app.ids.alice_raw)
    path = f"/api/vpn-portal/profiles/{portal_app.ids.own_profile}/connection"
    first = await portal_app.client.get(path)
    assert first.status_code == 200
    assert "vless://" in first.json()["uri"]

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnSubscription)
            .where(VpnSubscription.id == portal_app.ids.subscription)
            .values(expires_at=utcnow() - timedelta(seconds=1))
        )
        await session.commit()
    expired = await portal_app.client.get(path)
    assert expired.status_code == 409
    assert "uri" not in expired.text

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnSubscription)
            .where(VpnSubscription.id == portal_app.ids.subscription)
            .values(expires_at=utcnow() + timedelta(days=1))
        )
        await session.execute(
            update(VpnAccessKey)
            .where(VpnAccessKey.id == portal_app.ids.own_profile)
            .values(status="revoked")
        )
        await session.commit()
    revoked = await portal_app.client.get(path)
    assert revoked.status_code == 409
    assert "uri" not in revoked.text

    async with portal_app.factory() as session:
        await session.execute(
            update(VpnAccessKey)
            .where(VpnAccessKey.id == portal_app.ids.own_profile)
            .values(
                status="active",
                subscription_id=portal_app.ids.foreign_subscription,
            )
        )
        await session.commit()
    moved = await portal_app.client.get(path)
    assert moved.status_code == 404
    assert moved.json() == {"detail": "customer_profile_not_found"}


@pytest.mark.asyncio
async def test_lifespan_owns_and_closes_shared_oidc_client(monkeypatch) -> None:
    from app import main as main_module

    class FakeConnection:
        async def run_sync(self, operation) -> None:
            del operation

    class FakeBegin:
        async def __aenter__(self):
            return FakeConnection()

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

    class FakeEngine:
        def __init__(self) -> None:
            self.disposed = False

        def begin(self):
            return FakeBegin()

        async def dispose(self) -> None:
            self.disposed = True

    class FakeMonitoring:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.stopped = False

        async def bootstrap(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.stopped = True

    async def noop(*args, **kwargs) -> None:
        del args, kwargs

    fake_engine = FakeEngine()
    monkeypatch.setattr(main_module, "engine", fake_engine)
    monkeypatch.setattr(main_module, "run_startup_migrations", noop)
    monkeypatch.setattr(main_module, "backfill_profile_names", noop)
    monkeypatch.setattr(main_module, "ensure_owner_account", noop)
    monkeypatch.setattr(main_module, "ensure_default_zone_strategies", noop)
    monkeypatch.setattr(main_module, "ControlRuntimeOrchestrator", FakeMonitoring)
    app = FastAPI()

    async with main_module.lifespan(app):
        client = app.state.vpn_portal_http_client
        provider = app.state.vpn_portal_jwks_provider
        monitoring = app.state.monitoring
        assert client.is_closed is False
        assert client.follow_redirects is False
        assert client.timeout.connect == 10.0
        assert provider._http_client is client

    assert client.is_closed is True
    assert monitoring.stopped is True
    assert fake_engine.disposed is True


@pytest.mark.asyncio
async def test_explicit_admin_cors_is_preserved_while_portal_is_excluded(
    portal_app, monkeypatch
) -> None:
    from app import main as main_module

    portal_app.settings.cors_origins = "https://admin.example"
    monkeypatch.setattr(main_module, "settings", portal_app.settings)
    app = main_module.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=PORTAL_ORIGIN
    ) as client:
        headers = {
            "Origin": "https://admin.example",
            "Access-Control-Request-Method": "GET",
        }
        portal = await client.options("/api/vpn-portal/me", headers=headers)
        legacy = await client.options("/api/auth/me", headers=headers)

    assert "access-control-allow-origin" not in portal.headers
    assert "access-control-allow-credentials" not in portal.headers
    assert legacy.headers["access-control-allow-origin"] == "https://admin.example"
    assert legacy.headers["access-control-allow-credentials"] == "true"

@pytest.mark.asyncio
async def test_mini_app_preparse_guards_atomic_replay_and_account_conflict(portal_app) -> None:
    valid = "&".join(
        reversed(mini_app_data(100, portal_app.settings.vpn_telegram_bot_token).split("&"))
    )
    fresh = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN
    )
    try:
        wrong_type = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN, "Content-Type": "text/plain"},
            content=json.dumps({"init_data": valid}),
        )
        wrong_origin = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": "https://foreign.example"},
            json={"init_data": valid},
        )
        oversized_request = fresh.build_request(
            "POST",
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN, "Content-Type": "application/json; charset=utf-8"},
            content=ChunkedBody(b'{"init_data":"', b"x" * 16_384, b'"}'),
        )
        oversized = await fresh.send(oversized_request)
        assert wrong_type.status_code == 415
        assert wrong_origin.status_code == 403
        assert oversized.status_code == 413
        assert all(response.headers["cache-control"] == "no-store" for response in (wrong_type, wrong_origin, oversized))

        success = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": valid},
        )
        assert success.status_code == 200
        assert success.json()["display_name"] == "Mini 100"
        assert SESSION_COOKIE in fresh.cookies

        replay = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": valid},
        )
        assert replay.status_code == 200
        assert SESSION_COOKIE not in replay.headers.get("set-cookie", "")

        extra = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": valid, "config_uri": "secret", "status": "active"},
        )
        assert extra.status_code == 422
        assert extra.json() == {"detail": "invalid_customer_request"}

        bob_data = mini_app_data(200, portal_app.settings.vpn_telegram_bot_token)
        conflict = await fresh.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": bob_data},
        )
        assert conflict.status_code == 409

        second = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=portal_app.app), base_url=PORTAL_ORIGIN
        )
        try:
            replay_without_cookie = await second.post(
                "/api/vpn-portal/auth/mini-app",
                headers={"Origin": PORTAL_ORIGIN},
                json={"init_data": valid},
            )
            assert replay_without_cookie.status_code == 401
        finally:
            await second.aclose()
    finally:
        await fresh.aclose()

    async with portal_app.factory() as session:
        assert await session.scalar(select(func.count(VpnSubscription.id))) == 2


@pytest.mark.asyncio
async def test_invited_friend_can_exchange_mini_app_and_existing_session_fails_closed(
    portal_app,
) -> None:
    async with portal_app.factory() as session:
        invitation = await seed_invited_api_identity(session, "300")

    friend = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=portal_app.app),
        base_url=PORTAL_ORIGIN,
    )
    try:
        login = await friend.post(
            "/api/vpn-portal/auth/mini-app",
            headers={"Origin": PORTAL_ORIGIN},
            json={"init_data": mini_app_data(300, portal_app.settings.vpn_telegram_bot_token)},
        )
        assert login.status_code == 200
        assert (await friend.get("/api/vpn-portal/me")).status_code == 200

        async with portal_app.factory() as session:
            stored = await session.get(VpnFriendInvitation, invitation.slot)
            stored.revoked_at = utcnow()
            await session.commit()

        assert (await friend.get("/api/vpn-portal/me")).status_code == 401
    finally:
        await friend.aclose()


@pytest.mark.asyncio
async def test_invited_friend_can_complete_oidc_callback_until_revoked(
    portal_app,
    monkeypatch,
) -> None:
    async with portal_app.factory() as session:
        invitation = await seed_invited_api_identity(session, "300")

    async def invited_identity(*args, **kwargs) -> TelegramIdentity:
        del args, kwargs
        return TelegramIdentity(user_id="300", first_name="Invited")

    monkeypatch.setattr(
        "app.api.routes.vpn_portal.exchange_authorization_code",
        invited_identity,
    )
    portal_app.app.state.vpn_portal_http_client = None
    portal_app.app.state.vpn_portal_jwks_provider = None

    async def callback(db: AsyncSession):
        state, binding, _verifier = await create_login_attempt(db)
        await db.commit()
        request = Request(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/vpn-portal/auth/telegram/callback",
                "query_string": urlencode(
                    {"state": state, "code": "provider-code"}
                ).encode(),
                "headers": [
                    (b"cookie", f"{BINDING_COOKIE}={binding}".encode())
                ],
                "app": portal_app.app,
            }
        )
        return await telegram_callback(request, db)

    async with portal_app.factory() as session:
        success = await callback(session)
    assert success.headers["location"] == "/cabinet/"

    async with portal_app.factory() as session:
        stored = await session.get(VpnFriendInvitation, invitation.slot)
        stored.revoked_at = utcnow()
        await session.commit()

    async with portal_app.factory() as session:
        denied = await callback(session)
    assert denied.headers["location"] == "/cabinet/#login=failed"


@pytest.mark.asyncio
async def test_browser_pkce_callback_success_cancel_replay_and_provider_outage(portal_app) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "portal-key", "alg": "RS256", "use": "sig"})

    def token_for(user_id: int) -> str:
        now = int(time.time())
        return jwt.encode(
            {
                "iss": TELEGRAM_ISSUER,
                "aud": portal_app.settings.vpn_portal_oidc_client_id,
                "sub": f"subject-{user_id}",
                "iat": now,
                "exp": now + 300,
                "id": user_id,
                "preferred_username": "oidc_user",
                "given_name": "OIDC",
                "family_name": "Customer",
            },
            private_key,
            algorithm="RS256",
            headers={"kid": "portal-key"},
        )

    provider_up = True
    provider_user_id = 100

    def oidc_handler(request: httpx.Request) -> httpx.Response:
        if request.url == TELEGRAM_TOKEN_URL:
            if not provider_up:
                return httpx.Response(503, json={"error": "provider secret diagnostic"})
            return httpx.Response(200, json={"id_token": token_for(provider_user_id)})
        if request.url == TELEGRAM_JWKS_URL:
            return httpx.Response(200, json={"keys": [public_jwk]})
        raise AssertionError(f"unexpected provider URL: {request.url}")

    oidc_client = httpx.AsyncClient(
        transport=httpx.MockTransport(oidc_handler), timeout=10.0, follow_redirects=False
    )
    portal_app.app.state.vpn_portal_http_client = oidc_client
    portal_app.app.state.vpn_portal_jwks_provider = TelegramJWKSProvider(oidc_client)
    try:
        start = await portal_app.client.get("/api/vpn-portal/auth/telegram/start")
        assert start.status_code in {302, 307}
        query = parse_qs(urlparse(start.headers["location"]).query)
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [
            PORTAL_ORIGIN + "/api/vpn-portal/auth/telegram/callback"
        ]
        binding_cookie = start.headers["set-cookie"]
        assert "HttpOnly" in binding_cookie and "Secure" in binding_cookie and "SameSite=lax" in binding_cookie

        cancelled = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": query["state"][0], "error": "access_denied"},
        )
        assert cancelled.headers["location"] == "/cabinet/#login=failed"
        replay = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": query["state"][0], "code": "must-not-be-used"},
        )
        assert replay.headers["location"] == "/cabinet/#login=failed"

        missing_binding_start = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/start"
        )
        missing_binding_state = parse_qs(
            urlparse(missing_binding_start.headers["location"]).query
        )["state"][0]
        portal_app.client.cookies.delete(BINDING_COOKIE)
        missing_binding = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": missing_binding_state, "code": "unused"},
        )
        assert missing_binding.headers["location"] == "/cabinet/#login=failed"

        duplicate_start = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/start"
        )
        duplicate_state = parse_qs(urlparse(duplicate_start.headers["location"]).query)[
            "state"
        ][0]
        duplicate = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params=[
                ("state", duplicate_state),
                ("state", duplicate_state),
                ("code", "unused"),
            ],
        )
        assert duplicate.headers["location"] == "/cabinet/#login=failed"

        wrong_binding_start = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/start"
        )
        wrong_binding_state = parse_qs(
            urlparse(wrong_binding_start.headers["location"]).query
        )["state"][0]
        portal_app.client.cookies.set(BINDING_COOKIE, "x" * 43)
        wrong_binding = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": wrong_binding_state, "code": "unused"},
        )
        assert wrong_binding.headers["location"] == "/cabinet/#login=failed"

        portal_app.client.cookies.clear()
        success_start = await portal_app.client.get("/api/vpn-portal/auth/telegram/start")
        success_state = parse_qs(urlparse(success_start.headers["location"]).query)["state"][0]
        success = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": success_state, "code": "provider-code"},
        )
        assert success.headers["location"] == "/cabinet/"
        assert SESSION_COOKIE in portal_app.client.cookies

        async with portal_app.factory() as session:
            await session.execute(
                update(VpnCustomer)
                .where(VpnCustomer.id == portal_app.ids.bob)
                .values(status="archived")
            )
            await session.commit()
        provider_user_id = 200
        archived_start = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/start"
        )
        archived_state = parse_qs(urlparse(archived_start.headers["location"]).query)[
            "state"
        ][0]
        archived = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": archived_state, "code": "provider-code"},
        )
        assert archived.headers["location"] == "/cabinet/#login=failed"

        provider_user_id = 300
        denied_start = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/start"
        )
        denied_state = parse_qs(urlparse(denied_start.headers["location"]).query)[
            "state"
        ][0]
        denied = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": denied_state, "code": "provider-code"},
        )
        assert denied.headers["location"] == "/cabinet/#login=failed"
        async with portal_app.factory() as session:
            assert await session.scalar(select(func.count(VpnCustomer.id))) == 2

        outage_start = await portal_app.client.get("/api/vpn-portal/auth/telegram/start")
        outage_state = parse_qs(urlparse(outage_start.headers["location"]).query)["state"][0]
        provider_up = False
        outage = await portal_app.client.get(
            "/api/vpn-portal/auth/telegram/callback",
            params={"state": outage_state, "code": "secret-provider-code"},
        )
        assert outage.headers["location"] == "/cabinet/#login=failed"
        assert "secret-provider-code" not in outage.text
        assert "provider secret diagnostic" not in outage.text
    finally:
        await oidc_client.aclose()


@pytest.mark.asyncio
async def test_callback_configuration_failures_clear_binding_without_cleanup_errors(portal_app) -> None:
    start = await portal_app.client.get("/api/vpn-portal/auth/telegram/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    portal_app.settings.vpn_portal_enabled = False

    disabled = await portal_app.client.get(
        "/api/vpn-portal/auth/telegram/callback",
        params={"state": state, "code": "unused"},
    )
    assert disabled.headers["location"] == "/cabinet/#login=failed"
    assert "veltrix_login_binding=" in disabled.headers["set-cookie"]
    assert "Max-Age=0" in disabled.headers["set-cookie"]

    portal_app.settings.vpn_portal_enabled = True
    second_start = await portal_app.client.get("/api/vpn-portal/auth/telegram/start")
    second_state = parse_qs(urlparse(second_start.headers["location"]).query)["state"][0]
    portal_app.settings.vpn_portal_public_origin = "not an origin"
    invalid_origin = await portal_app.client.get(
        "/api/vpn-portal/auth/telegram/callback",
        params={"state": second_state, "code": "unused"},
    )
    assert invalid_origin.headers["location"] == "/cabinet/#login=failed"
    assert "veltrix_login_binding=" in invalid_origin.headers["set-cookie"]
    assert invalid_origin.headers["cache-control"] == "no-store"
