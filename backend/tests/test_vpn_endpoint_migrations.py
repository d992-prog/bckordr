from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from app.db import models
from app.db.base import Base
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS


TEST_DATABASE_NAME = "veltrix_portal_test"
TEST_DATABASE_HOST = "127.0.0.1"
UNSAFE_DATABASE_URL_ERROR = "unsafe VPN_PORTAL_TEST_PG_URL"
PRE_TASK1_MIGRATIONS = VPN_ENDPOINT_MIGRATIONS[:6]
TASK1_MIGRATIONS = VPN_ENDPOINT_MIGRATIONS[6:]


@dataclass(frozen=True)
class PostgresSchema:
    engine: AsyncEngine
    name: str


def _validated_test_database_url(raw_url: str) -> URL:
    try:
        url = make_url(raw_url)
        port = url.port
    except (ArgumentError, TypeError, ValueError):
        raise ValueError(UNSAFE_DATABASE_URL_ERROR) from None

    is_safe = (
        url.drivername == "postgresql+asyncpg"
        and url.host == TEST_DATABASE_HOST
        and url.database == TEST_DATABASE_NAME
        and port is not None
        and port != 5432
        and not url.query
    )
    if not is_safe:
        raise ValueError(UNSAFE_DATABASE_URL_ERROR)
    return url


@pytest_asyncio.fixture
async def postgres_schema() -> AsyncIterator[PostgresSchema]:
    raw_url = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not raw_url:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for real PostgreSQL coverage")

    database_url = _validated_test_database_url(raw_url)
    schema_name = f"vpn_endpoint_test_{uuid4().hex}"
    base_engine = create_async_engine(database_url, hide_parameters=True)
    tested_engine = create_async_engine(
        database_url,
        connect_args={"server_settings": {"search_path": schema_name}},
        hide_parameters=True,
    )
    schema_created = False

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        yield PostgresSchema(engine=tested_engine, name=schema_name)
    finally:
        try:
            await tested_engine.dispose()
        finally:
            try:
                if schema_created:
                    async with base_engine.begin() as connection:
                        await connection.execute(
                            text(f'DROP SCHEMA "{schema_name}" CASCADE')
                        )
            finally:
                await base_engine.dispose()


async def _run_endpoint_migrations_twice(engine: AsyncEngine) -> None:
    for _ in range(2):
        async with engine.begin() as connection:
            for statement in VPN_ENDPOINT_MIGRATIONS:
                await connection.execute(text(statement))


async def _run_task1_migrations_twice(engine: AsyncEngine) -> None:
    for _ in range(2):
        async with engine.begin() as connection:
            for statement in TASK1_MIGRATIONS:
                await connection.execute(text(statement))


async def _assert_operation_unique_index_catalog(
    connection: AsyncConnection,
) -> None:
    canonical_index = "expected_vpn_control_operations_worker_reserved_predicate"
    await connection.execute(
        text(
            f"""
            CREATE INDEX {canonical_index}
            ON vpn_control_operations(worker_id)
            WHERE state IN ('claimed','uncertain')
            """
        )
    )
    try:
        rows = (
            (
                await connection.execute(
                    text(
                        f"""
                        SELECT index_table.relname AS index_name,
                               catalog_index.indisunique AS is_unique,
                               ARRAY(
                                   SELECT attribute.attname::text
                                   FROM unnest(catalog_index.indkey::smallint[])
                                       WITH ORDINALITY
                                       AS indexed_column(attnum, position)
                                   JOIN pg_attribute AS attribute
                                     ON attribute.attrelid = source_table.oid
                                    AND attribute.attnum = indexed_column.attnum
                                   WHERE indexed_column.attnum <> 0
                                   ORDER BY indexed_column.position
                               ) AS columns,
                               pg_get_expr(
                                   catalog_index.indpred,
                                   catalog_index.indrelid,
                                   true
                               ) AS predicate
                        FROM pg_index AS catalog_index
                        JOIN pg_class AS source_table
                          ON source_table.oid = catalog_index.indrelid
                        JOIN pg_namespace AS source_schema
                          ON source_schema.oid = source_table.relnamespace
                        JOIN pg_class AS index_table
                          ON index_table.oid = catalog_index.indexrelid
                        WHERE source_schema.nspname = current_schema()
                          AND source_table.relname = 'vpn_control_operations'
                          AND index_table.relname IN (
                              'uq_vpn_control_operations_claim_token',
                              'uq_vpn_control_operations_worker_reserved',
                              '{canonical_index}'
                          )
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
    finally:
        await connection.execute(text(f"DROP INDEX {canonical_index}"))

    catalog = {row["index_name"]: row for row in rows}
    assert set(catalog) == {
        "uq_vpn_control_operations_claim_token",
        "uq_vpn_control_operations_worker_reserved",
        canonical_index,
    }
    claim_token_index = catalog["uq_vpn_control_operations_claim_token"]
    assert claim_token_index["is_unique"] is True
    assert claim_token_index["columns"] == ["claim_token"]
    assert claim_token_index["predicate"] is None

    worker_reservation_index = catalog["uq_vpn_control_operations_worker_reserved"]
    assert worker_reservation_index["is_unique"] is True
    assert worker_reservation_index["columns"] == ["worker_id"]
    assert (
        worker_reservation_index["predicate"] == catalog[canonical_index]["predicate"]
    )


def _postgres_error_details(error: IntegrityError) -> tuple[str | None, str | None]:
    candidates = (error.orig, getattr(error.orig, "__cause__", None))
    constraint_name = next(
        (
            getattr(candidate, "constraint_name", None)
            for candidate in candidates
            if getattr(candidate, "constraint_name", None)
        ),
        None,
    )
    sqlstate = next(
        (
            getattr(candidate, "sqlstate", None) or getattr(candidate, "pgcode", None)
            for candidate in candidates
            if candidate is not None
            and (
                getattr(candidate, "sqlstate", None)
                or getattr(candidate, "pgcode", None)
            )
        ),
        None,
    )
    return constraint_name, sqlstate


async def _assert_integrity_error(
    connection: AsyncConnection,
    statement: str,
    parameters: dict[str, object] | None,
    *,
    constraint_name: str,
    sqlstate: str,
) -> None:
    savepoint = await connection.begin_nested()
    try:
        with pytest.raises(IntegrityError) as caught:
            await connection.execute(text(statement), parameters or {})

        actual_constraint, actual_sqlstate = _postgres_error_details(caught.value)
        assert actual_constraint == constraint_name
        assert actual_sqlstate == sqlstate
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "postgresql://postgres:do-not-leak@127.0.0.1:62740/veltrix_portal_test",
        "postgresql+asyncpg://postgres:do-not-leak@localhost:62740/veltrix_portal_test",
        "postgresql+asyncpg://postgres:do-not-leak@127.0.0.1:62740/production",
        "postgresql+asyncpg://postgres:do-not-leak@127.0.0.1:5432/veltrix_portal_test",
        "postgresql+asyncpg://postgres:do-not-leak@127.0.0.1/veltrix_portal_test",
        "postgresql+asyncpg://postgres:do-not-leak@127.0.0.1:62740/veltrix_portal_test?host=production",
        "not a database URL containing do-not-leak",
    ],
)
def test_test_database_url_rejects_unsafe_targets_without_leaking_secrets(
    unsafe_url: str,
) -> None:
    with pytest.raises(ValueError) as caught:
        _validated_test_database_url(unsafe_url)

    assert str(caught.value) == UNSAFE_DATABASE_URL_ERROR
    assert "do-not-leak" not in str(caught.value)


def test_test_database_url_accepts_only_the_allowlisted_target() -> None:
    url = _validated_test_database_url(
        "postgresql+asyncpg://postgres:do-not-leak@127.0.0.1:62740/veltrix_portal_test"
    )

    assert url.drivername == "postgresql+asyncpg"
    assert url.host == TEST_DATABASE_HOST
    assert url.port == 62740
    assert url.database == TEST_DATABASE_NAME


def test_task1_migrations_follow_the_existing_endpoint_schema() -> None:
    assert PRE_TASK1_MIGRATIONS[-1] == (
        "CREATE INDEX IF NOT EXISTS ix_vpn_access_keys_endpoint_id "
        "ON vpn_access_keys(endpoint_id)"
    )
    assert "operation_generation" in TASK1_MIGRATIONS[0]
    assert not any(
        "vpn_control_operations" in statement for statement in PRE_TASK1_MIGRATIONS
    )


@pytest.mark.asyncio
async def test_legacy_schema_upgrade_is_idempotent_and_enforces_endpoint_ownership(
    postgres_schema: PostgresSchema,
) -> None:
    issued_at = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    expires_at = datetime(2026, 10, 20, 10, 30, tzinfo=UTC)
    verified_at = datetime(2026, 9, 20, 10, 0, tzinfo=UTC)
    endpoint_created_at = datetime(2026, 9, 19, 8, 0, tzinfo=UTC)
    endpoint_updated_at = datetime(2026, 9, 20, 9, 45, tzinfo=UTC)
    expected_legacy_values = {
        "id": 1,
        "worker_id": 1,
        "endpoint_id": 1,
        "external_uuid": "legacy-endpoint-migration",
        "config_uri": "vless://legacy-endpoint-migration@example.test",
        "status": "active",
        "issued_at": issued_at,
        "expires_at": expires_at,
    }
    expected_endpoint_values = {
        "id": 1,
        "worker_id": 1,
        "inbound_id": 19,
        "public_host": "legacy-vpn.example.test",
        "port": 8443,
        "protocol": "vless",
        "transport": "tcp",
        "security": "reality",
        "server_name": "cover.example.test",
        "public_key": "legacy-public-key",
        "short_id": "0123abcd",
        "fingerprint": "chrome",
        "flow": "xtls-rprx-vision",
        "status": "ready",
        "verified_at": verified_at,
        "last_error_code": "legacy-observation",
        "created_at": endpoint_created_at,
        "updated_at": endpoint_updated_at,
    }

    async with postgres_schema.engine.begin() as connection:
        assert (
            await connection.scalar(text("SELECT current_schema()"))
            == postgres_schema.name
        )
        await connection.execute(
            text("CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)")
        )
        await connection.execute(
            text(
                """
                CREATE TABLE vpn_access_keys (
                    id SERIAL PRIMARY KEY,
                    worker_id INTEGER REFERENCES worker_nodes(id),
                    external_uuid VARCHAR(128),
                    config_uri TEXT,
                    status VARCHAR(32),
                    issued_at TIMESTAMPTZ,
                    expires_at TIMESTAMPTZ
                )
                """
            )
        )
        for statement in PRE_TASK1_MIGRATIONS:
            await connection.execute(text(statement))
        await connection.execute(text("INSERT INTO worker_nodes (id) VALUES (1), (2)"))
        await connection.execute(
            text(
                """
                INSERT INTO vpn_endpoints (
                    id, worker_id, inbound_id, public_host, port, protocol,
                    transport, security, server_name, public_key, short_id,
                    fingerprint, flow, status, verified_at, last_error_code,
                    created_at, updated_at
                ) VALUES (
                    :id, :worker_id, :inbound_id, :public_host, :port, :protocol,
                    :transport, :security, :server_name, :public_key, :short_id,
                    :fingerprint, :flow, :status, :verified_at, :last_error_code,
                    :created_at, :updated_at
                )
                """
            ),
            expected_endpoint_values,
        )
        await connection.execute(
            text(
                """
                INSERT INTO vpn_access_keys (
                    id, worker_id, endpoint_id, external_uuid, config_uri,
                    status, issued_at, expires_at
                ) VALUES (
                    :id, :worker_id, :endpoint_id, :external_uuid, :config_uri,
                    :status, :issued_at, :expires_at
                )
                """
            ),
            expected_legacy_values,
        )
        before_key = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT id, worker_id, endpoint_id, external_uuid, config_uri,
                               status, issued_at, expires_at
                        FROM vpn_access_keys
                        WHERE id = 1
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        before_endpoint = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT id, worker_id, inbound_id, public_host, port,
                               protocol, transport, security, server_name,
                               public_key, short_id, fingerprint, flow, status,
                               verified_at, last_error_code, created_at, updated_at
                        FROM vpn_endpoints
                        WHERE id = 1
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        assert dict(before_key) == expected_legacy_values
        assert dict(before_endpoint) == expected_endpoint_values

    await _run_task1_migrations_twice(postgres_schema.engine)

    async with postgres_schema.engine.begin() as connection:
        upgraded = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, worker_id, external_uuid, config_uri, status, issued_at, expires_at,
                           endpoint_id, operation_generation, revoke_requested_at,
                           verified_client_email, panel_sub_id
                    FROM vpn_access_keys
                    WHERE id = 1
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        upgraded_endpoint = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT id, worker_id, inbound_id, public_host, port,
                               protocol, transport, security, server_name,
                               public_key, short_id, fingerprint, flow, status,
                               verified_at, last_error_code, created_at, updated_at
                        FROM vpn_endpoints
                        WHERE id = 1
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        assert {key: upgraded[key] for key in before_key} == dict(before_key)
        assert dict(upgraded_endpoint) == dict(before_endpoint)
        assert upgraded["operation_generation"] == 0
        assert upgraded["revoke_requested_at"] is None
        assert upgraded["verified_client_email"] is None
        assert upgraded["panel_sub_id"] is None
        assert await connection.scalar(text("SELECT count(*) FROM vpn_endpoints")) == 1
        assert (
            await connection.scalar(text("SELECT count(*) FROM vpn_control_operations"))
            == 0
        )
        await _assert_operation_unique_index_catalog(connection)

        await connection.execute(
            text("UPDATE vpn_access_keys SET panel_sub_id = '' WHERE id = 1")
        )
        await connection.execute(
            text(
                """
                INSERT INTO vpn_access_keys (id, worker_id, endpoint_id, external_uuid)
                VALUES (2, 1, 1, 'new-after-migration')
                """
            )
        )
        panel_sub_ids = (
            (
                await connection.execute(
                    text("SELECT panel_sub_id FROM vpn_access_keys ORDER BY id")
                )
            )
            .scalars()
            .all()
        )
        assert panel_sub_ids == ["", None]

        await connection.execute(
            text(
                """
                INSERT INTO vpn_control_operations (
                    id, access_key_id, worker_id, endpoint_id, generation,
                    action, request_snapshot, request_digest
                ) VALUES (
                    '00000000-0000-0000-0000-000000000001', 1, 1, 1, 1,
                    'provision', '{"expires_at_ms":-1}', :request_digest
                )
                """
            ),
            {"request_digest": "a" * 64},
        )
        stored_operation = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT request_snapshot, state, claim_token, claimed_at,
                               finished_at, error_code
                        FROM vpn_control_operations
                        """
                    )
                )
            )
            .mappings()
            .one()
        )
        assert stored_operation["request_snapshot"] == {"expires_at_ms": -1}
        assert stored_operation["state"] == "queued"
        assert all(
            stored_operation[column] is None
            for column in ("claim_token", "claimed_at", "finished_at", "error_code")
        )

        await _assert_integrity_error(
            connection,
            "UPDATE vpn_access_keys SET worker_id = 2 WHERE id = 1",
            None,
            constraint_name="fk_vpn_access_key_endpoint_worker",
            sqlstate="23503",
        )
        await _assert_integrity_error(
            connection,
            "UPDATE vpn_access_keys SET worker_id = NULL WHERE id = 1",
            None,
            constraint_name="ck_vpn_access_key_endpoint_worker",
            sqlstate="23514",
        )
        await _assert_integrity_error(
            connection,
            "UPDATE vpn_access_keys SET endpoint_id = 999 WHERE id = 1",
            None,
            constraint_name="fk_vpn_access_key_endpoint_worker",
            sqlstate="23503",
        )
        await _assert_integrity_error(
            connection,
            """
            INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port)
            VALUES (2, 1, 1, 'duplicate.example', 443)
            """,
            None,
            constraint_name="uq_vpn_endpoint_worker_inbound",
            sqlstate="23505",
        )
        await _assert_integrity_error(
            connection,
            "DELETE FROM vpn_endpoints WHERE id = 1",
            None,
            constraint_name="fk_vpn_access_key_endpoint_worker",
            sqlstate="23503",
        )
        await _assert_integrity_error(
            connection,
            "UPDATE vpn_access_keys SET operation_generation = -1 WHERE id = 1",
            None,
            constraint_name="ck_vpn_access_key_operation_generation",
            sqlstate="23514",
        )
        await _assert_integrity_error(
            connection,
            """
            INSERT INTO vpn_control_operations (
                id, access_key_id, worker_id, endpoint_id, generation,
                action, request_snapshot, request_digest
            ) VALUES (
                '00000000-0000-0000-0000-000000000002', 1, 1, 1, 1,
                'suspend', '{}', :request_digest
            )
            """,
            {"request_digest": "b" * 64},
            constraint_name="uq_vpn_control_operation_key_generation",
            sqlstate="23505",
        )
        for column, value, constraint_name in (
            ("generation", "0", "ck_vpn_control_operation_generation"),
            ("action", "'unknown'", "ck_vpn_control_operation_action"),
            ("state", "'unknown'", "ck_vpn_control_operation_state"),
        ):
            await _assert_integrity_error(
                connection,
                f"UPDATE vpn_control_operations SET {column} = {value}",
                None,
                constraint_name=constraint_name,
                sqlstate="23514",
            )

        await connection.execute(
            text(
                """
                INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port)
                VALUES (3, 2, 2, 'worker-protection.example', 443)
                """
            )
        )
        await _assert_integrity_error(
            connection,
            "DELETE FROM worker_nodes WHERE id = 2",
            None,
            constraint_name="vpn_endpoints_worker_id_fkey",
            sqlstate="23503",
        )

        after_rejections = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, worker_id, external_uuid, config_uri, status, issued_at, expires_at,
                           endpoint_id, operation_generation, panel_sub_id
                    FROM vpn_access_keys
                    WHERE id = 1
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        assert {
            key: after_rejections[key] for key in expected_legacy_values
        } == expected_legacy_values
        assert after_rejections["endpoint_id"] == 1
        assert after_rejections["operation_generation"] == 0
        assert after_rejections["panel_sub_id"] == ""


@pytest.mark.asyncio
async def test_fresh_metadata_then_migrations_preserve_catalog_parity(
    postgres_schema: PostgresSchema,
) -> None:
    async with postgres_schema.engine.begin() as connection:
        assert (
            await connection.scalar(text("SELECT current_schema()"))
            == postgres_schema.name
        )
        await connection.run_sync(Base.metadata.create_all)

    await _run_endpoint_migrations_twice(postgres_schema.engine)

    endpoint_table = models.VpnEndpoint.__table__
    access_key_table = models.VpnAccessKey.__table__
    operation_table = models.VpnControlOperation.__table__
    expected_named_constraints = {
        constraint.name
        for table in (endpoint_table, access_key_table, operation_table)
        for constraint in table.constraints
        if constraint.name is not None
        and (
            constraint.name.startswith("ck_vpn_endpoint_")
            or constraint.name.startswith("uq_vpn_endpoint_")
            or constraint.name.startswith("ck_vpn_control_operation_")
            or constraint.name.startswith("uq_vpn_control_operation_")
            or constraint.name
            in {
                "fk_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_operation_generation",
                "fk_vpn_control_operation_endpoint_worker",
            }
        )
    }
    expected_endpoint_indexes = {index.name for index in endpoint_table.indexes}
    expected_operation_indexes = {index.name for index in operation_table.indexes}
    expected_endpoint_constraint_types = {
        "uq_vpn_endpoint_worker_inbound": "u",
        "uq_vpn_endpoint_id_worker": "u",
        "ck_vpn_endpoint_inbound": "c",
        "ck_vpn_endpoint_port": "c",
        "ck_vpn_endpoint_status": "c",
        "ck_vpn_endpoint_security": "c",
        "ck_vpn_endpoint_ready": "c",
        "ck_vpn_access_key_operation_generation": "c",
        "uq_vpn_control_operation_key_generation": "u",
        "fk_vpn_control_operation_endpoint_worker": "f",
        "ck_vpn_control_operation_generation": "c",
        "ck_vpn_control_operation_action": "c",
        "ck_vpn_control_operation_state": "c",
    }

    async with postgres_schema.engine.connect() as connection:
        await _assert_operation_unique_index_catalog(connection)
        constraint_rows = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT con.conname, con.contype::text AS contype, count(*) AS copies
                    FROM pg_constraint AS con
                    WHERE con.conrelid IN (
                        'vpn_endpoints'::regclass,
                        'vpn_access_keys'::regclass,
                        'vpn_control_operations'::regclass
                    )
                    GROUP BY con.conname, con.contype
                    """
                    )
                )
            )
            .mappings()
            .all()
        )
        constraint_catalog = {
            row["conname"]: (row["contype"], row["copies"]) for row in constraint_rows
        }

        actual_named_constraints = {
            name
            for name in constraint_catalog
            if name.startswith("ck_vpn_endpoint_")
            or name.startswith("uq_vpn_endpoint_")
            or name.startswith("ck_vpn_control_operation_")
            or name.startswith("uq_vpn_control_operation_")
            or name
            in {
                "fk_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_operation_generation",
                "fk_vpn_control_operation_endpoint_worker",
            }
        }

        assert actual_named_constraints == expected_named_constraints
        assert all(
            constraint_catalog[name][1] == 1 for name in expected_named_constraints
        )
        assert {
            name: constraint_catalog[name][0]
            for name in expected_endpoint_constraint_types
        } == expected_endpoint_constraint_types
        assert constraint_catalog["fk_vpn_access_key_endpoint_worker"] == ("f", 1)
        assert constraint_catalog["ck_vpn_access_key_endpoint_worker"] == ("c", 1)
        assert constraint_catalog["vpn_control_operations_pkey"] == ("p", 1)
        assert constraint_catalog["vpn_endpoints_pkey"] == ("p", 1)
        assert constraint_catalog["vpn_endpoints_worker_id_fkey"] == ("f", 1)

        composite_definition = await connection.scalar(
            text(
                """
                SELECT pg_get_constraintdef(oid)
                FROM pg_constraint
                WHERE conrelid = 'vpn_access_keys'::regclass
                  AND conname = 'fk_vpn_access_key_endpoint_worker'
                """
            )
        )
        assert composite_definition == (
            "FOREIGN KEY (endpoint_id, worker_id) "
            "REFERENCES vpn_endpoints(id, worker_id) ON DELETE RESTRICT"
        )

        endpoint_worker_delete_action = await connection.scalar(
            text(
                """
                SELECT confdeltype::text
                FROM pg_constraint
                WHERE conrelid = 'vpn_endpoints'::regclass
                  AND conname = 'vpn_endpoints_worker_id_fkey'
                """
            )
        )
        assert endpoint_worker_delete_action == "r"

        operation_foreign_keys = {
            row["conname"]: (row["definition"], row["delete_action"])
            for row in (
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname,
                                   pg_get_constraintdef(oid) AS definition,
                                   confdeltype::text AS delete_action
                            FROM pg_constraint
                            WHERE conrelid = 'vpn_control_operations'::regclass
                              AND contype = 'f'
                            """
                        )
                    )
                )
                .mappings()
                .all()
            )
        }
        assert operation_foreign_keys["fk_vpn_control_operation_endpoint_worker"] == (
            "FOREIGN KEY (endpoint_id, worker_id) "
            "REFERENCES vpn_endpoints(id, worker_id) ON DELETE RESTRICT",
            "r",
        )
        access_key_foreign_key = next(
            value
            for value in operation_foreign_keys.values()
            if value[0].startswith("FOREIGN KEY (access_key_id)")
        )
        assert access_key_foreign_key[1] == "r"

        index_names = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND tablename IN (
                              'vpn_endpoints', 'vpn_access_keys', 'vpn_control_operations'
                          )
                        """
                    )
                )
            ).scalars()
        )
        actual_endpoint_indexes = {
            name for name in index_names if name.startswith("ix_vpn_endpoints_")
        }
        assert actual_endpoint_indexes == expected_endpoint_indexes
        assert "ix_vpn_access_keys_endpoint_id" in index_names
        assert {
            name for name in index_names if name in expected_operation_indexes
        } == expected_operation_indexes


@pytest.mark.asyncio
async def test_constraint_names_in_another_schema_do_not_block_installation(
    postgres_schema: PostgresSchema,
) -> None:
    decoy_schema = f"vpn_endpoint_test_{uuid4().hex}"
    raw_url = os.environ["VPN_PORTAL_TEST_PG_URL"]
    database_url = _validated_test_database_url(raw_url)
    base_engine = create_async_engine(database_url, hide_parameters=True)
    decoy_created = False

    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{decoy_schema}"'))
            decoy_created = True
            await connection.execute(
                text(
                    f"""
                    CREATE TABLE "{decoy_schema}".constraint_decoy (
                        endpoint_id INTEGER,
                        worker_id INTEGER,
                        generation INTEGER,
                        action VARCHAR(16),
                        state VARCHAR(16),
                        CONSTRAINT fk_vpn_access_key_endpoint_worker
                            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL),
                        CONSTRAINT ck_vpn_access_key_endpoint_worker
                            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL),
                        CONSTRAINT ck_vpn_access_key_operation_generation
                            CHECK (generation IS NULL OR generation >= 0),
                        CONSTRAINT uq_vpn_control_operation_key_generation
                            CHECK (generation IS NULL OR generation > 0),
                        CONSTRAINT fk_vpn_control_operation_endpoint_worker
                            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL),
                        CONSTRAINT ck_vpn_control_operation_generation
                            CHECK (generation IS NULL OR generation > 0),
                        CONSTRAINT ck_vpn_control_operation_action
                            CHECK (action IS NULL OR action <> ''),
                        CONSTRAINT ck_vpn_control_operation_state
                            CHECK (state IS NULL OR state <> '')
                    )
                    """
                )
            )
            for index_name, column_name in (
                ("ix_vpn_control_operations_state", "state"),
                ("ix_vpn_control_operations_worker_id", "worker_id"),
                ("ix_vpn_control_operations_access_key_id", "endpoint_id"),
                ("uq_vpn_control_operations_claim_token", "action"),
                ("uq_vpn_control_operations_worker_reserved", "generation"),
            ):
                await connection.execute(
                    text(
                        f'CREATE INDEX "{index_name}" '
                        f'ON "{decoy_schema}".constraint_decoy ({column_name})'
                    )
                )

        async with postgres_schema.engine.begin() as connection:
            await connection.execute(
                text("CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)")
            )
            await connection.execute(
                text(
                    """
                    CREATE TABLE vpn_access_keys (
                        id SERIAL PRIMARY KEY,
                        worker_id INTEGER REFERENCES worker_nodes(id)
                    )
                    """
                )
            )

        await _run_endpoint_migrations_twice(postgres_schema.engine)

        async with postgres_schema.engine.connect() as connection:
            installed = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = 'vpn_access_keys'::regclass
                              AND conname IN (
                                  'fk_vpn_access_key_endpoint_worker',
                                  'ck_vpn_access_key_endpoint_worker',
                                  'ck_vpn_access_key_operation_generation'
                              )
                            """
                        )
                    )
                ).scalars()
            )
        assert installed == {
            "fk_vpn_access_key_endpoint_worker",
            "ck_vpn_access_key_endpoint_worker",
            "ck_vpn_access_key_operation_generation",
        }
        async with postgres_schema.engine.connect() as connection:
            operation_constraints = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT conname
                            FROM pg_constraint
                            WHERE conrelid = 'vpn_control_operations'::regclass
                              AND conname IN (
                                  'uq_vpn_control_operation_key_generation',
                                  'fk_vpn_control_operation_endpoint_worker',
                                  'ck_vpn_control_operation_generation',
                                  'ck_vpn_control_operation_action',
                                  'ck_vpn_control_operation_state'
                              )
                            """
                        )
                    )
                ).scalars()
            )
            operation_indexes = set(
                (
                    await connection.execute(
                        text(
                            """
                            SELECT indexname
                            FROM pg_indexes
                            WHERE schemaname = current_schema()
                              AND tablename = 'vpn_control_operations'
                            """
                        )
                    )
                ).scalars()
            )
        assert operation_constraints == {
            "uq_vpn_control_operation_key_generation",
            "fk_vpn_control_operation_endpoint_worker",
            "ck_vpn_control_operation_generation",
            "ck_vpn_control_operation_action",
            "ck_vpn_control_operation_state",
        }
        assert operation_indexes >= {
            "ix_vpn_control_operations_state",
            "ix_vpn_control_operations_worker_id",
            "ix_vpn_control_operations_access_key_id",
            "uq_vpn_control_operations_claim_token",
            "uq_vpn_control_operations_worker_reserved",
        }
    finally:
        try:
            if decoy_created:
                async with base_engine.begin() as connection:
                    await connection.execute(
                        text(f'DROP SCHEMA "{decoy_schema}" CASCADE')
                    )
        finally:
            await base_engine.dispose()
