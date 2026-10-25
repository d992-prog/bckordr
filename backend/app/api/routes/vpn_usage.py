from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.core.config import get_settings
from app.db.base import utcnow
from app.db.models import User
from app.db.session import get_db
from app.schemas.vpn_usage import CustomerUsageResponse, NodeUsageRow
from app.services.vpn_usage_reports import get_customer_usage, list_node_usage

router = APIRouter(prefix="/control/vpn", tags=["control"])


@router.get("/usage/nodes", response_model=list[NodeUsageRow])
async def node_usage(
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    days: Literal["1", "7", "30"] = Query(default="1"),
    after_id: int = Query(default=0, ge=0, le=2**31 - 1),
    limit: int = Query(default=100, ge=1, le=100),
) -> list[NodeUsageRow]:
    del admin
    response.headers["Cache-Control"] = "no-store"
    return await list_node_usage(
        db,
        days=int(days),
        now=utcnow(),
        after_id=after_id,
        limit=limit,
        heartbeat_max_age_seconds=get_settings().worker_stall_threshold_seconds,
    )


@router.get("/customers/{customer_id}/usage", response_model=CustomerUsageResponse)
async def customer_usage(
    customer_id: Annotated[int, Path(ge=1, le=2**31 - 1)],
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    days: Literal["1", "7", "30"] = Query(default="1"),
) -> CustomerUsageResponse:
    del admin
    response.headers["Cache-Control"] = "no-store"
    result = await get_customer_usage(db, customer_id, days=int(days), now=utcnow())
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="VPN customer not found",
            headers={"Cache-Control": "no-store"},
        )
    return result
