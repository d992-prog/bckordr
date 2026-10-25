from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.schemas.control import (
    VpnAccessKeyResponse,
    VpnCustomerResponse,
    VpnSubscriptionResponse,
)


class AbuseDeviceContextResponse(BaseModel):
    device_id: int
    customer: VpnCustomerResponse
    subscription: VpnSubscriptionResponse
    access_key: VpnAccessKeyResponse | None


class AbuseIncidentResponse(BaseModel):
    id: int
    device_id: int
    customer_id: int
    display_name: str
    started_at: datetime
    last_observed_at: datetime
    peak_source_count: int
    acknowledged_at: datetime | None


class AcknowledgeAbuseIncidentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
