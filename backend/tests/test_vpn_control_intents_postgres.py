from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS
from app.services.vpn_control_intents import (
    active_vpn_control_worker_ids,
    claim_next_vpn_control_operation,
    finalize_vpn_control_operation,
    stage_vpn_control_operation,
)
from test_vpn_endpoint_migrations import (
    PostgresSchema,
    postgres_schema as postgres_schema,
)


NOW = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
EXPIRES_AT = NOW + timedelta(days=30)
PUBLIC_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
@dataclass(frozen=True)
class PostgresControl:
    engine: AsyncEngine
    sessions: async_sessionmaker[AsyncSession]

@pytest_asyncio.fixture
async def postgres_control(postgres_schema: PostgresSchema):
    async with postgres_schema.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield PostgresControl(
        engine=postgres_schema.engine,
        sessions=async_sessionmaker(
            postgres_schema.engine,
            expire_on_commit=False,
            class_=AsyncSession,
        ),
    )


async def _seed(control: PostgresControl) -> None:
    async with control.sessions() as session:
        session.add_all(
            [
                VpnCustomer(id=1, status="active"),
                VpnCustomer(id=2, status="active"),
                VpnCustomer(id=3, status="active"),
                WorkerNode(
                    id=2,
                    name="control-worker-a",
                    status="ready",
                    is_enabled=True,
                    vpn_enabled=True,
                    vpn_role="vpn_node",
                    vpn_runtime_status="ready",
                    ssh_host="192.0.2.2",
                ),
                WorkerNode(
                    id=3,
                    name="control-worker-b",
                    status="ready",
                    is_enabled=True,
                    vpn_enabled=True,
                    vpn_role="vpn_node",
                    vpn_runtime_status="ready",
                    ssh_host="192.0.2.3",
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
                    customer_id=2,
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    expires_at=EXPIRES_AT,
                    traffic_limit_gb=25,
                    max_devices=5,
                ),
                VpnSubscription(
                    id=5,
                    customer_id=3,
                    status="active",
                    starts_at=NOW - timedelta(days=1),
                    expires_at=EXPIRES_AT,
                    traffic_limit_gb=25,
                    max_devices=5,
                ),
                VpnEndpoint(
                    id=5,
                    worker_id=2,
                    inbound_id=11,
                    public_host="vpn-a.example.test",
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
                ),
                VpnEndpoint(
                    id=6,
                    worker_id=3,
                    inbound_id=12,
                    public_host="vpn-b.example.test",
                    port=443,
                    protocol="vless",
                    transport="raw",
                    security="reality",
                    server_name="cdn.example.test",
                    public_key=PUBLIC_KEY,
                    short_id="fedcba9876543210",
                    fingerprint="chrome",
                    flow="xtls-rprx-vision",
                    status="ready",
                    verified_at=NOW - timedelta(hours=1),
                ),
            ]
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
                    panel_sub_id="sub-7",
                    protocol="vless",
                    external_uuid="11111111-2222-4333-8444-555555555555",
                    status="pending_sync",
                    issued_at=NOW - timedelta(days=1),
                ),
                VpnAccessKey(
                    id=8,
                    subscription_id=4,
                    worker_id=2,
                    endpoint_id=5,
                    verified_client_email="veltrix-8-phone",
                    panel_sub_id="sub-8",
                    protocol="vless",
                    external_uuid="22222222-3333-4444-8555-666666666666",
                    status="pending_sync",
                    issued_at=NOW - timedelta(days=1),
                ),
                VpnAccessKey(
                    id=9,
                    subscription_id=5,
                    worker_id=3,
                    endpoint_id=6,
                    verified_client_email="veltrix-9-phone",
                    panel_sub_id="sub-9",
                    protocol="vless",
                    external_uuid="33333333-4444-4555-8666-777777777777",
                    status="pending_sync",
                    issued_at=NOW - timedelta(days=1),
                ),
            ]
        )
        await session.commit()


async def _stage(control: PostgresControl, key_id: int, *, now: datetime = NOW):
    async with control.sessions() as session:
        operation = await stage_vpn_control_operation(
            session,
            key_id,
            "provision",
            now=now,
        )
        operation_id = UUID(operation.id)
        await session.commit()
    return operation_id


async def _claim(control: PostgresControl, *, now: datetime = NOW):
    token = uuid4()
    async with control.sessions() as session:
        operation = await claim_next_vpn_control_operation(
            session,
            claim_token=token,
            now=now,
        )
        await session.commit()
    return operation, token


@pytest.mark.asyncio
async def test_two_claimers_cannot_claim_the_same_operation(postgres_control):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as first, postgres_control.sessions() as second:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        assert first_pid != second_pid
        first_claim = await claim_next_vpn_control_operation(
            first,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        )
        second_claim = await claim_next_vpn_control_operation(
            second,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        )
        assert first_claim is not None and UUID(first_claim.id) == operation_id
        assert second_claim is None
        await first.commit()
        await second.rollback()


@pytest.mark.asyncio
async def test_one_worker_reservation_blocks_a_second_operation(postgres_control):
    await _seed(postgres_control)
    await _stage(postgres_control, 7, now=NOW)
    await _stage(postgres_control, 8, now=NOW + timedelta(seconds=1))
    async with postgres_control.sessions() as first, postgres_control.sessions() as second:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        assert first_pid != second_pid
        claimed = await claim_next_vpn_control_operation(
            first,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=2),
        )
        assert claimed is not None and claimed.access_key_id == 7
        assert (
            await claim_next_vpn_control_operation(
                second,
                claim_token=uuid4(),
                now=NOW + timedelta(seconds=2),
            )
            is None
        )
        await first.commit()
        await second.rollback()
    async with postgres_control.sessions() as session:
        assert (
            await claim_next_vpn_control_operation(
                session,
                claim_token=uuid4(),
                now=NOW + timedelta(seconds=3),
            )
            is None
        )


@pytest.mark.asyncio
async def test_different_workers_are_claimed_before_first_transaction_commits(
    postgres_control,
):
    await _seed(postgres_control)
    await _stage(postgres_control, 7, now=NOW)
    await _stage(postgres_control, 9, now=NOW + timedelta(seconds=1))
    async with postgres_control.sessions() as first, postgres_control.sessions() as second:
        first_pid = await first.scalar(text("SELECT pg_backend_pid()"))
        second_pid = await second.scalar(text("SELECT pg_backend_pid()"))
        first_claim = await claim_next_vpn_control_operation(
            first,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=2),
        )
        second_claim = await claim_next_vpn_control_operation(
            second,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=2),
        )
        assert first_pid != second_pid
        assert first_claim is not None and first_claim.worker_id == 2
        assert second_claim is not None and second_claim.worker_id == 3
        await first.commit()
        await second.commit()


@pytest.mark.asyncio
async def test_rolled_back_claim_is_claimable_again(postgres_control):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as first:
        claimed = await claim_next_vpn_control_operation(
            first,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is not None
        await first.rollback()
    async with postgres_control.sessions() as second:
        reclaimed = await claim_next_vpn_control_operation(
            second,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=2),
        )
        assert reclaimed is not None and UUID(reclaimed.id) == operation_id


@pytest.mark.asyncio
async def test_newer_revoke_blocks_old_observed_provision_finalizer(postgres_control):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    claimed, token = await _claim(postgres_control, now=NOW + timedelta(seconds=1))
    assert claimed is not None and UUID(claimed.id) == operation_id
    async with postgres_control.sessions() as session:
        newer = await stage_vpn_control_operation(
            session,
            7,
            "revoke",
            now=NOW + timedelta(seconds=2),
        )
        await session.commit()
    async with postgres_control.sessions() as session:
        finalized = await finalize_vpn_control_operation(
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy", "action", "expected_status"),
    [("disabled", "suspend", "pending_suspend"), ("archived", "revoke", "pending_revoke")],
)
async def test_newer_archive_or_suspend_survives_old_provision_finalization(
    postgres_control,
    policy,
    action,
    expected_status,
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    claimed, token = await _claim(postgres_control, now=NOW + timedelta(seconds=1))
    assert claimed is not None
    async with postgres_control.sessions() as session:
        if policy == "disabled":
            subscription = await session.get(VpnSubscription, 3)
            assert subscription is not None
            subscription.status = "disabled"
        else:
            customer = await session.get(VpnCustomer, 1)
            assert customer is not None
            customer.status = "archived"
        await session.commit()
    async with postgres_control.sessions() as session:
        newer = await stage_vpn_control_operation(
            session,
            7,
            action,
            now=NOW + timedelta(seconds=2),
        )
        await session.commit()
    async with postgres_control.sessions() as session:
        old = await finalize_vpn_control_operation(
            session,
            operation_id,
            token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=3),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert old.state == "superseded"
        assert key.operation_generation == newer.generation == 2
        assert key.status == expected_status
        assert key.config_uri is None


@pytest.mark.asyncio
async def test_uncertain_reservation_survives_time_and_backend_change(postgres_control):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    claimed, token = await _claim(postgres_control, now=NOW + timedelta(seconds=1))
    assert claimed is not None
    async with postgres_control.sessions() as holder:
        holder_pid = await holder.scalar(text("SELECT pg_backend_pid()"))
        uncertain = await finalize_vpn_control_operation(
            holder,
            operation_id,
            token,
            receipt_state="uncertain",
            error_code="vpn_node_mutation_uncertain",
            now=NOW + timedelta(seconds=2),
        )
        assert uncertain.state == "uncertain"
        await holder.commit()
        await holder.scalar(text("SELECT 1"))
        await _stage(postgres_control, 8, now=NOW + timedelta(seconds=3))
        async with postgres_control.sessions() as later:
            later_pid = await later.scalar(text("SELECT pg_backend_pid()"))
            assert later_pid != holder_pid
            assert (
                await claim_next_vpn_control_operation(
                    later,
                    claim_token=uuid4(),
                    now=NOW + timedelta(days=365),
                )
                is None
            )
            assert await active_vpn_control_worker_ids(later) == {2}


@pytest.mark.asyncio
async def test_endpoint_disable_blocks_provision_but_not_confirmed_revoke(
    postgres_control,
):
    await _seed(postgres_control)
    provision_id = await _stage(postgres_control, 7)
    claimed, provision_token = await _claim(
        postgres_control,
        now=NOW + timedelta(seconds=1),
    )
    assert claimed is not None
    async with postgres_control.sessions() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
    async with postgres_control.sessions() as session:
        provision = await finalize_vpn_control_operation(
            session,
            provision_id,
            provision_token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert provision.state == "superseded"
        assert (key.status, key.config_uri) == ("pending_sync", None)
        await session.commit()
    async with postgres_control.sessions() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        key = await session.get(VpnAccessKey, 7)
        assert endpoint is not None and key is not None
        endpoint.status = "ready"
        key.config_uri = "vless://preserved-private-uri"
        await session.commit()
        revoke = await stage_vpn_control_operation(
            session,
            7,
            "revoke",
            now=NOW + timedelta(seconds=3),
        )
        revoke_id = UUID(revoke.id)
        await session.commit()
    revoke_claim, revoke_token = await _claim(
        postgres_control,
        now=NOW + timedelta(seconds=4),
    )
    assert revoke_claim is not None and UUID(revoke_claim.id) == revoke_id
    async with postgres_control.sessions() as session:
        endpoint = await session.get(VpnEndpoint, 5)
        assert endpoint is not None
        endpoint.status = "disabled"
        await session.commit()
    async with postgres_control.sessions() as session:
        finalized = await finalize_vpn_control_operation(
            session,
            revoke_id,
            revoke_token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=5),
        )
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        assert finalized.state == "succeeded"
        assert key.status == "revoked"
        assert key.config_uri == "vless://preserved-private-uri"


@pytest.mark.asyncio
async def test_migration_is_idempotent_with_queue_claim_and_history(postgres_control):
    await _seed(postgres_control)
    succeeded_id = await _stage(postgres_control, 7, now=NOW)
    succeeded, succeeded_token = await _claim(
        postgres_control,
        now=NOW + timedelta(seconds=1),
    )
    assert succeeded is not None
    async with postgres_control.sessions() as session:
        await finalize_vpn_control_operation(
            session,
            succeeded_id,
            succeeded_token,
            receipt_state="observed",
            error_code=None,
            now=NOW + timedelta(seconds=2),
        )
        await session.commit()
    await _stage(postgres_control, 8, now=NOW + timedelta(seconds=3))
    await _stage(postgres_control, 9, now=NOW + timedelta(seconds=4))
    claimed, _token = await _claim(postgres_control, now=NOW + timedelta(seconds=5))
    assert claimed is not None and claimed.worker_id == 2

    async with postgres_control.engine.connect() as connection:
        before = (
            await connection.execute(
                text(
                    "SELECT id, access_key_id, generation, action, request_snapshot, "
                    "request_digest, state, claim_token, claimed_at, finished_at, "
                    "error_code FROM vpn_control_operations ORDER BY id"
                )
            )
        ).mappings().all()
    for _ in range(2):
        async with postgres_control.engine.begin() as connection:
            for statement in VPN_ENDPOINT_MIGRATIONS:
                await connection.execute(text(statement))
    async with postgres_control.engine.connect() as connection:
        after = (
            await connection.execute(
                text(
                    "SELECT id, access_key_id, generation, action, request_snapshot, "
                    "request_digest, state, claim_token, claimed_at, finished_at, "
                    "error_code FROM vpn_control_operations ORDER BY id"
                )
            )
        ).mappings().all()
        states = set(
            await connection.scalars(
                select(VpnControlOperation.state).order_by(VpnControlOperation.state)
            )
        )
    assert before == after
    assert {"queued", "claimed", "succeeded"} <= states
