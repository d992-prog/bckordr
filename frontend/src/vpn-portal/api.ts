import type {
  PortalConfig,
  PortalConnection,
  PortalDevice,
  PortalDeviceConnection,
  PortalDeviceLocations,
  PortalMe,
  PortalPlan,
  PortalProfile,
  PortalSubscription,
  PortalTrial,
  PortalUsage,
  PortalUsageDays,
  PortalPreferences,
  PortalDeviceOperation,
  PortalDeviceLocation,
  CreatePortalDevice,
} from "./types";

const GENERIC_ERROR_MESSAGE = "Не удалось выполнить запрос. Попробуйте ещё раз.";
const DEVICE_ERRORS: Record<string, string> = {
  customer_device_request_invalid: "Запрос устарел. Обновите кабинет",
  customer_device_request_conflict: "Этот запрос уже относится к другому действию. Обновите кабинет",
  customer_device_limit_reached: "Все места для устройств заняты. Отключите ненужное устройство и дождитесь завершения",
  customer_device_subscription_unavailable: "Подписка недоступна. Обновите данные доступа",
  customer_device_endpoint_unavailable: "Эта страна сейчас недоступна. Выберите другую",
  customer_device_unavailable: "Действие сейчас недоступно. Обновите статус устройства",
  customer_device_operation_pending: "Для устройства уже выполняется действие. Обновите его статус",
  customer_device_replacement_limited: "Заменять доступ можно раз в 24 часа. Повторите позже",
};

export class PortalError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(status: number, message: string, code: string | null = null) {
    super(message);
    this.name = "PortalError";
    this.status = status;
    this.code = code;
  }
}

function errorMessage(status: number): string {
  if (status === 401) {
    return "Нужно войти снова.";
  }
  if (status === 403) {
    return "Действие недоступно.";
  }
  if (status === 429) {
    return "Слишком много попыток. Повторите позже.";
  }
  return GENERIC_ERROR_MESSAGE;
}

async function portalRequest<T>(path: string, options: RequestInit = {}, deviceError = false): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`/api/vpn-portal${path}`, {
      ...options,
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(options.headers as Record<string, string> | undefined),
      },
    });
  } catch {
    throw new PortalError(0, GENERIC_ERROR_MESSAGE);
  }

  if (!response.ok) {
    if (deviceError && response.status === 409) {
      try {
        const body = await response.json();
        const code = typeof body?.detail === "string" && Object.prototype.hasOwnProperty.call(DEVICE_ERRORS, body.detail) ? body.detail as string : null;
        throw new PortalError(response.status, code ? DEVICE_ERRORS[code] : errorMessage(response.status), code);
      } catch (error) {
        if (error instanceof PortalError) throw error;
      }
    }
    throw new PortalError(response.status, errorMessage(response.status));
  }

  try {
    return (await response.json()) as T;
  } catch {
    throw new PortalError(0, GENERIC_ERROR_MESSAGE);
  }
}

export const portalApi = {
  config: () => portalRequest<PortalConfig>("/config"),
  me: () => portalRequest<PortalMe>("/me"),
  loginMiniApp: (initData: string) =>
    portalRequest<PortalMe>("/auth/mini-app", {
      method: "POST",
      body: JSON.stringify({ init_data: initData }),
    }),
  logout: (csrf: string) =>
    portalRequest<{ logged_out: boolean }>("/logout", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
    }),
  plans: () => portalRequest<PortalPlan[]>("/plans"),
  subscriptions: () => portalRequest<PortalSubscription[]>("/subscriptions"),
  usage: (days: PortalUsageDays) => {
    if (![1, 7, 30].includes(days)) throw new Error("unsupported usage period");
    return portalRequest<PortalUsage>(`/usage?days=${days}`);
  },
  preferences: () => portalRequest<PortalPreferences>("/preferences"),
  updatePreferences: (expiryReminders: boolean, csrf: string) => {
    if (typeof expiryReminders !== "boolean") throw new Error("preference must be boolean");
    return portalRequest<PortalPreferences>("/preferences", { method: "PATCH", headers: { "X-CSRF-Token": csrf }, body: JSON.stringify({ expiry_reminders: expiryReminders }) });
  },
  creationLocations: (subscriptionId: number) => portalRequest<PortalDeviceLocation[]>(`/v2/locations?subscription_id=${subscriptionId}`),
  createDevice: (payload: CreatePortalDevice, csrf: string) => portalRequest<PortalDeviceOperation>("/v2/devices", { method: "POST", headers: { "X-CSRF-Token": csrf }, body: JSON.stringify(payload) }, true),
  revokeDevice: (id: number, requestId: string, csrf: string) => portalRequest<PortalDeviceOperation>(`/v2/devices/${id}/revoke`, { method: "POST", headers: { "X-CSRF-Token": csrf }, body: JSON.stringify({ request_id: requestId }) }, true),
  replaceDevice: (id: number, requestId: string, csrf: string) => portalRequest<PortalDeviceOperation>(`/v2/devices/${id}/replace`, { method: "POST", headers: { "X-CSRF-Token": csrf }, body: JSON.stringify({ request_id: requestId }) }, true),
  deviceOperation: (id: string) => portalRequest<PortalDeviceOperation>(`/v2/device-operations/${encodeURIComponent(id)}`),
  profiles: () => portalRequest<PortalProfile[]>("/profiles"),
  devices: () => portalRequest<PortalDevice[]>("/v2/devices"),
  deviceLocations: (id: number) =>
    portalRequest<PortalDeviceLocations>(`/v2/devices/${id}/locations`),
  selectDeviceLocation: (id: number, endpointId: number, csrf: string) =>
    portalRequest<PortalDeviceLocations>(`/v2/devices/${id}/location`, {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
      body: JSON.stringify({ endpoint_id: endpointId }),
    }),
  deviceConnection: (id: number) =>
    portalRequest<PortalDeviceConnection>(`/v2/devices/${id}/connection`),
  renameDevice: (id: number, displayName: string, csrf: string) =>
    portalRequest<PortalDevice>(`/v2/devices/${id}`, {
      method: "PATCH",
      headers: { "X-CSRF-Token": csrf },
      body: JSON.stringify({ display_name: displayName }),
    }),
  trial: () => portalRequest<PortalTrial>("/trial"),
  activateTrial: (csrf: string) =>
    portalRequest<PortalTrial>("/trial/activate", {
      method: "POST",
      headers: { "X-CSRF-Token": csrf },
    }),
  connection: (id: number) =>
    portalRequest<PortalConnection>(`/profiles/${id}/connection`),
  rename: (id: number, displayName: string, csrf: string) =>
    portalRequest<PortalProfile>(`/profiles/${id}`, {
      method: "PATCH",
      headers: { "X-CSRF-Token": csrf },
      body: JSON.stringify({ display_name: displayName }),
    }),
};
