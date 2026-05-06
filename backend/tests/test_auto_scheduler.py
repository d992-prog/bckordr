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
        "status": "ready",
        "priority": 100,
        "success_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_select_domains_for_autoplanning_filters_and_orders_due_domains():
    now = datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc)
    domains = [
        make_domain(id=1, fqdn="ready.fr", status="ready", priority=200),
        make_domain(id=2, fqdn="paused.fr", status="paused", priority=500),
        make_domain(id=3, fqdn="future.fr", drop_date=date(2026, 5, 6), priority=900),
        make_domain(id=4, fqdn="success.fr", success_at=now, priority=800),
        make_domain(id=5, fqdn="active-run.fr", status="scheduled", priority=700),
        make_domain(id=6, fqdn="restart.fr", status="attacking", priority=150),
        make_domain(id=7, fqdn="disabled.fr", attack_enabled=False, priority=999),
    ]

    selected = select_domains_for_autoplanning(domains, now=now, active_run_domain_ids={5})

    assert [domain.id for domain in selected] == [1, 6]


def test_select_domains_for_autoplanning_keeps_tiebreakers_stable():
    now = datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc)
    domains = [
        make_domain(id=1, fqdn="zeta.fr", priority=100),
        make_domain(id=2, fqdn="alpha.fr", priority=100),
        make_domain(id=3, fqdn="beta.fr", priority=300),
    ]

    selected = select_domains_for_autoplanning(domains, now=now, active_run_domain_ids=set())

    assert [domain.id for domain in selected] == [3, 2, 1]
