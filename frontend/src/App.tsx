import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  api,
  discoveryAvailableExportUrl,
  AttackEvent,
  AttackRun,
  AllZonefilesSettings,
  ContactProfile,
  ContactProfilePrefill,
  DiagnosticTelegramSettings,
  DiscoveryDomain,
  DiscoveryObservation,
  DiscoveryRuntimeSettings,
  DiscoveryZoneStats,
  DomainOverrideRule,
  DomainOverrideRulePhase,
  DomainOverrideSettings,
  DropDomain,
  Overview,
  RegistrarAccount,
  SessionResponse,
  StrategyPreview,
  WorkerNode,
  WorkerMaintenanceJob,
  WorkerSetup,
  WorkerTask,
  ZoneScanCandidate,
  ZoneScanJob,
  ZoneRule,
  ZoneRulePhase,
  ZoneStrategy,
} from "./api";

type Toast = { type: "success" | "error"; text: string } | null;
type Tab =
  | "domains"
  | "discovery"
  | "scanner"
  | "strategies"
  | "workers"
  | "accounts"
  | "contacts"
  | "attacks"
  | "settings";

const DEFAULT_DOMAIN_FORM = {
  domainsText: "",
  dropDate: "",
  zone: "fr",
  timezoneName: "Europe/Paris",
  registrarSlug: "gandi",
  zoneStrategyId: "",
  strategyMode: "inherit_zone",
  registrarAccountId: "",
  contactProfileId: "",
  priority: "100",
  requestedDurationYears: "1",
  registrationExtraParameters: "",
  attackEnabled: true,
  autoStartEnabled: false,
  autoStartLeadSeconds: "90",
  overrideMinGuaranteedRps: "",
  windowStartMinute: "31",
  windowStartSecond: "30",
  windowDurationSeconds: "95",
  notes: "",
};

const DEFAULT_DISCOVERY_FORM = {
  domainsText: "",
  zone: "",
  checkIntervalSeconds: "21600",
  sourceMode: "rdap",
  disableDropPrediction: false,
  notes: "",
};

const DEFAULT_DISCOVERY_OBSERVATION_FORM = {
  domainId: "",
  lifecycleStage: "pending_delete",
  availabilityStatus: "",
  httpStatus: "200",
  statusCodes: "pendingDelete",
  rawResponse: "",
  error: "",
};

const DEFAULT_DISCOVERY_FILTERS = {
  query: "",
  zone: "",
  status: "",
  lifecycle: "",
  pageSize: "25",
};

const DEFAULT_DISCOVERY_RUNTIME_FORM = {
  discoveryEnabled: true,
  discoveryWorkerEnabled: true,
  discoveryLocalFallbackEnabled: true,
  discoverySchedulerIntervalSeconds: "2",
  discoveryBatchSize: "50",
  discoveryConcurrency: "10",
  discoveryTimeoutSeconds: "4",
  discoveryWorkerTaskStaleSeconds: "180",
  workerDiscoveryConcurrency: "4",
  workerDiscoveryPollIntervalSeconds: "1",
};

function formatUtcDateDaysAgo(daysAgo: number): string {
  const now = new Date();
  const utcMidnight = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()));
  utcMidnight.setUTCDate(utcMidnight.getUTCDate() - daysAgo);
  return utcMidnight.toISOString().slice(0, 10);
}

const DEFAULT_ZONE_SCAN_FORM = {
  zone: "com",
  sourceType: "zone_historic",
  sourceDate: formatUtcDateDaysAgo(28),
  minScore: "35",
  limitOutput: "20",
  maxRdapChecks: "300000",
  concurrency: "100",
  rdapTimeoutSeconds: "5",
  pendingDeleteMinDays: "1",
  pendingDeleteMaxDays: "2",
  reservoirSize: "300000",
  randomSeed: "42",
  keepFile: false,
};

const DEFAULT_STRATEGY_FORM = {
  zone: "fr",
  name: "France Default",
  timezoneName: "Europe/Paris",
  ruleResolutionMode: "priority",
  defaultMinGuaranteedRps: "1",
  defaultRegistrarSlug: "gandi",
  gandiContactExtraParameters: "",
  gandiRegistrationExtraParameters: "",
  isActive: true,
  notes: "",
};

const DEFAULT_STRATEGY_GANDI_FORM = {
  contactExtraParameters: "",
  registrationExtraParameters: "",
};

const DEFAULT_RULE_FORM = {
  name: "",
  scheduleType: "hourly",
  hour: "",
  minute: "31",
  second: "30",
  weekdays: "",
  specificDate: "",
  windowDurationSeconds: "95",
  priority: "100",
  executionProfileMode: "flat",
  isEnabled: true,
  notes: "",
};

const DEFAULT_PHASE_FORM = {
  ruleId: "",
  name: "burst",
  sortOrder: "0",
  startOffsetSeconds: "0",
  durationSeconds: "0",
  rpsMode: "percent",
  rpsValue: "100",
  stopOnSuccess: true,
};

const DEFAULT_DOMAIN_OVERRIDE_FORM = {
  timezoneName: "Europe/Paris",
  ruleResolutionMode: "priority",
  defaultMinGuaranteedRps: "1",
  notes: "",
};

const DEFAULT_DOMAIN_OVERRIDE_RULE_FORM = {
  name: "",
  scheduleType: "hourly",
  hour: "",
  minute: "31",
  second: "30",
  weekdays: "",
  specificDate: "",
  windowDurationSeconds: "95",
  priority: "100",
  executionProfileMode: "flat",
  isEnabled: true,
  notes: "",
};

const DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM = {
  ruleId: "",
  name: "burst",
  sortOrder: "0",
  startOffsetSeconds: "0",
  durationSeconds: "0",
  rpsMode: "percent",
  rpsValue: "100",
  stopOnSuccess: true,
};

const DEFAULT_WORKER_FORM = {
  name: "",
  registrarSlug: "gandi",
  assignedRegistrarAccountId: "",
  ipAddress: "",
  region: "",
  maxRps: "16",
  targetRps: "16",
  sshHost: "",
  sshPort: "22",
  sshUsername: "root",
  sshPassword: "",
  sshKeyPath: "",
  notes: "",
};

const DEFAULT_ACCOUNT_FORM = {
  name: "",
  registrarSlug: "gandi",
  apiToken: "",
  apiBaseUrl: "",
  sharingId: "",
  defaultContactProfileId: "",
  supportsDryRun: true,
  isActive: true,
  notes: "",
};

const DEFAULT_CONTACT_FORM = {
  label: "",
  personType: "individual",
  givenName: "",
  familyName: "",
  organizationName: "",
  email: "",
  phone: "",
  mobile: "",
  fax: "",
  lang: "fr",
  streetAddress: "",
  city: "",
  state: "",
  zipCode: "",
  countryCode: "FR",
  dataObfuscated: false,
  mailObfuscated: false,
  icannContractAccept: true,
  extraParameters: "",
  isDefault: false,
  notes: "",
};

const DEFAULT_WORKER_RUNTIME_BASE_URL = "http://CONTROL_SERVER_IP:8080";

function makeWorkerForm(worker?: WorkerNode | null) {
  if (!worker) {
    return { ...DEFAULT_WORKER_FORM };
  }
  return {
    name: worker.name,
    registrarSlug: worker.registrar_slug,
    assignedRegistrarAccountId: worker.assigned_registrar_account_id ? String(worker.assigned_registrar_account_id) : "",
    ipAddress: worker.ip_address ?? "",
    region: worker.region ?? "",
    maxRps: String(worker.max_rps),
    targetRps: String(worker.target_rps),
    sshHost: worker.ssh_host ?? worker.ip_address ?? "",
    sshPort: String(worker.ssh_port ?? 22),
    sshUsername: worker.ssh_username ?? "root",
    sshPassword: "",
    sshKeyPath: worker.ssh_key_path ?? "",
    notes: worker.notes ?? "",
  };
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  const formatted = new Intl.DateTimeFormat("ru-RU", {
    timeZone: "Europe/Moscow",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
  return `${formatted} MSK`;
}

function formatDateTimeInZone(value: string | null, timeZone: string, label = timeZone) {
  if (!value) {
    return "—";
  }
  try {
    const formatted = new Intl.DateTimeFormat("ru-RU", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
    return `${formatted} ${label}`;
  } catch {
    return formatDateTime(value);
  }
}

function formatTimeInZone(value: string | null, timeZone: string) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    timeZone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function getTimeZoneOffsetMs(utcDate: Date, timeZone: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(utcDate);
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const zonedAsUtc = Date.UTC(
    Number(values.year),
    Number(values.month) - 1,
    Number(values.day),
    Number(values.hour),
    Number(values.minute),
    Number(values.second),
  );
  return zonedAsUtc - utcDate.getTime();
}

function zonedLocalTimeToIso(dateValue: string, timeValue: string, timeZone: string) {
  const [year, month, day] = dateValue.split("-").map(Number);
  const [hour = 0, minute = 0, second = 0] = timeValue.split(":").map(Number);
  const localAsUtc = Date.UTC(year, month - 1, day, hour, minute, second);
  let utcMs = localAsUtc;
  for (let index = 0; index < 3; index += 1) {
    utcMs = localAsUtc - getTimeZoneOffsetMs(new Date(utcMs), timeZone);
  }
  return new Date(utcMs).toISOString();
}

function formatPresetDescription(
  preset: { scheduleLabel: string; startTime: string; endTime: string; timezoneName: string; localWindowLabel?: string },
  previewDateValue: string,
) {
  const startAt = zonedLocalTimeToIso(previewDateValue, preset.startTime, preset.timezoneName);
  const endAt = zonedLocalTimeToIso(previewDateValue, preset.endTime, preset.timezoneName);
  const localWindow = preset.localWindowLabel ?? `${preset.startTime} → ${preset.endTime}`;
  const mskWindow = `${formatTimeInZone(startAt, "Europe/Moscow")} → ${formatTimeInZone(endAt, "Europe/Moscow")}`;
  return `${preset.scheduleLabel} ${localWindow}, ${preset.timezoneName} | MSK ${mskWindow}`;
}

function getEffectiveWindowDurationSeconds(domain: DropDomain) {
  if (!domain.runtime_window_start_at || !domain.runtime_window_end_at) {
    return domain.effective_window_duration_seconds ?? domain.window_duration_seconds;
  }
  return Math.max(
    1,
    Math.round(
      (new Date(domain.runtime_window_end_at).getTime() - new Date(domain.runtime_window_start_at).getTime()) / 1000,
    ),
  );
}

function formatDomainWindow(domain: DropDomain) {
  if (domain.runtime_window_start_at && domain.runtime_window_end_at) {
    return `${formatTimeInZone(domain.runtime_window_start_at, domain.timezone_name)} → ${formatTimeInZone(
      domain.runtime_window_end_at,
      domain.timezone_name,
    )}`;
  }
  const minute = domain.effective_window_start_minute ?? domain.window_start_minute;
  const second = domain.effective_window_start_second ?? domain.window_start_second;
  const duration = domain.effective_window_duration_seconds ?? domain.window_duration_seconds;
  return `${String(minute).padStart(2, "0")}:${String(second).padStart(
    2,
    "0",
  )} + ${duration}s`;
}

function formatPreviewWindow(window: StrategyPreview["windows"][number], timezoneName: string) {
  const localWindow = `${formatTimeInZone(window.start_at, timezoneName)} → ${formatTimeInZone(window.end_at, timezoneName)}`;
  if (timezoneName === "Europe/Moscow") {
    return `${localWindow} MSK`;
  }
  return `${localWindow} локально / ${formatPreviewWindowMsk(window)}`;
}

function formatPreviewWindowMsk(window: StrategyPreview["windows"][number]) {
  return `${formatTimeInZone(window.start_at, "Europe/Moscow")} → ${formatTimeInZone(window.end_at, "Europe/Moscow")} MSK`;
}

function formatRuleLocalTime(rule: Pick<ZoneRule, "hour" | "minute" | "second">) {
  return `${rule.hour ?? "*"}:${String(rule.minute).padStart(2, "0")}:${String(rule.second).padStart(2, "0")}`;
}

function formatDomainStrategyFallbackWindow(form: typeof DEFAULT_DOMAIN_FORM) {
  return `${String(Number(form.windowStartMinute) || 0).padStart(2, "0")}:${String(
    Number(form.windowStartSecond) || 0,
  ).padStart(2, "0")} + ${Number(form.windowDurationSeconds) || 0}s`;
}

function formatWorkerRuntimeMode(value: string | null | undefined) {
  if (value === "test") {
    return "Тест";
  }
  if (value === "live") {
    return "Бой";
  }
  return "неизвестно";
}

function formatWorkerConcurrency(worker: WorkerNode) {
  const multiplier = worker.registration_concurrency_multiplier ?? 2;
  const maxConcurrency = worker.registration_max_concurrency ?? 64;
  const estimated = Math.max(1, Math.min(maxConcurrency, Math.ceil(Math.max(worker.target_rps, 1) * multiplier)));
  return `x${multiplier} / макс. ${maxConcurrency} | ${worker.target_rps} RPS -> до ${estimated} параллельных`;
}

function formatRedemptionAnchorSource(value: string | null) {
  if (value === "rdap_updated_at") {
    return "дата обновления из RDAP/WHOIS";
  }
  if (value === "first_seen_redemption_at") {
    return "первое обнаружение redemption";
  }
  return "—";
}

function formatResolutionMode(value: string | null | undefined) {
  if (value === "priority") {
    return "по приоритету";
  }
  if (value === "merge") {
    return "объединять окна";
  }
  return value ?? "—";
}

function formatScheduleType(value: string | null | undefined) {
  if (value === "hourly") {
    return "каждый час";
  }
  if (value === "daily") {
    return "каждый день";
  }
  if (value === "weekly") {
    return "по дням недели";
  }
  if (value === "one_time") {
    return "один раз";
  }
  return value ?? "—";
}

function formatExecutionMode(value: string | null | undefined) {
  if (value === "flat") {
    return "ровно";
  }
  if (value === "phased") {
    return "по фазам";
  }
  return value ?? "—";
}

function formatRpsModeLabel(value: string | null | undefined) {
  if (value === "percent") {
    return "процент";
  }
  if (value === "fixed") {
    return "фиксированно";
  }
  return value ?? "—";
}

function formatStatusLabel(value: string | null | undefined) {
  const labels: Record<string, string> = {
    ready: "готово",
    success: "успех",
    succeeded: "успех",
    scheduled: "запланировано",
    running: "в работе",
    attacking: "атака",
    busy: "занят",
    planned: "запланировано",
    invalid: "ошибка",
    error: "ошибка",
    failed: "сбой",
    stopped: "остановлено",
    cancelled: "отменено",
    offline: "офлайн",
    inactive: "выключено",
    active: "активно",
    disabled: "выключено",
    tracking: "наблюдение",
    available: "доступен",
    queued: "в очереди",
    draft: "черновик",
    paused: "пауза",
    provisioning: "настройка",
    completed: "завершено",
    downloading: "скачивание",
    scanning: "сканирование",
    ignored: "скрыто",
  };
  return value ? labels[value] ?? value : "—";
}

function formatMaintenanceAction(value: string | null | undefined) {
  const labels: Record<string, string> = {
    check: "проверка SSH",
    install: "установка",
    update: "обновление",
  };
  return value ? labels[value] ?? value : "—";
}

function formatMaintenanceSummary(job: WorkerMaintenanceJob | undefined) {
  if (!job) {
    return "—";
  }
  return `${formatMaintenanceAction(job.action)}: ${formatStatusLabel(job.status)}`;
}

function formatLifecycleLabel(value: string | null | undefined) {
  const labels: Record<string, string> = {
    registered: "зарегистрирован",
    redemption: "redemption",
    pending_delete: "pendingDelete",
    not_found: "не найден",
    unknown: "неизвестно",
  };
  return value ? labels[value] ?? value : "—";
}

function formatAvailabilityLabel(value: string | null | undefined) {
  const labels: Record<string, string> = {
    available: "доступен",
    taken: "занят",
    unknown: "неизвестно",
  };
  return value ? labels[value] ?? value : "неизвестно";
}

function formatSourceType(value: string | null | undefined) {
  const labels: Record<string, string> = {
    zone_latest: "актуальный zonefile",
    zone_historic: "исторический zonefile",
    expired_latest: "актуальный список expired",
    expired_historic: "исторический список expired",
  };
  return value ? labels[value] ?? value : "—";
}

function formatTabLabel(value: Tab) {
  const labels: Record<Tab, string> = {
    domains: "домены",
    discovery: "discovery",
    scanner: "scanner",
    strategies: "стратегии",
    workers: "воркеры",
    accounts: "аккаунты",
    contacts: "контакты",
    attacks: "атаки",
    settings: "настройки",
  };
  return labels[value];
}

function statusClass(value: string) {
  if (["ready", "success", "scheduled"].includes(value)) {
    return "status available";
  }
  if (["running", "attacking", "busy", "planned", "warning", "info"].includes(value)) {
    return "status checking";
  }
  if (["invalid", "error", "failed", "stopped", "cancelled", "offline"].includes(value)) {
    return "status error";
  }
  return "status inactive";
}

function parseNumber(value: string) {
  if (!value.trim()) {
    return null;
  }
  return Number(value);
}

function formatRps(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "—";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function formatSeconds(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "—";
  }
  if (value < 60) {
    return `${value.toFixed(value >= 10 ? 0 : 1)} сек`;
  }
  return `${(value / 60).toFixed(1)} мин`;
}

function secondsBetween(start: string | null, end: string | null) {
  if (!start || !end) {
    return null;
  }
  const diff = new Date(end).getTime() - new Date(start).getTime();
  return diff > 0 ? diff / 1000 : null;
}

function truncateText(value: string | null | undefined, maxLength = 180) {
  if (!value) {
    return "—";
  }
  const trimmed = value.trim();
  return trimmed.length > maxLength ? `${trimmed.slice(0, maxLength)}…` : trimmed;
}

function extractReadableError(value: string | null | undefined) {
  if (!value) {
    return "—";
  }
  try {
    const parsed = JSON.parse(value) as { message?: unknown; cause?: unknown; code?: unknown };
    const message = typeof parsed.message === "string" ? parsed.message : "";
    const cause = typeof parsed.cause === "string" ? parsed.cause : "";
    const code = parsed.code !== undefined ? `HTTP ${parsed.code}` : "";
    return [code, cause, message].filter(Boolean).join(": ") || truncateText(value);
  } catch {
    return truncateText(value);
  }
}

function formatAttackEventType(value: string) {
  const labels: Record<string, string> = {
    attack_planned: "Запуск создан",
    task_ack: "Воркер взял задачу",
    task_result: "Результат воркера",
    attack_window_expired: "Окно закончилось",
    post_window_rdap_inconclusive: "После окна домен еще занят",
    post_window_rdap_registered: "После окна домен зарегистрирован",
    post_window_rdap_available: "После окна домен доступен",
    domain_registered: "Домен зарегистрирован",
    domain_dry_run: "Тест Gandi",
    worker_stalled: "Воркер завис/пропал",
    attack_rebalanced: "Мощность перераспределена",
    attack_stopped: "Запуск остановлен",
  };
  return labels[value] ?? value;
}

function groupTasksByRun(tasks: WorkerTask[]) {
  const map = new Map<number, WorkerTask[]>();
  for (const task of tasks) {
    const items = map.get(task.attack_run_id) ?? [];
    items.push(task);
    map.set(task.attack_run_id, items);
  }
  return map;
}

function groupEventsByRun(events: AttackEvent[]) {
  const map = new Map<number, AttackEvent[]>();
  for (const event of events) {
    if (!event.attack_run_id) {
      continue;
    }
    const items = map.get(event.attack_run_id) ?? [];
    items.push(event);
    map.set(event.attack_run_id, items);
  }
  return map;
}

function addCount(target: Map<string, number>, key: string, value: number) {
  target.set(key, (target.get(key) ?? 0) + value);
}

function collectTaskStatusCounts(task: WorkerTask) {
  const counts = new Map<string, number>();
  if (task.response_status_counts) {
    for (const [status, count] of Object.entries(task.response_status_counts)) {
      addCount(counts, status, count);
    }
  }
  if (counts.size === 0) {
    addCount(counts, task.last_http_status ? String(task.last_http_status) : "нет", 1);
  }
  return counts;
}

function formatCountMap(counts: Map<string, number>) {
  return Array.from(counts.entries())
    .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
    .map(([status, count]) => `${status} x${count}`)
    .join(", ") || "—";
}

function formatTaskSamples(task: WorkerTask) {
  const lastSamples = task.response_samples?.last ?? [];
  return lastSamples
    .slice(-3)
    .map((sample) => {
      const errorType = String(sample.error_type || "");
      const status = sample.status_code ?? (errorType || "ошибка");
      const body = sample.body_preview || sample.error || "";
      return body ? `${status}: ${String(body).slice(0, 220)}` : String(status);
    })
    .join(" | ");
}

function formatStatusSamples(task: WorkerTask) {
  const byStatus = task.response_samples?.by_status ?? {};
  return Object.entries(byStatus)
    .sort(([left], [right]) => left.localeCompare(right, undefined, { numeric: true }))
    .map(([status, samples]) => {
      const previewLimit = status === "400" ? 800 : 260;
      const previews = samples
        .slice(0, 2)
        .map((sample) => String(sample.body_preview || sample.error || "").slice(0, previewLimit))
        .filter(Boolean)
        .join(" / ");
      return previews ? `${status}: ${previews}` : status;
    })
    .join(" | ");
}

function summarizeAttackRun(attack: AttackRun, runTasks: WorkerTask[], runEvents: AttackEvent[]) {
  const totalAttempts = runTasks.reduce((sum, task) => sum + task.total_attempts, 0);
  const totalSuccess = runTasks.reduce((sum, task) => sum + task.success_attempts, 0);
  const startedAt = runTasks
    .map((task) => task.started_at)
    .filter((value): value is string => Boolean(value))
    .sort()[0] ?? attack.started_at;
  const finishedAt = runTasks
    .map((task) => task.finished_at)
    .filter((value): value is string => Boolean(value))
    .sort();
  const lastFinishedAt = finishedAt.length ? finishedAt[finishedAt.length - 1] : null;
  const elapsedSeconds = secondsBetween(startedAt, lastFinishedAt ?? attack.finished_at);
  const estimatedRps = elapsedSeconds ? totalAttempts / elapsedSeconds : null;
  const httpCounts = new Map<string, number>();
  for (const task of runTasks) {
    for (const [status, count] of collectTaskStatusCounts(task)) {
      addCount(httpCounts, status, count);
    }
  }
  const httpSummary = formatCountMap(httpCounts);
  const postWindowEvent = runEvents.find((event) => event.event_type.startsWith("post_window_rdap"));
  let conclusion = "Нет данных по попыткам";
  let conclusionTone = "inactive";
  if (totalSuccess > 0 || attack.status === "success") {
    conclusion = "Регистрация прошла";
    conclusionTone = "success";
  } else if (totalAttempts > 0 && httpCounts.has("500")) {
    conclusion = "Окно отработало, Gandi отвечал 500";
    conclusionTone = "running";
  } else if (totalAttempts > 0) {
    conclusion = "Окно отработало, регистрации нет";
    conclusionTone = "running";
  } else if (runTasks.length > 0) {
    conclusion = "Воркеры получили задачи, но попыток нет";
    conclusionTone = "error";
  } else if (attack.status === "planned") {
    conclusion = "Запуск ожидает окна";
    conclusionTone = "planned";
  } else if (attack.status === "failed") {
    conclusion = "Сбой без попыток";
    conclusionTone = "error";
  }
  return {
    totalAttempts,
    totalSuccess,
    elapsedSeconds,
    estimatedRps,
    httpSummary,
    postWindowEvent,
    conclusion,
    conclusionTone,
  };
}

function formatBytes(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "—";
  }
  if (value < 1024) {
    return `${value} B`;
  }
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value / 1024;
  let unitIndex = 0;
  while (amount >= 1024 && unitIndex < units.length - 1) {
    amount /= 1024;
    unitIndex += 1;
  }
  return `${amount.toFixed(amount >= 10 ? 1 : 2)} ${units[unitIndex]}`;
}

function splitDomains(value: string) {
  return value
    .split(/[\r\n,;\t ]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function discoveryRuntimeSettingsToForm(settings: DiscoveryRuntimeSettings) {
  return {
    discoveryEnabled: settings.discovery_enabled,
    discoveryWorkerEnabled: settings.discovery_worker_enabled,
    discoveryLocalFallbackEnabled: settings.discovery_local_fallback_enabled,
    discoverySchedulerIntervalSeconds: String(settings.discovery_scheduler_interval_seconds),
    discoveryBatchSize: String(settings.discovery_batch_size),
    discoveryConcurrency: String(settings.discovery_concurrency),
    discoveryTimeoutSeconds: String(settings.discovery_timeout_seconds),
    discoveryWorkerTaskStaleSeconds: String(settings.discovery_worker_task_stale_seconds),
    workerDiscoveryConcurrency: String(settings.worker_discovery_concurrency),
    workerDiscoveryPollIntervalSeconds: String(settings.worker_discovery_poll_interval_seconds),
  };
}

export default function App() {
  const [tab, setTab] = useState<Tab>("domains");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [toast, setToast] = useState<Toast>(null);

  const [overview, setOverview] = useState<Overview | null>(null);
  const [strategies, setStrategies] = useState<ZoneStrategy[]>([]);
  const [strategyRules, setStrategyRules] = useState<ZoneRule[]>([]);
  const [rulePhases, setRulePhases] = useState<Record<number, ZoneRulePhase[]>>({});
  const [strategyPreview, setStrategyPreview] = useState<StrategyPreview | null>(null);
  const [domainStrategyPreview, setDomainStrategyPreview] = useState<StrategyPreview | null>(null);
  const [domains, setDomains] = useState<DropDomain[]>([]);
  const [discoveryDomains, setDiscoveryDomains] = useState<DiscoveryDomain[]>([]);
  const [selectedDiscoveryDomainId, setSelectedDiscoveryDomainId] = useState<number | null>(null);
  const [discoveryObservations, setDiscoveryObservations] = useState<DiscoveryObservation[]>([]);
  const [discoveryZoneStats, setDiscoveryZoneStats] = useState<DiscoveryZoneStats[]>([]);
  const [discoveryRuntimeSettings, setDiscoveryRuntimeSettings] = useState<DiscoveryRuntimeSettings | null>(null);
  const [allZonefilesSettings, setAllZonefilesSettings] = useState<AllZonefilesSettings | null>(null);
  const [zoneScanJobs, setZoneScanJobs] = useState<ZoneScanJob[]>([]);
  const [zoneScanCandidates, setZoneScanCandidates] = useState<ZoneScanCandidate[]>([]);
  const [selectedZoneScanJobId, setSelectedZoneScanJobId] = useState<number | null>(null);
  const [workers, setWorkers] = useState<WorkerNode[]>([]);
  const [workerMaintenanceJobs, setWorkerMaintenanceJobs] = useState<WorkerMaintenanceJob[]>([]);
  const [workerSearch, setWorkerSearch] = useState("");
  const [workerStatusFilter, setWorkerStatusFilter] = useState("all");
  const [workerPage, setWorkerPage] = useState(1);
  const [workerPageSize, setWorkerPageSize] = useState(10);
  const [maintenanceActionFilter, setMaintenanceActionFilter] = useState("all");
  const [maintenanceStatusFilter, setMaintenanceStatusFilter] = useState("all");
  const [maintenancePage, setMaintenancePage] = useState(1);
  const [maintenancePageSize, setMaintenancePageSize] = useState(10);
  const [accounts, setAccounts] = useState<RegistrarAccount[]>([]);
  const [contacts, setContacts] = useState<ContactProfile[]>([]);
  const [attacks, setAttacks] = useState<AttackRun[]>([]);
  const [tasks, setTasks] = useState<WorkerTask[]>([]);
  const [events, setEvents] = useState<AttackEvent[]>([]);

  const [loginForm, setLoginForm] = useState({ username: "", password: "", remember_me: true });
  const [domainForm, setDomainForm] = useState(DEFAULT_DOMAIN_FORM);
  const [domainStrategyInitialized, setDomainStrategyInitialized] = useState(false);
  const [discoveryForm, setDiscoveryForm] = useState(DEFAULT_DISCOVERY_FORM);
  const [discoveryObservationForm, setDiscoveryObservationForm] = useState(DEFAULT_DISCOVERY_OBSERVATION_FORM);
  const [discoveryFilters, setDiscoveryFilters] = useState(DEFAULT_DISCOVERY_FILTERS);
  const [discoveryBulkIntervalSeconds, setDiscoveryBulkIntervalSeconds] = useState(DEFAULT_DISCOVERY_FORM.checkIntervalSeconds);
  const [discoveryRuntimeForm, setDiscoveryRuntimeForm] = useState(DEFAULT_DISCOVERY_RUNTIME_FORM);
  const [discoveryPage, setDiscoveryPage] = useState(1);
  const [allZonefilesTokenForm, setAllZonefilesTokenForm] = useState("");
  const [zoneScanForm, setZoneScanForm] = useState(DEFAULT_ZONE_SCAN_FORM);
  const [strategyForm, setStrategyForm] = useState(DEFAULT_STRATEGY_FORM);
  const [strategyGandiForm, setStrategyGandiForm] = useState(DEFAULT_STRATEGY_GANDI_FORM);
  const [ruleForm, setRuleForm] = useState(DEFAULT_RULE_FORM);
  const [phaseForm, setPhaseForm] = useState(DEFAULT_PHASE_FORM);
  const [selectedStrategyId, setSelectedStrategyId] = useState<number | null>(null);
  const [previewDate, setPreviewDate] = useState(new Date().toISOString().slice(0, 10));
  const [selectedOverrideDomainId, setSelectedOverrideDomainId] = useState<number | null>(null);
  const [domainOverrideSettings, setDomainOverrideSettings] = useState<DomainOverrideSettings | null>(null);
  const [domainOverrideRules, setDomainOverrideRules] = useState<DomainOverrideRule[]>([]);
  const [domainOverridePhases, setDomainOverridePhases] = useState<Record<number, DomainOverrideRulePhase[]>>({});
  const [domainOverridePreview, setDomainOverridePreview] = useState<StrategyPreview | null>(null);
  const [domainOverrideForm, setDomainOverrideForm] = useState(DEFAULT_DOMAIN_OVERRIDE_FORM);
  const [domainOverrideRuleForm, setDomainOverrideRuleForm] = useState(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
  const [domainOverridePhaseForm, setDomainOverridePhaseForm] = useState(DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM);
  const [workerForm, setWorkerForm] = useState(DEFAULT_WORKER_FORM);
  const [editingWorkerId, setEditingWorkerId] = useState<number | null>(null);
  const [workerSetup, setWorkerSetup] = useState<WorkerSetup | null>(null);
  const [workerSetupMode, setWorkerSetupMode] = useState<"test" | "live">("test");
  const [workerSetupRuntimeUrl, setWorkerSetupRuntimeUrl] = useState(DEFAULT_WORKER_RUNTIME_BASE_URL);
  const [workerSetupLoading, setWorkerSetupLoading] = useState(false);
  const [accountForm, setAccountForm] = useState(DEFAULT_ACCOUNT_FORM);
  const [contactForm, setContactForm] = useState(DEFAULT_CONTACT_FORM);
  const [editingContactId, setEditingContactId] = useState<number | null>(null);
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "" });
  const [telegramForm, setTelegramForm] = useState({ telegram_token: "", telegram_chat_id: "" });
  const [diagnosticTelegram, setDiagnosticTelegram] = useState<DiagnosticTelegramSettings>({
    telegram_token: "",
    telegram_chat_id: "",
  });
  const [importFile, setImportFile] = useState<File | null>(null);

  const accountMap = useMemo(() => new Map(accounts.map((item) => [item.id, item])), [accounts]);
  const contactMap = useMemo(() => new Map(contacts.map((item) => [item.id, item])), [contacts]);
  const domainMap = useMemo(() => new Map(domains.map((item) => [item.id, item])), [domains]);
  const strategyMap = useMemo(() => new Map(strategies.map((item) => [item.id, item])), [strategies]);
  const latestWorkerMaintenanceJobByWorker = useMemo(() => {
    const items = new Map<number, WorkerMaintenanceJob>();
    for (const job of workerMaintenanceJobs) {
      if (!items.has(job.worker_id)) {
        items.set(job.worker_id, job);
      }
    }
    return items;
  }, [workerMaintenanceJobs]);
  const activeOrSucceededInstallJobByWorker = useMemo(() => {
    const items = new Map<number, WorkerMaintenanceJob>();
    for (const job of workerMaintenanceJobs) {
      if (job.action !== "install" || !["queued", "running", "succeeded"].includes(job.status)) {
        continue;
      }
      if (!items.has(job.worker_id)) {
        items.set(job.worker_id, job);
      }
    }
    return items;
  }, [workerMaintenanceJobs]);
  const filteredWorkers = useMemo(() => {
    const search = workerSearch.trim().toLowerCase();
    return workers.filter((worker) => {
      if (workerStatusFilter !== "all" && worker.status !== workerStatusFilter) {
        return false;
      }
      if (!search) {
        return true;
      }
      return [
        worker.name,
        worker.ip_address,
        worker.region,
        worker.registrar_slug,
        worker.ssh_host,
      ]
        .filter(Boolean)
        .some((value) => String(value).toLowerCase().includes(search));
    });
  }, [workerSearch, workerStatusFilter, workers]);
  const workerTotalPages = Math.max(1, Math.ceil(filteredWorkers.length / workerPageSize));
  const workerCurrentPage = Math.min(workerPage, workerTotalPages);
  const visibleWorkers = filteredWorkers.slice((workerCurrentPage - 1) * workerPageSize, workerCurrentPage * workerPageSize);
  const filteredMaintenanceJobs = useMemo(() => {
    return workerMaintenanceJobs.filter((job) => {
      if (maintenanceActionFilter !== "all" && job.action !== maintenanceActionFilter) {
        return false;
      }
      if (maintenanceStatusFilter !== "all" && job.status !== maintenanceStatusFilter) {
        return false;
      }
      return true;
    });
  }, [maintenanceActionFilter, maintenanceStatusFilter, workerMaintenanceJobs]);
  const maintenanceTotalPages = Math.max(1, Math.ceil(filteredMaintenanceJobs.length / maintenancePageSize));
  const maintenanceCurrentPage = Math.min(maintenancePage, maintenanceTotalPages);
  const visibleMaintenanceJobs = filteredMaintenanceJobs.slice(
    (maintenanceCurrentPage - 1) * maintenancePageSize,
    maintenanceCurrentPage * maintenancePageSize,
  );
  const selectedStrategy = useMemo(
    () => strategies.find((item) => item.id === selectedStrategyId) ?? null,
    [selectedStrategyId, strategies],
  );
  const manualOverrideDomains = useMemo(
    () => domains.filter((item) => item.strategy_mode === "manual_override"),
    [domains],
  );
  const selectedOverrideDomain = useMemo(
    () => manualOverrideDomains.find((item) => item.id === selectedOverrideDomainId) ?? null,
    [manualOverrideDomains, selectedOverrideDomainId],
  );
  const activeDomainStrategies = useMemo(
    () =>
      strategies
        .filter((item) => item.is_active)
        .sort((left, right) => left.zone.localeCompare(right.zone) || left.name.localeCompare(right.name)),
    [strategies],
  );
  const selectedDomainStrategy = useMemo(() => {
    const explicitStrategyId = parseNumber(domainForm.zoneStrategyId);
    if (explicitStrategyId) {
      return strategies.find((item) => item.id === explicitStrategyId) ?? null;
    }
    const zone = domainForm.zone.trim().toLowerCase();
    return activeDomainStrategies.find((item) => item.zone.toLowerCase() === zone) ?? null;
  }, [activeDomainStrategies, domainForm.zone, domainForm.zoneStrategyId, strategies]);
  const selectedDiscoveryDomain = useMemo(
    () => discoveryDomains.find((item) => item.id === selectedDiscoveryDomainId) ?? null,
    [discoveryDomains, selectedDiscoveryDomainId],
  );
  const discoveryZoneOptions = useMemo(
    () => [...new Set(discoveryDomains.map((item) => item.zone).filter(Boolean))].sort(),
    [discoveryDomains],
  );
  const discoveryStatusOptions = useMemo(
    () => [...new Set(discoveryDomains.map((item) => item.status).filter(Boolean))].sort(),
    [discoveryDomains],
  );
  const discoveryLifecycleOptions = useMemo(
    () => [...new Set(discoveryDomains.map((item) => item.last_lifecycle_stage).filter(Boolean))].sort() as string[],
    [discoveryDomains],
  );
  const filteredDiscoveryDomains = useMemo(() => {
    const query = discoveryFilters.query.trim().toLowerCase();
    return discoveryDomains.filter((domain) => {
      if (query && !domain.fqdn.toLowerCase().includes(query)) {
        return false;
      }
      if (discoveryFilters.zone && domain.zone !== discoveryFilters.zone) {
        return false;
      }
      if (discoveryFilters.status && domain.status !== discoveryFilters.status) {
        return false;
      }
      if (discoveryFilters.lifecycle && domain.last_lifecycle_stage !== discoveryFilters.lifecycle) {
        return false;
      }
      return true;
    });
  }, [discoveryDomains, discoveryFilters]);
  const discoveryPageSize = Math.max(Number(discoveryFilters.pageSize) || 25, 1);
  const discoveryTotalPages = Math.max(Math.ceil(filteredDiscoveryDomains.length / discoveryPageSize), 1);
  const activeDiscoveryPage = Math.min(discoveryPage, discoveryTotalPages);
  const paginatedDiscoveryDomains = filteredDiscoveryDomains.slice(
    (activeDiscoveryPage - 1) * discoveryPageSize,
    activeDiscoveryPage * discoveryPageSize,
  );

  useEffect(() => {
    let mounted = true;
    api
      .getSession()
      .then((payload) => {
        if (!mounted) {
          return;
        }
        setSession(payload);
        setTelegramForm({
          telegram_token: payload.user.telegram_token ?? "",
          telegram_chat_id: payload.user.telegram_chat_id ?? "",
        });
      })
      .catch(() => {
        if (mounted) {
          setSession(null);
        }
      })
      .finally(() => {
        if (mounted) {
          setAuthLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (!session) {
      return;
    }
    void loadAll();
    const timer = window.setInterval(() => {
      void loadAll({ silent: true });
    }, 15000);
    return () => window.clearInterval(timer);
  }, [session?.user.id]);

  useEffect(() => {
    if (!selectedStrategyId && strategies.length > 0) {
      setSelectedStrategyId(strategies[0].id);
    }
  }, [selectedStrategyId, strategies]);

  useEffect(() => {
    if (domainStrategyInitialized || domainForm.zoneStrategyId || activeDomainStrategies.length === 0) {
      return;
    }
    const currentZone = domainForm.zone.trim().toLowerCase();
    const initialStrategy =
      activeDomainStrategies.find((item) => item.zone.toLowerCase() === currentZone) ?? activeDomainStrategies[0];
    selectDomainStrategy(String(initialStrategy.id));
    setDomainStrategyInitialized(true);
  }, [activeDomainStrategies, domainForm.zone, domainForm.zoneStrategyId, domainStrategyInitialized]);

  useEffect(() => {
    if (!selectedStrategyId || !session) {
      return;
    }
    void loadStrategyDetails(selectedStrategyId, previewDate);
  }, [selectedStrategyId, previewDate, session?.user.id]);

  useEffect(() => {
    setStrategyGandiForm({
      contactExtraParameters: selectedStrategy?.gandi_contact_extra_parameters ?? "",
      registrationExtraParameters: selectedStrategy?.gandi_registration_extra_parameters ?? "",
    });
  }, [selectedStrategy?.id, selectedStrategy?.gandi_contact_extra_parameters, selectedStrategy?.gandi_registration_extra_parameters]);

  useEffect(() => {
    const strategyId = parseNumber(domainForm.zoneStrategyId);
    if (!session || !strategyId || !domainForm.dropDate) {
      setDomainStrategyPreview(null);
      return;
    }
    let mounted = true;
    api
      .previewZoneStrategy(strategyId, domainForm.dropDate)
      .then((preview) => {
        if (mounted) {
          setDomainStrategyPreview(preview);
        }
      })
      .catch(() => {
        if (mounted) {
          setDomainStrategyPreview(null);
        }
      });
    return () => {
      mounted = false;
    };
  }, [domainForm.dropDate, domainForm.zoneStrategyId, session?.user.id]);

  useEffect(() => {
    if (!selectedOverrideDomainId && manualOverrideDomains.length > 0) {
      setSelectedOverrideDomainId(manualOverrideDomains[0].id);
      return;
    }
    if (selectedOverrideDomainId && !manualOverrideDomains.some((item) => item.id === selectedOverrideDomainId)) {
      setSelectedOverrideDomainId(manualOverrideDomains[0]?.id ?? null);
    }
  }, [manualOverrideDomains, selectedOverrideDomainId]);

  useEffect(() => {
    if (!session || !selectedOverrideDomainId) {
      setDomainOverrideSettings(null);
      setDomainOverrideRules([]);
      setDomainOverridePhases({});
      setDomainOverridePreview(null);
      return;
    }
    void loadDomainOverrideDetails(selectedOverrideDomainId, previewDate);
  }, [previewDate, selectedOverrideDomainId, session?.user.id]);

  async function loadAll(options?: { silent?: boolean }) {
    try {
      const [
        overviewData,
        strategiesData,
        domainsData,
        discoveryDomainsData,
        discoveryZoneStatsData,
        discoveryRuntimeSettingsData,
        allZonefilesSettingsData,
        zoneScanJobsData,
        zoneScanCandidatesData,
        workersData,
        workerMaintenanceJobsData,
        accountsData,
        contactsData,
        attacksData,
        tasksData,
        eventsData,
        diagnosticData,
      ] = await Promise.all([
        api.getOverview(),
        api.getZoneStrategies(),
        api.getDomains(),
        api.getDiscoveryDomains(),
        api.getDiscoveryZoneStats(),
        api.getDiscoveryRuntimeSettings(),
        api.getAllZonefilesSettings(),
        api.getZoneScanJobs(),
        api.getZoneScanCandidates(),
        api.getWorkers(),
        api.getWorkerMaintenanceJobs(),
        api.getRegistrarAccounts(),
        api.getContactProfiles(),
        api.getAttacks(),
        api.getTasks(),
        api.getEvents(),
        api.getDiagnosticTelegram(),
      ]);
      setOverview(overviewData);
      setStrategies(strategiesData);
      setDomains(domainsData);
      setDiscoveryDomains(discoveryDomainsData);
      setDiscoveryZoneStats(discoveryZoneStatsData);
      setDiscoveryRuntimeSettings(discoveryRuntimeSettingsData);
      setDiscoveryRuntimeForm(discoveryRuntimeSettingsToForm(discoveryRuntimeSettingsData));
      setAllZonefilesSettings(allZonefilesSettingsData);
      setZoneScanJobs(zoneScanJobsData);
      setZoneScanCandidates(zoneScanCandidatesData);
      setWorkers(workersData);
      setWorkerMaintenanceJobs(workerMaintenanceJobsData);
      setAccounts(accountsData);
      setContacts(contactsData);
      setAttacks(attacksData);
      setTasks(tasksData);
      setEvents(eventsData);
      setDiagnosticTelegram(diagnosticData);
    } catch (error) {
      if (!options?.silent) {
        setToast({ type: "error", text: error instanceof Error ? error.message : "Не удалось загрузить данные control-панели" });
      }
    }
  }

  async function loadStrategyDetails(strategyId: number, targetDate: string) {
    try {
      const rules = await api.getZoneRules(strategyId);
      setStrategyRules(rules);
      const phasesByRuleEntries = await Promise.all(
        rules.map(async (rule) => [rule.id, await api.getZoneRulePhases(rule.id)] as const),
      );
      setRulePhases(Object.fromEntries(phasesByRuleEntries));
      setStrategyPreview(await api.previewZoneStrategy(strategyId, targetDate));
      setPhaseForm((current) => ({
        ...current,
        ruleId: current.ruleId || (rules[0] ? String(rules[0].id) : ""),
      }));
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка загрузки деталей стратегии" });
    }
  }

  function syncDomainOverrideForm(domain: DropDomain, settings?: DomainOverrideSettings | null) {
    setDomainOverrideForm({
      timezoneName: settings?.timezone_name ?? domain.timezone_name,
      ruleResolutionMode: settings?.rule_resolution_mode ?? "priority",
      defaultMinGuaranteedRps: String(settings?.default_min_guaranteed_rps ?? domain.override_min_guaranteed_rps ?? 1),
      notes: settings?.notes ?? domain.notes ?? "",
    });
  }

  async function loadDomainOverrideDetails(domainId: number, targetDate: string) {
    const domain = domains.find((item) => item.id === domainId);
    if (!domain) {
      return;
    }
    try {
      const settings = await api.getDomainOverride(domainId);
      const rules = await api.getDomainOverrideRules(domainId);
      const phasesByRuleEntries = await Promise.all(
        rules.map(async (rule) => [rule.id, await api.getDomainOverrideRulePhases(rule.id)] as const),
      );
      const preview = await api.previewDomainOverride(domainId, targetDate);
      setDomainOverrideSettings(settings);
      setDomainOverrideRules(rules);
      setDomainOverridePhases(Object.fromEntries(phasesByRuleEntries));
      setDomainOverridePreview(preview);
      syncDomainOverrideForm(domain, settings);
      setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
      setDomainOverridePhaseForm((current) => ({
        ...DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM,
        ruleId: current.ruleId || (rules[0] ? String(rules[0].id) : ""),
      }));
    } catch (error) {
      if (error instanceof Error && error.message.includes("Domain override not found")) {
        setDomainOverrideSettings(null);
        setDomainOverrideRules([]);
        setDomainOverridePhases({});
        setDomainOverridePreview(null);
        syncDomainOverrideForm(domain, null);
        setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
        setDomainOverridePhaseForm(DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM);
        return;
      }
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка настроек override домена" });
    }
  }

  function selectDomainStrategy(strategyIdValue: string) {
    const strategyId = parseNumber(strategyIdValue);
    const strategy = strategyId ? strategies.find((item) => item.id === strategyId) : null;
    setDomainForm((current) => {
      if (!strategy) {
        return { ...current, zoneStrategyId: "", strategyMode: "inherit_zone" };
      }
      return {
        ...current,
        zone: strategy.zone,
        timezoneName: strategy.timezone_name,
        registrarSlug: strategy.default_registrar_slug || current.registrarSlug || "gandi",
        zoneStrategyId: String(strategy.id),
        strategyMode: "inherit_zone",
      };
    });
  }

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.login(loginForm);
      setSession(payload);
      setTelegramForm({
        telegram_token: payload.user.telegram_token ?? "",
        telegram_chat_id: payload.user.telegram_chat_id ?? "",
      });
      setToast({ type: "success", text: "Вход выполнен" });
      await loadAll();
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка входа" });
    }
  }

  async function logout() {
    await api.logout();
    setSession(null);
    setOverview(null);
    setStrategies([]);
    setDomains([]);
    setDiscoveryDomains([]);
    setDiscoveryZoneStats([]);
    setAllZonefilesSettings(null);
    setZoneScanJobs([]);
    setZoneScanCandidates([]);
    setSelectedZoneScanJobId(null);
    setWorkers([]);
    setAccounts([]);
    setContacts([]);
    setAttacks([]);
    setTasks([]);
    setEvents([]);
  }

  function renderAuth() {
    return (
      <div className="auth-shell">
        <section className="hero">
          <div>
            <p className="eyebrow">Control-сервер</p>
            <h1>Veltrix Drop Catcher</h1>
            <p className="subtitle">
              Панель управления для доменов с известной датой дропа, воркер-серверов, аккаунтов регистраторов и
              атакующих окон по времени реестра.
            </p>
          </div>
          <div className="stats">
            <article><span>Режим</span><strong>control</strong></article>
            <article><span>Стратегия</span><strong>приоритет</strong></article>
            <article><span>Зона по умолчанию</span><strong>.fr</strong></article>
            <article><span>Окно</span><strong>31:30 → 33:05</strong></article>
          </div>
        </section>

        <section className="auth-grid">
          <div className="auth-panel">
            <h2>Вход</h2>
            <form className="form" onSubmit={submitLogin}>
              <label>
                <span>Логин</span>
                <input value={loginForm.username} onChange={(event) => setLoginForm((current) => ({ ...current, username: event.target.value }))} />
              </label>
              <label>
                <span>Пароль</span>
                <input type="password" value={loginForm.password} onChange={(event) => setLoginForm((current) => ({ ...current, password: event.target.value }))} />
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={loginForm.remember_me} onChange={(event) => setLoginForm((current) => ({ ...current, remember_me: event.target.checked }))} />
                <span>Запомнить сессию</span>
              </label>
              <button type="submit">Войти</button>
            </form>
          </div>

          <div className="auth-panel">
            <h2>Регистрация</h2>
            <p className="muted">
              Окно оставлено видимым, но в текущем control-only проекте регистрация новых пользователей отключена.
            </p>
            <form className="form">
              <label><span>Логин</span><input disabled placeholder="отключено" /></label>
              <label><span>Пароль</span><input disabled placeholder="отключено" /></label>
              <button type="button" className="ghost" disabled>Регистрация отключена</button>
            </form>
          </div>
        </section>
      </div>
    );
  }

  async function submitDomains(event: FormEvent) {
    event.preventDefault();
    try {
      const payloadBase = {
        zone: domainForm.zone,
        timezone_name: domainForm.timezoneName,
        registrar_slug: domainForm.registrarSlug,
        zone_strategy_id: parseNumber(domainForm.zoneStrategyId),
        strategy_mode: domainForm.strategyMode,
        registrar_account_id: parseNumber(domainForm.registrarAccountId),
        contact_profile_id: parseNumber(domainForm.contactProfileId),
        drop_date: domainForm.dropDate,
        priority: Number(domainForm.priority),
        requested_duration_years: Number(domainForm.requestedDurationYears),
        registration_extra_parameters: domainForm.registrationExtraParameters || null,
        attack_enabled: domainForm.attackEnabled,
        auto_start_enabled: domainForm.autoStartEnabled,
        auto_start_lead_seconds: Number(domainForm.autoStartLeadSeconds),
        override_min_guaranteed_rps: parseNumber(domainForm.overrideMinGuaranteedRps),
        window_start_minute: Number(domainForm.windowStartMinute),
        window_start_second: Number(domainForm.windowStartSecond),
        window_duration_seconds: Number(domainForm.windowDurationSeconds),
        notes: domainForm.notes || null,
      };
      const result = importFile
        ? await api.importDomains(importFile, payloadBase)
        : await api.createDomain({ domains: splitDomains(domainForm.domainsText), ...payloadBase });
      setDomainForm(DEFAULT_DOMAIN_FORM);
      setDomainStrategyInitialized(false);
      setImportFile(null);
      await loadAll();
      setToast({
        type: "success",
        text: `Добавлено: ${result.inserted.length}${result.skipped.length ? `, пропущено: ${result.skipped.join(", ")}` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления доменов" });
    }
  }

  async function submitDiscoveryDomains(event: FormEvent) {
    event.preventDefault();
    try {
      const result = await api.importDiscoveryDomains({
        domains: splitDomains(discoveryForm.domainsText),
        zone: discoveryForm.zone || null,
        check_interval_seconds: Number(discoveryForm.checkIntervalSeconds),
        source_mode: discoveryForm.sourceMode,
        drop_prediction_enabled: !discoveryForm.disableDropPrediction,
        notes: discoveryForm.notes || null,
      });
      setDiscoveryForm(DEFAULT_DISCOVERY_FORM);
      await loadAll();
      setToast({
        type: "success",
        text: `Discovery добавлено: ${result.inserted.length}${result.skipped.length ? `, пропущено: ${result.skipped.join(", ")}` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления discovery-доменов" });
    }
  }

  async function submitDiscoveryObservation(event: FormEvent) {
    event.preventDefault();
    const domainId = Number(discoveryObservationForm.domainId);
    if (!domainId) {
      setToast({ type: "error", text: "Выбери discovery-домен" });
      return;
    }
    try {
      await api.createDiscoveryObservation(domainId, {
        source: "manual",
        http_status: parseNumber(discoveryObservationForm.httpStatus),
        lifecycle_stage: discoveryObservationForm.lifecycleStage || null,
        availability_status: discoveryObservationForm.availabilityStatus || null,
        status_codes: splitDomains(discoveryObservationForm.statusCodes),
        raw_response: discoveryObservationForm.rawResponse || null,
        error: discoveryObservationForm.error || null,
      });
      await loadAll();
      if (selectedDiscoveryDomainId === domainId) {
        await loadDiscoveryTimeline(domainId);
      }
      setToast({ type: "success", text: "Наблюдение discovery сохранено" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения observation" });
    }
  }

  async function checkDiscoveryDomainNow(domainId: number) {
    try {
      const domain = await api.checkDiscoveryDomain(domainId);
      await loadAll();
      if (selectedDiscoveryDomainId === domainId) {
        await loadDiscoveryTimeline(domainId);
      }
      setToast({ type: "success", text: `Проверка discovery: ${domain.fqdn} -> ${formatStatusLabel(domain.status)}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка проверки discovery-домена" });
    }
  }

  async function updateDiscoveryInterval(scope: "filtered" | "all") {
    const checkIntervalSeconds = Number(discoveryBulkIntervalSeconds);
    if (!Number.isFinite(checkIntervalSeconds) || checkIntervalSeconds < 10 || checkIntervalSeconds > 86400) {
      setToast({ type: "error", text: "Интервал должен быть от 10 до 86400 секунд" });
      return;
    }
    const targetDomains = scope === "filtered" ? filteredDiscoveryDomains : discoveryDomains;
    const domainIds = targetDomains.map((domain) => domain.id);
    if (domainIds.length === 0) {
      setToast({ type: "error", text: "Нет доменов для обновления интервала" });
      return;
    }
    try {
      const result = await api.updateDiscoveryDomainsInterval({
        domain_ids: domainIds,
        check_interval_seconds: checkIntervalSeconds,
        reschedule_pending: true,
      });
      await loadAll();
      setToast({
        type: "success",
        text: `Базовый интервал ${result.check_interval_seconds} сек применен к доменам: ${result.updated}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления интервала discovery" });
    }
  }

  async function loadDiscoveryTimeline(domainId: number) {
    try {
      const observations = await api.getDiscoveryObservations(domainId);
      setSelectedDiscoveryDomainId(domainId);
      setDiscoveryObservations(observations);
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка загрузки истории discovery" });
    }
  }

  async function saveAllZonefilesToken(event: FormEvent) {
    event.preventDefault();
    try {
      const settings = await api.updateAllZonefilesSettings({ api_token: allZonefilesTokenForm || null });
      setAllZonefilesSettings(settings);
      setAllZonefilesTokenForm("");
      setToast({ type: "success", text: settings.configured ? "Токен AllZonefiles сохранен" : "Токен AllZonefiles очищен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения токена AllZonefiles" });
    }
  }

  async function testAllZonefilesToken() {
    try {
      const result = await api.testAllZonefilesSettings();
      setToast({
        type: result.ok ? "success" : "error",
        text: `${result.message}${result.zones_count !== null ? `; zones=${result.zones_count}` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка проверки AllZonefiles" });
    }
  }

  async function saveDiscoveryRuntimeSettings(event: FormEvent) {
    event.preventDefault();
    const payload = {
      discovery_enabled: discoveryRuntimeForm.discoveryEnabled,
      discovery_worker_enabled: discoveryRuntimeForm.discoveryWorkerEnabled,
      discovery_local_fallback_enabled: discoveryRuntimeForm.discoveryLocalFallbackEnabled,
      discovery_scheduler_interval_seconds: Number(discoveryRuntimeForm.discoverySchedulerIntervalSeconds),
      discovery_batch_size: Number(discoveryRuntimeForm.discoveryBatchSize),
      discovery_concurrency: Number(discoveryRuntimeForm.discoveryConcurrency),
      discovery_timeout_seconds: Number(discoveryRuntimeForm.discoveryTimeoutSeconds),
      discovery_worker_task_stale_seconds: Number(discoveryRuntimeForm.discoveryWorkerTaskStaleSeconds),
      worker_discovery_concurrency: Number(discoveryRuntimeForm.workerDiscoveryConcurrency),
      worker_discovery_poll_interval_seconds: Number(discoveryRuntimeForm.workerDiscoveryPollIntervalSeconds),
    };
    if (Object.values(payload).some((value) => typeof value === "number" && !Number.isFinite(value))) {
      setToast({ type: "error", text: "В настройках discovery есть некорректное число" });
      return;
    }
    try {
      const settings = await api.updateDiscoveryRuntimeSettings(payload);
      setDiscoveryRuntimeSettings(settings);
      setDiscoveryRuntimeForm(discoveryRuntimeSettingsToForm(settings));
      setToast({ type: "success", text: "Настройки discovery сохранены" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения discovery-настроек" });
    }
  }

  async function submitZoneScanJob(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createZoneScanJob(buildZoneScanPayload());
      await loadAll();
      setToast({ type: "success", text: "Задача сканирования создана; сервер начнет выполнение автоматически" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка запуска сканирования зоны" });
    }
  }

  function buildZoneScanPayload(overrides: Record<string, unknown> = {}) {
    return {
      zone: zoneScanForm.zone,
      source_type: zoneScanForm.sourceType,
      source_date: zoneScanForm.sourceDate || null,
      min_score: Number(zoneScanForm.minScore),
      limit_output: Number(zoneScanForm.limitOutput),
      max_rdap_checks: Number(zoneScanForm.maxRdapChecks),
      concurrency: Number(zoneScanForm.concurrency),
      rdap_timeout_seconds: Number(zoneScanForm.rdapTimeoutSeconds),
      pending_delete_min_days: zoneScanForm.pendingDeleteMinDays === "" ? null : Number(zoneScanForm.pendingDeleteMinDays),
      pending_delete_max_days: zoneScanForm.pendingDeleteMaxDays === "" ? null : Number(zoneScanForm.pendingDeleteMaxDays),
      reservoir_size: Number(zoneScanForm.reservoirSize),
      random_seed: Number(zoneScanForm.randomSeed),
      keep_file: zoneScanForm.keepFile,
      ...overrides,
    };
  }

  async function createPendingWindowProbeJobs() {
    try {
      await Promise.all([
        api.createZoneScanJob(buildZoneScanPayload({
          source_type: "zone_historic",
          source_date: formatUtcDateDaysAgo(29),
          pending_delete_min_days: 1,
          pending_delete_max_days: 2,
          random_seed: Number(zoneScanForm.randomSeed),
          keep_file: false,
        })),
        api.createZoneScanJob(buildZoneScanPayload({
          source_type: "zone_historic",
          source_date: formatUtcDateDaysAgo(28),
          pending_delete_min_days: 1,
          pending_delete_max_days: 2,
          random_seed: Number(zoneScanForm.randomSeed) + 1,
          keep_file: false,
        })),
      ]);
      await loadAll();
      setToast({ type: "success", text: "Созданы 2 scan job для pendingDelete через 1-2 дня" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка запуска поиска pendingDelete" });
    }
  }

  async function refreshZoneScanner(jobId: number | null = selectedZoneScanJobId) {
    try {
      const [jobs, candidates] = await Promise.all([api.getZoneScanJobs(), api.getZoneScanCandidates(jobId)]);
      setZoneScanJobs(jobs);
      setZoneScanCandidates(candidates);
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления scanner" });
    }
  }

  async function cancelZoneScanJob(jobId: number) {
    try {
      await api.cancelZoneScanJob(jobId);
      await refreshZoneScanner();
      setToast({ type: "success", text: `Задача #${jobId} остановлена` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка остановки задачи" });
    }
  }

  async function deleteZoneScanFile(jobId: number) {
    try {
      await api.deleteZoneScanJobFile(jobId);
      await refreshZoneScanner();
      setToast({ type: "success", text: `Файл задачи #${jobId} удален` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления файла" });
    }
  }

  async function deleteZoneScanJob(jobId: number) {
    try {
      await api.deleteZoneScanJob(jobId);
      if (selectedZoneScanJobId === jobId) {
        setSelectedZoneScanJobId(null);
      }
      await refreshZoneScanner(null);
      setToast({ type: "success", text: `Job #${jobId} удален` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления задачи" });
    }
  }

  async function selectZoneScanJob(jobId: number | null) {
    setSelectedZoneScanJobId(jobId);
    await refreshZoneScanner(jobId);
  }

  async function addZoneScanCandidateToDiscovery(candidateId: number) {
    try {
      const domain = await api.addZoneScanCandidateToDiscovery(candidateId);
      await loadAll();
      setToast({ type: "success", text: `${domain.fqdn} добавлен в Discovery` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления в Discovery" });
    }
  }

  async function ignoreZoneScanCandidate(candidateId: number) {
    try {
      await api.ignoreZoneScanCandidate(candidateId);
      await refreshZoneScanner();
      setToast({ type: "success", text: "Кандидат скрыт" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка скрытия кандидата" });
    }
  }

  async function submitStrategy(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createZoneStrategy({
        zone: strategyForm.zone,
        name: strategyForm.name,
        timezone_name: strategyForm.timezoneName,
        rule_resolution_mode: strategyForm.ruleResolutionMode,
        default_min_guaranteed_rps: Number(strategyForm.defaultMinGuaranteedRps),
        default_registrar_slug: strategyForm.defaultRegistrarSlug,
        gandi_contact_extra_parameters: strategyForm.gandiContactExtraParameters || null,
        gandi_registration_extra_parameters: strategyForm.gandiRegistrationExtraParameters || null,
        is_active: strategyForm.isActive,
        notes: strategyForm.notes || null,
      });
      setStrategyForm(DEFAULT_STRATEGY_FORM);
      await loadAll();
      setToast({ type: "success", text: "Стратегия зоны добавлена" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка стратегии зоны" });
    }
  }

  async function saveSelectedStrategyGandiParameters(event: FormEvent) {
    event.preventDefault();
    if (!selectedStrategy) {
      setToast({ type: "error", text: "Сначала выбери стратегию зоны" });
      return;
    }
    try {
      const updated = await api.updateZoneStrategy(selectedStrategy.id, {
        gandi_contact_extra_parameters: strategyGandiForm.contactExtraParameters || null,
        gandi_registration_extra_parameters: strategyGandiForm.registrationExtraParameters || null,
      });
      await loadAll({ silent: true });
      setSelectedStrategyId(updated.id);
      setToast({ type: "success", text: `Gandi параметры .${updated.zone} сохранены` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения Gandi параметров" });
    }
  }

  async function createPresetStrategy(zone: string) {
    try {
      const strategy = await api.createZoneStrategyPreset(zone);
      await loadAll();
      setSelectedStrategyId(strategy.id);
      await loadStrategyDetails(strategy.id, previewDate);
      setToast({ type: "success", text: `Стандартная стратегия .${strategy.zone} готова` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка создания пресета" });
    }
  }

  async function submitRule(event: FormEvent) {
    event.preventDefault();
    if (!selectedStrategyId) {
      setToast({ type: "error", text: "Сначала выбери стратегию зоны" });
      return;
    }
    try {
      await api.createZoneRule(selectedStrategyId, {
        name: ruleForm.name,
        schedule_type: ruleForm.scheduleType,
        hour: parseNumber(ruleForm.hour),
        minute: Number(ruleForm.minute),
        second: Number(ruleForm.second),
        weekdays: ruleForm.weekdays || null,
        specific_date: ruleForm.specificDate || null,
        window_duration_seconds: Number(ruleForm.windowDurationSeconds),
        priority: Number(ruleForm.priority),
        execution_profile_mode: ruleForm.executionProfileMode,
        is_enabled: ruleForm.isEnabled,
        notes: ruleForm.notes || null,
      });
      setRuleForm(DEFAULT_RULE_FORM);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: "Окно зоны добавлено" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка окна зоны" });
    }
  }

  async function submitPhase(event: FormEvent) {
    event.preventDefault();
    const ruleId = parseNumber(phaseForm.ruleId);
    if (!ruleId || !selectedStrategyId) {
      setToast({ type: "error", text: "Выбери окно для фазы" });
      return;
    }
    try {
      await api.createZoneRulePhase(ruleId, {
        name: phaseForm.name,
        sort_order: Number(phaseForm.sortOrder),
        start_offset_seconds: Number(phaseForm.startOffsetSeconds),
        duration_seconds: Number(phaseForm.durationSeconds),
        rps_mode: phaseForm.rpsMode,
        rps_value: Number(phaseForm.rpsValue),
        stop_on_success: phaseForm.stopOnSuccess,
      });
      setPhaseForm((current) => ({ ...DEFAULT_PHASE_FORM, ruleId: current.ruleId }));
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: "Фаза зоны добавлена" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка фазы зоны" });
    }
  }

  async function removeZoneRule(ruleId: number) {
    if (!selectedStrategyId) {
      return;
    }
    try {
      const payload = await api.deleteZoneRule(ruleId);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления окна зоны" });
    }
  }

  async function removeZonePhase(phaseId: number) {
    if (!selectedStrategyId) {
      return;
    }
    try {
      const payload = await api.deleteZoneRulePhase(phaseId);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления фазы зоны" });
    }
  }

  async function saveDomainOverrideSettings(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для ручного override" });
      return;
    }
    try {
      await api.updateDomainOverride(selectedOverrideDomain.id, {
        timezone_name: domainOverrideForm.timezoneName,
        rule_resolution_mode: domainOverrideForm.ruleResolutionMode,
        default_min_guaranteed_rps: Number(domainOverrideForm.defaultMinGuaranteedRps),
        notes: domainOverrideForm.notes || null,
      });
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Настройки override домена сохранены" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка настроек override домена" });
    }
  }

  async function submitDomainOverrideRule(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для override-окна" });
      return;
    }
    try {
      await api.createDomainOverrideRule(selectedOverrideDomain.id, {
        name: domainOverrideRuleForm.name,
        schedule_type: domainOverrideRuleForm.scheduleType,
        hour: parseNumber(domainOverrideRuleForm.hour),
        minute: Number(domainOverrideRuleForm.minute),
        second: Number(domainOverrideRuleForm.second),
        weekdays: domainOverrideRuleForm.weekdays || null,
        specific_date: domainOverrideRuleForm.specificDate || null,
        window_duration_seconds: Number(domainOverrideRuleForm.windowDurationSeconds),
        priority: Number(domainOverrideRuleForm.priority),
        execution_profile_mode: domainOverrideRuleForm.executionProfileMode,
        is_enabled: domainOverrideRuleForm.isEnabled,
        notes: domainOverrideRuleForm.notes || null,
      });
      setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Override-окно домена добавлено" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка override-окна домена" });
    }
  }

  async function submitDomainOverridePhase(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для override-фазы" });
      return;
    }
    const ruleId = parseNumber(domainOverridePhaseForm.ruleId);
    if (!ruleId) {
      setToast({ type: "error", text: "Выбери override-окно для фазы" });
      return;
    }
    try {
      await api.createDomainOverrideRulePhase(ruleId, {
        name: domainOverridePhaseForm.name,
        sort_order: Number(domainOverridePhaseForm.sortOrder),
        start_offset_seconds: Number(domainOverridePhaseForm.startOffsetSeconds),
        duration_seconds: Number(domainOverridePhaseForm.durationSeconds),
        rps_mode: domainOverridePhaseForm.rpsMode,
        rps_value: Number(domainOverridePhaseForm.rpsValue),
        stop_on_success: domainOverridePhaseForm.stopOnSuccess,
      });
      setDomainOverridePhaseForm((current) => ({
        ...DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM,
        ruleId: current.ruleId,
      }));
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Override-фаза домена добавлена" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка override-фазы домена" });
    }
  }

  async function removeDomainOverrideRule(ruleId: number) {
    if (!selectedOverrideDomain) {
      return;
    }
    try {
      const payload = await api.deleteDomainOverrideRule(ruleId);
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления override-окна" });
    }
  }

  async function removeDomainOverridePhase(phaseId: number) {
    if (!selectedOverrideDomain) {
      return;
    }
    try {
      const payload = await api.deleteDomainOverrideRulePhase(phaseId);
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления override-фазы" });
    }
  }

  async function submitWorker(event: FormEvent) {
    event.preventDefault();
    const payload: Record<string, unknown> = {
      name: workerForm.name,
      registrar_slug: workerForm.registrarSlug,
      assigned_registrar_account_id: parseNumber(workerForm.assignedRegistrarAccountId),
      ip_address: workerForm.ipAddress || null,
      region: workerForm.region || null,
      max_rps: Number(workerForm.maxRps),
      target_rps: Number(workerForm.targetRps),
      ssh_host: workerForm.sshHost || workerForm.ipAddress || null,
      ssh_port: Number(workerForm.sshPort || 22),
      ssh_username: workerForm.sshUsername || "root",
      ssh_key_path: workerForm.sshKeyPath || null,
      notes: workerForm.notes || null,
    };
    if (workerForm.sshPassword.trim()) {
      payload.ssh_password = workerForm.sshPassword;
    }
    try {
      if (editingWorkerId) {
        await api.updateWorker(editingWorkerId, payload);
      } else {
        await api.createWorker(payload);
      }
      setWorkerForm(makeWorkerForm());
      setEditingWorkerId(null);
      await loadAll();
      setToast({ type: "success", text: editingWorkerId ? "Воркер обновлен" : "Воркер добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : editingWorkerId ? "Ошибка обновления воркера" : "Ошибка добавления воркера" });
    }
  }

  function startEditWorker(worker: WorkerNode) {
    setEditingWorkerId(worker.id);
    setWorkerForm(makeWorkerForm(worker));
  }

  function resetWorkerForm() {
    setEditingWorkerId(null);
    setWorkerForm(makeWorkerForm());
  }

  async function loadWorkerSetup(worker: WorkerNode, options?: { mode?: "test" | "live"; runtimeUrl?: string }) {
    const mode = options?.mode ?? workerSetupMode;
    const runtimeUrl = options?.runtimeUrl ?? workerSetupRuntimeUrl;
    setWorkerSetupLoading(true);
    try {
      const payload = await api.getWorkerSetup(worker.id, {
        simulate_mode: mode === "test",
        runtime_base_url: runtimeUrl && runtimeUrl !== DEFAULT_WORKER_RUNTIME_BASE_URL ? runtimeUrl : null,
      });
      setWorkerSetup(payload);
      setWorkerSetupMode(payload.simulate_mode ? "test" : "live");
      setWorkerSetupRuntimeUrl(payload.runtime_base_url);
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка генерации команд воркера" });
    } finally {
      setWorkerSetupLoading(false);
    }
  }

  async function copyWorkerCommands(commands: string[], label: string) {
    try {
      await navigator.clipboard.writeText(commands.join("\n"));
      setToast({ type: "success", text: `${label}: команды скопированы` });
    } catch {
      setToast({ type: "error", text: "Не удалось скопировать команды" });
    }
  }

  async function submitAccount(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createRegistrarAccount({
        name: accountForm.name,
        registrar_slug: accountForm.registrarSlug,
        api_token: accountForm.apiToken || null,
        api_base_url: accountForm.apiBaseUrl || null,
        sharing_id: accountForm.sharingId || null,
        default_contact_profile_id: parseNumber(accountForm.defaultContactProfileId),
        supports_dry_run: accountForm.supportsDryRun,
        is_active: accountForm.isActive,
        notes: accountForm.notes || null,
      });
      setAccountForm(DEFAULT_ACCOUNT_FORM);
      await loadAll();
      setToast({ type: "success", text: "Аккаунт регистратора добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления аккаунта" });
    }
  }

  async function submitContact(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = {
        label: contactForm.label,
        person_type: contactForm.personType,
        given_name: contactForm.givenName,
        family_name: contactForm.familyName,
        organization_name: contactForm.organizationName || null,
        email: contactForm.email,
        phone: contactForm.phone,
        mobile: contactForm.mobile || null,
        fax: contactForm.fax || null,
        lang: contactForm.lang || null,
        street_address: contactForm.streetAddress,
        city: contactForm.city,
        state: contactForm.state || null,
        zip_code: contactForm.zipCode,
        country_code: contactForm.countryCode,
        data_obfuscated: contactForm.dataObfuscated,
        mail_obfuscated: contactForm.mailObfuscated,
        icann_contract_accept: contactForm.icannContractAccept,
        extra_parameters: contactForm.extraParameters || null,
        is_default: contactForm.isDefault,
        notes: contactForm.notes || null,
      };
      if (editingContactId) {
        await api.updateContactProfile(editingContactId, payload);
      } else {
        await api.createContactProfile(payload);
      }
      setContactForm(DEFAULT_CONTACT_FORM);
      setEditingContactId(null);
      await loadAll();
      setToast({ type: "success", text: editingContactId ? "Профиль контакта обновлен" : "Профиль контакта добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения профиля контакта" });
    }
  }

  async function saveTelegram(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.updateTelegram(telegramForm);
      setSession(payload);
      setToast({ type: "success", text: "Личный Telegram обновлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения Telegram" });
    }
  }

  async function saveDiagnosticTelegram(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.updateDiagnosticTelegram(diagnosticTelegram);
      setDiagnosticTelegram(payload);
      setToast({ type: "success", text: "Диагностический Telegram обновлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения диагностического Telegram" });
    }
  }

  async function savePassword(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.changePassword(passwordForm);
      setSession(payload);
      setPasswordForm({ current_password: "", new_password: "" });
      setToast({ type: "success", text: "Пароль изменен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка смены пароля" });
    }
  }

  async function startTodayAttacks(force_rebuild = false) {
    try {
      const payload = await api.startAttacks({ force_rebuild });
      await loadAll();
      setToast({ type: "success", text: `Спланировано атак: ${payload.length}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка старта атак" });
    }
  }

  async function startDomainAttack(domainId: number) {
    try {
      await api.startAttacks({ domain_ids: [domainId], force_rebuild: true });
      await loadAll();
      setToast({ type: "success", text: "Атака по домену перепланирована" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка старта атаки" });
    }
  }

  async function simulateDomainRegistration(domainId: number) {
    try {
      const payload = await api.simulateRegistration({
        domain_ids: [domainId],
        duration_seconds: 95,
        force_rebuild: true,
      });
      await loadAll();
      setToast({ type: "success", text: `Запущена симуляция регистрации: ${payload.length}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка симуляции регистрации" });
    }
  }

  async function stopAllAttacks() {
    try {
      const payload = await api.stopAttacks({ reason: "Остановлено из панели" });
      await loadAll();
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка остановки атак" });
    }
  }

  async function rebalanceAttacksNow() {
    try {
      const payload = await api.rebalanceAttacks();
      await loadAll();
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка перераспределения" });
    }
  }

  async function toggleDomain(domain: DropDomain) {
    try {
      await api.updateDomain(domain.id, {
        attack_enabled: !domain.attack_enabled,
        status: !domain.attack_enabled ? "queued" : "paused",
      });
      await loadAll();
      setToast({ type: "success", text: `Домен ${!domain.attack_enabled ? "включен" : "поставлен на паузу"}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления домена" });
    }
  }

  async function toggleDomainAutoStart(domain: DropDomain) {
    try {
      await api.updateDomain(domain.id, {
        auto_start_enabled: !domain.auto_start_enabled,
        auto_start_lead_seconds: domain.auto_start_lead_seconds || 90,
      });
      await loadAll();
      setToast({ type: "success", text: `Автостарт ${!domain.auto_start_enabled ? "включен" : "выключен"}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка автостарта" });
    }
  }

  async function toggleWorker(worker: WorkerNode) {
    try {
      await api.updateWorker(worker.id, {
        is_enabled: !worker.is_enabled,
        status: !worker.is_enabled ? "ready" : "disabled",
      });
      await loadAll();
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления воркера" });
    }
  }

  async function startWorkerMaintenance(worker: WorkerNode, action: "check" | "install" | "update") {
    try {
      const job = action === "check"
        ? await api.checkWorkerSsh(worker.id)
        : action === "install"
          ? await api.installWorkerServer(worker.id)
          : await api.updateWorkerServer(worker.id);
      await loadAll();
      setToast({
        type: "success",
        text: `${action === "check" ? "SSH-проверка" : action === "install" ? "Установка воркера" : "Обновление воркера"} запущено: job #${job.id}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обслуживания воркера" });
    }
  }

  async function startAllWorkerUpdates() {
    try {
      const result = await api.updateAllWorkerServers();
      await loadAll();
      setToast({
        type: "success",
        text: `Массовое обновление запущено: ${result.started_count}; пропущено: ${result.skipped_count}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка массового обновления воркеров" });
    }
  }

  async function validateAccount(id: number) {
    try {
      const payload = await api.validateRegistrarAccount(id);
      await loadAll();
      setToast({
        type: payload.last_validation_status === "ready" ? "success" : "error",
        text: payload.last_validation_message ?? "Проверка завершена",
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка проверки аккаунта" });
    }
  }

  async function prefillContactFromAccount(account: RegistrarAccount) {
    try {
      const payload = await api.prefillContactFromRegistrarAccount(account.id);
      applyPrefilledContact(payload);
      setTab("contacts");
      setToast({ type: "success", text: `Черновик контакта импортирован из ${account.name}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка prefill из Gandi" });
    }
  }

  async function dryRunDomain(domain: DropDomain) {
    try {
      const payload = await api.dryRunDomain(domain.id);
      await loadAll();
      setToast({
        type: payload.status === "ready" ? "success" : "error",
        text: `Dry-run ${domain.fqdn}: ${payload.status}${payload.http_status ? ` (HTTP ${payload.http_status})` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка dry-run проверки домена" });
    }
  }

  async function dryRunReadyDueTodayDomains() {
    try {
      const payload = await api.dryRunDomainsBatch({ due_today_only: true, only_ready: true });
      await loadAll();
      setToast({
        type: payload.error === 0 && payload.invalid === 0 ? "success" : "error",
        text: `Dry run batch: total ${payload.total}, ready ${payload.ready}, invalid ${payload.invalid}, error ${payload.error}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка batch dry-run" });
    }
  }

  async function deleteItem(kind: "domain" | "discovery" | "worker" | "account" | "contact", id: number) {
    try {
      if (kind === "domain") {
        await api.deleteDomain(id);
      }
      if (kind === "discovery") {
        await api.deleteDiscoveryDomain(id);
      }
      if (kind === "worker") {
        await api.deleteWorker(id);
      }
      if (kind === "account") {
        await api.deleteRegistrarAccount(id);
      }
      if (kind === "contact") {
        await api.deleteContactProfile(id);
      }
      await loadAll();
      setToast({ type: "success", text: "Удалено" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления" });
    }
  }

  function formatDomainReadiness(domain: DropDomain) {
    if (!domain.readiness_reasons) {
      return "готово";
    }
    const translations: Record<string, string> = {
      "strategy is missing": "нет стратегии для зоны",
      "strategy rules are missing": "у стратегии нет окон дропа",
      "registrar account is missing": "не выбран аккаунт регистратора",
      "contact profile is missing": "не выбран контакт",
      "drop date is missing": "не указана дата дропа",
      "contact extra parameter x-se_ident_number is missing": "для .se/.nu/.fi в контакте нужен x-se_ident_number",
    };
    return domain.readiness_reasons
      .split(";")
      .map((reason) => translations[reason.trim()] ?? reason.trim())
      .filter(Boolean)
      .join("; ");
  }

  function applyPrefilledContact(payload: ContactProfilePrefill) {
    setContactForm({
      label: payload.label,
      personType: payload.person_type,
      givenName: payload.given_name,
      familyName: payload.family_name,
      organizationName: payload.organization_name ?? "",
      email: payload.email,
      phone: payload.phone,
      mobile: payload.mobile ?? "",
      fax: payload.fax ?? "",
      lang: payload.lang ?? "fr",
      streetAddress: payload.street_address,
      city: payload.city,
      state: payload.state ?? "",
      zipCode: payload.zip_code,
      countryCode: payload.country_code,
      dataObfuscated: payload.data_obfuscated ?? false,
      mailObfuscated: payload.mail_obfuscated ?? false,
      icannContractAccept: payload.icann_contract_accept ?? true,
      extraParameters: payload.extra_parameters ?? "",
      isDefault: payload.is_default,
      notes: payload.notes ?? "",
    });
  }

  function editContact(contact: ContactProfile) {
    setEditingContactId(contact.id);
    setContactForm({
      label: contact.label,
      personType: contact.person_type,
      givenName: contact.given_name,
      familyName: contact.family_name,
      organizationName: contact.organization_name ?? "",
      email: contact.email,
      phone: contact.phone,
      mobile: contact.mobile ?? "",
      fax: contact.fax ?? "",
      lang: contact.lang ?? "",
      streetAddress: contact.street_address,
      city: contact.city,
      state: contact.state ?? "",
      zipCode: contact.zip_code,
      countryCode: contact.country_code,
      dataObfuscated: Boolean(contact.data_obfuscated),
      mailObfuscated: Boolean(contact.mail_obfuscated),
      icannContractAccept: Boolean(contact.icann_contract_accept),
      extraParameters: contact.extra_parameters ?? "",
      isDefault: contact.is_default,
      notes: contact.notes ?? "",
    });
    setTab("contacts");
  }

  function renderDiscovery() {
    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Аналитика дропа</h2>
              <p className="muted">
                Это аналитический контур: он фиксирует lifecycle/availability, считает вероятное окно дропа и не запускает регистрацию.
              </p>
            </div>
            <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          </div>
          <form className="form" onSubmit={submitDiscoveryDomains}>
            <label>
              <span>Домены для наблюдения</span>
              <textarea
                rows={6}
                value={discoveryForm.domainsText}
                onChange={(event) => setDiscoveryForm((current) => ({ ...current, domainsText: event.target.value }))}
                placeholder="example.com&#10;example.net&#10;example.org"
              />
            </label>
            <div className="form two-columns">
              <label><span>Зона вручную</span><input value={discoveryForm.zone} onChange={(event) => setDiscoveryForm((current) => ({ ...current, zone: event.target.value }))} placeholder="пусто = взять из домена" /></label>
              <label><span>Базовый интервал, сек</span><input value={discoveryForm.checkIntervalSeconds} onChange={(event) => setDiscoveryForm((current) => ({ ...current, checkIntervalSeconds: event.target.value }))} /></label>
              <label><span>Источник проверки</span><input value={discoveryForm.sourceMode} onChange={(event) => setDiscoveryForm((current) => ({ ...current, sourceMode: event.target.value }))} /></label>
              <label><span>Заметки</span><input value={discoveryForm.notes} onChange={(event) => setDiscoveryForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            </div>
            <label className="checkline">
              <input
                type="checkbox"
                checked={discoveryForm.disableDropPrediction}
                onChange={(event) => setDiscoveryForm((current) => ({ ...current, disableDropPrediction: event.target.checked }))}
              />
              <span>Не рассчитывать прогноз дропа</span>
            </label>
            <button type="submit">Добавить в аналитику</button>
          </form>
        </div>

        <div className="card">
          <h2>Ручное наблюдение</h2>
          <p className="muted">Для первичного теста можно вручную зафиксировать RDAP/EPP статус. Автоматический чекер будет следующим слоем.</p>
          <form className="form" onSubmit={submitDiscoveryObservation}>
            <label>
              <span>Домен discovery</span>
              <select value={discoveryObservationForm.domainId} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, domainId: event.target.value }))}>
                <option value="">Выбери домен</option>
                {discoveryDomains.map((domain) => <option key={domain.id} value={domain.id}>{domain.fqdn} | {formatStatusLabel(domain.status)}</option>)}
              </select>
            </label>
            <div className="form two-columns">
              <label>
                <span>Стадия lifecycle</span>
                <select value={discoveryObservationForm.lifecycleStage} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, lifecycleStage: event.target.value }))}>
                  <option value="registered">зарегистрирован</option>
                  <option value="redemption">redemption</option>
                  <option value="pending_delete">pending_delete</option>
                  <option value="not_found">не найден</option>
                  <option value="unknown">неизвестно</option>
                </select>
              </label>
              <label>
                <span>Доступность</span>
                <select value={discoveryObservationForm.availabilityStatus} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, availabilityStatus: event.target.value }))}>
                  <option value="">неизвестно</option>
                  <option value="taken">занят</option>
                  <option value="available">доступен</option>
                </select>
              </label>
              <label><span>HTTP статус</span><input value={discoveryObservationForm.httpStatus} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, httpStatus: event.target.value }))} /></label>
              <label><span>Коды статуса</span><input value={discoveryObservationForm.statusCodes} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, statusCodes: event.target.value }))} placeholder="pendingDelete redemptionPeriod" /></label>
            </div>
            <label><span>Сырой ответ / заметка</span><textarea rows={3} value={discoveryObservationForm.rawResponse} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, rawResponse: event.target.value }))} /></label>
            <label><span>Ошибка</span><input value={discoveryObservationForm.error} onChange={(event) => setDiscoveryObservationForm((current) => ({ ...current, error: event.target.value }))} /></label>
            <button type="submit">Сохранить наблюдение</button>
          </form>
        </div>

        <div className="card full-span">
          <h2>Сводка по зонам</h2>
          <div className="key-value compact">
            {discoveryZoneStats.length === 0 ? <div><span>Нет данных</span><strong>0</strong></div> : null}
            {discoveryZoneStats.map((item) => (
              <div key={item.zone}>
                <span>
                  .{item.zone} {item.has_strategy_pattern ? <span className="status available">✓ паттерн</span> : null}
                </span>
                <strong>{item.total} всего | {item.pending_delete} pending | {item.predicted} с прогнозом | {item.available} доступно</strong>
              </div>
            ))}
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>История discovery</h2>
              <p className="muted">
                История RDAP/manual observations по выбранному домену. Это основной журнал для вычисления перехода redemption {"->"} pendingDelete {"->"} available.
              </p>
            </div>
            {selectedDiscoveryDomain ? (
              <button type="button" className="ghost" onClick={() => void loadDiscoveryTimeline(selectedDiscoveryDomain.id)}>
                Обновить историю
              </button>
            ) : null}
          </div>
          {selectedDiscoveryDomain ? (
            <>
              <div className="key-value compact timeline-summary">
                <div>
                  <span>Домен</span>
                  <strong>{selectedDiscoveryDomain.fqdn}</strong>
                </div>
                <div>
                  <span>Текущий статус</span>
                  <strong>{formatStatusLabel(selectedDiscoveryDomain.status)} | {formatLifecycleLabel(selectedDiscoveryDomain.last_lifecycle_stage)}</strong>
                </div>
                <div>
                  <span>Автопрогноз</span>
                  <strong>{selectedDiscoveryDomain.drop_prediction_enabled ? "включен" : "выключен"}</strong>
                </div>
                <div>
                  <span>От какой даты считаем</span>
                  <strong>{formatDateTime(selectedDiscoveryDomain.redemption_anchor_at)} | {formatRedemptionAnchorSource(selectedDiscoveryDomain.redemption_anchor_source)}</strong>
                </div>
                <div>
                  <span>Предполагаемый pendingDelete</span>
                  <strong>{formatDateTime(selectedDiscoveryDomain.predicted_pending_delete_at)}</strong>
                </div>
                <div>
                  <span>Фактический pendingDelete</span>
                  <strong>{formatDateTime(selectedDiscoveryDomain.pending_delete_previous_seen_at)} {"->"} {formatDateTime(selectedDiscoveryDomain.first_seen_pending_delete_at)}</strong>
                </div>
                <div>
                  <span>Предполагаемый drop</span>
                  <strong>{formatDateTime(selectedDiscoveryDomain.predicted_drop_start_at)} {"->"} {formatDateTime(selectedDiscoveryDomain.predicted_drop_end_at)}</strong>
                </div>
              </div>
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>Время</th>
                      <th>Источник</th>
                      <th>Lifecycle</th>
                      <th>Коды статуса</th>
                      <th>HTTP / latency</th>
                      <th>Ошибка / ответ</th>
                    </tr>
                  </thead>
                  <tbody>
                    {discoveryObservations.length === 0 ? (
                      <tr>
                        <td colSpan={6}>Истории пока нет. Нажми “Проверить сейчас” или дождись автоматического RDAP-чека.</td>
                      </tr>
                    ) : null}
                    {discoveryObservations.map((observation) => (
                      <tr key={observation.id}>
                        <td>{formatDateTime(observation.observed_at)}</td>
                        <td>{observation.source}</td>
                        <td>
                          <div>{formatLifecycleLabel(observation.lifecycle_stage)}</div>
                          <div className="row-hint">доступность: {formatAvailabilityLabel(observation.availability_status)}</div>
                        </td>
                        <td><span className="code-inline">{observation.status_codes ?? "—"}</span></td>
                        <td>
                          <div>{observation.http_status ?? "—"}</div>
                          <div className="row-hint">{observation.latency_ms ? `${observation.latency_ms} ms` : "задержка: —"}</div>
                        </td>
                        <td>
                          {observation.error ? <div className="row-hint">ошибка: {observation.error}</div> : null}
                          {observation.raw_response ? <div className="row-hint clipped-text">{observation.raw_response}</div> : <div className="row-hint">ответ: —</div>}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          ) : (
            <p className="muted">Выбери домен через кнопку “История” в таблице ниже.</p>
          )}
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Домены discovery</h2>
              <p className="muted">
                Показано {paginatedDiscoveryDomains.length} из {filteredDiscoveryDomains.length}; всего в discovery {discoveryDomains.length}.
              </p>
            </div>
            <div className="button-row">
              <a className="button-link ghost" href={discoveryAvailableExportUrl(discoveryFilters.zone || undefined)}>
                Скачать доступные CSV
              </a>
              <button
                type="button"
                className="ghost"
                onClick={() => {
                  setDiscoveryFilters(DEFAULT_DISCOVERY_FILTERS);
                  setDiscoveryPage(1);
                }}
              >
                Сбросить фильтры
              </button>
            </div>
          </div>
          <div className="filter-panel">
            <label>
              <span>Базовый интервал, сек</span>
              <input
                type="number"
                min="10"
                max="86400"
                value={discoveryBulkIntervalSeconds}
                onChange={(event) => setDiscoveryBulkIntervalSeconds(event.target.value)}
              />
            </label>
            <div className="form-actions compact-actions">
              <button type="button" className="ghost" onClick={() => void updateDiscoveryInterval("filtered")}>
                Применить к фильтру
              </button>
              <button type="button" className="ghost" onClick={() => void updateDiscoveryInterval("all")}>
                Применить ко всем
              </button>
            </div>
          </div>
          <div className="filter-panel">
            <label>
              <span>Поиск</span>
              <input
                value={discoveryFilters.query}
                onChange={(event) => {
                  setDiscoveryFilters((current) => ({ ...current, query: event.target.value }));
                  setDiscoveryPage(1);
                }}
                placeholder="domain.com"
              />
            </label>
            <label>
              <span>Зона</span>
              <select
                value={discoveryFilters.zone}
                onChange={(event) => {
                  setDiscoveryFilters((current) => ({ ...current, zone: event.target.value }));
                  setDiscoveryPage(1);
                }}
              >
                <option value="">Все зоны</option>
                {discoveryZoneOptions.map((zone) => <option key={zone} value={zone}>.{zone}</option>)}
              </select>
            </label>
            <label>
              <span>Статус</span>
              <select
                value={discoveryFilters.status}
                onChange={(event) => {
                  setDiscoveryFilters((current) => ({ ...current, status: event.target.value }));
                  setDiscoveryPage(1);
                }}
              >
                <option value="">Все статусы</option>
                {discoveryStatusOptions.map((status) => <option key={status} value={status}>{formatStatusLabel(status)}</option>)}
              </select>
            </label>
            <label>
              <span>Lifecycle</span>
              <select
                value={discoveryFilters.lifecycle}
                onChange={(event) => {
                  setDiscoveryFilters((current) => ({ ...current, lifecycle: event.target.value }));
                  setDiscoveryPage(1);
                }}
              >
                <option value="">Все стадии</option>
                {discoveryLifecycleOptions.map((lifecycle) => <option key={lifecycle} value={lifecycle}>{formatLifecycleLabel(lifecycle)}</option>)}
              </select>
            </label>
            <label>
              <span>На странице</span>
              <select
                value={discoveryFilters.pageSize}
                onChange={(event) => {
                  setDiscoveryFilters((current) => ({ ...current, pageSize: event.target.value }));
                  setDiscoveryPage(1);
                }}
              >
                <option value="10">10</option>
                <option value="25">25</option>
                <option value="50">50</option>
                <option value="100">100</option>
              </select>
            </label>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Домен</th>
                  <th>Статус</th>
                  <th>Lifecycle</th>
                  <th>Прогноз pendingDelete</th>
                  <th>Прогноз drop</th>
                  <th>Доступность</th>
                  <th>Следующая проверка</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {paginatedDiscoveryDomains.length === 0 ? (
                  <tr>
                    <td colSpan={8}>По этим фильтрам доменов нет.</td>
                  </tr>
                ) : null}
                {paginatedDiscoveryDomains.map((domain) => (
                  <tr key={domain.id}>
                    <td><strong>{domain.fqdn}</strong><div className="row-hint">.{domain.zone} | {domain.source_mode}</div></td>
                    <td><span className={statusClass(domain.status)}>{formatStatusLabel(domain.status)}</span></td>
                    <td>
                      <div>{formatLifecycleLabel(domain.last_lifecycle_stage)}</div>
                      <div className="row-hint">коды: {domain.last_status_codes ?? "—"}</div>
                      <div className="row-hint">проверено: {formatDateTime(domain.last_checked_at)}</div>
                    </td>
                    <td>
                      <div>{formatDateTime(domain.predicted_pending_delete_at)}</div>
                      <div className="row-hint">считаем от: {formatDateTime(domain.redemption_anchor_at)}</div>
                      <div className="row-hint">источник: {formatRedemptionAnchorSource(domain.redemption_anchor_source)}</div>
                    </td>
                    <td>
                      <div>{formatDateTime(domain.predicted_drop_start_at)}</div>
                      <div className="row-hint">до {formatDateTime(domain.predicted_drop_end_at)}</div>
                      <div className="row-hint">pendingDelete увидели: {formatDateTime(domain.first_seen_pending_delete_at)}</div>
                    </td>
                    <td>
                      <div>{formatAvailabilityLabel(domain.last_availability)}</div>
                      <div className="row-hint">впервые доступен: {formatDateTime(domain.available_first_seen_at)}</div>
                      {domain.last_error ? <div className="row-hint">ошибка: {domain.last_error}</div> : null}
                    </td>
                    <td>{formatDateTime(domain.next_check_at)}</td>
                    <td>
                      <div className="actions">
                        <button type="button" onClick={() => void checkDiscoveryDomainNow(domain.id)}>Проверить сейчас</button>
                        <button type="button" className="ghost" onClick={() => void loadDiscoveryTimeline(domain.id)}>История</button>
                        <button type="button" className="ghost" onClick={() => setDiscoveryObservationForm((current) => ({ ...current, domainId: String(domain.id) }))}>Наблюдение</button>
                        <button type="button" className="danger" onClick={() => void deleteItem("discovery", domain.id)}>Удалить</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="pagination-bar">
            <button
              type="button"
              className="ghost"
              disabled={activeDiscoveryPage <= 1}
              onClick={() => setDiscoveryPage((current) => Math.max(current - 1, 1))}
            >
              Назад
            </button>
            <span>
              Страница {activeDiscoveryPage} / {discoveryTotalPages}
            </span>
            <button
              type="button"
              className="ghost"
              disabled={activeDiscoveryPage >= discoveryTotalPages}
              onClick={() => setDiscoveryPage((current) => Math.min(current + 1, discoveryTotalPages))}
            >
              Вперед
            </button>
          </div>
        </div>
      </section>
    );
  }

  function renderZoneScanner() {
    const selectedJob = selectedZoneScanJobId ? zoneScanJobs.find((job) => job.id === selectedZoneScanJobId) ?? null : null;
    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>AllZonefiles</h2>
              <p className="muted">
                API: {allZonefilesSettings?.base_url ?? "—"}; токен: {allZonefilesSettings?.configured ? "настроен" : "не настроен"}
              </p>
            </div>
            <button type="button" className="ghost" onClick={() => void testAllZonefilesToken()}>Проверить подключение</button>
          </div>
          <form className="form" onSubmit={saveAllZonefilesToken}>
            <label>
              <span>API токен</span>
              <input
                type="password"
                value={allZonefilesTokenForm}
                onChange={(event) => setAllZonefilesTokenForm(event.target.value)}
                placeholder={allZonefilesSettings?.configured ? "Новый token или пусто для очистки" : "allzfio_..."}
              />
            </label>
            <button type="submit">Сохранить токен</button>
          </form>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h2>Новая задача сканирования</h2>
              <p className="muted">Для поиска pendingDelete через 1-2 дня используй исторический zonefile за 28-29 дней назад, а не актуальный zonefile.</p>
            </div>
          </div>
          <form className="form" onSubmit={submitZoneScanJob}>
            <div className="form-grid">
              <label><span>Зона</span><input value={zoneScanForm.zone} onChange={(event) => setZoneScanForm((current) => ({ ...current, zone: event.target.value }))} /></label>
              <label>
                <span>Источник</span>
                <select value={zoneScanForm.sourceType} onChange={(event) => setZoneScanForm((current) => ({ ...current, sourceType: event.target.value }))}>
                  <option value="zone_latest">актуальный zonefile</option>
                  <option value="zone_historic">исторический zonefile</option>
                  <option value="expired_latest">актуальный список expired</option>
                  <option value="expired_historic">исторический список expired</option>
                </select>
              </label>
              <label><span>Дата файла</span><input type="date" value={zoneScanForm.sourceDate} onChange={(event) => setZoneScanForm((current) => ({ ...current, sourceDate: event.target.value }))} /></label>
              <label><span>Мин. score</span><input value={zoneScanForm.minScore} onChange={(event) => setZoneScanForm((current) => ({ ...current, minScore: event.target.value }))} /></label>
              <label><span>Лимит результата</span><input value={zoneScanForm.limitOutput} onChange={(event) => setZoneScanForm((current) => ({ ...current, limitOutput: event.target.value }))} /></label>
              <label><span>Макс. RDAP</span><input value={zoneScanForm.maxRdapChecks} onChange={(event) => setZoneScanForm((current) => ({ ...current, maxRdapChecks: event.target.value }))} /></label>
              <label><span>Потоки</span><input value={zoneScanForm.concurrency} onChange={(event) => setZoneScanForm((current) => ({ ...current, concurrency: event.target.value }))} /></label>
              <label><span>Таймаут RDAP</span><input value={zoneScanForm.rdapTimeoutSeconds} onChange={(event) => setZoneScanForm((current) => ({ ...current, rdapTimeoutSeconds: event.target.value }))} /></label>
              <label><span>Pending мин. дней</span><input value={zoneScanForm.pendingDeleteMinDays} onChange={(event) => setZoneScanForm((current) => ({ ...current, pendingDeleteMinDays: event.target.value }))} /></label>
              <label><span>Pending макс. дней</span><input value={zoneScanForm.pendingDeleteMaxDays} onChange={(event) => setZoneScanForm((current) => ({ ...current, pendingDeleteMaxDays: event.target.value }))} /></label>
              <label><span>Размер выборки</span><input value={zoneScanForm.reservoirSize} onChange={(event) => setZoneScanForm((current) => ({ ...current, reservoirSize: event.target.value }))} /></label>
              <label><span>Seed рандома</span><input value={zoneScanForm.randomSeed} onChange={(event) => setZoneScanForm((current) => ({ ...current, randomSeed: event.target.value }))} /></label>
            </div>
            <label className="checkbox">
              <input type="checkbox" checked={zoneScanForm.keepFile} onChange={(event) => setZoneScanForm((current) => ({ ...current, keepFile: event.target.checked }))} />
              <span>Оставить скачанный .gz файл после сканирования</span>
            </label>
            <button type="submit">Запустить сканирование</button>
            <button type="button" className="secondary" onClick={createPendingWindowProbeJobs}>
              Найти pendingDelete через 1-2 дня
            </button>
          </form>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Задачи сканирования</h2>
              <p className="muted">Выбери задачу, чтобы отфильтровать кандидатов. Активные задачи продолжают работать на сервере.</p>
            </div>
            <button type="button" className="ghost" onClick={() => void refreshZoneScanner()}>Обновить</button>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Зона</th>
                  <th>Статус</th>
                  <th>Файл</th>
                  <th>Прогресс</th>
                  <th>RDAP</th>
                  <th>Найдено</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {zoneScanJobs.length === 0 ? (
                  <tr><td colSpan={8}>Задач сканирования пока нет.</td></tr>
                ) : null}
                {zoneScanJobs.map((job) => (
                  <tr key={job.id}>
                    <td>#{job.id}</td>
                    <td>
                      <strong>.{job.zone}</strong>
                      <div className="row-hint">{formatSourceType(job.source_type)}{job.source_date ? ` / ${job.source_date}` : ""}</div>
                    </td>
                    <td>
                      <span className={statusClass(job.status)}>{formatStatusLabel(job.status)}</span>
                      {job.last_error ? <div className="row-hint">ошибка: {job.last_error}</div> : null}
                    </td>
                    <td>
                      <div>{job.file_name ?? "—"}</div>
                      <div className="row-hint">{formatBytes(job.downloaded_bytes)} / {formatBytes(job.file_size_bytes)}</div>
                    </td>
                    <td>
                      <div>строк: {job.scanned_lines.toLocaleString("ru-RU")}</div>
                      <div className="row-hint">отфильтровано: {job.filtered_candidates.toLocaleString("ru-RU")}</div>
                    </td>
                    <td>
                      <div>{job.completed_rdap.toLocaleString("ru-RU")} / {job.submitted_rdap.toLocaleString("ru-RU")}</div>
                      <div className="row-hint">макс: {job.max_rdap_checks.toLocaleString("ru-RU")}; потоки: {job.concurrency}</div>
                    </td>
                    <td>
                      <strong>{job.found_candidates}</strong>
                      <div className="row-hint">ошибки: {job.error_count}</div>
                    </td>
                    <td>
                      <div className="actions">
                        <button type="button" onClick={() => void selectZoneScanJob(job.id)}>Кандидаты</button>
                        <button type="button" className="ghost" onClick={() => void cancelZoneScanJob(job.id)}>Стоп</button>
                        <button type="button" className="ghost" onClick={() => void deleteZoneScanFile(job.id)}>Удалить файл</button>
                        <button type="button" className="danger" onClick={() => void deleteZoneScanJob(job.id)}>Удалить</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Найденные кандидаты</h2>
              <p className="muted">
                {selectedJob ? `Задача #${selectedJob.id}, .${selectedJob.zone}` : "Все последние кандидаты"}; показано {zoneScanCandidates.length}.
              </p>
            </div>
            <button type="button" className="ghost" onClick={() => void selectZoneScanJob(null)}>Показать все</button>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Домен</th>
                  <th>Lifecycle</th>
                  <th>Прогноз pendingDelete</th>
                  <th>Score</th>
                  <th>Проверено</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {zoneScanCandidates.length === 0 ? (
                  <tr><td colSpan={6}>Кандидаты пока не найдены.</td></tr>
                ) : null}
                {zoneScanCandidates.map((candidate) => (
                  <tr key={candidate.id}>
                    <td>
                      <strong>{candidate.fqdn}</strong>
                      <div className="row-hint">job #{candidate.job_id} | .{candidate.zone}</div>
                    </td>
                    <td>
                      <span className={statusClass(candidate.lifecycle_stage)}>{formatLifecycleLabel(candidate.lifecycle_stage)}</span>
                      <div className="row-hint">{candidate.status_codes ?? "коды: —"}</div>
                    </td>
                    <td>
                      <div>{formatDateTime(candidate.predicted_pending_delete_at)}</div>
                      <div className="row-hint">считаем от: {formatDateTime(candidate.redemption_anchor_at)}</div>
                      <div className="row-hint">дней: {candidate.days_to_pending_delete ?? "—"}</div>
                    </td>
                    <td>
                      <strong>{candidate.score}</strong>
                      <div className="row-hint">{candidate.reason ?? "—"}</div>
                    </td>
                    <td>
                      <div>{formatDateTime(candidate.checked_at)}</div>
                      <div className="row-hint">HTTP: {candidate.http_status ?? "—"}</div>
                    </td>
                    <td>
                      <div className="actions">
                        <button
                          type="button"
                          disabled={Boolean(candidate.discovery_domain_id)}
                          onClick={() => void addZoneScanCandidateToDiscovery(candidate.id)}
                        >
                          {candidate.discovery_domain_id ? "Уже в discovery" : "Добавить в discovery"}
                        </button>
                        <button type="button" className="ghost" onClick={() => void ignoreZoneScanCandidate(candidate.id)}>Скрыть</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    );
  }

  function renderDomains() {
    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Домены дропа</h2>
              <p className="muted">У каждого домена обязательна дата дропа. Внутри дня атака работает только по окну зоны.</p>
            </div>
            <div className="actions">
              <button type="button" className="ghost" onClick={() => void dryRunReadyDueTodayDomains()}>Dry-run на сегодня</button>
              <button type="button" onClick={() => void startTodayAttacks(false)}>Старт на сегодня</button>
              <button type="button" className="ghost" onClick={() => void startTodayAttacks(true)}>Перестроить атаки</button>
            </div>
          </div>

          <form className="form" onSubmit={submitDomains}>
            <p className="muted">
              Выбери стратегию зоны, дату дропа и домены. Часовой пояс, окно и регистратор подтянутся из стратегии.
            </p>
            <label>
              <span>Домены по одному или пачкой</span>
              <textarea
                rows={5}
                value={domainForm.domainsText}
                onChange={(event) => setDomainForm((current) => ({ ...current, domainsText: event.target.value }))}
                placeholder="example.fr&#10;example2.fr"
              />
            </label>
            <label>
              <span>Или импорт файла csv/txt/xlsx</span>
              <input type="file" accept=".txt,.csv,.xlsx" onChange={(event) => setImportFile(event.target.files?.[0] ?? null)} />
            </label>
            <div className="form two-columns">
              <label>
                <span>Стратегия зоны</span>
                <select value={domainForm.zoneStrategyId} onChange={(event) => selectDomainStrategy(event.target.value)}>
                  <option value="">Без стратегии / ручные поля</option>
                  {activeDomainStrategies.map((strategy) => (
                    <option key={strategy.id} value={strategy.id}>
                      .{strategy.zone} — {strategy.name}
                    </option>
                  ))}
                </select>
              </label>
              <label><span>Дата дропа</span><input type="date" value={domainForm.dropDate} onChange={(event) => setDomainForm((current) => ({ ...current, dropDate: event.target.value }))} /></label>
              <label>
                <span>Приоритет</span>
                <input value={domainForm.priority} onChange={(event) => setDomainForm((current) => ({ ...current, priority: event.target.value }))} />
                <small className="field-hint">Это вес, а не процент. Для 97% одному домену и 1% остальным ставь 1000 / 10 / 10 / 10.</small>
              </label>
              <label><span>Лет регистрации</span><input value={domainForm.requestedDurationYears} onChange={(event) => setDomainForm((current) => ({ ...current, requestedDurationYears: event.target.value }))} /></label>
              <label>
                <span>Аккаунт регистратора</span>
                <select value={domainForm.registrarAccountId} onChange={(event) => setDomainForm((current) => ({ ...current, registrarAccountId: event.target.value }))}>
                  <option value="">Автовыбор</option>
                  {accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}
                </select>
              </label>
              <label>
                <span>Профиль контакта</span>
                <select value={domainForm.contactProfileId} onChange={(event) => setDomainForm((current) => ({ ...current, contactProfileId: event.target.value }))}>
                  <option value="">Автовыбор</option>
                  {contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.label}</option>)}
                </select>
              </label>
              <label className="checkbox"><input type="checkbox" checked={domainForm.attackEnabled} onChange={(event) => setDomainForm((current) => ({ ...current, attackEnabled: event.target.checked }))} /><span>Атака активна</span></label>
              <label className="checkbox"><input type="checkbox" checked={domainForm.autoStartEnabled} onChange={(event) => setDomainForm((current) => ({ ...current, autoStartEnabled: event.target.checked }))} /><span>Автостарт по окну</span></label>
              <label><span>Автостарт за, сек</span><input value={domainForm.autoStartLeadSeconds} onChange={(event) => setDomainForm((current) => ({ ...current, autoStartLeadSeconds: event.target.value }))} /></label>
            </div>
            <div className="domain-strategy-preview">
              <div>
                <span>Зона</span>
                <strong>{selectedDomainStrategy ? `.${selectedDomainStrategy.zone}` : domainForm.zone ? `.${domainForm.zone}` : "—"}</strong>
              </div>
              <div>
                <span>Часовой пояс</span>
                <strong>{selectedDomainStrategy?.timezone_name ?? domainForm.timezoneName}</strong>
              </div>
              <div>
                <span>Регистратор</span>
                <strong>{selectedDomainStrategy?.default_registrar_slug ?? domainForm.registrarSlug}</strong>
              </div>
              <div>
                <span>Мин. RPS</span>
                <strong>
                  {domainForm.overrideMinGuaranteedRps ||
                    (selectedDomainStrategy ? String(selectedDomainStrategy.default_min_guaranteed_rps) : "—")}
                </strong>
              </div>
              <div className="wide-input">
                <span>Окно стратегии</span>
                <strong>
                  {domainStrategyPreview?.windows.length
                    ? domainStrategyPreview.windows
                        .slice(0, 3)
                        .map((window) => formatPreviewWindow(window, domainStrategyPreview.timezone_name))
                        .join("; ")
                    : selectedDomainStrategy
                      ? "Выбери дату дропа, чтобы увидеть расчет окна"
                      : formatDomainStrategyFallbackWindow(domainForm)}
                </strong>
              </div>
            </div>
            <details className="collapsible-card domain-advanced-options">
              <summary>
                Расширенные настройки домена
                <small>Нужны только для ручных override или зоны без готовой стратегии.</small>
              </summary>
              <div className="form two-columns">
                <label>
                  <span>Режим стратегии</span>
                  <select value={domainForm.strategyMode} onChange={(event) => setDomainForm((current) => ({ ...current, strategyMode: event.target.value }))}>
                    <option value="inherit_zone">наследовать стратегию зоны</option>
                    <option value="manual_override">ручной override домена</option>
                  </select>
                </label>
                <label><span>Зона</span><input value={domainForm.zone} onChange={(event) => setDomainForm((current) => ({ ...current, zone: event.target.value, zoneStrategyId: "" }))} /></label>
                <label><span>Часовой пояс</span><input value={domainForm.timezoneName} onChange={(event) => setDomainForm((current) => ({ ...current, timezoneName: event.target.value }))} /></label>
                <label><span>Регистратор</span><input value={domainForm.registrarSlug} onChange={(event) => setDomainForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
                <label><span>Override мин. RPS</span><input value={domainForm.overrideMinGuaranteedRps} onChange={(event) => setDomainForm((current) => ({ ...current, overrideMinGuaranteedRps: event.target.value }))} placeholder="пусто = значение зоны" /></label>
                <label><span>Минута окна</span><input value={domainForm.windowStartMinute} onChange={(event) => setDomainForm((current) => ({ ...current, windowStartMinute: event.target.value }))} /></label>
                <label><span>Секунда окна</span><input value={domainForm.windowStartSecond} onChange={(event) => setDomainForm((current) => ({ ...current, windowStartSecond: event.target.value }))} /></label>
                <label><span>Длина окна, сек</span><input value={domainForm.windowDurationSeconds} onChange={(event) => setDomainForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
              </div>
            </details>
            <label>
              <span>Дополнительные параметры регистрации Gandi (JSON)</span>
              <textarea
                rows={3}
                value={domainForm.registrationExtraParameters}
                onChange={(event) => setDomainForm((current) => ({ ...current, registrationExtraParameters: event.target.value }))}
                placeholder='{"fr_lock": true}'
              />
            </label>
            <label><span>Заметки</span><textarea rows={3} value={domainForm.notes} onChange={(event) => setDomainForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить домены</button>
          </form>
        </div>

        <div className="card">
          <h2>Активный пул</h2>
          <div className="key-value compact">
            <div><span>Всего доменов</span><strong>{overview?.total_domains ?? 0}</strong></div>
            <div><span>Дроп сегодня</span><strong>{overview?.due_today_domains ?? 0}</strong></div>
            <div><span>Сейчас в атаке</span><strong>{overview?.active_attack_domains ?? 0}</strong></div>
            <div><span>Успешно сегодня</span><strong>{overview?.success_today_domains ?? 0}</strong></div>
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Ручной override домена</h2>
              <p className="muted">Для доменов в режиме `manual_override` здесь задаются собственные настройки, окна, фазы и предпросмотр.</p>
            </div>
            <div className="actions">
              <label>
                <span>Дата предпросмотра</span>
                <input type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} />
              </label>
              <button type="button" className="ghost" onClick={() => selectedOverrideDomainId ? void loadDomainOverrideDetails(selectedOverrideDomainId, previewDate) : undefined}>Обновить override</button>
            </div>
          </div>

          <div className="form two-columns">
            <label>
              <span>Домен с ручным override</span>
              <select value={selectedOverrideDomainId ?? ""} onChange={(event) => setSelectedOverrideDomainId(event.target.value ? Number(event.target.value) : null)}>
                <option value="">Выбери домен</option>
                {manualOverrideDomains.map((domain) => (
                  <option key={domain.id} value={domain.id}>
                    {domain.fqdn} | {domain.drop_date} | {domain.status}
                  </option>
                ))}
              </select>
            </label>
            <div className="key-value compact">
              <div><span>Override объект</span><strong>{domainOverrideSettings ? `#${domainOverrideSettings.id}` : "не создан"}</strong></div>
              <div><span>Окна</span><strong>{domainOverrideRules.length}</strong></div>
              <div><span>Окон в предпросмотре</span><strong>{domainOverridePreview?.windows.length ?? 0}</strong></div>
              <div><span>Готовность</span><strong>{selectedOverrideDomain ? formatDomainReadiness(selectedOverrideDomain) : "—"}</strong></div>
            </div>
          </div>

          {selectedOverrideDomain ? (
            <>
              <div className="grid two">
                <div className="card">
                  <h3>Настройки override</h3>
                  <form className="form" onSubmit={saveDomainOverrideSettings}>
                    <div className="form two-columns">
                      <label><span>Часовой пояс</span><input value={domainOverrideForm.timezoneName} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, timezoneName: event.target.value }))} /></label>
                      <label>
                        <span>Как выбирать окно</span>
                        <select value={domainOverrideForm.ruleResolutionMode} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, ruleResolutionMode: event.target.value }))}>
                          <option value="priority">по приоритету</option>
                          <option value="merge">объединять окна</option>
                        </select>
                      </label>
                      <label><span>Мин. RPS по умолчанию</span><input value={domainOverrideForm.defaultMinGuaranteedRps} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, defaultMinGuaranteedRps: event.target.value }))} /></label>
                    </div>
                    <label><span>Заметки</span><textarea rows={2} value={domainOverrideForm.notes} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, notes: event.target.value }))} /></label>
                    <button type="submit">{domainOverrideSettings ? "Сохранить override" : "Инициализировать override"}</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Override-окно</h3>
                  <form className="form" onSubmit={submitDomainOverrideRule}>
                    <div className="form two-columns">
                      <label><span>Название</span><input value={domainOverrideRuleForm.name} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, name: event.target.value }))} /></label>
                      <label>
                        <span>Расписание</span>
                        <select value={domainOverrideRuleForm.scheduleType} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, scheduleType: event.target.value }))}>
                          <option value="hourly">каждый час</option>
                          <option value="daily">каждый день</option>
                          <option value="weekly">по дням недели</option>
                          <option value="one_time">один раз</option>
                        </select>
                      </label>
                      <label><span>Час</span><input value={domainOverrideRuleForm.hour} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, hour: event.target.value }))} placeholder="пусто = каждый час" /></label>
                      <label><span>Минута</span><input value={domainOverrideRuleForm.minute} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, minute: event.target.value }))} /></label>
                      <label><span>Секунда</span><input value={domainOverrideRuleForm.second} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, second: event.target.value }))} /></label>
                      <label><span>Приоритет</span><input value={domainOverrideRuleForm.priority} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, priority: event.target.value }))} /></label>
                      <label><span>Дни недели</span><input value={domainOverrideRuleForm.weekdays} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, weekdays: event.target.value }))} placeholder="1,3,5" /></label>
                      <label><span>Конкретная дата</span><input type="date" value={domainOverrideRuleForm.specificDate} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, specificDate: event.target.value }))} /></label>
                      <label><span>Длина окна, сек</span><input value={domainOverrideRuleForm.windowDurationSeconds} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
                      <label>
                        <span>Подача запросов</span>
                        <select value={domainOverrideRuleForm.executionProfileMode} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, executionProfileMode: event.target.value }))}>
                          <option value="flat">ровно</option>
                          <option value="phased">по фазам</option>
                        </select>
                      </label>
                      <label className="checkbox"><input type="checkbox" checked={domainOverrideRuleForm.isEnabled} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, isEnabled: event.target.checked }))} /><span>Включено</span></label>
                    </div>
                    <label><span>Заметки</span><textarea rows={2} value={domainOverrideRuleForm.notes} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, notes: event.target.value }))} /></label>
                    <button type="submit">Добавить override-окно</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Override-фаза</h3>
                  <form className="form" onSubmit={submitDomainOverridePhase}>
                    <div className="form two-columns">
                      <label>
                        <span>Окно</span>
                        <select value={domainOverridePhaseForm.ruleId} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, ruleId: event.target.value }))}>
                          <option value="">Выбери окно</option>
                          {domainOverrideRules.map((rule) => <option key={rule.id} value={rule.id}>{rule.name}</option>)}
                        </select>
                      </label>
                      <label><span>Название</span><input value={domainOverridePhaseForm.name} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, name: event.target.value }))} /></label>
                      <label><span>Порядок</span><input value={domainOverridePhaseForm.sortOrder} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, sortOrder: event.target.value }))} /></label>
                      <label><span>Старт через, сек</span><input value={domainOverridePhaseForm.startOffsetSeconds} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, startOffsetSeconds: event.target.value }))} /></label>
                      <label><span>Длительность, сек</span><input value={domainOverridePhaseForm.durationSeconds} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, durationSeconds: event.target.value }))} /></label>
                      <label>
                        <span>Режим RPS</span>
                        <select value={domainOverridePhaseForm.rpsMode} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, rpsMode: event.target.value }))}>
                          <option value="percent">процент</option>
                          <option value="fixed">фиксированно</option>
                        </select>
                      </label>
                      <label><span>Значение RPS</span><input value={domainOverridePhaseForm.rpsValue} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, rpsValue: event.target.value }))} /></label>
                      <label className="checkbox"><input type="checkbox" checked={domainOverridePhaseForm.stopOnSuccess} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, stopOnSuccess: event.target.checked }))} /><span>Остановить при успехе</span></label>
                    </div>
                    <button type="submit" disabled={domainOverrideRules.length === 0}>Добавить override-фазу</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Предпросмотр override</h3>
                  <div className="key-value compact">
                    <div><span>Часовой пояс</span><strong>{domainOverridePreview?.timezone_name ?? domainOverrideForm.timezoneName}</strong></div>
                    <div><span>Как выбираются окна</span><strong>{formatResolutionMode(domainOverridePreview?.resolution_mode ?? domainOverrideForm.ruleResolutionMode)}</strong></div>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Окно</th>
                          <th>Приоритет</th>
                          <th>Старт</th>
                          <th>Конец</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(domainOverridePreview?.windows ?? []).map((window) => {
                          const timezoneName = domainOverridePreview?.timezone_name ?? domainOverrideForm.timezoneName;
                          return (
                          <tr key={`${window.rule_id}-${window.start_at}`}>
                            <td>{window.rule_name ?? `окно #${window.rule_id}`}</td>
                            <td>{window.priority}</td>
                            <td>
                              <div>{formatDateTimeInZone(window.start_at, timezoneName, "локально")}</div>
                              <div className="row-hint">{formatDateTime(window.start_at)}</div>
                            </td>
                            <td>
                              <div>{formatDateTimeInZone(window.end_at, timezoneName, "локально")}</div>
                              <div className="row-hint">{formatDateTime(window.end_at)}</div>
                            </td>
                          </tr>
                          );
                        })}
                        {(domainOverridePreview?.windows ?? []).length === 0 ? (
                          <tr>
                            <td colSpan={4} className="empty">На эту дату окна не найдены</td>
                          </tr>
                        ) : null}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>

              <div className="promo-list">
                {domainOverrideRules.map((rule) => {
                  const previewWindow = domainOverridePreview?.windows.find((window) => window.rule_id === rule.id);
                  return (
                  <article key={rule.id} className="strategy-rule-card">
                    <div className="domain-card-head compact-head">
                      <div>
                        <div className="domain-title-row">
                          <h3>{rule.name}</h3>
                          <span className={statusClass(rule.is_enabled ? "ready" : "inactive")}>{formatScheduleType(rule.schedule_type)}</span>
                        </div>
                        <p className="muted">
                          приоритет {rule.priority} | местное время {formatRuleLocalTime(rule)} | окно {rule.window_duration_seconds} сек | запросы: {formatExecutionMode(rule.execution_profile_mode)}
                        </p>
                        {previewWindow ? <p className="row-hint">MSK по выбранной дате: {formatPreviewWindowMsk(previewWindow)}</p> : null}
                      </div>
                      <div className="actions">
                        <button type="button" className="danger" onClick={() => void removeDomainOverrideRule(rule.id)}>Удалить окно</button>
                      </div>
                    </div>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Фаза</th>
                            <th>Порядок</th>
                            <th>Старт через</th>
                            <th>Длительность</th>
                            <th>Режим</th>
                            <th>Значение</th>
                            <th>Стоп</th>
                            <th></th>
                          </tr>
                        </thead>
                        <tbody>
                          {(domainOverridePhases[rule.id] ?? []).map((phase) => (
                            <tr key={phase.id}>
                              <td>{phase.name}</td>
                              <td>{phase.sort_order}</td>
                              <td>{phase.start_offset_seconds} сек</td>
                              <td>{phase.duration_seconds} сек</td>
                              <td>{formatRpsModeLabel(phase.rps_mode)}</td>
                              <td>{phase.rps_value}</td>
                              <td>{phase.stop_on_success ? "да" : "нет"}</td>
                              <td><button type="button" className="danger" onClick={() => void removeDomainOverridePhase(phase.id)}>Удалить</button></td>
                            </tr>
                          ))}
                          {(domainOverridePhases[rule.id] ?? []).length === 0 ? (
                            <tr>
                              <td colSpan={8} className="empty">Фаз пока нет</td>
                            </tr>
                          ) : null}
                        </tbody>
                      </table>
                    </div>
                  </article>
                  );
                })}
                {domainOverrideRules.length === 0 ? <p className="empty">Override-окон пока нет</p> : null}
              </div>
            </>
          ) : (
            <p className="empty">Пока нет доменов в режиме `manual_override`.</p>
          )}
        </div>

        <div className="card full-span">
          <div className="card-head">
            <h2>Список доменов</h2>
            <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Домен</th>
                  <th>Статус</th>
                  <th>Дата дропа</th>
                  <th>Готовность</th>
                  <th>Приоритет</th>
                  <th>Стратегия</th>
                  <th>Рабочий RPS</th>
                  <th>Аккаунт</th>
                  <th>Контакт</th>
                  <th>Окно</th>
                  <th>Успех</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {domains.map((domain) => (
                  <tr key={domain.id}>
                    <td>
                      <strong>{domain.fqdn}</strong>
                        <div className="row-hint">{domain.zone} | {domain.registrar_slug} | {domain.strategy_mode}</div>
                      </td>
                      <td>
                        <span className={statusClass(domain.status)}>{formatStatusLabel(domain.status)}</span>
                        {domain.runtime_attack_status ? <div className="row-hint">запуск: {formatStatusLabel(domain.runtime_attack_status)}</div> : null}
                      </td>
                      <td>{domain.drop_date}</td>
                      <td>
                        <div>{formatDomainReadiness(domain)}</div>
                        {domain.override_min_guaranteed_rps !== null ? <div className="row-hint">мин. override: {domain.override_min_guaranteed_rps}</div> : null}
                        {domain.dry_run_status ? <div className="row-hint">dry-run: {domain.dry_run_status}{domain.dry_run_http_status ? ` / ${domain.dry_run_http_status}` : ""}</div> : null}
                      </td>
                      <td>{domain.priority}</td>
                      <td>
                        <div>{domain.zone_strategy_id ? strategyMap.get(domain.zone_strategy_id)?.name ?? `#${domain.zone_strategy_id}` : "авто"}</div>
                        {domain.runtime_phase_name ? <div className="row-hint">фаза: {domain.runtime_phase_name}</div> : null}
                      </td>
                      <td>
                        <div>мин. {formatRps(domain.runtime_minimum_rps)}</div>
                        <div>желаемый {formatRps(domain.runtime_desired_rps)}</div>
                        <div>выделено {formatRps(domain.runtime_allocated_rps)}</div>
                        <div className="row-hint">воркеры: {domain.runtime_assigned_worker_count}</div>
                      </td>
                      <td>{domain.registrar_account_id ? accountMap.get(domain.registrar_account_id)?.name ?? `#${domain.registrar_account_id}` : "авто"}</td>
                    <td>{domain.contact_profile_id ? contactMap.get(domain.contact_profile_id)?.label ?? `#${domain.contact_profile_id}` : "авто"}</td>
                    <td>
                      <div>{formatDomainWindow(domain)}</div>
                      <div className="row-hint">
                        {domain.effective_window_source === "strategy" ? "окно стратегии" : "ручное окно"} · {getEffectiveWindowDurationSeconds(domain)}s
                      </div>
                      {domain.auto_start_enabled ? <div className="row-hint">автостарт за {domain.auto_start_lead_seconds}s</div> : <div className="row-hint">автостарт выкл</div>}
                    </td>
                    <td>
                      <div>{domain.success_at ? formatDateTime(domain.success_at) : "—"}</div>
                      {domain.dry_run_checked_at ? <div className="row-hint">проверено: {formatDateTime(domain.dry_run_checked_at)}</div> : null}
                    </td>
                    <td>
                      <div className="actions">
                        <button type="button" className="ghost" onClick={() => void startDomainAttack(domain.id)}>Старт</button>
                        <button type="button" className="ghost" onClick={() => void simulateDomainRegistration(domain.id)}>Сим. реги</button>
                        <button type="button" className="ghost" onClick={() => void dryRunDomain(domain)}>Тест</button>
                        <button type="button" className="ghost" onClick={() => void toggleDomain(domain)}>{domain.attack_enabled ? "Пауза" : "Вкл"}</button>
                        <button type="button" className="ghost" onClick={() => void toggleDomainAutoStart(domain)}>{domain.auto_start_enabled ? "Авто выкл" : "Авто вкл"}</button>
                        {domain.strategy_mode === "manual_override" ? (
                          <button type="button" className="ghost" onClick={() => setSelectedOverrideDomainId(domain.id)}>Override</button>
                        ) : null}
                        <button type="button" className="danger" onClick={() => void deleteItem("domain", domain.id)}>Удалить</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    );
  }

  function renderWorkers() {
    const setupWorker = workerSetup ? workers.find((worker) => worker.id === workerSetup.worker_id) ?? null : null;
    const fullInstallCommands = workerSetup?.full_install_commands ?? [];
    const updateExistingCommands = workerSetup?.update_existing_commands ?? [];
    const switchModeCommands = workerSetupMode === "test" ? workerSetup?.switch_to_test_commands ?? [] : workerSetup?.switch_to_live_commands ?? [];
    const verifyCommands = workerSetup?.verify_commands ?? [];

    return (
      <section className="grid two">
        <div className="card">
          <h2>{editingWorkerId ? `Редактировать воркер #${editingWorkerId}` : "Добавить воркер"}</h2>
          <form className="form" onSubmit={submitWorker}>
            <div className="form two-columns">
              <label><span>Имя</span><input value={workerForm.name} onChange={(event) => setWorkerForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>IP сервера</span><input value={workerForm.ipAddress} onChange={(event) => setWorkerForm((current) => ({ ...current, ipAddress: event.target.value, sshHost: current.sshHost || event.target.value }))} placeholder="2.27.x.x" /></label>
              <label><span>Регион</span><input value={workerForm.region} onChange={(event) => setWorkerForm((current) => ({ ...current, region: event.target.value }))} placeholder="DE, NL, FI..." /></label>
              <label>
                <span>Закрепленный аккаунт</span>
                <select value={workerForm.assignedRegistrarAccountId} onChange={(event) => setWorkerForm((current) => ({ ...current, assignedRegistrarAccountId: event.target.value }))}>
                  <option value="">Не закреплен</option>
                  {accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}
                </select>
              </label>
              <label><span>Целевой RPS</span><input value={workerForm.targetRps} onChange={(event) => setWorkerForm((current) => ({ ...current, targetRps: event.target.value }))} /></label>
              <label><span>Макс. RPS</span><input value={workerForm.maxRps} onChange={(event) => setWorkerForm((current) => ({ ...current, maxRps: event.target.value }))} /></label>
              <label><span>Регистратор</span><input value={workerForm.registrarSlug} onChange={(event) => setWorkerForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
            </div>
            <h3>SSH доступ для установки и обновления</h3>
            <p className="muted">Пароль нужен для автоматизации через панель. API не возвращает пароль обратно в браузер.</p>
            <div className="form two-columns">
              <label><span>SSH host</span><input value={workerForm.sshHost} onChange={(event) => setWorkerForm((current) => ({ ...current, sshHost: event.target.value }))} placeholder="обычно IP сервера" /></label>
              <label><span>SSH port</span><input value={workerForm.sshPort} onChange={(event) => setWorkerForm((current) => ({ ...current, sshPort: event.target.value }))} /></label>
              <label><span>SSH логин</span><input value={workerForm.sshUsername} onChange={(event) => setWorkerForm((current) => ({ ...current, sshUsername: event.target.value }))} /></label>
              <label><span>SSH пароль</span><input type="password" value={workerForm.sshPassword} onChange={(event) => setWorkerForm((current) => ({ ...current, sshPassword: event.target.value }))} placeholder={editingWorkerId ? "оставь пустым, чтобы не менять" : "root пароль"} /></label>
              <label><span>SSH key path</span><input value={workerForm.sshKeyPath} onChange={(event) => setWorkerForm((current) => ({ ...current, sshKeyPath: event.target.value }))} placeholder="опционально, например /root/.ssh/id_ed25519" /></label>
            </div>
            <label><span>Заметки</span><textarea rows={3} value={workerForm.notes} onChange={(event) => setWorkerForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">{editingWorkerId ? "Обновить воркер" : "Сохранить воркер"}</button>
              {editingWorkerId ? <button type="button" className="ghost" onClick={resetWorkerForm}>Отмена</button> : null}
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Суммарный RPS</h2>
          <div className="key-value compact">
            <div><span>Текущий</span><strong>{overview?.capacity.current_rps ?? 0}</strong></div>
            <div><span>Целевой</span><strong>{overview?.capacity.target_rps ?? 0}</strong></div>
            <div><span>Максимум</span><strong>{overview?.capacity.max_rps ?? 0}</strong></div>
            <div><span>Воркеры онлайн</span><strong>{overview?.capacity.online_workers ?? 0} / {overview?.capacity.enabled_workers ?? 0}</strong></div>
          </div>
        </div>

        {workerSetup ? (
          <div className="card full-span">
            <div className="card-head">
              <div>
                <h2>Установка воркера #{workerSetup.worker_id}</h2>
                <p className="muted">Команды рассчитаны для {workerSetup.worker_name}. Выполняй их на worker сервере под root.</p>
              </div>
              <button type="button" className="ghost" onClick={() => setWorkerSetup(null)}>Скрыть</button>
            </div>
            <div className="form two-columns">
              <label>
                <span>Режим</span>
                <select
                  value={workerSetupMode}
                  onChange={(event) => {
                    const mode = event.target.value as "test" | "live";
                    setWorkerSetupMode(mode);
                    if (setupWorker) {
                      void loadWorkerSetup(setupWorker, { mode });
                    }
                  }}
                >
                  <option value="test">Тестовая нагрузка без реальной регистрации</option>
                  <option value="live">Боевой режим с реальными запросами</option>
                </select>
              </label>
              <label>
                <span>Runtime URL control</span>
                <input
                  value={workerSetupRuntimeUrl}
                  onChange={(event) => setWorkerSetupRuntimeUrl(event.target.value)}
                  onBlur={(event) => setupWorker ? void loadWorkerSetup(setupWorker, { runtimeUrl: event.currentTarget.value }) : undefined}
                />
              </label>
            </div>
            {workerSetup.runtime_base_url.includes("CONTROL_SERVER_IP") ? (
              <p className="notice warning">Укажи прямой адрес control runtime, например http://2.27.21.88:8080. Лучше один раз добавить WORKER_RUNTIME_PUBLIC_BASE_URL в .env control сервера.</p>
            ) : null}
            <div className="setup-grid">
              <div className="setup-block">
                <div className="setup-block-head">
                  <strong>Новый worker сервер</strong>
                  <button type="button" className="ghost" onClick={() => void copyWorkerCommands(fullInstallCommands, "Новый сервер")}>Копировать</button>
                </div>
                <textarea readOnly rows={8} value={fullInstallCommands.join("\n")} />
              </div>
              <div className="setup-block">
                <div className="setup-block-head">
                  <strong>Если worker уже установлен</strong>
                  <button type="button" className="ghost" onClick={() => void copyWorkerCommands(updateExistingCommands, "Обновление worker")}>Копировать</button>
                </div>
                <textarea readOnly rows={8} value={updateExistingCommands.join("\n")} />
              </div>
              <div className="setup-block">
                <div className="setup-block-head">
                  <strong>{workerSetupMode === "test" ? "Переключить в тест" : "Переключить в бой"}</strong>
                  <button type="button" className="ghost" onClick={() => void copyWorkerCommands(switchModeCommands, "Переключение режима")}>Копировать</button>
                </div>
                <textarea readOnly rows={5} value={switchModeCommands.join("\n")} />
              </div>
              <div className="setup-block">
                <div className="setup-block-head">
                  <strong>Проверка</strong>
                  <button type="button" className="ghost" onClick={() => void copyWorkerCommands(verifyCommands, "Проверка")}>Копировать</button>
                </div>
                <textarea readOnly rows={5} value={verifyCommands.join("\n")} />
              </div>
            </div>
            {workerSetupLoading ? <p className="muted">Обновляю команды...</p> : null}
          </div>
        ) : null}

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Воркеры</h2>
              <p className="muted">Показано {visibleWorkers.length} из {filteredWorkers.length}; всего воркеров {workers.length}.</p>
            </div>
            <div className="actions">
              <button type="button" className="ghost" onClick={() => void startAllWorkerUpdates()}>Обновить все серверы</button>
              <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
            </div>
          </div>
          <div className="filters-row">
            <label>
              <span>Поиск</span>
              <input
                value={workerSearch}
                onChange={(event) => {
                  setWorkerSearch(event.target.value);
                  setWorkerPage(1);
                }}
                placeholder="имя, IP, регион"
              />
            </label>
            <label>
              <span>Статус</span>
              <select
                value={workerStatusFilter}
                onChange={(event) => {
                  setWorkerStatusFilter(event.target.value);
                  setWorkerPage(1);
                }}
              >
                <option value="all">Все</option>
                <option value="ready">Готово</option>
                <option value="offline">Офлайн</option>
                <option value="disabled">Выключено</option>
                <option value="provisioning">Настройка</option>
              </select>
            </label>
            <label>
              <span>На странице</span>
              <select
                value={workerPageSize}
                onChange={(event) => {
                  setWorkerPageSize(Number(event.target.value));
                  setWorkerPage(1);
                }}
              >
                <option value={5}>5</option>
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
            </label>
          </div>
          <div className="user-list">
            {visibleWorkers.map((worker) => {
              const installJob = activeOrSucceededInstallJobByWorker.get(worker.id);
              const workerInstalled = Boolean(worker.last_seen_at || worker.last_heartbeat_at || installJob?.status === "succeeded");
              const installInProgress = installJob?.status === "queued" || installJob?.status === "running";
              const installDisabled = !worker.ssh_access_configured || workerInstalled || installInProgress;
              const installState = workerInstalled
                ? "уже установлен"
                : installInProgress
                  ? `установка ${formatStatusLabel(installJob?.status)}`
                  : worker.ssh_access_configured
                    ? "можно установить"
                    : "SSH не настроен";
              return (
                <article key={worker.id} className="user-card">
                <div className="user-card-head">
                  <div>
                    <strong>{worker.name}</strong>
                    <p>
                      <span className={statusClass(worker.status)}>{formatStatusLabel(worker.status)}</span>
                      <span className="muted"> {worker.ip_address ?? "нет IP"} | {worker.region ?? "нет региона"} | {worker.registrar_slug}</span>
                    </p>
                  </div>
                  <div className="muted">видели: {formatDateTime(worker.last_seen_at)}</div>
                </div>
                <div className="key-value">
                  <div><span>Текущий / Целевой / Макс. RPS</span><strong>{worker.current_rps} / {worker.target_rps} / {worker.max_rps}</strong></div>
                  <div><span>Текущая емкость</span><strong>{worker.current_capacity_rps}</strong></div>
                  <div><span>CPU / RAM</span><strong>{worker.cpu_load}% / {worker.ram_usage_percent}%</strong></div>
                  <div><span>Сдвиг часов</span><strong>{worker.clock_drift_ms} ms</strong></div>
                  <div><span>Режим воркера</span><strong>{formatWorkerRuntimeMode(worker.runtime_mode)}</strong></div>
                  <div><span>Параллельность реги</span><strong>{formatWorkerConcurrency(worker)}</strong></div>
                  <div><span>SSH доступ</span><strong>{worker.ssh_access_configured ? `${worker.ssh_username ?? "root"}@${worker.ssh_host ?? worker.ip_address}:${worker.ssh_port}` : "не настроен"}</strong></div>
                  <div><span>Установка</span><strong>{installState}</strong></div>
                  <div><span>Последнее обслуживание</span><strong>{formatMaintenanceSummary(latestWorkerMaintenanceJobByWorker.get(worker.id))}</strong></div>
                  <div><span>Доменов на воркере</span><strong>{worker.current_domain_count}</strong></div>
                  <div><span>Закрепленный аккаунт</span><strong>{worker.assigned_registrar_account_id ? accountMap.get(worker.assigned_registrar_account_id)?.name ?? worker.assigned_registrar_account_id : "не закреплен"}</strong></div>
                  <div><span>ID воркера</span><strong>{worker.id}</strong></div>
                  <div><span>Токен control</span><strong>{worker.control_token ?? "создается автоматически"}</strong></div>
                </div>
                <div className="actions">
                  <button type="button" className="ghost" onClick={() => void loadWorkerSetup(worker)}>Команды установки</button>
                  <button type="button" className="ghost" onClick={() => void startWorkerMaintenance(worker, "check")} disabled={!worker.ssh_access_configured}>Проверить SSH</button>
                  <button type="button" className="ghost" onClick={() => void startWorkerMaintenance(worker, "install")} disabled={installDisabled}>Установить воркер</button>
                  <button type="button" className="ghost" onClick={() => void startWorkerMaintenance(worker, "update")} disabled={!worker.ssh_access_configured}>Обновить сервер</button>
                  <button type="button" className="ghost" onClick={() => startEditWorker(worker)}>Редактировать</button>
                  <button type="button" className="ghost" onClick={() => void toggleWorker(worker)}>{worker.is_enabled ? "Выключить" : "Включить"}</button>
                  <button type="button" className="danger" onClick={() => void deleteItem("worker", worker.id)}>Удалить</button>
                </div>
              </article>
              );
            })}
            {visibleWorkers.length === 0 ? <p className="empty">Воркеры по фильтрам не найдены.</p> : null}
          </div>
          <div className="pagination">
            <button type="button" className="ghost" onClick={() => setWorkerPage((page) => Math.max(1, page - 1))} disabled={workerCurrentPage <= 1}>Назад</button>
            <strong>Страница {workerCurrentPage} / {workerTotalPages}</strong>
            <button type="button" className="ghost" onClick={() => setWorkerPage((page) => Math.min(workerTotalPages, page + 1))} disabled={workerCurrentPage >= workerTotalPages}>Вперед</button>
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Обслуживание серверов</h2>
              <p className="muted">Показано {visibleMaintenanceJobs.length} из {filteredMaintenanceJobs.length}; всего задач {workerMaintenanceJobs.length}.</p>
            </div>
            <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          </div>
          <div className="filters-row">
            <label>
              <span>Действие</span>
              <select
                value={maintenanceActionFilter}
                onChange={(event) => {
                  setMaintenanceActionFilter(event.target.value);
                  setMaintenancePage(1);
                }}
              >
                <option value="all">Все</option>
                <option value="install">Установка</option>
                <option value="update">Обновление</option>
                <option value="check">Проверка SSH</option>
              </select>
            </label>
            <label>
              <span>Статус</span>
              <select
                value={maintenanceStatusFilter}
                onChange={(event) => {
                  setMaintenanceStatusFilter(event.target.value);
                  setMaintenancePage(1);
                }}
              >
                <option value="all">Все</option>
                <option value="queued">В очереди</option>
                <option value="running">В работе</option>
                <option value="succeeded">Успех</option>
                <option value="failed">Сбой</option>
              </select>
            </label>
            <label>
              <span>На странице</span>
              <select
                value={maintenancePageSize}
                onChange={(event) => {
                  setMaintenancePageSize(Number(event.target.value));
                  setMaintenancePage(1);
                }}
              >
                <option value={10}>10</option>
                <option value={20}>20</option>
                <option value={50}>50</option>
              </select>
            </label>
          </div>
          {visibleMaintenanceJobs.length ? (
            <div className="simple-table">
              <table>
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Воркер</th>
                    <th>Действие</th>
                    <th>Статус</th>
                    <th>Создано</th>
                    <th>Ошибка</th>
                  </tr>
                </thead>
                <tbody>
                  {visibleMaintenanceJobs.map((job) => (
                    <tr key={job.id}>
                      <td>{job.id}</td>
                      <td>{workers.find((worker) => worker.id === job.worker_id)?.name ?? job.worker_id}</td>
                      <td>{formatMaintenanceAction(job.action)}</td>
                      <td><span className={statusClass(job.status)}>{formatStatusLabel(job.status)}</span></td>
                      <td>{formatDateTime(job.created_at)}</td>
                      <td>{job.error_message ?? "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <p className="muted">Задач обслуживания по фильтрам нет.</p>
          )}
          <div className="pagination">
            <button type="button" className="ghost" onClick={() => setMaintenancePage((page) => Math.max(1, page - 1))} disabled={maintenanceCurrentPage <= 1}>Назад</button>
            <strong>Страница {maintenanceCurrentPage} / {maintenanceTotalPages}</strong>
            <button type="button" className="ghost" onClick={() => setMaintenancePage((page) => Math.min(maintenanceTotalPages, page + 1))} disabled={maintenanceCurrentPage >= maintenanceTotalPages}>Вперед</button>
          </div>
        </div>
      </section>
    );
  }

  function renderAccounts() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Аккаунт регистратора</h2>
          <form className="form" onSubmit={submitAccount}>
            <div className="form two-columns">
              <label><span>Имя</span><input value={accountForm.name} onChange={(event) => setAccountForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>Регистратор</span><input value={accountForm.registrarSlug} onChange={(event) => setAccountForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
              <label><span>API токен</span><input value={accountForm.apiToken} onChange={(event) => setAccountForm((current) => ({ ...current, apiToken: event.target.value }))} /></label>
              <label><span>API base URL</span><input value={accountForm.apiBaseUrl} onChange={(event) => setAccountForm((current) => ({ ...current, apiBaseUrl: event.target.value }))} placeholder="https://api.gandi.net/v5/domain/domains" /></label>
              <label><span>sharing_id</span><input value={accountForm.sharingId} onChange={(event) => setAccountForm((current) => ({ ...current, sharingId: event.target.value }))} /></label>
              <label>
                <span>Контакт по умолчанию</span>
                <select value={accountForm.defaultContactProfileId} onChange={(event) => setAccountForm((current) => ({ ...current, defaultContactProfileId: event.target.value }))}>
                  <option value="">Не назначен</option>
                  {contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.label}</option>)}
                </select>
              </label>
              <label className="checkbox"><input type="checkbox" checked={accountForm.supportsDryRun} onChange={(event) => setAccountForm((current) => ({ ...current, supportsDryRun: event.target.checked }))} /><span>Dry-Run</span></label>
              <label className="checkbox"><input type="checkbox" checked={accountForm.isActive} onChange={(event) => setAccountForm((current) => ({ ...current, isActive: event.target.checked }))} /><span>Активен</span></label>
            </div>
            <label><span>Заметки</span><textarea rows={3} value={accountForm.notes} onChange={(event) => setAccountForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить аккаунт</button>
          </form>
        </div>

        <div className="card">
          <h2>Подсказка по Gandi</h2>
          <p className="muted">
            Для Gandi нужен Bearer PAT, корректный contact profile и при необходимости свой `api_base_url` для sandbox
            или альтернативной среды. Проверка аккаунта в панели сейчас проверяет авторизацию, а реальную create-схему
            лучше подтверждать через `Dry run` на конкретном домене.
          </p>
        </div>

        <div className="card full-span">
          <div className="proxy-list">
            {accounts.map((account) => (
              <article key={account.id} className="proxy-row">
                <div>
                  <strong>{account.name}</strong>
                  <p>{account.registrar_slug} | статус: <span className={statusClass(account.last_validation_status)}>{formatStatusLabel(account.last_validation_status)}</span></p>
                  <p>контакт: {account.default_contact_profile_id ? contactMap.get(account.default_contact_profile_id)?.label ?? account.default_contact_profile_id : "не назначен"}</p>
                  <p>проверено: {formatDateTime(account.last_validated_at)}</p>
                  {account.last_validation_message ? <p className="row-hint">{account.last_validation_message}</p> : null}
                </div>
                <div className="actions">
                  <button type="button" className="ghost" onClick={() => void validateAccount(account.id)}>Проверить</button>
                  <button type="button" className="ghost" onClick={() => void prefillContactFromAccount(account)}>Заполнить контакт</button>
                  <button type="button" className="danger" onClick={() => void deleteItem("account", account.id)}>Удалить</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderStrategies() {
    const strategyNowIso = new Date().toISOString();
    const strategyPresets = [
      {
        zone: "fr",
        title: ".fr FRNIC",
        scheduleLabel: "каждый час",
        startTime: "00:31:30",
        endTime: "00:33:05",
        localWindowLabel: "31:30 → 33:05",
        timezoneName: "Europe/Paris",
      },
      { zone: "com", title: ".com Verisign", scheduleLabel: "ежедневно", startTime: "18:00:00", endTime: "18:45:00", timezoneName: "UTC" },
      { zone: "net", title: ".net Verisign", scheduleLabel: "ежедневно", startTime: "18:00:00", endTime: "18:45:00", timezoneName: "UTC" },
      { zone: "org", title: ".org PIR", scheduleLabel: "ежедневно", startTime: "15:14:30", endTime: "15:16:10", timezoneName: "UTC" },
      { zone: "us", title: ".us Registry Services", scheduleLabel: "ежедневно", startTime: "00:01:00", endTime: "00:03:00", timezoneName: "UTC" },
      { zone: "ae", title: ".ae TDRA", scheduleLabel: "ежедневно", startTime: "03:32:30", endTime: "03:34:10", timezoneName: "Asia/Dubai" },
      { zone: "se", title: ".se IIS", scheduleLabel: "ежедневно", startTime: "06:00:45", endTime: "06:04:15", timezoneName: "Europe/Stockholm" },
      { zone: "bg", title: ".bg Register.BG", scheduleLabel: "ежедневно", startTime: "01:43:30", endTime: "01:50:30", timezoneName: "Europe/Sofia" },
      { zone: "hr", title: ".hr CARNet", scheduleLabel: "ежедневно", startTime: "05:29:30", endTime: "05:56:30", timezoneName: "Europe/Zagreb" },
      { zone: "ee", title: ".ee EIS", scheduleLabel: "ежедневно", startTime: "00:04:30", endTime: "00:06:50", timezoneName: "Europe/Tallinn" },
      { zone: "rs", title: ".rs RNIDS", scheduleLabel: "ежедневно", startTime: "20:15:30", endTime: "20:16:50", timezoneName: "Europe/Belgrade" },
      { zone: "nl", title: ".nl SIDN", scheduleLabel: "ежедневно", startTime: "01:59:30", endTime: "02:04:30", timezoneName: "Europe/Amsterdam" },
      { zone: "no", title: ".no Norid", scheduleLabel: "ежедневно", startTime: "03:16:30", endTime: "03:20:15", timezoneName: "Europe/Oslo" },
      { zone: "me", title: ".me DoMEn", scheduleLabel: "ежедневно", startTime: "18:59:30", endTime: "19:01:30", timezoneName: "Europe/Podgorica" },
      { zone: "mk", title: ".mk MARnet", scheduleLabel: "ежедневно", startTime: "21:59:30", endTime: "22:15:30", timezoneName: "Europe/Skopje" },
      { zone: "sk", title: ".sk SK-NIC", scheduleLabel: "ежедневно", startTime: "01:59:30", endTime: "02:14:30", timezoneName: "Europe/Bratislava" },
      { zone: "tr", title: ".tr TRABIS", scheduleLabel: "ежедневно", startTime: "00:49:30", endTime: "00:51:10", timezoneName: "Europe/Istanbul" },
    ];

    return (
      <section className="stack strategies-page">
        <div className="card full-span">
          <div className="card-head strategy-topline">
            <div>
              <h2>Стратегии зон</h2>
              <p className="muted">
                Здесь задается, когда домены конкретной зоны нужно атаковать: часовой пояс, окна дропа и распределение RPS.
              </p>
            </div>
            <div className="strategy-controls">
              <label>
                <span>Текущая стратегия</span>
                <select
                  value={selectedStrategyId ?? ""}
                  onChange={(event) => setSelectedStrategyId(event.target.value ? Number(event.target.value) : null)}
                >
                  {strategies.length === 0 ? <option value="">Стратегий пока нет</option> : null}
                  {strategies.map((strategy) => (
                    <option key={strategy.id} value={strategy.id}>
                      .{strategy.zone} — {strategy.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                <span>Дата предпросмотра</span>
                <input type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} />
              </label>
            </div>
          </div>

          <div className="strategy-summary">
            <article>
              <span>Выбрана зона</span>
              <strong>{selectedStrategy ? `.${selectedStrategy.zone}` : "—"}</strong>
            </article>
            <article>
              <span>Часовой пояс</span>
              <strong>{selectedStrategy?.timezone_name ?? "—"}</strong>
            </article>
            <article>
              <span>Окон в стратегии</span>
              <strong>{strategyRules.length}</strong>
            </article>
            <article>
              <span>Активных стратегий</span>
              <strong>{strategies.filter((item) => item.is_active).length} / {strategies.length}</strong>
            </article>
          </div>
        </div>

        <div className="card full-span preset-card">
          <div className="card-head">
            <div>
              <h2>Быстрый старт</h2>
              <p className="muted">Создай стандартную стратегию зоны одной кнопкой. Если она уже есть, панель просто выберет ее.</p>
            </div>
          </div>
          <div className="preset-actions">
            {strategyPresets.map((preset) => (
              <button key={preset.zone} type="button" className="ghost preset-button" onClick={() => void createPresetStrategy(preset.zone)}>
                <strong>{preset.title}</strong>
                <span>{formatPresetDescription(preset, previewDate)}</span>
              </button>
            ))}
          </div>
        </div>

        <section className="grid two compact-section">
          <details className="card collapsible-card">
          <summary>
            <span>Ручное создание стратегии</span>
            <small>Нужно только для нестандартных зон или новых гипотез</small>
          </summary>
          <form className="form" onSubmit={submitStrategy}>
            <div className="form two-columns">
              <label>
                <span>Зона</span>
                <input value={strategyForm.zone} onChange={(event) => setStrategyForm((current) => ({ ...current, zone: event.target.value }))} />
              </label>
              <label>
                <span>Название</span>
                <input value={strategyForm.name} onChange={(event) => setStrategyForm((current) => ({ ...current, name: event.target.value }))} />
              </label>
              <label>
                <span>Часовой пояс</span>
                <input value={strategyForm.timezoneName} onChange={(event) => setStrategyForm((current) => ({ ...current, timezoneName: event.target.value }))} />
              </label>
              <label>
                <span>Как выбирать окно</span>
                <select value={strategyForm.ruleResolutionMode} onChange={(event) => setStrategyForm((current) => ({ ...current, ruleResolutionMode: event.target.value }))}>
                  <option value="priority">по приоритету</option>
                  <option value="merge">объединять окна</option>
                </select>
              </label>
              <label>
                <span>Мин. гарантированный RPS</span>
                <input value={strategyForm.defaultMinGuaranteedRps} onChange={(event) => setStrategyForm((current) => ({ ...current, defaultMinGuaranteedRps: event.target.value }))} />
              </label>
              <label>
                <span>Регистратор</span>
                <input value={strategyForm.defaultRegistrarSlug} onChange={(event) => setStrategyForm((current) => ({ ...current, defaultRegistrarSlug: event.target.value }))} />
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={strategyForm.isActive} onChange={(event) => setStrategyForm((current) => ({ ...current, isActive: event.target.checked }))} />
                <span>Активна</span>
              </label>
            </div>
            <label>
              <span>Gandi: параметры контакта (JSON)</span>
              <textarea
                rows={2}
                value={strategyForm.gandiContactExtraParameters}
                onChange={(event) => setStrategyForm((current) => ({ ...current, gandiContactExtraParameters: event.target.value }))}
                placeholder='{"x-se_ident_number":"AB1234567"}'
              />
              <small>Уходит в owner/admin/bill/tech.extra_parameters. Используй для зон, где Gandi требует ID/паспорт.</small>
            </label>
            <label>
              <span>Gandi: параметры регистрации (JSON)</span>
              <textarea
                rows={2}
                value={strategyForm.gandiRegistrationExtraParameters}
                onChange={(event) => setStrategyForm((current) => ({ ...current, gandiRegistrationExtraParameters: event.target.value }))}
                placeholder='{"premium":false}'
              />
              <small>Уходит в верхний extra_parameters операции регистрации. Для .no сейчас оставь пустым, раз dry-run дает HTTP 200.</small>
            </label>
            <label>
              <span>Заметки</span>
              <textarea rows={3} value={strategyForm.notes} onChange={(event) => setStrategyForm((current) => ({ ...current, notes: event.target.value }))} />
            </label>
            <button type="submit">Создать стратегию</button>
          </form>
          </details>

          <div className="card">
          <h2>Как это читать</h2>
          <p className="muted">
            Стратегия привязана к зоне. Внутри стратегии находятся окна дропа. Если окон несколько,
            режим “по приоритету” берет самое важное окно, а “объединять окна” оставляет все подходящие окна.
          </p>
          <div className="key-value compact">
            <div><span>Режим выбранной стратегии</span><strong>{formatResolutionMode(selectedStrategy?.rule_resolution_mode)}</strong></div>
            <div><span>RPS по умолчанию</span><strong>{selectedStrategy?.default_min_guaranteed_rps ?? "—"}</strong></div>
            <div><span>Регистратор</span><strong>{selectedStrategy?.default_registrar_slug ?? "—"}</strong></div>
            <div>
              <span>Gandi контакт</span>
              <strong>{selectedStrategy?.gandi_contact_extra_parameters ? "задан JSON" : "—"}</strong>
            </div>
            <div>
              <span>Gandi регистрация</span>
              <strong>{selectedStrategy?.gandi_registration_extra_parameters ? "задан JSON" : "—"}</strong>
            </div>
            <div>
              <span>Время стратегии</span>
              <strong>{selectedStrategy?.timezone_name ?? "—"}</strong>
              {selectedStrategy ? (
                <div className="row-hint">
                  сейчас: {formatTimeInZone(strategyNowIso, selectedStrategy.timezone_name)} локально / {formatTimeInZone(strategyNowIso, "Europe/Moscow")} MSK
                </div>
              ) : null}
            </div>
          </div>
          <form className="form" onSubmit={saveSelectedStrategyGandiParameters}>
            <label>
              <span>Gandi: параметры контакта выбранной зоны</span>
              <textarea
                rows={2}
                value={strategyGandiForm.contactExtraParameters}
                onChange={(event) => setStrategyGandiForm((current) => ({ ...current, contactExtraParameters: event.target.value }))}
                placeholder='{"x-se_ident_number":"AB1234567"}'
                disabled={!selectedStrategy}
              />
              <small>Пример для .se/.nu/.fi: номер документа. Для .no сейчас оставь пусто.</small>
            </label>
            <label>
              <span>Gandi: параметры регистрации выбранной зоны</span>
              <textarea
                rows={2}
                value={strategyGandiForm.registrationExtraParameters}
                onChange={(event) => setStrategyGandiForm((current) => ({ ...current, registrationExtraParameters: event.target.value }))}
                placeholder='{"premium":false}'
                disabled={!selectedStrategy}
              />
              <small>Используется только если Gandi явно требует top-level extra_parameters.</small>
            </label>
            <button type="submit" disabled={!selectedStrategy}>Сохранить Gandi параметры</button>
          </form>
          </div>
        </section>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Список стратегий</h2>
              <p className="muted">Краткая таблица всех зон. Детали окон показываются ниже для выбранной стратегии.</p>
            </div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Зона</th>
                  <th>Название</th>
                  <th>Часовой пояс</th>
                  <th>Окна</th>
                  <th>Мин. RPS</th>
                  <th>Регистратор</th>
                  <th>Статус</th>
                  <th>Действие</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((strategy) => (
                  <tr key={strategy.id}>
                    <td>.{strategy.zone}</td>
                    <td>{strategy.name}</td>
                    <td>{strategy.timezone_name}</td>
                    <td>{formatResolutionMode(strategy.rule_resolution_mode)}</td>
                    <td>{strategy.default_min_guaranteed_rps}</td>
                    <td>{strategy.default_registrar_slug}</td>
                    <td><span className={statusClass(strategy.is_active ? "ready" : "inactive")}>{strategy.is_active ? "активна" : "выключена"}</span></td>
                    <td>
                      <button type="button" className="ghost" onClick={() => setSelectedStrategyId(strategy.id)}>
                        Выбрать
                      </button>
                    </td>
                  </tr>
                ))}
                {strategies.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="empty">Стратегий пока нет</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>

        <section className="grid two compact-section">
          <details className="card collapsible-card">
          <summary>
            <span>Добавить окно дропа вручную</span>
            <small>Используй, если пресет зоны не подходит</small>
          </summary>
          <p className="muted">Окно добавляется в выбранную сверху стратегию.</p>
          <form className="form" onSubmit={submitRule}>
            <div className="form two-columns">
              <label><span>Название окна</span><input value={ruleForm.name} onChange={(event) => setRuleForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label>
                <span>Расписание</span>
                <select value={ruleForm.scheduleType} onChange={(event) => setRuleForm((current) => ({ ...current, scheduleType: event.target.value }))}>
                  <option value="hourly">каждый час</option>
                  <option value="daily">каждый день</option>
                  <option value="weekly">по дням недели</option>
                  <option value="one_time">один раз</option>
                </select>
              </label>
              <label><span>Час</span><input value={ruleForm.hour} onChange={(event) => setRuleForm((current) => ({ ...current, hour: event.target.value }))} placeholder="пусто = каждый час" /></label>
              <label><span>Минута</span><input value={ruleForm.minute} onChange={(event) => setRuleForm((current) => ({ ...current, minute: event.target.value }))} /></label>
              <label><span>Секунда</span><input value={ruleForm.second} onChange={(event) => setRuleForm((current) => ({ ...current, second: event.target.value }))} /></label>
              <label><span>Приоритет</span><input value={ruleForm.priority} onChange={(event) => setRuleForm((current) => ({ ...current, priority: event.target.value }))} /></label>
              <label><span>Дни недели</span><input value={ruleForm.weekdays} onChange={(event) => setRuleForm((current) => ({ ...current, weekdays: event.target.value }))} placeholder="1,3,5" /></label>
              <label><span>Конкретная дата</span><input type="date" value={ruleForm.specificDate} onChange={(event) => setRuleForm((current) => ({ ...current, specificDate: event.target.value }))} /></label>
              <label><span>Длина окна, сек</span><input value={ruleForm.windowDurationSeconds} onChange={(event) => setRuleForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
              <label>
                <span>Подача запросов</span>
                <select value={ruleForm.executionProfileMode} onChange={(event) => setRuleForm((current) => ({ ...current, executionProfileMode: event.target.value }))}>
                  <option value="flat">ровно</option>
                  <option value="phased">по фазам</option>
                </select>
              </label>
              <label className="checkbox"><input type="checkbox" checked={ruleForm.isEnabled} onChange={(event) => setRuleForm((current) => ({ ...current, isEnabled: event.target.checked }))} /><span>Включено</span></label>
            </div>
            <label><span>Заметки</span><textarea rows={2} value={ruleForm.notes} onChange={(event) => setRuleForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить окно</button>
          </form>
          </details>

          <details className="card collapsible-card">
          <summary>
            <span>Фазы RPS</span>
            <small>Дополнительная настройка нагрузки внутри окна</small>
          </summary>
          <p className="muted">Фазы нужны только если у окна выбран режим “по фазам”.</p>
          <form className="form" onSubmit={submitPhase}>
            <div className="form two-columns">
              <label>
                <span>Окно</span>
                <select value={phaseForm.ruleId} onChange={(event) => setPhaseForm((current) => ({ ...current, ruleId: event.target.value }))}>
                  <option value="">Выбери окно</option>
                  {strategyRules.map((rule) => (
                    <option key={rule.id} value={rule.id}>{rule.name}</option>
                  ))}
                </select>
              </label>
              <label><span>Название фазы</span><input value={phaseForm.name} onChange={(event) => setPhaseForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>Порядок</span><input value={phaseForm.sortOrder} onChange={(event) => setPhaseForm((current) => ({ ...current, sortOrder: event.target.value }))} /></label>
              <label><span>Старт через, сек</span><input value={phaseForm.startOffsetSeconds} onChange={(event) => setPhaseForm((current) => ({ ...current, startOffsetSeconds: event.target.value }))} /></label>
              <label><span>Длительность, сек</span><input value={phaseForm.durationSeconds} onChange={(event) => setPhaseForm((current) => ({ ...current, durationSeconds: event.target.value }))} /></label>
              <label>
                <span>Режим RPS</span>
                <select value={phaseForm.rpsMode} onChange={(event) => setPhaseForm((current) => ({ ...current, rpsMode: event.target.value }))}>
                  <option value="percent">процент</option>
                  <option value="fixed">фиксированно</option>
                </select>
              </label>
              <label><span>Значение RPS</span><input value={phaseForm.rpsValue} onChange={(event) => setPhaseForm((current) => ({ ...current, rpsValue: event.target.value }))} /></label>
              <label className="checkbox"><input type="checkbox" checked={phaseForm.stopOnSuccess} onChange={(event) => setPhaseForm((current) => ({ ...current, stopOnSuccess: event.target.checked }))} /><span>Остановить при успехе</span></label>
            </div>
            <button type="submit">Добавить фазу</button>
          </form>
          </details>
        </section>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Окна и фазы</h2>
              <p className="muted">Текущая стратегия: {selectedStrategy ? `.${selectedStrategy.zone} — ${selectedStrategy.name}` : "не выбрана"}.</p>
            </div>
          </div>
          <div className="strategy-rule-list">
            {strategyRules.map((rule) => {
              const previewWindow = strategyPreview?.windows.find((window) => window.rule_id === rule.id);
              return (
              <article key={rule.id} className="strategy-rule-card">
                <div className="domain-card-head compact-head">
                  <div>
                    <div className="domain-title-row">
                      <h3>{rule.name}</h3>
                      <span className={statusClass(rule.is_enabled ? "ready" : "inactive")}>{formatScheduleType(rule.schedule_type)}</span>
                    </div>
                    <p className="muted">
                      приоритет {rule.priority} | местное время {formatRuleLocalTime(rule)} | окно {rule.window_duration_seconds} сек | запросы: {formatExecutionMode(rule.execution_profile_mode)}
                    </p>
                    {previewWindow ? <p className="row-hint">MSK по выбранной дате: {formatPreviewWindowMsk(previewWindow)}</p> : null}
                  </div>
                  <div className="actions">
                    <button type="button" className="danger" onClick={() => void removeZoneRule(rule.id)}>Удалить окно</button>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Фаза</th>
                        <th>Порядок</th>
                        <th>Старт через</th>
                        <th>Длительность</th>
                        <th>Режим</th>
                        <th>Значение</th>
                        <th>Стоп</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {(rulePhases[rule.id] ?? []).map((phase) => (
                        <tr key={phase.id}>
                          <td>{phase.name}</td>
                          <td>{phase.sort_order}</td>
                          <td>{phase.start_offset_seconds} сек</td>
                          <td>{phase.duration_seconds} сек</td>
                          <td>{formatRpsModeLabel(phase.rps_mode)}</td>
                          <td>{phase.rps_value}</td>
                          <td>{phase.stop_on_success ? "да" : "нет"}</td>
                          <td><button type="button" className="danger" onClick={() => void removeZonePhase(phase.id)}>Удалить</button></td>
                        </tr>
                      ))}
                      {(rulePhases[rule.id] ?? []).length === 0 ? (
                        <tr>
                          <td colSpan={8} className="empty">Фаз пока нет</td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              </article>
              );
            })}
            {strategyRules.length === 0 ? <p className="empty">У выбранной стратегии пока нет окон</p> : null}
          </div>
        </div>

        <div className="card full-span">
          <h2>Предпросмотр расписания</h2>
          <div className="key-value compact">
            <div><span>Часовой пояс</span><strong>{strategyPreview?.timezone_name ?? selectedStrategy?.timezone_name ?? "—"}</strong></div>
            <div><span>Как выбираются окна</span><strong>{formatResolutionMode(strategyPreview?.resolution_mode ?? selectedStrategy?.rule_resolution_mode)}</strong></div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Окно</th>
                  <th>Приоритет</th>
                  <th>Старт</th>
                  <th>Конец</th>
                </tr>
              </thead>
              <tbody>
                {(strategyPreview?.windows ?? []).map((window) => {
                  const timezoneName = strategyPreview?.timezone_name ?? selectedStrategy?.timezone_name ?? "UTC";
                  return (
                  <tr key={`${window.rule_id}-${window.start_at}`}>
                    <td>{window.rule_name ?? `окно #${window.rule_id}`}</td>
                    <td>{window.priority}</td>
                    <td>
                      <div>{formatDateTimeInZone(window.start_at, timezoneName, "локально")}</div>
                      <div className="row-hint">{formatDateTime(window.start_at)}</div>
                    </td>
                    <td>
                      <div>{formatDateTimeInZone(window.end_at, timezoneName, "локально")}</div>
                      <div className="row-hint">{formatDateTime(window.end_at)}</div>
                    </td>
                  </tr>
                  );
                })}
                {(strategyPreview?.windows ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty">На эту дату окна не найдены</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    );
  }

  function renderContacts() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>{editingContactId ? "Редактирование контакта" : "Профиль контакта"}</h2>
          {editingContactId ? (
            <p className="row-hint">Сейчас редактируется существующий профиль контакта. Сохрани изменения или нажми отмену.</p>
          ) : null}
          <form className="form" onSubmit={submitContact}>
            <div className="form two-columns">
              <label><span>Метка</span><input value={contactForm.label} onChange={(event) => setContactForm((current) => ({ ...current, label: event.target.value }))} /></label>
              <label><span>Тип</span><input value={contactForm.personType} onChange={(event) => setContactForm((current) => ({ ...current, personType: event.target.value }))} /></label>
              <label><span>Имя</span><input value={contactForm.givenName} onChange={(event) => setContactForm((current) => ({ ...current, givenName: event.target.value }))} /></label>
              <label><span>Фамилия</span><input value={contactForm.familyName} onChange={(event) => setContactForm((current) => ({ ...current, familyName: event.target.value }))} /></label>
              <label><span>Организация</span><input value={contactForm.organizationName} onChange={(event) => setContactForm((current) => ({ ...current, organizationName: event.target.value }))} /></label>
              <label><span>Email</span><input value={contactForm.email} onChange={(event) => setContactForm((current) => ({ ...current, email: event.target.value }))} /></label>
              <label><span>Телефон</span><input value={contactForm.phone} onChange={(event) => setContactForm((current) => ({ ...current, phone: event.target.value }))} /></label>
              <label><span>Мобильный</span><input value={contactForm.mobile} onChange={(event) => setContactForm((current) => ({ ...current, mobile: event.target.value }))} /></label>
              <label><span>Fax</span><input value={contactForm.fax} onChange={(event) => setContactForm((current) => ({ ...current, fax: event.target.value }))} /></label>
              <label><span>Язык</span><input value={contactForm.lang} onChange={(event) => setContactForm((current) => ({ ...current, lang: event.target.value }))} /></label>
              <label><span>Улица</span><input value={contactForm.streetAddress} onChange={(event) => setContactForm((current) => ({ ...current, streetAddress: event.target.value }))} /></label>
              <label><span>Город</span><input value={contactForm.city} onChange={(event) => setContactForm((current) => ({ ...current, city: event.target.value }))} /></label>
              <label><span>Регион/штат</span><input value={contactForm.state} onChange={(event) => setContactForm((current) => ({ ...current, state: event.target.value }))} /></label>
              <label><span>Индекс</span><input value={contactForm.zipCode} onChange={(event) => setContactForm((current) => ({ ...current, zipCode: event.target.value }))} /></label>
              <label><span>Страна</span><input value={contactForm.countryCode} onChange={(event) => setContactForm((current) => ({ ...current, countryCode: event.target.value }))} /></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.dataObfuscated} onChange={(event) => setContactForm((current) => ({ ...current, dataObfuscated: event.target.checked }))} /><span>Скрывать данные</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.mailObfuscated} onChange={(event) => setContactForm((current) => ({ ...current, mailObfuscated: event.target.checked }))} /><span>Скрывать email</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.icannContractAccept} onChange={(event) => setContactForm((current) => ({ ...current, icannContractAccept: event.target.checked }))} /><span>ICANN принят</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.isDefault} onChange={(event) => setContactForm((current) => ({ ...current, isDefault: event.target.checked }))} /><span>По умолчанию</span></label>
            </div>
            <label><span>Заметки</span><textarea rows={3} value={contactForm.notes} onChange={(event) => setContactForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">{editingContactId ? "Сохранить контакт" : "Добавить профиль контакта"}</button>
              {editingContactId ? (
                <button
                  type="button"
                  className="ghost"
                  onClick={() => {
                    setEditingContactId(null);
                    setContactForm(DEFAULT_CONTACT_FORM);
                  }}
                >
                  Отмена
                </button>
              ) : null}
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Логика контактов</h2>
          <p className="muted">
            Проект уже поддерживает несколько профилей контактов. Один можно сделать дефолтным, остальные назначать под
            аккаунты или домены отдельно, без необходимости решать это один раз и навсегда прямо сейчас.
          </p>
        </div>

        <div className="card full-span">
          <div className="promo-list">
            {contacts.map((contact) => (
              <article key={contact.id} className="promo-row">
                <strong>{contact.label}</strong>
                <p>{contact.given_name} {contact.family_name} | {contact.person_type}{contact.is_default ? " | по умолчанию" : ""}</p>
                <p>{contact.email} | {contact.phone}{contact.mobile ? ` | мобильный ${contact.mobile}` : ""}</p>
                <p>{contact.street_address}, {contact.city}, {contact.zip_code}, {contact.country_code}</p>
                {contact.lang || contact.icann_contract_accept !== null || contact.extra_parameters ? (
                  <p className="row-hint">
                    {contact.lang ? `язык ${contact.lang}` : "язык —"}
                    {contact.icann_contract_accept !== null ? ` | ICANN ${String(contact.icann_contract_accept)}` : ""}
                    {contact.extra_parameters ? " | extra JSON задан" : ""}
                  </p>
                ) : null}
                <div className="actions">
                  <button type="button" onClick={() => editContact(contact)}>Редактировать</button>
                  <button type="button" className="danger" onClick={() => void deleteItem("contact", contact.id)}>Удалить</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderAttacks() {
    const tasksByRun = groupTasksByRun(tasks);
    const eventsByRun = groupEventsByRun(events);
    const attackSummaries = new Map(
      attacks.map((attack) => [
        attack.id,
        summarizeAttackRun(attack, tasksByRun.get(attack.id) ?? [], eventsByRun.get(attack.id) ?? []),
      ]),
    );
    const recentAttacks = attacks.slice(0, 6);

    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Управление атаками</h2>
              <p className="muted">По умолчанию используется приоритетная стратегия: сначала насыщаем мощностью более важные домены.</p>
            </div>
            <div className="actions">
              <button type="button" onClick={() => void startTodayAttacks(false)}>Старт на сегодня</button>
              <button type="button" className="ghost" onClick={() => void startTodayAttacks(true)}>Пересобрать</button>
              <button type="button" className="ghost" onClick={() => void rebalanceAttacksNow()}>Перераспределить</button>
              <button type="button" className="danger" onClick={() => void stopAllAttacks()}>Стоп всё</button>
            </div>
          </div>
          <div className="key-value compact">
            <div><span>Запланировано запусков</span><strong>{overview?.scheduled_runs ?? 0}</strong></div>
            <div><span>Запущено сейчас</span><strong>{overview?.running_runs ?? 0}</strong></div>
            <div><span>Целевая мощность</span><strong>{overview?.capacity.target_rps ?? 0}</strong></div>
            <div><span>Макс. мощность</span><strong>{overview?.capacity.max_rps ?? 0}</strong></div>
          </div>
        </div>

        <div className="card">
          <h2>Поведение планировщика</h2>
          <p className="muted">
            При старте control строит запуски атак и задачи воркеров для доменов с дропом сегодня. Освобождение воркеров после
            успешной регистрации и их повторное распределение будет реализовано на worker/runtime этапе.
          </p>
        </div>

        <div className="card full-span">
          <h2>Запуски атак</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Домен</th>
                  <th>Статус</th>
                  <th>Фаза</th>
                  <th>Старт</th>
                  <th>Конец</th>
                  <th>Воркеры</th>
                  <th>Рабочий RPS</th>
                  <th>План RPS</th>
                  <th>Текущий RPS</th>
                  <th>Макс. RPS</th>
                  <th>Итог окна</th>
                </tr>
              </thead>
              <tbody>
                {attacks.map((attack) => {
                  const summary = attackSummaries.get(attack.id);
                  return (
                    <tr key={attack.id}>
                      <td>{attack.id}</td>
                      <td>{domainMap.get(attack.domain_id)?.fqdn ?? attack.domain_id}</td>
                      <td><span className={statusClass(attack.status)}>{formatStatusLabel(attack.status)}</span></td>
                      <td>{attack.runtime_phase_name ?? domainMap.get(attack.domain_id)?.runtime_phase_name ?? "—"}</td>
                      <td>{formatDateTime(attack.planned_start_at)}</td>
                      <td>{formatDateTime(attack.planned_end_at)}</td>
                      <td>{attack.assigned_worker_count}</td>
                      <td>
                        <div>мин. {formatRps(attack.runtime_minimum_rps ?? domainMap.get(attack.domain_id)?.runtime_minimum_rps)}</div>
                        <div>желательно {formatRps(attack.runtime_desired_rps ?? domainMap.get(attack.domain_id)?.runtime_desired_rps)}</div>
                        <div>выделено {formatRps(attack.runtime_allocated_rps ?? domainMap.get(attack.domain_id)?.runtime_allocated_rps ?? attack.planned_rps)}</div>
                      </td>
                      <td>{attack.planned_rps}</td>
                      <td>{attack.current_rps}</td>
                      <td>{attack.max_rps}</td>
                      <td>
                        {summary ? (
                          <div className="run-mini-summary">
                            <span className={statusClass(summary.conclusionTone)}>{summary.conclusion}</span>
                            <div>попыток: {summary.totalAttempts}</div>
                            <div>успехов: {summary.totalSuccess}</div>
                            <div>HTTP воркеров: {summary.httpSummary}</div>
                            <div>факт: {formatRps(summary.estimatedRps)} RPS</div>
                          </div>
                        ) : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card full-span">
          <h2>Отчеты по последним окнам</h2>
          <div className="run-report-list">
            {recentAttacks.map((attack) => {
              const runTasks = (tasksByRun.get(attack.id) ?? []).slice().sort((left, right) => left.worker_id - right.worker_id);
              const runEvents = eventsByRun.get(attack.id) ?? [];
              const summary = attackSummaries.get(attack.id) ?? summarizeAttackRun(attack, runTasks, runEvents);
              const domain = domainMap.get(attack.domain_id);
              return (
                <article key={attack.id} className="run-report">
                  <div className="run-report-head">
                    <div>
                      <h3>Запуск #{attack.id} · {domain?.fqdn ?? attack.domain_id}</h3>
                      <p className="muted">{formatDateTime(attack.planned_start_at)} → {formatDateTime(attack.planned_end_at)}</p>
                    </div>
                    <span className={statusClass(summary.conclusionTone)}>{summary.conclusion}</span>
                  </div>
                  <div className="key-value compact run-metrics">
                    <div><span>Всего попыток</span><strong>{summary.totalAttempts}</strong></div>
                    <div><span>Успешных попыток</span><strong>{summary.totalSuccess}</strong></div>
                    <div><span>Расчетный факт RPS</span><strong>{formatRps(summary.estimatedRps)}</strong></div>
                    <div><span>Длительность задач</span><strong>{formatSeconds(summary.elapsedSeconds)}</strong></div>
                    <div><span>HTTP ответы</span><strong>{summary.httpSummary}</strong></div>
                    <div><span>После окна</span><strong>{summary.postWindowEvent ? summary.postWindowEvent.message : "—"}</strong></div>
                  </div>
                  {attack.stop_reason ? <p className="muted">Причина остановки: {attack.stop_reason}</p> : null}
                  <div className="worker-breakdown">
                    {runTasks.length ? runTasks.map((task) => {
                      const taskElapsed = secondsBetween(task.started_at, task.finished_at);
                      const taskRps = task.actual_rps || (taskElapsed ? task.total_attempts / taskElapsed : null);
                      const httpSummary = formatCountMap(collectTaskStatusCounts(task));
                      const errorSummary = task.response_error_counts
                        ? Object.entries(task.response_error_counts)
                            .sort(([left], [right]) => left.localeCompare(right))
                            .map(([name, count]) => `${name} x${count}`)
                            .join(", ")
                        : "";
                      const samplesSummary = formatTaskSamples(task);
                      const statusSamplesSummary = formatStatusSamples(task);
                      return (
                        <div key={task.id} className="worker-breakdown-row">
                          <div>
                            <strong>worker #{task.worker_id}</strong>
                            <span className={statusClass(task.status)}>{formatStatusLabel(task.status)}</span>
                          </div>
                          <div>попыток: <strong>{task.total_attempts}</strong></div>
                          <div>успехов: <strong>{task.success_attempts}</strong></div>
                          <div>факт: <strong>{formatRps(taskRps)} RPS</strong></div>
                          <div>HTTP: <strong>{httpSummary}</strong></div>
                          <div className="worker-error">ошибка: {extractReadableError(task.last_error)}</div>
                          {errorSummary ? <div className="worker-error">типы ошибок: {errorSummary}</div> : null}
                          {statusSamplesSummary ? <div className="worker-error">примеры HTTP: {statusSamplesSummary}</div> : null}
                          {samplesSummary ? <div className="worker-error">последние ответы: {samplesSummary}</div> : null}
                        </div>
                      );
                    }) : <p className="muted">По этому запуску задач воркеров пока нет.</p>}
                  </div>
                </article>
              );
            })}
          </div>
        </div>

        <div className="card">
          <h2>Задачи воркеров</h2>
          <div className="log-list">
            {tasks.slice(0, 20).map((task) => (
              <article key={task.id} className="log-row">
                <span className={statusClass(task.status)}>{formatStatusLabel(task.status)}</span>
                <div>
                  <strong>запуск #{task.attack_run_id} | воркер #{task.worker_id}</strong>
                  <p>
                    домен: {domainMap.get(task.domain_id)?.fqdn ?? task.domain_id}
                    {" | "}план RPS: {task.planned_rps}
                    {" | "}попыток: {task.total_attempts}
                    {" | "}успехов: {task.success_attempts}
                    {" | "}HTTP: {task.last_http_status ?? "—"}
                  </p>
                  {task.last_error ? <p>ошибка: {extractReadableError(task.last_error)}</p> : null}
                </div>
              </article>
            ))}
          </div>
        </div>

        <div className="card">
          <h2>События атак</h2>
          <div className="log-list">
            {events.slice(0, 20).map((event) => (
              <article key={event.id} className="log-row">
                <span className={statusClass(event.level)}>{event.level}</span>
                <div>
                  <strong>{formatAttackEventType(event.event_type)}</strong>
                  <p>{event.message}</p>
                  <p>{formatDateTime(event.created_at)}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderSettings() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Личный Telegram</h2>
          <form className="form" onSubmit={saveTelegram}>
            <label><span>Токен бота</span><input value={telegramForm.telegram_token} onChange={(event) => setTelegramForm((current) => ({ ...current, telegram_token: event.target.value }))} /></label>
            <label><span>Chat ID</span><input value={telegramForm.telegram_chat_id} onChange={(event) => setTelegramForm((current) => ({ ...current, telegram_chat_id: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">Сохранить</button>
              <button type="button" className="ghost" onClick={() => void api.testTelegram().then((payload) => setToast({ type: "success", text: payload.detail })).catch((error: Error) => setToast({ type: "error", text: error.message }))}>Тест</button>
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Диагностический Telegram</h2>
          <form className="form" onSubmit={saveDiagnosticTelegram}>
            <label><span>Токен бота</span><input value={diagnosticTelegram.telegram_token ?? ""} onChange={(event) => setDiagnosticTelegram((current) => ({ ...current, telegram_token: event.target.value }))} /></label>
            <label><span>Chat ID</span><input value={diagnosticTelegram.telegram_chat_id ?? ""} onChange={(event) => setDiagnosticTelegram((current) => ({ ...current, telegram_chat_id: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">Сохранить</button>
              <button type="button" className="ghost" onClick={() => void api.testDiagnosticTelegram().then((payload) => setToast({ type: "success", text: payload.detail })).catch((error: Error) => setToast({ type: "error", text: error.message }))}>Тест</button>
            </div>
          </form>
        </div>

        <div className="card">
          <div className="card-head">
            <div>
              <h2>Discovery-проверки</h2>
              <p className="muted">
                Эти настройки управляют фоновыми проверками доменов. Control применяет их сразу, а параметры воркера попадут в
                `.env` после установки или обновления воркеров.
              </p>
            </div>
            <span className={statusClass(discoveryRuntimeSettings?.discovery_worker_enabled ? "ready" : "paused")}>
              {discoveryRuntimeSettings?.discovery_worker_enabled ? "через воркеры" : "воркеры выключены"}
            </span>
          </div>
          <form className="form" onSubmit={saveDiscoveryRuntimeSettings}>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={discoveryRuntimeForm.discoveryEnabled}
                onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryEnabled: event.target.checked }))}
              />
              <span>Включить discovery-checker</span>
            </label>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={discoveryRuntimeForm.discoveryWorkerEnabled}
                onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryWorkerEnabled: event.target.checked }))}
              />
              <span>Раздавать проверки подключенным воркерам</span>
            </label>
            <label className="inline-check">
              <input
                type="checkbox"
                checked={discoveryRuntimeForm.discoveryLocalFallbackEnabled}
                onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryLocalFallbackEnabled: event.target.checked }))}
              />
              <span>Fallback на control, если воркеров нет</span>
            </label>
            <div className="form-grid">
              <label><span>Период планировщика, сек</span><input value={discoveryRuntimeForm.discoverySchedulerIntervalSeconds} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoverySchedulerIntervalSeconds: event.target.value }))} /></label>
              <label><span>Задач за цикл</span><input value={discoveryRuntimeForm.discoveryBatchSize} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryBatchSize: event.target.value }))} /></label>
              <label><span>Потоки control fallback</span><input value={discoveryRuntimeForm.discoveryConcurrency} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryConcurrency: event.target.value }))} /></label>
              <label><span>Timeout проверки, сек</span><input value={discoveryRuntimeForm.discoveryTimeoutSeconds} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryTimeoutSeconds: event.target.value }))} /></label>
              <label><span>TTL зависшей задачи, сек</span><input value={discoveryRuntimeForm.discoveryWorkerTaskStaleSeconds} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, discoveryWorkerTaskStaleSeconds: event.target.value }))} /></label>
              <label><span>Потоки discovery на воркер</span><input value={discoveryRuntimeForm.workerDiscoveryConcurrency} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, workerDiscoveryConcurrency: event.target.value }))} /></label>
              <label><span>Polling воркера, сек</span><input value={discoveryRuntimeForm.workerDiscoveryPollIntervalSeconds} onChange={(event) => setDiscoveryRuntimeForm((current) => ({ ...current, workerDiscoveryPollIntervalSeconds: event.target.value }))} /></label>
            </div>
            <div className="actions">
              <button type="submit">Сохранить discovery-настройки</button>
            </div>
            <p className="muted">
              После изменения потоков/polling нажми во вкладке workers кнопку обновления всех воркеров, чтобы новые значения попали на серверы.
            </p>
          </form>
        </div>

        <div className="card">
          <h2>Смена пароля</h2>
          <form className="form" onSubmit={savePassword}>
            <label><span>Текущий пароль</span><input type="password" value={passwordForm.current_password} onChange={(event) => setPasswordForm((current) => ({ ...current, current_password: event.target.value }))} /></label>
            <label><span>Новый пароль</span><input type="password" value={passwordForm.new_password} onChange={(event) => setPasswordForm((current) => ({ ...current, new_password: event.target.value }))} /></label>
            <button type="submit">Изменить пароль</button>
          </form>
        </div>

        <div className="card">
          <h2>Статус билда</h2>
          <div className="key-value compact">
            <div><span>Публичная регистрация</span><strong>отключена</strong></div>
            <div><span>Создание пользователей в админке</span><strong>отключено</strong></div>
            <div><span>Control API</span><strong>включен</strong></div>
            <div><span>Runtime на воркерах</span><strong>следующий этап</strong></div>
          </div>
        </div>
      </section>
    );
  }

  if (authLoading) {
    return <div className="shell loading">Загрузка...</div>;
  }

  if (!session) {
    return renderAuth();
  }

  return (
    <div className="app-shell">
      <header className="hero hero-top">
        <div>
          <p className="eyebrow">Control-сервер</p>
          <h1>Veltrix Drop Catcher</h1>
          <p className="subtitle">
            Один control-сервер управляет списком доменов, воркерами, контактами, аккаунтами регистраторов и планированием
            атакующих окон по времени реестра.
          </p>
          <div className="hero-meta">
            <span className={statusClass(session.user.status)}>{formatStatusLabel(session.user.status)}</span>
            <span className="muted">@{session.user.username}</span>
            <span className="muted">проверено: {formatDateTime(overview?.checked_at ?? null)}</span>
          </div>
        </div>
        <div className="stats">
          <article><span>Домены</span><strong>{overview?.total_domains ?? 0}</strong></article>
          <article><span>Дроп сегодня</span><strong>{overview?.due_today_domains ?? 0}</strong></article>
          <article><span>Целевой RPS</span><strong>{overview?.capacity.target_rps ?? 0}</strong></article>
          <article><span>Макс. RPS</span><strong>{overview?.capacity.max_rps ?? 0}</strong></article>
        </div>
      </header>

      <div className="toolbar">
        <div className="tab-strip">
          {(["domains", "discovery", "scanner", "strategies", "workers", "accounts", "contacts", "attacks", "settings"] as Tab[]).map((item) => (
            <button key={item} type="button" className={tab === item ? "ghost active-chip" : "ghost"} onClick={() => setTab(item)}>
              {formatTabLabel(item)}
            </button>
          ))}
        </div>
        <div className="toolbar-actions">
          <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          <button type="button" className="ghost" onClick={() => void logout()}>Выйти</button>
        </div>
      </div>

      {toast ? <div className={`toast ${toast.type}`}>{toast.text}</div> : null}

      {tab === "domains" ? renderDomains() : null}
      {tab === "discovery" ? renderDiscovery() : null}
      {tab === "scanner" ? renderZoneScanner() : null}
      {tab === "strategies" ? renderStrategies() : null}
      {tab === "workers" ? renderWorkers() : null}
      {tab === "accounts" ? renderAccounts() : null}
      {tab === "contacts" ? renderContacts() : null}
      {tab === "attacks" ? renderAttacks() : null}
      {tab === "settings" ? renderSettings() : null}
    </div>
  );
}
