import type { VpnReleaseCheck, VpnReleaseReadiness } from "./api";

export const VPN_RELEASE_STATUS_LABELS: Record<VpnReleaseCheck["state"], string> = {
  pass: "Пройдено",
  warn: "Предупреждение",
  fail: "Ошибка",
};

const SECTION_DEFINITIONS = [
  {
    key: "infrastructure",
    title: "Инфраструктура",
    codes: [
      "system",
      "system_health",
      "control",
      "control_health",
      "local",
      "local_health",
      "public",
      "public_health",
      "cabinet",
      "cabinet_health",
      "disk",
      "disk_health",
      "backup",
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

export function formatVpnReleaseEntityLabel(entityId: number | null): string | null {
  return Number.isInteger(entityId) ? `Нода #${entityId}` : null;
}
