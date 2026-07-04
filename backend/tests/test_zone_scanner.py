from datetime import date, datetime, timezone

from app.services.zone_scanner import (
    CandidateResult,
    ZoneScanConfig,
    build_allzonefiles_download_url,
    result_is_zone_scan_candidate,
)


def test_build_allzonefiles_zone_download_urls():
    assert (
        build_allzonefiles_download_url(
            base_url="https://allzonefiles.io/api/v1/",
            source_type="zone_latest",
            zone=".com",
            source_date=None,
        )
        == "https://allzonefiles.io/api/v1/zones/com/dl"
    )
    assert (
        build_allzonefiles_download_url(
            base_url="https://allzonefiles.io/api/v1",
            source_type="zone_historic",
            zone="net",
            source_date=date(2026, 7, 2),
        )
        == "https://allzonefiles.io/api/v1/zones/net/dl/2026-07-02"
    )


def test_zone_scan_candidate_requires_pending_delete_window():
    config = ZoneScanConfig(
        zone="com",
        min_score=35,
        limit_output=20,
        max_rdap_checks=1000,
        concurrency=10,
        rdap_timeout_seconds=5.0,
        pending_delete_min_days=1.0,
        pending_delete_max_days=2.0,
        reservoir_size=1000,
        random_seed=42,
        keep_file=True,
    )
    matching = CandidateResult(
        fqdn="x9-longcandidate.com",
        zone="com",
        lifecycle_stage="redemption",
        status_codes='["redemption period"]',
        http_status=200,
        checked_at=datetime(2026, 7, 4, tzinfo=timezone.utc),
        redemption_anchor_at=datetime(2026, 6, 5, tzinfo=timezone.utc),
        predicted_pending_delete_at=datetime(2026, 7, 5, 12, tzinfo=timezone.utc),
        days_to_pending_delete=1.5,
        score=60,
        reason="long+numeric",
        error=None,
    )
    too_late = CandidateResult(
        **{**matching.__dict__, "fqdn": "latecandidate.com", "days_to_pending_delete": 28.0}
    )

    assert result_is_zone_scan_candidate(matching, config)
    assert not result_is_zone_scan_candidate(too_late, config)
