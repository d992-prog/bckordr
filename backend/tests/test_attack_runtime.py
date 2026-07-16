from datetime import date, datetime, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app.services.attack_runtime import (
    allocate_domain_target_rps,
    allocate_worker_rps,
    build_domain_runtime_snapshots,
    domain_window_bounds,
    plan_worker_assignments,
    refresh_domain_task_targets,
)


def make_domain(**overrides):
    base = {
        "id": 1,
        "fqdn": "drop.fr",
        "drop_date": date(2026, 3, 24),
        "timezone_name": "Europe/Paris",
        "window_start_minute": 31,
        "window_start_second": 59,
        "window_duration_seconds": 61,
        "priority": 100,
        "status": "queued",
        "registrar_slug": "gandi",
        "registrar_account_id": 1,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def make_worker(**overrides):
    base = {
        "id": 1,
        "name": "worker-1",
        "registrar_slug": "gandi",
        "assigned_registrar_account_id": None,
        "target_rps": 16.0,
        "max_rps": 16.0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_domain_window_bounds_starts_on_first_window_of_drop_day():
    anchor = datetime(2026, 3, 23, 18, 0, tzinfo=timezone.utc)

    start_at, end_at = domain_window_bounds(make_domain(), anchor=anchor)

    paris = ZoneInfo("Europe/Paris")
    assert start_at.astimezone(paris) == datetime(2026, 3, 24, 0, 31, 59, tzinfo=paris)
    assert end_at.astimezone(paris) == datetime(2026, 3, 24, 0, 33, 0, tzinfo=paris)


def test_domain_window_bounds_rolls_to_next_hour_after_window_expires():
    paris = ZoneInfo("Europe/Paris")
    anchor = datetime(2026, 3, 24, 14, 33, 5, tzinfo=paris).astimezone(timezone.utc)

    start_at, end_at = domain_window_bounds(make_domain(), anchor=anchor)

    assert start_at.astimezone(paris) == datetime(2026, 3, 24, 15, 31, 59, tzinfo=paris)
    assert end_at.astimezone(paris) == datetime(2026, 3, 24, 15, 33, 0, tzinfo=paris)


def test_domain_window_bounds_stops_after_last_drop_day_window():
    paris = ZoneInfo("Europe/Paris")
    anchor = datetime(2026, 3, 24, 23, 33, 5, tzinfo=paris).astimezone(timezone.utc)

    assert domain_window_bounds(make_domain(), anchor=anchor) is None


def test_plan_worker_assignments_keeps_one_worker_per_domain_then_boosts_priority():
    now = datetime(2026, 3, 24, 14, 32, tzinfo=timezone.utc)
    domains = [
        make_domain(id=1, fqdn="alpha.fr", registrar_account_id=1, priority=200),
        make_domain(id=2, fqdn="beta.fr", registrar_account_id=2, priority=100),
    ]
    workers = [
        make_worker(id=11, name="acc-1", assigned_registrar_account_id=1),
        make_worker(id=12, name="acc-2", assigned_registrar_account_id=2),
        make_worker(id=13, name="flex", assigned_registrar_account_id=None),
    ]

    assignments = plan_worker_assignments(domains=domains, workers=workers, now=now)

    assert [worker.id for worker in assignments[1]] == [11, 13]
    assert [worker.id for worker in assignments[2]] == [12]


def test_allocate_worker_rps_caps_last_worker_at_remaining_target():
    workers = [
        make_worker(id=1, target_rps=16.0, max_rps=16.0),
        make_worker(id=2, target_rps=16.0, max_rps=16.0),
    ]

    allocation = allocate_worker_rps(workers, target_rps=20.0)

    assert allocation == {1: 16.0, 2: 4.0}


def test_refresh_domain_task_targets_reallocates_live_planned_rps_from_strategy_phase():
    domain = make_domain(drop_date=date(2026, 5, 5), timezone_name="Europe/Paris")
    tasks = [
        SimpleNamespace(worker_id=1, planned_rps=16.0, actual_rps=0.0),
        SimpleNamespace(worker_id=2, planned_rps=16.0, actual_rps=0.0),
    ]
    workers = {
        1: make_worker(id=1, target_rps=16.0, max_rps=16.0),
        2: make_worker(id=2, target_rps=16.0, max_rps=16.0),
    }
    strategy = SimpleNamespace(
        rules=[
            SimpleNamespace(
                id=10,
                name="FR phased",
                is_enabled=True,
                schedule_type="hourly",
                hour=None,
                minute=32,
                second=0,
                weekdays=None,
                specific_date=None,
                window_duration_seconds=61,
                priority=100,
                execution_profile_mode="phased",
            )
        ],
        phases_by_rule_id={
            10: [
                SimpleNamespace(
                    id=1,
                    zone_rule_id=10,
                    name="burst",
                    sort_order=0,
                    start_offset_seconds=0,
                    duration_seconds=61,
                    rps_mode="fixed",
                    rps_value=20.0,
                )
            ]
        },
        minimum_guaranteed_rps=1.0,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
    )

    total = refresh_domain_task_targets(
        domain,
        tasks=tasks,
        workers_by_id=workers,
        effective_strategy=strategy,
        now=datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc),
    )

    assert total == 20.0
    assert [task.planned_rps for task in tasks] == [16.0, 4.0]


def test_allocate_domain_target_rps_gives_everyone_minimum_then_splits_remainder_by_priority():
    domains = [
        make_domain(id=1, fqdn="alpha.fr", priority=300),
        make_domain(id=2, fqdn="beta.fr", priority=100),
    ]

    allocation = allocate_domain_target_rps(
        domains,
        desired_rps_by_domain={1: 10.0, 2: 10.0},
        minimum_rps_by_domain={1: 2.0, 2: 2.0},
        total_available_rps=10.0,
    )

    assert allocation[1] == 6.5
    assert allocation[2] == 3.5


def test_allocate_domain_target_rps_respects_desired_caps_and_redistributes_leftover():
    domains = [
        make_domain(id=1, fqdn="alpha.fr", priority=300),
        make_domain(id=2, fqdn="beta.fr", priority=100),
    ]

    allocation = allocate_domain_target_rps(
        domains,
        desired_rps_by_domain={1: 3.0, 2: 20.0},
        minimum_rps_by_domain={1: 1.0, 2: 1.0},
        total_available_rps=20.0,
    )

    assert allocation[1] == 3.0
    assert allocation[2] == 17.0


def test_allocate_domain_target_rps_handles_capacity_below_total_minimum_with_priority_bias():
    domains = [
        make_domain(id=1, fqdn="alpha.fr", priority=300),
        make_domain(id=2, fqdn="beta.fr", priority=100),
    ]

    allocation = allocate_domain_target_rps(
        domains,
        desired_rps_by_domain={1: 10.0, 2: 10.0},
        minimum_rps_by_domain={1: 4.0, 2: 4.0},
        total_available_rps=4.0,
    )

    assert allocation[1] == 3.0
    assert allocation[2] == 1.0


def test_build_domain_runtime_snapshots_reports_min_desired_allocated_and_phase():
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    domain = make_domain(
        id=1,
        fqdn="alpha.fr",
        drop_date=date(2026, 5, 5),
        timezone_name="Europe/Paris",
        override_min_guaranteed_rps=2.0,
    )
    workers = [
        make_worker(id=1, target_rps=16.0, max_rps=16.0),
        make_worker(id=2, target_rps=16.0, max_rps=16.0),
    ]
    strategy = SimpleNamespace(
        rules=[
            SimpleNamespace(
                id=10,
                name="FR phased",
                is_enabled=True,
                schedule_type="hourly",
                hour=None,
                minute=32,
                second=0,
                weekdays=None,
                specific_date=None,
                window_duration_seconds=61,
                priority=100,
                execution_profile_mode="phased",
            )
        ],
        phases_by_rule_id={
            10: [
                SimpleNamespace(
                    id=1,
                    zone_rule_id=10,
                    name="burst",
                    sort_order=0,
                    start_offset_seconds=0,
                    duration_seconds=61,
                    rps_mode="fixed",
                    rps_value=20.0,
                )
            ]
        },
        minimum_guaranteed_rps=1.0,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
    )
    run = SimpleNamespace(id=77, status="running", planned_rps=20.0)
    tasks = [
        SimpleNamespace(domain_id=1, worker_id=1, planned_rps=16.0, status="running"),
        SimpleNamespace(domain_id=1, worker_id=2, planned_rps=4.0, status="running"),
    ]

    snapshots = build_domain_runtime_snapshots(
        [domain],
        workers=workers,
        strategy_map={1: strategy},
        now=now,
        active_run_by_domain_id={1: run},
        active_tasks_by_domain_id={1: tasks},
    )

    snapshot = snapshots[1]
    assert snapshot.minimum_rps == 2.0
    assert snapshot.desired_rps == 20.0
    assert snapshot.allocated_rps == 20.0
    assert snapshot.assigned_worker_count == 2
    assert snapshot.phase_name == "burst"
    assert snapshot.attack_run_id == 77
    assert snapshot.attack_status == "running"
    paris = ZoneInfo("Europe/Paris")
    assert snapshot.window_start_at is not None
    assert snapshot.window_end_at is not None
    assert snapshot.window_start_at.astimezone(paris) == datetime(2026, 5, 5, 14, 32, 0, tzinfo=paris)
    assert snapshot.window_end_at.astimezone(paris) == datetime(2026, 5, 5, 14, 33, 1, tzinfo=paris)


def test_runtime_snapshot_uses_effective_strategy_window_over_domain_fallback():
    now = datetime(2026, 5, 5, 12, 30, 45, tzinfo=timezone.utc)
    domain = make_domain(
        id=9,
        fqdn="old-fields.fr",
        drop_date=date(2026, 5, 5),
        timezone_name="Europe/Paris",
        window_start_minute=31,
        window_start_second=59,
        window_duration_seconds=61,
    )
    worker = make_worker(id=1, target_rps=16.0, max_rps=16.0)
    strategy = SimpleNamespace(
        rules=[
            SimpleNamespace(
                id=19,
                name="FR expanded",
                is_enabled=True,
                schedule_type="hourly",
                hour=None,
                minute=31,
                second=30,
                weekdays=None,
                specific_date=None,
                window_duration_seconds=95,
                priority=100,
                execution_profile_mode="flat",
            )
        ],
        phases_by_rule_id={19: []},
        minimum_guaranteed_rps=1.0,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
    )

    snapshot = build_domain_runtime_snapshots(
        [domain],
        workers=[worker],
        strategy_map={domain.id: strategy},
        now=now,
        active_run_by_domain_id={},
        active_tasks_by_domain_id={},
    )[domain.id]

    paris = ZoneInfo("Europe/Paris")
    assert snapshot.window_start_at is not None
    assert snapshot.window_end_at is not None
    assert snapshot.window_start_at.astimezone(paris) == datetime(2026, 5, 5, 14, 31, 30, tzinfo=paris)
    assert snapshot.window_end_at.astimezone(paris) == datetime(2026, 5, 5, 14, 33, 5, tzinfo=paris)


def test_build_domain_runtime_snapshots_keeps_allocation_zero_without_active_tasks():
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    domain = make_domain(id=2, fqdn="beta.fr", drop_date=date(2026, 5, 5), timezone_name="Europe/Paris")
    workers = [make_worker(id=5, target_rps=12.0, max_rps=16.0)]
    strategy = SimpleNamespace(
        rules=[
            SimpleNamespace(
                id=11,
                name="FR flat",
                is_enabled=True,
                schedule_type="hourly",
                hour=None,
                minute=32,
                second=0,
                weekdays=None,
                specific_date=None,
                window_duration_seconds=61,
                priority=100,
                execution_profile_mode="flat",
            )
        ],
        phases_by_rule_id={11: []},
        minimum_guaranteed_rps=1.0,
        timezone_name="Europe/Paris",
        rule_resolution_mode="priority",
    )

    snapshots = build_domain_runtime_snapshots(
        [domain],
        workers=workers,
        strategy_map={2: strategy},
        now=now,
        active_run_by_domain_id={},
        active_tasks_by_domain_id={},
    )

    snapshot = snapshots[2]
    assert snapshot.minimum_rps == 1.0
    assert snapshot.desired_rps == 12.0
    assert snapshot.allocated_rps == 0.0
    assert snapshot.assigned_worker_count == 0
    assert snapshot.phase_name is None
    assert snapshot.attack_run_id is None


def test_build_domain_runtime_snapshots_zeroes_metrics_for_non_due_domains():
    now = datetime(2026, 5, 5, 12, 32, 10, tzinfo=timezone.utc)
    domain = make_domain(id=3, fqdn="future.fr", drop_date=date(2026, 5, 6), timezone_name="Europe/Paris")
    workers = [make_worker(id=7, target_rps=12.0, max_rps=16.0)]

    snapshots = build_domain_runtime_snapshots(
        [domain],
        workers=workers,
        strategy_map={},
        now=now,
        active_run_by_domain_id={},
        active_tasks_by_domain_id={},
    )

    snapshot = snapshots[3]
    assert snapshot.minimum_rps == 0.0
    assert snapshot.desired_rps == 0.0
    assert snapshot.allocated_rps == 0.0
    assert snapshot.phase_name is None
