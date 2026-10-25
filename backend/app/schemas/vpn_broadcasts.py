from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.vpn_broadcasts import validate_body


class BroadcastBodyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(strict=True)

    @field_validator("body")
    @classmethod
    def canonical_body(cls, value: str) -> str:
        return validate_body(value)


class BroadcastEmptyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BroadcastConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(strict=True, ge=1)
    expected_recipient_count: int = Field(strict=True, ge=0)
    confirm: Literal[True]

    @field_validator("confirm", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> bool:
        if value is not True:
            raise ValueError("vpn_broadcast_confirmation_required")
        return True


class BroadcastConfigResponse(BaseModel):
    enabled: bool
    registry_state: Literal[
        "ready", "history_unverified", "bot_changed", "unconfigured"
    ]
    eligible_recipients: int | None
    explanation: str


class BroadcastCounts(BaseModel):
    recipient_count: int
    pending: int
    claimed: int
    sending: int
    sent: int
    failed: int
    skipped: int
    uncertain: int
    cancelled: int


class BroadcastSummary(BaseModel):
    id: int
    revision: int
    state: Literal["draft", "running", "completed", "cancelled", "failed"]
    created_at: datetime
    confirmed_at: datetime | None
    cancelled_at: datetime | None


class BroadcastDetail(BroadcastSummary):
    body: str
    preview_revision: int | None
    preview_recipient_count: int | None
    previewed_at: datetime | None
    counts: BroadcastCounts
