from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.vpn_display import validate_display_name


class PortalMe(BaseModel):
    display_name: str
    csrf_token: str


class PortalPlan(BaseModel):
    id: int
    name: str
    description: str | None
    duration_days: int | None
    traffic_limit_gb: int | None
    max_devices: int
    price_amount: float
    currency: str
    is_trial: bool


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


class PortalDevice(PortalProfile):
    legacy_profile_id: int | None


class PortalDeviceLocation(BaseModel):
    endpoint_id: int
    name: str
    country_code: str | None
    is_published: bool


class PortalDeviceLocations(BaseModel):
    current: PortalDeviceLocation | None
    pending_endpoint_id: int | None
    selection_state: Literal["ready", "preparing", "failed", "verification_required", "unavailable"]
    available: list[PortalDeviceLocation]


class SelectPortalDeviceLocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint_id: int = Field(gt=0)


class PortalDeviceConnection(BaseModel):
    subscription_url: str
    direct_uri: str


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
