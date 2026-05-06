from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AdminAuditLog


async def add_audit_log(
    session: AsyncSession,
    *,
    actor_user_id: int | None,
    target_user_id: int | None,
    action: str,
    details: str | None = None,
) -> None:
    session.add(
        AdminAuditLog(
            actor_user_id=actor_user_id,
            target_user_id=target_user_id,
            action=action,
            details=details,
        )
    )
