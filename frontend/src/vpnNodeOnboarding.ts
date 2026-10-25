export const SSH_FINGERPRINT_COMMAND = "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256";

export type VpnNodeOnboardingState =
  | "needs_preflight"
  | "needs_trust"
  | "ready_to_start"
  | "queued"
  | "running"
  | "awaiting_test"
  | "ready"
  | "failed";

export type VpnNodeOnboardingWorker = {
  status: string;
  is_enabled: boolean;
  vpn_enabled: boolean;
  vpn_role: string;
  ssh_access_configured: boolean;
};

export type VpnNodeOnboardingSetupAction =
  | { kind: "wait_for_worker" }
  | { kind: "open_worker" }
  | { kind: "patch_worker"; patch: { vpn_enabled: true } | { vpn_role: "vpn_node" } }
  | { kind: "configure_ssh" };

export function vpnNodeOnboardingSetupAction(
  worker: VpnNodeOnboardingWorker,
): VpnNodeOnboardingSetupAction | null {
  if (!worker.is_enabled) {
    return { kind: "open_worker" };
  }
  if (worker.status !== "ready") {
    return { kind: "wait_for_worker" };
  }
  if (!worker.vpn_enabled) {
    return { kind: "patch_worker", patch: { vpn_enabled: true } };
  }
  if (worker.vpn_role === "none") {
    return { kind: "patch_worker", patch: { vpn_role: "vpn_node" } };
  }
  return worker.ssh_access_configured ? null : { kind: "configure_ssh" };
}

export function createVpnNodeOnboardingActionGuard() {
  let busy = false;
  return {
    async run(action: () => Promise<void>): Promise<boolean> {
      if (busy) {
        return false;
      }
      busy = true;
      try {
        await action();
        return true;
      } finally {
        busy = false;
      }
    },
  };
}

export function shouldContinueVpnNodeOnboardingPoll(state: {
  stale: boolean;
  failed: boolean;
  running: boolean;
}): boolean {
  return state.stale || state.failed || state.running;
}

export function shouldRenderVpnNodeOnboardingSection(section: string): boolean {
  return section === "nodes";
}

export function createVpnNodeOnboardingRequestGate() {
  const generations = new Map<number, number>();
  return {
    begin(workerId: number) {
      const generation = (generations.get(workerId) ?? 0) + 1;
      generations.set(workerId, generation);
      return generation;
    },
    invalidate(workerId: number) {
      generations.set(workerId, (generations.get(workerId) ?? 0) + 1);
    },
    invalidateAll() {
      for (const workerId of generations.keys()) {
        generations.set(workerId, (generations.get(workerId) ?? 0) + 1);
      }
    },
    isCurrent(workerId: number, generation: number) {
      return generations.get(workerId) === generation;
    },
  };
}

export function isVpnNodeFingerprintConfirmed(
  confirmedFingerprint: string | null,
  currentFingerprint: string | null,
): boolean {
  return Boolean(currentFingerprint) && confirmedFingerprint === currentFingerprint;
}

export function reconcileVpnNodeTestProfileUri(
  uri: string | null,
  state: VpnNodeOnboardingState,
): string | null {
  return state === "awaiting_test" ? uri : null;
}

const ONBOARDING_PRESENTATIONS = {
  needs_preflight: ["Проверить сервер", "Проверяем только настройки и SSH‑ключ."],
  needs_trust: ["Подтвердить SSH‑ключ", "Сверьте отпечаток SSH‑ключа через консоль сервера в панели хостинга."],
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
  const documentRef = options.documentRef === undefined
    ? (typeof document === "undefined" ? null : document)
    : options.documentRef;
  if (documentRef?.body) {
    const field = documentRef.createElement("textarea");
    field.value = value;
    field.readOnly = true;
    field.style.position = "fixed";
    field.style.opacity = "0";
    documentRef.body.appendChild(field);
    try {
      field.focus();
      field.setSelectionRange(0, value.length);
      if (documentRef.execCommand("copy")) {
        return true;
      }
    } catch {
      // Some WebViews omit the legacy command but still expose Clipboard API.
    } finally {
      field.remove();
    }
  }

  const clipboard = options.clipboard === undefined
    ? (typeof navigator === "undefined" ? null : navigator.clipboard)
    : options.clipboard;
  if (clipboard?.writeText) {
    try {
      await clipboard.writeText(value);
      return true;
    } catch {
      // Restricted WebViews can expose the API while rejecting every write.
    }
  }
  return false;
}
