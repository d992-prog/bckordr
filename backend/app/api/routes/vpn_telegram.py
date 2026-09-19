from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import get_db
from app.services.vpn_telegram import process_telegram_update

router = APIRouter(prefix="/vpn-telegram", tags=["vpn-telegram"])


@router.post("/webhook/{secret}")
async def vpn_telegram_webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    settings = get_settings()
    if not settings.vpn_telegram_bot_token or not settings.vpn_telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="VPN Telegram bot is not configured")
    if not secrets.compare_digest(secret, settings.vpn_telegram_webhook_secret):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    if settings.vpn_telegram_secret_token and not secrets.compare_digest(
        x_telegram_bot_api_secret_token or "",
        settings.vpn_telegram_secret_token,
    ):
        raise HTTPException(status_code=403, detail="Invalid Telegram secret token")

    try:
        payload = await request.json()
    except ValueError:
        return {"ok": True, "processed": False, "duplicate": False}
    if not isinstance(payload, dict):
        return {"ok": True, "processed": False, "duplicate": False}

    result = await process_telegram_update(db, payload, settings)
    await db.commit()
    return {"ok": True, **result}
