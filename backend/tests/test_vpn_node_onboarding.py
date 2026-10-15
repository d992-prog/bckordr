from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import VpnEndpoint, WorkerMaintenanceJob, WorkerNode
from app.services.vpn_node_bootstrap import (
    VpnNodeBootstrapError,
    VpnNodeBootstrapReceipt,
)
from app.services.vpn_node_deployment import NodeDeploymentError
from app.services.vpn_node_health import VpnNodeHealthReceipt
from app.services.vpn_node_onboarding import (
    VpnNodeOnboardingView,
    get_vpn_node_onboarding_view,
    run_vpn_node_onboarding_job,
)
from app.services.vpn_policy import select_public_vpn_endpoint
from app.services.vpn_reality_endpoint_deployment import (
    EndpointInstallerDeploymentError,
)
from app.services.vpn_reality_endpoint_installer import make_endpoint_receipt


@pytest_asyncio.fixture
async def sessions():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield factory
    finally:
        await engine.dispose()


def _worker(**overrides) -> WorkerNode:
    values = {
        "name": "polish-server",
        "status": "ready",
        "is_enabled": True,
        "ssh_host": "192.0.2.16",
        "ssh_username": "root",
        "ssh_password": "secret",
        "vpn_enabled": True,
        "vpn_role": "vpn_node",
        "vpn_runtime_status": "needs_config",
        "vpn_public_host": "192.0.2.16",
    }
    values.update(overrides)
    return WorkerNode(**values)


HOST_KEY = (
    "192.0.2.16 ssh-ed25519 "
    "AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm"
)


async def _job(sessions, **worker_overrides) -> tuple[int, int]:
    async with sessions() as session:
        worker = _worker(
            ssh_host_key=HOST_KEY,
            ssh_host_key_fingerprint="SHA256:" + "A" * 43,
            **worker_overrides,
        )
        session.add(worker)
        await session.flush()
        job = WorkerMaintenanceJob(worker_id=worker.id, action="vpn_onboard", status="queued")
        session.add(job)
        await session.commit()
        return job.id, worker.id


def _install_receipt(request, *, state="staged", inbound_id=27):
    return make_endpoint_receipt(
        state=state,
        worker_id=request.worker_id,
        inbound_id=inbound_id,
        public_host=request.public_host,
        server_name=request.server_name,
        public_key="A" * 43,
        short_id=request.short_id,
    )


def _configure_control(monkeypatch, onboarding, tmp_path, *, known_host=HOST_KEY):
    snapshot = SimpleNamespace(known_hosts=(known_host + "\n").encode("ascii"))
    monkeypatch.setattr(onboarding, "load_transport_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(
        onboarding,
        "get_settings",
        lambda: SimpleNamespace(
            session_secret_key="release-secret-with-enough-entropy",
            vpn_control_known_hosts_path=str(tmp_path / "known_hosts"),
        ),
    )
    return snapshot


def _successful_stages(worker_id: int, calls: list[str] | None = None):
    seen = calls if calls is not None else []

    async def bootstrap(_snapshot, _bundle, *, expected_sha256):
        seen.append("bootstrap")
        return VpnNodeBootstrapReceipt(
            "prepared",
            "http://127.0.0.1:2053/panel/",
            "/etc/x-ui/x-ui.db",
            "v3.8.5",
        )

    async def deploy(_snapshot, _helper, *, expected_sha256):
        seen.append("deploy_node")
        return "installed"

    async def install(_snapshot, _bundle, *, expected_sha256):
        seen.append("install_endpoint_helper")
        return expected_sha256

    async def execute(_snapshot, request, *, controlled_worker_id):
        assert controlled_worker_id == worker_id
        seen.append(request.action)
        return _install_receipt(
            request,
            state="observed" if request.action == "inspect" else "staged",
        )

    async def health(_snapshot, _request, *, now_ms):
        seen.append("health")
        return VpnNodeHealthReceipt("healthy", None, "running")

    return seen, bootstrap, deploy, install, execute, health


async def _run_with_stages(
    sessions,
    job_id,
    stages,
    *,
    bootstrap=None,
    deploy=None,
    install=None,
    execute=None,
    health=None,
):
    _, default_bootstrap, default_deploy, default_install, default_execute, default_health = stages
    await run_vpn_node_onboarding_job(
        job_id,
        session_factory=sessions,
        bootstrap=bootstrap or default_bootstrap,
        deploy_node=deploy or default_deploy,
        install_endpoint_helper=install or default_install,
        execute_endpoint=execute or default_execute,
        health_transport=health or default_health,
    )


def test_onboarding_view_is_a_safe_frozen_contract() -> None:
    view = VpnNodeOnboardingView(
        worker_id=16,
        state="ready_to_start",
        fingerprint="SHA256:" + "A" * 43,
        job_id=None,
        endpoint_id=None,
        error_code=None,
    )

    assert view.worker_id == 16
    assert "host_key" not in repr(view)


@pytest.mark.asyncio
async def test_view_derives_preflight_trust_and_ready_to_start(sessions) -> None:
    async with sessions() as session:
        missing_ssh = _worker(name="missing-ssh", ssh_password=None)
        untrusted = _worker(name="untrusted")
        trusted = _worker(
            name="trusted",
            ssh_host_key="192.0.2.16 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm",
            ssh_host_key_fingerprint="SHA256:" + "A" * 43,
        )
        session.add_all([missing_ssh, untrusted, trusted])
        await session.commit()

        assert (await get_vpn_node_onboarding_view(session, missing_ssh.id)).state == "needs_preflight"
        assert (await get_vpn_node_onboarding_view(session, untrusted.id)).state == "needs_trust"
        view = await get_vpn_node_onboarding_view(session, trusted.id)
        assert view.state == "ready_to_start"
        assert view.fingerprint == trusted.ssh_host_key_fingerprint


@pytest.mark.asyncio
async def test_view_prioritizes_endpoint_then_current_or_failed_job(sessions) -> None:
    async with sessions() as session:
        worker = _worker(
            ssh_host_key="192.0.2.16 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZm",
            ssh_host_key_fingerprint="SHA256:" + "A" * 43,
        )
        session.add(worker)
        await session.flush()
        job = WorkerMaintenanceJob(worker_id=worker.id, action="vpn_onboard", status="queued")
        session.add(job)
        await session.commit()

        queued = await get_vpn_node_onboarding_view(session, worker.id)
        assert (queued.state, queued.job_id) == ("queued", job.id)

        job.status = "running"
        await session.commit()
        assert (await get_vpn_node_onboarding_view(session, worker.id)).state == "running"

        job.status = "failed"
        job.error_message = "vpn_onboarding_port_conflict"
        await session.commit()
        failed = await get_vpn_node_onboarding_view(session, worker.id)
        assert (failed.state, failed.error_code) == (
            "failed",
            "vpn_onboarding_port_conflict",
        )

        endpoint = VpnEndpoint(
            worker_id=worker.id,
            inbound_id=9,
            public_host="192.0.2.16",
            port=443,
            protocol="vless",
            transport="tcp",
            security="reality",
            server_name="gateway.icloud.com",
            public_key="ZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmZmY",
            short_id="aabbccddeeff0011",
            fingerprint="chrome",
            flow="xtls-rprx-vision",
            status="staged",
            health_checked_at=datetime.now(UTC),
        )
        session.add(endpoint)
        await session.commit()
        staged = await get_vpn_node_onboarding_view(session, worker.id)
        assert (staged.state, staged.endpoint_id, staged.error_code) == (
            "awaiting_test",
            endpoint.id,
            None,
        )

        endpoint.status = "ready"
        endpoint.verified_at = datetime.now(UTC)
        await session.commit()
        assert (await get_vpn_node_onboarding_view(session, worker.id)).state == "ready"


@pytest.mark.asyncio
async def test_onboarding_stages_healthy_endpoint_in_exact_order(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    calls: list[str] = []
    snapshot = SimpleNamespace(known_hosts=(HOST_KEY + "\n").encode("ascii"))
    monkeypatch.setattr(onboarding, "load_transport_snapshot", lambda *_: snapshot)
    monkeypatch.setattr(
        onboarding,
        "get_settings",
        lambda: SimpleNamespace(
            session_secret_key="release-secret-with-enough-entropy",
            vpn_control_known_hosts_path=str(tmp_path / "known_hosts"),
        ),
    )

    async def bootstrap(_snapshot, _bundle, *, expected_sha256):
        assert len(expected_sha256) == 64
        calls.append("bootstrap")
        return VpnNodeBootstrapReceipt(
            "prepared",
            "http://127.0.0.1:2053/panel/",
            "/etc/x-ui/x-ui.db",
            "v3.8.5",
        )

    async def deploy(_snapshot, _helper, *, expected_sha256):
        assert len(expected_sha256) == 64
        calls.append("deploy_node")
        return "installed"

    async def install(_snapshot, _bundle, *, expected_sha256):
        calls.append("install_endpoint_helper")
        return expected_sha256

    async def execute(_snapshot, request, *, controlled_worker_id):
        assert controlled_worker_id == worker_id
        assert request.action == "ensure"
        assert request.public_host == "192.0.2.16"
        assert request.server_name == "gateway.icloud.com"
        assert len(request.short_id) == 16
        calls.append("ensure_endpoint")
        return _install_receipt(request)

    original_stage = onboarding.stage_protected_endpoint

    async def stage(*args, **kwargs):
        calls.append("stage_endpoint")
        return await original_stage(*args, **kwargs)

    monkeypatch.setattr(onboarding, "stage_protected_endpoint", stage)

    async def health(_snapshot, request, *, now_ms):
        assert request.worker_id == worker_id
        assert request.checked_at_ms == now_ms
        calls.append("health")
        return VpnNodeHealthReceipt("healthy", None, "running")

    await run_vpn_node_onboarding_job(
        job_id,
        session_factory=sessions,
        bootstrap=bootstrap,
        deploy_node=deploy,
        install_endpoint_helper=install,
        execute_endpoint=execute,
        health_transport=health,
    )

    assert calls == [
        "bootstrap",
        "deploy_node",
        "install_endpoint_helper",
        "ensure_endpoint",
        "stage_endpoint",
        "health",
    ]
    async with sessions() as session:
        worker = await session.get(WorkerNode, worker_id)
        job = await session.get(WorkerMaintenanceJob, job_id)
        endpoint = await session.scalar(
            select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
        )
        assert worker is not None and worker.vpn_runtime_status == "awaiting_test"
        assert worker.vpn_last_checked_at is not None and worker.vpn_last_error is None
        assert job is not None and job.status == "succeeded"
        assert job.log == "vpn_onboarding_awaiting_test"
        assert endpoint is not None
        assert endpoint.status == "staged"
        assert endpoint.max_active_profiles == 20
        assert endpoint.health_checked_at is not None
        assert endpoint.last_error_code is None
        assert await select_public_vpn_endpoint(
            session,
            now=datetime.now(UTC),
            health_max_age_seconds=300,
        ) is None


@pytest.mark.asyncio
async def test_exact_repeat_reuses_endpoint_short_id_and_preserves_custom_capacity(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    first_job, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    requests = []
    stages = _successful_stages(worker_id)
    original_execute = stages[4]

    async def execute(*args, **kwargs):
        requests.append(args[1])
        return await original_execute(*args, **kwargs)

    await _run_with_stages(sessions, first_job, stages, execute=execute)
    async with sessions() as session:
        endpoint = await session.scalar(
            select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
        )
        assert endpoint is not None
        endpoint.max_active_profiles = 99
        second = WorkerMaintenanceJob(
            worker_id=worker_id,
            action="vpn_onboard",
            status="queued",
        )
        session.add(second)
        await session.commit()
        second_job = second.id

    await _run_with_stages(sessions, second_job, stages, execute=execute)

    async with sessions() as session:
        endpoints = list(
            (
                await session.scalars(
                    select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
                )
            ).all()
        )
        assert len(endpoints) == 1
        assert endpoints[0].max_active_profiles == 99
    assert [request.short_id for request in requests] == [
        requests[0].short_id,
        requests[0].short_id,
    ]


@pytest.mark.asyncio
async def test_endpoint_uncertainty_inspects_before_resuming(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)
    actions: list[str] = []

    async def execute(_snapshot, request, *, controlled_worker_id):
        assert controlled_worker_id == worker_id
        actions.append(request.action)
        if request.action == "ensure":
            raise EndpointInstallerDeploymentError(mutation_uncertain=True)
        return _install_receipt(request, state="observed")

    await _run_with_stages(sessions, job_id, stages, execute=execute)

    assert actions == ["ensure", "inspect"]
    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None and job.status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bootstrap_code", "persisted_code"),
    [
        ("vpn_node_bootstrap_port_conflict", "vpn_onboarding_port_conflict"),
        (
            "vpn_node_bootstrap_mutation_uncertain",
            "vpn_node_bootstrap_mutation_uncertain",
        ),
        (
            "vpn_node_bootstrap_reconciliation_required",
            "vpn_onboarding_reconciliation_required",
        ),
        ("vpn_node_bootstrap_foreign_state", "vpn_onboarding_reconciliation_required"),
    ],
)
async def test_bootstrap_failures_persist_only_safe_codes(
    sessions, tmp_path, monkeypatch, bootstrap_code, persisted_code
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)

    async def fail_bootstrap(*_args, **_kwargs):
        raise VpnNodeBootstrapError(bootstrap_code)

    await _run_with_stages(sessions, job_id, stages, bootstrap=fail_bootstrap)

    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        worker = await session.get(WorkerNode, worker_id)
        assert job is not None and job.error_message == persisted_code
        assert job.log is None
        assert worker is not None and worker.vpn_last_error == persisted_code


@pytest.mark.asyncio
async def test_failed_stage_can_restart_same_job_without_duplicate_endpoint(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)

    async def incomplete_deploy(*_args, **_kwargs):
        raise NodeDeploymentError("vpn_node_deployment_incomplete")

    await _run_with_stages(sessions, job_id, stages, deploy=incomplete_deploy)
    async with sessions() as session:
        failed = await session.get(WorkerMaintenanceJob, job_id)
        assert failed is not None
        assert failed.error_message == "vpn_onboarding_reconciliation_required"

    await _run_with_stages(sessions, job_id, stages)

    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        endpoints = list(
            (
                await session.scalars(
                    select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
                )
            ).all()
        )
        assert job is not None and job.status == "succeeded"
        assert len(endpoints) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_stage", ["bootstrap", "deploy", "install", "ensure", "health"])
async def test_restart_after_each_stage_reobserves_and_completes(
    sessions, tmp_path, monkeypatch, failed_stage
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)
    _, ok_bootstrap, ok_deploy, ok_install, ok_execute, ok_health = stages
    failed = False

    async def bootstrap(*args, **kwargs):
        nonlocal failed
        if failed_stage == "bootstrap" and not failed:
            failed = True
            raise VpnNodeBootstrapError("vpn_node_bootstrap_mutation_uncertain")
        return await ok_bootstrap(*args, **kwargs)

    async def deploy(*args, **kwargs):
        nonlocal failed
        if failed_stage == "deploy" and not failed:
            failed = True
            raise NodeDeploymentError("vpn_node_deployment_incomplete")
        return await ok_deploy(*args, **kwargs)

    async def install(*args, **kwargs):
        nonlocal failed
        if failed_stage == "install" and not failed:
            failed = True
            raise EndpointInstallerDeploymentError(mutation_uncertain=False)
        return await ok_install(*args, **kwargs)

    async def execute(*args, **kwargs):
        nonlocal failed
        if failed_stage == "ensure" and not failed:
            failed = True
            raise EndpointInstallerDeploymentError(mutation_uncertain=False)
        return await ok_execute(*args, **kwargs)

    async def health(*args, **kwargs):
        nonlocal failed
        if failed_stage == "health" and not failed:
            failed = True
            return VpnNodeHealthReceipt(
                "unhealthy",
                "vpn_node_health_listener_unavailable",
                "running",
            )
        return await ok_health(*args, **kwargs)

    await _run_with_stages(
        sessions,
        job_id,
        stages,
        bootstrap=bootstrap,
        deploy=deploy,
        install=install,
        execute=execute,
        health=health,
    )
    async with sessions() as session:
        first = await session.get(WorkerMaintenanceJob, job_id)
        assert first is not None and first.status == "failed"

    await _run_with_stages(
        sessions,
        job_id,
        stages,
        bootstrap=bootstrap,
        deploy=deploy,
        install=install,
        execute=execute,
        health=health,
    )

    async with sessions() as session:
        completed = await session.get(WorkerMaintenanceJob, job_id)
        endpoints = list(
            (
                await session.scalars(
                    select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
                )
            ).all()
        )
        assert completed is not None and completed.status == "succeeded"
        assert len(endpoints) == 1


@pytest.mark.asyncio
async def test_changed_pin_stops_before_any_remote_mutation(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(
        monkeypatch,
        onboarding,
        tmp_path,
        known_host="192.0.2.16 ssh-ed25519 changed-key",
    )
    stages = _successful_stages(worker_id)

    await _run_with_stages(sessions, job_id, stages)

    assert stages[0] == []
    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None
        assert job.error_message == "vpn_onboarding_host_key_changed"


@pytest.mark.asyncio
async def test_public_host_falls_back_to_ssh_host(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions, vpn_public_host=None)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)
    public_hosts: list[str] = []
    original_execute = stages[4]

    async def execute(*args, **kwargs):
        public_hosts.append(args[1].public_host)
        return await original_execute(*args, **kwargs)

    await _run_with_stages(sessions, job_id, stages, execute=execute)

    assert public_hosts == ["192.0.2.16"]
    async with sessions() as session:
        worker = await session.get(WorkerNode, worker_id)
        assert worker is not None and worker.vpn_public_host == "192.0.2.16"


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_change", [{"archived_at": datetime.now(UTC)}, {"is_enabled": False}])
async def test_unavailable_worker_stops_before_remote_mutation(
    sessions, tmp_path, monkeypatch, worker_change
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions, **worker_change)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)

    await _run_with_stages(sessions, job_id, stages)

    assert stages[0] == []
    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None
        assert job.error_message == "vpn_onboarding_worker_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("reservation", ["control", "attack", "maintenance"])
async def test_active_reservation_stops_before_remote_mutation(
    sessions, tmp_path, monkeypatch, reservation
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    if reservation == "control":
        async def controls(_session):
            return {worker_id}

        monkeypatch.setattr(onboarding, "active_vpn_control_worker_ids", controls)
    elif reservation == "attack":
        async def attacks(_session):
            return {worker_id}

        monkeypatch.setattr(onboarding, "active_attack_worker_ids", attacks)
    else:
        async with sessions() as session:
            session.add(
                WorkerMaintenanceJob(
                    worker_id=worker_id,
                    action="vpn_update",
                    status="running",
                )
            )
            await session.commit()
    stages = _successful_stages(worker_id)

    await _run_with_stages(sessions, job_id, stages)

    assert stages[0] == []
    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None
        assert job.error_message == "vpn_onboarding_worker_busy"


@pytest.mark.asyncio
async def test_uncertain_endpoint_without_clean_inspect_requires_reconciliation(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)

    async def uncertain(*_args, **_kwargs):
        raise EndpointInstallerDeploymentError(mutation_uncertain=True)

    await _run_with_stages(sessions, job_id, stages, execute=uncertain)

    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        assert job is not None
        assert job.error_message == "vpn_onboarding_reconciliation_required"


@pytest.mark.asyncio
async def test_foreign_endpoint_registration_conflict_is_safe_and_preserved(
    sessions, tmp_path, monkeypatch
) -> None:
    from app.services import vpn_node_onboarding as onboarding

    job_id, worker_id = await _job(sessions)
    async with sessions() as session:
        session.add(
            VpnEndpoint(
                worker_id=worker_id,
                inbound_id=88,
                public_host="192.0.2.16",
                port=443,
                protocol="vless",
                transport="tcp",
                security="reality",
                server_name="gateway.icloud.com",
                public_key="A" * 43,
                short_id="0011223344556677",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                status="staged",
            )
        )
        await session.commit()
    _configure_control(monkeypatch, onboarding, tmp_path)
    stages = _successful_stages(worker_id)

    await _run_with_stages(sessions, job_id, stages)

    async with sessions() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        endpoints = list(
            (
                await session.scalars(
                    select(VpnEndpoint).where(VpnEndpoint.worker_id == worker_id)
                )
            ).all()
        )
        assert job is not None
        assert job.error_message == "vpn_endpoint_registration_conflict"
        assert [(item.inbound_id, item.port) for item in endpoints] == [(88, 443)]
