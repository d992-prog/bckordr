from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.core.config import Settings
from app.db.models import RegistrarAccount


@dataclass(slots=True)
class RegistrarValidationResult:
    status: str
    message: str


async def validate_registrar_account_remote(
    account: RegistrarAccount,
    settings: Settings,
) -> RegistrarValidationResult:
    if account.registrar_slug != "gandi":
        return RegistrarValidationResult(
            status="ready",
            message=f"Remote validation is not implemented for {account.registrar_slug}; local validation passed",
        )

    if not account.api_token:
        return RegistrarValidationResult(status="invalid", message="Missing api token")

    query: dict[str, str] = {"page": "1", "per_page": "1"}
    if account.sharing_id:
        query["sharing_id"] = account.sharing_id
    base_url = (account.api_base_url or "https://api.gandi.net/v5/domain/domains").rstrip("/")
    url = f"{base_url}?{urlencode(query)}"
    headers = {"Authorization": f"Bearer {account.api_token}"}

    timeout = httpx.Timeout(settings.request_timeout, connect=settings.request_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:
        return RegistrarValidationResult(status="error", message=f"Remote validation failed: {exc}")

    if response.status_code in {200, 202}:
        return RegistrarValidationResult(
            status="ready",
            message="Remote auth validation passed against Gandi API; use per-domain dry-run for create validation",
        )
    if response.status_code in {401, 403}:
        return RegistrarValidationResult(status="invalid", message=f"Gandi rejected credentials with HTTP {response.status_code}")
    return RegistrarValidationResult(
        status="error",
        message=f"Unexpected validation response from Gandi: HTTP {response.status_code}",
    )
