from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import require_admin
from app.api.routes import control
from app.api.routes.control import router as control_router
from app.db.base import Base
from app.db.models import AdminAuditLog, WorkerMaintenanceJob, WorkerNode
from app.db.session import get_db
from app.schemas.control import (
    VpnNodeConfirmRequest,
    VpnNodeOnboardingResponse,
    VpnNodeTestProfileResponse,
    VpnNodeTrustRequest,
)
from app.services.vpn_node_onboarding import (
    VpnNodeOnboardingError,
    VpnNodeOnboardingView,
)
from app.services.vpn_node_trust import VpnNodeTrustError, VpnNodeTrustProbe

FINGERPRINT = "SHA256:" + "A" * 43
HOST_KEY = "ssh-ed25519 " + "A" * 44


def _worker(**changes) -> WorkerNode:
    values = {
        "name": "polish-server",
        "status": "ready",
        "is_enabled": True,
        "vpn_role": "vpn_node",
        "vpn_enabled": True,
        "vpn_runtime_status": "not_installed",
        "ssh_host": "192.0.2.44",
        "ssh_port": 22,
        "ssh_username": "root",
        "ssh_password": "test-only-password",
    }
    values.update(changes)
    return WorkerNode(**values)


@pytest_asyncio.fixture
async def api_env():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    session_factory = async_sessionmaker(
        engine, expire_on_commit=False, class_=AsyncSession
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    app = FastAPI()
    app.include_router(control_router)

    async def override_get_db():
        async with session_factory() as session:
            yield session

    async def fake_admin():
        return SimpleNamespace(id=73, role="owner")

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[require_admin] = fake_admin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://testserver",
    ) as client:
        yield client, session_factory, app
    await engine.dispose()


async def _add_worker(session_factory, **changes) -> int:
    async with session_factory() as session:
        worker = _worker(**changes)
        session.add(worker)
        await session.commit()
        return worker.id


def _settings():
    return SimpleNamespace(
        vpn_control_known_hosts_path="C:/veltrix/known_hosts",
        session_secret_key="s" * 64,
        vpn_friend_beta_release_id="b" * 64,
    )


def test_onboarding_schemas_are_narrow_and_reject_invalid_confirmation():
    request = VpnNodeTrustRequest(fingerprint=FINGERPRINT)
    response = VpnNodeOnboardingResponse(
        worker_id=1,
        state="needs_trust",
        fingerprint=request.fingerprint,
        job_id=None,
        endpoint_id=None,
        error_code=None,
    )
    assert set(response.model_dump()) == {
        "worker_id",
        "state",
        "fingerprint",
        "job_id",
        "endpoint_id",
        "error_code",
    }
    assert set(VpnNodeTestProfileResponse(config_uri="vless://public").model_dump()) == {
        "config_uri"
    }
    for payload in (
        {"fingerprint": "SHA256:short"},
        {"fingerprint": FINGERPRINT, "ssh_password": "must-not-be-accepted"},
    ):
        with pytest.raises(ValidationError):
            VpnNodeTrustRequest(**payload)
    with pytest.raises(ValidationError):
        VpnNodeConfirmRequest(confirmed=False)


@pytest.mark.asyncio
async def test_all_onboarding_routes_require_an_admin(api_env):
    client, _session_factory, app = api_env
    app.dependency_overrides.pop(require_admin)
    requests = (
        ("POST", "/control/workers/1/vpn-onboarding/preflight", None),
        ("POST", "/control/workers/1/vpn-onboarding/trust", {"fingerprint": FINGERPRINT}),
        ("POST", "/control/workers/1/vpn-onboarding/start", None),
        ("GET", "/control/workers/1/vpn-onboarding", None),
        ("POST", "/control/workers/1/vpn-onboarding/test-profile", None),
        ("POST", "/control/workers/1/vpn-onboarding/confirm", {"confirmed": True}),
    )
    for method, path, body in requests:
        response = await client.request(method, path, json=body)
        assert response.status_code == 401

    async def reject_non_admin():
        raise HTTPException(status_code=403, detail="Admin access required")

    app.dependency_overrides[require_admin] = reject_non_admin
    for method, path, body in requests:
        response = await client.request(method, path, json=body)
        assert response.status_code == 403


@pytest.mark.asyncio
async def test_preflight_is_read_only_and_returns_only_public_identity(api_env, monkeypatch):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(session_factory)
    calls = []

    async def fake_probe(worker):
        calls.append(worker.id)
        return VpnNodeTrustProbe(
            known_hosts_line=HOST_KEY,
            fingerprint=FINGERPRINT,
            architecture="x86_64",
            xui_installed=False,
            occupied_ports=(),
        )

    monkeypatch.setattr(control, "probe_vpn_node_trust", fake_probe)
    response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/preflight"
    )

    assert response.status_code == 200
    assert response.json() == {
        "worker_id": worker_id,
        "state": "needs_trust",
        "fingerprint": FINGERPRINT,
        "job_id": None,
        "endpoint_id": None,
        "error_code": None,
    }
    assert calls == [worker_id]
    async with session_factory() as session:
        worker = await session.get(WorkerNode, worker_id)
        assert worker is not None
        assert worker.ssh_host_key is None
        assert worker.ssh_host_key_fingerprint is None
        assert await session.scalar(select(AdminAuditLog.id)) is None


@pytest.mark.parametrize(
    "changes",
    (
        {"archived_at": datetime.now(UTC)},
        {"vpn_enabled": False, "vpn_role": "none"},
        {"ssh_password": None, "ssh_key_path": None},
    ),
)
@pytest.mark.asyncio
async def test_preflight_rejects_unusable_workers_without_transport(
    api_env, monkeypatch, changes
):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(session_factory, **changes)

    async def forbidden_probe(_worker):
        pytest.fail("invalid worker must be rejected before SSH")

    monkeypatch.setattr(control, "probe_vpn_node_trust", forbidden_probe)
    response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/preflight"
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "vpn_onboarding_worker_unavailable"}


@pytest.mark.asyncio
async def test_trust_reprobes_pins_public_identity_and_audits(api_env, monkeypatch):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(session_factory)
    captured = {}

    async def fake_trust(worker, expected_fingerprint, **kwargs):
        captured.update(
            worker_id=worker.id,
            fingerprint=expected_fingerprint,
            known_hosts_path=str(kwargs["known_hosts_path"]),
        )
        return VpnNodeTrustProbe(
            known_hosts_line=HOST_KEY,
            fingerprint=FINGERPRINT,
            architecture="x86_64",
            xui_installed=True,
            occupied_ports=(2053,),
        )

    monkeypatch.setattr(control, "trust_vpn_node", fake_trust)
    monkeypatch.setattr(control, "get_settings", _settings)
    response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/trust",
        json={"fingerprint": FINGERPRINT},
    )

    assert response.status_code == 200
    assert response.json()["state"] == "ready_to_start"
    assert response.json()["fingerprint"] == FINGERPRINT
    assert captured == {
        "worker_id": worker_id,
        "fingerprint": FINGERPRINT,
        "known_hosts_path": "C:\\veltrix\\known_hosts",
    }
    async with session_factory() as session:
        worker = await session.get(WorkerNode, worker_id)
        audit = await session.scalar(select(AdminAuditLog))
        assert worker is not None and worker.ssh_host_key == HOST_KEY
        assert worker.ssh_host_key_fingerprint == FINGERPRINT
        assert audit is not None
        assert (audit.actor_user_id, audit.action, audit.details) == (
            73,
            "vpn_node_onboarding_trust",
            f"worker_id={worker_id}",
        )


@pytest.mark.asyncio
async def test_invalid_fingerprint_is_422_and_transport_errors_are_safe_503(
    api_env, monkeypatch
):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(session_factory)
    invalid = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/trust",
        json={"fingerprint": "SHA256:not-valid"},
    )
    assert invalid.status_code == 422

    async def fail_probe(_worker):
        raise VpnNodeTrustError("vpn_node_trust_connection_failed")

    monkeypatch.setattr(control, "probe_vpn_node_trust", fail_probe)
    failed = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/preflight"
    )
    assert failed.status_code == 503
    assert failed.json() == {"detail": "vpn_node_trust_connection_failed"}
    assert "test-only-password" not in failed.text

    async def fail_trust(*_args, **_kwargs):
        raise VpnNodeTrustError("vpn_node_trust_host_key_mismatch")

    monkeypatch.setattr(control, "trust_vpn_node", fail_trust)
    mismatch = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/trust",
        json={"fingerprint": FINGERPRINT},
    )
    assert mismatch.status_code == 409
    assert mismatch.json() == {"detail": "vpn_node_trust_host_key_mismatch"}


@pytest.mark.asyncio
async def test_start_is_idempotent_and_dispatches_one_background_job(api_env, monkeypatch):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(
        session_factory,
        ssh_host_key=HOST_KEY,
        ssh_host_key_fingerprint=FINGERPRINT,
    )
    dispatched = []

    async def fake_runner(job_id):
        dispatched.append(job_id)

    monkeypatch.setattr(control, "run_worker_maintenance_job", fake_runner)
    first = await client.post(f"/control/workers/{worker_id}/vpn-onboarding/start")
    second = await client.post(f"/control/workers/{worker_id}/vpn-onboarding/start")

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    assert first.json()["state"] == "queued"
    assert dispatched == [first.json()["job_id"]]
    async with session_factory() as session:
        jobs = list((await session.scalars(select(WorkerMaintenanceJob))).all())
        audits = list((await session.scalars(select(AdminAuditLog))).all())
        assert [(job.action, job.status) for job in jobs] == [("vpn_onboard", "queued")]
        assert [(audit.actor_user_id, audit.action) for audit in audits] == [
            (73, "vpn_node_onboarding_start")
        ]


@pytest.mark.asyncio
async def test_start_rejects_attack_control_and_other_job_conflicts(api_env, monkeypatch):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(
        session_factory,
        ssh_host_key=HOST_KEY,
        ssh_host_key_fingerprint=FINGERPRINT,
    )

    async def no_control(_session):
        return set()

    async def attack(_session, _worker):
        return SimpleNamespace(
            eligible=False,
            reasons=("Worker is assigned to an active domain attack",),
        )

    monkeypatch.setattr(control, "active_vpn_control_worker_ids", no_control)
    monkeypatch.setattr(control, "evaluate_vpn_node", attack)
    attack_response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/start"
    )
    assert attack_response.status_code == 409
    assert attack_response.json() == {"detail": "vpn_onboarding_worker_busy"}

    async def no_attack(_session, _worker):
        return SimpleNamespace(eligible=False, reasons=("VPN runtime is not ready",))

    async def active_control(_session):
        return {worker_id}

    monkeypatch.setattr(control, "evaluate_vpn_node", no_attack)
    monkeypatch.setattr(control, "active_vpn_control_worker_ids", active_control)
    control_response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/start"
    )
    assert control_response.status_code == 409
    assert control_response.json() == {"detail": "vpn_onboarding_worker_busy"}

    monkeypatch.setattr(control, "active_vpn_control_worker_ids", no_control)
    async with session_factory() as session:
        session.add(
            WorkerMaintenanceJob(worker_id=worker_id, action="vpn_update", status="running")
        )
        await session.commit()
    job_response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/start"
    )
    assert job_response.status_code == 409
    assert job_response.json() == {"detail": "vpn_onboarding_worker_busy"}


@pytest.mark.asyncio
async def test_status_never_exposes_job_log_or_unknown_error(api_env):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(
        session_factory,
        ssh_host_key=HOST_KEY,
        ssh_host_key_fingerprint=FINGERPRINT,
    )
    async with session_factory() as session:
        session.add(
            WorkerMaintenanceJob(
                worker_id=worker_id,
                action="vpn_onboard",
                status="failed",
                log="ssh_password=leak",
                error_message="raw remote traceback leak",
            )
        )
        await session.commit()

    response = await client.get(f"/control/workers/{worker_id}/vpn-onboarding")
    assert response.status_code == 200
    assert response.json()["state"] == "failed"
    assert response.json()["error_code"] == "vpn_onboarding_health_failed"
    assert "ssh_password" not in response.text
    assert "traceback" not in response.text


@pytest.mark.asyncio
async def test_test_profile_is_no_store_and_confirm_uses_authenticated_admin(
    api_env, monkeypatch
):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(
        session_factory,
        vpn_runtime_status="awaiting_test",
        ssh_host_key=HOST_KEY,
        ssh_host_key_fingerprint=FINGERPRINT,
    )
    monkeypatch.setattr(control, "get_settings", _settings)
    captured = {}

    async def fake_profile(session, value, **kwargs):
        del session, kwargs
        assert value == worker_id
        return "vless://temporary-profile"

    async def fake_confirm(session, value, **kwargs):
        del session
        captured.update(worker_id=value, admin_id=kwargs["admin_id"])
        return SimpleNamespace(id=9)

    async def fake_view(_session, value):
        return VpnNodeOnboardingView(
            worker_id=value,
            state="ready",
            fingerprint=FINGERPRINT,
            job_id=4,
            endpoint_id=9,
            error_code=None,
        )

    monkeypatch.setattr(control, "create_vpn_node_test_profile", fake_profile)
    monkeypatch.setattr(control, "confirm_vpn_node_test_profile", fake_confirm)
    monkeypatch.setattr(control, "get_vpn_node_onboarding_view", fake_view)

    profile = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/test-profile"
    )
    assert profile.status_code == 200
    assert profile.headers["cache-control"] == "no-store"
    assert profile.json() == {"config_uri": "vless://temporary-profile"}

    confirmed = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/confirm",
        json={"confirmed": True, "admin_id": 999},
    )
    assert confirmed.status_code == 422
    confirmed = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/confirm",
        json={"confirmed": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["state"] == "ready"
    assert captured == {"worker_id": worker_id, "admin_id": 73}

    async with session_factory() as session:
        actions = list((await session.scalars(select(AdminAuditLog.action))).all())
        assert actions == [
            "vpn_node_onboarding_test_profile",
            "vpn_node_onboarding_confirm",
        ]


@pytest.mark.asyncio
async def test_onboarding_service_state_failures_are_bounded_conflicts(
    api_env, monkeypatch
):
    client, session_factory, _app = api_env
    worker_id = await _add_worker(
        session_factory,
        ssh_host_key=HOST_KEY,
        ssh_host_key_fingerprint=FINGERPRINT,
    )

    async def fail_profile(*_args, **_kwargs):
        raise VpnNodeOnboardingError("vpn_onboarding_reconciliation_required")

    monkeypatch.setattr(control, "create_vpn_node_test_profile", fail_profile)
    response = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/test-profile"
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "vpn_onboarding_reconciliation_required"}

    async def fail_health(*_args, **_kwargs):
        raise VpnNodeOnboardingError("vpn_node_health_endpoint_missing")

    monkeypatch.setattr(control, "create_vpn_node_test_profile", fail_health)
    unavailable = await client.post(
        f"/control/workers/{worker_id}/vpn-onboarding/test-profile"
    )
    assert unavailable.status_code == 503
    assert unavailable.json() == {"detail": "vpn_node_health_endpoint_missing"}
