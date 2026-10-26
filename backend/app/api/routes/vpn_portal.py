from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Path, Query, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnCustomer, VpnPlan
from app.db.session import get_db
from app.schemas.vpn_cabinet import PortalUsageResponse
from app.schemas.vpn_portal import (
    MiniAppLogin,
    PortalConnection,
    PortalMe,
    PortalPlan,
    PortalProfile,
    PortalPreferences,
    PortalSubscription,
    PortalTrial,
    RenamePortalProfile,
    PortalDevice, PortalDeviceConnection, PortalDeviceLocations, SelectPortalDeviceLocation,
    CreatePortalDevice, PortalDeviceOperation, PortalDeviceRequest,
    PortalDeviceLocation,
)
from app.services.vpn_cabinet_settings import get_cabinet_settings
from app.services.vpn_devices import MissingCustomerDevice, lock_owned_device, rename_device, select_device_location
from app.services.vpn_device_operations import (
    owned_device_operation, request_device_creation, request_device_operation,
)
from app.services.vpn_device_view import (
    customer_device, customer_device_connection, customer_device_locations, list_customer_devices,
    customer_creation_locations,
)
from app.services.vpn_device_subscriptions import encode_device_subscription, subscription_connection_uri
from app.services.vpn_customer_view import (
    MissingCustomerProfile,
    UnavailableCustomerConnection,
    customer_connection,
    list_customer_profiles,
    list_customer_subscriptions,
    rename_customer_profile,
)
from app.services.vpn_portal_auth import (
    BINDING_COOKIE,
    SESSION_COOKIE,
    PortalAuthenticationError,
    PortalPrincipal,
    consume_login_attempt,
    create_login_attempt,
    delete_binding_cookie,
    delete_session_cookie,
    exchange_mini_app_session,
    identity_admitted,
    issue_session,
    lock_current_principal,
    lookup_session,
    public_origin,
    revoke_session,
    set_binding_cookie,
    set_session_cookie,
    valid_mutation,
)
from app.services.vpn_portal_http import portal_capabilities
from app.services.vpn_portal_telegram import (
    TelegramAuthenticationError,
    authorization_url,
    exchange_authorization_code,
)
from app.services.vpn_public_trial import (
    PublicTrialConflict,
    PublicTrialUnavailable,
    activate_public_trial,
    public_trial_status,
)
from app.services.vpn_friend_invitations import BOT_USERNAME
from app.services.vpn_telegram_identity import TelegramIdentity, resolve_telegram_customer
from app.services.vpn_usage_reports import get_customer_usage


router = APIRouter(prefix="/vpn-portal", tags=["vpn-portal"])


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _friendly_name(principal: PortalPrincipal) -> str:
    customer = principal.customer
    full_name = " ".join(
        part.strip() for part in (customer.first_name, customer.last_name) if part and part.strip()
    )
    if full_name:
        return full_name
    if customer.telegram_username:
        return f"@{customer.telegram_username.strip().lstrip('@')}"
    return "Клиент Veltrix VPN"


def _portal_me(principal: PortalPrincipal) -> PortalMe:
    return PortalMe(display_name=_friendly_name(principal), csrf_token=principal.csrf)


async def current_customer(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> PortalPrincipal:
    principal = await lookup_session(
        db,
        request.cookies.get(SESSION_COOKIE),
        _settings(request),
    )
    if principal is None:
        raise HTTPException(status_code=401, detail="customer_authentication_required")
    return principal


def require_mutation(
    request: Request,
    principal: PortalPrincipal = Depends(current_customer),
    x_csrf_token: str | None = Header(default=None),
) -> PortalPrincipal:
    if not valid_mutation(
        request.headers.get("origin"),
        x_csrf_token,
        principal,
        _settings(request),
    ):
        raise HTTPException(status_code=403, detail="customer_request_rejected")
    return principal


def _require_capability(request: Request, capability: str) -> Settings:
    settings = _settings(request)
    if not portal_capabilities(settings)[capability]:
        raise HTTPException(status_code=404, detail="customer_portal_unavailable")
    return settings


def _callback_url(settings: Settings) -> str:
    return public_origin(settings) + settings.api_prefix.rstrip("/") + "/vpn-portal/auth/telegram/callback"


def _bot_url(settings: Settings) -> str | None:
    username = settings.vpn_telegram_bot_username
    if BOT_USERNAME.fullmatch(username) is None or not username.lower().endswith("bot"):
        return None
    return f"https://t.me/{username}"


@router.get("/config")
async def config(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, object]:
    capabilities = portal_capabilities(_settings(request))
    cabinet = await get_cabinet_settings(db)
    now = utcnow()
    notice = cabinet.notice
    return {
        **capabilities,
        "login_path": (
            _settings(request).api_prefix.rstrip("/") + "/vpn-portal/auth/telegram/start"
            if capabilities["browser_login_enabled"]
            else None
        ),
        "bot_url": _bot_url(_settings(request)),
        "support_text": _settings(request).vpn_support_text,
        "support_url": cabinet.support_contact,
        "notice": (
            notice.model_dump(mode="json")
            if notice is not None and notice.starts_at <= now < notice.ends_at
            else None
        ),
        "reminders_enabled": cabinet.reminders_enabled,
    }


@router.get("/me", response_model=PortalMe)
async def me(principal: PortalPrincipal = Depends(current_customer)) -> PortalMe:
    return _portal_me(principal)


@router.get("/preferences", response_model=PortalPreferences)
async def preferences(
    response: Response,
    principal: PortalPrincipal = Depends(current_customer),
) -> PortalPreferences:
    response.headers["Cache-Control"] = "no-store"
    return PortalPreferences(expiry_reminders=principal.customer.expiry_reminders)


@router.patch("/preferences", response_model=PortalPreferences)
async def update_preferences(
    payload: PortalPreferences, request: Request, response: Response,
    principal: PortalPrincipal = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> PortalPreferences:
    settings = _settings(request)
    principal = await _device_mutation_principal(db, principal, settings)
    principal.customer.expiry_reminders = payload.expiry_reminders
    await db.flush()
    await _device_mutation_principal(db, principal, settings)
    await db.commit()
    response.headers["Cache-Control"] = "no-store"
    return payload


@router.get("/usage", response_model=PortalUsageResponse)
async def usage(
    response: Response,
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
    days: Literal["1", "7", "30"] = Query(default="1"),
) -> PortalUsageResponse:
    response.headers["Cache-Control"] = "no-store"
    report = await get_customer_usage(db, principal.customer.id, days=int(days), now=utcnow())
    if report is None:
        raise HTTPException(
            status_code=404, detail="customer_usage_unavailable",
            headers={"Cache-Control": "no-store"},
        )
    return PortalUsageResponse.model_validate(report.model_dump(exclude={"customer_id"}))


@router.get("/plans", response_model=list[PortalPlan])
async def plans(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> list[PortalPlan]:
    settings = _settings(request)
    result = await db.scalars(
        select(VpnPlan)
        .where(VpnPlan.is_active.is_(True), VpnPlan.is_public.is_(True))
        .order_by(VpnPlan.display_order.asc(), VpnPlan.id.asc())
    )
    return [
        PortalPlan(
            id=plan.id,
            name=plan.name,
            description=plan.description,
            duration_days=plan.duration_days,
            traffic_limit_gb=plan.traffic_limit_gb,
            max_devices=plan.max_devices,
            price_amount=plan.price_amount,
            currency=plan.currency,
            is_trial=plan.slug == settings.vpn_public_trial_plan_slug,
        )
        for plan in result
    ]


@router.get("/trial", response_model=PortalTrial)
async def trial_status(
    request: Request,
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
) -> PortalTrial:
    view = await public_trial_status(db, _settings(request), principal.customer, datetime.now(UTC))
    return PortalTrial.model_validate(asdict(view))


@router.post("/trial/activate", response_model=PortalTrial)
async def trial_activate(
    request: Request,
    principal: PortalPrincipal = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> PortalTrial:
    settings = _settings(request)
    current_time = datetime.now(UTC)
    principal = await lock_current_principal(db, principal, settings, current_time)
    if principal is None:
        await db.rollback()
        raise HTTPException(status_code=401, detail="customer_authentication_required") from None
    customer = principal.customer
    identity = TelegramIdentity(
        user_id=principal.session.telegram_user_id,
        username=customer.telegram_username,
        first_name=customer.first_name,
        last_name=customer.last_name,
    )
    try:
        view = await activate_public_trial(db, settings, identity, current_time)
    except PublicTrialUnavailable:
        await db.rollback()
        raise HTTPException(status_code=409, detail="public_trial_capacity_unavailable") from None
    except PublicTrialConflict:
        await db.rollback()
        raise HTTPException(status_code=409, detail="public_trial_already_used") from None
    await db.commit()
    return PortalTrial.model_validate(asdict(view))


@router.post("/auth/mini-app", response_model=PortalMe)
async def mini_app_login(
    payload: MiniAppLogin,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> PortalMe:
    settings = _settings(request)
    try:
        result = await exchange_mini_app_session(
            db,
            payload.init_data,
            settings,
            raw_session=request.cookies.get(SESSION_COOKIE),
        )
    except PortalAuthenticationError as error:
        await db.rollback()
        status_code = 409 if error.account_conflict else 401
        raise HTTPException(status_code=status_code, detail="customer_authentication_failed") from None
    await db.commit()
    if result.raw_session is not None:
        set_session_cookie(response, result.raw_session, settings)
    return _portal_me(result.principal)


@router.get("/auth/telegram/start")
async def telegram_start(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    settings = _require_capability(request, "browser_login_enabled")
    state, binding, verifier = await create_login_attempt(db)
    await db.commit()
    response = RedirectResponse(
        authorization_url(
            settings.vpn_portal_oidc_client_id,
            _callback_url(settings),
            state,
            verifier,
        )
    )
    set_binding_cookie(response, binding, settings)
    return response


def _callback_failure(settings: Settings) -> RedirectResponse:
    response = RedirectResponse("/cabinet/#login=failed")
    try:
        delete_binding_cookie(response, settings)
    except ValueError:
        response.delete_cookie(
            BINDING_COOKIE,
            path="/",
            secure=True,
            httponly=True,
            samesite="lax",
        )
    return response


@router.get("/auth/telegram/callback")
async def telegram_callback(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    settings = _settings(request)
    if not portal_capabilities(settings)["browser_login_enabled"]:
        return _callback_failure(settings)
    states = request.query_params.getlist("state")
    codes = request.query_params.getlist("code")
    binding = request.cookies.get(BINDING_COOKIE)
    if len(states) != 1 or not binding:
        return _callback_failure(settings)

    verifier = await consume_login_attempt(db, states[0], binding, datetime.now(UTC))
    if verifier is None or len(codes) != 1 or request.query_params.get("error") is not None:
        return _callback_failure(settings)

    try:
        identity = await exchange_authorization_code(
            settings.vpn_portal_oidc_client_id,
            settings.vpn_portal_oidc_client_secret,
            _callback_url(settings),
            codes[0],
            verifier,
            http_client=request.app.state.vpn_portal_http_client,
            jwks_provider=request.app.state.vpn_portal_jwks_provider,
        )
        if not await identity_admitted(db, settings, identity.user_id):
            raise TelegramAuthenticationError("telegram_authentication_failed")
        customer = await resolve_telegram_customer(db, identity)
        if customer.status != "active" or not await identity_admitted(
            db,
            settings,
            identity.user_id,
            lock_public_trial=True,
        ):
            raise TelegramAuthenticationError("telegram_authentication_failed")
        raw_session = await issue_session(db, customer.id, identity.user_id)
        await db.commit()
    except (TelegramAuthenticationError, ValueError):
        await db.rollback()
        return _callback_failure(settings)

    response = RedirectResponse("/cabinet/")
    delete_binding_cookie(response, settings)
    set_session_cookie(response, raw_session, settings)
    return response


@router.get("/subscriptions", response_model=list[PortalSubscription])
async def subscriptions(
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
) -> list[PortalSubscription]:
    return await list_customer_subscriptions(db, principal.customer.id)


@router.get("/profiles", response_model=list[PortalProfile])
async def profiles(
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
) -> list[PortalProfile]:
    return await list_customer_profiles(db, principal.customer)


@router.get("/profiles/{profile_id}/connection", response_model=PortalConnection)
async def profile_connection(
    profile_id: int,
    principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
) -> PortalConnection:
    try:
        return await customer_connection(db, principal.customer, profile_id)
    except MissingCustomerProfile:
        raise HTTPException(status_code=404, detail="customer_profile_not_found") from None
    except UnavailableCustomerConnection:
        raise HTTPException(status_code=409, detail="customer_connection_unavailable") from None


@router.patch("/profiles/{profile_id}", response_model=PortalProfile)
async def rename_profile(
    profile_id: int,
    payload: RenamePortalProfile,
    principal: PortalPrincipal = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> PortalProfile:
    try:
        profile = await rename_customer_profile(
            db, principal.customer, profile_id, payload.display_name
        )
    except MissingCustomerProfile:
        raise HTTPException(status_code=404, detail="customer_profile_not_found") from None
    await db.commit()
    return profile


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    principal: PortalPrincipal = Depends(require_mutation),
    db: AsyncSession = Depends(get_db),
) -> dict[str, bool]:
    await revoke_session(db, principal)
    await db.commit()
    delete_session_cookie(response, _settings(request))
    return {"logged_out": True}


async def _device_mutation_principal(db, principal, settings):
    # Revalidate against time after any wait for the customer serialization lock.
    await db.scalar(select(VpnCustomer.id).where(VpnCustomer.id == principal.customer.id).with_for_update())
    principal = await lock_current_principal(db, principal, settings, datetime.now(UTC))
    if principal is None:
        await db.rollback()
        raise HTTPException(status_code=401, detail="customer_authentication_required")
    return principal


@router.get("/v2/devices", response_model=list[PortalDevice])
async def devices(principal: PortalPrincipal = Depends(current_customer), db: AsyncSession = Depends(get_db)):
    return await list_customer_devices(db, principal.customer.id)


@router.get("/v2/locations", response_model=list[PortalDeviceLocation])
async def creation_locations(
    request: Request, subscription_id: int = Query(gt=0, le=2**31 - 1),
    principal: PortalPrincipal = Depends(current_customer), db: AsyncSession = Depends(get_db),
):
    try:
        return await customer_creation_locations(db, principal.customer.id, subscription_id, _settings(request))
    except (MissingCustomerDevice, ValueError) as error:
        raise _device_operation_error(error) from None


_DEVICE_ERRORS = frozenset({
    "customer_device_request_invalid", "customer_device_request_conflict", "customer_device_limit_reached",
    "customer_device_subscription_unavailable", "customer_device_endpoint_unavailable", "customer_device_unavailable",
    "customer_device_operation_pending", "customer_device_replacement_limited",
})


def _device_operation_error(error):
    if isinstance(error, MissingCustomerDevice):
        return HTTPException(status_code=404, detail="customer_device_not_found")
    code = str(error)
    return HTTPException(status_code=409, detail=code if code in _DEVICE_ERRORS else "customer_device_unavailable")


@router.post("/v2/devices", response_model=PortalDeviceOperation, status_code=202)
async def create_owned_device(
    payload: CreatePortalDevice, request: Request, response: Response,
    principal: PortalPrincipal = Depends(require_mutation), db: AsyncSession = Depends(get_db),
):
    settings = _settings(request)
    principal = await _device_mutation_principal(db, principal, settings)
    try:
        operation = await request_device_creation(
            db, customer_id=principal.customer.id, settings=settings, now=datetime.now(UTC),
            **payload.model_dump(),
        )
        await _device_mutation_principal(db, principal, settings)
        await db.commit()
        response.status_code = 200 if operation.stage == "completed" else 202
        return operation
    except (MissingCustomerDevice, ValueError) as error:
        await db.rollback()
        raise _device_operation_error(error) from None


async def _owned_device_action(device_id, action, payload, request, response, principal, db):
    settings = _settings(request)
    principal = await _device_mutation_principal(db, principal, settings)
    try:
        operation = await request_device_operation(
            db, customer_id=principal.customer.id, device_id=device_id, action=action,
            request_id=payload.request_id, settings=settings, now=datetime.now(UTC),
        )
        await _device_mutation_principal(db, principal, settings)
        await db.commit()
        response.status_code = 200 if operation.stage == "completed" else 202
        return operation
    except (MissingCustomerDevice, ValueError) as error:
        await db.rollback()
        raise _device_operation_error(error) from None


@router.post("/v2/devices/{device_id}/revoke", response_model=PortalDeviceOperation, status_code=202)
async def revoke_owned_device(
    device_id: Annotated[int, Path(gt=0, le=2**31 - 1)], payload: PortalDeviceRequest, request: Request, response: Response,
    principal: PortalPrincipal = Depends(require_mutation), db: AsyncSession = Depends(get_db),
):
    return await _owned_device_action(device_id, "revoke", payload, request, response, principal, db)


@router.post("/v2/devices/{device_id}/replace", response_model=PortalDeviceOperation, status_code=202)
async def replace_owned_device(
    device_id: Annotated[int, Path(gt=0, le=2**31 - 1)], payload: PortalDeviceRequest, request: Request, response: Response,
    principal: PortalPrincipal = Depends(require_mutation), db: AsyncSession = Depends(get_db),
):
    return await _owned_device_action(device_id, "replace", payload, request, response, principal, db)


@router.get("/v2/device-operations/{operation_id}", response_model=PortalDeviceOperation)
async def device_operation(
    operation_id: UUID, principal: PortalPrincipal = Depends(current_customer), db: AsyncSession = Depends(get_db),
):
    try:
        return await owned_device_operation(db, principal.customer.id, operation_id)
    except MissingCustomerDevice:
        raise HTTPException(status_code=404, detail="customer_device_not_found") from None


@router.patch("/v2/devices/{device_id}", response_model=PortalDevice)
async def rename_owned_device(
    device_id: int, payload: RenamePortalProfile, request: Request,
    principal: PortalPrincipal = Depends(require_mutation), db: AsyncSession = Depends(get_db),
):
    principal = await _device_mutation_principal(db, principal, _settings(request))
    try:
        _, _, device, keys = await lock_owned_device(db, principal.customer.id, device_id)
        key = next((key for key in keys if key.device_id == device.id and key.subscription_id == device.subscription_id), None)
        if key is None:
            device.display_name, device.updated_at = payload.display_name, datetime.now(UTC)
            await db.flush()
        else:
            await rename_device(db, key, payload.display_name)
        await db.commit()
        return await customer_device(db, principal.customer.id, device_id)
    except MissingCustomerDevice:
        await db.rollback()
        raise HTTPException(status_code=404, detail="customer_device_not_found") from None


@router.get("/v2/devices/{device_id}/locations", response_model=PortalDeviceLocations)
async def device_locations(
    device_id: int, request: Request, principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await customer_device_locations(db, principal.customer.id, device_id, _settings(request))
    except MissingCustomerDevice:
        raise HTTPException(status_code=404, detail="customer_device_not_found") from None


@router.post("/v2/devices/{device_id}/location", response_model=PortalDeviceLocations, status_code=202)
async def select_owned_device_location(
    device_id: int, payload: SelectPortalDeviceLocation, request: Request,
    principal: PortalPrincipal = Depends(require_mutation), db: AsyncSession = Depends(get_db),
):
    settings = _settings(request)
    principal = await _device_mutation_principal(db, principal, settings)
    try:
        await select_device_location(db, customer_id=principal.customer.id, device_id=device_id,
                                     endpoint_id=payload.endpoint_id, settings=settings, now=datetime.now(UTC))
        await db.commit()
        return await customer_device_locations(db, principal.customer.id, device_id, settings)
    except MissingCustomerDevice:
        await db.rollback()
        raise HTTPException(status_code=404, detail="customer_device_not_found") from None
    except ValueError as error:
        await db.rollback()
        code = ("customer_location_verification_required" if str(error) == "customer_location_verification_required"
                else "customer_location_unavailable")
        raise HTTPException(status_code=409, detail=code) from None


@router.get("/v2/devices/{device_id}/connection", response_model=PortalDeviceConnection)
async def device_connection(
    device_id: int, request: Request, principal: PortalPrincipal = Depends(current_customer),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await customer_device_connection(db, principal.customer.id, device_id, _settings(request))
    except MissingCustomerDevice:
        raise HTTPException(status_code=404, detail="customer_device_not_found") from None
    except ValueError:
        raise HTTPException(status_code=409, detail="customer_connection_unavailable") from None


@router.get("/subscription/{token}")
async def device_subscription(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
    try:
        uri = await subscription_connection_uri(db, _settings(request), token)
    except ValueError:
        raise HTTPException(status_code=404, detail="customer_connection_unavailable", headers=headers) from None
    return Response(encode_device_subscription(uri), media_type="text/plain", headers=headers)
