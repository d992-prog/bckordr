from __future__ import annotations

import shlex

from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import VpnNodeEvent, WorkerMaintenanceJob, WorkerNode
from app.db.session import AsyncSessionLocal
from app.services.app_settings import DiscoveryRuntimeSettings, get_discovery_runtime_settings

VPN_MAINTENANCE_ACTIONS = {"vpn_check", "vpn_install", "vpn_update", "vpn_restart"}
VPN_RUNNING_STATUS_BY_ACTION = {
    "vpn_check": "checking",
    "vpn_install": "installing",
    "vpn_update": "updating",
    "vpn_restart": "restarting",
}


def _shell_quote(value: str | int | float | bool) -> str:
    return shlex.quote(str(value))


def _build_printf_command(path: str, lines: list[str]) -> str:
    quoted_lines = " ".join(_shell_quote(line) for line in lines)
    return f"printf '%s\\n' {quoted_lines} > {_shell_quote(path)}"


def _build_worker_env_lines(
    worker: WorkerNode,
    *,
    runtime_base_url: str,
    simulate_mode: bool,
    discovery_settings: DiscoveryRuntimeSettings | None = None,
) -> list[str]:
    settings = get_settings()
    worker_discovery_enabled = (
        discovery_settings.discovery_worker_enabled if discovery_settings is not None else settings.discovery_worker_enabled
    )
    worker_discovery_concurrency = (
        discovery_settings.worker_discovery_concurrency
        if discovery_settings is not None
        else settings.worker_discovery_concurrency
    )
    worker_discovery_poll_interval_seconds = (
        discovery_settings.worker_discovery_poll_interval_seconds
        if discovery_settings is not None
        else settings.worker_discovery_poll_interval_seconds
    )
    return [
        f"CONTROL_BASE_URL={runtime_base_url.rstrip('/')}",
        f"WORKER_ID={worker.id}",
        f"CONTROL_TOKEN={worker.control_token or ''}",
        "POLL_INTERVAL_SECONDS=2",
        "HEARTBEAT_INTERVAL_SECONDS=5",
        "REQUEST_TIMEOUT_SECONDS=10",
        "CONNECT_TIMEOUT_SECONDS=2",
        f"SIMULATE_MODE={'true' if simulate_mode else 'false'}",
        "SIMULATE_LATENCY_MS=20",
        "SIMULATE_JITTER_MS=10",
        "SIMULATE_SUCCESS_RATE=1.0",
        "SIMULATE_SUCCESS_STATUS_CODE=200",
        "SIMULATE_FAILURE_STATUS_CODE=503",
        "SIMULATE_RANDOM_SEED=12345",
        "GANDI_CREATE_STATUS_POLL_ENABLED=false",
        "GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5",
        "GANDI_STATUS_POLL_MAX_ATTEMPTS=8",
        "REGISTRATION_CONCURRENCY_MULTIPLIER=8",
        "REGISTRATION_MAX_CONCURRENCY=160",
        f"DISCOVERY_WORKER_ENABLED={'true' if worker_discovery_enabled else 'false'}",
        f"DISCOVERY_WORKER_CONCURRENCY={worker_discovery_concurrency}",
        f"DISCOVERY_WORKER_POLL_INTERVAL_SECONDS={worker_discovery_poll_interval_seconds}",
        "MAX_IDLE_BACKOFF_SECONDS=10",
    ]


def _build_env_upsert_command(path: str, key: str, value: str | int | float | bool) -> str:
    line = f"{key}={value}"
    quoted_path = _shell_quote(path)
    quoted_line = _shell_quote(line)
    quoted_pattern = _shell_quote(f"^{key}=")
    quoted_replacement = _shell_quote(f"s|^{key}=.*|{line}|")
    return f"grep -q {quoted_pattern} {quoted_path} && sed -i {quoted_replacement} {quoted_path} || printf '%s\\n' {quoted_line} >> {quoted_path}"


def _build_worker_discovery_env_commands(discovery_settings: DiscoveryRuntimeSettings | None = None) -> list[str]:
    settings = get_settings()
    worker_discovery_enabled = (
        discovery_settings.discovery_worker_enabled if discovery_settings is not None else settings.discovery_worker_enabled
    )
    worker_discovery_concurrency = (
        discovery_settings.worker_discovery_concurrency
        if discovery_settings is not None
        else settings.worker_discovery_concurrency
    )
    worker_discovery_poll_interval_seconds = (
        discovery_settings.worker_discovery_poll_interval_seconds
        if discovery_settings is not None
        else settings.worker_discovery_poll_interval_seconds
    )
    env_path = "/opt/domain-drop-catcher/worker/.env"
    return [
        _build_env_upsert_command(env_path, "GANDI_CREATE_STATUS_POLL_ENABLED", "false"),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_ENABLED", "true" if worker_discovery_enabled else "false"),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_CONCURRENCY", worker_discovery_concurrency),
        _build_env_upsert_command(env_path, "DISCOVERY_WORKER_POLL_INTERVAL_SECONDS", worker_discovery_poll_interval_seconds),
    ]


def _build_worker_service_command() -> str:
    lines = [
        "[Unit]",
        "Description=Domain Drop Catcher Worker",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=/opt/domain-drop-catcher/worker",
        "ExecStart=/opt/domain-drop-catcher/worker/.venv/bin/python -m app.main",
        "Restart=always",
        "RestartSec=2",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return _build_printf_command("/etc/systemd/system/domain-drop-worker.service", lines)


def _bash(command: str) -> str:
    return f"bash -lc {_shell_quote(command)}"


def _build_vpn_status_command() -> str:
    return _bash(
        "systemctl is-active x-ui.service "
        "|| systemctl is-active x-ui "
        "|| systemctl is-active 3x-ui.service "
        "|| true"
    )


def _build_vpn_ready_command() -> str:
    return _bash(
        "systemctl is-active x-ui.service "
        "|| systemctl is-active x-ui "
        "|| systemctl is-active 3x-ui.service"
    )


def build_worker_maintenance_commands(
    action: str,
    *,
    worker: WorkerNode | None = None,
    discovery_settings: DiscoveryRuntimeSettings | None = None,
) -> list[str]:
    if action == "check":
        return [
            "hostname",
            "whoami",
            "systemctl is-active domain-drop-worker.service || true",
        ]
    if action == "install":
        if worker is None:
            raise ValueError("Worker is required for install action")
        settings = get_settings()
        runtime_base_url = settings.worker_runtime_public_base_url or "http://CONTROL_SERVER_IP:8080"
        env_command = _build_printf_command(
            "/opt/domain-drop-catcher/worker/.env",
            _build_worker_env_lines(
                worker,
                runtime_base_url=runtime_base_url,
                simulate_mode=False,
                discovery_settings=discovery_settings,
            ),
        )
        return [
            "apt-get update",
            "apt-get install -y git python3.11 python3.11-venv python3.11-dev build-essential",
            "test ! -e /opt/domain-drop-catcher",
            f"git clone {_shell_quote(settings.worker_setup_repository_url)} /opt/domain-drop-catcher",
            f"{_shell_quote(settings.worker_setup_python_bin)} -m venv /opt/domain-drop-catcher/worker/.venv",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install --upgrade pip",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
            env_command,
            _build_worker_service_command(),
            "systemctl daemon-reload",
            "systemctl enable --now domain-drop-worker.service",
            "systemctl is-active domain-drop-worker.service",
        ]
    if action == "update":
        discovery_env_commands = _build_worker_discovery_env_commands(discovery_settings)
        return [
            "cd /opt/domain-drop-catcher && git pull --ff-only origin main",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
            *discovery_env_commands,
            "systemctl daemon-reload",
            "systemctl restart domain-drop-worker.service",
            "systemctl is-active domain-drop-worker.service",
        ]
    if action == "vpn_check":
        return [
            "hostname",
            "whoami",
            "command -v x-ui || true",
            _build_vpn_status_command(),
            _build_vpn_ready_command(),
            _bash("systemctl --no-pager --full status x-ui.service || systemctl --no-pager --full status x-ui || true"),
            _bash("ss -lntp | grep -E ':(443|8443|2053|54321|62789)\\b' || true"),
            "test -d /usr/local/x-ui && echo x-ui-dir-present || true",
        ]
    if action == "vpn_install":
        return [
            "apt-get update",
            "apt-get install -y curl socat jq tar",
            _bash(
                "set -e; "
                "if command -v x-ui >/dev/null 2>&1 || systemctl list-unit-files | grep -Eq '^x-ui(\\.service)?'; "
                "then echo '3x-ui already installed'; "
                "else curl -fsSL https://raw.githubusercontent.com/mhsanaei/3x-ui/master/install.sh -o /tmp/3x-ui-install.sh "
                "&& chmod +x /tmp/3x-ui-install.sh "
                "&& yes '' | timeout 600 bash /tmp/3x-ui-install.sh; "
                "fi"
            ),
            "systemctl enable --now x-ui.service || systemctl enable --now x-ui || systemctl enable --now 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    if action == "vpn_update":
        return [
            "apt-get install -y curl || true",
            _bash("if command -v x-ui >/dev/null 2>&1; then x-ui update; else echo 'x-ui command not found'; fi"),
            "systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    if action == "vpn_restart":
        return [
            "systemctl restart x-ui.service || systemctl restart x-ui || systemctl restart 3x-ui.service",
            _build_vpn_ready_command(),
        ]
    raise ValueError(f"Unsupported worker maintenance action: {action}")


async def run_worker_maintenance_job(job_id: int) -> None:
    async with AsyncSessionLocal() as session:
        job = await session.get(WorkerMaintenanceJob, job_id)
        if job is None:
            return
        worker = await session.get(WorkerNode, job.worker_id)
        if worker is None:
            job.status = "failed"
            job.error_message = "Worker not found"
            job.finished_at = utcnow()
            await session.commit()
            return

        job.status = "running"
        job.started_at = utcnow()
        job.updated_at = utcnow()
        if job.action in VPN_MAINTENANCE_ACTIONS:
            worker.vpn_runtime_status = VPN_RUNNING_STATUS_BY_ACTION[job.action]
            worker.vpn_last_error = None
            worker.vpn_last_checked_at = utcnow()
        await session.commit()

        try:
            discovery_settings = await get_discovery_runtime_settings(session, get_settings())
            commands = build_worker_maintenance_commands(
                job.action,
                worker=worker,
                discovery_settings=discovery_settings,
            )
            log = await execute_worker_ssh_commands(worker, commands)
        except Exception as exc:  # pragma: no cover - exact SSH errors depend on environment
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = utcnow()
            job.updated_at = utcnow()
            if job.action in VPN_MAINTENANCE_ACTIONS:
                worker.vpn_runtime_status = "error"
                worker.vpn_last_error = str(exc)
                worker.vpn_last_checked_at = utcnow()
                session.add(
                    VpnNodeEvent(
                        worker_id=worker.id,
                        level="error",
                        event_type=job.action,
                        message=f"VPN maintenance failed: {str(exc)[:500]}",
                        details={"job_id": job.id},
                    )
                )
            else:
                worker.ssh_last_check_status = "failed"
                worker.ssh_last_check_message = str(exc)
                worker.ssh_last_checked_at = utcnow()
            await session.commit()
            return

        job.status = "succeeded"
        job.log = log
        job.finished_at = utcnow()
        job.updated_at = utcnow()
        if job.action == "check":
            worker.ssh_last_check_status = "ok"
            worker.ssh_last_check_message = "SSH check succeeded"
            worker.ssh_last_checked_at = utcnow()
        if job.action in VPN_MAINTENANCE_ACTIONS:
            worker.vpn_runtime_status = "ready"
            worker.vpn_last_error = None
            worker.vpn_last_checked_at = utcnow()
            session.add(
                VpnNodeEvent(
                    worker_id=worker.id,
                    level="info",
                    event_type=job.action,
                    message="VPN maintenance succeeded",
                    details={"job_id": job.id},
                )
            )
        await session.commit()


async def execute_worker_ssh_commands(worker: WorkerNode, commands: list[str]) -> str:
    try:
        import asyncssh
    except ImportError as exc:  # pragma: no cover - dependency is installed in production
        raise RuntimeError("asyncssh is not installed; run backend pip install after updating the project") from exc

    host = worker.ssh_host or worker.ip_address
    if not host:
        raise ValueError("Worker SSH host is missing")
    if not worker.ssh_username:
        raise ValueError("Worker SSH username is missing")
    if not worker.ssh_password and not worker.ssh_key_path:
        raise ValueError("Worker SSH password or key path is missing")

    connect_kwargs = {
        "host": host,
        "port": worker.ssh_port or 22,
        "username": worker.ssh_username,
        "known_hosts": None,
    }
    if worker.ssh_password:
        connect_kwargs["password"] = worker.ssh_password
    if worker.ssh_key_path:
        connect_kwargs["client_keys"] = [worker.ssh_key_path]

    log_parts: list[str] = []
    async with asyncssh.connect(**connect_kwargs) as connection:
        for command in commands:
            log_parts.append(f"$ {command}")
            result = await connection.run(command, check=False)
            stdout = result.stdout.strip()
            stderr = result.stderr.strip()
            if stdout:
                log_parts.append(stdout)
            if stderr:
                log_parts.append(stderr)
            log_parts.append(f"exit={result.exit_status}")
            if result.exit_status != 0:
                raise RuntimeError(f"Command failed with exit={result.exit_status}: {command}\n{stderr or stdout}")
    return "\n".join(log_parts)
