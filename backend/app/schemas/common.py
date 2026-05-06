from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class MessageResponse(BaseModel):
    detail: str


class HealthResponse(BaseModel):
    status: str
    checked_at: datetime


class DomainHealthItem(BaseModel):
    domain_id: int
    domain: str
    status: str
    check_mode: str
    last_check_at: datetime | None
    worker_heartbeat_at: datetime | None
    consecutive_failures: int
    is_stale: bool


class MonitoringHealthResponse(BaseModel):
    status: str
    checked_at: datetime
    active_domains: int
    stale_domains: int
    workers_in_memory: int
    items: list[DomainHealthItem]
