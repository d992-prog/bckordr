from __future__ import annotations

import asyncio
import logging
import math
import random
from collections import deque
from datetime import datetime, timezone
from time import perf_counter
from types import SimpleNamespace

import httpx

try:
    import psutil
except ModuleNotFoundError:  # pragma: no cover - exercised in constrained environments
    class _PsutilFallback:
        @staticmethod
        def cpu_percent(interval=None) -> float:
            del interval
            return 0.0

        @staticmethod
        def virtual_memory() -> SimpleNamespace:
            return SimpleNamespace(percent=0.0)

    psutil = _PsutilFallback()

from app.config import WorkerSettings
from app.control_client import ControlClient, ControlTask, ControlTaskStatus
from app.gandi import register_domain

logger = logging.getLogger(__name__)

RESPONSE_SAMPLE_PREVIEW_LIMIT = 2000


def _add_count(counter: dict[str, int], key: str | int | None) -> None:
    normalized = str(key if key is not None else "none")
    counter[normalized] = counter.get(normalized, 0) + 1


def _record_response_sample(
    samples: dict[str, object],
    *,
    status_code: int | None = None,
    latency_ms: float | None = None,
    body_preview: str | None = None,
    error: str | None = None,
    error_type: str | None = None,
) -> None:
    sample = {
        "at": datetime.now(timezone.utc).isoformat(),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "body_preview": (body_preview or "")[:RESPONSE_SAMPLE_PREVIEW_LIMIT],
        "error": (error or "")[:RESPONSE_SAMPLE_PREVIEW_LIMIT],
        "error_type": error_type or "",
    }
    first = samples.setdefault("first", [])
    assert isinstance(first, list)
    if len(first) < 3:
        first.append(sample)
    last = samples.setdefault("last", [])
    assert isinstance(last, list)
    last.append(sample)
    del last[:-3]
    if status_code is not None:
        by_status = samples.setdefault("by_status", {})
        assert isinstance(by_status, dict)
        status_samples = by_status.setdefault(str(status_code), [])
        assert isinstance(status_samples, list)
        if len(status_samples) < 3:
            status_samples.append(sample)


class WorkerRunner:
    def __init__(self, settings: WorkerSettings) -> None:
        self.settings = settings
        self.control = ControlClient(settings)
        self._stop = False
        self._clock_offset_ms = 0
        self._current_rps = 0.0
        self._current_capacity_rps = 0.0
        self._simulate_random = random.Random(settings.simulate_random_seed)

    async def close(self) -> None:
        await self.control.close()

    async def run(self) -> None:
        try:
            while not self._stop:
                await self._heartbeat(status="ready")
                task = await self.control.next_task()
                if task is None:
                    await asyncio.sleep(self.settings.poll_interval_seconds)
                    continue
                await self.control.acknowledge_task(task.task_id)
                await self._execute_task(task)
        finally:
            await self.close()

    async def _heartbeat(self, *, status: str) -> None:
        payload = {
            "worker_id": self.settings.worker_id,
            "status": status,
            "current_rps": round(self._current_rps, 2),
            "current_capacity_rps": round(self._current_capacity_rps, 2),
            "cpu_load": psutil.cpu_percent(interval=None),
            "ram_usage_percent": psutil.virtual_memory().percent,
            "clock_drift_ms": self._clock_drift_ms(),
            "runtime_mode": "test" if self.settings.simulate_mode else "live",
            "registration_concurrency_multiplier": self.settings.registration_concurrency_multiplier,
            "registration_max_concurrency": self.settings.registration_max_concurrency,
            "ip_address": None,
            "region": None,
        }
        request_started_at = datetime.now(timezone.utc)
        response = await self.control.heartbeat(payload)
        response_received_at = datetime.now(timezone.utc)
        server_time = response.get("server_time")
        if server_time:
            server_time_utc = datetime.fromisoformat(server_time.replace("Z", "+00:00"))
            request_midpoint = request_started_at + (response_received_at - request_started_at) / 2
            self._clock_offset_ms = int(abs((server_time_utc - request_midpoint).total_seconds() * 1000))

    def _clock_drift_ms(self) -> int:
        return self._clock_offset_ms

    def _runtime_limits(self, planned_rps: float) -> tuple[float, int]:
        effective_rps = max(0.0, float(planned_rps))
        dispatch_interval = 1.0 / max(effective_rps, 0.1)
        concurrency_limit = max(
            1,
            min(
                self.settings.registration_max_concurrency,
                math.ceil(max(effective_rps, 1.0) * self.settings.registration_concurrency_multiplier),
            ),
        )
        return dispatch_interval, concurrency_limit

    def _apply_live_task_status(self, task: ControlTask, status: ControlTaskStatus) -> tuple[float, int]:
        task.planned_rps = max(0.0, status.planned_rps)
        self._current_capacity_rps = task.planned_rps
        return self._runtime_limits(task.planned_rps)

    async def _wait_until_start(self, task: ControlTask) -> bool:
        while datetime.now(timezone.utc) < task.planned_start_at:
            status = await self.control.get_task_status(task.task_id)
            self._apply_live_task_status(task, status)
            if status.status not in {"queued", "running"}:
                logger.info("Task %s cancelled before start: %s", task.task_id, status.status)
                return False
            await self._heartbeat(status="waiting")
            await asyncio.sleep(min(1.0, self.settings.heartbeat_interval_seconds))
        return True

    async def _execute_task(self, task: ControlTask) -> None:
        if not await self._wait_until_start(task):
            return

        if task.registrar["registrar_slug"] != "gandi":
            await self.control.report_result(
                task.task_id,
                {
                    "status": "failed",
                    "last_error": f"Unsupported registrar: {task.registrar['registrar_slug']}",
                },
            )
            return

        self._current_capacity_rps = task.planned_rps
        await self._heartbeat(status="running")

        async with self._make_registration_client() as client:
            dispatch_interval, concurrency_limit = self._runtime_limits(task.planned_rps)
            next_dispatch_at = perf_counter()
            next_status_poll_at = perf_counter()
            next_progress_at = perf_counter() + 0.5
            pending: set[asyncio.Task[tuple[int, float, str]]] = set()
            attempt_times: deque[float] = deque()
            total_attempts = 0
            success_attempts = 0
            last_status: int | None = None
            last_error: str | None = None
            last_latency_ms: float | None = None
            response_status_counts: dict[str, int] = {}
            response_error_counts: dict[str, int] = {}
            response_samples: dict[str, object] = {"first": [], "last": [], "by_status": {}}
            stop_requested = False

            while datetime.now(timezone.utc) <= task.planned_end_at or pending:
                loop_now = perf_counter()
                utc_now = datetime.now(timezone.utc)

                if loop_now >= next_status_poll_at:
                    runtime_status = await self.control.get_task_status(task.task_id)
                    previous_planned_rps = task.planned_rps
                    dispatch_interval, concurrency_limit = self._apply_live_task_status(task, runtime_status)
                    if abs(previous_planned_rps - task.planned_rps) > 1e-9:
                        next_dispatch_at = perf_counter() + dispatch_interval
                    if runtime_status.status not in {"queued", "running"}:
                        logger.info("Task %s stopped by control: %s", task.task_id, runtime_status.status)
                        stop_requested = True
                    next_status_poll_at = loop_now + 0.5

                while attempt_times and loop_now - attempt_times[0] > 1.0:
                    attempt_times.popleft()
                self._current_rps = float(len(attempt_times))

                while (
                    not stop_requested
                    and utc_now <= task.planned_end_at
                    and task.planned_rps > 0
                    and len(pending) < concurrency_limit
                    and loop_now >= next_dispatch_at
                ):
                    pending.add(asyncio.create_task(self._attempt_register(client, task)))
                    total_attempts += 1
                    attempt_times.append(loop_now)
                    self._current_rps = float(len(attempt_times))
                    next_dispatch_at += dispatch_interval
                    loop_now = perf_counter()
                    utc_now = datetime.now(timezone.utc)

                if not pending and (stop_requested or utc_now > task.planned_end_at):
                    break

                timeout_candidates = [0.25]
                timeout_candidates.append(max(0.0, next_status_poll_at - perf_counter()))
                timeout_candidates.append(max(0.0, next_progress_at - perf_counter()))
                if not stop_requested and utc_now <= task.planned_end_at and task.planned_rps > 0 and len(pending) < concurrency_limit:
                    timeout_candidates.append(max(0.0, next_dispatch_at - perf_counter()))
                timeout = min(timeout_candidates)
                if pending:
                    done, pending = await asyncio.wait(
                        pending,
                        timeout=timeout,
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                else:
                    done = set()
                    await asyncio.sleep(timeout)

                for completed in done:
                    try:
                        status_code, latency_ms, body_preview = await completed
                    except Exception as exc:
                        last_error = str(exc)
                        error_type = exc.__class__.__name__
                        _add_count(response_error_counts, error_type)
                        _record_response_sample(response_samples, error=last_error, error_type=error_type)
                        continue

                    last_status = status_code
                    last_latency_ms = latency_ms
                    _add_count(response_status_counts, status_code)
                    _record_response_sample(
                        response_samples,
                        status_code=status_code,
                        latency_ms=latency_ms,
                        body_preview=body_preview,
                    )
                    if status_code == 200:
                        success_attempts += 1
                        for queued in pending:
                            queued.cancel()
                        if pending:
                            await asyncio.gather(*pending, return_exceptions=True)
                        await self.control.report_result(
                            task.task_id,
                            {
                                "status": "success",
                                "total_attempts": total_attempts,
                                "success_attempts": success_attempts,
                                "latency_ms": latency_ms,
                                "last_http_status": status_code,
                                "response_status_counts": response_status_counts,
                                "response_error_counts": response_error_counts,
                                "response_samples": response_samples,
                                "success_response_code": status_code,
                                "success_message": body_preview[:500],
                            },
                        )
                        self._current_rps = 0.0
                        self._current_capacity_rps = 0.0
                        await self._heartbeat(status="ready")
                        return
                    last_error = body_preview[:500]

                if perf_counter() >= next_progress_at:
                    while attempt_times and perf_counter() - attempt_times[0] > 1.0:
                        attempt_times.popleft()
                    self._current_rps = float(len(attempt_times))
                    await self.control.report_progress(
                        task.task_id,
                        {
                            "actual_rps": self._current_rps,
                            "total_attempts": total_attempts,
                            "success_attempts": success_attempts,
                            "latency_ms": last_latency_ms,
                            "last_http_status": last_status,
                            "last_error": last_error,
                            "response_status_counts": response_status_counts,
                            "response_error_counts": response_error_counts,
                            "response_samples": response_samples,
                        },
                    )
                    await self._heartbeat(status="running")
                    next_progress_at = perf_counter() + 0.5

            for queued in pending:
                queued.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            await self.control.report_result(
                task.task_id,
                {
                    "status": "stopped" if stop_requested else "failed",
                    "total_attempts": total_attempts,
                    "success_attempts": success_attempts,
                    "latency_ms": last_latency_ms,
                    "last_http_status": last_status,
                    "last_error": last_error or "Attack window completed without success",
                    "response_status_counts": response_status_counts,
                    "response_error_counts": response_error_counts,
                    "response_samples": response_samples,
                },
            )
            self._current_rps = 0.0
            self._current_capacity_rps = 0.0
            await self._heartbeat(status="ready")

    def _make_registration_client(self) -> httpx.AsyncClient:
        try:
            return httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, http2=True)
        except ImportError:
            logger.warning("http2 extras are unavailable; falling back to http1 client")
            return httpx.AsyncClient(timeout=self.settings.request_timeout_seconds, http2=False)

    async def _attempt_register(self, client: httpx.AsyncClient, task: ControlTask) -> tuple[int, float, str]:
        started = perf_counter()
        if self.settings.simulate_mode:
            delay_ms = max(0, self.settings.simulate_latency_ms)
            if self.settings.simulate_jitter_ms > 0:
                delay_ms += self._simulate_random.uniform(0, self.settings.simulate_jitter_ms)
            await asyncio.sleep(delay_ms / 1000)
            is_success = self._simulate_random.random() < self.settings.simulate_success_rate
            status_code = (
                self.settings.simulate_success_status_code
                if is_success
                else self.settings.simulate_failure_status_code
            )
            body = "simulated success" if is_success else "simulated failure"
            return status_code, (perf_counter() - started) * 1000, body
        status_code, body = await register_domain(
            task,
            client,
            poll_create_status=self.settings.gandi_create_status_poll_enabled,
            status_poll_interval_seconds=self.settings.gandi_status_poll_interval_seconds,
            status_poll_max_attempts=self.settings.gandi_status_poll_max_attempts,
        )
        latency_ms = (perf_counter() - started) * 1000
        return status_code, latency_ms, body
