import asyncio
import json
from datetime import timedelta

import pytest
import pytest_asyncio
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base, utcnow
from app.db.models import AppSetting
from app.services.app_settings import get_app_setting, set_app_setting


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cabinet.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, None),
        ("", None),
        ("  ", None),
        ("@veltrix_support", "https://t.me/veltrix_support"),
        ("https://t.me/veltrix_support", "https://t.me/veltrix_support"),
    ],
)
def test_support_contact_is_canonical(value, expected):
    from app.schemas.vpn_cabinet import CabinetSettings

    assert CabinetSettings(support_contact=value).support_contact == expected


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "http://t.me/veltrix_support",
        "tg://resolve?domain=veltrix_support",
        "https://evil.example/veltrix_support",
        "https://t.me.evil.example/veltrix_support",
        "https://user:password@t.me/veltrix_support",
        "https://t.me/veltrix_support/extra",
        "https://t.me/veltrix_support?start=secret",
        "https://t.me/veltrix_support#fragment",
        "https://t.me:443/veltrix_support",
        "https://t.me/+invite",
        "@bad",
        "@1wrong_name",
        "@bad-name",
        "veltrix_support",
        "@" + "a" * 33,
        "@hello\nworld",
        123,
    ],
)
def test_support_contact_rejects_other_destinations(value):
    from app.schemas.vpn_cabinet import CabinetSettings

    with pytest.raises(ValidationError):
        CabinetSettings(support_contact=value)


@pytest.mark.parametrize("prefix", ["@", "https://t.me/"])
@pytest.mark.parametrize(
    "name",
    [
        "share",
        "JoinChat",
        "proxy",
        "socks",
        "login",
        "invoice",
        "contact",
        "boost",
        "addemoji",
        "addstickers",
        "addtheme",
        "addstyle",
        "addlist",
        "setlanguage",
        "confirmphone",
        "giftcode",
        "auction",
        "newbot",
        "oauth",
    ],
)
def test_support_contact_rejects_reserved_telegram_routes(prefix, name):
    from app.schemas.vpn_cabinet import CabinetSettings

    with pytest.raises(ValidationError):
        CabinetSettings(support_contact=prefix + name)


def notice(now):
    return {
        "title": "Работы",
        "body": "Первая строка\nВторая\tстрока",
        "starts_at": now,
        "ends_at": now + timedelta(hours=1),
    }


@pytest.mark.parametrize(
    "field, value",
    [
        ("revision", -1),
        ("revision", True),
        ("revision", "1"),
        ("reminders_enabled", "true"),
        ("unknown", True),
    ],
)
def test_settings_fields_are_strict(field, value):
    from app.schemas.vpn_cabinet import CabinetSettings

    with pytest.raises(ValidationError):
        CabinetSettings(**{field: value})


@pytest.mark.parametrize(
    "field, value",
    [
        ("title", ""),
        ("title", "x" * 101),
        ("body", ""),
        ("body", "x" * 1001),
        ("title", "Bad\x00text"),
        ("body", "Bad\x7ftext"),
        ("body", "Bad\x85text"),
        ("starts_at", "2026-10-26T12:00:00"),
        ("ends_at", "2026-10-26T13:00:00"),
    ],
)
def test_notice_rejects_invalid_plain_text_or_naive_time(field, value):
    from app.schemas.vpn_cabinet import CabinetSettings

    with pytest.raises(ValidationError):
        CabinetSettings(notice={**notice(utcnow()), field: value})


def test_notice_has_a_positive_aware_window():
    from app.schemas.vpn_cabinet import CabinetSettings

    now = utcnow()
    assert CabinetSettings(notice=notice(now)).notice.body.endswith("\tстрока")
    for end in (now, now - timedelta(seconds=1)):
        with pytest.raises(ValidationError):
            CabinetSettings(notice={**notice(now), "ends_at": end})


@pytest.mark.asyncio
async def test_missing_and_invalid_persisted_settings_fail_closed_without_writes(
    sessions,
):
    from app.schemas.vpn_cabinet import CabinetSettings
    from app.services.vpn_cabinet_settings import (
        CABINET_SETTINGS_KEY,
        get_cabinet_settings,
    )

    async with sessions() as db:
        assert await get_cabinet_settings(db) == CabinetSettings()
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 0
        for raw in (
            "not json",
            "[]",
            "null",
            '{"support_contact":"https://evil.example"}',
            '{"revision":-1}',
            '{"reminders_enabled":true,"reminders_enabled_at":"bad"}',
        ):
            await set_app_setting(db, CABINET_SETTINGS_KEY, raw)
            assert await get_cabinet_settings(db) == CabinetSettings()
            assert await get_app_setting(db, CABINET_SETTINGS_KEY) == raw


@pytest.mark.asyncio
async def test_revisions_and_reminder_enable_time_survive_edits_and_reset(sessions):
    from app.schemas.vpn_cabinet import CabinetSettings
    from app.services.vpn_cabinet_settings import (
        CABINET_SETTINGS_KEY,
        CabinetRevisionConflict,
        get_cabinet_settings,
        get_reminders_enabled_at,
        update_cabinet_settings,
    )

    now = utcnow()
    async with sessions() as db:
        saved = await update_cabinet_settings(
            db, CabinetSettings(reminders_enabled=True), now=now
        )
        await db.commit()
        assert saved.revision == 1
        assert await get_reminders_enabled_at(db) == now
        raw = json.loads(await get_app_setting(db, CABINET_SETTINGS_KEY))
        assert raw["reminders_enabled_at"]
        assert "reminders_enabled_at" not in saved.model_dump()
        with pytest.raises(CabinetRevisionConflict):
            await update_cabinet_settings(db, CabinetSettings(), now=now)
        await db.rollback()
        assert (await get_cabinet_settings(db)).revision == 1
        saved = await update_cabinet_settings(
            db,
            saved.model_copy(update={"support_contact": None}),
            now=now + timedelta(hours=1),
        )
        await db.commit()
        assert saved.revision == 2 and await get_reminders_enabled_at(db) == now
        saved = await update_cabinet_settings(
            db, saved.model_copy(update={"reminders_enabled": False}), now=now
        )
        await db.commit()
        assert await get_reminders_enabled_at(db) is None
        enabled_again = now + timedelta(days=1)
        await update_cabinet_settings(
            db, saved.model_copy(update={"reminders_enabled": True}), now=enabled_again
        )
        await db.commit()
        assert await get_reminders_enabled_at(db) == enabled_again
        assert await db.scalar(select(func.count()).select_from(AppSetting)) == 1


@pytest.mark.asyncio
async def test_concurrent_first_writers_have_one_winner(sessions):
    from app.schemas.vpn_cabinet import CabinetSettings
    from app.services.vpn_cabinet_settings import (
        CabinetRevisionConflict,
        get_cabinet_settings,
        update_cabinet_settings,
    )

    async def write():
        async with sessions() as db:
            try:
                saved = await update_cabinet_settings(
                    db, CabinetSettings(), now=utcnow()
                )
                await db.commit()
                return saved.revision
            except CabinetRevisionConflict:
                await db.rollback()
                return "conflict"

    assert sorted(await asyncio.gather(write(), write()), key=str) == [1, "conflict"]
    async with sessions() as db:
        assert (await get_cabinet_settings(db)).revision == 1
