from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class RegisterRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")
    password: str = Field(min_length=6, max_length=128)
    language: str = "ru"


class LoginRequest(BaseModel):
    username: str
    password: str
    remember_me: bool = False


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str = Field(min_length=6, max_length=128)


class TelegramSettingsRequest(BaseModel):
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


class PromoApplyRequest(BaseModel):
    code: str


class UserResponse(BaseModel):
    id: int
    username: str
    role: str
    status: str
    language: str
    max_domains: int | None
    access_expires_at: datetime | None
    status_message: str | None
    telegram_token: str | None
    telegram_chat_id: str | None
    last_login_at: datetime | None
    deleted_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SessionResponse(BaseModel):
    user: UserResponse
    has_feature_access: bool
