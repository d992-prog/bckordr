from __future__ import annotations

import asyncio
import json
import re
import socket
import zlib
from time import perf_counter
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable
from urllib.parse import quote

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import DiscoveryDomain, DiscoveryObservation

IANA_RDAP_BOOTSTRAP_URL = "https://data.iana.org/rdap/dns.json"
WHOIS_FALLBACK_SOURCE = "whois_fallback"
WHOIS_RATE_LIMIT_ERROR = "WHOIS rate limit or access denied"
PENDING_DELETE_DURATION = timedelta(days=5)
REDEMPTION_DURATION = timedelta(days=30)
DROP_DAY_SCAN_INTERVAL = timedelta(seconds=10)
REDEMPTION_SCAN_INTERVAL = timedelta(minutes=15)
DEFAULT_SCAN_INTERVAL = timedelta(hours=6)
PENDING_DELETE_SCAN_INTERVAL = timedelta(minutes=5)
ERROR_RETRY_INTERVAL = timedelta(minutes=3)
ERROR_RETRY_JITTER = timedelta(minutes=1)
ACTIVE_NEXT_CHECK_JITTER = timedelta(seconds=10)
BATCH_NEXT_CHECK_SPREAD = timedelta(seconds=10)
INITIAL_DISCOVERY_IMPORT_SPREAD = timedelta(minutes=5)
WHOIS_RESPONSE_LIMIT_BYTES = 10000
WHOSE_DOMAINS_AVAILABILITY_LOOKUP = "whose-domains://availability"
WHOSE_DOMAINS_BULK_SEARCH_URL = "https://whose.domains/api/tools/bulk-search"
WHOIS_STATUS_PATTERN = re.compile(r"(?im)^\s*(?:domain\s+)?status\s*:\s*(.+?)\s*$")
WHOIS_NOT_FOUND_PATTERNS = (
    "no match",
    "not found",
    "no data found",
    "does not exist in database",
    "does not exist in the database",
    "domain not found",
    "domain not allocated",
    "no object found",
    "object does not exist",
    "no entries found",
    "does not appear registered yet",
    "is available",
    "available for registration",
    "status: free",
    "je slobodan",
)
WHOIS_RATE_LIMIT_PATTERNS = (
    "quota exceeded",
    "query limit exceeded",
    "rate limit",
    "too many requests",
    "access denied",
    "blocked",
    "blacklisted",
)
RDAP_404_TAKEN_TITLES = {
    "auction pending",
}
PIR_DROPZONE_PHRASE = "pir dropzone service"
AVAILABLE_STATUS_CODES = {
    "available",
}
WHOIS_SERVERS: dict[str, str] = {
    "ac": "whois.nic.ac",
    "ad": "whois.nic.ad",
    "ae": "whois.aeda.net.ae",
    "aero": "whois.aero",
    "af": "whois.nic.af",
    "ag": "whois.nic.ag",
    "ai": "whois.nic.ai",
    "am": "whois.amnic.net",
    "ar": "whois.nic.ar",
    "asia": "whois.nic.asia",
    "at": "whois.nic.at",
    "au": "whois.auda.org.au",
    "be": "whois.dns.be",
    "bg": "whois.register.bg",
    "biz": "whois.nic.biz",
    "br": "whois.registro.br",
    "by": "whois.cctld.by",
    "ca": "whois.cira.ca",
    "cc": "ccwhois.verisign-grs.com",
    "ch": "whois.nic.ch",
    "cl": "whois.nic.cl",
    "com": "whois.verisign-grs.com",
    "cn": "whois.cnnic.cn",
    "co": "whois.registry.co",
    "cz": "whois.nic.cz",
    "de": "whois.denic.de",
    "dk": "whois.dk-hostmaster.dk",
    "ee": "whois.tld.ee",
    "es": "whois.nic.es",
    "eu": "whois.eu",
    "fr": "whois.nic.fr",
    "ge": "whois.nic.ge",
    "hk": "whois.hkirc.hk",
    "hr": "whois.dns.hr",
    "hu": "whois.nic.hu",
    "id": "whois.id",
    "ie": "whois.weare.ie",
    "il": "whois.isoc.org.il",
    "in": "whois.registry.in",
    "info": "whois.afilias.net",
    "io": "whois.nic.io",
    "ir": "whois.nic.ir",
    "is": "whois.isnic.is",
    "it": "whois.nic.it",
    "jp": "whois.jprs.jp",
    "kr": "whois.kr",
    "kz": "whois.nic.kz",
    "li": "whois.nic.li",
    "lt": "whois.domreg.lt",
    "lu": "whois.dns.lu",
    "lv": "whois.nic.lv",
    "me": "whois.nic.me",
    "md": "whois.nic.md",
    "mk": "whois.marnet.mk",
    "mx": "whois.mx",
    "my": "whois.mynic.my",
    "net": "whois.verisign-grs.com",
    "nl": "whois.domain-registry.nl",
    "no": "whois.norid.no",
    "nu": "whois.iis.nu",
    "nz": "whois.irs.net.nz",
    "org": "whois.publicinterestregistry.org",
    "pe": "kero.yachay.pe",
    "pl": "whois.dns.pl",
    "pt": "whois.dns.pt",
    "ro": "whois.rotld.ro",
    "rs": "whois.rnids.rs",
    "ru": "whois.tcinet.ru",
    "se": "whois.iis.se",
    "sg": "whois.sgnic.sg",
    "si": "whois.register.si",
    "sk": "whois.sk-nic.sk",
    "th": "whois.thnic.co.th",
    "tr": "whois.trabis.gov.tr",
    "tv": "tvwhois.verisign-grs.com",
    "tw": "whois.twnic.net.tw",
    "ua": "whois.ua",
    "uk": "whois.nic.uk",
    "uy": "whois.nic.org.uy",
    "us": "whois.nic.us",
    "ve": "whois.nic.ve",
    "vn": "whois.vnnic.vn",
    "za": "whois.registry.net.za",
}
WHOIS_SERVER_FALLBACKS: dict[str, tuple[str, ...]] = {
    "bg": ("whois.register.bg", WHOSE_DOMAINS_AVAILABILITY_LOOKUP),
    "no": ("whois.norid.no",),
    "ro": ("whois.rotld.ro", "whois.nic.ro"),
    "rs": ("whois.rnids.rs", "https://www.rnids.rs/sr/whois?search={fqdn}"),
}
STATIC_RDAP_BASE_URLS: dict[str, str] = {
    "us": "https://rdap.nic.us",
}

WhoisLookup = Callable[[str, str, float], Awaitable[str]]


@dataclass(frozen=True)
class DiscoveryObservationInput:
    source: str
    observed_at: datetime
    http_status: int | None = None
    latency_ms: int | None = None
    lifecycle_stage: str | None = None
    availability_status: str | None = None
    status_codes: list[str] = field(default_factory=list)
    rdap_updated_at: datetime | None = None
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
    if normalized & AVAILABLE_STATUS_CODES:
        return "not_found"
    if "pendingdelete" in normalized:
        return "pending_delete"
    if "redemptionperiod" in normalized:
        return "redemption"
    if status_codes:
        return "registered"
    return "unknown"


def classify_rdap_lifecycle_and_availability(
    status_codes: list[str],
    *,
    http_status: int | None = None,
    payload: dict | None = None,
) -> tuple[str, str]:
    title = str((payload or {}).get("title", "")).strip().lower()
    if http_status == 404 and title in RDAP_404_TAKEN_TITLES:
        return "unknown", "taken"
    lifecycle_stage = normalize_lifecycle_stage(status_codes, http_status=http_status)
    availability_status = "available" if lifecycle_stage == "not_found" else "taken"
    return lifecycle_stage, availability_status


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
    static_base_url = STATIC_RDAP_BASE_URLS.get(zone)
    if static_base_url:
        return f"{static_base_url.rstrip('/')}/domain/{fqdn}"
    raise ValueError(f"RDAP bootstrap has no endpoint for .{zone}")


async def check_discovery_domain_rdap(
    domain: DiscoveryDomain,
    *,
    client: httpx.AsyncClient | None = None,
    bootstrap_payload: dict | None = None,
    bootstrap_url: str = IANA_RDAP_BOOTSTRAP_URL,
    timeout_seconds: float = 5.0,
    whois_lookup: WhoisLookup | None = None,
) -> DiscoveryObservationInput:
    close_client = client is None
    http_client = client or httpx.AsyncClient(timeout=timeout_seconds)
    started_at = perf_counter()
    observed_at = datetime.now(timezone.utc)
    try:
        bootstrap = bootstrap_payload or await fetch_rdap_bootstrap(http_client, bootstrap_url)
        try:
            rdap_url = resolve_rdap_domain_url(domain.fqdn, bootstrap)
        except ValueError:
            return await check_discovery_domain_whois(
                domain,
                observed_at=observed_at,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                whois_lookup=whois_lookup,
            )
        rdap_response = await http_client.get(rdap_url)
        latency_ms = int((perf_counter() - started_at) * 1000)
        status_codes: list[str] = []
        raw_response = rdap_response.text
        content_type = rdap_response.headers.get("content-type", "").lower()
        if "json" in content_type:
            payload = rdap_response.json()
            status_codes = [str(item) for item in payload.get("status", []) if item]
            rdap_updated_at = extract_rdap_updated_at(payload)
        else:
            payload = {}
            rdap_updated_at = None
        lifecycle_stage, availability_status = classify_rdap_lifecycle_and_availability(
            status_codes,
            http_status=rdap_response.status_code,
            payload=payload,
        )
        if infer_zone(domain.fqdn) == "org" and lifecycle_stage == "not_found":
            whois_observation = await check_discovery_domain_whois(
                domain,
                observed_at=observed_at,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                whois_lookup=whois_lookup,
            )
            if whois_observation.lifecycle_stage == "dropzone":
                return whois_observation
        zone = infer_zone(domain.fqdn)
        if zone == "no" and lifecycle_stage == "unknown" and _whois_servers_for_zone(zone):
            whois_observation = await check_discovery_domain_whois(
                domain,
                observed_at=observed_at,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                whois_lookup=whois_lookup,
            )
            if whois_observation.lifecycle_stage != "unknown" or whois_observation.error:
                return whois_observation
        return DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=rdap_response.status_code,
            latency_ms=latency_ms,
            lifecycle_stage=lifecycle_stage,
            availability_status=availability_status,
            status_codes=status_codes,
            rdap_updated_at=rdap_updated_at,
            raw_response=raw_response[:10000],
        )
    except Exception as exc:
        if _whois_servers_for_zone(infer_zone(domain.fqdn)):
            whois_observation = await check_discovery_domain_whois(
                domain,
                observed_at=observed_at,
                started_at=started_at,
                timeout_seconds=timeout_seconds,
                whois_lookup=whois_lookup,
            )
            if whois_observation.error:
                return replace(
                    whois_observation,
                    error=f"RDAP failed: {exc}; WHOIS failed: {whois_observation.error}",
                )
            return whois_observation
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


async def check_discovery_domain_whois(
    domain: DiscoveryDomain,
    *,
    observed_at: datetime | None = None,
    started_at: float | None = None,
    timeout_seconds: float = 5.0,
    whois_lookup: WhoisLookup | None = None,
) -> DiscoveryObservationInput:
    started = perf_counter() if started_at is None else started_at
    checked_at = observed_at or datetime.now(timezone.utc)
    zone = infer_zone(domain.fqdn)
    servers = _whois_servers_for_zone(zone)
    if not servers:
        return DiscoveryObservationInput(
            source=WHOIS_FALLBACK_SOURCE,
            observed_at=checked_at,
            latency_ms=int((perf_counter() - started) * 1000),
            lifecycle_stage="unknown",
            availability_status="unknown",
            error=f"No WHOIS fallback configured for .{zone}",
        )
    lookup = whois_lookup or default_whois_lookup
    last_error: Exception | None = None
    last_observation: DiscoveryObservationInput | None = None
    for server in servers:
        try:
            raw_response = await lookup(domain.fqdn, server, timeout_seconds)
            observation = parse_whois_response(
                raw_response,
                fqdn=domain.fqdn,
                observed_at=checked_at,
                latency_ms=int((perf_counter() - started) * 1000),
            )
            if observation.error == WHOIS_RATE_LIMIT_ERROR and server != servers[-1]:
                last_observation = observation
                continue
            return observation
        except Exception as exc:
            last_error = exc
            continue
    if last_observation is not None:
        return last_observation
    return DiscoveryObservationInput(
        source=WHOIS_FALLBACK_SOURCE,
        observed_at=checked_at,
        latency_ms=int((perf_counter() - started) * 1000),
        lifecycle_stage="unknown",
        availability_status="unknown",
        error=str(last_error) if last_error else f"No WHOIS fallback configured for .{zone}",
    )


async def default_whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
    if server == WHOSE_DOMAINS_AVAILABILITY_LOOKUP:
        return await _query_whose_domains_availability(fqdn, timeout_seconds)
    if server.startswith(("http://", "https://")):
        return await _query_http_whois(fqdn, server, timeout_seconds)
    return await asyncio.to_thread(_query_whois, fqdn, server, timeout_seconds)


async def _query_whose_domains_availability(fqdn: str, timeout_seconds: float) -> str:
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        response = await client.post(
            WHOSE_DOMAINS_BULK_SEARCH_URL,
            json={"domains": [fqdn], "searchType": "availability"},
        )
        response.raise_for_status()
        return response.text


async def _query_http_whois(fqdn: str, url_template: str, timeout_seconds: float) -> str:
    url = url_template.format(fqdn=quote(fqdn, safe=""))
    async with httpx.AsyncClient(
        timeout=timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0"},
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _whois_servers_for_zone(zone: str) -> tuple[str, ...]:
    if zone in WHOIS_SERVER_FALLBACKS:
        return WHOIS_SERVER_FALLBACKS[zone]
    server = WHOIS_SERVERS.get(zone)
    return (server,) if server else ()


def _query_whois(fqdn: str, server: str, timeout_seconds: float) -> str:
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in resolve_whois_addresses(server, 43):
        try:
            with socket.socket(family, socktype, proto) as sock:
                sock.settimeout(timeout_seconds)
                sock.connect(sockaddr)
                sock.sendall(f"{fqdn}\r\n".encode("utf-8"))
                chunks: list[bytes] = []
                received = 0
                while received < WHOIS_RESPONSE_LIMIT_BYTES:
                    chunk = sock.recv(min(4096, WHOIS_RESPONSE_LIMIT_BYTES - received))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                return b"".join(chunks).decode("utf-8", errors="replace")
        except OSError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise OSError(f"WHOIS server {server} did not resolve")


def resolve_whois_addresses(server: str, port: int = 43) -> list[tuple]:
    addresses = list(socket.getaddrinfo(server, port, type=socket.SOCK_STREAM))
    return sorted(addresses, key=lambda item: 0 if item[0] == socket.AF_INET else 1)


def parse_whois_response(
    raw_response: str,
    *,
    fqdn: str,
    observed_at: datetime,
    latency_ms: int | None = None,
) -> DiscoveryObservationInput:
    del fqdn
    raw = raw_response or ""
    normalized_raw = raw.lower()
    error = _detect_whois_error(normalized_raw)
    status_codes = _extract_whois_status_codes(raw)
    lifecycle_stage = _classify_whois_lifecycle(raw, status_codes)
    availability_status = _availability_for_lifecycle(lifecycle_stage)
    if error:
        lifecycle_stage = "unknown"
        availability_status = "taken" if error == WHOIS_RATE_LIMIT_ERROR else "unknown"
    return DiscoveryObservationInput(
        source=WHOIS_FALLBACK_SOURCE,
        observed_at=observed_at,
        http_status=200 if raw else None,
        latency_ms=latency_ms,
        lifecycle_stage=lifecycle_stage,
        availability_status=availability_status,
        status_codes=status_codes,
        raw_response=raw[:WHOIS_RESPONSE_LIMIT_BYTES],
        error=error,
    )


def _detect_whois_error(normalized_raw: str) -> str | None:
    for pattern in WHOIS_RATE_LIMIT_PATTERNS:
        if pattern in normalized_raw:
            return WHOIS_RATE_LIMIT_ERROR
    return None


def _extract_whois_status_codes(raw_response: str) -> list[str]:
    statuses: list[str] = []
    for match in WHOIS_STATUS_PATTERN.finditer(raw_response):
        value = match.group(1).strip()
        if not value:
            continue
        status = value.split("http://", 1)[0].split("https://", 1)[0].strip()
        if status and status not in statuses:
            statuses.append(status)
    return statuses


def _classify_whois_lifecycle(raw_response: str, status_codes: list[str]) -> str:
    json_lifecycle = _classify_json_availability(raw_response)
    if json_lifecycle is not None:
        return json_lifecycle
    normalized_raw = raw_response.lower()
    normalized_statuses = {_normalize_status_code(item) for item in status_codes}
    if PIR_DROPZONE_PHRASE in normalized_raw:
        return "dropzone"
    if normalized_statuses & AVAILABLE_STATUS_CODES:
        return "not_found"
    if any(pattern in normalized_raw for pattern in WHOIS_NOT_FOUND_PATTERNS):
        return "not_found"
    if "pendingdelete" in normalized_statuses or "pendingdelete" in _normalize_status_code(normalized_raw):
        return "pending_delete"
    if "redemptionperiod" in normalized_statuses or "redemptionperiod" in _normalize_status_code(normalized_raw):
        return "redemption"
    if status_codes or raw_response.strip():
        return "registered"
    return "unknown"


def _classify_json_availability(raw_response: str) -> str | None:
    try:
        payload = json.loads(raw_response)
    except ValueError:
        return None
    for node in _walk_json_values(payload):
        if not isinstance(node, dict):
            continue
        availability = node.get("availability")
        available = node.get("available")
        status = node.get("status")
        if availability is not None:
            value = str(availability).strip().lower()
            if value in {"true", "available", "1", "yes"}:
                return "not_found"
            if value in {"false", "registered", "taken", "0", "no"}:
                return "registered"
        if isinstance(available, bool):
            return "not_found" if available else "registered"
        if isinstance(status, str):
            normalized_status = status.strip().lower()
            if normalized_status in {"available", "free"}:
                return "not_found"
            if normalized_status in {"registered", "taken", "unavailable"}:
                return "registered"
    return None


def _walk_json_values(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_json_values(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json_values(child)


def _availability_for_lifecycle(lifecycle_stage: str) -> str:
    if lifecycle_stage == "not_found":
        return "available"
    if lifecycle_stage == "dropzone":
        return "dropzone"
    return "taken"


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
    whois_lookup: WhoisLookup | None = None,
    concurrency: int = 5,
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
        except Exception:
            bootstrap_payload = {"services": []}

        semaphore = asyncio.Semaphore(max(int(concurrency), 1))

        async def check_domain(domain: DiscoveryDomain) -> DiscoveryObservationInput:
            async with semaphore:
                return await check_discovery_domain_rdap(
                    domain,
                    client=http_client,
                    bootstrap_payload=bootstrap_payload,
                    bootstrap_url=bootstrap_url,
                    timeout_seconds=timeout_seconds,
                    whois_lookup=whois_lookup,
                )

        observations = await asyncio.gather(*(check_domain(domain) for domain in domains))

        for index, (domain, observation) in enumerate(zip(domains, observations, strict=True)):
            previous_status = domain.status
            previous_pending_at = domain.first_seen_pending_delete_at
            previous_available_at = domain.available_first_seen_at
            observation = replace(observation, observed_at=now)
            apply_discovery_observation(
                domain,
                observation,
                next_check_offset=_batch_next_check_offset(index=index, total=len(domains)),
                include_active_jitter=False,
            )
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
        await session.flush()
        for domain in domains:
            await trim_discovery_observations(session, domain.id)
        return len(domains)
    finally:
        if close_client:
            await http_client.aclose()


async def trim_discovery_observations(
    session: AsyncSession,
    discovery_domain_id: int,
    *,
    keep: int = 5,
) -> None:
    result = await session.execute(
        select(DiscoveryObservation.id)
        .where(DiscoveryObservation.discovery_domain_id == discovery_domain_id)
        .order_by(DiscoveryObservation.observed_at.desc(), DiscoveryObservation.id.desc())
        .offset(max(keep, 0))
    )
    stale_ids = list(result.scalars().all())
    if stale_ids:
        await session.execute(delete(DiscoveryObservation).where(DiscoveryObservation.id.in_(stale_ids)))


def calculate_next_check_at(
    domain: DiscoveryDomain,
    now: datetime,
    *,
    include_active_jitter: bool = True,
) -> datetime | None:
    if domain.is_enabled is False or domain.status in {"available", "ignored"}:
        return None

    if domain.status == "error":
        return now + _stable_jittered_interval(
            getattr(domain, "fqdn", ""),
            base=ERROR_RETRY_INTERVAL,
            jitter=ERROR_RETRY_JITTER,
        )

    if (
        domain.status == "pending_delete"
        and domain.predicted_drop_start_at is not None
        and domain.predicted_drop_end_at is not None
        and domain.predicted_drop_start_at.date() <= now.date() <= domain.predicted_drop_end_at.date()
    ):
        return now + _active_next_check_interval(
            domain,
            base=DROP_DAY_SCAN_INTERVAL,
            include_jitter=include_active_jitter,
        )

    configured_interval = getattr(domain, "check_interval_seconds", None)
    if domain.last_lifecycle_stage == "redemption":
        domain_interval = (
            timedelta(seconds=max(int(configured_interval), 10)) if configured_interval else REDEMPTION_SCAN_INTERVAL
        )
        return now + _active_next_check_interval(
            domain,
            base=min(domain_interval, REDEMPTION_SCAN_INTERVAL),
            include_jitter=include_active_jitter,
        )
    if domain.status == "pending_delete":
        domain_interval = (
            timedelta(seconds=max(int(configured_interval), 10)) if configured_interval else PENDING_DELETE_SCAN_INTERVAL
        )
        return now + _active_next_check_interval(
            domain,
            base=min(domain_interval, PENDING_DELETE_SCAN_INTERVAL),
            include_jitter=include_active_jitter,
        )
    domain_interval = timedelta(seconds=max(int(configured_interval), 10)) if configured_interval else DEFAULT_SCAN_INTERVAL
    return now + _active_next_check_interval(
        domain,
        base=min(domain_interval, DEFAULT_SCAN_INTERVAL),
        include_jitter=include_active_jitter,
    )


def stagger_initial_check_at(
    now: datetime,
    *,
    index: int,
    total: int,
    spread: timedelta = INITIAL_DISCOVERY_IMPORT_SPREAD,
) -> datetime:
    if total <= 1:
        return now
    safe_index = max(index, 0)
    slot_seconds = spread.total_seconds() / total
    return now + timedelta(seconds=slot_seconds * safe_index)


def _stable_jittered_interval(key: str, *, base: timedelta, jitter: timedelta) -> timedelta:
    jitter_seconds = max(int(jitter.total_seconds()), 0)
    if jitter_seconds <= 0:
        return base
    checksum = zlib.crc32(key.encode("utf-8"))
    return base + timedelta(seconds=checksum % (jitter_seconds + 1))


def _active_next_check_interval(
    domain: DiscoveryDomain,
    *,
    base: timedelta,
    include_jitter: bool,
) -> timedelta:
    if not include_jitter:
        return base
    return _stable_jittered_interval(
        getattr(domain, "fqdn", ""),
        base=base,
        jitter=ACTIVE_NEXT_CHECK_JITTER,
    )


def _batch_next_check_offset(*, index: int, total: int) -> timedelta:
    if total <= 1:
        return timedelta(0)
    spread_seconds = max(int(BATCH_NEXT_CHECK_SPREAD.total_seconds()), 0)
    if spread_seconds <= 0:
        return timedelta(0)
    return timedelta(seconds=max(index, 0) % (spread_seconds + 1))


def _is_whois_rate_limit_observation(observation: DiscoveryObservationInput) -> bool:
    return observation.source == WHOIS_FALLBACK_SOURCE and observation.error == WHOIS_RATE_LIMIT_ERROR


def apply_discovery_observation(
    domain: DiscoveryDomain,
    observation: DiscoveryObservationInput,
    *,
    next_check_offset: timedelta = timedelta(0),
    include_active_jitter: bool = True,
) -> None:
    observed_at = _ensure_aware(observation.observed_at)
    lifecycle_stage = observation.lifecycle_stage or normalize_lifecycle_stage(
        observation.status_codes,
        http_status=observation.http_status,
    )
    status_codes_json = json.dumps(observation.status_codes, ensure_ascii=True) if observation.status_codes else None
    drop_prediction_enabled = getattr(domain, "drop_prediction_enabled", True) is not False

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
        if observation.source != WHOIS_FALLBACK_SOURCE:
            anchor_at, anchor_source = resolve_redemption_anchor(domain, observation, observed_at)
            domain.redemption_anchor_at = domain.redemption_anchor_at or anchor_at
            domain.redemption_anchor_source = domain.redemption_anchor_source or anchor_source
        if drop_prediction_enabled and observation.source != WHOIS_FALLBACK_SOURCE:
            if domain.predicted_pending_delete_at is None:
                domain.predicted_pending_delete_at = domain.redemption_anchor_at + REDEMPTION_DURATION
            if domain.predicted_drop_start_at is None:
                domain.predicted_drop_start_at = domain.predicted_pending_delete_at + PENDING_DELETE_DURATION
            if domain.predicted_drop_end_at is None:
                domain.predicted_drop_end_at = domain.predicted_drop_start_at
    elif lifecycle_stage == "pending_delete":
        domain.status = "pending_delete"
        if domain.first_seen_pending_delete_at is None:
            domain.pending_delete_previous_seen_at = previous_checked_at or observed_at
            domain.first_seen_pending_delete_at = observed_at
            if drop_prediction_enabled and observation.source != WHOIS_FALLBACK_SOURCE:
                if domain.predicted_pending_delete_at is None:
                    domain.predicted_pending_delete_at = domain.pending_delete_previous_seen_at
                domain.predicted_drop_start_at = domain.pending_delete_previous_seen_at + PENDING_DELETE_DURATION
                domain.predicted_drop_end_at = observed_at + PENDING_DELETE_DURATION
        domain.last_seen_pending_delete_at = observed_at
    elif lifecycle_stage == "not_found" or observation.availability_status == "available":
        domain.status = "available"
        domain.available_first_seen_at = domain.available_first_seen_at or observed_at
    elif lifecycle_stage == "dropzone" or observation.availability_status == "dropzone":
        domain.status = "dropzone"
    elif domain.status in {"tracking", "error"}:
        domain.status = "tracking"

    if observation.error and not _is_whois_rate_limit_observation(observation):
        domain.status = "error"

    next_check_at = calculate_next_check_at(domain, observed_at, include_active_jitter=include_active_jitter)
    if next_check_at is not None and next_check_offset > timedelta(0):
        next_check_at += next_check_offset
    domain.next_check_at = next_check_at
    domain.updated_at = observed_at


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def extract_rdap_updated_at(payload: dict) -> datetime | None:
    events = payload.get("events", [])
    if not isinstance(events, list):
        return None
    preferred_actions = {"last changed", "last update"}
    for event in events:
        if not isinstance(event, dict):
            continue
        event_date = event.get("eventDate")
        if not isinstance(event_date, str):
            continue
        parsed = _parse_rdap_datetime(event_date)
        if parsed is None:
            continue
        action = str(event.get("eventAction", "")).strip().lower()
        if action in preferred_actions:
            return parsed
    return None


def resolve_redemption_anchor(
    domain: DiscoveryDomain,
    observation: DiscoveryObservationInput,
    observed_at: datetime,
) -> tuple[datetime, str]:
    if observation.rdap_updated_at is not None:
        candidate = _ensure_aware(observation.rdap_updated_at)
        if candidate <= observed_at:
            return candidate, "rdap_updated_at"
    if domain.first_seen_redemption_at is not None:
        return _ensure_aware(domain.first_seen_redemption_at), "first_seen_redemption_at"
    return observed_at, "first_seen_redemption_at"


def _parse_rdap_datetime(value: str) -> datetime | None:
    try:
        return _ensure_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


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
    if domain.first_seen_pending_delete_at and previous_pending_at is None:
        return (
            "Discovery pendingDelete\n\n"
            f"Domain: {domain.fqdn}\n"
            f"First seen: {domain.first_seen_pending_delete_at.isoformat()}\n"
            f"Predicted drop: {_format_optional_datetime_range(domain.predicted_drop_start_at, domain.predicted_drop_end_at)}"
        )
    if domain.status == "dropzone" and previous_status != "dropzone":
        return (
            "Discovery dropzone\n\n"
            f"Domain: {domain.fqdn}\n"
            "State: PIR Dropzone / special application required\n"
            "Action: confirm through registrar before treating as normal available"
        )
    if domain.available_first_seen_at and previous_available_at is None:
        return (
            "Discovery available\n\n"
            f"Domain: {domain.fqdn}\n"
            f"First seen: {domain.available_first_seen_at.isoformat()}"
        )
    return None


def _format_optional_datetime_range(start: datetime | None, end: datetime | None) -> str:
    if start is None or end is None:
        return "unknown"
    return f"{start.isoformat()} - {end.isoformat()}"
