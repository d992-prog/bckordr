from __future__ import annotations

import csv
from collections import defaultdict
from datetime import date, datetime
from io import StringIO
import json
from pathlib import Path
import shlex

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_admin
from app.db.base import utcnow
from app.db.models import (
    AttackEvent,
    AttackRun,
    ContactProfile,
    DomainOverridePhase,
    DomainOverrideRule,
    DomainRuleOverride,
    DiscoveryDomain,
    DiscoveryObservation,
    DropDomain,
    RegistrarAccount,
    User,
    WorkerMaintenanceJob,
    WorkerNode,
    WorkerTask,
    ZoneScanCandidate,
    ZoneScanJob,
    ZoneRule,
    ZoneRulePhase,
    ZoneStrategy,
)
from app.db.session import get_db
from app.schemas.common import MessageResponse
from app.schemas.control import (
    AttackEventResponse,
    AttackRegistrationSimulationRequest,
    AttackRunResponse,
    AttackStartRequest,
    AttackStopRequest,
    AllZonefilesSettingsResponse,
    AllZonefilesSettingsUpdateRequest,
    AllZonefilesTestResponse,
    ContactProfileCreateRequest,
    ContactProfilePrefillResponse,
    ContactProfileResponse,
    ContactProfileUpdateRequest,
    ControlOverviewResponse,
    DomainOverrideRuleCreateRequest,
    DomainOverrideRulePhaseCreateRequest,
    DomainOverrideRulePhaseResponse,
    DomainOverrideRulePhaseUpdateRequest,
    DomainOverrideRuleResponse,
    DomainOverrideRuleUpdateRequest,
    DomainOverrideSettingsCreateRequest,
    DomainDryRunBatchRequest,
    DomainDryRunBatchResponse,
    DiscoveryDomainBulkCreateRequest,
    DiscoveryDomainCreateRequest,
    DiscoveryDomainImportResponse,
    DiscoveryDomainResponse,
    DiscoveryObservationCreateRequest,
    DiscoveryObservationResponse,
    DiscoveryZoneStatsResponse,
    DomainOverrideSettingsResponse,
    DomainOverrideSettingsUpdateRequest,
    DomainDryRunResponse,
    DomainImportResponse,
    DropDomainBulkCreateRequest,
    DropDomainCreateRequest,
    DropDomainResponse,
    DropDomainUpdateRequest,
    RegistrarAccountCreateRequest,
    RegistrarAccountResponse,
    RegistrarAccountUpdateRequest,
    RegistrarAccountValidateResponse,
    WorkerNodeCreateRequest,
    WorkerMaintenanceJobResponse,
    WorkerNodeResponse,
    WorkerSetupResponse,
    WorkerNodeUpdateRequest,
    WorkerTaskResponse,
    ZoneScanCandidateResponse,
    ZoneScanJobCreateRequest,
    ZoneScanJobResponse,
    ZoneStrategyCreateRequest,
    ZoneRuleCreateRequest,
    ZoneRulePhaseCreateRequest,
    ZoneRulePhaseResponse,
    ZoneRulePhaseUpdateRequest,
    ZoneRuleResponse,
    ZoneRuleUpdateRequest,
    StrategyPreviewResponse,
    StrategyPreviewWindowResponse,
    ZoneStrategyResponse,
    ZoneStrategyUpdateRequest,
)
from app.services.attack_runtime import (
    build_domain_runtime_snapshots,
    load_effective_strategies,
    plan_immediate_registration_runs,
    plan_attack_runs,
    rebalance_worker_pool,
    recompute_worker_domain_counts,
    recompute_run_statistics,
)
from app.services.audit import add_audit_log
from app.services.bootstrap import ensure_zone_strategy_preset
from app.services.domain_parser import normalize_domain, parse_upload
from app.services.discovery import (
    DiscoveryObservationInput,
    apply_discovery_observation,
    check_discovery_domain_rdap,
    infer_zone,
    normalize_discovery_domain,
    stagger_initial_check_at,
    trim_discovery_observations,
)
from app.services.gandi_dry_run import GandiDryRunResult, run_gandi_domain_dry_run
from app.services.gandi_prefill import build_gandi_contact_prefill
from app.services.strategy_runtime import (
    evaluate_domain_readiness,
    is_domain_due_today,
    preview_strategy_windows,
    resolve_effective_strategy,
)
from app.services.worker_allowlist import sync_worker_runtime_allowlist
from app.services.worker_maintenance import run_worker_maintenance_job
from app.services.security import generate_session_token
from app.services.registrars import validate_registrar_account_remote
from app.services.zone_scanner import (
    add_zone_scan_candidate_to_discovery,
    build_allzonefiles_download_url,
    get_allzonefiles_token,
    ignore_zone_scan_candidate,
    set_allzonefiles_token,
    test_allzonefiles_connection,
)
from app.core.config import get_settings

router = APIRouter(prefix="/control", tags=["control"])
ONLINE_WORKER_MAX_AGE_SECONDS = 120


def _worker_is_online(worker: WorkerNode, now: datetime) -> bool:
    if not worker.is_enabled or worker.status in {"offline", "disabled"}:
        return False
    if worker.last_seen_at is None:
        return worker.status in {"ready", "busy", "planned", "provisioning"}
    return (now - worker.last_seen_at).total_seconds() <= ONLINE_WORKER_MAX_AGE_SECONDS


def _serialize_registrar_account(account: RegistrarAccount) -> RegistrarAccountResponse:
    response = RegistrarAccountResponse.model_validate(account)
    return response.model_copy(update={"api_token": None})


def _shell_quote(value: str | int | float | bool) -> str:
    return shlex.quote(str(value))


def _build_worker_env_lines(worker: WorkerNode, *, runtime_base_url: str, simulate_mode: bool) -> list[str]:
    return [
        f"CONTROL_BASE_URL={runtime_base_url.rstrip('/')}",
        f"WORKER_ID={worker.id}",
        f"CONTROL_TOKEN={worker.control_token or ''}",
        "POLL_INTERVAL_SECONDS=2",
        "HEARTBEAT_INTERVAL_SECONDS=5",
        "REQUEST_TIMEOUT_SECONDS=10",
        "CONNECT_TIMEOUT_SECONDS=2",
        f"SIMULATE_MODE={'true' if simulate_mode else 'false'}",
        "SIMULATE_LATENCY_MS=20",
        "SIMULATE_JITTER_MS=10",
        "SIMULATE_SUCCESS_RATE=1.0",
        "SIMULATE_SUCCESS_STATUS_CODE=200",
        "SIMULATE_FAILURE_STATUS_CODE=503",
        "SIMULATE_RANDOM_SEED=12345",
        "GANDI_CREATE_STATUS_POLL_ENABLED=false",
        "GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5",
        "GANDI_STATUS_POLL_MAX_ATTEMPTS=8",
        "REGISTRATION_CONCURRENCY_MULTIPLIER=8",
        "REGISTRATION_MAX_CONCURRENCY=160",
        "MAX_IDLE_BACKOFF_SECONDS=10",
    ]


def _build_printf_command(path: str, lines: list[str]) -> str:
    quoted_lines = " ".join(_shell_quote(line) for line in lines)
    return f"printf '%s\\n' {quoted_lines} > {_shell_quote(path)}"


def _build_worker_service_command() -> str:
    lines = [
        "[Unit]",
        "Description=Domain Drop Catcher Worker",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        "WorkingDirectory=/opt/domain-drop-catcher/worker",
        "ExecStart=/opt/domain-drop-catcher/worker/.venv/bin/python -m app.main",
        "Restart=always",
        "RestartSec=2",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]
    return _build_printf_command("/etc/systemd/system/domain-drop-worker.service", lines)


def _build_worker_setup_response(
    worker: WorkerNode,
    *,
    runtime_base_url: str,
    simulate_mode: bool,
) -> WorkerSetupResponse:
    settings = get_settings()
    env_path = "/opt/domain-drop-catcher/worker/.env"
    python_bin = settings.worker_setup_python_bin
    env_command = _build_printf_command(
        env_path,
        _build_worker_env_lines(worker, runtime_base_url=runtime_base_url, simulate_mode=simulate_mode),
    )
    mode = "test" if simulate_mode else "live"
    full_install_commands = [
        "apt-get update",
        "apt-get install -y git python3.11 python3.11-venv python3.11-dev build-essential",
        f"git clone {_shell_quote(settings.worker_setup_repository_url)} /opt/domain-drop-catcher",
        f"{_shell_quote(python_bin)} -m venv /opt/domain-drop-catcher/worker/.venv",
        "/opt/domain-drop-catcher/worker/.venv/bin/pip install --upgrade pip",
        "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
        env_command,
        _build_worker_service_command(),
        "systemctl daemon-reload",
        "systemctl enable --now domain-drop-worker.service",
    ]
    update_existing_commands = [
        "cd /opt/domain-drop-catcher && git pull",
        "/opt/domain-drop-catcher/worker/.venv/bin/pip install -e /opt/domain-drop-catcher/worker",
        env_command,
        "systemctl daemon-reload",
        "systemctl restart domain-drop-worker.service",
    ]
    switch_to_test_commands = [
        "sed -i 's/^SIMULATE_MODE=.*/SIMULATE_MODE=true/' /opt/domain-drop-catcher/worker/.env",
        "sed -i 's/^SIMULATE_SUCCESS_RATE=.*/SIMULATE_SUCCESS_RATE=1.0/' /opt/domain-drop-catcher/worker/.env",
        "systemctl restart domain-drop-worker.service",
    ]
    switch_to_live_commands = [
        "sed -i 's/^SIMULATE_MODE=.*/SIMULATE_MODE=false/' /opt/domain-drop-catcher/worker/.env",
        "systemctl restart domain-drop-worker.service",
    ]
    verify_commands = [
        "systemctl status domain-drop-worker.service --no-pager",
        "journalctl -u domain-drop-worker.service -n 120 --no-pager",
        "grep -E '^(CONTROL_BASE_URL|WORKER_ID|SIMULATE_MODE|SIMULATE_SUCCESS_RATE|REGISTRATION_CONCURRENCY_MULTIPLIER|REGISTRATION_MAX_CONCURRENCY)=' /opt/domain-drop-catcher/worker/.env",
    ]
    return WorkerSetupResponse(
        worker_id=worker.id,
        worker_name=worker.name,
        runtime_base_url=runtime_base_url.rstrip("/"),
        mode=mode,
        simulate_mode=simulate_mode,
        env_file=env_path,
        write_env_command=env_command,
        full_install_commands=full_install_commands,
        update_existing_commands=update_existing_commands,
        switch_to_test_commands=switch_to_test_commands,
        switch_to_live_commands=switch_to_live_commands,
        verify_commands=verify_commands,
    )


async def _enforce_single_default_contact(session: AsyncSession, contact_id: int) -> None:
    result = await session.execute(
        select(ContactProfile).where(ContactProfile.id != contact_id, ContactProfile.is_default.is_(True))
    )
    for contact in result.scalars().all():
        contact.is_default = False


async def _get_default_registrar_account_id(db: AsyncSession, registrar_slug: str) -> int | None:
    result = await db.execute(
        select(RegistrarAccount.id)
        .where(RegistrarAccount.registrar_slug == registrar_slug, RegistrarAccount.is_active.is_(True))
        .order_by(RegistrarAccount.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _get_default_contact_profile_id(db: AsyncSession) -> int | None:
    result = await db.execute(
        select(ContactProfile.id).where(ContactProfile.is_default.is_(True)).order_by(ContactProfile.id.asc()).limit(1)
    )
    return result.scalar_one_or_none()


async def _resolve_contact_profile(db: AsyncSession, domain: DropDomain, account: RegistrarAccount | None) -> ContactProfile | None:
    if domain.contact_profile_id:
        return await db.get(ContactProfile, domain.contact_profile_id)
    if account and account.default_contact_profile_id:
        return await db.get(ContactProfile, account.default_contact_profile_id)
    default_contact_id = await _get_default_contact_profile_id(db)
    if default_contact_id is None:
        return None
    return await db.get(ContactProfile, default_contact_id)


async def _run_and_persist_domain_dry_run(db: AsyncSession, domain: DropDomain) -> DomainDryRunResponse:
    account = await db.get(RegistrarAccount, domain.registrar_account_id) if domain.registrar_account_id else None
    contact = await _resolve_contact_profile(db, domain, account)

    if account is None:
        result = GandiDryRunResult(status="invalid", http_status=None, message="Domain has no registrar account", checked_at=utcnow())
    elif contact is None:
        result = GandiDryRunResult(status="invalid", http_status=None, message="Domain has no contact profile", checked_at=utcnow())
    elif account.registrar_slug != "gandi":
        result = GandiDryRunResult(
            status="error",
            http_status=None,
            message=f"Dry-run is not implemented for {account.registrar_slug}",
            checked_at=utcnow(),
        )
    elif not account.supports_dry_run:
        result = GandiDryRunResult(
            status="invalid",
            http_status=None,
            message="Registrar account does not support dry-run",
            checked_at=utcnow(),
        )
    else:
        result = await run_gandi_domain_dry_run(domain, account, contact, get_settings())

    domain.dry_run_checked_at = result.checked_at
    domain.dry_run_status = result.status
    domain.dry_run_http_status = result.http_status
    domain.dry_run_message = result.message
    domain.updated_at = result.checked_at
    db.add(
        AttackEvent(
            domain_id=domain.id,
            level="info" if result.status == "ready" else "warning" if result.status == "invalid" else "error",
            event_type="domain_dry_run",
            message=f"Dry-run {result.status}: HTTP {result.http_status or 'n/a'}",
        )
    )
    return DomainDryRunResponse(
        domain_id=domain.id,
        status=result.status,
        http_status=result.http_status,
        message=result.message,
        checked_at=result.checked_at,
    )


async def _get_zone_strategy_for_domain(db: AsyncSession, zone: str) -> ZoneStrategy | None:
    result = await db.execute(
        select(ZoneStrategy)
        .where(ZoneStrategy.zone == zone, ZoneStrategy.is_active.is_(True))
        .order_by(ZoneStrategy.id.asc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _load_domain_override_rules_and_phases(
    db: AsyncSession,
    domain_rule_override_id: int,
) -> tuple[list[DomainOverrideRule], list[DomainOverridePhase]]:
    rules = (
        await db.execute(
            select(DomainOverrideRule)
            .where(DomainOverrideRule.domain_rule_override_id == domain_rule_override_id)
            .order_by(DomainOverrideRule.priority.desc(), DomainOverrideRule.id.asc())
        )
    ).scalars().all()
    rule_ids = [rule.id for rule in rules]
    phases: list[DomainOverridePhase] = []
    if rule_ids:
        phases = (
            await db.execute(
                select(DomainOverridePhase)
                .where(DomainOverridePhase.domain_override_rule_id.in_(rule_ids))
                .order_by(DomainOverridePhase.sort_order.asc(), DomainOverridePhase.id.asc())
            )
        ).scalars().all()
    return rules, phases


async def _load_zone_rules_and_phases(
    db: AsyncSession,
    zone_strategy_id: int,
) -> tuple[list[ZoneRule], list[ZoneRulePhase]]:
    rules = (
        await db.execute(
            select(ZoneRule)
            .where(ZoneRule.zone_strategy_id == zone_strategy_id, ZoneRule.is_enabled.is_(True))
            .order_by(ZoneRule.priority.desc(), ZoneRule.id.asc())
        )
    ).scalars().all()
    rule_ids = [rule.id for rule in rules]
    phases: list[ZoneRulePhase] = []
    if rule_ids:
        phases = (
            await db.execute(
                select(ZoneRulePhase)
                .where(ZoneRulePhase.zone_rule_id.in_(rule_ids))
                .order_by(ZoneRulePhase.sort_order.asc(), ZoneRulePhase.id.asc())
            )
        ).scalars().all()
    return rules, phases


async def _apply_domain_readiness(db: AsyncSession, domain: DropDomain) -> None:
    zone_strategy = None
    if domain.zone_strategy_id:
        zone_strategy = await db.get(ZoneStrategy, domain.zone_strategy_id)
    elif domain.strategy_mode == "inherit_zone":
        zone_strategy = await _get_zone_strategy_for_domain(db, domain.zone)
        if zone_strategy is not None:
            domain.zone_strategy_id = zone_strategy.id
            if not domain.timezone_name:
                domain.timezone_name = zone_strategy.timezone_name

    domain_override = await db.get(DomainRuleOverride, domain.domain_rule_override_id) if domain.domain_rule_override_id else None
    effective_strategy = None
    rules: list[object] = []
    phases: list[object] = []
    if domain.strategy_mode == "manual_override" and domain_override is not None:
        rules, phases = await _load_domain_override_rules_and_phases(db, domain_override.id)
    elif zone_strategy is not None:
        rules, phases = await _load_zone_rules_and_phases(db, zone_strategy.id)
    if zone_strategy is not None or domain_override is not None:
        effective_strategy = resolve_effective_strategy(
            domain,
            zone_strategy=zone_strategy,
            domain_override=domain_override,
            rules=rules,
            phases=phases,
        )
    readiness = evaluate_domain_readiness(domain, effective_strategy=effective_strategy)
    domain.status = readiness.status if domain.attack_enabled else "paused"
    domain.readiness_reasons = "; ".join(readiness.reasons) if readiness.reasons else None


async def _insert_domains_from_bulk(
    payload: DropDomainBulkCreateRequest,
    db: AsyncSession,
) -> DomainImportResponse:
    inserted: list[DropDomain] = []
    skipped: list[str] = []
    normalized_inputs = [normalize_domain(item) for item in payload.domains]
    existing_domains = {
        item
        for item in (
            await db.execute(
                select(DropDomain.fqdn).where(DropDomain.fqdn.in_([item for item in normalized_inputs if item]))
            )
        ).scalars().all()
    }

    for raw in payload.domains:
        normalized = normalize_domain(raw)
        if not normalized or normalized in existing_domains:
            skipped.append(raw)
            continue
        registrar_account_id = payload.registrar_account_id or await _get_default_registrar_account_id(db, payload.registrar_slug)
        contact_profile_id = payload.contact_profile_id or await _get_default_contact_profile_id(db)
        zone_strategy = await _get_zone_strategy_for_domain(db, payload.zone.lower())
        domain = DropDomain(
            fqdn=normalized,
            zone=payload.zone.lower(),
            timezone_name=zone_strategy.timezone_name if zone_strategy else payload.timezone_name,
            registrar_slug=payload.registrar_slug,
            zone_strategy_id=zone_strategy.id if zone_strategy else payload.zone_strategy_id,
            strategy_mode=payload.strategy_mode,
            registrar_account_id=registrar_account_id,
            contact_profile_id=contact_profile_id,
            drop_date=payload.drop_date,
            priority=payload.priority,
            requested_duration_years=payload.requested_duration_years,
            registration_extra_parameters=payload.registration_extra_parameters,
            attack_enabled=payload.attack_enabled,
            auto_start_enabled=payload.auto_start_enabled,
            auto_start_lead_seconds=payload.auto_start_lead_seconds,
            override_min_guaranteed_rps=payload.override_min_guaranteed_rps,
            window_start_minute=payload.window_start_minute,
            window_start_second=payload.window_start_second,
            window_duration_seconds=payload.window_duration_seconds,
            notes=payload.notes,
        )
        await _apply_domain_readiness(db, domain)
        db.add(domain)
        inserted.append(domain)
        existing_domains.add(normalized)

    await db.commit()
    for domain in inserted:
        await db.refresh(domain)

    return DomainImportResponse(
        inserted=[DropDomainResponse.model_validate(domain) for domain in inserted],
        skipped=skipped,
    )


async def _insert_discovery_domains_from_bulk(
    payload: DiscoveryDomainBulkCreateRequest,
    db: AsyncSession,
) -> DiscoveryDomainImportResponse:
    inserted: list[DiscoveryDomain] = []
    skipped: list[str] = []
    normalized_inputs: list[str] = []

    for raw in payload.domains:
        try:
            normalized_inputs.append(normalize_discovery_domain(raw))
        except ValueError:
            skipped.append(raw)

    existing_domains = {
        item
        for item in (
            await db.execute(
                select(DiscoveryDomain.fqdn).where(DiscoveryDomain.fqdn.in_(normalized_inputs))
            )
        ).scalars().all()
    }

    seen_domains = set(existing_domains)
    new_domains: list[str] = []
    for raw in payload.domains:
        try:
            normalized = normalize_discovery_domain(raw)
        except ValueError:
            continue
        if normalized in seen_domains:
            skipped.append(normalized)
            continue
        new_domains.append(normalized)
        seen_domains.add(normalized)

    base_check_at = utcnow()
    for index, normalized in enumerate(new_domains):
        domain = DiscoveryDomain(
            fqdn=normalized,
            zone=(payload.zone or infer_zone(normalized)).lower(),
            check_interval_seconds=payload.check_interval_seconds,
            source_mode=payload.source_mode,
            drop_prediction_enabled=payload.drop_prediction_enabled,
            notes=payload.notes,
            next_check_at=stagger_initial_check_at(base_check_at, index=index, total=len(new_domains)),
        )
        db.add(domain)
        inserted.append(domain)

    await db.commit()
    for domain in inserted:
        await db.refresh(domain)

    return DiscoveryDomainImportResponse(
        inserted=[DiscoveryDomainResponse.model_validate(domain) for domain in inserted],
        skipped=skipped,
    )


async def _load_domain_runtime_snapshots(
    db: AsyncSession,
    domains: list[DropDomain],
    *,
    now: datetime,
):
    if not domains:
        return {}
    domain_ids = [domain.id for domain in domains]
    strategy_map = await load_effective_strategies(db, domains)
    workers = (
        await db.execute(
            select(WorkerNode)
            .where(WorkerNode.is_enabled.is_(True))
            .order_by(WorkerNode.target_rps.desc(), WorkerNode.max_rps.desc(), WorkerNode.name.asc())
        )
    ).scalars().all()
    active_runs = (
        await db.execute(
            select(AttackRun).where(
                AttackRun.domain_id.in_(domain_ids),
                AttackRun.status.in_(["planned", "running"]),
            )
        )
    ).scalars().all()
    active_tasks = (
        await db.execute(
            select(WorkerTask).where(
                WorkerTask.domain_id.in_(domain_ids),
                WorkerTask.status.in_(["queued", "planned", "running"]),
            )
        )
    ).scalars().all()
    active_tasks_by_domain_id: dict[int, list[WorkerTask]] = defaultdict(list)
    for task in active_tasks:
        active_tasks_by_domain_id[task.domain_id].append(task)
    active_run_by_domain_id = {run.domain_id: run for run in active_runs}
    return build_domain_runtime_snapshots(
        domains,
        workers=workers,
        strategy_map=strategy_map,
        now=now,
        active_run_by_domain_id=active_run_by_domain_id,
        active_tasks_by_domain_id=active_tasks_by_domain_id,
    )


def _serialize_domain_response(domain: DropDomain, runtime_snapshots) -> DropDomainResponse:
    response = DropDomainResponse.model_validate(domain)
    snapshot = runtime_snapshots.get(domain.id)
    if snapshot is None:
        return response
    return response.model_copy(
        update={
            "runtime_minimum_rps": snapshot.minimum_rps,
            "runtime_desired_rps": snapshot.desired_rps,
            "runtime_allocated_rps": snapshot.allocated_rps,
            "runtime_assigned_worker_count": snapshot.assigned_worker_count,
            "runtime_phase_name": snapshot.phase_name,
            "runtime_attack_run_id": snapshot.attack_run_id,
            "runtime_attack_status": snapshot.attack_status,
            "runtime_window_start_at": snapshot.window_start_at,
            "runtime_window_end_at": snapshot.window_end_at,
            "effective_window_start_minute": snapshot.effective_window_start_minute,
            "effective_window_start_second": snapshot.effective_window_start_second,
            "effective_window_duration_seconds": snapshot.effective_window_duration_seconds,
            "effective_window_source": snapshot.effective_window_source,
        }
    )


def _serialize_attack_run_response(run: AttackRun, runtime_snapshots) -> AttackRunResponse:
    response = AttackRunResponse.model_validate(run)
    snapshot = runtime_snapshots.get(run.domain_id)
    if snapshot is None:
        return response
    return response.model_copy(
        update={
            "runtime_minimum_rps": snapshot.minimum_rps,
            "runtime_desired_rps": snapshot.desired_rps,
            "runtime_allocated_rps": snapshot.allocated_rps,
            "runtime_phase_name": snapshot.phase_name,
        }
    )


@router.get("/overview", response_model=ControlOverviewResponse)
async def get_overview(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ControlOverviewResponse:
    del admin
    now = utcnow()
    workers = (await db.execute(select(WorkerNode).order_by(WorkerNode.name.asc()))).scalars().all()
    domains = (await db.execute(select(DropDomain))).scalars().all()
    enabled_workers = [worker for worker in workers if worker.is_enabled]
    online_workers = [worker for worker in enabled_workers if _worker_is_online(worker, now)]
    due_today_domains = [domain for domain in domains if domain.attack_enabled and is_domain_due_today(domain, now)]
    success_today_domains = [domain for domain in domains if domain.success_at is not None and is_domain_due_today(domain, now)]
    return ControlOverviewResponse(
        checked_at=now,
        total_domains=len(domains),
        due_today_domains=len(due_today_domains),
        active_attack_domains=int(
            await db.scalar(select(func.count(DropDomain.id)).where(DropDomain.status.in_(["scheduled", "attacking"])))
            or 0
        ),
        success_today_domains=len(success_today_domains),
        scheduled_runs=int(await db.scalar(select(func.count(AttackRun.id)).where(AttackRun.status == "planned")) or 0),
        running_runs=int(await db.scalar(select(func.count(AttackRun.id)).where(AttackRun.status == "running")) or 0),
        total_accounts=int(await db.scalar(select(func.count(RegistrarAccount.id))) or 0),
        total_contacts=int(await db.scalar(select(func.count(ContactProfile.id))) or 0),
        capacity={
            "current_rps": round(sum(worker.current_rps for worker in enabled_workers), 2),
            "target_rps": round(sum(worker.target_rps for worker in enabled_workers), 2),
            "max_rps": round(sum(worker.max_rps for worker in enabled_workers), 2),
            "enabled_workers": len(enabled_workers),
            "online_workers": len(online_workers),
        },
    )


@router.get("/domains", response_model=list[DropDomainResponse])
async def list_domains(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DropDomainResponse]:
    del admin
    result = await db.execute(
        select(DropDomain).order_by(DropDomain.drop_date.asc(), DropDomain.priority.desc(), DropDomain.fqdn.asc())
    )
    domains = result.scalars().all()
    runtime_snapshots = await _load_domain_runtime_snapshots(db, domains, now=utcnow())
    return [_serialize_domain_response(domain, runtime_snapshots) for domain in domains]


@router.get("/zone-strategies", response_model=list[ZoneStrategyResponse])
async def list_zone_strategies(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ZoneStrategyResponse]:
    del admin
    result = await db.execute(select(ZoneStrategy).order_by(ZoneStrategy.zone.asc(), ZoneStrategy.name.asc()))
    return [ZoneStrategyResponse.model_validate(strategy) for strategy in result.scalars().all()]


@router.post("/zone-strategies", response_model=ZoneStrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_strategy(
    payload: ZoneStrategyCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneStrategyResponse:
    strategy = ZoneStrategy(**payload.model_dump())
    db.add(strategy)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_strategy_create",
        details=f"zone={payload.zone} name={payload.name}",
    )
    await db.commit()
    await db.refresh(strategy)
    return ZoneStrategyResponse.model_validate(strategy)


@router.post("/zone-strategies/presets/{zone}", response_model=ZoneStrategyResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_strategy_preset(
    zone: str,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneStrategyResponse:
    try:
        strategy = await ensure_zone_strategy_preset(db, zone)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_strategy_preset_create",
        details=f"zone={strategy.zone} strategy_id={strategy.id}",
    )
    await db.commit()
    await db.refresh(strategy)
    return ZoneStrategyResponse.model_validate(strategy)


@router.patch("/zone-strategies/{strategy_id}", response_model=ZoneStrategyResponse)
async def update_zone_strategy(
    strategy_id: int,
    payload: ZoneStrategyUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneStrategyResponse:
    strategy = await db.get(ZoneStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Zone strategy not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(strategy, field, value)
    strategy.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_strategy_update",
        details=f"strategy_id={strategy_id}",
    )
    await db.commit()
    await db.refresh(strategy)
    return ZoneStrategyResponse.model_validate(strategy)


@router.get("/zone-strategies/{strategy_id}/rules", response_model=list[ZoneRuleResponse])
async def list_zone_rules(
    strategy_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ZoneRuleResponse]:
    del admin
    result = await db.execute(
        select(ZoneRule)
        .where(ZoneRule.zone_strategy_id == strategy_id)
        .order_by(ZoneRule.priority.desc(), ZoneRule.id.asc())
    )
    return [ZoneRuleResponse.model_validate(rule) for rule in result.scalars().all()]


@router.post("/zone-strategies/{strategy_id}/rules", response_model=ZoneRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_rule(
    strategy_id: int,
    payload: ZoneRuleCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneRuleResponse:
    strategy = await db.get(ZoneStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Zone strategy not found")
    rule = ZoneRule(zone_strategy_id=strategy_id, **payload.model_dump())
    db.add(rule)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_rule_create",
        details=f"strategy_id={strategy_id} name={payload.name}",
    )
    await db.commit()
    await db.refresh(rule)
    return ZoneRuleResponse.model_validate(rule)


@router.patch("/zone-rules/{rule_id}", response_model=ZoneRuleResponse)
async def update_zone_rule(
    rule_id: int,
    payload: ZoneRuleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneRuleResponse:
    rule = await db.get(ZoneRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Zone rule not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    rule.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_rule_update",
        details=f"rule_id={rule_id}",
    )
    await db.commit()
    await db.refresh(rule)
    return ZoneRuleResponse.model_validate(rule)


@router.delete("/zone-rules/{rule_id}", response_model=MessageResponse)
async def delete_zone_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    rule = await db.get(ZoneRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Zone rule not found")
    await db.delete(rule)
    await db.commit()
    return MessageResponse(detail="Zone rule deleted")


@router.get("/zone-rules/{rule_id}/phases", response_model=list[ZoneRulePhaseResponse])
async def list_zone_rule_phases(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ZoneRulePhaseResponse]:
    del admin
    result = await db.execute(
        select(ZoneRulePhase)
        .where(ZoneRulePhase.zone_rule_id == rule_id)
        .order_by(ZoneRulePhase.sort_order.asc(), ZoneRulePhase.id.asc())
    )
    return [ZoneRulePhaseResponse.model_validate(phase) for phase in result.scalars().all()]


@router.post("/zone-rules/{rule_id}/phases", response_model=ZoneRulePhaseResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_rule_phase(
    rule_id: int,
    payload: ZoneRulePhaseCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneRulePhaseResponse:
    rule = await db.get(ZoneRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Zone rule not found")
    phase = ZoneRulePhase(zone_rule_id=rule_id, **payload.model_dump())
    db.add(phase)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_rule_phase_create",
        details=f"rule_id={rule_id} name={payload.name}",
    )
    await db.commit()
    await db.refresh(phase)
    return ZoneRulePhaseResponse.model_validate(phase)


@router.patch("/zone-rule-phases/{phase_id}", response_model=ZoneRulePhaseResponse)
async def update_zone_rule_phase(
    phase_id: int,
    payload: ZoneRulePhaseUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneRulePhaseResponse:
    phase = await db.get(ZoneRulePhase, phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail="Zone rule phase not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(phase, field, value)
    phase.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_rule_phase_update",
        details=f"phase_id={phase_id}",
    )
    await db.commit()
    await db.refresh(phase)
    return ZoneRulePhaseResponse.model_validate(phase)


@router.delete("/zone-rule-phases/{phase_id}", response_model=MessageResponse)
async def delete_zone_rule_phase(
    phase_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    phase = await db.get(ZoneRulePhase, phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail="Zone rule phase not found")
    await db.delete(phase)
    await db.commit()
    return MessageResponse(detail="Zone rule phase deleted")


@router.get("/zone-strategies/{strategy_id}/preview", response_model=StrategyPreviewResponse)
async def preview_zone_strategy(
    strategy_id: int,
    target_date: date,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> StrategyPreviewResponse:
    del admin
    strategy = await db.get(ZoneStrategy, strategy_id)
    if strategy is None:
        raise HTTPException(status_code=404, detail="Zone strategy not found")
    rules = (
        await db.execute(
            select(ZoneRule)
            .where(ZoneRule.zone_strategy_id == strategy_id, ZoneRule.is_enabled.is_(True))
            .order_by(ZoneRule.priority.desc(), ZoneRule.id.asc())
        )
    ).scalars().all()
    domain_stub = type("PreviewDomain", (), {"drop_date": target_date})()
    preview = preview_strategy_windows(domain_stub, strategy=strategy, rules=rules, target_date=target_date)
    return StrategyPreviewResponse(
        strategy_id=strategy.id,
        timezone_name=strategy.timezone_name,
        resolution_mode=preview.resolution_mode,
        target_date=target_date,
        windows=[
            StrategyPreviewWindowResponse(
                rule_id=window.rule_id,
                priority=window.priority,
                start_at=window.start_at,
                end_at=window.end_at,
                rule_name=window.rule_name,
            )
            for window in preview.windows
        ],
    )


async def _ensure_domain_override(
    db: AsyncSession,
    domain: DropDomain,
) -> DomainRuleOverride:
    if domain.domain_rule_override_id:
        domain_override = await db.get(DomainRuleOverride, domain.domain_rule_override_id)
        if domain_override is not None:
            return domain_override
    domain_override = DomainRuleOverride(
        timezone_name=domain.timezone_name or "UTC",
        rule_resolution_mode="priority",
        default_min_guaranteed_rps=domain.override_min_guaranteed_rps or 1.0,
        notes=domain.notes,
    )
    db.add(domain_override)
    await db.flush()
    domain.domain_rule_override_id = domain_override.id
    domain.strategy_mode = "manual_override"
    return domain_override


@router.get("/domains/{domain_id}/override", response_model=DomainOverrideSettingsResponse)
async def get_domain_override(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideSettingsResponse:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    if not domain.domain_rule_override_id:
        raise HTTPException(status_code=404, detail="Domain override not found")
    domain_override = await db.get(DomainRuleOverride, domain.domain_rule_override_id)
    if domain_override is None:
        raise HTTPException(status_code=404, detail="Domain override not found")
    return DomainOverrideSettingsResponse.model_validate(domain_override)


@router.post("/domains/{domain_id}/override", response_model=DomainOverrideSettingsResponse, status_code=status.HTTP_201_CREATED)
async def create_domain_override(
    domain_id: int,
    payload: DomainOverrideSettingsCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideSettingsResponse:
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    if domain.domain_rule_override_id:
        raise HTTPException(status_code=400, detail="Domain override already exists")
    domain_override = DomainRuleOverride(**payload.model_dump())
    db.add(domain_override)
    await db.flush()
    domain.domain_rule_override_id = domain_override.id
    domain.strategy_mode = "manual_override"
    await _apply_domain_readiness(db, domain)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_create",
        details=f"domain_id={domain_id} override_id={domain_override.id}",
    )
    await db.commit()
    await db.refresh(domain_override)
    return DomainOverrideSettingsResponse.model_validate(domain_override)


@router.patch("/domains/{domain_id}/override", response_model=DomainOverrideSettingsResponse)
async def update_domain_override(
    domain_id: int,
    payload: DomainOverrideSettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideSettingsResponse:
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    domain_override = await _ensure_domain_override(db, domain)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(domain_override, field, value)
    domain_override.updated_at = utcnow()
    await _apply_domain_readiness(db, domain)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_update",
        details=f"domain_id={domain_id} override_id={domain_override.id}",
    )
    await db.commit()
    await db.refresh(domain_override)
    return DomainOverrideSettingsResponse.model_validate(domain_override)


@router.get("/domains/{domain_id}/override/rules", response_model=list[DomainOverrideRuleResponse])
async def list_domain_override_rules(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DomainOverrideRuleResponse]:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None or not domain.domain_rule_override_id:
        raise HTTPException(status_code=404, detail="Domain override not found")
    result = await db.execute(
        select(DomainOverrideRule)
        .where(DomainOverrideRule.domain_rule_override_id == domain.domain_rule_override_id)
        .order_by(DomainOverrideRule.priority.desc(), DomainOverrideRule.id.asc())
    )
    return [DomainOverrideRuleResponse.model_validate(rule) for rule in result.scalars().all()]


@router.post("/domains/{domain_id}/override/rules", response_model=DomainOverrideRuleResponse, status_code=status.HTTP_201_CREATED)
async def create_domain_override_rule(
    domain_id: int,
    payload: DomainOverrideRuleCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideRuleResponse:
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    domain_override = await _ensure_domain_override(db, domain)
    rule = DomainOverrideRule(domain_rule_override_id=domain_override.id, **payload.model_dump())
    db.add(rule)
    await db.flush()
    await _apply_domain_readiness(db, domain)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_rule_create",
        details=f"domain_id={domain_id} rule_name={payload.name}",
    )
    await db.commit()
    await db.refresh(rule)
    return DomainOverrideRuleResponse.model_validate(rule)


@router.patch("/domain-override-rules/{rule_id}", response_model=DomainOverrideRuleResponse)
async def update_domain_override_rule(
    rule_id: int,
    payload: DomainOverrideRuleUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideRuleResponse:
    rule = await db.get(DomainOverrideRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Domain override rule not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(rule, field, value)
    rule.updated_at = utcnow()
    domain = (
        await db.execute(select(DropDomain).where(DropDomain.domain_rule_override_id == rule.domain_rule_override_id).limit(1))
    ).scalar_one_or_none()
    if domain is not None:
        await _apply_domain_readiness(db, domain)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_rule_update",
        details=f"rule_id={rule_id}",
    )
    await db.commit()
    await db.refresh(rule)
    return DomainOverrideRuleResponse.model_validate(rule)


@router.delete("/domain-override-rules/{rule_id}", response_model=MessageResponse)
async def delete_domain_override_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    rule = await db.get(DomainOverrideRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Domain override rule not found")
    domain = (
        await db.execute(select(DropDomain).where(DropDomain.domain_rule_override_id == rule.domain_rule_override_id).limit(1))
    ).scalar_one_or_none()
    await db.delete(rule)
    if domain is not None:
        await _apply_domain_readiness(db, domain)
    await db.commit()
    return MessageResponse(detail="Domain override rule deleted")


@router.get("/domain-override-rules/{rule_id}/phases", response_model=list[DomainOverrideRulePhaseResponse])
async def list_domain_override_rule_phases(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DomainOverrideRulePhaseResponse]:
    del admin
    result = await db.execute(
        select(DomainOverridePhase)
        .where(DomainOverridePhase.domain_override_rule_id == rule_id)
        .order_by(DomainOverridePhase.sort_order.asc(), DomainOverridePhase.id.asc())
    )
    return [DomainOverrideRulePhaseResponse.model_validate(phase) for phase in result.scalars().all()]


@router.post("/domain-override-rules/{rule_id}/phases", response_model=DomainOverrideRulePhaseResponse, status_code=status.HTTP_201_CREATED)
async def create_domain_override_rule_phase(
    rule_id: int,
    payload: DomainOverrideRulePhaseCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideRulePhaseResponse:
    rule = await db.get(DomainOverrideRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="Domain override rule not found")
    phase = DomainOverridePhase(domain_override_rule_id=rule_id, **payload.model_dump())
    db.add(phase)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_phase_create",
        details=f"rule_id={rule_id} name={payload.name}",
    )
    await db.commit()
    await db.refresh(phase)
    return DomainOverrideRulePhaseResponse.model_validate(phase)


@router.patch("/domain-override-phases/{phase_id}", response_model=DomainOverrideRulePhaseResponse)
async def update_domain_override_phase(
    phase_id: int,
    payload: DomainOverrideRulePhaseUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainOverrideRulePhaseResponse:
    phase = await db.get(DomainOverridePhase, phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail="Domain override phase not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(phase, field, value)
    phase.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="domain_override_phase_update",
        details=f"phase_id={phase_id}",
    )
    await db.commit()
    await db.refresh(phase)
    return DomainOverrideRulePhaseResponse.model_validate(phase)


@router.delete("/domain-override-phases/{phase_id}", response_model=MessageResponse)
async def delete_domain_override_phase(
    phase_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    phase = await db.get(DomainOverridePhase, phase_id)
    if phase is None:
        raise HTTPException(status_code=404, detail="Domain override phase not found")
    await db.delete(phase)
    await db.commit()
    return MessageResponse(detail="Domain override phase deleted")


@router.get("/domains/{domain_id}/override/preview", response_model=StrategyPreviewResponse)
async def preview_domain_override(
    domain_id: int,
    target_date: date,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> StrategyPreviewResponse:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None or not domain.domain_rule_override_id:
        raise HTTPException(status_code=404, detail="Domain override not found")
    domain_override = await db.get(DomainRuleOverride, domain.domain_rule_override_id)
    if domain_override is None:
        raise HTTPException(status_code=404, detail="Domain override not found")
    rules, _phases = await _load_domain_override_rules_and_phases(db, domain_override.id)
    preview_domain_stub = type("PreviewDomain", (), {"drop_date": target_date})()
    preview = preview_strategy_windows(preview_domain_stub, strategy=domain_override, rules=rules, target_date=target_date)
    return StrategyPreviewResponse(
        strategy_id=domain_override.id,
        timezone_name=domain_override.timezone_name,
        resolution_mode=preview.resolution_mode,
        target_date=target_date,
        windows=[
            StrategyPreviewWindowResponse(
                rule_id=window.rule_id,
                priority=window.priority,
                start_at=window.start_at,
                end_at=window.end_at,
                rule_name=window.rule_name,
            )
            for window in preview.windows
        ],
    )


@router.post("/domains", response_model=DomainImportResponse, status_code=status.HTTP_201_CREATED)
async def create_domains(
    payload: DropDomainCreateRequest | DropDomainBulkCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainImportResponse:
    del admin
    if isinstance(payload, DropDomainCreateRequest):
        payload = DropDomainBulkCreateRequest(
            domains=[payload.fqdn],
            zone=payload.zone,
            timezone_name=payload.timezone_name,
            registrar_slug=payload.registrar_slug,
            zone_strategy_id=payload.zone_strategy_id,
            strategy_mode=payload.strategy_mode,
            registrar_account_id=payload.registrar_account_id,
            contact_profile_id=payload.contact_profile_id,
            drop_date=payload.drop_date,
            priority=payload.priority,
            requested_duration_years=payload.requested_duration_years,
            registration_extra_parameters=payload.registration_extra_parameters,
            attack_enabled=payload.attack_enabled,
            auto_start_enabled=payload.auto_start_enabled,
            auto_start_lead_seconds=payload.auto_start_lead_seconds,
            override_min_guaranteed_rps=payload.override_min_guaranteed_rps,
            window_start_minute=payload.window_start_minute,
            window_start_second=payload.window_start_second,
            window_duration_seconds=payload.window_duration_seconds,
            notes=payload.notes,
        )
    return await _insert_domains_from_bulk(payload, db)


@router.post("/domains/import", response_model=DomainImportResponse, status_code=status.HTTP_201_CREATED)
async def import_domains(
    file: UploadFile = File(...),
    drop_date: date = Form(...),
    zone: str = Form("fr"),
    timezone_name: str = Form("Europe/Paris"),
    registrar_slug: str = Form("gandi"),
    zone_strategy_id: int | None = Form(default=None),
    strategy_mode: str = Form(default="inherit_zone"),
    registrar_account_id: int | None = Form(default=None),
    contact_profile_id: int | None = Form(default=None),
    priority: int = Form(default=100),
    requested_duration_years: int = Form(default=1),
    registration_extra_parameters: str | None = Form(default=None),
    attack_enabled: bool = Form(default=True),
    auto_start_enabled: bool = Form(default=False),
    auto_start_lead_seconds: int = Form(default=90),
    override_min_guaranteed_rps: float | None = Form(default=None),
    window_start_minute: int = Form(default=31),
    window_start_second: int = Form(default=30),
    window_duration_seconds: int = Form(default=95),
    notes: str | None = Form(default=None),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainImportResponse:
    del admin
    domains = await parse_upload(file, 5 * 1024 * 1024)
    return await _insert_domains_from_bulk(
        DropDomainBulkCreateRequest(
            domains=domains,
            zone=zone,
            timezone_name=timezone_name,
            registrar_slug=registrar_slug,
            zone_strategy_id=zone_strategy_id,
            strategy_mode=strategy_mode,
            registrar_account_id=registrar_account_id,
            contact_profile_id=contact_profile_id,
            drop_date=drop_date,
            priority=priority,
            requested_duration_years=requested_duration_years,
            registration_extra_parameters=registration_extra_parameters,
            attack_enabled=attack_enabled,
            auto_start_enabled=auto_start_enabled,
            auto_start_lead_seconds=auto_start_lead_seconds,
            override_min_guaranteed_rps=override_min_guaranteed_rps,
            window_start_minute=window_start_minute,
            window_start_second=window_start_second,
            window_duration_seconds=window_duration_seconds,
            notes=notes,
        ),
        db,
    )


@router.patch("/domains/{domain_id}", response_model=DropDomainResponse)
async def update_domain(
    domain_id: int,
    payload: DropDomainUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DropDomainResponse:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    data = payload.model_dump(exclude_unset=True)
    if "fqdn" in data:
        normalized = normalize_domain(data["fqdn"])
        if not normalized:
            raise HTTPException(status_code=400, detail="Invalid domain")
        data["fqdn"] = normalized
    for field, value in data.items():
        setattr(domain, field, value)
    await _apply_domain_readiness(db, domain)
    domain.updated_at = utcnow()
    await db.commit()
    await db.refresh(domain)
    return DropDomainResponse.model_validate(domain)


@router.post("/domains/{domain_id}/dry-run", response_model=DomainDryRunResponse)
async def dry_run_domain_registration(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainDryRunResponse:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    response = await _run_and_persist_domain_dry_run(db, domain)
    await db.commit()
    return response


@router.post("/domains/dry-run/batch", response_model=DomainDryRunBatchResponse)
async def dry_run_domains_batch(
    payload: DomainDryRunBatchRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DomainDryRunBatchResponse:
    del admin
    now = utcnow()
    query = select(DropDomain).where(DropDomain.attack_enabled.is_(True))
    if payload.domain_ids:
        query = query.where(DropDomain.id.in_(payload.domain_ids))
    if payload.only_ready:
        query = query.where(DropDomain.status == "ready")

    domains = (
        await db.execute(query.order_by(DropDomain.priority.desc(), DropDomain.drop_date.asc(), DropDomain.fqdn.asc()))
    ).scalars().all()
    if payload.due_today_only:
        domains = [domain for domain in domains if is_domain_due_today(domain, now)]

    results: list[DomainDryRunResponse] = []
    for domain in domains:
        results.append(await _run_and_persist_domain_dry_run(db, domain))

    await db.commit()
    return DomainDryRunBatchResponse(
        total=len(results),
        ready=sum(1 for item in results if item.status == "ready"),
        invalid=sum(1 for item in results if item.status == "invalid"),
        error=sum(1 for item in results if item.status == "error"),
        results=results,
    )


@router.delete("/domains/{domain_id}", response_model=MessageResponse)
async def delete_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    domain = await db.get(DropDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Domain not found")
    await db.delete(domain)
    await db.commit()
    return MessageResponse(detail="Domain deleted")


@router.get("/discovery/domains", response_model=list[DiscoveryDomainResponse])
async def list_discovery_domains(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DiscoveryDomainResponse]:
    del admin
    result = await db.execute(select(DiscoveryDomain).order_by(DiscoveryDomain.fqdn.asc()))
    return [DiscoveryDomainResponse.model_validate(domain) for domain in result.scalars().all()]


@router.post("/discovery/domains", response_model=DiscoveryDomainImportResponse, status_code=status.HTTP_201_CREATED)
async def create_discovery_domain(
    payload: DiscoveryDomainCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiscoveryDomainImportResponse:
    del admin
    return await _insert_discovery_domains_from_bulk(
        DiscoveryDomainBulkCreateRequest(
            domains=[payload.fqdn],
            zone=payload.zone,
            check_interval_seconds=payload.check_interval_seconds,
            source_mode=payload.source_mode,
            drop_prediction_enabled=payload.drop_prediction_enabled,
            notes=payload.notes,
        ),
        db,
    )


@router.post(
    "/discovery/domains/import",
    response_model=DiscoveryDomainImportResponse,
    status_code=status.HTTP_201_CREATED,
)
async def import_discovery_domains(
    payload: DiscoveryDomainBulkCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiscoveryDomainImportResponse:
    del admin
    return await _insert_discovery_domains_from_bulk(payload, db)


@router.post(
    "/discovery/domains/{domain_id}/observations",
    response_model=DiscoveryDomainResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_discovery_observation(
    domain_id: int,
    payload: DiscoveryObservationCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiscoveryDomainResponse:
    del admin
    domain = await db.get(DiscoveryDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Discovery domain not found")

    observed_at = utcnow()
    observation_input = DiscoveryObservationInput(
        source=payload.source,
        observed_at=observed_at,
        http_status=payload.http_status,
        lifecycle_stage=payload.lifecycle_stage,
        availability_status=payload.availability_status,
        status_codes=payload.status_codes,
        raw_response=payload.raw_response,
        error=payload.error,
    )
    apply_discovery_observation(domain, observation_input)
    db.add(
        DiscoveryObservation(
            discovery_domain_id=domain.id,
            source=payload.source,
            observed_at=observed_at,
            http_status=payload.http_status,
            lifecycle_stage=domain.last_lifecycle_stage,
            availability_status=payload.availability_status,
            status_codes=json.dumps(payload.status_codes, ensure_ascii=True) if payload.status_codes else None,
            raw_response=payload.raw_response,
            error=payload.error,
        )
    )
    await db.flush()
    await trim_discovery_observations(db, domain.id)
    await db.commit()
    await db.refresh(domain)
    return DiscoveryDomainResponse.model_validate(domain)


@router.post("/discovery/domains/{domain_id}/check", response_model=DiscoveryDomainResponse)
async def check_discovery_domain_now(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiscoveryDomainResponse:
    del admin
    domain = await db.get(DiscoveryDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Discovery domain not found")

    observation = await check_discovery_domain_rdap(
        domain,
        bootstrap_url=get_settings().discovery_rdap_bootstrap_url,
        timeout_seconds=get_settings().discovery_timeout_seconds,
    )
    apply_discovery_observation(domain, observation)
    db.add(
        DiscoveryObservation(
            discovery_domain_id=domain.id,
            source=observation.source,
            observed_at=observation.observed_at,
            http_status=observation.http_status,
            latency_ms=observation.latency_ms,
            lifecycle_stage=observation.lifecycle_stage,
            availability_status=observation.availability_status,
            status_codes=json.dumps(observation.status_codes, ensure_ascii=True) if observation.status_codes else None,
            raw_response=observation.raw_response,
            error=observation.error,
        )
    )
    await db.flush()
    await trim_discovery_observations(db, domain.id)
    await db.commit()
    await db.refresh(domain)
    return DiscoveryDomainResponse.model_validate(domain)


@router.get("/discovery/domains/available/export.csv")
async def export_available_discovery_domains(
    zone: str | None = None,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> Response:
    del admin
    query = select(DiscoveryDomain).where(DiscoveryDomain.status == "available")
    if zone:
        query = query.where(DiscoveryDomain.zone == zone.strip().lower())
    result = await db.execute(query.order_by(DiscoveryDomain.available_first_seen_at.asc(), DiscoveryDomain.fqdn.asc()))
    domains = list(result.scalars().all())

    fieldnames = [
        "fqdn",
        "zone",
        "status",
        "available_first_seen_at",
        "last_checked_at",
        "last_lifecycle_stage",
        "last_availability",
        "last_error",
        "predicted_drop_start_at",
        "predicted_drop_end_at",
    ]
    for index in range(1, 6):
        fieldnames.extend(
            [
                f"attempt_{index}_observed_at",
                f"attempt_{index}_source",
                f"attempt_{index}_http_status",
                f"attempt_{index}_lifecycle",
                f"attempt_{index}_availability",
                f"attempt_{index}_status_codes",
                f"attempt_{index}_error",
            ]
        )

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for domain in domains:
        observations_result = await db.execute(
            select(DiscoveryObservation)
            .where(DiscoveryObservation.discovery_domain_id == domain.id)
            .order_by(DiscoveryObservation.observed_at.desc(), DiscoveryObservation.id.desc())
            .limit(5)
        )
        observations = list(observations_result.scalars().all())
        row = {
            "fqdn": domain.fqdn,
            "zone": domain.zone,
            "status": domain.status,
            "available_first_seen_at": _csv_datetime(domain.available_first_seen_at),
            "last_checked_at": _csv_datetime(domain.last_checked_at),
            "last_lifecycle_stage": domain.last_lifecycle_stage or "",
            "last_availability": domain.last_availability or "",
            "last_error": domain.last_error or "",
            "predicted_drop_start_at": _csv_datetime(domain.predicted_drop_start_at),
            "predicted_drop_end_at": _csv_datetime(domain.predicted_drop_end_at),
        }
        for index, observation in enumerate(observations, start=1):
            row[f"attempt_{index}_observed_at"] = _csv_datetime(observation.observed_at)
            row[f"attempt_{index}_source"] = observation.source
            row[f"attempt_{index}_http_status"] = observation.http_status or ""
            row[f"attempt_{index}_lifecycle"] = observation.lifecycle_stage or ""
            row[f"attempt_{index}_availability"] = observation.availability_status or ""
            row[f"attempt_{index}_status_codes"] = observation.status_codes or ""
            row[f"attempt_{index}_error"] = observation.error or ""
        writer.writerow(row)

    suffix = f"-{zone.strip().lower()}" if zone else ""
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="discovery-available{suffix}.csv"'},
    )


def _csv_datetime(value: datetime | None) -> str:
    return value.isoformat() if value else ""


@router.get(
    "/discovery/domains/{domain_id}/observations",
    response_model=list[DiscoveryObservationResponse],
)
async def list_discovery_observations(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DiscoveryObservationResponse]:
    del admin
    domain_exists = await db.scalar(select(DiscoveryDomain.id).where(DiscoveryDomain.id == domain_id).limit(1))
    if domain_exists is None:
        raise HTTPException(status_code=404, detail="Discovery domain not found")
    result = await db.execute(
        select(DiscoveryObservation)
        .where(DiscoveryObservation.discovery_domain_id == domain_id)
        .order_by(DiscoveryObservation.observed_at.desc(), DiscoveryObservation.id.desc())
        .limit(200)
    )
    return [DiscoveryObservationResponse.model_validate(item) for item in result.scalars().all()]


@router.get("/discovery/zone-stats", response_model=list[DiscoveryZoneStatsResponse])
async def list_discovery_zone_stats(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[DiscoveryZoneStatsResponse]:
    del admin
    result = await db.execute(
        select(
            DiscoveryDomain.zone,
            func.count(DiscoveryDomain.id),
            func.sum(case((DiscoveryDomain.status == "pending_delete", 1), else_=0)),
            func.sum(case((DiscoveryDomain.status == "available", 1), else_=0)),
            func.sum(case((DiscoveryDomain.predicted_drop_start_at.is_not(None), 1), else_=0)),
        ).group_by(DiscoveryDomain.zone)
    )
    return [
        DiscoveryZoneStatsResponse(
            zone=row[0],
            total=int(row[1] or 0),
            pending_delete=int(row[2] or 0),
            available=int(row[3] or 0),
            predicted=int(row[4] or 0),
        )
        for row in result.all()
    ]


@router.delete("/discovery/domains/{domain_id}", response_model=MessageResponse)
async def delete_discovery_domain(
    domain_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    domain = await db.get(DiscoveryDomain, domain_id)
    if domain is None:
        raise HTTPException(status_code=404, detail="Discovery domain not found")
    await db.delete(domain)
    await db.commit()
    return MessageResponse(detail="Discovery domain deleted")


@router.get("/zone-scanner/settings", response_model=AllZonefilesSettingsResponse)
async def get_zone_scanner_settings(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AllZonefilesSettingsResponse:
    del admin
    token = await get_allzonefiles_token(db)
    settings = get_settings()
    return AllZonefilesSettingsResponse(configured=bool(token), base_url=settings.allzonefiles_base_url)


@router.post("/zone-scanner/settings", response_model=AllZonefilesSettingsResponse)
async def update_zone_scanner_settings(
    payload: AllZonefilesSettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AllZonefilesSettingsResponse:
    await set_allzonefiles_token(db, payload.api_token)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="allzonefiles_settings_update",
        details="token_configured=true" if payload.api_token else "token_configured=false",
    )
    await db.commit()
    settings = get_settings()
    return AllZonefilesSettingsResponse(configured=bool(payload.api_token), base_url=settings.allzonefiles_base_url)


@router.post("/zone-scanner/settings/test", response_model=AllZonefilesTestResponse)
async def test_zone_scanner_settings(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> AllZonefilesTestResponse:
    del admin
    token = await get_allzonefiles_token(db)
    if not token:
        raise HTTPException(status_code=400, detail="AllZonefiles API token is not configured")
    ok, message, zones_count = await test_allzonefiles_connection(
        token=token,
        base_url=get_settings().allzonefiles_base_url,
    )
    return AllZonefilesTestResponse(ok=ok, message=message, zones_count=zones_count)


@router.get("/zone-scanner/jobs", response_model=list[ZoneScanJobResponse])
async def list_zone_scan_jobs(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ZoneScanJobResponse]:
    del admin
    result = await db.execute(select(ZoneScanJob).order_by(ZoneScanJob.created_at.desc(), ZoneScanJob.id.desc()).limit(100))
    return [ZoneScanJobResponse.model_validate(job) for job in result.scalars().all()]


@router.post("/zone-scanner/jobs", response_model=ZoneScanJobResponse, status_code=status.HTTP_201_CREATED)
async def create_zone_scan_job(
    payload: ZoneScanJobCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneScanJobResponse:
    token = await get_allzonefiles_token(db)
    if not token:
        raise HTTPException(status_code=400, detail="AllZonefiles API token is not configured")
    settings = get_settings()
    try:
        download_url = build_allzonefiles_download_url(
            base_url=settings.allzonefiles_base_url,
            source_type=payload.source_type,
            zone=payload.zone,
            source_date=payload.source_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    job = ZoneScanJob(
        zone=payload.zone.lower().lstrip("."),
        source_type=payload.source_type,
        source_date=payload.source_date,
        download_url=download_url,
        min_score=payload.min_score,
        limit_output=payload.limit_output,
        max_rdap_checks=payload.max_rdap_checks,
        concurrency=payload.concurrency,
        rdap_timeout_seconds=payload.rdap_timeout_seconds,
        pending_delete_min_days=payload.pending_delete_min_days,
        pending_delete_max_days=payload.pending_delete_max_days,
        reservoir_size=payload.reservoir_size,
        random_seed=payload.random_seed,
        keep_file=payload.keep_file,
    )
    db.add(job)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="zone_scan_job_create",
        details=f"zone={job.zone} source_type={job.source_type} max_rdap_checks={job.max_rdap_checks}",
    )
    await db.commit()
    await db.refresh(job)
    return ZoneScanJobResponse.model_validate(job)


@router.post("/zone-scanner/jobs/{job_id}/cancel", response_model=ZoneScanJobResponse)
async def cancel_zone_scan_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneScanJobResponse:
    del admin
    job = await db.get(ZoneScanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Zone scan job not found")
    if job.status not in {"completed", "failed"}:
        job.status = "cancelled"
        job.finished_at = utcnow()
    await db.commit()
    await db.refresh(job)
    return ZoneScanJobResponse.model_validate(job)


@router.delete("/zone-scanner/jobs/{job_id}/file", response_model=MessageResponse)
async def delete_zone_scan_job_file(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    job = await db.get(ZoneScanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Zone scan job not found")
    if not job.file_path:
        return MessageResponse(detail="No file to delete")
    path = Path(job.file_path)
    if path.exists() and path.is_file():
        path.unlink()
    job.file_path = None
    job.file_name = None
    await db.commit()
    return MessageResponse(detail="Zone scan file deleted")


@router.delete("/zone-scanner/jobs/{job_id}", response_model=MessageResponse)
async def delete_zone_scan_job(
    job_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    job = await db.get(ZoneScanJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Zone scan job not found")
    if job.status in {"downloading", "scanning"}:
        raise HTTPException(status_code=400, detail="Cancel running job before deleting it")
    if job.file_path:
        path = Path(job.file_path)
        if path.exists() and path.is_file():
            path.unlink()
    await db.delete(job)
    await db.commit()
    return MessageResponse(detail="Zone scan job deleted")


@router.get("/zone-scanner/candidates", response_model=list[ZoneScanCandidateResponse])
async def list_zone_scan_candidates(
    job_id: int | None = None,
    include_ignored: bool = False,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ZoneScanCandidateResponse]:
    del admin
    query = select(ZoneScanCandidate)
    if job_id is not None:
        query = query.where(ZoneScanCandidate.job_id == job_id)
    if not include_ignored:
        query = query.where(ZoneScanCandidate.is_ignored.is_(False))
    query = query.order_by(ZoneScanCandidate.created_at.desc(), ZoneScanCandidate.id.desc()).limit(500)
    result = await db.execute(query)
    return [ZoneScanCandidateResponse.model_validate(candidate) for candidate in result.scalars().all()]


@router.post("/zone-scanner/candidates/{candidate_id}/add-to-discovery", response_model=DiscoveryDomainResponse)
async def add_zone_scan_candidate_to_discovery_endpoint(
    candidate_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> DiscoveryDomainResponse:
    del admin
    try:
        domain = await add_zone_scan_candidate_to_discovery(db, candidate_id=candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(domain)
    return DiscoveryDomainResponse.model_validate(domain)


@router.post("/zone-scanner/candidates/{candidate_id}/ignore", response_model=ZoneScanCandidateResponse)
async def ignore_zone_scan_candidate_endpoint(
    candidate_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ZoneScanCandidateResponse:
    del admin
    try:
        candidate = await ignore_zone_scan_candidate(db, candidate_id=candidate_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(candidate)
    return ZoneScanCandidateResponse.model_validate(candidate)


@router.get("/workers", response_model=list[WorkerNodeResponse])
async def list_workers(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[WorkerNodeResponse]:
    del admin
    result = await db.execute(select(WorkerNode).order_by(WorkerNode.name.asc()))
    return [WorkerNodeResponse.model_validate(worker) for worker in result.scalars().all()]


@router.get("/workers/{worker_id}/setup", response_model=WorkerSetupResponse)
async def get_worker_setup(
    worker_id: int,
    simulate_mode: bool = Query(default=True),
    runtime_base_url: str | None = Query(default=None, max_length=255),
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> WorkerSetupResponse:
    del admin
    worker = await db.get(WorkerNode, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    effective_runtime_base_url = (
        runtime_base_url
        or get_settings().worker_runtime_public_base_url
        or "http://CONTROL_SERVER_IP:8080"
    )
    if not worker.control_token:
        worker.control_token = generate_session_token()
        await db.commit()
        await db.refresh(worker)
    return _build_worker_setup_response(
        worker,
        runtime_base_url=effective_runtime_base_url,
        simulate_mode=simulate_mode,
    )


@router.get("/workers/maintenance-jobs", response_model=list[WorkerMaintenanceJobResponse])
async def list_worker_maintenance_jobs(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[WorkerMaintenanceJobResponse]:
    del admin
    result = await db.execute(select(WorkerMaintenanceJob).order_by(WorkerMaintenanceJob.id.desc()).limit(50))
    return [WorkerMaintenanceJobResponse.model_validate(job) for job in result.scalars().all()]


@router.post(
    "/workers/{worker_id}/maintenance/check",
    response_model=WorkerMaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_worker_ssh_check(
    worker_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> WorkerMaintenanceJobResponse:
    return await _start_worker_maintenance_job(
        worker_id=worker_id,
        action="check",
        background_tasks=background_tasks,
        db=db,
        admin=admin,
    )


@router.post(
    "/workers/{worker_id}/maintenance/update",
    response_model=WorkerMaintenanceJobResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_worker_update(
    worker_id: int,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> WorkerMaintenanceJobResponse:
    return await _start_worker_maintenance_job(
        worker_id=worker_id,
        action="update",
        background_tasks=background_tasks,
        db=db,
        admin=admin,
    )


async def _start_worker_maintenance_job(
    *,
    worker_id: int,
    action: str,
    background_tasks: BackgroundTasks,
    db: AsyncSession,
    admin: User,
) -> WorkerMaintenanceJobResponse:
    worker = await db.get(WorkerNode, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    if not worker.ssh_access_configured:
        raise HTTPException(status_code=400, detail="Worker SSH access is not configured")
    job = WorkerMaintenanceJob(worker_id=worker_id, action=action, status="queued")
    db.add(job)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action=f"worker_maintenance_{action}",
        details=f"worker_id={worker_id}",
    )
    await db.commit()
    await db.refresh(job)
    background_tasks.add_task(run_worker_maintenance_job, job.id)
    return WorkerMaintenanceJobResponse.model_validate(job)


@router.post("/workers", response_model=WorkerNodeResponse, status_code=status.HTTP_201_CREATED)
async def create_worker(
    payload: WorkerNodeCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> WorkerNodeResponse:
    data = payload.model_dump()
    data["control_token"] = data.get("control_token") or generate_session_token()
    worker = WorkerNode(**data)
    db.add(worker)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="worker_create",
        details=f"name={payload.name} target_rps={payload.target_rps}",
    )
    await db.commit()
    await sync_worker_runtime_allowlist(db, get_settings())
    await db.refresh(worker)
    return WorkerNodeResponse.model_validate(worker)


@router.patch("/workers/{worker_id}", response_model=WorkerNodeResponse)
async def update_worker(
    worker_id: int,
    payload: WorkerNodeUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> WorkerNodeResponse:
    worker = await db.get(WorkerNode, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(worker, field, value)
    worker.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="worker_update",
        details=f"worker_id={worker_id}",
    )
    await db.commit()
    await sync_worker_runtime_allowlist(db, get_settings())
    await db.refresh(worker)
    return WorkerNodeResponse.model_validate(worker)


@router.delete("/workers/{worker_id}", response_model=MessageResponse)
async def delete_worker(
    worker_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    worker = await db.get(WorkerNode, worker_id)
    if worker is None:
        raise HTTPException(status_code=404, detail="Worker not found")
    await db.delete(worker)
    await db.commit()
    await sync_worker_runtime_allowlist(db, get_settings())
    return MessageResponse(detail="Worker deleted")


@router.get("/registrar-accounts", response_model=list[RegistrarAccountResponse])
async def list_registrar_accounts(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[RegistrarAccountResponse]:
    del admin
    result = await db.execute(select(RegistrarAccount).order_by(RegistrarAccount.name.asc()))
    return [_serialize_registrar_account(account) for account in result.scalars().all()]


@router.post("/registrar-accounts", response_model=RegistrarAccountResponse, status_code=status.HTTP_201_CREATED)
async def create_registrar_account(
    payload: RegistrarAccountCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RegistrarAccountResponse:
    account = RegistrarAccount(**payload.model_dump())
    db.add(account)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="registrar_account_create",
        details=f"name={payload.name} registrar={payload.registrar_slug}",
    )
    await db.commit()
    await db.refresh(account)
    return _serialize_registrar_account(account)


@router.patch("/registrar-accounts/{account_id}", response_model=RegistrarAccountResponse)
async def update_registrar_account(
    account_id: int,
    payload: RegistrarAccountUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RegistrarAccountResponse:
    account = await db.get(RegistrarAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Registrar account not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(account, field, value)
    account.updated_at = utcnow()
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="registrar_account_update",
        details=f"account_id={account_id}",
    )
    await db.commit()
    await db.refresh(account)
    return _serialize_registrar_account(account)


@router.post("/registrar-accounts/{account_id}/validate", response_model=RegistrarAccountValidateResponse)
async def validate_registrar_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> RegistrarAccountValidateResponse:
    del admin
    account = await db.get(RegistrarAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Registrar account not found")
    errors: list[str] = []
    if not account.api_token:
        errors.append("missing api token")
    if account.registrar_slug == "gandi" and not account.default_contact_profile_id:
        errors.append("missing default contact profile")
    account.last_validated_at = utcnow()
    if errors:
        account.last_validation_status = "invalid"
        account.last_validation_message = ", ".join(errors)
    else:
        result = await validate_registrar_account_remote(account, get_settings())
        account.last_validation_status = result.status
        account.last_validation_message = result.message
    await db.commit()
    return RegistrarAccountValidateResponse(
        id=account.id,
        last_validation_status=account.last_validation_status,
        last_validation_message=account.last_validation_message,
        last_validated_at=account.last_validated_at,
    )


@router.post("/registrar-accounts/{account_id}/prefill-contact", response_model=ContactProfilePrefillResponse)
async def prefill_contact_from_registrar_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ContactProfilePrefillResponse:
    del admin
    account = await db.get(RegistrarAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Registrar account not found")
    if account.registrar_slug != "gandi":
        raise HTTPException(status_code=400, detail=f"Prefill is not implemented for {account.registrar_slug}")
    try:
        payload = await build_gandi_contact_prefill(account, get_settings())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Gandi prefill failed with HTTP {exc.response.status_code}",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Gandi prefill request failed: {exc}") from exc
    return ContactProfilePrefillResponse(**payload)


@router.delete("/registrar-accounts/{account_id}", response_model=MessageResponse)
async def delete_registrar_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    account = await db.get(RegistrarAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Registrar account not found")
    await db.delete(account)
    await db.commit()
    return MessageResponse(detail="Registrar account deleted")


@router.get("/contact-profiles", response_model=list[ContactProfileResponse])
async def list_contact_profiles(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[ContactProfileResponse]:
    del admin
    result = await db.execute(select(ContactProfile).order_by(ContactProfile.is_default.desc(), ContactProfile.label.asc()))
    return [ContactProfileResponse.model_validate(contact) for contact in result.scalars().all()]


@router.post("/contact-profiles", response_model=ContactProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_contact_profile(
    payload: ContactProfileCreateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ContactProfileResponse:
    contact = ContactProfile(**payload.model_dump())
    db.add(contact)
    await db.flush()
    if contact.is_default:
        await _enforce_single_default_contact(db, contact.id)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="contact_profile_create",
        details=f"label={payload.label}",
    )
    await db.commit()
    await db.refresh(contact)
    return ContactProfileResponse.model_validate(contact)


@router.patch("/contact-profiles/{contact_id}", response_model=ContactProfileResponse)
async def update_contact_profile(
    contact_id: int,
    payload: ContactProfileUpdateRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> ContactProfileResponse:
    contact = await db.get(ContactProfile, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact profile not found")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(contact, field, value)
    contact.updated_at = utcnow()
    if payload.is_default:
        await _enforce_single_default_contact(db, contact.id)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="contact_profile_update",
        details=f"contact_id={contact_id}",
    )
    await db.commit()
    await db.refresh(contact)
    return ContactProfileResponse.model_validate(contact)


@router.delete("/contact-profiles/{contact_id}", response_model=MessageResponse)
async def delete_contact_profile(
    contact_id: int,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    del admin
    contact = await db.get(ContactProfile, contact_id)
    if contact is None:
        raise HTTPException(status_code=404, detail="Contact profile not found")
    await db.delete(contact)
    await db.commit()
    return MessageResponse(detail="Contact profile deleted")


@router.get("/attacks", response_model=list[AttackRunResponse])
async def list_attack_runs(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AttackRunResponse]:
    del admin
    runs = (await db.execute(select(AttackRun).order_by(AttackRun.created_at.desc()))).scalars().all()
    domain_ids = [run.domain_id for run in runs]
    domains = []
    if domain_ids:
        domains = (
            await db.execute(select(DropDomain).where(DropDomain.id.in_(domain_ids)))
        ).scalars().all()
    runtime_snapshots = await _load_domain_runtime_snapshots(db, domains, now=utcnow())
    return [_serialize_attack_run_response(run, runtime_snapshots) for run in runs]


@router.get("/tasks", response_model=list[WorkerTaskResponse])
async def list_worker_tasks(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[WorkerTaskResponse]:
    del admin
    result = await db.execute(select(WorkerTask).order_by(WorkerTask.created_at.desc()).limit(500))
    return [WorkerTaskResponse.model_validate(task) for task in result.scalars().all()]


@router.get("/events", response_model=list[AttackEventResponse])
async def list_attack_events(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AttackEventResponse]:
    del admin
    result = await db.execute(select(AttackEvent).order_by(AttackEvent.created_at.desc()).limit(500))
    return [AttackEventResponse.model_validate(event) for event in result.scalars().all()]


@router.post("/attacks/start", response_model=list[AttackRunResponse])
async def start_attacks(
    payload: AttackStartRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AttackRunResponse]:
    now = utcnow()
    query = select(DropDomain).where(DropDomain.attack_enabled.is_(True))
    if payload.domain_ids:
        query = query.where(DropDomain.id.in_(payload.domain_ids))
    domains = (
        await db.execute(query.order_by(DropDomain.priority.desc(), DropDomain.drop_date.asc(), DropDomain.fqdn.asc()))
    ).scalars().all()
    if not payload.domain_ids:
        domains = [domain for domain in domains if is_domain_due_today(domain, now)]
    if not domains:
        raise HTTPException(status_code=400, detail="No domains selected for attack planning")

    workers = (
        await db.execute(
            select(WorkerNode)
            .where(WorkerNode.is_enabled.is_(True))
            .order_by(WorkerNode.target_rps.desc(), WorkerNode.max_rps.desc(), WorkerNode.name.asc())
        )
    ).scalars().all()
    if not workers:
        raise HTTPException(status_code=400, detail="No enabled workers configured")

    if not payload.force_rebuild:
        existing_domain_ids = set(
            (
                await db.execute(
                    select(AttackRun.domain_id).where(
                        AttackRun.domain_id.in_([domain.id for domain in domains]),
                        AttackRun.status.in_(["planned", "running"]),
                    )
                )
            ).scalars().all()
        )
        if existing_domain_ids and len(existing_domain_ids) == len(domains):
            raise HTTPException(status_code=400, detail="Selected domains already have active attack runs")

    created_runs = await plan_attack_runs(
        db,
        domains=domains,
        workers=workers,
        now=now,
        force_rebuild=payload.force_rebuild,
    )

    await recompute_worker_domain_counts(db)
    await recompute_run_statistics(db)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="attack_start",
        details=f"domains={len(domains)} created_runs={len(created_runs)}",
    )
    await db.commit()
    for run in created_runs:
        await db.refresh(run)
    return [AttackRunResponse.model_validate(run) for run in created_runs]


@router.post("/attacks/simulate-registration", response_model=list[AttackRunResponse])
async def simulate_registration_attack(
    payload: AttackRegistrationSimulationRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[AttackRunResponse]:
    now = utcnow()
    domain_ids = [domain_id for domain_id in payload.domain_ids if domain_id > 0]
    if not domain_ids:
        raise HTTPException(status_code=400, detail="No domains selected for registration simulation")

    domains = (
        await db.execute(
            select(DropDomain)
            .where(DropDomain.id.in_(domain_ids), DropDomain.attack_enabled.is_(True))
            .order_by(DropDomain.priority.desc(), DropDomain.drop_date.asc(), DropDomain.fqdn.asc())
        )
    ).scalars().all()
    if not domains:
        raise HTTPException(status_code=400, detail="No enabled domains selected for registration simulation")

    workers = (
        await db.execute(
            select(WorkerNode)
            .where(WorkerNode.is_enabled.is_(True))
            .order_by(WorkerNode.target_rps.desc(), WorkerNode.max_rps.desc(), WorkerNode.name.asc())
        )
    ).scalars().all()
    if not workers:
        raise HTTPException(status_code=400, detail="No enabled workers configured")

    created_runs = await plan_immediate_registration_runs(
        db,
        domains=domains,
        workers=workers,
        now=now,
        duration_seconds=payload.duration_seconds,
        force_rebuild=payload.force_rebuild,
    )
    await recompute_worker_domain_counts(db)
    await recompute_run_statistics(db)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="attack_simulate_registration",
        details=f"domains={len(domains)} created_runs={len(created_runs)} duration={payload.duration_seconds}",
    )
    await db.commit()
    for run in created_runs:
        await db.refresh(run)
    return [AttackRunResponse.model_validate(run) for run in created_runs]


@router.post("/attacks/stop", response_model=MessageResponse)
async def stop_attacks(
    payload: AttackStopRequest,
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    now = utcnow()
    query = select(AttackRun).where(AttackRun.status.in_(["planned", "running"]))
    if payload.domain_ids:
        query = query.where(AttackRun.domain_id.in_(payload.domain_ids))
    runs = (await db.execute(query)).scalars().all()
    if not runs:
        raise HTTPException(status_code=404, detail="No active attack runs found")

    affected_domain_ids = [run.domain_id for run in runs]
    tasks = (
        await db.execute(
            select(WorkerTask).where(
                WorkerTask.domain_id.in_(affected_domain_ids),
                WorkerTask.status.in_(["queued", "planned", "running"]),
            )
        )
    ).scalars().all()
    for run in runs:
        run.status = "stopped"
        run.finished_at = now
        run.stop_reason = payload.reason or "Stopped from control panel"
    for task in tasks:
        task.status = "cancelled"
        task.finished_at = now
        task.stop_reason = payload.reason or "Stopped from control panel"

    domains = (await db.execute(select(DropDomain).where(DropDomain.id.in_(affected_domain_ids)))).scalars().all()
    for domain in domains:
        domain.status = "queued" if domain.attack_enabled else "paused"
        domain.updated_at = now
        db.add(
            AttackEvent(
                domain_id=domain.id,
                level="warning",
                event_type="attack_stopped",
                message=payload.reason or "Stopped from control panel",
            )
        )

    await recompute_worker_domain_counts(db)
    await rebalance_worker_pool(db)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="attack_stop",
        details=f"runs={len(runs)}",
    )
    await db.commit()
    return MessageResponse(detail=f"Stopped {len(runs)} attack runs")


@router.post("/attacks/rebalance", response_model=MessageResponse)
async def rebalance_attacks(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> MessageResponse:
    created = await rebalance_worker_pool(db)
    await recompute_worker_domain_counts(db)
    await recompute_run_statistics(db)
    await add_audit_log(
        db,
        actor_user_id=admin.id,
        target_user_id=None,
        action="attack_rebalance",
        details=f"created_tasks={created}",
    )
    await db.commit()
    return MessageResponse(detail=f"Rebalanced {created} worker tasks")
