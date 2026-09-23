from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import Settings
from app.db import migrations
from app.db.models import VpnAccessKey, VpnCustomer, VpnEndpoint
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS


def test_public_trial_columns_and_constraints() -> None:
    trial = VpnCustomer.__table__.c.trial_started_at
    capacity = VpnEndpoint.__table__.c.max_active_profiles
    warning = VpnEndpoint.__table__.c.capacity_warning_percent
    claimed = VpnAccessKey.__table__.c.ready_notice_claimed_at
    notified = VpnAccessKey.__table__.c.ready_notified_at
    for column in (trial, claimed, notified):
        assert column.type.timezone is True
        assert column.nullable is True
    assert trial.index is True
    assert any(
        index.name == "ix_vpn_customers_trial_started_at"
        and tuple(column.name for column in index.columns) == ("trial_started_at",)
        for index in VpnCustomer.__table__.indexes
    )
    assert capacity.type.python_type is int
    assert capacity.nullable is True
    assert warning.type.python_type is int
    assert warning.nullable is False
    assert warning.default.arg == 80
    assert warning.server_default.arg == "80"
    constraints = {
        item.name: str(item.sqltext)
        for item in VpnEndpoint.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert (
        constraints["ck_vpn_endpoint_capacity"]
        == "max_active_profiles IS NULL OR max_active_profiles > 0"
    )
    assert (
        constraints["ck_vpn_endpoint_capacity_warning"]
        == "capacity_warning_percent BETWEEN 1 AND 100"
    )


def test_public_trial_settings_default_off(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "VPN_PUBLIC_TRIAL_ENABLED",
        "VPN_PUBLIC_TRIAL_RELEASE_ID",
        "VPN_PUBLIC_TRIAL_PLAN_SLUG",
        "VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS",
        "VPN_READY_NOTIFICATIONS_ENABLED",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(_env_file=None)
    assert settings.vpn_public_trial_enabled is False
    assert settings.vpn_public_trial_release_id == ""
    assert settings.vpn_public_trial_plan_slug == "trial-7d"
    assert settings.vpn_endpoint_health_max_age_seconds == 300
    assert settings.vpn_ready_notifications_enabled is False


def test_public_trial_upgrade_statements_are_registered() -> None:
    statements = getattr(migrations, "VPN_PUBLIC_TRIAL_MIGRATIONS", ())
    assert statements
    assert (
        migrations.MIGRATIONS[-len(VPN_ENDPOINT_MIGRATIONS) - 1 : -1]
        == VPN_ENDPOINT_MIGRATIONS
    )
    sql = "\n".join(statements)
    assert "ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ" in sql
    assert "ix_vpn_customers_trial_started_at" in sql
    assert "ADD COLUMN IF NOT EXISTS ready_notice_claimed_at TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS ready_notified_at TIMESTAMPTZ" in sql
    assert "MIN(starts_at)" in sql
    assert "status = 'trial' AND starts_at IS NOT NULL" in sql
    assert "COALESCE(last_synced_at, issued_at, created_at)" in sql
    assert (
        "status = 'active' AND config_uri IS NOT NULL AND ready_notified_at IS NULL"
        in sql
    )
    assert "vpn_ready_notice_backfill_v1" in sql
    assert "ON CONFLICT (key) DO NOTHING" in sql
    assert "RETURNING key" in sql


@pytest_asyncio.fixture
async def postgres_schema() -> AsyncIterator[AsyncEngine]:
    raw_url = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not raw_url:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for real PostgreSQL coverage")
    url = make_url(raw_url)
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.database == "veltrix_portal_test"
        and url.port not in (None, 5432)
        and not url.query
    ):
        raise ValueError("unsafe VPN_PORTAL_TEST_PG_URL")
    schema = f"vpn_public_trial_test_{uuid4().hex}"
    base_engine = create_async_engine(url, hide_parameters=True)
    engine = create_async_engine(
        url,
        connect_args={"server_settings": {"search_path": schema}},
        hide_parameters=True,
    )
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
            created = True
        yield engine
    finally:
        await engine.dispose()
        try:
            if created:
                async with base_engine.begin() as connection:
                    await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        finally:
            await base_engine.dispose()


async def _create_prechange_schema(engine: AsyncEngine, *, with_endpoint: bool) -> None:
    async with engine.begin() as connection:
        for statement in (
            "CREATE TABLE app_settings (id SERIAL PRIMARY KEY, key VARCHAR(128) UNIQUE NOT NULL, value TEXT)",
            "CREATE TABLE users (id SERIAL PRIMARY KEY)",
            "CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)",
            "CREATE TABLE vpn_customers (id SERIAL PRIMARY KEY, telegram_user_id VARCHAR(64), status VARCHAR(32) DEFAULT 'active')",
            "CREATE TABLE vpn_subscriptions (id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES vpn_customers(id), status VARCHAR(32), starts_at TIMESTAMPTZ)",
            "CREATE TABLE vpn_access_keys (id SERIAL PRIMARY KEY, subscription_id INTEGER REFERENCES vpn_subscriptions(id), worker_id INTEGER REFERENCES worker_nodes(id), config_uri TEXT, status VARCHAR(32), last_synced_at TIMESTAMPTZ, issued_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())",
            "CREATE TABLE vpn_friend_invitations (slot SMALLINT PRIMARY KEY, redeemed_at TIMESTAMPTZ, telegram_user_id VARCHAR(64), access_key_id INTEGER REFERENCES vpn_access_keys(id))",
        ):
            await connection.execute(text(statement))
        if with_endpoint:
            await connection.execute(
                text(
                    "CREATE TABLE vpn_endpoints (id SERIAL PRIMARY KEY, worker_id INTEGER REFERENCES worker_nodes(id), inbound_id INTEGER, public_host VARCHAR(255), port INTEGER, status VARCHAR(32), security VARCHAR(32), verified_at TIMESTAMPTZ, UNIQUE (id, worker_id))"
                )
            )


async def _run_trial_migrations(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        for statement in (
            *migrations.VPN_PUBLIC_TRIAL_MIGRATIONS,
            *VPN_ENDPOINT_MIGRATIONS,
        ):
            await connection.execute(text(statement))


async def _schema_contract(
    engine: AsyncEngine,
) -> tuple[
    dict[tuple[str, str], tuple[str, str, str | None]], set[str], dict[str, str]
]:
    async with engine.connect() as connection:
        columns = (
            await connection.execute(
                text("""
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = current_schema()
              AND (table_name, column_name) IN (
                ('vpn_customers', 'trial_started_at'),
                ('vpn_access_keys', 'ready_notice_claimed_at'),
                ('vpn_access_keys', 'ready_notified_at'),
                ('vpn_endpoints', 'max_active_profiles'),
                ('vpn_endpoints', 'capacity_warning_percent')
              )
        """)
            )
        ).all()
        indexes = (
            (
                await connection.execute(
                    text("""
            SELECT indexname FROM pg_indexes
            WHERE schemaname = current_schema()
              AND indexname = 'ix_vpn_customers_trial_started_at'
        """)
                )
            )
            .scalars()
            .all()
        )
        checks = (
            await connection.execute(
                text("""
            SELECT conname, pg_get_constraintdef(oid)
            FROM pg_constraint
            WHERE conrelid = 'vpn_endpoints'::regclass
              AND conname IN ('ck_vpn_endpoint_capacity', 'ck_vpn_endpoint_capacity_warning')
        """)
            )
        ).all()
    return (
        {
            (table, column): (data_type, nullable, default)
            for table, column, data_type, nullable, default in columns
        },
        set(indexes),
        dict(checks),
    )


def _assert_public_trial_contract(
    contract: tuple[
        dict[tuple[str, str], tuple[str, str, str | None]], set[str], dict[str, str]
    ],
) -> None:
    columns, indexes, checks = contract
    assert columns == {
        ("vpn_customers", "trial_started_at"): (
            "timestamp with time zone",
            "YES",
            None,
        ),
        ("vpn_access_keys", "ready_notice_claimed_at"): (
            "timestamp with time zone",
            "YES",
            None,
        ),
        ("vpn_access_keys", "ready_notified_at"): (
            "timestamp with time zone",
            "YES",
            None,
        ),
        ("vpn_endpoints", "max_active_profiles"): ("integer", "YES", None),
        ("vpn_endpoints", "capacity_warning_percent"): ("integer", "NO", "80"),
    }
    assert indexes == {"ix_vpn_customers_trial_started_at"}
    assert set(checks) == {
        "ck_vpn_endpoint_capacity",
        "ck_vpn_endpoint_capacity_warning",
    }
    assert "max_active_profiles IS NULL" in checks["ck_vpn_endpoint_capacity"]
    assert "max_active_profiles > 0" in checks["ck_vpn_endpoint_capacity"]
    assert "capacity_warning_percent >= 1" in checks["ck_vpn_endpoint_capacity_warning"]
    assert (
        "capacity_warning_percent <= 100" in checks["ck_vpn_endpoint_capacity_warning"]
    )


async def _existing_snapshot(
    engine: AsyncEngine,
) -> dict[str, list[tuple[object, ...]]]:
    columns_by_table = {
        "vpn_customers": "id, telegram_user_id, status",
        "vpn_subscriptions": "id, customer_id, status, starts_at",
        "vpn_access_keys": "id, subscription_id, worker_id, config_uri, status, last_synced_at, issued_at, created_at",
        "vpn_endpoints": "id, worker_id, inbound_id, public_host, port, status, security, verified_at",
        "vpn_friend_invitations": "slot, redeemed_at, telegram_user_id, access_key_id",
    }
    async with engine.connect() as connection:
        return {
            table: list(
                (
                    await connection.execute(
                        text(f"SELECT {columns} FROM {table} ORDER BY 1")
                    )
                ).all()
            )
            for table, columns in columns_by_table.items()
        }


@pytest.mark.asyncio
async def test_prechange_postgres_upgrade_backfills_once(
    postgres_schema: AsyncEngine,
) -> None:
    early = datetime(2026, 9, 1, tzinfo=UTC)
    late = datetime(2026, 9, 2, tzinfo=UTC)
    issued = datetime(2026, 9, 3, tzinfo=UTC)
    synced = datetime(2026, 9, 4, tzinfo=UTC)
    preserved = datetime(2026, 8, 1, tzinfo=UTC)
    await _create_prechange_schema(postgres_schema, with_endpoint=True)
    async with postgres_schema.begin() as connection:
        # Existing populated columns model a deployment interrupted before backfill.
        await connection.execute(
            text("ALTER TABLE vpn_customers ADD COLUMN trial_started_at TIMESTAMPTZ")
        )
        await connection.execute(
            text("ALTER TABLE vpn_access_keys ADD COLUMN ready_notified_at TIMESTAMPTZ")
        )
        await connection.execute(
            text(
                "INSERT INTO vpn_customers (id, telegram_user_id, trial_started_at) VALUES (1, 'ordinary', NULL), (2, 'friend', NULL), (3, 'prefilled', :preserved), (4, 'null-start', NULL)"
            ),
            {"preserved": preserved},
        )
        await connection.execute(
            text(
                "INSERT INTO vpn_subscriptions (id, customer_id, status, starts_at) VALUES (11, 2, 'trial', :late), (12, 2, 'trial', :early), (13, 1, 'active', :early), (14, 3, 'trial', :late), (15, 4, 'trial', NULL)"
            ),
            {"early": early, "late": late},
        )
        await connection.execute(
            text("""
                INSERT INTO vpn_access_keys
                    (id, subscription_id, config_uri, status, last_synced_at, issued_at, created_at, ready_notified_at)
                VALUES
                    (21, 12, 'vless://synced', 'active', :synced, :issued, :late, NULL),
                    (22, 13, 'vless://issued', 'active', NULL, :issued, :late, NULL),
                    (23, 13, 'vless://created', 'active', NULL, NULL, :late, NULL),
                    (24, 13, NULL, 'active', NULL, :issued, :late, NULL),
                    (25, 13, 'vless://inactive', 'revoked', NULL, :issued, :late, NULL),
                    (26, 13, 'vless://preserved', 'active', :synced, :issued, :late, :preserved)
            """),
            {"synced": synced, "issued": issued, "late": late, "preserved": preserved},
        )
        await connection.execute(
            text(
                "INSERT INTO vpn_friend_invitations (slot, redeemed_at, telegram_user_id, access_key_id) VALUES (1, :early, 'friend', 21)"
            ),
            {"early": early},
        )
        await connection.execute(text("INSERT INTO worker_nodes (id) VALUES (1)"))
        await connection.execute(
            text(
                "INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port, status, security) VALUES (1, 1, 1, 'example.test', 443, 'staged', 'none')"
            )
        )

    before = await _existing_snapshot(postgres_schema)
    await _run_trial_migrations(postgres_schema)
    assert await _existing_snapshot(postgres_schema) == before
    async with postgres_schema.begin() as connection:
        await connection.execute(
            text("""
                INSERT INTO vpn_access_keys
                    (id, subscription_id, config_uri, status, created_at)
                VALUES (27, 13, 'vless://new', 'active', :late)
            """),
            {"late": late},
        )
    before_second_run = await _existing_snapshot(postgres_schema)
    await _run_trial_migrations(postgres_schema)
    assert await _existing_snapshot(postgres_schema) == before_second_run

    async with postgres_schema.connect() as connection:
        customers = (
            await connection.execute(
                text("SELECT id, trial_started_at FROM vpn_customers ORDER BY id")
            )
        ).all()
        keys = (
            await connection.execute(
                text(
                    "SELECT id, ready_notice_claimed_at, ready_notified_at FROM vpn_access_keys ORDER BY id"
                )
            )
        ).all()
        endpoint = (
            await connection.execute(
                text(
                    "SELECT max_active_profiles, capacity_warning_percent FROM vpn_endpoints WHERE id = 1"
                )
            )
        ).one()
        friend = (
            await connection.execute(
                text(
                    "SELECT slot, redeemed_at, telegram_user_id, access_key_id FROM vpn_friend_invitations"
                )
            )
        ).one()
        assert customers == [(1, None), (2, early), (3, preserved), (4, None)]
        assert keys == [
            (21, None, synced),
            (22, None, issued),
            (23, None, late),
            (24, None, None),
            (25, None, None),
            (26, None, preserved),
            (27, None, None),
        ]
        assert endpoint == (None, 80)
        assert friend == (1, early, "friend", 21)
        assert (
            await connection.scalar(
                text(
                    "SELECT count(*) FROM app_settings WHERE key = 'vpn_ready_notice_backfill_v1'"
                )
            )
            == 1
        )
    _assert_public_trial_contract(await _schema_contract(postgres_schema))
    for values in ((0, 80), (-1, 80), (1, 0), (1, 101)):
        with pytest.raises(IntegrityError):
            async with postgres_schema.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE vpn_endpoints SET max_active_profiles=:capacity, capacity_warning_percent=:warning WHERE id=1"
                    ),
                    {"capacity": values[0], "warning": values[1]},
                )


@pytest.mark.asyncio
async def test_fresh_postgres_endpoint_create_matches_upgrade_contract(
    postgres_schema: AsyncEngine,
) -> None:
    await _create_prechange_schema(postgres_schema, with_endpoint=False)
    async with postgres_schema.connect() as connection:
        assert (
            await connection.scalar(text("SELECT to_regclass('vpn_endpoints')")) is None
        )
    await _run_trial_migrations(postgres_schema)
    await _run_trial_migrations(postgres_schema)
    fresh_contract = await _schema_contract(postgres_schema)
    _assert_public_trial_contract(fresh_contract)
    async with postgres_schema.connect() as connection:
        assert (
            await connection.scalar(text("SELECT to_regclass('vpn_endpoints')"))
            is not None
        )

    upgrade_schema = f"vpn_public_trial_upgrade_test_{uuid4().hex}"
    base_engine = create_async_engine(postgres_schema.url, hide_parameters=True)
    upgrade_engine = create_async_engine(
        postgres_schema.url,
        connect_args={"server_settings": {"search_path": upgrade_schema}},
        hide_parameters=True,
    )
    created = False
    try:
        async with base_engine.begin() as connection:
            await connection.execute(text(f'CREATE SCHEMA "{upgrade_schema}"'))
            created = True
        await _create_prechange_schema(upgrade_engine, with_endpoint=True)
        await _run_trial_migrations(upgrade_engine)
        upgraded_contract = await _schema_contract(upgrade_engine)
        _assert_public_trial_contract(upgraded_contract)
        assert fresh_contract == upgraded_contract
    finally:
        await upgrade_engine.dispose()
        try:
            if created:
                async with base_engine.begin() as connection:
                    await connection.execute(
                        text(f'DROP SCHEMA "{upgrade_schema}" CASCADE')
                    )
        finally:
            await base_engine.dispose()
