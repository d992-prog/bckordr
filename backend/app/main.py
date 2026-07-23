from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import api_router
from app.core.config import get_settings
from app.db.base import Base
from app.db.migrations import run_startup_migrations
from app.db.session import AsyncSessionLocal, engine
from app.services.bootstrap import ensure_default_zone_strategies, ensure_owner_account
from app.services.control_runtime import ControlRuntimeOrchestrator
from app.services.notifier import TelegramNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await run_startup_migrations(engine)
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

    try:
        yield
    finally:
        await monitoring.shutdown()
        await engine.dispose()


def create_app() -> FastAPI:
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=settings.cors_origin_list != ["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(api_router, prefix=settings.api_prefix)

    if settings.frontend_dist_dir.exists():
        app.mount("/", StaticFiles(directory=settings.frontend_dist_dir, html=True), name="frontend")

    return app


app = create_app()
