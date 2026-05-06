from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.services.attack_runtime import domain_window_bounds, worker_matches_domain
from app.db.base import utcnow
from app.db.models import AttackRun, DropDomain, User, WorkerNode
from app.db.session import get_db
from app.schemas.common import DomainHealthItem, HealthResponse, MonitoringHealthResponse

router = APIRouter(tags=["health"])
ONLINE_WORKER_MAX_AGE_SECONDS = 120


@router.get("/health", response_model=HealthResponse)
async def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok", checked_at=utcnow())


@router.get("/health/monitoring", response_model=MonitoringHealthResponse)
async def monitoring_health(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MonitoringHealthResponse:
    now = utcnow()
    del user
    paris_today = now.astimezone(ZoneInfo("Europe/Paris")).date()
    domains = (
        await db.execute(
            select(DropDomain)
            .where(DropDomain.attack_enabled.is_(True))
            .order_by(DropDomain.drop_date.asc(), DropDomain.priority.desc(), DropDomain.fqdn.asc())
        )
    ).scalars().all()
    workers = (await db.execute(select(WorkerNode).order_by(WorkerNode.name.asc()))).scalars().all()
    failed_run_counts = {
        domain_id: count
        for domain_id, count in (
            await db.execute(
                select(AttackRun.domain_id, func.count(AttackRun.id))
                .where(AttackRun.status == "failed")
                .group_by(AttackRun.domain_id)
            )
        ).all()
    }
    items: list[DomainHealthItem] = []
    online_workers = [
        worker
        for worker in workers
        if worker.is_enabled
        and worker.last_seen_at is not None
        and (now - worker.last_seen_at).total_seconds() <= ONLINE_WORKER_MAX_AGE_SECONDS
    ]

    for domain in domains:
        compatible_workers = [worker for worker in workers if worker_matches_domain(worker, domain)]
        compatible_online_workers = [worker for worker in online_workers if worker_matches_domain(worker, domain)]
        latest_worker_seen = max(
            (worker.last_seen_at for worker in compatible_workers if worker.last_seen_at is not None),
            default=None,
        )
        window_state = "idle"
        if domain.drop_date == paris_today:
            bounds = domain_window_bounds(domain, anchor=now)
            if bounds is None:
                window_state = "window-finished"
            else:
                start_at, end_at = bounds
                if start_at <= now <= end_at:
                    window_state = "window-open"
                elif now < start_at:
                    window_state = "waiting-window"
                else:
                    window_state = "window-finished"
        stale = (
            domain.drop_date == paris_today
            and domain.status in {"scheduled", "attacking"}
            and not compatible_online_workers
        )
        items.append(
            DomainHealthItem(
                domain_id=domain.id,
                domain=domain.fqdn,
                status=domain.status,
                check_mode=window_state,
                last_check_at=domain.updated_at,
                worker_heartbeat_at=latest_worker_seen,
                consecutive_failures=int(failed_run_counts.get(domain.id, 0)),
                is_stale=stale,
            )
        )

    stale_count = sum(1 for item in items if item.is_stale)
    return MonitoringHealthResponse(
        status="ok" if stale_count == 0 else "degraded",
        checked_at=now,
        active_domains=len(items),
        stale_domains=stale_count,
        workers_in_memory=len(online_workers),
        items=items,
    )
