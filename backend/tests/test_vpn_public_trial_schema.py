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
    assert "trial_started_at" in VpnCustomer.__table__.columns
    assert {"max_active_profiles", "capacity_warning_percent"} <= set(VpnEndpoint.__table__.columns.keys())
    assert {"ready_notice_claimed_at", "ready_notified_at"} <= set(VpnAccessKey.__table__.columns.keys())
    constraints = {
        item.name: str(item.sqltext)
        for item in VpnEndpoint.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert constraints["ck_vpn_endpoint_capacity"] == "max_active_profiles IS NULL OR max_active_profiles > 0"
    assert constraints["ck_vpn_endpoint_capacity_warning"] == "capacity_warning_percent BETWEEN 1 AND 100"


def test_public_trial_settings_default_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.vpn_public_trial_enabled is False
    assert settings.vpn_public_trial_release_id == ""
    assert settings.vpn_public_trial_plan_slug == "trial-7d"
    assert settings.vpn_endpoint_health_max_age_seconds == 300
    assert settings.vpn_ready_notifications_enabled is False


def test_public_trial_upgrade_statements_are_registered() -> None:
    statements = getattr(migrations, "VPN_PUBLIC_TRIAL_MIGRATIONS", ())
    assert statements
    assert migrations.MIGRATIONS[-len(VPN_ENDPOINT_MIGRATIONS) - 1 : -1] == VPN_ENDPOINT_MIGRATIONS
    sql = "\n".join(statements)
    assert "ADD COLUMN IF NOT EXISTS trial_started_at TIMESTAMPTZ" in sql
    assert "ix_vpn_customers_trial_started_at" in sql
    assert "ADD COLUMN IF NOT EXISTS ready_notice_claimed_at TIMESTAMPTZ" in sql
    assert "ADD COLUMN IF NOT EXISTS ready_notified_at TIMESTAMPTZ" in sql
    assert "MIN(starts_at)" in sql
    assert "status = 'trial' AND starts_at IS NOT NULL" in sql
    assert "COALESCE(last_synced_at, issued_at, created_at)" in sql
    assert "status = 'active' AND config_uri IS NOT NULL AND ready_notified_at IS NULL" in sql


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


@pytest.mark.asyncio
async def test_prechange_postgres_upgrade_backfills_once(postgres_schema: AsyncEngine) -> None:
    early = datetime(2026, 9, 1, tzinfo=UTC)
    late = datetime(2026, 9, 2, tzinfo=UTC)
    issued = datetime(2026, 9, 3, tzinfo=UTC)
    async with postgres_schema.begin() as connection:
        for statement in (
            "CREATE TABLE users (id SERIAL PRIMARY KEY)",
            "CREATE TABLE worker_nodes (id SERIAL PRIMARY KEY)",
            "CREATE TABLE vpn_customers (id SERIAL PRIMARY KEY, telegram_user_id VARCHAR(64), status VARCHAR(32) DEFAULT 'active')",
            "CREATE TABLE vpn_subscriptions (id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES vpn_customers(id), status VARCHAR(32), starts_at TIMESTAMPTZ)",
            "CREATE TABLE vpn_access_keys (id SERIAL PRIMARY KEY, subscription_id INTEGER REFERENCES vpn_subscriptions(id), worker_id INTEGER REFERENCES worker_nodes(id), config_uri TEXT, status VARCHAR(32), last_synced_at TIMESTAMPTZ, issued_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT NOW())",
            "CREATE TABLE vpn_endpoints (id SERIAL PRIMARY KEY, worker_id INTEGER REFERENCES worker_nodes(id), inbound_id INTEGER, public_host VARCHAR(255), port INTEGER, status VARCHAR(32), security VARCHAR(32), verified_at TIMESTAMPTZ, UNIQUE (id, worker_id))",
            "CREATE TABLE vpn_friend_invitations (slot SMALLINT PRIMARY KEY, redeemed_at TIMESTAMPTZ, telegram_user_id VARCHAR(64), access_key_id INTEGER REFERENCES vpn_access_keys(id))",
        ):
            await connection.execute(text(statement))
        await connection.execute(text("INSERT INTO vpn_customers (id, telegram_user_id) VALUES (1, 'ordinary'), (2, 'friend')"))
        await connection.execute(
            text("INSERT INTO vpn_subscriptions (id, customer_id, status, starts_at) VALUES (11, 2, 'trial', :late), (12, 2, 'trial', :early), (13, 1, 'active', :early)"),
            {"early": early, "late": late},
        )
        await connection.execute(
            text("INSERT INTO vpn_access_keys (id, subscription_id, config_uri, status, issued_at) VALUES (21, 12, 'vless://ready', 'active', :issued), (22, 13, NULL, 'active', :issued)"),
            {"issued": issued},
        )
        await connection.execute(text("INSERT INTO vpn_friend_invitations (slot, redeemed_at, telegram_user_id, access_key_id) VALUES (1, NOW(), 'friend', 21)"))
        await connection.execute(text("INSERT INTO worker_nodes (id) VALUES (1)"))
        await connection.execute(text("INSERT INTO vpn_endpoints (id, worker_id, inbound_id, public_host, port, status, security) VALUES (1, 1, 1, 'example.test', 443, 'staged', 'none')"))

    for _ in range(2):
        async with postgres_schema.begin() as connection:
            for statement in migrations.VPN_PUBLIC_TRIAL_MIGRATIONS:
                await connection.execute(text(statement))
            for statement in VPN_ENDPOINT_MIGRATIONS:
                await connection.execute(text(statement))

    async with postgres_schema.connect() as connection:
        customers = (await connection.execute(text("SELECT id, trial_started_at FROM vpn_customers ORDER BY id"))).all()
        keys = (await connection.execute(text("SELECT id, ready_notice_claimed_at, ready_notified_at FROM vpn_access_keys ORDER BY id"))).all()
        endpoint = (await connection.execute(text("SELECT max_active_profiles, capacity_warning_percent FROM vpn_endpoints WHERE id = 1"))).one()
        assert customers == [(1, None), (2, early)]
        assert keys[0] == (21, None, issued)
        assert keys[1] == (22, None, None)
        assert endpoint == (None, 80)
    for values in ((0, 80), (-1, 80), (1, 0), (1, 101)):
        with pytest.raises(IntegrityError):
            async with postgres_schema.begin() as connection:
                await connection.execute(
                    text("UPDATE vpn_endpoints SET max_active_profiles=:capacity, capacity_warning_percent=:warning WHERE id=1"),
                    {"capacity": values[0], "warning": values[1]},
                )
