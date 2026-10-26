import type { VpnCabinetSettings } from "./api";

export type CabinetSettingsDraft = {
  supportContact: string;
  noticeEnabled: boolean;
  title: string;
  body: string;
  startsAt: string;
  endsAt: string;
  remindersEnabled: boolean;
  originalNotice: VpnCabinetSettings["notice"];
};

export function cabinetLocalDateTime(iso: string): string {
  const date = new Date(iso);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, -1).replace(/:00\.000$/, "").replace(/\.000$/, "");
}

export function cabinetSettingsDraft(settings: VpnCabinetSettings): CabinetSettingsDraft {
  return {
    supportContact: settings.support_contact ?? "", noticeEnabled: settings.notice !== null,
    title: settings.notice?.title ?? "", body: settings.notice?.body ?? "",
    startsAt: settings.notice ? cabinetLocalDateTime(settings.notice.starts_at) : "",
    endsAt: settings.notice ? cabinetLocalDateTime(settings.notice.ends_at) : "",
    remindersEnabled: settings.reminders_enabled,
    originalNotice: settings.notice,
  };
}

function localDate(value: string): Date {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d{1,3}))?)?$/.exec(value);
  const date = new Date(value);
  if (!match || !Number.isFinite(date.getTime()) || date.getFullYear() !== Number(match[1]) || date.getMonth() + 1 !== Number(match[2])
    || date.getDate() !== Number(match[3]) || date.getHours() !== Number(match[4]) || date.getMinutes() !== Number(match[5]) || date.getSeconds() !== Number(match[6] ?? 0)) {
    throw new Error("Укажите существующие дату и время начала и конца показа");
  }
  return date;
}

function noticeDateTime(local: string, original?: string): string {
  // Preserve the server's DST fold and precision until the operator edits this time.
  return original && cabinetLocalDateTime(original) === local ? original : localDate(local).toISOString();
}

export function cabinetSettingsPayload(draft: CabinetSettingsDraft, revision: number): VpnCabinetSettings {
  const support = draft.supportContact.trim();
  if (support && !/^(?:@|https:\/\/t\.me\/)[A-Za-z][A-Za-z0-9_]{4,31}$/.test(support)) {
    throw new Error("Контакт поддержки: @username или https://t.me/username");
  }
  let notice: VpnCabinetSettings["notice"] = null;
  if (draft.noticeEnabled) {
    for (const [label, text, limit] of [["Заголовок", draft.title, 100], ["Текст", draft.body, 1000]] as const) {
      if (!text.trim() || Array.from(text).length > limit || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(text.replace(/[\n\t]/g, ""))) {
        throw new Error(`${label} объявления: от 1 до ${limit} символов обычного текста без скрытых управляющих знаков`);
      }
    }
    const startsAt = noticeDateTime(draft.startsAt, draft.originalNotice?.starts_at);
    const endsAt = noticeDateTime(draft.endsAt, draft.originalNotice?.ends_at);
    const unchangedWindow = startsAt === draft.originalNotice?.starts_at && endsAt === draft.originalNotice?.ends_at;
    if (!unchangedWindow && Date.parse(endsAt) <= Date.parse(startsAt)) throw new Error("Конец показа должен быть позже начала");
    notice = { title: draft.title, body: draft.body, starts_at: startsAt, ends_at: endsAt };
  }
  return { revision, support_contact: support || null, notice, reminders_enabled: draft.remindersEnabled };
}

export function cabinetSettingsErrorText(cause: unknown): string {
  return cause instanceof Error && cause.message === "vpn_cabinet_settings_revision_conflict"
    ? "Настройки изменены другим оператором. Ваши изменения остались в форме. Загрузите актуальные настройки перед сохранением"
    : "Результат сохранения неизвестен. Загрузите актуальные настройки перед следующим сохранением";
}
