from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.db.models import User, UserSession
from app.db.session import get_db
from app.services.security import SESSION_COOKIE_NAME, hash_session_token, user_has_feature_access


async def get_current_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    raw_token = request.cookies.get(SESSION_COOKIE_NAME)
    if not raw_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")

    token_hash = hash_session_token(raw_token)
    result = await db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash, UserSession.revoked_at.is_(None))
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid session")

    session, user = row
    now = utcnow()
    if session.expires_at <= now:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    session.last_used_at = now
    await db.commit()
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role not in {"owner", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return user


async def require_feature_access(user: User = Depends(get_current_user)) -> User:
    if not user_has_feature_access(user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=user.status_message or "Account access is restricted",
        )
    return user
