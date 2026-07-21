from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

import httpx
from sqlalchemy import select


REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
BACKEND_ENV = BACKEND_DIR / ".env"

if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_dotenv(BACKEND_ENV)

from app.db.models import ContactProfile, DropDomain, RegistrarAccount  # noqa: E402
from app.db.session import AsyncSessionLocal  # noqa: E402
from app.services.gandi_dry_run import build_gandi_dry_run_request  # noqa: E402


def _redact_headers(headers: dict[str, str]) -> dict[str, str]:
    redacted = dict(headers)
    if redacted.get("Authorization"):
        redacted["Authorization"] = "Bearer <redacted>"
    return redacted


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    content_type = response.headers.get("content-type", "")
    body = response.text
    payload: dict[str, Any] = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
        "body": body,
    }
    if "application/json" in content_type.lower():
        try:
            payload["json"] = response.json()
        except json.JSONDecodeError:
            payload["json_error"] = "response body is not valid JSON"
    return payload


def _append_query(url: str, params: dict[str, str]) -> str:
    if not params:
        return url
    separator = "&" if "?" in url else "?"
    from urllib.parse import urlencode

    return f"{url}{separator}{urlencode(params)}"


def _create_status_url(account: RegistrarAccount, fqdn: str) -> str:
    base_url = (account.api_base_url or "https://api.gandi.net/v5/domain/domains").rstrip("/")
    url = f"{base_url}/{fqdn}/createstatus"
    query: dict[str, str] = {}
    if account.sharing_id:
        query["sharing_id"] = account.sharing_id
    return _append_query(url, query)


async def _load_domain_bundle(fqdn: str) -> tuple[DropDomain, RegistrarAccount, ContactProfile]:
    async with AsyncSessionLocal() as session:
        domain = (
            await session.execute(select(DropDomain).where(DropDomain.fqdn == fqdn.lower()))
        ).scalar_one_or_none()
        if domain is None:
            raise RuntimeError(f"Domain not found in drop_domains: {fqdn}")
        if domain.registrar_account_id is None:
            raise RuntimeError(f"Domain has no registrar account: {fqdn}")
        if domain.contact_profile_id is None:
            raise RuntimeError(f"Domain has no contact profile: {fqdn}")

        account = await session.get(RegistrarAccount, domain.registrar_account_id)
        contact = await session.get(ContactProfile, domain.contact_profile_id)
        if account is None:
            raise RuntimeError(f"Registrar account not found: {domain.registrar_account_id}")
        if contact is None:
            raise RuntimeError(f"Contact profile not found: {domain.contact_profile_id}")

        # Detach loaded objects before the session closes; the builder only reads scalar fields.
        session.expunge(domain)
        session.expunge(account)
        session.expunge(contact)
        return domain, account, contact


async def run_probe(args: argparse.Namespace) -> int:
    if not args.confirm_live_create:
        raise RuntimeError("Refusing to send a live create request without --confirm-live-create")

    domain, account, contact = await _load_domain_bundle(args.fqdn)
    url, headers, payload = build_gandi_dry_run_request(domain, account, contact)
    headers.pop("Dry-Run", None)

    started_at = datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "fqdn": domain.fqdn,
        "started_at": started_at.isoformat(),
        "registrar_account_id": account.id,
        "registrar_account_name": account.name,
        "contact_profile_id": contact.id,
        "contact_profile_label": contact.label,
        "request": {
            "method": "POST",
            "url": url,
            "headers": _redact_headers(headers),
            "payload": payload,
        },
        "response": None,
        "polls": [],
    }

    timeout = httpx.Timeout(args.timeout, connect=args.connect_timeout)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        response = await client.post(url, headers=headers, json=payload)
        report["response"] = _response_payload(response)

        if args.poll_status and response.status_code == 202:
            poll_url = response.headers.get("Location") or _create_status_url(account, domain.fqdn)
            poll_headers = {"Authorization": headers["Authorization"]}
            for attempt in range(1, args.poll_attempts + 1):
                if attempt > 1:
                    await asyncio.sleep(args.poll_interval)
                poll_response = await client.get(poll_url, headers=poll_headers, follow_redirects=False)
                report["polls"].append(
                    {
                        "attempt": attempt,
                        "url": poll_url,
                        "response": _response_payload(poll_response),
                    }
                )
                if poll_response.status_code == 303:
                    break
                if poll_response.status_code == 200:
                    try:
                        step = str(poll_response.json().get("step") or "").upper()
                    except json.JSONDecodeError:
                        step = ""
                    if step in {"ERROR", "SUPPORT"}:
                        break
                elif poll_response.status_code != 202:
                    break

    report["finished_at"] = datetime.now(timezone.utc).isoformat()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_fqdn = domain.fqdn.replace(".", "-")
    timestamp = started_at.strftime("%Y%m%dT%H%M%SZ")
    output_path = output_dir / f"gandi-create-probe-{safe_fqdn}-{timestamp}.json"
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    response_report = report["response"] or {}
    print(f"report={output_path}")
    print(f"http_status={response_report.get('status_code')}")
    if response.headers.get("Location"):
        print(f"location={response.headers['Location']}")
    body_preview = str(response_report.get("body") or "").replace("\n", " ")[:500]
    print(f"body_preview={body_preview}")
    if report["polls"]:
        last_poll = report["polls"][-1]["response"]
        print(f"last_poll_status={last_poll.get('status_code')}")
        print(f"last_poll_body={str(last_poll.get('body') or '').replace(chr(10), ' ')[:500]}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one live Gandi domain create request and save a sanitized diagnostic report.",
    )
    parser.add_argument("fqdn", help="Domain from drop_domains, for example energiost.se")
    parser.add_argument(
        "--confirm-live-create",
        action="store_true",
        help="Required. The request is live and may register the domain/spend balance if accepted.",
    )
    parser.add_argument("--poll-status", action="store_true", help="Poll createstatus when Gandi returns HTTP 202.")
    parser.add_argument("--poll-attempts", type=int, default=8)
    parser.add_argument("--poll-interval", type=float, default=0.5)
    parser.add_argument("--timeout", type=float, default=12.0)
    parser.add_argument("--connect-timeout", type=float, default=3.0)
    parser.add_argument("--output-dir", default="gandi-probes")
    return parser.parse_args()


def main() -> int:
    return asyncio.run(run_probe(parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
