"""Real row-lock proof; only the isolated, validated synthetic database is allowed."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.base import Base
from app.db.models import VpnCustomer, VpnSubscription
from app.services.vpn_lifecycle import run_vpn_lifecycle_maintenance
from test_vpn_endpoint_migrations import postgres_schema as postgres_schema


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["extension", "unlimited", "cancellation", "still_due"])
async def test_expiration_waits_for_editor_and_rechecks_committed_policy(
    postgres_schema, monkeypatch, change,
):
    engine = postgres_schema.engine
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    old_expiry = now - timedelta(minutes=1)
    new_expiry = {
        "extension": now + timedelta(days=7),
        "unlimited": None,
        "cancellation": old_expiry,
        "still_due": now - timedelta(seconds=1),
    }[change]
    new_status = "cancelled" if change == "cancellation" else "active"

    async with factory() as seed:
        customer = VpnCustomer(status="active")
        seed.add(customer)
        await seed.flush()
        target = VpnSubscription(customer_id=customer.id, status="active", expires_at=old_expiry)
        untouched = VpnSubscription(customer_id=customer.id, status="trial", expires_at=old_expiry)
        seed.add_all([target, untouched])
        await seed.commit()
        target_id, untouched_id = target.id, untouched.id

    selected, resume = asyncio.Event(), asyncio.Event()
    async with factory() as lifecycle, factory() as editor:
        stale = await lifecycle.get(VpnSubscription, target_id)
        lifecycle_pid = await lifecycle.scalar(text("SELECT pg_backend_pid()"))
        editor_pid = await editor.scalar(text("SELECT pg_backend_pid()"))
        assert lifecycle_pid != editor_pid
        original_scalars = lifecycle.scalars

        async def pause_after_candidates(statement, *args, **kwargs):
            result = await original_scalars(statement, *args, **kwargs)
            if not selected.is_set() and "vpn_subscriptions" in str(statement):
                selected.set()
                await asyncio.wait_for(resume.wait(), timeout=20)
            return result

        monkeypatch.setattr(lifecycle, "scalars", pause_after_candidates)
        maintenance = asyncio.create_task(run_vpn_lifecycle_maintenance(lifecycle, now=now))
        try:
            await asyncio.wait_for(selected.wait(), timeout=10)
            await editor.execute(
                update(VpnSubscription).where(VpnSubscription.id == target_id)
                .values(status=new_status, expires_at=new_expiry)
            )
            resume.set()
            # Prove that the real backend is waiting on our second connection;
            # scheduling events alone would also pass without a database lock.
            async with engine.connect() as monitor:
                async with asyncio.timeout(10):
                    while True:
                        blockers = await monitor.scalar(
                            text("SELECT pg_blocking_pids(:pid)"), {"pid": lifecycle_pid},
                        )
                        if editor_pid in blockers:
                            break
                        assert not maintenance.done(), "Lifecycle did not wait for the editor"
                        await asyncio.sleep(0.02)
            await editor.commit()
            result = await asyncio.wait_for(maintenance, timeout=15)
            assert result["expired_subscriptions"] == (2 if change == "still_due" else 1)
            assert stale.status == ("expired" if change == "still_due" else new_status)
            assert stale.expires_at == new_expiry
        finally:
            resume.set()
            if not maintenance.done():
                maintenance.cancel()
            await asyncio.gather(maintenance, return_exceptions=True)

    async with factory() as verifier:
        stored = await verifier.get(VpnSubscription, target_id)
        assert stored.status == ("expired" if change == "still_due" else new_status)
        assert stored.expires_at == new_expiry
        assert (await verifier.get(VpnSubscription, untouched_id)).status == "expired"
