from __future__ import annotations

import asyncio
import json
import re
import socket
from datetime import datetime, timezone
from time import perf_counter
from urllib.parse import quote

import httpx

from app.control_client import DiscoveryControlTask

WHOIS_FALLBACK_SOURCE = "whois_fallback"
WHOIS_RATE_LIMIT_ERROR = "WHOIS rate limit or access denied"
WHOIS_RESPONSE_LIMIT_BYTES = 10000
WHOSE_DOMAINS_AVAILABILITY_LOOKUP = "whose-domains://availability"
WHOSE_DOMAINS_BULK_SEARCH_URL = "https://whose.domains/api/tools/bulk-search"
PIR_DROPZONE_PHRASE = "pir dropzone service"
WHOIS_STATUS_PATTERN = re.compile(r"(?im)^\s*(?:domain\s+)?status\s*:\s*(.+?)\s*$")
AVAILABLE_STATUS_CODES = {"available"}
RDAP_404_TAKEN_TITLES = {"auction pending"}
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
WHOIS_SERVERS: dict[str, str] = {
    "ad": "whois.nic.ad",
    "ae": "whois.aeda.net.ae",
    "al": "whois.ripe.net",
    "ar": "whois.nic.ar",
    "ba": "whois.ripe.net",
    "bg": "whois.register.bg",
    "br": "whois.registro.br",
    "by": "whois.cctld.by",
    "com": "whois.verisign-grs.com",
    "cy": "whois.ripe.net",
    "cz": "whois.nic.cz",
    "dk": "whois.dk-hostmaster.dk",
    "ee": "whois.tld.ee",
    "eu": "whois.eu",
    "fi": "whois.fi",
    "fr": "whois.nic.fr",
    "ge": "whois.nic.ge",
    "hr": "whois.dns.hr",
    "lt": "whois.domreg.lt",
    "lv": "whois.nic.lv",
    "md": "whois.nic.md",
    "me": "whois.nic.me",
    "mk": "whois.marnet.mk",
    "mt": "whois.ripe.net",
    "net": "whois.verisign-grs.com",
    "nl": "whois.domain-registry.nl",
    "no": "whois.norid.no",
    "org": "whois.publicinterestregistry.org",
    "pl": "whois.dns.pl",
    "ro": "whois.rotld.ro",
    "rs": "whois.rnids.rs",
    "se": "whois.iis.se",
    "sg": "whois.sgnic.sg",
    "sk": "whois.sk-nic.sk",
    "tr": "whois.trabis.gov.tr",
    "uk": "whois.nic.uk",
    "us": "whois.nic.us",
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


async def check_discovery_task(task: DiscoveryControlTask) -> dict:
    started_at = perf_counter()
    observed_at = datetime.now(timezone.utc)
    timeout_seconds = max(float(task.timeout_seconds), 0.25)
    async with httpx.AsyncClient(timeout=timeout_seconds, headers={"User-Agent": "Mozilla/5.0"}) as client:
        try:
            bootstrap = await _fetch_rdap_bootstrap(client, task.bootstrap_url)
        except Exception:
            bootstrap = {"services": []}
        try:
            rdap_url = _resolve_rdap_domain_url(task.fqdn, task.zone, bootstrap)
        except ValueError:
            return await _check_whois(task.fqdn, task.zone, observed_at, started_at, timeout_seconds)

        try:
            response = await client.get(rdap_url)
            latency_ms = int((perf_counter() - started_at) * 1000)
            payload: dict = {}
            status_codes: list[str] = []
            raw_response = response.text
            if "json" in response.headers.get("content-type", "").lower():
                payload = response.json()
                status_codes = [str(item) for item in payload.get("status", []) if item]
            lifecycle_stage, availability_status = _classify_rdap(status_codes, response.status_code, payload)
            if task.zone == "org" and lifecycle_stage == "not_found":
                whois_observation = await _check_whois(task.fqdn, task.zone, observed_at, started_at, timeout_seconds)
                if whois_observation["lifecycle_stage"] == "dropzone":
                    return whois_observation
            if task.zone == "no" and lifecycle_stage == "unknown" and _whois_servers_for_zone(task.zone):
                whois_observation = await _check_whois(task.fqdn, task.zone, observed_at, started_at, timeout_seconds)
                if whois_observation["lifecycle_stage"] != "unknown" or whois_observation.get("error"):
                    return whois_observation
            return _result(
                source="rdap",
                observed_at=observed_at,
                http_status=response.status_code,
                latency_ms=latency_ms,
                lifecycle_stage=lifecycle_stage,
                availability_status=availability_status,
                status_codes=status_codes,
                raw_response=raw_response,
            )
        except Exception as exc:
            if _whois_servers_for_zone(task.zone):
                whois_observation = await _check_whois(task.fqdn, task.zone, observed_at, started_at, timeout_seconds)
                if whois_observation.get("error"):
                    whois_observation["error"] = f"RDAP failed: {exc}; WHOIS failed: {whois_observation['error']}"
                return whois_observation
            return _result(
                source="rdap",
                observed_at=observed_at,
                latency_ms=int((perf_counter() - started_at) * 1000),
                lifecycle_stage="unknown",
                availability_status="unknown",
                error=str(exc),
            )


async def _fetch_rdap_bootstrap(client: httpx.AsyncClient, bootstrap_url: str) -> dict:
    response = await client.get(bootstrap_url)
    response.raise_for_status()
    return response.json()


def _resolve_rdap_domain_url(fqdn: str, zone: str, bootstrap_payload: dict) -> str:
    normalized_zone = zone.lower()
    for service in bootstrap_payload.get("services", []):
        if not isinstance(service, list) or len(service) < 2:
            continue
        tlds, urls = service[0], service[1]
        if not isinstance(tlds, list) or not isinstance(urls, list):
            continue
        if normalized_zone not in {str(item).lower() for item in tlds}:
            continue
        base_url = next((str(item) for item in urls if item), "")
        if base_url:
            return f"{base_url.rstrip('/')}/domain/{fqdn}"
    static_base_url = STATIC_RDAP_BASE_URLS.get(normalized_zone)
    if static_base_url:
        return f"{static_base_url.rstrip('/')}/domain/{fqdn}"
    raise ValueError(f"RDAP bootstrap has no endpoint for .{zone}")


async def _check_whois(
    fqdn: str,
    zone: str,
    observed_at: datetime,
    started_at: float,
    timeout_seconds: float,
) -> dict:
    servers = _whois_servers_for_zone(zone)
    if not servers:
        return _result(
            source=WHOIS_FALLBACK_SOURCE,
            observed_at=observed_at,
            latency_ms=int((perf_counter() - started_at) * 1000),
            lifecycle_stage="unknown",
            availability_status="unknown",
            error=f"No WHOIS fallback configured for .{zone}",
        )
    last_error: Exception | None = None
    last_rate_limited: dict | None = None
    for server in servers:
        try:
            raw_response = await _lookup_whois(fqdn, server, timeout_seconds)
            observation = _parse_whois(raw_response, observed_at, int((perf_counter() - started_at) * 1000))
            if observation.get("error") == WHOIS_RATE_LIMIT_ERROR and server != servers[-1]:
                last_rate_limited = observation
                continue
            return observation
        except Exception as exc:
            last_error = exc
            continue
    if last_rate_limited is not None:
        return last_rate_limited
    return _result(
        source=WHOIS_FALLBACK_SOURCE,
        observed_at=observed_at,
        latency_ms=int((perf_counter() - started_at) * 1000),
        lifecycle_stage="unknown",
        availability_status="unknown",
        error=str(last_error) if last_error else f"No WHOIS fallback configured for .{zone}",
    )


async def _lookup_whois(fqdn: str, server: str, timeout_seconds: float) -> str:
    if server == WHOSE_DOMAINS_AVAILABILITY_LOOKUP:
        return await _query_whose_domains_availability(fqdn, timeout_seconds)
    if server.startswith(("http://", "https://")):
        return await _query_http_whois(fqdn, server, timeout_seconds)
    return await asyncio.to_thread(_query_whois, fqdn, server, timeout_seconds)


async def _query_whose_domains_availability(fqdn: str, timeout_seconds: float) -> str:
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.post(
            WHOSE_DOMAINS_BULK_SEARCH_URL,
            json={"domains": [fqdn], "searchType": "availability"},
        )
        response.raise_for_status()
        return response.text


async def _query_http_whois(fqdn: str, url_template: str, timeout_seconds: float) -> str:
    url = url_template.format(fqdn=quote(fqdn, safe=""))
    async with httpx.AsyncClient(timeout=timeout_seconds, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"}) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


def _query_whois(fqdn: str, server: str, timeout_seconds: float) -> str:
    last_error: OSError | None = None
    for family, socktype, proto, _, sockaddr in sorted(
        socket.getaddrinfo(server, 43, type=socket.SOCK_STREAM),
        key=lambda item: 0 if item[0] == socket.AF_INET else 1,
    ):
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


def _whois_servers_for_zone(zone: str) -> tuple[str, ...]:
    normalized_zone = zone.lower()
    if normalized_zone in WHOIS_SERVER_FALLBACKS:
        return WHOIS_SERVER_FALLBACKS[normalized_zone]
    server = WHOIS_SERVERS.get(normalized_zone)
    return (server,) if server else ()


def _parse_whois(raw_response: str, observed_at: datetime, latency_ms: int) -> dict:
    raw = raw_response or ""
    normalized_raw = raw.lower()
    error = _detect_whois_error(normalized_raw)
    status_codes = _extract_whois_status_codes(raw)
    lifecycle_stage = _classify_whois_lifecycle(raw, status_codes)
    availability_status = "available" if lifecycle_stage == "not_found" else "taken"
    if lifecycle_stage == "dropzone":
        availability_status = "dropzone"
    if error:
        lifecycle_stage = "unknown"
        availability_status = "taken" if error == WHOIS_RATE_LIMIT_ERROR else "unknown"
    return _result(
        source=WHOIS_FALLBACK_SOURCE,
        observed_at=observed_at,
        http_status=200 if raw else None,
        latency_ms=latency_ms,
        lifecycle_stage=lifecycle_stage,
        availability_status=availability_status,
        status_codes=status_codes,
        raw_response=raw,
        error=error,
    )


def _classify_rdap(status_codes: list[str], http_status: int | None, payload: dict) -> tuple[str, str]:
    title = str(payload.get("title", "")).strip().lower()
    if http_status == 404 and title in RDAP_404_TAKEN_TITLES:
        return "unknown", "taken"
    lifecycle_stage = _normalize_lifecycle_stage(status_codes, http_status=http_status)
    availability_status = "available" if lifecycle_stage == "not_found" else "taken"
    return lifecycle_stage, availability_status


def _normalize_lifecycle_stage(status_codes: list[str], *, http_status: int | None = None) -> str:
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


def _normalize_status_code(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _result(
    *,
    source: str,
    observed_at: datetime,
    http_status: int | None = None,
    latency_ms: int | None = None,
    lifecycle_stage: str | None = None,
    availability_status: str | None = None,
    status_codes: list[str] | None = None,
    raw_response: str | None = None,
    error: str | None = None,
) -> dict:
    return {
        "source": source,
        "observed_at": observed_at.isoformat(),
        "http_status": http_status,
        "latency_ms": latency_ms,
        "lifecycle_stage": lifecycle_stage,
        "availability_status": availability_status,
        "status_codes": status_codes or [],
        "raw_response": (raw_response or "")[:WHOIS_RESPONSE_LIMIT_BYTES],
        "error": error,
    }
