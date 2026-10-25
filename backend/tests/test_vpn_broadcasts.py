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
from app.services.vpn_broadcasts import (
    cancel_broadcast,
    confirm_broadcast,
    create_broadcast_draft,
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
