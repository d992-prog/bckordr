import re
import unicodedata
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from app.schemas.vpn_usage import DeviceUsageRow, UsageTotals

# These t.me paths perform Telegram actions rather than opening a username.
_TELEGRAM_ROUTES = {
    "addemoji",
    "addlist",
    "addstickers",
    "addstyle",
    "addtheme",
    "auction",
    "auth",
    "boost",
    "call",
    "confirmphone",
    "contact",
    "giftcode",
    "invoice",
    "joinchat",
    "login",
    "newbot",
    "oauth",
    "proxy",
    "setlanguage",
    "share",
    "socks",
}


class CabinetNotice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(strict=True, min_length=1, max_length=100)
    body: str = Field(strict=True, min_length=1, max_length=1000)
    starts_at: AwareDatetime
    ends_at: AwareDatetime

    @field_validator("title", "body")
    @classmethod
    def plain_text(cls, value: str) -> str:
        if any(
            char not in "\n\t" and unicodedata.category(char) in {"Cc", "Cf", "Cs"}
            for char in value
        ):
            raise ValueError("vpn_cabinet_notice_invalid_text")
        return value

    @model_validator(mode="after")
    def positive_window(self):
        if self.ends_at <= self.starts_at:
            raise ValueError("vpn_cabinet_notice_invalid_window")
        return self


class CabinetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(default=0, strict=True, ge=0)
    support_contact: str | None = Field(default=None, strict=True)
    notice: CabinetNotice | None = None
    reminders_enabled: bool = Field(default=False, strict=True)

    @field_validator("support_contact")
    @classmethod
    def canonical_support_contact(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        match = re.fullmatch(
            r"(?:@|https://t\.me/)([A-Za-z][A-Za-z0-9_]{4,31})", value.strip()
        )
        if match is None or match[1].lower() in _TELEGRAM_ROUTES:
            raise ValueError("vpn_cabinet_support_contact_invalid")
        return "https://t.me/" + match[1]


class PortalUsageResponse(UsageTotals):
    days: Literal[1, 7, 30]
    devices: list[DeviceUsageRow]
