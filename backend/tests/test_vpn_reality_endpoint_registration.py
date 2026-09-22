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


async def _seed_worker(factory, *, archived_at=None):
    async with factory() as db:
        db.add(
            WorkerNode(
                id=15,
                name="controlled-node-15",
                status="ready",
                is_enabled=True,
                vpn_enabled=True,
                vpn_role="vpn_node",
                vpn_runtime_status="ready",
                archived_at=archived_at,
            )
        )
        db.add(
            VpnEndpoint(
                id=1,
                worker_id=15,
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
        first = await module.stage_protected_endpoint(db, _receipt())
        second = await module.stage_protected_endpoint(db, _receipt("observed"))
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
async def test_stage_rejects_archived_worker_and_port_or_identity_conflicts(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database, archived_at=NOW)
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_worker_unavailable$"):
            await module.stage_protected_endpoint(db, _receipt())
        await db.rollback()

    async with database() as db:
        worker = await db.get(WorkerNode, 15)
        worker.archived_at = None
        db.add(VpnEndpoint(worker_id=15, inbound_id=99, public_host="other.example.test", port=443, protocol="vless", transport="raw", security="reality", server_name="other-front.example.test", public_key=PUBLIC_KEY, short_id="aabb", fingerprint="chrome", flow="xtls-rprx-vision", status="staged"))
        await db.commit()
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_conflict$"):
            await module.stage_protected_endpoint(db, _receipt())


@pytest.mark.asyncio
async def test_promotion_atomically_sets_ready_acceptance_metadata_and_exact_marker(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        endpoint = await module.stage_protected_endpoint(db, _receipt())
        await db.commit()
        endpoint_id = endpoint.id

    cleaned = _receipt("acceptance_client_removed")
    async with database() as db:
        promoted = await module.promote_protected_endpoint(
            db,
            cleaned,
            _acceptance(module),
            now=NOW + timedelta(minutes=1),
        )
        assert promoted.status == "ready"
        assert promoted.verified_at == NOW
        await db.commit()

    async with database() as db:
        endpoint = await db.get(VpnEndpoint, endpoint_id)
        marker = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_FRIEND_BETA_RELEASE_READY_KEY))
        evidence = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_ENDPOINT_ACCEPTANCE_KEY))
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
        await module.stage_protected_endpoint(db, _receipt())
        await db.commit()

    cases = [
        (_receipt("acceptance_client_present"), _acceptance(module), "vpn_endpoint_registration_not_clean"),
        (_receipt("observed"), _acceptance(module, receipt_digest="d" * 64), "vpn_endpoint_registration_acceptance_mismatch"),
        (_receipt("observed"), _acceptance(module, checked_at=NOW - timedelta(hours=1)), "vpn_endpoint_registration_acceptance_stale"),
    ]
    for receipt, acceptance, code in cases:
        async with database() as db:
            with pytest.raises(module.EndpointRegistrationError, match=f"^{code}$"):
                await module.promote_protected_endpoint(db, receipt, acceptance, now=NOW)
            await db.rollback()


@pytest.mark.asyncio
async def test_marker_conflict_rolls_back_ready_and_acceptance_metadata(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    async with database() as db:
        await module.stage_protected_endpoint(db, _receipt())
        db.add(AppSetting(key=module.VPN_FRIEND_BETA_RELEASE_READY_KEY, value="d" * 64))
        await db.commit()

    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_release_conflict$"):
            await module.promote_protected_endpoint(db, _receipt("observed"), _acceptance(module), now=NOW)
        await db.rollback()

    async with database() as db:
        endpoint = await db.scalar(module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27))
        evidence = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_ENDPOINT_ACCEPTANCE_KEY))
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
        await module.stage_protected_endpoint(db, _receipt())
        await module.promote_protected_endpoint(db, receipt, acceptance, now=NOW)
        await db.commit()
    async with database() as db:
        repeated = await module.promote_protected_endpoint(
            db, receipt, acceptance, now=NOW + timedelta(days=1)
        )
        assert repeated.status == "ready"
        await db.rollback()
    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_acceptance_conflict$"):
            await module.promote_protected_endpoint(
                db,
                receipt,
                replace(acceptance, evidence_digest="e" * 64),
                now=NOW,
            )


@pytest.mark.asyncio
async def test_expected_acceptance_conflict_leaves_no_pending_marker_if_caller_continues(database) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_registration")
    await _seed_worker(database)
    receipt = _receipt("observed")
    async with database() as db:
        await module.stage_protected_endpoint(db, _receipt())
        db.add(AppSetting(key=module.VPN_ENDPOINT_ACCEPTANCE_KEY, value="different"))
        await db.commit()

    async with database() as db:
        with pytest.raises(module.EndpointRegistrationError, match="^vpn_endpoint_registration_acceptance_conflict$"):
            await module.promote_protected_endpoint(db, receipt, _acceptance(module), now=NOW)
        # A caller may handle a policy conflict and continue its transaction.
        await db.commit()

    async with database() as db:
        endpoint = await db.scalar(module.select(VpnEndpoint).where(VpnEndpoint.inbound_id == 27))
        marker = await db.scalar(module.select(AppSetting).where(AppSetting.key == module.VPN_FRIEND_BETA_RELEASE_READY_KEY))
    assert endpoint.status == "staged"
    assert marker is None
