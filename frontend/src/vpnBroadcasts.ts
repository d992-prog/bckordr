import type { VpnBroadcastConfig, VpnBroadcastDetail } from "./api";

export function broadcastCharacters(text: string): number { return Array.from(text).length; }

export function validateBroadcastBody(text: string): string {
  const body = text.replace(/\r\n/g, "\n");
  if (!body.trim() || broadcastCharacters(body) > 4096 || /[\uD800-\uDFFF]/u.test(body)) {
    throw new Error("Введите текст новости: от 1 до 4096 символов.");
  }
  return body;
}

export function canConfirmBroadcast(config: VpnBroadcastConfig | null, preview: VpnBroadcastDetail | null, body: string): boolean {
  return Boolean(config?.enabled && config.registry_state === "ready" && preview?.state === "draft"
    && preview.body === body && preview.preview_revision === preview.revision
    && preview.preview_recipient_count !== null && preview.previewed_at);
}

export function broadcastStateLabel(state: string): string {
  switch (state) {
    case "draft": return "Черновик";
    case "running": return "Отправляется";
    case "completed": return "Завершена";
    case "cancelled": return "Остановлена";
    case "failed": return "Остановлена из-за ошибки";
    default: return "Нет данных";
  }
}

export function broadcastErrorText(error: unknown): string {
  const code = error instanceof Error ? error.message : "";
  switch (code) {
    case "vpn_broadcast_audience_changed": return "Список получателей изменился. Обновите предпросмотр и подтвердите отправку заново.";
    case "vpn_broadcast_preview_changed": return "Версия черновика изменилась. Обновите предпросмотр перед отправкой.";
    case "vpn_broadcast_not_draft": return "Рассылка уже не черновик. Обновите её состояние.";
    case "vpn_broadcast_registry_unavailable": return "Реестр получателей недоступен для отправки. Проверьте его состояние.";
    case "vpn_broadcasts_disabled": return "Отправка рассылок отключена. Черновики можно сохранять.";
    case "vpn_bot_unconfigured": return "VPN-бот не настроен. Настройте бота перед рассылкой.";
    case "vpn_broadcast_invalid_request": return "Проверьте текст новости: от 1 до 4096 символов.";
    default: return "Не удалось выполнить действие. Обновите состояние и попробуйте ещё раз.";
  }
}
