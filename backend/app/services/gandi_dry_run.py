from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import ContactProfile, DropDomain, RegistrarAccount

GANDI_BASE_URL = "https://api.gandi.net/v5/domain/domains"


@dataclass(slots=True)
class GandiDryRunResult:
    status: str
    http_status: int | None
    message: str
    checked_at: datetime


def _append_query(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def _coerce_extra_parameters(value: str | dict | list | None, *, field_name: str) -> dict | list | None:
    if value is None or value == "":
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {field_name}: {exc.msg}") from exc
        if not isinstance(parsed, (dict, list)):
            raise ValueError(f"{field_name} must decode to JSON object or array")
        return parsed
    raise ValueError(f"{field_name} must be JSON text or a JSON-compatible object")


def build_gandi_dry_run_request(
    domain: DropDomain,
    account: RegistrarAccount,
    contact: ContactProfile,
) -> tuple[str, dict[str, str], dict]:
    if not account.api_token:
        raise ValueError("Registrar API token is missing")

    url = account.api_base_url or GANDI_BASE_URL
    query: dict[str, str] = {}
    if account.sharing_id:
        query["sharing_id"] = account.sharing_id
    url = _append_query(url, query)

    contact_payload = {
        "given": contact.given_name,
        "family": contact.family_name,
        "email": contact.email,
        "phone": contact.phone,
        "streetaddr": contact.street_address,
        "city": contact.city,
        "zip": contact.zip_code,
        "country": contact.country_code,
        "type": contact.person_type,
    }
    if contact.organization_name:
        contact_payload["orgname"] = contact.organization_name
    if contact.state:
        contact_payload["state"] = contact.state
    if contact.mobile:
        contact_payload["mobile"] = contact.mobile
    if contact.fax:
        contact_payload["fax"] = contact.fax
    if contact.lang:
        contact_payload["lang"] = contact.lang
    if contact.data_obfuscated is not None:
        contact_payload["data_obfuscated"] = contact.data_obfuscated
    if contact.mail_obfuscated is not None:
        contact_payload["mail_obfuscated"] = contact.mail_obfuscated
    if contact.icann_contract_accept is not None:
        contact_payload["icann_contract_accept"] = contact.icann_contract_accept
    contact_extra_parameters = _coerce_extra_parameters(contact.extra_parameters, field_name="contact.extra_parameters")
    if contact_extra_parameters is not None:
        contact_payload["extra_parameters"] = contact_extra_parameters

    payload = {
        "fqdn": domain.fqdn,
        "duration": domain.requested_duration_years,
        "owner": dict(contact_payload),
        "admin": dict(contact_payload),
        "bill": dict(contact_payload),
        "tech": dict(contact_payload),
    }
    domain_extra_parameters = _coerce_extra_parameters(
        domain.registration_extra_parameters,
        field_name="domain.registration_extra_parameters",
    )
    if domain_extra_parameters is not None:
        payload["extra_parameters"] = domain_extra_parameters

    headers = {
        "Authorization": f"Bearer {account.api_token}",
        "Content-Type": "application/json",
        "Dry-Run": "1",
    }
    return url, headers, payload


async def run_gandi_domain_dry_run(
    domain: DropDomain,
    account: RegistrarAccount,
    contact: ContactProfile,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GandiDryRunResult:
    checked_at = utcnow()
    try:
        url, headers, payload = build_gandi_dry_run_request(domain, account, contact)
    except ValueError as exc:
        return GandiDryRunResult(status="invalid", http_status=None, message=str(exc), checked_at=checked_at)

    timeout = httpx.Timeout(settings.request_timeout, connect=settings.request_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport) as client:
            response = await client.post(url, json=payload, headers=headers)
    except Exception as exc:
        return GandiDryRunResult(status="error", http_status=None, message=f"Dry-run request failed: {exc}", checked_at=checked_at)

    if response.status_code in {200, 201, 202}:
        return GandiDryRunResult(status="ready", http_status=response.status_code, message=response.text, checked_at=checked_at)
    if response.status_code in {400, 401, 403, 409, 422}:
        return GandiDryRunResult(status="invalid", http_status=response.status_code, message=response.text, checked_at=checked_at)
    return GandiDryRunResult(
        status="error",
        http_status=response.status_code,
        message=response.text or f"Unexpected dry-run response: HTTP {response.status_code}",
        checked_at=checked_at,
    )
