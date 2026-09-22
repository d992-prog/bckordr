"""Transactional control-side registration of the protected REALITY endpoint."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
import json
import re

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting, VpnEndpoint, WorkerNode
from app.services.vpn_reality_endpoint_installer import (
    EndpointInstallReceipt,
    parse_install_receipt,
)


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
CONTROLLED_WORKER_ID = 15
VPN_FRIEND_BETA_RELEASE_READY_KEY = "vpn_friend_beta_release_ready_v1"
VPN_ENDPOINT_ACCEPTANCE_KEY = "vpn_endpoint_external_acceptance_v1"
MAX_ACCEPTANCE_AGE = timedelta(minutes=30)
MAX_ACCEPTANCE_CLOCK_SKEW = timedelta(minutes=1)


class EndpointRegistrationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _invalid() -> None:
    error = EndpointRegistrationError("vpn_endpoint_acceptance_invalid")
    try:
        raise error from None
    finally:
        error.__context__ = None


def _fail(code: str) -> None:
    error = EndpointRegistrationError(code)
    try:
        raise error from None
    finally:
        error.__context__ = None


@dataclass(frozen=True, slots=True)
class ExternalEndpointAcceptance:
    release_id: str
    receipt_digest: str
    evidence_digest: str
    checked_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.release_id, str)
            or _DIGEST.fullmatch(self.release_id) is None
            or not isinstance(self.receipt_digest, str)
            or _DIGEST.fullmatch(self.receipt_digest) is None
            or not isinstance(self.evidence_digest, str)
            or _DIGEST.fullmatch(self.evidence_digest) is None
            or not isinstance(self.checked_at, datetime)
            or self.checked_at.tzinfo is None
            or self.checked_at.utcoffset() is None
            or self.checked_at.utcoffset() != timedelta(0)
        ):
            _invalid()


def _validated_receipt(receipt: object) -> EndpointInstallReceipt:
    if not isinstance(receipt, EndpointInstallReceipt):
        _fail("vpn_endpoint_registration_receipt_invalid")
    try:
        return parse_install_receipt(asdict(receipt))
    except ValueError:
        _fail("vpn_endpoint_registration_receipt_invalid")


def _endpoint_values(receipt: EndpointInstallReceipt) -> dict[str, object]:
    return {
        "worker_id": receipt.worker_id,
        "inbound_id": receipt.inbound_id,
        "public_host": receipt.public_host,
        "port": receipt.port,
        "protocol": receipt.protocol,
        "transport": receipt.transport,
        "security": receipt.security,
        "server_name": receipt.server_name,
        "public_key": receipt.public_key,
        "short_id": receipt.short_id,
        "fingerprint": receipt.fingerprint,
        "flow": receipt.flow,
    }


def _endpoint_matches(endpoint: VpnEndpoint, receipt: EndpointInstallReceipt) -> bool:
    return all(getattr(endpoint, key) == value for key, value in _endpoint_values(receipt).items())


async def _lock_worker(session: AsyncSession, worker_id: int) -> WorkerNode:
    if worker_id != CONTROLLED_WORKER_ID:
        _fail("vpn_endpoint_registration_worker_unavailable")
    with session.no_autoflush:
        worker = await session.scalar(
            select(WorkerNode)
            .where(WorkerNode.id == worker_id)
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    if (
        worker is None
        or worker.archived_at is not None
        or worker.status != "ready"
        or worker.is_enabled is not True
        or worker.vpn_enabled is not True
        or worker.vpn_role != "vpn_node"
    ):
        _fail("vpn_endpoint_registration_worker_unavailable")
    return worker


async def _lock_endpoints(session: AsyncSession) -> list[VpnEndpoint]:
    with session.no_autoflush:
        result = await session.execute(
            select(VpnEndpoint)
            .order_by(VpnEndpoint.id.asc())
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    return list(result.scalars().all())


def _find_endpoint(
    endpoints: list[VpnEndpoint], receipt: EndpointInstallReceipt
) -> VpnEndpoint | None:
    exact = [endpoint for endpoint in endpoints if _endpoint_matches(endpoint, receipt)]
    if len(exact) > 1:
        _fail("vpn_endpoint_registration_conflict")
    for endpoint in endpoints:
        if exact and endpoint is exact[0]:
            continue
        if (
            (endpoint.worker_id == receipt.worker_id and endpoint.inbound_id == receipt.inbound_id)
            or (endpoint.worker_id == receipt.worker_id and endpoint.port == receipt.port)
            or (endpoint.status == "ready" and endpoint.security == "reality")
        ):
            _fail("vpn_endpoint_registration_conflict")
    return exact[0] if exact else None


async def stage_protected_endpoint(
    session: AsyncSession,
    receipt: EndpointInstallReceipt,
) -> VpnEndpoint:
    """Stage one exact endpoint; caller owns commit or rollback."""
    receipt = _validated_receipt(receipt)
    if receipt.state not in {
        "staged",
        "already_present",
        "observed",
        "acceptance_client_removed",
    }:
        _fail("vpn_endpoint_registration_not_clean")
    await _lock_worker(session, receipt.worker_id)
    endpoints = await _lock_endpoints(session)
    endpoint = _find_endpoint(endpoints, receipt)
    if endpoint is not None:
        if endpoint.status not in {"staged", "ready"}:
            _fail("vpn_endpoint_registration_conflict")
        return endpoint
    endpoint = VpnEndpoint(**_endpoint_values(receipt), status="staged")
    session.add(endpoint)
    await session.flush()
    return endpoint


def _acceptance_json(
    receipt: EndpointInstallReceipt,
    acceptance: ExternalEndpointAcceptance,
) -> str:
    value = {
        "version": 1,
        "worker_id": receipt.worker_id,
        "inbound_id": receipt.inbound_id,
        "receipt_digest": acceptance.receipt_digest,
        "release_id": acceptance.release_id,
        "evidence_digest": acceptance.evidence_digest,
        "checked_at": acceptance.checked_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


async def _lock_settings(session: AsyncSession) -> dict[str, AppSetting]:
    keys = (VPN_ENDPOINT_ACCEPTANCE_KEY, VPN_FRIEND_BETA_RELEASE_READY_KEY)
    with session.no_autoflush:
        result = await session.execute(
            select(AppSetting)
            .where(AppSetting.key.in_(keys))
            .order_by(AppSetting.key.asc())
            .execution_options(populate_existing=True)
            .with_for_update()
        )
    return {setting.key: setting for setting in result.scalars().all()}


def _set_exact_setting(
    session: AsyncSession,
    settings: dict[str, AppSetting],
    *,
    key: str,
    value: str,
) -> None:
    setting = settings.get(key)
    if setting is None:
        setting = AppSetting(key=key, value=value)
        session.add(setting)
        settings[key] = setting


async def promote_protected_endpoint(
    session: AsyncSession,
    receipt: EndpointInstallReceipt,
    acceptance: ExternalEndpointAcceptance,
    *,
    now: datetime | None = None,
) -> VpnEndpoint:
    """Atomically promote the endpoint, evidence metadata, and release marker."""
    receipt = _validated_receipt(receipt)
    if receipt.state not in {"observed", "acceptance_client_removed"}:
        _fail("vpn_endpoint_registration_not_clean")
    if not isinstance(acceptance, ExternalEndpointAcceptance):
        _fail("vpn_endpoint_acceptance_invalid")
    current = datetime.now(UTC) if now is None else now
    if (
        not isinstance(current, datetime)
        or current.tzinfo is None
        or current.utcoffset() != timedelta(0)
    ):
        _fail("vpn_endpoint_acceptance_invalid")
    if acceptance.receipt_digest != receipt.receipt_digest:
        _fail("vpn_endpoint_registration_acceptance_mismatch")
    stale = (
        acceptance.checked_at < current - MAX_ACCEPTANCE_AGE
        or acceptance.checked_at > current + MAX_ACCEPTANCE_CLOCK_SKEW
    )

    await _lock_worker(session, receipt.worker_id)
    endpoint = _find_endpoint(await _lock_endpoints(session), receipt)
    if endpoint is None or endpoint.status not in {"staged", "ready"}:
        _fail("vpn_endpoint_registration_endpoint_unavailable")
    settings = await _lock_settings(session)
    metadata = _acceptance_json(receipt, acceptance)
    marker_setting = settings.get(VPN_FRIEND_BETA_RELEASE_READY_KEY)
    evidence_setting = settings.get(VPN_ENDPOINT_ACCEPTANCE_KEY)
    if marker_setting is not None and marker_setting.value != acceptance.release_id:
        _fail("vpn_endpoint_registration_release_conflict")
    if evidence_setting is not None and evidence_setting.value != metadata:
        _fail("vpn_endpoint_registration_acceptance_conflict")
    if (
        endpoint.status == "ready"
        and endpoint.verified_at is not None
        and marker_setting is not None
        and evidence_setting is not None
    ):
        return endpoint
    if stale:
        _fail("vpn_endpoint_registration_acceptance_stale")
    _set_exact_setting(
        session,
        settings,
        key=VPN_FRIEND_BETA_RELEASE_READY_KEY,
        value=acceptance.release_id,
    )
    _set_exact_setting(
        session,
        settings,
        key=VPN_ENDPOINT_ACCEPTANCE_KEY,
        value=metadata,
    )
    endpoint.status = "ready"
    endpoint.verified_at = acceptance.checked_at
    endpoint.last_error_code = None
    await session.flush()
    return endpoint
