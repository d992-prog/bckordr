import type {
  PortalConfig,
  PortalConnection,
  PortalMe,
  PortalProfile,
  PortalSubscription,
} from "./types";

const GENERIC_ERROR_MESSAGE = "Не удалось выполнить запрос. Попробуйте ещё раз.";

export class PortalError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "PortalError";
    this.status = status;
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

async function portalRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
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
  subscriptions: () => portalRequest<PortalSubscription[]>("/subscriptions"),
  profiles: () => portalRequest<PortalProfile[]>("/profiles"),
  connection: (id: number) =>
    portalRequest<PortalConnection>(`/profiles/${id}/connection`),
  rename: (id: number, displayName: string, csrf: string) =>
    portalRequest<PortalProfile>(`/profiles/${id}`, {
      method: "PATCH",
      headers: { "X-CSRF-Token": csrf },
      body: JSON.stringify({ display_name: displayName }),
    }),
};
