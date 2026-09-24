from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import UUID

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import event, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.schemas.vpn_portal import (
    MiniAppLogin,
    PortalConnection,
    PortalMe,
    PortalProfile,
    PortalSubscription,
    RenamePortalProfile,
)
from app.services.vpn_customer_view import (
    MissingCustomerProfile,
    UnavailableCustomerConnection,
    customer_connection,
    list_customer_profiles,
    list_customer_subscriptions,
    may_read_connection,
    rename_customer_profile,
    subscription_state,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
VALID_URI = "vless://11111111-1111-4111-8111-111111111111@vpn.example.test:443?security=tls#legacy"


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    database_path = (tmp_path / "vpn-customer-view.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_customer_view(factory) -> SimpleNamespace:
    async with factory() as session:
        own = VpnCustomer(
            telegram_user_id="100",
            telegram_username="private_handle",
            first_name="Private",
            notes="customer secret",
            status="active",
        )
        foreign = VpnCustomer(telegram_user_id="200", status="active")
        session.add_all([own, foreign])
        await session.flush()

        subscriptions = [
            VpnSubscription(
                customer_id=own.id,
                status="active",
                starts_at=NOW - timedelta(days=1),
                expires_at=NOW + timedelta(days=10),
                max_devices=1,
                traffic_limit_gb=25,
                notes="subscription secret",
            ),
            VpnSubscription(
                customer_id=own.id,
                status="trial",
                starts_at=NOW - timedelta(hours=1),
                expires_at=None,
                max_devices=2,
            ),
            VpnSubscription(
                customer_id=own.id,
                status="active",
                starts_at=NOW - timedelta(days=5),
                expires_at=NOW,
                max_devices=2,
            ),
            VpnSubscription(
                customer_id=own.id,
                status="active",
                starts_at=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(days=20),
                max_devices=2,
            ),
            VpnSubscription(
                customer_id=own.id,
                status="disabled",
                expires_at=NOW + timedelta(days=30),
                max_devices=2,
            ),
            VpnSubscription(
                customer_id=own.id,
                status="cancelled",
                expires_at=NOW + timedelta(days=40),
                max_devices=2,
            ),
            VpnSubscription(
                customer_id=foreign.id,
                status="active",
                expires_at=NOW + timedelta(days=5),
                max_devices=2,
            ),
        ]
        session.add_all(subscriptions)
        await session.flush()

        statuses = [
            "pending_sync",
            "syncing",
            "active",
            "pending_revoke",
            "pending_suspend",
            "suspended",
            "failed",
            "revoked",
        ]
        keys = []
        for index, status in enumerate(statuses, start=1):
            keys.append(
                VpnAccessKey(
                    subscription_id=subscriptions[0].id,
                    worker_id=None,
                    public_name=f"legacy-{index}",
                    display_name=f"Profile {index}",
                    external_uuid=f"secret-uuid-{index}",
                    config_uri=VALID_URI if status == "active" else None,
                    status=status,
                    last_error="raw worker error",
                )
            )
        keys.extend(
            [
                VpnAccessKey(
                    subscription_id=subscriptions[4].id,
                    display_name=None,
                    status="failed",
                ),
                VpnAccessKey(
                    subscription_id=subscriptions[6].id,
                    display_name="Foreign",
                    config_uri=VALID_URI,
                    status="active",
                ),
            ]
        )
        session.add_all(keys)
        await session.commit()
        return SimpleNamespace(
            own_id=own.id,
            foreign_id=foreign.id,
            subscription_ids=[item.id for item in subscriptions],
            key_ids=[item.id for item in keys],
        )


def test_portal_schemas_are_explicit_safe_and_validate_writable_payloads() -> None:
    assert PortalMe(display_name="Customer", csrf_token="csrf").model_dump() == {
        "display_name": "Customer",
        "csrf_token": "csrf",
    }
    assert PortalSubscription(
        id=1,
        state="active",
        starts_at=None,
        expires_at=None,
        profile_limit=2,
        profiles_used=1,
        traffic_limit_gb_per_profile=25,
    ).model_dump() == {
        "id": 1,
        "service_name": "Veltrix VPN",
        "state": "active",
        "starts_at": None,
        "expires_at": None,
        "profile_limit": 2,
        "profiles_used": 1,
        "traffic_limit_gb_per_profile": 25,
    }
    assert PortalProfile(
        id=2,
        subscription_id=1,
        display_name="Phone",
        state="active",
        can_connect=True,
    ).model_dump() == {
        "id": 2,
        "subscription_id": 1,
        "display_name": "Phone",
        "state": "active",
        "can_connect": True,
    }
    assert PortalConnection(uri="vless://safe").model_dump() == {"uri": "vless://safe"}

    assert RenamePortalProfile(display_name="  Домашний ноутбук  ").display_name == "Домашний ноутбук"
    assert RenamePortalProfile(display_name="  " + "я" * 64 + "  ").display_name == "я" * 64
    for model, values in (
        (RenamePortalProfile, {"display_name": "Phone", "status": "active"}),
        (MiniAppLogin, {"init_data": "signed", "customer_id": 1}),
    ):
        with pytest.raises(ValidationError) as error:
            model.model_validate(values)
        assert error.value.errors()[0]["type"] == "extra_forbidden"

    for display_name in ("   ", "x" * 65, "bad\x00name"):
        with pytest.raises(ValidationError, match="invalid_display_name"):
            RenamePortalProfile(display_name=display_name)
    for init_data in ("", "x" * 16385):
        with pytest.raises(ValidationError):
            MiniAppLogin(init_data=init_data)


@pytest.mark.parametrize(
    ("status", "starts_at", "expires_at", "expected"),
    [
        ("active", None, None, "active"),
        ("trial", NOW - timedelta(days=1), NOW + timedelta(days=1), "trial"),
        ("active", NOW + timedelta(seconds=1), NOW - timedelta(seconds=1), "scheduled"),
        ("trial", None, NOW, "expired"),
        ("expired", NOW + timedelta(days=1), NOW + timedelta(days=2), "expired"),
        ("disabled", None, None, "disabled"),
        ("cancelled", None, None, "cancelled"),
        ("internal_secret_state", None, None, "unavailable"),
    ],
)
def test_subscription_state_has_safe_precedence_and_normalizes_timezones(
    status: str,
    starts_at: datetime | None,
    expires_at: datetime | None,
    expected: str,
) -> None:
    subscription = SimpleNamespace(
        status=status,
        starts_at=starts_at.replace(tzinfo=None) if starts_at else None,
        expires_at=expires_at.astimezone(timezone(timedelta(hours=5))) if expires_at else None,
    )

    assert subscription_state(subscription, NOW) == expected


def test_may_read_connection_checks_entitlement_but_not_full_slot_count() -> None:
    customer = SimpleNamespace(status="active")
    subscription = SimpleNamespace(
        status="active",
        starts_at=NOW - timedelta(days=1),
        expires_at=NOW + timedelta(days=1),
        max_devices=1,
    )
    key = SimpleNamespace(
        status="active",
        expires_at=NOW + timedelta(minutes=1),
        config_uri=VALID_URI,
        display_name="Phone",
    )

    assert may_read_connection(customer, subscription, key, NOW) is True
    key.config_uri = "vless://raw-secret-that-is-malformed"
    assert may_read_connection(customer, subscription, key, NOW) is False
    key.config_uri = VALID_URI
    key.expires_at = NOW
    assert may_read_connection(customer, subscription, key, NOW) is False
    key.expires_at = None
    key.status = "mystery"
    assert may_read_connection(customer, subscription, key, NOW) is False


@pytest.mark.asyncio
async def test_lists_are_owned_safe_complete_counted_and_deterministic(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        subscriptions = await list_customer_subscriptions(session, customer.id, now=NOW)
        profiles = await list_customer_profiles(session, customer, now=NOW)

    assert [item.id for item in subscriptions] == [
        ids.subscription_ids[0],
        ids.subscription_ids[1],
        ids.subscription_ids[2],
        ids.subscription_ids[3],
        ids.subscription_ids[4],
        ids.subscription_ids[5],
    ]
    assert [item.state for item in subscriptions] == [
        "active",
        "trial",
        "expired",
        "scheduled",
        "disabled",
        "cancelled",
    ]
    assert subscriptions[0].profiles_used == 7
    assert subscriptions[0].profile_limit == 1
    assert subscriptions[0].traffic_limit_gb_per_profile == 25
    assert subscriptions[4].profiles_used == 1
    assert all(item.service_name == "Veltrix VPN" for item in subscriptions)

    assert [item.id for item in profiles] == ids.key_ids[:-1]
    assert [item.state for item in profiles[:8]] == [
        "pending_sync",
        "syncing",
        "active",
        "pending_revoke",
        "pending_suspend",
        "suspended",
        "failed",
        "revoked",
    ]
    assert profiles[2].can_connect is True
    assert sum(item.can_connect for item in profiles) == 1
    assert profiles[-1].display_name == "Профиль"

    sensitive_fields = {
        "config_uri",
        "external_uuid",
        "public_name",
        "notes",
        "worker_id",
        "last_error",
        "telegram_user_id",
        "telegram_username",
        "first_name",
        "last_name",
    }
    assert all(sensitive_fields.isdisjoint(item.model_dump()) for item in subscriptions)
    assert all(sensitive_fields.isdisjoint(item.model_dump()) for item in profiles)


@pytest.mark.asyncio
async def test_list_queries_apply_customer_ownership_in_sql(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    statements: list[str] = []

    def capture_sql(connection, clauseelement, multiparams, params, execution_options):
        del connection, multiparams, params, execution_options
        statements.append(
            " ".join(str(clauseelement.compile(dialect=postgresql.dialect())).lower().split())
        )

    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        event.listen(session.bind.sync_engine, "before_execute", capture_sql)
        try:
            await list_customer_subscriptions(session, customer.id, now=NOW)
            await list_customer_profiles(session, customer, now=NOW)
        finally:
            event.remove(session.bind.sync_engine, "before_execute", capture_sql)

    subscription_queries = [statement for statement in statements if "from vpn_subscriptions" in statement]
    profile_queries = [
        statement
        for statement in statements
        if "from vpn_access_keys join vpn_subscriptions" in statement
    ]
    assert any("vpn_subscriptions.customer_id =" in statement for statement in subscription_queries)
    assert any("vpn_subscriptions.customer_id =" in statement for statement in profile_queries)


@pytest.mark.asyncio
async def test_connection_hides_foreign_and_missing_profiles_with_same_error(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        own = await customer_connection(session, customer, ids.key_ids[2], now=NOW)
        assert own.uri != VALID_URI
        assert own.uri.startswith(VALID_URI.split("#", 1)[0])
        assert "%C2%B7" in own.uri

        errors = []
        for profile_id in (ids.key_ids[-1], 999_999):
            with pytest.raises(MissingCustomerProfile) as error:
                await customer_connection(session, customer, profile_id, now=NOW)
            errors.append((type(error.value), error.value.args))
        assert errors[0] == errors[1]
        assert "secret" not in str(errors[0]).lower()


@pytest.mark.asyncio
async def test_malformed_owned_connection_fails_closed_without_leaking_uri(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    malformed = "vless://raw-secret-that-must-not-leak"
    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        key = await session.get(VpnAccessKey, ids.key_ids[2])
        assert customer is not None and key is not None
        key.config_uri = malformed
        await session.commit()

    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        profiles = await list_customer_profiles(session, customer, now=NOW)
        assert next(item for item in profiles if item.id == ids.key_ids[2]).can_connect is False
        with pytest.raises(UnavailableCustomerConnection) as error:
            await customer_connection(session, customer, ids.key_ids[2], now=NOW)
        assert malformed not in str(error.value)


@pytest.mark.asyncio
async def test_connection_refreshes_cached_customer_subscription_and_key_state(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    async with session_factory() as cached_session:
        customer = await cached_session.get(VpnCustomer, ids.own_id)
        subscription = await cached_session.get(VpnSubscription, ids.subscription_ids[0])
        key = await cached_session.get(VpnAccessKey, ids.key_ids[2])
        assert customer is not None and subscription is not None and key is not None

        async with session_factory() as writer:
            await writer.execute(
                update(VpnCustomer).where(VpnCustomer.id == customer.id).values(status="archived")
            )
            await writer.commit()
        with pytest.raises(UnavailableCustomerConnection):
            await customer_connection(cached_session, customer, key.id, now=NOW)

        async with session_factory() as writer:
            await writer.execute(
                update(VpnCustomer).where(VpnCustomer.id == customer.id).values(status="active")
            )
            await writer.execute(
                update(VpnSubscription)
                .where(VpnSubscription.id == subscription.id)
                .values(expires_at=NOW)
            )
            await writer.commit()
        with pytest.raises(UnavailableCustomerConnection):
            await customer_connection(cached_session, customer, key.id, now=NOW)

        async with session_factory() as writer:
            await writer.execute(
                update(VpnSubscription)
                .where(VpnSubscription.id == subscription.id)
                .values(expires_at=NOW + timedelta(days=1))
            )
            await writer.execute(
                update(VpnAccessKey).where(VpnAccessKey.id == key.id).values(status="revoked")
            )
            await writer.commit()
        with pytest.raises(UnavailableCustomerConnection):
            await customer_connection(cached_session, customer, key.id, now=NOW)

        async with session_factory() as writer:
            await writer.execute(
                update(VpnAccessKey)
                .where(VpnAccessKey.id == key.id)
                .values(status="active", subscription_id=ids.subscription_ids[6])
            )
            await writer.commit()
        with pytest.raises(MissingCustomerProfile):
            await customer_connection(cached_session, customer, key.id, now=NOW)


@pytest.mark.asyncio
async def test_rename_owns_and_locks_profile_allows_inactive_and_changes_only_name(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    statements: list[str] = []

    def capture_sql(connection, clauseelement, multiparams, params, execution_options):
        del connection, multiparams, params, execution_options
        statements.append(
            " ".join(str(clauseelement.compile(dialect=postgresql.dialect())).lower().split())
        )

    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        key = await session.get(VpnAccessKey, ids.key_ids[2])
        subscription = await session.get(VpnSubscription, ids.subscription_ids[0])
        assert customer is not None and key is not None and subscription is not None
        subscription.expires_at = NOW
        before = {
            "subscription_id": key.subscription_id,
            "worker_id": key.worker_id,
            "public_name": key.public_name,
            "external_uuid": key.external_uuid,
            "config_uri": key.config_uri,
            "status": key.status,
            "expires_at": key.expires_at,
            "last_error": key.last_error,
        }
        await session.flush()

        event.listen(session.bind.sync_engine, "before_execute", capture_sql)
        try:
            profile = await rename_customer_profile(
                session, customer, key.id, "  Путешествие  ", now=NOW
            )
        finally:
            event.remove(session.bind.sync_engine, "before_execute", capture_sql)

        assert profile.display_name == "Путешествие"
        assert profile.can_connect is False
        assert {
            "subscription_id": key.subscription_id,
            "worker_id": key.worker_id,
            "public_name": key.public_name,
            "external_uuid": key.external_uuid,
            "config_uri": key.config_uri,
            "status": key.status,
            "expires_at": key.expires_at,
            "last_error": key.last_error,
        } == before
        await session.commit()

    lock_query = next(
        statement
        for statement in statements
        if "from vpn_access_keys join vpn_subscriptions" in statement
    )
    assert "vpn_subscriptions.customer_id =" in lock_query
    assert lock_query.endswith("for update of vpn_access_keys")

    async with session_factory() as session:
        key = await session.get(VpnAccessKey, ids.key_ids[2])
        assert key is not None and key.display_name == "Путешествие"


@pytest.mark.asyncio
async def test_rename_foreign_and_missing_profiles_have_same_error(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        errors = []
        for profile_id in (ids.key_ids[-1], 999_999):
            with pytest.raises(MissingCustomerProfile) as error:
                await rename_customer_profile(session, customer, profile_id, "Safe", now=NOW)
            errors.append((type(error.value), error.value.args))
        assert errors[0] == errors[1]


@pytest.mark.asyncio
async def test_unknown_key_state_is_unavailable_and_never_connectable(session_factory) -> None:
    ids = await _seed_customer_view(session_factory)
    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        key = await session.get(VpnAccessKey, ids.key_ids[2])
        assert customer is not None and key is not None
        key.status = "worker_internal_secret"
        await session.commit()

    async with session_factory() as session:
        customer = await session.get(VpnCustomer, ids.own_id)
        assert customer is not None
        profiles = await list_customer_profiles(session, customer, now=NOW)
        profile = next(item for item in profiles if item.id == ids.key_ids[2])
        assert profile.state == "unavailable"
        assert profile.can_connect is False


def test_valid_uuid_in_fixture_is_canonical() -> None:
    assert str(UUID(VALID_URI.split("://", 1)[1].split("@", 1)[0])) in VALID_URI
