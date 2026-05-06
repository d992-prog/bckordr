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
  override_min_guaranteed_rps: number | null;
  readiness_reasons: string | null;
  runtime_minimum_rps: number | null;
  runtime_desired_rps: number | null;
  runtime_allocated_rps: number | null;
  runtime_assigned_worker_count: number;
  runtime_phase_name: string | null;
  runtime_attack_run_id: number | null;
  runtime_attack_status: string | null;
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

export type ZoneStrategy = {
  id: number;
  zone: string;
  name: string;
  timezone_name: string;
  rule_resolution_mode: string;
  default_min_guaranteed_rps: number;
  default_registrar_slug: string;
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
  current_domain_count: number;
  last_seen_at: string | null;
  last_heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
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
      throw new Error(payload.detail || `Request failed with ${response.status}`);
    }
    throw new Error((await response.text()) || `Request failed with ${response.status}`);
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
  deleteWorker: (id: number) => request<{ detail: string }>(`/control/workers/${id}`, { method: "DELETE" }),

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
