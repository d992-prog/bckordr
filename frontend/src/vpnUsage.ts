export function formatTrafficBytes(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value) || value < 0) return "Нет данных";
  const units = ["Б", "КиБ", "МиБ", "ГиБ", "ТиБ", "ПиБ", "ЭиБ"];
  const index = value === 0 ? 0 : Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
  return `${new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 1 }).format(value / 1024 ** Math.max(0, index))} ${units[Math.max(0, index)]}`;
}

export function usageCoverageLabel(coverage: string): string {
  switch (coverage) {
    case "partial": return "Частичные данные";
    case "current": return "Данные актуальны";
    case "stale": return "Данные устарели";
    case "unavailable": return "Сбор недоступен";
    default: return "Нет данных";
  }
}

export function formatUsageTime(value: string | null): string {
  const date = value ? new Date(value) : null;
  if (!date || !Number.isFinite(date.getTime())) return "Нет данных";
  return `${new Intl.DateTimeFormat("ru-RU", { dateStyle: "short", timeStyle: "short", timeZone: "UTC" }).format(date)} UTC`;
}
