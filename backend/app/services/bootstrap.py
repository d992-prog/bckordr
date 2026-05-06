from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.db.models import User
from app.services.security import hash_password


async def ensure_owner_account(
    session_factory: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> None:
    if not settings.owner_login or not settings.owner_password:
        return

    async with session_factory() as session:
        result = await session.execute(select(User).where(User.username == settings.owner_login))
        owner = result.scalar_one_or_none()
        if owner is None:
            session.add(
                User(
                    username=settings.owner_login,
                    password_hash=hash_password(settings.owner_password),
                    role="owner",
                    status="approved",
                    language="ru",
                )
            )
        else:
            owner.role = "owner"
            owner.status = "approved"
            owner.deleted_at = None
            owner.status_message = None
        await session.commit()
