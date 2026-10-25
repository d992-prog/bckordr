from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_vpn_friend_invitations import NOW
from test_vpn_telegram import telegram_app as telegram_app  # noqa: PLC0414
from test_vpn_telegram import telegram_message, webhook_headers

from app.core.config import Settings
from app.db.models import AppSetting, VpnBotChat, VpnTelegramUpdate
from app.services.vpn_bot_chats import (
    REGISTRY_BOT_KEY,
    backfill_private_bot_chats,
    configured_vpn_bot_id,
    registry_state,
)


def settings(token="111:synthetic-only"):
    return Settings(_env_file=None, VPN_TELEGRAM_BOT_TOKEN=token)


@pytest.mark.parametrize(
    "token",
    [
        "",
        "missing-colon",
        "0:secret",
        "01:secret",
        "111:",
        "111:bad/secret",
        "111:bad\nsecret",
    ],
)
def test_malformed_bot_configuration_is_rejected_without_echo(token):
    with pytest.raises(ValueError, match="^vpn_bot_unconfigured$"):
        configured_vpn_bot_id(settings(token))


def test_registry_uses_public_bot_id_not_the_secret():
    assert configured_vpn_bot_id(settings()) == "111"


@pytest.mark.asyncio
async def test_private_webhook_registers_chat_and_only_new_inbound_unblocks_it(
    telegram_app,
):
    telegram_app.settings.vpn_telegram_bot_token = "111:synthetic-only"
    payload = telegram_message(10, 12345, "/start")
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct", headers=webhook_headers(), json=payload
    )
    assert response.status_code == 200
    async with telegram_app.session_factory() as db:
        chat = await db.get(VpnBotChat, ("111", "12345"))
        assert chat is not None and chat.last_update_id == 10
        assert chat.blocked_at is None
        chat.blocked_at = NOW
        await db.commit()
    await telegram_app.client.post(
        "/vpn-telegram/webhook/correct", headers=webhook_headers(), json=payload
    )
    async with telegram_app.session_factory() as db:
        assert (await db.get(VpnBotChat, ("111", "12345"))).blocked_at is not None
    # Telegram may restart update numbering after a long inactivity; a new ID need not be larger.
    await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(9, 12345, "/support"),
    )
    async with telegram_app.session_factory() as db:
        chat = await db.get(VpnBotChat, ("111", "12345"))
        assert chat.blocked_at is None and chat.last_update_id == 9
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 1


@pytest.mark.asyncio
async def test_forged_group_and_malformed_messages_never_register_private_recipients(
    telegram_app,
):
    telegram_app.settings.vpn_telegram_bot_token = "111:synthetic-only"
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers("wrong"),
        json=telegram_message(20, 12345, "/start"),
    )
    assert response.status_code == 403
    group = telegram_message(21, 12345, "/start")
    group["message"]["chat"] = {"id": -100123, "type": "group"}
    mismatch = telegram_message(22, 12345, "/start")
    mismatch["message"]["chat"]["id"] = 67890
    for payload in (group, mismatch, {"update_id": 23}):
        await telegram_app.client.post(
            "/vpn-telegram/webhook/correct", headers=webhook_headers(), json=payload
        )
    async with telegram_app.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 0


@pytest.mark.asyncio
async def test_rejected_invitation_still_registers_a_real_private_chat(telegram_app):
    telegram_app.settings.vpn_telegram_bot_token = "111:synthetic-only"
    response = await telegram_app.client.post(
        "/vpn-telegram/webhook/correct",
        headers=webhook_headers(),
        json=telegram_message(24, 12345, "/start invalid-invite"),
    )
    assert response.status_code == 200
    async with telegram_app.session_factory() as db:
        assert await db.get(VpnBotChat, ("111", "12345")) is not None


@pytest.mark.asyncio
async def test_historical_backfill_validates_private_payload_and_keeps_blocked_state(
    telegram_app,
):
    async with telegram_app.session_factory() as db:
        valid = telegram_message(30, 12345, "<redacted-message>")
        group = telegram_message(31, 99999, "/start")
        group["message"]["chat"]["type"] = "group"
        mismatch = telegram_message(32, 99999, "/start")
        mismatch["message"]["chat"]["id"] = 88888
        wrong_update_id = telegram_message(33, 77777, "/start")
        boolean_sender = telegram_message(34, 77777, "/start")
        boolean_sender["message"]["from"]["id"] = True
        for identity, payload in (
            (30, valid),
            (31, group),
            (32, mismatch),
            (99, wrong_update_id),
            (34, boolean_sender),
        ):
            db.add(
                VpnTelegramUpdate(
                    update_id=str(identity), payload=payload, created_at=NOW
                )
            )
        await db.commit()
        assert (await backfill_private_bot_chats(db, "111"))[0] == 1
        await db.commit()
        chat = await db.get(VpnBotChat, ("111", "12345"))
        assert chat.first_seen_at.replace(tzinfo=NOW.tzinfo) == NOW
        chat.blocked_at = NOW + timedelta(minutes=1)
        await db.commit()
        assert (await backfill_private_bot_chats(db, "111"))[0] == 1
        await db.commit()
        assert (
            await db.get(VpnBotChat, ("111", "12345"), populate_existing=True)
        ).blocked_at is not None
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "confirmed,remote", [("222", "111"), ("111", "222"), ("01", "111")]
)
async def test_registry_cli_rejects_identity_mismatch_before_any_writes(
    telegram_app,
    confirmed,
    remote,
):
    from app.cli.vpn_bot_chats import restore_registry

    async def identity(_settings):
        return remote

    with pytest.raises(ValueError, match="^vpn_bot_registry_identity_mismatch$"):
        await restore_registry(
            telegram_app.session_factory,
            settings(),
            confirmed,
            identity_reader=identity,
        )
    async with telegram_app.session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 0


@pytest.mark.asyncio
async def test_registry_cli_pages_history_and_does_not_reassign_a_different_bot(
    telegram_app,
):
    from app.cli.vpn_bot_chats import restore_registry

    async def identity(_settings):
        return "111"

    async with telegram_app.session_factory() as db:
        for number in range(1, 502):
            db.add(
                VpnTelegramUpdate(
                    update_id=str(number),
                    payload=telegram_message(number, 12345, "redacted"),
                    created_at=NOW,
                )
            )
        await db.commit()
        assert await registry_state(db, settings()) == "history_unverified"
    assert (
        await restore_registry(
            telegram_app.session_factory, settings(), "111", identity_reader=identity
        )
        == 501
    )
    async with telegram_app.session_factory() as db:
        assert await registry_state(db, settings()) == "ready"
        assert await registry_state(db, settings("222:other-token")) == "bot_changed"
        assert await registry_state(db, settings("bad")) == "unconfigured"
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 1
        marker = await db.scalar(
            select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
        )
        assert marker.value == "111"
    assert (
        await restore_registry(
            telegram_app.session_factory, settings(), "111", identity_reader=identity
        )
        == 501
    )

    async def other_identity(_settings):
        return "222"

    with pytest.raises(ValueError, match="^vpn_bot_registry_bot_changed$"):
        await restore_registry(
            telegram_app.session_factory,
            settings("222:other-token"),
            "222",
            identity_reader=other_identity,
        )
    async with telegram_app.session_factory() as db:
        assert (
            await db.scalar(
                select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
            )
        ).value == "111"
        assert await db.scalar(select(func.count()).select_from(VpnBotChat)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ok": False},
        {"ok": True, "result": {"id": True, "is_bot": True}},
        {"ok": True, "result": {"id": 111, "is_bot": False}},
        "not-json",
    ],
)
async def test_cli_getme_invalid_response_never_exposes_token_or_response(
    monkeypatch, payload
):
    import httpx

    from app.cli import vpn_bot_chats as cli

    async def respond(request):
        assert request.method == "GET" and request.url.path.endswith("/getMe")
        if payload == "not-json":
            return httpx.Response(200, text="111:synthetic-only")
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cli, "_identity_client", lambda: client)
    with pytest.raises(ValueError, match="^vpn_bot_registry_identity_unavailable$"):
        await cli.get_telegram_bot_id(settings())


@pytest.mark.asyncio
async def test_cli_getme_success_and_help_are_readonly(monkeypatch):
    import httpx

    from app.cli import vpn_bot_chats as cli

    requests = []

    async def respond(request):
        requests.append(request)
        return httpx.Response(
            200, json={"ok": True, "result": {"id": 111, "is_bot": True}}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    monkeypatch.setattr(cli, "_identity_client", lambda: client)
    assert await cli.get_telegram_bot_id(settings()) == "111"
    assert len(requests) == 1
    help_text = " ".join(cli.parser().format_help().split())
    assert "all historical VPN webhook updates belong to this bot" in help_text
    assert "does not send messages" in help_text


@pytest.mark.asyncio
async def test_interrupted_cli_keeps_gate_closed_and_can_resume(
    telegram_app, monkeypatch
):
    from app.cli import vpn_bot_chats as cli

    async def identity(_settings):
        return "111"

    calls = 0

    async def interrupted_page(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return 1, 500
        raise RuntimeError("synthetic interruption")

    real_page = cli.backfill_private_bot_chats
    monkeypatch.setattr(cli, "backfill_private_bot_chats", interrupted_page)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        await cli.restore_registry(
            telegram_app.session_factory, settings(), "111", identity_reader=identity
        )
    async with telegram_app.session_factory() as db:
        assert await registry_state(db, settings()) == "history_unverified"
        assert (
            await db.scalar(
                select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
            )
        ).value == "backfilling:111"
    monkeypatch.setattr(cli, "backfill_private_bot_chats", real_page)
    assert (
        await cli.restore_registry(
            telegram_app.session_factory, settings(), "111", identity_reader=identity
        )
        == 0
    )
    async with telegram_app.session_factory() as db:
        assert await registry_state(db, settings()) == "ready"
