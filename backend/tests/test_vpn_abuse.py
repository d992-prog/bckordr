from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_vpn_usage import NOW, seed
from test_vpn_usage import factory as factory  # noqa: PLC0414

from app.db import models as m
from app.services.vpn_usage_sources import source_hash

EPOCH = "a" * 16
SECRET = "synthetic-source-secret-" * 3


@pytest.fixture
def abuse():
    try:
        return import_module("app.services.vpn_abuse")
    except ModuleNotFoundError:
        pytest.fail("complete-window abuse detector is missing")


async def device_seed(factory):
    worker, endpoint, keys = await seed(factory, count=1)
    async with factory() as db, db.begin():
        first = await db.get(m.VpnAccessKey, keys[0])
        first.created_at = first.last_synced_at = NOW - timedelta(minutes=1)
        other = m.WorkerNode(name="other-country", status="ready")
        db.add(other)
        await db.flush()
        target = m.VpnEndpoint(
            worker_id=other.id,
            inbound_id=8,
            public_host="other.example.test",
            port=443,
            protocol="vless",
            transport="tcp",
            security="none",
            verified_at=NOW,
            is_published=False,
        )
        db.add(target)
        await db.flush()
        child = m.VpnAccessKey(
            subscription_id=first.subscription_id,
            device_id=first.device_id,
            worker_id=other.id,
            endpoint_id=target.id,
            status="suspended",
            protocol="vless",
            external_uuid="11111111-2222-4333-8444-555555555555",
            verified_client_email="managed-other",
            config_uri="vless://unchanged",
            created_at=NOW - timedelta(minutes=1),
            last_synced_at=NOW - timedelta(minutes=1),
        )
        db.add(child)
        await db.flush()
        return SimpleNamespace(
            device=first.device_id,
            keys=[first.id, child.id],
            workers=[worker, other.id],
            endpoints=[endpoint, target.id],
        )


async def samples(
    factory,
    ids,
    minute,
    *,
    counts=(3, 3),
    epochs=(EPOCH, EPOCH),
    states=("ok", "ok"),
    skew=0,
    missing=(),
):
    async with factory() as db, db.begin():
        for index, (key, worker) in enumerate(zip(ids.keys, ids.workers)):
            if index in missing:
                continue
            row = await db.get(m.VpnKeySourceSample, key)
            if row is None:
                row = m.VpnKeySourceSample(access_key_id=key)
                db.add(row)
            row.worker_id = worker
            row.minute_at = minute
            row.observed_at = minute + timedelta(seconds=5 + index * skew)
            row.state = states[index]
            row.key_epoch = epochs[index] if states[index] == "ok" else None
            row.source_hashes = (
                [
                    source_hash(SECRET, ids.device, f"192.0.2.{n + 1}")
                    for n in range(counts[index])
                ]
                if states[index] == "ok"
                else None
            )


async def evaluate(abuse, factory, ids, minute, *, seconds=25):
    await abuse.evaluate_device_abuse(
        factory, ids.device, minute_at=minute, now=minute + timedelta(seconds=seconds)
    )


async def state(factory, ids):
    async with factory() as db:
        return await db.get(m.VpnDeviceAbuseState, ids.device)


async def incidents(factory):
    async with factory() as db:
        return list(
            await db.scalars(select(m.VpnAbuseIncident).order_by(m.VpnAbuseIncident.id))
        )


async def windows(abuse, factory, ids, start=0, count=5, **kwargs):
    for index in range(start, start + count):
        minute = NOW + timedelta(minutes=index)
        await samples(factory, ids, minute, **kwargs)
        await evaluate(abuse, factory, ids, minute)


@pytest.mark.asyncio
async def test_five_complete_same_minute_windows_dedupe_and_never_mutate_credentials(
    abuse, factory
):
    ids = await device_seed(factory)
    async with factory() as db:
        before = [
            (
                key.id,
                key.status,
                key.external_uuid,
                key.config_uri,
                key.operation_generation,
            )
            for key in await db.scalars(
                select(m.VpnAccessKey).order_by(m.VpnAccessKey.id)
            )
        ]
    await windows(abuse, factory, ids, count=4)
    assert await incidents(factory) == []
    await windows(abuse, factory, ids, start=4, count=1)
    rows = await incidents(factory)
    assert len(rows) == 1 and rows[0].peak_source_count == 3
    assert (await state(factory, ids)).consecutive_windows == 5
    await evaluate(abuse, factory, ids, NOW + timedelta(minutes=4))
    assert len(await incidents(factory)) == 1
    async with factory() as db:
        after = [
            (
                key.id,
                key.status,
                key.external_uuid,
                key.config_uri,
                key.operation_generation,
            )
            for key in await db.scalars(
                select(m.VpnAccessKey).order_by(m.VpnAccessKey.id)
            )
        ]
        assert before == after
        assert await db.scalar(select(m.VpnControlOperation)) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        "unsupported",
        "unavailable",
        "skew",
        "mixed_epoch",
        "unbound",
        "unverified",
        "pending_revoke",
        "uncertain",
    ],
)
async def test_every_reserved_child_must_have_complete_effective_coverage(
    abuse, factory, change
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=4)
    minute = NOW + timedelta(minutes=4)
    kwargs = {}
    if change in {"unsupported", "unavailable"}:
        kwargs["states"] = ("ok", change)
    elif change == "skew":
        kwargs["skew"] = 16
    elif change == "mixed_epoch":
        kwargs["epochs"] = (EPOCH, "b" * 16)
    else:
        async with factory() as db, db.begin():
            key = await db.get(m.VpnAccessKey, ids.keys[1])
            if change == "unbound":
                key.endpoint_id = None
            elif change == "unverified":
                key.verified_client_email = None
            elif change == "pending_revoke":
                key.status = "pending_revoke"
                kwargs["states"] = ("ok", "unavailable")
            else:
                key.status = "failed"
                kwargs["states"] = ("ok", "unavailable")
    await samples(factory, ids, minute, **kwargs)
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0
    assert await incidents(factory) == []


@pytest.mark.asyncio
async def test_collecting_can_complete_but_missing_expired_minute_cannot_bridge_streak(
    abuse, factory
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=3)
    minute = NOW + timedelta(minutes=3)
    await samples(factory, ids, minute, missing=(1,))
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).coverage == "collecting"
    await samples(factory, ids, minute)
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 4
    await samples(factory, ids, minute + timedelta(minutes=1), missing=(1,))
    await evaluate(abuse, factory, ids, minute + timedelta(minutes=1), seconds=91)
    assert (await state(factory, ids)).consecutive_windows == 0
    await windows(abuse, factory, ids, start=5, count=1)
    assert (await state(factory, ids)).consecutive_windows == 1
    assert await incidents(factory) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gap", "epoch", "two_sources", "old_receipt"])
async def test_gap_epoch_rotation_low_count_and_old_receipt_reset(abuse, factory, kind):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=4)
    minute = NOW + timedelta(minutes=5 if kind == "gap" else 4)
    await samples(
        factory,
        ids,
        minute,
        counts=(2, 2) if kind == "two_sources" else (3, 3),
        epochs=("b" * 16,) * 2 if kind == "epoch" else (EPOCH,) * 2,
    )
    await evaluate(
        abuse, factory, ids, minute, seconds=91 if kind == "old_receipt" else 25
    )
    assert (await state(factory, ids)).consecutive_windows == (
        1 if kind in {"gap", "epoch"} else 0
    )
    assert await incidents(factory) == []


@pytest.mark.asyncio
async def test_new_child_and_sticky_device_revoke_invalidate_previously_complete_minute(
    abuse, factory
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=4)
    async with factory() as db, db.begin():
        key = await db.get(m.VpnAccessKey, ids.keys[1])
        key.status = "revoked"
    minute = NOW + timedelta(minutes=4)
    await samples(factory, ids, minute)
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 1
    await windows(abuse, factory, ids, start=5, count=4)
    assert len(await incidents(factory)) == 1
    minute = NOW + timedelta(minutes=8)
    async with factory() as db, db.begin():
        device = await db.get(m.VpnDevice, ids.device)
        device.revoke_requested_at = minute
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0
    # Neither an older callback nor a late source snapshot can revive a revoked parent.
    await evaluate(abuse, factory, ids, NOW)
    assert (await state(factory, ids)).consecutive_windows == 0


@pytest.mark.asyncio
async def test_acknowledge_is_idempotent_and_requires_five_fresh_minutes(
    abuse, factory
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids)
    incident = (await incidents(factory))[0]
    async with factory() as db, db.begin():
        await abuse.acknowledge_incident(
            db, incident.id, actor_id=None, now=NOW + timedelta(minutes=4, seconds=30)
        )
    await evaluate(abuse, factory, ids, NOW + timedelta(minutes=4))
    assert (await state(factory, ids)).consecutive_windows == 0
    await windows(abuse, factory, ids, start=5, count=4)
    async with factory() as db, db.begin():
        await abuse.acknowledge_incident(
            db, incident.id, actor_id=None, now=NOW + timedelta(minutes=8, seconds=30)
        )
    assert (await state(factory, ids)).consecutive_windows == 4
    await windows(abuse, factory, ids, start=9, count=1)
    rows = await incidents(factory)
    assert (
        len(rows) == 2 and rows[0].acknowledged_at and rows[1].acknowledged_at is None
    )
    async with factory() as db:
        log = await db.scalar(select(m.AdminAuditLog))
        assert log.details == f"incident_id={incident.id};device_id={ids.device}"


@pytest.mark.asyncio
async def test_db_only_expiry_is_bounded_and_waits_for_next_minute_deadline(
    abuse, factory, monkeypatch
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=3)
    assert (
        await abuse.expire_abuse_windows(
            factory, now=NOW + timedelta(minutes=3, seconds=31)
        )
        == 0
    )
    assert (
        await abuse.expire_abuse_windows(
            factory, now=NOW + timedelta(minutes=4, seconds=31)
        )
        == 1
    )
    assert (await state(factory, ids)).consecutive_windows == 0
    monkeypatch.setattr(abuse, "ABUSE_CLEANUP_BATCH_SIZE", 1)
    async with factory() as db, db.begin():
        subscription_id = await db.scalar(
            select(m.VpnDevice.subscription_id).where(m.VpnDevice.id == ids.device)
        )
        for index in range(2):
            device = m.VpnDevice(
                subscription_id=subscription_id, display_name=f"Offline {index}"
            )
            db.add(device)
            await db.flush()
            db.add(
                m.VpnDeviceAbuseState(
                    device_id=device.id,
                    last_minute_at=NOW,
                    coverage="collecting",
                    consecutive_windows=0,
                )
            )
    assert (
        await abuse.expire_abuse_windows(factory, now=NOW + timedelta(minutes=5)) == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["generation", "identity", "new_child", "resynced_after_sample"]
)
async def test_changed_child_binding_cannot_reuse_old_fifth_window(
    abuse, factory, change
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=4)
    minute = NOW + timedelta(minutes=3)
    async with factory() as db, db.begin():
        key = await db.get(m.VpnAccessKey, ids.keys[1])
        if change == "generation":
            key.operation_generation += 1
        elif change == "identity":
            key.verified_client_email = "new-identity"
        elif change == "resynced_after_sample":
            key.last_synced_at = minute + timedelta(seconds=10)
        else:
            db.add(
                m.VpnAccessKey(
                    subscription_id=key.subscription_id,
                    device_id=ids.device,
                    worker_id=key.worker_id,
                    status="pending_sync",
                    protocol="vless",
                    external_uuid="99999999-2222-4333-8444-555555555555",
                    created_at=minute,
                )
            )
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0
    assert await incidents(factory) == []


@pytest.mark.asyncio
async def test_acknowledgement_watermark_cannot_be_reopened_by_partial_same_minute(
    abuse, factory
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids)
    minute = NOW + timedelta(minutes=4)
    row = (await incidents(factory))[0]
    async with factory() as db, db.begin():
        await abuse.acknowledge_incident(
            db, row.id, actor_id=None, now=minute + timedelta(seconds=25)
        )
        sample = await db.get(m.VpnKeySourceSample, ids.keys[1])
        await db.delete(sample)
    await evaluate(abuse, factory, ids, minute)
    await samples(factory, ids, minute)
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0


@pytest.mark.asyncio
async def test_collector_evaluates_only_after_source_apply_commits(
    abuse, factory, monkeypatch
):
    from dataclasses import replace

    from test_vpn_usage import KNOWN_HOSTS, receipt

    from app.services import vpn_usage as usage

    _, _, keys = await seed(factory, count=1)
    calls = []

    async def verify_committed(sessions, device_id, *, minute_at, now):
        async with sessions() as db:
            row = await db.get(m.VpnKeySourceSample, keys[0])
            assert row is not None and row.state == "ok"
            assert (await db.scalar(select(m.VpnNodeUsagePoll))).claim_token is None
        calls.append((device_id, minute_at))

    async def transport(_snapshot, request, **_kwargs):
        result = receipt(SimpleNamespace(request=request))
        return replace(
            result,
            keys=tuple(
                replace(key, source_state="ok", sources=("192.0.2.1",))
                for key in result.keys
            ),
        )

    monkeypatch.setattr(abuse, "evaluate_device_abuse", verify_committed)
    monkeypatch.setattr(
        usage, "get_settings", lambda: SimpleNamespace(session_secret_key=SECRET)
    )
    assert await usage.collect_usage_poll(
        factory,
        KNOWN_HOSTS,
        now=lambda: NOW,
        snapshot_loader=lambda *_: object(),
        transport=transport,
    )
    assert len(calls) == 1 and calls[0][1] == NOW


@pytest.mark.asyncio
async def test_runtime_expiry_runs_after_retention_transaction_returns(
    abuse, monkeypatch
):
    from app.services import control_runtime as runtime

    calls = []

    async def retention(*args, **kwargs):
        calls.append("retention")

    async def expiry(*args, **kwargs):
        assert calls == ["retention"]
        calls.append("expiry")

    monkeypatch.setattr(runtime, "cleanup_usage_retention", retention)
    monkeypatch.setattr(runtime, "expire_abuse_windows", expiry, raising=False)
    fake = SimpleNamespace(
        _vpn_control_database=SimpleNamespace(session_factory=object())
    )
    await runtime.ControlRuntimeOrchestrator._run_vpn_usage_cleanup(fake, NOW)
    assert calls == ["retention", "expiry"]


@pytest.mark.asyncio
async def test_replaced_same_minute_low_count_resets_and_later_peaks_update_one_incident(
    abuse, factory
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids)
    minute = NOW + timedelta(minutes=4)
    await samples(factory, ids, minute, counts=(0, 0))
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0
    assert (await state(factory, ids)).coverage == "ok"
    assert (await state(factory, ids)).last_source_count == 0
    await windows(abuse, factory, ids, start=5, count=5, counts=(4, 4))
    rows = await incidents(factory)
    assert len(rows) == 1 and rows[0].peak_source_count == 4
    assert rows[0].last_observed_at.replace(tzinfo=NOW.tzinfo) == NOW + timedelta(
        minutes=9, seconds=5
    )


@pytest.mark.asyncio
async def test_state_lock_wait_cannot_revive_an_expired_collection_window(
    abuse, factory, monkeypatch
):
    ids = await device_seed(factory)
    await windows(abuse, factory, ids, count=4)
    minute = NOW + timedelta(minutes=4)
    await samples(factory, ids, minute)
    ticks = iter((0.0, 100.0))
    monkeypatch.setattr(abuse, "monotonic", lambda: next(ticks), raising=False)
    await evaluate(abuse, factory, ids, minute)
    assert (await state(factory, ids)).consecutive_windows == 0
    assert await incidents(factory) == []
