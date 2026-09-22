from dataclasses import dataclass

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings

settings = get_settings()

engine = create_async_engine(
    settings.db_url,
    hide_parameters=True,
    pool_pre_ping=True,
    future=True,
)
AsyncSessionLocal = async_sessionmaker(
    engine,
    expire_on_commit=False,
    class_=AsyncSession,
)


@dataclass(frozen=True)
class VpnControlDatabase:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def vpn_control_engine_options(settings: Settings) -> dict[str, object]:
    options: dict[str, object] = {
        "hide_parameters": True,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.db_url.startswith(("postgresql+asyncpg://", "postgresql://")):
        options["connect_args"] = {
            "command_timeout": settings.vpn_control_db_command_timeout_seconds,
            "server_settings": {
                "statement_timeout": str(
                    settings.vpn_control_db_statement_timeout_ms
                )
            },
        }
    return options


def create_vpn_control_database(settings: Settings) -> VpnControlDatabase:
    if (
        settings.vpn_control_db_command_timeout_seconds <= 0
        or settings.vpn_control_db_statement_timeout_ms <= 0
    ):
        raise ValueError("vpn_control_database_timeout_invalid")
    dedicated_engine = create_async_engine(
        settings.db_url,
        **vpn_control_engine_options(settings),
    )
    return VpnControlDatabase(
        engine=dedicated_engine,
        session_factory=async_sessionmaker(
            dedicated_engine,
            expire_on_commit=False,
            class_=AsyncSession,
        ),
    )


async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
