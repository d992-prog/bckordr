from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    WorkerNode,
)
from app.services import vpn_ready_notifications as notices
from app.services.vpn_ready_notifications import (
    READY_TEXT,
    ReadyNoticeClaim,
    claim_ready_notice,
    deliver_next_ready_notice,
)
from test_vpn_endpoint_migrations import (
    PostgresSchema,
    postgres_schema as postgres_schema,
)


NOW = datetime(2026, 9, 23, 15, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session_factory(
    tmp_path: Path,
) -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'vpn-ready-notifications.sqlite3'}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_ready_key(
    factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str = "",
    **changes: object,
) -> int:
    async with factory() as db:
        worker = WorkerNode(name=f"ready-node{suffix}")
        customer = VpnCustomer(
            telegram_user_id=f"123456{suffix}",
            status="active",
        )
        db.add_all([worker, customer])
        await db.flush()
        subscription = VpnSubscription(
            customer_id=customer.id,
            status="trial",
            starts_at=NOW - timedelta(minutes=1),
            expires_at=NOW + timedelta(days=7),
        )
        db.add(subscription)
        await db.flush()
        key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            status="active",
            config_uri="vless://secret-uuid@vpn.example.test:443",
        )
        models = {
            "key": key,
            "subscription": subscription,
            "customer": customer,
        }
        for target, field, value in changes.values():
            setattr(models[target], field, value)
        db.add(key)
        await db.commit()
        return key.id


@pytest.mark.parametrize(
    "change",
    [
        ("key", "status", "suspended"),
        ("key", "config_uri", None),
        ("key", "config_uri", ""),
        ("key", "worker_id", None),
        ("key", "ready_notice_claimed_at", NOW - timedelta(minutes=1)),
        ("key", "ready_notified_at", NOW - timedelta(minutes=1)),
        ("subscription", "status", "expired"),
        ("subscription", "expires_at", NOW),
        ("subscription", "starts_at", NOW + timedelta(seconds=1)),
        ("customer", "status", "archived"),
        ("customer", "telegram_user_id", None),
        ("customer", "telegram_user_id", ""),
    ],
    ids=[
        "inactive-key",
        "missing-uri",
        "empty-uri",
        "missing-worker",
        "already-claimed",
        "already-notified",
        "inactive-subscription",
        "expired-subscription",
        "future-subscription",
        "inactive-customer",
        "missing-telegram-id",
        "empty-telegram-id",
    ],
)
@pytest.mark.asyncio
async def test_claim_rejects_ineligible_profiles(session_factory, change) -> None:
    await _seed_ready_key(session_factory, change=change)

    async with session_factory() as db:
        assert await claim_ready_notice(db, now=NOW) is None


@pytest.mark.parametrize("status", ["active", "trial"])
@pytest.mark.asyncio
async def test_claim_accepts_active_unexpired_subscription_and_is_durable_once(
    session_factory,
    status: str,
) -> None:
    key_id = await _seed_ready_key(
        session_factory,
        change=("subscription", "status", status),
    )

    async with session_factory() as first:
        claim = await claim_ready_notice(first, now=NOW)
        assert claim == ReadyNoticeClaim(
            access_key_id=key_id,
            worker_id=1,
            chat_id="123456",
            claimed_at=NOW,
        )
        await first.commit()

    async with session_factory() as second:
        assert await claim_ready_notice(second, now=NOW + timedelta(seconds=1)) is None


@pytest.mark.asyncio
async def test_stale_claim_is_recovered_but_fresh_claim_is_not_stolen(
    session_factory,
) -> None:
    fresh_id = await _seed_ready_key(
        session_factory,
        suffix="1",
        change=("key", "ready_notice_claimed_at", NOW - timedelta(seconds=119)),
    )
    stale_id = await _seed_ready_key(
        session_factory,
        suffix="2",
        change=("key", "ready_notice_claimed_at", NOW - timedelta(seconds=121)),
    )

    async with session_factory() as db:
        claim = await claim_ready_notice(db, now=NOW)
        await db.commit()

    assert claim is not None
    assert claim.access_key_id == stale_id
    async with session_factory() as db:
        fresh = await db.get(VpnAccessKey, fresh_id)
        stale = await db.get(VpnAccessKey, stale_id)
    assert fresh is not None and fresh.ready_notice_claimed_at == (
        NOW - timedelta(seconds=119)
    ).replace(tzinfo=None)
    assert stale is not None and stale.ready_notice_claimed_at == NOW.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_never_attempted_key_precedes_large_due_retry_backlog(
    session_factory,
) -> None:
    retry_ids = [
        await _seed_ready_key(
            session_factory,
            suffix=f"retry-{index}",
            change=(
                "key",
                "ready_notice_retry_at",
                NOW - timedelta(minutes=index + 1),
            ),
        )
        for index in range(12)
    ]
    never_attempted_id = await _seed_ready_key(session_factory, suffix="new")

    async with session_factory() as db:
        claim = await claim_ready_notice(db, now=NOW)
        await db.commit()

    assert claim is not None
    assert claim.access_key_id == never_attempted_id
    assert claim.access_key_id not in retry_ids


@pytest.mark.asyncio
async def test_due_retries_use_oldest_time_and_exact_boundary(session_factory) -> None:
    newer_id = await _seed_ready_key(
        session_factory,
        suffix="newer",
        change=("key", "ready_notice_retry_at", NOW - timedelta(seconds=30)),
    )
    oldest_id = await _seed_ready_key(
        session_factory,
        suffix="oldest",
        change=("key", "ready_notice_retry_at", NOW - timedelta(seconds=50)),
    )
    exact_id = await _seed_ready_key(
        session_factory,
        suffix="exact",
        change=("key", "ready_notice_retry_at", NOW),
    )

    claimed_ids: list[int] = []
    for _ in range(3):
        async with session_factory() as db:
            claim = await claim_ready_notice(db, now=NOW)
            assert claim is not None
            claimed_ids.append(claim.access_key_id)
            await db.commit()

    assert claimed_ids == [oldest_id, newer_id, exact_id]


@pytest.mark.asyncio
async def test_stale_claim_does_not_bypass_future_retry(session_factory) -> None:
    key_id = await _seed_ready_key(
        session_factory,
        change=("key", "ready_notice_claimed_at", NOW - timedelta(minutes=3)),
        retry=("key", "ready_notice_retry_at", NOW + timedelta(seconds=10)),
    )

    async with session_factory() as db:
        assert await claim_ready_notice(db, now=NOW) is None
        await db.rollback()
    async with session_factory() as db:
        claim = await claim_ready_notice(db, now=NOW + timedelta(seconds=10))
        await db.commit()

    assert claim is not None
    assert claim.access_key_id == key_id


@pytest.mark.asyncio
async def test_postgres_concurrent_workers_claim_ready_notice_once(
    postgres_schema: PostgresSchema,
) -> None:
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(
        postgres_schema.engine,
        expire_on_commit=False,
        class_=AsyncSession,
    )
    key_id = await _seed_ready_key(sessions)
    first_claimed = asyncio.Event()
    release_first = asyncio.Event()

    async def first_worker() -> ReadyNoticeClaim | None:
        async with sessions() as db:
            await db.execute(text("SET LOCAL statement_timeout = '5s'"))
            claim = await claim_ready_notice(db, now=NOW)
            first_claimed.set()
            await release_first.wait()
            await db.commit()
            return claim

    async def second_worker() -> ReadyNoticeClaim | None:
        await first_claimed.wait()
        async with sessions() as db:
            await db.execute(text("SET LOCAL statement_timeout = '5s'"))
            claim = await claim_ready_notice(db, now=NOW + timedelta(seconds=1))
            await db.commit()
            return claim

    first_task = asyncio.create_task(first_worker())
    try:
        second_claim = await asyncio.wait_for(second_worker(), timeout=6)
    finally:
        release_first.set()
    first_claim = await asyncio.wait_for(first_task, timeout=6)

    claims = [claim for claim in (first_claim, second_claim) if claim is not None]
    assert len(claims) == 1
    assert claims[0].access_key_id == key_id


@pytest.mark.asyncio
async def test_delivery_commits_claim_before_send_and_notifies_once(session_factory) -> None:
    key_id = await _seed_ready_key(
        session_factory,
        change=("key", "ready_notice_retry_at", NOW - timedelta(seconds=1)),
    )
    sent: list[tuple[str, str]] = []

    async def sender(_settings: Settings, chat_id: str, text: str) -> None:
        async with session_factory() as db:
            key = await db.get(VpnAccessKey, key_id)
            assert key is not None
            assert key.ready_notice_claimed_at is not None
            assert key.ready_notified_at is None
        sent.append((chat_id, text))

    settings = Settings(_env_file=None)
    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=NOW,
    ) is True
    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=NOW + timedelta(seconds=1),
    ) is False

    assert sent == [("123456", READY_TEXT)]
    assert "vless://" not in sent[0][1]
    async with session_factory() as db:
        key = await db.get(VpnAccessKey, key_id)
        assert key is not None
        assert key.ready_notice_claimed_at is None
        assert key.ready_notice_retry_at is None
        assert key.ready_notified_at == NOW.replace(tzinfo=None)


@pytest.mark.asyncio
async def test_delivery_failure_resets_matching_claim_and_records_bounded_event(
    session_factory,
) -> None:
    key_id = await _seed_ready_key(session_factory)
    secret = "vless://must-not-leak"

    async def sender(_settings: Settings, _chat_id: str, _text: str) -> None:
        raise RuntimeError(secret)

    assert await deliver_next_ready_notice(
        session_factory,
        Settings(_env_file=None),
        sender,
        now=NOW,
    ) is False

    async with session_factory() as db:
        key = await db.get(VpnAccessKey, key_id)
        event = await db.scalar(
            select(VpnNodeEvent).where(
                VpnNodeEvent.event_type == "telegram_ready_delivery_failed"
            )
        )
    assert key is not None
    assert key.ready_notice_claimed_at is None
    assert key.ready_notified_at is None
    assert event is not None
    assert event.worker_id == 1
    assert event.message == "Telegram VPN ready notification delivery failed"
    assert event.details == {"access_key_id": key_id}
    assert secret not in f"{event.message}{event.details}"


@pytest.mark.asyncio
async def test_failed_key_backs_off_while_next_key_is_delivered(
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_key_id = await _seed_ready_key(session_factory, suffix="1")
    second_key_id = await _seed_ready_key(session_factory, suffix="2")
    attempts: list[str] = []

    async def sender(_settings: Settings, chat_id: str, _text: str) -> None:
        attempts.append(chat_id)
        if chat_id == "1234561":
            raise RuntimeError("permanent failure with secret data")

    settings = Settings(_env_file=None)
    failure_completed_at = NOW + timedelta(seconds=9)
    completion_times = iter((failure_completed_at, NOW + timedelta(seconds=70)))
    monkeypatch.setattr(notices, "utcnow", lambda: next(completion_times))
    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=NOW,
    ) is False
    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=NOW + timedelta(seconds=1),
    ) is True
    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=NOW + timedelta(seconds=2),
    ) is False

    async with session_factory() as db:
        events_before_retry = list(
            await db.scalars(
                select(VpnNodeEvent).where(
                    VpnNodeEvent.event_type == "telegram_ready_delivery_failed"
                )
            )
        )
        first_key = await db.get(VpnAccessKey, first_key_id)
        second_key = await db.get(VpnAccessKey, second_key_id)
    assert attempts == ["1234561", "1234562"]
    assert len(events_before_retry) == 1
    assert first_key is not None and first_key.ready_notified_at is None
    assert first_key.ready_notice_retry_at == (
        failure_completed_at + timedelta(seconds=60)
    ).replace(tzinfo=None)
    assert second_key is not None and second_key.ready_notified_at is not None
    assert second_key.ready_notice_retry_at is None

    assert await deliver_next_ready_notice(
        session_factory,
        settings,
        sender,
        now=failure_completed_at + timedelta(seconds=60),
    ) is False
    async with session_factory() as db:
        events_after_retry = list(
            await db.scalars(
                select(VpnNodeEvent).where(
                    VpnNodeEvent.event_type == "telegram_ready_delivery_failed"
                )
            )
        )
    assert attempts == ["1234561", "1234562", "1234561"]
    assert len(events_after_retry) == 2


@pytest.mark.asyncio
async def test_cancelled_delivery_preserves_cancellation_and_recovers_stale_claim(
    session_factory,
) -> None:
    key_id = await _seed_ready_key(session_factory)
    started = asyncio.Event()

    async def sender(_settings: Settings, _chat_id: str, _text: str) -> None:
        started.set()
        await asyncio.Event().wait()

    delivery = asyncio.create_task(
        deliver_next_ready_notice(
            session_factory,
            Settings(_env_file=None),
            sender,
            now=NOW,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    delivery.cancel()
    with pytest.raises(asyncio.CancelledError):
        await delivery

    async with session_factory() as db:
        fresh_claim = await claim_ready_notice(
            db,
            now=NOW + timedelta(seconds=119),
        )
        await db.rollback()
    assert fresh_claim is None

    async with session_factory() as db:
        recovered = await claim_ready_notice(
            db,
            now=NOW + timedelta(seconds=121),
        )
        await db.commit()
    assert recovered is not None
    assert recovered.access_key_id == key_id


@pytest.mark.asyncio
async def test_failure_never_clears_a_replaced_claim(session_factory) -> None:
    key_id = await _seed_ready_key(session_factory)
    replacement = NOW + timedelta(seconds=30)

    async def sender(_settings: Settings, _chat_id: str, _text: str) -> None:
        async with session_factory() as db:
            key = await db.get(VpnAccessKey, key_id)
            assert key is not None
            key.ready_notice_claimed_at = replacement
            await db.commit()
        raise RuntimeError("ordinary failure")

    assert await deliver_next_ready_notice(
        session_factory,
        Settings(_env_file=None),
        sender,
        now=NOW,
    ) is False

    async with session_factory() as db:
        key = await db.get(VpnAccessKey, key_id)
    assert key is not None
    assert key.ready_notice_claimed_at == replacement.replace(tzinfo=None)
    assert key.ready_notice_retry_at is None
    assert key.ready_notified_at is None
