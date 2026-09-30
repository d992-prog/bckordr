function russianCount(value: number, one: string, few: string, many: string): string {
  const remainder100 = Math.abs(value) % 100;
  const remainder10 = remainder100 % 10;
  const form = remainder100 >= 11 && remainder100 <= 14
    ? many
    : remainder10 === 1
      ? one
      : remainder10 >= 2 && remainder10 <= 4 ? few : many;
  return `${value} ${form}`;
}

export function formatPlanDuration(days: number | null): string {
  return days === null ? "Без срока" : russianCount(days, "день", "дня", "дней");
}

export function formatPlanDevices(devices: number): string {
  return russianCount(devices, "устройство", "устройства", "устройств");
}

export function formatPlanTraffic(gigabytes: number | null): string {
  return gigabytes === null ? "Без лимита" : `${gigabytes.toLocaleString("ru-RU")} ГБ`;
}

export function formatPlanPrice(amount: number, currency: string): string {
  if (amount === 0) {
    return "Бесплатно";
  }
  if (!Number.isFinite(amount) || !/^[A-Za-z]{3}$/.test(currency)) {
    return "Цена уточняется";
  }
  try {
    return new Intl.NumberFormat("ru-RU", {
      style: "currency",
      currency: currency.toUpperCase(),
      maximumFractionDigits: Number.isInteger(amount) ? 0 : 2,
    }).format(amount);
  } catch {
    return "Цена уточняется";
  }
}
