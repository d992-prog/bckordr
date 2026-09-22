from __future__ import annotations

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.services.vpn_node_transport import NodeControlReceipt


OPERATION_ID = UUID("aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
TOKEN = UUID("bbbbbbbb-cccc-4ddd-8eee-ffffffffffff")
DIGEST = "a" * 64
SECRET = "synthetic-reconciliation-secret"


class Session:
    def __init__(self, operation, events):
        self.operation = operation
        self.events = events

    async def __aenter__(self):
        self.events.append("open")
        return self

    async def __aexit__(self, *_args):
        self.events.append("close")

    async def scalar(self, statement):
        assert statement._for_update_arg is not None
        self.events.append("locked-read")
        return self.operation

    async def commit(self):
        self.events.append("commit")

    async def rollback(self):
        self.events.append("rollback")


def operation(state):
    return SimpleNamespace(
        id=str(OPERATION_ID),
        claim_token=str(TOKEN),
        request_digest=DIGEST,
        state=state,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["succeeded", "failed", "superseded", "uncertain"])
async def test_already_finalized_operation_is_idempotent_without_node_call(state):
    from app.services.vpn_control_reconciliation import (
        reconcile_vpn_control_finalize,
    )

    events = []

    async def lookup(*_args, **_kwargs):
        pytest.fail("node lookup called for already-finalized operation")

    assert await reconcile_vpn_control_finalize(
        lambda: Session(operation(state), events),
        SimpleNamespace(password=SECRET),
        operation_id=OPERATION_ID,
        claim_token=TOKEN,
        request_digest=DIGEST,
        safe_receipt=NodeControlReceipt("observed", None),
        requires_node_lookup=True,
        receipt_lookup=lookup,
        finalize=lambda *_args, **_kwargs: pytest.fail("finalize replayed"),
    )
    assert events == ["open", "locked-read", "commit", "close"]


@pytest.mark.asyncio
async def test_still_claimed_operation_uses_exact_node_receipt_then_finalizes_fresh():
    from app.services.vpn_control_reconciliation import (
        reconcile_vpn_control_finalize,
    )

    events = []
    sessions = [
        Session(operation("claimed"), events),
        Session(operation("claimed"), events),
    ]
    snapshot = SimpleNamespace(password=SECRET)

    async def lookup(received_snapshot, **identity):
        events.append(("lookup", received_snapshot, identity))
        return NodeControlReceipt("observed", None)

    async def finalize(db, operation_id, claim_token, **receipt):
        events.append(("finalize", db, operation_id, claim_token, receipt))

    assert await reconcile_vpn_control_finalize(
        lambda: sessions.pop(0),
        snapshot,
        operation_id=OPERATION_ID,
        claim_token=TOKEN,
        request_digest=DIGEST,
        safe_receipt=NodeControlReceipt("uncertain", "vpn_node_mutation_uncertain"),
        requires_node_lookup=True,
        receipt_lookup=lookup,
        finalize=finalize,
    )
    lookup_event = next(
        event for event in events if isinstance(event, tuple) and event[0] == "lookup"
    )
    assert lookup_event == (
        "lookup",
        snapshot,
        {"operation_id": OPERATION_ID, "request_digest": DIGEST},
    )
    finalize_event = next(
        event for event in events if isinstance(event, tuple) and event[0] == "finalize"
    )
    assert finalize_event[2:] == (
        OPERATION_ID,
        TOKEN,
        {"receipt_state": "observed", "error_code": None},
    )
    assert events.count("commit") == 2
    assert events.count("close") == 2


@pytest.mark.asyncio
async def test_missing_node_receipt_keeps_claim_reserved_without_finalize():
    from app.services.vpn_control_reconciliation import (
        reconcile_vpn_control_finalize,
    )

    events = []
    assert not await reconcile_vpn_control_finalize(
        lambda: Session(operation("claimed"), events),
        SimpleNamespace(password=SECRET),
        operation_id=OPERATION_ID,
        claim_token=TOKEN,
        request_digest=DIGEST,
        safe_receipt=NodeControlReceipt("uncertain", "vpn_node_mutation_uncertain"),
        requires_node_lookup=True,
        receipt_lookup=lambda *_args, **_kwargs: None,
        finalize=lambda *_args, **_kwargs: pytest.fail("missing receipt finalized"),
    )
    assert events == ["open", "locked-read", "commit", "close"]


@pytest.mark.asyncio
async def test_prewrite_reconciliation_reuses_safe_receipt_without_node_call():
    from app.services.vpn_control_reconciliation import (
        reconcile_vpn_control_finalize,
    )

    events = []
    sessions = [
        Session(operation("claimed"), events),
        Session(operation("claimed"), events),
    ]

    async def finalize(_db, _operation_id, _claim_token, **receipt):
        events.append(receipt)

    assert await reconcile_vpn_control_finalize(
        lambda: sessions.pop(0),
        None,
        operation_id=OPERATION_ID,
        claim_token=TOKEN,
        request_digest=DIGEST,
        safe_receipt=NodeControlReceipt("failed", "vpn_node_preflight_failed"),
        requires_node_lookup=False,
        receipt_lookup=lambda *_args, **_kwargs: pytest.fail("node lookup called"),
        finalize=finalize,
    )
    assert {
        "receipt_state": "failed",
        "error_code": "vpn_node_preflight_failed",
    } in events


@pytest.mark.asyncio
async def test_reconciliation_commit_loss_rechecks_without_second_node_lookup():
    from app.services.vpn_control_reconciliation import (
        reconcile_vpn_control_finalize,
    )

    events = []
    terminal = operation("claimed")

    class LostCommitSession(Session):
        async def commit(self):
            self.events.append("commit-response-lost")
            terminal.state = "succeeded"
            raise RuntimeError("synthetic lost reconciliation commit response")

    sessions = [
        Session(operation("claimed"), events),
        LostCommitSession(operation("claimed"), events),
        Session(terminal, events),
    ]
    lookups = 0

    async def lookup(*_args, **_kwargs):
        nonlocal lookups
        lookups += 1
        return NodeControlReceipt("observed", None)

    assert await reconcile_vpn_control_finalize(
        lambda: sessions.pop(0),
        SimpleNamespace(password=SECRET),
        operation_id=OPERATION_ID,
        claim_token=TOKEN,
        request_digest=DIGEST,
        safe_receipt=NodeControlReceipt("observed", None),
        requires_node_lookup=True,
        receipt_lookup=lookup,
        finalize=lambda *_args, **_kwargs: None,
    )
    assert lookups == 1
    assert not sessions


@pytest.mark.asyncio
async def test_identity_mismatch_fails_with_static_secret_free_error():
    from app.services.vpn_control_reconciliation import (
        VpnControlReconciliationError,
        reconcile_vpn_control_finalize,
    )

    row = operation("claimed")
    row.request_digest = "b" * 64
    with pytest.raises(VpnControlReconciliationError) as caught:
        await reconcile_vpn_control_finalize(
            lambda: Session(row, []),
            SimpleNamespace(password=SECRET),
            operation_id=OPERATION_ID,
            claim_token=TOKEN,
            request_digest=DIGEST,
            safe_receipt=NodeControlReceipt("observed", None),
            requires_node_lookup=True,
        )
    assert str(caught.value) == "vpn_control_reconciliation_failed"
    assert SECRET not in repr(caught.value) + str(caught.value)
