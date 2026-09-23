from dataclasses import FrozenInstanceError, asdict
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import (
    AppSetting,
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnControlOperation,
    VpnEndpoint,
    VpnPlan,
    VpnSubscription,
    WorkerNode,
    WorkerTask,
)
from app.core.config import Settings
from app.services.vpn_telegram_identity import TelegramIdentity
from app.services.vpn_policy import NODE_CAPACITY_STATUSES, select_public_vpn_endpoint


NOW = datetime(2026, 9, 23, 12, tzinfo=UTC)


class HookedSession(AsyncSession):
    on_lock = None

    async def scalar(self, statement, **kwargs):
        if statement._for_update_arg is not None and self.on_lock is not None:
            hook, self.on_lock = self.on_lock, None
            await hook(self)
        return await super().scalar(statement, **kwargs)


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _allow_invalid_rows(dbapi_connection, _connection_record):
        # Exercise policy checks even for values normally rejected by DB constraints.
        dbapi_connection.execute("PRAGMA ignore_check_constraints=ON")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=HookedSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def worker(name, **changes):
    values = dict(
        name=name,
        status="ready",
        is_enabled=True,
        vpn_enabled=True,
        vpn_role="vpn_node",
        vpn_runtime_status="ready",
        vpn_public_host=f"{name}.example",
        vpn_inbound_id=1,
        ssh_host="10.0.0.1",
        ssh_password="secret",
        vpn_last_checked_at=NOW,
    )
    values.update(changes)
    return WorkerNode(**values)


def endpoint(node, **changes):
    values = dict(
        worker_id=node.id,
        inbound_id=1,
        public_host=f"{node.name}.example",
        port=443,
        protocol="vless",
        transport="raw",
        security="reality",
        status="ready",
        verified_at=NOW,
        max_active_profiles=4,
    )
    values.update(changes)
    return VpnEndpoint(**values)


async def add_key(session, target, status="active"):
    customer = VpnCustomer(status="active")
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=20)
    session.add(subscription)
    await session.flush()
    key = VpnAccessKey(
        subscription_id=subscription.id,
        worker_id=target.worker_id,
        endpoint_id=target.id,
        status=status,
    )
    session.add(key)
    await session.flush()
    return key


@pytest.mark.asyncio
async def test_selects_least_utilized_ratio_then_lowest_id(session_factory):
    async with session_factory() as session:
        nodes = [worker(str(i)) for i in range(3)]
        session.add_all(nodes)
        await session.flush()
        targets = [endpoint(nodes[0], max_active_profiles=2), endpoint(nodes[1]), endpoint(nodes[2])]
        session.add_all(targets)
        await session.flush()
        await add_key(session, targets[0])
        await add_key(session, targets[1])
        await add_key(session, targets[2])
        selected = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert selected is not None
        assert selected.endpoint.id == targets[1].id
        assert selected.occupied_profiles == 1
        assert selected.max_active_profiles == 4
        assert selected.utilization == 0.25
        with pytest.raises(FrozenInstanceError):
            selected.occupied_profiles = 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"status": "draining"},
        {"verified_at": None},
        {"security": "tls"},
        {"max_active_profiles": None},
        {"max_active_profiles": 0},
        {"archived_at": NOW},
        {"vpn_enabled": False},
        {"vpn_last_checked_at": None},
        {"vpn_last_checked_at": NOW - timedelta(seconds=61)},
    ],
)
async def test_excludes_invalid_endpoint_or_worker(session_factory, change):
    async with session_factory() as session:
        node = worker("excluded")
        fallback_node = worker("fallback")
        for field, value in change.items():
            if hasattr(node, field):
                setattr(node, field, value)
        session.add_all([node, fallback_node])
        await session.flush()
        invalid = endpoint(node)
        for field, value in change.items():
            if hasattr(invalid, field):
                setattr(invalid, field, value)
        fallback = endpoint(fallback_node)
        session.add_all([invalid, fallback])
        await session.flush()
        selected = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert selected is not None
        assert selected.endpoint.id == fallback.id


@pytest.mark.asyncio
async def test_full_endpoint_is_excluded(session_factory):
    async with session_factory() as session:
        first, second = worker("full"), worker("free")
        session.add_all([first, second])
        await session.flush()
        full, free = endpoint(first, max_active_profiles=1), endpoint(second)
        session.add_all([full, free])
        await session.flush()
        await add_key(session, full)
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None and result.endpoint.id == free.id


@pytest.mark.asyncio
async def test_active_attack_excludes_worker(session_factory):
    async with session_factory() as session:
        busy, free = worker("busy"), worker("free")
        session.add_all([busy, free])
        await session.flush()
        busy_endpoint, free_endpoint = endpoint(busy), endpoint(free)
        session.add_all([busy_endpoint, free_endpoint])
        domain = DropDomain(fqdn="target.fr", zone="fr", drop_date=NOW.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=NOW,
            planned_end_at=NOW + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(WorkerTask(attack_run_id=run.id, domain_id=domain.id, worker_id=busy.id, status="running"))
        await session.flush()
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None and result.endpoint.id == free_endpoint.id


@pytest.mark.asyncio
async def test_all_capacity_statuses_count_but_revoked_does_not(session_factory):
    statuses = (
        "pending_sync", "syncing", "active", "pending_suspend", "suspended", "pending_revoke", "failed",
    )
    assert NODE_CAPACITY_STATUSES == statuses
    async with session_factory() as session:
        node = worker("statuses")
        session.add(node)
        await session.flush()
        target = endpoint(node, max_active_profiles=8)
        session.add(target)
        await session.flush()
        for status in (*statuses, "revoked"):
            await add_key(session, target, status)
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60)
        assert result is not None
        assert result.occupied_profiles == 7
        assert result.utilization == 7 / 8


@pytest.mark.asyncio
@pytest.mark.parametrize("max_age", [0, -30])
async def test_health_age_is_clamped_to_one_second(session_factory, max_age):
    async with session_factory() as session:
        recent = worker("recent", vpn_last_checked_at=NOW - timedelta(seconds=1))
        stale = worker("stale", vpn_last_checked_at=NOW - timedelta(seconds=2))
        session.add_all([recent, stale])
        await session.flush()
        session.add_all([endpoint(stale), endpoint(recent)])
        await session.flush()
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=max_age)
        assert result is not None and result.endpoint.worker_id == recent.id


@pytest.mark.asyncio
async def test_lock_recounts_full_first_candidate_and_tries_next(session_factory):
    async with session_factory() as session:
        first, second = worker("first"), worker("second")
        session.add_all([first, second])
        await session.flush()
        first_endpoint, second_endpoint = endpoint(first, max_active_profiles=1), endpoint(second)
        session.add_all([first_endpoint, second_endpoint])
        await session.flush()
        events = []

        @event.listens_for(session.sync_session, "after_transaction_create")
        def trace_savepoint(_session, transaction):
            if transaction.nested:
                events.append("savepoint")

        @event.listens_for(session.sync_session, "after_soft_rollback")
        def trace_rollback(_session, transaction):
            if transaction.nested:
                events.append("rollback")

        async def fill_first(current_session):
            await add_key(current_session, first_endpoint)

        session.on_lock = fill_first
        original_scalar = session.scalar

        async def record_locks(statement, **kwargs):
            if statement._for_update_arg is not None:
                entity = statement.column_descriptions[0]["entity"]
                events.append((entity, next(iter(statement.compile().params.values()))))
            return await original_scalar(statement, **kwargs)

        session.scalar = record_locks
        result = await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60, lock=True)
        assert result is not None and result.endpoint.id == second_endpoint.id
        assert events[:7] == [
            "savepoint",
            (WorkerNode, first.id),
            (VpnEndpoint, first_endpoint.id),
            "rollback",
            "savepoint",
            (WorkerNode, second.id),
            (VpnEndpoint, second_endpoint.id),
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "model,changes",
    [
        (VpnEndpoint, {"status": "draining"}),
        (VpnEndpoint, {"security": "tls"}),
        (VpnEndpoint, {"verified_at": None}),
        (VpnEndpoint, {"max_active_profiles": None}),
        (WorkerNode, {"vpn_enabled": False}),
        (WorkerNode, {"vpn_last_checked_at": NOW - timedelta(minutes=2)}),
    ],
)
async def test_lock_revalidates_and_returns_none_without_alternative(
    session_factory, model, changes,
):
    async with session_factory() as session:
        node = worker("only")
        session.add(node)
        await session.flush()
        target = endpoint(node)
        session.add(target)
        await session.flush()

        async def invalidate(current_session):
            await current_session.execute(
                update(model)
                .where(model.id == (target.id if model is VpnEndpoint else node.id))
                .values(**changes)
                .execution_options(synchronize_session=False)
            )

        session.on_lock = invalidate
        assert await select_public_vpn_endpoint(
            session, now=NOW, health_max_age_seconds=60, lock=True
        ) is None


@pytest.mark.asyncio
async def test_lock_rejects_pending_orm_mutation_without_flushing_it(session_factory):
    async with session_factory() as session:
        node = worker("only")
        session.add(node)
        await session.flush()
        session.add(endpoint(node))
        await session.flush()
        pending = worker("pending")
        session.add(pending)

        with pytest.raises(ValueError, match="pending ORM changes"):
            await select_public_vpn_endpoint(
                session, now=NOW, health_max_age_seconds=60, lock=True
            )
        assert pending in session.new


@pytest.mark.asyncio
async def test_no_candidate_returns_none(session_factory):
    async with session_factory() as session:
        assert await select_public_vpn_endpoint(session, now=NOW, health_max_age_seconds=60) is None


def trial_settings(**overrides):
    values = dict(
        VPN_PUBLIC_TRIAL_ENABLED=True,
        VPN_PUBLIC_TRIAL_RELEASE_ID="a" * 64,
        VPN_CONTROL_DISPATCH_ENABLED=True,
        VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
    )
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def seed_trial(db):
    node = worker("public")
    plan = VpnPlan(slug="trial-7d", name="Trial", duration_days=7, max_devices=1, traffic_limit_gb=13)
    customer = VpnCustomer(telegram_user_id="123", status="active")
    db.add_all([node, plan, customer, AppSetting(key="vpn_public_release_ready_v1", value="a" * 64)])
    await db.flush()
    target = endpoint(
        node, server_name="cdn.example.test", public_key="A" * 43,
        short_id="0123456789abcdef", fingerprint="chrome", flow="xtls-rprx-vision",
    )
    db.add(target)
    await db.commit()
    return customer, plan, target


async def trial_counts(db):
    return [await db.scalar(select(func.count()).select_from(model))
            for model in (VpnSubscription, VpnAccessKey, VpnControlOperation)]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [
    "disabled", "dispatch", "marker", "release", "uppercase", "missing_plan",
    "duration", "devices", "inactive", "transport", "capacity",
])
async def test_trial_readiness_fails_closed_without_rows(session_factory, monkeypatch, failure):
    from app.services import vpn_public_trial as trial

    async with session_factory() as db:
        customer, plan, target = await seed_trial(db)
        settings = trial_settings()
        monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
        if failure == "disabled":
            settings.vpn_public_trial_enabled = False
        elif failure == "dispatch":
            settings.vpn_control_dispatch_enabled = False
        elif failure == "marker":
            (await db.scalar(select(AppSetting))).value = "b" * 64
        elif failure in {"release", "uppercase"}:
            settings.vpn_public_trial_release_id = "A" * 64 if failure == "uppercase" else "bad"
        elif failure == "missing_plan":
            await db.delete(plan)
        elif failure == "duration":
            plan.duration_days = 8
        elif failure == "devices":
            plan.max_devices = 2
        elif failure == "inactive":
            plan.is_active = False
        elif failure == "capacity":
            target.max_active_profiles = None
        elif failure == "transport":
            monkeypatch.undo()  # Real strict transport rejects the absent known-hosts file.
        await db.commit()
        view = await trial.public_trial_status(db, settings, customer, NOW)
        assert view.state == ("disabled" if failure == "disabled" else "capacity_paused")
        with pytest.raises(trial.PublicTrialUnavailable, match="^public_trial_unavailable$"):
            await trial.activate_public_trial(db, settings, TelegramIdentity("00123"), NOW)
        await db.rollback()
        assert await trial_counts(db) == [0, 0, 0]
        assert (await db.get(VpnCustomer, 1)).trial_started_at is None


@pytest.mark.asyncio
async def test_trial_activation_copies_plan_and_replays_same_private_chain(session_factory, monkeypatch):
    from app.services import vpn_public_trial as trial

    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, plan, target = await seed_trial(db)
        settings = trial_settings()
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "available"
        result = await trial.activate_public_trial(db, settings, TelegramIdentity("00123"), NOW.replace(tzinfo=None))
        assert result.state == "preparing"
        assert result.duration_days == 7 and result.profile_limit == 1
        assert result.expires_at == NOW + timedelta(days=7)
        await db.commit()
        subscription = await db.get(VpnSubscription, result.subscription_id)
        key = await db.get(VpnAccessKey, result.access_key_id)
        assert subscription.customer_id == customer.id
        assert (subscription.plan_id, subscription.traffic_limit_gb, subscription.max_devices) == (plan.id, 13, 1)
        assert subscription.status == "trial"
        assert key.endpoint_id == target.id and key.worker_id == target.worker_id
        assert key.protocol == "vless" and key.status == "pending_sync"
        assert key.config_uri is None and UUID(key.external_uuid).int > 0
        assert len(key.panel_sub_id) == 32
        assert key.verified_client_email == f"veltrix-trial-{customer.id}"
        assert key.display_name == "Veltrix VPN"
        assert customer.trial_started_at == NOW
        operation = await db.scalar(select(VpnControlOperation))
        assert (operation.generation, operation.action, operation.state) == (1, "provision", "queued")
        assert set(asdict(result)) == {"state", "duration_days", "profile_limit", "subscription_id", "access_key_id", "expires_at"}
        assert key.external_uuid not in repr(result)
        with pytest.raises(FrozenInstanceError):
            result.state = "active"
        settings.vpn_public_trial_enabled = False
        replay = await trial.activate_public_trial(db, settings, TelegramIdentity("123"), NOW)
        assert replay == result
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "preparing"
        assert await trial_counts(db) == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,expected", [
    ("queued", "preparing"), ("claimed", "preparing"), ("failed", "preparing"),
    ("uncertain", "preparing"), ("ready", "active"), ("no_uri", "preparing"),
    ("expired", "used"), ("revoked", "used"), ("disabled", "used"),
    ("cancelled", "used"), ("customer_disabled", "used"),
])
async def test_trial_status_chain_states(session_factory, monkeypatch, change, expected):
    from app.services import vpn_public_trial as trial

    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        result = await trial.activate_public_trial(db, trial_settings(), TelegramIdentity("123"), NOW)
        key = await db.get(VpnAccessKey, result.access_key_id)
        subscription = await db.get(VpnSubscription, result.subscription_id)
        operation = await db.scalar(select(VpnControlOperation))
        if change in {"queued", "claimed", "failed", "uncertain"}:
            operation.state = change
        else:
            operation.state = "succeeded"
            key.status = "active"
            key.config_uri = "vless://private-material"
            if change == "no_uri":
                key.config_uri = None
            elif change == "expired":
                subscription.expires_at = NOW
            elif change == "revoked":
                key.status = "revoked"
            elif change in {"disabled", "cancelled"}:
                subscription.status = change
            elif change == "customer_disabled":
                customer.status = "disabled"
        await db.commit()
        result = await trial.public_trial_status(db, trial_settings(VPN_PUBLIC_TRIAL_ENABLED=False), customer, NOW)
        assert result.state == expected
        assert "private-material" not in repr(result)
        if expected == "used":
            with pytest.raises(trial.PublicTrialConflict):
                await trial.activate_public_trial(db, trial_settings(), TelegramIdentity("123"), NOW)
        else:
            replay = await trial.activate_public_trial(db, trial_settings(), TelegramIdentity("123"), NOW)
            assert replay.subscription_id == result.subscription_id
        assert await trial_counts(db) == [1, 1, 1]


@pytest.mark.asyncio
async def test_trial_past_marker_blocks_new_trial(session_factory, monkeypatch):
    from app.services import vpn_public_trial as trial

    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        customer.trial_started_at = NOW - timedelta(days=8)
        await db.commit()
        assert (await trial.public_trial_status(db, trial_settings(), customer, NOW)).state == "used"
        with pytest.raises(trial.PublicTrialConflict):
            await trial.activate_public_trial(db, trial_settings(), TelegramIdentity("123"), NOW)
        assert await trial_counts(db) == [0, 0, 0]


@pytest.mark.asyncio
async def test_trial_staging_failure_caller_rollback_clears_everything(session_factory, monkeypatch):
    from app.services import vpn_public_trial as trial
    from app.services.vpn_control_intents import VpnControlIntentError

    async def fail_stage(*_args, **_kwargs):
        raise VpnControlIntentError("internal-secret")

    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    monkeypatch.setattr(trial, "stage_vpn_control_operation", fail_stage)
    async with session_factory() as db:
        await seed_trial(db)
        with pytest.raises(trial.PublicTrialUnavailable, match="^public_trial_unavailable$"):
            await trial.activate_public_trial(db, trial_settings(), TelegramIdentity("123"), NOW)
        await db.rollback()
        assert await trial_counts(db) == [0, 0, 0]
        assert (await db.get(VpnCustomer, 1)).trial_started_at is None


@pytest.mark.asyncio
async def test_friend_trial_marker_and_replay_and_second_trial_rejection(session_factory, monkeypatch):
    from app.services import vpn_friend_invitations as friends
    from app.db.models import User, VpnFriendInvitation

    monkeypatch.setattr(friends, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        db.add(User(id=1, username="owner", password_hash="hash", role="admin", status="active"))
        db.add(AppSetting(key="vpn_friend_beta_release_ready_v1", value="a" * 64))
        await db.flush()
        for slot, token in [(1, "A" * 43), (2, "B" * 43)]:
            db.add(VpnFriendInvitation(slot=slot, token_digest=friends.digest_invite_token(token),
                                      created_by_user_id=1, redeem_expires_at=NOW + timedelta(days=1)))
        await db.commit()
        settings = trial_settings(VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64)
        first = await friends.redeem_friend_invitation(db, settings, "A" * 43, TelegramIdentity("123"), NOW)
        assert customer.trial_started_at == NOW
        assert await friends.redeem_friend_invitation(db, settings, "A" * 43, TelegramIdentity("123"), NOW) == first
        with pytest.raises(friends.FriendInvitationConflict):
            await friends.redeem_friend_invitation(db, settings, "B" * 43, TelegramIdentity("123"), NOW)
        assert await trial_counts(db) == [1, 1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("binding", ["redeemed", "missing_redeemed_at", "wrong_identity", "missing_marker"])
async def test_friend_trial_is_never_a_public_replay(session_factory, monkeypatch, binding):
    from app.services import vpn_friend_invitations as friends
    from app.services import vpn_public_trial as trial
    from app.db.models import VpnFriendInvitation

    monkeypatch.setattr(friends, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        db.add(AppSetting(key="vpn_friend_beta_release_ready_v1", value="a" * 64))
        invitation = VpnFriendInvitation(
            slot=1, token_digest=friends.digest_invite_token("A" * 43),
            redeem_expires_at=NOW + timedelta(days=1),
        )
        db.add(invitation)
        await db.commit()
        settings = trial_settings(VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64)
        identity = TelegramIdentity("123")
        first = await friends.redeem_friend_invitation(db, settings, "A" * 43, identity, NOW)
        assert await friends.redeem_friend_invitation(db, settings, "A" * 43, identity, NOW) == first
        if binding == "missing_redeemed_at":
            invitation.redeemed_at = None
        elif binding == "wrong_identity":
            invitation.telegram_user_id = "456"
        elif binding == "missing_marker":
            customer.trial_started_at = None
        await db.commit()

        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "used"
        with pytest.raises(trial.PublicTrialConflict):
            await trial.activate_public_trial(db, settings, identity, NOW)
        assert await trial_counts(db) == [1, 1, 1]
        if binding == "redeemed":
            assert await friends.redeem_friend_invitation(db, settings, "A" * 43, identity, NOW) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("generation", [0, 2])
async def test_trial_active_requires_successful_current_generation(session_factory, monkeypatch, generation):
    from app.services import vpn_public_trial as trial

    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        settings = trial_settings()
        result = await trial.activate_public_trial(db, settings, TelegramIdentity("123"), NOW)
        key = await db.get(VpnAccessKey, result.access_key_id)
        operation = await db.scalar(select(VpnControlOperation))
        operation.state = "succeeded"
        key.status = "active"
        key.config_uri = "vless://synthetic-current-generation"
        key.operation_generation = generation
        await db.commit()
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "preparing"

        key.operation_generation = 2
        db.add(VpnControlOperation(
            id=str(uuid4()), access_key_id=key.id, worker_id=key.worker_id,
            endpoint_id=key.endpoint_id, generation=2, action="provision", state="succeeded",
            request_snapshot=operation.request_snapshot, request_digest=operation.request_digest,
        ))
        await db.commit()
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "active"


@pytest.mark.asyncio
async def test_trial_status_reads_current_generation_from_database(session_factory, monkeypatch):
    from app.services import vpn_public_trial as trial

    monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        settings = trial_settings()
        result = await trial.activate_public_trial(db, settings, TelegramIdentity("123"), NOW)
        key = await db.get(VpnAccessKey, result.access_key_id)
        (await db.scalar(select(VpnControlOperation))).state = "succeeded"
        key.status = "active"
        key.config_uri = "vless://synthetic-current-generation"
        await db.commit()
        original_scalar = db.scalar

        async def advance_generation_before_operation_read(statement, **kwargs):
            if statement.column_descriptions[0]["entity"] is VpnControlOperation:
                await db.execute(update(VpnAccessKey).where(VpnAccessKey.id == key.id)
                                 .values(operation_generation=2)
                                 .execution_options(synchronize_session=False))
            return await original_scalar(statement, **kwargs)

        db.scalar = advance_generation_before_operation_read
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "preparing"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["expired", "disabled"])
async def test_legacy_friend_without_marker_blocks_second_invitation(session_factory, monkeypatch, status):
    from app.services import vpn_friend_invitations as friends
    from app.services import vpn_public_trial as trial
    from app.db.models import VpnFriendInvitation

    monkeypatch.setattr(friends, "load_transport_snapshot", lambda *_: object())
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        db.add(AppSetting(key="vpn_friend_beta_release_ready_v1", value="a" * 64))
        for slot, token in [(1, "A" * 43), (2, "B" * 43)]:
            db.add(VpnFriendInvitation(
                slot=slot, token_digest=friends.digest_invite_token(token),
                redeem_expires_at=NOW + timedelta(days=1),
            ))
        await db.commit()
        settings = trial_settings(VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64)
        identity = TelegramIdentity("123")
        first = await friends.redeem_friend_invitation(db, settings, "A" * 43, identity, NOW)
        subscription = await db.get(VpnSubscription, first.subscription_id)
        subscription.status = status
        customer.trial_started_at = None
        await db.commit()
        assert (await trial.public_trial_status(db, settings, customer, NOW)).state == "used"
        with pytest.raises(friends.FriendInvitationConflict):
            await friends.redeem_friend_invitation(db, settings, "B" * 43, identity, NOW)
        assert await trial_counts(db) == [1, 1, 1]
        assert await friends.redeem_friend_invitation(db, settings, "A" * 43, identity, NOW) == first


@pytest.mark.asyncio
@pytest.mark.parametrize("prior_trial", [False, True])
async def test_friend_trial_rejection_or_staging_failure_preserves_marker(session_factory, monkeypatch, prior_trial):
    from app.services import vpn_friend_invitations as friends
    from app.services.vpn_control_intents import VpnControlIntentError
    from app.db.models import VpnFriendInvitation

    async def fail_stage(*_args, **_kwargs):
        raise VpnControlIntentError("synthetic-staging-failure")

    monkeypatch.setattr(friends, "load_transport_snapshot", lambda *_: object())
    monkeypatch.setattr(friends, "stage_vpn_control_operation", fail_stage)
    async with session_factory() as db:
        customer, _, _ = await seed_trial(db)
        previous = NOW - timedelta(days=8) if prior_trial else None
        customer.trial_started_at = previous
        db.add(AppSetting(key="vpn_friend_beta_release_ready_v1", value="a" * 64))
        db.add(VpnFriendInvitation(slot=1, token_digest=friends.digest_invite_token("A" * 43),
                                  redeem_expires_at=NOW + timedelta(days=1)))
        await db.commit()
        settings = trial_settings(VPN_FRIEND_BETA_ENABLED=True, VPN_FRIEND_BETA_RELEASE_ID="a" * 64)
        error = friends.FriendInvitationConflict if prior_trial else friends.FriendInvitationUnavailable
        with pytest.raises(error):
            await friends.redeem_friend_invitation(db, settings, "A" * 43, TelegramIdentity("123"), NOW)
        await db.refresh(customer)
        assert (customer.trial_started_at is not None) == prior_trial
        assert await trial_counts(db) == [0, 0, 0]
        assert (await db.get(VpnFriendInvitation, 1)).redeemed_at is None
