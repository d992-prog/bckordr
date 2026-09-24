"""Transaction-separated, single-operation VPN control dispatcher."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import inspect
import json
from pathlib import Path
from uuid import UUID, uuid4

from app.db.models import WorkerNode
from app.services.vpn_control_intents import (
    claim_next_vpn_control_operation,
    finalize_vpn_control_operation,
)
from app.services.vpn_control_reconciliation import reconcile_vpn_control_finalize
from app.services.vpn_node_transport import (
    NodeControlReceipt,
    VpnNodeTransportError,
    execute_vpn_node_request,
    load_transport_snapshot,
)


DEFAULT_FINALIZE_TIMEOUT = 15.0


class _FinalizeGate:
    def __init__(self) -> None:
        self._state = "before_commit"

    def begin_commit(self) -> bool:
        if self._state != "before_commit":
            return False
        self._state = "committing"
        return True

    def revoke(self) -> bool:
        if self._state != "before_commit":
            return False
        self._state = "revoked"
        return True


async def _resolve(value):
    return await value if inspect.isawaitable(value) else value


async def _rollback(db) -> None:
    try:
        await db.rollback()
    except BaseException:
        pass


async def _finalize_once(
    session_factory,
    finalize,
    operation_id: UUID,
    claim_token: UUID,
    receipt: NodeControlReceipt,
    now: Callable[[], object] | None,
    gate: _FinalizeGate,
) -> bool:
    async with session_factory() as db:
        try:
            kwargs = {
                "receipt_state": receipt.state,
                "error_code": receipt.error_code,
            }
            if now is not None:
                kwargs["now"] = now()
            await _resolve(finalize(db, operation_id, claim_token, **kwargs))
            if not gate.begin_commit():
                await _rollback(db)
                return False
            await db.commit()
            return True
        except BaseException:
            await _rollback(db)
            raise


def _task_outcome(task: asyncio.Task) -> bool:
    try:
        return task.result()
    except asyncio.CancelledError:
        return False
    except Exception:
        return False


async def _await_definitive(
    task: asyncio.Task, cancellation: asyncio.CancelledError | None
) -> asyncio.CancelledError | None:
    while not task.done():
        try:
            await asyncio.wait({task})
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            continue
    return cancellation


async def _bounded_finalize(
    session_factory,
    finalize,
    operation_id: UUID,
    claim_token: UUID,
    receipt: NodeControlReceipt,
    precommit_timeout: float,
    now: Callable[[], object] | None,
) -> bool:
    """Revoke before COMMIT by the deadline; never detach after COMMIT begins."""
    gate = _FinalizeGate()
    task = asyncio.create_task(
        _finalize_once(
            session_factory,
            finalize,
            operation_id,
            claim_token,
            receipt,
            now,
            gate,
        )
    )
    deadline = asyncio.get_running_loop().time() + precommit_timeout
    cancellation: asyncio.CancelledError | None = None
    while not task.done():
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        try:
            done, _pending = await asyncio.wait({task}, timeout=remaining)
        except asyncio.CancelledError as error:
            cancellation = cancellation or error
            continue
        if task in done:
            break
        break

    if not task.done():
        if gate.revoke():
            task.cancel()
        # Once commit has started, cancellation cannot safely classify its outcome.
        # Wait for the database operation and session close instead of detaching it.
        cancellation = await _await_definitive(task, cancellation)
    try:
        outcome = _task_outcome(task)
    except BaseException:
        if cancellation is not None:
            raise cancellation from None
        raise
    if cancellation is not None:
        raise cancellation from None
    return outcome


async def _finalize_with_reconciliation(
    session_factory,
    finalize,
    reconcile,
    snapshot,
    operation_id: UUID,
    claim_token: UUID,
    request_digest: str,
    receipt: NodeControlReceipt,
    requires_node_lookup: bool,
    precommit_timeout: float,
    now: Callable[[], object] | None,
) -> bool:
    if await _bounded_finalize(
        session_factory,
        finalize,
        operation_id,
        claim_token,
        receipt,
        precommit_timeout,
        now,
    ):
        return True
    return await _resolve(
        reconcile(
            session_factory,
            snapshot,
            operation_id=operation_id,
            claim_token=claim_token,
            request_digest=request_digest,
            safe_receipt=receipt,
            requires_node_lookup=requires_node_lookup,
            now=now,
        )
    )


def _failure_receipt(phase: str) -> NodeControlReceipt:
    if phase == "prewrite" or phase == "preflight":
        return NodeControlReceipt("failed", "vpn_node_preflight_failed")
    return NodeControlReceipt("uncertain", "vpn_node_mutation_uncertain")


def _requires_node_lookup(phase: str) -> bool:
    return phase not in {"prewrite", "preflight"}


async def dispatch_next_vpn_control_operation(
    session_factory,
    known_hosts_path: Path,
    *,
    claim_token_factory: Callable[[], UUID] = uuid4,
    claim=claim_next_vpn_control_operation,
    finalize=finalize_vpn_control_operation,
    reconcile=reconcile_vpn_control_finalize,
    snapshot_loader=load_transport_snapshot,
    transport=execute_vpn_node_request,
    finalize_timeout: float = DEFAULT_FINALIZE_TIMEOUT,
    now: Callable[[], object] | None = None,
) -> bool:
    """Claim, execute once, and finalize; timeout only bounds pre-COMMIT revoke."""
    claim_token = claim_token_factory()
    operation_id = None
    request_digest = None
    request_bytes = None
    snapshot = None
    snapshot_failed = False
    claim_committed = False

    try:
        async with session_factory() as db:
            try:
                claim_kwargs = {"claim_token": claim_token}
                if now is not None:
                    claim_kwargs["now"] = now()
                operation = await _resolve(claim(db, **claim_kwargs))
                if operation is not None:
                    operation_id = UUID(operation.id)
                    request_digest = operation.request_digest
                    request_bytes = (
                        json.dumps(
                            operation.request_snapshot,
                            separators=(",", ":"),
                            ensure_ascii=True,
                            allow_nan=False,
                        ).encode("ascii")
                        + b"\n"
                    )
                    worker = await db.get(WorkerNode, operation.worker_id)
                    if worker is None:
                        snapshot_failed = True
                    else:
                        try:
                            snapshot = snapshot_loader(worker, Path(known_hosts_path))
                        except Exception:
                            snapshot_failed = True
                await db.commit()
                claim_committed = True
            except BaseException:
                await _rollback(db)
                raise
    except asyncio.CancelledError:
        if claim_committed and operation_id is not None:
            try:
                await _finalize_with_reconciliation(
                    session_factory,
                    finalize,
                    reconcile,
                    snapshot,
                    operation_id,
                    claim_token,
                    request_digest,
                    _failure_receipt("preflight"),
                    False,
                    finalize_timeout,
                    now,
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        raise
    except Exception:
        if claim_committed and operation_id is not None:
            await _finalize_with_reconciliation(
                session_factory,
                finalize,
                reconcile,
                snapshot,
                operation_id,
                claim_token,
                request_digest,
                _failure_receipt("preflight"),
                False,
                finalize_timeout,
                now,
            )
            return True
        raise

    if operation_id is None:
        return False
    assert request_digest is not None and request_bytes is not None
    if snapshot_failed:
        await _finalize_with_reconciliation(
            session_factory,
            finalize,
            reconcile,
            snapshot,
            operation_id,
            claim_token,
            request_digest,
            _failure_receipt("preflight"),
            False,
            finalize_timeout,
            now,
        )
        return True
    assert snapshot is not None

    phase = "prewrite"
    validated_receipt = None

    def observe(next_phase: str, receipt: NodeControlReceipt | None) -> None:
        nonlocal phase, validated_receipt
        phase = next_phase
        if next_phase == "receipt_validated":
            validated_receipt = receipt

    try:
        receipt = await transport(
            snapshot,
            request_bytes,
            operation_id=operation_id,
            request_digest=request_digest,
            phase_observer=observe,
        )
        validated_receipt = receipt
        phase = "receipt_validated"
    except asyncio.CancelledError:
        receipt = validated_receipt or _failure_receipt(phase)
        try:
            await _finalize_with_reconciliation(
                session_factory,
                finalize,
                reconcile,
                snapshot,
                operation_id,
                claim_token,
                request_digest,
                receipt,
                _requires_node_lookup(phase),
                finalize_timeout,
                now,
            )
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
        raise
    except VpnNodeTransportError as error:
        phase = error.phase
        receipt = validated_receipt or _failure_receipt(error.phase)
    except Exception:
        receipt = validated_receipt or _failure_receipt(phase)

    await _finalize_with_reconciliation(
        session_factory,
        finalize,
        reconcile,
        snapshot,
        operation_id,
        claim_token,
        request_digest,
        receipt,
        _requires_node_lookup(phase),
        finalize_timeout,
        now,
    )
    return True
