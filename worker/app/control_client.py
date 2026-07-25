from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from app.config import WorkerSettings


@dataclass(slots=True)
class ControlTask:
    task_id: int
    attack_run_id: int
    domain_id: int
    worker_id: int
    fqdn: str
    zone: str
    planned_start_at: datetime
    planned_end_at: datetime
    planned_rps: float
    requested_duration_years: int
    registration_extra_parameters: str | None
    registrar: dict
    contact: dict


@dataclass(slots=True)
class ControlTaskStatus:
    task_id: int
    status: str
    stop_reason: str | None
    planned_rps: float


@dataclass(slots=True)
class CreatePermitResponse:
    allowed: bool
    stop: bool
    reason: str | None
    lease_expires_at: datetime | None


@dataclass(slots=True)
class DiscoveryControlTask:
    task_id: int
    discovery_domain_id: int
    worker_id: int
    fqdn: str
    zone: str
    source_mode: str
    bootstrap_url: str
    timeout_seconds: float


class ControlClient:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.client = httpx.AsyncClient(
            base_url=settings.control_base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.request_timeout_seconds, connect=settings.connect_timeout_seconds),
            headers={"X-Worker-Token": settings.control_token},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def heartbeat(self, payload: dict) -> dict:
        response = await self.client.post("/api/worker-runtime/heartbeat", json=payload)
        response.raise_for_status()
        return response.json()

    async def next_task(self) -> ControlTask | None:
        response = await self.client.get("/api/worker-runtime/tasks/next", params={"worker_id": self.settings.worker_id})
        response.raise_for_status()
        payload = response.json()
        task = payload.get("task")
        if not task:
            return None
        return ControlTask(
            task_id=task["task_id"],
            attack_run_id=task["attack_run_id"],
            domain_id=task["domain_id"],
            worker_id=task["worker_id"],
            fqdn=task["fqdn"],
            zone=task["zone"],
            planned_start_at=_parse_control_datetime(task["planned_start_at"]),
            planned_end_at=_parse_control_datetime(task["planned_end_at"]),
            planned_rps=float(task["planned_rps"]),
            requested_duration_years=int(task["requested_duration_years"]),
            registration_extra_parameters=task.get("registration_extra_parameters"),
            registrar=task["registrar"],
            contact=task["contact"],
        )

    async def acknowledge_task(self, task_id: int) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/tasks/{task_id}/ack",
            json={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()

    async def get_task_status(self, task_id: int) -> ControlTaskStatus:
        response = await self.client.get(
            f"/api/worker-runtime/tasks/{task_id}/status",
            params={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()
        payload = response.json()
        return ControlTaskStatus(
            task_id=int(payload["task_id"]),
            status=payload["status"],
            stop_reason=payload.get("stop_reason"),
            planned_rps=float(payload.get("planned_rps", 0.0)),
        )

    async def report_progress(self, task_id: int, payload: dict) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/tasks/{task_id}/progress",
            json={"worker_id": self.settings.worker_id, **payload},
        )
        response.raise_for_status()

    async def report_result(self, task_id: int, payload: dict) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/tasks/{task_id}/result",
            json={"worker_id": self.settings.worker_id, **payload},
        )
        response.raise_for_status()

    async def acquire_create_permit(self, task_id: int) -> CreatePermitResponse:
        response = await self.client.post(
            f"/api/worker-runtime/tasks/{task_id}/create-permit/acquire",
            json={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()
        payload = response.json()
        lease_expires_at = payload.get("lease_expires_at")
        return CreatePermitResponse(
            allowed=bool(payload.get("allowed")),
            stop=bool(payload.get("stop")),
            reason=payload.get("reason"),
            lease_expires_at=_parse_control_datetime(lease_expires_at) if lease_expires_at else None,
        )

    async def release_create_permit(self, task_id: int) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/tasks/{task_id}/create-permit/release",
            json={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()

    async def next_discovery_task(self) -> DiscoveryControlTask | None:
        response = await self.client.get(
            "/api/worker-runtime/discovery/tasks/next",
            params={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()
        payload = response.json()
        task = payload.get("task")
        if not task:
            return None
        return DiscoveryControlTask(
            task_id=task["task_id"],
            discovery_domain_id=task["discovery_domain_id"],
            worker_id=task["worker_id"],
            fqdn=task["fqdn"],
            zone=task["zone"],
            source_mode=task.get("source_mode") or "rdap",
            bootstrap_url=task["bootstrap_url"],
            timeout_seconds=float(task.get("timeout_seconds", self.settings.request_timeout_seconds)),
        )

    async def acknowledge_discovery_task(self, task_id: int) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/discovery/tasks/{task_id}/ack",
            json={"worker_id": self.settings.worker_id},
        )
        response.raise_for_status()

    async def report_discovery_result(self, task_id: int, payload: dict) -> None:
        response = await self.client.post(
            f"/api/worker-runtime/discovery/tasks/{task_id}/result",
            json={"worker_id": self.settings.worker_id, **payload},
        )
        response.raise_for_status()


def _parse_control_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
