import asyncio
from datetime import timedelta

import httpx
import pytest
from test_vpn_friend_invitations import NOW
from test_vpn_friend_invitations import (
    session_factory as session_factory,  # noqa: PLC0414
)

from app.services import vpn_bot_delivery as delivery


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,data,state,code,retry",
    [
        (
            200,
            {"ok": True, "result": {"message_id": 7, "chat": {"id": 12345}}},
            "sent",
            None,
            None,
        ),
        (
            200,
            {"ok": True, "result": {"message_id": 7, "chat": {"id": 77777}}},
            "uncertain",
            "vpn_bot_delivery_uncertain",
            None,
        ),
        (
            200,
            {"ok": True, "result": {"message_id": True, "chat": {"id": 12345}}},
            "uncertain",
            "vpn_bot_delivery_uncertain",
            None,
        ),
        (
            200,
            {"ok": True, "result": {"message_id": 2**63, "chat": {"id": 12345}}},
            "uncertain",
            "vpn_bot_delivery_uncertain",
            None,
        ),
        (200, {"ok": True}, "uncertain", "vpn_bot_delivery_uncertain", None),
        (200, {"ok": False, "error_code": 403}, "blocked", "vpn_bot_blocked", None),
        (403, {"ok": False, "error_code": 403}, "blocked", "vpn_bot_blocked", None),
        (
            400,
            {"ok": False, "error_code": 400},
            "failed",
            "vpn_bot_request_rejected",
            None,
        ),
        (401, {"ok": False, "error_code": 401}, "failed", "vpn_bot_auth_failed", None),
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
            "retry",
            "vpn_bot_rate_limited",
            3,
        ),
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": True}},
            "retry",
            "vpn_bot_rate_limited",
            60,
        ),
        (429, {"ok": False, "error_code": 429}, "retry", "vpn_bot_rate_limited", 60),
        (
            429,
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 86401}},
            "retry",
            "vpn_bot_rate_limited",
            60,
        ),
        (
            503,
            {"ok": True, "result": {"message_id": 7, "chat": {"id": 12345}}},
            "uncertain",
            "vpn_bot_delivery_uncertain",
            None,
        ),
        (
            503,
            {"ok": False, "error_code": 429},
            "uncertain",
            "vpn_bot_delivery_uncertain",
            None,
        ),
        (302, {"ok": True}, "uncertain", "vpn_bot_delivery_uncertain", None),
        (200, "111:synthetic-secret", "uncertain", "vpn_bot_delivery_uncertain", None),
    ],
)
async def test_exact_one_shot_results_without_secret_or_response_echo(
    monkeypatch, caplog, status, data, state, code, retry
):
    requests = []

    async def respond(request):
        requests.append(request)
        return (
            httpx.Response(status, json=data)
            if not isinstance(data, str)
            else httpx.Response(status, text=data)
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(respond), follow_redirects=False
    )
    monkeypatch.setattr(delivery, "_telegram_client", lambda: client)
    result = await delivery.post_telegram_payload(
        "111:synthetic-secret", {"chat_id": "12345", "text": "private-news-body"}
    )
    assert (
        result.state == state
        and result.error_code == code
        and result.retry_after == retry
    )
    assert result.message_id == (7 if state == "sent" else None)
    assert len(requests) == 1
    assert "synthetic-secret" not in repr(result) + caplog.text
    assert "private-news-body" not in repr(result) + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,state",
    [
        (httpx.ConnectTimeout, "retry"),
        (httpx.ConnectError, "retry"),
        (httpx.PoolTimeout, "retry"),
        (httpx.ReadTimeout, "uncertain"),
        (httpx.ReadError, "uncertain"),
        (httpx.WriteTimeout, "uncertain"),
        (httpx.WriteError, "uncertain"),
        (httpx.RemoteProtocolError, "uncertain"),
    ],
)
async def test_transport_outcomes_are_not_blindly_retried(monkeypatch, error, state):
    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        raise error("synthetic-secret-and-body", request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(delivery, "_telegram_client", lambda: client)
    result = await delivery.post_telegram_payload(
        "111:synthetic-secret", {"chat_id": "12345", "text": "private-news-body"}
    )
    assert result.state == state and attempts == 1
    assert "synthetic-secret" not in repr(result)


@pytest.mark.asyncio
async def test_cancel_during_http_propagates_without_retry(monkeypatch):
    async def respond(_request):
        raise asyncio.CancelledError

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(delivery, "_telegram_client", lambda: client)
    with pytest.raises(asyncio.CancelledError):
        await delivery.post_telegram_payload(
            "111:synthetic-secret", {"chat_id": "12345", "text": "private-news-body"}
        )


@pytest.mark.asyncio
async def test_gate_has_global_and_per_chat_slots_and_independent_tokens(
    session_factory,
):
    assert (
        await delivery.reserve_bot_send_slot(
            session_factory, "111:synthetic", "12345", now=NOW
        )
        == 0
    )
    assert await delivery.reserve_bot_send_slot(
        session_factory, "111:synthetic", "67890", now=NOW
    ) == pytest.approx(0.05)
    assert (
        await delivery.reserve_bot_send_slot(
            session_factory, "111:synthetic", "12345", now=NOW
        )
        == 1
    )
    assert (
        await delivery.reserve_bot_send_slot(
            session_factory, "222:different", "12345", now=NOW
        )
        == 0
    )
    assert (
        await delivery.reserve_bot_send_slot(
            session_factory,
            "111:synthetic",
            "67890",
            now=NOW + timedelta(milliseconds=50),
        )
        == 0
    )
    await delivery.extend_bot_cooldown(session_factory, "111:synthetic", 30, now=NOW)
    assert (
        await delivery.reserve_bot_send_slot(
            session_factory, "111:synthetic", "12345", now=NOW + timedelta(seconds=1)
        )
        == 29
    )


@pytest.mark.asyncio
async def test_each_command_chunk_and_inline_keyboard_uses_shared_gate(monkeypatch):
    from app.core.config import Settings
    from app.services import vpn_telegram

    calls = []

    async def gated(_settings, payload):
        calls.append(payload)
        return delivery.TelegramDeliveryResult("sent", message_id=7)

    monkeypatch.setattr(vpn_telegram, "send_gated_payload", gated)
    config = Settings(
        _env_file=None,
        VPN_TELEGRAM_BOT_TOKEN="111:synthetic",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345",
    )
    await vpn_telegram.send_telegram_message(
        config, "12345", "x" * 8500, cabinet_allowed=True
    )
    assert len(calls) == 4
    assert "keyboard" in calls[0]["reply_markup"]
    assert "inline_keyboard" in calls[-1]["reply_markup"]


@pytest.mark.asyncio
async def test_notifier_gates_only_exact_same_vpn_token(monkeypatch):
    from app.core.config import Settings
    from app.services import notifier

    gated_calls, http_calls = [], []

    async def gated(_settings, payload):
        gated_calls.append(payload)
        return delivery.TelegramDeliveryResult("sent", message_id=7)

    async def respond(request):
        http_calls.append(request)
        return httpx.Response(200, json={"ok": True})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(notifier, "send_gated_payload", gated)
    monkeypatch.setattr(
        notifier.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs),
    )
    config = Settings(_env_file=None, VPN_TELEGRAM_BOT_TOKEN="111:synthetic")
    sender = notifier.TelegramNotifier(config)
    await sender.send_message("same-bot", token="111:synthetic", chat_id="12345")
    await sender.send_message("other-bot", token="222:operations", chat_id="12345")
    assert [item["text"] for item in gated_calls] == ["same-bot"]
    assert len(http_calls) == 1 and "/bot222:operations/" in http_calls[0].url.path


@pytest.mark.asyncio
async def test_delivery_gate_uses_its_own_bounded_database_and_disposes_it(monkeypatch):
    from types import SimpleNamespace

    from app.core.config import Settings
    from app.db import session

    created, disposed = [], []
    factory = object()

    async def dispose():
        disposed.append(True)

    def create(config):
        created.append(config.db_url)
        return SimpleNamespace(
            session_factory=factory, engine=SimpleNamespace(dispose=dispose)
        )

    monkeypatch.setattr(delivery, "_delivery_database", None)
    monkeypatch.setattr(session, "create_vpn_control_database", create)
    config = Settings(_env_file=None, DB_URL="sqlite+aiosqlite:///:memory:")
    assert delivery._delivery_sessions(config) is factory
    assert delivery._delivery_sessions(config) is factory
    assert len(created) == 1
    await delivery.close_bot_delivery_database()
    assert disposed == [True] and delivery._delivery_database is None


@pytest.mark.asyncio
async def test_gated_send_waits_outside_transaction_and_propagates_shared_cooldown(
    monkeypatch,
):
    from app.core.config import Settings

    slots, sleeps, cooldowns = [1, 0], [], []

    async def reserve(*_args, **_kwargs):
        return slots.pop(0)

    async def sleep(delay):
        sleeps.append(delay)

    async def post(*_args):
        return delivery.TelegramDeliveryResult(
            "retry", retry_after=37, error_code="vpn_bot_rate_limited"
        )

    async def cooldown(_factory, _token, delay):
        cooldowns.append(delay)

    monkeypatch.setattr(delivery, "reserve_bot_send_slot", reserve)
    monkeypatch.setattr(delivery.asyncio, "sleep", sleep)
    monkeypatch.setattr(delivery, "post_telegram_payload", post)
    monkeypatch.setattr(delivery, "extend_bot_cooldown", cooldown)
    result = await delivery.send_gated_payload(
        Settings(_env_file=None, VPN_TELEGRAM_BOT_TOKEN="111:synthetic"),
        {"chat_id": "12345", "text": "News"},
        session_factory=object(),
    )
    assert result.state == "retry" and sleeps == [1] and cooldowns == [37]
