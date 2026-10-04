import type { VpnReleaseCheck, VpnReleaseReadiness } from "./api";

export const VPN_RELEASE_STATUS_LABELS: Record<VpnReleaseCheck["state"], string> = {
  pass: "Пройдено",
  warn: "Предупреждение",
  fail: "Ошибка",
};

export const VPN_RELEASE_COMMIT_PHRASE = "ГОТОВО К РЕЛИЗУ";

export type VpnReleaseCommitConfirmation = {
  phrase: string;
  checkedAt: string | null;
};

export type VpnReleaseNavigationTarget = "nodes" | "capacity" | "maintenance";

const VPN_RELEASE_NAVIGATION_DESTINATIONS = {
  nodes: { tab: "workers", elementId: "vpn-nodes-section" },
  capacity: { tab: "vpn", elementId: "vpn-capacity-section" },
  maintenance: { tab: "workers", elementId: "vpn-maintenance-section" },
} as const;

export function createVpnReleaseRequestGate() {
  let generation = 0;
  return {
    begin: () => ++generation,
    invalidate: () => {
      generation += 1;
    },
    isCurrent: (requestGeneration: number) => requestGeneration === generation,
  };
}

export function getVpnReleaseNavigationDestination(target: VpnReleaseNavigationTarget) {
  return VPN_RELEASE_NAVIGATION_DESTINATIONS[target];
}

const VPN_RELEASE_CHECK_LABELS: Readonly<Record<string, string>> = {
  system_health: "Состояние системы",
  control_health: "Служба управления",
  local_health: "Локальный контур",
  public_health: "Публичный контур",
  cabinet_health: "Кабинет клиента",
  disk_health: "Дисковое пространство",
  backup_health: "Резервное копирование",
  known_hosts: "Доверенные SSH-узлы",
  worker_active: "Активность воркера",
  worker_health: "Состояние воркера",
  endpoint_configuration: "Конфигурация точки доступа",
  endpoint_health: "Состояние точки доступа",
  endpoint_external_proof: "Внешняя проверка подключения",
  endpoint_redundancy: "Резервирование нод",
  endpoint_capacity: "Ёмкость VPN‑ноды",
  aggregate_capacity: "Общая ёмкость",
  control_operations: "Операции управления",
  maintenance: "Обслуживание",
  dispatch: "Диспетчеризация",
  release_id: "Идентификатор релиза",
  payment_disabled: "Оплата отключена",
  public_trial_disabled: "Пробный доступ отключён",
  public_trial_plan: "Тариф пробного доступа",
  portal_public_access: "Публичный доступ к кабинету",
};

export function formatVpnReleaseCheckLabel(code: string): string {
  return Object.prototype.hasOwnProperty.call(VPN_RELEASE_CHECK_LABELS, code)
    ? VPN_RELEASE_CHECK_LABELS[code]
    : "Проверка";
}

const SECTION_DEFINITIONS = [
  {
    key: "infrastructure",
    title: "Инфраструктура",
    codes: [
      "system_health",
      "control_health",
      "local_health",
      "public_health",
      "cabinet_health",
      "disk_health",
      "backup_health",
      "known_hosts",
    ],
  },
  {
    key: "nodes",
    title: "Ноды",
    codes: [
      "worker_active",
      "worker_health",
      "endpoint_configuration",
      "endpoint_health",
      "endpoint_external_proof",
      "endpoint_redundancy",
    ],
  },
  {
    key: "capacity",
    title: "Ёмкость",
    codes: ["endpoint_capacity", "aggregate_capacity"],
  },
  {
    key: "queue",
    title: "Очередь и обслуживание",
    codes: ["control_operations", "maintenance", "dispatch"],
  },
  {
    key: "product",
    title: "Продукт",
    codes: [
      "release_id",
      "payment_disabled",
      "public_trial_disabled",
      "public_trial_plan",
      "portal_public_access",
    ],
  },
] as const;

export type VpnReleaseCheckGroup = {
  key: (typeof SECTION_DEFINITIONS)[number]["key"] | "other";
  title: string;
  checks: VpnReleaseCheck[];
};

const CODE_ORDER = new Map<string, number>();
const CODE_SECTION = new Map<string, VpnReleaseCheckGroup["key"]>();

for (const section of SECTION_DEFINITIONS) {
  section.codes.forEach((code, index) => {
    CODE_ORDER.set(code, index);
    CODE_SECTION.set(code, section.key);
  });
}

function compareChecks(left: VpnReleaseCheck, right: VpnReleaseCheck): number {
  const codeDifference = (CODE_ORDER.get(left.code) ?? Number.MAX_SAFE_INTEGER)
    - (CODE_ORDER.get(right.code) ?? Number.MAX_SAFE_INTEGER);
  if (codeDifference !== 0) {
    return codeDifference;
  }
  if (left.code !== right.code) {
    return left.code < right.code ? -1 : 1;
  }
  return (left.entity_id ?? Number.MAX_SAFE_INTEGER) - (right.entity_id ?? Number.MAX_SAFE_INTEGER);
}

export function groupVpnReleaseChecks(checks: VpnReleaseCheck[]): VpnReleaseCheckGroup[] {
  const groups = new Map<VpnReleaseCheckGroup["key"], VpnReleaseCheck[]>();
  for (const check of checks) {
    const key = CODE_SECTION.get(check.code) ?? "other";
    groups.set(key, [...(groups.get(key) ?? []), check]);
  }

  return [
    ...SECTION_DEFINITIONS.map(({ key, title }) => ({
      key,
      title,
      checks: (groups.get(key) ?? []).sort(compareChecks),
    })),
    { key: "other" as const, title: "Прочее", checks: (groups.get("other") ?? []).sort(compareChecks) },
  ].filter((group) => group.checks.length > 0);
}

export function summarizeVpnRelease(report: VpnReleaseReadiness) {
  const counts = { pass: 0, warn: 0, fail: 0 };
  for (const check of report.checks) {
    counts[check.state] += 1;
  }
  return {
    ...counts,
    canCommit: report.ready && counts.fail === 0,
  };
}

export function isVpnReleaseCommitConfirmationValid(
  confirmation: VpnReleaseCommitConfirmation,
  report: VpnReleaseReadiness | null,
): boolean {
  return report !== null
    && confirmation.phrase === VPN_RELEASE_COMMIT_PHRASE
    && confirmation.checkedAt === report.checked_at
    && summarizeVpnRelease(report).canCommit;
}

export function formatVpnReleaseCheckedAt(value: string | null): string {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
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
  }).format(date);
  return `${formatted} MSK`;
}

const NODE_ENTITY_CODES = new Set([
  "worker_active",
  "worker_health",
  "control_operations",
  "maintenance",
]);

const VPN_ENDPOINT_ENTITY_CODES = new Set([
  "endpoint_configuration",
  "endpoint_health",
  "endpoint_external_proof",
  "endpoint_capacity",
]);

export function isSafeVpnReleaseEntityId(value: number | null): value is number {
  return Number.isSafeInteger(value) && value !== null && value > 0;
}

export function formatVpnReleaseEntityLabel(
  check: Pick<VpnReleaseCheck, "code" | "entity_id">,
): string | null {
  if (!isSafeVpnReleaseEntityId(check.entity_id)) {
    return null;
  }
  if (check.code === "public_trial_plan") {
    return `Тариф #${check.entity_id}`;
  }
  if (VPN_ENDPOINT_ENTITY_CODES.has(check.code)) {
    return `VPN‑нода #${check.entity_id}`;
  }
  return NODE_ENTITY_CODES.has(check.code) ? `Нода #${check.entity_id}` : null;
}

export function canConfirmVpnExternalProof(
  check: Pick<VpnReleaseCheck, "code" | "state" | "entity_id">,
): boolean {
  return check.code === "endpoint_external_proof"
    && check.state === "fail"
    && isSafeVpnReleaseEntityId(check.entity_id);
}
