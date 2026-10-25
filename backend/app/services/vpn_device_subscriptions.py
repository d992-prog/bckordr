"""Signed bearer URLs and a live, single-selected-credential subscription body."""

import base64
import hashlib
from datetime import datetime

from itsdangerous import BadData, URLSafeSerializer
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnDevice, VpnSubscription
from app.services.vpn_customer_view import may_read_connection
from app.services.vpn_display import InvalidVpnDisplay, display_uri
from app.services.vpn_portal_auth import public_origin

SALT = "veltrix-vpn-device-subscription-v1"


def device_signer(settings: Settings) -> URLSafeSerializer:
    secret = settings.session_secret_key
    if len(secret) < 32 or secret == "change-me-session-secret":
        raise ValueError("device_subscription_unavailable")
    return URLSafeSerializer(
        secret, salt=SALT, signer_kwargs={"digest_method": hashlib.sha256}
    )


def device_token(settings: Settings, device: VpnDevice) -> str:
    return device_signer(settings).dumps([device.id, device.token_generation])


def decode_device_token(settings: Settings, token: str) -> tuple[int, int]:
    if not isinstance(token, str) or not 1 <= len(token) <= 512:
        raise ValueError("device_subscription_unavailable")
    try:
        payload = device_signer(settings).loads(token)
    except BadData:
        raise ValueError("device_subscription_unavailable") from None
    if (
        type(payload) is not list
        or len(payload) != 2
        or any(type(value) is not int for value in payload)
        or payload[0] <= 0
        or payload[1] < 0
    ):
        raise ValueError("device_subscription_unavailable")
    return payload[0], payload[1]


def device_subscription_url(settings: Settings, device: VpnDevice) -> str:
    return (
        public_origin(settings)
        + settings.api_prefix.rstrip("/")
        + "/vpn-portal/subscription/"
        + device_token(settings, device)
    )


def encode_device_subscription(uri: str) -> str:
    return base64.b64encode((uri + "\n").encode("utf-8")).decode("ascii")


async def subscription_connection_uri(
    db: AsyncSession,
    settings: Settings,
    token: str,
    *,
    now: datetime | None = None,
) -> str:
    device_id, generation = decode_device_token(settings, token)
    row = (
        await db.execute(
            select(VpnDevice, VpnAccessKey, VpnSubscription, VpnCustomer)
            .join(VpnSubscription, VpnSubscription.id == VpnDevice.subscription_id)
            .join(VpnCustomer, VpnCustomer.id == VpnSubscription.customer_id)
            .join(
                VpnAccessKey,
                and_(
                    VpnAccessKey.id == VpnDevice.preferred_key_id,
                    VpnAccessKey.device_id == VpnDevice.id,
                    VpnAccessKey.subscription_id == VpnDevice.subscription_id,
                ),
            )
            .where(VpnDevice.id == device_id, VpnDevice.token_generation == generation)
            .execution_options(populate_existing=True)
        )
    ).first()
    if row is None:
        raise ValueError("device_subscription_unavailable")
    device, key, subscription, customer = row
    if (
        device.revoke_requested_at is not None
        or key.revoke_requested_at is not None
        or key.revoked_at is not None
        or not may_read_connection(customer, subscription, key, now or utcnow())
    ):
        raise ValueError("device_subscription_unavailable")
    try:
        return display_uri(key.config_uri, device.display_name)
    except InvalidVpnDisplay:
        raise ValueError("device_subscription_unavailable") from None
