from types import SimpleNamespace

from candidate_harvester import (
    ProgressStats,
    classify_lifecycle,
    build_diagnosis,
    collect_reservoir_candidates,
    iter_domains,
    low_value_score,
    normalize_domain,
    resolve_rdap_domain_url,
    should_consider_domain,
)
from quick import build_args as build_quick_args
from redemption_scan import build_args as build_redemption_args


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
