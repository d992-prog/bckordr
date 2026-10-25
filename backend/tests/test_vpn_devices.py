from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from app.api.deps import require_admin
from app.api.routes.control import router
from app.db import models
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription
from app.db.session import get_db
from app.services.vpn_policy import count_device_slots, validate_subscription_access
from app.services.vpn_subscription_sync import stage_subscription_update
from test_vpn_policy import session_factory


async def seed(db):
    customer = VpnCustomer(status="active")
    db.add(customer)
    await db.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=2)
    db.add(subscription)
    await db.flush()
    key = VpnAccessKey(subscription_id=subscription.id, status="active", display_name="Phone",
                       external_uuid="11111111-1111-4111-8111-111111111111",
                       config_uri="vless://safe-fake@vpn.example:443#original")
    db.add(key)
    await db.flush()
    return customer, subscription, key


def test_device_schema_is_present_and_has_ownership_constraints():
    assert hasattr(models, "VpnDevice")
    table = models.VpnDevice.__table__
    assert "status" not in table.c
    assert "device_id" in VpnAccessKey.__table__.c
    assert {constraint.name for constraint in VpnAccessKey.__table__.constraints} >= {
        "fk_vpn_access_key_device_subscription", "uq_vpn_access_key_device_endpoint",
    }
    assert {constraint.name for constraint in table.constraints} >= {
        "fk_vpn_device_legacy_profile", "fk_vpn_device_preferred_key", "fk_vpn_device_pending_key",
        "ck_vpn_device_token_generation",
    }


def test_resync_bypasses_only_device_cap():
    subscription = VpnSubscription(status="active", max_devices=1)
    customer = VpnCustomer(status="active")
    assert validate_subscription_access(subscription, customer, device_slots=20,
                                        enforce_device_limit=False) is None
    subscription.expires_at = utcnow() - timedelta(seconds=1)
    assert validate_subscription_access(subscription, customer, device_slots=20,
                                        enforce_device_limit=False) == "VPN subscription has expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("child_status", ["active", "pending_sync", "suspended", "failed", "pending_revoke"])
async def test_slots_group_device_children_and_keep_legacy_fallback(session_factory, child_status):
    assert hasattr(models, "VpnDevice")
    from app.services.vpn_devices import attach_initial_device
    async with session_factory() as db:
        _, subscription, key = await seed(db)
        device = await attach_initial_device(db, key)
        child = VpnAccessKey(subscription_id=subscription.id, device_id=device.id, status=child_status)
        legacy = VpnAccessKey(subscription_id=subscription.id, status="pending_sync")
        foreign = VpnAccessKey(subscription_id=subscription.id + 100, status="active")
        db.add_all([child, legacy, foreign])
        await db.flush()
        assert await count_device_slots(db, subscription.id) == 2
        assert await count_device_slots(db, subscription.id, exclude_key_id=key.id) == 2
        key.status = "revoked"
        await db.flush()
        assert await count_device_slots(db, subscription.id) == 2
        child.status = "revoked"
        await db.flush()
        assert await count_device_slots(db, subscription.id) == 1


@pytest.mark.asyncio
async def test_attach_rename_revoke_are_shared_sticky_and_rollback_safe(session_factory):
    assert hasattr(models, "VpnDevice")
    from app.services.vpn_devices import attach_initial_device, rename_device, stage_device_revoke
    from app.services.vpn_profile_names import initialize_customer_names
    async with session_factory() as db:
        customer, subscription, key = await seed(db)
        identity = key.external_uuid, key.config_uri, key.expires_at
        device = await attach_initial_device(db, key)
        assert await attach_initial_device(db, key) is device
        assert (device.legacy_profile_id, device.preferred_key_id, device.pending_key_id) == (key.id, key.id, None)
        child = VpnAccessKey(subscription_id=subscription.id, device_id=device.id, status="suspended")
        db.add(child)
        await db.flush()
        assert await initialize_customer_names(db, customer.id) == 2
        await rename_device(db, child, "Laptop")
        assert device.display_name == key.display_name == child.display_name == "Laptop"
        assert (key.external_uuid, key.config_uri, key.expires_at) == identity
        device.pending_key_id = child.id
        await stage_device_revoke(db, child)
        assert device.revoke_requested_at and device.pending_key_id is None
        assert device.token_generation == 1
        assert key.status == child.status == "pending_revoke"
        await stage_device_revoke(db, key)
        assert device.token_generation == 1
        await stage_subscription_update(db, subscription, {"expires_at": utcnow() + timedelta(days=30)})
        assert key.status == child.status == "pending_revoke"
        key.status = "revoked"
        await db.flush()
        assert await count_device_slots(db, subscription.id) == 1
        child.status = "revoked"
        await db.flush()
        assert await count_device_slots(db, subscription.id) == 0
        await db.rollback()
        assert await db.scalar(select(func.count()).select_from(models.VpnDevice)) == 0


@pytest.mark.asyncio
async def test_linked_parent_rejects_cross_device_preferred_key(session_factory):
    assert hasattr(models, "VpnDevice")
    from app.services.vpn_devices import attach_initial_device
    async with session_factory() as db:
        _, subscription, key = await seed(db)
        device = await attach_initial_device(db, key)
        other = VpnAccessKey(subscription_id=subscription.id, status="active")
        db.add(other)
        await db.flush()
        device.preferred_key_id = other.id
        await db.flush()
        with pytest.raises(ValueError, match="vpn_device_binding_invalid"):
            await attach_initial_device(db, key)


@pytest.mark.asyncio
async def test_lower_limit_does_not_reconfigure_and_resync_is_allowed(session_factory):
    async with session_factory() as db:
        _, subscription, key = await seed(db)
        db.add(VpnAccessKey(subscription_id=subscription.id, status="suspended"))
        await db.flush()
        await stage_subscription_update(db, subscription, {"max_devices": 1})
        assert subscription.max_devices == 1
        assert key.status == "active"
        assert validate_subscription_access(subscription, await db.get(VpnCustomer, subscription.customer_id),
                                            device_slots=2) == "VPN device limit reached"


@pytest.mark.asyncio
async def test_usage_api_counts_beyond_access_key_list_cap(session_factory):
    assert hasattr(models, "VpnDevice")
    from app.services.vpn_devices import attach_initial_device
    from app.services.vpn_customer_view import list_customer_subscriptions
    async with session_factory() as db:
        customer, subscription, key = await seed(db)
        device = await attach_initial_device(db, key)
        db.add_all([VpnAccessKey(subscription_id=subscription.id, device_id=device.id, status="active")
                    for _ in range(1001)])
        db.add(VpnAccessKey(subscription_id=subscription.id, status="suspended"))
        await db.commit()
        assert (await list_customer_subscriptions(db, customer.id))[0].profiles_used == 2
    app = FastAPI()
    app.include_router(router)
    async def database():
        async with session_factory() as db:
            yield db
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_admin] = lambda: SimpleNamespace(id=1)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/control/vpn/subscriptions/1/device-usage")
        assert response.status_code == 200
        assert response.json() == {"subscription_id": 1, "devices_used": 2, "max_devices": 2}
        assert (await client.get("/control/vpn/subscriptions/999/device-usage")).status_code == 404
