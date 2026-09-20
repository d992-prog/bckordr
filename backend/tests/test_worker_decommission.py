from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import WorkerNode


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_worker_archive_timestamp_is_persisted(session_factory):
    archived_at = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
    async with session_factory() as session:
        worker = WorkerNode(name="retired-node", archived_at=archived_at)
        session.add(worker)
        await session.commit()
        await session.refresh(worker)

        assert worker.archived_at is not None
        assert worker.archived_at.replace(tzinfo=UTC) == archived_at
