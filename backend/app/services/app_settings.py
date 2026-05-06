from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AppSetting

DIAGNOSTIC_TELEGRAM_TOKEN_KEY = "diagnostic_telegram_token"
DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY = "diagnostic_telegram_chat_id"


async def get_app_setting(session: AsyncSession, key: str) -> str | None:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def set_app_setting(session: AsyncSession, key: str, value: str | None) -> AppSetting:
    result = await session.execute(select(AppSetting).where(AppSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting is None:
        setting = AppSetting(key=key, value=value)
        session.add(setting)
    else:
        setting.value = value
    await session.flush()
    return setting


async def get_diagnostic_telegram_settings(session: AsyncSession) -> tuple[str | None, str | None]:
    token = await get_app_setting(session, DIAGNOSTIC_TELEGRAM_TOKEN_KEY)
    chat_id = await get_app_setting(session, DIAGNOSTIC_TELEGRAM_CHAT_ID_KEY)
    return token, chat_id
