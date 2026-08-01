export type User = {
  id: number;
  username: string;
  role: string;
  status: string;
  language: string;
  max_domains: number | null;
  access_expires_at: string | null;
  status_message: string | null;
  telegram_token: string | null;
  telegram_chat_id: string | null;
  last_login_at: string | null;
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type SessionResponse = {
  user: User;
  has_feature_access: boolean;
};

export type DiagnosticTelegramSettings = {
  telegram_token: string | null;
  telegram_chat_id: string | null;
};

export type CapacitySummary = {
  current_rps: number;
  target_rps: number;
  max_rps: number;
  enabled_workers: number;
  online_workers: number;
};

export type Overview = {
  checked_at: string;
  total_domains: number;
  due_today_domains: number;
  active_attack_domains: number;
  success_today_domains: number;
  scheduled_runs: number;
  running_runs: number;
  total_accounts: number;
  total_contacts: number;
  capacity: CapacitySummary;
};

export type DropDomain = {
  id: number;
  fqdn: string;
  zone: string;
  timezone_name: string;
  registrar_slug: string;
  zone_strategy_id: number | null;
  strategy_mode: string;
  registrar_account_id: number | null;
  contact_profile_id: number | null;
  drop_date: string;
  priority: number;
  requested_duration_years: number;
  registration_extra_parameters: string | null;
  status: string;
  attack_enabled: boolean;
  auto_start_enabled: boolean;
  auto_start_lead_seconds: number;
  override_min_guaranteed_rps: number | null;
  readiness_reasons: string | null;
  runtime_minimum_rps: number | null;
  runtime_desired_rps: number | null;
  runtime_allocated_rps: number | null;
  runtime_assigned_worker_count: number;
  runtime_phase_name: string | null;
  runtime_attack_run_id: number | null;
  runtime_attack_status: string | null;
  runtime_window_start_at: string | null;
  runtime_window_end_at: string | null;
  effective_window_start_minute: number | null;
  effective_window_start_second: number | null;
  effective_window_duration_seconds: number | null;
  effective_window_source: string;
  window_start_minute: number;
  window_start_second: number;
  window_duration_seconds: number;
  notes: string | null;
  dry_run_checked_at: string | null;
  dry_run_status: string | null;
  dry_run_http_status: number | null;
  dry_run_message: string | null;
  success_at: string | null;
  success_worker_id: number | null;
  success_response_code: number | null;
  success_message: string | null;
  created_at: string;
  updated_at: string;
};

export type DomainImportResponse = {
  inserted: DropDomain[];
  skipped: string[];
};

export type DiscoveryDomain = {
  id: number;
  fqdn: string;
  zone: string;
  status: string;
  is_enabled: boolean;
  check_interval_seconds: number;
  source_mode: string;
  drop_prediction_enabled: boolean;
  last_lifecycle_stage: string | null;
  last_status_codes: string | null;
  last_availability: string | null;
  last_checked_at: string | null;
  last_status_signature: string | null;
  last_owner_signature: string | null;
  last_change_at: string | null;
  last_change_summary: string | null;
  next_check_at: string | null;
  first_seen_redemption_at: string | null;
  last_seen_redemption_at: string | null;
  redemption_anchor_at: string | null;
  redemption_anchor_source: string | null;
  predicted_pending_delete_at: string | null;
  pending_delete_previous_seen_at: string | null;
  first_seen_pending_delete_at: string | null;
  last_seen_pending_delete_at: string | null;
  predicted_drop_start_at: string | null;
  predicted_drop_end_at: string | null;
  available_first_seen_at: string | null;
  last_error: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type DiscoveryDomainImportResponse = {
  inserted: DiscoveryDomain[];
  skipped: string[];
};

export type DiscoveryDomainIntervalUpdateResponse = {
  updated: number;
  check_interval_seconds: number;
};

export type DiscoveryZoneStats = {
  zone: string;
  total: number;
  pending_delete: number;
  available: number;
  predicted: number;
  has_strategy_pattern: boolean;
};

export type AllZonefilesSettings = {
  configured: boolean;
  base_url: string;
};

export type AllZonefilesTestResult = {
  ok: boolean;
  message: string;
  zones_count: number | null;
};

export type DiscoveryRuntimeSettings = {
  discovery_enabled: boolean;
  discovery_worker_enabled: boolean;
  discovery_local_fallback_enabled: boolean;
  discovery_scheduler_interval_seconds: number;
  discovery_batch_size: number;
  discovery_concurrency: number;
  discovery_timeout_seconds: number;
  discovery_worker_task_stale_seconds: number;
  worker_discovery_concurrency: number;
  worker_discovery_poll_interval_seconds: number;
};

export type ZoneScanJob = {
  id: number;
  zone: string;
  source_type: string;
  source_date: string | null;
  status: string;
  file_name: string | null;
  file_path: string | null;
  download_url: string | null;
  file_size_bytes: number | null;
  downloaded_bytes: number;
  scanned_lines: number;
  parsed_domains: number;
  filtered_candidates: number;
  submitted_rdap: number;
  completed_rdap: number;
  found_candidates: number;
  error_count: number;
  min_score: number;
  limit_output: number;
  max_rdap_checks: number;
  concurrency: number;
  rdap_timeout_seconds: number;
  pending_delete_min_days: number | null;
  pending_delete_max_days: number | null;
  reservoir_size: number;
  random_seed: number;
  keep_file: boolean;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type ZoneScanCandidate = {
  id: number;
  job_id: number;
  fqdn: string;
  zone: string;
  lifecycle_stage: string;
  status_codes: string | null;
  http_status: number | null;
  checked_at: string;
  redemption_anchor_at: string | null;
  predicted_pending_delete_at: string | null;
  days_to_pending_delete: number | null;
  score: number;
  reason: string | null;
  error: string | null;
  discovery_domain_id: number | null;
  is_ignored: boolean;
  created_at: string;
};

export type DiscoveryObservation = {
  id: number;
  discovery_domain_id: number;
  source: string;
  observed_at: string;
  http_status: number | null;
  latency_ms: number | null;
  lifecycle_stage: string | null;
  availability_status: string | null;
  status_codes: string | null;
  registrar_name: string | null;
  owner_handle: string | null;
  name_servers: string | null;
  status_signature: string | null;
  owner_signature: string | null;
  change_detected: boolean;
  change_summary: string | null;
  raw_response: string | null;
  error: string | null;
  created_at: string;
};

export type ZoneStrategy = {
  id: number;
  zone: string;
  name: string;
  timezone_name: string;
  rule_resolution_mode: string;
  default_min_guaranteed_rps: number;
  default_registrar_slug: string;
  gandi_contact_extra_parameters: string | null;
  gandi_registration_extra_parameters: string | null;
  is_active: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type ZoneRule = {
  id: number;
  zone_strategy_id: number;
  name: string;
  is_enabled: boolean;
  priority: number;
  schedule_type: string;
  hour: number | null;
  minute: number;
  second: number;
  weekdays: string | null;
  specific_date: string | null;
  window_duration_seconds: number;
  execution_profile_mode: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type ZoneRulePhase = {
  id: number;
  zone_rule_id: number;
  name: string;
  sort_order: number;
  start_offset_seconds: number;
  duration_seconds: number;
  rps_mode: string;
  rps_value: number;
  stop_on_success: boolean;
  created_at: string;
  updated_at: string;
};

export type StrategyPreviewWindow = {
  rule_id: number;
  priority: number;
  start_at: string;
  end_at: string;
  rule_name: string | null;
};

export type StrategyPreview = {
  strategy_id: number | null;
  timezone_name: string;
  resolution_mode: string;
  target_date: string;
  windows: StrategyPreviewWindow[];
};

export type DomainOverrideSettings = {
  id: number;
  timezone_name: string;
  rule_resolution_mode: string;
  default_min_guaranteed_rps: number;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type DomainOverrideRule = {
  id: number;
  domain_rule_override_id: number;
  name: string;
  is_enabled: boolean;
  priority: number;
  schedule_type: string;
  hour: number | null;
  minute: number;
  second: number;
  weekdays: string | null;
  specific_date: string | null;
  window_duration_seconds: number;
  execution_profile_mode: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type DomainOverrideRulePhase = {
  id: number;
  domain_override_rule_id: number;
  name: string;
  sort_order: number;
  start_offset_seconds: number;
  duration_seconds: number;
  rps_mode: string;
  rps_value: number;
  stop_on_success: boolean;
  created_at: string;
  updated_at: string;
};

export type WorkerNode = {
  id: number;
  name: string;
  registrar_slug: string;
  assigned_registrar_account_id: number | null;
  api_base_url: string | null;
  control_token: string | null;
  status: string;
  is_enabled: boolean;
  ip_address: string | null;
  region: string | null;
  notes: string | null;
  max_rps: number;
  target_rps: number;
  current_rps: number;
  current_capacity_rps: number;
  cpu_load: number;
  ram_usage_percent: number;
  clock_drift_ms: number;
  runtime_mode: string;
  registration_concurrency_multiplier: number;
  registration_max_concurrency: number;
  vpn_role: string;
  vpn_enabled: boolean;
  vpn_runtime_status: string;
  vpn_public_host: string | null;
  vpn_panel_url: string | null;
  vpn_panel_username: string | null;
  vpn_inbound_id: number | null;
  vpn_last_checked_at: string | null;
  vpn_last_error: string | null;
  current_domain_count: number;
  ssh_host: string | null;
  ssh_port: number;
  ssh_username: string | null;
  ssh_key_path: string | null;
  ssh_last_check_status: string | null;
  ssh_last_check_message: string | null;
  ssh_last_checked_at: string | null;
  ssh_access_configured: boolean;
  last_seen_at: string | null;
  last_heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkerSetup = {
  worker_id: number;
  worker_name: string;
  runtime_base_url: string;
  mode: string;
  simulate_mode: boolean;
  env_file: string;
  write_env_command: string;
  full_install_commands: string[];
  update_existing_commands: string[];
  switch_to_test_commands: string[];
  switch_to_live_commands: string[];
  verify_commands: string[];
};

export type WorkerMaintenanceJob = {
  id: number;
  worker_id: number;
  action: string;
  status: string;
  log: string | null;
  error_message: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkerMaintenanceBulkResponse = {
  action: string;
  started_count: number;
  skipped_count: number;
  jobs: WorkerMaintenanceJob[];
  skipped_worker_ids: number[];
};

export type VpnOverview = {
  enabled_nodes: number;
  ready_nodes: number;
  active_customers: number;
  active_subscriptions: number;
  active_keys: number;
};

export type VpnPlan = {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  duration_days: number | null;
  traffic_limit_gb: number | null;
  max_devices: number;
  price_amount: number;
  currency: string;
  is_active: boolean;
  created_at: string;
  updated_at: string;
};

export type VpnCustomer = {
  id: number;
  telegram_user_id: string | null;
  telegram_username: string | null;
  first_name: string | null;
  last_name: string | null;
  status: string;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type VpnSubscription = {
  id: number;
  customer_id: number;
  plan_id: number | null;
  status: string;
  starts_at: string | null;
  expires_at: string | null;
  traffic_limit_gb: number | null;
  max_devices: number;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type VpnAccessKey = {
  id: number;
  subscription_id: number;
  worker_id: number | null;
  protocol: string;
  public_name: string | null;
  external_uuid: string | null;
  config_uri: string | null;
  status: string;
  issued_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
  last_synced_at: string | null;
  last_error: string | null;
  created_at: string;
  updated_at: string;
};

export type VpnNodeEvent = {
  id: number;
  worker_id: number;
  level: string;
  event_type: string;
  message: string;
  details: Record<string, unknown> | null;
  created_at: string;
};

export type RegistrarAccount = {
  id: number;
  name: string;
  registrar_slug: string;
  api_token: string | null;
  api_base_url: string | null;
  sharing_id: string | null;
  default_contact_profile_id: number | null;
  is_active: boolean;
  supports_dry_run: boolean;
  last_validation_status: string;
  last_validation_message: string | null;
  last_validated_at: string | null;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type ContactProfile = {
  id: number;
  label: string;
  person_type: string;
  given_name: string;
  family_name: string;
  organization_name: string | null;
  email: string;
  phone: string;
  mobile: string | null;
  fax: string | null;
  lang: string | null;
  street_address: string;
  city: string;
  state: string | null;
  zip_code: string;
  country_code: string;
  data_obfuscated: boolean | null;
  mail_obfuscated: boolean | null;
  icann_contract_accept: boolean | null;
  extra_parameters: string | null;
  is_default: boolean;
  notes: string | null;
  created_at: string;
  updated_at: string;
};

export type ContactProfilePrefill = {
  label: string;
  person_type: string;
  given_name: string;
  family_name: string;
  organization_name: string | null;
  email: string;
  phone: string;
  mobile: string | null;
  fax: string | null;
  lang: string | null;
  street_address: string;
  city: string;
  state: string | null;
  zip_code: string;
  country_code: string;
  data_obfuscated: boolean | null;
  mail_obfuscated: boolean | null;
  icann_contract_accept: boolean | null;
  extra_parameters: string | null;
  is_default: boolean;
  notes: string | null;
};

export type AttackRun = {
  id: number;
  domain_id: number;
  status: string;
  planned_start_at: string;
  planned_end_at: string;
  started_at: string | null;
  finished_at: string | null;
  assigned_worker_count: number;
  planned_rps: number;
  current_rps: number;
  max_rps: number;
  runtime_minimum_rps: number | null;
  runtime_desired_rps: number | null;
  runtime_allocated_rps: number | null;
  runtime_phase_name: string | null;
  success_worker_id: number | null;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
};

export type WorkerTask = {
  id: number;
  attack_run_id: number;
  domain_id: number;
  worker_id: number;
  status: string;
  planned_rps: number;
  actual_rps: number;
  total_attempts: number;
  success_attempts: number;
  latency_ms: number | null;
  last_http_status: number | null;
  last_error: string | null;
  response_status_counts: Record<string, number> | null;
  response_error_counts: Record<string, number> | null;
  response_samples: {
    first?: Array<Record<string, unknown>>;
    last?: Array<Record<string, unknown>>;
    by_status?: Record<string, Array<Record<string, unknown>>>;
  } | null;
  assigned_at: string | null;
  acknowledged_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  stop_reason: string | null;
  created_at: string;
  updated_at: string;
};

export type AttackEvent = {
  id: number;
  attack_run_id: number | null;
  domain_id: number | null;
  worker_id: number | null;
  level: string;
  event_type: string;
  message: string;
  created_at: string;
};

export type DomainDryRunResult = {
  domain_id: number;
  status: string;
  http_status: number | null;
  message: string;
  checked_at: string;
};

export type DomainDryRunBatchResult = {
  total: number;
  ready: number;
  invalid: number;
  error: number;
  results: DomainDryRunResult[];
};

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

export function discoveryAvailableExportUrl(zone?: string): string {
  const query = zone ? `?zone=${encodeURIComponent(zone)}` : "";
  return `${API_BASE}/control/discovery/domains/available/export.csv${query}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    credentials: "include",
    headers: {
      Accept: "application/json",
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      ...(init?.headers ?? {}),
    },
    ...init,
  });

  if (!response.ok) {
    const contentType = response.headers.get("content-type") ?? "";
    if (contentType.includes("application/json")) {
      const payload = (await response.json()) as { detail?: string };
      throw new Error(payload.detail || `Запрос завершился ошибкой ${response.status}`);
    }
    throw new Error((await response.text()) || `Запрос завершился ошибкой ${response.status}`);
  }

  return (await response.json()) as T;
}

export const api = {
  login: (payload: { username: string; password: string; remember_me: boolean }) =>
    request<SessionResponse>("/auth/login", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  register: (payload: { username: string; password: string; language: string }) =>
    request<SessionResponse>("/auth/register", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  logout: () => request<{ detail: string }>("/auth/logout", { method: "POST" }),
  getSession: () => request<SessionResponse>("/auth/me"),
  changePassword: (payload: { current_password: string; new_password: string }) =>
    request<SessionResponse>("/auth/change-password", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateTelegram: (payload: { telegram_token?: string | null; telegram_chat_id?: string | null }) =>
    request<SessionResponse>("/auth/telegram", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  testTelegram: () => request<{ detail: string }>("/auth/telegram/test", { method: "POST" }),
  getDiagnosticTelegram: () => request<DiagnosticTelegramSettings>("/admin/diagnostic-telegram"),
  updateDiagnosticTelegram: (payload: DiagnosticTelegramSettings) =>
    request<DiagnosticTelegramSettings>("/admin/diagnostic-telegram", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  testDiagnosticTelegram: () => request<{ detail: string }>("/admin/diagnostic-telegram/test", { method: "POST" }),

  getOverview: () => request<Overview>("/control/overview"),
  getZoneStrategies: () => request<ZoneStrategy[]>("/control/zone-strategies"),
  createZoneStrategy: (payload: Record<string, unknown>) =>
    request<ZoneStrategy>("/control/zone-strategies", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateZoneStrategy: (id: number, payload: Record<string, unknown>) =>
    request<ZoneStrategy>(`/control/zone-strategies/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  createZoneStrategyPreset: (zone: string) =>
    request<ZoneStrategy>(`/control/zone-strategies/presets/${encodeURIComponent(zone)}`, {
      method: "POST",
    }),
  getZoneRules: (strategyId: number) => request<ZoneRule[]>(`/control/zone-strategies/${strategyId}/rules`),
  createZoneRule: (strategyId: number, payload: Record<string, unknown>) =>
    request<ZoneRule>(`/control/zone-strategies/${strategyId}/rules`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteZoneRule: (ruleId: number) => request<{ detail: string }>(`/control/zone-rules/${ruleId}`, { method: "DELETE" }),
  getZoneRulePhases: (ruleId: number) => request<ZoneRulePhase[]>(`/control/zone-rules/${ruleId}/phases`),
  createZoneRulePhase: (ruleId: number, payload: Record<string, unknown>) =>
    request<ZoneRulePhase>(`/control/zone-rules/${ruleId}/phases`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteZoneRulePhase: (phaseId: number) =>
    request<{ detail: string }>(`/control/zone-rule-phases/${phaseId}`, { method: "DELETE" }),
  previewZoneStrategy: (strategyId: number, targetDate: string) =>
    request<StrategyPreview>(`/control/zone-strategies/${strategyId}/preview?target_date=${encodeURIComponent(targetDate)}`),
  getDomains: () => request<DropDomain[]>("/control/domains"),
  createDomain: (payload: Record<string, unknown>) =>
    request<DomainImportResponse>("/control/domains", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  importDomains: (file: File, payload: Record<string, string | number | boolean | null | undefined>) => {
    const formData = new FormData();
    formData.append("file", file);
    Object.entries(payload).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") {
        formData.append(key, String(value));
      }
    });
    return request<DomainImportResponse>("/control/domains/import", {
      method: "POST",
      body: formData,
    });
  },
  updateDomain: (id: number, payload: Record<string, unknown>) =>
    request<DropDomain>(`/control/domains/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  dryRunDomain: (id: number) =>
    request<DomainDryRunResult>(`/control/domains/${id}/dry-run`, {
      method: "POST",
    }),
  dryRunDomainsBatch: (payload: { domain_ids?: number[]; due_today_only?: boolean; only_ready?: boolean }) =>
    request<DomainDryRunBatchResult>("/control/domains/dry-run/batch", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteDomain: (id: number) => request<{ detail: string }>(`/control/domains/${id}`, { method: "DELETE" }),
  getDiscoveryDomains: () => request<DiscoveryDomain[]>("/control/discovery/domains"),
  importDiscoveryDomains: (payload: Record<string, unknown>) =>
    request<DiscoveryDomainImportResponse>("/control/discovery/domains/import", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  createDiscoveryObservation: (id: number, payload: Record<string, unknown>) =>
    request<DiscoveryDomain>(`/control/discovery/domains/${id}/observations`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getDiscoveryObservations: (id: number) =>
    request<DiscoveryObservation[]>(`/control/discovery/domains/${id}/observations`),
  checkDiscoveryDomain: (id: number) =>
    request<DiscoveryDomain>(`/control/discovery/domains/${id}/check`, {
      method: "POST",
    }),
  updateDiscoveryDomainsInterval: (payload: {
    domain_ids: number[];
    check_interval_seconds: number;
    reschedule_pending?: boolean;
  }) =>
    request<DiscoveryDomainIntervalUpdateResponse>("/control/discovery/domains/interval", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getDiscoveryZoneStats: () => request<DiscoveryZoneStats[]>("/control/discovery/zone-stats"),
  getDiscoveryRuntimeSettings: () => request<DiscoveryRuntimeSettings>("/control/discovery/runtime-settings"),
  updateDiscoveryRuntimeSettings: (payload: Partial<DiscoveryRuntimeSettings>) =>
    request<DiscoveryRuntimeSettings>("/control/discovery/runtime-settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteDiscoveryDomain: (id: number) =>
    request<{ detail: string }>(`/control/discovery/domains/${id}`, { method: "DELETE" }),

  getAllZonefilesSettings: () => request<AllZonefilesSettings>("/control/zone-scanner/settings"),
  updateAllZonefilesSettings: (payload: { api_token: string | null }) =>
    request<AllZonefilesSettings>("/control/zone-scanner/settings", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  testAllZonefilesSettings: () =>
    request<AllZonefilesTestResult>("/control/zone-scanner/settings/test", {
      method: "POST",
    }),
  getZoneScanJobs: () => request<ZoneScanJob[]>("/control/zone-scanner/jobs"),
  createZoneScanJob: (payload: Record<string, unknown>) =>
    request<ZoneScanJob>("/control/zone-scanner/jobs", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  cancelZoneScanJob: (id: number) =>
    request<ZoneScanJob>(`/control/zone-scanner/jobs/${id}/cancel`, {
      method: "POST",
    }),
  deleteZoneScanJobFile: (id: number) =>
    request<{ detail: string }>(`/control/zone-scanner/jobs/${id}/file`, { method: "DELETE" }),
  deleteZoneScanJob: (id: number) => request<{ detail: string }>(`/control/zone-scanner/jobs/${id}`, { method: "DELETE" }),
  getZoneScanCandidates: (jobId?: number | null) =>
    request<ZoneScanCandidate[]>(
      `/control/zone-scanner/candidates${jobId ? `?job_id=${encodeURIComponent(String(jobId))}` : ""}`,
    ),
  addZoneScanCandidateToDiscovery: (id: number) =>
    request<DiscoveryDomain>(`/control/zone-scanner/candidates/${id}/add-to-discovery`, {
      method: "POST",
    }),
  ignoreZoneScanCandidate: (id: number) =>
    request<ZoneScanCandidate>(`/control/zone-scanner/candidates/${id}/ignore`, {
      method: "POST",
    }),
  getDomainOverride: (domainId: number) => request<DomainOverrideSettings>(`/control/domains/${domainId}/override`),
  createDomainOverride: (domainId: number, payload: Record<string, unknown>) =>
    request<DomainOverrideSettings>(`/control/domains/${domainId}/override`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateDomainOverride: (domainId: number, payload: Record<string, unknown>) =>
    request<DomainOverrideSettings>(`/control/domains/${domainId}/override`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getDomainOverrideRules: (domainId: number) =>
    request<DomainOverrideRule[]>(`/control/domains/${domainId}/override/rules`),
  createDomainOverrideRule: (domainId: number, payload: Record<string, unknown>) =>
    request<DomainOverrideRule>(`/control/domains/${domainId}/override/rules`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteDomainOverrideRule: (ruleId: number) =>
    request<{ detail: string }>(`/control/domain-override-rules/${ruleId}`, { method: "DELETE" }),
  getDomainOverrideRulePhases: (ruleId: number) =>
    request<DomainOverrideRulePhase[]>(`/control/domain-override-rules/${ruleId}/phases`),
  createDomainOverrideRulePhase: (ruleId: number, payload: Record<string, unknown>) =>
    request<DomainOverrideRulePhase>(`/control/domain-override-rules/${ruleId}/phases`, {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  deleteDomainOverrideRulePhase: (phaseId: number) =>
    request<{ detail: string }>(`/control/domain-override-phases/${phaseId}`, { method: "DELETE" }),
  previewDomainOverride: (domainId: number, targetDate: string) =>
    request<StrategyPreview>(`/control/domains/${domainId}/override/preview?target_date=${encodeURIComponent(targetDate)}`),

  getWorkers: () => request<WorkerNode[]>("/control/workers"),
  createWorker: (payload: Record<string, unknown>) =>
    request<WorkerNode>("/control/workers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateWorker: (id: number, payload: Record<string, unknown>) =>
    request<WorkerNode>(`/control/workers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getWorkerSetup: (id: number, payload: { simulate_mode: boolean; runtime_base_url?: string | null }) => {
    const params = new URLSearchParams({ simulate_mode: String(payload.simulate_mode) });
    if (payload.runtime_base_url) {
      params.set("runtime_base_url", payload.runtime_base_url);
    }
    return request<WorkerSetup>(`/control/workers/${id}/setup?${params.toString()}`);
  },
  getWorkerMaintenanceJobs: () => request<WorkerMaintenanceJob[]>("/control/workers/maintenance-jobs"),
  checkWorkerSsh: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/check`, { method: "POST" }),
  installWorkerServer: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/install`, { method: "POST" }),
  updateWorkerServer: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/update`, { method: "POST" }),
  checkWorkerVpn: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-check`, { method: "POST" }),
  installWorkerVpn: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-install`, { method: "POST" }),
  updateWorkerVpn: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-update`, { method: "POST" }),
  restartWorkerVpn: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-restart`, { method: "POST" }),
  autoconfigWorkerVpn: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-autoconfig`, { method: "POST" }),
  createWorkerVpnInbound: (id: number) =>
    request<WorkerMaintenanceJob>(`/control/workers/${id}/maintenance/vpn-create-inbound`, { method: "POST" }),
  updateAllWorkerServers: () =>
    request<WorkerMaintenanceBulkResponse>("/control/workers/maintenance/update-all", { method: "POST" }),
  updateAllVpnNodes: () =>
    request<WorkerMaintenanceBulkResponse>("/control/workers/maintenance/vpn-update-all", { method: "POST" }),
  autoconfigAllVpnNodes: () =>
    request<WorkerMaintenanceBulkResponse>("/control/workers/maintenance/vpn-autoconfig-all", { method: "POST" }),
  deleteWorker: (id: number) => request<{ detail: string }>(`/control/workers/${id}`, { method: "DELETE" }),

  getVpnOverview: () => request<VpnOverview>("/control/vpn/overview"),
  getVpnPlans: () => request<VpnPlan[]>("/control/vpn/plans"),
  createVpnPlan: (payload: Record<string, unknown>) =>
    request<VpnPlan>("/control/vpn/plans", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateVpnPlan: (id: number, payload: Record<string, unknown>) =>
    request<VpnPlan>(`/control/vpn/plans/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteVpnPlan: (id: number) => request<{ detail: string }>(`/control/vpn/plans/${id}`, { method: "DELETE" }),
  getVpnCustomers: () => request<VpnCustomer[]>("/control/vpn/customers"),
  createVpnCustomer: (payload: Record<string, unknown>) =>
    request<VpnCustomer>("/control/vpn/customers", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateVpnCustomer: (id: number, payload: Record<string, unknown>) =>
    request<VpnCustomer>(`/control/vpn/customers/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getVpnSubscriptions: () => request<VpnSubscription[]>("/control/vpn/subscriptions"),
  createVpnSubscription: (payload: Record<string, unknown>) =>
    request<VpnSubscription>("/control/vpn/subscriptions", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateVpnSubscription: (id: number, payload: Record<string, unknown>) =>
    request<VpnSubscription>(`/control/vpn/subscriptions/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  getVpnAccessKeys: () => request<VpnAccessKey[]>("/control/vpn/access-keys"),
  createVpnAccessKey: (payload: Record<string, unknown>) =>
    request<VpnAccessKey>("/control/vpn/access-keys", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  provisionVpnAccessKey: (id: number) =>
    request<VpnAccessKey>(`/control/vpn/access-keys/${id}/provision`, { method: "POST" }),
  getVpnNodeEvents: (workerId?: number) =>
    request<VpnNodeEvent[]>(
      `/control/vpn/node-events${workerId ? `?worker_id=${encodeURIComponent(String(workerId))}` : ""}`,
    ),

  getRegistrarAccounts: () => request<RegistrarAccount[]>("/control/registrar-accounts"),
  createRegistrarAccount: (payload: Record<string, unknown>) =>
    request<RegistrarAccount>("/control/registrar-accounts", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateRegistrarAccount: (id: number, payload: Record<string, unknown>) =>
    request<RegistrarAccount>(`/control/registrar-accounts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  validateRegistrarAccount: (id: number) =>
    request<{ id: number; last_validation_status: string; last_validation_message: string | null; last_validated_at: string | null }>(
      `/control/registrar-accounts/${id}/validate`,
      { method: "POST" },
    ),
  prefillContactFromRegistrarAccount: (id: number) =>
    request<ContactProfilePrefill>(`/control/registrar-accounts/${id}/prefill-contact`, {
      method: "POST",
    }),
  deleteRegistrarAccount: (id: number) =>
    request<{ detail: string }>(`/control/registrar-accounts/${id}`, { method: "DELETE" }),

  getContactProfiles: () => request<ContactProfile[]>("/control/contact-profiles"),
  createContactProfile: (payload: Record<string, unknown>) =>
    request<ContactProfile>("/control/contact-profiles", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateContactProfile: (id: number, payload: Record<string, unknown>) =>
    request<ContactProfile>(`/control/contact-profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteContactProfile: (id: number) =>
    request<{ detail: string }>(`/control/contact-profiles/${id}`, { method: "DELETE" }),

  getAttacks: () => request<AttackRun[]>("/control/attacks"),
  getTasks: () => request<WorkerTask[]>("/control/tasks"),
  getEvents: () => request<AttackEvent[]>("/control/events"),
  startAttacks: (payload: { domain_ids?: number[]; force_rebuild?: boolean }) =>
    request<AttackRun[]>("/control/attacks/start", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  simulateRegistration: (payload: { domain_ids: number[]; duration_seconds?: number; force_rebuild?: boolean }) =>
    request<AttackRun[]>("/control/attacks/simulate-registration", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  rebalanceAttacks: () =>
    request<{ detail: string }>("/control/attacks/rebalance", {
      method: "POST",
    }),
  stopAttacks: (payload: { domain_ids?: number[]; reason?: string }) =>
    request<{ detail: string }>("/control/attacks/stop", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
};
