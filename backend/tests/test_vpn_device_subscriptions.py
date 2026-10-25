import base64
import hashlib
from datetime import timedelta

import pytest
from itsdangerous import URLSafeSerializer
from sqlalchemy import update
from test_vpn_friend_invitations import NOW
from test_vpn_policy import session_factory as session_factory  # noqa: PLC0414

from app.core.config import Settings
from app.db.models import VpnAccessKey, VpnCustomer, VpnDevice, VpnSubscription
from app.services.vpn_device_subscriptions import (
    SALT,
    decode_device_token,
    device_subscription_url,
    device_token,
    encode_device_subscription,
    subscription_connection_uri,
)
from app.services.vpn_devices import attach_initial_device

SECRET = "synthetic-persistent-secret-not-for-production-123456789"
URI = "vless://11111111-1111-4111-8111-111111111111@vpn.example:443?security=reality#stored"


def settings(**values):
    return Settings(
        _env_file=None,
        SESSION_SECRET_KEY=SECRET,
        VPN_PORTAL_PUBLIC_ORIGIN="https://vpn.example",
        **values,
    )


async def seed(db):
    customer = VpnCustomer(status="active")
    db.add(customer)
    await db.flush()
    subscription = VpnSubscription(
        customer_id=customer.id, status="active", expires_at=NOW + timedelta(days=30)
    )
    db.add(subscription)
    await db.flush()
    key = VpnAccessKey(
        subscription_id=subscription.id,
        status="active",
        display_name="Телефон",
        config_uri=URI,
        expires_at=subscription.expires_at,
    )
    db.add(key)
    await db.flush()
    device = await attach_initial_device(db, key)
    await db.commit()
    return customer, subscription, key, device


def test_token_is_stable_across_name_and_preferred_country_changes():
    device = VpnDevice(
        id=7, token_generation=0, display_name="Phone", preferred_key_id=21
    )
    token = device_token(settings(), device)
    device.display_name, device.preferred_key_id = "Laptop", 99
    assert device_token(settings(), device) == token
    assert decode_device_token(settings(), token) == (7, 0)
    assert (
        device_subscription_url(settings(api_prefix="/custom"), device)
        == f"https://vpn.example/custom/vpn-portal/subscription/{token}"
    )
    device.token_generation = 1
    assert device_token(settings(), device) != token


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        [1],
        [1, 0, 0],
        [True, 0],
        [1, False],
        ["1", 0],
        [0, 0],
        [-1, 0],
        [1, -1],
    ],
)
def test_signed_but_malformed_payload_fails_closed(payload):
    token = URLSafeSerializer(
        SECRET, salt=SALT, signer_kwargs={"digest_method": hashlib.sha256}
    ).dumps(payload)
    with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
        decode_device_token(settings(), token)


@pytest.mark.parametrize("secret", ["", "short", "change-me-session-secret"])
def test_weak_secret_cannot_issue_or_accept_urls(secret):
    config = settings()
    token = device_token(config, VpnDevice(id=1, token_generation=0))
    config.session_secret_key = secret
    with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
        device_token(config, VpnDevice(id=1, token_generation=0))
    with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
        decode_device_token(config, token)


def test_tampering_wrong_salt_and_secret_rotation_are_rejected_without_echo():
    config = settings()
    token = device_token(config, VpnDevice(id=1, token_generation=0))
    wrong_salt = URLSafeSerializer(
        SECRET, salt="unrelated", signer_kwargs={"digest_method": hashlib.sha256}
    ).dumps([1, 0])
    for invalid in (token + "x", wrong_salt, "bad", "x" * 513, None, True):
        with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
            decode_device_token(config, invalid)
    config.session_secret_key = "another-synthetic-persistent-secret-123456789"
    with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
        decode_device_token(config, token)


@pytest.mark.asyncio
async def test_feed_uses_only_live_preferred_uri_and_keeps_old_country_while_preparing(
    session_factory,
):
    async with session_factory() as db:
        _, subscription, key, device = await seed(db)
        token = device_token(settings(), device)
        pending = VpnAccessKey(
            subscription_id=subscription.id,
            device_id=device.id,
            status="pending_sync",
            config_uri=None,
        )
        db.add(pending)
        await db.flush()
        device.pending_key_id = pending.id
        await db.commit()
        uri = await subscription_connection_uri(db, settings(), token, now=NOW)
        assert uri.startswith(URI.split("#", 1)[0] + "#")
        assert (
            base64.b64decode(encode_device_subscription(uri)).decode("utf-8")
            == uri + "\n"
        )
        assert key.config_uri == URI
        pending.status = "active"
        pending.config_uri = URI.replace("vpn.example", "pl.vpn.example")
        device.preferred_key_id, device.pending_key_id = pending.id, None
        await db.commit()
        assert "@pl.vpn.example:" in await subscription_connection_uri(
            db, settings(), token, now=NOW
        )
        assert device_token(settings(), device) == token


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case",
    [
        "customer",
        "subscription",
        "scheduled",
        "key",
        "expiry",
        "key_revoke",
        "device_revoke",
        "rotate",
        "foreign",
        "missing",
        "invalid_uri",
    ],
)
async def test_feed_rechecks_live_policy_and_binding_before_returning_secret(
    session_factory, case
):
    async with session_factory() as db:
        customer, subscription, key, device = await seed(db)
        token = device_token(settings(), device)
        if case == "customer":
            customer.status = "disabled"
        elif case == "subscription":
            subscription.expires_at = NOW
        elif case == "scheduled":
            subscription.starts_at = NOW + timedelta(minutes=1)
        elif case == "key":
            key.status = "suspended"
        elif case == "expiry":
            key.expires_at = NOW
        elif case == "key_revoke":
            key.revoke_requested_at = NOW
        elif case == "device_revoke":
            device.revoke_requested_at = NOW
        elif case == "rotate":
            device.token_generation += 1
        elif case == "foreign":
            other = VpnAccessKey(
                subscription_id=subscription.id, config_uri=URI, status="active"
            )
            db.add(other)
            await db.flush()
            device.preferred_key_id = other.id
        elif case == "missing":
            device.preferred_key_id = None
        else:
            key.config_uri = "https://not-a-vpn.example/secret"
        await db.commit()
        with pytest.raises(
            ValueError, match="^device_subscription_unavailable$"
        ) as failure:
            await subscription_connection_uri(db, settings(), token, now=NOW)
        assert token not in str(failure.value) and URI not in str(failure.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["customer", "subscription", "key", "device"])
async def test_feed_refreshes_policy_even_when_caller_cached_old_orm_rows(session_factory, target):
    async with session_factory() as cached:
        customer, subscription, key, device = await seed(cached)
        token = device_token(settings(), device)
        model, identity, values = {
            "customer": (VpnCustomer, customer.id, {"status": "blocked"}),
            "subscription": (VpnSubscription, subscription.id, {"expires_at": NOW}),
            "key": (VpnAccessKey, key.id, {"status": "pending_revoke"}),
            "device": (VpnDevice, device.id, {"revoke_requested_at": NOW}),
        }[target]
        async with session_factory() as writer:
            await writer.execute(update(model).where(model.id == identity).values(**values))
            await writer.commit()
        with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
            await subscription_connection_uri(cached, settings(), token, now=NOW)


@pytest.mark.asyncio
async def test_feed_never_uses_preferred_key_from_another_subscription(session_factory):
    async with session_factory() as db:
        customer, _, _, device = await seed(db)
        other_subscription = VpnSubscription(customer_id=customer.id, status="active")
        db.add(other_subscription)
        await db.flush()
        foreign = VpnAccessKey(subscription_id=other_subscription.id, device_id=device.id,
                               status="active", config_uri=URI)
        db.add(foreign)
        await db.flush()
        device.preferred_key_id = foreign.id
        await db.commit()
        with pytest.raises(ValueError, match="^device_subscription_unavailable$"):
            await subscription_connection_uri(db, settings(), device_token(settings(), device), now=NOW)


@pytest.mark.asyncio
async def test_hidden_endpoint_and_uncertain_pending_do_not_remove_ready_current_feed(session_factory):
    from uuid import uuid4

    from app.db.models import VpnControlOperation, VpnEndpoint, WorkerNode
    async with session_factory() as db:
        _, subscription, key, device = await seed(db)
        worker = WorkerNode(name="never-public-technical-name")
        db.add(worker)
        await db.flush()
        endpoint = VpnEndpoint(worker_id=worker.id, inbound_id=1, public_host="vpn.example", port=443,
                               is_published=False)
        db.add(endpoint)
        await db.flush()
        key.worker_id, key.endpoint_id = worker.id, endpoint.id
        pending = VpnAccessKey(subscription_id=subscription.id, device_id=device.id, worker_id=worker.id,
                               status="pending_sync", operation_generation=1)
        db.add(pending)
        await db.flush()
        device.pending_key_id = pending.id
        db.add(VpnControlOperation(id=str(uuid4()), access_key_id=pending.id, worker_id=worker.id,
                                   endpoint_id=endpoint.id, generation=1, action="provision", state="uncertain",
                                   request_snapshot={}, request_digest="a" * 64))
        await db.commit()
        assert "@vpn.example:" in await subscription_connection_uri(
            db, settings(), device_token(settings(), device), now=NOW,
        )
