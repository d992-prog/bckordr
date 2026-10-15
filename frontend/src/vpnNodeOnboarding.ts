export type VpnNodeOnboardingState =
  | "needs_preflight"
  | "needs_trust"
  | "ready_to_start"
  | "queued"
  | "running"
  | "awaiting_test"
  | "ready"
  | "failed";

const ONBOARDING_PRESENTATIONS = {
  needs_preflight: ["Проверить сервер", "Проверяем только настройки и SSH‑ключ."],
  needs_trust: ["Подтвердить SSH‑ключ", "Сверьте fingerprint в панели хостинга."],
  ready_to_start: ["Настроить VPN‑ноду", "Установим защищённое подключение на TCP 443."],
  queued: ["Настройка в очереди", "Можно закрыть страницу — работа продолжится."],
  running: ["Настраиваем VPN‑ноду…", "Не выключайте сервер."],
  awaiting_test: ["Проверить подключение", "Скопируйте временный профиль и подключитесь."],
  ready: ["VPN‑нода готова", "Нода участвует в выдаче новых профилей."],
  failed: ["Повторить безопасно", "Перед повтором проверим текущее состояние."],
} as const;

export function onboardingPresentation(state: VpnNodeOnboardingState) {
  return ONBOARDING_PRESENTATIONS[state];
}

const ONBOARDING_ERRORS: Record<string, string> = {
  vpn_node_trust_host_key_mismatch: "SSH‑ключ сервера изменился. Настройка остановлена.",
  vpn_onboarding_host_key_changed: "SSH‑ключ сервера изменился. Настройка остановлена.",
  vpn_onboarding_port_conflict: "TCP 443 уже занят другой службой.",
  vpn_onboarding_reconciliation_required: "Нужно проверить незавершённое изменение на сервере.",
  vpn_node_bootstrap_mutation_uncertain: "Связь оборвалась во время настройки. Повторная проверка не создаст дубликат.",
  vpn_onboarding_worker_busy: "Сервер занят другой задачей. Повторите позже.",
  vpn_onboarding_worker_unavailable: "Сервер пока недоступен для настройки.",
};

export function onboardingErrorMessage(code: string | null | undefined): string {
  return code && Object.prototype.hasOwnProperty.call(ONBOARDING_ERRORS, code)
    ? ONBOARDING_ERRORS[code]
    : "Не удалось продолжить настройку. Повторите проверку позже.";
}

type CopyOptions = {
  clipboard?: Pick<Clipboard, "writeText"> | null;
  documentRef?: Document | null;
};

export async function copyVpnNodeTestProfile(
  value: string,
  options: CopyOptions = {},
): Promise<boolean> {
  const clipboard = options.clipboard === undefined
    ? (typeof navigator === "undefined" ? null : navigator.clipboard)
    : options.clipboard;
  try {
    if (clipboard?.writeText) {
      await clipboard.writeText(value);
      return true;
    }
  } catch {
    // Some iOS webviews expose Clipboard API but reject writes.
  }

  const documentRef = options.documentRef === undefined
    ? (typeof document === "undefined" ? null : document)
    : options.documentRef;
  if (!documentRef?.body) {
    return false;
  }
  const field = documentRef.createElement("textarea");
  field.value = value;
  field.setAttribute("readonly", "");
  field.style.position = "fixed";
  field.style.inset = "0 auto auto -9999px";
  documentRef.body.appendChild(field);
  try {
    field.focus();
    field.select();
    field.setSelectionRange(0, value.length);
    return documentRef.execCommand("copy");
  } catch {
    return false;
  } finally {
    field.remove();
  }
}
