from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import event, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db import models
from app.db.base import Base
from app.db.migrations import MIGRATIONS


PORTAL_TABLES = {
    "vpn_customer_sessions",
    "vpn_portal_login_attempts",
    "vpn_portal_mini_app_exchanges",
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


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@pytest.mark.asyncio
async def test_fresh_metadata_creates_portal_tables_with_digest_only_columns():
    engine, _session_factory = await _make_session_factory()

    async with engine.connect() as connection:
        table_names = await connection.run_sync(lambda sync_connection: set(inspect(sync_connection).get_table_names()))
        columns = {
            table_name: await connection.run_sync(
                lambda sync_connection, name=table_name: {
                    column["name"] for column in inspect(sync_connection).get_columns(name)
                }
            )
            for table_name in PORTAL_TABLES
        }

    assert PORTAL_TABLES <= table_names
    assert columns["vpn_customer_sessions"] == {
        "token_hash",
        "customer_id",
        "telegram_user_id",
        "created_at",
        "expires_at",
        "revoked_at",
    }
    assert columns["vpn_portal_login_attempts"] == {
        "state_hash",
        "binding_hash",
        "code_verifier",
        "created_at",
        "expires_at",
        "consumed_at",
    }
    assert columns["vpn_portal_mini_app_exchanges"] == {
        "digest",
        "customer_id",
        "created_at",
        "expires_at",
    }
    assert "user_sessions" in table_names
    assert PORTAL_TABLES.isdisjoint({"user_sessions"})

    await engine.dispose()


def test_legacy_access_key_construction_defaults_display_name_to_none():
    access_key = models.VpnAccessKey(
        subscription_id=1,
        public_name="Legacy public label",
        external_uuid="legacy-uuid",
        config_uri="vless://legacy-uuid@example.test",
        status="active",
    )

    assert access_key.display_name is None
    assert access_key.public_name == "Legacy public label"


@pytest.mark.asyncio
async def test_portal_records_and_access_key_display_name_round_trip():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        customer = models.VpnCustomer(telegram_user_id="portal-customer")
        plan = models.VpnPlan(slug="portal-plan", name="Portal plan")
        session.add_all([customer, plan])
        await session.flush()
        subscription = models.VpnSubscription(
            customer_id=customer.id,
            plan_id=plan.id,
            starts_at=now,
            expires_at=now + timedelta(days=30),
        )
        session.add(subscription)
        await session.flush()
        session.add_all(
            [
                models.VpnAccessKey(
                    subscription_id=subscription.id,
                    display_name="My phone",
                    public_name="Existing public label",
                    external_uuid="portal-key-uuid",
                    config_uri="vless://portal-key-uuid@example.test",
                ),
                models.VpnCustomerSession(
                    token_hash="a" * 64,
                    customer_id=customer.id,
                    telegram_user_id="portal-customer",
                    expires_at=now + timedelta(hours=1),
                    revoked_at=now + timedelta(minutes=30),
                ),
                models.VpnPortalLoginAttempt(
                    state_hash="b" * 64,
                    binding_hash="c" * 64,
                    code_verifier="temporary-verifier",
                    expires_at=now + timedelta(minutes=5),
                    consumed_at=now + timedelta(minutes=1),
                ),
                models.VpnPortalMiniAppExchange(
                    digest="d" * 64,
                    customer_id=customer.id,
                    expires_at=now + timedelta(minutes=2),
                ),
            ]
        )
        await session.commit()

    async with session_factory() as session:
        access_key = (await session.execute(select(models.VpnAccessKey))).scalar_one()
        customer_session = (await session.execute(select(models.VpnCustomerSession))).scalar_one()
        login_attempt = (await session.execute(select(models.VpnPortalLoginAttempt))).scalar_one()
        exchange = (await session.execute(select(models.VpnPortalMiniAppExchange))).scalar_one()

    assert access_key.display_name == "My phone"
    assert access_key.public_name == "Existing public label"
    assert customer_session.telegram_user_id == "portal-customer"
    assert _as_utc(customer_session.expires_at) == now + timedelta(hours=1)
    assert _as_utc(customer_session.revoked_at) == now + timedelta(minutes=30)
    assert login_attempt.binding_hash == "c" * 64
    assert login_attempt.code_verifier == "temporary-verifier"
    assert _as_utc(login_attempt.expires_at) == now + timedelta(minutes=5)
    assert _as_utc(login_attempt.consumed_at) == now + timedelta(minutes=1)
    assert _as_utc(exchange.expires_at) == now + timedelta(minutes=2)
    assert all(record.created_at is not None for record in (customer_session, login_attempt, exchange))

    await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "values"),
    [
        (
            "VpnCustomerSession",
            {
                "token_hash": "e" * 64,
                "telegram_user_id": "duplicate-customer",
            },
        ),
        (
            "VpnPortalLoginAttempt",
            {
                "state_hash": "f" * 64,
                "binding_hash": "0" * 64,
            },
        ),
        ("VpnPortalMiniAppExchange", {"digest": "1" * 64}),
    ],
)
async def test_portal_digest_primary_keys_reject_duplicates_independently(model_name, values):
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        customer = models.VpnCustomer(telegram_user_id=f"customer-{model_name}")
        session.add(customer)
        await session.commit()
        customer_id = customer.id

    model_type = getattr(models, model_name)
    common_values = {"expires_at": now + timedelta(minutes=5), **values}
    if model_name != "VpnPortalLoginAttempt":
        common_values["customer_id"] = customer_id

    async with session_factory() as session:
        session.add(model_type(**common_values))
        await session.commit()

    async with session_factory() as session:
        session.add(model_type(**common_values))
        with pytest.raises(IntegrityError):
            await session.commit()
        await session.rollback()

    await engine.dispose()


@pytest.mark.asyncio
async def test_customer_deletion_cascades_portal_customer_records():
    engine, session_factory = await _make_session_factory()
    now = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)

    async with session_factory() as session:
        customer = models.VpnCustomer(telegram_user_id="cascade-customer")
        session.add(customer)
        await session.flush()
        session.add_all(
            [
                models.VpnCustomerSession(
                    token_hash="2" * 64,
                    customer_id=customer.id,
                    telegram_user_id="cascade-customer",
                    expires_at=now + timedelta(hours=1),
                ),
                models.VpnPortalMiniAppExchange(
                    digest="3" * 64,
                    customer_id=customer.id,
                    expires_at=now + timedelta(minutes=2),
                ),
            ]
        )
        await session.commit()
        await session.delete(customer)
        await session.commit()

    async with session_factory() as session:
        customer_sessions = (await session.execute(select(models.VpnCustomerSession))).scalars().all()
        exchanges = (await session.execute(select(models.VpnPortalMiniAppExchange))).scalars().all()

    assert customer_sessions == []
    assert exchanges == []

    await engine.dispose()


def test_portal_settings_default_disabled_and_accept_explicit_aliases(monkeypatch):
    aliases = {
        "VPN_PORTAL_ENABLED": "true",
        "VPN_PORTAL_PUBLIC_ORIGIN": "https://portal.example.test",
        "VPN_PORTAL_ALLOW_LOCAL_HTTP": "true",
        "VPN_PORTAL_PUBLIC_ACCESS": "true",
        "VPN_PORTAL_ALLOWED_TELEGRAM_IDS": "123,456",
        "VPN_PORTAL_OIDC_CLIENT_ID": "client-id",
        "VPN_PORTAL_OIDC_CLIENT_SECRET": "client-secret",
    }
    for alias in aliases:
        monkeypatch.delenv(alias, raising=False)

    defaults = Settings(_env_file=None)
    configured = Settings(_env_file=None, **aliases)

    assert defaults.vpn_portal_enabled is False
    assert defaults.vpn_portal_public_origin == ""
    assert defaults.vpn_portal_allow_local_http is False
    assert defaults.vpn_portal_public_access is False
    assert defaults.vpn_portal_allowed_telegram_ids == ""
    assert defaults.vpn_portal_oidc_client_id == ""
    assert defaults.vpn_portal_oidc_client_secret == ""
    assert configured.vpn_portal_enabled is True
    assert configured.vpn_portal_public_origin == "https://portal.example.test"
    assert configured.vpn_portal_allow_local_http is True
    assert configured.vpn_portal_public_access is True
    assert configured.vpn_portal_allowed_telegram_ids == "123,456"
    assert configured.vpn_portal_oidc_client_id == "client-id"
    assert configured.vpn_portal_oidc_client_secret == "client-secret"
    assert "client-secret" not in repr(configured)


def test_display_name_upgrade_migration_is_idempotent_postgres_sql():
    assert "ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS display_name VARCHAR(64) NULL" in MIGRATIONS


def test_backend_declares_pyjwt_crypto_dependency():
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))

    assert "PyJWT[crypto]>=2.10,<3" in pyproject["project"]["dependencies"]
