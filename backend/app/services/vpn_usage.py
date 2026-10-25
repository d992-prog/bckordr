"""Conservative usage aggregation with no network IO inside database locks."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import or_, select

from app.db import models
from app.db.base import utcnow
from app.services.vpn_endpoint_types import (
    is_valid_vpn_endpoint_target,
    public_endpoint_fingerprint,
)
from app.services.vpn_fleet_health import (
    _as_utc,
    _control_exists,
    _maintenance_exists,
    _target,
    _worker_is_busy,
    _worker_is_eligible,
)
from app.services.vpn_node_transport import (
    execute_vpn_node_usage_over_ssh,
    load_transport_snapshot,
)
from app.services.vpn_node_usage import (
    MAX_INT64,
    MAX_USAGE_IDENTITIES,
    UsageIdentity,
    UsageReceipt,
    UsageRequest,
    _integer,
    parse_node_usage_receipt,
    serialize_node_usage_receipt,
    serialize_node_usage_request,
)
from app.services.vpn_xray_runtime import _email, _normalized

USAGE_LEASE_SECONDS = 45
_SAFE_ERRORS = frozenset(
    {
        "vpn_usage_transport_failed",
        "vpn_usage_receipt_invalid",
        "vpn_usage_binding_changed",
        "vpn_usage_unavailable",
        "vpn_usage_overflow",
        "vpn_usage_identity_unverified",
        "vpn_usage_counter_overflow",
    }
)


@dataclass(frozen=True, slots=True)
class UsagePollClaim:
    worker_id: int
    claim_token: str
    claimed_at: datetime
    request: UsageRequest = field(repr=False)
    transport_snapshot: object = field(repr=False)
    bindings: tuple = field(repr=False)
    worker_fingerprint: str


def traffic_delta(previous, current) -> tuple[int, int] | None:
    for value in (previous, current):
        if value is not None and not (
            _integer(value.record_id, 1)
            and all(
                _integer(getattr(value, name))
                for name in ("reset_count", "upload_bytes", "download_bytes")
            )
        ):
            raise ValueError("vpn_usage_counter_invalid") from None
    if previous is None:
        return None
    elapsed = (
        _as_utc(current.sampled_at) - _as_utc(previous.sampled_at)
    ).total_seconds()
    if not 0 < elapsed <= 120:
        return None
    if (previous.counter_epoch, previous.record_id, previous.reset_count) != (
        current.counter_epoch,
        current.record_id,
        current.reset_count,
    ):
        return None
    up, down = (
        current.upload_bytes - previous.upload_bytes,
        current.download_bytes - previous.download_bytes,
    )
    return (up, down) if up >= 0 and down >= 0 else None


def _current(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("vpn_usage_time_invalid") from None
    return _as_utc(value)


def _milliseconds(value: datetime) -> int:
    delta = value - datetime(1970, 1, 1, tzinfo=UTC)
    return (delta.days * 86_400 + delta.seconds) * 1000 + delta.microseconds // 1000


def _worker_fingerprint(worker) -> str:
    values = [
        getattr(worker, name)
        for name in (
            "ssh_host",
            "ip_address",
            "ssh_port",
            "ssh_username",
            "ssh_password",
            "ssh_key_path",
            "ssh_host_key",
            "ssh_host_key_fingerprint",
        )
    ]
    return hashlib.sha256(
        json.dumps(values, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


async def _bindings(db, worker_id: int) -> tuple[tuple, tuple[UsageIdentity, ...]]:
    rows = (
        await db.execute(
            select(models.VpnAccessKey, models.VpnEndpoint)
            .outerjoin(
                models.VpnEndpoint,
                models.VpnEndpoint.id == models.VpnAccessKey.endpoint_id,
            )
            .where(models.VpnAccessKey.worker_id == worker_id)
            .order_by(models.VpnAccessKey.id)
            .limit(MAX_USAGE_IDENTITIES + 1)
        )
    ).all()
    if len(rows) > MAX_USAGE_IDENTITIES:
        raise ValueError("vpn_usage_overflow") from None
    from uuid import UUID

    bindings, identities = [], []
    uuids, emails = set(), set()
    for key, endpoint in rows:
        fingerprint = (
            None if endpoint is None else public_endpoint_fingerprint(_target(endpoint))
        )
        bindings.append(
            (
                key.id,
                key.worker_id,
                key.endpoint_id,
                key.device_id,
                key.subscription_id,
                key.operation_generation,
                key.protocol,
                key.external_uuid,
                key.verified_client_email,
                key.status,
                key.revoked_at,
                key.revoke_requested_at,
                fingerprint,
                None
                if endpoint is None
                else (
                    endpoint.worker_id,
                    endpoint.protocol,
                    endpoint.verified_at is not None,
                ),
            )
        )
        try:
            uid = UUID(key.external_uuid)
            valid = (
                str(uid) == key.external_uuid
                and _email(key.verified_client_email)
                and endpoint is not None
                and key.protocol == endpoint.protocol == "vless"
                and endpoint.worker_id == worker_id
                and endpoint.verified_at is not None
                and is_valid_vpn_endpoint_target(_target(endpoint))
            )
        except (ValueError, TypeError, AttributeError):
            valid = False
        if not valid:
            continue
        normalized, lowered = _normalized(uid), key.verified_client_email.lower()
        if normalized in uuids or lowered in emails:
            raise ValueError("vpn_usage_identity_unverified") from None
        uuids.add(normalized)
        emails.add(lowered)
        identities.append(
            UsageIdentity(
                key.id,
                endpoint.inbound_id,
                key.external_uuid,
                key.verified_client_email,
            )
        )
    return tuple(bindings), tuple(identities)


def _failure(poll, error_code: str) -> None:
    poll.claim_token = None
    poll.lease_until = None
    poll.traffic_state = "unavailable"
    poll.source_state = "unavailable"
    poll.last_error_code = (
        error_code if error_code in _SAFE_ERRORS else "vpn_usage_unavailable"
    )


async def claim_usage_poll(
    session_factory,
    known_hosts_path: Path,
    now: datetime,
    *,
    snapshot_loader=load_transport_snapshot,
) -> UsagePollClaim | None:
    current = _current(now)
    async with session_factory() as db, db.begin():
        worker = await db.scalar(
            select(models.WorkerNode)
            .outerjoin(
                models.VpnNodeUsagePoll,
                models.VpnNodeUsagePoll.worker_id == models.WorkerNode.id,
            )
            .where(
                models.WorkerNode.archived_at.is_(None),
                models.WorkerNode.is_enabled.is_(True),
                models.WorkerNode.status.not_in(("offline", "disabled")),
                models.WorkerNode.vpn_enabled.is_(True),
                models.WorkerNode.vpn_role != "none",
                ~_maintenance_exists(models.WorkerNode.id),
                ~_control_exists(models.WorkerNode.id),
                or_(
                    models.VpnNodeUsagePoll.next_poll_at.is_(None),
                    models.VpnNodeUsagePoll.next_poll_at <= current,
                ),
                or_(
                    models.VpnNodeUsagePoll.lease_until.is_(None),
                    models.VpnNodeUsagePoll.lease_until <= current,
                ),
            )
            .order_by(
                models.VpnNodeUsagePoll.next_poll_at.asc().nulls_first(),
                models.WorkerNode.id,
            )
            .limit(1)
            .with_for_update(of=models.WorkerNode, skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if (
            worker is None
            or not _worker_is_eligible(worker)
            or await _worker_is_busy(db, worker.id)
        ):
            return None
        existing_poll = await db.scalar(
            select(models.VpnNodeUsagePoll.worker_id).where(
                models.VpnNodeUsagePoll.worker_id == worker.id
            )
        )
        poll = await db.scalar(
            select(models.VpnNodeUsagePoll)
            .where(models.VpnNodeUsagePoll.worker_id == worker.id)
            .with_for_update(skip_locked=True)
            .execution_options(populate_existing=True)
        )
        if poll is None and existing_poll is not None:
            return None
        if poll is not None and (
            poll.next_poll_at is not None
            and _as_utc(poll.next_poll_at) > current
            or poll.lease_until is not None
            and _as_utc(poll.lease_until) > current
        ):
            return None
        # Snapshot before changing ORM state; policy helpers must never autoflush partial claims.
        try:
            bindings, identities = await _bindings(db, worker.id)
            if not identities:
                raise ValueError("vpn_usage_identity_unverified")
            token = str(uuid4())
            minute = current.replace(second=0, microsecond=0)
            request = UsageRequest(1, token, _milliseconds(minute), identities)
            serialize_node_usage_request(request, now_ms=_milliseconds(current))
            snapshot = snapshot_loader(worker, Path(known_hosts_path))
        except Exception as error:  # noqa: BLE001 - store fixed safe coverage only
            code = (
                str(error)
                if type(error) is ValueError and str(error) in _SAFE_ERRORS
                else "vpn_usage_unavailable"
            )
            if poll is None:
                poll = models.VpnNodeUsagePoll(worker_id=worker.id)
                db.add(poll)
            _failure(poll, code)
            poll.last_attempt_at = current
            poll.next_poll_at = current.replace(second=0, microsecond=0) + timedelta(
                minutes=1
            )
            return None
        if poll is None:
            poll = models.VpnNodeUsagePoll(worker_id=worker.id)
            db.add(poll)
        poll.claim_token = token
        poll.lease_until = current + timedelta(seconds=USAGE_LEASE_SECONDS)
        poll.next_poll_at = minute + timedelta(minutes=1)
        poll.last_attempt_at = current
        poll.last_error_code = None
        return UsagePollClaim(
            worker.id,
            token,
            current,
            request,
            snapshot,
            bindings,
            _worker_fingerprint(worker),
        )


async def apply_usage_poll(
    session_factory,
    claim: UsagePollClaim,
    receipt: UsageReceipt | None,
    *,
    now: datetime,
    error_code: str | None = None,
) -> bool:
    current = _current(now)
    if receipt is not None:
        try:
            receipt = parse_node_usage_receipt(
                serialize_node_usage_receipt(
                    receipt, claim.request, now_ms=_milliseconds(current)
                ),
                claim.request,
                now_ms=_milliseconds(current),
            )
        except Exception:  # noqa: BLE001 - do not retain raw node receipt details
            receipt, error_code = None, "vpn_usage_receipt_invalid"
    async with session_factory() as db, db.begin():
        worker = await db.scalar(
            select(models.WorkerNode)
            .where(models.WorkerNode.id == claim.worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        poll = await db.scalar(
            select(models.VpnNodeUsagePoll)
            .where(models.VpnNodeUsagePoll.worker_id == claim.worker_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            worker is None
            or poll is None
            or poll.claim_token != claim.claim_token
            or poll.lease_until is None
            or _as_utc(poll.lease_until) <= current
        ):
            return False
        try:
            bindings, _identities = await _bindings(db, worker.id)
            valid = (
                _worker_is_eligible(worker)
                and not await _worker_is_busy(db, worker.id)
                and bindings == claim.bindings
                and _worker_fingerprint(worker) == claim.worker_fingerprint
            )
        except Exception:  # noqa: BLE001 - changing bindings invalidate this batch
            valid = False
        if not valid:
            _failure(poll, "vpn_usage_binding_changed")
            return False
        if receipt is None:
            _failure(poll, error_code or "vpn_usage_unavailable")
            return True
        observed = datetime.fromtimestamp(receipt.observed_ms / 1000, UTC)
        complete = len(claim.bindings) == len(receipt.keys)
        overflow = False
        for key in receipt.keys:
            if key.traffic_state != "ok":
                complete = False
                continue
            previous = await db.get(models.VpnKeyUsageSample, key.access_key_id)
            if previous is not None and _as_utc(previous.sampled_at) >= observed:
                complete = False
                continue
            sample = models.VpnKeyUsageSample(
                access_key_id=key.access_key_id,
                worker_id=worker.id,
                sampled_at=observed,
                counter_epoch=receipt.counter_epoch,
                record_id=key.record_id,
                reset_count=key.reset_count,
                upload_bytes=key.upload_bytes,
                download_bytes=key.download_bytes,
            )
            delta = (
                None
                if previous is not None and previous.worker_id != worker.id
                else traffic_delta(previous, sample)
            )
            if delta is None:
                complete = False
            else:
                # shortcut: minute deltas are booked to the ending UTC hour; split measured intervals if finer boundary attribution is required
                hour = observed.replace(minute=0, second=0, microsecond=0)
                aggregate = await db.get(
                    models.VpnUsageHour, (key.access_key_id, worker.id, hour)
                )
                if aggregate is None:
                    db.add(
                        models.VpnUsageHour(
                            access_key_id=key.access_key_id,
                            worker_id=worker.id,
                            hour_start=hour,
                            upload_bytes=delta[0],
                            download_bytes=delta[1],
                            sample_count=1,
                            first_sample_at=previous.sampled_at,
                            last_sample_at=observed,
                        )
                    )
                elif (
                    aggregate.upload_bytes + delta[0] > MAX_INT64
                    or aggregate.download_bytes + delta[1] > MAX_INT64
                    or aggregate.sample_count >= MAX_INT64
                ):
                    complete, overflow = False, True
                else:
                    aggregate.upload_bytes += delta[0]
                    aggregate.download_bytes += delta[1]
                    aggregate.sample_count += 1
                    aggregate.last_sample_at = observed
            if previous is None:
                db.add(sample)
            else:
                for name in (
                    "worker_id",
                    "sampled_at",
                    "counter_epoch",
                    "record_id",
                    "reset_count",
                    "upload_bytes",
                    "download_bytes",
                ):
                    setattr(previous, name, getattr(sample, name))
        poll.claim_token = None
        poll.lease_until = None
        poll.last_success_at = current
        poll.last_error_code = "vpn_usage_counter_overflow" if overflow else None
        poll.traffic_state = (
            "ok"
            if complete
            else "partial"
            if any(key.traffic_state == "ok" for key in receipt.keys)
            else "unavailable"
        )
        poll.source_state = "unsupported"
        poll.protocol_version = 1
        return True


async def collect_usage_poll(
    session_factory,
    known_hosts_path: Path,
    *,
    now=utcnow,
    snapshot_loader=load_transport_snapshot,
    transport=execute_vpn_node_usage_over_ssh,
) -> bool:
    claim = await claim_usage_poll(
        session_factory, known_hosts_path, now(), snapshot_loader=snapshot_loader
    )
    if claim is None:
        return False
    try:
        receipt = await transport(
            claim.transport_snapshot,
            claim.request,
            now_ms=_milliseconds(claim.claimed_at),
        )
        error = None
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - never store transport stdout or credentials
        receipt, error = None, "vpn_usage_transport_failed"
    return await apply_usage_poll(
        session_factory, claim, receipt, now=now(), error_code=error
    )
