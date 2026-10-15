"""Resumable control-side staging for one protected VPN node."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import AppSetting, VpnEndpoint, WorkerMaintenanceJob, WorkerNode
from app.db.session import AsyncSessionLocal
from app.services.vpn_control_intents import (
    active_vpn_control_worker_ids,
    build_reality_config_uri,
)
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    public_endpoint_fingerprint,
)
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
    VpnNodeHealthReceipt,
    VpnNodeHealthRequest,
)
from app.services.vpn_node_transport import (
    VpnNodeTransportError,
    _known_hosts_target,
    _parse_literal_known_hosts,
    execute_vpn_node_health_over_ssh,
    load_transport_snapshot,
)
from app.services.vpn_policy import (
    active_attack_worker_ids,
    active_vpn_maintenance_worker_ids,
    lock_vpn_worker,
)
from app.services.vpn_reality_endpoint_deployment import (
    EndpointInstallerDeploymentError,
    build_endpoint_installer_bundle,
    execute_endpoint_installer_over_ssh,
    install_endpoint_installer_over_ssh,
)
from app.services.vpn_reality_endpoint_installer import (
    FINGERPRINT,
    FLOW,
    PORT,
    PROTOCOL,
    REALITY_SERVER_NAME,
    SECURITY,
    TRANSPORT,
    EndpointInstallReceipt,
    EndpointInstallRequest,
    make_endpoint_receipt,
)
from app.services.vpn_reality_endpoint_registration import (
    VPN_ENDPOINT_ACCEPTANCE_KEY,
    VPN_FRIEND_BETA_RELEASE_READY_KEY,
    EndpointRegistrationError,
    ExternalEndpointAcceptance,
    promote_protected_endpoint,
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
_ACCEPTANCE_PROFILE_KEY = "vpn_node_acceptance_profile_v1"


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
        .order_by(VpnEndpoint.updated_at.desc(), VpnEndpoint.id.desc())
        .execution_options(populate_existing=True)
        .limit(1)
    )
    active_job = await session.scalar(
        select(WorkerMaintenanceJob)
        .where(
            WorkerMaintenanceJob.worker_id == worker_id,
            WorkerMaintenanceJob.action == "vpn_onboard",
            WorkerMaintenanceJob.status.in_(("queued", "running")),
        )
        .order_by(
            WorkerMaintenanceJob.updated_at.desc(),
            WorkerMaintenanceJob.id.desc(),
        )
        .execution_options(populate_existing=True)
        .limit(1)
    )
    job = active_job or await session.scalar(
        select(WorkerMaintenanceJob)
        .where(
            WorkerMaintenanceJob.worker_id == worker_id,
            WorkerMaintenanceJob.action == "vpn_onboard",
        )
        .order_by(
            WorkerMaintenanceJob.updated_at.desc(),
            WorkerMaintenanceJob.id.desc(),
        )
        .execution_options(populate_existing=True)
        .limit(1)
    )
    error_source = None
    if active_job is not None:
        state = job.status
    elif endpoint is not None and endpoint.status == "ready":
        state = "ready"
    elif endpoint is not None and endpoint.status == "staged":
        endpoint_updated = endpoint.updated_at
        job_updated = job.updated_at if job is not None else None
        if (
            job is not None
            and job.status == "failed"
            and job_updated is not None
            and (
                endpoint_updated is None
                or _as_utc(job_updated) > _as_utc(endpoint_updated)
            )
        ):
            state = "failed"
            error_source = job.error_message
        else:
            state = "failed" if endpoint.last_error_code else "awaiting_test"
            error_source = endpoint.last_error_code
    elif job is not None and job.status == "failed":
        state = "failed"
        error_source = job.error_message
    elif not worker.ssh_access_configured:
        state = "needs_preflight"
    elif not worker.ssh_host_key or not worker.ssh_host_key_fingerprint:
        state = "needs_trust"
    else:
        state = "ready_to_start"
    error = None
    if state == "failed":
        error = (
            error_source
            if error_source in SAFE_ONBOARDING_ERRORS
            else "vpn_onboarding_health_failed"
        )
    return VpnNodeOnboardingView(
        worker_id=worker_id,
        state=state,
        fingerprint=worker.ssh_host_key_fingerprint,
        job_id=job.id if job is not None else None,
        endpoint_id=endpoint.id if endpoint is not None else None,
        error_code=error,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _fail(code: str) -> None:
    error = VpnNodeOnboardingError(code)
    try:
        raise error from None
    finally:
        error.__context__ = None


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


def _endpoint_receipt(endpoint: VpnEndpoint, state: str) -> EndpointInstallReceipt:
    try:
        return make_endpoint_receipt(
            state=state,
            worker_id=endpoint.worker_id,
            inbound_id=endpoint.inbound_id,
            public_host=endpoint.public_host,
            server_name=endpoint.server_name,
            public_key=endpoint.public_key,
            short_id=endpoint.short_id,
        )
    except (TypeError, ValueError):
        _fail("vpn_onboarding_reconciliation_required")


def _acceptance_identity(
    worker: WorkerNode,
    endpoint: VpnEndpoint,
    observed: EndpointInstallReceipt,
    secret: str,
) -> tuple[UUID, str]:
    if type(secret) is not str or not secret or secret == "change-me-session-secret":
        _fail("vpn_onboarding_failed")
    digest = hmac.new(
        secret.encode("utf-8"),
        (
            f"vpn-onboarding-test:{endpoint.worker_id}:{endpoint.inbound_id}:"
            f"{observed.receipt_digest}"
        ).encode("ascii"),
        hashlib.sha256,
    ).digest()
    return UUID(bytes=digest[:16], version=4), f"veltrix-check-{worker.id}@local"


def _acceptance_marker_key(endpoint: VpnEndpoint) -> str:
    return f"{_ACCEPTANCE_PROFILE_KEY}:{endpoint.worker_id}:{endpoint.inbound_id}"


def _acceptance_marker_value(endpoint: VpnEndpoint, receipt: EndpointInstallReceipt) -> str:
    return f"{endpoint.id}:{receipt.receipt_digest}"


async def _lock_acceptance_context(
    session: AsyncSession,
    worker_id: int,
) -> tuple[WorkerNode, VpnEndpoint]:
    if type(worker_id) is not int or not 1 <= worker_id < 2**63:
        _fail("vpn_onboarding_worker_unavailable")
    worker = await lock_vpn_worker(session, worker_id)
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
    if (
        worker.id in await active_attack_worker_ids(session)
        or worker.id in await active_vpn_control_worker_ids(session)
        or worker.id in await active_vpn_maintenance_worker_ids(session)
    ):
        _fail("vpn_onboarding_worker_busy")
    endpoints = list(
        (
            await session.scalars(
                select(VpnEndpoint)
                .where(
                    VpnEndpoint.worker_id == worker.id,
                    VpnEndpoint.status.in_(("staged", "ready")),
                )
                .order_by(VpnEndpoint.id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
        ).all()
    )
    if len(endpoints) != 1:
        _fail("vpn_onboarding_reconciliation_required")
    endpoint = endpoints[0]
    if (
        endpoint.security != "reality"
        or endpoint.last_error_code is not None
        or endpoint.health_checked_at is None
        or worker.vpn_last_error is not None
        or worker.vpn_last_checked_at is None
        or worker.vpn_inbound_id != endpoint.inbound_id
        or worker.vpn_public_host != endpoint.public_host
        or worker.vpn_inbound_port != endpoint.port
        or worker.vpn_inbound_protocol != endpoint.protocol
        or worker.vpn_inbound_transport != endpoint.transport
        or worker.vpn_inbound_security != endpoint.security
    ):
        _fail("vpn_onboarding_health_failed")
    return worker, endpoint


def _load_pinned_snapshot(worker: WorkerNode, known_hosts_path: Path):
    path = Path(known_hosts_path)
    if not path.is_absolute():
        _fail("vpn_onboarding_failed")
    try:
        snapshot = load_transport_snapshot(worker, path)
    except VpnNodeTransportError:
        _fail("vpn_onboarding_host_key_changed")
    if not _pin_matches(snapshot, worker):
        _fail("vpn_onboarding_host_key_changed")
    return snapshot


def _install_request(
    endpoint: VpnEndpoint,
    action: Literal["inspect", "add_acceptance_client", "remove_acceptance_client"],
    *,
    receipt: EndpointInstallReceipt | None = None,
    acceptance_uuid: UUID | None = None,
    acceptance_email: str | None = None,
) -> EndpointInstallRequest:
    return EndpointInstallRequest(
        action=action,
        worker_id=endpoint.worker_id,
        public_host=endpoint.public_host,
        server_name=endpoint.server_name,
        short_id=endpoint.short_id,
        inbound_id=receipt.inbound_id if receipt is not None else None,
        receipt_digest=receipt.receipt_digest if receipt is not None else None,
        acceptance_uuid=acceptance_uuid,
        acceptance_email=acceptance_email,
    )


async def _exact_endpoint_action(
    snapshot,
    request: EndpointInstallRequest,
    expected: EndpointInstallReceipt,
) -> EndpointInstallReceipt:
    try:
        receipt = await execute_endpoint_installer_over_ssh(
            snapshot,
            request,
            controlled_worker_id=request.worker_id,
        )
    except EndpointInstallerDeploymentError:
        _fail("vpn_onboarding_reconciliation_required")
    except Exception:  # noqa: BLE001 - remote details must not cross the boundary
        _fail("vpn_onboarding_failed")
    if receipt != expected:
        _fail("vpn_onboarding_reconciliation_required")
    return receipt


async def create_vpn_node_test_profile(
    session: AsyncSession,
    worker_id: int,
    *,
    secret: str,
    known_hosts_path: Path,
) -> str:
    """Add or recover one deterministic acceptance client and return only its URI."""
    worker, endpoint = await _lock_acceptance_context(session, worker_id)
    if endpoint.status != "staged" or worker.vpn_runtime_status != "awaiting_test":
        _fail("vpn_onboarding_reconciliation_required")
    observed = _endpoint_receipt(endpoint, "observed")
    acceptance_uuid, acceptance_email = _acceptance_identity(
        worker, endpoint, observed, secret
    )
    marker_key = _acceptance_marker_key(endpoint)
    marker_value = _acceptance_marker_value(endpoint, observed)
    marker = await session.scalar(
        select(AppSetting).where(AppSetting.key == marker_key).with_for_update()
    )
    if marker is not None and marker.value != marker_value:
        _fail("vpn_onboarding_reconciliation_required")

    snapshot = _load_pinned_snapshot(worker, known_hosts_path)
    inspect_request = _install_request(endpoint, "inspect")
    try:
        inspected = await execute_endpoint_installer_over_ssh(
            snapshot,
            inspect_request,
            controlled_worker_id=worker.id,
        )
    except EndpointInstallerDeploymentError:
        if marker is None:
            _fail("vpn_onboarding_reconciliation_required")
        inspected = None
    except Exception:  # noqa: BLE001 - remote details must not cross the boundary
        _fail("vpn_onboarding_failed")
    if inspected is not None and inspected != observed:
        _fail("vpn_onboarding_reconciliation_required")

    add_request = _install_request(
        endpoint,
        "add_acceptance_client",
        receipt=observed,
        acceptance_uuid=acceptance_uuid,
        acceptance_email=acceptance_email,
    )
    await _exact_endpoint_action(
        snapshot,
        add_request,
        _endpoint_receipt(endpoint, "acceptance_client_present"),
    )

    if marker is None:
        session.add(AppSetting(key=marker_key, value=marker_value))
    await session.flush()
    return build_reality_config_uri(_target(endpoint), acceptance_uuid)


def _attestation_digest(
    *,
    worker_id: int,
    endpoint_id: int,
    admin_id: int,
    receipt_digest: str,
    checked_at: datetime,
) -> str:
    payload = {
        "worker_id": worker_id,
        "endpoint_id": endpoint_id,
        "admin_id": admin_id,
        "receipt_digest": receipt_digest,
        "checked_at": checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


async def _already_confirmed(
    session: AsyncSession,
    worker: WorkerNode,
    endpoint: VpnEndpoint,
    release_id: str,
) -> bool:
    if (
        endpoint.status != "ready"
        or endpoint.verified_at is None
        or endpoint.external_verified_at is None
        or endpoint.external_config_fingerprint
        != public_endpoint_fingerprint(_target(endpoint))
        or worker.vpn_runtime_status != "ready"
    ):
        return False
    evidence_key = (
        f"{VPN_ENDPOINT_ACCEPTANCE_KEY}:{endpoint.worker_id}:{endpoint.inbound_id}"
    )
    settings = {
        setting.key: setting
        for setting in (
            await session.scalars(
                select(AppSetting).where(
                    AppSetting.key.in_(
                        (VPN_FRIEND_BETA_RELEASE_READY_KEY, evidence_key)
                    )
                )
            )
        ).all()
    }
    release = settings.get(VPN_FRIEND_BETA_RELEASE_READY_KEY)
    return (
        release is not None
        and release.value == release_id
        and evidence_key in settings
    )


async def confirm_vpn_node_test_profile(
    session: AsyncSession,
    worker_id: int,
    *,
    admin_id: int,
    release_id: str,
    secret: str,
    known_hosts_path: Path,
) -> VpnEndpoint:
    """Remove the exact test client, promote atomically, and verify fresh health."""
    if (
        type(admin_id) is not int
        or not 1 <= admin_id < 2**63
        or type(release_id) is not str
        or re.fullmatch(r"[0-9a-f]{64}", release_id) is None
    ):
        _fail("vpn_onboarding_failed")
    worker, endpoint = await _lock_acceptance_context(session, worker_id)
    if await _already_confirmed(session, worker, endpoint, release_id):
        return endpoint
    if endpoint.status != "staged" or worker.vpn_runtime_status != "awaiting_test":
        _fail("vpn_onboarding_reconciliation_required")

    observed = _endpoint_receipt(endpoint, "observed")
    acceptance_uuid, acceptance_email = _acceptance_identity(
        worker, endpoint, observed, secret
    )
    marker = await session.scalar(
        select(AppSetting)
        .where(AppSetting.key == _acceptance_marker_key(endpoint))
        .with_for_update()
    )
    if marker is None or marker.value != _acceptance_marker_value(endpoint, observed):
        _fail("vpn_onboarding_reconciliation_required")
    snapshot = _load_pinned_snapshot(worker, known_hosts_path)
    remove_request = _install_request(
        endpoint,
        "remove_acceptance_client",
        receipt=observed,
        acceptance_uuid=acceptance_uuid,
        acceptance_email=acceptance_email,
    )
    expected_removed = _endpoint_receipt(endpoint, "acceptance_client_removed")
    try:
        removed = await execute_endpoint_installer_over_ssh(
            snapshot,
            remove_request,
            controlled_worker_id=worker.id,
        )
    except EndpointInstallerDeploymentError:
        _fail("vpn_onboarding_reconciliation_required")
    except Exception:  # noqa: BLE001 - remote details must not cross the boundary
        _fail("vpn_onboarding_failed")
    if removed != expected_removed:
        _fail("vpn_onboarding_reconciliation_required")
    clean_receipt = removed

    checked_at = utcnow()
    if (
        not isinstance(checked_at, datetime)
        or checked_at.tzinfo is None
        or checked_at.utcoffset() is None
        or checked_at.utcoffset().total_seconds() != 0
    ):
        _fail("vpn_onboarding_failed")
    acceptance = ExternalEndpointAcceptance(
        release_id=release_id,
        receipt_digest=clean_receipt.receipt_digest,
        evidence_digest=_attestation_digest(
            worker_id=worker.id,
            endpoint_id=endpoint.id,
            admin_id=admin_id,
            receipt_digest=clean_receipt.receipt_digest,
            checked_at=checked_at,
        ),
        checked_at=checked_at,
    )
    savepoint = await session.begin_nested()
    try:
        endpoint = await promote_protected_endpoint(
            session,
            clean_receipt,
            acceptance,
            controlled_worker_id=worker.id,
            now=checked_at,
        )
        checked_at_ms = int(checked_at.timestamp() * 1000)
        try:
            health = await execute_vpn_node_health_over_ssh(
                snapshot,
                VpnNodeHealthRequest(
                    worker_id=worker.id,
                    target=_target(endpoint),
                    checked_at_ms=checked_at_ms,
                ),
                now_ms=checked_at_ms,
            )
        except VpnNodeTransportError:
            _fail("vpn_onboarding_health_failed")
        except Exception:  # noqa: BLE001 - remote details must not cross the boundary
            _fail("vpn_onboarding_health_failed")
        if health != VpnNodeHealthReceipt("healthy", None, "running"):
            _fail(
                health.error_code
                if health.error_code in VPN_NODE_HEALTH_ERROR_CODES
                else "vpn_onboarding_health_failed"
            )
        endpoint.health_checked_at = checked_at
        endpoint.external_verified_at = checked_at
        endpoint.external_config_fingerprint = public_endpoint_fingerprint(
            _target(endpoint)
        )
        endpoint.last_error_code = None
        worker.vpn_runtime_status = "ready"
        worker.vpn_last_checked_at = checked_at
        worker.vpn_last_error = None
        await session.flush()
        await savepoint.commit()
        return endpoint
    except BaseException as error:
        if savepoint.is_active:
            await savepoint.rollback()
        await session.refresh(endpoint)
        if isinstance(error, VpnNodeOnboardingError):
            raise
        if isinstance(error, EndpointRegistrationError):
            _fail(error.code)
        if isinstance(error, Exception):
            _fail("vpn_onboarding_failed")
        raise


async def _persist_failure(session_factory, job_id: int, code: str) -> None:
    safe_code = code if code in SAFE_ONBOARDING_ERRORS else "vpn_onboarding_failed"
    async with session_factory() as session:
        worker_id = await session.scalar(
            select(WorkerMaintenanceJob.worker_id).where(
                WorkerMaintenanceJob.id == job_id
            )
        )
        if worker_id is None:
            return
        worker = await lock_vpn_worker(session, worker_id)
        if worker is None:
            return
        job = await session.scalar(
            select(WorkerMaintenanceJob)
            .where(WorkerMaintenanceJob.id == job_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
        if job is None or job.action != "vpn_onboard" or job.status == "succeeded":
            return
        failed_at = utcnow()
        job.status = "failed"
        job.error_message = safe_code
        job.log = None
        job.finished_at = failed_at
        job.updated_at = failed_at
        worker.vpn_runtime_status = "error"
        worker.vpn_last_error = safe_code
        worker.vpn_last_checked_at = failed_at
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
            job_hint = await session.get(WorkerMaintenanceJob, job_id)
            if job_hint is None or job_hint.action != "vpn_onboard":
                return
            worker = await lock_vpn_worker(session, job_hint.worker_id)
            if worker is None:
                _fail("vpn_onboarding_worker_unavailable")
            job = await session.scalar(
                select(WorkerMaintenanceJob)
                .where(WorkerMaintenanceJob.id == job_id)
                .execution_options(populate_existing=True)
                .with_for_update()
            )
            if job is None or job.action != "vpn_onboard" or job.status == "succeeded":
                return
            if (
                worker.archived_at is not None
                or worker.status != "ready"
                or worker.is_enabled is not True
                or worker.vpn_enabled is not True
                or worker.vpn_role == "none"
                or not worker.ssh_access_configured
                or not worker.ssh_host_key
                or not worker.ssh_host_key_fingerprint
            ):
                _fail("vpn_onboarding_worker_unavailable")

            settings = get_settings()
            public_host = worker.vpn_public_host or worker.ssh_host or worker.ip_address
            if not public_host:
                _fail("vpn_onboarding_worker_unavailable")
            short_id = _short_id(settings.session_secret_key, worker.id, public_host)
            ready_endpoint = await session.scalar(
                select(VpnEndpoint)
                .where(
                    VpnEndpoint.worker_id == worker.id,
                    VpnEndpoint.public_host == public_host,
                    VpnEndpoint.port == PORT,
                    VpnEndpoint.protocol == PROTOCOL,
                    VpnEndpoint.transport == TRANSPORT,
                    VpnEndpoint.security == SECURITY,
                    VpnEndpoint.server_name == REALITY_SERVER_NAME,
                    VpnEndpoint.short_id == short_id,
                    VpnEndpoint.fingerprint == FINGERPRINT,
                    VpnEndpoint.flow == FLOW,
                    VpnEndpoint.status == "ready",
                    VpnEndpoint.verified_at.is_not(None),
                    VpnEndpoint.last_error_code.is_(None),
                )
                .order_by(VpnEndpoint.id.asc())
                .execution_options(populate_existing=True)
                .limit(1)
            )
            if ready_endpoint is not None:
                completed_at = utcnow()
                job.status = "succeeded"
                job.log = "vpn_onboarding_ready"
                job.error_message = None
                job.started_at = job.started_at or completed_at
                job.finished_at = completed_at
                job.updated_at = completed_at
                await session.commit()
                return

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

            job.status = "running"
            job.error_message = None
            job.log = None
            job.started_at = job.started_at or utcnow()
            job.updated_at = utcnow()
            worker.vpn_runtime_status = "onboarding"
            worker.vpn_last_error = None
            known_hosts_path = Path(settings.vpn_control_known_hosts_path)
            if not known_hosts_path.is_absolute():
                _fail("vpn_onboarding_failed")
            try:
                snapshot = load_transport_snapshot(worker, known_hosts_path)
            except VpnNodeTransportError:
                _fail("vpn_onboarding_host_key_changed")
            if not _pin_matches(snapshot, worker):
                _fail("vpn_onboarding_host_key_changed")

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
                endpoint_was_ready = endpoint.status == "ready"
                if endpoint.max_active_profiles is None:
                    endpoint.max_active_profiles = 20
                checked_at = datetime.now(UTC)
                checked_at_ms = int(checked_at.timestamp() * 1000)
                try:
                    health = await health_transport(
                        snapshot,
                        VpnNodeHealthRequest(
                            worker_id=worker.id,
                            target=_target(endpoint),
                            checked_at_ms=checked_at_ms,
                        ),
                        now_ms=checked_at_ms,
                    )
                except VpnNodeTransportError:
                    _fail("vpn_onboarding_health_failed")
                if health.state != "healthy":
                    _fail(health.error_code or "vpn_onboarding_health_failed")
                endpoint.status = "ready" if endpoint_was_ready else "staged"
                endpoint.health_checked_at = checked_at
                endpoint.last_error_code = None
                worker.vpn_inbound_id = endpoint.inbound_id
                worker.vpn_public_host = endpoint.public_host
                worker.vpn_inbound_port = endpoint.port
                worker.vpn_inbound_protocol = endpoint.protocol
                worker.vpn_inbound_transport = endpoint.transport
                worker.vpn_inbound_security = endpoint.security
                worker.vpn_listener_status = "listening"
                worker.vpn_runtime_status = (
                    "ready" if endpoint_was_ready else "awaiting_test"
                )
                worker.vpn_last_checked_at = checked_at
                worker.vpn_last_error = None
                job.status = "succeeded"
                job.log = (
                    "vpn_onboarding_ready"
                    if endpoint_was_ready
                    else "vpn_onboarding_awaiting_test"
                )
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
