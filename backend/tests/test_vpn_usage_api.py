from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, insert
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api import api_router
from app.api.deps import get_current_user, require_admin
from app.db import models as m
from app.db.base import Base, utcnow
from app.db.session import get_db

NODES = "/api/control/vpn/usage/nodes"
CUSTOMER = "/api/control/vpn/customers/{}/usage"
TOTAL_FIELDS = {
    "upload_bytes",
    "download_bytes",
    "measured_since",
    "last_sample_at",
    "coverage",
}


def report_app(sessions):
    app = FastAPI()
    app.include_router(api_router, prefix="/api")

    async def database():
        async with sessions() as db:
            yield db

    async def admin():
        return SimpleNamespace(id=1, role="admin")

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[require_admin] = admin
    return app


@pytest_asyncio.fixture
async def env():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = report_app(sessions)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        yield SimpleNamespace(app=app, sessions=sessions, engine=engine, client=client)
    await engine.dispose()


async def seed(env, *, count=1):
    now = utcnow()
    async with env.sessions() as db:
        customer = m.VpnCustomer(telegram_user_id="synthetic-private-id")
        foreign = m.VpnCustomer()
        workers = [
            m.WorkerNode(
                name=f"worker-{index}",
                vpn_enabled=True,
                vpn_role="vpn_node",
                last_heartbeat_at=now,
                cpu_load=1.5,
                ram_usage_percent=34,
            )
            for index in range(count)
        ]
        db.add_all([customer, foreign, *workers])
        await db.flush()
        subscription = m.VpnSubscription(customer_id=customer.id)
        db.add(subscription)
        await db.flush()
        device = m.VpnDevice(
            subscription_id=subscription.id,
            display_name="Laptop",
            created_at=now - timedelta(minutes=1),
        )
        db.add(device)
        await db.flush()
        endpoints = []
        keys = []
        for worker in workers:
            endpoint = m.VpnEndpoint(
                worker_id=worker.id,
                inbound_id=1,
                public_host="private.example",
                port=443,
                max_active_profiles=10,
            )
            db.add(endpoint)
            await db.flush()
            key = m.VpnAccessKey(
                subscription_id=subscription.id,
                device_id=device.id,
                worker_id=worker.id,
                endpoint_id=endpoint.id,
                status="active",
                created_at=now - timedelta(minutes=1),
                config_uri="vless://private-secret",
                verified_client_email=f"private-{worker.id}",
            )
            db.add(key)
            await db.flush()
            keys.append(key.id)
            endpoints.append(endpoint.id)
        await db.commit()
        return SimpleNamespace(
            customer=customer.id,
            foreign=foreign.id,
            subscription=subscription.id,
            device=device.id,
            workers=[w.id for w in workers],
            keys=keys,
            endpoints=endpoints,
            now=now,
        )


def hour(key, worker, now, *, up=10, down=20, minutes=1):
    return m.VpnUsageHour(
        access_key_id=key,
        worker_id=worker,
        hour_start=now.replace(minute=0, second=0, microsecond=0),
        upload_bytes=up,
        download_bytes=down,
        sample_count=minutes,
        first_sample_at=now - timedelta(minutes=minutes),
        last_sample_at=now,
    )


def sample(key, worker, now):
    return m.VpnKeyUsageSample(
        access_key_id=key,
        worker_id=worker,
        sampled_at=now,
        counter_epoch="a" * 64,
        record_id=key,
        reset_count=0,
        upload_bytes=1000,
        download_bytes=2000,
    )


def poll(worker, now, *, state="ok"):
    return m.VpnNodeUsagePoll(
        worker_id=worker,
        last_attempt_at=now,
        last_success_at=now,
        traffic_state=state,
        protocol_version=1,
    )


@pytest.mark.asyncio
async def test_reports_are_registered_and_admin_only(env):
    ids = await seed(env)
    assert (await env.client.get(NODES)).status_code == 200
    assert (await env.client.get(CUSTOMER.format(ids.customer))).status_code == 200
    env.app.dependency_overrides.pop(require_admin)
    assert (await env.client.get(NODES)).status_code == 401
    assert (await env.client.get(CUSTOMER.format(ids.customer))).status_code == 401

    async def ordinary_user():
        return SimpleNamespace(id=2, role="user")

    env.app.dependency_overrides[get_current_user] = ordinary_user
    assert (await env.client.get(NODES)).status_code == 403
    assert (await env.client.get(CUSTOMER.format(ids.customer))).status_code == 403


@pytest.mark.asyncio
async def test_days_and_pagination_are_literal_bounded_and_exclusive(env):
    ids = await seed(env, count=3)
    for days in (1, 7, 30):
        assert (await env.client.get(NODES, params={"days": days})).status_code == 200
        assert (
            await env.client.get(CUSTOMER.format(ids.customer), params={"days": days})
        ).json()["days"] == days
    for query in (
        "days=0",
        "days=2",
        "days=31",
        "days=1.0",
        "days=true",
        "limit=0",
        "limit=101",
        "after_id=-1",
    ):
        assert (await env.client.get(NODES + "?" + query)).status_code == 422
    for days in (0, 2, 31):
        assert (
            await env.client.get(CUSTOMER.format(ids.customer), params={"days": days})
        ).status_code == 422
    first = (await env.client.get(NODES + "?limit=2")).json()
    second = (
        await env.client.get(
            NODES, params={"after_id": first[-1]["worker_id"], "limit": 2}
        )
    ).json()
    assert [row["worker_id"] for row in first + second] == ids.workers


@pytest.mark.asyncio
async def test_baseline_is_not_measured_zero_and_no_data_does_not_leak_secrets(env):
    ids = await seed(env)
    row = (await env.client.get(NODES)).json()[0]
    assert row["coverage"] == "none" and row["upload_bytes"] is None
    async with env.sessions() as db:
        db.add_all(
            [
                sample(ids.keys[0], ids.workers[0], ids.now),
                poll(ids.workers[0], ids.now, state="partial"),
            ]
        )
        await db.commit()
    response = await env.client.get(CUSTOMER.format(ids.customer))
    assert response.json()["upload_bytes"] is None
    assert response.json()["coverage"] == "partial"
    assert response.json()["last_sample_at"] is not None
    assert set(response.json()) == TOTAL_FIELDS | {"customer_id", "days", "devices"}
    assert set(response.json()["devices"][0]) == TOTAL_FIELDS | {
        "device_id",
        "display_name",
    }
    assert set(row) == TOTAL_FIELDS | {
        "worker_id",
        "active_devices",
        "occupied_profiles",
        "max_active_profiles",
        "cpu_load",
        "ram_usage_percent",
    }
    for secret in (
        "private",
        "vless://",
        "external_uuid",
        "ip_address",
        "source_hashes",
    ):
        assert secret not in response.text
    async with env.sessions() as db:
        db.add(hour(ids.keys[0], ids.workers[0], ids.now, up=0, down=0))
        await db.commit()
    measured = (await env.client.get(CUSTOMER.format(ids.customer))).json()
    assert measured["upload_bytes"] == measured["download_bytes"] == 0
    assert measured["measured_since"] is not None


@pytest.mark.asyncio
async def test_customer_sums_all_children_subscriptions_and_periods_without_foreign_rows(
    env,
):
    ids = await seed(env, count=2)
    async with env.sessions() as db:
        subscription = m.VpnSubscription(customer_id=ids.customer)
        db.add(subscription)
        await db.flush()
        device = m.VpnDevice(subscription_id=subscription.id, display_name="Old phone")
        db.add(device)
        await db.flush()
        key = m.VpnAccessKey(
            subscription_id=subscription.id,
            device_id=device.id,
            worker_id=ids.workers[0],
            status="revoked",
        )
        db.add(key)
        await db.flush()
        db.add_all(
            [
                hour(ids.keys[0], ids.workers[0], ids.now, up=1, down=2),
                hour(ids.keys[1], ids.workers[1], ids.now, up=3, down=4),
                hour(key.id, ids.workers[0], ids.now - timedelta(days=2), up=5, down=6),
                hour(
                    key.id, ids.workers[0], ids.now - timedelta(days=10), up=7, down=8
                ),
                hour(
                    key.id,
                    ids.workers[0],
                    ids.now - timedelta(days=31),
                    up=900,
                    down=900,
                ),
            ]
        )
        await db.commit()
    for days, expected in ((1, (4, 6)), (7, (9, 12)), (30, (16, 20))):
        body = (
            await env.client.get(CUSTOMER.format(ids.customer), params={"days": days})
        ).json()
        assert (body["upload_bytes"], body["download_bytes"]) == expected
        assert len(body["devices"]) == 2
        assert body["devices"][0]["upload_bytes"] == 4
    foreign = (await env.client.get(CUSTOMER.format(ids.foreign))).json()
    assert foreign["devices"] == [] and foreign["upload_bytes"] is None
    assert (await env.client.get(CUSTOMER.format(999999))).status_code == 404


@pytest.mark.asyncio
async def test_node_uses_recorded_hour_worker_distinct_devices_and_summed_capacity(env):
    ids = await seed(env, count=2)
    async with env.sessions() as db:
        endpoint = m.VpnEndpoint(
            worker_id=ids.workers[0],
            inbound_id=2,
            public_host="private.example",
            port=8443,
            max_active_profiles=20,
        )
        db.add(endpoint)
        await db.flush()
        db.add(
            m.VpnAccessKey(
                subscription_id=ids.subscription,
                device_id=ids.device,
                worker_id=ids.workers[0],
                endpoint_id=endpoint.id,
                status="pending_revoke",
            )
        )
        # A binding may move; historical bytes belong to the worker recorded by the measurement.
        db.add_all(
            [
                hour(ids.keys[1], ids.workers[0], ids.now, up=5, down=7),
                poll(ids.workers[0], ids.now),
            ]
        )
        await db.commit()
    rows = (await env.client.get(NODES)).json()
    assert rows[0]["upload_bytes"] == 5 and rows[1]["upload_bytes"] is None
    assert rows[0]["active_devices"] == 1 and rows[0]["occupied_profiles"] == 2
    assert rows[0]["max_active_profiles"] == 30
    assert rows[0]["cpu_load"] == 1.5 and rows[0]["ram_usage_percent"] == 34


@pytest.mark.asyncio
async def test_old_totals_survive_failed_or_missing_poll_and_stale_heartbeat(env):
    ids = await seed(env)
    old = ids.now - timedelta(minutes=10)
    async with env.sessions() as db:
        worker = await db.get(m.WorkerNode, ids.workers[0])
        worker.last_heartbeat_at = old
        db.add_all(
            [
                hour(ids.keys[0], ids.workers[0], old),
                sample(ids.keys[0], ids.workers[0], old),
            ]
        )
        await db.commit()
    row = (await env.client.get(NODES)).json()[0]
    assert row["upload_bytes"] == 10 and row["coverage"] == "stale"
    assert row["cpu_load"] is None and row["ram_usage_percent"] is None
    async with env.sessions() as db:
        db.add(poll(ids.workers[0], ids.now, state="unavailable"))
        await db.commit()
    row = (await env.client.get(NODES)).json()[0]
    assert row["upload_bytes"] == 10 and row["coverage"] == "unavailable"
    assert (await env.client.get(CUSTOMER.format(ids.customer))).json()[
        "coverage"
    ] == "unavailable"


@pytest.mark.asyncio
async def test_latest_good_poll_cannot_hide_gaps_and_queries_do_not_grow_per_node_or_key(
    env,
):
    ids = await seed(env, count=5)
    async with env.sessions() as db:
        for key, worker in zip(ids.keys, ids.workers):
            db.add_all(
                [
                    hour(key, worker, ids.now),
                    sample(key, worker, ids.now),
                    poll(worker, ids.now),
                ]
            )
        await db.commit()
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(env.engine.sync_engine, "before_cursor_execute", record)
    try:
        nodes = await env.client.get(NODES)
        node_count = len(statements)
        statements.clear()
        customer = await env.client.get(CUSTOMER.format(ids.customer))
        customer_count = len(statements)
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", record)
    assert nodes.status_code == customer.status_code == 200
    assert node_count <= 6 and customer_count <= 6
    # Five new children are still one logical profile, and no historic interval is fabricated.
    assert customer.json()["upload_bytes"] == 50
    assert customer.json()["coverage"] == "partial"
    assert all(
        "LIMIT" not in statement.upper() or "worker_nodes" in statement
        for statement in statements
    )


@pytest.mark.asyncio
async def test_another_keys_old_history_cannot_fill_a_current_keys_missing_intervals(
    env, monkeypatch
):
    from app.api.routes import vpn_usage

    ids = await seed(env, count=2)
    monkeypatch.setattr(vpn_usage, "utcnow", lambda: ids.now)
    start = ids.now.replace(minute=0, second=0, microsecond=0) - timedelta(days=1)
    async with env.sessions() as db:
        # This key used to belong to node 1, and has now moved to node 2.
        for index in range(25):
            bucket = start + timedelta(hours=index)
            end = min(bucket + timedelta(hours=1), ids.now)
            if end <= bucket:
                continue
            interval = hour(ids.keys[1], ids.workers[0], end, minutes=60)
            interval.hour_start = bucket
            interval.first_sample_at = bucket
            interval.sample_count = int((end - bucket).total_seconds() // 60) + 1
            db.add(interval)
        db.add_all(
            [
                hour(ids.keys[0], ids.workers[0], ids.now),
                sample(ids.keys[0], ids.workers[0], ids.now),
                poll(ids.workers[0], ids.now),
            ]
        )
        await db.commit()
    assert (await env.client.get(NODES)).json()[0]["coverage"] == "partial"


@pytest.mark.asyncio
async def test_empty_node_explicit_failed_attempt_is_unavailable(env):
    async with env.sessions() as db:
        worker = m.WorkerNode(name="empty-failed", vpn_enabled=True)
        db.add(worker)
        await db.flush()
        db.add(poll(worker.id, utcnow(), state="unavailable"))
        await db.commit()
    row = (await env.client.get(NODES)).json()[0]
    assert row["coverage"] == "unavailable" and row["upload_bytes"] is None


@pytest.mark.asyncio
async def test_more_than_1000_keys_are_aggregated_without_loading_credentials_or_get_writes(
    env,
):
    ids = await seed(env)
    async with env.sessions() as db:
        await db.execute(
            insert(m.VpnAccessKey),
            [
                {
                    "id": 10000 + index,
                    "subscription_id": ids.subscription,
                    "worker_id": ids.workers[0],
                    "status": "active",
                }
                for index in range(1001)
            ],
        )
        await db.execute(
            insert(m.VpnUsageHour),
            [
                {
                    "access_key_id": 10000 + index,
                    "worker_id": ids.workers[0],
                    "hour_start": ids.now.replace(minute=0, second=0, microsecond=0),
                    "upload_bytes": 1,
                    "download_bytes": 2,
                    "sample_count": 1,
                    "first_sample_at": ids.now - timedelta(minutes=1),
                    "last_sample_at": ids.now,
                }
                for index in range(1001)
            ],
        )
        await db.commit()
    statements = []

    def record(_conn, _cursor, statement, *_args):
        statements.append(statement)

    event.listen(env.engine.sync_engine, "before_cursor_execute", record)
    try:
        nodes = (await env.client.get(NODES)).json()
        customer = (await env.client.get(CUSTOMER.format(ids.customer))).json()
    finally:
        event.remove(env.engine.sync_engine, "before_cursor_execute", record)
    assert nodes[0]["upload_bytes"] == customer["upload_bytes"] == 1001
    assert nodes[0]["active_devices"] == 1002
    assert len(statements) == 8
    assert all(statement.lstrip().startswith("SELECT") for statement in statements)
    for secret_column in (
        "external_uuid",
        "verified_client_email",
        "config_uri",
        "source_hashes",
        "ssh_password",
    ):
        assert all(secret_column not in statement for statement in statements)


@pytest.mark.asyncio
async def test_recent_success_does_not_erase_old_missing_intervals_or_unknown_capacity(
    env,
):
    ids = await seed(env)
    async with env.sessions() as db:
        endpoint = await db.get(m.VpnEndpoint, ids.endpoints[0])
        endpoint.max_active_profiles = None
        worker = await db.get(m.WorkerNode, ids.workers[0])
        worker.last_heartbeat_at = ids.now + timedelta(minutes=1)
        db.add_all(
            [
                hour(ids.keys[0], ids.workers[0], ids.now),
                sample(ids.keys[0], ids.workers[0], ids.now),
                poll(ids.workers[0], ids.now),
            ]
        )
        await db.commit()
    row = (await env.client.get(NODES)).json()[0]
    assert row["coverage"] == "partial" and row["max_active_profiles"] is None
    assert row["cpu_load"] is row["ram_usage_percent"] is None


@pytest.mark.asyncio
async def test_non_vpn_workers_are_not_in_usage_list_but_disabled_historical_nodes_remain(
    env,
):
    async with env.sessions() as db:
        db.add(m.WorkerNode(name="not-a-vpn-node"))
        worker = m.WorkerNode(name="disabled-vpn-node", vpn_enabled=False)
        db.add(worker)
        await db.flush()
        db.add(poll(worker.id, utcnow(), state="unavailable"))
        await db.commit()
        worker_id = worker.id
    assert [row["worker_id"] for row in (await env.client.get(NODES)).json()] == [
        worker_id
    ]


async def assert_mutable_current_hour_preserves_known_bytes(env, monkeypatch):
    from app.api.routes import vpn_usage

    ids = await seed(env)
    monkeypatch.setattr(vpn_usage, "utcnow", lambda: ids.now)
    bucket = ids.now.replace(minute=0, second=0, microsecond=0)
    async with env.sessions() as db:
        db.add(hour(ids.keys[0], ids.workers[0], ids.now))
        await db.commit()
    assert (await env.client.get(NODES)).json()[0]["upload_bytes"] == 10
    assert (await env.client.get(CUSTOMER.format(ids.customer))).json()[
        "upload_bytes"
    ] == 10
    async with env.sessions() as db:
        current = await db.get(m.VpnUsageHour, (ids.keys[0], ids.workers[0], bucket))
        current.upload_bytes = 15
        current.last_sample_at = ids.now + timedelta(milliseconds=1)
        await db.commit()
    node = (await env.client.get(NODES)).json()[0]
    customer = (await env.client.get(CUSTOMER.format(ids.customer))).json()
    assert node["upload_bytes"] == customer["upload_bytes"] == 15
    assert node["coverage"] == customer["coverage"] == "partial"
    assert customer["devices"][0]["upload_bytes"] == 15


@pytest.mark.asyncio
async def test_current_hour_update_after_request_clock_does_not_erase_known_totals(
    env, monkeypatch
):
    await assert_mutable_current_hour_preserves_known_bytes(env, monkeypatch)


def test_after_request_sample_keeps_bytes_but_is_not_complete_current_coverage():
    from app.services.vpn_usage_reports import _totals

    now = utcnow().replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=1)
    aggregate = SimpleNamespace(
        upload_bytes=15,
        download_bytes=20,
        measured_since=start,
        last_sample_at=now + timedelta(milliseconds=1),
    )
    evidence = SimpleNamespace(
        last_sample_at=now,
        unavailable_count=0,
        last_attempt_at=now,
    )
    result = _totals(aggregate, evidence, now=now)
    assert result.upload_bytes == 15 and result.coverage == "partial"


async def assert_boundary_interval_cannot_hide_internal_reset_gap(env, monkeypatch):
    from app.api.routes import vpn_usage

    ids = await seed(env)
    now = ids.now.replace(minute=0, second=0, microsecond=0)
    start = now - timedelta(days=1)
    monkeypatch.setattr(vpn_usage, "utcnow", lambda: now)
    async with env.sessions() as db:
        key = await db.get(m.VpnAccessKey, ids.keys[0])
        key.created_at = start - timedelta(days=1)
        # 1441 possible ending-minute intervals include one before the period;
        # an internal counter reset loses one, leaving a misleading count of 1440.
        for index in range(25):
            bucket = start + timedelta(hours=index)
            count = 1 if index == 24 else 59 if index == 12 else 60
            interval = hour(
                ids.keys[0], ids.workers[0], bucket, up=count, down=count * 2
            )
            interval.first_sample_at = bucket - timedelta(minutes=1)
            interval.last_sample_at = (
                bucket if index == 24 else bucket + timedelta(minutes=59)
            )
            interval.sample_count = count
            db.add(interval)
        db.add_all(
            [sample(ids.keys[0], ids.workers[0], now), poll(ids.workers[0], now)]
        )
        await db.commit()
    node = (await env.client.get(NODES)).json()[0]
    customer = (await env.client.get(CUSTOMER.format(ids.customer))).json()
    device = customer["devices"][0]
    assert (
        node["upload_bytes"]
        == customer["upload_bytes"]
        == device["upload_bytes"]
        == 1440
    )
    assert {
        "node": node["coverage"],
        "customer": customer["coverage"],
        "device": device["coverage"],
    } == {
        "node": "partial",
        "customer": "partial",
        "device": "partial",
    }


@pytest.mark.asyncio
async def test_boundary_minute_does_not_compensate_for_internal_reset_gap(
    env, monkeypatch
):
    await assert_boundary_interval_cannot_hide_internal_reset_gap(env, monkeypatch)
