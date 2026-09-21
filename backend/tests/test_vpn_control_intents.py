from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from importlib import import_module
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.services.vpn_node_request import (
    node_request_digest,
    parse_node_request,
    serialize_node_request,
)


CLIENT_UUID = "11111111-2222-4333-8444-555555555555"
OPERATION_UUID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
ISSUED_AT = datetime(2026, 9, 20, 10, 30, 15, 123000, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 10, 21, 12, 0, 0, 456000, tzinfo=UTC)
PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"


def _api():
    return import_module("app.services.vpn_control_intents")


def _key(**overrides) -> VpnAccessKey:
    values = {
        "id": 7,
        "subscription_id": 3,
        "worker_id": 2,
        "endpoint_id": 5,
        "operation_generation": 4,
        "verified_client_email": "veltrix-7-phone",
        "panel_sub_id": "stable_sub-id",
        "protocol": "vless",
        "external_uuid": CLIENT_UUID,
        "config_uri": None,
        "status": "pending_sync",
        "issued_at": ISSUED_AT,
        "created_at": ISSUED_AT - timedelta(days=1),
    }
    values.update(overrides)
    return VpnAccessKey(**values)


def _subscription(**overrides) -> VpnSubscription:
    values = {
        "id": 3,
        "customer_id": 1,
        "status": "active",
        "starts_at": NOW - timedelta(days=1),
        "expires_at": EXPIRES_AT,
        "traffic_limit_gb": 25,
        "max_devices": 3,
    }
    values.update(overrides)
    return VpnSubscription(**values)


def _endpoint(**overrides) -> VpnEndpoint:
    values = {
        "id": 5,
        "worker_id": 2,
        "inbound_id": 11,
        "public_host": "vpn.example.test",
        "port": 443,
        "protocol": "vless",
        "transport": "raw",
        "security": "reality",
        "server_name": "cdn.example.test",
        "public_key": PUBLIC_KEY,
        "short_id": "0123456789abcdef",
        "fingerprint": "chrome",
        "flow": "xtls-rprx-vision",
        "status": "ready",
        "verified_at": NOW - timedelta(hours=1),
    }
    values.update(overrides)
    return VpnEndpoint(**values)


def _request(**overrides):
    values = {
        "access_key": _key(),
        "subscription": _subscription(),
        "endpoint": _endpoint(),
        "operation_id": OPERATION_UUID,
        "generation": 5,
        "action": "provision",
        "allow_create": True,
        "allow_shared_restart": False,
    }
    values.update(overrides)
    return _api().build_control_request(**values)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'control-intents.sqlite3'}")

    @event.listens_for(engine.sync_engine, "connect")
    def _foreign_keys(connection, _record) -> None:
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session:
        session.add_all(
            [
                VpnCustomer(id=1, status="active"),
                VpnCustomer(id=2, status="active"),
                WorkerNode(
                    id=2,
                    name="intent-worker",
                    status="ready",
                    is_enabled=True,
                    vpn_enabled=True,
                    vpn_role="vpn_node",
                    vpn_runtime_status="ready",
                    ssh_host="192.0.2.2",
                    ssh_password="ssh-super-secret",
                    vpn_panel_password="panel-super-secret",
                ),
            ]
        )
        await session.flush()
        session.add_all(
            [
                VpnSubscription(
                    id=3,
                    customer_id=1,
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    expires_at=EXPIRES_AT,
                    traffic_limit_gb=25,
                    max_devices=5,
                ),
                VpnSubscription(
                    id=4,
                    customer_id=1,
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    expires_at=EXPIRES_AT,
                    traffic_limit_gb=5,
                    max_devices=5,
                ),
                VpnSubscription(
                    id=5,
                    customer_id=2,
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    expires_at=EXPIRES_AT,
                    traffic_limit_gb=5,
                    max_devices=5,
                ),
            ]
        )
        session.add(
            VpnEndpoint(
                id=5,
                worker_id=2,
                inbound_id=11,
                public_host="vpn.example.test",
                port=443,
                protocol="vless",
                transport="raw",
                security="reality",
                server_name="cdn.example.test",
                public_key=PUBLIC_KEY,
                short_id="0123456789abcdef",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                status="ready",
                verified_at=NOW - timedelta(hours=1),
            )
        )
        await session.flush()
        session.add_all(
            [
                VpnAccessKey(
                    id=7,
                    subscription_id=3,
                    worker_id=2,
                    endpoint_id=5,
                    verified_client_email="veltrix-7-phone",
                    panel_sub_id="stable_sub-id",
                    protocol="vless",
                    external_uuid=CLIENT_UUID,
                    status="pending_sync",
                    issued_at=ISSUED_AT,
                ),
                VpnAccessKey(
                    id=8,
                    subscription_id=4,
                    worker_id=2,
                    endpoint_id=5,
                    verified_client_email="veltrix-8-tablet",
                    panel_sub_id="other-sub-id",
                    protocol="vless",
                    external_uuid="22222222-3333-4444-8555-666666666666",
                    config_uri="vless://other-private-uri",
                    status="active",
                    issued_at=ISSUED_AT,
                ),
            ]
        )
        await session.commit()
    try:
        yield factory
    finally:
        await engine.dispose()


class RecordingSession:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.statements = []
        self.flushes = 0

    @property
    def no_autoflush(self):
        return self.session.no_autoflush

    @property
    def sync_session(self):
        return self.session.sync_session

    async def scalar(self, statement):
        self.statements.append(statement)
        return await self.session.scalar(statement)

    async def scalars(self, statement):
        self.statements.append(statement)
        return await self.session.scalars(statement)

    async def execute(self, statement):
        self.statements.append(statement)
        return await self.session.execute(statement)

    def add(self, instance) -> None:
        self.session.add(instance)

    async def flush(self) -> None:
        self.flushes += 1
        await self.session.flush()

    def begin_nested(self):
        return self.session.begin_nested()

    def get_bind(self):
        return self.session.get_bind()

    async def commit(self) -> None:
        raise AssertionError("staging must not commit")


def _operation(
    *,
    operation_id: str,
    access_key_id: int = 7,
    generation: int,
    state: str,
) -> VpnControlOperation:
    return VpnControlOperation(
        id=operation_id,
        access_key_id=access_key_id,
        worker_id=2,
        endpoint_id=5,
        generation=generation,
        action="provision",
        request_snapshot={"detached": generation},
        request_digest=f"{generation:064x}",
        state=state,
        claim_token=(f"00000000-0000-4000-8000-{generation:012d}" if state in {"claimed", "uncertain"} else None),
        claimed_at=(NOW - timedelta(minutes=1) if state in {"claimed", "uncertain"} else None),
    )


def _statement_tables(statement) -> frozenset[str]:
    def names(source) -> set[str]:
        if hasattr(source, "name"):
            return {source.name}
        return names(source.left) | names(source.right)

    return frozenset(
        name for source in statement.get_final_froms() for name in names(source)
    )


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_build_control_request_uses_only_persisted_identity_policy_and_endpoint_snapshot():
    request = _request()

    assert serialize_node_request(request) == {
        "version": 1,
        "operation_id": str(OPERATION_UUID),
        "access_key_id": 7,
        "generation": 5,
        "action": "provision",
        "target": {
            "endpoint_id": 5,
            "worker_id": 2,
            "inbound_id": 11,
            "public_host": "vpn.example.test",
            "port": 443,
            "protocol": "vless",
            "transport": "raw",
            "security": "reality",
            "server_name": "cdn.example.test",
            "public_key": PUBLIC_KEY,
            "short_id": "0123456789abcdef",
            "fingerprint": "chrome",
            "flow": "xtls-rprx-vision",
        },
        "client_uuid": CLIENT_UUID,
        "client_email": "veltrix-7-phone",
        "sub_id": "stable_sub-id",
        "expires_at_ms": 1_792_584_000_456,
        "traffic_limit_bytes": 25 * 1024**3,
        "created_at_ms": 1_789_900_215_123,
        "allow_create": True,
        "allow_shared_restart": False,
    }
    assert node_request_digest(request) == node_request_digest(_request())


def test_build_control_request_normalizes_naive_times_as_utc_and_unlimited_values_to_zero():
    request = _request(
        access_key=_key(issued_at=None, created_at=ISSUED_AT.replace(tzinfo=None)),
        subscription=_subscription(expires_at=None, traffic_limit_gb=None),
        allow_create=False,
    )

    assert request.created_at_ms == 1_789_900_215_123
    assert request.expires_at_ms == 0
    assert request.traffic_limit_bytes == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("expires_at", datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))),
        ("expires_at", datetime.max.replace(tzinfo=timezone(-timedelta(hours=14)))),
        ("issued_at", datetime.min.replace(tzinfo=timezone(timedelta(hours=14)))),
        ("issued_at", datetime.max.replace(tzinfo=timezone(-timedelta(hours=14)))),
    ],
)
def test_build_control_request_rejects_utc_normalization_overflow_with_static_error(
    field,
    value,
):
    api = _api()
    changes = (
        {"subscription": _subscription(expires_at=value)}
        if field == "expires_at"
        else {"access_key": _key(issued_at=value)}
    )

    with pytest.raises(api.VpnControlIntentError) as caught:
        _request(**changes)

    assert caught.value.code == "vpn_control_time_invalid"
    assert str(caught.value) == "vpn_control_time_invalid"
    assert caught.value.__context__ is None


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"access_key": _key(external_uuid="11111111222243338444555555555555")}, "vpn_control_client_identity_invalid"),
        ({"access_key": _key(verified_client_email=None)}, "vpn_control_client_identity_unverified"),
        ({"access_key": _key(panel_sub_id=None)}, "vpn_control_client_identity_unverified"),
        ({"access_key": _key(panel_sub_id="")}, "vpn_control_create_identity_invalid"),
        ({"access_key": _key(revoke_requested_at=NOW)}, "vpn_control_revoke_sticky"),
        ({"endpoint": _endpoint(security="tls")}, "vpn_control_endpoint_unsupported"),
        ({"endpoint": _endpoint(security="none", server_name=None, public_key=None, short_id=None, fingerprint=None, flow=None)}, "vpn_control_endpoint_unsupported"),
        ({"subscription": _subscription(traffic_limit_gb=2**40)}, "vpn_control_policy_overflow"),
    ],
)
def test_build_control_request_rejects_untrusted_or_unsafe_creation(change, code):
    api = _api()
    with pytest.raises(api.VpnControlIntentError) as caught:
        _request(**change)
    assert caught.value.code == code
    assert str(caught.value) == code
    assert caught.value.__context__ is None


def test_build_control_request_preserves_verified_empty_legacy_sub_id_without_creation():
    request = _request(
        access_key=_key(panel_sub_id="", config_uri="vless://persisted-secret"),
        action="suspend",
        allow_create=False,
        allow_shared_restart=True,
    )

    assert request.sub_id == ""
    assert request.action == "suspend"
    assert request.allow_shared_restart is True


@pytest.mark.parametrize("action", ["provision", "suspend"])
def test_build_control_request_rejects_every_non_revoke_action_after_sticky_revoke(action):
    api = _api()
    with pytest.raises(api.VpnControlIntentError) as caught:
        _request(
            access_key=_key(
                revoke_requested_at=NOW,
                config_uri="vless://persisted-secret",
            ),
            action=action,
            allow_create=False,
            allow_shared_restart=action == "suspend",
        )
    assert caught.value.code == "vpn_control_revoke_sticky"


def test_build_control_request_allows_repeated_revoke_after_sticky_revoke():
    request = _request(
        access_key=_key(
            revoke_requested_at=NOW,
            config_uri="vless://persisted-secret",
            status="pending_revoke",
        ),
        action="revoke",
        allow_create=False,
        allow_shared_restart=True,
    )

    assert request.action == "revoke"


def test_build_control_config_uri_is_exact_percent_encoded_reality_uri_without_fragment():
    api = _api()
    request = _request(
        endpoint=_endpoint(
            server_name="cdn.example.test",
            public_key="abc-DEF_01234567890123456789012345678901234",
            short_id="abcd",
            fingerprint="chrome",
        )
    )

    uri = api.build_control_config_uri(request)
    parsed = urlsplit(uri)

    assert parsed.scheme == "vless"
    assert parsed.username == CLIENT_UUID
    assert parsed.hostname == "vpn.example.test"
    assert parsed.port == 443
    assert parsed.fragment == ""
    assert parse_qs(parsed.query, strict_parsing=True) == {
        "encryption": ["none"],
        "flow": ["xtls-rprx-vision"],
        "security": ["reality"],
        "sni": ["cdn.example.test"],
        "fp": ["chrome"],
        "pbk": ["abc-DEF_01234567890123456789012345678901234"],
        "sid": ["abcd"],
        "type": ["raw"],
    }
    assert "#" not in uri


def test_build_control_config_uri_rejects_non_reality_request_with_static_error():
    api = _api()
    request = _request(
        access_key=_key(panel_sub_id="", config_uri="vless://legacy"),
        endpoint=_endpoint(
            security="none",
            server_name=None,
            public_key=None,
            short_id=None,
            fingerprint=None,
            flow=None,
            status="draining",
        ),
        action="revoke",
        allow_create=False,
        allow_shared_restart=True,
    )

    with pytest.raises(api.VpnControlIntentError) as caught:
        api.build_control_config_uri(request)
    assert caught.value.code == "vpn_control_config_uri_unsupported"
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_stage_locks_canonical_rows_flushes_without_commit_and_stores_detached_request(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        recording = RecordingSession(session)

        operation = await api.stage_vpn_control_operation(recording, 7, "provision", now=NOW)

        assert recording.flushes == 1
        assert operation.state == "queued"
        assert operation.generation == 1
        assert operation.error_code is None
        assert operation.claim_token is None
        request = parse_node_request(operation.request_snapshot)
        assert request.operation_id == UUID(operation.id)
        assert request.generation == 1
        assert request.action == "provision"
        assert request.allow_create is True
        assert request.allow_shared_restart is False
        assert operation.request_digest == node_request_digest(request)
        key = await session.get(VpnAccessKey, 7)
        endpoint = await session.get(VpnEndpoint, 5)
        assert key is not None and endpoint is not None
        assert (key.operation_generation, key.status, key.revoke_requested_at) == (
            1,
            "pending_sync",
            None,
        )
        endpoint.public_host = "mutated-after-snapshot.example"
        assert operation.request_snapshot["target"]["public_host"] == "vpn.example.test"
        serialized = str(operation.request_snapshot)
        assert "config_uri" not in operation.request_snapshot
        assert "ssh-super-secret" not in serialized
        assert "panel-super-secret" not in serialized
        assert "vless://" not in serialized

        assert [_statement_tables(statement) for statement in recording.statements] == [
            frozenset({"vpn_access_keys", "vpn_subscriptions"}),
            frozenset({"vpn_customers"}),
            frozenset({"vpn_subscriptions"}),
            frozenset({"vpn_access_keys"}),
            frozenset({"worker_nodes"}),
            frozenset({"vpn_endpoints"}),
            frozenset({"vpn_control_operations"}),
        ]
        assert recording.statements[0]._for_update_arg is None
        for statement in recording.statements[1:]:
            assert statement._for_update_arg is not None
            assert statement.get_execution_options()["populate_existing"] is True
        for statement in (recording.statements[2], recording.statements[3], recording.statements[6]):
            assert statement._order_by_clauses


@pytest.mark.asyncio
async def test_stage_revalidates_discovery_after_locking_without_mutating_rows(session_factory):
    api = _api()

    class StaleDiscoverySession(RecordingSession):
        async def scalar(self, statement):
            self.statements.append(statement)
            if len(self.statements) == 1:
                return 2
            return await self.session.scalar(statement)

    async with session_factory() as session:
        recording = StaleDiscoverySession(session)
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(recording, 7, "provision", now=NOW)
        assert caught.value.code == "vpn_control_binding_changed"
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert (key.operation_generation, key.status) == (0, "pending_sync")
        assert recording.flushes == 0


@pytest.mark.asyncio
async def test_stage_rejects_unflushed_caller_state_before_any_refresh(session_factory):
    api = _api()
    async with session_factory() as session:
        subscription = await session.get(VpnSubscription, 3)
        assert subscription is not None
        subscription.status = "disabled"
        recording = RecordingSession(session)

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(recording, 7, "suspend", now=NOW)

        assert caught.value.code == "vpn_control_unflushed_state"
        assert subscription.status == "disabled"
        assert recording.statements == []
        assert recording.flushes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_now",
    [
        0,
        datetime.min.replace(tzinfo=timezone(timedelta(hours=14))),
        datetime.max.replace(tzinfo=timezone(-timedelta(hours=14))),
    ],
)
async def test_stage_rejects_invalid_now_before_any_sql(session_factory, invalid_now):
    api = _api()
    async with session_factory() as session:
        recording = RecordingSession(session)

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(recording, 7, "provision", now=invalid_now)

        assert caught.value.code == "vpn_control_time_invalid"
        assert recording.statements == []
        assert recording.flushes == 0


@pytest.mark.asyncio
async def test_stage_rejects_worker_endpoint_mismatch_after_authoritative_locks(session_factory):
    api = _api()

    class MismatchedWorkerSession(RecordingSession):
        async def scalar(self, statement):
            self.statements.append(statement)
            if _statement_tables(statement) == frozenset({"worker_nodes"}):
                return WorkerNode(id=99, name="stale-worker")
            return await self.session.scalar(statement)

    async with session_factory() as session:
        recording = MismatchedWorkerSession(session)
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(recording, 7, "provision", now=NOW)
        assert caught.value.code == "vpn_control_binding_invalid"
        assert recording.flushes == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_state", ["claimed", "uncertain"])
async def test_stage_supersedes_only_same_key_queued_rows_and_preserves_reservations(
    session_factory,
    protected_state,
):
    api = _api()
    async with session_factory() as session:
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        key.operation_generation = 4
        queued = _operation(
            operation_id="10000000-0000-4000-8000-000000000001",
            generation=1,
            state="queued",
        )
        protected = _operation(
            operation_id="10000000-0000-4000-8000-000000000002",
            generation=2,
            state=protected_state,
        )
        terminal = _operation(
            operation_id="10000000-0000-4000-8000-000000000003",
            generation=3,
            state="succeeded",
        )
        future = _operation(
            operation_id="10000000-0000-4000-8000-000000000006",
            generation=6,
            state="queued",
        )
        other_key = _operation(
            operation_id="10000000-0000-4000-8000-000000000004",
            access_key_id=8,
            generation=1,
            state="queued",
        )
        session.add_all([queued, protected, terminal, future, other_key])
        await session.commit()

        new_operation = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)

        assert (new_operation.generation, new_operation.state) == (5, "queued")
        assert queued.state == "superseded"
        assert queued.finished_at == NOW
        assert queued.error_code == "vpn_control_superseded"
        assert protected.state == protected_state
        assert protected.finished_at is None
        assert protected.error_code is None
        assert terminal.state == "succeeded"
        assert terminal.finished_at is None
        assert future.state == "queued"
        assert future.finished_at is None
        assert other_key.state == "queued"
        assert other_key.finished_at is None


@pytest.mark.asyncio
async def test_stage_revoke_is_sticky_and_later_provision_cannot_clear_or_advance_it(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        operation = await api.stage_vpn_control_operation(session, 7, "revoke", now=NOW)
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert key.revoke_requested_at is not None and _utc(key.revoke_requested_at) == NOW
        assert (key.operation_generation, key.status) == (1, "pending_revoke")
        request = parse_node_request(operation.request_snapshot)
        assert request.action == "revoke"
        assert request.allow_create is False
        assert request.allow_shared_restart is True

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(
                session,
                7,
                "provision",
                now=NOW + timedelta(minutes=1),
            )
        assert caught.value.code == "vpn_control_revoke_sticky"
        assert key.revoke_requested_at is not None and _utc(key.revoke_requested_at) == NOW
        assert (key.operation_generation, key.status) == (1, "pending_revoke")

        newer = await api.stage_vpn_control_operation(
            session,
            7,
            "revoke",
            now=NOW + timedelta(minutes=2),
        )
        assert newer.generation == 2
        assert key.revoke_requested_at is not None and _utc(key.revoke_requested_at) == NOW


@pytest.mark.asyncio
async def test_stage_uses_policy_for_pending_suspend_but_preserves_manual_pending_revoke(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        key.status = "pending_suspend"
        key.config_uri = "vless://existing-private-uri"
        await session.commit()

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(session, 7, "suspend", now=NOW)
        assert caught.value.code == "vpn_control_policy_requires_provision"
        resumed = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        assert resumed.action == "provision"
        assert key.status == "pending_sync"
        await session.rollback()

        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        key.status = "pending_revoke"
        await session.commit()
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        assert caught.value.code == "vpn_control_revoke_sticky"
        revoked = await api.stage_vpn_control_operation(session, 7, "revoke", now=NOW)
        assert revoked.action == "revoke"
        assert key.revoke_requested_at is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "endpoint_status", "config_uri", "subscription_status", "expires_at"),
    [
        ("provision", "ready", None, "active", EXPIRES_AT),
        ("provision", "ready", "vless://existing-private-uri", "active", EXPIRES_AT),
        ("provision", "draining", "vless://existing-private-uri", "active", EXPIRES_AT),
        ("suspend", "ready", "vless://existing-private-uri", "expired", NOW),
        ("suspend", "draining", "vless://existing-private-uri", "expired", NOW),
        ("suspend", "disabled", "vless://existing-private-uri", "expired", NOW),
        ("revoke", "ready", None, "active", EXPIRES_AT),
        ("revoke", "draining", None, "active", EXPIRES_AT),
        ("revoke", "disabled", None, "active", EXPIRES_AT),
    ],
)
async def test_stage_allows_only_the_supported_policy_endpoint_matrix(
    session_factory,
    action,
    endpoint_status,
    config_uri,
    subscription_status,
    expires_at,
):
    api = _api()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        subscription = await session.get(VpnSubscription, 3)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and subscription is not None and key is not None
        endpoint.status = endpoint_status
        subscription.status = subscription_status
        subscription.expires_at = expires_at
        key.config_uri = config_uri
        await session.commit()

        operation = await api.stage_vpn_control_operation(session, 7, action, now=NOW)

        request = parse_node_request(operation.request_snapshot)
        assert request.action == action
        assert request.allow_create is (action == "provision" and config_uri is None)
        assert request.allow_shared_restart is (action in {"suspend", "revoke"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mutation", "action", "code"),
    [
        ("unbound", "provision", "vpn_control_binding_required"),
        ("archived_worker", "provision", "vpn_control_worker_unavailable"),
        ("unverified", "provision", "vpn_control_endpoint_unverified"),
        ("identity_null", "provision", "vpn_control_client_identity_unverified"),
        ("unsupported_transport", "provision", "vpn_control_endpoint_unsupported"),
        ("create_draining", "provision", "vpn_control_endpoint_creation_unavailable"),
        ("resume_disabled", "provision", "vpn_control_endpoint_resume_unavailable"),
        ("inactive_customer", "provision", "vpn_control_policy_requires_suspend"),
        ("cancelled_subscription", "provision", "vpn_control_policy_requires_revoke"),
        ("active_suspend", "suspend", "vpn_control_policy_requires_provision"),
        ("unknown_action", "rotate", "vpn_control_action_invalid"),
    ],
)
async def test_stage_rejects_invalid_binding_endpoint_or_persisted_policy(
    session_factory,
    mutation,
    action,
    code,
):
    api = _api()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        subscription = await session.get(VpnSubscription, 3)
        customer = await session.get(VpnCustomer, 1)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and subscription is not None and customer is not None and key is not None
        if mutation == "unbound":
            key.endpoint_id = None
        elif mutation == "archived_worker":
            worker = await session.get(WorkerNode, 2)
            assert worker is not None
            worker.archived_at = NOW
        elif mutation == "unverified":
            endpoint.verified_at = None
            endpoint.status = "staged"
        elif mutation == "identity_null":
            key.verified_client_email = None
        elif mutation == "unsupported_transport":
            endpoint.transport = "ws"
        elif mutation == "create_draining":
            endpoint.status = "draining"
        elif mutation == "resume_disabled":
            endpoint.status = "disabled"
            key.config_uri = "vless://existing-private-uri"
        elif mutation == "inactive_customer":
            customer.status = "disabled"
        elif mutation == "cancelled_subscription":
            subscription.status = "cancelled"
        await session.commit()

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(session, 7, action, now=NOW)
        assert caught.value.code == code
        assert str(caught.value) == code
        assert caught.value.__context__ is None
        refreshed = await session.get(VpnAccessKey, 7)
        assert refreshed is not None and refreshed.operation_generation == 0
        assert not (await session.scalars(select(VpnControlOperation))).all()


@pytest.mark.asyncio
async def test_stage_accepts_verified_legacy_empty_sub_id_only_for_existing_profile(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and key is not None
        endpoint.status = "draining"
        endpoint.security = "none"
        endpoint.server_name = None
        endpoint.public_key = None
        endpoint.short_id = None
        endpoint.fingerprint = None
        endpoint.flow = None
        key.config_uri = "vless://legacy-private-uri"
        key.panel_sub_id = ""
        await session.commit()

        operation = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)

        request = parse_node_request(operation.request_snapshot)
        assert request.sub_id == ""
        assert request.target.security == "none"
        assert request.allow_create is False


@pytest.mark.asyncio
@pytest.mark.parametrize("config_uri", ["", " \t "])
async def test_stage_treats_empty_or_whitespace_uri_as_creation_on_draining_endpoint(
    session_factory,
    config_uri,
):
    api = _api()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and key is not None
        endpoint.status = "draining"
        key.config_uri = config_uri
        await session.commit()

        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)

        assert caught.value.code == "vpn_control_endpoint_creation_unavailable"
        assert key.config_uri == config_uri
        assert key.operation_generation == 0


@pytest.mark.asyncio
async def test_stage_does_not_strip_or_replace_an_actual_nonempty_uri(session_factory):
    api = _api()
    uri = "  vless://existing-private-uri  "
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and key is not None
        endpoint.status = "draining"
        key.config_uri = uri
        await session.commit()

        operation = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)

        assert parse_node_request(operation.request_snapshot).allow_create is False
        assert key.config_uri == uri


@pytest.mark.asyncio
async def test_stage_rolls_back_with_caller_transaction(session_factory):
    api = _api()
    async with session_factory() as session:
        operation = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        operation_id = operation.id
        await session.rollback()

    async with session_factory() as verification:
        key = await verification.get(VpnAccessKey, 7)
        assert key is not None
        assert (key.operation_generation, key.status) == (0, "pending_sync")
        assert await verification.get(VpnControlOperation, operation_id) is None


async def _stage_claimed(
    session_factory,
    *,
    action="provision",
    now=NOW,
):
    api = _api()
    token = uuid4()
    async with session_factory() as session:
        operation = await api.stage_vpn_control_operation(session, 7, action, now=now)
        operation_id = UUID(operation.id)
        await session.commit()
    async with session_factory() as session:
        claimed = await api.claim_next_vpn_control_operation(
            session,
            claim_token=token,
            now=now + timedelta(seconds=1),
        )
        assert claimed is not None and UUID(claimed.id) == operation_id
        await session.commit()
    return operation_id, token


@pytest.mark.asyncio
async def test_claim_oldest_operation_reserves_worker_and_never_commits(session_factory):
    api = _api()
    async with session_factory() as session:
        first = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        await session.commit()
    async with session_factory() as session:
        second = await api.stage_vpn_control_operation(
            session,
            8,
            "provision",
            now=NOW + timedelta(seconds=1),
        )
        await session.commit()

    async with session_factory() as session:
        token = uuid4()
        claimed = await api.claim_next_vpn_control_operation(
            session,
            claim_token=token,
            now=NOW + timedelta(seconds=2),
        )
        assert claimed is not None
        assert claimed.id == first.id
        assert claimed.state == "claimed"
        assert claimed.claim_token == str(token)
        assert _utc(claimed.claimed_at) == NOW + timedelta(seconds=2)
        assert await api.active_vpn_control_worker_ids(session) == {2}
        assert (
            await api.claim_next_vpn_control_operation(
                session,
                claim_token=uuid4(),
                now=NOW + timedelta(days=30),
            )
            is None
        )
        assert (await session.get(VpnControlOperation, second.id)).state == "queued"
        await session.rollback()

    async with session_factory() as session:
        reclaimed = await api.claim_next_vpn_control_operation(
            session,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=3),
        )
        assert reclaimed is not None and reclaimed.id == first.id


@pytest.mark.asyncio
async def test_claim_locks_authoritative_rows_in_canonical_order_and_operation_last(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        await session.commit()
    async with session_factory() as session:
        recording = RecordingSession(session)
        claimed = await api.claim_next_vpn_control_operation(
            recording,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is not None
        tables = [_statement_tables(statement) for statement in recording.statements]
        assert tables == [
            frozenset({"vpn_control_operations"}),
            frozenset({"vpn_control_operations"}),
            frozenset({"vpn_access_keys", "vpn_subscriptions"}),
            frozenset({"vpn_customers"}),
            frozenset({"vpn_subscriptions"}),
            frozenset({"vpn_access_keys"}),
            frozenset({"worker_nodes"}),
            frozenset({"vpn_endpoints"}),
            frozenset({"vpn_control_operations"}),
            frozenset({"vpn_control_operations"}),
        ]
        assert all(statement._for_update_arg is None for statement in recording.statements[:3])
        assert all(statement._for_update_arg is not None for statement in recording.statements[3:8])
        assert all(
            statement._for_update_arg.skip_locked is True
            for statement in recording.statements[3:8]
        )
        assert recording.statements[8]._for_update_arg is None
        assert recording.statements[9]._for_update_arg is not None
        assert recording.statements[9]._for_update_arg.skip_locked is True


@pytest.mark.asyncio
async def test_claim_supersedes_queued_operation_when_persisted_policy_changed(
    session_factory,
):
    api = _api()
    async with session_factory() as session:
        operation = await api.stage_vpn_control_operation(session, 7, "provision", now=NOW)
        await session.commit()
    async with session_factory() as session:
        subscription = await session.get(VpnSubscription, 3)
        assert subscription is not None
        subscription.status = "disabled"
        await session.commit()
    async with session_factory() as session:
        assert (
            await api.claim_next_vpn_control_operation(
                session,
                claim_token=uuid4(),
                now=NOW + timedelta(minutes=1),
            )
            is None
        )
        stale = await session.get(VpnControlOperation, operation.id)
        assert stale is not None
        assert (stale.state, stale.error_code) == (
            "superseded",
            "vpn_control_superseded",
        )
        assert _utc(stale.finished_at) == NOW + timedelta(minutes=1)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_token", [None, "not-a-uuid", UUID(int=0)])
async def test_claim_rejects_invalid_token_before_sql(session_factory, bad_token):
    api = _api()
    async with session_factory() as session:
        recording = RecordingSession(session)
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.claim_next_vpn_control_operation(
                recording,
                claim_token=bad_token,
                now=NOW,
            )
        assert caught.value.code == "vpn_control_claim_token_invalid"
        assert recording.statements == []


@pytest.mark.asyncio
async def test_finalize_observed_provision_sets_exact_uri_and_sync_fields(session_factory):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)

    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert (finalized.state, finalized.error_code) == ("succeeded", None)
        assert _utc(finalized.finished_at) == NOW + timedelta(seconds=2)
        assert key.status == "active"
        assert key.config_uri == api.build_control_config_uri(
            parse_node_request(finalized.request_snapshot)
        )
        assert key.expires_at is not None and _utc(key.expires_at) == EXPIRES_AT
        assert _utc(key.last_synced_at) == NOW + timedelta(seconds=2)
        assert key.last_error is None


@pytest.mark.asyncio
async def test_finalize_discovers_without_lock_then_locks_operation_last(session_factory):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)
    async with session_factory() as session:
        recording = RecordingSession(session)
        finalized = await api.finalize_vpn_control_operation(
            recording,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        assert finalized.state == "succeeded"
        tables = [_statement_tables(statement) for statement in recording.statements]
        assert tables == [
            frozenset({"vpn_control_operations"}),
            frozenset({"vpn_access_keys", "vpn_subscriptions"}),
            frozenset({"vpn_customers"}),
            frozenset({"vpn_subscriptions"}),
            frozenset({"vpn_access_keys"}),
            frozenset({"worker_nodes"}),
            frozenset({"vpn_endpoints"}),
            frozenset({"vpn_control_operations"}),
        ]
        assert recording.statements[0]._for_update_arg is None
        assert recording.statements[1]._for_update_arg is None
        assert all(statement._for_update_arg is not None for statement in recording.statements[2:])


@pytest.mark.asyncio
async def test_finalize_old_provision_after_new_revoke_never_activates_or_writes_uri(
    session_factory,
):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)
    async with session_factory() as session:
        newer = await api.stage_vpn_control_operation(
            session,
            7,
            "revoke",
            now=NOW + timedelta(seconds=2),
        )
        await session.commit()

    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=3),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == "superseded"
        assert key.operation_generation == newer.generation == 2
        assert key.status == "pending_revoke"
        assert key.config_uri is None
        assert key.last_synced_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_state", "error_code", "expected_state"),
    [
        ("failed", "vpn_node_preflight_failed", "failed"),
        ("failed", "vpn_node_interrupted_before_mutation", "failed"),
        ("uncertain", "vpn_node_mutation_uncertain", "uncertain"),
        ("blocked", "vpn_node_reconciliation_required", "uncertain"),
        ("stale", "vpn_node_operation_stale", "superseded"),
        ("blocked", "vpn_node_key_revoked", "failed"),
    ],
)
async def test_finalize_maps_static_node_receipts_without_losing_pending_policy(
    session_factory,
    receipt_state,
    error_code,
    expected_state,
):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)
    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state=receipt_state,
            error_code=error_code,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == expected_state
        assert finalized.error_code == error_code
        assert key.status == "pending_sync"
        assert key.config_uri is None
        if expected_state == "uncertain":
            assert finalized.finished_at is None
            assert await api.active_vpn_control_worker_ids(session) == {2}
        else:
            assert _utc(finalized.finished_at) == NOW + timedelta(seconds=2)
            if expected_state == "failed":
                assert key.last_error == error_code
            else:
                assert key.last_error is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "subscription_status", "expected_status"),
    [
        ("suspend", "expired", "suspended"),
        ("revoke", "cancelled", "revoked"),
    ],
)
async def test_finalize_observed_suspend_and_revoke_preserve_uri(
    session_factory,
    action,
    subscription_status,
    expected_status,
):
    api = _api()
    async with session_factory() as session:
        subscription = await session.get(VpnSubscription, 3)
        key = await session.get(VpnAccessKey, 7)
        assert subscription is not None and key is not None
        subscription.status = subscription_status
        key.config_uri = "vless://preserved-private-uri"
        await session.commit()
    operation_id, token = await _stage_claimed(
        session_factory,
        action=action,
        now=NOW,
    )
    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == "succeeded"
        assert key.status == expected_status
        assert key.config_uri == "vless://preserved-private-uri"
        if action == "revoke":
            assert _utc(key.revoked_at) == NOW + timedelta(seconds=2)


@pytest.mark.asyncio
async def test_finalize_endpoint_disable_blocks_provision_but_allows_confirmed_revoke(
    session_factory,
):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == "superseded"
        assert (key.status, key.config_uri) == ("pending_sync", None)
        await session.commit()

    async with session_factory() as session:
        key = await session.get(VpnAccessKey, 7)
        endpoint = await session.get(VpnEndpoint, 5)
        assert key is not None and endpoint is not None
        key.config_uri = "vless://preserved-private-uri"
        endpoint.status = "ready"
        await session.commit()
    revoke_id, revoke_token = await _stage_claimed(
        session_factory,
        action="revoke",
        now=NOW,
    )
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
    async with session_factory() as session:
        finalized = await api.finalize_vpn_control_operation(
            session,
            revoke_id,
            revoke_token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == "succeeded"
        assert key.status == "revoked"


@pytest.mark.asyncio
async def test_finalize_is_idempotent_for_same_token_and_rejects_other_token(
    session_factory,
):
    api = _api()
    operation_id, token = await _stage_claimed(session_factory)
    async with session_factory() as session:
        first = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        await session.commit()
    async with session_factory() as session:
        replay = await api.finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(days=1),
        )
        assert replay.state == "succeeded"
        assert _utc(replay.finished_at) == _utc(first.finished_at)
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.finalize_vpn_control_operation(
                session,
                operation_id,
                uuid4(),
                receipt_state="observed",
                error_code=None,
                now=NOW + timedelta(days=1),
            )
        assert caught.value.code == "vpn_control_claim_token_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("receipt_state", "error_code"),
    [
        ("observed", "vpn_node_preflight_failed"),
        ("failed", None),
        ("uncertain", "arbitrary-secret-text"),
        ("blocked", "vpn_node_mutation_uncertain"),
        ("unknown", None),
    ],
)
async def test_finalize_rejects_noncontract_receipt_pairs_before_sql(
    session_factory,
    receipt_state,
    error_code,
):
    api = _api()
    async with session_factory() as session:
        recording = RecordingSession(session)
        with pytest.raises(api.VpnControlIntentError) as caught:
            await api.finalize_vpn_control_operation(
                recording,
                OPERATION_UUID,
                uuid4(),
                receipt_state=receipt_state,
                error_code=error_code,
                now=NOW,
            )
        assert caught.value.code == "vpn_control_receipt_invalid"
        assert recording.statements == []


@pytest.mark.asyncio
async def test_active_worker_ids_include_only_queued_claimed_and_uncertain(session_factory):
    api = _api()
    async with session_factory() as session:
        session.add_all(
            [
                _operation(
                    operation_id=f"30000000-0000-4000-8000-{generation:012d}",
                    access_key_id=7 if generation == 1 else 8,
                    generation=generation,
                    state=state,
                )
                for generation, state in enumerate(
                    ("queued", "succeeded"),
                    1,
                )
            ]
        )
        await session.flush()
        assert await api.active_vpn_control_worker_ids(session) == {2}
        queued = await session.get(
            VpnControlOperation,
            "30000000-0000-4000-8000-000000000001",
        )
        assert queued is not None
        for active_state in ("claimed", "uncertain"):
            queued.state = active_state
            queued.claim_token = str(uuid4())
            await session.flush()
            assert await api.active_vpn_control_worker_ids(session) == {2}
        queued.state = "failed"
        queued.claim_token = None
        await session.flush()
        assert await api.active_vpn_control_worker_ids(session) == set()
