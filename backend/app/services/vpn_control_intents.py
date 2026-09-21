"""Build immutable endpoint-control requests without performing remote IO."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal
from urllib.parse import quote, urlencode
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.services.vpn_endpoint_types import EndpointOperation, VpnEndpointTarget
from app.services.vpn_node_request import (
    VpnNodeRequest,
    VpnNodeRequestError,
    node_request_digest,
    parse_node_request,
    serialize_node_request,
)
from app.services.vpn_subscription_sync import subscription_key_action


_MAX_INTEGER = 2**63 - 1
_MAX_GENERATION = 2**31 - 1
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_PENDING_STATUS = {
    "provision": "pending_sync",
    "suspend": "pending_suspend",
    "revoke": "pending_revoke",
}
_RECEIPTS = {
    ("observed", None),
    ("failed", "vpn_node_preflight_failed"),
    ("failed", "vpn_node_interrupted_before_mutation"),
    ("uncertain", "vpn_node_mutation_uncertain"),
    ("stale", "vpn_node_operation_stale"),
    ("blocked", "vpn_node_reconciliation_required"),
    ("blocked", "vpn_node_key_revoked"),
}


class _CandidateBusy(Exception):
    pass


class VpnControlIntentError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str) -> None:
    error = VpnControlIntentError(code)
    try:
        raise error from None
    finally:
        error.__context__ = None


def _milliseconds(value: datetime | None, *, required: bool) -> int:
    if value is None:
        if required:
            _fail("vpn_control_creation_time_invalid")
        return 0
    if not isinstance(value, datetime):
        _fail("vpn_control_time_invalid")
    try:
        normalized = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        delta = normalized - _EPOCH
        milliseconds = (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000
    except OverflowError:
        _fail("vpn_control_time_invalid")
    if milliseconds < int(required) or milliseconds > _MAX_INTEGER:
        _fail("vpn_control_time_invalid")
    return milliseconds


def _client_uuid(access_key: VpnAccessKey) -> UUID:
    value = access_key.external_uuid
    if type(value) is not str:
        _fail("vpn_control_client_identity_invalid")
    try:
        parsed = UUID(value)
    except (AttributeError, TypeError, ValueError):
        _fail("vpn_control_client_identity_invalid")
    if str(parsed) != value:
        _fail("vpn_control_client_identity_invalid")
    return parsed


def _target(
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    endpoint: VpnEndpoint,
) -> VpnEndpointTarget:
    if (
        access_key.subscription_id != subscription.id
        or access_key.endpoint_id != endpoint.id
        or access_key.worker_id != endpoint.worker_id
        or access_key.protocol != endpoint.protocol
        or endpoint.verified_at is None
    ):
        _fail("vpn_control_binding_invalid")
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


def build_control_request(
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    endpoint: VpnEndpoint,
    *,
    operation_id: UUID,
    generation: int,
    action: EndpointOperation,
    allow_create: bool,
    allow_shared_restart: bool,
) -> VpnNodeRequest:
    """Derive one validated node request exclusively from persisted records."""
    if type(generation) is not int or not 1 <= generation <= _MAX_GENERATION:
        _fail("vpn_control_generation_invalid")
    if action != "revoke" and (
        access_key.revoke_requested_at is not None
        or access_key.revoked_at is not None
        or access_key.status in ("pending_revoke", "revoked")
    ):
        _fail("vpn_control_revoke_sticky")
    if access_key.verified_client_email is None or access_key.panel_sub_id is None:
        _fail("vpn_control_client_identity_unverified")
    if allow_create and not access_key.panel_sub_id:
        _fail("vpn_control_create_identity_invalid")
    if allow_create and endpoint.security != "reality":
        _fail("vpn_control_endpoint_unsupported")

    traffic_gb = subscription.traffic_limit_gb
    if traffic_gb is None:
        traffic_bytes = 0
    elif type(traffic_gb) is not int or traffic_gb < 0 or traffic_gb > _MAX_INTEGER // 1024**3:
        _fail("vpn_control_policy_overflow")
    else:
        traffic_bytes = traffic_gb * 1024**3

    request = VpnNodeRequest(
        operation_id=operation_id,
        access_key_id=access_key.id,
        generation=generation,
        action=action,
        target=_target(access_key, subscription, endpoint),
        client_uuid=_client_uuid(access_key),
        client_email=access_key.verified_client_email,
        sub_id=access_key.panel_sub_id,
        expires_at_ms=_milliseconds(subscription.expires_at, required=False),
        traffic_limit_bytes=traffic_bytes,
        created_at_ms=_milliseconds(access_key.issued_at or access_key.created_at, required=True),
        allow_create=allow_create,
        allow_shared_restart=allow_shared_restart,
    )
    try:
        serialize_node_request(request)
    except VpnNodeRequestError:
        _fail("vpn_control_endpoint_unsupported")
    return request


def build_control_config_uri(request: VpnNodeRequest) -> str:
    """Build the unlabelled VLESS/REALITY URI represented by a request."""
    try:
        serialize_node_request(request)
    except VpnNodeRequestError:
        _fail("vpn_control_request_invalid")
    target = request.target
    if target.security != "reality":
        _fail("vpn_control_config_uri_unsupported")
    host = target.public_host.encode("idna").decode("ascii")
    if ":" in host:
        host = f"[{host}]"
    query = urlencode(
        {
            "encryption": "none",
            "flow": target.flow,
            "security": "reality",
            "sni": target.server_name,
            "fp": target.fingerprint,
            "pbk": target.public_key,
            "sid": target.short_id,
            "type": target.transport,
        },
        quote_via=quote,
        safe="",
    )
    return f"vless://{quote(str(request.client_uuid), safe='')}@{host}:{target.port}?{query}"


def _locked(statement, *, skip_locked: bool = False):
    return statement.with_for_update(skip_locked=skip_locked).execution_options(
        populate_existing=True
    )


def _current_time(value: datetime | None) -> datetime:
    current = utcnow() if value is None else value
    if not isinstance(current, datetime):
        _fail("vpn_control_time_invalid")
    try:
        return current.replace(tzinfo=UTC) if current.tzinfo is None else current.astimezone(UTC)
    except OverflowError:
        _fail("vpn_control_time_invalid")


def _validate_endpoint_policy(
    access_key: VpnAccessKey,
    endpoint: VpnEndpoint,
    action: EndpointOperation,
) -> bool:
    if endpoint.verified_at is None:
        _fail("vpn_control_endpoint_unverified")
    if endpoint.status not in {"ready", "draining", "disabled"}:
        _fail("vpn_control_endpoint_unavailable")
    if endpoint.protocol != "vless" or endpoint.transport not in {"tcp", "raw"}:
        _fail("vpn_control_endpoint_unsupported")
    if endpoint.security not in {"none", "reality"}:
        _fail("vpn_control_endpoint_unsupported")

    existing_uri = access_key.config_uri
    allow_create = action == "provision" and not (
        isinstance(existing_uri, str) and bool(existing_uri.strip())
    )
    if action == "provision":
        if allow_create:
            if endpoint.status != "ready":
                _fail("vpn_control_endpoint_creation_unavailable")
            if endpoint.security != "reality":
                _fail("vpn_control_endpoint_unsupported")
        elif endpoint.status not in {"ready", "draining"}:
            _fail("vpn_control_endpoint_resume_unavailable")
    return allow_create


async def stage_vpn_control_operation(
    db: AsyncSession,
    access_key_id: int,
    action: EndpointOperation,
    *,
    now: datetime | None = None,
) -> VpnControlOperation:
    """Lock authoritative policy rows and flush one immutable queued intent."""
    if type(access_key_id) is not int or access_key_id <= 0:
        _fail("vpn_control_access_key_invalid")
    if action not in ("provision", "suspend", "revoke"):
        _fail("vpn_control_action_invalid")
    current = _current_time(now)
    if db.sync_session.new or db.sync_session.dirty or db.sync_session.deleted:
        _fail("vpn_control_unflushed_state")

    with db.no_autoflush:
        customer_id = await db.scalar(
            select(VpnSubscription.customer_id)
            .join(VpnAccessKey, VpnAccessKey.subscription_id == VpnSubscription.id)
            .where(VpnAccessKey.id == access_key_id)
        )
        if customer_id is None:
            _fail("vpn_control_access_key_not_found")

        customer = await db.scalar(
            _locked(select(VpnCustomer).where(VpnCustomer.id == customer_id))
        )
        if customer is None:
            _fail("vpn_control_binding_changed")

        subscriptions = (
            await db.scalars(
                _locked(
                    select(VpnSubscription)
                    .where(VpnSubscription.customer_id == customer.id)
                    .order_by(VpnSubscription.id)
                )
            )
        ).all()
        subscription_by_id = {subscription.id: subscription for subscription in subscriptions}
        subscription_ids = tuple(subscription_by_id)
        if not subscription_ids:
            _fail("vpn_control_binding_changed")

        access_keys = (
            await db.scalars(
                _locked(
                    select(VpnAccessKey)
                    .where(VpnAccessKey.subscription_id.in_(subscription_ids))
                    .order_by(VpnAccessKey.id)
                )
            )
        ).all()
        access_key = next((key for key in access_keys if key.id == access_key_id), None)
        if access_key is None:
            _fail("vpn_control_binding_changed")
        subscription = subscription_by_id.get(access_key.subscription_id)
        if subscription is None or subscription.customer_id != customer.id:
            _fail("vpn_control_binding_changed")
        if access_key.endpoint_id is None:
            _fail("vpn_control_binding_required")
        if access_key.worker_id is None:
            _fail("vpn_control_binding_invalid")

        worker = await db.scalar(
            _locked(select(WorkerNode).where(WorkerNode.id == access_key.worker_id))
        )
        if worker is None or worker.archived_at is not None:
            _fail("vpn_control_worker_unavailable")

        endpoint = await db.scalar(
            _locked(select(VpnEndpoint).where(VpnEndpoint.id == access_key.endpoint_id))
        )
        if (
            endpoint is None
            or endpoint.worker_id != worker.id
            or access_key.worker_id != worker.id
            or access_key.endpoint_id != endpoint.id
        ):
            _fail("vpn_control_binding_invalid")

        prior_operations = (
            await db.scalars(
                _locked(
                    select(VpnControlOperation)
                    .where(VpnControlOperation.access_key_id == access_key.id)
                    .order_by(VpnControlOperation.generation, VpnControlOperation.id)
                )
            )
        ).all()

    sticky_revoke = (
        access_key.revoke_requested_at is not None
        or access_key.revoked_at is not None
        or access_key.status in {"pending_revoke", "revoked"}
    )
    if sticky_revoke and action != "revoke":
        _fail("vpn_control_revoke_sticky")

    desired = "revoke" if sticky_revoke or action == "revoke" else {
        "sync": "provision",
        "suspend": "suspend",
        "revoke": "revoke",
    }[subscription_key_action(subscription, customer, current)]
    if action != desired:
        _fail(
            {
                "provision": "vpn_control_policy_requires_provision",
                "suspend": "vpn_control_policy_requires_suspend",
                "revoke": "vpn_control_policy_requires_revoke",
            }[desired]
        )

    allow_create = _validate_endpoint_policy(access_key, endpoint, action)
    if (
        type(access_key.operation_generation) is not int
        or not 0 <= access_key.operation_generation < _MAX_GENERATION
    ):
        _fail("vpn_control_generation_invalid")
    generation = access_key.operation_generation + 1
    operation_uuid = uuid4()
    request = build_control_request(
        access_key,
        subscription,
        endpoint,
        operation_id=operation_uuid,
        generation=generation,
        action=action,
        allow_create=allow_create,
        allow_shared_restart=action in {"suspend", "revoke"},
    )
    snapshot = serialize_node_request(request)

    if action == "revoke" and access_key.revoke_requested_at is None:
        access_key.revoke_requested_at = current
    for prior in prior_operations:
        if prior.state == "queued" and prior.generation < generation:
            prior.state = "superseded"
            prior.finished_at = current
            prior.error_code = "vpn_control_superseded"
            prior.updated_at = current

    access_key.operation_generation = generation
    access_key.status = {
        "provision": "pending_sync",
        "suspend": "pending_suspend",
        "revoke": "pending_revoke",
    }[action]
    access_key.last_error = None
    access_key.updated_at = current
    operation = VpnControlOperation(
        id=str(operation_uuid),
        access_key_id=access_key.id,
        worker_id=worker.id,
        endpoint_id=endpoint.id,
        generation=generation,
        action=action,
        request_snapshot=snapshot,
        request_digest=node_request_digest(request),
        state="queued",
        created_at=current,
        updated_at=current,
    )
    db.add(operation)
    await db.flush()
    return operation


def _uuid(value: object, code: str) -> UUID:
    if not isinstance(value, UUID) or value.int == 0:
        _fail(code)
    return value


def _desired_action(
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    customer: VpnCustomer,
    now: datetime,
) -> EndpointOperation:
    if (
        access_key.revoke_requested_at is not None
        or access_key.revoked_at is not None
        or access_key.status in {"pending_revoke", "revoked"}
    ):
        return "revoke"
    return {
        "sync": "provision",
        "suspend": "suspend",
        "revoke": "revoke",
    }[subscription_key_action(subscription, customer, now)]


def _operation_request(operation: VpnControlOperation) -> VpnNodeRequest:
    try:
        operation_id = UUID(operation.id)
        request = parse_node_request(operation.request_snapshot)
    except (TypeError, ValueError, VpnNodeRequestError):
        _fail("vpn_control_request_invalid")
    if (
        str(operation_id) != operation.id
        or request.operation_id != operation_id
        or request.access_key_id != operation.access_key_id
        or request.generation != operation.generation
        or request.action != operation.action
        or request.target.worker_id != operation.worker_id
        or request.target.endpoint_id != operation.endpoint_id
        or node_request_digest(request) != operation.request_digest
    ):
        _fail("vpn_control_request_invalid")
    return request


def _intent_matches(
    operation: VpnControlOperation,
    request: VpnNodeRequest,
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    customer: VpnCustomer,
    endpoint: VpnEndpoint,
    now: datetime,
) -> bool:
    if not (
        operation.generation == access_key.operation_generation
        and operation.action == _desired_action(access_key, subscription, customer, now)
        and access_key.status == _PENDING_STATUS[operation.action]
        and access_key.subscription_id == subscription.id
        and access_key.worker_id == operation.worker_id == endpoint.worker_id
        and access_key.endpoint_id == operation.endpoint_id == endpoint.id
    ):
        return False
    try:
        expected = build_control_request(
            access_key,
            subscription,
            endpoint,
            operation_id=request.operation_id,
            generation=operation.generation,
            action=operation.action,
            allow_create=_validate_endpoint_policy(
                access_key,
                endpoint,
                operation.action,
            ),
            allow_shared_restart=operation.action in {"suspend", "revoke"},
        )
    except VpnControlIntentError:
        return False
    return (
        serialize_node_request(expected) == operation.request_snapshot
        and node_request_digest(expected) == operation.request_digest
    )


def _require_complete_context(complete: bool, *, skip_locked: bool) -> None:
    if complete:
        return
    if skip_locked:
        raise _CandidateBusy
    _fail("vpn_control_binding_changed")


async def _lock_context(
    db: AsyncSession,
    *,
    access_key_id: int,
    worker_id: int,
    endpoint_id: int,
    skip_locked: bool = False,
) -> tuple[VpnCustomer, VpnSubscription, VpnAccessKey, WorkerNode, VpnEndpoint] | None:
    customer_id = await db.scalar(
        select(VpnSubscription.customer_id)
        .join(VpnAccessKey, VpnAccessKey.subscription_id == VpnSubscription.id)
        .where(VpnAccessKey.id == access_key_id)
    )
    if customer_id is None:
        _fail("vpn_control_binding_changed")
    customer = await db.scalar(
        _locked(
            select(VpnCustomer).where(VpnCustomer.id == customer_id),
            skip_locked=skip_locked,
        )
    )
    if customer is None:
        if skip_locked:
            return None
        _fail("vpn_control_binding_changed")
    subscription_ids = tuple(
        await db.scalars(
            select(VpnSubscription.id)
            .where(VpnSubscription.customer_id == customer.id)
            .order_by(VpnSubscription.id)
        )
    )
    _require_complete_context(bool(subscription_ids), skip_locked=skip_locked)
    subscriptions = (
        await db.scalars(
            _locked(
                select(VpnSubscription)
                .where(VpnSubscription.id.in_(subscription_ids))
                .order_by(VpnSubscription.id),
                skip_locked=skip_locked,
            )
        )
    ).all()
    _require_complete_context(
        tuple(subscription.id for subscription in subscriptions) == subscription_ids
        and all(subscription.customer_id == customer.id for subscription in subscriptions),
        skip_locked=skip_locked,
    )
    subscription_by_id = {subscription.id: subscription for subscription in subscriptions}
    access_key_ids = tuple(
        await db.scalars(
            select(VpnAccessKey.id)
            .where(VpnAccessKey.subscription_id.in_(subscription_ids))
            .order_by(VpnAccessKey.id)
        )
    )
    _require_complete_context(
        access_key_id in access_key_ids,
        skip_locked=skip_locked,
    )
    access_keys = (
        await db.scalars(
            _locked(
                select(VpnAccessKey)
                .where(VpnAccessKey.id.in_(access_key_ids))
                .order_by(VpnAccessKey.id),
                skip_locked=skip_locked,
            )
        )
    ).all()
    _require_complete_context(
        tuple(key.id for key in access_keys) == access_key_ids
        and all(key.subscription_id in subscription_by_id for key in access_keys),
        skip_locked=skip_locked,
    )
    access_key = next((key for key in access_keys if key.id == access_key_id), None)
    _require_complete_context(access_key is not None, skip_locked=skip_locked)
    assert access_key is not None
    subscription = subscription_by_id.get(access_key.subscription_id)
    if subscription is None:
        _fail("vpn_control_binding_changed")
    worker = await db.scalar(
        _locked(
            select(WorkerNode).where(WorkerNode.id == worker_id),
            skip_locked=skip_locked,
        )
    )
    if worker is None and skip_locked:
        return None
    endpoint = await db.scalar(
        _locked(
            select(VpnEndpoint).where(VpnEndpoint.id == endpoint_id),
            skip_locked=skip_locked,
        )
    )
    if endpoint is None and skip_locked:
        return None
    if worker is None or endpoint is None:
        _fail("vpn_control_binding_changed")
    return customer, subscription, access_key, worker, endpoint


async def claim_next_vpn_control_operation(
    db: AsyncSession,
    *,
    claim_token: UUID,
    now: datetime | None = None,
) -> VpnControlOperation | None:
    """Claim one current queued intent; the caller owns the transaction."""
    token = _uuid(claim_token, "vpn_control_claim_token_invalid")
    current = _current_time(now)
    if db.sync_session.new or db.sync_session.dirty or db.sync_session.deleted:
        _fail("vpn_control_unflushed_state")
    if await db.scalar(
        select(VpnControlOperation.id).where(
            VpnControlOperation.claim_token == str(token)
        )
    ) is not None:
        _fail("vpn_control_claim_token_conflict")

    reserved_workers = select(VpnControlOperation.worker_id).where(
        VpnControlOperation.state.in_(("claimed", "uncertain"))
    )
    candidates = (
        await db.execute(
            select(
                VpnControlOperation.id,
                VpnControlOperation.access_key_id,
                VpnControlOperation.worker_id,
                VpnControlOperation.endpoint_id,
            )
            .where(
                VpnControlOperation.state == "queued",
                VpnControlOperation.worker_id.not_in(reserved_workers),
            )
            .order_by(
                VpnControlOperation.generation,
                VpnControlOperation.created_at,
                VpnControlOperation.id,
            )
        )
    ).all()
    if not candidates:
        return None

    async def attempt(candidate) -> tuple[VpnControlOperation, bool]:
        context = await _lock_context(
            db,
            access_key_id=candidate.access_key_id,
            worker_id=candidate.worker_id,
            endpoint_id=candidate.endpoint_id,
            skip_locked=True,
        )
        if context is None:
            raise _CandidateBusy
        customer, subscription, access_key, _worker, endpoint = context
        if await db.scalar(
            select(VpnControlOperation.id).where(
                VpnControlOperation.worker_id == candidate.worker_id,
                VpnControlOperation.state.in_(("claimed", "uncertain")),
            )
        ) is not None:
            raise _CandidateBusy
        operation = await db.scalar(
            _locked(
                select(VpnControlOperation).where(
                    VpnControlOperation.id == candidate.id,
                    VpnControlOperation.state == "queued",
                ),
                skip_locked=True,
            )
        )
        if operation is None:
            raise _CandidateBusy
        if (
            operation.access_key_id != access_key.id
            or operation.worker_id != candidate.worker_id
            or operation.endpoint_id != candidate.endpoint_id
        ):
            _fail("vpn_control_binding_changed")
        request = _operation_request(operation)
        if not _intent_matches(
            operation,
            request,
            access_key,
            subscription,
            customer,
            endpoint,
            current,
        ):
            operation.state = "superseded"
            operation.finished_at = current
            operation.error_code = "vpn_control_superseded"
            operation.updated_at = current
            await db.flush()
            return operation, False
        operation.state = "claimed"
        operation.claim_token = str(token)
        operation.claimed_at = current
        operation.error_code = None
        operation.updated_at = current
        await db.flush()
        return operation, True

    if db.get_bind().dialect.name != "postgresql":
        for candidate in candidates:
            try:
                operation, claimed = await attempt(candidate)
            except _CandidateBusy:
                continue
            except IntegrityError:
                _fail("vpn_control_claim_conflict")
            if claimed:
                return operation
        return None

    for candidate in candidates:
        try:
            async with db.begin_nested():
                operation, claimed = await attempt(candidate)
            if claimed:
                return operation
        except _CandidateBusy:
            continue
        except IntegrityError:
            _fail("vpn_control_claim_conflict")
    return None


async def finalize_vpn_control_operation(
    db: AsyncSession,
    operation_id: UUID,
    claim_token: UUID,
    *,
    receipt_state: Literal["observed", "failed", "uncertain", "stale", "blocked"],
    error_code: str | None,
    now: datetime | None = None,
) -> VpnControlOperation:
    """Apply a node receipt only while its persisted intent is still authoritative."""
    identity = _uuid(operation_id, "vpn_control_operation_id_invalid")
    token = _uuid(claim_token, "vpn_control_claim_token_invalid")
    if (receipt_state, error_code) not in _RECEIPTS:
        _fail("vpn_control_receipt_invalid")
    current = _current_time(now)
    if db.sync_session.new or db.sync_session.dirty or db.sync_session.deleted:
        _fail("vpn_control_unflushed_state")

    discovered = (
        await db.execute(
            select(
                VpnControlOperation.access_key_id,
                VpnControlOperation.worker_id,
                VpnControlOperation.endpoint_id,
            ).where(VpnControlOperation.id == str(identity))
        )
    ).one_or_none()
    if discovered is None:
        _fail("vpn_control_operation_not_found")
    context = await _lock_context(
        db,
        access_key_id=discovered.access_key_id,
        worker_id=discovered.worker_id,
        endpoint_id=discovered.endpoint_id,
    )
    assert context is not None
    customer, subscription, access_key, _worker, endpoint = context
    operation = await db.scalar(
        _locked(
            select(VpnControlOperation).where(
                VpnControlOperation.id == str(identity)
            )
        )
    )
    if operation is None:
        _fail("vpn_control_operation_not_found")
    if (
        operation.access_key_id != access_key.id
        or operation.worker_id != endpoint.worker_id
        or operation.endpoint_id != endpoint.id
    ):
        _fail("vpn_control_binding_changed")
    if operation.claim_token != str(token):
        _fail("vpn_control_claim_token_mismatch")
    request = _operation_request(operation)
    if operation.state in {"succeeded", "failed", "superseded"}:
        return operation
    if operation.state == "uncertain":
        if receipt_state == "uncertain" or (
            receipt_state == "blocked"
            and error_code == "vpn_node_reconciliation_required"
        ):
            return operation
        _fail("vpn_control_reconciliation_required")
    if operation.state != "claimed":
        _fail("vpn_control_operation_not_claimed")

    current_intent = _intent_matches(
        operation,
        request,
        access_key,
        subscription,
        customer,
        endpoint,
        current,
    )
    if receipt_state == "uncertain" or (
        receipt_state == "blocked" and error_code == "vpn_node_reconciliation_required"
    ):
        operation.state = "uncertain"
        operation.error_code = error_code
        operation.updated_at = current
        await db.flush()
        return operation

    if receipt_state == "stale" or not current_intent:
        operation.state = "superseded"
        operation.error_code = (
            error_code if receipt_state == "stale" else "vpn_control_superseded"
        )
        operation.finished_at = current
        operation.updated_at = current
        await db.flush()
        return operation

    if receipt_state in {"failed", "blocked"}:
        operation.state = "failed"
        operation.error_code = error_code
        operation.finished_at = current
        operation.updated_at = current
        access_key.status = _PENDING_STATUS[operation.action]
        access_key.last_synced_at = current
        access_key.last_error = error_code
        access_key.updated_at = current
        await db.flush()
        return operation

    operation.state = "succeeded"
    operation.error_code = None
    operation.finished_at = current
    operation.updated_at = current
    access_key.status = {
        "provision": "active",
        "suspend": "suspended",
        "revoke": "revoked",
    }[operation.action]
    access_key.expires_at = subscription.expires_at
    access_key.last_synced_at = current
    access_key.last_error = None
    access_key.updated_at = current
    if operation.action == "provision" and request.target.security == "reality":
        access_key.config_uri = build_control_config_uri(request)
    elif operation.action == "revoke":
        access_key.revoked_at = access_key.revoked_at or current
    await db.flush()
    return operation


async def active_vpn_control_worker_ids(db: AsyncSession) -> set[int]:
    """Return every worker protected before or during endpoint dispatch."""
    return set(
        await db.scalars(
            select(VpnControlOperation.worker_id)
            .where(
                VpnControlOperation.state.in_(("queued", "claimed", "uncertain"))
            )
            .distinct()
        )
    )
