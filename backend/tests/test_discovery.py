from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import socket

import httpx
import pytest
import asyncio
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import DiscoveryDomain, DiscoveryObservation, ZoneRule, ZoneStrategy
from app.db.session import get_db
from app.services.discovery import (
    DiscoveryObservationInput,
    WHOIS_SERVERS,
    WHOSE_DOMAINS_AVAILABILITY_LOOKUP,
    WHOIS_RATE_LIMIT_ERROR,
    apply_discovery_observation,
    calculate_next_check_at,
    check_discovery_domain_rdap,
    check_discovery_domain_whois,
    extract_rdap_updated_at,
    normalize_lifecycle_stage,
    parse_whois_response,
    process_due_discovery_domains,
    resolve_rdap_domain_url,
    resolve_whois_addresses,
    stagger_initial_check_at,
    trim_discovery_observations,
    _build_transition_notification,
)


def test_discovery_normalizes_epp_statuses():
    assert normalize_lifecycle_stage(["clientTransferProhibited", "redemptionPeriod"]) == "redemption"
    assert normalize_lifecycle_stage(["pendingDelete"]) == "pending_delete"
    assert normalize_lifecycle_stage([], http_status=404) == "not_found"


def test_discovery_resolves_rdap_url_from_iana_bootstrap():
    bootstrap = {
        "services": [
            [["org"], ["https://rdap.publicinterestregistry.example/rdap/"]],
            [["com", "net"], ["https://rdap.verisign.example/com/v1/"]],
        ]
    }

    assert (
        resolve_rdap_domain_url("example.com", bootstrap)
        == "https://rdap.verisign.example/com/v1/domain/example.com"
    )


def test_discovery_extracts_rdap_updated_at_from_last_changed_event():
    payload = {
        "events": [
            {"eventAction": "registration", "eventDate": "2024-05-20T14:44:28Z"},
            {"eventAction": "last changed", "eventDate": "2026-07-02T09:14:52Z"},
        ]
    }

    assert extract_rdap_updated_at(payload) == datetime(2026, 7, 2, 9, 14, 52, tzinfo=timezone.utc)


def test_discovery_ignores_rdap_database_update_as_redemption_anchor():
    payload = {
        "events": [
            {"eventAction": "registration", "eventDate": "2024-05-20T14:44:28Z"},
            {"eventAction": "last update of RDAP database", "eventDate": "2026-07-04T12:00:00Z"},
        ]
    }

    assert extract_rdap_updated_at(payload) is None


def test_discovery_observation_marks_status_and_owner_change_with_exact_time():
    domain = DiscoveryDomain(fqdn="example.com", zone="com", status="tracking")
    first_seen = datetime(2026, 7, 25, 10, 0, 0, tzinfo=timezone.utc)
    changed_at = datetime(2026, 7, 25, 10, 0, 7, tzinfo=timezone.utc)

    first_observation = apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=first_seen,
            lifecycle_stage="pending_delete",
            availability_status="taken",
            status_codes=["pendingDelete"],
            registrar_name="Old Registrar",
            owner_handle="OLD-HOLDER",
            name_servers=["ns1.old.example"],
        ),
    )
    changed_observation = apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=changed_at,
            lifecycle_stage="registered",
            availability_status="taken",
            status_codes=["active"],
            registrar_name="New Registrar",
            owner_handle="NEW-HOLDER",
            name_servers=["ns1.new.example"],
        ),
    )

    assert first_observation.change_detected is True
    assert first_observation.change_summary == "first seen"
    assert changed_observation.change_detected is True
    assert domain.last_change_at == changed_at
    assert "status" in (changed_observation.change_summary or "")
    assert "owner" in (changed_observation.change_summary or "")


def test_discovery_parses_generic_whois_pending_delete():
    observation = parse_whois_response(
        """
        Domain Name: EXAMPLE.MX
        Domain Status: pendingDelete
        Updated Date: 2026-07-06T10:00:00Z
        """,
        fqdn="example.mx",
        observed_at=datetime(2026, 7, 6, 10, 1, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "pending_delete"
    assert observation.availability_status == "taken"
    assert observation.status_codes == ["pendingDelete"]


def test_discovery_parses_generic_whois_not_found():
    observation = parse_whois_response(
        "No match for domain \"free-example.mx\".",
        fqdn="free-example.mx",
        observed_at=datetime(2026, 7, 6, 10, 1, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_treats_whois_access_limit_as_safe_taken_without_hard_error_status():
    domain = DiscoveryDomain(fqdn="365proservices.ae", zone="ae", status="tracking")
    observation = parse_whois_response(
        "BLACKLISTED: You have exceeded the query limit for your network or IP address.",
        fqdn=domain.fqdn,
        observed_at=datetime(2026, 7, 18, 0, 1, tzinfo=timezone.utc),
        latency_ms=25,
    )

    apply_discovery_observation(domain, observation)

    assert observation.error == WHOIS_RATE_LIMIT_ERROR
    assert observation.lifecycle_stage == "unknown"
    assert observation.availability_status == "taken"
    assert domain.status == "tracking"
    assert domain.last_error == WHOIS_RATE_LIMIT_ERROR


def test_discovery_parses_eurid_available_status_as_available():
    observation = parse_whois_response(
        """
        % WHOIS richelais.eu
        Domain: richelais.eu
        Script: LATIN
        Status: AVAILABLE
        """,
        fqdn="richelais.eu",
        observed_at=datetime(2026, 7, 7, 9, 40, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"
    assert observation.status_codes == ["AVAILABLE"]


@pytest.mark.parametrize(
    ("zone", "server"),
    [
        ("hr", "whois.dns.hr"),
        ("md", "whois.nic.md"),
        ("mk", "whois.marnet.mk"),
        ("tr", "whois.trabis.gov.tr"),
        ("ge", "whois.nic.ge"),
        ("no", "whois.norid.no"),
    ],
)
def test_discovery_has_whois_fallback_for_requested_cctlds(zone: str, server: str):
    assert WHOIS_SERVERS[zone] == server


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fqdn", "server"),
    [
        ("5senses.hr", "whois.dns.hr"),
        ("agrosucces.md", "whois.nic.md"),
        ("40burninghot.mk", "whois.marnet.mk"),
        ("0058.tr", "whois.trabis.gov.tr"),
        ("babina.ge", "whois.nic.ge"),
        ("countrywestern.no", "whois.norid.no"),
    ],
)
async def test_discovery_falls_back_to_requested_cctld_whois_servers(fqdn: str, server: str):
    domain = DiscoveryDomain(fqdn=fqdn, zone=fqdn.rsplit(".", 1)[-1])

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        return httpx.Response(404)

    async def whois_lookup(received_fqdn: str, received_server: str, timeout_seconds: float) -> str:
        assert received_fqdn == fqdn
        assert received_server == server
        assert timeout_seconds == 5.0
        return f"domain: {fqdn}\nstatus: active\n"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "registered"
    assert observation.availability_status == "taken"


@pytest.mark.asyncio
async def test_discovery_tries_ro_whois_fallback_server_when_primary_is_unreachable():
    domain = DiscoveryDomain(fqdn="1acsisabsolut.ro", zone="ro")
    attempted_servers: list[str] = []

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "1acsisabsolut.ro"
        assert timeout_seconds == 5.0
        attempted_servers.append(server)
        if server == "whois.rotld.ro":
            raise OSError("[Errno 113] No route to host")
        return "Domain Name: 1acsisabsolut.ro\nDomain Status: OK\n"

    observation = await check_discovery_domain_whois(domain, whois_lookup=whois_lookup)

    assert attempted_servers == ["whois.rotld.ro", "whois.nic.ro"]
    assert observation.error is None
    assert observation.lifecycle_stage == "registered"
    assert observation.availability_status == "taken"


@pytest.mark.asyncio
async def test_discovery_tries_rs_http_whois_fallback_when_port43_resets():
    domain = DiscoveryDomain(fqdn="aluradom.rs", zone="rs")
    attempted_servers: list[str] = []

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "aluradom.rs"
        assert timeout_seconds == 5.0
        attempted_servers.append(server)
        if server == "whois.rnids.rs":
            raise ConnectionResetError("[Errno 104] Connection reset by peer")
        return "Naziv domena: aluradom.rs\nStatus domena: Active\n"

    observation = await check_discovery_domain_whois(domain, whois_lookup=whois_lookup)

    assert attempted_servers == ["whois.rnids.rs", "https://www.rnids.rs/sr/whois?search={fqdn}"]
    assert observation.error is None
    assert observation.lifecycle_stage == "registered"
    assert observation.availability_status == "taken"


@pytest.mark.asyncio
async def test_discovery_tries_external_availability_fallback_when_bg_registry_is_rate_limited():
    domain = DiscoveryDomain(fqdn="24travel.bg", zone="bg")
    attempted_servers: list[str] = []

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "24travel.bg"
        assert timeout_seconds == 5.0
        attempted_servers.append(server)
        if server == "whois.register.bg":
            return "Query limit exceeded, try again later."
        return '{"code":0,"data":{"results":[{"domain":"24travel.bg","available":true}]}}'

    observation = await check_discovery_domain_whois(domain, whois_lookup=whois_lookup)

    assert attempted_servers == [
        "whois.register.bg",
        WHOSE_DOMAINS_AVAILABILITY_LOOKUP,
    ]
    assert observation.error is None
    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


@pytest.mark.asyncio
async def test_discovery_falls_back_to_no_whois_when_rdap_is_inconclusive():
    domain = DiscoveryDomain(fqdn="countrywestern.no", zone="no")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["no"], ["https://rdap.norid.example/"]]]})
        return httpx.Response(200, json={"ldhName": "countrywestern.no"})

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "countrywestern.no"
        assert server == "whois.norid.no"
        assert timeout_seconds == 5.0
        return "No match for domain \"countrywestern.no\"."

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_parses_rnids_available_phrase_as_available():
    observation = parse_whois_response(
        "Naziv domena slobodan-primer.rs je slobodan!",
        fqdn="slobodan-primer.rs",
        observed_at=datetime(2026, 7, 19, 17, 50, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_parses_does_not_appear_registered_phrase_as_available():
    observation = parse_whois_response(
        "24travel.bg does not appear registered yet",
        fqdn="24travel.bg",
        observed_at=datetime(2026, 7, 20, 9, 20, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_parses_json_availability_as_available():
    observation = parse_whois_response(
        '{"code":0,"data":{"results":[{"domain":"24travel.bg","available":true}]}}',
        fqdn="24travel.bg",
        observed_at=datetime(2026, 7, 20, 9, 20, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_parses_json_availability_as_registered():
    observation = parse_whois_response(
        '{"code":0,"data":{"results":[{"domain":"busy.bg","available":false}]}}',
        fqdn="busy.bg",
        observed_at=datetime(2026, 7, 20, 9, 20, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "registered"
    assert observation.availability_status == "taken"


def test_discovery_parses_register_bg_missing_database_phrase_as_available():
    observation = parse_whois_response(
        "DOMAIN NAME: 24travel.bg does not exist in database!",
        fqdn="24travel.bg",
        observed_at=datetime(2026, 7, 20, 9, 10, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_parses_register_bg_missing_the_database_phrase_as_available():
    observation = parse_whois_response(
        "DOMAIN NAME: 24travel.bg does not exist in the database!",
        fqdn="24travel.bg",
        observed_at=datetime(2026, 7, 20, 9, 10, tzinfo=timezone.utc),
        latency_ms=25,
    )

    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


def test_discovery_treats_query_limit_exceeded_as_rate_limit_not_registered():
    domain = DiscoveryDomain(fqdn="24travel.bg", zone="bg", status="tracking")
    observation = parse_whois_response(
        "Query limit exceeded, try again later.",
        fqdn=domain.fqdn,
        observed_at=datetime(2026, 7, 20, 9, 15, tzinfo=timezone.utc),
        latency_ms=25,
    )

    apply_discovery_observation(domain, observation)

    assert observation.error == WHOIS_RATE_LIMIT_ERROR
    assert observation.lifecycle_stage == "unknown"
    assert observation.availability_status == "taken"
    assert domain.status == "tracking"
    assert domain.last_error == WHOIS_RATE_LIMIT_ERROR


def test_discovery_whois_address_resolution_prefers_ipv4(monkeypatch: pytest.MonkeyPatch):
    def fake_getaddrinfo(host: str, port: int, *args, **kwargs):
        assert host == "whois.rotld.ro"
        assert port == 43
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2a03:5e80:0:4:192:162:16:18", 43, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("192.162.16.18", 43)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    addresses = resolve_whois_addresses("whois.rotld.ro", 43)

    assert addresses[0][0] == socket.AF_INET
    assert addresses[0][4] == ("192.162.16.18", 43)


@pytest.mark.asyncio
async def test_discovery_rdap_check_updates_domain_and_records_observation():
    previous_seen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="example.com", zone="com", last_checked_at=previous_seen)

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(
                200,
                json={"services": [[["com"], ["https://rdap.registry.example/"]]]},
            )
        if str(request.url) == "https://rdap.registry.example/domain/example.com":
            return httpx.Response(200, json={"status": ["pendingDelete"]})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client)

    apply_discovery_observation(domain, observation)

    assert observation.source == "rdap"
    assert observation.http_status == 200
    assert observation.lifecycle_stage == "pending_delete"
    assert domain.status == "pending_delete"
    assert domain.predicted_drop_start_at == previous_seen + timedelta(days=5)


@pytest.mark.asyncio
async def test_discovery_rdap_check_parses_rdap_json_content_type():
    domain = DiscoveryDomain(fqdn="example.com", zone="com")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(
                200,
                json={"services": [[["com"], ["https://rdap.registry.example/"]]]},
            )
        if str(request.url) == "https://rdap.registry.example/domain/example.com":
            return httpx.Response(
                200,
                headers={"content-type": "application/rdap+json"},
                json={"status": ["redemptionPeriod"]},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client)

    assert observation.lifecycle_stage == "redemption"
    assert observation.status_codes == ["redemptionPeriod"]


@pytest.mark.asyncio
async def test_discovery_falls_back_to_whois_when_rdap_bootstrap_has_no_zone():
    domain = DiscoveryDomain(fqdn="example.mx", zone="mx")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "example.mx"
        assert server == "whois.mx"
        assert timeout_seconds == 5.0
        return "Domain Name: EXAMPLE.MX\nDomain Status: pendingDelete\n"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    apply_discovery_observation(domain, observation)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "pending_delete"
    assert domain.status == "pending_delete"
    assert domain.predicted_drop_start_at is None


@pytest.mark.asyncio
async def test_discovery_uses_static_rdap_for_us_domains():
    domain = DiscoveryDomain(fqdn="example.us", zone="us")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        if str(request.url) == "https://rdap.nic.us/domain/example.us":
            return httpx.Response(200, headers={"content-type": "application/rdap+json"}, json={"status": ["pendingDelete"]})
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        raise AssertionError("WHOIS should not be used when static .us RDAP works")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    assert observation.source == "rdap"
    assert observation.lifecycle_stage == "pending_delete"
    assert observation.availability_status == "taken"


@pytest.mark.asyncio
async def test_discovery_does_not_mark_cz_auction_pending_as_available():
    domain = DiscoveryDomain(fqdn="eshopak.cz", zone="cz", status="tracking")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["cz"], ["https://rdap.nic.cz/"]]]})
        if str(request.url) == "https://rdap.nic.cz/domain/eshopak.cz":
            return httpx.Response(
                404,
                headers={"content-type": "application/rdap+json"},
                json={"errorCode": 404, "title": "Auction pending"},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client)

    apply_discovery_observation(domain, observation)

    assert observation.lifecycle_stage == "unknown"
    assert observation.availability_status == "taken"
    assert domain.status == "tracking"
    assert domain.available_first_seen_at is None


@pytest.mark.asyncio
async def test_discovery_marks_nominet_rdap_404_as_available():
    domain = DiscoveryDomain(fqdn="globalexpansioninternational.uk", zone="uk", status="tracking")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["uk"], ["https://rdap.nominet.uk/uk/"]]]})
        if str(request.url) == "https://rdap.nominet.uk/uk/domain/globalexpansioninternational.uk":
            return httpx.Response(
                404,
                headers={"content-type": "application/json;charset=utf-8"},
                json={"errorCode": 404, "title": "Domain globalexpansioninternational.uk not found"},
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client)

    apply_discovery_observation(domain, observation)

    assert observation.source == "rdap"
    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"
    assert domain.status == "available"
    assert domain.available_first_seen_at is not None


@pytest.mark.asyncio
async def test_discovery_falls_back_to_nominet_whois_for_uk_domains():
    domain = DiscoveryDomain(fqdn="globalexpansioninternational.uk", zone="uk", status="tracking")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "globalexpansioninternational.uk"
        assert server == "whois.nic.uk"
        assert timeout_seconds == 5.0
        return 'No match for "globalexpansioninternational.uk".\nThis domain name has not been registered.\n'

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "not_found"
    assert observation.availability_status == "available"


@pytest.mark.asyncio
async def test_discovery_marks_org_pir_dropzone_without_available_status():
    domain = DiscoveryDomain(fqdn="actonfamily.org", zone="org", status="pending_delete")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["org"], ["https://rdap.publicinterestregistry.org/rdap/"]]]})
        if str(request.url) == "https://rdap.publicinterestregistry.org/rdap/domain/actonfamily.org":
            return httpx.Response(404, headers={"content-type": "application/rdap+json"}, json={"errorCode": 404})
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "actonfamily.org"
        assert server == "whois.publicinterestregistry.org"
        assert timeout_seconds == 5.0
        return (
            "This domain is currently available for application via the PIR Dropzone service.\n"
            ">>> Last update of WHOIS database: 2026-07-06T17:10:41Z <<<\n"
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    apply_discovery_observation(domain, observation)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "dropzone"
    assert observation.availability_status == "dropzone"
    assert domain.status == "dropzone"
    assert domain.available_first_seen_at is None


@pytest.mark.asyncio
async def test_discovery_falls_back_to_whois_when_rdap_response_is_invalid_json():
    domain = DiscoveryDomain(fqdn="example.cz", zone="cz")

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["cz"], ["https://rdap.nic.cz/"]]]})
        if str(request.url) == "https://rdap.nic.cz/domain/example.cz":
            return httpx.Response(200, headers={"content-type": "application/rdap+json"}, text="")
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "example.cz"
        assert server == "whois.nic.cz"
        assert timeout_seconds == 5.0
        return "domain: example.cz\nstatus: pendingDelete\n"

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        observation = await check_discovery_domain_rdap(domain, client=client, whois_lookup=whois_lookup)

    assert observation.source == "whois_fallback"
    assert observation.lifecycle_stage == "pending_delete"
    assert observation.availability_status == "taken"


@pytest.mark.asyncio
async def test_process_due_discovery_domains_uses_whois_when_bootstrap_fails():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(DiscoveryDomain(fqdn="example.com", zone="com", next_check_at=now))
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            raise httpx.ConnectError("bootstrap unavailable", request=request)
        return httpx.Response(404)

    async def whois_lookup(fqdn: str, server: str, timeout_seconds: float) -> str:
        assert fqdn == "example.com"
        assert server == "whois.verisign-grs.com"
        return "Domain Name: EXAMPLE.COM\nDomain Status: pendingDelete\n"

    async with session_factory() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            processed = await process_due_discovery_domains(
                session,
                now=now,
                batch_size=5,
                client=client,
                whois_lookup=whois_lookup,
            )
        await session.commit()

    async with session_factory() as session:
        domain = await session.get(DiscoveryDomain, 1)

    assert processed == 1
    assert domain is not None
    assert domain.status == "pending_delete"
    assert domain.last_error is None

    await engine.dispose()


def test_pending_delete_observation_creates_drop_range():
    previous_seen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 6, 1, 12, 15, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="sample.com", zone="com", last_checked_at=previous_seen)

    apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=200,
            lifecycle_stage="pending_delete",
            status_codes=["pendingDelete"],
            raw_response='{"status":["pendingDelete"]}',
        ),
    )

    assert domain.status == "pending_delete"
    assert domain.first_seen_pending_delete_at == observed_at
    assert domain.pending_delete_previous_seen_at == previous_seen
    assert domain.predicted_drop_start_at == previous_seen + timedelta(days=5)
    assert domain.predicted_drop_end_at == observed_at + timedelta(days=5)


def test_pending_delete_observation_can_skip_drop_prediction():
    previous_seen = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    observed_at = datetime(2026, 6, 1, 12, 15, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        last_checked_at=previous_seen,
        drop_prediction_enabled=False,
    )

    apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=200,
            lifecycle_stage="pending_delete",
            status_codes=["pendingDelete"],
            raw_response='{"status":["pendingDelete"]}',
        ),
    )

    assert domain.status == "pending_delete"
    assert domain.first_seen_pending_delete_at == observed_at
    assert domain.pending_delete_previous_seen_at == previous_seen
    assert domain.predicted_pending_delete_at is None
    assert domain.predicted_drop_start_at is None
    assert domain.predicted_drop_end_at is None


def test_pending_delete_notification_allows_missing_drop_prediction():
    observed_at = datetime(2026, 7, 6, 13, 37, 9, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="example.pl",
        zone="pl",
        status="pending_delete",
        first_seen_pending_delete_at=observed_at,
    )

    message = _build_transition_notification(
        domain,
        previous_status="tracking",
        previous_pending_at=None,
        previous_available_at=None,
    )

    assert message is not None
    assert "Discovery pendingDelete" in message
    assert "Predicted drop: unknown" in message


def test_dropzone_notification_is_separate_from_available():
    domain = DiscoveryDomain(fqdn="actonfamily.org", zone="org", status="dropzone")

    message = _build_transition_notification(
        domain,
        previous_status="pending_delete",
        previous_pending_at=datetime(2026, 7, 6, 16, 11, 30, tzinfo=timezone.utc),
        previous_available_at=None,
    )

    assert message is not None
    assert "Discovery dropzone" in message
    assert "normal available" in message


def test_redemption_observation_predicts_pending_delete_and_drop_from_rdap_updated_at():
    observed_at = datetime(2026, 7, 4, 12, 35, 37, tzinfo=timezone.utc)
    updated_at = datetime(2026, 7, 3, 7, 55, 16, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="greenhousepost.net", zone="net")

    apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=200,
            lifecycle_stage="redemption",
            availability_status="taken",
            status_codes=["redemptionPeriod"],
            rdap_updated_at=updated_at,
        ),
    )

    assert domain.status == "redemption"
    assert domain.redemption_anchor_at == updated_at
    assert domain.redemption_anchor_source == "rdap_updated_at"
    assert domain.predicted_pending_delete_at == updated_at + timedelta(days=30)
    assert domain.predicted_drop_start_at == updated_at + timedelta(days=35)
    assert domain.predicted_drop_end_at == updated_at + timedelta(days=35)


def test_redemption_observation_can_skip_drop_prediction():
    observed_at = datetime(2026, 7, 4, 12, 35, 37, tzinfo=timezone.utc)
    updated_at = datetime(2026, 7, 3, 7, 55, 16, tzinfo=timezone.utc)
    domain = DiscoveryDomain(fqdn="greenhousepost.net", zone="net", drop_prediction_enabled=False)

    apply_discovery_observation(
        domain,
        DiscoveryObservationInput(
            source="rdap",
            observed_at=observed_at,
            http_status=200,
            lifecycle_stage="redemption",
            availability_status="taken",
            status_codes=["redemptionPeriod"],
            rdap_updated_at=updated_at,
        ),
    )

    assert domain.status == "redemption"
    assert domain.redemption_anchor_at == updated_at
    assert domain.redemption_anchor_source == "rdap_updated_at"
    assert domain.predicted_pending_delete_at is None
    assert domain.predicted_drop_start_at is None
    assert domain.predicted_drop_end_at is None


def test_discovery_uses_ten_second_interval_on_predicted_drop_day():
    now = datetime(2026, 6, 6, 10, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        status="pending_delete",
        predicted_drop_start_at=datetime(2026, 6, 6, 0, 0, tzinfo=timezone.utc),
        predicted_drop_end_at=datetime(2026, 6, 6, 23, 59, tzinfo=timezone.utc),
    )

    next_check = calculate_next_check_at(domain, now)

    assert next_check is not None
    assert timedelta(seconds=10) <= next_check - now <= timedelta(seconds=20)


def test_discovery_uses_fifteen_minute_interval_for_redemption_domains():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        status="redemption",
        last_lifecycle_stage="redemption",
    )

    next_check = calculate_next_check_at(domain, now)

    assert next_check is not None
    assert timedelta(minutes=15) <= next_check - now <= timedelta(minutes=15, seconds=10)


def test_discovery_retries_error_domains_quickly():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    domain = DiscoveryDomain(
        fqdn="sample.com",
        zone="com",
        status="error",
        last_error="temporary rdap timeout",
    )

    retry_at = calculate_next_check_at(domain, now)

    assert retry_at is not None
    assert timedelta(minutes=3) <= retry_at - now <= timedelta(minutes=4)


def test_discovery_spreads_error_retry_with_stable_jitter():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    first = DiscoveryDomain(fqdn="alpha-example.com", zone="com", status="error")
    second = DiscoveryDomain(fqdn="beta-example.com", zone="com", status="error")

    first_delay = calculate_next_check_at(first, now) - now
    second_delay = calculate_next_check_at(second, now) - now

    assert timedelta(minutes=3) <= first_delay <= timedelta(minutes=4)
    assert timedelta(minutes=3) <= second_delay <= timedelta(minutes=4)
    assert first_delay != second_delay


def test_discovery_spreads_active_next_checks_with_short_jitter():
    now = datetime(2026, 6, 1, 10, 0, tzinfo=timezone.utc)
    first = DiscoveryDomain(fqdn="alpha-example.com", zone="com", status="pending_delete")
    second = DiscoveryDomain(fqdn="beta-example.com", zone="com", status="pending_delete")

    first_delay = calculate_next_check_at(first, now) - now
    second_delay = calculate_next_check_at(second, now) - now

    assert timedelta(minutes=5) <= first_delay <= timedelta(minutes=5, seconds=10)
    assert timedelta(minutes=5) <= second_delay <= timedelta(minutes=5, seconds=10)
    assert first_delay != second_delay


def test_discovery_initial_import_spreads_over_five_minutes():
    now = datetime(2026, 7, 7, 10, 0, tzinfo=timezone.utc)
    total = 10

    checks = [stagger_initial_check_at(now, index=index, total=total) for index in range(total)]

    assert checks[0] == now
    assert checks[-1] < now + timedelta(minutes=5)
    assert timedelta(seconds=25) <= checks[1] - checks[0] <= timedelta(seconds=35)


@pytest.mark.asyncio
async def test_discovery_api_imports_and_lists_domains():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/control/discovery/domains/import",
            json={
                "domains": ["Example.COM", "example.com", "test.org"],
                "drop_prediction_enabled": False,
                "notes": "seed",
            },
        )
        assert response.status_code == 201
        assert response.json()["skipped"] == ["example.com"]

        list_response = await client.get("/control/discovery/domains")
        assert list_response.status_code == 200
        payload = list_response.json()
        assert [item["fqdn"] for item in payload] == ["example.com", "test.org"]
        assert payload[0]["zone"] == "com"
        assert payload[0]["status"] == "tracking"

        async with session_factory() as session:
            strategy = ZoneStrategy(
                zone="com",
                name="COM window",
                timezone_name="UTC",
                is_active=True,
            )
            session.add(strategy)
            await session.flush()
            session.add(
                ZoneRule(
                    zone_strategy_id=strategy.id,
                    name="COM daily",
                    schedule_type="daily",
                    hour=18,
                    minute=0,
                    second=0,
                    window_duration_seconds=120,
                    is_enabled=True,
                )
            )
            await session.commit()

        stats_response = await client.get("/control/discovery/zone-stats")
        assert stats_response.status_code == 200
        stats_by_zone = {item["zone"]: item for item in stats_response.json()}
        assert stats_by_zone["com"]["has_strategy_pattern"] is True
        assert stats_by_zone["org"]["has_strategy_pattern"] is False
        assert payload[0]["drop_prediction_enabled"] is False
        assert payload[1]["drop_prediction_enabled"] is False
        first_check = datetime.fromisoformat(payload[0]["next_check_at"])
        second_check = datetime.fromisoformat(payload[1]["next_check_at"])
        assert 100 <= abs((second_check - first_check).total_seconds()) <= 200

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_api_bulk_updates_check_interval_and_reschedules_active_domains():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        import_response = await client.post(
            "/control/discovery/domains/import",
            json={"domains": ["alpha.com", "beta.com", "gamma.com"], "check_interval_seconds": 21600},
        )
        assert import_response.status_code == 201
        inserted = import_response.json()["inserted"]
        selected_ids = [inserted[0]["id"], inserted[1]["id"]]

        update_response = await client.patch(
            "/control/discovery/domains/interval",
            json={
                "domain_ids": selected_ids,
                "check_interval_seconds": 120,
                "reschedule_pending": True,
            },
        )
        assert update_response.status_code == 200
        assert update_response.json() == {"updated": 2, "check_interval_seconds": 120}

        list_response = await client.get("/control/discovery/domains")
        assert list_response.status_code == 200
        domains = {item["fqdn"]: item for item in list_response.json()}

    assert domains["alpha.com"]["check_interval_seconds"] == 120
    assert domains["beta.com"]["check_interval_seconds"] == 120
    assert domains["gamma.com"]["check_interval_seconds"] == 21600
    assert domains["alpha.com"]["next_check_at"] != domains["beta.com"]["next_check_at"]

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_trim_keeps_change_observations_beyond_recent_attempts():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    base_time = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        domain = DiscoveryDomain(fqdn="changed.example", zone="com")
        session.add(domain)
        await session.flush()
        session.add(
            DiscoveryObservation(
                discovery_domain_id=domain.id,
                source="rdap",
                observed_at=base_time,
                lifecycle_stage="pending_delete",
                availability_status="taken",
                change_detected=True,
                change_summary="status changed",
            )
        )
        for index in range(6):
            session.add(
                DiscoveryObservation(
                    discovery_domain_id=domain.id,
                    source="rdap",
                    observed_at=base_time + timedelta(seconds=index + 1),
                    lifecycle_stage="pending_delete",
                    availability_status="taken",
                    change_detected=False,
                )
            )
        await session.flush()

        await trim_discovery_observations(session, domain.id)
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(
            select(DiscoveryObservation).order_by(DiscoveryObservation.observed_at.asc())
        )
        observations = list(result.scalars().all())

    assert len(observations) == 6
    assert observations[0].change_detected is True
    assert observations[0].change_summary == "status changed"

    await engine.dispose()


@pytest.mark.asyncio
async def test_drop_domain_import_auto_creates_discovery_tracking_with_short_interval():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/control/domains",
            json={
                "domains": ["auto-discovery.com"],
                "zone": "com",
                "drop_date": "2026-07-26",
                "attack_enabled": False,
            },
        )
        assert response.status_code == 201

    async with session_factory() as session:
        discovery_domain = await session.scalar(
            select(DiscoveryDomain).where(DiscoveryDomain.fqdn == "auto-discovery.com")
        )

    assert discovery_domain is not None
    assert discovery_domain.status == "tracking"
    assert discovery_domain.check_interval_seconds == 10
    assert discovery_domain.next_check_at is not None

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_api_keeps_recent_attempts_and_exports_available_csv():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/control/discovery/domains/import",
            json={"domains": ["drop-example.com"], "check_interval_seconds": 10},
        )
        assert response.status_code == 201
        domain_id = response.json()["inserted"][0]["id"]

        stages = [
            ("registered", "taken", None),
            ("registered", "taken", "timeout"),
            ("pending_delete", "taken", None),
            ("pending_delete", "taken", None),
            ("pending_delete", "taken", "temporary RDAP error"),
            ("pending_delete", "taken", None),
            ("not_found", "available", None),
        ]
        for lifecycle_stage, availability_status, error in stages:
            observation_response = await client.post(
                f"/control/discovery/domains/{domain_id}/observations",
                json={
                    "source": "manual",
                    "http_status": 200,
                    "lifecycle_stage": lifecycle_stage,
                    "availability_status": availability_status,
                    "status_codes": [lifecycle_stage],
                    "error": error,
                },
            )
            assert observation_response.status_code == 201

        observations_response = await client.get(f"/control/discovery/domains/{domain_id}/observations")
        assert observations_response.status_code == 200
        observations = observations_response.json()
        assert len(observations) == 6
        assert observations[0]["availability_status"] == "available"
        assert observations[-1]["error"] is None
        assert observations[-1]["change_summary"] == "first seen"

        export_response = await client.get("/control/discovery/domains/available/export.csv?zone=com")
        assert export_response.status_code == 200
        assert "text/csv" in export_response.headers["content-type"]
        csv_body = export_response.text
        assert "fqdn,zone,status,available_first_seen_at" in csv_body
        assert "drop-example.com,com,available," in csv_body
        assert "last_change_at" in csv_body
        assert "last_change_summary" in csv_body
        assert "change_history" in csv_body
        assert "state_history" in csv_body
        assert "attempt_1_observed_at" in csv_body
        assert "attempt_1_change_detected" in csv_body
        assert "attempt_1_change_summary" in csv_body
        assert "first seen" in csv_body
        assert "temporary RDAP error" in csv_body

    await engine.dispose()


@pytest.mark.asyncio
async def test_discovery_observations_endpoint_includes_preserved_status_changes():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=1, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin

    base_time = datetime(2026, 7, 25, 10, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        domain = DiscoveryDomain(fqdn="timeline.example", zone="com")
        session.add(domain)
        await session.flush()
        session.add(
            DiscoveryObservation(
                discovery_domain_id=domain.id,
                source="rdap",
                observed_at=base_time,
                lifecycle_stage="pending_delete",
                availability_status="taken",
                change_detected=True,
                change_summary="first seen",
            )
        )
        for index in range(205):
            session.add(
                DiscoveryObservation(
                    discovery_domain_id=domain.id,
                    source="rdap",
                    observed_at=base_time + timedelta(seconds=index + 1),
                    lifecycle_stage="pending_delete",
                    availability_status="taken",
                    change_detected=False,
                )
            )
        await session.commit()
        domain_id = domain.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(f"/control/discovery/domains/{domain_id}/observations")

    assert response.status_code == 200
    observations = response.json()
    assert len(observations) == 201
    assert any(item["change_summary"] == "first seen" for item in observations)

    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_discovery_domains_persists_observation_and_notification():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        session.add(DiscoveryDomain(fqdn="example.com", zone="com", next_check_at=now))
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        if str(request.url) == "https://rdap.registry.example/domain/example.com":
            return httpx.Response(200, json={"status": ["pendingDelete"]})
        return httpx.Response(404)

    notifications: list[str] = []

    async with session_factory() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            processed = await process_due_discovery_domains(
                session,
                now=now,
                batch_size=5,
                client=client,
                notify=notifications.append,
            )
        await session.commit()

    async with session_factory() as session:
        domain = await session.get(DiscoveryDomain, 1)

    assert processed == 1
    assert domain is not None
    assert domain.status == "pending_delete"
    assert domain.next_check_at is not None
    next_check_delay = domain.next_check_at - now.replace(tzinfo=None)
    assert timedelta(seconds=10) <= next_check_delay <= timedelta(seconds=20)
    assert notifications
    assert "pendingDelete" in notifications[0]

    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_discovery_domains_spreads_batch_within_short_window():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        for index in range(4):
            session.add(
                DiscoveryDomain(
                    fqdn=f"example{index}.com",
                    zone="com",
                    next_check_at=now,
                    check_interval_seconds=10,
                )
            )
        await session.commit()

    async def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        return httpx.Response(200, json={"status": ["pendingDelete"]})

    async with session_factory() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            processed = await process_due_discovery_domains(
                session,
                now=now,
                batch_size=4,
                client=client,
                concurrency=4,
            )
        await session.commit()

    async with session_factory() as session:
        result = await session.execute(select(DiscoveryDomain).order_by(DiscoveryDomain.id.asc()))
        domains = list(result.scalars().all())

    delays = [domain.next_check_at - now.replace(tzinfo=None) for domain in domains]

    assert processed == 4
    assert all(timedelta(seconds=10) <= delay <= timedelta(seconds=20) for delay in delays)
    assert len(set(delays)) > 1

    await engine.dispose()


@pytest.mark.asyncio
async def test_process_due_discovery_domains_checks_domains_concurrently():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    now = datetime(2026, 7, 6, 12, 0, tzinfo=timezone.utc)
    async with session_factory() as session:
        for index in range(4):
            session.add(DiscoveryDomain(fqdn=f"example{index}.com", zone="com", next_check_at=now))
        await session.commit()

    active_requests = 0
    max_active_requests = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active_requests, max_active_requests
        if str(request.url) == "https://data.iana.org/rdap/dns.json":
            return httpx.Response(200, json={"services": [[["com"], ["https://rdap.registry.example/"]]]})
        active_requests += 1
        max_active_requests = max(max_active_requests, active_requests)
        await asyncio.sleep(0.05)
        active_requests -= 1
        return httpx.Response(200, json={"status": ["pendingDelete"]})

    async with session_factory() as session:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            processed = await process_due_discovery_domains(
                session,
                now=now,
                batch_size=4,
                client=client,
                concurrency=4,
            )
        await session.commit()

    assert processed == 4
    assert max_active_requests > 1

    await engine.dispose()
