"""Resolve an ambiguous finalize COMMIT without repeating a node mutation."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import re
from uuid import UUID

from sqlalchemy import select

from app.db.models import VpnControlOperation
from app.services.vpn_control_intents import finalize_vpn_control_operation
from app.services.vpn_node_transport import (
    NodeControlReceipt,
    lookup_vpn_node_receipt,
)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_FINALIZED_STATES = frozenset({"succeeded", "failed", "superseded", "uncertain"})
_RECEIPTS = frozenset(
    {
        ("observed", None),
        ("failed", "vpn_node_preflight_failed"),
        ("failed", "vpn_node_interrupted_before_mutation"),
        ("uncertain", "vpn_node_mutation_uncertain"),
        ("stale", "vpn_node_operation_stale"),
        ("blocked", "vpn_node_reconciliation_required"),
        ("blocked", "vpn_node_key_revoked"),
    }
)


class VpnControlReconciliationError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("vpn_control_reconciliation_failed")


def _fail() -> None:
    error = VpnControlReconciliationError()
    try:
        raise error from None
    finally:
        error.__context__ = None


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


async def _rollback(db) -> None:
    try:
        await db.rollback()
    except BaseException:
        pass


async def _locked_operation_state(
    session_factory,
    operation_id: UUID,
    claim_token: UUID,
    request_digest: str,
) -> str:
    async with session_factory() as db:
        try:
            operation = await db.scalar(
                select(VpnControlOperation)
                .where(VpnControlOperation.id == str(operation_id))
                .with_for_update()
            )
            if (
                operation is None
                or operation.claim_token != str(claim_token)
                or operation.request_digest != request_digest
            ):
                _fail()
            state = operation.state
            if state not in _FINALIZED_STATES and state != "claimed":
                _fail()
            await db.commit()
            return state
        except BaseException:
            await _rollback(db)
            raise


async def reconcile_vpn_control_finalize(
    session_factory,
    snapshot,
    *,
    operation_id: UUID,
    claim_token: UUID,
    request_digest: str,
    safe_receipt: NodeControlReceipt,
    requires_node_lookup: bool,
    now: Callable[[], object] | None = None,
    receipt_lookup=lookup_vpn_node_receipt,
    finalize=finalize_vpn_control_operation,
) -> bool:
    """Finalize a still-claimed row or accept an already committed final state."""
    if (
        not isinstance(operation_id, UUID)
        or not isinstance(claim_token, UUID)
        or type(request_digest) is not str
        or _DIGEST.fullmatch(request_digest) is None
        or not isinstance(safe_receipt, NodeControlReceipt)
        or (safe_receipt.state, safe_receipt.error_code) not in _RECEIPTS
        or type(requires_node_lookup) is not bool
        or (requires_node_lookup and snapshot is None)
    ):
        _fail()

    try:
        state = await _locked_operation_state(
            session_factory, operation_id, claim_token, request_digest
        )
    except asyncio.CancelledError:
        raise
    except VpnControlReconciliationError:
        raise
    except Exception:
        _fail()

    if state in _FINALIZED_STATES:
        return True

    try:
        receipt = (
            await _resolve(
                receipt_lookup(
                    snapshot,
                    operation_id=operation_id,
                    request_digest=request_digest,
                )
            )
            if requires_node_lookup
            else safe_receipt
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        _fail()
    if receipt is None:
        return False
    if (
        not isinstance(receipt, NodeControlReceipt)
        or (receipt.state, receipt.error_code) not in _RECEIPTS
    ):
        _fail()

    commit_started = False
    try:
        async with session_factory() as db:
            try:
                kwargs = {
                    "receipt_state": receipt.state,
                    "error_code": receipt.error_code,
                }
                if now is not None:
                    kwargs["now"] = now()
                await _resolve(finalize(db, operation_id, claim_token, **kwargs))
                commit_started = True
                await db.commit()
            except BaseException:
                await _rollback(db)
                raise
    except asyncio.CancelledError:
        raise
    except VpnControlReconciliationError:
        raise
    except Exception:
        if not commit_started:
            _fail()
        try:
            state = await _locked_operation_state(
                session_factory, operation_id, claim_token, request_digest
            )
        except asyncio.CancelledError:
            raise
        except VpnControlReconciliationError:
            raise
        except Exception:
            _fail()
        return state in _FINALIZED_STATES
    return True
