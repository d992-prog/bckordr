from __future__ import annotations

import logging
from datetime import datetime

import httpx

from app.core.config import Settings

logger = logging.getLogger(__name__)


class TelegramNotifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    async def send_message(
        self,
        message: str,
        *,
        token: str,
        chat_id: str,
    ) -> None:
        if not token or not chat_id:
            return

        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
        }

        async with httpx.AsyncClient(timeout=self.settings.request_timeout) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()

    async def send_domain_available(
        self,
        domain: str,
        checked_at: datetime,
        *,
        token: str,
        chat_id: str,
    ) -> None:
        if not token or not chat_id:
            return

        message = (
            "\U0001F680 DOMAIN AVAILABLE\n\n"
            f"Domain: {domain}\n"
            f"Time: {checked_at.isoformat()}"
        )
        try:
            await self.send_message(message, token=token, chat_id=chat_id)
        except Exception:
            logger.exception("Failed to deliver Telegram alert for %s", domain)

    async def send_diagnostic(
        self,
        title: str,
        details: str,
        *,
        token: str,
        chat_id: str,
    ) -> None:
        message = f"\U0001F6E0 {title}\n\n{details}"
        try:
            await self.send_message(message, token=token, chat_id=chat_id)
        except Exception:
            logger.exception("Failed to deliver diagnostic Telegram message: %s", title)
