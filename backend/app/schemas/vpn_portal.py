from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.vpn_display import validate_display_name


class PortalMe(BaseModel):
    display_name: str
    csrf_token: str


class PortalTrial(BaseModel):
    state: Literal["disabled", "available", "capacity_paused", "preparing", "active", "used"]
    duration_days: int
    profile_limit: int
    subscription_id: int | None
    access_key_id: int | None
    expires_at: datetime | None


class PortalSubscription(BaseModel):
    id: int
    service_name: str = "Veltrix VPN"
    state: str
    starts_at: datetime | None
    expires_at: datetime | None
    profile_limit: int
    profiles_used: int
    traffic_limit_gb_per_profile: int | None


class PortalProfile(BaseModel):
    id: int
    subscription_id: int
    display_name: str
    state: str
    can_connect: bool


class PortalConnection(BaseModel):
    uri: str


class RenamePortalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_display_name(value)


class MiniAppLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")

    init_data: str = Field(min_length=1, max_length=16384)
