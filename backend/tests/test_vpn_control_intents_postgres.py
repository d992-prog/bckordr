from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import BackgroundTasks, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
)

from app.db.base import Base
from app.db.models import (
    AttackRun,
    DropDomain,
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnCustomerSession,
    VpnEndpoint,
    VpnFriendInvitation,
    VpnSubscription,
    WorkerNode,
    WorkerMaintenanceJob,
    WorkerTask,
)
from app.db.vpn_endpoint_migrations import VPN_ENDPOINT_MIGRATIONS
from app.services.vpn_control_intents import (
    VpnControlIntentError,
    active_vpn_control_worker_ids,
    claim_next_vpn_control_operation,
    finalize_vpn_control_operation,
    stage_vpn_control_operation,
)
from app.services.vpn_friend_invitations import disable_friend_invitation
from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
from app.services import vpn_friend_invitations, vpn_lifecycle
from app.services import attack_runtime
from app.services import worker_maintenance
from app.services.attack_runtime import load_attack_available_workers
from app.services.worker_decommission import (
    WorkerDecommissionConflictError,
    decommission_worker,
)
from app.api.routes.control import (
    _start_worker_maintenance_job,
    start_all_vpn_node_updates,
    update_worker,
)
from app.api.routes import control as control_routes
from app.schemas.control import WorkerNodeUpdateRequest
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


async def _configure_workers(control: PostgresControl) -> None:
    async with control.sessions() as session:
        workers = (await session.scalars(select(WorkerNode).order_by(WorkerNode.id))).all()
        for worker in workers:
            worker.ssh_username = "root"
            worker.ssh_password = f"synthetic-{worker.id}"
            worker.vpn_role = "vpn_node"
            worker.vpn_enabled = True
        await session.commit()


@pytest.mark.asyncio
async def test_disable_and_lifecycle_race_has_one_authoritative_revoke_without_deadlock(
    postgres_control: PostgresControl,
    monkeypatch: pytest.MonkeyPatch,
):
    await _seed(postgres_control)
    async with postgres_control.sessions() as session:
        customer = await session.get(VpnCustomer, 1)
        subscription = await session.get(VpnSubscription, 3)
        key = await session.get(VpnAccessKey, 7)
        assert customer is not None and subscription is not None and key is not None
        customer.telegram_user_id = "700001"
        await session.flush()
        operation = await stage_vpn_control_operation(
            session,
            key.id,
            "provision",
            now=NOW - timedelta(minutes=2),
        )
        operation.state = "succeeded"
        operation.finished_at = NOW - timedelta(minutes=1)
        key.status = "active"
        key.config_uri = "vless://stable-invited-key"
        subscription.status = "trial"
        subscription.expires_at = NOW - timedelta(seconds=1)
        session.add_all(
            [
                VpnFriendInvitation(
                    slot=1,
                    token_digest="a" * 64,
                    created_at=NOW - timedelta(days=1),
                    redeem_expires_at=NOW + timedelta(days=6),
                    redeemed_at=NOW - timedelta(days=1),
                    telegram_user_id="700001",
                    access_key_id=key.id,
                ),
                VpnCustomerSession(
                    token_hash="b" * 64,
                    customer_id=customer.id,
                    telegram_user_id="700001",
                    created_at=NOW - timedelta(hours=1),
                    expires_at=NOW + timedelta(days=1),
                ),
            ]
        )
        await session.commit()

    barrier = asyncio.Barrier(2)
    staging_paths: set[str] = set()
    real_stage = stage_vpn_control_operation

    async def rendezvous(path: str, *args, **kwargs):
        if args[1] != 7:
            return await real_stage(*args, **kwargs)
        staging_paths.add(path)
        try:
            await asyncio.wait_for(barrier.wait(), timeout=5)
        except TimeoutError as error:
            raise AssertionError(
                f"only these staging paths reached the race barrier: {staging_paths}"
            ) from error
        return await real_stage(*args, **kwargs)

    async def disable_stage(*args, **kwargs):
        return await rendezvous("disable", *args, **kwargs)

    async def lifecycle_stage(*args, **kwargs):
        return await rendezvous("lifecycle", *args, **kwargs)

    monkeypatch.setattr(
        vpn_friend_invitations,
        "stage_vpn_control_operation",
        disable_stage,
    )
    monkeypatch.setattr(
        vpn_lifecycle,
        "stage_vpn_control_operation",
        lifecycle_stage,
    )

    async def disable() -> None:
        async with postgres_control.sessions() as session:
            await disable_friend_invitation(session, 1, NOW)
            await session.commit()

    async def lifecycle() -> None:
        async with postgres_control.sessions() as session:
            await run_vpn_lifecycle_maintenance(session, now=NOW)

    await asyncio.wait_for(asyncio.gather(disable(), lifecycle()), timeout=10)

    async with postgres_control.sessions() as session:
        invitation = await session.get(VpnFriendInvitation, 1)
        portal_session = await session.get(VpnCustomerSession, "b" * 64)
        key = await session.get(VpnAccessKey, 7)
        subscription = await session.get(VpnSubscription, 3)
        customer = await session.get(VpnCustomer, 1)
        operations = (
            await session.scalars(
                select(VpnControlOperation)
                .where(VpnControlOperation.access_key_id == 7)
                .order_by(VpnControlOperation.generation)
            )
        ).all()

    assert staging_paths == {"disable", "lifecycle"}
    assert invitation is not None and invitation.access_key_id == 7
    assert key is not None and key.subscription_id == 3
    assert subscription is not None and subscription.customer_id == 1
    assert customer is not None
    assert invitation.telegram_user_id == customer.telegram_user_id == "700001"
    assert invitation.revoked_at is not None
    assert portal_session is not None and portal_session.revoked_at is not None
    assert portal_session.customer_id == customer.id
    assert portal_session.telegram_user_id == customer.telegram_user_id
    assert key.revoke_requested_at is not None
    assert key.status == "pending_revoke"
    current = [item for item in operations if item.generation == key.operation_generation]
    assert len(current) == 1
    assert (current[0].action, current[0].state) == ("revoke", "queued")
    assert sum(item.action == "revoke" for item in operations) == 1


async def _assert_blocked(task: asyncio.Task) -> None:
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(asyncio.shield(task), timeout=0.05)


@pytest.mark.asyncio
async def test_stage_rejects_exhausted_integer_generation_without_aborting_transaction(
    postgres_control,
):
    await _seed(postgres_control)
    maximum = 2**31 - 1
    async with postgres_control.sessions() as session:
        key = await session.get(VpnAccessKey, 7)
        assert key is not None
        key.operation_generation = maximum
        await session.commit()

        with pytest.raises(VpnControlIntentError) as caught:
            await stage_vpn_control_operation(session, 7, "provision", now=NOW)

        assert caught.value.code == "vpn_control_generation_invalid"
        assert await session.scalar(text("SELECT 1")) == 1
        assert await session.scalar(
            select(VpnAccessKey.operation_generation).where(VpnAccessKey.id == 7)
        ) == maximum
        assert not (await session.scalars(select(VpnControlOperation))).all()


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
async def test_claim_savepoint_supersedes_stale_first_and_claims_valid_second(
    postgres_control,
):
    await _seed(postgres_control)
    stale_id = await _stage(postgres_control, 7, now=NOW)
    valid_id = await _stage(postgres_control, 8, now=NOW + timedelta(seconds=1))
    async with postgres_control.sessions() as session:
        subscription = await session.get(VpnSubscription, 3)
        assert subscription is not None
        subscription.status = "disabled"
        await session.commit()

    token = uuid4()
    async with postgres_control.sessions() as session:
        claimed = await claim_next_vpn_control_operation(
            session,
            claim_token=token,
            now=NOW + timedelta(seconds=2),
        )
        first = await session.get(VpnControlOperation, str(stale_id))
        second = await session.get(VpnControlOperation, str(valid_id))
        assert claimed is not None and UUID(claimed.id) == valid_id
        assert first is not None and first.state == "superseded"
        assert second is not None
        assert (second.state, second.claim_token) == ("claimed", str(token))


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
async def test_claim_supersedes_staged_snapshot_after_subscription_policy_drift(
    postgres_control,
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        subscription = await session.get(VpnSubscription, 3)
        assert subscription is not None
        subscription.expires_at = EXPIRES_AT + timedelta(days=1)
        subscription.traffic_limit_gb = 50
        await session.commit()

    async with postgres_control.sessions() as session:
        assert (
            await claim_next_vpn_control_operation(
                session,
                claim_token=uuid4(),
                now=NOW + timedelta(seconds=1),
            )
            is None
        )
        operation = await session.get(VpnControlOperation, str(operation_id))
        key = await session.get(VpnAccessKey, 7)
        assert operation is not None and key is not None
        assert operation.state == "superseded"
        assert key.status == "pending_sync"
        assert key.config_uri is None


@pytest.mark.asyncio
async def test_observed_provision_cannot_apply_changed_expiry_or_traffic_snapshot(
    postgres_control,
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    claimed, token = await _claim(postgres_control, now=NOW + timedelta(seconds=1))
    assert claimed is not None
    changed_expiry = EXPIRES_AT + timedelta(days=1)
    async with postgres_control.sessions() as session:
        subscription = await session.get(VpnSubscription, 3)
        assert subscription is not None
        subscription.expires_at = changed_expiry
        subscription.traffic_limit_gb = 50
        await session.commit()

    async with postgres_control.sessions() as session:
        finalized = await finalize_vpn_control_operation(
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
        assert key.status == "pending_sync"
        assert key.expires_at is None
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


@pytest.mark.asyncio
async def test_attack_rechecks_reservation_after_waiting_for_staging_worker_lock(
    postgres_control,
    monkeypatch,
):
    await _seed(postgres_control)
    first_read = asyncio.Event()
    release_first_read = asyncio.Event()
    real_reservations = attack_runtime.active_vpn_mutation_worker_ids

    async with postgres_control.sessions() as attacker, postgres_control.sessions() as stager:
        attacker_pid = await attacker.scalar(text("SELECT pg_backend_pid()"))
        stager_pid = await stager.scalar(text("SELECT pg_backend_pid()"))
        assert attacker_pid != stager_pid
        first_call = True

        async def controlled_reservations(session):
            nonlocal first_call
            result = await real_reservations(session)
            if session is attacker and first_call:
                first_call = False
                first_read.set()
                await release_first_read.wait()
            return result

        monkeypatch.setattr(
            attack_runtime,
            "active_vpn_mutation_worker_ids",
            controlled_reservations,
        )
        attack_task = asyncio.create_task(
            load_attack_available_workers(attacker, worker_ids=[2])
        )
        await asyncio.wait_for(first_read.wait(), timeout=1)
        operation = await stage_vpn_control_operation(stager, 7, "provision", now=NOW)
        assert operation.state == "queued"
        release_first_read.set()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(attack_task), timeout=0.05)

        await stager.commit()
        assert await asyncio.wait_for(attack_task, timeout=1) == []
        await attacker.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "bulk", "update", "decommission"])
async def test_committed_stage_blocks_real_worker_mutation_after_its_worker_lock(
    postgres_control,
    monkeypatch,
    path,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    background = BackgroundTasks()

    async def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("losing update must not synchronize runtime state")

    monkeypatch.setattr(control_routes, "sync_worker_runtime_allowlist", forbidden_sync)
    async with postgres_control.sessions() as stager, postgres_control.sessions() as competitor:
        operation = await stage_vpn_control_operation(stager, 7, "provision", now=NOW)
        assert operation.state == "queued"

        if path == "single":
            competing = asyncio.create_task(
                _start_worker_maintenance_job(
                    worker_id=2,
                    action="vpn_update",
                    background_tasks=background,
                    db=competitor,
                    admin=SimpleNamespace(id=None),
                )
            )
        elif path == "bulk":
            competing = asyncio.create_task(
                start_all_vpn_node_updates(
                    background_tasks=background,
                    db=competitor,
                    admin=SimpleNamespace(id=None),
                )
            )
        elif path == "update":
            competing = asyncio.create_task(
                update_worker(
                    2,
                    WorkerNodeUpdateRequest(ssh_password=None),
                    competitor,
                    SimpleNamespace(id=None),
                )
            )
        else:
            competing = asyncio.create_task(decommission_worker(competitor, 2))

        await _assert_blocked(competing)
        await stager.commit()

        if path in {"single", "update"}:
            with pytest.raises(HTTPException) as caught:
                await asyncio.wait_for(competing, timeout=1)
            assert caught.value.status_code == 409
            assert not background.tasks
        elif path == "decommission":
            with pytest.raises(WorkerDecommissionConflictError, match="VPN control operation"):
                await asyncio.wait_for(competing, timeout=1)
        else:
            response = await asyncio.wait_for(competing, timeout=30)
            assert response.skipped_worker_ids == [2]
            assert [job.worker_id for job in response.jobs] == [3]
            assert len(background.tasks) == 1
        await competitor.rollback()

    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        assert worker is not None
        assert worker.archived_at is None
        assert worker.ssh_password == "synthetic-2"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["single", "bulk"])
async def test_real_maintenance_creation_commits_before_waiting_stage_rechecks(
    postgres_control,
    monkeypatch,
    path,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    worker_locked = asyncio.Event()
    release_worker = asyncio.Event()
    real_control_reservations = control_routes.active_vpn_control_worker_ids
    background = BackgroundTasks()

    async with postgres_control.sessions() as maintenance, postgres_control.sessions() as stager:
        first_call = True

        async def pause_after_worker_lock(session):
            nonlocal first_call
            if session is maintenance and first_call:
                first_call = False
                worker_locked.set()
                await release_worker.wait()
            return await real_control_reservations(session)

        monkeypatch.setattr(
            control_routes,
            "active_vpn_control_worker_ids",
            pause_after_worker_lock,
        )
        if path == "single":
            maintenance_task = asyncio.create_task(
                _start_worker_maintenance_job(
                    worker_id=2,
                    action="vpn_update",
                    background_tasks=background,
                    db=maintenance,
                    admin=SimpleNamespace(id=None),
                )
            )
        else:
            maintenance_task = asyncio.create_task(
                start_all_vpn_node_updates(
                    background_tasks=background,
                    db=maintenance,
                    admin=SimpleNamespace(id=None),
                )
            )
        await asyncio.wait_for(worker_locked.wait(), timeout=30)
        stage_task = asyncio.create_task(
            stage_vpn_control_operation(stager, 7, "provision", now=NOW)
        )
        await _assert_blocked(stage_task)
        release_worker.set()
        await asyncio.wait_for(maintenance_task, timeout=30)
        with pytest.raises(VpnControlIntentError) as caught:
            await asyncio.wait_for(stage_task, timeout=30)
        assert caught.value.code == "vpn_control_worker_busy"
        await stager.rollback()


@pytest.mark.asyncio
async def test_running_maintenance_is_published_before_stage_and_uses_real_runner(
    postgres_control,
    monkeypatch,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    async with postgres_control.sessions() as session:
        job = WorkerMaintenanceJob(worker_id=2, action="vpn_restart", status="queued")
        session.add(job)
        await session.commit()
        job_id = job.id

    before_worker_lock = asyncio.Event()
    release_runner = asyncio.Event()
    real_lock_worker = worker_maintenance.lock_vpn_worker

    async def paused_lock(session, worker_id):
        before_worker_lock.set()
        await release_runner.wait()
        return await real_lock_worker(session, worker_id)

    async def no_ssh(_worker, _commands):
        return "synthetic-success"

    monkeypatch.setattr(worker_maintenance, "AsyncSessionLocal", postgres_control.sessions)
    monkeypatch.setattr(worker_maintenance, "lock_vpn_worker", paused_lock)
    monkeypatch.setattr(worker_maintenance, "execute_worker_ssh_commands", no_ssh)
    runner = asyncio.create_task(worker_maintenance.run_worker_maintenance_job(job_id))
    await asyncio.wait_for(before_worker_lock.wait(), timeout=30)

    async with postgres_control.sessions() as stager:
        with pytest.raises(VpnControlIntentError) as caught:
            await stage_vpn_control_operation(stager, 7, "provision", now=NOW)
        assert caught.value.code == "vpn_control_worker_busy"
        await stager.rollback()

    release_runner.set()
    await asyncio.wait_for(runner, timeout=30)
    async with postgres_control.sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None and job.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["attack", "maintenance"])
async def test_committed_cross_system_work_blocks_waiting_stage_after_worker_lock(
    postgres_control,
    conflict,
):
    await _seed(postgres_control)
    async with postgres_control.sessions() as holder, postgres_control.sessions() as stager:
        worker = await holder.scalar(
            select(WorkerNode).where(WorkerNode.id == 2).with_for_update()
        )
        assert worker is not None
        if conflict == "attack":
            domain = DropDomain(fqdn="race.example", zone="example", drop_date=NOW.date())
            holder.add(domain)
            await holder.flush()
            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=NOW,
                planned_end_at=NOW + timedelta(minutes=1),
            )
            holder.add(run)
            await holder.flush()
            holder.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=2,
                    status="running",
                )
            )
        else:
            holder.add(
                WorkerMaintenanceJob(worker_id=2, action="vpn_update", status="queued")
            )
        await holder.flush()

        stage_task = asyncio.create_task(
            stage_vpn_control_operation(stager, 7, "provision", now=NOW)
        )
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(asyncio.shield(stage_task), timeout=0.05)
        await holder.commit()
        with pytest.raises(VpnControlIntentError) as caught:
            await asyncio.wait_for(stage_task, timeout=30)
        assert caught.value.code == "vpn_control_worker_busy"
        await stager.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["attack", "maintenance"])
async def test_claim_skips_worker_after_cross_system_work_commits(
    postgres_control,
    conflict,
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        if conflict == "attack":
            domain = DropDomain(fqdn="claim-race.example", zone="example", drop_date=NOW.date())
            session.add(domain)
            await session.flush()
            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=NOW,
                planned_end_at=NOW + timedelta(minutes=1),
            )
            session.add(run)
            await session.flush()
            session.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=2,
                    status="running",
                )
            )
        else:
            session.add(
                WorkerMaintenanceJob(worker_id=2, action="vpn_restart", status="running")
            )
        await session.commit()

    async with postgres_control.sessions() as claimer:
        assert await claim_next_vpn_control_operation(
            claimer,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        ) is None
        operation = await claimer.get(VpnControlOperation, str(operation_id))
        assert operation is not None and operation.state == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict", ["attack", "maintenance"])
async def test_claim_does_not_cross_uncommitted_real_worker_mutation(
    postgres_control,
    monkeypatch,
    conflict,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    release_maintenance = asyncio.Event()
    maintenance_locked = asyncio.Event()
    background = BackgroundTasks()
    operation_id = "bbbbbbbb-0000-4000-8000-000000000099"

    async with postgres_control.sessions() as holder, postgres_control.sessions() as claimer:
        if conflict == "attack":
            workers = await load_attack_available_workers(holder, worker_ids=[2])
            assert [worker.id for worker in workers] == [2]
            domain = DropDomain(fqdn="claim-held.example", zone="example", drop_date=NOW.date())
            holder.add(domain)
            await holder.flush()
            run = AttackRun(
                domain_id=domain.id,
                status="running",
                planned_start_at=NOW,
                planned_end_at=NOW + timedelta(minutes=1),
            )
            holder.add(run)
            await holder.flush()
            holder.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=2,
                    status="running",
                )
            )
            await holder.flush()
            maintenance_task = None
        else:
            async def pause_before_commit(*_args, **_kwargs):
                maintenance_locked.set()
                await release_maintenance.wait()

            monkeypatch.setattr(control_routes, "add_audit_log", pause_before_commit)
            maintenance_task = asyncio.create_task(
                _start_worker_maintenance_job(
                    worker_id=2,
                    action="vpn_restart",
                    background_tasks=background,
                    db=holder,
                    admin=SimpleNamespace(id=None),
                )
            )
            await asyncio.wait_for(maintenance_locked.wait(), timeout=1)

        async with postgres_control.sessions() as inserter:
            inserter.add(
                VpnControlOperation(
                    id=operation_id,
                    access_key_id=7,
                    worker_id=2,
                    endpoint_id=5,
                    generation=1,
                    action="provision",
                    request_snapshot={},
                    request_digest="8" * 64,
                    state="queued",
                )
            )
            await inserter.commit()

        claimed = await claim_next_vpn_control_operation(
            claimer,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=1),
        )
        assert claimed is None
        await claimer.rollback()

        if conflict == "attack":
            await holder.commit()
        else:
            release_maintenance.set()
            await asyncio.wait_for(maintenance_task, timeout=1)

    async with postgres_control.sessions() as session:
        assert await claim_next_vpn_control_operation(
            session,
            claim_token=uuid4(),
            now=NOW + timedelta(seconds=2),
        ) is None
        operation = await session.get(VpnControlOperation, operation_id)
        assert operation is not None and operation.state == "queued"


@pytest.mark.asyncio
async def test_real_runner_rechecks_new_control_reservation_at_last_pre_ssh_gate(
    postgres_control,
    monkeypatch,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    async with postgres_control.sessions() as session:
        job = WorkerMaintenanceJob(worker_id=2, action="vpn_restart", status="queued")
        session.add(job)
        await session.commit()
        job_id = job.id

    pre_ssh_gate = asyncio.Event()
    release_gate = asyncio.Event()
    reservation_calls = 0
    ssh_calls = 0
    real_reservations = worker_maintenance.active_vpn_control_worker_ids

    async def pause_second_reservation_query(session):
        nonlocal reservation_calls
        reservation_calls += 1
        if reservation_calls == 2:
            pre_ssh_gate.set()
            await release_gate.wait()
        return await real_reservations(session)

    async def forbidden_ssh(_worker, _commands):
        nonlocal ssh_calls
        ssh_calls += 1
        return "unexpected"

    monkeypatch.setattr(worker_maintenance, "AsyncSessionLocal", postgres_control.sessions)
    monkeypatch.setattr(
        worker_maintenance,
        "active_vpn_control_worker_ids",
        pause_second_reservation_query,
    )
    monkeypatch.setattr(worker_maintenance, "execute_worker_ssh_commands", forbidden_ssh)
    runner = asyncio.create_task(worker_maintenance.run_worker_maintenance_job(job_id))
    await asyncio.wait_for(pre_ssh_gate.wait(), timeout=30)

    async with postgres_control.sessions() as inserter:
        inserter.add(
            VpnControlOperation(
                id="aaaaaaaa-0000-4000-8000-000000000099",
                access_key_id=7,
                worker_id=2,
                endpoint_id=5,
                generation=1,
                action="provision",
                request_snapshot={},
                request_digest="9" * 64,
                state="queued",
            )
        )
        await inserter.commit()
    release_gate.set()
    await asyncio.wait_for(runner, timeout=30)

    async with postgres_control.sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None and job.status == "failed"
        assert job.error_message == "Worker has an active VPN control operation"
    assert reservation_calls == 2
    assert ssh_calls == 0


@pytest.mark.asyncio
async def test_control_reservation_blocks_maintenance_update_and_decommission(
    postgres_control,
    monkeypatch,
):
    await _seed(postgres_control)
    await _stage(postgres_control, 7)
    background = BackgroundTasks()
    admin = SimpleNamespace(id=1)

    async def forbidden_sync(*_args, **_kwargs):
        raise AssertionError("losing worker update must not sync runtime state")

    monkeypatch.setattr("app.api.routes.control.sync_worker_runtime_allowlist", forbidden_sync)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        assert worker is not None
        worker.ssh_username = "root"
        worker.ssh_password = "synthetic-secret"
        await session.commit()

    async with postgres_control.sessions() as session:
        with pytest.raises(HTTPException) as maintenance_error:
            await _start_worker_maintenance_job(
                worker_id=2,
                action="vpn_update",
                background_tasks=background,
                db=session,
                admin=admin,
            )
        assert maintenance_error.value.status_code == 409
        assert not background.tasks

    async with postgres_control.sessions() as session:
        with pytest.raises(HTTPException) as update_error:
            await update_worker(
                2,
                WorkerNodeUpdateRequest(ssh_password=None),
                session,
                admin,
            )
        assert update_error.value.status_code == 409
        await session.rollback()
        worker = await session.get(WorkerNode, 2)
        assert worker is not None and worker.ssh_password == "synthetic-secret"

    async with postgres_control.sessions() as session:
        with pytest.raises(WorkerDecommissionConflictError, match="VPN control operation"):
            await decommission_worker(session, 2)
        await session.rollback()
        worker = await session.get(WorkerNode, 2)
        assert worker is not None
        assert worker.archived_at is None
        assert worker.ssh_password == "synthetic-secret"


@pytest.mark.asyncio
async def test_bulk_vpn_maintenance_skips_reserved_worker_and_schedules_no_callback_for_it(
    postgres_control,
):
    await _seed(postgres_control)
    await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        workers = (await session.scalars(select(WorkerNode).order_by(WorkerNode.id))).all()
        for worker in workers:
            worker.ssh_username = "root"
            worker.ssh_password = f"synthetic-{worker.id}"
            worker.vpn_role = "vpn_node"
            worker.vpn_enabled = True
        await session.commit()

    background = BackgroundTasks()
    async with postgres_control.sessions() as session:
        response = await start_all_vpn_node_updates(
            background_tasks=background,
            db=session,
            admin=SimpleNamespace(id=None),
        )

    assert response.skipped_worker_ids == [2]
    assert [job.worker_id for job in response.jobs] == [3]
    assert len(background.tasks) == 1


@pytest.mark.asyncio
async def test_attack_batch_lock_order_is_compatible_with_ascending_bulk_maintenance(
    postgres_control,
):
    await _seed(postgres_control)
    await _configure_workers(postgres_control)
    async with postgres_control.sessions() as session:
        first = await session.get(WorkerNode, 2)
        second = await session.get(WorkerNode, 3)
        assert first is not None and second is not None
        first.target_rps = 1
        second.target_rps = 100
        domain = DropDomain(fqdn="bulk-race.example", zone="example", drop_date=NOW.date())
        session.add(domain)
        await session.flush()
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=NOW,
            planned_end_at=NOW + timedelta(minutes=1),
        )
        session.add(run)
        await session.commit()
        domain_id = domain.id
        run_id = run.id

    gate = asyncio.Event()
    both_ready = asyncio.Event()
    ready_count = 0
    lock_statements: list[str] = []

    class BarrierSession:
        def __init__(self, session):
            self.session = session

        def __getattr__(self, name):
            return getattr(self.session, name)

        async def execute(self, statement):
            nonlocal ready_count
            if statement._for_update_arg is not None and "worker_nodes" in str(statement):
                lock_statements.append(str(statement))
                ready_count += 1
                if ready_count == 2:
                    both_ready.set()
                await gate.wait()
            return await self.session.execute(statement)

    async with postgres_control.sessions() as maintenance, postgres_control.sessions() as attacker:
        maintenance_session = BarrierSession(maintenance)
        attack_session = BarrierSession(attacker)
        background = BackgroundTasks()
        bulk_task = asyncio.create_task(
            start_all_vpn_node_updates(
                background_tasks=background,
                db=maintenance_session,
                admin=SimpleNamespace(id=None),
            )
        )
        attack_task = asyncio.create_task(
            load_attack_available_workers(attack_session, worker_ids=[3, 2])
        )
        await asyncio.wait_for(both_ready.wait(), timeout=30)
        gate.set()
        done, _pending = await asyncio.wait(
            {bulk_task, attack_task},
            timeout=30,
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert done, "bulk maintenance and attack deadlocked"

        if attack_task in done and not bulk_task.done():
            attack_workers = attack_task.result()
            for worker in attack_workers:
                attacker.add(
                    WorkerTask(
                        attack_run_id=run_id,
                        domain_id=domain_id,
                        worker_id=worker.id,
                        status="running",
                    )
                )
            await attacker.commit()
            bulk_response = await asyncio.wait_for(bulk_task, timeout=30)
        else:
            bulk_response = await asyncio.wait_for(bulk_task, timeout=30)
            attack_workers = await asyncio.wait_for(attack_task, timeout=30)
        await attacker.rollback()

    assert len(lock_statements) == 2
    assert all("ORDER BY worker_nodes.id ASC" in statement for statement in lock_statements)
    if attack_workers:
        assert [worker.id for worker in attack_workers] == [3, 2]
        assert bulk_response.started_count == 0
        assert bulk_response.skipped_worker_ids == [2, 3]
        assert not background.tasks
    else:
        assert bulk_response.started_count == 2
        assert [job.worker_id for job in bulk_response.jobs] == [2, 3]
        assert len(background.tasks) == 2
