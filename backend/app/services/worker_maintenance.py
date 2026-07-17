from __future__ import annotations

from app.db.base import utcnow
from app.db.models import WorkerMaintenanceJob, WorkerNode
from app.db.session import AsyncSessionLocal


def build_worker_maintenance_commands(action: str) -> list[str]:
    if action == "check":
        return [
            "hostname",
            "whoami",
            "systemctl is-active domain-drop-worker.service || true",
        ]
    if action == "update":
        return [
            "cd /opt/domain-drop-catcher && git pull --ff-only origin main",
            "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
            "systemctl restart domain-drop-worker.service",
            "systemctl is-active domain-drop-worker.service",
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
        await session.commit()

        try:
            commands = build_worker_maintenance_commands(job.action)
            log = await execute_worker_ssh_commands(worker, commands)
        except Exception as exc:  # pragma: no cover - exact SSH errors depend on environment
            job.status = "failed"
            job.error_message = str(exc)
            job.finished_at = utcnow()
            job.updated_at = utcnow()
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
