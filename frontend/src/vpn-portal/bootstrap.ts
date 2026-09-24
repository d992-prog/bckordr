import type { PortalConfig, PortalMe } from "./types";

const TELEGRAM_CACHE_KEY = "__telegram__initParams";
const TELEGRAM_LAUNCH_PARAMETERS = new Set([
  "tgWebAppData",
  "tgWebAppVersion",
  "tgWebAppPlatform",
  "tgWebAppThemeParams",
  "tgWebAppStartParam",
  "tgWebAppShowSettings",
  "tgWebAppBotInline",
  "tgWebAppFullscreen",
]);

interface TelegramWebAppLike {
  initData?: string;
  platform?: string;
  ready?: () => void;
  expand?: () => void;
}

interface LaunchEnvironment {
  Telegram?: { WebApp?: TelegramWebAppLike };
  sessionStorage?: Pick<Storage, "getItem" | "setItem">;
  location?: Pick<Location, "href">;
  history?: Pick<History, "state" | "replaceState">;
}

export interface TelegramLaunch {
  initData: string;
  platform: string;
  isMiniAppLaunch: boolean;
}

interface BootstrapApi {
  config: () => Promise<PortalConfig>;
  loginMiniApp: (initData: string) => Promise<PortalMe>;
  me: () => Promise<PortalMe>;
}

type BootstrapFailureKind =
  | "disabled"
  | "login-required"
  | "reopen-mini-app"
  | "fresh-launch-required"
  | "account-switch"
  | "cookie-unavailable"
  | "error";

export type PortalBootstrapResult =
  | { kind: "ready"; config: PortalConfig; me: PortalMe }
  | { kind: BootstrapFailureKind; config: PortalConfig | null };

function removeTelegramCacheData(environment: LaunchEnvironment): void {
  try {
    const cache = environment.sessionStorage?.getItem(TELEGRAM_CACHE_KEY);
    if (cache === null || cache === undefined) {
      return;
    }
    const parsed: unknown = JSON.parse(cache);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return;
    }
    const entries = parsed as Record<string, unknown>;
    if (!Object.prototype.hasOwnProperty.call(entries, "tgWebAppData")) {
      return;
    }
    const { tgWebAppData: _discarded, ...preserved } = entries;
    environment.sessionStorage?.setItem(TELEGRAM_CACHE_KEY, JSON.stringify(preserved));
  } catch {
    // Storage can be denied in embedded browsers; launch data was already captured.
  }
}

function cleanParameterString(value: string): string {
  const parameters = new URLSearchParams(value);
  for (const key of [...parameters.keys()]) {
    if (TELEGRAM_LAUNCH_PARAMETERS.has(key)) {
      parameters.delete(key);
    }
  }
  return parameters.toString();
}

function stripTelegramParameters(environment: LaunchEnvironment): void {
  if (!environment.location || !environment.history) {
    return;
  }
  try {
    const url = new URL(environment.location.href);
    url.search = cleanParameterString(url.search);
    if (url.hash.startsWith("#") && url.hash.includes("=")) {
      const rawHash = url.hash.slice(1);
      const routeSeparator = rawHash.indexOf("?");
      if (routeSeparator >= 0) {
        const route = rawHash.slice(0, routeSeparator);
        const cleanedParameters = cleanParameterString(rawHash.slice(routeSeparator + 1));
        url.hash = `#${route}${cleanedParameters.length > 0 ? `?${cleanedParameters}` : ""}`;
      } else {
        const cleanedHash = cleanParameterString(rawHash);
        url.hash = cleanedHash.length > 0 ? `#${cleanedHash}` : "";
      }
    }
    const safeUrl = `${url.pathname}${url.search}${url.hash}`;
    environment.history.replaceState(environment.history.state, "", safeUrl);
  } catch {
    // An unavailable history API must not prevent authentication.
  }
}

export function captureTelegramLaunch(
  environment: LaunchEnvironment = window,
): TelegramLaunch {
  const webApp = environment.Telegram?.WebApp;
  const initData = typeof webApp?.initData === "string" ? webApp.initData : "";
  const platform = typeof webApp?.platform === "string" ? webApp.platform : "unknown";
  const launch = {
    initData,
    platform,
    isMiniAppLaunch: initData.length > 0,
  };

  removeTelegramCacheData(environment);
  stripTelegramParameters(environment);
  return launch;
}

function hasStatus(error: unknown, status: number): boolean {
  return typeof error === "object"
    && error !== null
    && "status" in error
    && error.status === status;
}

async function runBootstrap(
  api: BootstrapApi,
  launch: TelegramLaunch,
): Promise<PortalBootstrapResult> {
  let portalConfig: PortalConfig;
  try {
    portalConfig = await api.config();
  } catch {
    return { kind: "error", config: null };
  }

  if (!portalConfig.enabled) {
    return { kind: "disabled", config: portalConfig };
  }

  if (launch.initData.length > 0) {
    let exchangedMe: PortalMe;
    try {
      exchangedMe = await api.loginMiniApp(launch.initData);
    } catch (error) {
      if (hasStatus(error, 409)) {
        return { kind: "account-switch", config: portalConfig };
      }
      return { kind: "fresh-launch-required", config: portalConfig };
    }

    try {
      const me = await api.me();
      if (me.csrf_token !== exchangedMe.csrf_token) {
        return { kind: "account-switch", config: portalConfig };
      }
      return { kind: "ready", config: portalConfig, me };
    } catch (error) {
      if (hasStatus(error, 401)) {
        return { kind: "cookie-unavailable", config: portalConfig };
      }
      return { kind: "error", config: portalConfig };
    }
  }

  try {
    const me = await api.me();
    return { kind: "ready", config: portalConfig, me };
  } catch (error) {
    return {
      kind: hasStatus(error, 401)
        ? launch.platform === "unknown" ? "login-required" : "reopen-mini-app"
        : "error",
      config: portalConfig,
    };
  }
}

export function createPortalBootstrap(api: BootstrapApi) {
  let inflight: Promise<PortalBootstrapResult> | null = null;
  return (launch: TelegramLaunch): Promise<PortalBootstrapResult> => {
    if (inflight === null) {
      const shared = runBootstrap(api, launch).finally(() => {
        if (inflight === shared) {
          inflight = null;
        }
      });
      inflight = shared;
    }
    return inflight;
  };
}

export class SessionGeneration {
  private value = 0;

  current(): number {
    return this.value;
  }

  invalidate(): number {
    this.value += 1;
    return this.value;
  }

  isCurrent(generation: number): boolean {
    return generation === this.value;
  }
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebAppLike };
  }
}
