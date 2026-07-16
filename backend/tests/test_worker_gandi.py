from __future__ import annotations

import importlib.util
import sys
import types
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest


def _load_worker_gandi_module():
    worker_app_dir = Path(__file__).resolve().parents[2] / "worker" / "app"
    saved_modules = {name: sys.modules.get(name) for name in ("app", "app.control_client", "app.gandi")}

    worker_pkg = types.ModuleType("app")
    worker_pkg.__path__ = [str(worker_app_dir)]
    sys.modules["app"] = worker_pkg

    loaded = {}
    try:
        for module_name in ("control_client", "gandi"):
            module_path = worker_app_dir / f"{module_name}.py"
            spec = importlib.util.spec_from_file_location(f"app.{module_name}", module_path)
            assert spec is not None and spec.loader is not None
            module = importlib.util.module_from_spec(spec)
            sys.modules[f"app.{module_name}"] = module
            spec.loader.exec_module(module)
            loaded[module_name] = module
    finally:
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    return loaded["gandi"]


gandi = _load_worker_gandi_module()


def make_task(**overrides):
    base = {
        "task_id": 1,
        "attack_run_id": 1,
        "domain_id": 1,
        "worker_id": 1,
        "fqdn": "alpha.fr",
        "zone": "fr",
        "planned_start_at": datetime.now(timezone.utc),
        "planned_end_at": datetime.now(timezone.utc),
        "planned_rps": 16.0,
        "requested_duration_years": 1,
        "registration_extra_parameters": None,
        "registrar": {
            "id": 1,
            "name": "Gandi main",
            "registrar_slug": "gandi",
            "api_token": "pat_token",
            "sharing_id": "org-123",
            "api_base_url": "https://api.gandi.net/v5/domain/domains",
            "supports_dry_run": True,
        },
        "contact": {
            "id": 1,
            "label": "Default FR",
            "person_type": "individual",
            "given_name": "Alice",
            "family_name": "Doe",
            "organization_name": None,
            "email": "alice@example.org",
            "phone": "+33.123456789",
            "street_address": "5 rue neuve",
            "city": "Paris",
            "state": "FR-IDF",
            "zip_code": "75001",
            "country_code": "FR",
            "mobile": None,
            "fax": None,
            "lang": None,
            "data_obfuscated": None,
            "mail_obfuscated": None,
            "icann_contract_accept": None,
            "extra_parameters": None,
        },
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_registration_request_includes_all_required_contact_roles_and_sharing_id():
    task = make_task()

    url, headers, payload = gandi.build_registration_request(task)

    assert "sharing_id=org-123" in url
    assert headers["Authorization"] == "Bearer pat_token"
    assert payload["fqdn"] == "alpha.fr"
    assert payload["duration"] == 1
    for role_name in ("owner", "admin", "bill", "tech"):
        assert payload[role_name]["given"] == "Alice"
        assert payload[role_name]["family"] == "Doe"
        assert payload[role_name]["email"] == "alice@example.org"
        assert payload[role_name]["country"] == "FR"


def test_build_registration_request_supports_domain_and_contact_extra_parameters():
    task = make_task(
        registration_extra_parameters='{"fr_lock": true, "legal_type": "individual"}',
        contact={
            "id": 1,
            "label": "Default FR",
            "person_type": "individual",
            "given_name": "Alice",
            "family_name": "Doe",
            "organization_name": None,
            "email": "alice@example.org",
            "phone": "+33.123456789",
            "street_address": "5 rue neuve",
            "city": "Paris",
            "state": "FR-IDF",
            "zip_code": "75001",
            "country_code": "FR",
            "mobile": "+33.987654321",
            "fax": "+33.111111111",
            "lang": "fr",
            "data_obfuscated": True,
            "mail_obfuscated": False,
            "icann_contract_accept": True,
            "extra_parameters": '{"local_presence": "fr"}',
        },
    )

    _, headers, payload = gandi.build_registration_request(task, dry_run=True)

    assert headers["Dry-Run"] == "1"
    assert payload["extra_parameters"] == {"fr_lock": True, "legal_type": "individual"}
    assert payload["owner"]["mobile"] == "+33.987654321"
    assert payload["owner"]["fax"] == "+33.111111111"
    assert payload["owner"]["lang"] == "fr"
    assert payload["owner"]["data_obfuscated"] is True
    assert payload["owner"]["mail_obfuscated"] is False
    assert payload["owner"]["icann_contract_accept"] is True
    assert payload["owner"]["extra_parameters"] == {"local_presence": "fr"}


@pytest.mark.asyncio
async def test_register_domain_polls_createstatus_until_success_redirect():
    task = make_task()
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                202,
                headers={"Location": "https://api.gandi.net/v5/domain/domains/alpha.fr/createstatus"},
                json={"message": "accepted"},
            )
        if len([item for item in calls if item[1].endswith("/createstatus")]) == 1:
            return httpx.Response(200, json={"step": "WAIT", "step_nb": 1})
        return httpx.Response(303, headers={"Location": "https://api.gandi.net/v5/domain/domains/alpha.fr"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status_code, body = await gandi.register_domain(
            task,
            client,
            status_poll_interval_seconds=0.0,
            status_poll_max_attempts=3,
        )

    assert status_code == 200
    assert "registered" in body


@pytest.mark.asyncio
async def test_register_domain_can_skip_createstatus_polling_after_accepted():
    task = make_task()
    calls: list[tuple[str, str]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(
                202,
                headers={"Location": "https://api.gandi.net/v5/domain/domains/alpha.fr/createstatus"},
                json={"message": "accepted"},
            )
        return httpx.Response(500, json={"message": "createstatus should not be polled"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status_code, body = await gandi.register_domain(
            task,
            client,
            poll_create_status=False,
            status_poll_interval_seconds=0.0,
            status_poll_max_attempts=3,
        )

    assert status_code == 202
    assert "accepted" in body
    assert calls == [("POST", "/v5/domain/domains")]


@pytest.mark.asyncio
async def test_register_domain_returns_error_for_createstatus_error_step():
    task = make_task()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                headers={"Location": "https://api.gandi.net/v5/domain/domains/alpha.fr/createstatus"},
                json={"message": "accepted"},
            )
        return httpx.Response(
            200,
            json={"step": "ERROR", "step_nb": 3, "errortype": "validation", "errortype_label": "Missing field"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status_code, body = await gandi.register_domain(
            task,
            client,
            status_poll_interval_seconds=0.0,
            status_poll_max_attempts=2,
        )

    assert status_code == 409
    assert "Missing field" in body


@pytest.mark.asyncio
async def test_register_domain_returns_pending_when_createstatus_never_finishes():
    task = make_task()

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                202,
                headers={"Location": "https://api.gandi.net/v5/domain/domains/alpha.fr/createstatus"},
                json={"message": "accepted"},
            )
        return httpx.Response(200, json={"step": "RUN", "step_nb": 2})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        status_code, body = await gandi.register_domain(
            task,
            client,
            status_poll_interval_seconds=0.0,
            status_poll_max_attempts=2,
        )

    assert status_code == 202
    assert "RUN" in body
