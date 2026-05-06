from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(slots=True)
class ResolvedStrategySource:
    source: str
    strategy: object


@dataclass(slots=True)
class EffectiveStrategy:
    source: str
    strategy_id: int | None
    timezone_name: str
    rule_resolution_mode: str
    minimum_guaranteed_rps: float
    rules: list[object]
    phases_by_rule_id: dict[int, list[object]]


@dataclass(slots=True)
class RuleWindowMatch:
    rule_id: int
    priority: int
    start_at: datetime
    end_at: datetime
    rule_name: str | None = None


@dataclass(slots=True)
class StrategyPreview:
    resolution_mode: str
    windows: list[RuleWindowMatch]


@dataclass(slots=True)
class StrategyRuntimeProfile:
    window: RuleWindowMatch
    phase: object | None
    target_rps: float


@dataclass(slots=True)
class DomainReadinessResult:
    status: str
    reasons: list[str]


def resolve_strategy_source(domain, *, zone_strategy, domain_override):
    if getattr(domain, "strategy_mode", "inherit_zone") == "manual_override" and domain_override is not None:
        return ResolvedStrategySource(source="domain", strategy=domain_override)
    if zone_strategy is None:
        raise ValueError("Zone strategy is required when domain inherits zone settings")
    return ResolvedStrategySource(source="zone", strategy=zone_strategy)


def resolve_effective_strategy(domain, *, zone_strategy, domain_override, rules, phases):
    resolved = resolve_strategy_source(domain, zone_strategy=zone_strategy, domain_override=domain_override)
    strategy = resolved.strategy
    grouped_phases: dict[int, list[object]] = {}
    for phase in phases:
        owner_rule_id = getattr(phase, "zone_rule_id", None)
        if owner_rule_id is None:
            owner_rule_id = getattr(phase, "domain_override_rule_id", None)
        if owner_rule_id is None:
            continue
        grouped_phases.setdefault(owner_rule_id, []).append(phase)
    for items in grouped_phases.values():
        items.sort(key=lambda item: (getattr(item, "sort_order", 0), getattr(item, "id", 0)))
    return EffectiveStrategy(
        source=resolved.source,
        strategy_id=getattr(strategy, "id", None),
        timezone_name=getattr(strategy, "timezone_name", "UTC"),
        rule_resolution_mode=getattr(strategy, "rule_resolution_mode", "priority"),
        minimum_guaranteed_rps=float(getattr(strategy, "default_min_guaranteed_rps", 1.0)),
        rules=list(rules),
        phases_by_rule_id=grouped_phases,
    )


def match_rule_windows(domain, *, strategy, rules, now: datetime) -> list[RuleWindowMatch]:
    tz = ZoneInfo(strategy.timezone_name)
    localized_now = now.astimezone(tz)
    if localized_now.date() != domain.drop_date:
        return []

    matches: list[RuleWindowMatch] = []
    for rule in rules:
        if not getattr(rule, "is_enabled", True):
            continue
        schedule_type = getattr(rule, "schedule_type", "hourly")
        start_at = _resolve_rule_start(localized_now.date(), localized_now, rule, tz)
        if start_at is None:
            continue
        end_at = start_at + timedelta(seconds=rule.window_duration_seconds)
        matches.append(
            RuleWindowMatch(
                rule_id=rule.id,
                priority=getattr(rule, "priority", 100),
                start_at=start_at.astimezone(timezone.utc),
                end_at=end_at.astimezone(timezone.utc),
                rule_name=getattr(rule, "name", None),
            )
        )
    return matches


def evaluate_domain_readiness(domain, *, effective_strategy) -> DomainReadinessResult:
    reasons: list[str] = []
    if effective_strategy is None:
        reasons.append("strategy is missing")
    elif not getattr(effective_strategy, "rules", []):
        reasons.append("strategy rules are missing")
    if not getattr(domain, "registrar_account_id", None):
        reasons.append("registrar account is missing")
    if not getattr(domain, "contact_profile_id", None):
        reasons.append("contact profile is missing")
    if not getattr(domain, "drop_date", None):
        reasons.append("drop date is missing")
    return DomainReadinessResult(status="draft" if reasons else "ready", reasons=reasons)


def is_domain_due_today(domain, now: datetime) -> bool:
    timezone_name = getattr(domain, "timezone_name", None) or "UTC"
    return now.astimezone(ZoneInfo(timezone_name)).date() == getattr(domain, "drop_date", None)


def preview_strategy_windows(domain, *, strategy, rules, target_date) -> StrategyPreview:
    tz = ZoneInfo(strategy.timezone_name)
    preview_anchor = datetime.combine(target_date, time(12, 0, 0), tzinfo=tz).astimezone(timezone.utc)
    preview_domain = type("PreviewDomain", (), {"drop_date": target_date})()
    matches = match_rule_windows(preview_domain, strategy=strategy, rules=rules, now=preview_anchor)
    matches.sort(key=lambda item: (-item.priority, item.start_at, item.rule_id))
    resolution_mode = getattr(strategy, "rule_resolution_mode", "priority")
    if resolution_mode == "priority" and matches:
        return StrategyPreview(resolution_mode=resolution_mode, windows=[matches[0]])
    return StrategyPreview(resolution_mode=resolution_mode, windows=matches)


def resolve_strategy_window(domain, *, strategy, rules, now: datetime) -> RuleWindowMatch | None:
    tz = ZoneInfo(strategy.timezone_name)
    localized_now = now.astimezone(tz)
    if localized_now.date() != domain.drop_date:
        return None

    candidates: list[RuleWindowMatch] = []
    for rule in rules:
        if not getattr(rule, "is_enabled", True):
            continue
        start_at = _resolve_runtime_rule_start(domain.drop_date, localized_now, rule, tz)
        if start_at is None:
            continue
        end_at = start_at + timedelta(seconds=rule.window_duration_seconds)
        if end_at.astimezone(timezone.utc) < now:
            continue
        candidates.append(
            RuleWindowMatch(
                rule_id=rule.id,
                priority=getattr(rule, "priority", 100),
                start_at=start_at.astimezone(timezone.utc),
                end_at=end_at.astimezone(timezone.utc),
                rule_name=getattr(rule, "name", None),
            )
        )

    if not candidates:
        return None

    resolution_mode = getattr(strategy, "rule_resolution_mode", "priority")
    if resolution_mode == "priority":
        candidates.sort(
            key=lambda item: (
                0 if item.start_at <= now <= item.end_at else 1,
                -item.priority,
                item.start_at,
                item.rule_id,
            )
        )
    else:
        candidates.sort(
            key=lambda item: (
                0 if item.start_at <= now <= item.end_at else 1,
                item.start_at,
                -item.priority,
                item.rule_id,
            )
        )
    return candidates[0]


def resolve_active_phase(*, window, phases, now: datetime):
    if not phases:
        return None
    elapsed_seconds = max(0.0, (min(now, window.end_at) - window.start_at).total_seconds())
    for phase in phases:
        phase_start = float(getattr(phase, "start_offset_seconds", 0))
        phase_duration = float(getattr(phase, "duration_seconds", 0))
        phase_end = phase_start + phase_duration if phase_duration > 0 else None
        if elapsed_seconds < phase_start:
            continue
        if phase_end is None or elapsed_seconds < phase_end:
            return phase
    return phases[-1]


def calculate_phase_target_rps(phase, *, compatible_capacity_rps: float) -> float:
    if phase is None:
        return float(max(0.0, compatible_capacity_rps))
    mode = getattr(phase, "rps_mode", "percent")
    value = float(getattr(phase, "rps_value", compatible_capacity_rps))
    if mode == "fixed":
        return max(0.0, value)
    return max(0.0, compatible_capacity_rps * (value / 100.0))


def resolve_strategy_runtime_profile(domain, *, strategy, now: datetime, compatible_capacity_rps: float) -> StrategyRuntimeProfile | None:
    window = resolve_strategy_window(domain, strategy=strategy, rules=strategy.rules, now=now)
    if window is None:
        return None
    phase = resolve_active_phase(
        window=window,
        phases=strategy.phases_by_rule_id.get(window.rule_id, []),
        now=now,
    )
    target_rps = calculate_phase_target_rps(phase, compatible_capacity_rps=compatible_capacity_rps)
    return StrategyRuntimeProfile(window=window, phase=phase, target_rps=target_rps)


def _resolve_rule_start(target_date, localized_now: datetime, rule, tz: ZoneInfo) -> datetime | None:
    schedule_type = getattr(rule, "schedule_type", "hourly")
    hour = getattr(rule, "hour", None)
    minute = getattr(rule, "minute", 0)
    second = getattr(rule, "second", 0)

    if schedule_type == "hourly":
        return datetime.combine(
            target_date,
            time(hour=localized_now.hour, minute=minute, second=second),
            tzinfo=tz,
        )

    if schedule_type == "daily":
        return datetime.combine(
            target_date,
            time(hour=hour or 0, minute=minute, second=second),
            tzinfo=tz,
        )

    if schedule_type == "weekly":
        weekdays_raw = getattr(rule, "weekdays", None) or ""
        allowed_weekdays = {int(item.strip()) for item in weekdays_raw.split(",") if item.strip()}
        if target_date.isoweekday() not in allowed_weekdays:
            return None
        return datetime.combine(
            target_date,
            time(hour=hour or 0, minute=minute, second=second),
            tzinfo=tz,
        )

    if schedule_type == "one_time":
        specific_date = getattr(rule, "specific_date", None)
        if specific_date != target_date:
            return None
        return datetime.combine(
            target_date,
            time(hour=hour or 0, minute=minute, second=second),
            tzinfo=tz,
        )

    return None


def _resolve_runtime_rule_start(target_date, localized_now: datetime, rule, tz: ZoneInfo) -> datetime | None:
    schedule_type = getattr(rule, "schedule_type", "hourly")
    start_at = _resolve_rule_start(target_date, localized_now, rule, tz)
    if start_at is None:
        return None

    if schedule_type == "hourly":
        end_current = start_at + timedelta(seconds=rule.window_duration_seconds)
        if localized_now > end_current:
            next_hour = localized_now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            if next_hour.date() != target_date:
                return None
            return datetime.combine(
                next_hour.date(),
                time(hour=next_hour.hour, minute=getattr(rule, "minute", 0), second=getattr(rule, "second", 0)),
                tzinfo=tz,
            )
        return start_at

    end_at = start_at + timedelta(seconds=rule.window_duration_seconds)
    if localized_now > end_at:
        return None
    return start_at
