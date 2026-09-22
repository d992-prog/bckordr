from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models import VpnControlOperation, VpnSubscription, WorkerNode
from app.services.vpn_control_intents import (
    claim_next_vpn_control_operation,
    finalize_vpn_control_operation,
)
from app.services.vpn_control_dispatcher import dispatch_next_vpn_control_operation
from app.services.vpn_node_transport import (
    NodeControlReceipt,
    VpnNodeTransportError,
)
from test_vpn_endpoint_migrations import postgres_schema as postgres_schema
from test_vpn_control_intents_postgres import (
    NOW,
    _seed,
    _stage,
    postgres_control as postgres_control,
)


@pytest.mark.asyncio
async def test_two_dispatchers_use_independent_backends_and_execute_once(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7, now=NOW)
    second_operation_id = await _stage(
        postgres_control, 8, now=NOW + timedelta(milliseconds=1)
    )
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    claim_pids = []

    async def claim(db, **kwargs):
        claim_pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        return await claim_next_vpn_control_operation(db, **kwargs)

    async def transport(_snapshot, _request, **_kwargs):
        async with postgres_control.sessions() as observer:
            calls.append(await observer.scalar(text("SELECT pg_backend_pid()")))
            state = await observer.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            assert state == "claimed"
        entered.set()
        await release.wait()
        return NodeControlReceipt("observed", None)

    async with (
        postgres_control.engine.connect() as first_connection,
        postgres_control.engine.connect() as second_connection,
    ):
        first_sessions = async_sessionmaker(
            first_connection, expire_on_commit=False, class_=AsyncSession
        )
        second_sessions = async_sessionmaker(
            second_connection, expire_on_commit=False, class_=AsyncSession
        )
        kwargs = dict(
            known_hosts_path=tmp_path / "known_hosts",
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
            claim=claim,
            now=lambda: NOW + timedelta(seconds=1),
        )
        first = asyncio.create_task(
            dispatch_next_vpn_control_operation(first_sessions, **kwargs)
        )
        await entered.wait()
        second = asyncio.create_task(
            dispatch_next_vpn_control_operation(second_sessions, **kwargs)
        )
        await asyncio.sleep(0.05)
        release.set()
        assert sorted(await asyncio.gather(first, second)) == [False, True]
    assert len(calls) == 1
    assert len(set(claim_pids)) == 2
    async with postgres_control.sessions() as session:
        states = dict(
            (
                await session.execute(
                    select(VpnControlOperation.id, VpnControlOperation.state).where(
                        VpnControlOperation.id.in_(
                            (str(operation_id), str(second_operation_id))
                        )
                    )
                )
            ).all()
        )
        assert states == {
            str(operation_id): "succeeded",
            str(second_operation_id): "queued",
        }


@pytest.mark.asyncio
async def test_committed_claim_survives_session_loss_and_time(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    async def process_loss(*_args, **_kwargs):
        raise SystemExit

    with pytest.raises(SystemExit):
        await dispatch_next_vpn_control_operation(
            postgres_control.sessions,
            tmp_path / "known_hosts",
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=process_loss,
            now=lambda: NOW + timedelta(seconds=1),
        )
    async with (
        postgres_control.engine.connect() as first_connection,
        postgres_control.engine.connect() as later_connection,
    ):
        first_pid = await first_connection.scalar(text("SELECT pg_backend_pid()"))
        later_pid = await later_connection.scalar(text("SELECT pg_backend_pid()"))
        assert later_pid != first_pid
        assert (
            await first_connection.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == "claimed"
        )
    assert not await dispatch_next_vpn_control_operation(
        postgres_control.sessions,
        tmp_path / "known_hosts",
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=lambda *_args, **_kwargs: pytest.fail("reclaimed"),
        now=lambda: NOW + timedelta(days=365),
    )


@pytest.mark.asyncio
async def test_different_workers_progress_concurrently(postgres_control, tmp_path):
    await _seed(postgres_control)
    await _stage(postgres_control, 7, now=NOW)
    await _stage(postgres_control, 9, now=NOW + timedelta(seconds=1))
    async with postgres_control.sessions() as session:
        for worker_id in (2, 3):
            worker = await session.get(WorkerNode, worker_id)
            worker.ssh_username = "root"
            worker.ssh_password = "secret"
        await session.commit()

    entered = []
    both_entered = asyncio.Event()

    async def transport(snapshot, _request, **_kwargs):
        entered.append(snapshot.worker_id)
        if len(entered) == 2:
            both_entered.set()
        await asyncio.wait_for(both_entered.wait(), 2)
        return NodeControlReceipt("observed", None)

    kwargs = dict(
        known_hosts_path=tmp_path / "known_hosts",
        snapshot_loader=lambda worker, _path: SimpleNamespace(worker_id=worker.id),
        transport=transport,
        now=lambda: NOW + timedelta(seconds=2),
    )
    results = await asyncio.gather(
        dispatch_next_vpn_control_operation(postgres_control.sessions, **kwargs),
        dispatch_next_vpn_control_operation(postgres_control.sessions, **kwargs),
    )
    assert results == [True, True]
    assert set(entered) == {2, 3}


@pytest.mark.asyncio
async def test_uncertain_execution_cannot_be_reclaimed_by_time(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    first_id = await _stage(postgres_control, 7, now=NOW)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    async def ambiguous(_snapshot, _request, *, phase_observer, **_kwargs):
        phase_observer("stdin_write_attempted", None)
        raise VpnNodeTransportError("mutation")

    common = dict(
        known_hosts_path=tmp_path / "known_hosts",
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        now=lambda: NOW + timedelta(seconds=1),
    )
    assert await dispatch_next_vpn_control_operation(
        postgres_control.sessions,
        transport=ambiguous,
        **common,
    )
    async with postgres_control.sessions() as session:
        operation = await session.get(VpnControlOperation, str(first_id))
        assert operation.state == "uncertain"
    await _stage(postgres_control, 8, now=NOW + timedelta(seconds=2))

    assert not await dispatch_next_vpn_control_operation(
        postgres_control.sessions,
        transport=lambda *_args, **_kwargs: pytest.fail("uncertain work reclaimed"),
        known_hosts_path=tmp_path / "known_hosts",
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        now=lambda: NOW + timedelta(days=365),
    )


class SessionProxy:
    def __init__(self, session, *, commit=None):
        self.session = session
        self._commit = commit

    def __getattr__(self, name):
        return getattr(self.session, name)

    async def __aenter__(self):
        await self.session.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.session.__aexit__(*args)

    async def commit(self):
        if self._commit is None:
            return await self.session.commit()
        return await self._commit(self.session)


@pytest.mark.asyncio
async def test_cancellation_during_real_claim_commit_sends_nothing(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    commit_started = asyncio.Event()
    never = asyncio.Event()
    transport_calls = 0

    async def blocked_commit(_session):
        commit_started.set()
        await never.wait()

    def sessions():
        return SessionProxy(postgres_control.sessions(), commit=blocked_commit)

    async def transport(*_args, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1

    task = asyncio.create_task(
        dispatch_next_vpn_control_operation(
            sessions,
            tmp_path / "known_hosts",
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
            now=lambda: NOW + timedelta(seconds=1),
        )
    )
    await commit_started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport_calls == 0
    async with postgres_control.sessions() as session:
        assert (
            await session.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == "queued"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected_state"),
    [
        ("prewrite", "failed"),
        ("stdin_write_attempted", "uncertain"),
        ("receipt_validated", "succeeded"),
    ],
)
async def test_postcommit_cancellation_barriers_finalize_real_rows(
    postgres_control, tmp_path, phase, expected_state
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    async def cancelled(_snapshot, _request, *, phase_observer, **_kwargs):
        if phase in {"stdin_write_attempted", "receipt_validated"}:
            phase_observer("stdin_write_attempted", None)
        if phase == "receipt_validated":
            phase_observer("receipt_validated", NodeControlReceipt("observed", None))
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_vpn_control_operation(
            postgres_control.sessions,
            tmp_path / "known_hosts",
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=cancelled,
            now=lambda: NOW + timedelta(seconds=1),
        )
    async with postgres_control.sessions() as session:
        assert (
            await session.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == expected_state
        )


@pytest.mark.asyncio
async def test_no_claim_commits_real_superseded_row(postgres_control, tmp_path):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        subscription = await session.get(VpnSubscription, 3)
        subscription.traffic_limit_gb = 50
        await session.commit()

    assert not await dispatch_next_vpn_control_operation(
        postgres_control.sessions,
        tmp_path / "known_hosts",
        transport=lambda *_args, **_kwargs: pytest.fail("transport called"),
        now=lambda: NOW + timedelta(seconds=1),
    )
    async with postgres_control.sessions() as session:
        assert (
            await session.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == "superseded"
        )


@pytest.mark.asyncio
async def test_finalize_uses_distinct_backend_and_transaction(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    claim_pids = []
    finalize_pids = []

    async def claim(db, **kwargs):
        claim_pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        return await claim_next_vpn_control_operation(db, **kwargs)

    async def finalize(db, *args, **kwargs):
        finalize_pids.append(await db.scalar(text("SELECT pg_backend_pid()")))
        assert (
            await db.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == "claimed"
        )
        return await finalize_vpn_control_operation(db, *args, **kwargs)

    async with (
        postgres_control.engine.connect() as claim_connection,
        postgres_control.engine.connect() as finalize_connection,
    ):
        calls = 0

        def sessions():
            nonlocal calls
            calls += 1
            connection = claim_connection if calls == 1 else finalize_connection
            return AsyncSession(bind=connection, expire_on_commit=False)

        assert await dispatch_next_vpn_control_operation(
            sessions,
            tmp_path / "known_hosts",
            claim=claim,
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=lambda *_args, **_kwargs: asyncio.sleep(
                0, result=NodeControlReceipt("observed", None)
            ),
            now=lambda: NOW + timedelta(seconds=1),
        )
    assert len(claim_pids) == len(finalize_pids) == 1
    assert claim_pids[0] != finalize_pids[0]


@pytest.mark.asyncio
async def test_finalize_commit_failure_stays_reserved_without_resend(
    postgres_control, tmp_path
):
    await _seed(postgres_control)
    operation_id = await _stage(postgres_control, 7)
    async with postgres_control.sessions() as session:
        worker = await session.get(WorkerNode, 2)
        worker.ssh_username = "root"
        worker.ssh_password = "secret"
        await session.commit()

    calls = 0
    session_calls = 0

    async def fail_commit(_session):
        raise RuntimeError("synthetic finalize commit failure")

    def sessions():
        nonlocal session_calls
        session_calls += 1
        session = postgres_control.sessions()
        return (
            SessionProxy(session, commit=fail_commit) if session_calls == 2 else session
        )

    async def transport(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return NodeControlReceipt("observed", None)

    assert await dispatch_next_vpn_control_operation(
        sessions,
        tmp_path / "known_hosts",
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=transport,
        now=lambda: NOW + timedelta(seconds=1),
    )
    async with postgres_control.sessions() as session:
        assert (
            await session.scalar(
                select(VpnControlOperation.state).where(
                    VpnControlOperation.id == str(operation_id)
                )
            )
            == "claimed"
        )
    assert not await dispatch_next_vpn_control_operation(
        postgres_control.sessions,
        tmp_path / "known_hosts",
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=lambda *_args, **_kwargs: pytest.fail("operation resent"),
        now=lambda: NOW + timedelta(days=1),
    )
    assert calls == 1
