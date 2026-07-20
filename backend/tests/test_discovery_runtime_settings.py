import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.services.app_settings import get_discovery_runtime_settings, set_discovery_runtime_settings


@pytest.mark.asyncio
async def test_discovery_runtime_settings_default_to_environment_values():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    settings = Settings(
        DB_URL="sqlite+aiosqlite://",
        OWNER_PASSWORD="password",
        DISCOVERY_BATCH_SIZE=25,
        DISCOVERY_TIMEOUT_SECONDS=7.5,
        DISCOVERY_WORKER_ENABLED=True,
        DISCOVERY_LOCAL_FALLBACK_ENABLED=False,
        DISCOVERY_WORKER_TASK_STALE_SECONDS=240,
        WORKER_DISCOVERY_CONCURRENCY=9,
        WORKER_DISCOVERY_POLL_INTERVAL_SECONDS=1.5,
    )
    async with session_factory() as session:
        runtime_settings = await get_discovery_runtime_settings(session, settings)

    assert runtime_settings.discovery_batch_size == 25
    assert runtime_settings.discovery_timeout_seconds == 7.5
    assert runtime_settings.discovery_worker_enabled is True
    assert runtime_settings.discovery_local_fallback_enabled is False
    assert runtime_settings.discovery_worker_task_stale_seconds == 240
    assert runtime_settings.worker_discovery_concurrency == 9
    assert runtime_settings.worker_discovery_poll_interval_seconds == 1.5


@pytest.mark.asyncio
async def test_discovery_runtime_settings_can_be_overridden_from_database():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    settings = Settings(DB_URL="sqlite+aiosqlite://", OWNER_PASSWORD="password", DISCOVERY_BATCH_SIZE=10)
    async with session_factory() as session:
        saved = await set_discovery_runtime_settings(
            session,
            settings,
            {
                "discovery_batch_size": 80,
                "discovery_timeout_seconds": 3.5,
                "discovery_worker_enabled": False,
                "discovery_local_fallback_enabled": True,
                "discovery_worker_task_stale_seconds": 90,
                "worker_discovery_concurrency": 16,
                "worker_discovery_poll_interval_seconds": 0.75,
            },
        )
        await session.commit()
        loaded = await get_discovery_runtime_settings(session, settings)

    assert saved == loaded
    assert loaded.discovery_batch_size == 80
    assert loaded.discovery_timeout_seconds == 3.5
    assert loaded.discovery_worker_enabled is False
    assert loaded.discovery_local_fallback_enabled is True
    assert loaded.discovery_worker_task_stale_seconds == 90
    assert loaded.worker_discovery_concurrency == 16
    assert loaded.worker_discovery_poll_interval_seconds == 0.75
