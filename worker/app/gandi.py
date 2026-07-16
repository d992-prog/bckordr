from __future__ import annotations

import asyncio
import json
from urllib.parse import urlencode

import httpx

from app.control_client import ControlTask

GANDI_BASE_URL = "https://api.gandi.net/v5/domain/domains"


def _append_query(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}{urlencode(params)}"


def build_contact_payload(contact: dict) -> dict:
    payload = {
        "given": contact["given_name"],
        "family": contact["family_name"],
        "email": contact["email"],
        "phone": contact["phone"],
        "streetaddr": contact["street_address"],
        "city": contact["city"],
        "zip": contact["zip_code"],
        "country": contact["country_code"],
        "type": contact["person_type"],
    }
    if contact.get("organization_name"):
        payload["orgname"] = contact["organization_name"]
    if contact.get("state"):
        payload["state"] = contact["state"]
    if contact.get("mobile"):
        payload["mobile"] = contact["mobile"]
    if contact.get("fax"):
        payload["fax"] = contact["fax"]
    if contact.get("lang"):
        payload["lang"] = contact["lang"]
    if contact.get("data_obfuscated") is not None:
        payload["data_obfuscated"] = contact["data_obfuscated"]
    if contact.get("mail_obfuscated") is not None:
        payload["mail_obfuscated"] = contact["mail_obfuscated"]
    if contact.get("icann_contract_accept") is not None:
        payload["icann_contract_accept"] = contact["icann_contract_accept"]
    if contact.get("extra_parameters"):
        payload["extra_parameters"] = _coerce_extra_parameters(contact["extra_parameters"], field_name="contact.extra_parameters")
    return payload


def _coerce_extra_parameters(value, *, field_name: str) -> dict | list:
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


def build_registration_request(task: ControlTask, *, dry_run: bool = False) -> tuple[str, dict, dict]:
    registrar = task.registrar
    if not registrar.get("api_token"):
        raise ValueError("Registrar API token is missing")

    url = registrar.get("api_base_url") or GANDI_BASE_URL
    query: dict[str, str] = {}
    if registrar.get("sharing_id"):
        query["sharing_id"] = registrar["sharing_id"]
    url = _append_query(url, query)

    headers = {
        "Authorization": f"Bearer {registrar['api_token']}",
        "Content-Type": "application/json",
    }
    if dry_run:
        headers["Dry-Run"] = "1"

    contact_payload = build_contact_payload(task.contact)
    payload = {
        "fqdn": task.fqdn,
        "duration": task.requested_duration_years,
        "owner": dict(contact_payload),
        "admin": dict(contact_payload),
        "bill": dict(contact_payload),
        "tech": dict(contact_payload),
    }
    if getattr(task, "registration_extra_parameters", None):
        payload["extra_parameters"] = _coerce_extra_parameters(
            task.registration_extra_parameters,
            field_name="registration_extra_parameters",
        )
    return url, headers, payload


def build_createstatus_url(task: ControlTask) -> str:
    registrar = task.registrar
    base_url = (registrar.get("api_base_url") or GANDI_BASE_URL).rstrip("/")
    url = f"{base_url}/{task.fqdn}/createstatus"
    query: dict[str, str] = {}
    if registrar.get("sharing_id"):
        query["sharing_id"] = registrar["sharing_id"]
    return _append_query(url, query)


async def poll_creation_status(
    task: ControlTask,
    client: httpx.AsyncClient,
    *,
    status_url: str | None,
    headers: dict[str, str],
    status_poll_interval_seconds: float,
    status_poll_max_attempts: int,
) -> tuple[int, str]:
    poll_url = status_url or build_createstatus_url(task)
    max_attempts = max(1, int(status_poll_max_attempts))
    interval_seconds = max(0.0, float(status_poll_interval_seconds))
    last_step = "WAIT"
    last_body = "creation accepted"

    for attempt_index in range(max_attempts):
        response = await client.get(poll_url, headers=headers, follow_redirects=False)
        if response.status_code == 303:
            location = response.headers.get("Location")
            return 200, f"registered via createstatus redirect to {location or 'domain info'}"
        if response.status_code != 200:
            return response.status_code, response.text

        payload = response.json() if "application/json" in (response.headers.get("content-type") or "") else {}
        step = str(payload.get("step") or "WAIT").upper()
        last_step = step
        if step == "ERROR":
            error_label = payload.get("errortype_label") or payload.get("errortype") or response.text or "Gandi create status error"
            return 409, str(error_label)
        if step == "SUPPORT":
            return 409, response.text or "Gandi create status requires support intervention"
        last_body = response.text or f"create status {step}"
        if attempt_index + 1 < max_attempts:
            await asyncio.sleep(interval_seconds)

    return 202, f"creation accepted; latest create status step={last_step}; body={last_body[:500]}"


async def register_domain(
    task: ControlTask,
    client: httpx.AsyncClient,
    *,
    dry_run: bool = False,
    poll_create_status: bool = True,
    status_poll_interval_seconds: float = 0.5,
    status_poll_max_attempts: int = 8,
) -> tuple[int, str]:
    url, headers, payload = build_registration_request(task, dry_run=dry_run)
    response = await client.post(url, json=payload, headers=headers)
    if dry_run or response.status_code != 202:
        return response.status_code, response.text
    if not poll_create_status:
        return 202, response.text or "creation accepted; createstatus polling skipped"
    return await poll_creation_status(
        task,
        client,
        status_url=response.headers.get("Location"),
        headers={"Authorization": headers["Authorization"]},
        status_poll_interval_seconds=status_poll_interval_seconds,
        status_poll_max_attempts=status_poll_max_attempts,
    )
