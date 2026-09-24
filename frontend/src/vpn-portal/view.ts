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
