const STATE_LABELS = new Map<string, string>([
  ["active", "Активна"],
  ["trial", "Пробный доступ"],
  ["scheduled", "Начнётся позже"],
  ["disabled", "Приостановлена"],
  ["expired", "Истекла"],
  ["cancelled", "Отменена"],
  ["pending_sync", "Подготавливается"],
  ["syncing", "Подготавливается"],
  ["suspended", "Приостановлен"],
  ["pending_suspend", "Отключается"],
  ["pending_revoke", "Отзывается"],
  ["revoked", "Отозван"],
  ["failed", "Нужна помощь"],
]);

export function stateLabel(state: string): string {
  return STATE_LABELS.get(state) ?? "Статус уточняется";
}

export function portalInitials(displayName: string): string {
  const words = displayName.trim().split(/\s+/u).filter(Boolean);
  if (!words.length) return "Я";
  return (words.length === 1 ? Array.from(words[0]).slice(0, 2).join("")
    : Array.from(words[0])[0] + Array.from(words[words.length - 1])[0]).toLocaleUpperCase("ru-RU");
}

export function portalDate(value: string | null): string {
  if (value === null) {
    return "Без срока окончания";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "Дата недоступна";
  }

  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(date);
}

export function portalNoticeVisible(notice: {starts_at: string; ends_at: string} | null | undefined, now = Date.now()): boolean {
  return Boolean(notice && Date.parse(notice.starts_at) <= now && now < Date.parse(notice.ends_at));
}

export function portalRemainingTime(expiresAt: string | null, now = Date.now()): string {
  if (expiresAt === null) return "Без ограничения по сроку";
  const expires = Date.parse(expiresAt);
  if (!Number.isFinite(expires)) return "Срок уточняется";
  const days = Math.ceil((expires - now) / 86400000);
  if (days <= 0) return "Срок закончился";
  const word = new Intl.PluralRules("ru").select(days);
  return `Осталось ${days} ${word === "one" ? "день" : word === "few" ? "дня" : "дней"}`;
}
