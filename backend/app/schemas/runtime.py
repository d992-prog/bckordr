from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class WorkerHeartbeatRequest(BaseModel):
    worker_id: int
    status: str
    ip_address: str | None = None
    region: str | None = None
    current_rps: float = 0.0
    current_capacity_rps: float = 0.0
    cpu_load: float = 0.0
    ram_usage_percent: float = 0.0
    clock_drift_ms: int = 0
    runtime_mode: str = "unknown"
    registration_concurrency_multiplier: float = 2.0
    registration_max_concurrency: int = 64


class WorkerHeartbeatResponse(BaseModel):
    detail: str
    server_time: datetime


class WorkerContactPayload(BaseModel):
    id: int | None
    label: str | None
    person_type: str
    given_name: str
    family_name: str
    organization_name: str | None
    email: str
    phone: str
    mobile: str | None = None
    fax: str | None = None
    lang: str | None = None
    street_address: str
    city: str
    state: str | None
    zip_code: str
    country_code: str
    data_obfuscated: bool | None = None
    mail_obfuscated: bool | None = None
    icann_contract_accept: bool | None = None
    extra_parameters: str | None = None


class WorkerRegistrarPayload(BaseModel):
    id: int | None
    name: str | None
    registrar_slug: str
    api_token: str | None
    sharing_id: str | None
    api_base_url: str | None = None
    supports_dry_run: bool


class WorkerTaskPayloadResponse(BaseModel):
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
    registration_extra_parameters: str | None = None
    registrar: WorkerRegistrarPayload
    contact: WorkerContactPayload


class WorkerTaskAckRequest(BaseModel):
    worker_id: int


class WorkerTaskResultRequest(BaseModel):
    worker_id: int
    status: str
    total_attempts: int = 0
    success_attempts: int = 0
    latency_ms: float | None = None
    last_http_status: int | None = None
    last_error: str | None = None
    success_response_code: int | None = None
    success_message: str | None = None


class WorkerTaskProgressRequest(BaseModel):
    worker_id: int
    actual_rps: float = 0.0
    total_attempts: int = 0
    success_attempts: int = 0
    latency_ms: float | None = None
    last_http_status: int | None = None
    last_error: str | None = None


class WorkerTaskResponseEnvelope(BaseModel):
    task: WorkerTaskPayloadResponse | None


class WorkerTaskResultResponse(BaseModel):
    detail: str

    model_config = ConfigDict(from_attributes=True)


class WorkerTaskStatusResponse(BaseModel):
    task_id: int
    status: str
    stop_reason: str | None = None
    planned_rps: float = 0.0
