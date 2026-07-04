from types import SimpleNamespace
from threading import Event, Thread

import candidate_harvester
from candidate_harvester import (
    HarvesterResult,
    ProgressStats,
    _write_redemption_debug,
    classify_lifecycle,
    build_diagnosis,
    collect_reservoir_candidates,
    extract_rdap_updated_at,
    iter_domains,
    low_value_score,
    normalize_domain,
    result_is_accepted,
    resolve_rdap_domain_url,
    should_consider_domain,
)
from quick import build_args as build_quick_args
from redemption_scan import build_args as build_redemption_args
from redemption_scan_fast import build_args as build_fast_redemption_args
from redemption_scan_turbo import build_args as build_turbo_redemption_args


def test_normalize_domain_extracts_fqdn_from_common_lines():
    assert normalize_domain("Example.COM") == "example.com"
    assert normalize_domain("https://www.example.net/path") == "example.net"
    assert normalize_domain("example.org,2026-07-02") == "example.org"
    assert normalize_domain("not a domain") is None


def test_low_value_score_prefers_long_numeric_hyphenated_domains():
    assert low_value_score("x7q9-z11820393.com") >= 70
    assert low_value_score("cars.com") < 40
    assert low_value_score("brandable.org") < 60


def test_should_consider_domain_filters_by_tld_and_score():
    assert should_consider_domain("x7q9-z11820393.com", tld="com", min_score=60)
    assert not should_consider_domain("x7q9-z11820393.net", tld="com", min_score=60)
    assert not should_consider_domain("cars.com", tld="com", min_score=60)


def test_resolve_rdap_domain_url_from_bootstrap():
    bootstrap = {
        "services": [
            [["org"], ["https://rdap.publicinterestregistry.example/rdap/"]],
            [["com", "net"], ["https://rdap.verisign.example/com/v1/"]],
        ]
    }

    assert resolve_rdap_domain_url("sample.com", bootstrap) == "https://rdap.verisign.example/com/v1/domain/sample.com"


def test_classify_lifecycle_prefers_redemption_before_pending_delete():
    assert classify_lifecycle(["clientTransferProhibited", "redemptionPeriod"], 200) == "redemption"
    assert classify_lifecycle(["pendingDelete"], 200) == "pending_delete"
    assert classify_lifecycle([], 404) == "not_found"


def test_extract_rdap_updated_at_uses_last_changed_event():
    payload = {
        "events": [
            {"eventAction": "registration", "eventDate": "2025-05-22T19:11:40Z"},
            {"eventAction": "last changed", "eventDate": "2026-07-03T07:55:56Z"},
        ]
    }

    assert extract_rdap_updated_at(payload).isoformat() == "2026-07-03T07:55:56+00:00"


def test_result_is_accepted_requires_pending_delete_window_for_redemption():
    args = SimpleNamespace(pending_delete_min_days=1.0, pending_delete_max_days=2.0)
    matching = HarvesterResult(
        domain="soon-delete.net",
        tld="net",
        lifecycle="redemption",
        status_codes="redemptionPeriod",
        http_status=200,
        checked_at="2026-07-04T12:00:00+00:00",
        redemption_anchor_at="2026-06-06T00:00:00+00:00",
        predicted_pending_delete_at="2026-07-06T00:00:00+00:00",
        days_to_pending_delete=1.5,
        score=60,
        reason="long",
    )
    too_late = HarvesterResult(
        domain="late-delete.net",
        tld="net",
        lifecycle="redemption",
        status_codes="redemptionPeriod",
        http_status=200,
        checked_at="2026-07-04T12:00:00+00:00",
        redemption_anchor_at="2026-07-03T00:00:00+00:00",
        predicted_pending_delete_at="2026-08-02T00:00:00+00:00",
        days_to_pending_delete=28.5,
        score=60,
        reason="long",
    )

    assert result_is_accepted(matching, args, accepted_lifecycles={"redemption"})
    assert not result_is_accepted(too_late, args, accepted_lifecycles={"redemption"})


def test_redemption_debug_writer_records_redemption_even_outside_window(tmp_path):
    output = tmp_path / "debug.csv"
    result = HarvesterResult(
        domain="late-delete.net",
        tld="net",
        lifecycle="redemption",
        status_codes="redemptionPeriod",
        http_status=200,
        checked_at="2026-07-04T12:00:00+00:00",
        redemption_anchor_at="2026-07-03T00:00:00+00:00",
        predicted_pending_delete_at="2026-08-02T00:00:00+00:00",
        days_to_pending_delete=28.5,
        score=60,
        reason="long",
    )
    stats = ProgressStats()
    args = SimpleNamespace(redemption_debug_limit=10)

    import csv

    with output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(result.__dict__.keys()))
        writer.writeheader()
        _write_redemption_debug(result, writer, stats, args)

    assert stats.written_redemption_debug == 1
    assert "late-delete.net" in output.read_text(encoding="utf-8")


def test_rdap_pool_submits_next_domain_after_first_completion(monkeypatch, tmp_path):
    slow_tail_can_finish = Event()
    ninth_task_started = Event()
    submitted_domains: list[str] = []

    def fake_check_rdap(domain, bootstrap, *, timeout):
        submitted_domains.append(domain)
        if domain == "slow-8.com":
            ninth_task_started.set()
        if domain == "slow-7.com":
            slow_tail_can_finish.wait(timeout=5)
        return HarvesterResult(
            domain=domain,
            tld="com",
            lifecycle="registered",
            status_codes="active",
            http_status=200,
            checked_at="2026-07-04T12:00:00+00:00",
            redemption_anchor_at=None,
            predicted_pending_delete_at=None,
            days_to_pending_delete=None,
            score=60,
            reason="long",
        )

    args = SimpleNamespace(
        bootstrap_url="unused",
        bootstrap_timeout=1,
        input=["unused.txt"],
        output=tmp_path / "out.csv",
        output_txt=None,
        redemption_debug_output=None,
        concurrency=2,
        sample_mode="reservoir",
        max_rdap_checks=9,
        limit_output=20,
        accept_lifecycle=["redemption"],
        rdap_timeout=1,
        progress_interval=0.05,
        pending_delete_min_days=None,
        pending_delete_max_days=None,
    )

    monkeypatch.setattr(candidate_harvester, "fetch_bootstrap", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        candidate_harvester,
        "collect_reservoir_candidates",
        lambda inputs, args, stats: [*[f"fast-{index}.com" for index in range(7)], "slow-7.com", "slow-8.com"],
    )
    monkeypatch.setattr(candidate_harvester, "check_rdap", fake_check_rdap)

    run_thread = Thread(target=candidate_harvester.run, args=(args,))
    run_thread.start()

    assert ninth_task_started.wait(timeout=1)

    slow_tail_can_finish.set()
    run_thread.join(timeout=5)
    assert not run_thread.is_alive()
    assert "slow-8.com" in submitted_domains


def test_quick_args_build_safe_default_command():
    args = build_quick_args([".com", "expired-com.txt"])

    assert args == [
        "--input",
        "expired-com.txt",
        "--tld",
        "com",
        "--output",
        "candidates-com.csv",
        "--output-txt",
        "candidates-com.txt",
        "--limit-output",
        "20",
        "--max-rdap-checks",
        "1000",
        "--concurrency",
        "10",
    ]


def test_iter_domains_updates_progress_stats(tmp_path):
    input_file = tmp_path / "domains.txt"
    input_file.write_text("example.com\nnot a domain\nsample.net\n", encoding="utf-8")
    stats = ProgressStats()

    assert list(iter_domains([input_file], stats)) == ["example.com", "sample.net"]
    assert stats.scanned_lines == 3
    assert stats.parsed_domains == 2


def test_reservoir_candidates_sample_across_input(tmp_path):
    input_file = tmp_path / "domains.txt"
    input_file.write_text("\n".join(f"x7q9-z11820{i}.com" for i in range(100)), encoding="utf-8")
    stats = ProgressStats()
    args = SimpleNamespace(
        tld="com",
        min_score=40,
        reservoir_size=10,
        random_seed=7,
        progress_interval=9999,
        limit_output=10,
    )

    sample = collect_reservoir_candidates([input_file], args, stats)

    assert len(sample) == 10
    assert stats.scanned_lines == 100
    assert stats.filtered_candidates == 100
    assert sample != [f"x7q9-z11820{i}.com" for i in range(10)]


def test_redemption_scan_args_use_reservoir_mode():
    args = build_redemption_args(["com", "com.2026-07-02.txt"])

    assert "--sample-mode" in args
    assert "reservoir" in args
    assert "--max-rdap-checks" in args
    assert "50000" in args


def test_fast_redemption_scan_args_use_aggressive_limits():
    args = build_fast_redemption_args(["com", "com.2026-07-02.txt"])

    assert "--sample-mode" in args
    assert "reservoir" in args
    assert "--concurrency" in args
    assert "100" in args
    assert "--limit-output" in args
    assert "20" in args
    assert "--redemption-debug-output" in args
    assert "redemption-candidates-com-fast-redemption-debug.csv" in args
    assert "--pending-delete-min-days" in args
    assert "1" in args
    assert "--pending-delete-max-days" in args
    assert "2" in args
    assert "--max-rdap-checks" in args
    assert "200000" in args
    assert "--reservoir-size" in args
    assert "200000" in args


def test_turbo_redemption_scan_args_use_high_parallel_limits():
    args = build_turbo_redemption_args(["com", "com.2026-07-02.txt"])

    assert "--sample-mode" in args
    assert "reservoir" in args
    assert "--concurrency" in args
    assert "300" in args
    assert "--rdap-timeout" in args
    assert "4" in args
    assert "--limit-output" in args
    assert "20" in args
    assert "--redemption-debug-output" in args
    assert "redemption-candidates-com-turbo-redemption-debug.csv" in args
    assert "--pending-delete-min-days" in args
    assert "1" in args
    assert "--pending-delete-max-days" in args
    assert "2" in args
    assert "--max-rdap-checks" in args
    assert "300000" in args
    assert "--reservoir-size" in args
    assert "300000" in args


def test_build_diagnosis_explains_zero_candidates():
    stats = ProgressStats(
        scanned_lines=1000,
        parsed_domains=990,
        filtered_candidates=200,
        submitted_rdap=200,
        completed_rdap=200,
        written_candidates=0,
    )
    diagnosis = build_diagnosis(
        stats,
        lifecycle_counts={"registered": 198, "error": 2},
        accepted_lifecycles={"redemption"},
    )

    assert "RDAP checks worked" in diagnosis
    assert "accepted lifecycle was not seen" in diagnosis
    assert "registered=198" in diagnosis
