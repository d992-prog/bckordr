import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_vpn_bot_chats import settings
from test_vpn_friend_invitations import NOW
from test_vpn_friend_invitations import (
    session_factory as session_factory,  # noqa: PLC0414
)

from app.db.models import (
    AppSetting,
    VpnBotChat,
    VpnBroadcastCampaign,
    VpnBroadcastDelivery,
)
from app.services.vpn_bot_chats import REGISTRY_BOT_KEY
from app.services.vpn_bot_delivery import TelegramDeliveryResult
from app.services.vpn_broadcasts import (
    cancel_broadcast,
    confirm_broadcast,
    create_broadcast_draft,
    dispatch_next_broadcast_delivery,
    preview_broadcast,
    update_broadcast_draft,
)


async def seed(db):
    db.add(AppSetting(key=REGISTRY_BOT_KEY, value="111"))
    for bot_id, chat_id, blocked in (
        ("111", "12345", False),
        ("111", "67890", True),
        ("222", "99999", False),
    ):
        db.add(
            VpnBotChat(
                bot_id=bot_id,
                chat_id=chat_id,
                first_seen_at=NOW,
                last_seen_at=NOW,
                blocked_at=NOW if blocked else None,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_draft_preview_edit_confirm_replay_freezes_only_eligible_private_chats(
    session_factory,
):
    async with session_factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(
            db, settings(), "<b>Новости</b>\r\nНовая страна", actor_id=None, now=NOW
        )
        await db.commit()
        assert draft.body == "<b>Новости</b>\nНовая страна"
        assert draft.state == "draft"
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 0
        )
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=1)
        )
        assert draft.preview_recipient_count == 1
        revision = draft.revision
        await update_broadcast_draft(
            db, draft.id, "Исправленная новость", actor_id=None
        )
        assert draft.revision == revision + 1 and draft.preview_revision is None
        with pytest.raises(ValueError, match="^vpn_broadcast_preview_changed$"):
            await confirm_broadcast(
                db,
                settings(),
                draft.id,
                expected_revision=revision,
                expected_recipient_count=1,
                actor_id=None,
            )
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=2)
        )
        db.add(
            VpnBotChat(
                bot_id="111",
                chat_id="54321",
                first_seen_at=NOW + timedelta(seconds=3),
                last_seen_at=NOW + timedelta(seconds=3),
            )
        )
        await db.flush()
        result = await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=draft.revision,
            expected_recipient_count=1,
            actor_id=None,
        )
        await db.commit()
        assert result.state == "running"
        assert (await db.scalars(select(VpnBroadcastDelivery.chat_id))).all() == [
            "12345"
        ]
        again = await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=draft.revision,
            expected_recipient_count=1,
            actor_id=None,
        )
        assert again.id == result.id
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 1
        )
        with pytest.raises(ValueError, match="^vpn_broadcast_not_draft$"):
            await update_broadcast_draft(
                db, draft.id, "Нельзя менять отправленную новость", actor_id=None
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["", " \n\t", "x" * 4097, "bad\ud800", 42])
async def test_invalid_news_creates_no_draft(session_factory, body):
    async with session_factory() as db:
        with pytest.raises(ValueError, match="^vpn_broadcast_invalid_body$"):
            await create_broadcast_draft(db, settings(), body, actor_id=None)
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastCampaign)) == 0
        )


@pytest.mark.asyncio
async def test_blocked_recipient_change_requires_new_preview_without_creating_jobs(
    session_factory,
):
    async with session_factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(db, settings(), "Новость", actor_id=None)
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=1)
        )
        (await db.get(VpnBotChat, ("111", "12345"))).blocked_at = NOW
        await db.flush()
        with pytest.raises(ValueError, match="^vpn_broadcast_audience_changed$"):
            await confirm_broadcast(
                db,
                settings(),
                draft.id,
                expected_revision=1,
                expected_recipient_count=1,
                actor_id=None,
            )
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 0
        )
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=2)
        )
        await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=1,
            expected_recipient_count=0,
            actor_id=None,
        )
        assert draft.state == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [None, "222", "backfilling:111"])
async def test_unverified_or_changed_registry_blocks_preview_and_confirmation(
    session_factory, marker
):
    async with session_factory() as db:
        if marker is not None:
            db.add(AppSetting(key=REGISTRY_BOT_KEY, value=marker))
            await db.flush()
        draft = await create_broadcast_draft(db, settings(), "Новость", actor_id=None)
        with pytest.raises(ValueError, match="^vpn_broadcast_registry_unavailable$"):
            await preview_broadcast(db, settings(), draft.id)
        with pytest.raises(ValueError, match="^vpn_broadcast_registry_unavailable$"):
            await confirm_broadcast(
                db,
                settings(),
                draft.id,
                expected_revision=1,
                expected_recipient_count=0,
                actor_id=None,
            )
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 0
        )


@pytest.mark.asyncio
async def test_cancel_leaves_inflight_and_terminal_delivery_outcomes_intact(
    session_factory,
):
    async with session_factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(db, settings(), "Новость", actor_id=None)
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=1)
        )
        await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=1,
            expected_recipient_count=1,
            actor_id=None,
        )
        delivery = await db.scalar(select(VpnBroadcastDelivery))
        delivery.state, delivery.sending_at = "sending", NOW
        await db.flush()
        await cancel_broadcast(db, draft.id, actor_id=None)
        assert draft.state == "cancelled" and delivery.state == "sending"
        await cancel_broadcast(db, draft.id, actor_id=None)
        assert (
            await db.scalar(select(func.count()).select_from(VpnBroadcastDelivery)) == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["pending", "claimed"])
async def test_cancel_stops_unsent_jobs(session_factory, state):
    async with session_factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(db, settings(), "Новость", actor_id=None)
        await preview_broadcast(
            db, settings(), draft.id, now=NOW + timedelta(seconds=1)
        )
        await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=1,
            expected_recipient_count=1,
            actor_id=None,
        )
        delivery = await db.scalar(select(VpnBroadcastDelivery))
        delivery.state = state
        await db.flush()
        await cancel_broadcast(db, draft.id, actor_id=None)
        await db.refresh(delivery)
        assert delivery.state == "cancelled"


async def campaign(session_factory):
    async with session_factory() as db:
        await seed(db)
        draft = await create_broadcast_draft(
            db, settings(), "Не отправлять: вымышленная новость", actor_id=None, now=NOW
        )
        await preview_broadcast(db, settings(), draft.id, now=NOW)
        await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=1,
            expected_recipient_count=1,
            actor_id=None,
            now=NOW,
        )
        await db.commit()
        return draft.id


@pytest.fixture
def immediate_gate(monkeypatch):
    async def reserve(*_args, **_kwargs):
        return 0

    monkeypatch.setattr("app.services.vpn_broadcasts.reserve_bot_send_slot", reserve)


@pytest.mark.asyncio
async def test_dispatch_marks_sending_before_http_and_finalizes_exact_success(
    session_factory, immediate_gate
):
    campaign_id = await campaign(session_factory)
    calls = []

    async def sender(token, payload):
        calls.append(payload)
        assert token == "111:synthetic-only"
        assert set(payload) == {"chat_id", "text", "link_preview_options"}
        assert payload["link_preview_options"] == {"is_disabled": True}
        async with session_factory() as db:
            job = await db.scalar(select(VpnBroadcastDelivery))
            assert job.state == "sending" and job.attempt_count == 1
        return TelegramDeliveryResult("sent", message_id=71)

    assert await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    assert not await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        job = await db.scalar(select(VpnBroadcastDelivery))
        assert job.state == "sent" and job.telegram_message_id == 71
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_uncertain_delivery_is_terminal_not_blindly_resent(
    session_factory, immediate_gate
):
    campaign_id = await campaign(session_factory)
    calls = []

    async def sender(*_args):
        calls.append(True)
        return TelegramDeliveryResult(
            "uncertain", error_code="vpn_bot_delivery_uncertain"
        )

    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(minutes=5),
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "uncertain"
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "completed"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_inflight_preserves_success_without_reopening_campaign(
    session_factory, immediate_gate
):
    campaign_id = await campaign(session_factory)

    async def sender(*_args):
        async with session_factory() as db:
            await cancel_broadcast(db, campaign_id, actor_id=None, now=NOW)
            await db.commit()
        return TelegramDeliveryResult("sent", message_id=71)

    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "sent"
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "cancelled"


@pytest.mark.asyncio
async def test_process_death_after_sending_marker_recovers_uncertain(
    session_factory, immediate_gate
):
    await campaign(session_factory)
    calls = []

    async def sender(*_args):
        calls.append(True)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_broadcast_delivery(
            session_factory, settings(), sender=sender, now=lambda: NOW
        )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "sending"
    await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(seconds=31),
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "uncertain"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_cancel_while_waiting_for_rate_gate_never_sends(
    session_factory, monkeypatch
):
    campaign_id = await campaign(session_factory)

    async def reserve(*_args, **_kwargs):
        async with session_factory() as db:
            await cancel_broadcast(db, campaign_id, actor_id=None, now=NOW)
            await db.commit()
        return 1

    async def sender(*_args):
        pytest.fail("cancelled queued delivery must not perform HTTP")

    monkeypatch.setattr("app.services.vpn_broadcasts.reserve_bot_send_slot", reserve)
    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "cancelled"


@pytest.mark.asyncio
async def test_death_before_sending_marker_is_safe_to_retry_after_lease(
    session_factory, monkeypatch
):
    await campaign(session_factory)
    calls = []

    async def interrupted_gate(*_args, **_kwargs):
        raise asyncio.CancelledError

    async def immediate(*_args, **_kwargs):
        return 0

    async def sender(*_args):
        calls.append(True)
        return TelegramDeliveryResult("sent", message_id=71)

    monkeypatch.setattr(
        "app.services.vpn_broadcasts.reserve_bot_send_slot", interrupted_gate
    )
    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_broadcast_delivery(
            session_factory, settings(), sender=sender, now=lambda: NOW
        )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "claimed"
    monkeypatch.setattr("app.services.vpn_broadcasts.reserve_bot_send_slot", immediate)
    await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(seconds=31),
    )
    assert calls == [True]
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "sent"


@pytest.mark.asyncio
async def test_429_uses_durable_backoff_and_can_retry_known_rejection(
    session_factory, immediate_gate
):
    await campaign(session_factory)
    calls = []

    async def sender(*_args):
        calls.append(True)
        return (
            TelegramDeliveryResult(
                "retry", retry_after=37, error_code="vpn_bot_rate_limited"
            )
            if len(calls) == 1
            else TelegramDeliveryResult("sent", message_id=71)
        )

    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    assert not await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(seconds=5),
    )
    assert calls == [True]
    await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(seconds=38),
    )
    async with session_factory() as db:
        job = await db.scalar(select(VpnBroadcastDelivery))
        assert job.state == "sent" and job.attempt_count == 2


@pytest.mark.asyncio
async def test_403_marks_private_chat_blocked_and_does_not_retry(
    session_factory, immediate_gate
):
    await campaign(session_factory)

    async def sender(*_args):
        return TelegramDeliveryResult("blocked", error_code="vpn_bot_blocked")

    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "skipped"
        assert (await db.get(VpnBotChat, ("111", "12345"))).blocked_at is not None


@pytest.mark.asyncio
async def test_recipient_blocked_after_confirmation_is_skipped_without_http(
    session_factory, immediate_gate
):
    await campaign(session_factory)
    async with session_factory() as db:
        (await db.get(VpnBotChat, ("111", "12345"))).blocked_at = NOW
        await db.commit()

    async def sender(*_args):
        pytest.fail("blocked private chat must not receive HTTP")

    assert await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "skipped"


@pytest.mark.asyncio
async def test_auth_rejection_stops_unattempted_recipients(
    session_factory, immediate_gate
):
    async with session_factory() as db:
        await seed(db)
        (await db.get(VpnBotChat, ("111", "67890"))).blocked_at = None
        draft = await create_broadcast_draft(
            db, settings(), "Вымышленная новость", actor_id=None, now=NOW
        )
        await preview_broadcast(db, settings(), draft.id, now=NOW)
        await confirm_broadcast(
            db,
            settings(),
            draft.id,
            expected_revision=1,
            expected_recipient_count=2,
            actor_id=None,
            now=NOW,
        )
        await db.commit()
        campaign_id = draft.id

    async def sender(*_args):
        return TelegramDeliveryResult("failed", error_code="vpn_bot_auth_failed")

    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    assert not await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.get(VpnBroadcastCampaign, campaign_id)).state == "failed"
        assert sorted((await db.scalars(select(VpnBroadcastDelivery.state))).all()) == [
            "cancelled",
            "failed",
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_first", [False, True])
async def test_uncertain_final_commit_does_not_send_again(
    session_factory, immediate_gate, monkeypatch, commit_first
):
    from app.services import vpn_broadcasts

    await campaign(session_factory)
    calls = []
    real_finalize = vpn_broadcasts._finalize_delivery

    async def sender(*_args):
        calls.append(True)
        return TelegramDeliveryResult("sent", message_id=71)

    async def broken_finalize(*args):
        if commit_first:
            await real_finalize(*args)
        raise RuntimeError("synthetic uncertain commit")

    monkeypatch.setattr(vpn_broadcasts, "_finalize_delivery", broken_finalize)
    with pytest.raises(RuntimeError, match="synthetic uncertain commit"):
        await dispatch_next_broadcast_delivery(
            session_factory, settings(), sender=sender, now=lambda: NOW
        )
    monkeypatch.setattr(vpn_broadcasts, "_finalize_delivery", real_finalize)
    await dispatch_next_broadcast_delivery(
        session_factory,
        settings(),
        sender=sender,
        now=lambda: NOW + timedelta(seconds=31),
    )
    assert calls == [True]
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == (
            "sent" if commit_first else "uncertain"
        )


@pytest.mark.asyncio
async def test_cooldown_arriving_during_marker_commit_prevents_http(
    session_factory, immediate_gate, monkeypatch
):
    from app.services import vpn_broadcasts

    await campaign(session_factory)
    real_mark = vpn_broadcasts._mark_sending

    async def mark_and_cooldown(*args):
        result = await real_mark(*args)
        await vpn_broadcasts.extend_bot_cooldown(
            session_factory, settings().vpn_telegram_bot_token, 37, now=NOW
        )
        return result

    async def sender(*_args):
        pytest.fail("fresh shared cooldown must be checked before HTTP")

    monkeypatch.setattr(vpn_broadcasts, "_mark_sending", mark_and_cooldown)
    await dispatch_next_broadcast_delivery(
        session_factory, settings(), sender=sender, now=lambda: NOW
    )
    async with session_factory() as db:
        assert (await db.scalar(select(VpnBroadcastDelivery))).state == "pending"
