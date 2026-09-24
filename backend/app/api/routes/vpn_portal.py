from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.session import get_db
from app.schemas.vpn_portal import (
    MiniAppLogin,
    PortalConnection,
    PortalMe,
    PortalProfile,
    PortalSubscription,
    PortalTrial,
    RenamePortalProfile,
)
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
from app.services.vpn_telegram_identity import TelegramIdentity, resolve_telegram_customer


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


@router.get("/config")
async def config(request: Request) -> dict[str, object]:
    capabilities = portal_capabilities(_settings(request))
    return {
        **capabilities,
        "login_path": (
            _settings(request).api_prefix.rstrip("/") + "/vpn-portal/auth/telegram/start"
            if capabilities["browser_login_enabled"]
            else None
        ),
        "support_text": _settings(request).vpn_support_text,
    }


@router.get("/me", response_model=PortalMe)
async def me(principal: PortalPrincipal = Depends(current_customer)) -> PortalMe:
    return _portal_me(principal)


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
