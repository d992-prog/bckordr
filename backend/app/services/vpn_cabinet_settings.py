from datetime import datetime

from pydantic import AwareDatetime, ValidationError, model_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting
from app.schemas.vpn_cabinet import CabinetSettings
from app.services.app_settings import get_app_setting, set_app_setting

CABINET_SETTINGS_KEY = "vpn_cabinet_settings_v1"


class CabinetRevisionConflict(ValueError):
    pass


class _StoredCabinetSettings(CabinetSettings):
    reminders_enabled_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def reminder_gate(self):
        if self.reminders_enabled != (self.reminders_enabled_at is not None):
            raise ValueError("vpn_cabinet_reminder_gate_invalid")
        return self


def _parse(raw: str | None) -> _StoredCabinetSettings:
    try:
        return (
            _StoredCabinetSettings.model_validate_json(raw)
            if raw is not None
            else _StoredCabinetSettings()
        )
    except (ValidationError, ValueError):
        return _StoredCabinetSettings()


def _public(value: _StoredCabinetSettings) -> CabinetSettings:
    return CabinetSettings.model_validate(
        value.model_dump(exclude={"reminders_enabled_at"})
    )


async def get_cabinet_settings(db: AsyncSession) -> CabinetSettings:
    return _public(_parse(await get_app_setting(db, CABINET_SETTINGS_KEY)))


async def get_reminders_enabled_at(db: AsyncSession) -> datetime | None:
    return _parse(await get_app_setting(db, CABINET_SETTINGS_KEY)).reminders_enabled_at


async def update_cabinet_settings(
    db: AsyncSession,
    payload: CabinetSettings,
    *,
    now: datetime,
) -> CabinetSettings:
    # The unique insert also serializes simultaneous first writes before a row exists.
    insert = pg_insert if db.bind.dialect.name == "postgresql" else sqlite_insert
    await db.execute(
        insert(AppSetting)
        .values(
            key=CABINET_SETTINGS_KEY, value=_StoredCabinetSettings().model_dump_json()
        )
        .on_conflict_do_nothing(index_elements=["key"])
    )
    row = await db.scalar(
        select(AppSetting)
        .where(AppSetting.key == CABINET_SETTINGS_KEY)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current = _parse(row.value)
    if payload.revision != current.revision:
        raise CabinetRevisionConflict("vpn_cabinet_settings_revision_conflict")
    enabled_at = (
        (current.reminders_enabled_at or now) if payload.reminders_enabled else None
    )
    updated = _StoredCabinetSettings(
        **{**payload.model_dump(), "revision": current.revision + 1},
        reminders_enabled_at=enabled_at,
    )
    await set_app_setting(db, CABINET_SETTINGS_KEY, updated.model_dump_json())
    return _public(updated)
