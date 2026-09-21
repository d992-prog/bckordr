from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VpnAccessKey, VpnEndpoint, WorkerNode
from app.services.vpn_endpoint_types import (
    EndpointOperation as EndpointOperation,
    VpnEndpointError as VpnEndpointError,
    VpnEndpointTarget as VpnEndpointTarget,
)


def require_bound_client_uuid(access_key: VpnAccessKey) -> UUID:
    try:
        return UUID(access_key.external_uuid or "")
    except (ValueError, TypeError, AttributeError):
        raise VpnEndpointError("vpn_endpoint_client_identity_invalid") from None


async def resolve_recorded_endpoint(
    db: AsyncSession,
    access_key: VpnAccessKey,
    *,
    worker: WorkerNode | None,
    operation: EndpointOperation,
) -> VpnEndpointTarget:
    """Resolve recorded endpoint state only.

    The caller separately authorizes the operation, takes required locks, and
    remotely verifies the endpoint before any mutation.
    """
    if operation not in ("provision", "suspend", "revoke"):
        raise VpnEndpointError("vpn_endpoint_operation_invalid")
    if access_key.endpoint_id is None:
        raise VpnEndpointError("vpn_endpoint_binding_required")
    if worker is None or worker.id != access_key.worker_id:
        raise VpnEndpointError("vpn_endpoint_worker_mismatch")
    if worker.archived_at is not None:
        raise VpnEndpointError("vpn_endpoint_worker_archived")

    statement = (
        select(VpnEndpoint)
        .where(VpnEndpoint.id == access_key.endpoint_id)
        .execution_options(populate_existing=True)
    )
    with db.no_autoflush:
        endpoint = await db.scalar(statement)

    if endpoint is None:
        raise VpnEndpointError("vpn_endpoint_not_found")
    if endpoint.worker_id != worker.id:
        raise VpnEndpointError("vpn_endpoint_worker_mismatch")
    if endpoint.verified_at is None:
        raise VpnEndpointError("vpn_endpoint_unverified")
    if endpoint.status not in ("ready", "draining", "disabled"):
        raise VpnEndpointError("vpn_endpoint_unavailable")

    if operation == "provision":
        if access_key.status in ("revoked", "pending_revoke") or access_key.revoked_at is not None:
            raise VpnEndpointError("vpn_endpoint_key_revoked")
        if endpoint.status == "disabled":
            raise VpnEndpointError("vpn_endpoint_disabled")
        if endpoint.status == "draining" and not access_key.config_uri:
            raise VpnEndpointError("vpn_endpoint_existing_profile_required")

    if endpoint.security not in ("none", "tls", "reality") or (
        endpoint.status == "ready" and endpoint.security == "none"
    ):
        raise VpnEndpointError("vpn_endpoint_transport_invalid")
    if endpoint.protocol != "vless" or endpoint.transport not in ("tcp", "raw"):
        raise VpnEndpointError("vpn_endpoint_transport_unsupported")

    public_host = endpoint.public_host
    inbound_id = endpoint.inbound_id
    port = endpoint.port
    if (
        not isinstance(inbound_id, int)
        or isinstance(inbound_id, bool)
        or inbound_id <= 0
        or not isinstance(port, int)
        or isinstance(port, bool)
        or not 1 <= port <= 65535
        or not isinstance(public_host, str)
        or not public_host
        or public_host != public_host.strip()
        or any(character.isspace() for character in public_host)
        or any(character in public_host for character in "/?#@")
    ):
        raise VpnEndpointError("vpn_endpoint_address_invalid")

    if access_key.protocol != endpoint.protocol:
        raise VpnEndpointError("vpn_endpoint_protocol_mismatch")
    require_bound_client_uuid(access_key)

    return VpnEndpointTarget(
        endpoint_id=endpoint.id,
        worker_id=endpoint.worker_id,
        inbound_id=inbound_id,
        public_host=public_host,
        port=port,
        protocol=endpoint.protocol,
        transport=endpoint.transport,
        security=endpoint.security,
        server_name=endpoint.server_name,
        public_key=endpoint.public_key,
        short_id=endpoint.short_id,
        fingerprint=endpoint.fingerprint,
        flow=endpoint.flow,
    )
