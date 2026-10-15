"""Resumable control-side staging for one protected VPN node."""

from __future__ import annotations

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import VpnEndpoint, WorkerMaintenanceJob, WorkerNode
from app.db.session import AsyncSessionLocal
from app.services.vpn_control_intents import active_vpn_control_worker_ids
from app.services.vpn_endpoint_types import VpnEndpointTarget
from app.services.vpn_node_bootstrap import (
    VpnNodeBootstrapError,
    _node_config_bytes,
    build_vpn_node_bootstrap_bundle,
    execute_vpn_node_bootstrap,
)
from app.services.vpn_node_bundle import build_node_bundle
from app.services.vpn_node_deployment import (
    NodeDeploymentError,
    build_node_deployment_helper,
    deploy_node_helper_over_ssh,
)
from app.services.vpn_node_health import (
    VPN_NODE_HEALTH_ERROR_CODES,
    VpnNodeHealthRequest,
)
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    _known_hosts_target,
    _parse_literal_known_hosts,
    execute_vpn_node_health_over_ssh,
    load_transport_snapshot,
)
from app.services.vpn_policy import active_attack_worker_ids, lock_vpn_worker
from app.services.vpn_reality_endpoint_deployment import (
    EndpointInstallerDeploymentError,
    build_endpoint_installer_bundle,
    execute_endpoint_installer_over_ssh,
    install_endpoint_installer_over_ssh,
)
from app.services.vpn_reality_endpoint_installer import (
    REALITY_SERVER_NAME,
    EndpointInstallRequest,
)
from app.services.vpn_reality_endpoint_registration import (
    EndpointRegistrationError,
    stage_protected_endpoint,
)

SAFE_ONBOARDING_ERRORS = frozenset(
    {
        "vpn_onboarding_worker_unavailable",
        "vpn_onboarding_worker_busy",
        "vpn_onboarding_host_key_changed",
        "vpn_onboarding_port_conflict",
        "vpn_onboarding_reconciliation_required",
        "vpn_onboarding_health_failed",
        "vpn_onboarding_failed",
        "vpn_endpoint_registration_conflict",
        "vpn_node_bootstrap_mutation_uncertain",
        *VPN_NODE_HEALTH_ERROR_CODES,
    }
)

_PANEL_URL = "http://127.0.0.1:2053/panel/"


class VpnNodeOnboardingError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code if code in SAFE_ONBOARDING_ERRORS else "vpn_onboarding_failed"
        super().__init__(self.code)


@dataclass(frozen=True, slots=True)
class VpnNodeOnboardingView:
    worker_id: int
    state: Literal[
        "needs_preflight",
        "needs_trust",
        "ready_to_start",
        "queued",
        "running",
        "awaiting_test",
        "ready",
        "failed",
    ]
    fingerprint: str | None
    job_id: int | None
    endpoint_id: int | None
    error_code: str | None


async def get_vpn_node_onboarding_view(
    session: AsyncSession,
    worker_id: int,
) -> VpnNodeOnboardingView:
    worker = await session.get(WorkerNode, worker_id)
    if worker is None:
        raise ValueError("vpn_onboarding_worker_unavailable") from None
    endpoint = await session.scalar(
        select(VpnEndpoint)
        .where(VpnEndpoint.worker_id == worker_id)
        .order_by(VpnEndpoint.id.desc())
        .limit(1)
    )
    job = await session.scalar(
        select(WorkerMaintenanceJob)
        .where(
            WorkerMaintenanceJob.worker_id == worker_id,
            WorkerMaintenanceJob.action == "vpn_onboard",
        )
        .order_by(WorkerMaintenanceJob.id.desc())
        .limit(1)
    )
    if endpoint is not None and endpoint.status == "ready":
        state = "ready"
    elif job is not None and job.status in {"queued", "running"}:
        state = job.status
    elif endpoint is not None and endpoint.status == "staged":
        state = "failed" if endpoint.last_error_code else "awaiting_test"
    elif job is not None and job.status == "failed":
        state = "failed"
    elif not worker.ssh_access_configured:
        state = "needs_preflight"
    elif not worker.ssh_host_key or not worker.ssh_host_key_fingerprint:
        state = "needs_trust"
    else:
        state = "ready_to_start"
    error = None
    if state == "failed":
        candidate = endpoint.last_error_code if endpoint is not None else None
        candidate = candidate or (job.error_message if job is not None else None)
        error = candidate if candidate in SAFE_ONBOARDING_ERRORS else "vpn_onboarding_health_failed"
    return VpnNodeOnboardingView(
        worker_id=worker_id,
        state=state,
        fingerprint=worker.ssh_host_key_fingerprint,
        job_id=job.id if job is not None else None,
        endpoint_id=endpoint.id if endpoint is not None else None,
        error_code=error,
    )


def _fail(code: str) -> None:
    raise VpnNodeOnboardingError(code) from None


def _short_id(secret: str, worker_id: int, public_host: str) -> str:
    if not secret or secret == "change-me-session-secret":
        _fail("vpn_onboarding_failed")
    return hmac.new(
        secret.encode("utf-8"),
        f"vpn-endpoint:{worker_id}:{public_host}".encode("ascii"),
        hashlib.sha256,
    ).hexdigest()[:16]


def _pin_matches(snapshot, worker: WorkerNode) -> bool:
    try:
        target = _known_hosts_target(snapshot.host, snapshot.port)
        entry = _parse_literal_known_hosts(snapshot.known_hosts).get(target)
        if entry is None or entry.line != worker.ssh_host_key:
            return False
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(entry.key).digest()
        ).decode("ascii").rstrip("=")
        return hmac.compare_digest(fingerprint, worker.ssh_host_key_fingerprint)
    except (AttributeError, TypeError, UnicodeError, VpnNodeTransportError):
        return False


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


def _bootstrap_error(error: VpnNodeBootstrapError) -> str:
    return {
        "vpn_node_bootstrap_port_conflict": "vpn_onboarding_port_conflict",
        "vpn_node_bootstrap_mutation_uncertain": "vpn_node_bootstrap_mutation_uncertain",
        "vpn_node_bootstrap_reconciliation_required": "vpn_onboarding_reconciliation_required",
        "vpn_node_bootstrap_foreign_state": "vpn_onboarding_reconciliation_required",
    }.get(error.code, "vpn_onboarding_failed")


async def _persist_failure(session_factory, job_id: int, code: str) -> None:
    safe_code = code if code in SAFE_ONBOARDING_ERRORS else "vpn_onboarding_failed"
    async with session_factory() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        if job is None:
            return
        worker = await session.get(WorkerNode, job.worker_id)
        job.status = "failed"
        job.error_message = safe_code
        job.log = None
        job.finished_at = utcnow()
        job.updated_at = utcnow()
        if worker is not None:
            worker.vpn_runtime_status = "error"
            worker.vpn_last_error = safe_code
            worker.vpn_last_checked_at = utcnow()
        await session.commit()


async def run_vpn_node_onboarding_job(
    job_id: int,
    *,
    session_factory=AsyncSessionLocal,
    bootstrap=execute_vpn_node_bootstrap,
    deploy_node=deploy_node_helper_over_ssh,
    install_endpoint_helper=install_endpoint_installer_over_ssh,
    execute_endpoint=execute_endpoint_installer_over_ssh,
    health_transport=execute_vpn_node_health_over_ssh,
) -> None:
    """Stage one exact protected endpoint; retries re-observe every remote stage."""
    try:
        async with session_factory() as session:
            job = await session.get(WorkerMaintenanceJob, job_id)
            if job is None or job.action != "vpn_onboard" or job.status == "succeeded":
                return
            worker = await session.get(WorkerNode, job.worker_id)
            if worker is None:
                _fail("vpn_onboarding_worker_unavailable")
            job.status = "running"
            job.error_message = None
            job.log = None
            job.started_at = job.started_at or utcnow()
            job.updated_at = utcnow()
            worker.vpn_runtime_status = "onboarding"
            worker.vpn_last_error = None
            await session.commit()

            worker = await lock_vpn_worker(session, job.worker_id)
            if (
                worker is None
                or worker.archived_at is not None
                or worker.status != "ready"
                or worker.is_enabled is not True
                or worker.vpn_enabled is not True
                or worker.vpn_role == "none"
                or not worker.ssh_access_configured
                or not worker.ssh_host_key
                or not worker.ssh_host_key_fingerprint
            ):
                _fail("vpn_onboarding_worker_unavailable")
            other_job = await session.scalar(
                select(WorkerMaintenanceJob.id).where(
                    WorkerMaintenanceJob.worker_id == worker.id,
                    WorkerMaintenanceJob.id != job.id,
                    WorkerMaintenanceJob.status.in_(("queued", "running")),
                ).limit(1)
            )
            if (
                other_job is not None
                or worker.id in await active_attack_worker_ids(session)
                or worker.id in await active_vpn_control_worker_ids(session)
            ):
                _fail("vpn_onboarding_worker_busy")

            settings = get_settings()
            known_hosts_path = Path(settings.vpn_control_known_hosts_path)
            if not known_hosts_path.is_absolute():
                _fail("vpn_onboarding_failed")
            try:
                snapshot = load_transport_snapshot(worker, known_hosts_path)
            except VpnNodeTransportError:
                _fail("vpn_onboarding_host_key_changed")
            if not _pin_matches(snapshot, worker):
                _fail("vpn_onboarding_host_key_changed")

            public_host = worker.vpn_public_host or worker.ssh_host or worker.ip_address
            if not public_host:
                _fail("vpn_onboarding_worker_unavailable")
            short_id = _short_id(settings.session_secret_key, worker.id, public_host)
            source_root = Path(__file__).resolve().parents[2]
            with TemporaryDirectory(prefix="veltrix-vpn-onboard-") as temporary:
                directory = Path(temporary)
                bootstrap_path = directory / "bootstrap.pyz"
                bootstrap_digest = build_vpn_node_bootstrap_bundle(
                    source_root,
                    bootstrap_path,
                    worker_id=worker.id,
                    panel_url=_PANEL_URL,
                )
                try:
                    bootstrap_receipt = await bootstrap(
                        snapshot,
                        bootstrap_path.read_bytes(),
                        expected_sha256=bootstrap_digest,
                    )
                except VpnNodeBootstrapError as error:
                    _fail(_bootstrap_error(error))

                node_path = directory / "vpn-node.pyz"
                node_digest = build_node_bundle(source_root, node_path)
                node_config = _node_config_bytes(
                    bootstrap_receipt.panel_url,
                    PurePosixPath(bootstrap_receipt.database_path),
                )
                deploy_path = directory / "node-deploy.pyz"
                deploy_digest = build_node_deployment_helper(
                    source_root,
                    deploy_path,
                    node_bundle=node_path.read_bytes(),
                    expected_node_sha256=node_digest,
                    node_config=node_config,
                )
                deployed = await deploy_node(
                    snapshot,
                    deploy_path.read_bytes(),
                    expected_sha256=deploy_digest,
                )
                if deployed not in {"installed", "recovered"}:
                    _fail("vpn_onboarding_reconciliation_required")

                endpoint_path = directory / "endpoint-installer.pyz"
                endpoint_digest = build_endpoint_installer_bundle(
                    source_root,
                    endpoint_path,
                    worker_id=worker.id,
                )
                await install_endpoint_helper(
                    snapshot,
                    endpoint_path.read_bytes(),
                    expected_sha256=endpoint_digest,
                )
                request = EndpointInstallRequest(
                    action="ensure",
                    worker_id=worker.id,
                    public_host=public_host,
                    server_name=REALITY_SERVER_NAME,
                    short_id=short_id,
                )
                try:
                    receipt = await execute_endpoint(
                        snapshot,
                        request,
                        controlled_worker_id=worker.id,
                    )
                except EndpointInstallerDeploymentError as error:
                    if not error.mutation_uncertain:
                        raise
                    inspect = EndpointInstallRequest(
                        action="inspect",
                        worker_id=worker.id,
                        public_host=public_host,
                        server_name=REALITY_SERVER_NAME,
                        short_id=short_id,
                    )
                    try:
                        receipt = await execute_endpoint(
                            snapshot,
                            inspect,
                            controlled_worker_id=worker.id,
                        )
                    except EndpointInstallerDeploymentError:
                        _fail("vpn_onboarding_reconciliation_required")

                endpoint = await stage_protected_endpoint(
                    session,
                    receipt,
                    controlled_worker_id=worker.id,
                )
                if endpoint.max_active_profiles is None:
                    endpoint.max_active_profiles = 20
                checked_at = datetime.now(UTC)
                checked_at_ms = int(checked_at.timestamp() * 1000)
                health = await health_transport(
                    snapshot,
                    VpnNodeHealthRequest(
                        worker_id=worker.id,
                        target=_target(endpoint),
                        checked_at_ms=checked_at_ms,
                    ),
                    now_ms=checked_at_ms,
                )
                if health.state != "healthy":
                    _fail(health.error_code or "vpn_onboarding_health_failed")
                endpoint.status = "staged"
                endpoint.health_checked_at = checked_at
                endpoint.last_error_code = None
                worker.vpn_inbound_id = endpoint.inbound_id
                worker.vpn_public_host = endpoint.public_host
                worker.vpn_inbound_port = endpoint.port
                worker.vpn_inbound_protocol = endpoint.protocol
                worker.vpn_inbound_transport = endpoint.transport
                worker.vpn_inbound_security = endpoint.security
                worker.vpn_listener_status = "listening"
                worker.vpn_runtime_status = "awaiting_test"
                worker.vpn_last_checked_at = checked_at
                worker.vpn_last_error = None
                job.status = "succeeded"
                job.log = "vpn_onboarding_awaiting_test"
                job.error_message = None
                job.finished_at = utcnow()
                job.updated_at = utcnow()
                await session.commit()
    except VpnNodeOnboardingError as error:
        await _persist_failure(session_factory, job_id, error.code)
    except EndpointRegistrationError as error:
        await _persist_failure(session_factory, job_id, error.code)
    except (EndpointInstallerDeploymentError, NodeDeploymentError, VpnNodeTransportError):
        await _persist_failure(
            session_factory,
            job_id,
            "vpn_onboarding_reconciliation_required",
        )
    except Exception:  # noqa: BLE001 - only a fixed safe code is persisted
        await _persist_failure(session_factory, job_id, "vpn_onboarding_failed")
