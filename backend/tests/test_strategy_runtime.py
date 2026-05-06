from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services.strategy_runtime import (
    calculate_phase_target_rps,
    evaluate_domain_readiness,
    is_domain_due_today,
    match_rule_windows,
    preview_strategy_windows,
    resolve_active_phase,
    resolve_strategy_runtime_profile,
    resolve_strategy_window,
    resolve_effective_strategy,
    resolve_strategy_source,
)


def test_resolve_strategy_source_prefers_zone_inheritance():
    domain = SimpleNamespace(
        strategy_mode="inherit_zone",
        zone="fr",
        zone_strategy_id=10,
        drop_date=date(2026, 5, 1),
    )
    zone_strategy = SimpleNamespace(id=10, zone="fr", timezone_name="Europe/Paris")

    resolved = resolve_strategy_source(domain, zone_strategy=zone_strategy, domain_override=None)

    assert resolved.source == "zone"
    assert resolved.strategy.id == 10


def test_resolve_effective_strategy_uses_domain_override_when_requested():
    domain = SimpleNamespace(
        strategy_mode="manual_override",
        zone="fr",
        zone_strategy_id=10,
        drop_date=date(2026, 5, 1),
    )
    zone_strategy = SimpleNamespace(id=10, zone="fr", timezone_name="Europe/Paris")
    domain_override = SimpleNamespace(
        id=20,
        timezone_name="Europe/Paris",
        rule_resolution_mode="merge",
        default_min_guaranteed_rps=3.0,
    )

    resolved = resolve_effective_strategy(
        domain,
        zone_strategy=zone_strategy,
        domain_override=domain_override,
        rules=[],
        phases=[],
    )

    assert resolved.source == "domain"
    assert resolved.rule_resolution_mode == "merge"
    assert resolved.minimum_guaranteed_rps == 3.0


def test_resolve_effective_strategy_groups_domain_override_phases_by_domain_rule():
    domain = SimpleNamespace(
        strategy_mode="manual_override",
        zone="fr",
        zone_strategy_id=10,
        drop_date=date(2026, 5, 1),
    )
    zone_strategy = SimpleNamespace(id=10, zone="fr", timezone_name="Europe/Paris")
    domain_override = SimpleNamespace(
        id=20,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
        default_min_guaranteed_rps=2.0,
    )
    rule = SimpleNamespace(
        id=301,
        name="Domain burst",
        is_enabled=True,
        schedule_type="hourly",
        hour=None,
        minute=31,
        second=59,
        weekdays=None,
        specific_date=None,
        window_duration_seconds=61,
        priority=100,
        execution_profile_mode="phased",
    )
    phase = SimpleNamespace(
        id=401,
        domain_override_rule_id=301,
        name="burst",
        sort_order=0,
        start_offset_seconds=0,
        duration_seconds=61,
        rps_mode="fixed",
        rps_value=9.0,
    )

    resolved = resolve_effective_strategy(
        domain,
        zone_strategy=zone_strategy,
        domain_override=domain_override,
        rules=[rule],
        phases=[phase],
    )

    assert resolved.source == "domain"
    assert resolved.rules[0].id == 301
    assert resolved.phases_by_rule_id[301][0].name == "burst"


def test_match_rule_windows_returns_hourly_window_for_drop_day():
    strategy = SimpleNamespace(timezone_name="Europe/Paris", rule_resolution_mode="priority")
    rule = SimpleNamespace(
        id=1,
        is_enabled=True,
        schedule_type="hourly",
        hour=None,
        minute=31,
        second=59,
        weekdays=None,
        specific_date=None,
        window_duration_seconds=61,
        priority=100,
    )
    domain = SimpleNamespace(drop_date=date(2026, 5, 1))

    matches = match_rule_windows(
        domain,
        strategy=strategy,
        rules=[rule],
        now=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
    )

    assert len(matches) == 1
    assert matches[0].rule_id == 1


def test_is_domain_due_today_uses_domain_timezone():
    domain = SimpleNamespace(drop_date=date(2026, 5, 2), timezone_name="Pacific/Auckland")

    assert is_domain_due_today(domain, datetime(2026, 5, 1, 12, 30, tzinfo=timezone.utc)) is True


def test_match_rule_windows_supports_daily_and_weekly_rules():
    strategy = SimpleNamespace(timezone_name="Europe/Paris", rule_resolution_mode="merge")
    domain = SimpleNamespace(drop_date=date(2026, 5, 5))
    rules = [
        SimpleNamespace(
            id=1,
            is_enabled=True,
            schedule_type="daily",
            hour=9,
            minute=0,
            second=0,
            weekdays=None,
            specific_date=None,
            window_duration_seconds=120,
            priority=50,
        ),
        SimpleNamespace(
            id=2,
            is_enabled=True,
            schedule_type="weekly",
            hour=11,
            minute=30,
            second=0,
            weekdays="2,4",
            specific_date=None,
            window_duration_seconds=180,
            priority=70,
        ),
    ]

    matches = match_rule_windows(
        domain,
        strategy=strategy,
        rules=rules,
        now=datetime(2026, 5, 5, 8, 0, tzinfo=timezone.utc),
    )

    assert [match.rule_id for match in matches] == [1, 2]


def test_preview_strategy_windows_prefers_highest_priority_for_priority_mode():
    strategy = SimpleNamespace(
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
        default_min_guaranteed_rps=1.0,
    )
    domain = SimpleNamespace(drop_date=date(2026, 5, 5))
    rules = [
        SimpleNamespace(
            id=10,
            name="Lower",
            is_enabled=True,
            schedule_type="daily",
            hour=9,
            minute=0,
            second=0,
            weekdays=None,
            specific_date=None,
            window_duration_seconds=60,
            priority=10,
        ),
        SimpleNamespace(
            id=20,
            name="Higher",
            is_enabled=True,
            schedule_type="daily",
            hour=10,
            minute=0,
            second=0,
            weekdays=None,
            specific_date=None,
            window_duration_seconds=60,
            priority=99,
        ),
    ]

    preview = preview_strategy_windows(domain, strategy=strategy, rules=rules, target_date=date(2026, 5, 5))

    assert preview.resolution_mode == "priority"
    assert [window.rule_id for window in preview.windows] == [20]


def test_resolve_strategy_window_rolls_hourly_rule_to_next_hour_inside_drop_day():
    strategy = SimpleNamespace(timezone_name="Europe/Paris", rule_resolution_mode="priority")
    domain = SimpleNamespace(drop_date=date(2026, 5, 5))
    rules = [
        SimpleNamespace(
            id=1,
            name="FR hourly",
            is_enabled=True,
            schedule_type="hourly",
            hour=None,
            minute=31,
            second=59,
            weekdays=None,
            specific_date=None,
            window_duration_seconds=61,
            priority=100,
            execution_profile_mode="flat",
        )
    ]

    window = resolve_strategy_window(
        domain,
        strategy=strategy,
        rules=rules,
        now=datetime(2026, 5, 5, 13, 33, 5, tzinfo=timezone.utc),
    )

    assert window is not None
    assert window.rule_id == 1
    assert window.start_at.minute == 31
    assert window.start_at.second == 59


def test_resolve_active_phase_returns_current_phase_for_running_window():
    phases = [
        SimpleNamespace(
            id=1,
            zone_rule_id=10,
            name="prefire",
            sort_order=0,
            start_offset_seconds=0,
            duration_seconds=10,
            rps_mode="percent",
            rps_value=25.0,
        ),
        SimpleNamespace(
            id=2,
            zone_rule_id=10,
            name="burst",
            sort_order=1,
            start_offset_seconds=10,
            duration_seconds=20,
            rps_mode="fixed",
            rps_value=12.0,
        ),
    ]
    window = SimpleNamespace(
        rule_id=10,
        start_at=datetime(2026, 5, 5, 12, 0, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 5, 5, 12, 1, 0, tzinfo=timezone.utc),
    )

    phase = resolve_active_phase(window=window, phases=phases, now=datetime(2026, 5, 5, 12, 0, 15, tzinfo=timezone.utc))

    assert phase is not None
    assert phase.name == "burst"


def test_calculate_phase_target_rps_supports_percent_and_fixed_modes():
    percent_phase = SimpleNamespace(rps_mode="percent", rps_value=50.0)
    fixed_phase = SimpleNamespace(rps_mode="fixed", rps_value=9.0)

    assert calculate_phase_target_rps(percent_phase, compatible_capacity_rps=20.0) == 10.0
    assert calculate_phase_target_rps(fixed_phase, compatible_capacity_rps=20.0) == 9.0


def test_resolve_strategy_runtime_profile_uses_phase_target_when_window_is_active():
    domain = SimpleNamespace(drop_date=date(2026, 5, 5))
    rule = SimpleNamespace(
        id=10,
        name="FR phased",
        is_enabled=True,
        schedule_type="hourly",
        hour=None,
        minute=31,
        second=59,
        weekdays=None,
        specific_date=None,
        window_duration_seconds=61,
        priority=100,
        execution_profile_mode="phased",
    )
    phase = SimpleNamespace(
        id=2,
        zone_rule_id=10,
        name="burst",
        sort_order=0,
        start_offset_seconds=0,
        duration_seconds=61,
        rps_mode="percent",
        rps_value=50.0,
    )
    strategy = SimpleNamespace(
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
        minimum_guaranteed_rps=1.0,
        rules=[rule],
        phases_by_rule_id={10: [phase]},
    )

    profile = resolve_strategy_runtime_profile(
        domain,
        strategy=strategy,
        now=datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc),
        compatible_capacity_rps=20.0,
    )

    assert profile is not None
    assert profile.window.rule_id == 10
    assert profile.phase.name == "burst"
    assert profile.target_rps == 10.0


def test_manual_override_without_rules_is_not_ready():
    domain = SimpleNamespace(
        strategy_mode="manual_override",
        registrar_account_id=1,
        contact_profile_id=1,
        drop_date=date(2026, 5, 5),
        attack_enabled=True,
    )
    domain_override = SimpleNamespace(
        id=20,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
        default_min_guaranteed_rps=1.0,
    )

    effective_strategy = resolve_effective_strategy(
        domain,
        zone_strategy=None,
        domain_override=domain_override,
        rules=[],
        phases=[],
    )
    readiness = evaluate_domain_readiness(domain, effective_strategy=effective_strategy)

    assert readiness.status == "draft"
    assert any("rule" in reason for reason in readiness.reasons)
