from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import (
    AttackEvent,
    AttackRun,
    DomainOverridePhase,
    DomainOverrideRule,
    DomainRuleOverride,
    DropDomain,
    DiscoveryDomain,
    WorkerNode,
    WorkerTask,
    ZoneRule,
    ZoneRulePhase,
    ZoneStrategy,
)
from app.services.discovery import (
    IANA_RDAP_BOOTSTRAP_URL,
    WhoisLookup,
    check_discovery_domain_rdap,
    check_discovery_domain_whois,
)
from app.services.strategy_runtime import (
    is_domain_due_today,
    resolve_effective_strategy,
    resolve_strategy_runtime_profile,
)

AUTOPLAN_ELIGIBLE_STATUSES = {"ready", "queued", "scheduled", "attacking"}
POST_WINDOW_RDAP_CONFIRMATION_THRESHOLD = 3
POST_WINDOW_RDAP_CHECK_INTERVAL_SECONDS = 60
POST_WINDOW_RDAP_CHECK_EVENT_TYPES = {
    "post_window_rdap_registered",
    "post_window_rdap_inconclusive",
}
WHOIS_SAFETY_CONFIRMATION_ZONES = {"fr"}


@dataclass(slots=True)
class DomainRuntimeSnapshot:
    minimum_rps: float
    desired_rps: float
    allocated_rps: float
    assigned_worker_count: int
    phase_name: str | None
    attack_run_id: int | None
    attack_status: str | None
    window_start_at: datetime | None = None
    window_end_at: datetime | None = None


def worker_matches_domain(worker: WorkerNode, domain: DropDomain) -> bool:
    if not getattr(worker, "is_enabled", True) or getattr(worker, "status", "ready") in {"offline", "disabled"}:
        return False
    if worker.registrar_slug != domain.registrar_slug:
        return False
    if worker.assigned_registrar_account_id is None:
        return True
    return worker.assigned_registrar_account_id == domain.registrar_account_id


def select_domains_for_autoplanning(
    domains: list[DropDomain],
    *,
    now: datetime,
    active_run_domain_ids: set[int],
    bounds_by_domain_id: dict[int, tuple[datetime, datetime] | None] | None = None,
) -> list[DropDomain]:
    eligible: list[DropDomain] = []
    for domain in domains:
        if not domain.attack_enabled:
            continue
        if not getattr(domain, "auto_start_enabled", False):
            continue
        if domain.success_at is not None:
            continue
        if domain.id in active_run_domain_ids:
            continue
        if domain.status not in AUTOPLAN_ELIGIBLE_STATUSES:
            continue
        if not is_domain_due_today(domain, now):
            continue
        bounds = (
            bounds_by_domain_id.get(domain.id)
            if bounds_by_domain_id is not None and domain.id in bounds_by_domain_id
            else domain_window_bounds(domain, now)
        )
        if bounds is None:
            continue
        planned_start_at, planned_end_at = bounds
        lead_seconds = max(int(getattr(domain, "auto_start_lead_seconds", 90) or 0), 0)
        if now < planned_start_at - timedelta(seconds=lead_seconds):
            continue
        if now > planned_end_at:
            continue
        eligible.append(domain)
    eligible.sort(key=lambda domain: (-domain.priority, domain.drop_date, domain.fqdn))
    return eligible


def _is_registered_taken_observation(observation: object) -> bool:
    return (
        getattr(observation, "error", None) is None
        and getattr(observation, "http_status", None) == 200
        and getattr(observation, "lifecycle_stage", None) == "registered"
        and getattr(observation, "availability_status", None) == "taken"
    )


async def _check_domain_safety_observation(
    domain: DropDomain,
    *,
    client: httpx.AsyncClient,
    bootstrap_url: str,
    whois_lookup: WhoisLookup | None = None,
) -> object:
    discovery_domain = DiscoveryDomain(fqdn=domain.fqdn, zone=domain.zone)
    observation = await check_discovery_domain_rdap(
        discovery_domain,
        client=client,
        bootstrap_url=bootstrap_url,
        whois_lookup=whois_lookup,
    )
    if _is_registered_taken_observation(observation):
        return observation
    if domain.zone.lower() not in WHOIS_SAFETY_CONFIRMATION_ZONES:
        return observation

    whois_observation = await check_discovery_domain_whois(
        discovery_domain,
        observed_at=getattr(observation, "observed_at", None),
        timeout_seconds=5.0,
        whois_lookup=whois_lookup,
    )
    if _is_registered_taken_observation(whois_observation):
        return whois_observation
    return observation


async def _filter_domains_available_for_autostart(
    session: AsyncSession,
    domains: list[DropDomain],
    *,
    now: datetime,
    client: httpx.AsyncClient | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
    whois_lookup: WhoisLookup | None = None,
) -> list[DropDomain]:
    if not domains:
        return []

    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=5.0)
    available: list[DropDomain] = []
    try:
        for domain in domains:
            observation = await _check_domain_safety_observation(
                domain,
                client=http_client,
                bootstrap_url=bootstrap_url,
                whois_lookup=whois_lookup,
            )
            if _is_registered_taken_observation(observation):
                reason = "Pre-start RDAP safety check confirmed domain is already registered"
                domain.status = "failed"
                domain.attack_enabled = False
                domain.readiness_reasons = reason
                domain.updated_at = now
                session.add(
                    AttackEvent(
                        domain_id=domain.id,
                        level="error",
                        event_type="pre_start_domain_taken",
                        message=(
                            f"{domain.fqdn} disabled before auto-start: "
                            f"lifecycle={observation.lifecycle_stage} availability={observation.availability_status} "
                            f"http={observation.http_status} source={observation.source}"
                        ),
                        created_at=now,
                    )
                )
                continue
            available.append(domain)
    finally:
        if close_client:
            await http_client.aclose()

    return available


def domain_window_bounds(domain: DropDomain, anchor: datetime | None = None) -> tuple[datetime, datetime] | None:
    tz = ZoneInfo(domain.timezone_name or "Europe/Paris")
    current = (anchor or utcnow()).astimezone(tz)
    target_date = max(current.date(), domain.drop_date)

    start_at = datetime.combine(
        target_date,
        time(
            hour=current.hour if target_date == current.date() else 0,
            minute=domain.window_start_minute,
            second=domain.window_start_second,
        ),
        tzinfo=tz,
    )
    if target_date == current.date():
        end_current = start_at + timedelta(seconds=domain.window_duration_seconds)
        if current > end_current:
            next_hour = current.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            if next_hour.date() != target_date:
                return None
            start_at = datetime.combine(
                next_hour.date(),
                time(hour=next_hour.hour, minute=domain.window_start_minute, second=domain.window_start_second),
                tzinfo=tz,
            )
    end_at = start_at + timedelta(seconds=domain.window_duration_seconds)
    return start_at.astimezone(ZoneInfo("UTC")), end_at.astimezone(ZoneInfo("UTC"))


async def load_effective_strategies(
    session: AsyncSession,
    domains: list[DropDomain],
) -> dict[int, object]:
    strategy_ids = {domain.zone_strategy_id for domain in domains if domain.zone_strategy_id}
    domain_override_ids = {domain.domain_rule_override_id for domain in domains if domain.domain_rule_override_id}
    if not strategy_ids and not domain_override_ids:
        return {}

    strategies = []
    if strategy_ids:
        strategies = (
            await session.execute(select(ZoneStrategy).where(ZoneStrategy.id.in_(strategy_ids)))
        ).scalars().all()
    strategy_map = {strategy.id: strategy for strategy in strategies}

    zone_rules: list[ZoneRule] = []
    if strategy_ids:
        zone_rules = (
            await session.execute(
                select(ZoneRule)
                .where(ZoneRule.zone_strategy_id.in_(strategy_ids), ZoneRule.is_enabled.is_(True))
                .order_by(ZoneRule.priority.desc(), ZoneRule.id.asc())
            )
        ).scalars().all()
    rules_by_strategy: dict[int, list[ZoneRule]] = defaultdict(list)
    for rule in zone_rules:
        rules_by_strategy[rule.zone_strategy_id].append(rule)

    zone_phases: list[ZoneRulePhase] = []
    if strategy_ids:
        zone_phases = (
            await session.execute(
                select(ZoneRulePhase)
                .join(ZoneRule, ZoneRule.id == ZoneRulePhase.zone_rule_id)
                .where(ZoneRule.zone_strategy_id.in_(strategy_ids))
                .order_by(ZoneRulePhase.sort_order.asc(), ZoneRulePhase.id.asc())
            )
        ).scalars().all()
    phases_by_strategy: dict[int, list[ZoneRulePhase]] = defaultdict(list)
    rule_to_strategy = {rule.id: rule.zone_strategy_id for rule in zone_rules}
    for phase in zone_phases:
        strategy_id = rule_to_strategy.get(phase.zone_rule_id)
        if strategy_id is not None:
            phases_by_strategy[strategy_id].append(phase)

    domain_overrides = []
    if domain_override_ids:
        domain_overrides = (
            await session.execute(select(DomainRuleOverride).where(DomainRuleOverride.id.in_(domain_override_ids)))
        ).scalars().all()
    domain_override_map = {override.id: override for override in domain_overrides}

    domain_rules: list[DomainOverrideRule] = []
    if domain_override_ids:
        domain_rules = (
            await session.execute(
                select(DomainOverrideRule)
                .where(
                    DomainOverrideRule.domain_rule_override_id.in_(domain_override_ids),
                    DomainOverrideRule.is_enabled.is_(True),
                )
                .order_by(DomainOverrideRule.priority.desc(), DomainOverrideRule.id.asc())
            )
        ).scalars().all()
    rules_by_override: dict[int, list[DomainOverrideRule]] = defaultdict(list)
    for rule in domain_rules:
        rules_by_override[rule.domain_rule_override_id].append(rule)

    domain_phases: list[DomainOverridePhase] = []
    if domain_override_ids:
        domain_phases = (
            await session.execute(
                select(DomainOverridePhase)
                .join(DomainOverrideRule, DomainOverrideRule.id == DomainOverridePhase.domain_override_rule_id)
                .where(DomainOverrideRule.domain_rule_override_id.in_(domain_override_ids))
                .order_by(DomainOverridePhase.sort_order.asc(), DomainOverridePhase.id.asc())
            )
        ).scalars().all()
    phases_by_override: dict[int, list[DomainOverridePhase]] = defaultdict(list)
    rule_to_override = {rule.id: rule.domain_rule_override_id for rule in domain_rules}
    for phase in domain_phases:
        override_id = rule_to_override.get(phase.domain_override_rule_id)
        if override_id is not None:
            phases_by_override[override_id].append(phase)

    effective: dict[int, object] = {}
    for domain in domains:
        zone_strategy = strategy_map.get(domain.zone_strategy_id) if domain.zone_strategy_id else None
        domain_override = domain_override_map.get(domain.domain_rule_override_id) if domain.domain_rule_override_id else None
        if zone_strategy is None and domain_override is None:
            continue
        if getattr(domain, "strategy_mode", "inherit_zone") == "manual_override" and domain_override is not None:
            effective[domain.id] = resolve_effective_strategy(
                domain,
                zone_strategy=zone_strategy,
                domain_override=domain_override,
                rules=rules_by_override.get(domain_override.id, []),
                phases=phases_by_override.get(domain_override.id, []),
            )
            continue
        if zone_strategy is None:
            continue
        effective[domain.id] = resolve_effective_strategy(
            domain,
            zone_strategy=zone_strategy,
            domain_override=domain_override,
            rules=rules_by_strategy.get(zone_strategy.id, []),
            phases=phases_by_strategy.get(zone_strategy.id, []),
        )
    return effective


def is_within_window(domain: DropDomain, now: datetime) -> bool:
    bounds = domain_window_bounds(domain, anchor=now)
    if bounds is None:
        return False
    start_at, end_at = bounds
    return start_at <= now <= end_at


def domain_allocation_weight(domain: DropDomain, now: datetime) -> float:
    weight = float(max(1, domain.priority))
    if domain.drop_date == now.astimezone(ZoneInfo(domain.timezone_name or "Europe/Paris")).date():
        weight *= 1.25
    if is_within_window(domain, now):
        weight *= 1.5
    if domain.status == "attacking":
        weight *= 1.15
    return weight


def _assignment_score(
    *,
    domain: DropDomain,
    assigned_rps: float,
    total_target_rps: float,
    total_weight: float,
    now: datetime,
) -> float:
    weight = domain_allocation_weight(domain, now)
    ideal_rps = total_target_rps * (weight / total_weight) if total_weight > 0 else total_target_rps
    deficit = ideal_rps - assigned_rps
    return deficit + (weight / 1000.0)


def allocate_domain_target_rps(
    domains: list[DropDomain],
    *,
    desired_rps_by_domain: dict[int, float],
    minimum_rps_by_domain: dict[int, float],
    total_available_rps: float,
) -> dict[int, float]:
    allocation = {domain.id: 0.0 for domain in domains}
    remaining = max(0.0, float(total_available_rps))
    if remaining <= 0 or not domains:
        return allocation

    desired_caps = {
        domain.id: max(0.0, float(desired_rps_by_domain.get(domain.id, 0.0)))
        for domain in domains
    }
    minimum_caps = {
        domain.id: min(
            desired_caps[domain.id],
            max(0.0, float(minimum_rps_by_domain.get(domain.id, 0.0))),
        )
        for domain in domains
    }

    total_minimum = sum(minimum_caps.values())
    if remaining <= total_minimum:
        return _weighted_fill(
            domains,
            capacities_by_domain=minimum_caps,
            total_available_rps=remaining,
        )

    allocation = minimum_caps.copy()
    remaining -= total_minimum
    extra_caps = {
        domain.id: max(0.0, desired_caps[domain.id] - allocation[domain.id])
        for domain in domains
    }
    extras = _weighted_fill(
        domains,
        capacities_by_domain=extra_caps,
        total_available_rps=remaining,
    )
    for domain in domains:
        allocation[domain.id] = round(allocation[domain.id] + extras[domain.id], 2)
    return allocation


def _weighted_fill(
    domains: list[DropDomain],
    *,
    capacities_by_domain: dict[int, float],
    total_available_rps: float,
) -> dict[int, float]:
    allocation = {domain.id: 0.0 for domain in domains}
    remaining = max(0.0, float(total_available_rps))
    eligible = {
        domain.id: max(0.0, float(capacities_by_domain.get(domain.id, 0.0)))
        for domain in domains
        if capacities_by_domain.get(domain.id, 0.0) > 0
    }

    while remaining > 1e-9 and eligible:
        total_weight = sum(float(max(1, domain.priority)) for domain in domains if domain.id in eligible)
        if total_weight <= 0:
            break
        round_remaining = remaining
        proposed: dict[int, float] = {}
        for domain in domains:
            capacity = eligible.get(domain.id)
            if capacity is None:
                continue
            share = round_remaining * (float(max(1, domain.priority)) / total_weight)
            grant = min(capacity, share)
            if grant <= 0:
                continue
            grant = round(grant, 2)
            if grant <= 0:
                grant = min(capacity, round_remaining)
            proposed[domain.id] = grant
        progress = round(sum(proposed.values()), 2)
        for domain_id, grant in proposed.items():
            allocation[domain_id] = round(allocation[domain_id] + grant, 2)
            eligible[domain_id] = round(eligible[domain_id] - grant, 2)
        remaining = round(max(0.0, remaining - progress), 2)
        eligible = {domain_id: cap for domain_id, cap in eligible.items() if cap > 1e-9}
        if progress <= 0:
            break

    return allocation


def plan_worker_assignments(
    *,
    domains: list[DropDomain],
    workers: list[WorkerNode],
    now: datetime,
    existing_assignments: dict[int, list[WorkerNode]] | None = None,
    domain_target_rps_by_id: dict[int, float] | None = None,
) -> dict[int, list[WorkerNode]]:
    assignments = {domain.id: list((existing_assignments or {}).get(domain.id, [])) for domain in domains}
    assigned_worker_ids = {worker.id for assigned in assignments.values() for worker in assigned}
    free_workers = [worker for worker in workers if worker.id not in assigned_worker_ids]
    total_target_rps = sum(worker.target_rps for worker in workers)
    total_weight = sum(domain_allocation_weight(domain, now) for domain in domains) or 1.0
    domain_map = {domain.id: domain for domain in domains}

    # First pass: guarantee at least one worker for each compatible domain by priority.
    for domain in domains:
        if assignments[domain.id]:
            continue
        for worker in list(free_workers):
            if worker_matches_domain(worker, domain):
                assignments[domain.id].append(worker)
                free_workers.remove(worker)
                break

    # Second pass: allocate remaining workers by deficit versus weighted target share.
    while free_workers:
        best_worker = free_workers[0]
        best_domain_id: int | None = None
        best_score: float | None = None

        for domain_id, domain in domain_map.items():
            if not worker_matches_domain(best_worker, domain):
                continue
            assigned_rps = sum(worker.target_rps for worker in assignments[domain_id])
            if domain_target_rps_by_id is not None:
                score = float(domain_target_rps_by_id.get(domain_id, 0.0) - assigned_rps)
            else:
                score = _assignment_score(
                    domain=domain,
                    assigned_rps=assigned_rps,
                    total_target_rps=total_target_rps,
                    total_weight=total_weight,
                    now=now,
                )
            if best_score is None or score > best_score:
                best_score = score
                best_domain_id = domain_id

        if best_domain_id is None:
            free_workers.pop(0)
            continue

        assignments[best_domain_id].append(best_worker)
        free_workers.pop(0)

    return assignments


def plan_domain_rps_targets(
    domains: list[DropDomain],
    *,
    workers: list[WorkerNode],
    strategy_map: dict[int, object],
    now: datetime,
) -> tuple[dict[int, tuple[datetime, datetime] | None], dict[int, float], dict[int, float]]:
    bounds_by_domain: dict[int, tuple[datetime, datetime] | None] = {}
    desired_rps_by_domain: dict[int, float] = {}
    minimum_rps_by_domain: dict[int, float] = {}
    total_available_rps = sum(worker.target_rps for worker in workers)

    for domain in domains:
        compatible_workers = [worker for worker in workers if worker_matches_domain(worker, domain)]
        bounds, desired_rps, minimum_rps = resolve_domain_budget_requirements(
            domain,
            compatible_workers=compatible_workers,
            effective_strategy=strategy_map.get(domain.id),
            now=now,
        )
        bounds_by_domain[domain.id] = bounds
        desired_rps_by_domain[domain.id] = desired_rps if bounds is not None else 0.0
        minimum_rps_by_domain[domain.id] = minimum_rps if bounds is not None else 0.0

    domain_target_rps_by_id = allocate_domain_target_rps(
        domains,
        desired_rps_by_domain=desired_rps_by_domain,
        minimum_rps_by_domain=minimum_rps_by_domain,
        total_available_rps=total_available_rps,
    )
    return bounds_by_domain, desired_rps_by_domain, domain_target_rps_by_id


def allocate_worker_rps(workers: list[WorkerNode], *, target_rps: float) -> dict[int, float]:
    remaining = max(0.0, float(target_rps))
    allocation: dict[int, float] = {}
    for worker in workers:
        if remaining <= 0:
            break
        planned = min(float(worker.target_rps), remaining)
        allocation[worker.id] = round(planned, 2)
        remaining -= planned
    return allocation


def resolve_task_window_and_rps(
    domain: DropDomain,
    *,
    selected_workers: list[WorkerNode],
    effective_strategy,
    now: datetime,
) -> tuple[tuple[datetime, datetime] | None, float]:
    compatible_capacity = sum(worker.target_rps for worker in selected_workers)
    if effective_strategy is not None and effective_strategy.rules:
        profile = resolve_strategy_runtime_profile(
            domain,
            strategy=effective_strategy,
            now=now,
            compatible_capacity_rps=compatible_capacity,
        )
        if profile is not None:
            override_min_rps = getattr(domain, "override_min_guaranteed_rps", None)
            minimum_rps = float(
                override_min_rps
                if override_min_rps is not None
                else effective_strategy.minimum_guaranteed_rps
            )
            target_rps = min(compatible_capacity, max(minimum_rps, profile.target_rps))
            return (profile.window.start_at, profile.window.end_at), target_rps
    return domain_window_bounds(domain, anchor=now), compatible_capacity


def resolve_domain_budget_requirements(
    domain: DropDomain,
    *,
    compatible_workers: list[WorkerNode],
    effective_strategy,
    now: datetime,
) -> tuple[tuple[datetime, datetime] | None, float, float]:
    compatible_capacity = sum(worker.target_rps for worker in compatible_workers)
    override_min_rps = getattr(domain, "override_min_guaranteed_rps", None)
    minimum_rps = float(
        override_min_rps
        if override_min_rps is not None
        else getattr(effective_strategy, "minimum_guaranteed_rps", 0.0)
    )
    bounds, desired_rps = resolve_task_window_and_rps(
        domain,
        selected_workers=compatible_workers,
        effective_strategy=effective_strategy,
        now=now,
    )
    desired_rps = min(compatible_capacity, desired_rps)
    minimum_rps = min(max(0.0, minimum_rps), desired_rps)
    return bounds, desired_rps, minimum_rps


def build_domain_runtime_snapshots(
    domains: list[DropDomain],
    *,
    workers: list[WorkerNode],
    strategy_map: dict[int, object],
    now: datetime,
    active_run_by_domain_id: dict[int, AttackRun],
    active_tasks_by_domain_id: dict[int, list[WorkerTask]],
) -> dict[int, DomainRuntimeSnapshot]:
    snapshots: dict[int, DomainRuntimeSnapshot] = {}
    for domain in domains:
        if not is_domain_due_today(domain, now):
            snapshots[domain.id] = DomainRuntimeSnapshot(
                minimum_rps=0.0,
                desired_rps=0.0,
                allocated_rps=0.0,
                assigned_worker_count=0,
                phase_name=None,
                attack_run_id=None,
                attack_status=None,
                window_start_at=None,
                window_end_at=None,
            )
            continue
        compatible_workers = [worker for worker in workers if worker_matches_domain(worker, domain)]
        bounds, desired_rps, minimum_rps = resolve_domain_budget_requirements(
            domain,
            compatible_workers=compatible_workers,
            effective_strategy=strategy_map.get(domain.id),
            now=now,
        )
        active_tasks = active_tasks_by_domain_id.get(domain.id, [])
        active_run = active_run_by_domain_id.get(domain.id)
        profile = None
        if strategy_map.get(domain.id) is not None and getattr(strategy_map[domain.id], "rules", None):
            profile = resolve_strategy_runtime_profile(
                domain,
                strategy=strategy_map[domain.id],
                now=now,
                compatible_capacity_rps=sum(worker.target_rps for worker in compatible_workers),
            )
        snapshots[domain.id] = DomainRuntimeSnapshot(
            minimum_rps=round(minimum_rps, 2),
            desired_rps=round(desired_rps, 2),
            allocated_rps=round(sum(float(task.planned_rps) for task in active_tasks), 2),
            assigned_worker_count=len(active_tasks),
            phase_name=getattr(profile.phase, "name", None) if profile is not None else None,
            attack_run_id=getattr(active_run, "id", None),
            attack_status=getattr(active_run, "status", None),
            window_start_at=bounds[0] if bounds is not None else None,
            window_end_at=bounds[1] if bounds is not None else None,
        )
    return snapshots


def refresh_domain_task_targets(
    domain: DropDomain,
    *,
    tasks: list[WorkerTask],
    workers_by_id: dict[int, WorkerNode],
    effective_strategy,
    now: datetime,
    target_rps_override: float | None = None,
) -> float:
    ordered_tasks = [task for task in tasks if task.worker_id in workers_by_id]
    if not ordered_tasks:
        return 0.0

    compatible_workers = [workers_by_id[task.worker_id] for task in ordered_tasks]
    compatible_capacity = sum(worker.target_rps for worker in compatible_workers)
    target_rps = compatible_capacity

    if target_rps_override is not None:
        target_rps = min(compatible_capacity, max(0.0, float(target_rps_override)))
    elif effective_strategy is not None and getattr(effective_strategy, "rules", None):
        profile = resolve_strategy_runtime_profile(
            domain,
            strategy=effective_strategy,
            now=now,
            compatible_capacity_rps=compatible_capacity,
        )
        if profile is not None:
            override_min_rps = getattr(domain, "override_min_guaranteed_rps", None)
            minimum_rps = float(
                override_min_rps
                if override_min_rps is not None
                else effective_strategy.minimum_guaranteed_rps
            )
            target_rps = min(compatible_capacity, max(minimum_rps, profile.target_rps))

    allocation = allocate_worker_rps(compatible_workers, target_rps=target_rps)
    total = 0.0
    for task in ordered_tasks:
        planned = allocation.get(task.worker_id, 0.0)
        task.planned_rps = planned
        total += planned
    return round(total, 2)


async def recompute_run_statistics(session: AsyncSession) -> None:
    runs = (await session.execute(select(AttackRun))).scalars().all()
    active_tasks = (
        await session.execute(select(WorkerTask).where(WorkerTask.status.in_(["queued", "running"])))
    ).scalars().all()
    workers = (await session.execute(select(WorkerNode))).scalars().all()
    worker_map = {worker.id: worker for worker in workers}
    by_run: dict[int, list[WorkerTask]] = defaultdict(list)
    for task in active_tasks:
        by_run[task.attack_run_id].append(task)

    for run in runs:
        tasks = by_run.get(run.id, [])
        run.assigned_worker_count = len(tasks)
        run.planned_rps = round(sum(task.planned_rps for task in tasks), 2)
        run.current_rps = round(sum(task.actual_rps for task in tasks), 2)
        run.max_rps = round(sum(worker_map.get(task.worker_id).max_rps for task in tasks if worker_map.get(task.worker_id)), 2)


async def supervise_worker_pool(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    stall_threshold_seconds: int = 45,
) -> int:
    effective_now = now or utcnow()
    stall_threshold_seconds = max(1, int(stall_threshold_seconds))
    stale_before = effective_now - timedelta(seconds=stall_threshold_seconds)
    workers = (
        await session.execute(
            select(WorkerNode).where(
                WorkerNode.is_enabled.is_(True),
                WorkerNode.status.in_(["ready", "running", "waiting", "busy"]),
            )
        )
    ).scalars().all()
    stalled_workers = [
        worker
        for worker in workers
        if worker.last_heartbeat_at is None or worker.last_heartbeat_at < stale_before
    ]
    if not stalled_workers:
        return 0

    affected_domain_ids: set[int] = set()
    affected_task_count = 0
    for worker in stalled_workers:
        worker.status = "offline"
        worker.current_rps = 0.0
        worker.current_capacity_rps = 0.0
        session.add(
            AttackEvent(
                worker_id=worker.id,
                level="warning",
                event_type="worker_stalled",
                message=f"Worker {worker.name} marked offline after heartbeat stall",
            )
        )
        worker_tasks = (
            await session.execute(
                select(WorkerTask).where(
                    WorkerTask.worker_id == worker.id,
                    WorkerTask.status.in_(["queued", "running"]),
                )
            )
        ).scalars().all()
        for task in worker_tasks:
            task.status = "failed"
            task.finished_at = effective_now
            task.stop_reason = "Worker heartbeat stalled"
            task.last_error = "Worker heartbeat stalled"
            affected_domain_ids.add(task.domain_id)
            affected_task_count += 1
            session.add(
                AttackEvent(
                    attack_run_id=task.attack_run_id,
                    domain_id=task.domain_id,
                    worker_id=worker.id,
                    level="error",
                    event_type="worker_task_stalled",
                    message=f"Task #{task.id} failed because worker {worker.name} stalled",
                )
            )

    if affected_domain_ids:
        domains = (
            await session.execute(select(DropDomain).where(DropDomain.id.in_(affected_domain_ids)))
        ).scalars().all()
        runs = (
            await session.execute(
                select(AttackRun).where(
                    AttackRun.domain_id.in_(affected_domain_ids),
                    AttackRun.status.in_(["planned", "running"]),
                )
            )
        ).scalars().all()
        run_by_domain_id = {run.domain_id: run for run in runs}
        for domain in domains:
            run = run_by_domain_id.get(domain.id)
            if run is None or domain.success_at is not None or not domain.attack_enabled:
                continue
            if effective_now > run.planned_end_at:
                run.status = "failed"
                run.finished_at = effective_now
                run.stop_reason = "All worker tasks stalled after attack window"
                domain.status = "failed"
                continue
            if is_within_window(domain, effective_now):
                run.status = "running"
                run.started_at = run.started_at or effective_now
                domain.status = "attacking"
            else:
                run.status = "planned"
                domain.status = "scheduled"

    await session.flush()
    return affected_task_count


def _coerce_aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo("UTC"))


def _count_consecutive_registered_checks(events: list[AttackEvent]) -> int:
    count = 0
    for event in sorted(events, key=lambda item: item.created_at or datetime.min, reverse=True):
        if event.event_type == "post_window_rdap_registered":
            count += 1
            continue
        if event.event_type == "post_window_rdap_inconclusive":
            break
    return count


async def finalize_expired_attack_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
    confirmation_threshold: int = POST_WINDOW_RDAP_CONFIRMATION_THRESHOLD,
    check_interval_seconds: int = POST_WINDOW_RDAP_CHECK_INTERVAL_SECONDS,
    whois_lookup: WhoisLookup | None = None,
) -> int:
    effective_now = now or utcnow()
    confirmation_threshold = max(1, int(confirmation_threshold))
    check_interval_seconds = max(0, int(check_interval_seconds))
    runs = (
        await session.execute(
            select(AttackRun, DropDomain)
            .join(DropDomain, DropDomain.id == AttackRun.domain_id)
            .where(
                AttackRun.status.in_(["planned", "running", "verifying"]),
                AttackRun.planned_end_at < effective_now,
                DropDomain.attack_enabled.is_(True),
                DropDomain.success_at.is_(None),
            )
            .order_by(AttackRun.planned_end_at.asc(), AttackRun.id.asc())
        )
    ).all()
    if not runs:
        return 0

    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=5.0)
    processed = 0
    try:
        for run, domain in runs:
            if run.status in {"planned", "running"}:
                run.status = "verifying"
                domain.status = "verifying"
                active_tasks = (
                    await session.execute(
                        select(WorkerTask).where(
                            WorkerTask.attack_run_id == run.id,
                            WorkerTask.status.in_(["queued", "planned", "running"]),
                        )
                    )
                ).scalars().all()
                for task in active_tasks:
                    task.status = "cancelled"
                    task.finished_at = effective_now
                    task.stop_reason = "Attack window expired; post-window RDAP verification started"
                session.add(
                    AttackEvent(
                        attack_run_id=run.id,
                        domain_id=domain.id,
                        level="warning",
                        event_type="attack_window_expired",
                        message=f"Attack window expired for {domain.fqdn}; starting post-window RDAP verification",
                        created_at=effective_now,
                    )
                )

            check_events = (
                await session.execute(
                    select(AttackEvent)
                    .where(
                        AttackEvent.attack_run_id == run.id,
                        AttackEvent.event_type.in_(POST_WINDOW_RDAP_CHECK_EVENT_TYPES),
                    )
                    .order_by(AttackEvent.created_at.desc(), AttackEvent.id.desc())
                )
            ).scalars().all()
            last_check_at = _coerce_aware_utc(check_events[0].created_at) if check_events else None
            if last_check_at is not None and (effective_now - last_check_at).total_seconds() < check_interval_seconds:
                continue

            observation = await _check_domain_safety_observation(
                domain,
                client=http_client,
                bootstrap_url=bootstrap_url,
                whois_lookup=whois_lookup,
            )
            is_registered_taken = _is_registered_taken_observation(observation)
            event_type = "post_window_rdap_registered" if is_registered_taken else "post_window_rdap_inconclusive"
            level = "warning" if is_registered_taken else "info"
            message = (
                f"Post-window RDAP check for {domain.fqdn}: "
                f"lifecycle={observation.lifecycle_stage} availability={observation.availability_status} "
                f"http={observation.http_status or 'n/a'} source={observation.source}"
            )
            if observation.error:
                message = f"{message} error={observation.error}"
            session.add(
                AttackEvent(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    level=level,
                    event_type=event_type,
                    message=message,
                    created_at=effective_now,
                )
            )
            processed += 1

            consecutive_registered = _count_consecutive_registered_checks(check_events)
            consecutive_registered = consecutive_registered + 1 if is_registered_taken else 0
            total_checks = len(check_events) + 1
            if consecutive_registered >= confirmation_threshold:
                reason = "Post-window RDAP safety check confirmed domain is already registered"
                run.status = "failed"
                run.finished_at = effective_now
                run.stop_reason = reason
                domain.status = "failed"
                domain.attack_enabled = False
                domain.readiness_reasons = reason
                session.add(
                    AttackEvent(
                        attack_run_id=run.id,
                        domain_id=domain.id,
                        level="error",
                        event_type="post_window_domain_taken",
                        message=f"{domain.fqdn} disabled after {consecutive_registered} consecutive RDAP registered/taken confirmations",
                        created_at=effective_now,
                    )
                )
                continue

            if total_checks >= confirmation_threshold and not is_registered_taken:
                reason = "Post-window RDAP safety checks were inconclusive; domain remains enabled"
                run.status = "failed"
                run.finished_at = effective_now
                run.stop_reason = reason
                domain.status = "queued"
                domain.readiness_reasons = reason
                session.add(
                    AttackEvent(
                        attack_run_id=run.id,
                        domain_id=domain.id,
                        level="info",
                        event_type="post_window_rdap_released",
                        message=f"{domain.fqdn} remains enabled after inconclusive post-window RDAP checks",
                        created_at=effective_now,
                    )
                )
    finally:
        if close_client:
            await http_client.aclose()

    await recompute_worker_domain_counts(session)
    await recompute_run_statistics(session)
    return processed


async def recompute_worker_domain_counts(session: AsyncSession) -> None:
    result = await session.execute(
        select(WorkerTask.worker_id, WorkerTask.id)
        .where(WorkerTask.status.in_(["queued", "running", "planned"]))
    )
    counts: dict[int, int] = defaultdict(int)
    for worker_id, _task_id in result.all():
        counts[worker_id] += 1
    workers = (await session.execute(select(WorkerNode))).scalars().all()
    for worker in workers:
        worker.current_domain_count = int(counts.get(worker.id, 0))


async def plan_attack_runs(
    session: AsyncSession,
    *,
    domains: list[DropDomain],
    workers: list[WorkerNode],
    now: datetime,
    force_rebuild: bool = False,
) -> list[AttackRun]:
    if not domains or not workers:
        return []

    domain_ids = [domain.id for domain in domains]
    if force_rebuild:
        runs = (
            await session.execute(
                select(AttackRun).where(
                    AttackRun.domain_id.in_(domain_ids),
                    AttackRun.status.in_(["planned", "running"]),
                )
            )
        ).scalars().all()
        for run in runs:
            run.status = "stopped"
            run.finished_at = now
            run.stop_reason = "Force rebuild requested"
        tasks = (
            await session.execute(
                select(WorkerTask).where(
                    WorkerTask.domain_id.in_(domain_ids),
                    WorkerTask.status.in_(["queued", "planned", "running"]),
                )
            )
        ).scalars().all()
        for task in tasks:
            task.status = "cancelled"
            task.finished_at = now
            task.stop_reason = "Force rebuild requested"
    else:
        existing_domain_ids = set(
            (
                await session.execute(
                    select(AttackRun.domain_id).where(
                        AttackRun.domain_id.in_(domain_ids),
                        AttackRun.status.in_(["planned", "running"]),
                    )
                )
            ).scalars().all()
        )
        domains = [domain for domain in domains if domain.id not in existing_domain_ids]
        if not domains:
            return []

    strategy_map = await load_effective_strategies(session, domains)
    bounds_by_domain, _desired_rps_by_domain, domain_target_rps_by_id = plan_domain_rps_targets(
        domains,
        workers=workers,
        strategy_map=strategy_map,
        now=now,
    )
    assignments = plan_initial_run_workers(
        domains=domains,
        workers=workers,
        now=now,
        domain_target_rps_by_id=domain_target_rps_by_id,
    )
    created_runs: list[AttackRun] = []
    for domain in domains:
        selected_workers = assignments.get(domain.id, [])
        if not selected_workers:
            session.add(
                AttackEvent(
                    domain_id=domain.id,
                    level="warning",
                    event_type="attack_plan_skipped",
                    message=f"No compatible workers available for {domain.fqdn}",
                )
            )
            continue

        bounds = bounds_by_domain.get(domain.id)
        target_rps = float(domain_target_rps_by_id.get(domain.id, 0.0))
        if bounds is None:
            session.add(
                AttackEvent(
                    domain_id=domain.id,
                    level="warning",
                    event_type="attack_window_closed",
                    message=f"No remaining attack windows today for {domain.fqdn}",
                )
            )
            continue
        if target_rps <= 0:
            continue

        planned_start_at, planned_end_at = bounds
        is_running = planned_start_at <= now <= planned_end_at and is_domain_due_today(domain, now)
        worker_rps_plan = allocate_worker_rps(selected_workers, target_rps=target_rps)
        planned_workers = [worker for worker in selected_workers if worker.id in worker_rps_plan]
        if not planned_workers:
            planned_workers = selected_workers[:1]
            worker_rps_plan = allocate_worker_rps(planned_workers, target_rps=planned_workers[0].target_rps)
        run = AttackRun(
            domain_id=domain.id,
            status="running" if is_running else "planned",
            planned_start_at=planned_start_at,
            planned_end_at=planned_end_at,
            started_at=now if is_running else None,
            assigned_worker_count=len(planned_workers),
            planned_rps=target_rps,
            current_rps=sum(min(worker.current_rps, worker_rps_plan.get(worker.id, 0.0)) for worker in planned_workers) if is_running else 0.0,
            max_rps=sum(worker.max_rps for worker in planned_workers),
        )
        session.add(run)
        await session.flush()
        for worker in planned_workers:
            session.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=worker.id,
                    status="running" if is_running else "queued",
                    planned_rps=worker_rps_plan.get(worker.id, worker.target_rps),
                    actual_rps=min(worker.current_rps, worker_rps_plan.get(worker.id, worker.target_rps)) if is_running else 0.0,
                    assigned_at=now,
                    started_at=now if is_running else None,
                )
            )
        domain.status = "attacking" if is_running else "scheduled"
        domain.updated_at = now
        created_runs.append(run)
        session.add(
            AttackEvent(
                attack_run_id=run.id,
                domain_id=domain.id,
                level="info",
                event_type="attack_planned",
                message=f"Attack planned for {domain.fqdn}; workers={len(planned_workers)} planned_rps={run.planned_rps:.2f}",
            )
        )

    await recompute_worker_domain_counts(session)
    await recompute_run_statistics(session)
    return created_runs


async def plan_immediate_registration_runs(
    session: AsyncSession,
    *,
    domains: list[DropDomain],
    workers: list[WorkerNode],
    now: datetime,
    duration_seconds: int = 95,
    force_rebuild: bool = True,
) -> list[AttackRun]:
    if not domains or not workers:
        return []

    duration_seconds = min(max(int(duration_seconds), 5), 600)
    domain_ids = [domain.id for domain in domains]
    if force_rebuild:
        runs = (
            await session.execute(
                select(AttackRun).where(
                    AttackRun.domain_id.in_(domain_ids),
                    AttackRun.status.in_(["planned", "running", "verifying"]),
                )
            )
        ).scalars().all()
        for run in runs:
            run.status = "stopped"
            run.finished_at = now
            run.stop_reason = "Manual registration simulation replaced this run"
        tasks = (
            await session.execute(
                select(WorkerTask).where(
                    WorkerTask.domain_id.in_(domain_ids),
                    WorkerTask.status.in_(["queued", "planned", "running"]),
                )
            )
        ).scalars().all()
        for task in tasks:
            task.status = "cancelled"
            task.finished_at = now
            task.stop_reason = "Manual registration simulation replaced this task"
    else:
        existing_domain_ids = set(
            (
                await session.execute(
                    select(AttackRun.domain_id).where(
                        AttackRun.domain_id.in_(domain_ids),
                        AttackRun.status.in_(["planned", "running", "verifying"]),
                    )
                )
            ).scalars().all()
        )
        domains = [domain for domain in domains if domain.id not in existing_domain_ids]
        if not domains:
            return []

    assignments = plan_initial_run_workers(
        domains=domains,
        workers=workers,
        now=now,
        domain_target_rps_by_id={
            domain.id: sum(worker.target_rps for worker in workers if worker_matches_domain(worker, domain))
            for domain in domains
        },
    )
    created_runs: list[AttackRun] = []
    planned_end_at = now + timedelta(seconds=duration_seconds)
    for domain in domains:
        selected_workers = assignments.get(domain.id, [])
        selected_workers = [worker for worker in selected_workers if worker_matches_domain(worker, domain)]
        if not selected_workers:
            session.add(
                AttackEvent(
                    domain_id=domain.id,
                    level="warning",
                    event_type="registration_simulation_skipped",
                    message=f"No compatible workers available for immediate registration simulation of {domain.fqdn}",
                    created_at=now,
                )
            )
            continue

        target_rps = sum(worker.target_rps for worker in selected_workers)
        if target_rps <= 0:
            continue
        worker_rps_plan = allocate_worker_rps(selected_workers, target_rps=target_rps)
        planned_workers = [worker for worker in selected_workers if worker.id in worker_rps_plan]
        run = AttackRun(
            domain_id=domain.id,
            status="running",
            planned_start_at=now,
            planned_end_at=planned_end_at,
            started_at=now,
            assigned_worker_count=len(planned_workers),
            planned_rps=round(target_rps, 2),
            current_rps=0.0,
            max_rps=sum(worker.max_rps for worker in planned_workers),
        )
        session.add(run)
        await session.flush()
        for worker in planned_workers:
            session.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=worker.id,
                    status="queued",
                    planned_rps=worker_rps_plan.get(worker.id, worker.target_rps),
                    actual_rps=0.0,
                    assigned_at=now,
                )
            )
        domain.status = "attacking"
        domain.updated_at = now
        created_runs.append(run)
        session.add(
            AttackEvent(
                attack_run_id=run.id,
                domain_id=domain.id,
                level="warning",
                event_type="registration_simulation_planned",
                message=(
                    f"Immediate registration simulation started for {domain.fqdn}; "
                    f"workers={len(planned_workers)} planned_rps={run.planned_rps:.2f} "
                    f"duration={duration_seconds}s"
                ),
                created_at=now,
            )
        )

    await recompute_worker_domain_counts(session)
    await recompute_run_statistics(session)
    return created_runs


async def autoplan_due_attack_runs(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    client: httpx.AsyncClient | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
) -> list[AttackRun]:
    effective_now = now or utcnow()
    domains = (
        await session.execute(
            select(DropDomain).where(DropDomain.attack_enabled.is_(True))
        )
    ).scalars().all()
    active_run_domain_ids = set(
        (
            await session.execute(
                select(AttackRun.domain_id).where(AttackRun.status.in_(["planned", "running"]))
            )
        ).scalars().all()
    )
    workers = (
        await session.execute(
            select(WorkerNode)
            .where(WorkerNode.is_enabled.is_(True))
            .order_by(WorkerNode.target_rps.desc(), WorkerNode.max_rps.desc(), WorkerNode.name.asc())
        )
    ).scalars().all()
    if not workers:
        return []
    strategy_map = await load_effective_strategies(session, domains)
    bounds_by_domain_id, _desired_rps_by_domain, _domain_target_rps_by_id = plan_domain_rps_targets(
        domains,
        workers=workers,
        strategy_map=strategy_map,
        now=effective_now,
    )
    selected_domains = select_domains_for_autoplanning(
        domains,
        now=effective_now,
        active_run_domain_ids=active_run_domain_ids,
        bounds_by_domain_id=bounds_by_domain_id,
    )
    if not selected_domains:
        return []
    selected_domains = await _filter_domains_available_for_autostart(
        session,
        selected_domains,
        now=effective_now,
        client=client,
        bootstrap_url=bootstrap_url,
    )
    if not selected_domains:
        return []

    return await plan_attack_runs(
        session,
        domains=selected_domains,
        workers=workers,
        now=effective_now,
        force_rebuild=False,
    )


async def refresh_active_task_targets(session: AsyncSession, *, now: datetime | None = None) -> int:
    effective_now = now or utcnow()
    runs = (
        await session.execute(
            select(AttackRun, DropDomain)
            .join(DropDomain, DropDomain.id == AttackRun.domain_id)
            .where(
                AttackRun.status.in_(["planned", "running"]),
                DropDomain.attack_enabled.is_(True),
                DropDomain.success_at.is_(None),
                DropDomain.status.in_(["queued", "scheduled", "attacking"]),
            )
        )
    ).all()
    if not runs:
        return 0

    domains = [domain for _, domain in runs]
    run_by_domain_id = {domain.id: run for run, domain in runs}
    tasks = (
        await session.execute(
            select(WorkerTask).where(
                WorkerTask.attack_run_id.in_([run.id for run, _domain in runs]),
                WorkerTask.status.in_(["queued", "planned", "running"]),
            )
        )
    ).scalars().all()
    if not tasks:
        return 0

    workers = (
        await session.execute(select(WorkerNode).where(WorkerNode.id.in_([task.worker_id for task in tasks])))
    ).scalars().all()
    workers_by_id = {worker.id: worker for worker in workers}
    tasks_by_domain: dict[int, list[WorkerTask]] = defaultdict(list)
    for task in tasks:
        tasks_by_domain[task.domain_id].append(task)

    strategy_map = await load_effective_strategies(session, domains)
    _bounds_by_domain, _desired_rps_by_domain, domain_target_rps_by_id = plan_domain_rps_targets(
        domains,
        workers=workers,
        strategy_map=strategy_map,
        now=effective_now,
    )
    updated = 0
    for domain in domains:
        domain_tasks = tasks_by_domain.get(domain.id, [])
        if not domain_tasks:
            continue
        previous_values = [task.planned_rps for task in domain_tasks]
        total = refresh_domain_task_targets(
            domain,
            tasks=domain_tasks,
            workers_by_id=workers_by_id,
            effective_strategy=strategy_map.get(domain.id),
            now=effective_now,
            target_rps_override=domain_target_rps_by_id.get(domain.id),
        )
        run = run_by_domain_id[domain.id]
        run.planned_rps = total
        run.assigned_worker_count = len(domain_tasks)
        if any(before != after.planned_rps for before, after in zip(previous_values, domain_tasks)):
            updated += 1

    await recompute_run_statistics(session)
    return updated


async def rebalance_worker_pool(session: AsyncSession, *, now: datetime | None = None) -> int:
    now = now or utcnow()
    workers = (
        await session.execute(
            select(WorkerNode)
            .where(WorkerNode.is_enabled.is_(True))
            .order_by(WorkerNode.target_rps.desc(), WorkerNode.max_rps.desc(), WorkerNode.name.asc())
        )
    ).scalars().all()
    active_tasks = (await session.execute(select(WorkerTask).where(WorkerTask.status.in_(["queued", "running"])))).scalars().all()
    runs = (
        await session.execute(
            select(AttackRun, DropDomain)
            .join(DropDomain, DropDomain.id == AttackRun.domain_id)
            .where(
                AttackRun.status.in_(["planned", "running"]),
                DropDomain.attack_enabled.is_(True),
                DropDomain.success_at.is_(None),
                DropDomain.status.in_(["queued", "scheduled", "attacking"]),
            )
            .order_by(DropDomain.priority.desc(), AttackRun.created_at.asc())
        )
    ).all()

    if not runs:
        await recompute_run_statistics(session)
        return 0

    domains = [domain for _, domain in runs]
    run_by_domain_id = {domain.id: run for run, domain in runs}
    worker_map = {worker.id: worker for worker in workers}
    current_assignments: dict[int, list[WorkerNode]] = defaultdict(list)
    current_planned_rps_by_domain: dict[int, float] = defaultdict(float)
    for task in active_tasks:
        worker = worker_map.get(task.worker_id)
        if worker is not None:
            current_assignments[task.domain_id].append(worker)
        current_planned_rps_by_domain[task.domain_id] += task.planned_rps

    strategy_map = await load_effective_strategies(session, domains)
    _bounds_by_domain, _desired_rps_by_domain, domain_target_rps_by_id = plan_domain_rps_targets(
        domains,
        workers=workers,
        strategy_map=strategy_map,
        now=now,
    )
    desired_assignments = plan_worker_assignments(
        domains=domains,
        workers=workers,
        now=now,
        existing_assignments=current_assignments,
        domain_target_rps_by_id=domain_target_rps_by_id,
    )

    created = 0
    for domain in domains:
        run = run_by_domain_id[domain.id]
        run.planned_rps = float(domain_target_rps_by_id.get(domain.id, run.planned_rps))
        within_window = is_within_window(domain, now)
        if within_window and run.status == "planned":
            run.status = "running"
            run.started_at = run.started_at or now
            domain.status = "attacking"
        if not within_window and run.planned_end_at < now:
            continue
        if current_planned_rps_by_domain.get(domain.id, 0.0) >= run.planned_rps:
            continue
        already_assigned = {worker.id for worker in current_assignments.get(domain.id, [])}
        for worker in desired_assignments.get(domain.id, []):
            if worker.id in already_assigned:
                continue
            remaining_target = max(0.0, run.planned_rps - current_planned_rps_by_domain.get(domain.id, 0.0))
            if remaining_target <= 0:
                break
            planned_rps = min(worker.target_rps, remaining_target)
            session.add(
                WorkerTask(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=worker.id,
                    status="running" if within_window else "queued",
                    planned_rps=planned_rps,
                    actual_rps=0.0,
                    assigned_at=now,
                    started_at=now if within_window else None,
                )
            )
            session.add(
                AttackEvent(
                    attack_run_id=run.id,
                    domain_id=domain.id,
                    worker_id=worker.id,
                    level="info",
                    event_type="worker_rebalanced",
                    message=f"Worker {worker.name} rebalanced to domain {domain.fqdn}",
                )
            )
            current_planned_rps_by_domain[domain.id] += planned_rps
            created += 1

    await recompute_run_statistics(session)
    return created


def plan_initial_run_workers(
    *,
    domains: list[DropDomain],
    workers: list[WorkerNode],
    now: datetime,
    domain_target_rps_by_id: dict[int, float] | None = None,
) -> dict[int, list[WorkerNode]]:
    return plan_worker_assignments(
        domains=domains,
        workers=workers,
        now=now,
        existing_assignments=None,
        domain_target_rps_by_id=domain_target_rps_by_id,
    )
