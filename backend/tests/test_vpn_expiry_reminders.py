"""Reminder tests never invoke Telegram: every dispatch has a synthetic sender."""

import asyncio
import importlib
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from test_vpn_friend_invitations import NOW
from test_vpn_friend_invitations import (
    session_factory as session_factory,  # noqa: PLC0414
)

from app.core.config import Settings
from app.db import models
from app.db.models import AppSetting, VpnBotChat, VpnCustomer, VpnSubscription
from app.schemas.vpn_cabinet import CabinetSettings
from app.services.vpn_bot_chats import REGISTRY_BOT_KEY
from app.services.vpn_bot_delivery import TelegramDeliveryResult
from app.services.vpn_cabinet_settings import (
    get_cabinet_settings,
    update_cabinet_settings,
)


def service():
    assert importlib.util.find_spec("app.services.vpn_expiry_reminders") is not None
    return importlib.import_module("app.services.vpn_expiry_reminders")


def settings():
    return Settings(
        _env_file=None,
        VPN_TELEGRAM_BOT_TOKEN="111:synthetic-only",
        VPN_PORTAL_ENABLED=True,
        VPN_PORTAL_PUBLIC_ORIGIN="https://portal.example",
        VPN_PORTAL_ALLOWED_TELEGRAM_IDS="12345,67890",
    )


async def gate(db, enabled, at=NOW - timedelta(days=4)):
    current = await get_cabinet_settings(db)
    await update_cabinet_settings(
        db,
        CabinetSettings(**{**current.model_dump(), "reminders_enabled": enabled}),
        now=at,
    )
    await db.commit()


async def seed(factory, *, enabled=True, expires_at=NOW + timedelta(days=2)):
    async with factory() as db:
        db.add(AppSetting(key=REGISTRY_BOT_KEY, value="111"))
        customer = VpnCustomer(telegram_user_id="12345", status="active")
        db.add(customer)
        await db.flush()
        subscription = VpnSubscription(
            customer_id=customer.id, status="active", expires_at=expires_at
        )
        db.add(subscription)
        db.add(
            VpnBotChat(
                bot_id="111", chat_id="12345", first_seen_at=NOW, last_seen_at=NOW
            )
        )
        await db.commit()
        if enabled:
            await gate(db, True)
        return customer.id, subscription.id


@pytest.fixture
def immediate_gate(monkeypatch):
    async def reserve(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(service(), "reserve_bot_send_slot", reserve)


async def dispatch(factory, calls, *, at=NOW, result=None):
    async def sender(_token, payload):
        calls.append(payload)
        async with factory() as db:
            job = await db.scalar(
                select(models.VpnExpiryReminder).where(
                    models.VpnExpiryReminder.state == "sending"
                )
            )
            assert job is not None and job.attempt_count >= 1
        return result or TelegramDeliveryResult("sent", message_id=71)

    return await service().dispatch_next_expiry_reminder(
        factory, settings(), sender=sender, now=lambda: at
    )


@pytest.mark.asyncio
async def test_preference_defaults_true_but_operator_default_does_not_queue(
    session_factory,
):
    customer_id, _ = await seed(session_factory, enabled=False)
    async with session_factory() as db:
        assert (
            getattr(await db.get(VpnCustomer, customer_id), "expiry_reminders", None)
            is True
        )
    calls = []
    assert await dispatch(session_factory, calls) is False
    assert calls == []
    async with session_factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(models.VpnExpiryReminder))
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "preference",
        "archived",
        "suspended",
        "cancelled",
        "no_expiry",
        "blocked",
        "browser_only",
        "registry",
        "bot_change",
    ],
)
async def test_ineligible_customers_never_send(session_factory, immediate_gate, change):
    customer_id, subscription_id = await seed(session_factory)
    async with session_factory() as db:
        customer, subscription = (
            await db.get(VpnCustomer, customer_id),
            await db.get(VpnSubscription, subscription_id),
        )
        if change == "preference":
            customer.expiry_reminders = False
        elif change == "archived":
            customer.status = "archived"
        elif change in ("suspended", "cancelled"):
            subscription.status = change
        elif change == "no_expiry":
            subscription.expires_at = None
        elif change == "blocked":
            (await db.get(VpnBotChat, ("111", "12345"))).blocked_at = NOW
        elif change == "browser_only":
            await db.delete(await db.get(VpnBotChat, ("111", "12345")))
        else:
            (
                await db.scalar(
                    select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
                )
            ).value = "222" if change == "bot_change" else None
        await db.commit()
    calls = []
    await dispatch(session_factory, calls)
    assert calls == []


@pytest.mark.asyncio
async def test_each_expiry_event_is_sent_once_across_new_dispatchers(
    session_factory, immediate_gate
):
    await seed(session_factory)
    calls = []
    for at in (
        NOW,
        NOW,
        NOW + timedelta(days=1, hours=1),
        NOW + timedelta(days=1, hours=1),
        NOW + timedelta(days=2),
        NOW + timedelta(days=3),
    ):
        await dispatch(session_factory, calls, at=at)
    assert len(calls) == 3
    assert all(
        "vless://" not in item["text"] and "оплат" not in item["text"].lower()
        for item in calls
    )
    assert "24" in calls[0]["text"] and "сентября" in calls[0]["text"]
    assert (
        calls[0]["reply_markup"]["inline_keyboard"][0][0]["web_app"]["url"]
        == "https://portal.example/cabinet/"
    )
    async with session_factory() as db:
        jobs = (await db.scalars(select(models.VpnExpiryReminder))).all()
        assert {job.event_kind for job in jobs} == {"3d", "1d", "expired"}
        assert all(job.state == "sent" and job.attempt_count == 1 for job in jobs)


@pytest.mark.asyncio
@pytest.mark.parametrize("hours,expected", [(12, "1d"), (-12, "expired")])
async def test_downtime_sends_only_most_relevant_window(
    session_factory, immediate_gate, hours, expected
):
    await seed(session_factory, expires_at=NOW + timedelta(hours=hours))
    calls = []
    await dispatch(session_factory, calls)
    await dispatch(session_factory, calls)
    assert len(calls) == 1
    async with session_factory() as db:
        assert (
            await db.scalar(select(models.VpnExpiryReminder))
        ).event_kind == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [-30, -1, 0.5, 2])
async def test_newly_enabled_gate_never_replays_historical_due_events(
    session_factory, immediate_gate, days
):
    await seed(session_factory, enabled=False, expires_at=NOW + timedelta(days=days))
    async with session_factory() as db:
        await gate(db, True, NOW)
    calls = []
    await dispatch(session_factory, calls)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "renewal",
        "preference",
        "archived",
        "suspended",
        "blocked",
        "gate",
        "reactivation",
        "bot",
        "chat",
    ],
)
async def test_claim_revalidates_current_policy_before_http(
    session_factory, immediate_gate, monkeypatch, change
):
    customer_id, subscription_id = await seed(session_factory)

    async def reserve(*_args, **_kwargs):
        async with session_factory() as db:
            if change == "renewal":
                (await db.get(VpnSubscription, subscription_id)).expires_at = (
                    NOW + timedelta(days=30)
                )
            elif change == "preference":
                (await db.get(VpnCustomer, customer_id)).expiry_reminders = False
            elif change == "archived":
                (await db.get(VpnCustomer, customer_id)).status = "archived"
            elif change == "suspended":
                (await db.get(VpnSubscription, subscription_id)).status = "suspended"
            elif change == "blocked":
                (await db.get(VpnBotChat, ("111", "12345"))).blocked_at = NOW
            elif change == "gate":
                await gate(db, False, NOW)
            elif change == "reactivation":
                await gate(db, False, NOW)
                await gate(db, True, NOW)
            elif change == "bot":
                (
                    await db.scalar(
                        select(AppSetting).where(AppSetting.key == REGISTRY_BOT_KEY)
                    )
                ).value = "222"
            elif change == "chat":
                (await db.get(VpnCustomer, customer_id)).telegram_user_id = "67890"
            await db.commit()
        return 0

    monkeypatch.setattr(service(), "reserve_bot_send_slot", reserve)
    calls = []
    await dispatch(session_factory, calls)
    assert calls == []
    async with session_factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "skipped"


@pytest.mark.asyncio
async def test_429_persists_retry_and_shared_bot_cooldown(
    session_factory, immediate_gate
):
    await seed(session_factory)
    calls = []
    await dispatch(
        session_factory,
        calls,
        result=TelegramDeliveryResult(
            "retry", retry_after=60, error_code="vpn_bot_rate_limited"
        ),
    )
    assert len(calls) == 1
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=59))
    assert len(calls) == 1
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=61))
    assert len(calls) == 2
    async with session_factory() as db:
        job = await db.scalar(select(models.VpnExpiryReminder))
        assert job.state == "sent" and job.attempt_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["uncertain", "failed", "blocked"])
async def test_terminal_outcomes_are_not_blindly_retried(
    session_factory, immediate_gate, state
):
    await seed(session_factory)
    calls = []
    await dispatch(
        session_factory,
        calls,
        result=TelegramDeliveryResult(
            state,
            error_code="vpn_bot_blocked"
            if state == "blocked"
            else "vpn_bot_delivery_uncertain",
        ),
    )
    await dispatch(session_factory, calls, at=NOW + timedelta(minutes=1))
    assert len(calls) == 1
    async with session_factory() as db:
        job = await db.scalar(select(models.VpnExpiryReminder))
        assert job.state == ("skipped" if state == "blocked" else state)
        if state == "blocked":
            assert (await db.get(VpnBotChat, ("111", "12345"))).blocked_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("started", [False, True])
async def test_restart_recovers_stale_claims_as_uncertain(
    session_factory, immediate_gate, started
):
    await seed(session_factory)
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    assert claim is not None
    if started:
        assert await service()._mark_sending(
            session_factory, settings(), claim, lambda: NOW
        )
    calls = []
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=31))
    assert calls == []
    async with session_factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "uncertain"


@pytest.mark.asyncio
async def test_cancelled_http_is_persistently_uncertain_after_restart(
    session_factory, immediate_gate
):
    await seed(session_factory)

    async def cancelled(*_args):
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await service().dispatch_next_expiry_reminder(
            session_factory, settings(), sender=cancelled, now=lambda: NOW
        )
    calls = []
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=31))
    assert calls == []


@pytest.mark.asyncio
async def test_expired_rate_wait_claim_is_not_put_back_for_delivery(
    session_factory, immediate_gate
):
    await seed(session_factory)
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    await service()._release_rate_wait(
        session_factory, claim, 60, lambda: NOW + timedelta(seconds=31)
    )
    async with session_factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "uncertain"


@pytest.mark.asyncio
async def test_renewal_during_cooldown_check_is_seen_before_http(
    session_factory, immediate_gate, monkeypatch
):
    _, subscription_id = await seed(session_factory)

    async def cooldown(*_args, **_kwargs):
        async with session_factory() as db:
            (await db.get(VpnSubscription, subscription_id)).expires_at = (
                NOW + timedelta(days=30)
            )
            await db.commit()
        return 0

    monkeypatch.setattr(service(), "bot_cooldown_remaining", cooldown)
    calls = []
    await dispatch(session_factory, calls)
    assert calls == []


@pytest.mark.asyncio
async def test_shared_cooldown_started_during_mark_sending_defers_http(
    session_factory, monkeypatch
):
    from app.services.vpn_bot_delivery import extend_bot_cooldown

    await seed(session_factory)
    original = service()._mark_sending
    injected = False

    async def competing_delivery(*args):
        nonlocal injected
        sending = await original(*args)
        if sending and not injected:
            injected = True
            await extend_bot_cooldown(
                session_factory, settings().vpn_telegram_bot_token, 60, now=NOW
            )
        return sending

    monkeypatch.setattr(service(), "_mark_sending", competing_delivery)
    calls = []
    await dispatch(session_factory, calls)
    assert calls == []
    async with session_factory() as db:
        job = await db.scalar(select(models.VpnExpiryReminder))
        assert job.state == "pending"
        assert service()._as_utc(job.available_at) >= NOW + timedelta(seconds=61)
        assert job.error_code == "vpn_bot_rate_limited"
        assert job.claim_token is job.sending_at is job.lease_until is None
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=60))
    assert calls == []
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=62))
    assert len(calls) == 1
    async with session_factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "sent"


@pytest.mark.asyncio
async def test_old_queued_window_is_skipped_and_expired_subscription_still_gets_current_event(
    session_factory, immediate_gate
):
    _, subscription_id = await seed(session_factory)
    # A rate wait leaves old work pending before the runtime is stopped.
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    await service()._release_rate_wait(session_factory, claim, 1, lambda: NOW)
    async with session_factory() as db:
        (await db.get(VpnSubscription, subscription_id)).status = "expired"
        await db.commit()
    calls = []
    await dispatch(session_factory, calls, at=NOW + timedelta(days=3))
    assert len(calls) == 1 and "истек" in calls[0]["text"]
    async with session_factory() as db:
        jobs = (
            await db.scalars(
                select(models.VpnExpiryReminder).order_by(models.VpnExpiryReminder.id)
            )
        ).all()
        assert [(job.event_kind, job.state) for job in jobs] == [
            ("3d", "skipped"),
            ("expired", "sent"),
        ]


@pytest.mark.asyncio
async def test_renewed_expiry_creates_new_dedupe_scope(session_factory, immediate_gate):
    _, subscription_id = await seed(session_factory)
    calls = []
    await dispatch(session_factory, calls)
    async with session_factory() as db:
        (await db.get(VpnSubscription, subscription_id)).expires_at = NOW + timedelta(
            days=4
        )
        await db.commit()
    await dispatch(session_factory, calls, at=NOW + timedelta(days=1))
    assert len(calls) == 2
    async with session_factory() as db:
        jobs = (await db.scalars(select(models.VpnExpiryReminder))).all()
        assert len(jobs) == 2 and all(
            job.event_kind == "3d" and job.state == "sent" for job in jobs
        )


@pytest.mark.asyncio
async def test_skipped_recipient_does_not_starve_following_customer(
    session_factory, immediate_gate
):
    customer_id, _ = await seed(session_factory)
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    await service()._release_rate_wait(session_factory, claim, 1, lambda: NOW)
    async with session_factory() as db:
        (await db.get(VpnCustomer, customer_id)).expiry_reminders = False
        second = VpnCustomer(telegram_user_id="67890")
        db.add(second)
        await db.flush()
        db.add(
            VpnSubscription(customer_id=second.id, expires_at=NOW + timedelta(days=2))
        )
        db.add(
            VpnBotChat(
                bot_id="111", chat_id="67890", first_seen_at=NOW, last_seen_at=NOW
            )
        )
        await db.commit()
    calls = []
    await dispatch(session_factory, calls, at=NOW + timedelta(seconds=2))
    assert [payload["chat_id"] for payload in calls] == ["67890"]


@pytest.mark.asyncio
async def test_pending_discovery_and_recovery_pages_are_bounded(
    session_factory, immediate_gate
):
    customer_id, _ = await seed(session_factory)
    async with session_factory() as db:
        db.add_all(
            [
                VpnSubscription(
                    customer_id=customer_id, expires_at=NOW + timedelta(days=2)
                )
                for _ in range(24)
            ]
        )
        await db.commit()
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    assert claim is not None
    async with session_factory() as db:
        jobs = (await db.scalars(select(models.VpnExpiryReminder))).all()
        assert len(jobs) == 20
        for job in jobs:
            job.state, job.lease_until = "claimed", NOW
        more_subscriptions = (
            await db.scalars(
                select(VpnSubscription).where(
                    ~select(models.VpnExpiryReminder.id)
                    .where(
                        models.VpnExpiryReminder.subscription_id == VpnSubscription.id
                    )
                    .exists()
                )
            )
        ).all()
        for subscription in more_subscriptions:
            db.add(
                models.VpnExpiryReminder(
                    subscription_id=subscription.id,
                    expires_at=subscription.expires_at,
                    event_kind="3d",
                    enabled_at=NOW - timedelta(days=4),
                    bot_id="111",
                    chat_id="12345",
                    state="claimed",
                    lease_until=NOW,
                )
            )
        await db.commit()
    async with session_factory() as db, db.begin():
        await service()._recover(db, NOW + timedelta(seconds=31))
    async with session_factory() as db:
        assert (
            await db.scalar(
                select(func.count())
                .select_from(models.VpnExpiryReminder)
                .where(models.VpnExpiryReminder.state == "uncertain")
            )
            == 20
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(models.VpnExpiryReminder)
                .where(models.VpnExpiryReminder.state == "claimed")
            )
            == 5
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled,admitted", [(False, True), (True, False)])
async def test_reminders_without_usable_cabinet_entry_are_skipped(
    session_factory, immediate_gate, enabled, admitted
):
    await seed(session_factory)
    configuration = settings()
    configuration.vpn_portal_enabled = enabled
    configuration.vpn_portal_allowed_telegram_ids = "12345" if admitted else ""
    calls = []

    async def sender(*_args):
        calls.append(True)
        return TelegramDeliveryResult("sent", message_id=71)

    await service().dispatch_next_expiry_reminder(
        session_factory, configuration, sender=sender, now=lambda: NOW
    )
    assert calls == []
    async with session_factory() as db:
        assert (await db.scalar(select(models.VpnExpiryReminder))).state == "skipped"


@pytest.mark.asyncio
async def test_claim_locks_chat_before_customer_to_match_inbound_updates(
    session_factory, immediate_gate, monkeypatch
):
    await seed(session_factory)
    _, claim = await service()._claim_reminder(session_factory, settings(), lambda: NOW)
    async with session_factory() as db:
        locked = []
        original = db.scalar

        async def record(statement, **kwargs):
            if statement._for_update_arg is not None:
                locked.append(statement.column_descriptions[0].get("entity"))
                assert statement._for_update_arg.skip_locked is True
            return await original(statement, **kwargs)

        monkeypatch.setattr(db, "scalar", record)
        _, _, job = await service()._locked_reminder(
            db, claim.reminder_id, skip_locked=True
        )
        assert job is not None
        assert locked == [
            VpnBotChat,
            VpnCustomer,
            VpnSubscription,
            models.VpnExpiryReminder,
        ]
