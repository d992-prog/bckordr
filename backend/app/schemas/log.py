from datetime import datetime

from pydantic import BaseModel, ConfigDict


class LogResponse(BaseModel):
    id: int
    domain_id: int | None
    event_type: str
    message: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
