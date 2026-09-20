from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qsl, urlencode
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from starlette.responses import Response

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    VpnCustomer,
    VpnCustomerSession,
    VpnPortalLoginAttempt,
    VpnPortalMiniAppExchange,
)
from app.services.vpn_portal_auth import (
    BINDING_COOKIE,
    SESSION_COOKIE,
    PortalAuthenticationError,
    as_utc,
    cleanup_expired_portal_auth,
    consume_login_attempt,
    create_login_attempt,
    csrf_token,
    delete_binding_cookie,
    delete_session_cookie,
    digest_token,
    exchange_mini_app_session,
    identity_allowed,
    issue_session,
    lookup_session,
    public_origin,
    revoke_session,
    set_binding_cookie,
    set_session_cookie,
    valid_mutation,
)


NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
BOT_TOKEN = "12345:test-only"


def portal_settings(**overrides) -> Settings:
    values = {
        "VPN_PORTAL_ENABLED": True,
        "VPN_PORTAL_PUBLIC_ORIGIN": "https://Portal.Example.Test:443/",
        "VPN_PORTAL_ALLOWED_TELEGRAM_IDS": "123, 456",
        "VPN_TELEGRAM_BOT_TOKEN": BOT_TOKEN,
    }
    values.update(overrides)
    return Settings(**values)


def signed_init_data(user_id: str, timestamp: int, *, query_id: str = "test") -> str:
    fields = {
        "auth_date": str(timestamp),
        "query_id": query_id,
        "user": json.dumps({"id": user_id, "first_name": "Test"}, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    path = (tmp_path / "portal-auth.db").as_posix()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def postgres_session_factory():
    database_url = os.getenv("VPN_PORTAL_TEST_PG_URL")
    if not database_url:
        pytest.skip("VPN_PORTAL_TEST_PG_URL is required for real PostgreSQL coverage")
    schema_name = f"portal_auth_{uuid4().hex}"
    base_engine = create_async_engine(database_url)
    engine = base_engine.execution_options(schema_translate_map={None: schema_name})
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with base_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema_name}"'))
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: Base.metadata.create_all(
                    sync_connection,
                    tables=[
                        VpnCustomer.__table__,
                        VpnCustomerSession.__table__,
                        VpnPortalLoginAttempt.__table__,
                        VpnPortalMiniAppExchange.__table__,
                    ],
                )
            )
        yield factory
    finally:
        await engine.dispose()
        async with base_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE'))
        await base_engine.dispose()


def test_digest_csrf_and_utc_normalization_are_stable():
    assert digest_token("токен") == hashlib.sha256("токен".encode()).hexdigest()
    expected = hmac.new(b"raw-session", b"veltrix-portal-csrf-v1", hashlib.sha256).hexdigest()
    assert csrf_token("raw-session") == expected
    assert as_utc(datetime(2026, 1, 2, 3, 4, 5)) == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert as_utc(datetime(2026, 1, 2, 6, 4, 5, tzinfo=UTC)) == datetime(
        2026, 1, 2, 6, 4, 5, tzinfo=UTC
    )


@pytest.mark.parametrize(
    ("configured", "allow_http", "expected"),
    [
        ("https://Portal.Example.Test:443/", False, "https://portal.example.test"),
        ("https://Portal.Example.Test:8443", False, "https://portal.example.test:8443"),
        ("http://localhost:80/", True, "http://localhost"),
        ("http://127.0.0.1:3000", True, "http://127.0.0.1:3000"),
    ],
)
def test_public_origin_canonicalizes_safe_origins(configured, allow_http, expected):
    settings = portal_settings(
        VPN_PORTAL_PUBLIC_ORIGIN=configured,
        VPN_PORTAL_ALLOW_LOCAL_HTTP=allow_http,
    )
    assert public_origin(settings) == expected


@pytest.mark.parametrize(
    "configured",
    [
        "",
        " https://portal.example.test",
        "https://portal.example.test ",
        "https://portal.ex ample.test",
        "https://portal.example\u00a0.test",
        "https://pörtal.example.test",
        "https://portal.example.test\\evil",
        "https://user:pass@portal.example.test",
        "https://portal.example.test/path",
        "https://portal.example.test?",
        "https://portal.example.test#",
        "https://portal.example.test?x=1",
        "https://portal.example.test#x",
        "https://portal.example.test:0",
        "https://portal.example.test:65536",
        "https://portal.example.test:not-a-port",
        "https://portal.example.test:",
        "http://portal.example.test",
        "http://localhost",
    ],
)
def test_public_origin_rejects_unsafe_or_invalid_configuration(configured):
    with pytest.raises(ValueError, match="^portal_configuration_invalid$"):
        public_origin(portal_settings(VPN_PORTAL_PUBLIC_ORIGIN=configured))


def test_identity_allowlist_is_feature_gated_and_canonical():
    assert identity_allowed(portal_settings(), "000123") is True
    assert identity_allowed(portal_settings(), "999") is False
    assert identity_allowed(portal_settings(VPN_PORTAL_ALLOWED_TELEGRAM_IDS=""), "123") is False
    assert identity_allowed(portal_settings(VPN_PORTAL_ENABLED=False), "123") is False
    assert identity_allowed(portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True), "999") is True
    assert identity_allowed(portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True), "invalid") is False


def test_cookie_helpers_use_separate_secure_host_only_namespaces():
    response = Response()
    settings = portal_settings()
    set_session_cookie(response, "session", settings)
    set_binding_cookie(response, "binding", settings)
    delete_session_cookie(response, settings)
    delete_binding_cookie(response, settings)
    headers = response.headers.getlist("set-cookie")

    assert headers[0].startswith(f"{SESSION_COOKIE}=session;")
    assert "Max-Age=604800" in headers[0]
    assert headers[1].startswith(f"{BINDING_COOKIE}=binding;")
    assert "Max-Age=600" in headers[1]
    assert all("HttpOnly" in header and "Secure" in header for header in headers)
    assert all("SameSite=lax" in header and "Path=/" in header for header in headers)
    assert all("Domain=" not in header and "frdm_session" not in header for header in headers)
    assert f"{SESSION_COOKIE}=" in headers[2] and "Max-Age=0" in headers[2]
    assert f"{BINDING_COOKIE}=" in headers[3] and "Max-Age=0" in headers[3]


def test_http_local_cookie_is_not_secure():
    response = Response()
    set_session_cookie(
        response,
        "session",
        portal_settings(
            VPN_PORTAL_PUBLIC_ORIGIN="http://localhost:8000",
            VPN_PORTAL_ALLOW_LOCAL_HTTP=True,
        ),
    )
    assert "Secure" not in response.headers["set-cookie"]


@pytest.mark.asyncio
async def test_issue_lookup_revoke_and_csrf_enforce_customer_binding(session_factory):
    settings = portal_settings()
    async with session_factory() as db:
        customer = VpnCustomer(telegram_user_id="123", status="active")
        db.add(customer)
        await db.commit()
        raw = await issue_session(db, customer.id, "000123", now=NOW)
        await db.commit()

        principal = await lookup_session(db, raw, settings, now=NOW + timedelta(days=6))
        assert principal is not None
        assert principal.customer.id == customer.id
        assert as_utc(principal.session.expires_at) == NOW + timedelta(days=7)
        assert principal.csrf == csrf_token(raw)
        assert valid_mutation(public_origin(settings), principal.csrf, principal, settings)
        assert not valid_mutation("https://evil.test", principal.csrf, principal, settings)
        assert not valid_mutation("https://é.test", principal.csrf, principal, settings)
        assert not valid_mutation(public_origin(settings), "0" * 64, principal, settings)
        assert not valid_mutation(public_origin(settings), "not-ascii-é", principal, settings)
        assert await lookup_session(db, "admin-token", settings, now=NOW) is None

        await revoke_session(db, principal, now=NOW + timedelta(hours=1))
        await db.commit()
        assert await lookup_session(db, raw, settings, now=NOW + timedelta(hours=2)) is None


@pytest.mark.asyncio
async def test_lookup_rechecks_feature_allowlist_expiry_archive_and_rebinding(session_factory):
    settings = portal_settings()
    async with session_factory() as db:
        customer = VpnCustomer(telegram_user_id="123", status="active")
        db.add(customer)
        await db.commit()
        raw = await issue_session(db, customer.id, "123", now=NOW)
        await db.commit()
        assert await lookup_session(db, raw, settings, now=NOW) is not None
        assert await lookup_session(db, raw, portal_settings(VPN_PORTAL_ENABLED=False), now=NOW) is None
        assert await lookup_session(db, raw, portal_settings(VPN_PORTAL_ALLOWED_TELEGRAM_IDS="456"), now=NOW) is None
        assert await lookup_session(db, raw, settings, now=NOW + timedelta(days=7)) is None

        customer.status = "archived"
        await db.commit()
        assert await lookup_session(db, raw, settings, now=NOW) is None
        customer.status = "active"
        customer.telegram_user_id = "456"
        await db.commit()
        assert await lookup_session(db, raw, settings, now=NOW) is None


@pytest.mark.asyncio
async def test_issue_reloads_stale_customer_and_rejects_archive_or_rebinding(session_factory):
    async with session_factory() as first:
        customer = VpnCustomer(telegram_user_id="123", status="active")
        first.add(customer)
        await first.commit()
        customer_id = customer.id
        async with session_factory() as second:
            stored = await second.get(VpnCustomer, customer_id)
            stored.status = "archived"
            await second.commit()
        with pytest.raises(ValueError, match="^customer_unavailable$"):
            await issue_session(first, customer_id, "123", now=NOW)
        assert await first.scalar(select(func.count()).select_from(VpnCustomerSession)) == 0


@pytest.mark.asyncio
async def test_login_attempt_claim_is_durable_and_erases_verifier(session_factory):
    async with session_factory() as db:
        state, binding, verifier = await create_login_attempt(db, now=NOW)
        await db.commit()
        assert state != binding != verifier
        assert await consume_login_attempt(db, state, "wrong", NOW) is None
        claimed = await consume_login_attempt(db, state, binding, NOW + timedelta(minutes=9))
        assert claimed == verifier

    async with session_factory() as db:
        stored = await db.get(VpnPortalLoginAttempt, digest_token(state))
        assert stored is not None and stored.code_verifier is None
        assert as_utc(stored.consumed_at) == NOW + timedelta(minutes=9)
        assert await consume_login_attempt(db, state, binding, NOW + timedelta(minutes=9)) is None


@pytest.mark.asyncio
async def test_login_attempt_rejects_expired_and_malformed_without_consuming(session_factory):
    async with session_factory() as db:
        state, binding, _verifier = await create_login_attempt(db, now=NOW)
        await db.commit()
        assert await consume_login_attempt(db, "x" * 1000, binding, NOW) is None
        assert await consume_login_attempt(db, state, binding, NOW + timedelta(minutes=10)) is None
        stored = await db.get(VpnPortalLoginAttempt, digest_token(state))
        assert stored is not None and stored.consumed_at is None and stored.code_verifier is not None


@pytest.mark.asyncio
async def test_mini_app_fresh_exchange_claims_payload_and_issues_session_atomically(session_factory):
    settings = portal_settings()
    raw_data = signed_init_data("123", int(NOW.timestamp()) - 299)
    async with session_factory() as db:
        result = await exchange_mini_app_session(db, raw_data, settings, now=NOW)
        assert result.raw_session is not None
        assert result.principal.customer.telegram_user_id == "123"
        await db.commit()
        exchange = await db.scalar(select(VpnPortalMiniAppExchange))
        assert exchange is not None
        assert as_utc(exchange.created_at) == NOW
        assert as_utc(exchange.expires_at) == NOW + timedelta(minutes=10)


@pytest.mark.asyncio
async def test_mini_app_replay_requires_same_customer_valid_session(session_factory):
    settings = portal_settings()
    raw_data = signed_init_data("123", int(NOW.timestamp()), query_id="replay")
    async with session_factory() as db:
        first = await exchange_mini_app_session(db, raw_data, settings, now=NOW)
        await db.commit()
        replay = await exchange_mini_app_session(
            db,
            raw_data,
            settings,
            raw_session=first.raw_session,
            now=NOW + timedelta(minutes=1),
        )
        assert replay.raw_session is None
        assert replay.principal.customer.id == first.principal.customer.id
        with pytest.raises(PortalAuthenticationError) as error:
            await exchange_mini_app_session(db, raw_data, settings, now=NOW + timedelta(minutes=1))
        assert error.value.account_conflict is False


@pytest.mark.asyncio
async def test_mini_app_reordered_signed_payload_is_the_same_replay(session_factory):
    settings = portal_settings()
    raw_data = signed_init_data("123", int(NOW.timestamp()), query_id="canonical")
    reordered = urlencode(list(reversed(parse_qsl(raw_data, keep_blank_values=True))))
    async with session_factory() as db:
        first = await exchange_mini_app_session(db, raw_data, settings, now=NOW)
        await db.commit()
        replay = await exchange_mini_app_session(
            db,
            reordered,
            settings,
            raw_session=first.raw_session,
            now=NOW + timedelta(seconds=1),
        )
        assert replay.raw_session is None
        assert replay.principal.customer.id == first.principal.customer.id


@pytest.mark.asyncio
async def test_mini_app_refuses_account_switch_and_invalid_payload_never_falls_back(session_factory):
    settings = portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True)
    async with session_factory() as db:
        existing = VpnCustomer(telegram_user_id="456", status="active")
        db.add(existing)
        await db.commit()
        cookie = await issue_session(db, existing.id, "456", now=NOW)
        await db.commit()
        with pytest.raises(PortalAuthenticationError) as conflict:
            await exchange_mini_app_session(
                db,
                signed_init_data("123", int(NOW.timestamp())),
                settings,
                raw_session=cookie,
                now=NOW,
            )
        assert conflict.value.account_conflict is True
        assert await db.scalar(select(func.count()).select_from(VpnPortalMiniAppExchange)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnCustomer).where(VpnCustomer.telegram_user_id == "123")) == 0

        with pytest.raises(PortalAuthenticationError) as invalid:
            await exchange_mini_app_session(db, "invalid", settings, raw_session=cookie, now=NOW)
        assert invalid.value.account_conflict is False


@pytest.mark.asyncio
async def test_mini_app_replay_with_different_customer_cookie_is_auth_failure(session_factory):
    settings = portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True)
    claimed_payload = signed_init_data("123", int(NOW.timestamp()), query_id="claimed")
    async with session_factory() as db:
        first = await exchange_mini_app_session(db, claimed_payload, settings, now=NOW)
        other = VpnCustomer(telegram_user_id="456", status="active")
        db.add(other)
        await db.flush()
        other_cookie = await issue_session(db, other.id, "456", now=NOW)
        await db.commit()

        with pytest.raises(PortalAuthenticationError) as error:
            await exchange_mini_app_session(
                db,
                claimed_payload,
                settings,
                raw_session=other_cookie,
                now=NOW + timedelta(minutes=1),
            )
        assert first.raw_session is not None
        assert error.value.account_conflict is False


@pytest.mark.asyncio
async def test_mini_app_replay_rejects_same_customer_after_telegram_rebind(session_factory):
    settings = portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True)
    claimed_payload = signed_init_data("123", int(NOW.timestamp()), query_id="rebound")
    async with session_factory() as db:
        first = await exchange_mini_app_session(db, claimed_payload, settings, now=NOW)
        customer = first.principal.customer
        customer.telegram_user_id = "456"
        await db.flush()
        rebound_cookie = await issue_session(db, customer.id, "456", now=NOW)
        await db.commit()

        with pytest.raises(PortalAuthenticationError) as error:
            await exchange_mini_app_session(
                db,
                claimed_payload,
                settings,
                raw_session=rebound_cookie,
                now=NOW + timedelta(minutes=1),
            )
        assert error.value.account_conflict is False


@pytest.mark.asyncio
async def test_mini_app_archived_customer_failure_leaves_no_profile_or_auth_changes(session_factory):
    settings = portal_settings()
    raw_data = signed_init_data("123", int(NOW.timestamp()), query_id="archived")
    async with session_factory() as db:
        customer = VpnCustomer(
            telegram_user_id="123",
            first_name="Original",
            status="archived",
        )
        db.add(customer)
        await db.commit()
        customer_id = customer.id

        with pytest.raises(PortalAuthenticationError):
            await exchange_mini_app_session(db, raw_data, settings, now=NOW)
        await db.commit()

    async with session_factory() as db:
        stored = await db.get(VpnCustomer, customer_id)
        assert stored is not None and stored.first_name == "Original"
        assert await db.scalar(select(func.count()).select_from(VpnPortalMiniAppExchange)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnCustomerSession)) == 0


@pytest.mark.asyncio
async def test_cleanup_deletes_only_oldest_hundred_expired_rows_per_table(session_factory):
    async with session_factory() as db:
        customer = VpnCustomer(telegram_user_id="123", status="active")
        db.add(customer)
        await db.flush()
        for index in range(105):
            when = NOW - timedelta(seconds=105 - index)
            digest = f"{index:064x}"
            db.add(VpnPortalLoginAttempt(state_hash=digest, binding_hash=digest, code_verifier="v", created_at=when, expires_at=when))
            db.add(VpnPortalMiniAppExchange(digest=digest, customer_id=customer.id, created_at=when, expires_at=when))
            db.add(VpnCustomerSession(token_hash=digest, customer_id=customer.id, telegram_user_id="123", created_at=when, expires_at=when))
        future = NOW + timedelta(seconds=1)
        db.add(VpnPortalMiniAppExchange(digest="f" * 64, customer_id=customer.id, created_at=NOW, expires_at=future))
        await db.commit()

        assert await cleanup_expired_portal_auth(db, now=NOW) == {
            "login_attempts": 100,
            "mini_app_exchanges": 100,
            "customer_sessions": 100,
        }
        await db.commit()
        remaining = (await db.scalars(select(VpnPortalMiniAppExchange).order_by(VpnPortalMiniAppExchange.expires_at))).all()
        assert len(remaining) == 6
        assert as_utc(remaining[-1].expires_at) == future


@pytest.mark.asyncio
async def test_cleanup_uses_inclusive_expiry_but_preserves_unexpired_revoked_rows(session_factory):
    async with session_factory() as db:
        customer = VpnCustomer(telegram_user_id="123", status="active")
        db.add(customer)
        await db.flush()
        db.add_all(
            [
                VpnCustomerSession(
                    token_hash="a" * 64,
                    customer_id=customer.id,
                    telegram_user_id="123",
                    created_at=NOW,
                    expires_at=NOW,
                ),
                VpnCustomerSession(
                    token_hash="b" * 64,
                    customer_id=customer.id,
                    telegram_user_id="123",
                    created_at=NOW,
                    expires_at=NOW + timedelta(microseconds=1),
                    revoked_at=NOW,
                ),
            ]
        )
        await db.commit()

        result = await cleanup_expired_portal_auth(db, now=NOW)
        await db.commit()

        assert result["customer_sessions"] == 1
        remaining = (await db.scalars(select(VpnCustomerSession))).all()
        assert [session.token_hash for session in remaining] == ["b" * 64]


@pytest.mark.asyncio
async def test_cleanup_cannot_make_a_still_signed_exchange_payload_reusable(session_factory):
    settings = portal_settings()
    future_skew_payload = signed_init_data(
        "123",
        int(NOW.timestamp()) + 30,
        query_id="future-skew-boundary",
    )
    async with session_factory() as db:
        first = await exchange_mini_app_session(db, future_skew_payload, settings, now=NOW)
        await db.commit()
        assert first.raw_session is not None

        cleanup_time = NOW + timedelta(minutes=10)
        result = await cleanup_expired_portal_auth(db, now=cleanup_time)
        await db.commit()
        assert result["mini_app_exchanges"] == 1

        with pytest.raises(PortalAuthenticationError):
            await exchange_mini_app_session(
                db,
                future_skew_payload,
                settings,
                raw_session=first.raw_session,
                now=cleanup_time,
            )
        assert await db.scalar(select(func.count()).select_from(VpnPortalMiniAppExchange)) == 0


@pytest.mark.asyncio
async def test_postgres_login_attempt_has_one_durable_concurrent_winner(
    postgres_session_factory,
):
    async with postgres_session_factory() as db:
        state, binding, verifier = await create_login_attempt(db, now=NOW)
        await db.commit()

    async with (
        postgres_session_factory() as blocker,
        postgres_session_factory() as first,
        postgres_session_factory() as second,
        postgres_session_factory() as monitor,
    ):
        blocker_pid = await blocker.scalar(text("SELECT pg_backend_pid()"))
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        await blocker.scalar(
            select(VpnPortalLoginAttempt)
            .where(VpnPortalLoginAttempt.state_hash == digest_token(state))
            .with_for_update()
        )
        tasks = [
            asyncio.create_task(
                consume_login_attempt(first, state, binding, NOW + timedelta(minutes=1))
            ),
            asyncio.create_task(
                consume_login_attempt(second, state, binding, NOW + timedelta(minutes=1))
            ),
        ]
        try:
            for _attempt in range(100):
                first_blockers, second_blockers = (
                    await monitor.execute(
                        text(
                            "SELECT pg_blocking_pids(:first_pid), "
                            "pg_blocking_pids(:second_pid)"
                        ),
                        {"first_pid": first_pid, "second_pid": second_pid},
                    )
                ).one()
                if first_blockers and second_blockers:
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("both claimants did not contend on the durable attempt row")
            assert blocker_pid in {*first_blockers, *second_blockers}
            assert not tasks[0].done() and not tasks[1].done()
            await blocker.commit()
            results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    assert sorted(result is not None for result in results) == [False, True]
    assert verifier in results
    async with postgres_session_factory() as db:
        stored = await db.get(VpnPortalLoginAttempt, digest_token(state))
        assert stored is not None and stored.code_verifier is None
        assert stored.consumed_at is not None


@pytest.mark.asyncio
async def test_postgres_duplicate_mini_app_exchange_is_atomic_with_session(
    postgres_session_factory,
):
    settings = portal_settings()
    raw_data = signed_init_data("123", int(NOW.timestamp()), query_id="postgres-race")
    async with postgres_session_factory() as db:
        db.add(VpnCustomer(telegram_user_id="123", status="active"))
        await db.commit()

    both_observed_absence = asyncio.Event()
    observation_count = 0

    class RaceOrchestratingSession(AsyncSession):
        observed_initial_absence = False
        saw_exchange_integrity_error = False

        async def get(self, entity, ident, **kwargs):
            nonlocal observation_count
            result = await super().get(entity, ident, **kwargs)
            if entity is VpnPortalMiniAppExchange and not self.observed_initial_absence:
                assert result is None
                self.observed_initial_absence = True
                observation_count += 1
                if observation_count == 2:
                    both_observed_absence.set()
                await asyncio.wait_for(both_observed_absence.wait(), timeout=15)
            return result

        async def flush(self, objects=None):
            inserting_exchange = any(
                isinstance(instance, VpnPortalMiniAppExchange) for instance in self.new
            )
            try:
                return await super().flush(objects)
            except IntegrityError:
                if inserting_exchange:
                    self.saw_exchange_integrity_error = True
                raise

    racing_factory = async_sessionmaker(
        postgres_session_factory.kw["bind"],
        expire_on_commit=False,
        class_=RaceOrchestratingSession,
    )
    start = asyncio.Event()

    async def contender():
        async with racing_factory() as db:
            await start.wait()
            try:
                result = await exchange_mini_app_session(db, raw_data, settings, now=NOW)
            except PortalAuthenticationError:
                await db.rollback()
                return False, db.saw_exchange_integrity_error
            await db.commit()
            return result.raw_session is not None, db.saw_exchange_integrity_error

    tasks = [asyncio.create_task(contender()), asyncio.create_task(contender())]
    start.set()
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30)

    assert observation_count == 2
    assert sorted(success for success, _saw_error in results) == [False, True]
    assert sorted(saw_error for _success, saw_error in results) == [False, True]
    async with postgres_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(VpnPortalMiniAppExchange)) == 1
        assert await db.scalar(select(func.count()).select_from(VpnCustomerSession)) == 1


@pytest.mark.asyncio
async def test_postgres_issue_session_rejects_stale_archive_and_rebinding(
    postgres_session_factory,
):
    async with postgres_session_factory() as setup:
        customer = VpnCustomer(telegram_user_id="456", status="active")
        setup.add(customer)
        await setup.commit()
        customer_id = customer.id

    async with postgres_session_factory() as stale:
        assert await stale.get(VpnCustomer, customer_id) is not None
        async with postgres_session_factory() as concurrent:
            customer = await concurrent.get(VpnCustomer, customer_id)
            customer.status = "archived"
            await concurrent.commit()
        with pytest.raises(ValueError, match="^customer_unavailable$"):
            await issue_session(stale, customer_id, "456", now=NOW)
        await stale.rollback()

    async with postgres_session_factory() as restore:
        customer = await restore.get(VpnCustomer, customer_id)
        customer.status = "active"
        await restore.commit()
    async with postgres_session_factory() as stale:
        assert await stale.get(VpnCustomer, customer_id) is not None
        async with postgres_session_factory() as concurrent:
            customer = await concurrent.get(VpnCustomer, customer_id)
            customer.telegram_user_id = "789"
            await concurrent.commit()
        with pytest.raises(ValueError, match="^customer_unavailable$"):
            await issue_session(stale, customer_id, "456", now=NOW)


@pytest.mark.asyncio
async def test_postgres_mini_app_rejects_committed_rebind_after_session_lookup(
    postgres_session_factory,
):
    settings = portal_settings(VPN_PORTAL_PUBLIC_ACCESS=True)
    raw_data = signed_init_data("123", int(NOW.timestamp()), query_id="stale-principal")
    async with postgres_session_factory() as setup:
        customer = VpnCustomer(
            telegram_user_id="123",
            first_name="Original",
            status="active",
        )
        setup.add(customer)
        await setup.commit()
        customer_id = customer.id
        raw_session = await issue_session(setup, customer_id, "123", now=NOW)
        await setup.commit()

    lookup_finished = asyncio.Event()
    rebind_committed = asyncio.Event()

    class PauseAfterLookupSession(AsyncSession):
        paused = False

        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if not self.paused:
                rendered = str(statement)
                assert "vpn_customer_sessions" in rendered
                assert "JOIN vpn_customers" in rendered
                self.paused = True
                lookup_finished.set()
                await asyncio.wait_for(rebind_committed.wait(), timeout=15)
            return result

    exchange_factory = async_sessionmaker(
        postgres_session_factory.kw["bind"],
        expire_on_commit=False,
        class_=PauseAfterLookupSession,
    )

    async def commit_rebind():
        await asyncio.wait_for(lookup_finished.wait(), timeout=15)
        async with postgres_session_factory() as concurrent:
            stored = await concurrent.get(VpnCustomer, customer_id)
            stored.telegram_user_id = "456"
            await concurrent.commit()
        rebind_committed.set()

    async with exchange_factory() as db:
        rebind_task = asyncio.create_task(commit_rebind())
        try:
            with pytest.raises(PortalAuthenticationError) as error:
                await exchange_mini_app_session(
                    db,
                    raw_data,
                    settings,
                    raw_session=raw_session,
                    now=NOW,
                )
            assert error.value.account_conflict is False
            await rebind_task
            await db.commit()
        finally:
            if not rebind_task.done():
                rebind_task.cancel()
            await asyncio.gather(rebind_task, return_exceptions=True)

    async with postgres_session_factory() as db:
        customers = (await db.scalars(select(VpnCustomer).order_by(VpnCustomer.id))).all()
        assert [(customer.id, customer.telegram_user_id, customer.first_name) for customer in customers] == [
            (customer_id, "456", "Original")
        ]
        assert await db.scalar(select(func.count()).select_from(VpnPortalMiniAppExchange)) == 0
        assert await db.scalar(select(func.count()).select_from(VpnCustomerSession)) == 1
