from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.models import AppSetting
from app.services import control_runtime as runtime_module

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(runtime_module, "utcnow", lambda: NOW)

    async def isolated_cleanup(*_args, **_kwargs):
        return 0

    # Collector tests do not need a SQLite connection cancelled while it is opening.
    monkeypatch.setattr(runtime_module, "cleanup_usage_retention", isolated_cleanup)
    monkeypatch.setattr(runtime_module, "expire_abuse_windows", isolated_cleanup)


@pytest_asyncio.fixture
async def factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'usage-runtime.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "DISCOVERY_ENABLED": False,
            "VPN_LIFECYCLE_ENABLED": False,
            "VPN_USAGE_ENABLED": True,
            "VPN_CONTROL_KNOWN_HOSTS_PATH": "C:/synthetic/known_hosts",
            **overrides,
        },
    )


def orchestrator(factory, collector, *, config=None):
    disposed = []

    async def dispose():
        disposed.append(True)

    database_calls = []

    def database_factory(config):
        database_calls.append(config)
        return SimpleNamespace(
            engine=SimpleNamespace(dispose=dispose),
            session_factory=factory,
        )

    runtime = runtime_module.ControlRuntimeOrchestrator(
        factory,
        settings=config if config is not None else settings(),
        vpn_control_database_factory=database_factory,
        vpn_usage_collector=collector,
    )
    return runtime, database_calls, disposed


def test_usage_is_disabled_by_default_with_only_one_new_setting():
    config = Settings(_env_file=None)
    assert config.vpn_usage_enabled is False
    assert settings().vpn_usage_enabled is True
    assert {
        name for name in Settings.model_fields if name.startswith("vpn_usage_")
    } == {
        "vpn_usage_enabled",
    }
    assert runtime_module.VPN_USAGE_INTERVAL_SECONDS == 60
    assert runtime_module.VPN_USAGE_CONCURRENCY == 4
    assert runtime_module.VPN_USAGE_TIMEOUT_SECONDS == 35


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"VPN_USAGE_ENABLED": False},
        {"VPN_CONTROL_KNOWN_HOSTS_PATH": " "},
    ],
)
async def test_usage_disabled_or_unpinned_still_expires_source_samples(
    factory, overrides, monkeypatch
):
    from test_vpn_usage import seed

    from app.db.models import VpnKeySourceSample
    from app.services.vpn_usage import cleanup_usage_retention

    monkeypatch.setattr(
        runtime_module, "cleanup_usage_retention", cleanup_usage_retention
    )

    worker_id, _, keys = await seed(factory)
    expired = NOW - timedelta(hours=24, seconds=1)
    async with factory() as db, db.begin():
        db.add(
            VpnKeySourceSample(
                access_key_id=keys[0],
                worker_id=worker_id,
                minute_at=expired,
                observed_at=expired,
                state="ok",
                key_epoch="synthetic",
                source_hashes=[],
            )
        )

    async def collector(*args, **kwargs):
        pytest.fail("disabled usage must not collect")

    runtime, database_calls, disposed = orchestrator(
        factory,
        collector,
        config=settings(**overrides),
    )
    try:
        await runtime.run_cycle()
        assert runtime._vpn_usage_cleanup_task is not None
        await runtime._vpn_usage_cleanup_task
        assert not runtime._vpn_usage_tasks
        async with factory() as db:
            assert await db.get(VpnKeySourceSample, keys[0]) is None
    finally:
        await runtime.shutdown()
    assert len(database_calls) == 1 and disposed == [True]


@pytest.mark.asyncio
async def test_usage_drains_due_nodes_four_at_a_time_after_commit_and_once_per_minute(
    factory,
    monkeypatch,
):
    current = [NOW]
    monkeypatch.setattr(runtime_module, "utcnow", lambda: current[0])
    entered, release = asyncio.Event(), asyncio.Event()
    active = calls = maximum = 0
    cleanup_calls = []

    async def cleanup(db_factory, *, now):
        assert db_factory is factory
        cleanup_calls.append(now)

    monkeypatch.setattr(runtime_module, "cleanup_usage_retention", cleanup)

    async def committed_marker(session):
        if (
            await session.scalar(
                select(AppSetting).where(AppSetting.key == "usage-core-commit")
            )
            is None
        ):
            session.add(AppSetting(key="usage-core-commit", value="committed"))

    monkeypatch.setattr(
        runtime_module, "recompute_worker_domain_counts", committed_marker
    )

    async def collector(db_factory, known_hosts, **kwargs):
        nonlocal active, calls, maximum
        assert db_factory is factory
        assert str(known_hosts).replace("\\", "/") == "C:/synthetic/known_hosts"
        async with factory() as db:
            assert (
                await db.scalar(
                    select(AppSetting.value).where(
                        AppSetting.key == "usage-core-commit",
                    )
                )
                == "committed"
            )
        calls += 1
        active += 1
        maximum = max(maximum, active)
        if active == 4:
            entered.set()
        try:
            await release.wait()
        finally:
            active -= 1
        return calls <= 8

    runtime, database_calls, disposed = orchestrator(factory, collector)
    try:
        await runtime.run_cycle()
        await asyncio.wait_for(entered.wait(), 2)
        assert len(runtime._vpn_usage_tasks) == 4
        # The normal orchestrator and issuance database remain usable during blocked SSH.
        await asyncio.wait_for(runtime.run_cycle(), 2)
        async with factory() as db:
            db.add(AppSetting(key="issuance-progress", value="yes"))
            await asyncio.wait_for(db.commit(), 1)
        assert calls == 4
        release.set()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        await runtime.run_cycle()
        assert calls >= 8 and maximum == 4
        old_calls = calls
        assert cleanup_calls == [NOW]
        current[0] += timedelta(seconds=59)
        await runtime.run_cycle()
        assert calls == old_calls
        current[0] += timedelta(seconds=1)
        await runtime.run_cycle()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        assert calls > old_calls
        assert cleanup_calls == [NOW, current[0]]
    finally:
        release.set()
        await runtime.shutdown()
    assert len(database_calls) == 1 and disposed == [True]


@pytest.mark.asyncio
async def test_usage_timeout_cancels_and_awaits_cleanup_before_replacement(
    factory,
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        runtime_module, "VPN_USAGE_TIMEOUT_SECONDS", 0.02, raising=False
    )
    entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations = 0

    async def collector(*args, **kwargs):
        nonlocal cancellations
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations += 1
            if cancellations == 4:
                cancelled.set()
            await finish.wait()
            return False

    runtime, _, disposed = orchestrator(factory, collector)
    try:
        await runtime.run_cycle()
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(cancelled.wait(), 2)
        assert len(runtime._vpn_usage_tasks) == 4
        assert any(not task.done() for task in runtime._vpn_usage_tasks)
        await runtime.run_cycle()
        assert cancellations == 4 and disposed == []
        finish.set()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        assert "VPN usage collection timed out" in caplog.text
    finally:
        finish.set()
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_usage_shutdown_awaits_all_late_cleanup_before_disposing_database(
    factory,
):
    entered, cancelled, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
    cancellations = 0

    async def collector(*args, **kwargs):
        nonlocal cancellations
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations += 1
            if cancellations == 4:
                cancelled.set()
            await finish.wait()
            return False

    runtime, _, disposed = orchestrator(factory, collector)
    await runtime.run_cycle()
    await asyncio.wait_for(entered.wait(), 2)
    stopped = asyncio.create_task(runtime.shutdown())
    await asyncio.wait_for(cancelled.wait(), 2)
    try:
        assert not stopped.done() and disposed == []
    finally:
        finish.set()
        await asyncio.wait_for(stopped, 2)
    assert not runtime._vpn_usage_tasks and disposed == [True]


@pytest.mark.asyncio
async def test_shutdown_signals_usage_cancellation_before_waiting_for_other_tasks(
    factory,
):
    entered, cancelled = asyncio.Event(), asyncio.Event()
    other_cancelled, other_finish = asyncio.Event(), asyncio.Event()
    count = 0

    async def collector(*args, **kwargs):
        nonlocal count
        entered.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            count += 1
            if count == 4:
                cancelled.set()
            return False

    async def other_background_job():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            other_cancelled.set()
            await other_finish.wait()

    runtime, _, _ = orchestrator(factory, collector)
    await runtime.run_cycle()
    await asyncio.wait_for(entered.wait(), 2)
    runtime._vpn_broadcast_task = asyncio.create_task(other_background_job())
    await asyncio.sleep(0)
    stopped = asyncio.create_task(runtime.shutdown())
    try:
        await asyncio.wait_for(other_cancelled.wait(), 2)
        await asyncio.wait_for(cancelled.wait(), 0.1)
        assert not stopped.done()
    finally:
        other_finish.set()
        await asyncio.wait_for(stopped, 2)


@pytest.mark.asyncio
async def test_usage_errors_are_reaped_without_private_exception_details(
    factory, caplog
):
    async def collector(*args, **kwargs):
        raise RuntimeError("private stdout credential and address")

    runtime, _, _ = orchestrator(factory, collector)
    try:
        await runtime.run_cycle()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        assert "VPN usage collection failed" in caplog.text
        assert "private stdout" not in caplog.text
        assert not any(record.exc_info for record in caplog.records)
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_second", [7, 45])
async def test_usage_cycles_follow_utc_minutes_instead_of_process_start_time(
    factory,
    monkeypatch,
    startup_second,
):
    current = [NOW + timedelta(seconds=startup_second)]
    monkeypatch.setattr(runtime_module, "utcnow", lambda: current[0])
    calls = []

    async def collector(*args, **kwargs):
        calls.append(current[0])
        return False

    runtime, _, _ = orchestrator(factory, collector)
    try:
        await runtime.run_cycle()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        assert len(calls) == (4 if startup_second == 7 else 0)
        current[0] = NOW + timedelta(minutes=1)
        await runtime.run_cycle()
        await asyncio.gather(*runtime._vpn_usage_tasks)
        assert calls[-4:] == [current[0]] * 4
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS": 0},
        {"VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS": float("inf")},
        {"VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS": float("nan")},
        {"VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS": 0},
    ],
)
async def test_usage_requires_bounded_dedicated_database_options(factory, overrides):
    async def collector(*args, **kwargs):
        pytest.fail("unsafe database bounds must not collect")

    runtime, database_calls, disposed = orchestrator(
        factory, collector, config=settings(**overrides)
    )
    await runtime.run_cycle()
    await runtime.shutdown()
    assert database_calls == disposed == []


@pytest.mark.asyncio
async def test_usage_database_start_failure_is_safe_and_does_not_abort_core_cycle(
    factory,
    caplog,
):
    async def collector(*args, **kwargs):
        pytest.fail("failed database must not collect")

    runtime, _, _ = orchestrator(factory, collector)

    def unavailable_database(config):
        raise RuntimeError("private database connection details")

    runtime._vpn_control_database_factory = unavailable_database
    await runtime.run_cycle()
    async with factory() as db:
        db.add(AppSetting(key="issuance-after-failure", value="yes"))
        await db.commit()
    await runtime.shutdown()
    assert "VPN usage database unavailable" in caplog.text
    assert "private database" not in caplog.text
    assert not any(record.exc_info for record in caplog.records)


@pytest.mark.asyncio
async def test_usage_cleanup_errors_are_safe_and_independent_of_collection(
    factory,
    monkeypatch,
    caplog,
):
    calls = []

    async def collector(*args, **kwargs):
        calls.append(True)
        return False

    async def cleanup(*args, **kwargs):
        raise RuntimeError("private source and database data")

    monkeypatch.setattr(runtime_module, "cleanup_usage_retention", cleanup)
    runtime, _, _ = orchestrator(factory, collector)
    try:
        await runtime.run_cycle()
        await asyncio.gather(*runtime._vpn_usage_tasks, runtime._vpn_usage_cleanup_task)
        assert len(calls) == 4
        assert "VPN usage retention cleanup failed" in caplog.text
        assert "private source" not in caplog.text
        assert not any(record.exc_info for record in caplog.records)
    finally:
        await runtime.shutdown()
