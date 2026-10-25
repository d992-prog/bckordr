"""Conservative same-minute source evidence; never an access-enforcement path."""

import hashlib
import json
import re
from datetime import datetime, timedelta
from time import monotonic
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import models as m
from app.services.audit import add_audit_log
from app.services.vpn_endpoint_types import (
    is_valid_vpn_endpoint_target,
    public_endpoint_fingerprint,
)
from app.services.vpn_fleet_health import _as_utc, _target
from app.services.vpn_xray_runtime import _email

ABUSE_CLEANUP_BATCH_SIZE = 1000
WINDOW_WAIT_SECONDS = 90


def _reset(state, coverage="unavailable") -> None:
    state.started_at = None
    state.consecutive_windows = 0
    state.last_source_count = None
    state.coverage = coverage
    state.key_epoch = None


async def _lock_state(db, device_id):
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(m.VpnDeviceAbuseState)
        .from_select(
            ["device_id"], select(m.VpnDevice.id).where(m.VpnDevice.id == device_id)
        )
        .on_conflict_do_nothing(index_elements=["device_id"])
    )
    return await db.scalar(
        select(m.VpnDeviceAbuseState)
        .where(m.VpnDeviceAbuseState.device_id == device_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _verified(key, endpoint, sample, subscription_id) -> bool:
    try:
        return bool(
            key.subscription_id == subscription_id
            and str(UUID(key.external_uuid)) == key.external_uuid
            and _email(key.verified_client_email)
            and endpoint is not None
            and key.worker_id == endpoint.worker_id
            and key.protocol == endpoint.protocol == "vless"
            and endpoint.verified_at is not None
            and is_valid_vpn_endpoint_target(_target(endpoint))
            and (sample is None or sample.worker_id == key.worker_id)
            and (
                sample is None
                or all(
                    _as_utc(sample.observed_at) >= _as_utc(stamp)
                    for stamp in (key.created_at, key.last_synced_at)
                    if stamp is not None
                )
            )
        )
    except (ValueError, TypeError, AttributeError):
        return False


async def evaluate_device_abuse(
    session_factory, device_id: int, *, minute_at: datetime, now: datetime
) -> None:
    started = monotonic()
    minute, current = _as_utc(minute_at), _as_utc(now)
    if minute != minute.replace(second=0, microsecond=0) or current < minute:
        raise ValueError("vpn_abuse_minute_invalid")
    async with session_factory() as db, db.begin():
        state = await _lock_state(db, device_id)
        if state is None:
            return
        previous = _as_utc(state.last_minute_at) if state.last_minute_at else None
        if previous is not None and minute < previous:
            return
        # One authoritative snapshot after the state lock includes children committed while waiting.
        rows = (
            await db.execute(
                select(m.VpnDevice, m.VpnAccessKey, m.VpnEndpoint, m.VpnKeySourceSample)
                .outerjoin(
                    m.VpnAccessKey,
                    and_(
                        m.VpnAccessKey.device_id == m.VpnDevice.id,
                        m.VpnAccessKey.status != "revoked",
                    ),
                )
                .outerjoin(
                    m.VpnEndpoint, m.VpnEndpoint.id == m.VpnAccessKey.endpoint_id
                )
                .outerjoin(
                    m.VpnKeySourceSample,
                    m.VpnKeySourceSample.access_key_id == m.VpnAccessKey.id,
                )
                .where(m.VpnDevice.id == device_id)
                .order_by(m.VpnAccessKey.id)
            )
        ).all()
        current += timedelta(seconds=max(0.0, monotonic() - started))
        state.last_minute_at = minute
        if not rows or rows[0][0].revoke_requested_at is not None or rows[0][1] is None:
            _reset(state)
            return
        if current > minute + timedelta(seconds=WINDOW_WAIT_SECONDS):
            _reset(state)
            return
        if previous == minute and state.coverage in {"unsupported", "unavailable"}:
            return
        missing, epochs, observed, hashes, identities, bindings = (
            False,
            set(),
            [],
            set(),
            set(),
            [],
        )
        for device, key, endpoint, sample in rows:
            if not _verified(key, endpoint, sample, device.subscription_id):
                _reset(state)
                return
            identity = (
                key.worker_id,
                key.external_uuid,
                key.verified_client_email.lower(),
            )
            if identity in identities:
                _reset(state)
                return
            identities.add(identity)
            bindings.append(
                (
                    key.id,
                    key.subscription_id,
                    key.worker_id,
                    key.endpoint_id,
                    key.operation_generation,
                    key.external_uuid,
                    key.verified_client_email,
                    public_endpoint_fingerprint(_target(endpoint)),
                )
            )
            if sample is None or _as_utc(sample.minute_at) != minute:
                missing = True
                continue
            if sample.state != "ok":
                _reset(
                    state,
                    "unsupported" if sample.state == "unsupported" else "unavailable",
                )
                return
            if (
                not isinstance(sample.key_epoch, str)
                or re.fullmatch(r"[0-9a-f]{16}", sample.key_epoch) is None
                or not isinstance(sample.source_hashes, list)
                or len(sample.source_hashes) > 64
                or any(
                    not isinstance(value, str)
                    or re.fullmatch(r"[0-9a-f]{64}", value) is None
                    for value in sample.source_hashes
                )
            ):
                _reset(state)
                return
            stamp = _as_utc(sample.observed_at)
            if (
                not minute
                <= stamp
                <= min(current + timedelta(seconds=30), minute + timedelta(seconds=90))
            ):
                _reset(state)
                return
            epochs.add(sample.key_epoch)
            observed.append(stamp)
            hashes.update(sample.source_hashes)
        if len(epochs) > 1 or (
            observed and (max(observed) - min(observed)).total_seconds() > 15
        ):
            _reset(state)
            return
        if missing:
            # Retain only the immediately preceding complete streak while this minute collects.
            if not (
                (previous == minute and state.coverage == "collecting")
                or (
                    previous == minute - timedelta(minutes=1) and state.coverage == "ok"
                )
            ):
                _reset(state)
            state.coverage = "collecting"
            return
        # A changed binding cannot bridge evidence collected for the previous credentials/policy.
        epoch = hashlib.sha256(
            json.dumps([next(iter(epochs)), bindings], separators=(",", ":")).encode()
        ).hexdigest()[:16]
        count = len(hashes)
        if previous == minute and state.coverage != "collecting":
            if state.key_epoch != epoch:
                _reset(state)
            elif count <= 2:
                _reset(state, "ok")
                state.key_epoch, state.last_source_count = epoch, count
            return
        continuous = state.key_epoch == epoch and (
            (previous == minute and state.coverage == "collecting")
            or (previous == minute - timedelta(minutes=1) and state.coverage == "ok")
        )
        if count <= 2:
            _reset(state, "ok")
        else:
            state.consecutive_windows = (
                min(5, state.consecutive_windows + 1) if continuous else 1
            )
            if not continuous or state.started_at is None:
                state.started_at = minute
        state.coverage, state.key_epoch, state.last_source_count = "ok", epoch, count
        if state.consecutive_windows == 5:
            incident = await db.scalar(
                select(m.VpnAbuseIncident).where(
                    m.VpnAbuseIncident.device_id == device_id,
                    m.VpnAbuseIncident.acknowledged_at.is_(None),
                )
            )
            if incident is None:
                db.add(
                    m.VpnAbuseIncident(
                        device_id=device_id,
                        started_at=state.started_at,
                        last_observed_at=max(observed),
                        peak_source_count=count,
                    )
                )
            else:
                incident.last_observed_at = max(
                    _as_utc(incident.last_observed_at), max(observed)
                )
                incident.peak_source_count = max(incident.peak_source_count, count)


async def acknowledge_incident(
    db, incident_id: int, *, actor_id: int | None, now: datetime
):
    device_id = await db.scalar(
        select(m.VpnAbuseIncident.device_id).where(m.VpnAbuseIncident.id == incident_id)
    )
    if device_id is None:
        raise ValueError("vpn_abuse_incident_not_found")
    state = await _lock_state(db, device_id)
    incident = await db.scalar(
        select(m.VpnAbuseIncident)
        .where(m.VpnAbuseIncident.id == incident_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if incident.acknowledged_at is None:
        current = _as_utc(now)
        incident.acknowledged_at = current
        incident.acknowledged_by_user_id = actor_id
        _reset(state)
        minute = current.replace(second=0, microsecond=0)
        state.last_minute_at = (
            max(minute, _as_utc(state.last_minute_at))
            if state.last_minute_at
            else minute
        )
        await add_audit_log(
            db,
            actor_user_id=actor_id,
            target_user_id=None,
            action="vpn.abuse.acknowledge",
            details=f"incident_id={incident.id};device_id={device_id}",
        )
    await db.flush()
    return incident


async def expire_abuse_windows(session_factory, *, now: datetime) -> int:
    current, state = _as_utc(now), m.VpnDeviceAbuseState
    async with session_factory() as db, db.begin():
        rows = list(
            await db.scalars(
                select(state)
                .where(
                    or_(
                        and_(
                            state.coverage == "collecting",
                            state.last_minute_at < current - timedelta(seconds=90),
                        ),
                        and_(
                            state.consecutive_windows > 0,
                            state.last_minute_at < current - timedelta(seconds=150),
                        ),
                    )
                )
                .order_by(state.last_minute_at, state.device_id)
                .limit(ABUSE_CLEANUP_BATCH_SIZE)
                .with_for_update(skip_locked=True)
            )
        )
        for row in rows:
            _reset(row)
        return len(rows)
