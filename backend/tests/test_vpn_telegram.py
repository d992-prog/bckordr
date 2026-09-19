from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.api.routes.vpn_telegram import router as vpn_telegram_router
from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    VpnTelegramUpdate,
    WorkerNode,
)
from app.db.session import get_db
from app.services import vpn_telegram as vpn_telegram_service
from app.services.vpn_telegram import process_telegram_update


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
        VPN_TELEGRAM_WEBHOOK_SECRET="correct",
        VPN_TELEGRAM_SECRET_TOKEN="header-secret",
        VPN_SUPPORT_TEXT="Напишите администратору: @vpn_support",
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
) -> tuple[VpnCustomer, VpnSubscription, VpnAccessKey | None]:
    customer = VpnCustomer(telegram_user_id=telegram_user_id, status=customer_status)
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(
        customer_id=customer.id,
        status="active",
        starts_at=utcnow() - timedelta(days=1),
        expires_at=utcnow() + timedelta(days=30),
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
            status=key_status,
            config_uri=config_uri,
        )
        session.add(key)
    await session.commit()
    return customer, subscription, key


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
    assert "VPN-бот готов" in text
    assert f"#{subscription.id}" in text


@pytest.mark.asyncio
async def test_status_without_subscription_points_to_support(telegram_app):
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(12, 21002, "/status"),
    )
    assert response.status_code == 200
    assert "Активной VPN-подписки нет" in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
async def test_keys_returns_only_active_keys_for_valid_subscription(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, subscription, _ = await seed_subscription(
            session,
            telegram_user_id="21003",
            key_status="active",
            config_uri="vless://active",
        )
        session.add(
            VpnAccessKey(
                subscription_id=subscription.id,
                public_name="Старый",
                status="revoked",
                config_uri="vless://revoked",
            )
        )
        await session.commit()

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(13, customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    assert "vless://active" in telegram_app.delivered[-1]["text"]
    assert "vless://revoked" not in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
async def test_keys_are_never_sent_to_a_group_chat(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21008",
            key_status="active",
            config_uri="vless://private-only",
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
    assert "vless://private-only" not in telegram_app.delivered[-1]["text"]


@pytest.mark.asyncio
async def test_disabled_customer_cannot_receive_keys(telegram_app):
    async with telegram_app.session_factory() as session:
        customer, _, _ = await seed_subscription(
            session,
            telegram_user_id="21004",
            customer_status="disabled",
            key_status="active",
            config_uri="vless://disabled-customer",
        )

    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(14, customer.telegram_user_id, "/keys"),
    )
    assert response.status_code == 200
    text = telegram_app.delivered[-1]["text"]
    assert "VPN-профиль отключён" in text
    assert "vless://disabled-customer" not in text


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
        assert "<redacted>" in (update.error_message or "")
        assert "bot-token" not in (update.error_message or "")
        assert refreshed_customer is not None
        assert refreshed_customer.telegram_username == "vpn_client"
        assert event is not None
        assert event.details["update_id"] == "18"
        assert "bot-token" not in event.details["error"]


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
