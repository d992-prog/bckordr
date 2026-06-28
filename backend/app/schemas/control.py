from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class CapacitySummaryResponse(BaseModel):
    current_rps: float
    target_rps: float
    max_rps: float
    enabled_workers: int
    online_workers: int


class ControlOverviewResponse(BaseModel):
    checked_at: datetime
    total_domains: int
    due_today_domains: int
    active_attack_domains: int
    success_today_domains: int
    scheduled_runs: int
    running_runs: int
    total_accounts: int
    total_contacts: int
    capacity: CapacitySummaryResponse


class ZoneStrategyBase(BaseModel):
    zone: str = Field(min_length=2, max_length=32)
    name: str = Field(min_length=2, max_length=128)
    timezone_name: str = Field(min_length=3, max_length=64)
    rule_resolution_mode: str = "priority"
    default_min_guaranteed_rps: float = Field(default=1.0, ge=0.0)
    default_registrar_slug: str = Field(default="gandi", min_length=2, max_length=64)
    is_active: bool = True
    notes: str | None = None


class ZoneStrategyCreateRequest(ZoneStrategyBase):
    pass


class ZoneStrategyUpdateRequest(BaseModel):
    zone: str | None = Field(default=None, min_length=2, max_length=32)
    name: str | None = Field(default=None, min_length=2, max_length=128)
    timezone_name: str | None = Field(default=None, min_length=3, max_length=64)
    rule_resolution_mode: str | None = None
    default_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    default_registrar_slug: str | None = Field(default=None, min_length=2, max_length=64)
    is_active: bool | None = None
    notes: str | None = None


class ZoneStrategyResponse(ZoneStrategyBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ZoneRuleBase(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    is_enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    schedule_type: str = "hourly"
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int = Field(default=31, ge=0, le=59)
    second: int = Field(default=59, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int = Field(default=61, ge=1, le=86400)
    execution_profile_mode: str = "flat"
    notes: str | None = None


class ZoneRuleCreateRequest(ZoneRuleBase):
    pass


class ZoneRuleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    is_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    schedule_type: str | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    second: int | None = Field(default=None, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    execution_profile_mode: str | None = None
    notes: str | None = None


class ZoneRuleResponse(ZoneRuleBase):
    id: int
    zone_strategy_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ZoneRulePhaseBase(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    sort_order: int = Field(default=0, ge=0, le=1000)
    start_offset_seconds: int = Field(default=0, ge=0, le=86400)
    duration_seconds: int = Field(default=0, ge=0, le=86400)
    rps_mode: str = "percent"
    rps_value: float = Field(default=100.0, ge=0.0)
    stop_on_success: bool = True


class ZoneRulePhaseCreateRequest(ZoneRulePhaseBase):
    pass


class ZoneRulePhaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=64)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    start_offset_seconds: int | None = Field(default=None, ge=0, le=86400)
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)
    rps_mode: str | None = None
    rps_value: float | None = Field(default=None, ge=0.0)
    stop_on_success: bool | None = None


class ZoneRulePhaseResponse(ZoneRulePhaseBase):
    id: int
    zone_rule_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DomainOverrideSettingsBase(BaseModel):
    timezone_name: str = Field(min_length=3, max_length=64)
    rule_resolution_mode: str = "priority"
    default_min_guaranteed_rps: float = Field(default=1.0, ge=0.0)
    notes: str | None = None


class DomainOverrideSettingsCreateRequest(DomainOverrideSettingsBase):
    pass


class DomainOverrideSettingsUpdateRequest(BaseModel):
    timezone_name: str | None = Field(default=None, min_length=3, max_length=64)
    rule_resolution_mode: str | None = None
    default_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    notes: str | None = None


class DomainOverrideSettingsResponse(DomainOverrideSettingsBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DomainOverrideRuleBase(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    is_enabled: bool = True
    priority: int = Field(default=100, ge=0, le=1000)
    schedule_type: str = "hourly"
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int = Field(default=31, ge=0, le=59)
    second: int = Field(default=59, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int = Field(default=61, ge=1, le=86400)
    execution_profile_mode: str = "flat"
    notes: str | None = None


class DomainOverrideRuleCreateRequest(DomainOverrideRuleBase):
    pass


class DomainOverrideRuleUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    is_enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=1000)
    schedule_type: str | None = None
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    second: int | None = Field(default=None, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int | None = Field(default=None, ge=1, le=86400)
    execution_profile_mode: str | None = None
    notes: str | None = None


class DomainOverrideRuleResponse(DomainOverrideRuleBase):
    id: int
    domain_rule_override_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DomainOverrideRulePhaseBase(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    sort_order: int = Field(default=0, ge=0, le=1000)
    start_offset_seconds: int = Field(default=0, ge=0, le=86400)
    duration_seconds: int = Field(default=0, ge=0, le=86400)
    rps_mode: str = "percent"
    rps_value: float = Field(default=100.0, ge=0.0)
    stop_on_success: bool = True


class DomainOverrideRulePhaseCreateRequest(DomainOverrideRulePhaseBase):
    pass


class DomainOverrideRulePhaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=64)
    sort_order: int | None = Field(default=None, ge=0, le=1000)
    start_offset_seconds: int | None = Field(default=None, ge=0, le=86400)
    duration_seconds: int | None = Field(default=None, ge=0, le=86400)
    rps_mode: str | None = None
    rps_value: float | None = Field(default=None, ge=0.0)
    stop_on_success: bool | None = None


class DomainOverrideRulePhaseResponse(DomainOverrideRulePhaseBase):
    id: int
    domain_override_rule_id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StrategyPreviewWindowResponse(BaseModel):
    rule_id: int
    priority: int
    start_at: datetime
    end_at: datetime
    rule_name: str | None


class StrategyPreviewResponse(BaseModel):
    strategy_id: int | None
    timezone_name: str
    resolution_mode: str
    target_date: date
    windows: list[StrategyPreviewWindowResponse]


class ContactProfileBase(BaseModel):
    label: str = Field(min_length=2, max_length=128)
    person_type: str = "individual"
    given_name: str = Field(min_length=1, max_length=128)
    family_name: str = Field(min_length=1, max_length=128)
    organization_name: str | None = Field(default=None, max_length=255)
    email: str = Field(min_length=3, max_length=255)
    phone: str = Field(min_length=3, max_length=64)
    mobile: str | None = Field(default=None, max_length=64)
    fax: str | None = Field(default=None, max_length=64)
    lang: str | None = Field(default=None, max_length=16)
    street_address: str = Field(min_length=3, max_length=255)
    city: str = Field(min_length=2, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    zip_code: str = Field(min_length=2, max_length=32)
    country_code: str = Field(min_length=2, max_length=8)
    data_obfuscated: bool | None = None
    mail_obfuscated: bool | None = None
    icann_contract_accept: bool | None = None
    extra_parameters: str | None = None
    is_default: bool = False
    notes: str | None = None


class ContactProfileCreateRequest(ContactProfileBase):
    pass


class ContactProfileUpdateRequest(BaseModel):
    label: str | None = Field(default=None, min_length=2, max_length=128)
    person_type: str | None = None
    given_name: str | None = Field(default=None, min_length=1, max_length=128)
    family_name: str | None = Field(default=None, min_length=1, max_length=128)
    organization_name: str | None = Field(default=None, max_length=255)
    email: str | None = Field(default=None, min_length=3, max_length=255)
    phone: str | None = Field(default=None, min_length=3, max_length=64)
    mobile: str | None = Field(default=None, max_length=64)
    fax: str | None = Field(default=None, max_length=64)
    lang: str | None = Field(default=None, max_length=16)
    street_address: str | None = Field(default=None, min_length=3, max_length=255)
    city: str | None = Field(default=None, min_length=2, max_length=128)
    state: str | None = Field(default=None, max_length=128)
    zip_code: str | None = Field(default=None, min_length=2, max_length=32)
    country_code: str | None = Field(default=None, min_length=2, max_length=8)
    data_obfuscated: bool | None = None
    mail_obfuscated: bool | None = None
    icann_contract_accept: bool | None = None
    extra_parameters: str | None = None
    is_default: bool | None = None
    notes: str | None = None


class ContactProfileResponse(ContactProfileBase):
    id: int
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ContactProfilePrefillResponse(BaseModel):
    label: str
    person_type: str = "individual"
    given_name: str = ""
    family_name: str = ""
    organization_name: str | None = None
    email: str = ""
    phone: str = ""
    mobile: str | None = None
    fax: str | None = None
    lang: str | None = None
    street_address: str = ""
    city: str = ""
    state: str | None = None
    zip_code: str = ""
    country_code: str = "FR"
    data_obfuscated: bool | None = None
    mail_obfuscated: bool | None = None
    icann_contract_accept: bool | None = None
    extra_parameters: str | None = None
    is_default: bool = False
    notes: str | None = None


class RegistrarAccountBase(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    registrar_slug: str = "gandi"
    api_token: str | None = None
    api_base_url: str | None = Field(default=None, max_length=255)
    sharing_id: str | None = Field(default=None, max_length=128)
    default_contact_profile_id: int | None = None
    is_active: bool = True
    supports_dry_run: bool = True
    notes: str | None = None


class RegistrarAccountCreateRequest(RegistrarAccountBase):
    pass


class RegistrarAccountUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    registrar_slug: str | None = None
    api_token: str | None = None
    api_base_url: str | None = Field(default=None, max_length=255)
    sharing_id: str | None = Field(default=None, max_length=128)
    default_contact_profile_id: int | None = None
    is_active: bool | None = None
    supports_dry_run: bool | None = None
    notes: str | None = None


class RegistrarAccountValidateResponse(BaseModel):
    id: int
    last_validation_status: str
    last_validation_message: str | None
    last_validated_at: datetime | None


class RegistrarAccountResponse(RegistrarAccountBase):
    id: int
    last_validation_status: str
    last_validation_message: str | None
    last_validated_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkerNodeBase(BaseModel):
    name: str = Field(min_length=2, max_length=128)
    registrar_slug: str = "gandi"
    assigned_registrar_account_id: int | None = None
    api_base_url: str | None = Field(default=None, max_length=255)
    control_token: str | None = Field(default=None, max_length=255)
    status: str = "provisioning"
    is_enabled: bool = True
    ip_address: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=128)
    notes: str | None = None
    max_rps: float = Field(default=16.0, ge=0.1)
    target_rps: float = Field(default=16.0, ge=0.1)
    current_rps: float = Field(default=0.0, ge=0.0)
    current_capacity_rps: float = Field(default=0.0, ge=0.0)
    cpu_load: float = Field(default=0.0, ge=0.0)
    ram_usage_percent: float = Field(default=0.0, ge=0.0)
    clock_drift_ms: int = 0
    current_domain_count: int = Field(default=0, ge=0)


class WorkerNodeCreateRequest(WorkerNodeBase):
    pass


class WorkerNodeUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=128)
    registrar_slug: str | None = None
    assigned_registrar_account_id: int | None = None
    api_base_url: str | None = Field(default=None, max_length=255)
    control_token: str | None = Field(default=None, max_length=255)
    status: str | None = None
    is_enabled: bool | None = None
    ip_address: str | None = Field(default=None, max_length=64)
    region: str | None = Field(default=None, max_length=128)
    notes: str | None = None
    max_rps: float | None = Field(default=None, ge=0.1)
    target_rps: float | None = Field(default=None, ge=0.1)
    current_rps: float | None = Field(default=None, ge=0.0)
    current_capacity_rps: float | None = Field(default=None, ge=0.0)
    cpu_load: float | None = Field(default=None, ge=0.0)
    ram_usage_percent: float | None = Field(default=None, ge=0.0)
    clock_drift_ms: int | None = None
    current_domain_count: int | None = Field(default=None, ge=0)


class WorkerNodeResponse(WorkerNodeBase):
    id: int
    last_seen_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DropDomainBase(BaseModel):
    fqdn: str = Field(min_length=3, max_length=255)
    zone: str = Field(default="fr", min_length=2, max_length=32)
    timezone_name: str = Field(default="Europe/Paris", min_length=3, max_length=64)
    registrar_slug: str = Field(default="gandi", min_length=2, max_length=64)
    zone_strategy_id: int | None = None
    strategy_mode: str = "inherit_zone"
    registrar_account_id: int | None = None
    contact_profile_id: int | None = None
    drop_date: date
    priority: int = Field(default=100, ge=1, le=1000)
    requested_duration_years: int = Field(default=1, ge=1, le=10)
    registration_extra_parameters: str | None = None
    attack_enabled: bool = True
    override_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    window_start_minute: int = Field(default=31, ge=0, le=59)
    window_start_second: int = Field(default=59, ge=0, le=59)
    window_duration_seconds: int = Field(default=61, ge=1, le=3600)
    notes: str | None = None


class DropDomainCreateRequest(DropDomainBase):
    pass


class DropDomainBulkCreateRequest(BaseModel):
    domains: list[str] = Field(min_length=1)
    zone: str = Field(default="fr", min_length=2, max_length=32)
    timezone_name: str = Field(default="Europe/Paris", min_length=3, max_length=64)
    registrar_slug: str = Field(default="gandi", min_length=2, max_length=64)
    zone_strategy_id: int | None = None
    strategy_mode: str = "inherit_zone"
    registrar_account_id: int | None = None
    contact_profile_id: int | None = None
    drop_date: date
    priority: int = Field(default=100, ge=1, le=1000)
    requested_duration_years: int = Field(default=1, ge=1, le=10)
    registration_extra_parameters: str | None = None
    attack_enabled: bool = True
    override_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    window_start_minute: int = Field(default=31, ge=0, le=59)
    window_start_second: int = Field(default=59, ge=0, le=59)
    window_duration_seconds: int = Field(default=61, ge=1, le=3600)
    notes: str | None = None


class DropDomainUpdateRequest(BaseModel):
    fqdn: str | None = Field(default=None, min_length=3, max_length=255)
    zone: str | None = Field(default=None, min_length=2, max_length=32)
    timezone_name: str | None = Field(default=None, min_length=3, max_length=64)
    registrar_slug: str | None = Field(default=None, min_length=2, max_length=64)
    zone_strategy_id: int | None = None
    strategy_mode: str | None = None
    registrar_account_id: int | None = None
    contact_profile_id: int | None = None
    drop_date: date | None = None
    priority: int | None = Field(default=None, ge=1, le=1000)
    requested_duration_years: int | None = Field(default=None, ge=1, le=10)
    registration_extra_parameters: str | None = None
    attack_enabled: bool | None = None
    override_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    window_start_minute: int | None = Field(default=None, ge=0, le=59)
    window_start_second: int | None = Field(default=None, ge=0, le=59)
    window_duration_seconds: int | None = Field(default=None, ge=1, le=3600)
    notes: str | None = None
    status: str | None = None
    success_message: str | None = None


class DropDomainResponse(DropDomainBase):
    id: int
    status: str
    readiness_reasons: str | None
    runtime_minimum_rps: float | None = None
    runtime_desired_rps: float | None = None
    runtime_allocated_rps: float | None = None
    runtime_assigned_worker_count: int = 0
    runtime_phase_name: str | None = None
    runtime_attack_run_id: int | None = None
    runtime_attack_status: str | None = None
    dry_run_checked_at: datetime | None
    dry_run_status: str | None = None
    dry_run_http_status: int | None = None
    dry_run_message: str | None = None
    success_at: datetime | None
    success_worker_id: int | None
    success_response_code: int | None
    success_message: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DomainImportResponse(BaseModel):
    inserted: list[DropDomainResponse]
    skipped: list[str]


class DomainDryRunResponse(BaseModel):
    domain_id: int
    status: str
    http_status: int | None
    message: str
    checked_at: datetime


class DomainDryRunBatchRequest(BaseModel):
    domain_ids: list[int] | None = None
    due_today_only: bool = False
    only_ready: bool = True


class DomainDryRunBatchResponse(BaseModel):
    total: int
    ready: int
    invalid: int
    error: int
    results: list[DomainDryRunResponse]


class DiscoveryDomainCreateRequest(BaseModel):
    fqdn: str = Field(min_length=3, max_length=255)
    zone: str | None = Field(default=None, min_length=2, max_length=32)
    check_interval_seconds: int = Field(default=21600, ge=10, le=86400)
    source_mode: str = Field(default="rdap", min_length=2, max_length=32)
    notes: str | None = None


class DiscoveryDomainBulkCreateRequest(BaseModel):
    domains: list[str] = Field(min_length=1)
    zone: str | None = Field(default=None, min_length=2, max_length=32)
    check_interval_seconds: int = Field(default=21600, ge=10, le=86400)
    source_mode: str = Field(default="rdap", min_length=2, max_length=32)
    notes: str | None = None


class DiscoveryObservationCreateRequest(BaseModel):
    source: str = Field(default="manual", min_length=2, max_length=32)
    http_status: int | None = Field(default=None, ge=100, le=599)
    lifecycle_stage: str | None = Field(default=None, max_length=32)
    availability_status: str | None = Field(default=None, max_length=32)
    status_codes: list[str] = Field(default_factory=list)
    raw_response: str | None = None
    error: str | None = None


class DiscoveryDomainResponse(BaseModel):
    id: int
    fqdn: str
    zone: str
    status: str
    is_enabled: bool
    check_interval_seconds: int
    source_mode: str
    last_lifecycle_stage: str | None
    last_status_codes: str | None
    last_availability: str | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    first_seen_redemption_at: datetime | None
    last_seen_redemption_at: datetime | None
    pending_delete_previous_seen_at: datetime | None
    first_seen_pending_delete_at: datetime | None
    last_seen_pending_delete_at: datetime | None
    predicted_drop_start_at: datetime | None
    predicted_drop_end_at: datetime | None
    available_first_seen_at: datetime | None
    last_error: str | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscoveryDomainImportResponse(BaseModel):
    inserted: list[DiscoveryDomainResponse]
    skipped: list[str]


class DiscoveryObservationResponse(BaseModel):
    id: int
    discovery_domain_id: int
    source: str
    observed_at: datetime
    http_status: int | None
    latency_ms: int | None
    lifecycle_stage: str | None
    availability_status: str | None
    status_codes: str | None
    raw_response: str | None
    error: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class DiscoveryZoneStatsResponse(BaseModel):
    zone: str
    total: int
    pending_delete: int
    available: int
    predicted: int


class AttackStartRequest(BaseModel):
    domain_ids: list[int] | None = None
    force_rebuild: bool = False


class AttackStopRequest(BaseModel):
    domain_ids: list[int] | None = None
    reason: str | None = None


class AttackRunResponse(BaseModel):
    id: int
    domain_id: int
    status: str
    planned_start_at: datetime
    planned_end_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    assigned_worker_count: int
    planned_rps: float
    current_rps: float
    max_rps: float
    runtime_minimum_rps: float | None = None
    runtime_desired_rps: float | None = None
    runtime_allocated_rps: float | None = None
    runtime_phase_name: str | None = None
    success_worker_id: int | None
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkerTaskResponse(BaseModel):
    id: int
    attack_run_id: int
    domain_id: int
    worker_id: int
    status: str
    planned_rps: float
    actual_rps: float
    total_attempts: int
    success_attempts: int
    latency_ms: float | None
    last_http_status: int | None
    last_error: str | None
    assigned_at: datetime | None
    acknowledged_at: datetime | None
    started_at: datetime | None
    finished_at: datetime | None
    stop_reason: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AttackEventResponse(BaseModel):
    id: int
    attack_run_id: int | None
    domain_id: int | None
    worker_id: int | None
    level: str
    event_type: str
    message: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
