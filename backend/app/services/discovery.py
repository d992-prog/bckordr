from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.db.models import DiscoveryDomain

PENDING_DELETE_DURATION = timedelta(days=5)
DROP_DAY_SCAN_INTERVAL = timedelta(seconds=10)
REDEMPTION_SCAN_INTERVAL = timedelta(hours=1)
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)
PENDING_DELETE_SCAN_INTERVAL = timedelta(minutes=10)


@dataclass(frozen=True)
class DiscoveryObservationInput:
    source: str
    observed_at: datetime
    http_status: int | None = None
    latency_ms: int | None = None
    lifecycle_stage: str | None = None
    availability_status: str | None = None
    status_codes: list[str] = field(default_factory=list)
    raw_response: str | None = None
    error: str | None = None


def normalize_discovery_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    if not domain or "." not in domain:
        raise ValueError("domain must be a fully qualified name")
    return domain


def infer_zone(fqdn: str) -> str:
    return fqdn.rsplit(".", 1)[-1].lower()


def normalize_lifecycle_stage(status_codes: list[str], *, http_status: int | None = None) -> str:
    normalized = {item.lower() for item in status_codes}
    if http_status == 404:
        return "not_found"
    if "pendingdelete" in normalized:
        return "pending_delete"
    if "redemptionperiod" in normalized:
        return "redemption"
    if status_codes:
        return "registered"
    return "unknown"


def calculate_next_check_at(domain: DiscoveryDomain, now: datetime) -> datetime | None:
    if domain.is_enabled is False or domain.status in {"available", "ignored"}:
        return None

    if (
        domain.status == "pending_delete"
        and domain.predicted_drop_start_at is not None
        and domain.predicted_drop_end_at is not None
        and domain.predicted_drop_start_at.date() <= now.date() <= domain.predicted_drop_end_at.date()
    ):
        return now + DROP_DAY_SCAN_INTERVAL

    if domain.last_lifecycle_stage == "redemption":
        return now + REDEMPTION_SCAN_INTERVAL
    if domain.status == "pending_delete":
        return now + PENDING_DELETE_SCAN_INTERVAL
    return now + DEFAULT_SCAN_INTERVAL


def apply_discovery_observation(
    domain: DiscoveryDomain,
    observation: DiscoveryObservationInput,
) -> None:
    observed_at = _ensure_aware(observation.observed_at)
    lifecycle_stage = observation.lifecycle_stage or normalize_lifecycle_stage(
        observation.status_codes,
        http_status=observation.http_status,
    )
    status_codes_json = json.dumps(observation.status_codes, ensure_ascii=True) if observation.status_codes else None

    previous_checked_at = domain.last_checked_at
    domain.last_checked_at = observed_at
    domain.last_lifecycle_stage = lifecycle_stage
    domain.last_status_codes = status_codes_json
    domain.last_availability = observation.availability_status
    domain.last_error = observation.error

    if lifecycle_stage == "redemption":
        domain.status = "redemption"
        domain.first_seen_redemption_at = domain.first_seen_redemption_at or observed_at
        domain.last_seen_redemption_at = observed_at
    elif lifecycle_stage == "pending_delete":
        domain.status = "pending_delete"
        if domain.first_seen_pending_delete_at is None:
            domain.pending_delete_previous_seen_at = previous_checked_at or observed_at
            domain.first_seen_pending_delete_at = observed_at
            domain.predicted_drop_start_at = domain.pending_delete_previous_seen_at + PENDING_DELETE_DURATION
            domain.predicted_drop_end_at = observed_at + PENDING_DELETE_DURATION
        domain.last_seen_pending_delete_at = observed_at
    elif lifecycle_stage == "not_found" or observation.availability_status == "available":
        domain.status = "available"
        domain.available_first_seen_at = domain.available_first_seen_at or observed_at
    elif domain.status in {"tracking", "error"}:
        domain.status = "tracking"

    if observation.error:
        domain.status = "error"

    domain.next_check_at = calculate_next_check_at(domain, observed_at)
    domain.updated_at = observed_at


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
