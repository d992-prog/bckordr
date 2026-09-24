from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
import inspect
import math
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings
from app.db.base import Base
from app.db import session as session_module
from app.db.models import AppSetting, VpnEndpoint, WorkerNode
from app.services.control_runtime import ControlRuntimeOrchestrator
from app.services.vpn_node_transport import VpnNodeTransportError


RELEASE_ID = "a" * 64
READINESS_KEY = "vpn_friend_beta_release_ready_v1"
PUBLIC_READINESS_KEY = "vpn_public_release_ready_v1"
NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DB_URL": "sqlite+aiosqlite:///./vpn-control-runtime-test.db",
        "VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS": 12.5,
        "VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS": 7_500,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_vpn_control_postgres_engine_options_are_bounded() -> None:
    settings = _settings(DB_URL="postgresql+asyncpg://test:test@localhost/test")

    options = session_module.vpn_control_engine_options(settings)

    assert options == {
        "hide_parameters": True,
        "pool_pre_ping": True,
        "future": True,
        "connect_args": {
            "command_timeout": 12.5,
            "server_settings": {"statement_timeout": "7500"},
        },
    }


def test_vpn_control_sqlite_engine_options_have_no_asyncpg_arguments() -> None:
    options = session_module.vpn_control_engine_options(_settings())

    assert options == {
        "hide_parameters": True,
        "pool_pre_ping": True,
        "future": True,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS", 0),
        ("VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS", -1),
        ("VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS", 0),
        ("VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS", -1),
    ],
)
def test_vpn_control_database_rejects_non_positive_timeouts(
    field: str,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="^vpn_control_database_timeout_invalid$"):
        session_module.create_vpn_control_database(_settings(**{field: value}))


def test_ordinary_database_construction_remains_unmodified() -> None:
    source = inspect.getsource(session_module)

    assert """engine = create_async_engine(
    settings.db_url,
    hide_parameters=True,
    pool_pre_ping=True,
    future=True,
)""" in source
    assert """AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)""" in source


def test_vpn_control_database_builds_one_dedicated_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = object()
    factory = object()
    engine_calls: list[tuple[str, dict[str, object]]] = []
    factory_calls: list[tuple[object, dict[str, object]]] = []

    def _create_engine(url: str, **options: object) -> object:
        engine_calls.append((url, options))
        return engine

    def _create_factory(bind: object, **options: object) -> object:
        factory_calls.append((bind, options))
        return factory

    monkeypatch.setattr(session_module, "create_async_engine", _create_engine)
    monkeypatch.setattr(session_module, "async_sessionmaker", _create_factory)
    settings = _settings(DB_URL="postgresql+asyncpg://test:test@localhost/test")

    database = session_module.create_vpn_control_database(settings)

    assert database.engine is engine
    assert database.session_factory is factory
    assert engine_calls == [
        (
            settings.db_url,
            {
                "hide_parameters": True,
                "pool_pre_ping": True,
                "future": True,
                "connect_args": {
                    "command_timeout": 12.5,
                    "server_settings": {"statement_timeout": "7500"},
                },
            },
        )
    ]
    assert factory_calls == [
        (
            engine,
            {"expire_on_commit": False, "class_": AsyncSession},
        )
    ]


def _runtime_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "DB_URL": "sqlite+aiosqlite:///./vpn-control-runtime-test.db",
        "DISCOVERY_ENABLED": False,
        "VPN_LIFECYCLE_ENABLED": False,
        "VPN_FRIEND_BETA_ENABLED": True,
        "VPN_FRIEND_BETA_RELEASE_ID": RELEASE_ID,
        "VPN_CONTROL_DISPATCH_ENABLED": True,
        "VPN_CONTROL_KNOWN_HOSTS_PATH": "C:/veltrix/known_hosts",
        "VPN_CONTROL_DISPATCH_INTERVAL_SECONDS": 60.0,
        "VPN_CONTROL_FINALIZE_TIMEOUT_SECONDS": 4.25,
        "VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS": 12.5,
        "VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS": 7_500,
        "VPN_PORTAL_PUBLIC_ACCESS": False,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest_asyncio.fixture
async def runtime_database(
    tmp_path: Path,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], object]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'vpn-control-runtime.sqlite3'}"
    )
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory, engine
    finally:
        await engine.dispose()


async def _seed_runtime_readiness(
    factory: async_sessionmaker[AsyncSession],
    *,
    marker: str | None = RELEASE_ID,
    marker_key: str = READINESS_KEY,
    archived_at: datetime | None = None,
    with_endpoint: bool = True,
    endpoint_status: str = "ready",
    endpoint_security: str = "reality",
    endpoint_verified_at: datetime | None = NOW - timedelta(hours=1),
) -> None:
    async with factory() as db:
        db.add(
            WorkerNode(
                id=1,
                name="vpn-control-worker",
                status="ready",
                is_enabled=True,
                vpn_enabled=True,
                vpn_role="vpn_node",
                vpn_runtime_status="ready",
                ssh_host="192.0.2.10",
                ssh_port=22,
                ssh_username="root",
                ssh_password="test-only-password",
                archived_at=archived_at,
            )
        )
        if marker is not None:
            db.add(AppSetting(key=marker_key, value=marker))
        await db.flush()
        ignore_ready_check = endpoint_status == "ready" and endpoint_verified_at is None
        if ignore_ready_check:
            await db.execute(text("PRAGMA ignore_check_constraints = ON"))
        try:
            if with_endpoint:
                db.add(
                    VpnEndpoint(
                        id=1,
                        worker_id=1,
                        inbound_id=11,
                        public_host="vpn.example.test",
                        port=443,
                        protocol="vless",
                        transport="raw",
                        security=endpoint_security,
                        server_name="cdn.example.test",
                        public_key="A" * 43,
                        short_id="0123456789abcdef",
                        fingerprint="chrome",
                        flow="xtls-rprx-vision",
                        status=endpoint_status,
                        verified_at=endpoint_verified_at,
                    )
                )
            await db.commit()
        finally:
            if ignore_ready_check:
                await db.execute(text("PRAGMA ignore_check_constraints = OFF"))


@pytest.mark.asyncio
async def test_public_only_release_gate_starts_control_dispatch(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory, marker_key=PUBLIC_READINESS_KEY)
    settings = _runtime_settings(
        VPN_FRIEND_BETA_ENABLED=False,
        VPN_FRIEND_BETA_RELEASE_ID="",
        VPN_PUBLIC_TRIAL_ENABLED=True,
        VPN_PUBLIC_TRIAL_RELEASE_ID=RELEASE_ID,
    )
    dispatch_calls: list[object] = []
    dedicated_engine = _FakeDedicatedEngine()
    runtime = _orchestrator(
        factory,
        settings,
        database_factory=lambda _settings: SimpleNamespace(
            engine=dedicated_engine,
            session_factory=object(),
        ),
        dispatcher=lambda *args, **kwargs: dispatch_calls.append((args, kwargs)),
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    assert runtime._vpn_control_dispatch_task is not None
    await runtime._vpn_control_dispatch_task
    await runtime.shutdown()

    assert len(dispatch_calls) == 1


@pytest.mark.parametrize("marker", [None, "b" * 64])
@pytest.mark.asyncio
async def test_public_only_release_gate_requires_exact_marker(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    marker: str | None,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(
        factory,
        marker=marker,
        marker_key=PUBLIC_READINESS_KEY,
    )
    database_calls: list[Settings] = []
    runtime = _orchestrator(
        factory,
        _runtime_settings(
            VPN_FRIEND_BETA_ENABLED=False,
            VPN_FRIEND_BETA_RELEASE_ID="",
            VPN_PUBLIC_TRIAL_ENABLED=True,
            VPN_PUBLIC_TRIAL_RELEASE_ID=RELEASE_ID,
        ),
        database_factory=lambda settings: database_calls.append(settings),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


@pytest.mark.parametrize("release_id", ["", "A" * 64, "a" * 63])
@pytest.mark.asyncio
async def test_public_only_release_gate_requires_valid_release_id(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    release_id: str,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory, marker_key=PUBLIC_READINESS_KEY)
    database_calls: list[Settings] = []
    runtime = _orchestrator(
        factory,
        _runtime_settings(
            VPN_FRIEND_BETA_ENABLED=False,
            VPN_FRIEND_BETA_RELEASE_ID="",
            VPN_PUBLIC_TRIAL_ENABLED=True,
            VPN_PUBLIC_TRIAL_RELEASE_ID=release_id,
        ),
        database_factory=lambda settings: database_calls.append(settings),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


@pytest.mark.asyncio
async def test_control_dispatch_stays_off_when_both_release_paths_are_disabled(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory)
    database_calls: list[Settings] = []
    runtime = _orchestrator(
        factory,
        _runtime_settings(
            VPN_FRIEND_BETA_ENABLED=False,
            VPN_PUBLIC_TRIAL_ENABLED=False,
        ),
        database_factory=lambda settings: database_calls.append(settings),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


class _FakeDedicatedEngine:
    def __init__(self, events: list[str] | None = None) -> None:
        self.dispose_calls = 0
        self._events = events

    async def dispose(self) -> None:
        self.dispose_calls += 1
        if self._events is not None:
            self._events.append("engine_disposed")


def _orchestrator(
    factory: async_sessionmaker[AsyncSession],
    settings: Settings,
    *,
    database_factory,
    dispatcher,
    snapshot_loader,
) -> ControlRuntimeOrchestrator:
    return ControlRuntimeOrchestrator(
        factory,
        settings=settings,
        vpn_control_database_factory=database_factory,
        vpn_control_dispatcher=dispatcher,
        vpn_control_snapshot_loader=snapshot_loader,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("vpn_friend_beta_enabled", False),
        ("vpn_control_dispatch_enabled", False),
        ("vpn_portal_public_access", True),
        ("vpn_friend_beta_release_id", ""),
        ("vpn_friend_beta_release_id", "A" * 64),
        ("vpn_friend_beta_release_id", "a" * 63),
        ("vpn_control_known_hosts_path", ""),
        ("vpn_control_db_command_timeout_seconds", 0),
        ("vpn_control_db_command_timeout_seconds", math.nan),
        ("vpn_control_db_command_timeout_seconds", math.inf),
        ("vpn_control_db_statement_timeout_ms", 0),
        ("vpn_control_dispatch_interval_seconds", 0),
        ("vpn_control_dispatch_interval_seconds", math.nan),
        ("vpn_control_dispatch_interval_seconds", math.inf),
        ("vpn_control_finalize_timeout_seconds", 0),
        ("vpn_control_finalize_timeout_seconds", math.nan),
        ("vpn_control_finalize_timeout_seconds", math.inf),
    ],
)
@pytest.mark.asyncio
async def test_vpn_control_dispatch_fails_closed_before_dedicated_engine_creation(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    field: str,
    value: object,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory)
    settings = _runtime_settings()
    setattr(settings, field, value)
    database_calls: list[Settings] = []
    dispatch_calls: list[object] = []

    runtime = _orchestrator(
        factory,
        settings,
        database_factory=lambda value: database_calls.append(value),
        dispatcher=lambda *args, **kwargs: dispatch_calls.append((args, kwargs)),
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []
    assert dispatch_calls == []


@pytest.mark.parametrize("marker", [None, "b" * 64])
@pytest.mark.asyncio
async def test_env_flags_without_exact_private_marker_never_create_an_engine(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    marker: str | None,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory, marker=marker)
    database_calls: list[Settings] = []

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda value: database_calls.append(value),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


@pytest.mark.asyncio
async def test_archived_worker_never_enables_dispatch(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory, archived_at=NOW)
    database_calls: list[Settings] = []

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda value: database_calls.append(value),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


@pytest.mark.parametrize(
    "seed_overrides",
    [
        {"with_endpoint": False},
        {"endpoint_status": "staged"},
        {"endpoint_verified_at": None},
        {"endpoint_security": "tls"},
    ],
    ids=["no-endpoint", "not-ready", "not-verified", "not-reality"],
)
@pytest.mark.asyncio
async def test_endpoint_readiness_gates_independently_prevent_dispatch(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    seed_overrides: dict[str, object],
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory, **seed_overrides)
    database_calls: list[Settings] = []
    dispatch_calls: list[object] = []
    snapshot_calls: list[tuple[WorkerNode, Path]] = []

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda value: database_calls.append(value),
        dispatcher=lambda *args, **kwargs: dispatch_calls.append((args, kwargs)),
        snapshot_loader=lambda worker, path: snapshot_calls.append((worker, path)),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []
    assert dispatch_calls == []
    assert snapshot_calls == []


@pytest.mark.asyncio
async def test_invalid_transport_snapshot_never_creates_dedicated_engine(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory)
    database_calls: list[Settings] = []

    def _invalid_snapshot(_worker: WorkerNode, _path: Path) -> object:
        raise VpnNodeTransportError("preflight")

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda value: database_calls.append(value),
        dispatcher=lambda *args, **kwargs: None,
        snapshot_loader=_invalid_snapshot,
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert database_calls == []


@pytest.mark.asyncio
async def test_vpn_control_dispatch_is_lazy_sequential_and_interval_bounded(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, ordinary_engine = runtime_database
    await _seed_runtime_readiness(factory)
    settings = _runtime_settings()
    database_calls: list[Settings] = []
    dispatch_calls: list[tuple[object, Path, float]] = []
    snapshot_calls: list[tuple[WorkerNode, Path]] = []
    readiness_queries: list[str] = []
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()
    dedicated_engine = _FakeDedicatedEngine()
    dedicated_factory = object()

    def _capture_query(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        if all(name in statement for name in ("app_settings", "vpn_endpoints", "worker_nodes")):
            readiness_queries.append(statement)

    def _database_factory(value: Settings) -> object:
        database_calls.append(value)
        return SimpleNamespace(
            engine=dedicated_engine,
            session_factory=dedicated_factory,
        )

    def _snapshot_loader(worker: WorkerNode, path: Path) -> object:
        snapshot_calls.append((worker, path))
        return object()

    async def _dispatcher(
        session_factory: object,
        known_hosts_path: Path,
        *,
        finalize_timeout: float,
    ) -> bool:
        dispatch_calls.append(
            (session_factory, known_hosts_path, finalize_timeout)
        )
        dispatch_started.set()
        await release_dispatch.wait()
        return False

    event.listen(ordinary_engine.sync_engine, "before_cursor_execute", _capture_query)
    runtime = _orchestrator(
        factory,
        settings,
        database_factory=_database_factory,
        dispatcher=_dispatcher,
        snapshot_loader=_snapshot_loader,
    )
    try:
        assert database_calls == []
        await runtime.run_cycle()
        await asyncio.wait_for(dispatch_started.wait(), timeout=1)
        await runtime.run_cycle()

        assert database_calls == [settings]
        assert len(dispatch_calls) == 1
        assert dispatch_calls == [
            (
                dedicated_factory,
                Path(settings.vpn_control_known_hosts_path),
                settings.vpn_control_finalize_timeout_seconds,
            )
        ]
        assert len(snapshot_calls) == 1
        assert all(worker.id == 1 for worker, _path in snapshot_calls)
        assert all(
            path == Path(settings.vpn_control_known_hosts_path)
            for _worker, path in snapshot_calls
        )
        assert len(readiness_queries) == 1

        release_dispatch.set()
        assert runtime._vpn_control_dispatch_task is not None
        await runtime._vpn_control_dispatch_task
        await runtime.run_cycle()
        assert len(dispatch_calls) == 1
    finally:
        event.remove(
            ordinary_engine.sync_engine,
            "before_cursor_execute",
            _capture_query,
        )
        release_dispatch.set()
        await runtime.shutdown()

    assert dedicated_engine.dispose_calls == 1


@pytest.mark.asyncio
async def test_completed_dispatch_cannot_be_rescheduled_in_the_same_interval(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _engine = runtime_database
    dispatch_calls = 0

    async def _dispatcher(*_args, **_kwargs) -> bool:
        nonlocal dispatch_calls
        dispatch_calls += 1
        return False

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda _settings: SimpleNamespace(
            engine=_FakeDedicatedEngine(),
            session_factory=object(),
        ),
        dispatcher=_dispatcher,
        snapshot_loader=lambda worker, path: object(),
    )

    runtime._start_vpn_control_dispatch(NOW)
    assert runtime._vpn_control_dispatch_task is not None
    await runtime._vpn_control_dispatch_task
    runtime._start_vpn_control_dispatch(NOW)
    await asyncio.sleep(0)
    await runtime.shutdown()

    assert dispatch_calls == 1


@pytest.mark.asyncio
async def test_shutdown_awaits_dispatch_cancellation_before_engine_disposal(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
) -> None:
    factory, _ordinary_engine = runtime_database
    await _seed_runtime_readiness(factory)
    events: list[str] = []
    started = asyncio.Event()
    dedicated_engine = _FakeDedicatedEngine(events)

    async def _dispatcher(*_args, **_kwargs) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            events.append("dispatch_cancelled")

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda _settings: SimpleNamespace(
            engine=dedicated_engine,
            session_factory=object(),
        ),
        dispatcher=_dispatcher,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await asyncio.wait_for(started.wait(), timeout=1)
    await runtime.shutdown()

    assert events == ["dispatch_cancelled", "engine_disposed"]


@pytest.mark.asyncio
async def test_dispatch_exception_logs_only_a_static_message(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    caplog: pytest.LogCaptureFixture,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory)
    secret = "credential=do-not-log-this"

    async def _dispatcher(*_args, **_kwargs) -> bool:
        raise RuntimeError(secret)

    runtime = _orchestrator(
        factory,
        _runtime_settings(),
        database_factory=lambda _settings: SimpleNamespace(
            engine=_FakeDedicatedEngine(),
            session_factory=object(),
        ),
        dispatcher=_dispatcher,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    assert runtime._vpn_control_dispatch_task is not None
    await runtime._vpn_control_dispatch_task
    await runtime.shutdown()

    messages = [record.getMessage() for record in caplog.records]
    assert "VPN control dispatch failed" in messages
    assert secret not in caplog.text


@pytest.mark.asyncio
async def test_ready_notifications_flag_schedules_at_most_one_injected_sender(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _engine = runtime_database
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[tuple[object, Settings, object]] = []

    async def sender(_settings: Settings, _chat_id: str, _text: str) -> None:
        raise AssertionError("sender is passed through, not called by the runtime")

    async def deliver(session_factory, settings, injected_sender) -> bool:
        calls.append((session_factory, settings, injected_sender))
        started.set()
        await release.wait()
        return False

    monkeypatch.setattr(
        "app.services.control_runtime.deliver_next_ready_notice",
        deliver,
    )
    settings = _runtime_settings(
        VPN_FRIEND_BETA_ENABLED=False,
        VPN_CONTROL_DISPATCH_ENABLED=False,
        VPN_READY_NOTIFICATIONS_ENABLED=True,
    )
    runtime = ControlRuntimeOrchestrator(
        factory,
        settings=settings,
        vpn_ready_sender=sender,
    )

    await runtime.run_cycle()
    await asyncio.wait_for(started.wait(), timeout=1)
    first_task = runtime._vpn_ready_notification_task
    await runtime.run_cycle()

    assert first_task is not None
    assert runtime._vpn_ready_notification_task is first_task
    assert calls == [(factory, settings, sender)]

    release.set()
    await first_task
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_ready_notifications_disabled_never_schedules(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _engine = runtime_database
    calls = 0

    async def deliver(*_args) -> bool:
        nonlocal calls
        calls += 1
        return False

    monkeypatch.setattr(
        "app.services.control_runtime.deliver_next_ready_notice",
        deliver,
    )
    runtime = ControlRuntimeOrchestrator(
        factory,
        settings=_runtime_settings(
            VPN_FRIEND_BETA_ENABLED=False,
            VPN_CONTROL_DISPATCH_ENABLED=False,
            VPN_READY_NOTIFICATIONS_ENABLED=False,
        ),
    )

    await runtime.run_cycle()
    await runtime.shutdown()

    assert runtime._vpn_ready_notification_task is None
    assert calls == 0


@pytest.mark.asyncio
async def test_ready_notification_does_not_block_strict_dispatch(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _engine = runtime_database
    await _seed_runtime_readiness(factory)
    readiness_started = asyncio.Event()
    dispatch_started = asyncio.Event()
    release = asyncio.Event()

    async def deliver(*_args) -> bool:
        readiness_started.set()
        await release.wait()
        return False

    async def dispatcher(*_args, **_kwargs) -> bool:
        dispatch_started.set()
        await release.wait()
        return False

    monkeypatch.setattr(
        "app.services.control_runtime.deliver_next_ready_notice",
        deliver,
    )
    runtime = _orchestrator(
        factory,
        _runtime_settings(VPN_READY_NOTIFICATIONS_ENABLED=True),
        database_factory=lambda _settings: SimpleNamespace(
            engine=_FakeDedicatedEngine(),
            session_factory=object(),
        ),
        dispatcher=dispatcher,
        snapshot_loader=lambda worker, path: object(),
    )

    await runtime.run_cycle()
    await asyncio.wait_for(readiness_started.wait(), timeout=1)
    await asyncio.wait_for(dispatch_started.wait(), timeout=1)

    release.set()
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_shutdown_cancels_and_awaits_ready_notification_task(
    runtime_database: tuple[async_sessionmaker[AsyncSession], object],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory, _engine = runtime_database
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def deliver(*_args) -> bool:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(
        "app.services.control_runtime.deliver_next_ready_notice",
        deliver,
    )
    runtime = ControlRuntimeOrchestrator(
        factory,
        settings=_runtime_settings(
            VPN_FRIEND_BETA_ENABLED=False,
            VPN_CONTROL_DISPATCH_ENABLED=False,
            VPN_READY_NOTIFICATIONS_ENABLED=True,
        ),
    )

    await runtime.run_cycle()
    await asyncio.wait_for(started.wait(), timeout=1)
    await runtime.shutdown()

    assert cancelled.is_set()
    assert runtime._vpn_ready_notification_task is None


def _validated_postgres_url() -> str:
    raw_url = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not raw_url:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for real PostgreSQL coverage")
    url = make_url(raw_url)
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.database == "veltrix_portal_test"
        and url.port is not None
        and url.port != 5432
        and not url.query
    ):
        raise ValueError("unsafe VPN_PORTAL_TEST_PG_URL")
    return url.render_as_string(hide_password=False)


@pytest.mark.asyncio
async def test_postgres_cancelled_dispatch_query_rolls_back_and_returns_clean_connection() -> None:
    database_url = _validated_postgres_url()
    table_name = f"vpn_control_cancel_{uuid4().hex}"
    lock_key = uuid4().int % (2**63 - 1)
    base_engine = create_async_engine(database_url, hide_parameters=True)
    settings = _runtime_settings(
        DB_URL=database_url,
        VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS=5.0,
        VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS=10_000,
    )
    database = session_module.create_vpn_control_database(settings)

    try:
        async with base_engine.begin() as connection:
            await connection.execute(
                text(f'CREATE TABLE "{table_name}" (value INTEGER NOT NULL)')
            )

        blocker = await base_engine.connect()
        blocker_transaction = await blocker.begin()
        try:
            await blocker.execute(
                text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
            )
            async with database.session_factory() as db:
                backend_pid = await db.scalar(text("SELECT pg_backend_pid()"))
                await db.execute(
                    text(f'INSERT INTO "{table_name}" (value) VALUES (1)')
                )
                blocked_query = asyncio.create_task(
                    db.execute(
                        text("SELECT pg_advisory_xact_lock(:key)"),
                        {"key": lock_key},
                    )
                )
                for _ in range(100):
                    is_blocked = await blocker.scalar(
                        text(
                            "SELECT cardinality(pg_blocking_pids(:pid)) > 0"
                        ),
                        {"pid": backend_pid},
                    )
                    if is_blocked:
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("dispatcher query never became blocked")

                blocked_query.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await blocked_query
                await db.rollback()
            assert database.engine.pool.checkedout() == 0
        finally:
            await blocker_transaction.rollback()
            await blocker.close()

        await asyncio.sleep(0.05)
        async with database.session_factory() as db:
            assert await db.scalar(text("SELECT 1")) == 1
            assert await db.scalar(text(f'SELECT count(*) FROM "{table_name}"')) == 0
        assert database.engine.pool.checkedout() == 0
    finally:
        await database.engine.dispose()
        try:
            async with base_engine.begin() as connection:
                await connection.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        finally:
            await base_engine.dispose()
