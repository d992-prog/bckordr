from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Log


async def add_log(
    session: AsyncSession,
    *,
    owner_id: int | None = None,
    domain_id: int | None,
    event_type: str,
    message: str,
) -> None:
    session.add(
        Log(
            owner_id=owner_id,
            domain_id=domain_id,
            event_type=event_type,
            message=message,
        )
    )
