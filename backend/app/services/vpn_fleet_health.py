from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from weakref import WeakKeyDictionary

from sqlalchemy import exists, select, text

from app.db.base import utcnow
from app.db.models import (
    VpnControlOperation,
    VpnEndpoint,
    VpnNodeEvent,
    WorkerMaintenanceJob,
    WorkerNode,
)
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    is_valid_vpn_endpoint_target,
    public_endpoint_fingerprint,
)
from app.services.vpn_node_health import (
    VPN_NODE_HEALTH_ERROR_CODES,
    VpnNodeHealthReceipt,
    VpnNodeHealthRequest,
)
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    execute_vpn_node_health_over_ssh,
    load_transport_snapshot,
)
from app.services.vpn_policy import VPN_MUTATION_ACTIONS

_ENDPOINT_STATUSES = ("staged", "ready", "draining")
_MAINTENANCE_ACTIONS = (*VPN_MUTATION_ACTIONS, "vpn_check")
_CONTROL_STATES = ("queued", "claimed", "uncertain")
_TRANSPORT_ERROR = "vpn_node_health_transport_failed"
_INTERNAL_ERROR = "vpn_node_health_internal"
# Signed int64 encoding of the fixed ASCII namespace ``VLTRXHLT``.
_POSTGRES_FLEET_HEALTH_LOCK_ID = 6_218_437_898_136_996_948
_POSTGRES_FLEET_HEALTH_LOCK = text(
    f"SELECT pg_try_advisory_xact_lock({_POSTGRES_FLEET_HEALTH_LOCK_ID})"
)
_LOCAL_PROBE_LOCKS: WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    WeakKeyDictionary()
)


def _dialect_name(session) -> str:
    dialect = getattr(session.get_bind(), "dialect", None)
    return str(getattr(dialect, "name", ""))


def _local_probe_lock() -> asyncio.Lock:
    """Serialize non-PostgreSQL probes in this process and running event loop."""
    loop = asyncio.get_running_loop()
    lock = _LOCAL_PROBE_LOCKS.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _LOCAL_PROBE_LOCKS[loop] = lock
    return lock


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _worker_is_eligible(worker: WorkerNode | None) -> bool:
    return bool(
        worker is not None
        and worker.archived_at is None
        and worker.is_enabled
        and worker.status not in {"offline", "disabled"}
        and worker.vpn_enabled
        and worker.vpn_role != "none"
    )


def _maintenance_exists(worker_id) -> exists:
    return exists().where(
        WorkerMaintenanceJob.worker_id == worker_id,
        WorkerMaintenanceJob.action.in_(_MAINTENANCE_ACTIONS),
        WorkerMaintenanceJob.status.in_(("queued", "running")),
    )


def _control_exists(worker_id) -> exists:
    return exists().where(
        VpnControlOperation.worker_id == worker_id,
        VpnControlOperation.state.in_(_CONTROL_STATES),
    )


async def _worker_is_busy(session, worker_id: int) -> bool:
    return bool(
        await session.scalar(
            select(
                _maintenance_exists(worker_id) | _control_exists(worker_id)
            )
        )
    )


def _target(endpoint: VpnEndpoint) -> VpnEndpointTarget:
    return VpnEndpointTarget(
        endpoint_id=endpoint.id,
        worker_id=endpoint.worker_id,
        inbound_id=endpoint.inbound_id,
        public_host=endpoint.public_host,
        port=endpoint.port,
        protocol=endpoint.protocol,
        transport=endpoint.transport,
        security=endpoint.security,
        server_name=endpoint.server_name,
        public_key=endpoint.public_key,
        short_id=endpoint.short_id,
        fingerprint=endpoint.fingerprint,
        flow=endpoint.flow,
    )


def _failure_code(receipt: VpnNodeHealthReceipt) -> str | None:
    if receipt == VpnNodeHealthReceipt("healthy", None, "running"):
        return None
    if (
        receipt.state == "unhealthy"
        and receipt.error_code in VPN_NODE_HEALTH_ERROR_CODES
    ):
        return receipt.error_code
    return _INTERNAL_ERROR


def _apply_failure(
    session,
    worker: WorkerNode,
    endpoint: VpnEndpoint,
    attempted_at: datetime,
    error_code: str,
) -> None:
    previous_error = endpoint.last_error_code
    worker.vpn_runtime_status = "error"
    worker.vpn_last_checked_at = attempted_at
    worker.vpn_last_error = error_code
    endpoint.last_error_code = error_code
    if previous_error != error_code:
        session.add(
            VpnNodeEvent(
                worker_id=worker.id,
                level="error",
                event_type="vpn_fleet_health_failed",
                message="VPN fleet health probe failed",
                details={"endpoint_id": endpoint.id, "error_code": error_code},
            )
        )


async def probe_next_vpn_endpoint(
    session_factory,
    known_hosts_path: Path,
    *,
    snapshot_loader=load_transport_snapshot,
    transport=execute_vpn_node_health_over_ssh,
    now=utcnow,
) -> bool:
    """Probe at most one eligible endpoint while holding worker and endpoint locks."""
    async with session_factory() as session:
        local_lock = None
        local_lock_acquired = False
        try:
            if _dialect_name(session) == "postgresql":
                if not bool(await session.scalar(_POSTGRES_FLEET_HEALTH_LOCK)):
                    await session.rollback()
                    return False
            else:
                # SQLite/dev fallback is process-local; production PostgreSQL uses
                # the transaction-scoped advisory lock above across app instances.
                local_lock = _local_probe_lock()
                await local_lock.acquire()
                local_lock_acquired = True
            with session.no_autoflush:
                candidate = (
                    await session.execute(
                        select(
                            VpnEndpoint.id,
                            VpnEndpoint.worker_id,
                            VpnEndpoint.health_checked_at,
                        )
                        .join(WorkerNode, WorkerNode.id == VpnEndpoint.worker_id)
                        .where(
                            WorkerNode.archived_at.is_(None),
                            WorkerNode.is_enabled.is_(True),
                            WorkerNode.status.not_in(("offline", "disabled")),
                            WorkerNode.vpn_enabled.is_(True),
                            WorkerNode.vpn_role != "none",
                            VpnEndpoint.status.in_(_ENDPOINT_STATUSES),
                            ~_maintenance_exists(WorkerNode.id),
                            ~_control_exists(WorkerNode.id),
                        )
                        .order_by(
                            VpnEndpoint.health_checked_at.asc().nulls_first(),
                            WorkerNode.vpn_last_checked_at.asc().nulls_first(),
                            WorkerNode.id,
                            VpnEndpoint.id,
                        )
                        .limit(1)
                    )
                ).first()
                if candidate is None:
                    await session.rollback()
                    return False

                endpoint_id, worker_id, selected_health_checked_at = candidate
                worker = await session.scalar(
                    select(WorkerNode)
                    .where(WorkerNode.id == worker_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if not _worker_is_eligible(worker) or await _worker_is_busy(
                    session, worker_id
                ):
                    await session.rollback()
                    return False

                endpoint = await session.scalar(
                    select(VpnEndpoint)
                    .where(
                        VpnEndpoint.id == endpoint_id,
                        VpnEndpoint.worker_id == worker_id,
                    )
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                if (
                    endpoint is None
                    or endpoint.status not in _ENDPOINT_STATUSES
                    or _as_utc(endpoint.health_checked_at)
                    != _as_utc(selected_health_checked_at)
                ):
                    await session.rollback()
                    return False

            assert worker is not None
            attempted_at = _as_utc(now())
            if attempted_at is None:
                raise TypeError("vpn_fleet_health_time_invalid")
            checked_at_ms = int(attempted_at.timestamp() * 1000)
            endpoint.health_checked_at = attempted_at

            try:
                target = _target(endpoint)
                fingerprint = public_endpoint_fingerprint(target)
                if (
                    endpoint.external_verified_at is not None
                    or endpoint.external_config_fingerprint is not None
                ) and not (
                    endpoint.external_verified_at is not None
                    and endpoint.external_config_fingerprint == fingerprint
                ):
                    endpoint.external_verified_at = None
                    endpoint.external_config_fingerprint = None
                if not is_valid_vpn_endpoint_target(target):
                    raise ValueError("vpn_node_health_target_invalid")
                request = VpnNodeHealthRequest(
                    worker_id=worker.id,
                    target=target,
                    checked_at_ms=checked_at_ms,
                )
                snapshot = snapshot_loader(worker, Path(known_hosts_path))
                receipt = await transport(
                    snapshot,
                    request,
                    now_ms=checked_at_ms,
                )
                error_code = _failure_code(receipt)
            except asyncio.CancelledError:
                raise
            except VpnNodeTransportError:
                error_code = _TRANSPORT_ERROR
            except Exception:  # noqa: BLE001 - persist only the fixed safe code
                error_code = _INTERNAL_ERROR

            if error_code is None:
                worker.vpn_runtime_status = "ready"
                worker.vpn_last_checked_at = attempted_at
                worker.vpn_last_error = None
                endpoint.verified_at = attempted_at
                endpoint.last_error_code = None
            else:
                _apply_failure(session, worker, endpoint, attempted_at, error_code)

            await session.commit()
            return True
        except asyncio.CancelledError:
            await session.rollback()
            raise
        except BaseException:
            await session.rollback()
            raise
        finally:
            if local_lock_acquired:
                assert local_lock is not None
                local_lock.release()
