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
from test_vpn_friend_invitations import ready_session, NOW, _token_from_link


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


@pytest.mark.asyncio
async def test_legacy_provision_cannot_restore_device_latched_through_another_child(session_factory, monkeypatch):
    from app.services.vpn_devices import attach_initial_device, stage_device_revoke
    from app.services.vpn_provisioning import provision_vpn_access_key
    async with session_factory() as db:
        _, subscription, key = await seed(db)
        await attach_initial_device(db, key)
        await stage_device_revoke(db, key)
        key.status = "failed"  # Simulate a stale child retry after permanent revoke.
        await db.flush()
        await provision_vpn_access_key(db, key, subscription=subscription)
        assert key.status == "pending_revoke"


@pytest.mark.asyncio
async def test_subscription_device_limit_is_not_sent_to_legacy_ip_policy(session_factory, monkeypatch):
    from app.services import vpn_provisioning
    from app.db.models import WorkerNode
    payloads = []
    monkeypatch.setattr(vpn_provisioning, "build_vpn_client_provision_command",
                        lambda worker, payload: payloads.append(payload) or "safe-fake")
    async def transport(*args):
        return "DROPCATCH_VPN_CLIENT_STATUS=provisioned\nDROPCATCH_VPN_CLIENT_URL=vless://safe-fake@vpn.example:443"
    monkeypatch.setattr(vpn_provisioning, "execute_worker_ssh_commands", transport)
    async with session_factory() as db:
        _, subscription, key = await seed(db)
        worker = WorkerNode(name="safe-fake", ssh_host="192.0.2.1", ssh_password="safe-fake", vpn_inbound_id=1)
        db.add(worker)
        await db.flush()
        await vpn_provisioning.provision_vpn_access_key(db, key, subscription=subscription, worker=worker)
        assert payloads[0].max_devices == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "archive"])
async def test_permanent_policy_latches_even_already_revoked_devices(session_factory, operation):
    from app.services.vpn_devices import attach_initial_device
    from app.services.vpn_customer_lifecycle import stage_vpn_customer_archive
    async with session_factory() as db:
        customer, subscription, key = await seed(db)
        device = await attach_initial_device(db, key)
        key.status = "revoked"
        await db.flush()
        if operation == "cancel":
            await stage_subscription_update(db, subscription, {"status": "cancelled"})
        else:
            await stage_vpn_customer_archive(db, customer.id)
        assert device.revoke_requested_at is not None
        assert device.token_generation == 1
        assert key.status == "revoked"


async def create_profile(ready_session, monkeypatch, creator):
    from app.api.routes import control
    from app.db.models import AppSetting, VpnPlan
    from app.schemas.control import VpnAccessKeyCreateRequest
    from app.services import vpn_friend_invitations as friends, vpn_public_trial as trial
    from app.services.vpn_telegram_identity import TelegramIdentity
    from test_vpn_public_trial import trial_settings
    db, settings, _ = ready_session
    monkeypatch.setattr(control, "utcnow", lambda: NOW)
    if creator == "friend":
        issued = await friends.issue_friend_invitation(db, settings, actor_user_id=1, now=NOW)
        token, identity = _token_from_link(issued.link), TelegramIdentity("800001")
        result = await friends.redeem_friend_invitation(db, settings, token, identity, NOW)
        async def replay():
            return await friends.redeem_friend_invitation(db, settings, token, identity, NOW)
        return await db.get(VpnAccessKey, result.access_key_id), replay
    if creator == "trial":
        db.add_all([VpnPlan(slug="trial-7d", name="Trial", duration_days=7, max_devices=1),
                    AppSetting(key="vpn_public_release_ready_v1", value="a" * 64)])
        await db.flush()
        monkeypatch.setattr(trial, "load_transport_snapshot", lambda *_: object())
        settings, identity = trial_settings(), TelegramIdentity("800002")
        result = await trial.activate_public_trial(db, settings, identity, NOW)
        async def replay():
            return await trial.activate_public_trial(db, settings, identity, NOW)
        return await db.get(VpnAccessKey, result.access_key_id), replay
    customer = VpnCustomer(status="active")
    db.add(customer)
    await db.flush()
    subscription = VpnSubscription(customer_id=customer.id, status="active", max_devices=1)
    db.add(subscription)
    await db.flush()
    result = await control.create_vpn_access_key(VpnAccessKeyCreateRequest(subscription_id=subscription.id),
                                                 db, SimpleNamespace(id=1))
    async def replay():
        return await control.provision_existing_vpn_access_key(result.id, db, SimpleNamespace(id=1))
    return await db.get(VpnAccessKey, result.id), replay


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", ["admin", "friend", "trial"])
async def test_all_creators_attach_one_device_and_retry_keeps_identity(ready_session, monkeypatch, creator):
    db, _, _ = ready_session
    key, replay = await create_profile(ready_session, monkeypatch, creator)
    identity = key.external_uuid, key.config_uri, key.expires_at, key.worker_id, key.endpoint_id
    parent = await db.get(models.VpnDevice, key.device_id)
    assert parent.legacy_profile_id == parent.preferred_key_id == key.id
    assert parent.subscription_id == key.subscription_id
    assert parent.display_name == key.display_name
    await replay()
    assert (key.external_uuid, key.config_uri, key.expires_at, key.worker_id, key.endpoint_id) == identity
    assert await db.scalar(select(func.count()).select_from(models.VpnDevice)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("creator", ["admin", "friend", "trial"])
async def test_creator_failure_rolls_back_parent_with_key(ready_session, monkeypatch, creator):
    from app.api.routes import control
    from app.services import vpn_friend_invitations as friends, vpn_public_trial as trial
    from app.services.vpn_control_intents import VpnControlIntentError
    async def reject(*args, **kwargs):
        raise VpnControlIntentError("vpn_control_worker_busy")
    monkeypatch.setattr({"admin": control, "friend": friends, "trial": trial}[creator],
                        "stage_vpn_control_operation", reject)
    db, _, _ = ready_session
    with pytest.raises(Exception):
        await create_profile(ready_session, monkeypatch, creator)
    await db.rollback()
    assert await db.scalar(select(func.count()).select_from(models.VpnDevice)) == 0
    assert await db.scalar(select(func.count()).select_from(VpnAccessKey)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["admin", "delete", "friend", "cancel", "archive"])
async def test_explicit_revoke_sources_latch_all_children(ready_session, monkeypatch, source):
    from app.api.routes import control
    from app.services.vpn_customer_lifecycle import stage_vpn_customer_archive
    from app.services.vpn_friend_invitations import disable_friend_invitation
    db, _, _ = ready_session
    key, _ = await create_profile(ready_session, monkeypatch, "friend")
    child = VpnAccessKey(subscription_id=key.subscription_id, device_id=key.device_id, status="suspended")
    db.add(child)
    await db.flush()
    subscription = await db.get(VpnSubscription, key.subscription_id)
    if source in {"admin", "delete"}:
        route = control.revoke_existing_vpn_access_key if source == "admin" else control.delete_vpn_access_key
        await route(key.id if source == "admin" else child.id, db, SimpleNamespace(id=1))
    elif source == "friend":
        await disable_friend_invitation(db, 1, NOW)
    elif source == "cancel":
        await stage_subscription_update(db, subscription, {"status": "cancelled"})
    else:
        await stage_vpn_customer_archive(db, subscription.customer_id)
    assert key.status == child.status == "pending_revoke"
    parent = await db.get(models.VpnDevice, key.device_id)
    assert parent.revoke_requested_at and parent.token_generation == 1
    assert await count_device_slots(db, key.subscription_id) == 1
    await stage_subscription_update(db, subscription, {"expires_at": utcnow() + timedelta(days=30)})
    assert key.status == child.status == "pending_revoke"


@pytest.mark.asyncio
async def test_strict_retry_rechecks_parent_latch_when_child_state_is_stale(ready_session, monkeypatch):
    from app.services.vpn_control_intents import stage_vpn_control_operation, VpnControlIntentError
    from app.services.vpn_devices import stage_device_revoke
    db, _, _ = ready_session
    key, _ = await create_profile(ready_session, monkeypatch, "friend")
    await stage_device_revoke(db, key)
    key.status, key.revoke_requested_at = "pending_sync", None
    await db.flush()
    with pytest.raises(VpnControlIntentError, match="vpn_control_revoke_sticky"):
        await stage_vpn_control_operation(db, key.id, "provision", now=NOW)
    parent = await db.get(models.VpnDevice, key.device_id)
    assert parent.revoke_requested_at and parent.token_generation == 1
    assert await count_device_slots(db, key.subscription_id) == 1
