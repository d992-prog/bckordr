from datetime import date, datetime, timezone
from types import SimpleNamespace

from app.services.attack_runtime import select_domains_for_autoplanning


def make_domain(**overrides):
    base = {
        "id": 1,
        "fqdn": "alpha.fr",
        "drop_date": date(2026, 5, 5),
        "timezone_name": "Europe/Paris",
        "attack_enabled": True,
        "auto_start_enabled": False,
        "auto_start_lead_seconds": 90,
        "status": "ready",
        "priority": 100,
        "success_at": None,
        "window_start_minute": 31,
        "window_start_second": 59,
        "window_duration_seconds": 61,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_select_domains_for_autoplanning_filters_and_orders_due_domains():
    now = datetime(2026, 5, 5, 8, 30, 45, tzinfo=timezone.utc)
    domains = [
        make_domain(id=1, fqdn="ready.fr", status="ready", priority=200, auto_start_enabled=True),
        make_domain(id=2, fqdn="manual.fr", status="ready", priority=999),
        make_domain(id=3, fqdn="paused.fr", status="paused", priority=500, auto_start_enabled=True),
        make_domain(id=4, fqdn="future.fr", drop_date=date(2026, 5, 6), priority=900, auto_start_enabled=True),
        make_domain(id=5, fqdn="success.fr", success_at=now, priority=800, auto_start_enabled=True),
        make_domain(id=6, fqdn="active-run.fr", status="scheduled", priority=700, auto_start_enabled=True),
        make_domain(id=7, fqdn="restart.fr", status="attacking", priority=150, auto_start_enabled=True),
        make_domain(id=8, fqdn="disabled.fr", attack_enabled=False, priority=999, auto_start_enabled=True),
    ]

    selected = select_domains_for_autoplanning(domains, now=now, active_run_domain_ids={6})

    assert [domain.id for domain in selected] == [1, 7]


def test_select_domains_for_autoplanning_keeps_tiebreakers_stable():
    now = datetime(2026, 5, 5, 8, 30, 45, tzinfo=timezone.utc)
    domains = [
        make_domain(id=1, fqdn="zeta.fr", priority=100, auto_start_enabled=True),
        make_domain(id=2, fqdn="alpha.fr", priority=100, auto_start_enabled=True),
        make_domain(id=3, fqdn="beta.fr", priority=300, auto_start_enabled=True),
    ]

    selected = select_domains_for_autoplanning(domains, now=now, active_run_domain_ids=set())

    assert [domain.id for domain in selected] == [3, 2, 1]


def test_select_domains_for_autoplanning_waits_until_lead_window():
    too_early = datetime(2026, 5, 5, 8, 29, 0, tzinfo=timezone.utc)
    inside_lead = datetime(2026, 5, 5, 8, 30, 30, tzinfo=timezone.utc)
    domain = make_domain(auto_start_enabled=True, auto_start_lead_seconds=90)

    assert select_domains_for_autoplanning([domain], now=too_early, active_run_domain_ids=set()) == []
    assert select_domains_for_autoplanning([domain], now=inside_lead, active_run_domain_ids=set()) == [domain]


def test_select_domains_for_autoplanning_uses_effective_strategy_bounds_when_provided():
    now = datetime(2026, 5, 5, 12, 30, 10, tzinfo=timezone.utc)
    domain = make_domain(id=1, auto_start_enabled=True, auto_start_lead_seconds=90)
    effective_bounds = (
        datetime(2026, 5, 5, 12, 31, 30, tzinfo=timezone.utc),
        datetime(2026, 5, 5, 12, 33, 5, tzinfo=timezone.utc),
    )

    assert select_domains_for_autoplanning([domain], now=now, active_run_domain_ids=set()) == []
    assert (
        select_domains_for_autoplanning(
            [domain],
            now=now,
            active_run_domain_ids=set(),
            bounds_by_domain_id={1: effective_bounds},
        )
        == [domain]
    )
