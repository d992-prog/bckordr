from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.routes.control import _require_same_origin
from app.db.base import utcnow
from app.db.models import User
from app.db.session import get_db
from app.schemas.vpn_cabinet import CabinetSettings
from app.services.audit import add_audit_log
from app.services.vpn_cabinet_settings import (
    CabinetRevisionConflict,
    get_cabinet_settings,
    update_cabinet_settings,
)

router = APIRouter(prefix="/control/vpn", tags=["control"])
Database = Annotated[AsyncSession, Depends(get_db)]
Administrator = Annotated[User, Depends(require_admin)]


@router.get("/cabinet-settings", response_model=CabinetSettings)
async def cabinet_settings(
    response: Response, db: Database, admin: Administrator
) -> CabinetSettings:
    response.headers["Cache-Control"] = "no-store"
    return await get_cabinet_settings(db)


@router.put("/cabinet-settings", response_model=CabinetSettings)
async def save_cabinet_settings(
    payload: CabinetSettings,
    request: Request,
    response: Response,
    db: Database,
    admin: Administrator,
) -> CabinetSettings:
    _require_same_origin(request)
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await update_cabinet_settings(db, payload, now=utcnow())
    except CabinetRevisionConflict:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail="vpn_cabinet_settings_revision_conflict",
            headers={"Cache-Control": "no-store"},
        ) from None
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="vpn_cabinet_settings_updated",
        details=f"revision={result.revision}",
    )
    await db.commit()
    return result
