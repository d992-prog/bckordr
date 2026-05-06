from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from app.core.config import Settings
from app.db.models import RegistrarAccount


def _gandi_api_root(api_base_url: str | None) -> str:
    base_url = (api_base_url or "https://api.gandi.net/v5/domain/domains").rstrip("/")
    split = urlsplit(base_url)
    path = split.path
    marker = "/v5/"
    index = path.find(marker)
    if index == -1:
        return urlunsplit((split.scheme, split.netloc, "/v5", "", ""))
    return urlunsplit((split.scheme, split.netloc, path[: index + 3], "", ""))


def _split_name(value: str | None) -> tuple[str, str]:
    if not value:
        return "", ""
    parts = value.strip().split()
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _string(value) -> str:
    return str(value).strip() if value is not None else ""


def _choose_person_type(org_payload: dict | None) -> str:
    org_type = _string((org_payload or {}).get("type")).lower()
    if org_type in {"company", "association", "publicbody"}:
        return org_type
    if (org_payload or {}).get("corporate") is True:
        return "company"
    return "individual"


def _build_prefill_payload(
    *,
    account: RegistrarAccount,
    user_info: dict,
    organization_info: dict | None,
) -> dict:
    source = organization_info or user_info
    given_name = _string(source.get("firstname") or source.get("given"))
    family_name = _string(source.get("lastname") or source.get("family"))
    if not given_name and not family_name:
        given_name, family_name = _split_name(_string(source.get("name")))

    label_name = " ".join(part for part in [given_name, family_name] if part).strip() or account.name
    person_type = _choose_person_type(organization_info)

    return {
        "label": f"Gandi import | {label_name}",
        "person_type": person_type,
        "given_name": given_name,
        "family_name": family_name,
        "organization_name": _string(source.get("orgname") or source.get("companyname") or source.get("name")) or None,
        "email": _string(source.get("email")),
        "phone": _string(source.get("phone")),
        "mobile": _string(source.get("mobile")) or None,
        "fax": _string(source.get("fax")) or None,
        "lang": _string(source.get("lang")) or None,
        "street_address": _string(source.get("streetaddr") or source.get("street_address")),
        "city": _string(source.get("city")),
        "state": _string(source.get("state")) or None,
        "zip_code": _string(source.get("zip")),
        "country_code": _string(source.get("country")) or "FR",
        "data_obfuscated": source.get("data_obfuscated"),
        "mail_obfuscated": source.get("mail_obfuscated"),
        "icann_contract_accept": source.get("icann_contract_accept"),
        "extra_parameters": None,
        "is_default": False,
        "notes": f"Imported from Gandi API ({_gandi_api_root(account.api_base_url)})",
    }


async def build_gandi_contact_prefill(
    account: RegistrarAccount,
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    if not account.api_token:
        raise ValueError("Missing api token")

    api_root = _gandi_api_root(account.api_base_url)
    headers = {"Authorization": f"Bearer {account.api_token}"}
    timeout = httpx.Timeout(settings.request_timeout, connect=settings.request_timeout)

    async with httpx.AsyncClient(timeout=timeout, transport=transport, follow_redirects=True) as client:
        user_info_response = await client.get(f"{api_root}/organization/user-info", headers=headers)
        user_info_response.raise_for_status()
        user_info = user_info_response.json()

        organization_info: dict | None = None
        if account.sharing_id:
            organization_response = await client.get(
                f"{api_root}/organization/organizations/{account.sharing_id}",
                headers=headers,
            )
            if organization_response.status_code == 200:
                organization_info = organization_response.json()

    return _build_prefill_payload(account=account, user_info=user_info, organization_info=organization_info)
