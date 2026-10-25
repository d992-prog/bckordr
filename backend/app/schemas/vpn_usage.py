from datetime import datetime
from typing import Literal

from pydantic import BaseModel


class UsageTotals(BaseModel):
    upload_bytes: int | None
    download_bytes: int | None
    measured_since: datetime | None
    last_sample_at: datetime | None
    coverage: Literal["none", "partial", "current", "stale", "unavailable"]


class NodeUsageRow(UsageTotals):
    worker_id: int
    active_devices: int
    occupied_profiles: int
    max_active_profiles: int | None
    cpu_load: float | None
    ram_usage_percent: float | None


class DeviceUsageRow(UsageTotals):
    device_id: int
    display_name: str


class CustomerUsageResponse(UsageTotals):
    customer_id: int
    days: Literal[1, 7, 30]
    devices: list[DeviceUsageRow]
