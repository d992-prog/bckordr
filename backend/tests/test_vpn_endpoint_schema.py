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

CONTROL_OPERATION_COLUMNS = {
    "id",
    "access_key_id",
    "worker_id",
    "endpoint_id",
    "generation",
    "action",
    "request_snapshot",
    "request_digest",
    "state",
    "claim_token",
    "claimed_at",
    "finished_at",
    "error_code",
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


def _control_operation(**overrides):
    values = {
        "id": "00000000-0000-0000-0000-000000000001",
        "access_key_id": 1,
        "worker_id": 1,
        "endpoint_id": 1,
        "generation": 1,
        "action": "provision",
        "request_snapshot": {"expires_at_ms": -1, "client": {"enabled": True}},
        "request_digest": "a" * 64,
    }
    values.update(overrides)
    return models.VpnControlOperation(**values)


async def _seed_control_operation_owners(session: AsyncSession) -> None:
    await _seed_owners(session)
    session.add(_endpoint(id=1))
    await session.flush()
    session.add(
        models.VpnAccessKey(
            id=1,
            subscription_id=1,
            worker_id=1,
            endpoint_id=1,
            external_uuid="control-operation-key",
        )
    )
    await session.flush()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def test_models_metadata_exposes_vpn_endpoints_table():
    assert "vpn_endpoints" in Base.metadata.tables


def test_models_metadata_exposes_exact_control_operation_schema():
    assert "vpn_control_operations" in Base.metadata.tables

    table = models.VpnControlOperation.__table__
    assert set(table.columns.keys()) == CONTROL_OPERATION_COLUMNS
    assert table.c.id.primary_key
    assert table.c.id.type.length == 36
    assert table.c.request_digest.type.length == 64
    assert table.c.claim_token.type.length == 36
    assert table.c.error_code.type.length == 64

    assert {constraint.name for constraint in table.constraints} >= {
        "uq_vpn_control_operation_key_generation",
        "fk_vpn_control_operation_endpoint_worker",
        "ck_vpn_control_operation_generation",
        "ck_vpn_control_operation_action",
        "ck_vpn_control_operation_state",
    }
    assert {index.name for index in table.indexes} == {
        "ix_vpn_control_operations_state",
        "ix_vpn_control_operations_worker_id",
        "ix_vpn_control_operations_access_key_id",
        "uq_vpn_control_operations_claim_token",
        "uq_vpn_control_operations_worker_reserved",
    }


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
async def test_access_key_control_fields_have_backward_compatible_defaults():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            access_key = models.VpnAccessKey(subscription_id=1)
            session.add(access_key)
            await session.commit()
            access_key_id = access_key.id

        async with session_factory() as session:
            stored = await session.get(models.VpnAccessKey, access_key_id)

        assert stored is not None
        assert stored.operation_generation == 0
        assert stored.revoke_requested_at is None
        assert stored.verified_client_email is None
        assert stored.panel_sub_id is None

        table = models.VpnAccessKey.__table__
        assert table.c.operation_generation.nullable is False
        assert str(table.c.operation_generation.server_default.arg) == "0"
        assert table.c.verified_client_email.type.length == 64
        assert table.c.panel_sub_id.type.length == 64
        assert "ck_vpn_access_key_operation_generation" in {
            constraint.name for constraint in table.constraints
        }
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_operation_defaults_and_json_round_trip():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            operation = _control_operation()
            session.add(operation)
            await session.commit()

        async with session_factory() as session:
            stored = await session.get(
                models.VpnControlOperation,
                "00000000-0000-0000-0000-000000000001",
            )

        assert stored is not None
        assert stored.request_snapshot == {
            "expires_at_ms": -1,
            "client": {"enabled": True},
        }
        assert stored.state == "queued"
        assert stored.claim_token is None
        assert stored.claimed_at is None
        assert stored.finished_at is None
        assert stored.error_code is None
        assert stored.created_at is not None
        assert stored.updated_at is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"generation": 0},
        {"action": "unknown"},
        {"state": "unknown"},
    ],
)
async def test_control_operation_checks_reject_invalid_values(overrides):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add(_control_operation(**overrides))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_access_key_operation_generation_cannot_be_negative():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_owners(session)
            session.add(
                models.VpnAccessKey(
                    subscription_id=1,
                    operation_generation=-1,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_operation_key_generation_is_unique():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add_all(
                [
                    _control_operation(),
                    _control_operation(
                        id="00000000-0000-0000-0000-000000000002",
                        action="suspend",
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"access_key_id": 999},
        {"worker_id": 2},
        {"endpoint_id": 999},
    ],
)
async def test_control_operation_rejects_missing_or_mismatched_references(overrides):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add(_control_operation(**overrides))
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_control_operation_claim_token_is_unique_only_when_non_null():
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add_all(
                [
                    _control_operation(),
                    _control_operation(
                        id="00000000-0000-0000-0000-000000000002",
                        generation=2,
                    ),
                ]
            )
            await session.commit()

        async with session_factory() as session:
            session.add_all(
                [
                    _control_operation(
                        id="00000000-0000-0000-0000-000000000003",
                        generation=3,
                        claim_token="10000000-0000-0000-0000-000000000001",
                    ),
                    _control_operation(
                        id="00000000-0000-0000-0000-000000000004",
                        generation=4,
                        claim_token="10000000-0000-0000-0000-000000000001",
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("first_state", "second_state"),
    [("claimed", "claimed"), ("claimed", "uncertain"), ("uncertain", "uncertain")],
)
async def test_control_operation_reserves_one_active_row_per_worker(
    first_state,
    second_state,
):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add_all(
                [
                    _control_operation(state=first_state),
                    _control_operation(
                        id="00000000-0000-0000-0000-000000000002",
                        generation=2,
                        state=second_state,
                    ),
                ]
            )
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["access_key", "endpoint"])
async def test_control_operation_references_restrict_deletion(target):
    engine, session_factory = await _make_session_factory()
    try:
        async with session_factory() as session:
            await _seed_control_operation_owners(session)
            session.add(_control_operation())
            await session.commit()

        async with session_factory() as session:
            referenced = await session.get(
                models.VpnAccessKey if target == "access_key" else models.VpnEndpoint,
                1,
            )
            await session.delete(referenced)
            with pytest.raises(IntegrityError):
                await session.commit()
    finally:
        await engine.dispose()


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


def test_endpoint_migrations_remain_contiguous_with_named_constraints():
    from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS

    start = MIGRATIONS.index(VPN_ENDPOINT_MIGRATIONS[0])
    assert MIGRATIONS[start : start + len(VPN_ENDPOINT_MIGRATIONS)] == (
        VPN_ENDPOINT_MIGRATIONS
    )
    migration_sql = "\n".join(VPN_ENDPOINT_MIGRATIONS)
    assert "CREATE TABLE IF NOT EXISTS vpn_endpoints" in migration_sql
    assert "CONSTRAINT uq_vpn_endpoint_worker_inbound UNIQUE (worker_id, inbound_id)" in migration_sql
    assert "CONSTRAINT uq_vpn_endpoint_id_worker UNIQUE (id, worker_id)" in migration_sql
    assert "CONSTRAINT ck_vpn_endpoint_ready" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS endpoint_id INTEGER NULL" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS operation_generation INTEGER NOT NULL DEFAULT 0" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS revoke_requested_at TIMESTAMPTZ NULL" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS verified_client_email VARCHAR(64) NULL" in migration_sql
    assert "ADD COLUMN IF NOT EXISTS panel_sub_id VARCHAR(64) NULL" in migration_sql
    assert "CONSTRAINT fk_vpn_access_key_endpoint_worker" in migration_sql
    assert "CONSTRAINT ck_vpn_access_key_endpoint_worker" in migration_sql
    assert "CONSTRAINT ck_vpn_access_key_operation_generation" in migration_sql
    assert "CREATE TABLE IF NOT EXISTS vpn_control_operations" in migration_sql
    assert "CONSTRAINT uq_vpn_control_operation_key_generation" in migration_sql
    assert "CONSTRAINT fk_vpn_control_operation_endpoint_worker" in migration_sql
    assert "CONSTRAINT ck_vpn_control_operation_generation" in migration_sql
    assert "CONSTRAINT ck_vpn_control_operation_action" in migration_sql
    assert "CONSTRAINT ck_vpn_control_operation_state" in migration_sql
    assert migration_sql.count("IF NOT EXISTS (SELECT 1 FROM pg_constraint") == 3
    assert "END;\n    $$" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_endpoint_id" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_state" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_worker_id" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_vpn_control_operations_access_key_id" in migration_sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_control_operations_claim_token" in migration_sql
    assert (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_control_operations_worker_reserved\n"
        "    ON vpn_control_operations(worker_id)\n"
        "    WHERE state IN ('claimed','uncertain')"
    ) in migration_sql
