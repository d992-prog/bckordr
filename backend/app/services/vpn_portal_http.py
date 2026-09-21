from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse

from app.core.config import Settings
from app.services.vpn_portal_auth import (
    BINDING_COOKIE,
    delete_binding_cookie,
    identity_allowed,
    public_origin,
)


MAX_MINI_APP_BODY_BYTES = 16 * 1024
logger = logging.getLogger(__name__)


def is_portal_path(path: str, portal_prefix: str) -> bool:
    normalized = portal_prefix.rstrip("/")
    return path == normalized or path.startswith(normalized + "/")


def portal_capabilities(settings: Settings) -> dict[str, bool]:
    try:
        public_origin(settings)
    except ValueError:
        origin_valid = False
    else:
        origin_valid = True

    access_configured = settings.vpn_portal_public_access or any(
        identity_allowed(settings, candidate.strip())
        for candidate in settings.vpn_portal_allowed_telegram_ids.split(",")
        if candidate.strip()
    )
    base_enabled = bool(
        settings.vpn_portal_enabled and origin_valid and access_configured
    )
    browser_login_enabled = bool(
        base_enabled
        and settings.vpn_portal_oidc_client_id
        and settings.vpn_portal_oidc_client_secret
    )
    mini_app_enabled = bool(base_enabled and settings.vpn_telegram_bot_token)
    return {
        "enabled": browser_login_enabled or mini_app_enabled,
        "browser_login_enabled": browser_login_enabled,
        "mini_app_enabled": mini_app_enabled,
    }


class ScopedControlCorsMiddleware(CORSMiddleware):
    def __init__(self, app, *, portal_prefix: str, **kwargs: Any) -> None:
        super().__init__(app, **kwargs)
        self.portal_prefix = portal_prefix.rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and is_portal_path(
            scope.get("path", ""), self.portal_prefix
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)


class PortalHttpMiddleware:
    """Apply portal-only response caching and pre-parse Mini App guards."""

    def __init__(self, app, *, portal_prefix: str) -> None:
        self.app = app
        self.portal_prefix = portal_prefix.rstrip("/")
        self.mini_app_path = self.portal_prefix + "/auth/mini-app"
        self.telegram_callback_path = (
            self.portal_prefix + "/auth/telegram/callback"
        )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not is_portal_path(
            scope.get("path", ""), self.portal_prefix
        ):
            await self.app(scope, receive, send)
            return

        response_started = False
        response_complete = False

        async def no_store_send(message: dict[str, Any]) -> None:
            nonlocal response_complete, response_started
            if message["type"] == "http.response.start":
                headers = [
                    (name, value)
                    for name, value in message.get("headers", [])
                    if name.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            if message["type"] == "http.response.start":
                response_started = True
            elif message["type"] == "http.response.body" and not message.get(
                "more_body", False
            ):
                response_complete = True
            await send(message)

        try:
            if scope.get("path") == self.mini_app_path and scope.get("method") == "POST":
                rejection = self._mini_app_rejection(scope)
                if rejection is not None:
                    await rejection(scope, receive, no_store_send)
                    return
                body, body_error = await self._read_bounded_body(receive)
                if body_error is not None:
                    response = JSONResponse(
                        {"detail": "customer_request_rejected"}, status_code=body_error
                    )
                    await response(scope, receive, no_store_send)
                    return

                delivered = False

                async def replay_receive() -> dict[str, Any]:
                    nonlocal delivered
                    if delivered:
                        return {"type": "http.disconnect"}
                    delivered = True
                    return {"type": "http.request", "body": body, "more_body": False}

                receive = replay_receive

            await self.app(scope, receive, no_store_send)
        except Exception:
            if response_started:
                logger.error("VPN portal response terminated after an internal failure")
                if not response_complete:
                    try:
                        await send(
                            {
                                "type": "http.response.body",
                                "body": b"",
                                "more_body": False,
                            }
                        )
                    except Exception:
                        logger.error("VPN portal response transport unavailable")
                return
            logger.error("Unhandled VPN portal request failure")
            response = JSONResponse(
                {"detail": "customer_service_unavailable"},
                status_code=500,
            )
            if scope.get("path") == self.telegram_callback_path:
                try:
                    delete_binding_cookie(response, scope["app"].state.settings)
                except Exception:
                    response.delete_cookie(
                        BINDING_COOKIE,
                        path="/",
                        secure=True,
                        httponly=True,
                        samesite="lax",
                    )
            try:
                await response(scope, receive, no_store_send)
            except Exception:
                logger.error("VPN portal error response transport unavailable")

    def _mini_app_rejection(self, scope) -> JSONResponse | None:
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in scope.get("headers", [])
        }
        settings = scope["app"].state.settings
        capabilities = portal_capabilities(settings)
        if not capabilities["mini_app_enabled"]:
            return JSONResponse({"detail": "customer_authentication_failed"}, status_code=401)
        try:
            expected_origin = public_origin(settings)
        except ValueError:
            return JSONResponse({"detail": "customer_authentication_failed"}, status_code=401)
        if headers.get("origin") != expected_origin:
            return JSONResponse({"detail": "customer_request_rejected"}, status_code=403)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            return JSONResponse({"detail": "customer_request_rejected"}, status_code=415)
        return None

    @staticmethod
    async def _read_bounded_body(
        receive: Callable[[], Awaitable[dict[str, Any]]],
    ) -> tuple[bytes, int | None]:
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return b"", 400
            if message["type"] != "http.request":
                continue
            body.extend(message.get("body", b""))
            if len(body) > MAX_MINI_APP_BODY_BYTES:
                return b"", 413
            if not message.get("more_body", False):
                return bytes(body), None
