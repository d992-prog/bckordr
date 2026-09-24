from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnCustomer,
    VpnEndpoint,
    VpnPlan,
    VpnSubscription,
    WorkerMaintenanceJob,
    WorkerNode,
)
from app.services import vpn_release_readiness as release_readiness
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    public_endpoint_fingerprint,
)
from app.services.vpn_release_readiness import (
    BackupObservation,
    ControlOperationSnapshot,
    EndpointSnapshot,
    MaintenanceSnapshot,
    OperationalObservations,
    PlanSnapshot,
    ReadinessObservation,
    ReleaseReadinessSnapshot,
    WorkerSnapshot,
    evaluate_release_readiness,
    load_release_readiness_snapshot,
)

NOW = datetime(2026, 9, 24, 12, tzinfo=UTC)


def target(endpoint_id: int, worker_id: int) -> VpnEndpointTarget:
    return VpnEndpointTarget(
        endpoint_id=endpoint_id,
        worker_id=worker_id,
        inbound_id=10,
        public_host=f"vpn-{worker_id}.example",
        port=443,
        protocol="vless",
        transport="raw",
        security="reality",
        server_name="www.example.com",
        public_key="public-key",
        short_id="0123456789abcdef",
        fingerprint="chrome",
        flow="xtls-rprx-vision",
    )


def observation(
    state: str = "pass",
    *,
    observed_at: datetime | None = NOW,
    max_age_seconds: int = 300,
) -> ReadinessObservation:
    return ReadinessObservation(
        state=state,
        observed_at=observed_at,
        max_age_seconds=max_age_seconds,
    )


def operations(**changes) -> OperationalObservations:
    values = {
        "system": observation(),
        "control": observation(),
        "local": observation(),
        "public": observation(),
        "cabinet": observation(),
        "disk": observation(),
        "known_hosts": observation(),
    }
    values.update(changes)
    return OperationalObservations(**values)


def worker(worker_id: int, **changes) -> WorkerSnapshot:
    values = {
        "entity_id": worker_id,
        "status": "ready",
        "is_enabled": True,
        "vpn_enabled": True,
        "vpn_role": "vpn_node",
        "vpn_runtime_status": "ready",
        "archived_at": None,
        "vpn_last_checked_at": NOW,
    }
    values.update(changes)
    return WorkerSnapshot(**values)


def endpoint(endpoint_id: int, worker_id: int, **changes) -> EndpointSnapshot:
    endpoint_target = target(endpoint_id, worker_id)
    values = {
        "entity_id": endpoint_id,
        "worker": worker(worker_id),
        "target": endpoint_target,
        "status": "ready",
        "verified_at": NOW,
        "external_verified_at": NOW,
        "external_config_fingerprint": public_endpoint_fingerprint(endpoint_target),
        "max_active_profiles": 10,
        "capacity_warning_percent": 80,
        "occupied_profiles": 1,
    }
    values.update(changes)
    return EndpointSnapshot(**values)


def ready_snapshot(**changes) -> ReleaseReadinessSnapshot:
    values = {
        "release_id": "a" * 64,
        "dispatch_enabled": True,
        "known_hosts_configured": True,
        "public_trial_enabled": False,
        "payment_enabled": False,
        "public_trial_plan_slug": "trial-7d",
        "endpoint_health_max_age_seconds": 300,
        "plan": PlanSnapshot(7, "trial-7d", True, 7, 1),
        "endpoints": (endpoint(10, 1), endpoint(20, 2)),
        "control_operations": (),
        "maintenance_jobs": (),
        "operational": operations(),
        "backup": BackupObservation("pass", NOW, 86_400),
    }
    values.update(changes)
    return ReleaseReadinessSnapshot(**values)


def states(result, code: str) -> list[str]:
    return [check.state for check in result.checks if check.code == code]


def test_public_fingerprint_is_canonical_and_excludes_internal_ids() -> None:
    first = target(10, 1)
    same_public_identity = replace(first, endpoint_id=999, worker_id=888)

    assert public_endpoint_fingerprint(first) == public_endpoint_fingerprint(
        same_public_identity
    )
    assert len(public_endpoint_fingerprint(first)) == 64


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("inbound_id", 11),
        ("public_host", "other.example"),
        ("port", 8443),
        ("protocol", "trojan"),
        ("transport", "tcp"),
        ("security", "tls"),
        ("server_name", "other.example"),
        ("public_key", "other-public-key"),
        ("short_id", "abcdef0123456789"),
        ("fingerprint", "firefox"),
        ("flow", None),
    ],
)
def test_public_fingerprint_changes_for_each_public_identity_field(
    field: str, value: object
) -> None:
    original = target(10, 1)

    assert public_endpoint_fingerprint(original) != public_endpoint_fingerprint(
        replace(original, **{field: value})
    )


def test_fingerprint_api_accepts_only_structured_public_endpoint_values() -> None:
    fields = VpnEndpointTarget.__dataclass_fields__

    assert "config_uri" not in fields
    assert "external_uuid" not in fields
    assert "password" not in fields


def test_two_healthy_endpoints_on_distinct_workers_are_ready_and_immutable() -> None:
    result = evaluate_release_readiness(ready_snapshot(), now=NOW)

    assert result.ready is True
    assert result.checked_at == NOW
    assert states(result, "endpoint_redundancy") == ["pass"]
    assert all(
        check.entity_id is None or type(check.entity_id) is int
        for check in result.checks
    )
    with pytest.raises(FrozenInstanceError):
        result.ready = False
    with pytest.raises(FrozenInstanceError):
        result.checks[0].message = "unsafe"


def test_all_fixed_check_messages_are_safe_russian_text() -> None:
    messages = release_readiness._MESSAGES.values()

    assert all(any("\u0400" <= char <= "\u04ff" for char in message) for message in messages)
    assert all(
        not any("a" <= char.lower() <= "z" for char in message)
        for message in messages
    )


def test_operational_adapter_keeps_all_six_health_gates_separate() -> None:
    assert set(OperationalObservations.__dataclass_fields__) == {
        "system",
        "control",
        "local",
        "public",
        "cabinet",
        "disk",
        "known_hosts",
    }


def test_naive_database_datetimes_are_normalized_without_crashing() -> None:
    naive = NOW.replace(tzinfo=None)
    snapshot = ready_snapshot(
        endpoints=(
            endpoint(
                10,
                1,
                worker=worker(1, vpn_last_checked_at=naive),
                verified_at=naive,
                external_verified_at=naive,
            ),
            endpoint(
                20,
                2,
                worker=worker(2, vpn_last_checked_at=naive),
                verified_at=naive,
                external_verified_at=naive,
            ),
        ),
        operational=operations(
            system=observation(observed_at=naive),
        ),
        backup=BackupObservation("pass", naive, 86_400),
    )

    result = evaluate_release_readiness(snapshot, now=naive)

    assert result.ready is True
    assert result.checked_at.tzinfo is UTC
    assert all(
        check.observed_at is None or check.observed_at.tzinfo is not None
        for check in result.checks
    )


@pytest.mark.parametrize("endpoints", [(), (endpoint(10, 1),)])
def test_fewer_than_two_endpoints_fails_closed(
    endpoints: tuple[EndpointSnapshot, ...]
) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(endpoints=endpoints),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, "endpoint_redundancy") == ["fail"]


@pytest.mark.parametrize(
    ("changed_endpoint", "code"),
    [
        (
            endpoint(
                10,
                1,
                worker=worker(
                    1, vpn_last_checked_at=NOW - timedelta(seconds=301)
                ),
            ),
            "worker_health",
        ),
        (
            endpoint(10, 1, verified_at=NOW - timedelta(seconds=301)),
            "endpoint_health",
        ),
        (endpoint(10, 1, status="draining"), "endpoint_configuration"),
        (
            endpoint(10, 1, worker=worker(1, archived_at=NOW)),
            "worker_active",
        ),
    ],
)
def test_stale_or_inactive_fleet_member_is_a_hard_failure(
    changed_endpoint: EndpointSnapshot, code: str
) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(endpoints=(changed_endpoint, endpoint(20, 2))),
        now=NOW,
    )

    assert result.ready is False
    assert "fail" in states(result, code)


@pytest.mark.parametrize(
    "changes",
    [
        {"external_verified_at": None},
        {"external_config_fingerprint": None},
        {"external_config_fingerprint": "0" * 64},
    ],
)
def test_external_proof_must_exist_and_match_current_public_identity(changes) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(endpoint(10, 1, **changes), endpoint(20, 2))
        ),
        now=NOW,
    )

    assert result.ready is False
    assert "fail" in states(result, "endpoint_external_proof")


@pytest.mark.parametrize("capacity", [None, 0])
def test_endpoint_capacity_must_be_configured(capacity: int | None) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(10, 1, max_active_profiles=capacity),
                endpoint(20, 2),
            )
        ),
        now=NOW,
    )

    assert result.ready is False
    assert "fail" in states(result, "endpoint_capacity")


def test_a_full_endpoint_is_a_hard_failure() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(10, 1, max_active_profiles=2, occupied_profiles=2),
                endpoint(20, 2),
            )
        ),
        now=NOW,
    )

    assert result.ready is False
    assert "fail" in states(result, "endpoint_capacity")


def test_exhausted_aggregate_capacity_fails() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(10, 1, max_active_profiles=1, occupied_profiles=1),
                endpoint(20, 2, max_active_profiles=1, occupied_profiles=1),
            )
        ),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, "aggregate_capacity") == ["fail"]


def test_capacity_warning_alone_does_not_make_release_unready() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(10, 1, max_active_profiles=10, occupied_profiles=8),
                endpoint(20, 2),
            )
        ),
        now=NOW,
    )

    assert result.ready is True
    assert "warn" in states(result, "endpoint_capacity")


@pytest.mark.parametrize(
    "plan",
    [
        None,
        PlanSnapshot(7, "wrong", True, 7, 1),
        PlanSnapshot(7, "trial-7d", False, 7, 1),
        PlanSnapshot(7, "trial-7d", True, 6, 1),
        PlanSnapshot(7, "trial-7d", True, 7, 2),
    ],
)
def test_public_trial_plan_must_match_the_fixed_trial_contract(
    plan: PlanSnapshot | None,
) -> None:
    result = evaluate_release_readiness(ready_snapshot(plan=plan), now=NOW)

    assert result.ready is False
    assert states(result, "public_trial_plan") == ["fail"]


@pytest.mark.parametrize("state", ["queued", "running", "uncertain"])
def test_active_or_uncertain_control_operation_blocks_release(state: str) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            control_operations=(ControlOperationSnapshot(1, state),)
        ),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, "control_operations") == ["fail"]


@pytest.mark.parametrize("state", ["queued", "running"])
def test_active_maintenance_blocks_release(state: str) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(maintenance_jobs=(MaintenanceSnapshot(1, state),)),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, "maintenance") == ["fail"]


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"release_id": ""}, "release_id"),
        ({"release_id": "Z" * 64}, "release_id"),
        ({"dispatch_enabled": False}, "dispatch"),
        ({"known_hosts_configured": False}, "known_hosts"),
        ({"payment_enabled": True}, "payment_disabled"),
        ({"public_trial_enabled": True}, "public_trial_disabled"),
    ],
)
def test_release_configuration_prerequisites_fail_closed(changes, code: str) -> None:
    result = evaluate_release_readiness(ready_snapshot(**changes), now=NOW)

    assert result.ready is False
    assert "fail" in states(result, code)


@pytest.mark.parametrize(
    "name", ["system", "control", "local", "public", "cabinet", "disk"]
)
@pytest.mark.parametrize("state", ["unknown", "fail"])
def test_unknown_or_failed_operational_observation_blocks_release(
    name: str, state: str
) -> None:
    result = evaluate_release_readiness(
        ready_snapshot(operational=operations(**{name: observation(state)})),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, f"{name}_health") == ["fail"]


def test_missing_operational_observations_fail_closed() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(operational=None),
        now=NOW,
    )

    assert result.ready is False
    assert all(
        states(result, f"{name}_health") == ["fail"]
        for name in ("system", "control", "local", "public", "cabinet", "disk")
    )
    assert "fail" in states(result, "known_hosts")


@pytest.mark.parametrize(
    "backup",
    [
        None,
        BackupObservation("unknown", NOW, 86_400),
        BackupObservation("fail", NOW, 86_400),
        BackupObservation("pass", None, 86_400),
        BackupObservation("pass", NOW - timedelta(seconds=86_401), 86_400),
    ],
)
def test_missing_failed_or_stale_backup_fails_closed(
    backup: BackupObservation | None,
) -> None:
    result = evaluate_release_readiness(ready_snapshot(backup=backup), now=NOW)

    assert result.ready is False
    assert states(result, "backup_health") == ["fail"]


def test_an_operational_warning_does_not_make_release_unready() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(operational=operations(disk=observation("warn"))),
        now=NOW,
    )

    assert result.ready is True
    assert states(result, "disk_health") == ["warn"]


@pytest.mark.parametrize(
    ("case", "code"),
    [
        ("operational", "local_health"),
        ("backup", "backup_health"),
        ("worker", "worker_health"),
        ("endpoint", "endpoint_health"),
        ("external", "endpoint_external_proof"),
    ],
)
def test_future_observation_timestamps_fail_closed(case: str, code: str) -> None:
    future = NOW + timedelta(microseconds=1)
    changes = {
        "operational": {
            "operational": operations(local=observation(observed_at=future))
        },
        "backup": {"backup": BackupObservation("pass", future, 86_400)},
        "worker": {
            "endpoints": (
                endpoint(
                    10,
                    1,
                    worker=worker(1, vpn_last_checked_at=future),
                ),
                endpoint(20, 2),
            )
        },
        "endpoint": {
            "endpoints": (
                endpoint(10, 1, verified_at=future),
                endpoint(20, 2),
            )
        },
        "external": {
            "endpoints": (
                endpoint(10, 1, external_verified_at=future),
                endpoint(20, 2),
            )
        },
    }[case]
    result = evaluate_release_readiness(ready_snapshot(**changes), now=NOW)

    assert result.ready is False
    assert "fail" in states(result, code)


def test_whitespace_public_trial_plan_slug_fails_even_when_plan_matches() -> None:
    result = evaluate_release_readiness(
        ready_snapshot(
            public_trial_plan_slug="   ",
            plan=PlanSnapshot(7, "   ", True, 7, 1),
        ),
        now=NOW,
    )

    assert result.ready is False
    assert states(result, "public_trial_plan") == ["fail"]


def test_check_messages_are_fixed_and_never_include_sensitive_subjects() -> None:
    first = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(10, 1, external_config_fingerprint="secret-host-uri-uuid"),
                endpoint(20, 2),
            )
        ),
        now=NOW,
    )
    second = evaluate_release_readiness(
        ready_snapshot(
            endpoints=(
                endpoint(99, 9, external_config_fingerprint="different-secret"),
                endpoint(20, 2),
            )
        ),
        now=NOW,
    )
    first_message = next(
        check.message
        for check in first.checks
        if check.code == "endpoint_external_proof" and check.state == "fail"
    )
    second_message = next(
        check.message
        for check in second.checks
        if check.code == "endpoint_external_proof" and check.state == "fail"
    )

    assert first_message == second_message
    assert "secret" not in " ".join(check.message for check in first.checks).lower()
    assert "vpn-" not in " ".join(check.message for check in first.checks).lower()


@pytest_asyncio.fixture
async def session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    try:
        yield engine, factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_loader_uses_grouped_queries_and_policy_occupancy_statuses(
    session_factory,
) -> None:
    engine, factory = session_factory
    async with factory() as session:
        node = WorkerNode(
            name="loader",
            status="ready",
            is_enabled=True,
            vpn_enabled=True,
            vpn_role="vpn_node",
            vpn_runtime_status="ready",
            vpn_last_checked_at=NOW,
        )
        plan = VpnPlan(
            slug="trial-7d",
            name="Trial",
            is_active=True,
            duration_days=7,
            max_devices=1,
        )
        customer = VpnCustomer(status="active")
        session.add_all([node, plan, customer])
        await session.flush()
        subscription = VpnSubscription(
            customer_id=customer.id,
            plan_id=plan.id,
            status="active",
            max_devices=2,
        )
        session.add(subscription)
        await session.flush()
        row = VpnEndpoint(
            worker_id=node.id,
            inbound_id=10,
            public_host="vpn.example",
            port=443,
            protocol="vless",
            transport="raw",
            security="reality",
            server_name="www.example.com",
            public_key="public-key",
            short_id="0123456789abcdef",
            fingerprint="chrome",
            flow="xtls-rprx-vision",
            status="ready",
            verified_at=NOW,
            external_verified_at=NOW,
            max_active_profiles=10,
        )
        session.add(row)
        await session.flush()
        row.external_config_fingerprint = public_endpoint_fingerprint(
            VpnEndpointTarget(
                endpoint_id=row.id,
                worker_id=node.id,
                inbound_id=row.inbound_id,
                public_host=row.public_host,
                port=row.port,
                protocol=row.protocol,
                transport=row.transport,
                security=row.security,
                server_name=row.server_name,
                public_key=row.public_key,
                short_id=row.short_id,
                fingerprint=row.fingerprint,
                flow=row.flow,
            )
        )
        active = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=node.id,
            endpoint_id=row.id,
            status="pending_suspend",
        )
        revoked = VpnAccessKey(
            subscription_id=subscription.id,
            worker_id=node.id,
            endpoint_id=row.id,
            status="revoked",
        )
        session.add_all([active, revoked])
        await session.flush()
        session.add_all(
            [
                VpnControlOperation(
                    id="40000000-0000-4000-8000-000000000001",
                    access_key_id=active.id,
                    worker_id=node.id,
                    endpoint_id=row.id,
                    generation=1,
                    action="suspend",
                    request_snapshot={},
                    request_digest="b" * 64,
                    state="claimed",
                    claim_token="50000000-0000-4000-8000-000000000001",
                ),
                WorkerMaintenanceJob(
                    worker_id=node.id,
                    action="vpn_update",
                    status="queued",
                ),
            ]
        )
        await session.commit()

        selects = 0

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def count_selects(_conn, _cursor, statement, _params, _context, _many):
            nonlocal selects
            if statement.lstrip().upper().startswith("SELECT"):
                selects += 1

        settings = Settings(
            _env_file=None,
            VPN_PUBLIC_TRIAL_RELEASE_ID="a" * 64,
            VPN_PUBLIC_TRIAL_PLAN_SLUG="trial-7d",
            VPN_CONTROL_DISPATCH_ENABLED=True,
            VPN_CONTROL_KNOWN_HOSTS_PATH="C:/veltrix/known_hosts",
            VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300,
        )
        snapshot = await load_release_readiness_snapshot(
            session,
            settings,
            operational=operations(),
            backup=BackupObservation("pass", NOW, 86_400),
            payment_enabled=False,
        )

    assert selects == 5
    assert snapshot.plan == PlanSnapshot(plan.id, "trial-7d", True, 7, 1)
    assert snapshot.endpoints[0].occupied_profiles == 1
    assert snapshot.control_operations == (
        ControlOperationSnapshot(node.id, "running"),
    )
    assert snapshot.maintenance_jobs == (MaintenanceSnapshot(node.id, "queued"),)


@pytest.mark.asyncio
async def test_loader_propagates_database_errors_without_turning_them_into_checks() -> None:
    class BrokenSession:
        async def execute(self, _statement):
            raise RuntimeError("database-secret")

    settings = Settings(_env_file=None)
    with pytest.raises(RuntimeError, match="database-secret"):
        await load_release_readiness_snapshot(
            BrokenSession(),
            settings,
            operational=operations(),
            backup=BackupObservation("pass", NOW, 86_400),
        )
