from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from collections.abc import AsyncIterator
from urllib.parse import urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription, VpnTelegramUpdate
from app.services.vpn_portal_telegram import (
    TelegramAuthenticationError,
    verify_mini_app,
)
from app.services.vpn_telegram_identity import (
    TelegramIdentity,
    identity_from_user,
    optional_text,
    resolve_telegram_customer,
    telegram_user_id,
)


BOT_TOKEN = "12345:test-only"
NOW_SECONDS = 1_800_000_000


def _sign_fields(fields: dict[str, str], token: str = BOT_TOKEN) -> tuple[str, str]:
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    signature = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return signature, check


def signed_init_data(
    user_id: object,
    timestamp: int = NOW_SECONDS,
    token: str = BOT_TOKEN,
    *,
    user_overrides: dict[str, object] | None = None,
    extra_fields: dict[str, str] | None = None,
) -> str:
    user = {"id": user_id, "first_name": "Тест"}
    if user_overrides:
        user.update(user_overrides)
    fields = {
        "auth_date": str(timestamp),
        "query_id": "test-query",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
        **(extra_fields or {}),
    }
    fields["hash"], _check = _sign_fields(fields, token)
    return urlencode(fields)


def signed_raw_fields(fields: dict[str, str], token: str = BOT_TOKEN) -> str:
    signed_fields = dict(fields)
    signed_fields["hash"], _check = _sign_fields(signed_fields, token)
    return urlencode(signed_fields)


def assert_authentication_failure(raw: object, token: object = BOT_TOKEN) -> None:
    with pytest.raises(TelegramAuthenticationError) as error:
        verify_mini_app(raw, token, NOW_SECONDS)  # type: ignore[arg-type]
    assert str(error.value) == "telegram_authentication_failed"
    assert error.value.__cause__ is None


def test_identity_normalizes_id_and_truncates_optional_names():
    identity = identity_from_user(
        {
            "id": "000123",
            "sub": "999999",
            "username": "u" * 140,
            "first_name": "f" * 129,
            "last_name": "",
        }
    )

    assert identity == TelegramIdentity(
        user_id="123",
        username="u" * 128,
        first_name="f" * 128,
        last_name=None,
    )
    assert optional_text(17, 128) is None
    assert optional_text(None, 128) is None


@pytest.mark.parametrize(
    "value",
    [
        True,
        False,
        None,
        1.0,
        -1,
        0,
        "",
        "-1",
        "12.3",
        "１２３",
        "١٢٣",
        str(2**52),
        "9" * 10_000,
    ],
)
def test_telegram_user_id_rejects_noncanonical_or_out_of_range_values(value):
    with pytest.raises(ValueError, match="^invalid_identity$"):
        telegram_user_id(value)


def test_telegram_user_id_rejects_pathologically_large_integer_with_safe_error():
    with pytest.raises(ValueError, match="^invalid_identity$"):
        telegram_user_id(10**5_000)


def test_identity_never_falls_back_to_sub_or_username():
    with pytest.raises(ValueError, match="^invalid_identity$"):
        identity_from_user({"sub": "123", "username": "123"})


def test_verify_mini_app_accepts_valid_signed_data():
    raw = signed_init_data(
        123456,
        user_overrides={"username": "client", "last_name": "Проверка"},
    )

    identity, digest, timestamp = verify_mini_app(raw, BOT_TOKEN, NOW_SECONDS)

    assert identity == TelegramIdentity(
        user_id="123456",
        username="client",
        first_name="Тест",
        last_name="Проверка",
    )
    assert len(digest) == 64
    assert timestamp == NOW_SECONDS


@pytest.mark.parametrize("age_delta", [-300, 30])
def test_verify_mini_app_accepts_inclusive_age_boundaries(age_delta):
    raw = signed_init_data(123456, NOW_SECONDS + age_delta)

    _identity, _digest, timestamp = verify_mini_app(raw, BOT_TOKEN, NOW_SECONDS)

    assert timestamp == NOW_SECONDS + age_delta


@pytest.mark.parametrize("age_delta", [-301, 31])
def test_verify_mini_app_rejects_outside_age_boundaries(age_delta):
    assert_authentication_failure(signed_init_data(123456, NOW_SECONDS + age_delta))


def test_verify_mini_app_rejects_wrong_token_and_modified_hash():
    raw = signed_init_data(123456)
    fields = dict(pair.split("=", 1) for pair in raw.split("&"))
    fields["hash"] = "0" * 64

    assert_authentication_failure(raw, "54321:wrong-test-token")
    assert_authentication_failure("&".join(f"{key}={value}" for key, value in fields.items()))


def test_verify_mini_app_includes_signature_field_in_bot_token_hmac():
    raw = signed_init_data(123456, extra_fields={"signature": "third-party-signature"})

    identity, _digest, _timestamp = verify_mini_app(raw, BOT_TOKEN, NOW_SECONDS)

    assert identity.user_id == "123456"


def test_verify_mini_app_rejects_duplicate_decoded_query_keys():
    raw = signed_init_data(123456)
    duplicate = raw.replace("auth_date=", "%61uth_date=", 1) + f"&auth_date={NOW_SECONDS}"

    assert_authentication_failure(duplicate)


@pytest.mark.parametrize(
    "fields",
    [
        {"auth_date": str(NOW_SECONDS), "query_id": "test-query"},
        {"user": '{"id":123}', "query_id": "test-query"},
        {"auth_date": "not-a-time", "user": '{"id":123}'},
        {"auth_date": "1234567890123", "user": '{"id":123}'},
        {"auth_date": str(NOW_SECONDS), "user": "not-json"},
        {"auth_date": str(NOW_SECONDS), "user": "[]"},
        {"auth_date": str(NOW_SECONDS), "user": '{"sub":"123"}'},
    ],
)
def test_verify_mini_app_rejects_missing_or_malformed_signed_fields(fields):
    assert_authentication_failure(signed_raw_fields(fields))


@pytest.mark.parametrize("user_id", [True, -1, 2**52, "１２３", "9" * 10_000])
def test_verify_mini_app_rejects_invalid_user_ids(user_id):
    assert_authentication_failure(signed_init_data(user_id))


def test_verify_mini_app_normalizes_leading_zero_user_id():
    identity, _digest, _timestamp = verify_mini_app(
        signed_init_data("000000123"),
        BOT_TOKEN,
        NOW_SECONDS,
    )

    assert identity.user_id == "123"


def test_verify_mini_app_digest_is_canonical_across_order_and_url_encoding():
    canonical_raw = signed_init_data(123456, extra_fields={"start_param": "a b"})
    pairs = [pair.split("=", 1) for pair in canonical_raw.split("&")]
    reordered_raw = "&".join(f"{key}={value.replace('+', '%20')}" for key, value in reversed(pairs))
    decoded_fields = {
        "auth_date": str(NOW_SECONDS),
        "query_id": "test-query",
        "start_param": "a b",
        "user": json.dumps(
            {"id": 123456, "first_name": "Тест"},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    }
    received_hash, check = _sign_fields(decoded_fields)
    expected_digest = hashlib.sha256((check + "\n" + received_hash).encode()).hexdigest()

    first = verify_mini_app(canonical_raw, BOT_TOKEN, NOW_SECONDS)
    second = verify_mini_app(reordered_raw, BOT_TOKEN, NOW_SECONDS)

    assert first[1] == second[1] == expected_digest


@pytest.mark.parametrize(
    "raw,token",
    [
        ("", BOT_TOKEN),
        (signed_init_data(123456), ""),
        (None, BOT_TOKEN),
        (123, BOT_TOKEN),
        ("auth_date", BOT_TOKEN),
        (f"auth_date={NOW_SECONDS}&user=%7B%22id%22%3A123%7D", BOT_TOKEN),
        ("bad=%FF", BOT_TOKEN),
        ("x=" + "a" * 16_385, BOT_TOKEN),
        ("&".join(f"field{index}=x" for index in range(33)), BOT_TOKEN),
    ],
)
def test_verify_mini_app_rejects_bounded_or_invalid_transport_input(raw, token):
    assert_authentication_failure(raw, token)


def test_verify_mini_app_rejects_uppercase_hash_even_when_hexadecimal():
    raw = signed_init_data(123456)
    hash_value = raw.rsplit("hash=", 1)[1]
    assert hash_value != hash_value.upper()

    assert_authentication_failure(raw.rsplit("hash=", 1)[0] + "hash=" + hash_value.upper())


def test_verify_mini_app_handles_pathologically_nested_json_as_generic_failure():
    nested_json = "[" * 2_000 + "0" + "]" * 2_000
    raw = signed_raw_fields({"auth_date": str(NOW_SECONDS), "user": nested_json})

    assert_authentication_failure(raw)


@pytest_asyncio.fixture
async def sqlite_session_factory(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portal-telegram.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_resolve_telegram_customer_creates_active_customer_without_subscription(
    sqlite_session_factory,
):
    identity = TelegramIdentity("123", "client", "First", "Last")

    async with sqlite_session_factory() as session:
        customer = await resolve_telegram_customer(session, identity)
        await session.commit()

    async with sqlite_session_factory() as session:
        saved = await session.scalar(select(VpnCustomer).where(VpnCustomer.id == customer.id))
        subscriptions = (await session.scalars(select(VpnSubscription))).all()

    assert saved is not None
    assert saved.telegram_user_id == "123"
    assert saved.telegram_username == "client"
    assert saved.first_name == "First"
    assert saved.last_name == "Last"
    assert saved.status == "active"
    assert subscriptions == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["active", "archived", "disabled"])
async def test_resolve_telegram_customer_preserves_existing_customer_state_and_relations(
    sqlite_session_factory,
    status,
):
    async with sqlite_session_factory() as session:
        customer = VpnCustomer(
            telegram_user_id="456",
            telegram_username="old",
            first_name="Old",
            last_name="Name",
            status=status,
            notes="operator note",
        )
        session.add(customer)
        await session.flush()
        subscription = VpnSubscription(customer_id=customer.id, status="trial", notes="keep")
        session.add(subscription)
        await session.flush()
        access_key = VpnAccessKey(
            subscription_id=subscription.id,
            external_uuid=f"identity-{status}",
            config_uri="vless://unchanged",
            status="active",
        )
        session.add(access_key)
        await session.commit()
        customer_id = customer.id
        subscription_id = subscription.id
        access_key_id = access_key.id

    async with sqlite_session_factory() as session:
        resolved = await resolve_telegram_customer(
            session,
            TelegramIdentity("000456", "new", "Fresh", None),
        )
        await session.commit()

    async with sqlite_session_factory() as session:
        saved = await session.get(VpnCustomer, customer_id)
        saved_subscription = await session.get(VpnSubscription, subscription_id)
        saved_key = await session.get(VpnAccessKey, access_key_id)

    assert resolved.id == customer_id
    assert saved is not None
    assert saved.status == status
    assert saved.notes == "operator note"
    assert saved.telegram_username == "new"
    assert saved.first_name == "Fresh"
    assert saved.last_name is None
    assert saved_subscription is not None
    assert saved_subscription.status == "trial"
    assert saved_subscription.notes == "keep"
    assert saved_key is not None
    assert saved_key.config_uri == "vless://unchanged"
    assert saved_key.status == "active"


@pytest.mark.asyncio
async def test_resolve_telegram_customer_refreshes_stale_orm_state_before_profile_update(
    sqlite_session_factory,
):
    async with sqlite_session_factory() as seed_session:
        customer = VpnCustomer(
            telegram_user_id="789",
            telegram_username="original",
            status="active",
            notes="original note",
        )
        seed_session.add(customer)
        await seed_session.commit()
        customer_id = customer.id

    async with sqlite_session_factory() as stale_session:
        stale = await stale_session.get(VpnCustomer, customer_id)
        await stale_session.commit()
        assert stale is not None
        assert stale.status == "active"

        async with sqlite_session_factory() as concurrent_session:
            current = await concurrent_session.get(VpnCustomer, customer_id)
            assert current is not None
            current.status = "archived"
            current.notes = "concurrent operator note"
            await concurrent_session.commit()

        resolved = await resolve_telegram_customer(
            stale_session,
            TelegramIdentity("789", "fresh", "Fresh", "Identity"),
        )
        assert resolved is stale
        assert resolved.status == "archived"
        assert resolved.notes == "concurrent operator note"
        await stale_session.commit()

    async with sqlite_session_factory() as session:
        saved = await session.get(VpnCustomer, customer_id)

    assert saved is not None
    assert saved.status == "archived"
    assert saved.notes == "concurrent operator note"
    assert saved.telegram_username == "fresh"
    assert saved.first_name == "Fresh"
    assert saved.last_name == "Identity"


@pytest.mark.asyncio
async def test_postgres_insert_race_uses_savepoint_without_rolling_back_outer_work():
    database_url = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not database_url:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for real PostgreSQL race coverage")

    schema_name = f"portal_telegram_{uuid4().hex}"
    base_engine = create_async_engine(database_url)
    engine = base_engine.execution_options(schema_translate_map={None: schema_name})
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

    async with base_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[VpnCustomer.__table__, VpnTelegramUpdate.__table__],
                )
            )

        both_observed_absence = asyncio.Event()
        both_insert_statements_started = asyncio.Event()
        observation_count = 0
        customer_insert_count = 0

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def track_customer_inserts(
            _connection,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            nonlocal customer_insert_count
            if "INSERT INTO" in statement and "vpn_customers" in statement:
                customer_insert_count += 1
                if customer_insert_count == 2:
                    both_insert_statements_started.set()

        class RaceOrchestratingSession(AsyncSession):
            observed_absence = False
            saw_integrity_error = False

            async def scalar(self, statement, *args, **kwargs):
                nonlocal observation_count
                result = await super().scalar(statement, *args, **kwargs)
                if not self.observed_absence:
                    assert result is None
                    self.observed_absence = True
                    observation_count += 1
                    if observation_count == 2:
                        both_observed_absence.set()
                    await asyncio.wait_for(both_observed_absence.wait(), timeout=15)
                return result

            async def flush(self, objects=None):
                inserting_customer = any(
                    isinstance(instance, VpnCustomer) for instance in self.new
                )
                if not inserting_customer:
                    return await super().flush(objects)

                try:
                    result = await super().flush(objects)
                except IntegrityError:
                    self.saw_integrity_error = True
                    raise
                await asyncio.wait_for(
                    both_insert_statements_started.wait(),
                    timeout=15,
                )
                return result

        racing_factory = async_sessionmaker(
            engine,
            expire_on_commit=False,
            class_=RaceOrchestratingSession,
        )
        async def resolve_contender(label: str) -> tuple[int, bool]:
            async with racing_factory() as session:
                session.add(
                    VpnTelegramUpdate(
                        update_id=f"durable-before-race-{label}",
                        payload={"contender": label},
                    )
                )
                await session.flush()
                customer = await resolve_telegram_customer(
                    session,
                    TelegramIdentity("999", f"contender-{label}", "Race", label),
                )
                await session.commit()
                return customer.id, session.saw_integrity_error

        contender_results = await asyncio.wait_for(
            asyncio.gather(resolve_contender("a"), resolve_contender("b")),
            timeout=30,
        )

        async with session_factory() as session:
            saved_customer = await session.scalar(
                select(VpnCustomer).where(VpnCustomer.telegram_user_id == "999")
            )
            saved_updates = (
                await session.scalars(select(VpnTelegramUpdate).order_by(VpnTelegramUpdate.update_id))
            ).all()

        assert observation_count == 2
        assert customer_insert_count == 2
        assert sorted(saw_error for _customer_id, saw_error in contender_results) == [False, True]
        assert {customer_id for customer_id, _saw_error in contender_results} == {
            saved_customer.id
        }
        assert saved_customer.status == "active"
        assert saved_customer.notes is None
        assert saved_customer.telegram_username in {"contender-a", "contender-b"}
        assert saved_customer.first_name == "Race"
        assert saved_customer.last_name in {"a", "b"}
        assert [update.update_id for update in saved_updates] == [
            "durable-before-race-a",
            "durable-before-race-b",
        ]
        assert [update.payload for update in saved_updates] == [
            {"contender": "a"},
            {"contender": "b"},
        ]
    finally:
        await engine.dispose()
        async with base_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await base_engine.dispose()
