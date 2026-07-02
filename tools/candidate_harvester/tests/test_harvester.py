from candidate_harvester import (
    ProgressStats,
    classify_lifecycle,
    iter_domains,
    low_value_score,
    normalize_domain,
    resolve_rdap_domain_url,
    should_consider_domain,
)
from quick import build_args


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
    args = build_args([".com", "expired-com.txt"])

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
