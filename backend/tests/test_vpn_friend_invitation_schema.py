from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, UniqueConstraint, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.db.base import Base
from app.db.migrations import MIGRATIONS
from app.db.models import VpnFriendInvitation
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS


TEST_DATABASE_NAME = "veltrix_portal_test"
TEST_DATABASE_HOST = "127.0.0.1"
UNSAFE_DATABASE_URL_ERROR = "unsafe VPN_PORTAL_TEST_PG_URL"
FRIEND_INVITATION_MIGRATIONS = MIGRATIONS[-1:]
PRE_INVITATION_MIGRATIONS = MIGRATIONS[
    -len(VPN_ENDPOINT_MIGRATIONS) - 1 : -1
]


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
    schema_name = f"vpn_friend_invitation_test_{uuid4().hex}"
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


async def _create_pre_endpoint_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE users (id SERIAL PRIMARY KEY)"))
        await connection.execute(
            text(
                """
                CREATE TABLE vpn_customers (
                    id SERIAL PRIMARY KEY,
                    telegram_user_id VARCHAR(64) UNIQUE NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'active'
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                CREATE TABLE vpn_subscriptions (
                    id SERIAL PRIMARY KEY,
                    customer_id INTEGER NOT NULL REFERENCES vpn_customers(id) ON DELETE CASCADE,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    starts_at TIMESTAMPTZ NULL,
                    expires_at TIMESTAMPTZ NULL
                )
                """
            )
        )
        await connection.execute(text("CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)"))
        await connection.execute(
            text(
                """
                CREATE TABLE vpn_access_keys (
                    id SERIAL PRIMARY KEY,
                    subscription_id INTEGER NOT NULL
                        REFERENCES vpn_subscriptions(id) ON DELETE CASCADE,
                    worker_id INTEGER NULL REFERENCES worker_nodes(id) ON DELETE SET NULL,
                    protocol VARCHAR(32) NOT NULL DEFAULT 'vless',
                    public_name VARCHAR(128) NULL,
                    display_name VARCHAR(64) NULL,
                    external_uuid VARCHAR(128) UNIQUE NULL,
                    config_uri TEXT NULL,
                    status VARCHAR(32) NOT NULL DEFAULT 'active',
                    issued_at TIMESTAMPTZ NULL,
                    expires_at TIMESTAMPTZ NULL,
                    revoked_at TIMESTAMPTZ NULL
                )
                """
            )
        )


async def _run_migrations(engine: AsyncEngine, migrations: tuple[str, ...]) -> None:
    async with engine.begin() as connection:
        for statement in migrations:
            await connection.execute(text(statement))


async def _existing_row_snapshot(engine: AsyncEngine) -> dict[str, list[dict[str, object]]]:
    snapshot: dict[str, list[dict[str, object]]] = {}
    async with engine.connect() as connection:
        for table_name in (
            "users",
            "vpn_customers",
            "vpn_subscriptions",
            "vpn_access_keys",
        ):
            rows = (
                (await connection.execute(text(f"SELECT * FROM {table_name} ORDER BY id")))
                .mappings()
                .all()
            )
            snapshot[table_name] = [dict(row) for row in rows]
    return snapshot


async def _invitation_catalog(engine: AsyncEngine) -> tuple[dict[str, str], dict[str, tuple[str, str | None]]]:
    async with engine.connect() as connection:
        column_rows = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT column_name, data_type
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'vpn_friend_invitations'
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
        constraint_rows = (
            (
                await connection.execute(
                    text(
                        """
                        SELECT conname, pg_get_constraintdef(oid) AS definition,
                               confdeltype::text AS delete_action
                        FROM pg_constraint
                        WHERE conrelid = 'vpn_friend_invitations'::regclass
                        """
                    )
                )
            )
            .mappings()
            .all()
        )
    return (
        {row["column_name"]: row["data_type"] for row in column_rows},
        {
            row["conname"]: (row["definition"], row["delete_action"])
            for row in constraint_rows
        },
    )


def test_friend_invitation_schema_is_fixed_and_fail_closed() -> None:
    table = VpnFriendInvitation.__table__
    assert {column.name for column in table.columns} == {
        "slot",
        "token_digest",
        "created_by_user_id",
        "created_at",
        "redeem_expires_at",
        "redeemed_at",
        "telegram_user_id",
        "access_key_id",
        "revoked_at",
    }
    assert any(
        isinstance(item, CheckConstraint)
        and item.name == "ck_vpn_friend_invitation_slot"
        for item in table.constraints
    )
    assert any(
        isinstance(item, CheckConstraint)
        and item.name == "ck_vpn_friend_invitation_redemption"
        for item in table.constraints
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("access_key_id",)
        for item in table.constraints
    )


def test_friend_beta_and_dispatcher_default_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.vpn_friend_beta_enabled is False
    assert settings.vpn_friend_beta_release_id == ""
    assert settings.vpn_telegram_bot_username == ""
    assert settings.vpn_control_dispatch_enabled is False
    assert settings.vpn_control_known_hosts_path == ""


def test_invitation_migration_follows_endpoint_schema_with_exact_contract() -> None:
    assert PRE_INVITATION_MIGRATIONS == VPN_ENDPOINT_MIGRATIONS
    assert len(FRIEND_INVITATION_MIGRATIONS) == 1

    statement = " ".join(FRIEND_INVITATION_MIGRATIONS[0].split())
    assert statement.startswith("CREATE TABLE IF NOT EXISTS vpn_friend_invitations")
    assert "slot SMALLINT PRIMARY KEY" in statement
    assert "CHECK (slot BETWEEN 1 AND 10)" in statement
    assert "UNIQUE (token_digest)" in statement
    assert "UNIQUE (access_key_id)" in statement
    assert "REFERENCES users(id) ON DELETE SET NULL" in statement
    assert "REFERENCES vpn_access_keys(id) ON DELETE RESTRICT" in statement
    assert (
        "(redeemed_at IS NULL AND telegram_user_id IS NULL AND access_key_id IS NULL) OR "
        "(redeemed_at IS NOT NULL AND telegram_user_id IS NOT NULL AND access_key_id IS NOT NULL)"
        in statement
    )


@pytest.mark.asyncio
async def test_pre_invitation_postgres_upgrade_preserves_rows_and_repeats(
    postgres_schema: PostgresSchema,
) -> None:
    starts_at = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
    expires_at = datetime(2026, 10, 22, 10, 0, tzinfo=UTC)
    await _create_pre_endpoint_schema(postgres_schema.engine)
    await _run_migrations(postgres_schema.engine, PRE_INVITATION_MIGRATIONS)

    async with postgres_schema.engine.begin() as connection:
        assert await connection.scalar(text("SELECT current_schema()")) == postgres_schema.name
        await connection.execute(text("INSERT INTO users (id) VALUES (11)"))
        await connection.execute(
            text(
                """
                INSERT INTO vpn_customers (id, telegram_user_id, status)
                VALUES (21, 'legacy-friend', 'active')
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO vpn_subscriptions (
                    id, customer_id, status, starts_at, expires_at
                ) VALUES (
                    31, 21, 'active', :starts_at, :expires_at
                )
                """
            ),
            {"starts_at": starts_at, "expires_at": expires_at},
        )
        await connection.execute(
            text(
                """
                INSERT INTO vpn_access_keys (
                    id, subscription_id, protocol, public_name, display_name,
                    external_uuid, config_uri, status, issued_at, expires_at
                ) VALUES (
                    41, 31, 'vless', 'Legacy public name', 'Legacy display name',
                    'legacy-friend-uuid', 'vless://legacy-friend-uuid@example.test',
                    'active', :starts_at, :expires_at
                )
                """
            ),
            {"starts_at": starts_at, "expires_at": expires_at},
        )

    before = await _existing_row_snapshot(postgres_schema.engine)

    await _run_migrations(postgres_schema.engine, FRIEND_INVITATION_MIGRATIONS)
    await _run_migrations(postgres_schema.engine, FRIEND_INVITATION_MIGRATIONS)

    async with postgres_schema.engine.connect() as connection:
        invitation_count = await connection.scalar(
            text("SELECT count(*) FROM vpn_friend_invitations")
        )

    after = await _existing_row_snapshot(postgres_schema.engine)

    assert after == before
    assert invitation_count == 0


@pytest.mark.asyncio
async def test_fresh_postgres_schema_has_exact_invitation_constraints(
    postgres_schema: PostgresSchema,
) -> None:
    async with postgres_schema.engine.begin() as connection:
        assert await connection.scalar(text("SELECT current_schema()")) == postgres_schema.name
        await connection.run_sync(Base.metadata.create_all)

    await _run_migrations(postgres_schema.engine, MIGRATIONS)
    await _run_migrations(postgres_schema.engine, MIGRATIONS)

    columns, constraints = await _invitation_catalog(postgres_schema.engine)

    assert columns == {
        "slot": "smallint",
        "token_digest": "character varying",
        "created_by_user_id": "integer",
        "created_at": "timestamp with time zone",
        "redeem_expires_at": "timestamp with time zone",
        "redeemed_at": "timestamp with time zone",
        "telegram_user_id": "character varying",
        "access_key_id": "integer",
        "revoked_at": "timestamp with time zone",
    }
    assert constraints["ck_vpn_friend_invitation_slot"][0] == (
        "CHECK (((slot >= 1) AND (slot <= 10)))"
    )
    assert "redeemed_at IS NULL" in constraints[
        "ck_vpn_friend_invitation_redemption"
    ][0]
    assert constraints["vpn_friend_invitations_token_digest_key"][0] == (
        "UNIQUE (token_digest)"
    )
    assert constraints["uq_vpn_friend_invitation_access_key"][0] == (
        "UNIQUE (access_key_id)"
    )
    assert constraints["vpn_friend_invitations_created_by_user_id_fkey"][1] == "n"
    assert constraints["vpn_friend_invitations_access_key_id_fkey"][1] == "r"
