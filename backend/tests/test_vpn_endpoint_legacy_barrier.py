from __future__ import annotations

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnCustomer,
    VpnEndpoint,
    VpnNodeEvent,
    VpnSubscription,
    WorkerNode,
)
from app.services.vpn_endpoints import VpnEndpointError
from app.services.vpn_provisioning import (
    build_vpn_client_revoke_command,
    build_vpn_client_suspend_command,
    ensure_vpn_client_uuid,
    provision_vpn_access_key,
    revoke_vpn_access_key,
    suspend_vpn_access_key,
)
from app.services.worker_decommission import (
    WorkerDecommissionConflictError,
    decommission_worker,
)


CLIENT_UUID = "96e7df5c-745a-4f46-8ee9-4afbd2bc32d5"
CONFIG_URI = f"vless://{CLIENT_UUID}@endpoint.example:8443"
ISSUED_AT = datetime(2026, 9, 1, 8, 0, tzinfo=UTC)
EXPIRES_AT = datetime(2026, 10, 1, 8, 0, tzinfo=UTC)
SUBSCRIPTION_EXPIRES_AT = datetime(2026, 11, 1, 8, 0, tzinfo=UTC)
SYNCED_AT = datetime(2026, 9, 20, 8, 0, tzinfo=UTC)
REVOKED_AT = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)
VERIFIED_AT = datetime(2026, 9, 1, 7, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def session_factory(tmp_path):
    database_path = tmp_path / "vpn-endpoint-legacy-barrier.sqlite3"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_bound_profile(
    session: AsyncSession,
    *,
    endpoint_status: str = "draining",
    key_status: str = "active",
    external_uuid: str | None = CLIENT_UUID,
    revoked_at: datetime | None = None,
    add_unbound_key: bool = False,
) -> tuple[WorkerNode, WorkerNode, VpnSubscription, VpnAccessKey, VpnAccessKey | None]:
    worker = WorkerNode(
        id=1,
        name="bound-worker",
        status="ready",
        is_enabled=True,
        control_token="worker-token",
        ip_address="192.0.2.99",
        ssh_host="192.0.2.99",
        ssh_password="ssh-secret",
        vpn_role="drop_worker_vpn",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_public_host="wrong-worker-default.example",
        vpn_panel_password="panel-secret",
        vpn_inbound_id=99,
        vpn_inbound_port=9443,
        vpn_inbound_protocol="trojan",
        vpn_inbound_transport="ws",
        vpn_inbound_security="tls",
        vpn_last_checked_at=SYNCED_AT,
        vpn_last_error="existing-worker-error",
    )
    other_worker = WorkerNode(
        id=2,
        name="other-worker",
        status="ready",
        is_enabled=True,
        control_token="other-worker-token",
        ip_address="192.0.2.100",
        ssh_host="192.0.2.100",
        ssh_password="other-ssh-secret",
        vpn_role="drop_worker_vpn",
        vpn_enabled=True,
        vpn_runtime_status="ready",
        vpn_inbound_id=7,
    )
    customer = VpnCustomer(id=1, status="active")
    session.add_all([worker, other_worker, customer])
    await session.flush()
    subscription = VpnSubscription(
        id=1,
        customer_id=customer.id,
        status="active",
        expires_at=SUBSCRIPTION_EXPIRES_AT,
        traffic_limit_gb=25,
        max_devices=2,
    )
    endpoint = VpnEndpoint(
        id=10,
        worker_id=worker.id,
        inbound_id=10,
        public_host="endpoint.example",
        port=8443,
        protocol="vless",
        transport="tcp",
        security="none",
        status=endpoint_status,
        verified_at=VERIFIED_AT,
    )
    session.add_all([subscription, endpoint])
    await session.flush()
    bound_key = VpnAccessKey(
        id=100,
        subscription_id=subscription.id,
        worker_id=worker.id,
        endpoint_id=endpoint.id,
        protocol="vless",
        public_name="bound-phone",
        external_uuid=external_uuid,
        config_uri=CONFIG_URI,
        status=key_status,
        issued_at=ISSUED_AT,
        expires_at=EXPIRES_AT,
        revoked_at=revoked_at,
        last_synced_at=SYNCED_AT,
        last_error="existing-key-error",
    )
    session.add(bound_key)
    unbound_key = None
    if add_unbound_key:
        unbound_key = VpnAccessKey(
            id=101,
            subscription_id=subscription.id,
            worker_id=worker.id,
            protocol="vless",
            public_name="unbound-phone",
            external_uuid="3f7827ce-b7c1-4b79-ab7a-55cd7af5a25a",
            config_uri="vless://unbound-private-config",
            status="active",
            issued_at=ISSUED_AT,
            expires_at=EXPIRES_AT,
            last_synced_at=SYNCED_AT,
            last_error="unbound-existing-error",
        )
        session.add(unbound_key)
    await session.commit()
    return worker, other_worker, subscription, bound_key, unbound_key


def _identity_snapshot(access_key: VpnAccessKey) -> tuple[object, ...]:
    return (
        access_key.external_uuid,
        access_key.config_uri,
        access_key.issued_at,
        access_key.expires_at,
        access_key.revoked_at,
        access_key.endpoint_id,
        access_key.worker_id,
        access_key.last_synced_at,
    )


def _worker_health_snapshot(worker: WorkerNode) -> tuple[object, ...]:
    return (
        worker.vpn_last_checked_at,
        worker.vpn_last_error,
        worker.vpn_runtime_status,
        worker.updated_at,
    )


async def _invoke_mutator(
    operation: str,
    session: AsyncSession,
    access_key: VpnAccessKey,
    *,
    worker: WorkerNode | None = None,
) -> VpnAccessKey:
    kwargs = {"worker": worker} if worker is not None else {}
    if operation == "provision":
        return await provision_vpn_access_key(session, access_key, **kwargs)
    if operation == "suspend":
        return await suspend_vpn_access_key(session, access_key, **kwargs)
    if operation == "revoke":
        return await revoke_vpn_access_key(session, access_key, **kwargs)
    raise AssertionError(f"Unsupported test operation: {operation}")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("provision", "pending_sync"),
        ("suspend", "pending_suspend"),
        ("revoke", "pending_revoke"),
    ],
)
async def test_bound_mutators_stop_before_legacy_identity_ssh_events_and_worker_health(
    session_factory,
    monkeypatch,
    operation,
    expected_status,
):
    ssh_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal ssh_calls
        ssh_calls += 1
        raise AssertionError("endpoint-bound key reached the legacy SSH adapter")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        worker, _, _, access_key, _ = await _seed_bound_profile(session)
        if operation == "provision":
            access_key.issued_at = None
            await session.flush()
        identity_before = _identity_snapshot(access_key)
        worker_health_before = _worker_health_snapshot(worker)
        event_count_before = await session.scalar(select(func.count(VpnNodeEvent.id)))

        result = await _invoke_mutator(operation, session, access_key)
        await session.flush()

        assert result is access_key
        assert ssh_calls == 0
        assert access_key.status == expected_status
        assert access_key.last_error == "vpn_endpoint_remote_adapter_required"
        assert _identity_snapshot(access_key) == identity_before
        assert _worker_health_snapshot(worker) == worker_health_before
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == event_count_before


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["provision", "suspend", "revoke"])
@pytest.mark.parametrize("external_uuid", [None, "legacy-client-id"])
async def test_bound_mutators_never_generate_or_replace_invalid_client_identity(
    session_factory,
    monkeypatch,
    operation,
    external_uuid,
):
    ssh_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal ssh_calls
        ssh_calls += 1
        raise AssertionError("invalid endpoint-bound identity reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        _, _, _, access_key, _ = await _seed_bound_profile(
            session,
            external_uuid=external_uuid,
        )
        identity_before = _identity_snapshot(access_key)

        await _invoke_mutator(operation, session, access_key)

        assert ssh_calls == 0
        assert access_key.external_uuid == external_uuid
        assert access_key.last_error == "vpn_endpoint_client_identity_invalid"
        assert _identity_snapshot(access_key) == identity_before


@pytest.mark.asyncio
async def test_disabled_endpoint_revoke_is_held_pending_not_falsely_confirmed(
    session_factory,
    monkeypatch,
):
    ssh_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal ssh_calls
        ssh_calls += 1
        raise AssertionError("disabled endpoint revoke reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        _, _, _, access_key, _ = await _seed_bound_profile(
            session,
            endpoint_status="disabled",
        )

        await revoke_vpn_access_key(session, access_key)

        assert ssh_calls == 0
        assert access_key.status == "pending_revoke"
        assert access_key.revoked_at is None
        assert access_key.last_error == "vpn_endpoint_remote_adapter_required"


@pytest.mark.asyncio
async def test_resolver_worker_mismatch_is_static_and_does_not_leak_credentials(
    session_factory,
    monkeypatch,
):
    ssh_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal ssh_calls
        ssh_calls += 1
        raise AssertionError("mismatched endpoint worker reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        _, other_worker, _, access_key, _ = await _seed_bound_profile(session)

        await provision_vpn_access_key(session, access_key, worker=other_worker)

        assert ssh_calls == 0
        assert access_key.status == "pending_sync"
        assert access_key.last_error == "vpn_endpoint_worker_mismatch"
        assert access_key.external_uuid not in access_key.last_error
        assert access_key.config_uri not in access_key.last_error
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "expected_status"),
    [
        ("provision", "pending_sync"),
        ("suspend", "pending_suspend"),
        ("revoke", "pending_revoke"),
    ],
)
async def test_bound_mutators_use_endpoint_barrier_when_worker_legacy_defaults_are_missing(
    session_factory,
    monkeypatch,
    operation,
    expected_status,
):
    ssh_calls = 0

    async def fail_if_called(*_args, **_kwargs):
        nonlocal ssh_calls
        ssh_calls += 1
        raise AssertionError("endpoint-bound key reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        worker, _, _, access_key, _ = await _seed_bound_profile(session)
        worker.ssh_password = None
        worker.ssh_key_path = None
        worker.vpn_inbound_id = None
        worker.vpn_public_host = None
        worker.ip_address = None
        worker.ssh_host = None
        await session.flush()

        await _invoke_mutator(operation, session, access_key)

        assert ssh_calls == 0
        assert access_key.status == expected_status
        assert access_key.last_error == "vpn_endpoint_remote_adapter_required"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_status", "revoked_at"),
    [
        ("revoked", REVOKED_AT),
        ("pending_revoke", None),
        ("active", REVOKED_AT),
    ],
)
async def test_bound_provision_preserves_permanent_revocation_state(
    session_factory,
    monkeypatch,
    key_status,
    revoked_at,
):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("permanently revoked key reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        worker, _, _, access_key, _ = await _seed_bound_profile(
            session,
            key_status=key_status,
            revoked_at=revoked_at,
        )
        identity_before = _identity_snapshot(access_key)
        worker_health_before = _worker_health_snapshot(worker)
        updated_at_before = access_key.updated_at

        await provision_vpn_access_key(session, access_key)

        assert access_key.status == key_status
        assert access_key.revoked_at == revoked_at
        assert access_key.last_error == "vpn_endpoint_key_revoked"
        assert access_key.updated_at != updated_at_before
        assert _identity_snapshot(access_key) == identity_before
        assert _worker_health_snapshot(worker) == worker_health_before
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key_status", "revoked_at"),
    [
        ("revoked", REVOKED_AT),
        ("pending_revoke", None),
        ("active", REVOKED_AT),
    ],
)
async def test_bound_suspend_is_noop_for_permanent_revocation_state(
    session_factory,
    monkeypatch,
    key_status,
    revoked_at,
):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("permanently revoked key reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        worker, _, _, access_key, _ = await _seed_bound_profile(
            session,
            key_status=key_status,
            revoked_at=revoked_at,
        )
        access_key_snapshot = (
            access_key.status,
            access_key.last_error,
            access_key.updated_at,
            _identity_snapshot(access_key),
        )
        worker_health_before = _worker_health_snapshot(worker)

        await suspend_vpn_access_key(session, access_key)

        assert (
            access_key.status,
            access_key.last_error,
            access_key.updated_at,
            _identity_snapshot(access_key),
        ) == access_key_snapshot
        assert _worker_health_snapshot(worker) == worker_health_before
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == 0


@pytest.mark.asyncio
async def test_bound_revoke_keeps_already_revoked_profile_as_noop(session_factory, monkeypatch):
    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("already revoked key reached legacy SSH")

    monkeypatch.setattr(
        "app.services.vpn_provisioning.execute_worker_ssh_commands",
        fail_if_called,
    )
    async with session_factory() as session:
        worker, _, _, access_key, _ = await _seed_bound_profile(
            session,
            key_status="revoked",
            revoked_at=REVOKED_AT,
        )
        access_key_snapshot = (
            access_key.status,
            access_key.last_error,
            access_key.updated_at,
            _identity_snapshot(access_key),
        )
        worker_health_before = _worker_health_snapshot(worker)

        await revoke_vpn_access_key(session, access_key)

        assert (
            access_key.status,
            access_key.last_error,
            access_key.updated_at,
            _identity_snapshot(access_key),
        ) == access_key_snapshot
        assert _worker_health_snapshot(worker) == worker_health_before
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == 0


@pytest.mark.parametrize(
    "builder",
    [build_vpn_client_suspend_command, build_vpn_client_revoke_command],
)
def test_legacy_command_builders_reject_endpoint_bound_profiles(builder):
    worker = WorkerNode(id=1, name="worker", ip_address="192.0.2.1", vpn_inbound_id=99)
    access_key = VpnAccessKey(
        id=100,
        subscription_id=1,
        worker_id=worker.id,
        endpoint_id=10,
        external_uuid=CLIENT_UUID,
        config_uri=CONFIG_URI,
    )

    with pytest.raises(VpnEndpointError) as exc_info:
        builder(worker, access_key)

    assert exc_info.value.code == "vpn_endpoint_remote_adapter_required"
    assert CLIENT_UUID not in str(exc_info.value)
    assert CONFIG_URI not in str(exc_info.value)


@pytest.mark.parametrize("external_uuid", [None, "legacy-client-id"])
def test_uuid_helper_rejects_invalid_endpoint_bound_identity_without_replacing_it(external_uuid):
    access_key = VpnAccessKey(
        subscription_id=1,
        worker_id=1,
        endpoint_id=10,
        external_uuid=external_uuid,
    )

    with pytest.raises(VpnEndpointError) as exc_info:
        ensure_vpn_client_uuid(access_key)

    assert exc_info.value.code == "vpn_endpoint_client_identity_invalid"
    assert access_key.external_uuid == external_uuid


def test_uuid_helper_returns_existing_valid_endpoint_bound_identity():
    access_key = VpnAccessKey(
        subscription_id=1,
        worker_id=1,
        endpoint_id=10,
        external_uuid=CLIENT_UUID,
    )

    client_uuid = ensure_vpn_client_uuid(access_key)

    assert str(client_uuid) == CLIENT_UUID
    assert access_key.external_uuid == CLIENT_UUID


@pytest.mark.asyncio
@pytest.mark.parametrize("bound_status", ["active", "pending_revoke", "unexpected_state"])
async def test_decommission_blocks_any_nonrevoked_bound_profile_without_partial_mutation(
    session_factory,
    bound_status,
):
    async with session_factory() as session:
        worker, _, _, bound_key, unbound_key = await _seed_bound_profile(
            session,
            key_status=bound_status,
            add_unbound_key=True,
        )
        assert unbound_key is not None
        worker_snapshot = (
            worker.archived_at,
            worker.is_enabled,
            worker.status,
            worker.control_token,
            worker.ssh_password,
            worker.vpn_enabled,
            worker.vpn_role,
            worker.vpn_runtime_status,
            worker.vpn_panel_password,
        )
        bound_snapshot = (bound_key.status, bound_key.config_uri, bound_key.revoked_at, bound_key.last_error)
        unbound_snapshot = (
            unbound_key.status,
            unbound_key.config_uri,
            unbound_key.revoked_at,
            unbound_key.last_error,
        )

        with pytest.raises(
            WorkerDecommissionConflictError,
            match="Endpoint-bound VPN profiles must be remotely revoked before node removal",
        ):
            await decommission_worker(session, worker.id)

        assert (
            worker.archived_at,
            worker.is_enabled,
            worker.status,
            worker.control_token,
            worker.ssh_password,
            worker.vpn_enabled,
            worker.vpn_role,
            worker.vpn_runtime_status,
            worker.vpn_panel_password,
        ) == worker_snapshot
        assert (bound_key.status, bound_key.config_uri, bound_key.revoked_at, bound_key.last_error) == bound_snapshot
        assert (
            unbound_key.status,
            unbound_key.config_uri,
            unbound_key.revoked_at,
            unbound_key.last_error,
        ) == unbound_snapshot
        assert await session.scalar(select(func.count(VpnNodeEvent.id))) == 0


@pytest.mark.asyncio
async def test_decommission_allows_confirmed_revoked_bound_history(session_factory):
    async with session_factory() as session:
        worker, _, _, bound_key, unbound_key = await _seed_bound_profile(
            session,
            key_status="revoked",
            revoked_at=REVOKED_AT,
            add_unbound_key=True,
        )
        assert unbound_key is not None
        bound_snapshot = (
            bound_key.status,
            bound_key.config_uri,
            bound_key.revoked_at,
            bound_key.last_error,
        )

        result = await decommission_worker(session, worker.id)

        assert result.retired_key_count == 1
        assert worker.status == "archived"
        assert (
            bound_key.status,
            bound_key.config_uri,
            bound_key.revoked_at,
            bound_key.last_error,
        ) == bound_snapshot
        assert unbound_key.status == "revoked"
        assert unbound_key.config_uri is None


@pytest.mark.asyncio
async def test_decommission_other_worker_is_not_blocked_by_bound_profile(session_factory):
    async with session_factory() as session:
        _, other_worker, subscription, bound_key, _ = await _seed_bound_profile(session)
        other_key = VpnAccessKey(
            id=102,
            subscription_id=subscription.id,
            worker_id=other_worker.id,
            external_uuid="3d48e2d5-7d72-4200-a0d7-593137012e1e",
            config_uri="vless://other-unbound-profile",
            status="active",
        )
        session.add(other_key)
        await session.commit()
        bound_snapshot = (bound_key.status, bound_key.config_uri, bound_key.revoked_at)

        result = await decommission_worker(session, other_worker.id)

        assert result.retired_key_count == 1
        assert other_worker.status == "archived"
        assert other_key.status == "revoked"
        assert (bound_key.status, bound_key.config_uri, bound_key.revoked_at) == bound_snapshot
