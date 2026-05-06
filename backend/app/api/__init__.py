from fastapi import APIRouter

from app.api.routes.admin import router as admin_router
from app.api.routes.auth import router as auth_router
from app.api.routes.control import router as control_router
from app.api.routes.health import router as health_router
from app.api.routes.worker_runtime import router as worker_runtime_router


api_router = APIRouter()
api_router.include_router(auth_router)
api_router.include_router(admin_router)
api_router.include_router(control_router)
api_router.include_router(worker_runtime_router)
api_router.include_router(health_router)
