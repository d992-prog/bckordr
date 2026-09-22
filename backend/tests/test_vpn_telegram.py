from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.api.routes.vpn_telegram import router as vpn_telegram_router
from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import (
    AdminAuditLog,
    AppSetting,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnFriendInvitation,
    VpnNodeEvent,
    VpnSubscription,
    VpnTelegramUpdate,
    WorkerNode,
)
from app.db.session import get_db
from app.services import vpn_telegram as vpn_telegram_service
from app.services.vpn_telegram import (
    process_telegram_update,
    send_telegram_message,
    telegram_keyboard,
)
from app.services.vpn_friend_invitations import digest_invite_token
from test_vpn_endpoint_migrations import (
    PostgresSchema,
    postgres_schema as postgres_schema,
)


VALID_VLESS_URI = (
    "vless://11111111-1111-4111-8111-111111111111@vpn.example:443"
    "?type=tcp&security=tls#internal"
)
FRIEND_RELEASE_ID = "a" * 64
FRIEND_READINESS_KEY = "vpn_friend_beta_release_ready_v1"
FRIEND_TOKEN = "T" * 43
FRIEND_START = f"/start i_{FRIEND_TOKEN}"


def telegram_message(update_id: int, user_id: int | str, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id,
            "from": {
                "id": int(user_id),
                "username": "vpn_client",
                "first_name": "VPN",
                "last_name": "Client",
            },
            "chat": {"id": int(user_id), "type": "private"},
            "text": text,
        },
    }


@pytest_asyncio.fixture
async def telegram_app(monkeypatch):
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_TELEGRAM_BOT_USERNAME="veltrix_vpn_official_bot",
        VPN_TELEGRAM_WEBHOOK_SECRET="correct",
        VPN_TELEGRAM_SECRET_TOKEN="header-secret",
        VPN_SUPPORT_TEXT="Напишите администратору: @vpn_support",
        VPN_FRIEND_BETA_ENABLED=True,
        VPN_FRIEND_BETA_RELEASE_ID=FRIEND_RELEASE_ID,
        VPN_CONTROL_DISPATCH_ENABLED=True,
        VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
        VPN_PORTAL_PUBLIC_ACCESS=False,
    )
    delivered: list[dict[str, str]] = []

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    async def fake_sender(_settings, chat_id: str, text: str) -> None:
        delivered.append({"chat_id": chat_id, "text": text})

    monkeypatch.setattr("app.api.routes.vpn_telegram.get_settings", lambda: settings)
    monkeypatch.setattr("app.services.vpn_telegram.send_telegram_message", fake_sender)
    monkeypatch.setattr(
        "app.services.vpn_friend_invitations.load_transport_snapshot",
        lambda _worker, _path: object(),
    )

    app = FastAPI()
    app.include_router(vpn_telegram_router)
    app.include_router(control_router)
    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        yield SimpleNamespace(
            client=client,
            session_factory=session_factory,
            settings=settings,
            delivered=delivered,
        )

    await engine.dispose()


def webhook_headers(secret: str = "header-secret") -> dict[str, str]:
    return {"X-Telegram-Bot-Api-Secret-Token": secret}


async def seed_subscription(
    session: AsyncSession,
    *,
    telegram_user_id: str,
    customer_status: str = "active",
    key_status: str | None = None,
    config_uri: str | None = None,
    worker_id: int | None = None,
    display_name: str | None = "Телефон",
    subscription_status: str = "active",
    starts_at=None,
    expires_at=None,
) -> tuple[VpnCustomer, VpnSubscription, VpnAccessKey | None]:
    customer = VpnCustomer(telegram_user_id=telegram_user_id, status=customer_status)
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(
        customer_id=customer.id,
        status=subscription_status,
        starts_at=starts_at if starts_at is not None else utcnow() - timedelta(days=1),
        expires_at=expires_at if expires_at is not None else utcnow() + timedelta(days=30),
        max_devices=3,
    )
    session.add(subscription)
    await session.flush()
    key = None
    if key_status is not None:
        key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker_id,
            public_name="Телефон",
            display_name=display_name,
            status=key_status,
            config_uri=config_uri,
        )
        session.add(key)
    await session.commit()
    return customer, subscription, key


async def seed_friend_invitation(
    session: AsyncSession,
    *,
    token: str = FRIEND_TOKEN,
    slot: int = 1,
    redeem_expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> VpnFriendInvitation:
    now = utcnow()
    readiness = await session.scalar(
        select(AppSetting).where(AppSetting.key == FRIEND_READINESS_KEY)
    )
    if readiness is None:
        session.add_all(
            [
                AppSetting(key=FRIEND_READINESS_KEY, value=FRIEND_RELEASE_ID),
                WorkerNode(
                    id=501,
                    name="friend-beta-worker",
                    status="ready",
                    is_enabled=True,
                    vpn_enabled=True,
                    vpn_role="vpn_node",
                    vpn_runtime_status="ready",
                    ssh_host="192.0.2.51",
                    ssh_port=22,
                    ssh_username="root",
                    ssh_password="test-only-password",
                ),
            ]
        )
        await session.flush()
        session.add(
            VpnEndpoint(
                id=501,
                worker_id=501,
                inbound_id=51,
                public_host="vpn.example.test",
                port=443,
                protocol="vless",
                transport="raw",
                security="reality",
                server_name="cdn.example.test",
                public_key="A" * 43,
                short_id="0123456789abcdef",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                status="ready",
                verified_at=now - timedelta(hours=1),
            )
        )
    invitation = VpnFriendInvitation(
        slot=slot,
        token_digest=digest_invite_token(token),
        created_at=now,
        redeem_expires_at=redeem_expires_at or now + timedelta(days=7),
        revoked_at=revoked_at,
    )
    session.add(invitation)
    await session.commit()
    return invitation


async def friend_business_state(session: AsyncSession) -> tuple:
    invitation = await session.get(VpnFriendInvitation, 1)
    customer = await session.scalar(select(VpnCustomer).order_by(VpnCustomer.id))
    subscription = await session.scalar(select(VpnSubscription).order_by(VpnSubscription.id))
    access_key = await session.scalar(select(VpnAccessKey).order_by(VpnAccessKey.id))
    return (
        invitation.redeemed_at if invitation else None,
        invitation.telegram_user_id if invitation else None,
        invitation.access_key_id if invitation else None,
        customer.id if customer else None,
        subscription.id if subscription else None,
        subscription.starts_at if subscription else None,
        subscription.expires_at if subscription else None,
        access_key.id if access_key else None,
        access_key.external_uuid if access_key else None,
        int(await session.scalar(select(func.count(VpnControlOperation.id))) or 0),
    )


async def all_friend_business_state(session: AsyncSession) -> tuple:
    invitations = (
        await session.execute(
            select(
                VpnFriendInvitation.slot,
                VpnFriendInvitation.redeemed_at,
                VpnFriendInvitation.telegram_user_id,
                VpnFriendInvitation.access_key_id,
            ).order_by(VpnFriendInvitation.slot)
        )
    ).all()
    customers = (
        await session.execute(
            select(VpnCustomer.id, VpnCustomer.telegram_user_id, VpnCustomer.status)
            .order_by(VpnCustomer.id)
        )
    ).all()
    subscriptions = (
        await session.execute(
            select(
                VpnSubscription.id,
                VpnSubscription.customer_id,
                VpnSubscription.status,
                VpnSubscription.starts_at,
                VpnSubscription.expires_at,
            ).order_by(VpnSubscription.id)
        )
    ).all()
    access_keys = (
        await session.execute(
            select(
                VpnAccessKey.id,
                VpnAccessKey.subscription_id,
                VpnAccessKey.external_uuid,
                VpnAccessKey.status,
                VpnAccessKey.config_uri,
            ).order_by(VpnAccessKey.id)
        )
    ).all()
    operations = (
        await session.execute(
            select(
                VpnControlOperation.id,
                VpnControlOperation.access_key_id,
                VpnControlOperation.generation,
                VpnControlOperation.state,
            ).order_by(VpnControlOperation.access_key_id)
        )
    ).all()
    return tuple(map(tuple, (invitations, customers, subscriptions, access_keys, operations)))


def test_telegram_reply_keyboard_never_launches_an_authenticated_mini_app():
    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
    )

    allowed = telegram_keyboard(settings, "12345")
    disallowed = telegram_keyboard(settings, "54321")
    group = telegram_keyboard(settings, "-10012345")

    # Telegram does not supply WebAppInitData for a reply-keyboard launch.
    assert allowed["keyboard"] == [
        [{"text": "Моя подписка"}, {"text": "Мои профили"}],
        [{"text": "Помощь"}],
    ]
    assert all("web_app" not in button for row in allowed["keyboard"] for button in row)
    assert all("web_app" not in button for row in disallowed["keyboard"] for button in row)
    assert all("web_app" not in button for row in group["keyboard"] for button in row)


@pytest.fixture
def bot_api_requests(monkeypatch):
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        assert request.url.host == "api.telegram.org"
        assert request.url.path.endswith("/sendMessage")
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(requests)}})

    def client_factory(**kwargs):
        return real_client(transport=httpx.MockTransport(handle), **kwargs)

    monkeypatch.setattr(vpn_telegram_service.httpx, "AsyncClient", client_factory)
    return requests


@pytest.mark.asyncio
async def test_bot_sends_one_inline_cabinet_entry_after_all_response_chunks(bot_api_requests):
    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
    )
    text = "Подписка\n" + "x" * 8500

    await vpn_telegram_service.send_telegram_message(settings, "12345", text)

    chunks = vpn_telegram_service.split_telegram_text(text)
    assert len(bot_api_requests) == len(chunks) + 1
    assert [item["text"] for item in bot_api_requests[:-1]] == chunks
    for item in bot_api_requests[:-1]:
        assert item["reply_markup"] == telegram_keyboard(settings, "12345")
        assert all("web_app" not in button for row in item["reply_markup"]["keyboard"] for button in row)
    entry = bot_api_requests[-1]
    assert entry["chat_id"] == "12345"
    assert entry["text"] == "Подписка, VPN-профили и инструкции по подключению — в личном кабинете."
    assert entry["reply_markup"] == {
        "inline_keyboard": [[{
            "text": "Личный кабинет",
            "web_app": {"url": "https://portal.example/cabinet/"},
        }]],
    }
    assert "keyboard" not in entry["reply_markup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("chat_id", ["54321", "-10012345", "0", "000", "not-a-chat", "12.5", ""])
async def test_bot_never_sends_cabinet_entry_to_disallowed_identity(bot_api_requests, chat_id):
    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
    )

    await vpn_telegram_service.send_telegram_message(settings, chat_id, "Ответ")

    assert len(bot_api_requests) == 1
    assert "inline_keyboard" not in bot_api_requests[0]["reply_markup"]


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"VPN_PORTAL_ENABLED": False},
    {"VPN_PORTAL_PUBLIC_ORIGIN": "not-an-origin"},
    {"VPN_TELEGRAM_BOT_TOKEN": ""},
    {"VPN_PORTAL_ALLOWED_TELEGRAM_IDS": ""},
])
async def test_bot_never_sends_cabinet_entry_when_unavailable(bot_api_requests, changes):
    values = {
        "VPN_TELEGRAM_BOT_TOKEN": "bot-token",
        "VPN_PORTAL_ENABLED": True,
        "VPN_PORTAL_PUBLIC_ORIGIN": "https://portal.example",
        "VPN_PORTAL_ALLOWED_TELEGRAM_IDS": "12345",
        **changes,
    }

    await vpn_telegram_service.send_telegram_message(Settings(**values), "12345", "Ответ")

    assert len(bot_api_requests) == 1
    assert "inline_keyboard" not in bot_api_requests[0]["reply_markup"]


@pytest.mark.asyncio
async def test_inline_delivery_failure_does_not_replay_delivered_key(telegram_app, monkeypatch):
    async with telegram_app.session_factory() as session:
        await seed_subscription(
            session, telegram_user_id="12345", key_status="active", config_uri=VALID_VLESS_URI,
        )
    settings = telegram_app.settings.model_copy(update={
        "vpn_portal_enabled": True,
        "vpn_portal_public_origin": "https://portal.example",
        "vpn_portal_allowed_telegram_ids": "12345",
    })
    requests = []
    real_client = httpx.AsyncClient

    def handle(request):
        payload = json.loads(request.content)
        requests.append(payload)
        if "inline_keyboard" in payload["reply_markup"]:
            return httpx.Response(503, json={"ok": False})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    monkeypatch.setattr(
        vpn_telegram_service.httpx, "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    payload = telegram_message(123, 12345, "/keys")
    async with telegram_app.session_factory() as session:
        first = await process_telegram_update(session, payload, settings, sender=send_telegram_message)
        await session.commit()
    async with telegram_app.session_factory() as session:
        second = await process_telegram_update(session, payload, settings, sender=send_telegram_message)
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "123")
        )
        assert update.error_message is not None
        assert "bot-token" not in update.error_message

    assert first == {"processed": False, "duplicate": False}
    assert second == {"processed": False, "duplicate": True}
    assert len(requests) == 2
    assert "vless://" in requests[0]["text"]
    assert "inline_keyboard" in requests[1]["reply_markup"]


@pytest.mark.parametrize("chat_id", ["0", "000", "not-a-chat", "12.5", ""])
def test_telegram_keyboard_never_adds_web_app_for_zero_or_malformed_identity(chat_id):
    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
    )

    keyboard = telegram_keyboard(settings, chat_id)

    assert all("web_app" not in button for row in keyboard["keyboard"] for button in row)


@pytest.mark.parametrize(
    "settings",
    [
        Settings(
            VPN_TELEGRAM_BOT_TOKEN="bot-token",
            VPN_PORTAL_ENABLED=False,
            VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
            VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
        ),
        Settings(
            VPN_TELEGRAM_BOT_TOKEN="bot-token",
            VPN_PORTAL_ENABLED=True,
            VPN_PORTAL_PUBLIC_ORIGIN="not-an-origin",
            VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
        ),
    ],
)
def test_telegram_keyboard_falls_back_to_commands_when_portal_is_unavailable(settings):
    keyboard = telegram_keyboard(settings, "12345")

    assert keyboard["keyboard"] == [
        [{"text": "Моя подписка"}, {"text": "Мои профили"}],
        [{"text": "Помощь"}],
    ]


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_path_secret(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/wrong",
        headers=webhook_headers(),
        json={"update_id": 1},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_start_creates_customer_once_and_duplicate_update_is_idempotent(telegram_app):
    payload = telegram_message(10, 12345, "/start")
    first = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )
    second = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )
    assert first.status_code == 200
    assert first.json() == {"ok": True, "processed": True, "duplicate": False}
    assert second.status_code == 200
    assert second.json() == {"ok": True, "processed": True, "duplicate": True}
    assert len(telegram_app.delivered) == 1
    async with telegram_app.session_factory() as session:
        assert await session.scalar(select(func.count(VpnCustomer.id))) == 1
        assert await session.scalar(select(func.count(VpnTelegramUpdate.id))) == 1


@pytest.mark.asyncio
async def test_start_returns_current_subscription_status(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, subscription, _ = await seed_subscription(session, telegram_user_id="21001")

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(11, customer.telegram_user_id, "/start"),
    )
    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "VeltrixVPN" in text
    assert "Статус: активна" in text
    assert "Действует до:" in text
    assert f"Подписка #{subscription.id}" not in text
    assert "active" not in text


@pytest.mark.asyncio
async def test_status_without_subscription_points_to_support(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(12, 21002, "/status"),
    )
    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "Активной VPN-подписки нет" in text
    assert "«Помощь»" in text
    assert "«Поддержка»" not in text


@pytest.mark.asyncio
async def test_keys_returns_only_active_keys_for_valid_subscription(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, subscription, _ = await seed_subscription(
            session,
            telegram_user_id="21003",
            key_status="active",
            config_uri=VALID_VLESS_URI,
        )
        session.add(
            VpnAccessKey(
                subscription_id=subscription.id,
                public_name="Старый",
                status="revoked",
                config_uri=VALID_VLESS_URI.replace("#internal", "#revoked"),
            )
        )
        await session.commit()

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(13, customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "Телефон" in text
    assert "Veltrix%20VPN%20%C2%B7%20%D0%A2%D0%B5%D0%BB%D0%B5%D1%84%D0%BE%D0%BD" in text
    assert "#internal" not in text
    assert "#revoked" not in text


@pytest.mark.asyncio
async def test_keys_never_include_another_customers_profile(telegram_app):
    foreign_uri = VALID_VLESS_URI.replace(
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
    )
    async with telegram_app.session_factory() as session:
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21014",
            key_status="active",
            config_uri=VALID_VLESS_URI,
            display_name="Личный",
        )
        await seed_subscription(
            session,
            telegram_user_id="21015",
            key_status="active",
            config_uri=foreign_uri,
            display_name="Чужой профиль",
        )

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(26, customer.telegram_user_id, "/keys"),
    )

    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "Личный" in text
    assert "Чужой профиль" not in text
    assert "22222222-2222-4222-8222-222222222222" not in text


@pytest.mark.asyncio
async def test_keys_reject_expired_future_disabled_and_malformed_connections(telegram_app):
    now = utcnow()
    async with telegram_app.session_factory() as session:
        customer, valid_subscription, valid_key = await seed_subscription(
            session,
            telegram_user_id="21013",
            key_status="active",
            config_uri=VALID_VLESS_URI,
            display_name="Ноутбук",
        )
        assert valid_key is not None
        valid_key.expires_at = now - timedelta(seconds=1)
        for status, starts_at, expires_at in (
            ("active", now + timedelta(days=1), now + timedelta(days=2)),
            ("expired", now - timedelta(days=2), now - timedelta(days=1)),
            ("disabled", now - timedelta(days=1), now + timedelta(days=2)),
        ):
            subscription = VpnSubscription(
                customer_id=customer.id,
                status=status,
                starts_at=starts_at,
                expires_at=expires_at,
                max_devices=1,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                VpnAccessKey(
                    subscription_id=subscription.id,
                    display_name=f"Недоступен {status}",
                    status="active",
                    config_uri=VALID_VLESS_URI,
                )
            )
        malformed = VpnAccessKey(
            subscription_id=valid_subscription.id,
            display_name="Повреждённый",
            status="active",
            config_uri="vless://not-a-valid-profile",
        )
        session.add(malformed)
        await session.commit()

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(23, customer.telegram_user_id, "Мои профили"),
    )

    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert text == "Сейчас нет доступных профилей VeltrixVPN."
    assert "vless://" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("Моя подписка", "Активной VPN-подписки нет"),
        ("Мои профили", "нет доступных профилей"),
        ("Помощь", "Напишите администратору"),
    ],
)
async def test_friendly_button_aliases_work(telegram_app, command, expected):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(24 + len(command), 22000 + len(command), command),
    )

    assert response.status_code == 200
    assert expected in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("Статус", "Активной VPN-подписки нет"),
        ("Ключи", "нет доступных профилей"),
        ("Поддержка", "Напишите администратору"),
    ],
)
async def test_legacy_russian_aliases_work(telegram_app, command, expected):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(80 + len(command), 24000 + len(command), command),
    )

    assert response.status_code == 200
    assert expected in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
async def test_bot_metadata_is_normalized_before_customer_storage(telegram_app):
    payload = telegram_message(91, 24091, "/start")
    payload["message"]["from"] = {
        "id": "00024091",
        "username": "u" * 140,
        "first_name": "И" * 140,
        "last_name": "",
    }
    payload["message"]["chat"]["id"] = "00024091"

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )

    assert response.status_code == 200
    async with telegram_app.session_factory() as session:
        customer = await session.scalar(
            select(VpnCustomer).where(VpnCustomer.telegram_user_id == "24091")
        )
        assert customer is not None
        assert customer.telegram_username == "u" * 128
        assert customer.first_name == "И" * 128
        assert customer.last_name is None


@pytest.mark.asyncio
async def test_keys_are_never_sent_to_a_group_chat(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21008",
            key_status="active",
            config_uri=VALID_VLESS_URI,
        )

    payload = telegram_message(19, customer.telegram_user_id, "/keys")
    payload["message"]["chat"] = {"id": -10021008, "type": "supergroup"}
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )
    assert response.status_code == 200
    assert "только в личном чате" in telegram_app.delivered[-1]["text"]
    assert "vless://" not in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
async def test_disabled_customer_cannot_receive_keys(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21004",
            customer_status="disabled",
            key_status="active",
            config_uri=VALID_VLESS_URI,
        )

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(14, customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "VPN-профиль отключён" in text
    assert "«Помощь»" in text
    assert "«Поддержка»" not in text
    assert "vless://" not in text
    async with telegram_app.session_factory() as session:
        stored = await session.get(VpnCustomer, customer.id)
        assert stored is not None and stored.status == "disabled"


@pytest.mark.asyncio
async def test_malformed_telegram_identity_is_acknowledged_without_credentials(telegram_app):
    payload = telegram_message(25, 23001, "/keys")
    payload["message"]["from"]["id"] = -23001

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": False, "duplicate": False}
    assert telegram_app.delivered == []
    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "25")
        )
        assert update is not None and update.error_message == "invalid_identity"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        [{}],
        {"from": [{}], "chat": {"id": 23002, "type": "private"}},
        {"from": {"id": 23002}, "chat": [{}]},
        {
            "from": {"id": 23002},
            "chat": {"id": 99999, "type": "private"},
            "text": "/keys",
        },
    ],
)
async def test_malformed_private_message_shape_is_acknowledged_without_send(
    telegram_app, message
):
    update_id = 30 + len(str(message))
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json={"update_id": update_id, "message": message},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": False, "duplicate": False}
    assert telegram_app.delivered == []


@pytest.mark.asyncio
async def test_support_returns_configured_text(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(15, 21005, "/support"),
    )
    assert response.status_code == 200
    assert telegram_app.delivered[-1]["text"] == telegram_app.settings.vpn_support_text


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_header_secret(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers("wrong"),
        json=telegram_message(16, 21006, "/start"),
    )
    assert response.status_code == 403
    async with telegram_app.session_factory() as session:
        assert await session.scalar(select(func.count(VpnTelegramUpdate.id))) == 0
        assert await session.scalar(select(func.count(VpnCustomer.id))) == 0


@pytest.mark.asyncio
async def test_webhook_without_bot_configuration_returns_503(telegram_app, monkeypatch):
    settings = Settings(
        VPN_TELEGRAM_BOT_TOKEN="",
        VPN_TELEGRAM_WEBHOOK_SECRET="",
        VPN_TELEGRAM_SECRET_TOKEN="header-secret",
    )
    monkeypatch.setattr("app.api.routes.vpn_telegram.get_settings", lambda: settings)
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers={**webhook_headers(), "Content-Type": "application/json"},
        content=b"not-json",
    )
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_authenticated_malformed_update_is_acknowledged_and_recorded(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json={"update_id": 17},
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": False, "duplicate": False}
    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "17")
        )
        assert update is not None
        assert update.processed_at is None
        assert "message identity" in (update.error_message or "")


@pytest.mark.asyncio
async def test_delivery_failure_is_persisted_without_rollback(telegram_app, monkeypatch):
    async with telegram_app.session_factory() as session:
        worker = WorkerNode(
            name="telegram-node",
            registrar_slug="gandi",
            status="ready",
            is_enabled=True,
            ip_address="192.0.2.30",
            max_rps=10,
            target_rps=10,
            vpn_role="vpn_node",
            vpn_enabled=True,
            vpn_runtime_status="ready",
        )
        session.add(worker)
        await session.flush()
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21007",
            key_status="active",
            config_uri="vless://assigned",
            worker_id=worker.id,
        )

    async def failing_sender(_settings, _chat_id: str, _text: str) -> None:
        raise RuntimeError("bot-token delivery failed")

    monkeypatch.setattr("app.services.vpn_telegram.send_telegram_message", failing_sender)
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(18, customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": False, "duplicate": False}

    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "18")
        )
        refreshed_customer = await session.scalar(
            select(VpnCustomer).where(VpnCustomer.telegram_user_id == "21007")
        )
        event = await session.scalar(
            select(VpnNodeEvent).where(VpnNodeEvent.event_type == "telegram_delivery_failed")
        )
        assert update is not None
        assert update.processed_at is None
        assert update.error_message == "telegram_delivery_failed"
        assert refreshed_customer is not None
        assert refreshed_customer.telegram_username == "vpn_client"
        assert event is not None
        assert event.details == {
            "update_id": "18",
            "error": "telegram_delivery_failed",
        }


@pytest.mark.asyncio
async def test_admin_can_list_recent_telegram_updates(telegram_app):
    async with telegram_app.session_factory() as session:
        session.add_all(
            [
                VpnTelegramUpdate(update_id="older", payload={}),
                VpnTelegramUpdate(update_id="newer", payload={}, error_message="delivery failed"),
            ]
        )
        await session.commit()

    response = await telegram_app.client.get("/control/vpn/telegram-updates")
    assert response.status_code == 200
    payload = response.json()
    assert [item["update_id"] for item in payload[:2]] == ["newer", "older"]
    assert payload[0]["error_message"] == "delivery failed"


@pytest.mark.asyncio
async def test_update_reservation_survives_crash_after_delivery(telegram_app):
    class SimulatedProcessCrash(BaseException):
        pass

    calls = 0
    payload = telegram_message(20, 21009, "/start")

    async def crash_after_delivery(_settings, _chat_id: str, _text: str) -> None:
        nonlocal calls
        calls += 1
        raise SimulatedProcessCrash

    async with telegram_app.session_factory() as session:
        with pytest.raises(SimulatedProcessCrash):
            await process_telegram_update(
                session,
                payload,
                telegram_app.settings,
                sender=crash_after_delivery,
            )

    async def must_not_resend(_settings, _chat_id: str, _text: str) -> None:
        nonlocal calls
        calls += 1

    async with telegram_app.session_factory() as session:
        result = await process_telegram_update(
            session,
            payload,
            telegram_app.settings,
            sender=must_not_resend,
        )

    assert result == {"processed": False, "duplicate": True}
    assert calls == 1
    async with telegram_app.session_factory() as session:
        assert await session.scalar(select(func.count(VpnTelegramUpdate.id))) == 1


def test_telegram_message_chunks_stay_within_limit_without_losing_text():
    text = "\n".join(f"Ключ {index}\nvless://{'x' * 900}" for index in range(8))

    chunks = vpn_telegram_service.split_telegram_text(text, limit=4000)

    assert len(chunks) > 1
    assert all(len(chunk) <= 4000 for chunk in chunks)
    assert "".join(chunks) == text


@pytest.mark.parametrize(
    "token",
    [
        "A" * 43,
        "a" * 41 + "-_",
        "0123456789_abcdefghijklmnopqrstuvwxyz-ABCDE",
    ],
)
def test_friend_invite_parser_accepts_only_the_exact_ascii_start_form(token):
    assert len(token) == 43
    assert vpn_telegram_service.friend_invite_token(f"/start i_{token}") == token


@pytest.mark.parametrize(
    "text",
    [
        "/start",
        "/start i_" + "A" * 42,
        "/start i_" + "A" * 44,
        "/start i_" + "A" * 42 + "=",
        "/start i_" + "А" * 43,
        "/start i_" + "A" * 43 + " ",
        " /start i_" + "A" * 43,
        "/START i_" + "A" * 43,
        "/start  i_" + "A" * 43,
        "/start@veltrix_vpn_official_bot i_" + "A" * 43,
    ],
)
def test_friend_invite_parser_rejects_every_noncanonical_form(text):
    assert vpn_telegram_service.friend_invite_token(text) is None


@pytest.mark.parametrize(
    ("text", "stored"),
    [
        ("/start", "/start"),
        ("/status", "/status"),
        ("/keys", "/keys"),
        ("/support", "/support"),
        ("статус", "статус"),
        ("ключи", "ключи"),
        ("поддержка", "поддержка"),
        ("моя подписка", "моя подписка"),
        ("мои профили", "мои профили"),
        ("помощь", "помощь"),
        (FRIEND_START, "<redacted-start-payload>"),
        ("/start anything", "<redacted-start-payload>"),
        ("Статус", "<redacted-message>"),
        ("unknown " + FRIEND_TOKEN, "<redacted-message>"),
    ],
)
def test_telegram_text_persistence_uses_a_static_allowlist(text, stored):
    assert vpn_telegram_service.sanitized_telegram_text(text) == stored


def test_telegram_payload_projection_drops_adversarial_and_future_fields():
    link = f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}"
    payload = telegram_message(501, 50501, f"unknown {link}")
    payload["future_secret"] = FRIEND_TOKEN
    payload["friend_invitation"] = {"outcome": "redeemed", "access_key_id": 999}
    payload["message"].update(
        {
            "date": 1_795_000_000,
            "caption": link,
            "reply_to_message": {"text": FRIEND_TOKEN},
            "forward_origin": {"sender_name": link},
            "future_message_field": {"secret": FRIEND_TOKEN},
        }
    )

    sanitized = vpn_telegram_service.sanitized_telegram_payload(
        payload, payload["message"]["text"]
    )

    assert sanitized == {
        "update_id": 501,
        "message": {
            "message_id": 501,
            "date": 1_795_000_000,
            "from": {"id": 50501},
            "chat": {"id": 50501, "type": "private"},
            "text": "<redacted-message>",
        },
    }
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert FRIEND_TOKEN not in serialized
    assert link not in serialized


def test_telegram_payload_projection_canonicalizes_every_persisted_scalar():
    link = f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}"
    payload = {
        "update_id": {"secret": FRIEND_TOKEN},
        "message": {
            "message_id": [link],
            "date": {"secret": FRIEND_TOKEN},
            "from": {"id": link},
            "chat": {"id": {"secret": link}, "type": FRIEND_TOKEN},
            "text": f"unknown {link}",
        },
    }

    sanitized = vpn_telegram_service.sanitized_telegram_payload(
        payload, payload["message"]["text"]
    )

    assert sanitized == {
        "update_id": None,
        "message": {
            "message_id": None,
            "date": None,
            "from": {"id": None},
            "chat": {"id": None, "type": None},
            "text": "<redacted-message>",
        },
    }
    serialized = json.dumps(sanitized, ensure_ascii=False)
    assert FRIEND_TOKEN not in serialized
    assert link not in serialized


def test_telegram_payload_projection_preserves_canonical_scalar_boundaries():
    payload = {
        "update_id": 2**63 - 1,
        "message": {
            "message_id": 2**31 - 1,
            "date": 2**63 - 1,
            "from": {"id": 2**52 - 1},
            "chat": {"id": -(2**52 - 1), "type": "channel"},
            "text": "/status",
        },
    }

    assert vpn_telegram_service.sanitized_telegram_payload(payload, "/status") == payload


@pytest.mark.parametrize(
    ("field", "invalid"),
    [
        ("message_id", True),
        ("message_id", 0),
        ("message_id", 2**31),
        ("date", True),
        ("date", -1),
        ("date", 2**63),
        ("from_id", True),
        ("from_id", 0),
        ("from_id", 2**52),
        ("chat_id", True),
        ("chat_id", 0),
        ("chat_id", -(2**52)),
        ("chat_id", 2**52),
        ("chat_type", {"secret": FRIEND_TOKEN}),
        ("chat_type", "Private"),
    ],
)
def test_telegram_payload_projection_drops_noncanonical_scalar_boundaries(
    field, invalid
):
    payload = telegram_message(525, 50525, "/status")
    if field == "from_id":
        payload["message"]["from"]["id"] = invalid
        expected_path = ("from", "id")
    elif field == "chat_id":
        payload["message"]["chat"]["id"] = invalid
        expected_path = ("chat", "id")
    elif field == "chat_type":
        payload["message"]["chat"]["type"] = invalid
        expected_path = ("chat", "type")
    else:
        payload["message"][field] = invalid
        expected_path = (field,)

    sanitized = vpn_telegram_service.sanitized_telegram_payload(payload, "/status")
    value = sanitized["message"]
    for part in expected_path:
        value = value[part]
    assert value is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw_update_id",
    [
        FRIEND_TOKEN,
        f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}",
        {"secret": FRIEND_TOKEN},
        True,
        -1,
        2**63,
    ],
)
async def test_noncanonical_update_id_is_rejected_before_any_insert(
    telegram_app, caplog, raw_update_id
):
    payload = telegram_message(1, 50522, "/status")
    payload["update_id"] = raw_update_id

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": False, "duplicate": False}
    async with telegram_app.session_factory() as session:
        assert await session.scalar(select(func.count(VpnTelegramUpdate.id))) == 0
    assert FRIEND_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_adversarial_scalar_types_are_removed_from_the_stored_update(
    telegram_app, caplog
):
    link = f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}"
    payload = {
        "update_id": 522,
        "message": {
            "message_id": {"secret": FRIEND_TOKEN},
            "date": [link],
            "from": {"id": {"secret": FRIEND_TOKEN}},
            "chat": {"id": [link], "type": link},
            "text": f"unknown {link}",
        },
    }

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )

    assert response.status_code == 200
    async with telegram_app.session_factory() as session:
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "522")
        )
    assert stored is not None
    assert stored.payload == {
        "update_id": 522,
        "message": {
            "message_id": None,
            "date": None,
            "from": {"id": None},
            "chat": {"id": None, "type": None},
            "text": "<redacted-message>",
        },
    }
    persisted = repr((stored.update_id, stored.payload, stored.error_message))
    assert FRIEND_TOKEN not in persisted
    assert link not in persisted
    assert FRIEND_TOKEN not in caplog.text
    assert link not in caplog.text


@pytest.mark.asyncio
async def test_adversarial_update_is_redacted_before_database_insert(telegram_app, caplog):
    link = f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}"
    payload = telegram_message(502, 50502, f"unknown {link}")
    payload["future_secret"] = FRIEND_TOKEN
    payload["friend_invitation"] = {"outcome": "redeemed", "access_key_id": 999}
    payload["message"].update(
        {
            "caption": link,
            "reply_to_message": {"text": FRIEND_TOKEN},
            "forward_origin": {"sender_name": link},
            "future_message_field": {"secret": FRIEND_TOKEN},
        }
    )

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=payload,
    )

    assert response.status_code == 200
    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "502")
        )
        assert update is not None
        assert update.payload == {
            "update_id": 502,
            "message": {
                "message_id": 502,
                "date": None,
                "from": {"id": 50502},
                "chat": {"id": 50502, "type": "private"},
                "text": "<redacted-message>",
            },
        }
        persisted = json.dumps(update.payload, ensure_ascii=False)
        audits = list(await session.scalars(select(AdminAuditLog)))
    assert FRIEND_TOKEN not in persisted
    assert link not in persisted
    assert FRIEND_TOKEN not in caplog.text
    assert link not in caplog.text
    assert FRIEND_TOKEN not in repr(audits)
    assert link not in repr(audits)


@pytest.mark.asyncio
async def test_friend_invite_redeems_atomically_and_sends_preparing_status(telegram_app):
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(503, 50503, FRIEND_START),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": True, "duplicate": False}
    assert telegram_app.delivered[-1]["text"] == vpn_telegram_service.INVITATION_PREPARING_TEXT
    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "503")
        )
        invitation = await session.get(VpnFriendInvitation, 1)
        assert update is not None and update.processed_at is not None
        assert update.payload["message"]["text"] == "<redacted-start-payload>"
        assert invitation is not None and invitation.telegram_user_id == "50503"
        assert update.customer_id is not None
        access_key = await session.get(VpnAccessKey, invitation.access_key_id)
        assert access_key is not None
        assert update.payload["friend_invitation"] == {
            "outcome": "redeemed",
            "access_key_id": access_key.id,
        }
        subscription = await session.get(VpnSubscription, access_key.subscription_id)
        assert subscription is not None and update.customer_id == subscription.customer_id


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["malformed", "expired", "revoked", "wrong_owner"])
async def test_all_rejected_invitations_have_identical_response_and_no_business_change(
    telegram_app, case
):
    tokens = {
        "malformed": "M" * 42,
        "expired": "E" * 43,
        "revoked": "R" * 43,
        "wrong_owner": "W" * 43,
    }
    token = tokens[case]
    async with telegram_app.session_factory() as session:
        if case == "expired":
            await seed_friend_invitation(
                session, token=token, redeem_expires_at=utcnow() - timedelta(seconds=1)
            )
        elif case == "revoked":
            await seed_friend_invitation(session, token=token, revoked_at=utcnow())
        elif case == "wrong_owner":
            await seed_friend_invitation(session, token=token)

    if case == "wrong_owner":
        first = await telegram_app.client.post(
            "/vpn-telegram/webhook/correct",
            headers=webhook_headers(),
            json=telegram_message(510, 50510, f"/start i_{token}"),
        )
        assert first.status_code == 200
        telegram_app.delivered.clear()

    async with telegram_app.session_factory() as session:
        before = await friend_business_state(session)

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(511, 50511, f"/start i_{token}"),
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "processed": True, "duplicate": False}
    assert telegram_app.delivered == [
        {"chat_id": "50511", "text": vpn_telegram_service.INVITATION_REJECTED_TEXT}
    ]
    async with telegram_app.session_factory() as session:
        after = await friend_business_state(session)
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "511")
        )
        assert update is not None
        assert update.payload["message"]["text"] == "<redacted-start-payload>"
        assert update.payload["friend_invitation"] == {"outcome": "rejected"}
    assert after == before


@pytest.mark.asyncio
async def test_friend_invite_never_redeems_outside_matching_private_chat(telegram_app):
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)
    payload = telegram_message(512, 50512, FRIEND_START)
    payload["message"]["chat"] = {"id": -10050512, "type": "supergroup"}

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct", headers=webhook_headers(), json=payload
    )

    assert response.status_code == 200
    assert "только в личном чате" in telegram_app.delivered[-1]["text"]
    async with telegram_app.session_factory() as session:
        invitation = await session.get(VpnFriendInvitation, 1)
        assert invitation is not None and invitation.redeemed_at is None
        assert await session.scalar(select(func.count(VpnCustomer.id))) == 0


@pytest.mark.asyncio
async def test_friend_invite_crash_replays_status_without_business_mutation(telegram_app):
    class SimulatedProcessCrash(BaseException):
        pass

    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)
    payload = telegram_message(513, 50513, FRIEND_START)

    async def crash_after_commit(_settings, _chat_id: str, _text: str) -> None:
        raise SimulatedProcessCrash

    async with telegram_app.session_factory() as session:
        with pytest.raises(SimulatedProcessCrash):
            await process_telegram_update(
                session,
                payload,
                telegram_app.settings,
                sender=crash_after_commit,
            )

    async with telegram_app.session_factory() as session:
        committed = await friend_business_state(session)
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "513")
        )
        assert update is not None and update.processed_at is None

    replayed: list[str] = []

    async def capture(_settings, _chat_id: str, text: str) -> None:
        replayed.append(text)

    async with telegram_app.session_factory() as session:
        duplicate = await process_telegram_update(
            session, payload, telegram_app.settings, sender=capture
        )
        await session.commit()
    assert duplicate == {"processed": True, "duplicate": True}
    assert replayed == [vpn_telegram_service.INVITATION_PREPARING_TEXT]

    new_payload = telegram_message(514, 50513, FRIEND_START)
    async with telegram_app.session_factory() as session:
        repeated = await process_telegram_update(
            session, new_payload, telegram_app.settings, sender=capture
        )
        await session.commit()
        replay_state = await friend_business_state(session)
        original = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "513")
        )
        assert original is not None and original.processed_at is not None

    assert repeated == {"processed": True, "duplicate": False}
    assert replayed == [
        vpn_telegram_service.INVITATION_PREPARING_TEXT,
        vpn_telegram_service.INVITATION_PREPARING_TEXT,
    ]
    assert replay_state == committed


@pytest.mark.asyncio
async def test_friend_invite_replay_sends_the_profile_once_it_is_active(telegram_app):
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)
    payload = telegram_message(516, 50516, FRIEND_START)

    async def fail_after_commit(_settings, _chat_id: str, _text: str) -> None:
        raise RuntimeError("delivery failed")

    async with telegram_app.session_factory() as session:
        first = await process_telegram_update(
            session, payload, telegram_app.settings, sender=fail_after_commit
        )
        await session.commit()
        invitation = await session.get(VpnFriendInvitation, 1)
        assert invitation is not None
        access_key = await session.get(VpnAccessKey, invitation.access_key_id)
        assert access_key is not None and access_key.external_uuid is not None
        access_key.status = "active"
        access_key.config_uri = VALID_VLESS_URI.replace(
            "11111111-1111-4111-8111-111111111111", access_key.external_uuid
        )
        expected_uuid = access_key.external_uuid
        await session.commit()

    delivered: list[str] = []

    async def capture(_settings, _chat_id: str, text: str) -> None:
        delivered.append(text)

    async with telegram_app.session_factory() as session:
        replay = await process_telegram_update(
            session, payload, telegram_app.settings, sender=capture
        )
        await session.commit()

    assert first == {"processed": False, "duplicate": False}
    assert replay == {"processed": True, "duplicate": True}
    assert len(delivered) == 1
    assert "vless://" in delivered[0]
    assert expected_uuid in delivered[0]


@pytest.mark.asyncio
async def test_friend_invite_replay_refreshes_cached_chain_after_commit(telegram_app):
    async with telegram_app.session_factory() as seed_session:
        await seed_friend_invitation(seed_session)
    payload = telegram_message(517, 50517, FRIEND_START)

    async def fail_after_commit(_settings, _chat_id: str, _text: str) -> None:
        raise RuntimeError("delivery failed")

    delivered: list[str] = []

    async def capture(_settings, _chat_id: str, text: str) -> None:
        delivered.append(text)

    async with telegram_app.session_factory() as session:
        await process_telegram_update(
            session, payload, telegram_app.settings, sender=fail_after_commit
        )
        await session.commit()
        invitation = await session.get(VpnFriendInvitation, 1)
        assert invitation is not None
        cached_key = await session.get(VpnAccessKey, invitation.access_key_id)
        assert cached_key is not None and cached_key.external_uuid is not None
        active_uri = VALID_VLESS_URI.replace(
            "11111111-1111-4111-8111-111111111111", cached_key.external_uuid
        )
        await session.execute(
            update(VpnAccessKey)
            .where(VpnAccessKey.id == cached_key.id)
            .values(status="active", config_uri=active_uri)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        assert cached_key.status == "pending_sync"

        replay = await process_telegram_update(
            session, payload, telegram_app.settings, sender=capture
        )
        await session.commit()

    assert replay == {"processed": True, "duplicate": True}
    assert len(delivered) == 1
    assert cached_key.external_uuid in delivered[0]


@pytest.mark.asyncio
async def test_second_invitation_response_and_replay_use_only_its_exact_key(telegram_app):
    first_token = "1" * 43
    second_token = "2" * 43
    user_id = 50518
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session, token=first_token, slot=1)
        await seed_friend_invitation(session, token=second_token, slot=2)

    first = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(518, user_id, f"/start i_{first_token}"),
    )
    assert first.status_code == 200
    async with telegram_app.session_factory() as session:
        first_invitation = await session.get(VpnFriendInvitation, 1)
        assert first_invitation is not None
        first_key = await session.get(VpnAccessKey, first_invitation.access_key_id)
        assert first_key is not None and first_key.external_uuid is not None
        first_key.status = "active"
        first_key.config_uri = VALID_VLESS_URI.replace(
            "11111111-1111-4111-8111-111111111111", first_key.external_uuid
        )
        first_uuid = first_key.external_uuid
        await session.commit()

    class SimulatedProcessCrash(BaseException):
        pass

    attempted: list[str] = []

    async def crash_after_send(_settings, _chat_id: str, text: str) -> None:
        attempted.append(text)
        raise SimulatedProcessCrash

    second_payload = telegram_message(519, user_id, f"/start i_{second_token}")
    async with telegram_app.session_factory() as session:
        with pytest.raises(SimulatedProcessCrash):
            await process_telegram_update(
                session,
                second_payload,
                telegram_app.settings,
                sender=crash_after_send,
            )

    assert attempted == [vpn_telegram_service.INVITATION_PREPARING_TEXT]
    assert first_uuid not in attempted[0]
    async with telegram_app.session_factory() as session:
        pending_update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "519")
        )
        second_invitation = await session.get(VpnFriendInvitation, 2)
        assert pending_update is not None and second_invitation is not None
        assert pending_update.customer_id is not None
        assert second_invitation.access_key_id is not None
        second_key = await session.get(VpnAccessKey, second_invitation.access_key_id)
        assert second_key is not None and second_key.external_uuid is not None
        assert pending_update.payload["friend_invitation"] == {
            "outcome": "redeemed",
            "access_key_id": second_key.id,
        }
        assert pending_update.error_message is None
        second_key.status = "active"
        second_key.config_uri = VALID_VLESS_URI.replace(
            "11111111-1111-4111-8111-111111111111", second_key.external_uuid
        )
        second_uuid = second_key.external_uuid
        await session.commit()
        before_replay = await all_friend_business_state(session)

    replayed: list[str] = []

    async def capture(_settings, _chat_id: str, text: str) -> None:
        replayed.append(text)

    async with telegram_app.session_factory() as session:
        result = await process_telegram_update(
            session, second_payload, telegram_app.settings, sender=capture
        )
        await session.commit()
        after_replay = await all_friend_business_state(session)
        completed_update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "519")
        )

    assert result == {"processed": True, "duplicate": True}
    assert len(replayed) == 1
    assert second_uuid in replayed[0]
    assert first_uuid not in replayed[0]
    assert after_replay == before_replay
    assert completed_update is not None and completed_update.processed_at is not None
    assert completed_update.error_message is None


@pytest.mark.asyncio
async def test_malformed_update_is_parsed_before_its_sanitized_insert(
    telegram_app, monkeypatch
):
    order: list[str] = []
    original_parser = vpn_telegram_service.parse_telegram_message

    def observed_parser(payload):
        order.append("parse")
        return original_parser(payload)

    def observed_insert(_mapper, _connection, _target):
        order.append("insert")

    monkeypatch.setattr(vpn_telegram_service, "parse_telegram_message", observed_parser)
    event.listen(VpnTelegramUpdate, "before_insert", observed_insert)
    try:
        response = await telegram_app.client.post(
            "/vpn-telegram/webhook/correct",
            headers=webhook_headers(),
            json={
                "update_id": 520,
                "future_secret": FRIEND_TOKEN,
                "message": {"caption": FRIEND_TOKEN},
            },
        )
    finally:
        event.remove(VpnTelegramUpdate, "before_insert", observed_insert)

    assert response.status_code == 200
    assert order == ["parse", "insert"]
    async with telegram_app.session_factory() as session:
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "520")
        )
    assert stored is not None
    assert stored.payload == {
        "update_id": 520,
        "message": {
            "message_id": None,
            "date": None,
            "from": {"id": None},
            "chat": {"id": None, "type": None},
            "text": "<redacted-message>",
        },
    }
    assert stored.error_message == "Telegram update does not contain a message identity"
    assert FRIEND_TOKEN not in repr((stored.payload, stored.error_message))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case", "expected_text", "expected_context"),
    [
        (
            "rejected",
            vpn_telegram_service.INVITATION_REJECTED_TEXT,
            {"outcome": "rejected"},
        ),
        (
            "unavailable",
            vpn_telegram_service.INVITATION_UNAVAILABLE_TEXT,
            {"outcome": "unavailable"},
        ),
    ],
)
async def test_invitation_context_recovers_the_exact_response(
    telegram_app, case, expected_text, expected_context
):
    if case == "unavailable":
        async with telegram_app.session_factory() as session:
            await seed_friend_invitation(session)
        telegram_app.settings.vpn_control_dispatch_enabled = False
        text = FRIEND_START
    else:
        text = "/start i_" + "M" * 42
    payload = telegram_message(521, 50521, text)

    async def fail_delivery(_settings, _chat_id: str, _text: str) -> None:
        raise RuntimeError("delivery failed")

    async with telegram_app.session_factory() as session:
        first = await process_telegram_update(
            session, payload, telegram_app.settings, sender=fail_delivery
        )
        await session.commit()
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "521")
        )
        assert stored is not None
        assert stored.error_message == "telegram_delivery_failed"
        assert stored.payload["friend_invitation"] == expected_context

    delivered: list[str] = []

    async def capture(_settings, _chat_id: str, response_text: str) -> None:
        delivered.append(response_text)

    async with telegram_app.session_factory() as session:
        replay = await process_telegram_update(
            session, payload, telegram_app.settings, sender=capture
        )
        await session.commit()
        completed = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "521")
        )
        assert await session.scalar(select(func.count(VpnCustomer.id))) == 0
        assert await session.scalar(select(func.count(VpnSubscription.id))) == 0
        assert await session.scalar(select(func.count(VpnAccessKey.id))) == 0
        assert await session.scalar(select(func.count(VpnControlOperation.id))) == 0

    assert first == {"processed": False, "duplicate": False}
    assert replay == {"processed": True, "duplicate": True}
    assert delivered == [expected_text]
    assert completed is not None and completed.processed_at is not None
    assert completed.error_message is None


@pytest.mark.asyncio
async def test_invite_delivery_errors_never_persist_token_link_uuid_or_uri(
    telegram_app, caplog
):
    link = f"https://t.me/veltrix_vpn_official_bot?start=i_{FRIEND_TOKEN}"
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)

    captured: dict[str, str] = {}

    async def fail_with_secrets(_settings, _chat_id: str, _text: str) -> None:
        async with telegram_app.session_factory() as inspect_session:
            invitation = await inspect_session.get(VpnFriendInvitation, 1)
            assert invitation is not None and invitation.access_key_id is not None
            access_key = await inspect_session.get(VpnAccessKey, invitation.access_key_id)
            assert access_key is not None and access_key.external_uuid is not None
            captured["uuid"] = access_key.external_uuid
            captured["uri"] = VALID_VLESS_URI.replace(
                "11111111-1111-4111-8111-111111111111",
                access_key.external_uuid,
            )
        raise RuntimeError(
            f"{FRIEND_TOKEN} {link} {captured['uuid']} {captured['uri']}"
        )

    async with telegram_app.session_factory() as session:
        result = await process_telegram_update(
            session,
            telegram_message(515, 50515, FRIEND_START),
            telegram_app.settings,
            sender=fail_with_secrets,
        )
        await session.commit()

    assert result == {"processed": False, "duplicate": False}
    async with telegram_app.session_factory() as session:
        update = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "515")
        )
        invitation = await session.get(VpnFriendInvitation, 1)
        operations = list(await session.scalars(select(VpnControlOperation)))
        events = list(await session.scalars(select(VpnNodeEvent)))
        audits = list(await session.scalars(select(AdminAuditLog)))
    assert update is not None
    assert len(events) == 1
    assert invitation is not None and len(operations) == 1
    assert update.payload["friend_invitation"] == {
        "outcome": "redeemed",
        "access_key_id": invitation.access_key_id,
    }
    assert captured["uuid"] in repr(operations[0].request_snapshot)
    persisted = repr(
        (
            update.payload,
            update.error_message,
            invitation.token_digest,
            [(event.message, event.details) for event in events],
            [(audit.action, audit.details) for audit in audits],
        )
    )
    for secret in (FRIEND_TOKEN, link, captured["uuid"], captured["uri"]):
        assert secret not in persisted
        assert secret not in caplog.text
    assert update.error_message == "telegram_delivery_failed"
    assert events[0].details == {
        "update_id": "515",
        "error": "telegram_delivery_failed",
    }


@pytest.mark.asyncio
async def test_invite_delivery_failure_uses_its_exact_key_worker(telegram_app) -> None:
    async with telegram_app.session_factory() as session:
        await seed_friend_invitation(session)

    class SimulatedProcessCrash(BaseException):
        pass

    async def crash_after_commit(_settings, _chat_id: str, _text: str) -> None:
        raise SimulatedProcessCrash

    payload = telegram_message(524, 50524, FRIEND_START)
    async with telegram_app.session_factory() as session:
        with pytest.raises(SimulatedProcessCrash):
            await process_telegram_update(
                session,
                payload,
                telegram_app.settings,
                sender=crash_after_commit,
            )

    async with telegram_app.session_factory() as session:
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "524")
        )
        invitation = await session.get(VpnFriendInvitation, 1)
        assert stored is not None and stored.customer_id is not None
        assert invitation is not None and invitation.access_key_id is not None
        invited_key = await session.get(VpnAccessKey, invitation.access_key_id)
        assert invited_key is not None and invited_key.worker_id == 501
        session.add(
            WorkerNode(
                id=502,
                name="unrelated-worker",
                status="ready",
                is_enabled=True,
                vpn_enabled=True,
                vpn_role="vpn_node",
                vpn_runtime_status="ready",
                ssh_host="192.0.2.52",
            )
        )
        unrelated_subscription = VpnSubscription(
            customer_id=stored.customer_id,
            status="active",
            starts_at=utcnow(),
            expires_at=utcnow() + timedelta(days=30),
        )
        session.add(unrelated_subscription)
        await session.flush()
        session.add(
            VpnAccessKey(
                subscription_id=unrelated_subscription.id,
                worker_id=502,
                status="active",
            )
        )
        await session.commit()

    async with telegram_app.session_factory() as session:
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "524")
        )
        assert stored is not None
        await vpn_telegram_service._record_delivery_failure(session, stored)
        await session.commit()

    async with telegram_app.session_factory() as session:
        events = list(await session.scalars(select(VpnNodeEvent)))
    assert len(events) == 1
    assert events[0].worker_id == 501


@pytest.mark.asyncio
async def test_postgres_concurrent_invitation_replays_send_exactly_once(
    postgres_schema: PostgresSchema,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    settings = Settings(
        _env_file=None,
        VPN_TELEGRAM_BOT_TOKEN="bot-token",
        VPN_TELEGRAM_BOT_USERNAME="veltrix_vpn_official_bot",
        VPN_FRIEND_BETA_ENABLED=True,
        VPN_FRIEND_BETA_RELEASE_ID=FRIEND_RELEASE_ID,
        VPN_CONTROL_DISPATCH_ENABLED=True,
        VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
        VPN_PORTAL_PUBLIC_ACCESS=False,
    )
    monkeypatch.setattr(
        "app.services.vpn_friend_invitations.load_transport_snapshot",
        lambda _worker, _path: object(),
    )
    async with sessions() as session:
        await seed_friend_invitation(session)

    payload = telegram_message(523, 50523, FRIEND_START)

    class SimulatedProcessCrash(BaseException):
        pass

    async def crash_after_commit(_settings, _chat_id: str, _text: str) -> None:
        raise SimulatedProcessCrash

    async with sessions() as session:
        with pytest.raises(SimulatedProcessCrash):
            await process_telegram_update(
                session,
                payload,
                settings,
                sender=crash_after_commit,
            )

    first_sender_entered = asyncio.Event()
    release_first_sender = asyncio.Event()
    second_select_started = asyncio.Event()
    second_select_finished = asyncio.Event()
    sends: list[str] = []

    class ObservedReplaySession(AsyncSession):
        async def scalar(self, statement, *args, **kwargs):
            if "vpn_telegram_updates" in str(statement):
                second_select_started.set()
                result = await super().scalar(statement, *args, **kwargs)
                second_select_finished.set()
                return result
            return await super().scalar(statement, *args, **kwargs)

    async def hold_first_sender(_settings, _chat_id: str, _text: str) -> None:
        sends.append("first")
        first_sender_entered.set()
        await asyncio.wait_for(release_first_sender.wait(), timeout=15)

    async def record_second_sender(_settings, _chat_id: str, _text: str) -> None:
        sends.append("second")

    async def first_replay() -> dict[str, bool]:
        async with sessions() as session:
            result = await process_telegram_update(
                session,
                payload,
                settings,
                sender=hold_first_sender,
            )
            await session.commit()
            return result

    async def second_replay() -> dict[str, bool]:
        async with ObservedReplaySession(
            bind=postgres_schema.engine,
            expire_on_commit=False,
        ) as session:
            result = await process_telegram_update(
                session,
                payload,
                settings,
                sender=record_second_sender,
            )
            await session.commit()
            return result

    first_task = asyncio.create_task(first_replay())
    await asyncio.wait_for(first_sender_entered.wait(), timeout=15)
    second_task = asyncio.create_task(second_replay())
    try:
        await asyncio.wait_for(second_select_started.wait(), timeout=15)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(second_select_finished.wait(), timeout=0.25)
        assert sends == ["first"]
    finally:
        release_first_sender.set()
        first_result, second_result = await asyncio.wait_for(
            asyncio.gather(first_task, second_task),
            timeout=15,
        )
    async with sessions() as session:
        stored = await session.scalar(
            select(VpnTelegramUpdate).where(VpnTelegramUpdate.update_id == "523")
        )

    assert first_result == {"processed": True, "duplicate": True}
    assert second_result == {"processed": True, "duplicate": True}
    assert sends == ["first"]
    assert stored is not None and stored.processed_at is not None
