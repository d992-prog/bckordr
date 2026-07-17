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
    second: int = Field(default=30, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int = Field(default=95, ge=1, le=86400)
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
    second: int = Field(default=30, ge=0, le=59)
    weekdays: str | None = Field(default=None, max_length=64)
    specific_date: date | None = None
    window_duration_seconds: int = Field(default=95, ge=1, le=86400)
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
    runtime_mode: str = "unknown"
    registration_concurrency_multiplier: float = Field(default=2.0, ge=1.0)
    registration_max_concurrency: int = Field(default=64, ge=1)
    current_domain_count: int = Field(default=0, ge=0)
    ssh_host: str | None = Field(default=None, max_length=64)
    ssh_port: int = Field(default=22, ge=1, le=65535)
    ssh_username: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=255)
    ssh_last_check_status: str | None = Field(default=None, max_length=32)
    ssh_last_check_message: str | None = None
    ssh_last_checked_at: datetime | None = None


class WorkerNodeCreateRequest(WorkerNodeBase):
    ssh_password: str | None = Field(default=None, max_length=4096)


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
    runtime_mode: str | None = None
    registration_concurrency_multiplier: float | None = Field(default=None, ge=1.0)
    registration_max_concurrency: int | None = Field(default=None, ge=1)
    current_domain_count: int | None = Field(default=None, ge=0)
    ssh_host: str | None = Field(default=None, max_length=64)
    ssh_port: int | None = Field(default=None, ge=1, le=65535)
    ssh_username: str | None = Field(default=None, max_length=64)
    ssh_password: str | None = Field(default=None, max_length=4096)
    ssh_key_path: str | None = Field(default=None, max_length=255)
    ssh_last_check_status: str | None = Field(default=None, max_length=32)
    ssh_last_check_message: str | None = None
    ssh_last_checked_at: datetime | None = None


class WorkerNodeResponse(WorkerNodeBase):
    id: int
    ssh_access_configured: bool = False
    last_seen_at: datetime | None
    last_heartbeat_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class WorkerSetupResponse(BaseModel):
    worker_id: int
    worker_name: str
    runtime_base_url: str
    mode: str
    simulate_mode: bool
    env_file: str
    write_env_command: str
    full_install_commands: list[str]
    update_existing_commands: list[str]
    switch_to_test_commands: list[str]
    switch_to_live_commands: list[str]
    verify_commands: list[str]


class WorkerMaintenanceJobResponse(BaseModel):
    id: int
    worker_id: int
    action: str
    status: str
    log: str | None
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
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
    auto_start_enabled: bool = False
    auto_start_lead_seconds: int = Field(default=90, ge=0, le=3600)
    override_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    window_start_minute: int = Field(default=31, ge=0, le=59)
    window_start_second: int = Field(default=30, ge=0, le=59)
    window_duration_seconds: int = Field(default=95, ge=1, le=3600)
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
    auto_start_enabled: bool = False
    auto_start_lead_seconds: int = Field(default=90, ge=0, le=3600)
    override_min_guaranteed_rps: float | None = Field(default=None, ge=0.0)
    window_start_minute: int = Field(default=31, ge=0, le=59)
    window_start_second: int = Field(default=30, ge=0, le=59)
    window_duration_seconds: int = Field(default=95, ge=1, le=3600)
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
    auto_start_enabled: bool | None = None
    auto_start_lead_seconds: int | None = Field(default=None, ge=0, le=3600)
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
    runtime_window_start_at: datetime | None = None
    runtime_window_end_at: datetime | None = None
    effective_window_start_minute: int | None = None
    effective_window_start_second: int | None = None
    effective_window_duration_seconds: int | None = None
    effective_window_source: str = "domain"
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
    drop_prediction_enabled: bool = True
    notes: str | None = None


class DiscoveryDomainBulkCreateRequest(BaseModel):
    domains: list[str] = Field(min_length=1)
    zone: str | None = Field(default=None, min_length=2, max_length=32)
    check_interval_seconds: int = Field(default=21600, ge=10, le=86400)
    source_mode: str = Field(default="rdap", min_length=2, max_length=32)
    drop_prediction_enabled: bool = True
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
    drop_prediction_enabled: bool
    last_lifecycle_stage: str | None
    last_status_codes: str | None
    last_availability: str | None
    last_checked_at: datetime | None
    next_check_at: datetime | None
    first_seen_redemption_at: datetime | None
    last_seen_redemption_at: datetime | None
    redemption_anchor_at: datetime | None
    redemption_anchor_source: str | None
    predicted_pending_delete_at: datetime | None
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


class AllZonefilesSettingsResponse(BaseModel):
    configured: bool
    base_url: str


class AllZonefilesSettingsUpdateRequest(BaseModel):
    api_token: str | None = Field(default=None, max_length=512)


class AllZonefilesTestResponse(BaseModel):
    ok: bool
    message: str
    zones_count: int | None = None


class ZoneScanJobCreateRequest(BaseModel):
    zone: str = Field(min_length=2, max_length=32)
    source_type: str = Field(default="zone_latest", pattern="^(zone_latest|zone_historic|expired_latest|expired_historic)$")
    source_date: date | None = None
    min_score: int = Field(default=35, ge=0, le=100)
    limit_output: int = Field(default=20, ge=1, le=500)
    max_rdap_checks: int = Field(default=300000, ge=1, le=2000000)
    concurrency: int = Field(default=100, ge=1, le=500)
    rdap_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)
    pending_delete_min_days: float | None = Field(default=1.0, ge=-30.0, le=365.0)
    pending_delete_max_days: float | None = Field(default=2.0, ge=-30.0, le=365.0)
    reservoir_size: int = Field(default=300000, ge=1, le=2000000)
    random_seed: int = Field(default=42, ge=0)
    keep_file: bool = True


class ZoneScanJobResponse(BaseModel):
    id: int
    zone: str
    source_type: str
    source_date: date | None
    status: str
    file_name: str | None
    file_path: str | None
    download_url: str | None
    file_size_bytes: int | None
    downloaded_bytes: int
    scanned_lines: int
    parsed_domains: int
    filtered_candidates: int
    submitted_rdap: int
    completed_rdap: int
    found_candidates: int
    error_count: int
    min_score: int
    limit_output: int
    max_rdap_checks: int
    concurrency: int
    rdap_timeout_seconds: float
    pending_delete_min_days: float | None
    pending_delete_max_days: float | None
    reservoir_size: int
    random_seed: int
    keep_file: bool
    started_at: datetime | None
    finished_at: datetime | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ZoneScanCandidateResponse(BaseModel):
    id: int
    job_id: int
    fqdn: str
    zone: str
    lifecycle_stage: str
    status_codes: str | None
    http_status: int | None
    checked_at: datetime
    redemption_anchor_at: datetime | None
    predicted_pending_delete_at: datetime | None
    days_to_pending_delete: float | None
    score: int
    reason: str | None
    error: str | None
    discovery_domain_id: int | None
    is_ignored: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AttackStartRequest(BaseModel):
    domain_ids: list[int] | None = None
    force_rebuild: bool = False


class AttackRegistrationSimulationRequest(BaseModel):
    domain_ids: list[int]
    duration_seconds: int = 95
    force_rebuild: bool = True


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
    response_status_counts: dict[str, int] | None = None
    response_error_counts: dict[str, int] | None = None
    response_samples: dict | None = None
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
