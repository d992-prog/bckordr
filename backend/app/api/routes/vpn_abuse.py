from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from sqlalchemy import and_, case, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.api.routes.control import _require_same_origin, _vpn_access_key_response
from app.db import models as m
from app.db.base import utcnow
from app.db.session import get_db
from app.schemas.vpn_abuse import (
    AbuseDeviceContextResponse,
    AbuseIncidentResponse,
    AcknowledgeAbuseIncidentRequest,
)
from app.services.vpn_abuse import acknowledge_incident


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


router = APIRouter(
    prefix="/control/vpn/abuse", tags=["control"], dependencies=[Depends(_no_store)]
)
Database = Annotated[AsyncSession, Depends(get_db)]
Administrator = Annotated[m.User, Depends(require_admin)]
IncidentId = Annotated[int, Path(ge=1, le=2**31 - 1)]


@router.get("/devices/{device_id}/context", response_model=AbuseDeviceContextResponse)
async def device_context(
    device_id: Annotated[int, Path(ge=1, le=2**31 - 1)],
    customer_id: Annotated[int, Query(ge=1, le=2**31 - 1)],
    db: Database,
    admin: Administrator,
):
    del admin
    row = (
        await db.execute(
            select(m.VpnDevice, m.VpnSubscription, m.VpnCustomer)
            .join(
                m.VpnSubscription, m.VpnSubscription.id == m.VpnDevice.subscription_id
            )
            .join(m.VpnCustomer, m.VpnCustomer.id == m.VpnSubscription.customer_id)
            .where(m.VpnDevice.id == device_id, m.VpnCustomer.id == customer_id)
        )
    ).first()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="vpn_abuse_device_not_found",
            headers={"Cache-Control": "no-store"},
        )
    device, subscription, customer = row
    non_revoked = and_(
        m.VpnAccessKey.status != "revoked", m.VpnAccessKey.revoked_at.is_(None)
    )
    key = await db.scalar(
        select(m.VpnAccessKey)
        .where(
            m.VpnAccessKey.device_id == device.id,
            m.VpnAccessKey.subscription_id == subscription.id,
        )
        .order_by(
            case(
                (and_(non_revoked, m.VpnAccessKey.id == device.preferred_key_id), 0),
                (non_revoked, 1),
                else_=2,
            ),
            m.VpnAccessKey.id,
        )
        .limit(1)
    )
    return AbuseDeviceContextResponse(
        device_id=device.id,
        customer=customer,
        subscription=subscription,
        access_key=_vpn_access_key_response(key) if key is not None else None,
    )


def _query():
    return (
        select(
            m.VpnAbuseIncident.id,
            m.VpnAbuseIncident.device_id,
            m.VpnSubscription.customer_id,
            m.VpnDevice.display_name,
            m.VpnAbuseIncident.started_at,
            m.VpnAbuseIncident.last_observed_at,
            m.VpnAbuseIncident.peak_source_count,
            m.VpnAbuseIncident.acknowledged_at,
        )
        .join(m.VpnDevice, m.VpnDevice.id == m.VpnAbuseIncident.device_id)
        .join(m.VpnSubscription, m.VpnSubscription.id == m.VpnDevice.subscription_id)
    )


@router.get("/incidents", response_model=list[AbuseIncidentResponse])
async def list_incidents(
    db: Database,
    admin: Administrator,
    state: Literal["open", "acknowledged"] = Query("open"),
    after_id: int = Query(0, ge=0, le=2**31 - 1),
    limit: int = Query(100, ge=1, le=100),
):
    del admin
    predicate = (
        m.VpnAbuseIncident.acknowledged_at.is_(None)
        if state == "open"
        else m.VpnAbuseIncident.acknowledged_at.is_not(None)
    )
    return (
        (
            await db.execute(
                _query()
                .where(predicate, m.VpnAbuseIncident.id > after_id)
                .order_by(m.VpnAbuseIncident.id)
                .limit(limit)
            )
        )
        .mappings()
        .all()
    )


@router.post(
    "/incidents/{incident_id}/acknowledge", response_model=AbuseIncidentResponse
)
async def acknowledge(
    incident_id: IncidentId,
    payload: AcknowledgeAbuseIncidentRequest,
    request: Request,
    db: Database,
    admin: Administrator,
):
    del payload
    _require_same_origin(request)
    try:
        await acknowledge_incident(db, incident_id, actor_id=admin.id, now=utcnow())
    except ValueError:
        await db.rollback()
        raise HTTPException(
            status_code=404,
            detail="vpn_abuse_incident_not_found",
            headers={"Cache-Control": "no-store"},
        ) from None
    result = (
        (await db.execute(_query().where(m.VpnAbuseIncident.id == incident_id)))
        .mappings()
        .one()
    )
    await db.commit()
    return result
