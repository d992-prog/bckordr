from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base, utcnow
from app.db.models import (
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnCustomer,
    VpnNodeEvent,
    VpnSubscription,
    WorkerNode,
    WorkerTask,
)
from app.services.app_settings import VPN_LIFECYCLE_LAST_RESULT_KEY, get_app_setting
from app.services.control_runtime import ControlRuntimeOrchestrator
from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    database_path = (tmp_path / "vpn-lifecycle.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def _vpn_worker(name: str, *, password: str = "secret") -> WorkerNode:
    now = utcnow()
    return WorkerNode(
        name=name,
        status="ready",
        is_enabled=True,
        vpn_enabled=True,
        vpn_role="vpn_node",
        vpn_runtime_status="ready",
        vpn_public_host=f"{name}.example",
        vpn_inbound_id=1,
        ssh_host="10.0.0.1",
        ssh_password=password,
        last_seen_at=now,
        last_heartbeat_at=now,
    )


async def _subscription(
    session: AsyncSession,
    *,
    status: str = "active",
    expires_at: datetime | None = None,
    max_devices: int = 2,
) -> VpnSubscription:
    customer = VpnCustomer(status="active")
    session.add(customer)
    await session.flush()
    subscription = VpnSubscription(
        customer_id=customer.id,
        status=status,
        starts_at=datetime(2026, 9, 1, tzinfo=UTC),
        expires_at=expires_at,
        max_devices=max_devices,
    )
    session.add(subscription)
    await session.flush()
    return subscription


@pytest.mark.asyncio
async def test_lifecycle_retries_pending_sync_key_and_persists_result(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = _vpn_worker("safe")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, expires_at=now + timedelta(days=1))
        access_key = VpnAccessKey(subscription_id=subscription.id, status="pending_sync")
        session.add(access_key)
        await session.commit()
        key_id = access_key.id
        worker_id = worker.id

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        access_key.config_uri = f"vless://key-{access_key.id}"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", fake_provision)

    async with session_factory() as session:
        result = await run_vpn_lifecycle_maintenance(session, now=now, batch_size=50)
        stored_key = await session.get(VpnAccessKey, key_id)
        raw_result = await get_app_setting(session, VPN_LIFECYCLE_LAST_RESULT_KEY)

    assert result["checked_keys"] == 1
    assert result["provisioned_keys"] == 1
    assert stored_key is not None
    assert stored_key.status == "active"
    assert stored_key.worker_id == worker_id
    assert json.loads(raw_result or "{}")["ran_at"] == now.isoformat()


@pytest.mark.asyncio
async def test_lifecycle_prioritizes_due_suspension_over_old_pending_sync(session_factory):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        active_subscription = await _subscription(
            session,
            expires_at=now + timedelta(days=1),
            max_devices=10,
        )
        session.add_all(
            [
                VpnAccessKey(subscription_id=active_subscription.id, status="pending_sync")
                for _ in range(3)
            ]
        )
        expired_subscription = await _subscription(
            session,
            status="expired",
            expires_at=now - timedelta(minutes=1),
        )
        due_key = VpnAccessKey(subscription_id=expired_subscription.id, status="active")
        session.add(due_key)
        await session.commit()
        due_key_id = due_key.id

    async with session_factory() as session:
        result = await run_vpn_lifecycle_maintenance(session, now=now, batch_size=2)
        stored_due_key = await session.get(VpnAccessKey, due_key_id)

    assert result["checked_keys"] == 2
    assert stored_due_key is not None
    assert stored_due_key.status == "suspended"


@pytest.mark.asyncio
async def test_lifecycle_rotates_pending_revoke_retries(session_factory):
    first_run = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    second_run = first_run + timedelta(minutes=1)
    old_time = first_run - timedelta(days=1)
    async with session_factory() as session:
        subscription = await _subscription(
            session,
            status="expired",
            expires_at=first_run - timedelta(minutes=1),
        )
        first = VpnAccessKey(
            subscription_id=subscription.id,
            status="pending_revoke",
            config_uri="vless://first",
            updated_at=old_time,
        )
        second = VpnAccessKey(
            subscription_id=subscription.id,
            status="pending_revoke",
            config_uri="vless://second",
            updated_at=old_time,
        )
        session.add_all([first, second])
        await session.commit()
        first_id = first.id
        second_id = second.id

    async with session_factory() as session:
        await run_vpn_lifecycle_maintenance(session, now=first_run, batch_size=1)
    async with session_factory() as session:
        await run_vpn_lifecycle_maintenance(session, now=second_run, batch_size=1)
        first = await session.get(VpnAccessKey, first_id)
        second = await session.get(VpnAccessKey, second_id)

    assert first is not None and second is not None
    assert first.last_error == "Assigned VPN node is missing; revoke is pending"
    assert second.last_error == "Assigned VPN node is missing; revoke is pending"


@pytest.mark.asyncio
async def test_lifecycle_keeps_revoke_pending_on_busy_node(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    revoke_calls: list[int] = []
    async with session_factory() as session:
        worker = _vpn_worker("busy")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, status="expired", expires_at=now - timedelta(minutes=1))
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            status="pending_revoke",
            config_uri="vless://busy",
        )
        session.add(access_key)
        domain = DropDomain(fqdn="lifecycle-busy.fr", zone="fr", drop_date=now.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now - timedelta(minutes=1),
            planned_end_at=now + timedelta(minutes=1),
        )
        session.add(run)
        await session.flush()
        session.add(
            WorkerTask(
                attack_run_id=run.id,
                domain_id=domain.id,
                worker_id=worker.id,
                status="running",
            )
        )
        await session.commit()
        key_id = access_key.id

    async def fake_revoke(db, access_key, *, worker=None):
        del db, worker
        revoke_calls.append(access_key.id)
        access_key.status = "revoked"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.revoke_vpn_access_key", fake_revoke)

    async with session_factory() as session:
        result = await run_vpn_lifecycle_maintenance(session, now=now, batch_size=50)
        stored_key = await session.get(VpnAccessKey, key_id)
        events = list((await session.execute(select(VpnNodeEvent))).scalars().all())

    assert result["skipped_unsafe_keys"] == 1
    assert result["pending_revoke_keys"] == 1
    assert stored_key is not None
    assert stored_key.status == "pending_revoke"
    assert "active domain attack" in (stored_key.last_error or "")
    assert revoke_calls == []
    assert events[-1].event_type == "lifecycle_unsafe_skip"


@pytest.mark.asyncio
async def test_lifecycle_continues_after_failure_and_redacts_credentials(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        first_worker = _vpn_worker("failure", password="super-secret")
        second_worker = _vpn_worker("success")
        session.add_all([first_worker, second_worker])
        await session.flush()
        subscription = await _subscription(session, status="cancelled", expires_at=now - timedelta(minutes=1))
        failure_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=first_worker.id,
            status="active",
            public_name="failure",
            config_uri="vless://failure",
        )
        success_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=second_worker.id,
            status="active",
            public_name="success",
            config_uri="vless://success",
        )
        session.add_all([failure_key, success_key])
        await session.commit()
        failure_key_id = failure_key.id
        success_key_id = success_key.id

    async def fake_revoke(db, access_key, *, worker=None):
        del db
        if access_key.public_name == "failure":
            raise RuntimeError(f"ssh failed with {worker.ssh_password}")
        access_key.status = "revoked"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.revoke_vpn_access_key", fake_revoke)

    async with session_factory() as session:
        result = await run_vpn_lifecycle_maintenance(session, now=now, batch_size=50)
        failed = await session.get(VpnAccessKey, failure_key_id)
        succeeded = await session.get(VpnAccessKey, success_key_id)

    assert result["failed_keys"] == 1
    assert result["revoked_keys"] == 1
    assert failed is not None
    assert failed.status == "pending_revoke"
    assert "<redacted>" in (failed.last_error or "")
    assert "super-secret" not in (failed.last_error or "")
    assert succeeded is not None
    assert succeeded.status == "revoked"


@pytest.mark.asyncio
async def test_lifecycle_redacts_handled_provisioning_failure(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = _vpn_worker("handled-failure", password="ssh-password")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, expires_at=now + timedelta(days=1))
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=worker.id,
            status="pending_sync",
            external_uuid="vpn-client-uuid",
            config_uri="vless://credential-uri",
        )
        session.add(access_key)
        await session.commit()
        key_id = access_key.id
        worker_id = worker.id

    async def handled_failure(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        message = (
            f"command failed password={worker.ssh_password} uuid={access_key.external_uuid} "
            f"uri={access_key.config_uri}"
        )
        access_key.status = "pending_sync"
        access_key.last_error = message
        worker.vpn_last_error = message
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", handled_failure)

    async with session_factory() as session:
        await run_vpn_lifecycle_maintenance(session, now=now, batch_size=1)
        stored_key = await session.get(VpnAccessKey, key_id)
        stored_worker = await session.get(WorkerNode, worker_id)

    assert stored_key is not None and stored_worker is not None
    for secret in ("ssh-password", "vpn-client-uuid", "vless://credential-uri"):
        assert secret not in (stored_key.last_error or "")
        assert secret not in (stored_worker.vpn_last_error or "")
    assert "<redacted>" in (stored_key.last_error or "")


@pytest.mark.asyncio
async def test_lifecycle_revokes_never_provisioned_terminal_key_locally(session_factory):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        subscription = await _subscription(session, status="cancelled", expires_at=None)
        access_key = VpnAccessKey(subscription_id=subscription.id, status="pending_sync")
        session.add(access_key)
        await session.commit()
        key_id = access_key.id

        result = await run_vpn_lifecycle_maintenance(session, now=now, batch_size=50)
        stored_key = await session.get(VpnAccessKey, key_id)

    assert result["revoked_keys"] == 1
    assert stored_key is not None
    assert stored_key.status == "revoked"
    assert stored_key.revoked_at == now


@pytest.mark.asyncio
async def test_lifecycle_calls_are_serialized(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = _vpn_worker("serialized")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, expires_at=now + timedelta(days=1), max_devices=2)
        session.add_all(
            [
                VpnAccessKey(subscription_id=subscription.id, status="pending_sync", public_name="first"),
                VpnAccessKey(subscription_id=subscription.id, status="pending_sync", public_name="second"),
            ]
        )
        await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    provisioned: list[int] = []

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        provisioned.append(access_key.id)
        if len(provisioned) == 1:
            entered.set()
            await release.wait()
        access_key.worker_id = worker.id
        access_key.status = "active"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", fake_provision)

    async def invoke_cycle():
        async with session_factory() as session:
            return await run_vpn_lifecycle_maintenance(session, now=now, batch_size=1)

    first_cycle = asyncio.create_task(invoke_cycle())
    await asyncio.wait_for(entered.wait(), timeout=1)
    second_cycle = asyncio.create_task(invoke_cycle())
    await asyncio.sleep(0.05)
    assert len(provisioned) == 1
    release.set()
    await asyncio.gather(first_cycle, second_cycle)
    assert len(provisioned) == 2
    assert provisioned[0] != provisioned[1]


@pytest.mark.asyncio
async def test_control_runtime_schedules_vpn_lifecycle(session_factory, monkeypatch):
    async with session_factory() as session:
        worker = _vpn_worker("scheduled")
        # Keep this fixture out of the unrelated heartbeat supervisor path. SQLite
        # drops timezone metadata, while this test only verifies lifecycle cadence.
        worker.status = "provisioning"
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, expires_at=utcnow() + timedelta(days=1))
        key = VpnAccessKey(subscription_id=subscription.id, status="pending_sync")
        session.add(key)
        await session.commit()
        key_id = key.id

    async def fake_provision(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        access_key.worker_id = worker.id
        access_key.status = "active"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", fake_provision)
    settings = Settings(
        DISCOVERY_ENABLED=False,
        VPN_LIFECYCLE_ENABLED=True,
        VPN_LIFECYCLE_INTERVAL_SECONDS=60,
        VPN_LIFECYCLE_BATCH_SIZE=50,
    )
    orchestrator = ControlRuntimeOrchestrator(session_factory, settings=settings)

    await orchestrator.run_cycle()
    assert orchestrator._vpn_lifecycle_task is not None
    await orchestrator._vpn_lifecycle_task

    async with session_factory() as session:
        stored_key = await session.get(VpnAccessKey, key_id)
    assert stored_key is not None
    assert stored_key.status == "active"


@pytest.mark.asyncio
async def test_control_runtime_does_not_await_vpn_lifecycle_inline(session_factory, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_lifecycle(db, *, now=None, batch_size=50, key_timeout_seconds=30):
        del db, now, batch_size, key_timeout_seconds
        started.set()
        await release.wait()
        return {}

    monkeypatch.setattr("app.services.control_runtime.run_vpn_lifecycle_maintenance", slow_lifecycle)
    settings = Settings(
        DISCOVERY_ENABLED=False,
        VPN_LIFECYCLE_ENABLED=True,
        VPN_LIFECYCLE_INTERVAL_SECONDS=60,
        VPN_LIFECYCLE_BATCH_SIZE=50,
    )
    orchestrator = ControlRuntimeOrchestrator(session_factory, settings=settings)

    cycle = asyncio.create_task(orchestrator.run_cycle())
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.wait_for(asyncio.shield(cycle), timeout=0.2)
    assert orchestrator._vpn_lifecycle_task is not None
    assert not orchestrator._vpn_lifecycle_task.done()

    release.set()
    await orchestrator._vpn_lifecycle_task


@pytest.mark.asyncio
async def test_control_runtime_cancels_lifecycle_at_cycle_timeout(session_factory, monkeypatch):
    cancelled = asyncio.Event()

    async def hung_lifecycle(db, *, now=None, batch_size=50, key_timeout_seconds=30):
        del db, now, batch_size, key_timeout_seconds
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr("app.services.control_runtime.run_vpn_lifecycle_maintenance", hung_lifecycle)
    settings = Settings(
        DISCOVERY_ENABLED=False,
        VPN_LIFECYCLE_ENABLED=True,
        VPN_LIFECYCLE_INTERVAL_SECONDS=60,
        VPN_LIFECYCLE_BATCH_SIZE=50,
        VPN_LIFECYCLE_CYCLE_TIMEOUT_SECONDS=0.05,
    )
    orchestrator = ControlRuntimeOrchestrator(session_factory, settings=settings)

    await orchestrator.run_cycle()
    assert orchestrator._vpn_lifecycle_task is not None
    await asyncio.wait_for(orchestrator._vpn_lifecycle_task, timeout=1)

    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_portal_cleanup_failure_does_not_prevent_key_maintenance(
    session_factory,
    monkeypatch,
):
    lifecycle_completed = asyncio.Event()

    async def successful_lifecycle(db, *, now=None, batch_size=50, key_timeout_seconds=30):
        del db, now, batch_size, key_timeout_seconds
        lifecycle_completed.set()
        return {}

    async def failed_cleanup(db, *, now=None):
        del db, now
        raise RuntimeError("sensitive cleanup failure")

    monkeypatch.setattr(
        "app.services.control_runtime.run_vpn_lifecycle_maintenance",
        successful_lifecycle,
    )
    monkeypatch.setattr(
        "app.services.control_runtime.cleanup_expired_portal_auth",
        failed_cleanup,
    )
    orchestrator = ControlRuntimeOrchestrator(
        session_factory,
        settings=Settings(DISCOVERY_ENABLED=False, VPN_LIFECYCLE_ENABLED=True),
    )

    await orchestrator._run_vpn_lifecycle(datetime(2026, 9, 20, 12, 0, tzinfo=UTC))

    assert lifecycle_completed.is_set()


@pytest.mark.asyncio
async def test_stalled_portal_cleanup_commit_is_bounded_and_allows_next_lifecycle(
    session_factory,
    monkeypatch,
):
    lifecycle_runs = 0
    session_calls = 0
    stalled_commit_cancelled = asyncio.Event()

    async def successful_lifecycle(db, *, now=None, batch_size=50, key_timeout_seconds=30):
        nonlocal lifecycle_runs
        del db, now, batch_size, key_timeout_seconds
        lifecycle_runs += 1
        return {}

    async def successful_cleanup(db, *, now=None):
        del db, now
        return {}

    def controlled_session_factory():
        nonlocal session_calls
        session_calls += 1
        session = session_factory()
        if session_calls == 2:
            async def stalled_commit():
                try:
                    await asyncio.Event().wait()
                finally:
                    stalled_commit_cancelled.set()

            session.commit = stalled_commit
        return session

    monkeypatch.setattr(
        "app.services.control_runtime.run_vpn_lifecycle_maintenance",
        successful_lifecycle,
    )
    monkeypatch.setattr(
        "app.services.control_runtime.cleanup_expired_portal_auth",
        successful_cleanup,
    )
    monkeypatch.setattr(
        "app.services.control_runtime.PORTAL_AUTH_CLEANUP_TIMEOUT_SECONDS",
        0.05,
    )
    orchestrator = ControlRuntimeOrchestrator(
        controlled_session_factory,
        settings=Settings(DISCOVERY_ENABLED=False, VPN_LIFECYCLE_ENABLED=True),
    )

    await asyncio.wait_for(orchestrator._run_vpn_lifecycle(utcnow()), timeout=1)
    await asyncio.wait_for(orchestrator._run_vpn_lifecycle(utcnow()), timeout=1)

    assert stalled_commit_cancelled.is_set()
    assert lifecycle_runs == 2


@pytest.mark.asyncio
async def test_cycle_timeout_preserves_completed_key_and_advances_queue(
    session_factory,
    monkeypatch,
):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = _vpn_worker("partial-cycle")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(
            session,
            expires_at=now + timedelta(days=1),
            max_devices=2,
        )
        first = VpnAccessKey(
            subscription_id=subscription.id,
            status="pending_sync",
            public_name="first",
        )
        second = VpnAccessKey(
            subscription_id=subscription.id,
            status="pending_sync",
            public_name="second",
        )
        session.add_all([first, second])
        await session.commit()
        first_id = first.id
        second_id = second.id

    second_started = asyncio.Event()

    async def provision_until_cycle_timeout(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        if access_key.id == second_id:
            second_started.set()
            await asyncio.Event().wait()
        access_key.worker_id = worker.id
        access_key.status = "active"
        return access_key

    monkeypatch.setattr(
        "app.services.vpn_lifecycle.provision_vpn_access_key",
        provision_until_cycle_timeout,
    )
    settings = Settings(
        DISCOVERY_ENABLED=False,
        VPN_LIFECYCLE_ENABLED=True,
        VPN_LIFECYCLE_INTERVAL_SECONDS=60,
        VPN_LIFECYCLE_BATCH_SIZE=2,
        VPN_LIFECYCLE_KEY_TIMEOUT_SECONDS=1,
        VPN_LIFECYCLE_CYCLE_TIMEOUT_SECONDS=0.05,
    )
    orchestrator = ControlRuntimeOrchestrator(session_factory, settings=settings)

    await orchestrator._run_vpn_lifecycle(now)
    assert second_started.is_set()

    async with session_factory() as session:
        stored_first = await session.get(VpnAccessKey, first_id)
        stored_second = await session.get(VpnAccessKey, second_id)
    assert stored_first is not None and stored_first.status == "active"
    assert stored_second is not None and stored_second.status == "pending_sync"

    resumed: list[int] = []

    async def provision_remaining(db, access_key, *, subscription=None, worker=None):
        del db, subscription
        resumed.append(access_key.id)
        access_key.worker_id = worker.id
        access_key.status = "active"
        return access_key

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", provision_remaining)
    async with session_factory() as session:
        await run_vpn_lifecycle_maintenance(session, now=now + timedelta(minutes=1), batch_size=1)

    assert resumed == [second_id]


@pytest.mark.asyncio
async def test_lifecycle_times_out_a_hung_key_without_blocking_batch(session_factory, monkeypatch):
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = _vpn_worker("timeout")
        session.add(worker)
        await session.flush()
        subscription = await _subscription(session, expires_at=now + timedelta(days=1))
        key = VpnAccessKey(subscription_id=subscription.id, status="pending_sync")
        session.add(key)
        await session.commit()
        key_id = key.id

    async def hung_provision(db, access_key, *, subscription=None, worker=None):
        del db, access_key, subscription, worker
        await asyncio.Event().wait()

    monkeypatch.setattr("app.services.vpn_lifecycle.provision_vpn_access_key", hung_provision)

    async with session_factory() as session:
        result = await run_vpn_lifecycle_maintenance(
            session,
            now=now,
            batch_size=1,
            key_timeout_seconds=0.05,
        )
        stored_key = await session.get(VpnAccessKey, key_id)

    assert result["failed_keys"] == 1
    assert stored_key is not None
    assert stored_key.status == "pending_sync"
    assert "timed out" in (stored_key.last_error or "")
