from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.core.config import get_settings
from app.db.base import Base
from app.db.migrations import run_startup_migrations
from app.db.session import AsyncSessionLocal, engine
from app.services.bootstrap import ensure_default_zone_strategies, ensure_owner_account
from app.services.control_runtime import ControlRuntimeOrchestrator
from app.services.notifier import TelegramNotifier
from app.services.vpn_portal_http import (
    PortalHttpMiddleware,
    ScopedControlCorsMiddleware,
    is_portal_path,
)
from app.services.vpn_portal_logging import install_safe_logging
from app.services.vpn_portal_telegram import TelegramJWKSProvider
from app.services.vpn_profile_names import backfill_profile_names

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
install_safe_logging()

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_startup_migrations(engine)
    await backfill_profile_names(AsyncSessionLocal)
    await ensure_owner_account(AsyncSessionLocal, settings)
    await ensure_default_zone_strategies(
        AsyncSessionLocal,
        default_ident_number=settings.gandi_default_ident_number,
    )

    notifier = TelegramNotifier(settings)
    del notifier
    monitoring = ControlRuntimeOrchestrator(
        AsyncSessionLocal,
        interval_seconds=settings.control_scheduler_interval_seconds,
        worker_supervisor_interval_seconds=settings.worker_supervisor_interval_seconds,
        worker_stall_threshold_seconds=settings.worker_stall_threshold_seconds,
        settings=settings,
    )
    app.state.monitoring = monitoring
    await monitoring.bootstrap()

    portal_http_client = httpx.AsyncClient(timeout=10.0, follow_redirects=False)
    app.state.vpn_portal_http_client = portal_http_client
    app.state.vpn_portal_jwks_provider = TelegramJWKSProvider(portal_http_client)

    try:
        yield
    finally:
        await portal_http_client.aclose()
        await monitoring.shutdown()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        ScopedControlCorsMiddleware,
        portal_prefix=settings.api_prefix.rstrip("/") + "/vpn-portal",
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_origin_list != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(
        PortalHttpMiddleware,
        portal_prefix=settings.api_prefix.rstrip("/") + "/vpn-portal",
    )

    @app.exception_handler(RequestValidationError)
    async def scoped_request_validation_handler(request, exc):
        portal_prefix = settings.api_prefix.rstrip("/") + "/vpn-portal"
        if is_portal_path(request.url.path, portal_prefix):
            return JSONResponse(
                {"detail": "invalid_customer_request"},
                status_code=422,
                headers={"Cache-Control": "no-store"},
            )
        return await request_validation_exception_handler(request, exc)

    app.include_router(api_router, prefix=settings.api_prefix)

    if settings.frontend_dist_dir.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")

    return app


app = create_app()
