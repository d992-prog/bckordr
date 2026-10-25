import importlib.util
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from test_vpn_device_locations import NOW, location_env as location_env  # noqa: PLC0414
from test_vpn_device_subscriptions import SECRET

from app.db import models
from app.db.models import VpnAccessKey, VpnControlOperation, VpnDevice
from app.services.vpn_control_intents import claim_next_vpn_control_operation, finalize_vpn_control_operation
from app.services.vpn_customer_view import list_customer_subscriptions, customer_connection, UnavailableCustomerConnection
from app.services.vpn_device_subscriptions import device_token, subscription_connection_uri
from app.services.vpn_devices import select_device_location, stage_device_revoke
from app.services.vpn_policy import count_device_slots


@pytest_asyncio.fixture(autouse=True)
async def verified_identity(location_env):
    location_env.key.verified_client_email = "vpn-key-11"
    location_env.key.panel_sub_id = uuid4().hex
    await location_env.db.commit()


def service():
    assert importlib.util.find_spec("app.services.vpn_device_operations") is not None, "persistent device operations are missing"
    from app.services import vpn_device_operations
    return vpn_device_operations


async def replace(env, request_id=None, *, now=NOW):
    return await service().request_device_operation(
        env.db, customer_id=env.customer.id, device_id=env.device.id, action="replace",
        request_id=request_id or uuid4(), settings=env.settings, now=now,
    )


async def resume(env, *, now=NOW):
    await env.db.flush()
    await service().resume_device_operations(env.db, settings=env.settings, now=now)


async def receipt(env, *, state="observed"):
    await env.db.flush()
    token = uuid4()
    control = await claim_next_vpn_control_operation(env.db, claim_token=token, now=NOW)
    assert control is not None
    await env.db.commit()
    await finalize_vpn_control_operation(
        env.db, UUID(control.id), token, receipt_state=state,
        error_code="vpn_node_mutation_uncertain" if state == "uncertain" else None, now=NOW,
    )
    await env.db.commit()
    return control


def test_credential_generations_are_separate_and_operation_identity_is_persistent():
    assert "credential_generation" in VpnDevice.__table__.c
    assert "credential_generation" in VpnAccessKey.__table__.c
    assert hasattr(models, "VpnDeviceOperation")
    assert {tuple(column.name for column in constraint.columns) for constraint in VpnAccessKey.__table__.constraints} >= {
        ("device_id", "credential_generation", "endpoint_id"),
    }
    assert ("device_id", "endpoint_id") not in {
        tuple(column.name for column in constraint.columns) for constraint in VpnAccessKey.__table__.constraints
    }


@pytest.mark.asyncio
async def test_replace_waits_for_all_old_keys_preserves_identity_and_replay(location_env):
    env = location_env
    env.settings.session_secret_key = SECRET
    token = device_token(env.settings, env.device)
    await select_device_location(env.db, customer_id=env.customer.id, device_id=env.device.id,
                                 endpoint_id=env.endpoints[1].id, settings=env.settings, now=NOW)
    country_key = await env.db.get(VpnAccessKey, env.device.pending_key_id)
    await env.db.commit()
    identity = env.device.id, env.device.legacy_profile_id
    old_uuid = env.key.external_uuid
    request_id = uuid4()
    operation = await replace(env, request_id)
    assert (await replace(env, request_id)).id == operation.id
    assert operation.stage == "revoking"
    assert env.device.revoke_requested_at is None
    assert env.device.token_generation == 1
    assert env.key.status == country_key.status == "pending_revoke"
    with pytest.raises(ValueError, match="device_subscription_unavailable"):
        await subscription_connection_uri(env.db, env.settings, token, now=NOW)
    await env.db.commit()
    await receipt(env)
    await resume(env)
    assert operation.stage == "revoking"
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey)) == 2
    await env.db.commit()
    await receipt(env)
    assert await count_device_slots(env.db, env.subscription.id) == 1
    assert (await list_customer_subscriptions(env.db, env.customer.id, now=NOW))[0].profiles_used == 1
    await resume(env)
    assert operation.stage == "provisioning"
    fresh = await env.db.get(VpnAccessKey, env.device.pending_key_id)
    assert (env.device.id, env.device.legacy_profile_id) == identity
    assert fresh.device_id == env.device.id and fresh.credential_generation == 1
    assert fresh.endpoint_id == env.key.endpoint_id
    assert fresh.external_uuid != old_uuid and env.key.external_uuid == old_uuid
    assert env.key.status == country_key.status == "revoked"
    await env.db.commit()
    await receipt(env)
    await resume(env)
    assert operation.stage == "completed"
    assert env.device.preferred_key_id == fresh.id
    assert fresh.status == "active" and await count_device_slots(env.db, env.subscription.id) == 1
    with pytest.raises(UnavailableCustomerConnection):
        await customer_connection(env.db, env.customer, env.key.id, now=NOW)
    with pytest.raises(ValueError):
        await replace(env)
    assert (await replace(env, request_id)).id == operation.id
    await select_device_location(env.db, customer_id=env.customer.id, device_id=env.device.id,
                                 endpoint_id=env.endpoints[1].id, settings=env.settings, now=NOW)
    selected = await env.db.get(VpnAccessKey, env.device.pending_key_id)
    assert selected.id != country_key.id and selected.credential_generation == 1
    assert country_key.status == "revoked" and country_key.revoke_requested_at is not None


@pytest.mark.asyncio
async def test_create_is_owned_idempotent_and_capacity_bounded(location_env):
    env = location_env
    api = service()
    request_id = uuid4()
    payload = dict(customer_id=env.customer.id, subscription_id=env.subscription.id,
                   endpoint_id=env.endpoints[1].id, display_name="Laptop", request_id=request_id,
                   settings=env.settings, now=NOW)
    with pytest.raises(ValueError, match="customer_device_limit_reached"):
        await api.request_device_creation(env.db, **payload)
    env.subscription.max_devices = 2
    await env.db.commit()
    operation = await api.request_device_creation(env.db, **payload)
    assert (await api.request_device_creation(env.db, **payload)).id == operation.id
    assert operation.stage == "provisioning"
    assert await env.db.scalar(select(func.count()).select_from(VpnDevice)) == 2
    with pytest.raises(ValueError, match="customer_device_request_conflict"):
        await api.request_device_creation(env.db, **(payload | {"display_name": "Other"}))
    with pytest.raises(LookupError):
        await api.request_device_creation(env.db, **(payload | {"subscription_id": 999, "request_id": uuid4()}))
    await env.db.commit()
    await receipt(env)
    await resume(env)
    assert operation.stage == "completed"


@pytest.mark.asyncio
async def test_uncertain_revoke_never_provisions_or_releases_slot(location_env):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    await receipt(env, state="uncertain")
    await resume(env)
    assert operation.stage == "uncertain"
    assert await count_device_slots(env.db, env.subscription.id) == 1
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey)) == 1
    with pytest.raises(ValueError, match="customer_location_unavailable"):
        await select_device_location(env.db, customer_id=env.customer.id, device_id=env.device.id,
                                     endpoint_id=env.endpoints[1].id, settings=env.settings, now=NOW)


@pytest.mark.asyncio
@pytest.mark.parametrize("after_prepare", [False, True])
async def test_operator_revoke_dominates_replacement_and_late_provision_receipt(location_env, after_prepare):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    token = None
    control = None
    if after_prepare:
        await receipt(env)
        await resume(env)
        await env.db.commit()
        token = uuid4()
        control = await claim_next_vpn_control_operation(env.db, claim_token=token, now=NOW)
        assert control is not None and control.action == "provision"
        await env.db.commit()
    await stage_device_revoke(env.db, env.key, now=NOW + timedelta(seconds=1))
    await env.db.commit()
    if control:
        await finalize_vpn_control_operation(env.db, UUID(control.id), token,
                                             receipt_state="observed", error_code=None, now=NOW + timedelta(seconds=2))
        await env.db.commit()
    await resume(env)
    assert operation.stage == "failed" and operation.finished_at is not None
    assert env.device.revoke_requested_at is not None
    children = (await env.db.scalars(select(VpnAccessKey).where(VpnAccessKey.device_id == env.device.id))).all()
    assert all(child.status in {"pending_revoke", "revoked"} for child in children)
    assert env.device.pending_key_id is None


@pytest.mark.asyncio
async def test_revoke_is_all_generations_and_does_not_touch_other_device(location_env):
    env = location_env
    other = VpnAccessKey(subscription_id=env.subscription.id, status="active", external_uuid=str(uuid4()))
    env.db.add(other)
    await env.db.flush()
    from app.services.vpn_devices import attach_initial_device
    other_device = await attach_initial_device(env.db, other)
    operation = await service().request_device_operation(
        env.db, customer_id=env.customer.id, device_id=env.device.id, action="revoke",
        request_id=uuid4(), settings=env.settings, now=NOW,
    )
    assert env.device.token_generation == 1 and other_device.token_generation == 0
    assert other.status == "active" and operation.stage == "revoking"
    await env.db.commit()
    await receipt(env)
    await resume(env)
    assert operation.stage == "completed" and other.status == "active"
    assert await count_device_slots(env.db, env.subscription.id) == 1


@pytest.mark.asyncio
async def test_stale_active_old_generation_is_not_readable_by_legacy_api(location_env):
    env = location_env
    env.device.credential_generation = 1
    env.device.preferred_key_id = None
    await env.db.commit()
    with pytest.raises(UnavailableCustomerConnection):
        await customer_connection(env.db, env.customer, env.key.id, now=NOW)
    from app.services.vpn_customer_view import list_customer_profiles
    assert not (await list_customer_profiles(env.db, env.customer, now=NOW))[0].can_connect


@pytest.mark.asyncio
async def test_replacement_retries_safe_provision_failure_without_new_key_or_feed_generation(location_env):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    await receipt(env)
    await resume(env)
    fresh_id = env.device.pending_key_id
    control = await env.db.scalar(select(VpnControlOperation).where(VpnControlOperation.access_key_id == fresh_id))
    control.state, control.error_code = "failed", "vpn_node_preflight_failed"
    await env.db.commit()
    await resume(env)
    controls = (await env.db.scalars(select(VpnControlOperation).where(VpnControlOperation.access_key_id == fresh_id))).all()
    assert len(controls) == 2 and controls[-1].state == "queued"
    assert env.device.pending_key_id == fresh_id and env.device.token_generation == 1
    await env.db.commit()
    await receipt(env)
    await resume(env)
    assert operation.stage == "completed"


@pytest.mark.asyncio
async def test_existing_lifecycle_never_turns_old_key_revoke_into_parent_revoke(location_env):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
    await run_vpn_lifecycle_maintenance(env.db, now=NOW, settings=env.settings)
    assert env.device.revoke_requested_at is None and operation.finished_at is None
    await receipt(env)
    await run_vpn_lifecycle_maintenance(env.db, now=NOW, settings=env.settings)
    assert env.device.pending_key_id is not None and env.device.token_generation == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "capacity", "hidden", "transport"])
async def test_replacement_rechecks_subscription_and_endpoint_before_new_generation(location_env, monkeypatch, change):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    await receipt(env)
    if change == "expired":
        env.subscription.expires_at = NOW
    elif change == "capacity":
        env.db.add(VpnAccessKey(subscription_id=env.subscription.id, endpoint_id=env.endpoints[0].id,
                                worker_id=env.endpoints[0].worker_id, status="active"))
    elif change == "hidden":
        env.endpoints[0].is_published = False
    else:
        from app.services.vpn_node_transport import VpnNodeTransportError
        def reject(*_):
            raise VpnNodeTransportError("private-failure")
        monkeypatch.setattr("app.services.vpn_devices.load_transport_snapshot", reject)
    await env.db.commit()
    await resume(env)
    assert operation.stage == "failed" and operation.finished_at is None
    assert env.device.credential_generation == 0 and env.device.pending_key_id is None
    assert await env.db.scalar(select(func.count()).select_from(VpnAccessKey).where(VpnAccessKey.device_id == env.device.id)) == 1
    assert await count_device_slots(env.db, env.subscription.id) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["create", "replace"])
@pytest.mark.parametrize("invalid", ["blocked", "archived", "disabled", "expired", "scheduled"])
async def test_invalid_current_customer_or_subscription_never_starts_access_change(location_env, action, invalid):
    env = location_env
    env.subscription.max_devices = 2
    if invalid in {"blocked", "archived"}:
        env.customer.status = invalid
    elif invalid == "disabled":
        env.subscription.status = "disabled"
    elif invalid == "expired":
        env.subscription.expires_at = NOW
    else:
        env.subscription.starts_at = NOW + timedelta(days=1)
    await env.db.commit()
    with pytest.raises(ValueError, match="customer_device_subscription_unavailable"):
        if action == "replace":
            await replace(env)
        else:
            await service().request_device_creation(
                env.db, customer_id=env.customer.id, subscription_id=env.subscription.id,
                endpoint_id=env.endpoints[1].id, display_name="Laptop", request_id=uuid4(), settings=env.settings, now=NOW,
            )
    assert await env.db.scalar(select(func.count()).select_from(models.VpnDeviceOperation)) == 0
    assert env.device.token_generation == 0 and env.key.status == "active"


@pytest.mark.asyncio
async def test_unbound_legacy_identity_requires_verified_revoke_and_public_error(location_env):
    env = location_env
    legacy = VpnAccessKey(subscription_id=env.subscription.id, device_id=env.device.id,
                           external_uuid=str(uuid4()), status="active")
    env.db.add(legacy)
    await env.db.commit()
    operation = await replace(env)
    assert operation.stage == "failed" and operation.public_error == "customer_device_revoke_pending"
    await env.db.commit()
    await receipt(env)
    from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
    await run_vpn_lifecycle_maintenance(env.db, now=NOW, settings=env.settings)
    assert legacy.status == "pending_revoke" and legacy.revoked_at is None
    assert env.device.pending_key_id is None and env.device.credential_generation == 0
    assert await count_device_slots(env.db, env.subscription.id) == 1


@pytest.mark.asyncio
async def test_legacy_provisioning_cannot_restore_an_old_credential_generation(location_env):
    env = location_env
    env.device.credential_generation, env.device.preferred_key_id = 1, None
    old = VpnAccessKey(subscription_id=env.subscription.id, device_id=env.device.id, status="pending_sync")
    env.db.add(old)
    await env.db.commit()
    from app.services.vpn_provisioning import provision_vpn_access_key
    await provision_vpn_access_key(env.db, old, subscription=env.subscription)
    assert old.status == "pending_revoke"


@pytest.mark.asyncio
async def test_replacing_one_device_preserves_other_country_device_credentials(location_env):
    env = location_env
    from app.services.vpn_devices import attach_initial_device
    other = VpnAccessKey(subscription_id=env.subscription.id, endpoint_id=env.endpoints[2].id,
                         worker_id=env.endpoints[2].worker_id, status="active", external_uuid=str(uuid4()),
                         config_uri=env.key.config_uri, issued_at=NOW, expires_at=env.subscription.expires_at)
    env.db.add(other)
    await env.db.flush()
    other_device = await attach_initial_device(env.db, other)
    await env.db.commit()
    before = other.external_uuid, other.config_uri, other_device.preferred_key_id, other_device.token_generation
    await replace(env)
    assert other.status == "active" and other.revoke_requested_at is None
    assert (other.external_uuid, other.config_uri, other_device.preferred_key_id, other_device.token_generation) == before
    assert await env.db.scalar(select(VpnControlOperation.id).where(VpnControlOperation.access_key_id == other.id)) is None


@pytest.mark.asyncio
async def test_revoke_removes_every_credential_generation_not_just_preferred(location_env):
    env = location_env
    newer = VpnAccessKey(subscription_id=env.subscription.id, device_id=env.device.id,
                         credential_generation=1, endpoint_id=env.endpoints[0].id,
                         worker_id=env.endpoints[0].worker_id, status="active", external_uuid=str(uuid4()),
                         verified_client_email="vpn-key-newer", panel_sub_id=uuid4().hex)
    env.db.add(newer)
    await env.db.flush()
    env.device.credential_generation, env.device.preferred_key_id = 1, newer.id
    await env.db.commit()
    operation = await service().request_device_operation(
        env.db, customer_id=env.customer.id, device_id=env.device.id, action="revoke",
        request_id=uuid4(), settings=env.settings, now=NOW,
    )
    assert env.key.status == newer.status == "pending_revoke" and operation.stage == "revoking"
    assert {control.access_key_id for control in (await env.db.scalars(select(VpnControlOperation))).all()} == {env.key.id, newer.id}


@pytest.mark.asyncio
async def test_delayed_lifecycle_old_revoke_candidate_cannot_revoke_completed_replacement(location_env, monkeypatch):
    env = location_env
    operation = await replace(env)
    await env.db.commit()
    from app.services import vpn_lifecycle
    original = vpn_lifecycle.lock_key_device_context
    delayed = False
    async def after_other_tick_finishes_replacement(db, key_id):
        nonlocal delayed
        if not delayed:
            delayed = True
            await receipt(env)
            await resume(env)
            await receipt(env)
            await resume(env)
            assert operation.stage == "completed" and env.device.revoke_requested_at is None
        return await original(db, key_id)
    monkeypatch.setattr(vpn_lifecycle, "lock_key_device_context", after_other_tick_finishes_replacement)
    await vpn_lifecycle.run_vpn_lifecycle_maintenance(env.db, now=NOW, settings=env.settings)
    fresh = await env.db.get(VpnAccessKey, env.device.preferred_key_id)
    assert delayed and fresh.credential_generation == 1 and fresh.status == "active"
    assert env.device.revoke_requested_at is None and env.device.token_generation == 1
