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


@pytest.mark.asyncio
async def test_legacy_schema_upgrade_is_idempotent_and_enforces_endpoint_ownership(
    postgres_schema: PostgresSchema,
) -> None:
    issued_at = datetime(2026, 9, 20, 10, 30, tzinfo=UTC)
    expires_at = datetime(2026, 10, 20, 10, 30, tzinfo=UTC)
    expected_legacy_values = {
        "id": 1,
        "worker_id": 1,
        "external_uuid": "legacy-endpoint-migration",
        "config_uri": "vless://legacy-endpoint-migration@example.test",
        "status": "active",
        "issued_at": issued_at,
        "expires_at": expires_at,
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
        await connection.execute(text("INSERT INTO worker_nodes (id) VALUES (1), (2)"))
        await connection.execute(
            text(
                """
                INSERT INTO vpn_access_keys (
                    id, worker_id, external_uuid, config_uri, status, issued_at, expires_at
                ) VALUES (
                    1, :worker_id, :external_uuid, :config_uri, :status, :issued_at, :expires_at
                )
                """
            ),
            expected_legacy_values,
        )
        before = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, worker_id, external_uuid, config_uri, status, issued_at, expires_at
                    FROM vpn_access_keys
                    WHERE id = 1
                    """
                    )
                )
            )
            .mappings()
            .one()
        )
        assert dict(before) == expected_legacy_values

    await _run_endpoint_migrations_twice(postgres_schema.engine)

    async with postgres_schema.engine.begin() as connection:
        upgraded = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT id, worker_id, external_uuid, config_uri, status, issued_at, expires_at,
                           endpoint_id
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
            key: upgraded[key] for key in expected_legacy_values
        } == expected_legacy_values
        assert upgraded["endpoint_id"] is None
        assert await connection.scalar(text("SELECT count(*) FROM vpn_endpoints")) == 0

        await connection.execute(
            text(
                """
                INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port)
                VALUES (1, 1, 1, 'vpn.example', 443)
                """
            )
        )
        await connection.execute(
            text("UPDATE vpn_access_keys SET endpoint_id = 1 WHERE id = 1")
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
                           endpoint_id
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
    expected_named_constraints = {
        constraint.name
        for table in (endpoint_table, access_key_table)
        for constraint in table.constraints
        if constraint.name is not None
        and (
            constraint.name.startswith("ck_vpn_endpoint_")
            or constraint.name.startswith("uq_vpn_endpoint_")
            or constraint.name
            in {
                "fk_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_endpoint_worker",
            }
        )
    }
    expected_endpoint_indexes = {index.name for index in endpoint_table.indexes}
    expected_endpoint_constraint_types = {
        "uq_vpn_endpoint_worker_inbound": "u",
        "uq_vpn_endpoint_id_worker": "u",
        "ck_vpn_endpoint_inbound": "c",
        "ck_vpn_endpoint_port": "c",
        "ck_vpn_endpoint_status": "c",
        "ck_vpn_endpoint_security": "c",
        "ck_vpn_endpoint_ready": "c",
    }

    async with postgres_schema.engine.connect() as connection:
        constraint_rows = (
            (
                await connection.execute(
                    text(
                        """
                    SELECT con.conname, con.contype::text AS contype, count(*) AS copies
                    FROM pg_constraint AS con
                    WHERE con.conrelid IN (
                        'vpn_endpoints'::regclass,
                        'vpn_access_keys'::regclass
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
            or name
            in {
                "fk_vpn_access_key_endpoint_worker",
                "ck_vpn_access_key_endpoint_worker",
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

        index_names = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = current_schema()
                          AND tablename IN ('vpn_endpoints', 'vpn_access_keys')
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
                        CONSTRAINT fk_vpn_access_key_endpoint_worker
                            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL),
                        CONSTRAINT ck_vpn_access_key_endpoint_worker
                            CHECK (endpoint_id IS NULL OR worker_id IS NOT NULL)
                    )
                    """
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
                                  'ck_vpn_access_key_endpoint_worker'
                              )
                            """
                        )
                    )
                ).scalars()
            )
        assert installed == {
            "fk_vpn_access_key_endpoint_worker",
            "ck_vpn_access_key_endpoint_worker",
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
