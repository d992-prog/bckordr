from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class WorkerSettings(BaseSettings):
    control_base_url: str = Field(alias="CONTROL_BASE_URL")
    worker_id: int = Field(alias="WORKER_ID")
    control_token: str = Field(alias="CONTROL_TOKEN")
    poll_interval_seconds: float = Field(default=2.0, alias="POLL_INTERVAL_SECONDS")
    heartbeat_interval_seconds: float = Field(default=5.0, alias="HEARTBEAT_INTERVAL_SECONDS")
    request_timeout_seconds: float = Field(default=10.0, alias="REQUEST_TIMEOUT_SECONDS")
    connect_timeout_seconds: float = Field(default=2.0, alias="CONNECT_TIMEOUT_SECONDS")
    simulate_mode: bool = Field(default=False, alias="SIMULATE_MODE")
    simulate_latency_ms: int = Field(default=20, alias="SIMULATE_LATENCY_MS")
    simulate_jitter_ms: int = Field(default=10, alias="SIMULATE_JITTER_MS")
    simulate_success_rate: float = Field(default=0.0, alias="SIMULATE_SUCCESS_RATE", ge=0.0, le=1.0)
    simulate_success_status_code: int = Field(default=200, alias="SIMULATE_SUCCESS_STATUS_CODE")
    simulate_failure_status_code: int = Field(default=503, alias="SIMULATE_FAILURE_STATUS_CODE")
    simulate_random_seed: int = Field(default=12345, alias="SIMULATE_RANDOM_SEED")
    gandi_create_status_poll_enabled: bool = Field(default=False, alias="GANDI_CREATE_STATUS_POLL_ENABLED")
    gandi_status_poll_interval_seconds: float = Field(default=0.5, alias="GANDI_STATUS_POLL_INTERVAL_SECONDS")
    gandi_status_poll_max_attempts: int = Field(default=8, alias="GANDI_STATUS_POLL_MAX_ATTEMPTS")
    registration_concurrency_multiplier: float = Field(
        default=2.0,
        alias="REGISTRATION_CONCURRENCY_MULTIPLIER",
        ge=1.0,
        le=32.0,
    )
    registration_max_concurrency: int = Field(default=64, alias="REGISTRATION_MAX_CONCURRENCY", ge=1, le=512)
    max_idle_backoff_seconds: float = Field(default=10.0, alias="MAX_IDLE_BACKOFF_SECONDS")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )
