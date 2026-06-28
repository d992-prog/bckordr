from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Domain Drop Catcher Control"
    api_prefix: str = "/api"
    db_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/frdrop",
        alias="DB_URL",
    )
    telegram_token: str = Field(default="", alias="TELEGRAM_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")
    session_secret_key: str = Field(default="change-me-session-secret", alias="SESSION_SECRET_KEY")
    session_cookie_secure: bool = Field(default=False, alias="SESSION_COOKIE_SECURE")
    owner_login: str = Field(default="france_admin", alias="OWNER_LOGIN")
    owner_password: str = Field(default="", alias="OWNER_PASSWORD")
    rdap_base_url: str = Field(default="https://rdap.nic.fr/domain/", alias="RDAP_BASE_URL")
    request_timeout: float = 5.0
    dns_timeout_seconds: float = Field(default=2.5, alias="DNS_TIMEOUT_SECONDS")
    dns_fallback_nameservers: str = Field(default="1.1.1.1,8.8.8.8", alias="DNS_FALLBACK_NAMESERVERS")
    rdap_timeout_seconds: float = Field(default=4.0, alias="RDAP_TIMEOUT_SECONDS")
    worker_cycle_timeout_seconds: float = Field(default=10.0, alias="WORKER_CYCLE_TIMEOUT_SECONDS")
    worker_supervisor_interval_seconds: int = Field(
        default=15,
        alias="WORKER_SUPERVISOR_INTERVAL_SECONDS",
    )
    control_scheduler_interval_seconds: float = Field(
        default=1.0,
        alias="CONTROL_SCHEDULER_INTERVAL_SECONDS",
    )
    worker_stall_threshold_seconds: int = Field(default=45, alias="WORKER_STALL_THRESHOLD_SECONDS")
    discovery_enabled: bool = Field(default=True, alias="DISCOVERY_ENABLED")
    discovery_scheduler_interval_seconds: float = Field(default=5.0, alias="DISCOVERY_SCHEDULER_INTERVAL_SECONDS")
    discovery_batch_size: int = Field(default=10, alias="DISCOVERY_BATCH_SIZE")
    discovery_timeout_seconds: float = Field(default=5.0, alias="DISCOVERY_TIMEOUT_SECONDS")
    discovery_rdap_bootstrap_url: str = Field(
        default="https://data.iana.org/rdap/dns.json",
        alias="DISCOVERY_RDAP_BOOTSTRAP_URL",
    )
    max_proxy_attempts_per_cycle: int = Field(default=3, alias="MAX_PROXY_ATTEMPTS_PER_CYCLE")
    normal_check_interval: float = Field(default=1.5, alias="NORMAL_CHECK_INTERVAL")
    burst_check_interval: float = Field(default=0.35, alias="BURST_CHECK_INTERVAL")
    pattern_window_start_minute: int = Field(default=31, alias="PATTERN_WINDOW_START_MINUTE")
    pattern_window_end_minute: int = Field(default=34, alias="PATTERN_WINDOW_END_MINUTE")
    pattern_slow_interval: float = Field(default=60.0, alias="PATTERN_SLOW_INTERVAL")
    pattern_fast_interval: float = Field(default=0.5, alias="PATTERN_FAST_INTERVAL")
    available_recheck_interval: float = Field(default=1800.0, alias="AVAILABLE_RECHECK_INTERVAL")
    available_capture_watch_seconds: int = Field(
        default=15,
        alias="AVAILABLE_CAPTURE_WATCH_SECONDS",
    )
    available_capture_watch_interval: float = Field(
        default=0.5,
        alias="AVAILABLE_CAPTURE_WATCH_INTERVAL",
    )
    available_confirmation_threshold: int = Field(
        default=3,
        alias="AVAILABLE_CONFIRMATION_THRESHOLD",
    )
    proxy_fail_threshold: int = Field(default=3, alias="PROXY_FAIL_THRESHOLD")
    dead_proxy_retry_seconds: int = Field(default=300, alias="DEAD_PROXY_RETRY_SECONDS")
    default_pending_message: str = "Ваша учетная запись ожидает одобрения администратора или активации промокодом."
    login_rate_limit_attempts: int = Field(default=5, alias="LOGIN_RATE_LIMIT_ATTEMPTS")
    login_lock_minutes: int = Field(default=15, alias="LOGIN_LOCK_MINUTES")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")
    worker_runtime_allowlist_path: str | None = Field(default=None, alias="WORKER_RUNTIME_ALLOWLIST_PATH")
    worker_runtime_allowlist_reload_command: str | None = Field(
        default=None,
        alias="WORKER_RUNTIME_ALLOWLIST_RELOAD_COMMAND",
    )
    max_upload_bytes: int = 5 * 1024 * 1024

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    @property
    def cors_origin_list(self) -> list[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def dns_fallback_nameserver_list(self) -> list[str]:
        return [item.strip() for item in self.dns_fallback_nameservers.split(",") if item.strip()]

    @property
    def frontend_dist_dir(self) -> Path:
        return Path(__file__).resolve().parents[3] / "frontend" / "dist"


@lru_cache
def get_settings() -> Settings:
    return Settings()
