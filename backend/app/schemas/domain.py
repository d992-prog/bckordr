from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DomainCreateRequest(BaseModel):
    domain: str
    check_interval: float | None = Field(default=None, ge=0.2)
    burst_check_interval: float | None = Field(default=None, ge=0.1)
    scheduler_mode: str | None = None
    confirmation_threshold: int | None = Field(default=None, ge=1, le=10)
    available_recheck_enabled: bool | None = None
    available_recheck_interval: float | None = Field(default=None, ge=10.0)
    pattern_slow_interval: float | None = Field(default=None, ge=1.0)
    pattern_fast_interval: float | None = Field(default=None, ge=0.1)
    pattern_window_start_minute: int | None = Field(default=None, ge=0, le=59)
    pattern_window_end_minute: int | None = Field(default=None, ge=0, le=59)


class DomainBulkCreateRequest(BaseModel):
    domains: list[str]
    check_interval: float | None = Field(default=None, ge=0.2)
    burst_check_interval: float | None = Field(default=None, ge=0.1)
    scheduler_mode: str | None = None
    confirmation_threshold: int | None = Field(default=None, ge=1, le=10)
    available_recheck_enabled: bool | None = None
    available_recheck_interval: float | None = Field(default=None, ge=10.0)
    pattern_slow_interval: float | None = Field(default=None, ge=1.0)
    pattern_fast_interval: float | None = Field(default=None, ge=0.1)
    pattern_window_start_minute: int | None = Field(default=None, ge=0, le=59)
    pattern_window_end_minute: int | None = Field(default=None, ge=0, le=59)


class DomainUpdateRequest(BaseModel):
    is_active: bool | None = None
    manual_burst: bool | None = None
    scheduler_mode: str | None = None
    check_interval: float | None = Field(default=None, ge=0.2)
    burst_check_interval: float | None = Field(default=None, ge=0.1)
    confirmation_threshold: int | None = Field(default=None, ge=1, le=10)
    available_recheck_enabled: bool | None = None
    available_recheck_interval: float | None = Field(default=None, ge=10.0)
    pattern_slow_interval: float | None = Field(default=None, ge=1.0)
    pattern_fast_interval: float | None = Field(default=None, ge=0.1)
    pattern_window_start_minute: int | None = Field(default=None, ge=0, le=59)
    pattern_window_end_minute: int | None = Field(default=None, ge=0, le=59)
    check_mode: str | None = None


class DomainResponse(BaseModel):
    id: int
    domain: str
    zone: str
    status: str
    is_active: bool
    manual_burst: bool
    scheduler_mode: str
    check_interval: float
    burst_check_interval: float
    confirmation_threshold: int
    available_recheck_enabled: bool
    available_recheck_interval: float
    pattern_slow_interval: float
    pattern_fast_interval: float
    pattern_window_start_minute: int
    pattern_window_end_minute: int
    check_mode: str
    last_check_at: datetime | None
    last_cycle_started_at: datetime | None
    worker_heartbeat_at: datetime | None
    last_success_at: datetime | None
    available_at: datetime | None
    last_seen_owner: str | None
    last_seen_rdap_status: str | None
    last_owner_change_at: datetime | None
    available_confirmations: int
    consecutive_failures: int
    alert_sent_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DomainImportResponse(BaseModel):
    inserted: list[DomainResponse]
    skipped: list[str]
