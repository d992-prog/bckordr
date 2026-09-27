from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import importlib
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.models import AppSetting, VpnEndpoint, WorkerNode
from app.services.vpn_reality_endpoint_installer import make_endpoint_receipt


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
PUBLIC_KEY = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8"
RELEASE_ID = "b" * 64
INVALID_WORKER_BINDINGS = ((1, True), (15, None), (15, 0), (15, -1), (15, 2**63))
ENDPOINT_SNAPSHOT_FIELDS = (
    "id",
    "worker_id",
    "inbound_id",
    "public_host",
    "port",
    "protocol",
    "transport",
    "security",
    "server_name",
    "public_key",
    "short_id",
    "fingerprint",
    "flow",
    "status",
    "verified_at",
    "health_checked_at",
    "external_verified_at",
    "external_config_fingerprint",
    "last_error_code",
    "max_active_profiles",
    "capacity_warning_percent",
    "created_at",
    "updated_at",
)
SETTING_SNAPSHOT_FIELDS = ("id", "key", "value", "updated_at")


@pytest_asyncio.fixture
async def database(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'registration.sqlite3'}")
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    try:
        yield factory
    finally:
        await engine.dispose()


def _receipt(state="staged", **overrides):
    values = {
        "state": state,
        "worker_id": 15,
        "inbound_id": 27,
        "public_host": "vpn.example.test",
        "server_name": "front.example.test",
        "public_key": PUBLIC_KEY,
        "short_id": "0123456789abcdef",
    }
    values.update(overrides)
    return make_endpoint_receipt(**values)


def _acceptance(module, **overrides):
    values = {
        "release_id": RELEASE_ID,
        "receipt_digest": _receipt().receipt_digest,
        "evidence_digest": "c" * 64,
        "checked_at": NOW,
    }
    values.update(overrides)
    return module.ExternalEndpointAcceptance(**values)


def _evidence_key(module, *, worker_id=15, inbound_id=27):
    return f"{module.VPN_ENDPOINT_ACCEPTANCE_KEY}:{worker_id}:{inbound_id}"


def _snapshot(rows, fields):
    return tuple(tuple(getattr(row, field) for field in fields) for row in rows)


async def _existing_topology_snapshot(db, module):
    endpoints = await db.scalars(
        module.select(VpnEndpoint)
        .where(VpnEndpoint.worker_id == 15)
        .order_by(VpnEndpoint.id)
    )
    settings = await db.scalars(
        module.select(AppSetting)
        .where(
            AppSetting.key.in_(
                (
                    module.VPN_ENDPOINT_ACCEPTANCE_KEY,
                    module.VPN_FRIEND_BETA_RELEASE_READY_KEY,
                )
            )
        )
        .order_by(AppSetting.key)
    )
    return (
        _snapshot(endpoints.all(), ENDPOINT_SNAPSHOT_FIELDS),
        _snapshot(settings.all(), SETTING_SNAPSHOT_FIELDS),
    )


async def _seed_worker(
    factory,
    *,
    worker_id=15,
    archived_at=None,
    vpn_role="vpn_node",
):
    async with factory() as db:
        db.add(
            WorkerNode(
                id=worker_id,
                name=f"controlled-node-{worker_id}",
                status="ready",
                is_enabled=True,
                vpn_enabled=True,
                vpn_role=vpn_role,
                vpn_runtime_status="ready",
                archived_at=archived_at,
            )
        )
        db.add(
            VpnEndpoint(
                id=1,
                worker_id=worker_id,
                inbound_id=1,
                public_host="owner.example.test",
                port=8443,
                protocol="vless",
                transport="tcp",
                security="none",
                status="staged",
            )
        )
        await db.commit()


def test_registration_contract_is_importable() -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")

    acceptance = module.ExternalEndpointAcceptance(
        release_id="b" * 64,
        receipt_digest="a" * 64,
        evidence_digest="c" * 64,
        checked_at=NOW,
    )

    assert acceptance.release_id == "b" * 64
    assert acceptance.checked_at.tzinfo is UTC
    assert not hasattr(module, "CONTROLLED_WORKER_ID")


@pytest.mark.parametrize(
    "overrides",
    [
        {"release_id": "short"},
        {"receipt_digest": "A" * 64},
        {"evidence_digest": "secret probe output"},
        {"checked_at": NOW.replace(tzinfo=None)},
    ],
)
def test_acceptance_rejects_noncanonical_public_metadata(overrides: dict[str, object]) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    values: dict[str, object] = {
        "release_id": "b" * 64,
        "receipt_digest": "a" * 64,
        "evidence_digest": "c" * 64,
        "checked_at": NOW,
    }
    values.update(overrides)

    with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_acceptance_invalid$"):
        module.ExternalEndpointAcceptance(**values)


@pytest.mark.asyncio
async def test_stage_creates_exact_staged_endpoint_idempotently_and_preserves_owner_8443(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        first = await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        second = await module.stage_protected_endpoint(
            db, _receipt("observed"), controlled_worker_id=15
        )
        assert first is second
        assert first.status == "staged"
        assert first.verified_at is None
        await db.commit()

    async with database() as db:
        endpoints = list((await db.execute(module.select(VpnEndpoint).order_by(VpnEndpoint.id))).scalars())
    assert [(item.inbound_id, item.port, item.status) for item in endpoints] == [
        (1, 8443, "staged"),
        (27, 443, "staged"),
    ]


@pytest.mark.asyncio
async def test_worker_2_can_stage_and_promote_with_explicit_binding(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, worker_id=2)
    staged = _receipt(worker_id=2)
    cleaned = _receipt("acceptance_client_removed", worker_id=2)
    acceptance = _acceptance(module, receipt_digest=cleaned.receipt_digest)

    async with database() as db:
        endpoint = await module.stage_protected_endpoint(
            db,
            staged,
            controlled_worker_id=2,
        )
        promoted = await module.promote_protected_endpoint(
            db,
            cleaned,
            acceptance,
            controlled_worker_id=2,
            now=NOW,
        )
        assert promoted is endpoint
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(
            module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27)
        )
        marker = await db.scalar(
            module.select(AppSetting).where(
                AppSetting.key == module.VPN_FRIEND_BETA_RELEASE_READY_KEY
            )
        )

    assert (endpoint.worker_id, endpoint.status) == (2, "ready")
    assert marker.value == RELEASE_ID


@pytest.mark.asyncio
async def test_existing_ready_endpoint_does_not_block_second_worker_same_release(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    previous_verified_at = NOW - timedelta(days=1)
    legacy_evidence = "legacy-unscoped-sentinel"
    async with database() as db:
        db.add(
            WorkerNode(
                id=2,
                name="controlled-node-2",
                status="ready",
                is_enabled=True,
                vpn_enabled=True,
                vpn_role="vpn_node",
                vpn_runtime_status="ready",
            )
        )
        db.add(
            VpnEndpoint(
                worker_id=15,
                inbound_id=27,
                public_host="existing.example.test",
                port=443,
                protocol="vless",
                transport="raw",
                security="reality",
                server_name="existing-front.example.test",
                public_key=PUBLIC_KEY,
                short_id="fedcba9876543210",
                fingerprint="chrome",
                flow="xtls-rprx-vision",
                status="ready",
                verified_at=previous_verified_at,
            )
        )
        db.add(
            AppSetting(
                key=module.VPN_FRIEND_BETA_RELEASE_READY_KEY,
                value=RELEASE_ID,
            )
        )
        db.add(AppSetting(key=module.VPN_ENDPOINT_ACCEPTANCE_KEY, value=legacy_evidence))
        await db.commit()
        before_endpoints, before_settings = await _existing_topology_snapshot(
            db, module
        )
    assert len(before_endpoints) == 2
    assert len(before_settings) == 2

    staged = _receipt(worker_id=2)
    cleaned = _receipt("acceptance_client_removed", worker_id=2)
    async with database() as db:
        await module.stage_protected_endpoint(db, staged, controlled_worker_id=2)
        await module.promote_protected_endpoint(
            db,
            cleaned,
            _acceptance(module, receipt_digest=cleaned.receipt_digest),
            controlled_worker_id=2,
            now=NOW,
        )
        await db.commit()

    async with database() as db:
        after_endpoints, after_settings = await _existing_topology_snapshot(
            db, module
        )
        worker_2 = await db.scalar(
            module.select(VpnEndpoint).where(
                VpnEndpoint.worker_id == 2,
                VpnEndpoint.inbound_id == 27,
            )
        )
        settings = {
            setting.key: setting.value
            for setting in (await db.scalars(module.select(AppSetting))).all()
        }

    assert after_endpoints == before_endpoints
    assert after_settings == before_settings
    verified_at = worker_2.verified_at
    assert verified_at is not None
    verified_at = (
        verified_at.replace(tzinfo=UTC)
        if verified_at.tzinfo is None
        else verified_at.astimezone(UTC)
    )
    assert (worker_2.status, verified_at) == ("ready", NOW)
    assert settings[module.VPN_FRIEND_BETA_RELEASE_READY_KEY] == RELEASE_ID
    assert settings[module.VPN_ENDPOINT_ACCEPTANCE_KEY] == legacy_evidence
    assert '"worker_id":2' in settings[_evidence_key(module, worker_id=2)]


@pytest.mark.asyncio
async def test_stage_rejects_worker_binding_mismatch_without_writes(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, worker_id=2)

    async with database() as db:
        with pytest.raises(
            module.EndpointRegistrationError,
            match="^vpn_endpoint_registration_worker_unavailable$",
        ):
            await module.stage_protected_endpoint(
                db,
                _receipt(worker_id=2),
                controlled_worker_id=15,
            )
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(
            module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27)
        )
        setting = await db.scalar(module.select(AppSetting))

    assert endpoint is None
    assert setting is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_id", "controlled_worker_id"),
    INVALID_WORKER_BINDINGS,
)
async def test_stage_rejects_invalid_worker_binding_without_writes(
    database,
    worker_id: int,
    controlled_worker_id: object,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, worker_id=worker_id)

    async with database() as db:
        with pytest.raises(
            module.EndpointRegistrationError,
            match="^vpn_endpoint_registration_worker_unavailable$",
        ):
            await module.stage_protected_endpoint(
                db,
                _receipt(worker_id=worker_id),
                controlled_worker_id=controlled_worker_id,
            )
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(
            module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27)
        )
        setting = await db.scalar(module.select(AppSetting))

    assert endpoint is None
    assert setting is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker_id", "controlled_worker_id"),
    ((2, 15), *INVALID_WORKER_BINDINGS),
)
async def test_promotion_rejects_worker_binding_without_marker(
    database,
    worker_id: int,
    controlled_worker_id: object,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, worker_id=worker_id)
    cleaned = _receipt("observed", worker_id=worker_id)

    async with database() as db:
        await module.stage_protected_endpoint(
            db,
            _receipt(worker_id=worker_id),
            controlled_worker_id=worker_id,
        )
        await db.commit()

    async with database() as db:
        with pytest.raises(
            module.EndpointRegistrationError,
            match="^vpn_endpoint_registration_worker_unavailable$",
        ):
            await module.promote_protected_endpoint(
                db,
                cleaned,
                _acceptance(module, receipt_digest=cleaned.receipt_digest),
                controlled_worker_id=controlled_worker_id,
                now=NOW,
            )
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(
            module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27)
        )
        settings = list((await db.scalars(module.select(AppSetting))).all())

    assert endpoint.status == "staged"
    assert endpoint.verified_at is None
    assert settings == []


@pytest.mark.asyncio
async def test_stage_accepts_enabled_legacy_dual_role_worker(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, vpn_role="drop_worker_vpn")

    async with database() as db:
        endpoint = await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )

    assert endpoint.worker_id == 15
    assert endpoint.status == "staged"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conflicting_inbound_id", "conflicting_port"),
    ((99, 443), (27, 444)),
)
async def test_stage_rejects_archived_worker_and_same_worker_conflicts(
    database,
    conflicting_inbound_id: int,
    conflicting_port: int,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, archived_at=NOW)
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_worker_unavailable$"):
            await module.stage_protected_endpoint(
                db, _receipt(), controlled_worker_id=15
            )
        await db.rollback()

    async with database() as db:
        worker = await db.get(WorkerNode, 15)
        worker.archived_at = None
        db.add(VpnEndpoint(worker_id=15, inbound_id=conflicting_inbound_id, public_host="other.example.test", port=conflicting_port, protocol="vless", transport="raw", security="reality", server_name="other-front.example.test", public_key=PUBLIC_KEY, short_id="aabb", fingerprint="chrome", flow="xtls-rprx-vision", status="staged"))
        await db.commit()
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_conflict$"):
            await module.stage_protected_endpoint(
                db, _receipt(), controlled_worker_id=15
            )


@pytest.mark.asyncio
async def test_promotion_atomically_sets_ready_acceptance_metadata_and_exact_marker(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        endpoint = await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        await db.commit()
        endpoint_id = endpoint.id

    cleaned = _receipt("acceptance_client_removed")
    async with database() as db:
        promoted = await module.promote_protected_endpoint(
            db,
            cleaned,
            _acceptance(module),
            controlled_worker_id=15,
            now=NOW + timedelta(minutes=1),
        )
        assert promoted.status == "ready"
        assert promoted.verified_at == NOW
        await db.commit()

    async with database() as db:
        endpoint = await db.get(VpnEndpoint, endpoint_id)
        marker = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_FRIEND_BETA_RELEASE_READY_KEY))
        evidence = await db.scalar(
            module.select(AppSetting).where(AppSetting.key == _evidence_key(module))
        )
    assert endpoint.status == "ready"
    assert marker.value == RELEASE_ID
    assert '"receipt_digest":"' + cleaned.receipt_digest + '"' in evidence.value
    assert '"evidence_digest":"' + "c" * 64 + '"' in evidence.value
    assert "uuid" not in evidence.value.lower()
    assert "private" not in evidence.value.lower()


@pytest.mark.asyncio
async def test_promotion_requires_cleaned_or_observed_receipt_and_fresh_matching_acceptance(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        await db.commit()

    cases = [
        (_receipt("acceptance_client_present"), _acceptance(module), "vpn_endpoint_registration_not_clean"),
        (_receipt("observed"), _acceptance(module, receipt_digest="d" * 64), "vpn_endpoint_registration_acceptance_mismatch"),
        (_receipt("observed"), _acceptance(module, checked_at=NOW - timedelta(hours=1)), "vpn_endpoint_registration_acceptance_stale"),
    ]
    for receipt, acceptance, code in cases:
        async with database() as db:
            with pytest.raises(module.EndpointRegistrationError, match=f"^{code}$"):
                await module.promote_protected_endpoint(
                    db,
                    receipt,
                    acceptance,
                    controlled_worker_id=15,
                    now=NOW,
                )
            await db.rollback()


@pytest.mark.asyncio
async def test_marker_conflict_rolls_back_ready_and_acceptance_metadata(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        db.add(AppSetting(key=module.VPN_FRIEND_BETA_RELEASE_READY_KEY, value="d" * 64))
        await db.commit()

    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_release_conflict$"):
            await module.promote_protected_endpoint(
                db,
                _receipt("observed"),
                _acceptance(module),
                controlled_worker_id=15,
                now=NOW,
            )
        await db.rollback()

    async with database() as db:
        endpoint = await db.scalar(module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27))
        evidence = await db.scalar(
            module.select(AppSetting).where(AppSetting.key == _evidence_key(module))
        )
    assert endpoint.status == "staged"
    assert endpoint.verified_at is None
    assert evidence is None


@pytest.mark.asyncio
async def test_exact_repeat_promotion_is_idempotent_but_changed_evidence_is_rejected(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    receipt = _receipt("acceptance_client_removed")
    acceptance = _acceptance(module)
    async with database() as db:
        await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        await module.promote_protected_endpoint(
            db,
            receipt,
            acceptance,
            controlled_worker_id=15,
            now=NOW,
        )
        await db.commit()
    async with database() as db:
        repeated = await module.promote_protected_endpoint(
            db,
            receipt,
            acceptance,
            controlled_worker_id=15,
            now=NOW + timedelta(days=1),
        )
        assert repeated.status == "ready"
        await db.rollback()
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_acceptance_conflict$"):
            await module.promote_protected_endpoint(
                db,
                receipt,
                replace(acceptance, evidence_digest="e" * 64),
                controlled_worker_id=15,
                now=NOW,
            )


@pytest.mark.asyncio
async def test_expected_acceptance_conflict_leaves_no_pending_marker_if_caller_continues(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    receipt = _receipt("observed")
    async with database() as db:
        await module.stage_protected_endpoint(
            db, _receipt(), controlled_worker_id=15
        )
        db.add(AppSetting(key=_evidence_key(module), value="different"))
        await db.commit()

    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_acceptance_conflict$"):
            await module.promote_protected_endpoint(
                db,
                receipt,
                _acceptance(module),
                controlled_worker_id=15,
                now=NOW,
            )
        # A caller may handle a policy conflict and continue its transaction.
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27))
        marker = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_FRIEND_BETA_RELEASE_READY_KEY))
    assert endpoint.status == "staged"
    assert marker is None
