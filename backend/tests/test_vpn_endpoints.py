from __future__ import annotations

from contextlib import nullcontext
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime
from importlib import import_module
from importlib.util import find_spec
from typing import cast, get_args
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import VpnAccessKey, VpnCustomer, VpnEndpoint, VpnSubscription, WorkerNode


CLIENT_UUID = "96e7df5c-745a-4f46-8ee9-4afbd2bc32d5"
VERIFIED_AT = datetime(2026, 9, 21, 9, 30, tzinfo=UTC)


def _endpoint_module():
    spec = find_spec("app.services.vpn_endpoints")
    assert spec is not None, "app.services.vpn_endpoints must be available"
    return import_module("app.services.vpn_endpoints")


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    database_path = tmp_path / "vpn-endpoints.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with factory() as session:
        worker = WorkerNode(
            id=1,
            name="vpn-worker",
            status="ready",
            vpn_public_host="unsafe-default.example",
            vpn_inbound_id=999,
            vpn_inbound_port=9443,
            vpn_inbound_protocol="trojan",
            vpn_inbound_transport="ws",
            vpn_inbound_security="tls",
            vpn_panel_password="never-return-this",
            ssh_password="never-return-this-either",
        )
        customer = VpnCustomer(id=1, status="active")
        session.add_all([worker, customer])
        await session.flush()
        subscription = VpnSubscription(id=1, customer_id=customer.id, status="active")
        session.add(subscription)
        await session.flush()
        session.add_all(
            [
                VpnEndpoint(
                    id=10,
                    worker_id=worker.id,
                    inbound_id=10,
                    public_host="vpn.example",
                    port=8443,
                    protocol="vless",
                    transport="tcp",
                    security="none",
                    status="draining",
                    verified_at=VERIFIED_AT,
                ),
                VpnEndpoint(
                    id=20,
                    worker_id=worker.id,
                    inbound_id=20,
                    public_host="reality.example",
                    port=443,
                    protocol="vless",
                    transport="raw",
                    security="reality",
                    server_name="cdn.example",
                    public_key="public-key",
                    short_id="0123456789abcdef",
                    fingerprint="chrome",
                    flow="xtls-rprx-vision",
                    status="ready",
                    verified_at=VERIFIED_AT,
                ),
            ]
        )
        await session.flush()
        session.add(
            VpnAccessKey(
                id=100,
                subscription_id=subscription.id,
                worker_id=worker.id,
                endpoint_id=10,
                protocol="vless",
                external_uuid=CLIENT_UUID,
                config_uri="vless://existing-profile",
                status="active",
            )
        )
        await session.commit()

    try:
        yield factory
    finally:
        await engine.dispose()


async def _load_bound(session: AsyncSession) -> tuple[VpnAccessKey, WorkerNode]:
    access_key = await session.get(VpnAccessKey, 100)
    worker = await session.get(WorkerNode, 1)
    assert access_key is not None
    assert worker is not None
    return access_key, worker


def _detached_endpoint(**overrides) -> VpnEndpoint:
    values = {
        "id": 10,
        "worker_id": 1,
        "inbound_id": 10,
        "public_host": "vpn.example",
        "port": 8443,
        "protocol": "vless",
        "transport": "tcp",
        "security": "none",
        "status": "draining",
        "verified_at": VERIFIED_AT,
    }
    values.update(overrides)
    return VpnEndpoint(**values)


def _detached_key(**overrides) -> VpnAccessKey:
    values = {
        "id": 100,
        "subscription_id": 1,
        "worker_id": 1,
        "endpoint_id": 10,
        "protocol": "vless",
        "external_uuid": CLIENT_UUID,
        "config_uri": "vless://existing-profile",
        "status": "active",
    }
    values.update(overrides)
    return VpnAccessKey(**values)


def _detached_worker(**overrides) -> WorkerNode:
    values = {"id": 1, "name": "vpn-worker", "archived_at": None}
    values.update(overrides)
    return WorkerNode(**values)


class _ScalarSession:
    def __init__(self, endpoint: VpnEndpoint | None):
        self.endpoint = endpoint
        self.statement = None
        self.no_autoflush_entries = 0

    @property
    def no_autoflush(self):
        self.no_autoflush_entries += 1
        return nullcontext()

    async def scalar(self, statement):
        self.statement = statement
        return self.endpoint


async def _assert_error(code: str, awaitable) -> None:
    module = _endpoint_module()
    with pytest.raises(module.VpnEndpointError) as exc_info:
        await awaitable
    assert exc_info.value.code == code
    assert str(exc_info.value) == code


def test_service_module_is_available():
    assert _endpoint_module().__name__ == "app.services.vpn_endpoints"


def test_public_types_are_exact_and_error_strings_are_static():
    module = _endpoint_module()

    assert get_args(module.EndpointOperation) == ("provision", "suspend", "revoke")
    assert issubclass(module.VpnEndpointError, ValueError)
    error = module.VpnEndpointError("vpn_endpoint_static_code")
    assert error.code == "vpn_endpoint_static_code"
    assert str(error) == "vpn_endpoint_static_code"

    assert [field.name for field in fields(module.VpnEndpointTarget)] == [
        "endpoint_id",
        "worker_id",
        "inbound_id",
        "public_host",
        "port",
        "protocol",
        "transport",
        "security",
        "server_name",
        "public_key",
        "short_id",
        "fingerprint",
        "flow",
    ]
    assert module.VpnEndpointTarget.__dataclass_params__.frozen is True
    assert "__slots__" in module.VpnEndpointTarget.__dict__


def test_require_bound_client_uuid_returns_existing_identity_unchanged():
    module = _endpoint_module()
    access_key = _detached_key(external_uuid=CLIENT_UUID)

    result = module.require_bound_client_uuid(access_key)

    assert result == UUID(CLIENT_UUID)
    assert access_key.external_uuid == CLIENT_UUID


@pytest.mark.parametrize("value", [None, "", "not-a-uuid", object()])
def test_require_bound_client_uuid_rejects_invalid_identity_without_leaking_value(value):
    module = _endpoint_module()
    access_key = _detached_key(external_uuid=cast(str | None, value))

    with pytest.raises(module.VpnEndpointError) as exc_info:
        module.require_bound_client_uuid(access_key)

    assert exc_info.value.code == "vpn_endpoint_client_identity_invalid"
    assert str(exc_info.value) == "vpn_endpoint_client_identity_invalid"
    assert repr(value) not in str(exc_info.value)
    assert access_key.external_uuid is value


@pytest.mark.asyncio
async def test_resolver_returns_frozen_endpoint_only_snapshot_and_ignores_worker_defaults(
        session_factory,
):
    module = _endpoint_module()
    async with session_factory() as session:
        access_key, worker = await _load_bound(session)
        original_key_identity = (
            access_key.external_uuid,
            access_key.config_uri,
            access_key.worker_id,
            access_key.endpoint_id,
            access_key.status,
        )

        target = await module.resolve_recorded_endpoint(
            session, access_key, worker=worker, operation="provision"
        )
        worker.vpn_public_host = "changed-default.example"
        worker.vpn_inbound_id = 1234
        worker.vpn_inbound_port = 10443
        changed_target = await module.resolve_recorded_endpoint(
            session, access_key, worker=worker, operation="provision"
        )
        worker.vpn_public_host = None
        worker.vpn_inbound_id = None
        worker.vpn_inbound_port = None
        cleared_target = await module.resolve_recorded_endpoint(
            session, access_key, worker=worker, operation="provision"
        )

    expected = module.VpnEndpointTarget(
        endpoint_id=10,
        worker_id=1,
        inbound_id=10,
        public_host="vpn.example",
        port=8443,
        protocol="vless",
        transport="tcp",
        security="none",
        server_name=None,
        public_key=None,
        short_id=None,
        fingerprint=None,
        flow=None,
    )
    assert target == changed_target == cleared_target == expected
    assert (
        access_key.external_uuid,
        access_key.config_uri,
        access_key.worker_id,
        access_key.endpoint_id,
        access_key.status,
    ) == original_key_identity == (
        CLIENT_UUID,
        "vless://existing-profile",
        1,
        10,
        "active",
    )
    assert not hasattr(target, "worker")
    assert not hasattr(target, "password")
    assert not hasattr(target, "external_uuid")
    assert not hasattr(target, "config_uri")
    with pytest.raises(FrozenInstanceError):
        target.public_host = "mutated.example"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint_id", "operation", "config_uri", "expected_error"),
    [
        (20, "provision", None, None),
        (20, "suspend", None, None),
        (20, "revoke", None, None),
        (10, "provision", "vless://existing", None),
        (10, "suspend", None, None),
        (10, "revoke", None, None),
        (10, "provision", None, "vpn_endpoint_existing_profile_required"),
    ],
)
async def test_status_operation_matrix_for_ready_and_draining_endpoints(
    session_factory, endpoint_id, operation, config_uri, expected_error
):
    module = _endpoint_module()
    async with session_factory() as session:
        access_key, worker = await _load_bound(session)
        access_key.endpoint_id = endpoint_id
        access_key.config_uri = config_uri
        if expected_error is None:
            target = await module.resolve_recorded_endpoint(
                session, access_key, worker=worker, operation=operation
            )
            if endpoint_id == 20:
                assert target == module.VpnEndpointTarget(
                    endpoint_id=20,
                    worker_id=1,
                    inbound_id=20,
                    public_host="reality.example",
                    port=443,
                    protocol="vless",
                    transport="raw",
                    security="reality",
                    server_name="cdn.example",
                    public_key="public-key",
                    short_id="0123456789abcdef",
                    fingerprint="chrome",
                    flow="xtls-rprx-vision",
                )
            else:
                assert target.endpoint_id == endpoint_id
        else:
            await _assert_error(
                expected_error,
                module.resolve_recorded_endpoint(
                    session, access_key, worker=worker, operation=operation
                ),
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["suspend", "revoke"])
async def test_disabled_endpoint_allows_non_provision_operations(session_factory, operation):
    module = _endpoint_module()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 10)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
        access_key, worker = await _load_bound(session)

        target = await module.resolve_recorded_endpoint(
            session, access_key, worker=worker, operation=operation
        )

    assert target.endpoint_id == 10


@pytest.mark.asyncio
@pytest.mark.parametrize("key_status", ["revoked", "pending_revoke"])
async def test_provision_rejects_revoked_key_states(session_factory, key_status):
    module = _endpoint_module()
    async with session_factory() as session:
        access_key, worker = await _load_bound(session)
        access_key.status = key_status
        await _assert_error(
            "vpn_endpoint_key_revoked",
            module.resolve_recorded_endpoint(
                session, access_key, worker=worker, operation="provision"
            ),
        )


@pytest.mark.asyncio
async def test_provision_rejects_manual_revocation_timestamp(session_factory):
    module = _endpoint_module()
    async with session_factory() as session:
        access_key, worker = await _load_bound(session)
        access_key.revoked_at = VERIFIED_AT
        await _assert_error(
            "vpn_endpoint_key_revoked",
            module.resolve_recorded_endpoint(
                session, access_key, worker=worker, operation="provision"
            ),
        )


@pytest.mark.asyncio
async def test_provision_rejects_disabled_endpoint(session_factory):
    module = _endpoint_module()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 10)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
        access_key, worker = await _load_bound(session)
        await _assert_error(
            "vpn_endpoint_disabled",
            module.resolve_recorded_endpoint(
                session, access_key, worker=worker, operation="provision"
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("access_key", "worker", "operation", "code"),
    [
        (_detached_key(endpoint_id=None), None, "invalid", "vpn_endpoint_operation_invalid"),
        (_detached_key(endpoint_id=None), None, "suspend", "vpn_endpoint_binding_required"),
        (_detached_key(), None, "suspend", "vpn_endpoint_worker_mismatch"),
        (_detached_key(worker_id=2), _detached_worker(), "suspend", "vpn_endpoint_worker_mismatch"),
        (
            _detached_key(),
            _detached_worker(archived_at=VERIFIED_AT),
            "suspend",
            "vpn_endpoint_worker_archived",
        ),
    ],
)
async def test_prequery_validation_errors_are_static(access_key, worker, operation, code):
    module = _endpoint_module()
    db = _ScalarSession(_detached_endpoint())
    await _assert_error(
        code,
        module.resolve_recorded_endpoint(
            cast(AsyncSession, db),
            access_key,
            worker=worker,
            operation=cast(object, operation),
        ),
    )
    assert db.statement is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("endpoint", "code"),
    [
        (None, "vpn_endpoint_not_found"),
        (_detached_endpoint(worker_id=2), "vpn_endpoint_worker_mismatch"),
        (_detached_endpoint(verified_at=None), "vpn_endpoint_unverified"),
        (_detached_endpoint(status="staged"), "vpn_endpoint_unavailable"),
        (_detached_endpoint(status="unknown"), "vpn_endpoint_unavailable"),
        (_detached_endpoint(security="unknown"), "vpn_endpoint_transport_invalid"),
        (
            _detached_endpoint(status="ready", security="none"),
            "vpn_endpoint_transport_invalid",
        ),
    ],
)
async def test_endpoint_state_validation_errors_are_static(endpoint, code):
    module = _endpoint_module()
    db = _ScalarSession(endpoint)
    await _assert_error(
        code,
        module.resolve_recorded_endpoint(
            cast(AsyncSession, db),
            _detached_key(),
            worker=_detached_worker(),
            operation="suspend",
        ),
    )
    assert db.no_autoflush_entries == 1
    assert db.statement is not None
    assert db.statement.get_execution_options()["populate_existing"] is True
    assert db.statement._for_update_arg is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"protocol": "trojan"}, "vpn_endpoint_transport_unsupported"),
        ({"transport": "ws"}, "vpn_endpoint_transport_unsupported"),
        ({"inbound_id": 0}, "vpn_endpoint_address_invalid"),
        ({"inbound_id": True}, "vpn_endpoint_address_invalid"),
        ({"port": 0}, "vpn_endpoint_address_invalid"),
        ({"port": 65536}, "vpn_endpoint_address_invalid"),
        ({"port": True}, "vpn_endpoint_address_invalid"),
        ({"public_host": ""}, "vpn_endpoint_address_invalid"),
        ({"public_host": " vpn.example"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn.example "}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn host.example"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn\thost.example"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn\u00a0host.example"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn.example/path"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn.example?x=1"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "vpn.example#fragment"}, "vpn_endpoint_address_invalid"),
        ({"public_host": "user@vpn.example"}, "vpn_endpoint_address_invalid"),
    ],
)
async def test_protocol_transport_and_address_validation(overrides, code):
    module = _endpoint_module()
    endpoint = _detached_endpoint(**overrides)
    await _assert_error(
        code,
        module.resolve_recorded_endpoint(
            cast(AsyncSession, _ScalarSession(endpoint)),
            _detached_key(),
            worker=_detached_worker(),
            operation="suspend",
        ),
    )


@pytest.mark.asyncio
async def test_key_protocol_must_match_endpoint_protocol():
    module = _endpoint_module()
    await _assert_error(
        "vpn_endpoint_protocol_mismatch",
        module.resolve_recorded_endpoint(
            cast(AsyncSession, _ScalarSession(_detached_endpoint())),
            _detached_key(protocol="trojan"),
            worker=_detached_worker(),
            operation="suspend",
        ),
    )


@pytest.mark.asyncio
async def test_resolver_requires_valid_existing_uuid_without_writing_one():
    module = _endpoint_module()
    for value in (None, "secret-invalid-client-id"):
        access_key = _detached_key(external_uuid=value)
        await _assert_error(
            "vpn_endpoint_client_identity_invalid",
            module.resolve_recorded_endpoint(
                cast(AsyncSession, _ScalarSession(_detached_endpoint())),
                access_key,
                worker=_detached_worker(),
                operation="suspend",
            ),
        )
        assert access_key.external_uuid is value


@pytest.mark.asyncio
async def test_endpoint_query_refreshes_stale_identity_map_from_committed_state(session_factory):
    module = _endpoint_module()
    async with session_factory() as stale_session:
        access_key, worker = await _load_bound(stale_session)
        cached_endpoint = await stale_session.get(VpnEndpoint, 10)
        assert cached_endpoint is not None
        assert cached_endpoint.public_host == "vpn.example"

        async with session_factory() as writer:
            endpoint = await writer.get(VpnEndpoint, 10)
            assert endpoint is not None
            endpoint.public_host = "fresh.example"
            endpoint.port = 9443
            await writer.commit()

        target = await module.resolve_recorded_endpoint(
            stale_session, access_key, worker=worker, operation="suspend"
        )

    assert target.public_host == "fresh.example"
    assert target.port == 9443
    assert cached_endpoint.public_host == "fresh.example"


@pytest.mark.asyncio
async def test_no_autoflush_and_resolution_make_no_database_writes(session_factory):
    module = _endpoint_module()
    async with session_factory() as session:
        access_key, worker = await _load_bound(session)
        worker.name = "uncommitted-worker-name"
        transaction_events = {"flushes": 0, "commits": 0}

        def _record_flush(_session, _flush_context, _instances) -> None:
            transaction_events["flushes"] += 1

        def _record_commit(_session) -> None:
            transaction_events["commits"] += 1

        event.listen(session.sync_session, "before_flush", _record_flush)
        event.listen(session.sync_session, "before_commit", _record_commit)
        try:
            target = await module.resolve_recorded_endpoint(
                session, access_key, worker=worker, operation="suspend"
            )
        finally:
            event.remove(session.sync_session, "before_flush", _record_flush)
            event.remove(session.sync_session, "before_commit", _record_commit)

        assert transaction_events == {"flushes": 0, "commits": 0}

        async with session_factory() as observer:
            stored_worker = await observer.get(WorkerNode, 1)
            stored_key = await observer.get(VpnAccessKey, 100)
            stored_endpoint = await observer.get(VpnEndpoint, 10)
            assert stored_worker is not None
            assert stored_key is not None
            assert stored_endpoint is not None
            assert stored_worker.name == "vpn-worker"
            assert stored_key.external_uuid == CLIENT_UUID
            assert stored_key.status == "active"
            assert stored_key.config_uri == "vless://existing-profile"
            assert stored_endpoint.public_host == "vpn.example"
            assert stored_endpoint.port == 8443
            assert stored_endpoint.status == "draining"

    assert target.endpoint_id == 10


@pytest.mark.asyncio
async def test_real_database_supports_tls_and_raw_transport(session_factory):
    module = _endpoint_module()
    async with session_factory() as session:
        endpoint = await session.get(VpnEndpoint, 20)
        assert endpoint is not None
        endpoint.security = "tls"
        endpoint.transport = "raw"
        await session.commit()
        access_key, worker = await _load_bound(session)
        access_key.endpoint_id = 20

        target = await module.resolve_recorded_endpoint(
            session, access_key, worker=worker, operation="suspend"
        )

    assert target.security == "tls"
    assert target.transport == "raw"


def test_resolver_docstring_disclaims_authorization_locking_and_remote_verification():
    module = _endpoint_module()
    docstring = module.resolve_recorded_endpoint.__doc__ or ""
    assert "recorded" in docstring.lower()
    assert "authoriz" in docstring.lower()
    assert "lock" in docstring.lower()
    assert "remote" in docstring.lower()
