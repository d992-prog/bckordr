from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ProxyCreateRequest(BaseModel):
    proxy_url: str


class ProxyResponse(BaseModel):
    id: int
    host: str
    port: int
    login: str | None
    password: str | None
    type: str
    status: str
    fail_count: int
    last_used: datetime | None
    created_at: datetime
    display_url: str

    model_config = ConfigDict(from_attributes=True)
