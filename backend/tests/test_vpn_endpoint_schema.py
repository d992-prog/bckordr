from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import selectinload
from sqlalchemy.pool import StaticPool

from app.db import models
from app.db.base import Base
from app.db.migrations import MIGRATIONS


ENDPOINT_COLUMNS = {
    "id",
    "worker_id",
    "inbound_id",
    "public_host",
    "port",
    "protocol",
    "transport",
    "security",
    "server_name",
    "public_key",
    "short_id",
    "fingerprint",
    "flow",
    "status",
    "verified_at",
    "last_error_code",
    "created_at",
    "updated_at",
}


async def _make_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    return engine, session_factory


async def _seed_owners(session: AsyncSession) -> None:
    session.add_all(
        [
            models.WorkerNode(id=1, name="one"),
            models.WorkerNode(id=2, name="two"),
            models.VpnCustomer(id=1),
        ]
    )
    await session.flush()
    session.add(models.VpnSubscription(id=1, customer_id=1))
    await session.flush()


def _endpoint(**overrides):
    values = {
        "worker_id": 1,
        "inbound_id": 10,
        "public_host": "vpn-one.example.test",
        "port": 443,
    }
    values.update(overrides)
    return models.VpnEndpoint(**values)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_models_metadata_exposes_vpn_endpoints_table():
    assert "vpn_endpoints" in Base.metadata.tables


def test_legacy_access_key_construction_preserves_identity_with_null_endpoint():
    access_key = models.VpnAccessKey(
        subscription_id=1,
        worker_id=7,
        external_uuid="legacy",
        config_uri="vless://legacy",
    )

    assert hasattr(access_key, "endpoint_id")
    assert access_key.endpoint_id is None
    assert access_key.subscription_id == 1
    assert access_key.worker_id == 7
    assert access_key.external_uuid == "legacy"
    assert access_key.config_uri == "vless://legacy"


@pytest.mark.asyncio
async def test_fresh_schema_has_only_safe_endpoint_columns_and_staged_defaults():
    engine, session_factory = await _make_session_factory()
    try:
        async with engine.connect() as connection:
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"] for column in inspect(sync_connection).get_columns("vpn_endpoints")
                }
            )
        assert columns == ENDPOINT_COLUMNS

        async with session_factory() as session:
            await _seed_owners(session)
            endpoint = _endpoint()
            session.add(endpoint)
            await session.commit()
            endpoint_id = endpoint.id

        async with session_factory() as session:
            stored = await session.get(models.VpnEndpoint, endpoint_id)

        assert stored is not None
        assert stored.protocol == "vless"
        assert stored.transport == "tcp"
        assert stored.security == "none"
        assert stored.status == "staged"
        assert stored.verified_at is None
        assert stored.created_at is not None
        assert stored.updated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"inbound_id": 0},
        {"port": 0},
        {"port": 65536},
        {"status": "unknown"},
        {"security": "unknown"},
        {"status": "ready", "security": "none"},
        {"status": "ready", "security": "reality", "verified_at": None},
    ],
)
async def test_endpoint_constraints_reject_invalid_values(overrides):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            session.add(_endpoint(**overrides))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_endpoint_identity_is_unique_within_worker_only():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            session.add_all([_endpoint(), _endpoint(worker_id=2)])
            await session.commit()

        async with session_factory() as session:
            session.add(_endpoint(public_host="duplicate.example.test"))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint_id", "worker_id"),
    [
        (1, 2),
        (1, None),
        (999, 1),
    ],
)
async def test_access_key_endpoint_binding_rejects_wrong_or_missing_owners(endpoint_id, worker_id):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            session.add(_endpoint(id=1))
            await session.flush()
            session.add(
                models.VpnAccessKey(
                    subscription_id=1,
                    worker_id=worker_id,
                    endpoint_id=endpoint_id,
                    external_uuid=f"invalid-{endpoint_id}-{worker_id}",
                    config_uri="vless://invalid",
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_model", ["endpoint", "worker"])
async def test_referenced_endpoint_and_worker_cannot_be_deleted(target_model):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            endpoint = _endpoint(id=1)
            session.add(endpoint)
            await session.flush()
            session.add(
                models.VpnAccessKey(
                    subscription_id=1,
                    worker_id=1,
                    endpoint_id=1,
                    external_uuid=f"delete-{target_model}",
                    config_uri="vless://delete-check",
                )
            )
            await session.commit()

        async with session_factory() as session:
            target = await session.get(
                models.VpnEndpoint if target_model == "endpoint" else models.WorkerNode,
                1,
            )
            await session.delete(target)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_legacy_and_endpoint_bound_access_keys_round_trip_with_worker_relationship():
    engine, session_factory = await _make_session_factory()
    issued_at = datetime(2026, 9, 21, 12, 30, tzinfo=UTC)
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            session.add(_endpoint(id=1))
            await session.flush()
            session.add_all(
                [
                    models.VpnAccessKey(
                        subscription_id=1,
                        worker_id=1,
                        external_uuid="legacy-roundtrip",
                        config_uri="vless://legacy-roundtrip",
                        status="active",
                        issued_at=issued_at,
                    ),
                    models.VpnAccessKey(
                        subscription_id=1,
                        worker_id=1,
                        endpoint_id=1,
                        external_uuid="bound-roundtrip",
                        config_uri="vless://bound-roundtrip",
                        status="active",
                        issued_at=issued_at,
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            keys = (
                await session.execute(
                    select(models.VpnAccessKey)
                    .options(selectinload(models.VpnAccessKey.worker))
                    .order_by(models.VpnAccessKey.external_uuid)
                )
            ).scalars().all()

        bound, legacy = keys
        assert bound.endpoint_id == 1
        assert legacy.endpoint_id is None
        assert [key.external_uuid for key in keys] == ["bound-roundtrip", "legacy-roundtrip"]
        assert [key.config_uri for key in keys] == ["vless://bound-roundtrip", "vless://legacy-roundtrip"]
        assert all(key.status == "active" for key in keys)
        assert all(_as_utc(key.issued_at) == issued_at for key in keys)
        assert all(key.worker is not None and key.worker.name == "one" for key in keys)
    finally:
        await engine.dispose()


def test_endpoint_migrations_are_appended_with_named_constraints():
    from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS

    assert MIGRATIONS[-len(VPN_ENDPOINT_MIGRATIONS) :] == VPN_ENDPOINT_MIGRATIONS
    migration_sql = "\n".join(VPN_ENDPOINT_MIGRATIONS)
    assert "CREATE TABLE IF NOT EXISTS vpn_endpoints" in migration_sql
    assert "CONSTRAINT uq_vpn_endpoint_worker_inbound UNIQUE (worker_id, inbound_id)" in migration_sql
    assert "CONSTRAINT uq_vpn_endpoint_id_worker UNIQUE (id, worker_id)" in migration_sql
    assert "CONSTRAINT ck_vpn_endpoint_ready" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS endpoint_id INTEGER NULL" in migration_sql
    assert "CONSTRAINT fk_vpn_access_key_endpoint_worker" in migration_sql
    assert "CONSTRAINT ck_vpn_access_key_endpoint_worker" in migration_sql
    assert migration_sql.count("IF NOT EXISTS (SELECT 1 FROM pg_constraint") == 2
    assert "END;\n    $$" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_endpoint_id" in migration_sql
