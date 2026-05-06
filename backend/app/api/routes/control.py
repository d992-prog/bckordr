from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlalchemy import func, select
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
    DropDomain,
    RegistrarAccount,
    User,
    WorkerNode,
    WorkerTask,
    ZoneRule,
    ZoneRulePhase,
    ZoneStrategy,
)
from app.db.session import get_db
from app.schemas.common import MessageResponse
from app.schemas.control import (
    AttackEventResponse,
    AttackRunResponse,
    AttackStartRequest,
    AttackStopRequest,
    ContactProfileCreateRequest,
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
    WorkerNodeResponse,
    WorkerNodeUpdateRequest,
    WorkerTaskResponse,
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
    plan_attack_runs,
    rebalance_worker_pool,
    recompute_worker_domain_counts,
    recompute_run_statistics,
)
from app.services.audit import add_audit_log
from app.services.domain_parser import normalize_domain, parse_upload
from app.services.gandi_dry_run import GandiDryRunResult, run_gandi_domain_dry_run
from app.services.strategy_runtime import (
    evaluate_domain_readiness,
    is_domain_due_today,
    preview_strategy_windows,
    resolve_effective_strategy,
)
from app.services.security import generate_session_token
from app.services.registrars import validate_registrar_account_remote
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
        rules = []
        phases = []
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
    override_min_guaranteed_rps: float | None = Form(default=None),
    window_start_minute: int = Form(default=31),
    window_start_second: int = Form(default=59),
    window_duration_seconds: int = Form(default=61),
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


@router.get("/workers", response_model=list[WorkerNodeResponse])
async def list_workers(
    db: AsyncSession = Depends(get_db),
    admin: User = Depends(require_admin),
) -> list[WorkerNodeResponse]:
    del admin
    result = await db.execute(select(WorkerNode).order_by(WorkerNode.name.asc()))
    return [WorkerNodeResponse.model_validate(worker) for worker in result.scalars().all()]


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
