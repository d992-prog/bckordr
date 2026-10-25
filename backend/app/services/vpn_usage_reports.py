"""Read-only hourly reports; credential/source fields never leave the database."""

from datetime import datetime, timedelta

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    VpnAccessKey as Key,
)
from app.db.models import (
    VpnCustomer,
    VpnDevice,
    VpnEndpoint,
    VpnSubscription,
    WorkerNode,
)
from app.db.models import (
    VpnKeyUsageSample as Sample,
)
from app.db.models import (
    VpnNodeUsagePoll as Poll,
)
from app.db.models import (
    VpnUsageHour as Hour,
)
from app.schemas.vpn_usage import (
    CustomerUsageResponse,
    DeviceUsageRow,
    NodeUsageRow,
    UsageTotals,
)
from app.services.vpn_policy import NODE_CAPACITY_STATUSES, _as_utc

USAGE_FRESH_SECONDS = 120


def _start(now: datetime, days: int) -> datetime:
    if type(days) is not int or days not in (1, 7, 30):
        raise ValueError("vpn_usage_period_invalid")
    # shortcut: periods include the boundary UTC hour; split stored intervals for finer cutoffs.
    return now.replace(minute=0, second=0, microsecond=0) - timedelta(days=days)


def _hours(group, start, now):
    return (
        select(
            group.label("entity_id"),
            func.sum(Hour.upload_bytes).label("upload_bytes"),
            func.sum(Hour.download_bytes).label("download_bytes"),
            func.sum(Hour.sample_count).label("sample_count"),
            func.count(func.distinct(Hour.access_key_id)).label("key_count"),
            func.min(Hour.first_sample_at).label("measured_since"),
            func.max(Hour.last_sample_at).label("last_sample_at"),
        )
        .where(
            Hour.hour_start >= start, Hour.hour_start <= now, Hour.last_sample_at <= now
        )
        .group_by(group)
    )


def _evidence(group, start, now, key_scope, *, node=False):
    intervals = _hours(Hour.access_key_id, start, now).where(
        Hour.access_key_id.in_(key_scope)
    )
    if node:
        intervals = intervals.add_columns(Hour.worker_id).group_by(Hour.worker_id)
    intervals = intervals.subquery()
    interval_match = intervals.c.entity_id == Key.id
    if node:
        interval_match = and_(interval_match, intervals.c.worker_id == Key.worker_id)
    return (
        select(
            group.label("entity_id"),
            func.count(Key.id).label("key_count"),
            func.count(Sample.access_key_id).label("baseline_count"),
            func.min(Sample.sampled_at).label("oldest_sample_at"),
            func.max(Sample.sampled_at).label("last_sample_at"),
            func.max(Poll.last_attempt_at).label("last_attempt_at"),
            func.sum(
                case(
                    (
                        and_(
                            Poll.traffic_state == "unavailable",
                            Poll.last_attempt_at.is_not(None),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("unavailable_count"),
            func.sum(case((Poll.traffic_state == "partial", 1), else_=0)).label(
                "partial_count"
            ),
            func.count(Poll.last_success_at).label("poll_count"),
            func.min(Poll.last_success_at).label("oldest_poll_at"),
            func.sum(
                case(
                    (
                        or_(
                            intervals.c.measured_since.is_(None),
                            intervals.c.measured_since > start,
                            intervals.c.sample_count * 60
                            < (now - start).total_seconds(),
                        ),
                        1,
                    ),
                    else_=0,
                )
            ).label("missing_period_count"),
        )
        .select_from(Key)
        .outerjoin(
            Sample,
            and_(Sample.access_key_id == Key.id, Sample.worker_id == Key.worker_id),
        )
        .outerjoin(Poll, Poll.worker_id == Key.worker_id)
        .outerjoin(intervals, interval_match)
        .where(Key.id.in_(key_scope))
        .group_by(group)
    )


def _rows(rows):
    return {row.entity_id: row for row in rows}


def _totals(
    aggregate, evidence, *, start: datetime, now: datetime, failed_attempt=False
) -> UsageTotals:
    measured_at = _as_utc(aggregate.last_sample_at) if aggregate is not None else None
    baseline_at = _as_utc(evidence.last_sample_at) if evidence is not None else None
    last_at = max(
        (stamp for stamp in (measured_at, baseline_at) if stamp is not None),
        default=None,
    )
    fresh_since = now - timedelta(seconds=USAGE_FRESH_SECONDS)
    if failed_attempt or evidence is not None and evidence.unavailable_count:
        coverage = "unavailable"
    elif last_at is not None and last_at < fresh_since:
        coverage = "stale"
    elif (
        aggregate is None
        and last_at is None
        and (evidence is None or evidence.last_attempt_at is None)
    ):
        coverage = "none"
    elif aggregate is None or evidence is None:
        coverage = "partial"
    else:
        # Counts can disprove full coverage, not reconstruct missing or reset intervals.
        complete = (
            evidence.key_count == evidence.baseline_count == evidence.poll_count
            and aggregate.key_count == evidence.key_count
            and not evidence.missing_period_count
            and not evidence.partial_count
            and fresh_since <= _as_utc(evidence.oldest_sample_at) <= now
            and fresh_since <= _as_utc(evidence.oldest_poll_at) <= now
            and _as_utc(aggregate.measured_since) <= start
            and aggregate.sample_count * 60
            >= (now - start).total_seconds() * evidence.key_count
        )
        coverage = "current" if complete else "partial"
    return UsageTotals(
        upload_bytes=None if aggregate is None else int(aggregate.upload_bytes),
        download_bytes=None if aggregate is None else int(aggregate.download_bytes),
        measured_since=None if aggregate is None else _as_utc(aggregate.measured_since),
        last_sample_at=last_at,
        coverage=coverage,
    )


async def list_node_usage(
    db: AsyncSession,
    *,
    days: int,
    now: datetime,
    after_id: int = 0,
    limit: int = 100,
    heartbeat_max_age_seconds: int = 45,
) -> list[NodeUsageRow]:
    now = _as_utc(now)
    start = _start(now, days)
    workers = (
        await db.execute(
            select(
                WorkerNode.id,
                WorkerNode.cpu_load,
                WorkerNode.ram_usage_percent,
                WorkerNode.last_heartbeat_at,
                Poll.traffic_state,
                Poll.last_attempt_at,
            )
            .outerjoin(Poll, Poll.worker_id == WorkerNode.id)
            .where(
                WorkerNode.id > after_id,
                or_(
                    WorkerNode.vpn_enabled.is_(True),
                    WorkerNode.vpn_role != "none",
                    exists().where(VpnEndpoint.worker_id == WorkerNode.id),
                    Poll.worker_id.is_not(None),
                    exists().where(Hour.worker_id == WorkerNode.id),
                ),
            )
            .order_by(WorkerNode.id)
            .limit(limit)
        )
    ).all()
    ids = [worker.id for worker in workers]
    if not ids:
        return []
    hours = _rows(
        (
            await db.execute(
                _hours(Hour.worker_id, start, now).where(Hour.worker_id.in_(ids))
            )
        ).all()
    )
    evidence = _rows(
        (
            await db.execute(
                _evidence(
                    Key.worker_id,
                    start,
                    now,
                    select(Key.id).where(Key.worker_id.in_(ids)).correlate(None),
                    node=True,
                )
            )
        ).all()
    )
    occupied = (
        select(Key.worker_id, func.count(Key.id).label("occupied_profiles"))
        .join(
            VpnEndpoint,
            and_(
                VpnEndpoint.id == Key.endpoint_id,
                VpnEndpoint.worker_id == Key.worker_id,
            ),
        )
        .where(Key.worker_id.in_(ids), Key.status.in_(NODE_CAPACITY_STATUSES))
        .group_by(Key.worker_id)
        .subquery()
    )
    active_groups = (
        select(Key.worker_id)
        .outerjoin(VpnDevice, VpnDevice.id == Key.device_id)
        .where(
            Key.worker_id.in_(ids),
            Key.status == "active",
            Key.revoked_at.is_(None),
            Key.revoke_requested_at.is_(None),
            VpnDevice.revoke_requested_at.is_(None),
        )
        .group_by(
            Key.worker_id,
            Key.device_id,
            case((Key.device_id.is_(None), Key.id), else_=None),
        )
        .subquery()
    )
    active = (
        select(active_groups.c.worker_id, func.count().label("active_devices"))
        .group_by(active_groups.c.worker_id)
        .subquery()
    )
    capacity = (
        select(
            VpnEndpoint.worker_id,
            case(
                (
                    func.count(VpnEndpoint.max_active_profiles) == func.count(),
                    func.sum(VpnEndpoint.max_active_profiles),
                ),
                else_=None,
            ).label("max_active_profiles"),
        )
        .where(VpnEndpoint.worker_id.in_(ids))
        .group_by(VpnEndpoint.worker_id)
        .subquery()
    )
    counts = {
        row.id: row
        for row in (
            await db.execute(
                select(
                    WorkerNode.id,
                    occupied.c.occupied_profiles,
                    active.c.active_devices,
                    capacity.c.max_active_profiles,
                )
                .outerjoin(occupied, occupied.c.worker_id == WorkerNode.id)
                .outerjoin(active, active.c.worker_id == WorkerNode.id)
                .outerjoin(capacity, capacity.c.worker_id == WorkerNode.id)
                .where(WorkerNode.id.in_(ids))
            )
        ).all()
    }
    rows = []
    for worker in workers:
        heartbeat = _as_utc(worker.last_heartbeat_at)
        fresh = (
            heartbeat is not None
            and now - timedelta(seconds=max(1, heartbeat_max_age_seconds))
            <= heartbeat
            <= now
        )
        rows.append(
            NodeUsageRow(
                **_totals(
                    hours.get(worker.id),
                    evidence.get(worker.id),
                    start=start,
                    now=now,
                    failed_attempt=worker.traffic_state == "unavailable"
                    and worker.last_attempt_at is not None,
                ).model_dump(),
                worker_id=worker.id,
                active_devices=int(counts[worker.id].active_devices or 0),
                occupied_profiles=int(counts[worker.id].occupied_profiles or 0),
                max_active_profiles=counts[worker.id].max_active_profiles,
                cpu_load=worker.cpu_load if fresh else None,
                ram_usage_percent=worker.ram_usage_percent if fresh else None,
            )
        )
    return rows


def _combined(rows: list[UsageTotals]) -> UsageTotals:
    measured = [row for row in rows if row.upload_bytes is not None]
    states = {row.coverage for row in rows}
    if not states or states == {"none"}:
        coverage = "none"
    elif len(states) == 1:
        coverage = states.pop()
    elif states <= {"stale", "unavailable", "none"} and measured:
        coverage = "stale"
    else:
        coverage = "partial"
    return UsageTotals(
        upload_bytes=sum(row.upload_bytes for row in measured) if measured else None,
        download_bytes=sum(row.download_bytes for row in measured)
        if measured
        else None,
        measured_since=min((row.measured_since for row in measured), default=None),
        last_sample_at=max(
            (row.last_sample_at for row in rows if row.last_sample_at is not None),
            default=None,
        ),
        coverage=coverage,
    )


async def get_customer_usage(
    db: AsyncSession, customer_id: int, *, days: int, now: datetime
) -> CustomerUsageResponse | None:
    now = _as_utc(now)
    start = _start(now, days)
    if (
        await db.scalar(select(VpnCustomer.id).where(VpnCustomer.id == customer_id))
        is None
    ):
        return None
    devices = (
        await db.execute(
            select(VpnDevice.id, VpnDevice.display_name)
            .join(
                VpnSubscription,
                VpnSubscription.id == VpnDevice.subscription_id,
            )
            .where(VpnSubscription.customer_id == customer_id)
            .order_by(VpnDevice.id)
        )
    ).all()
    hours = _rows(
        (
            await db.execute(
                _hours(Key.device_id, start, now)
                .join(
                    Key,
                    Key.id == Hour.access_key_id,
                )
                .join(VpnSubscription, VpnSubscription.id == Key.subscription_id)
                .where(VpnSubscription.customer_id == customer_id)
            )
        ).all()
    )
    evidence = _rows(
        (
            await db.execute(
                _evidence(
                    Key.device_id,
                    start,
                    now,
                    select(Key.id)
                    .join(VpnSubscription, VpnSubscription.id == Key.subscription_id)
                    .where(VpnSubscription.customer_id == customer_id)
                    .correlate(None),
                )
                .join(
                    VpnSubscription,
                    VpnSubscription.id == Key.subscription_id,
                )
                .where(VpnSubscription.customer_id == customer_id)
            )
        ).all()
    )
    rows = [
        DeviceUsageRow(
            device_id=device.id,
            display_name=device.display_name,
            **_totals(
                hours.get(device.id), evidence.get(device.id), start=start, now=now
            ).model_dump(),
        )
        for device in devices
    ]
    all_totals = list(rows)
    if None in hours or None in evidence:
        all_totals.append(
            _totals(hours.get(None), evidence.get(None), start=start, now=now)
        )
    return CustomerUsageResponse(
        customer_id=customer_id,
        days=days,
        devices=rows,
        **_combined(all_totals).model_dump(),
    )
