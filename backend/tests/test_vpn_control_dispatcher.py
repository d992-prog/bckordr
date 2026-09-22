from __future__ import annotations

import asyncio
import os
import stat
from types import SimpleNamespace
from uuid import UUID

import asyncssh
import pytest

from app.services import vpn_node_transport
from app.services.vpn_control_dispatcher import (
    _FinalizeGate,
    _finalize_once,
    dispatch_next_vpn_control_operation,
)
from app.services.vpn_node_transport import (
    NodeControlReceipt,
    VpnNodeTransportError,
    load_transport_snapshot,
)


OPERATION_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
TOKEN = UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")


class Session:
    def __init__(self, events, operation=None, *, fail_commit=False):
        self.events = events
        self.operation = operation
        self.fail_commit = fail_commit
        self.closed = False

    async def __aenter__(self):
        self.events.append("session-open")
        return self

    async def __aexit__(self, *_args):
        self.closed = True
        self.events.append("session-close")

    async def commit(self):
        self.events.append("commit")
        if self.fail_commit:
            raise RuntimeError("commit failed")

    async def rollback(self):
        self.events.append("rollback")

    async def get(self, _model, _identity):
        return SimpleNamespace(
            ssh_host="host",
            ip_address=None,
            ssh_port=22,
            ssh_username="root",
            ssh_password="secret",
            ssh_key_path=None,
        )


def operation():
    return SimpleNamespace(
        id=str(OPERATION_ID),
        worker_id=7,
        request_snapshot={"version": 1},
        request_digest="a" * 64,
    )


@pytest.mark.asyncio
async def test_claim_is_committed_and_session_closed_before_transport(tmp_path):
    events = []
    claim_session = Session(events, operation())
    sessions = [claim_session, Session(events)]

    async def claim(db, *, claim_token):
        events.append(("claim", claim_token))
        return db.operation

    async def finalize(db, operation_id, claim_token, **receipt):
        events.append(("finalize", db.closed, operation_id, claim_token, receipt))

    async def transport(snapshot, request, **kwargs):
        events.append(("transport", claim_session.closed, request, snapshot.password))
        return NodeControlReceipt("observed", None)

    result = await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        tmp_path / "known_hosts",
        claim_token_factory=lambda: TOKEN,
        claim=claim,
        finalize=finalize,
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=transport,
    )
    assert result is True
    assert events.index("commit") < next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and event[0] == "transport"
    )
    transport_event = next(
        event
        for event in events
        if isinstance(event, tuple) and event[0] == "transport"
    )
    assert transport_event[1:] == (True, b'{"version":1}\n', "secret")
    finalized = next(
        event for event in events if isinstance(event, tuple) and event[0] == "finalize"
    )
    assert finalized[1] is False
    assert finalized[2:4] == (OPERATION_ID, TOKEN)


@pytest.mark.asyncio
async def test_no_claim_still_commits_and_never_connects(tmp_path):
    events = []
    session = Session(events)

    async def claim(*_args, **_kwargs):
        return None

    async def transport(*_args, **_kwargs):
        pytest.fail("transport called")

    assert not await dispatch_next_vpn_control_operation(
        lambda: session,
        tmp_path / "known_hosts",
        claim=claim,
        transport=transport,
    )
    assert events == ["session-open", "commit", "session-close"]


@pytest.mark.asyncio
async def test_claim_commit_failure_never_connects_or_finalizes(tmp_path):
    events = []
    session = Session(events, operation(), fail_commit=True)

    async def transport(*_args, **_kwargs):
        pytest.fail("transport called")

    async def finalize(*_args, **_kwargs):
        pytest.fail("finalize called")

    with pytest.raises(RuntimeError, match="commit failed"):
        await dispatch_next_vpn_control_operation(
            lambda: session,
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_private_key_material_is_snapshotted_before_commit(monkeypatch, tmp_path):
    events = []
    host_key = asyncssh.generate_private_key("ssh-ed25519")
    original_key = asyncssh.generate_private_key("ssh-ed25519")
    replacement_key = asyncssh.generate_private_key("ssh-ed25519")
    known_hosts_path = tmp_path / "known_hosts"
    key_path = tmp_path / "id_ed25519"
    replacement_path = tmp_path / "replacement_ed25519"
    known_hosts_path.write_bytes(b"host " + host_key.export_public_key("openssh"))
    key_path.write_bytes(original_key.export_private_key("openssh"))
    replacement_path.write_bytes(replacement_key.export_private_key("openssh"))
    real_lstat = os.lstat
    real_fstat = os.fstat

    def private_info(info):
        return SimpleNamespace(
            st_mode=stat.S_IFREG | 0o600,
            st_uid=0,
            st_dev=1,
            st_ino=info.st_size,
            st_size=info.st_size,
            st_mtime_ns=1,
            st_ctime_ns=1,
            st_file_attributes=0,
        )

    monkeypatch.setattr(vpn_node_transport, "_effective_uid", lambda: 0)
    monkeypatch.setattr(vpn_node_transport, "_validate_ancestors", lambda *_args: None)
    monkeypatch.setattr(os, "lstat", lambda path: private_info(real_lstat(path)))
    monkeypatch.setattr(os, "fstat", lambda fd: private_info(real_fstat(fd)))

    worker = SimpleNamespace(
        ssh_host="host",
        ip_address=None,
        ssh_port=22,
        ssh_username="root",
        ssh_password=None,
        ssh_key_path=str(key_path),
    )
    assert load_transport_snapshot(worker, known_hosts_path).client_key is not None

    class ReplacingCommitSession(Session):
        async def get(self, _model, _identity):
            return worker

        async def commit(self):
            await super().commit()
            os.replace(replacement_path, key_path)

    sessions = [ReplacingCommitSession(events, operation()), Session(events)]

    async def transport(snapshot, _request, **_kwargs):
        events.append("transport")
        assert snapshot.client_key.export_public_key("openssh") == (
            original_key.export_public_key("openssh")
        )
        assert key_path.read_bytes() == replacement_key.export_private_key("openssh")
        return NodeControlReceipt("observed", None)

    assert await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        known_hosts_path,
        claim=lambda *_args, **_kwargs: operation(),
        finalize=lambda *_args, **_kwargs: None,
        snapshot_loader=load_transport_snapshot,
        transport=transport,
    )
    assert events.index("commit") < events.index("transport")


@pytest.mark.asyncio
async def test_finalize_failure_rolls_back_and_never_resends(tmp_path):
    events = []
    sessions = [Session(events, operation()), Session(events)]
    transport_calls = 0

    async def transport(*_args, **_kwargs):
        nonlocal transport_calls
        transport_calls += 1
        return NodeControlReceipt("observed", None)

    async def finalize(*_args, **_kwargs):
        raise RuntimeError("finalize failed")

    assert await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        tmp_path / "known_hosts",
        claim=lambda *_args, **_kwargs: operation(),
        finalize=finalize,
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=transport,
    )
    assert transport_calls == 1
    assert "rollback" in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transport_phase", "state", "code"),
    [
        ("preflight", "failed", "vpn_node_preflight_failed"),
        ("mutation", "uncertain", "vpn_node_mutation_uncertain"),
    ],
)
async def test_transport_failure_is_finalized_once_without_retry(
    tmp_path, transport_phase, state, code
):
    events = []
    sessions = [Session(events, operation()), Session(events)]
    calls = 0

    async def transport(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise VpnNodeTransportError(transport_phase)

    async def finalize(_db, _operation_id, _token, **receipt):
        events.append(receipt)

    assert await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        tmp_path / "known_hosts",
        claim=lambda *_args, **_kwargs: operation(),
        finalize=finalize,
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=transport,
    )
    assert calls == 1
    assert {"receipt_state": state, "error_code": code} in events


@pytest.mark.asyncio
async def test_failure_after_exact_receipt_preserves_receipt(tmp_path):
    events = []
    sessions = [Session(events, operation()), Session(events)]

    async def transport(_snapshot, _request, *, phase_observer, **_kwargs):
        phase_observer("stdin_write_attempted", None)
        phase_observer("receipt_validated", NodeControlReceipt("observed", None))
        raise VpnNodeTransportError("mutation")

    async def finalize(_db, _operation_id, _token, **receipt):
        events.append(receipt)

    assert await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        tmp_path / "known_hosts",
        claim=lambda *_args, **_kwargs: operation(),
        finalize=finalize,
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=transport,
    )
    assert {"receipt_state": "observed", "error_code": None} in events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "expected"),
    [
        ("prewrite", ("failed", "vpn_node_preflight_failed")),
        ("stdin_write_attempted", ("uncertain", "vpn_node_mutation_uncertain")),
        ("receipt_validated", ("observed", None)),
    ],
)
async def test_post_commit_cancellation_shields_bounded_finalize_and_reraises(
    tmp_path, phase, expected
):
    events = []
    sessions = [Session(events, operation()), Session(events)]
    finalize_started = asyncio.Event()

    async def transport(_snapshot, _request, *, phase_observer, **_kwargs):
        if phase == "stdin_write_attempted":
            phase_observer("stdin_write_attempted", None)
        elif phase == "receipt_validated":
            phase_observer("stdin_write_attempted", None)
            phase_observer("receipt_validated", NodeControlReceipt("observed", None))
        raise asyncio.CancelledError

    async def finalize(_db, _operation_id, _token, **receipt):
        finalize_started.set()
        await asyncio.sleep(0)
        events.append(receipt)

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_vpn_control_operation(
            lambda: sessions.pop(0),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
            finalize_timeout=1,
        )
    assert finalize_started.is_set()
    assert {
        "receipt_state": expected[0],
        "error_code": expected[1],
    } in events


@pytest.mark.asyncio
async def test_cancellation_during_claim_commit_never_connects(tmp_path):
    events = []

    class CancelCommitSession(Session):
        async def commit(self):
            raise asyncio.CancelledError

    async def transport(*_args, **_kwargs):
        pytest.fail("transport called")

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_vpn_control_operation(
            lambda: CancelCommitSession(events, operation()),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
        )


@pytest.mark.asyncio
async def test_cancellation_while_closing_committed_claim_finalizes_preflight(tmp_path):
    events = []

    class CancelCloseSession(Session):
        async def __aexit__(self, *_args):
            self.closed = True
            events.append("claim-session-close-cancelled")
            raise asyncio.CancelledError

    sessions = [CancelCloseSession(events, operation()), Session(events)]

    async def finalize(_db, _operation_id, _token, **receipt):
        events.append(receipt)

    async def transport(*_args, **_kwargs):
        pytest.fail("transport called")

    with pytest.raises(asyncio.CancelledError):
        await dispatch_next_vpn_control_operation(
            lambda: sessions.pop(0),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=transport,
            finalize_timeout=1,
        )
    assert {
        "receipt_state": "failed",
        "error_code": "vpn_node_preflight_failed",
    } in events


@pytest.mark.asyncio
async def test_failure_while_closing_committed_claim_finalizes_preflight(tmp_path):
    events = []

    class FailCloseSession(Session):
        async def __aexit__(self, *_args):
            self.closed = True
            raise RuntimeError("close failed")

    sessions = [FailCloseSession(events, operation()), Session(events)]

    async def finalize(_db, _operation_id, _token, **receipt):
        events.append(receipt)

    assert await dispatch_next_vpn_control_operation(
        lambda: sessions.pop(0),
        tmp_path / "known_hosts",
        claim=lambda *_args, **_kwargs: operation(),
        finalize=finalize,
        snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
        transport=lambda *_args, **_kwargs: pytest.fail("transport called"),
    )
    assert {
        "receipt_state": "failed",
        "error_code": "vpn_node_preflight_failed",
    } in events


@pytest.mark.asyncio
async def test_cancellation_during_exact_receipt_finalize_waits_then_reraises(tmp_path):
    events = []
    sessions = [Session(events, operation()), Session(events)]
    started = asyncio.Event()
    release = asyncio.Event()

    async def finalize(_db, _operation_id, _token, **receipt):
        started.set()
        await release.wait()
        events.append(receipt)

    task = asyncio.create_task(
        dispatch_next_vpn_control_operation(
            lambda: sessions.pop(0),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=lambda *_args, **_kwargs: asyncio.sleep(
                0, result=NodeControlReceipt("observed", None)
            ),
            finalize_timeout=1,
        )
    )
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {"receipt_state": "observed", "error_code": None} in events
    assert events.count("commit") == 2


@pytest.mark.asyncio
async def test_finalize_timeout_cancels_collects_and_cannot_commit_later(tmp_path):
    events = []
    claim_session = Session(events, operation())
    finalize_session = Session(events)
    sessions = [claim_session, finalize_session]
    started = asyncio.Event()
    release = asyncio.Event()

    async def finalize(_db, _operation_id, _token, **_receipt):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            events.append("finalize-cancelled-once")
            await release.wait()

    dispatch = asyncio.create_task(
        dispatch_next_vpn_control_operation(
            lambda: sessions.pop(0),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=lambda *_args, **_kwargs: asyncio.sleep(
                0, result=NodeControlReceipt("observed", None)
            ),
            finalize_timeout=0.02,
        )
    )
    await started.wait()
    assert await asyncio.wait_for(dispatch, 0.5)
    assert finalize_session.closed
    assert events.count("commit") == 1
    assert "rollback" in events
    release.set()
    await asyncio.sleep(0.05)
    assert events.count("commit") == 1


@pytest.mark.asyncio
async def test_repeated_cancellation_keeps_exact_receipt_shielded(tmp_path):
    events = []
    sessions = [Session(events, operation()), Session(events)]
    started = asyncio.Event()
    release = asyncio.Event()

    async def finalize(_db, _operation_id, _token, **receipt):
        started.set()
        await release.wait()
        events.append(receipt)

    dispatch = asyncio.create_task(
        dispatch_next_vpn_control_operation(
            lambda: sessions.pop(0),
            tmp_path / "known_hosts",
            claim=lambda *_args, **_kwargs: operation(),
            finalize=finalize,
            snapshot_loader=lambda *_args: SimpleNamespace(password="secret"),
            transport=lambda *_args, **_kwargs: asyncio.sleep(
                0, result=NodeControlReceipt("observed", None)
            ),
            finalize_timeout=1,
        )
    )
    await started.wait()
    dispatch.cancel()
    await asyncio.sleep(0)
    dispatch.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(asyncio.shield(dispatch), 0.5)
    assert {"receipt_state": "observed", "error_code": None} in events
    assert events.count("commit") == 2
    assert events.count("session-close") == 2


@pytest.mark.asyncio
async def test_revoked_finalize_gate_rolls_back_before_commit():
    events = []
    session = Session(events)
    started = asyncio.Event()
    release = asyncio.Event()
    gate = _FinalizeGate()

    async def finalize(*_args, **_kwargs):
        started.set()
        await release.wait()

    task = asyncio.create_task(
        _finalize_once(
            lambda: session,
            finalize,
            OPERATION_ID,
            TOKEN,
            NodeControlReceipt("observed", None),
            None,
            gate,
        )
    )
    await started.wait()
    gate.revoke()
    release.set()
    assert await task is False
    assert "rollback" in events
    assert "commit" not in events
    assert session.closed
