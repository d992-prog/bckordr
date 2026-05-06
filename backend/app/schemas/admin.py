from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class AdminUserUpdateRequest(BaseModel):
    status: str | None = None
    status_message: str | None = None
    role: str | None = None
    language: str | None = None
    max_domains: int | None = Field(default=None, ge=1)
    access_expires_at: datetime | None = None


class AdminUserResponse(BaseModel):
    id: int
    username: str
    role: str
    status: str
    language: str
    max_domains: int | None
    access_expires_at: datetime | None
    status_message: str | None
    last_login_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime
    domain_count: int
    proxy_count: int


class ManualUserCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=6, max_length=128)
    role: str = "user"
    status: str = "approved"
    language: str = "ru"
    max_domains: int | None = Field(default=None, ge=1)
    access_expires_at: datetime | None = None
    status_message: str | None = None


class GrantAccessRequest(BaseModel):
    duration_seconds: int = Field(gt=0)


class PromoCodeCreateRequest(BaseModel):
    code: str = Field(min_length=4, max_length=64)
    duration_seconds: int = Field(gt=0)
    max_activations: int | None = Field(default=None, gt=0)
    expires_at: datetime | None = None
    is_active: bool = True


class PromoCodeResponse(BaseModel):
    id: int
    code: str
    duration_seconds: int
    max_activations: int | None
    activation_count: int
    is_active: bool
    expires_at: datetime | None
    created_by_user_id: int | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminAuditLogResponse(BaseModel):
    id: int
    actor_user_id: int | None
    target_user_id: int | None
    action: str
    details: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiagnosticTelegramSettingsRequest(BaseModel):
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class DiagnosticTelegramSettingsResponse(BaseModel):
    telegram_token: str | None
    telegram_chat_id: str | None
