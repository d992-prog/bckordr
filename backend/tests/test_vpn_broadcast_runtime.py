import asyncio
from types import SimpleNamespace

import pytest
from test_vpn_control_runtime import _FakeDedicatedEngine, _runtime_settings
from test_vpn_control_runtime import (
    runtime_database as runtime_database,  # noqa: PLC0414
)

from app.core.config import Settings
from app.db.models import AppSetting
from app.services.control_runtime import ControlRuntimeOrchestrator
from app.services.vpn_bot_chats import REGISTRY_BOT_KEY


@pytest.fixture(autouse=True)
def isolate_news_dispatch_from_usage_retention(monkeypatch):
    # News-only shutdown tests must not cancel an unrelated SQLite connection opener.
    monkeypatch.setattr(
        ControlRuntimeOrchestrator, "_start_vpn_usage", lambda _self, _now: None
    )


def test_news_sender_is_disabled_by_default():
    assert Settings(_env_file=None).vpn_broadcasts_enabled is False


def runtime(factory, dispatcher, *, enabled=True):
    return ControlRuntimeOrchestrator(
        factory,
        settings=_runtime_settings(
            VPN_BROADCASTS_ENABLED=enabled,
            VPN_TELEGRAM_BOT_TOKEN="111:synthetic-only",
            VPN_CONTROL_DISPATCH_ENABLED=False,
            VPN_FRIEND_BETA_ENABLED=False,
        ),
        vpn_control_database_factory=lambda _: SimpleNamespace(
            engine=_FakeDedicatedEngine(), session_factory=factory
        ),
        vpn_broadcast_dispatcher=dispatcher,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker,enabled,expected",
    [
        (None, True, 0),
        ("222", True, 0),
        ("backfilling:111", True, 0),
        ("111", False, 0),
        ("111", True, 1),
    ],
)
async def test_runtime_starts_only_for_enabled_verified_registry(
    runtime_database, marker, enabled, expected
):
    factory, _ = runtime_database
    if marker is not None:
        async with factory() as db:
            db.add(AppSetting(key=REGISTRY_BOT_KEY, value=marker))
            await db.commit()
    calls = []

    async def dispatch(*_args):
        calls.append(True)
        return False

    orchestrator = runtime(factory, dispatch, enabled=enabled)
    await orchestrator.run_cycle()
    if orchestrator._vpn_broadcast_task is not None:
        await orchestrator._vpn_broadcast_task
    await orchestrator.shutdown()
    assert len(calls) == expected


@pytest.mark.asyncio
async def test_background_turn_has_twenty_job_limit(runtime_database):
    factory, _ = runtime_database
    async with factory() as db:
        db.add(AppSetting(key=REGISTRY_BOT_KEY, value="111"))
        await db.commit()
    calls = []

    async def dispatch(*_args):
        calls.append(True)
        return True

    orchestrator = runtime(factory, dispatch)
    await orchestrator.run_cycle()
    await orchestrator._vpn_broadcast_task
    await orchestrator.shutdown()
    assert len(calls) == 20


@pytest.mark.asyncio
async def test_one_background_sender_does_not_block_orchestration_and_shutdown_awaits_it(
    runtime_database,
):
    factory, _ = runtime_database
    async with factory() as db:
        db.add(AppSetting(key=REGISTRY_BOT_KEY, value="111"))
        await db.commit()
    started, stopped = asyncio.Event(), asyncio.Event()
    calls = []

    async def dispatch(*_args):
        calls.append(True)
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    orchestrator = runtime(factory, dispatch)
    await orchestrator.run_cycle()
    await asyncio.wait_for(started.wait(), 5)
    await asyncio.wait_for(orchestrator.run_cycle(), 5)
    assert len(calls) == 1
    await asyncio.wait_for(orchestrator.shutdown(), 5)
    assert stopped.is_set() and orchestrator._vpn_broadcast_task is None
