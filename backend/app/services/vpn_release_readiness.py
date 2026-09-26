from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models import (
    VpnAccessKey,
    VpnControlOperation,
    VpnEndpoint,
    VpnPlan,
    WorkerMaintenanceJob,
    WorkerNode,
)
from app.services.vpn_endpoint_types import (
    VpnEndpointTarget,
    is_valid_vpn_endpoint_target,
    public_endpoint_fingerprint,
)
from app.services.vpn_policy import (
    NODE_CAPACITY_STATUSES,
    VPN_MUTATION_ACTIONS,
    active_attack_worker_ids,
    evaluate_vpn_node_attributes,
)

CheckState = Literal["pass", "warn", "fail"]
ObservationState = Literal["pass", "warn", "fail", "unknown"]
ControlOperationState = Literal["queued", "running", "uncertain"]
MaintenanceState = Literal["queued", "running"]

_RELEASE_READINESS_TABLES = text(
    "LOCK TABLE app_settings, vpn_plans, vpn_endpoints, worker_nodes, vpn_access_keys, "
    "vpn_control_operations, worker_maintenance_jobs, worker_tasks, "
    "attack_runs IN SHARE ROW EXCLUSIVE MODE NOWAIT"
)


class ReleaseReadinessBusy(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseCheck:
    code: str
    state: CheckState
    message: str
    entity_id: int | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.entity_id is not None and type(self.entity_id) is not int:
            raise TypeError("entity_id must be an integer")


@dataclass(frozen=True, slots=True)
class ReleaseReadiness:
    ready: bool
    checked_at: datetime
    checks: tuple[ReleaseCheck, ...]


@dataclass(frozen=True, slots=True)
class ReadinessObservation:
    state: ObservationState
    observed_at: datetime | None
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class BackupObservation:
    state: ObservationState
    observed_at: datetime | None
    max_age_seconds: int


@dataclass(frozen=True, slots=True)
class OperationalObservations:
    system: ReadinessObservation | None
    control: ReadinessObservation | None
    local: ReadinessObservation | None
    public: ReadinessObservation | None
    cabinet: ReadinessObservation | None
    disk: ReadinessObservation | None
    known_hosts: ReadinessObservation | None


@dataclass(frozen=True, slots=True)
class PlanSnapshot:
    entity_id: int
    slug: str
    is_active: bool
    duration_days: int | None
    max_devices: int


@dataclass(frozen=True, slots=True)
class WorkerSnapshot:
    entity_id: int
    status: str
    is_enabled: bool
    vpn_enabled: bool
    vpn_role: str
    vpn_runtime_status: str
    ssh_access_configured: bool
    vpn_inbound_id: int | None
    vpn_public_host: str | None
    archived_at: datetime | None
    vpn_last_checked_at: datetime | None

    @property
    def id(self) -> int:
        return self.entity_id


@dataclass(frozen=True, slots=True)
class EndpointSnapshot:
    entity_id: int
    worker: WorkerSnapshot
    target: VpnEndpointTarget
    status: str
    verified_at: datetime | None
    last_error_code: str | None
    external_verified_at: datetime | None
    external_config_fingerprint: str | None
    max_active_profiles: int | None
    capacity_warning_percent: int
    occupied_profiles: int


@dataclass(frozen=True, slots=True)
class ControlOperationSnapshot:
    worker_id: int
    state: ControlOperationState


@dataclass(frozen=True, slots=True)
class MaintenanceSnapshot:
    worker_id: int
    state: MaintenanceState


@dataclass(frozen=True, slots=True)
class ReleaseReadinessSnapshot:
    release_id: str
    dispatch_enabled: bool
    known_hosts_configured: bool
    public_trial_enabled: bool
    vpn_portal_public_access: bool
    payment_enabled: bool
    public_trial_plan_slug: str
    endpoint_health_max_age_seconds: int
    plan: PlanSnapshot | None
    endpoints: tuple[EndpointSnapshot, ...]
    control_operations: tuple[ControlOperationSnapshot, ...]
    maintenance_jobs: tuple[MaintenanceSnapshot, ...]
    active_attack_worker_ids: frozenset[int]
    operational: OperationalObservations | None
    backup: BackupObservation | None


_MESSAGES: dict[tuple[str, CheckState], str] = {
    ("release_id", "pass"): "Идентификатор релиза корректен.",
    ("release_id", "fail"): "Идентификатор релиза некорректен.",
    ("dispatch", "pass"): "Отправка команд ВПН включена.",
    ("dispatch", "fail"): "Отправка команд ВПН выключена.",
    ("known_hosts", "pass"): "Проверка ключей узлов готова.",
    ("known_hosts", "warn"): "Проверка ключей узлов требует внимания.",
    ("known_hosts", "fail"): "Проверка ключей узлов не готова.",
    ("payment_disabled", "pass"): "Платёжная интеграция выключена.",
    ("payment_disabled", "fail"): "Платёжная интеграция включена.",
    ("public_trial_disabled", "pass"): "Публичный пробный доступ выключен.",
    ("public_trial_disabled", "fail"): "Публичный пробный доступ уже включён.",
    ("portal_public_access", "pass"): "Публичный доступ к кабинету выключен.",
    ("portal_public_access", "fail"): "Публичный доступ к кабинету включён.",
    ("public_trial_plan", "pass"): "План пробного доступа корректен.",
    ("public_trial_plan", "fail"): "План пробного доступа некорректен.",
    ("control_operations", "pass"): "Незавершённых операций ВПН нет.",
    ("control_operations", "fail"): "Есть незавершённая операция ВПН.",
    ("maintenance", "pass"): "Незавершённых работ обслуживания нет.",
    ("maintenance", "fail"): "Есть незавершённая работа обслуживания.",
    ("system_health", "pass"): "Система готова.",
    ("system_health", "warn"): "Состояние системы требует внимания.",
    ("system_health", "fail"): "Система не готова.",
    ("control_health", "pass"): "Контур управления готов.",
    ("control_health", "warn"): "Контур управления требует внимания.",
    ("control_health", "fail"): "Контур управления не готов.",
    ("local_health", "pass"): "Локальная проверка пройдена.",
    ("local_health", "warn"): "Локальная проверка требует внимания.",
    ("local_health", "fail"): "Локальная проверка не пройдена.",
    ("public_health", "pass"): "Публичная проверка пройдена.",
    ("public_health", "warn"): "Публичная проверка требует внимания.",
    ("public_health", "fail"): "Публичная проверка не пройдена.",
    ("cabinet_health", "pass"): "Личный кабинет доступен.",
    ("cabinet_health", "warn"): "Личный кабинет требует внимания.",
    ("cabinet_health", "fail"): "Личный кабинет недоступен.",
    ("disk_health", "pass"): "Состояние диска допустимо.",
    ("disk_health", "warn"): "Состояние диска требует внимания.",
    ("disk_health", "fail"): "Состояние диска недопустимо.",
    ("backup_health", "pass"): "Резервное копирование готово.",
    ("backup_health", "warn"): "Резервное копирование требует внимания.",
    ("backup_health", "fail"): "Резервное копирование не готово.",
    ("worker_active", "pass"): "Узел ВПН активен.",
    ("worker_active", "fail"): "Узел ВПН неактивен.",
    ("worker_health", "pass"): "Состояние узла ВПН актуально.",
    ("worker_health", "fail"): "Состояние узла ВПН устарело или отсутствует.",
    ("endpoint_configuration", "pass"): "Конфигурация точки ВПН готова.",
    ("endpoint_configuration", "fail"): "Конфигурация точки ВПН не готова.",
    ("endpoint_health", "pass"): "Состояние точки ВПН актуально.",
    ("endpoint_health", "fail"): "Состояние точки ВПН устарело или отсутствует.",
    ("endpoint_external_proof", "pass"): "Внешняя проверка точки ВПН действительна.",
    ("endpoint_external_proof", "fail"): "Внешняя проверка точки ВПН недействительна.",
    ("endpoint_capacity", "pass"): "Ёмкость точки ВПН доступна.",
    ("endpoint_capacity", "warn"): "Ёмкость точки ВПН заканчивается.",
    ("endpoint_capacity", "fail"): "Ёмкость точки ВПН недоступна.",
    ("endpoint_redundancy", "pass"): "Резервирование точек ВПН готово.",
    ("endpoint_redundancy", "fail"): "Резервирование точек ВПН недостаточно.",
    ("aggregate_capacity", "pass"): "Общая ёмкость ВПН доступна.",
    ("aggregate_capacity", "fail"): "Общая ёмкость ВПН исчерпана.",
}


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_clean_uow(session: AsyncSession) -> None:
    if session.new or session.dirty or session.deleted:
        raise ValueError("vpn_release_readiness_pending_state")


async def lock_release_readiness_tables(session: AsyncSession) -> None:
    _require_clean_uow(session)
    if session.get_bind().dialect.name == "postgresql":
        try:
            await session.execute(_RELEASE_READINESS_TABLES)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise ReleaseReadinessBusy("vpn_release_readiness_busy") from None
            raise


def _check(
    code: str,
    state: CheckState,
    *,
    entity_id: int | None = None,
    observed_at: datetime | None = None,
) -> ReleaseCheck:
    return ReleaseCheck(
        code=code,
        state=state,
        message=_MESSAGES[(code, state)],
        entity_id=entity_id,
        observed_at=_as_utc(observed_at),
    )


def _fresh(value: datetime | None, now: datetime, max_age_seconds: int) -> bool:
    observed = _as_utc(value)
    return observed is not None and now - timedelta(
        seconds=max(max_age_seconds, 1)
    ) <= observed <= now


def _observation_check(
    code: str,
    value: ReadinessObservation | BackupObservation | None,
    now: datetime,
) -> ReleaseCheck:
    if (
        value is None
        or value.state not in {"pass", "warn"}
        or not _fresh(value.observed_at, now, value.max_age_seconds)
    ):
        return _check(
            code,
            "fail",
            observed_at=value.observed_at if value is not None else None,
        )
    return _check(code, value.state, observed_at=value.observed_at)


def evaluate_operational_checks(
    operational: OperationalObservations | None,
    backup: BackupObservation | None,
    known_hosts_configured: bool,
    now: datetime,
) -> tuple[ReleaseCheck, ...]:
    checked_at = _as_utc(now)
    assert checked_at is not None
    checks: list[ReleaseCheck] = []
    for name in ("system", "control", "local", "public", "cabinet", "disk"):
        value = getattr(operational, name) if operational is not None else None
        checks.append(_observation_check(f"{name}_health", value, checked_at))
    known_hosts = operational.known_hosts if operational is not None else None
    checks.append(
        _check("known_hosts", "fail")
        if not known_hosts_configured
        else _observation_check("known_hosts", known_hosts, checked_at)
    )
    checks.append(_observation_check("backup_health", backup, checked_at))
    return tuple(checks)


def evaluate_release_readiness(
    snapshot: ReleaseReadinessSnapshot,
    *,
    now: datetime,
) -> ReleaseReadiness:
    checked_at = _as_utc(now)
    assert checked_at is not None
    checks: list[ReleaseCheck] = []

    checks.append(
        _check(
            "release_id",
            "pass" if re.fullmatch(r"[0-9a-f]{64}", snapshot.release_id) else "fail",
        )
    )
    checks.append(_check("dispatch", "pass" if snapshot.dispatch_enabled else "fail"))
    checks.append(
        _check(
            "payment_disabled",
            "fail" if snapshot.payment_enabled else "pass",
        )
    )
    checks.append(
        _check(
            "public_trial_disabled",
            "fail" if snapshot.public_trial_enabled else "pass",
        )
    )
    checks.append(
        _check(
            "portal_public_access",
            "fail" if snapshot.vpn_portal_public_access else "pass",
        )
    )
    plan = snapshot.plan
    valid_plan = bool(
        snapshot.public_trial_plan_slug.strip()
        and plan is not None
        and plan.slug == snapshot.public_trial_plan_slug
        and plan.is_active
        and plan.duration_days == 7
        and plan.max_devices == 1
    )
    checks.append(
        _check(
            "public_trial_plan",
            "pass" if valid_plan else "fail",
            entity_id=plan.entity_id if plan is not None else None,
        )
    )

    blocked_operation = next(
        (
            item
            for item in sorted(
                snapshot.control_operations,
                key=lambda item: (item.worker_id, item.state),
            )
            if item.state in {"queued", "running", "uncertain"}
        ),
        None,
    )
    checks.append(
        _check(
            "control_operations",
            "fail" if blocked_operation is not None else "pass",
            entity_id=(
                blocked_operation.worker_id if blocked_operation is not None else None
            ),
        )
    )
    blocked_maintenance = next(
        (
            item
            for item in sorted(
                snapshot.maintenance_jobs,
                key=lambda item: (item.worker_id, item.state),
            )
            if item.state in {"queued", "running"}
        ),
        None,
    )
    checks.append(
        _check(
            "maintenance",
            "fail" if blocked_maintenance is not None else "pass",
            entity_id=(
                blocked_maintenance.worker_id
                if blocked_maintenance is not None
                else None
            ),
        )
    )

    checks.extend(
        evaluate_operational_checks(
            snapshot.operational,
            snapshot.backup,
            snapshot.known_hosts_configured,
            checked_at,
        )
    )

    healthy_worker_ids: set[int] = set()
    aggregate_available = 0
    max_age_seconds = max(snapshot.endpoint_health_max_age_seconds, 1)
    for item in sorted(snapshot.endpoints, key=lambda endpoint: endpoint.entity_id):
        worker_active = bool(
            item.worker.archived_at is None
            and evaluate_vpn_node_attributes(
                item.worker,
                active_attack_worker_ids=snapshot.active_attack_worker_ids,
            ).eligible
        )
        worker_fresh = _fresh(
            item.worker.vpn_last_checked_at,
            checked_at,
            max_age_seconds,
        )
        endpoint_configured = bool(
            item.status == "ready"
            and item.target.security == "reality"
            and item.target.endpoint_id == item.entity_id
            and item.target.worker_id == item.worker.entity_id
            and is_valid_vpn_endpoint_target(item.target)
        )
        endpoint_fresh = bool(
            _fresh(item.verified_at, checked_at, max_age_seconds)
            and not item.last_error_code
        )
        external_proof = bool(
            (external_verified_at := _as_utc(item.external_verified_at)) is not None
            and external_verified_at <= checked_at
            and item.external_config_fingerprint
            == public_endpoint_fingerprint(item.target)
        )
        capacity_configured = bool(
            item.max_active_profiles is not None
            and item.max_active_profiles > 0
            and 1 <= item.capacity_warning_percent <= 100
            and 0 <= item.occupied_profiles < item.max_active_profiles
        )
        capacity_state: CheckState = "fail"
        if capacity_configured:
            assert item.max_active_profiles is not None
            aggregate_available += item.max_active_profiles - item.occupied_profiles
            capacity_state = (
                "warn"
                if item.occupied_profiles * 100
                >= item.max_active_profiles * item.capacity_warning_percent
                else "pass"
            )

        checks.extend(
            (
                _check(
                    "worker_active",
                    "pass" if worker_active else "fail",
                    entity_id=item.worker.entity_id,
                ),
                _check(
                    "worker_health",
                    "pass" if worker_fresh else "fail",
                    entity_id=item.worker.entity_id,
                    observed_at=item.worker.vpn_last_checked_at,
                ),
                _check(
                    "endpoint_configuration",
                    "pass" if endpoint_configured else "fail",
                    entity_id=item.entity_id,
                ),
                _check(
                    "endpoint_health",
                    "pass" if endpoint_fresh else "fail",
                    entity_id=item.entity_id,
                    observed_at=item.verified_at,
                ),
                _check(
                    "endpoint_external_proof",
                    "pass" if external_proof else "fail",
                    entity_id=item.entity_id,
                    observed_at=item.external_verified_at,
                ),
                _check(
                    "endpoint_capacity",
                    capacity_state,
                    entity_id=item.entity_id,
                ),
            )
        )
        if all(
            (
                worker_active,
                worker_fresh,
                endpoint_configured,
                endpoint_fresh,
                external_proof,
                capacity_configured,
            )
        ):
            healthy_worker_ids.add(item.worker.entity_id)

    checks.append(
        _check(
            "endpoint_redundancy",
            "pass" if len(healthy_worker_ids) >= 2 else "fail",
        )
    )
    checks.append(
        _check(
            "aggregate_capacity",
            "pass" if aggregate_available > 0 else "fail",
        )
    )
    result_checks = tuple(checks)
    return ReleaseReadiness(
        ready=not any(check.state == "fail" for check in result_checks),
        checked_at=checked_at,
        checks=result_checks,
    )


async def load_release_readiness_snapshot(
    session: AsyncSession,
    settings: Settings,
    *,
    operational: OperationalObservations | None,
    backup: BackupObservation | None,
    payment_enabled: bool = False,
) -> ReleaseReadinessSnapshot:
    _require_clean_uow(session)
    plan_row = (
        (
            await session.execute(
                select(VpnPlan)
                .where(VpnPlan.slug == settings.vpn_public_trial_plan_slug)
                .order_by(VpnPlan.id)
            )
        )
        .scalars()
        .first()
    )
    endpoint_rows = (
        await session.execute(
            select(VpnEndpoint, WorkerNode)
            .join(WorkerNode, WorkerNode.id == VpnEndpoint.worker_id)
            .order_by(VpnEndpoint.id)
        )
    ).all()
    occupancy_rows = (
        await session.execute(
            select(VpnAccessKey.endpoint_id, func.count(VpnAccessKey.id))
            .where(
                VpnAccessKey.endpoint_id.is_not(None),
                VpnAccessKey.status.in_(NODE_CAPACITY_STATUSES),
            )
            .group_by(VpnAccessKey.endpoint_id)
        )
    ).all()
    operation_rows = (
        await session.execute(
            select(VpnControlOperation.worker_id, VpnControlOperation.state)
            .where(VpnControlOperation.state.in_(("queued", "claimed", "uncertain")))
            .order_by(VpnControlOperation.worker_id, VpnControlOperation.state)
        )
    ).all()
    maintenance_rows = (
        await session.execute(
            select(WorkerMaintenanceJob.worker_id, WorkerMaintenanceJob.status)
            .where(
                WorkerMaintenanceJob.action.in_(VPN_MUTATION_ACTIONS),
                WorkerMaintenanceJob.status.in_(("queued", "running")),
            )
            .order_by(WorkerMaintenanceJob.worker_id, WorkerMaintenanceJob.status)
        )
    ).all()
    attack_worker_ids = await active_attack_worker_ids(session)

    occupied_by_endpoint = {
        int(endpoint_id): int(count) for endpoint_id, count in occupancy_rows
    }
    endpoints: list[EndpointSnapshot] = []
    for endpoint_row, worker_row in endpoint_rows:
        endpoint_target = VpnEndpointTarget(
            endpoint_id=endpoint_row.id,
            worker_id=endpoint_row.worker_id,
            inbound_id=endpoint_row.inbound_id,
            public_host=endpoint_row.public_host,
            port=endpoint_row.port,
            protocol=endpoint_row.protocol,
            transport=endpoint_row.transport,
            security=endpoint_row.security,
            server_name=endpoint_row.server_name,
            public_key=endpoint_row.public_key,
            short_id=endpoint_row.short_id,
            fingerprint=endpoint_row.fingerprint,
            flow=endpoint_row.flow,
        )
        endpoints.append(
            EndpointSnapshot(
                entity_id=endpoint_row.id,
                worker=WorkerSnapshot(
                    entity_id=worker_row.id,
                    status=worker_row.status,
                    is_enabled=worker_row.is_enabled,
                    vpn_enabled=worker_row.vpn_enabled,
                    vpn_role=worker_row.vpn_role,
                    vpn_runtime_status=worker_row.vpn_runtime_status,
                    ssh_access_configured=worker_row.ssh_access_configured,
                    vpn_inbound_id=worker_row.vpn_inbound_id,
                    vpn_public_host=worker_row.vpn_public_host,
                    archived_at=worker_row.archived_at,
                    vpn_last_checked_at=worker_row.vpn_last_checked_at,
                ),
                target=endpoint_target,
                status=endpoint_row.status,
                verified_at=endpoint_row.verified_at,
                last_error_code=endpoint_row.last_error_code,
                external_verified_at=endpoint_row.external_verified_at,
                external_config_fingerprint=endpoint_row.external_config_fingerprint,
                max_active_profiles=endpoint_row.max_active_profiles,
                capacity_warning_percent=endpoint_row.capacity_warning_percent,
                occupied_profiles=occupied_by_endpoint.get(endpoint_row.id, 0),
            )
        )

    plan = (
        PlanSnapshot(
            entity_id=plan_row.id,
            slug=plan_row.slug,
            is_active=plan_row.is_active,
            duration_days=plan_row.duration_days,
            max_devices=plan_row.max_devices,
        )
        if plan_row is not None
        else None
    )
    control_operations = tuple(
        ControlOperationSnapshot(
            worker_id=int(worker_id),
            state="running" if state == "claimed" else state,
        )
        for worker_id, state in operation_rows
    )
    maintenance_jobs = tuple(
        MaintenanceSnapshot(worker_id=int(worker_id), state=state)
        for worker_id, state in maintenance_rows
    )
    return ReleaseReadinessSnapshot(
        release_id=settings.vpn_public_trial_release_id,
        dispatch_enabled=settings.vpn_control_dispatch_enabled,
        known_hosts_configured=bool(settings.vpn_control_known_hosts_path.strip()),
        public_trial_enabled=settings.vpn_public_trial_enabled,
        vpn_portal_public_access=settings.vpn_portal_public_access,
        payment_enabled=payment_enabled,
        public_trial_plan_slug=settings.vpn_public_trial_plan_slug,
        endpoint_health_max_age_seconds=settings.vpn_endpoint_health_max_age_seconds,
        plan=plan,
        endpoints=tuple(endpoints),
        control_operations=control_operations,
        maintenance_jobs=maintenance_jobs,
        active_attack_worker_ids=frozenset(attack_worker_ids),
        operational=operational,
        backup=backup,
    )
