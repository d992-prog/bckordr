from __future__ import annotations

import json
from time import perf_counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscoveryDomain, DiscoveryObservation

IANA_RDAP_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
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
    normalized = {_normalize_status_code(item) for item in status_codes}
    if http_status == 404:
        return "not_found"
    if "pendingdelete" in normalized:
        return "pending_delete"
    if "redemptionperiod" in normalized:
        return "redemption"
    if status_codes:
        return "registered"
    return "unknown"


def resolve_rdap_domain_url(fqdn: str, bootstrap_payload: dict) -> str:
    zone = infer_zone(fqdn)
    for service in bootstrap_payload.get("services", []):
        if not isinstance(service, list) or len(service) < 2:
            continue
        tlds, urls = service[0], service[1]
        if not isinstance(tlds, list) or not isinstance(urls, list):
            continue
        if zone not in {str(item).lower() for item in tlds}:
            continue
        base_url = next((str(item) for item in urls if item), "")
        if not base_url:
            break
        return f"{base_url.rstrip('/')}/domain/{fqdn}"
    raise ValueError(f"RDAP bootstrap has no endpoint for .{zone}")


async def check_discovery_domain_rdap(
    domain: DiscoveryDomain,
    *,
    client: httpx.AsyncClient | None = None,
    bootstrap_payload: dict | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
    timeout_seconds: float = 5.0,
) -> DiscoveryObservationInput:
    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout_seconds)
    started_at = perf_counter()
    observed_at = datetime.now(timezone.utc)
    try:
        bootstrap = bootstrap_payload or await fetch_rdap_bootstrap(http_client, bootstrap_url)
        rdap_url = resolve_rdap_domain_url(domain.fqdn, bootstrap)
        rdap_response = await http_client.get(rdap_url)
        latency_ms = int((perf_counter() - started_at) * 1000)
        status_codes: list[str] = []
        raw_response = rdap_response.text
        if rdap_response.headers.get("content-type", "").startswith("application/json"):
            payload = rdap_response.json()
            status_codes = [str(item) for item in payload.get("status", []) if item]
        lifecycle_stage = normalize_lifecycle_stage(status_codes, http_status=rdap_response.status_code)
        availability_status = "available" if lifecycle_stage == "not_found" else "taken"
        return DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=rdap_response.status_code,
            latency_ms=latency_ms,
            lifecycle_stage=lifecycle_stage,
            availability_status=availability_status,
            status_codes=status_codes,
            raw_response=raw_response[:10000],
        )
    except Exception as exc:
        return DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            latency_ms=int((perf_counter() - started_at) * 1000),
            lifecycle_stage="unknown",
            availability_status="unknown",
            error=str(exc),
        )
    finally:
        if close_client:
            await http_client.aclose()


async def fetch_rdap_bootstrap(client: httpx.AsyncClient, bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL) -> dict:
    response = await client.get(bootstrap_url)
    response.raise_for_status()
    return response.json()


async def process_due_discovery_domains(
    session: AsyncSession,
    *,
    now: datetime,
    batch_size: int = 10,
    client: httpx.AsyncClient | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
    timeout_seconds: float = 5.0,
    notify=None,
) -> int:
    result = await session.execute(
        select(DiscoveryDomain)
        .where(
            DiscoveryDomain.is_enabled.is_(True),
            DiscoveryDomain.status.notin_(["available", "ignored"]),
            (DiscoveryDomain.next_check_at.is_(None)) | (DiscoveryDomain.next_check_at <= now),
        )
        .order_by(DiscoveryDomain.next_check_at.asc(), DiscoveryDomain.id.asc())
        .limit(max(int(batch_size), 1))
    )
    domains = list(result.scalars().all())
    if not domains:
        return 0

    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout_seconds)
    try:
        try:
            bootstrap_payload = await fetch_rdap_bootstrap(http_client, bootstrap_url)
        except Exception as exc:
            for domain in domains:
                observation = DiscoveryObservationInput(
                    source="rdap",
                    observed_at=now,
                    lifecycle_stage="unknown",
                    availability_status="unknown",
                    error=f"RDAP bootstrap failed: {exc}",
                )
                apply_discovery_observation(domain, observation)
                session.add(_build_observation_model(domain, observation))
            return len(domains)

        for domain in domains:
            previous_status = domain.status
            previous_pending_at = domain.first_seen_pending_delete_at
            previous_available_at = domain.available_first_seen_at
            observation = await check_discovery_domain_rdap(
                domain,
                client=http_client,
                bootstrap_payload=bootstrap_payload,
                bootstrap_url=bootstrap_url,
                timeout_seconds=timeout_seconds,
            )
            observation = replace(observation, observed_at=now)
            apply_discovery_observation(domain, observation)
            session.add(_build_observation_model(domain, observation))
            if notify:
                message = _build_transition_notification(
                    domain,
                    previous_status=previous_status,
                    previous_pending_at=previous_pending_at,
                    previous_available_at=previous_available_at,
                )
                if message:
                    maybe_result = notify(message)
                    if hasattr(maybe_result, "__await__"):
                        await maybe_result
        return len(domains)
    finally:
        if close_client:
            await http_client.aclose()


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


def _normalize_status_code(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _build_observation_model(
    domain: DiscoveryDomain,
    observation: DiscoveryObservationInput,
) -> DiscoveryObservation:
    return DiscoveryObservation(
        discovery_domain_id=domain.id,
        source=observation.source,
        observed_at=observation.observed_at,
        http_status=observation.http_status,
        latency_ms=observation.latency_ms,
        lifecycle_stage=observation.lifecycle_stage,
        availability_status=observation.availability_status,
        status_codes=json.dumps(observation.status_codes, ensure_ascii=True) if observation.status_codes else None,
        raw_response=observation.raw_response,
        error=observation.error,
    )


def _build_transition_notification(
    domain: DiscoveryDomain,
    *,
    previous_status: str | None,
    previous_pending_at: datetime | None,
    previous_available_at: datetime | None,
) -> str | None:
    del previous_status
    if domain.first_seen_pending_delete_at and previous_pending_at is None:
        return (
            "Discovery pendingDelete\n\n"
            f"Domain: {domain.fqdn}\n"
            f"First seen: {domain.first_seen_pending_delete_at.isoformat()}\n"
            f"Predicted drop: {domain.predicted_drop_start_at.isoformat()} - {domain.predicted_drop_end_at.isoformat()}"
        )
    if domain.available_first_seen_at and previous_available_at is None:
        return (
            "Discovery available\n\n"
            f"Domain: {domain.fqdn}\n"
            f"First seen: {domain.available_first_seen_at.isoformat()}"
        )
    return None
