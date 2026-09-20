import test from "node:test";
import assert from "node:assert/strict";

import {
  captureTelegramLaunch,
  createPortalBootstrap,
  SessionGeneration,
} from "../src/vpn-portal/bootstrap.ts";

function config(overrides = {}) {
  return {
    enabled: true,
    browser_login_enabled: true,
    mini_app_enabled: true,
    login_path: "/api/vpn-portal/auth/telegram/start",
    support_text: "Напишите в поддержку.",
    ...overrides,
  };
}

function portalError(status, message = "request failed") {
  return Object.assign(new Error(message), { status });
}

test("captures original SDK initData, narrows cache cleanup, and strips launch parameters", () => {
  const entries = new Map([
    ["__telegram__initParams", JSON.stringify({
      tgWebAppData: "cached-secret",
      tgWebAppVersion: "8.0",
      tgWebAppThemeParams: "{\"bg_color\":\"#fff\"}",
    })],
    ["unrelated", "keep-me"],
  ]);
  const storage = {
    getItem: (key) => entries.get(key) ?? null,
    setItem: (key, value) => entries.set(key, value),
  };
  const location = {
    href: "https://vpn.example/cabinet/?tgWebAppVersion=8.0#tgWebAppData=secret&tgWebAppPlatform=android",
  };
  let replacement = null;
  const history = {
    state: { preserved: true },
    replaceState: (state, _unused, url) => {
      replacement = { state, url: String(url) };
    },
  };
  const environment = {
    Telegram: { WebApp: { initData: "original-signed-data", platform: "android" } },
    sessionStorage: storage,
    location,
    history,
  };

  const launch = captureTelegramLaunch(environment);

  assert.deepEqual(launch, {
    initData: "original-signed-data",
    platform: "android",
    isMiniAppLaunch: true,
  });
  assert.deepEqual(JSON.parse(entries.get("__telegram__initParams")), {
    tgWebAppVersion: "8.0",
    tgWebAppThemeParams: "{\"bg_color\":\"#fff\"}",
  });
  assert.equal(entries.get("unrelated"), "keep-me");
  assert.deepEqual(replacement, {
    state: { preserved: true },
    url: "/cabinet/",
  });
});

test("storage and URL cleanup failures never lose captured launch data", () => {
  const launch = captureTelegramLaunch({
    Telegram: { WebApp: { initData: "signed", platform: "ios" } },
    get sessionStorage() {
      throw new Error("blocked");
    },
    location: { href: "not a URL" },
    history: { replaceState: () => { throw new Error("blocked"); } },
  });

  assert.equal(launch.initData, "signed");
  assert.equal(launch.isMiniAppLaunch, true);
});

test("strips launch data after a safe hash route while preserving the route and unrelated parameters", () => {
  let replacement = "";
  captureTelegramLaunch({
    Telegram: { WebApp: { initData: "signed", platform: "android" } },
    location: {
      href: "https://vpn.example/cabinet/#profiles?keep=yes&tgWebAppData=secret&tgWebAppVersion=8.0",
    },
    history: {
      state: null,
      replaceState: (_state, _unused, url) => { replacement = String(url); },
    },
  });

  assert.equal(replacement, "/cabinet/#profiles?keep=yes");
});

test("an SDK object with unknown platform and empty initData is an ordinary browser", () => {
  const launch = captureTelegramLaunch({
    Telegram: { WebApp: { initData: "", platform: "unknown" } },
  });

  assert.deepEqual(launch, {
    initData: "",
    platform: "unknown",
    isMiniAppLaunch: false,
  });
});

test("Mini App bootstrap reads config, exchanges identity, confirms cookie, and deduplicates inflight calls", async () => {
  const calls = [];
  let releaseConfig;
  const configGate = new Promise((resolve) => { releaseConfig = resolve; });
  const api = {
    config: async () => {
      calls.push("config");
      await configGate;
      return config();
    },
    loginMiniApp: async (payload) => {
      calls.push(`login:${payload}`);
      return { display_name: "Анна", csrf_token: "confirmed-csrf" };
    },
    me: async () => {
      calls.push("me");
      return { display_name: "Анна", csrf_token: "confirmed-csrf" };
    },
  };
  const bootstrap = createPortalBootstrap(api);
  const launch = { initData: "signed", platform: "ios", isMiniAppLaunch: true };

  const first = bootstrap(launch);
  const second = bootstrap(launch);
  releaseConfig();

  assert.equal(first, second);
  assert.deepEqual(await first, {
    kind: "ready",
    config: config(),
    me: { display_name: "Анна", csrf_token: "confirmed-csrf" },
  });
  assert.deepEqual(calls, ["config", "login:signed", "me"]);
});

test("a failed or expired signed launch never falls back to an old cookie", async () => {
  const calls = [];
  const api = {
    config: async () => { calls.push("config"); return config(); },
    loginMiniApp: async () => { calls.push("login"); throw portalError(401); },
    me: async () => { calls.push("me"); return { display_name: "Old Alice", csrf_token: "old" }; },
  };
  const bootstrap = createPortalBootstrap(api);

  const result = await bootstrap({ initData: "expired", platform: "ios", isMiniAppLaunch: true });

  assert.equal(result.kind, "fresh-launch-required");
  assert.deepEqual(calls, ["config", "login"]);
});

test("an account-switch conflict exposes logout flow without reading old-cookie identity", async () => {
  const calls = [];
  const api = {
    config: async () => { calls.push("config"); return config(); },
    loginMiniApp: async () => { calls.push("login"); throw portalError(409); },
    me: async () => { calls.push("me"); return { display_name: "Alice", csrf_token: "old" }; },
  };
  const bootstrap = createPortalBootstrap(api);

  const result = await bootstrap({ initData: "bob-signed", platform: "android", isMiniAppLaunch: true });

  assert.equal(result.kind, "account-switch");
  assert.deepEqual(calls, ["config", "login"]);
});

test("a successful exchange whose cookie cannot be confirmed reports cookie blocking", async () => {
  const api = {
    config: async () => config(),
    loginMiniApp: async () => ({ display_name: "Анна", csrf_token: "csrf" }),
    me: async () => { throw portalError(401); },
  };
  const result = await createPortalBootstrap(api)({
    initData: "signed",
    platform: "ios",
    isMiniAppLaunch: true,
  });

  assert.equal(result.kind, "cookie-unavailable");
});

test("a changed cookie between exchange and confirmation never renders either account", async () => {
  const api = {
    config: async () => config(),
    loginMiniApp: async () => ({ display_name: "Bob", csrf_token: "bob-csrf" }),
    me: async () => ({ display_name: "Alice", csrf_token: "alice-csrf" }),
  };
  const result = await createPortalBootstrap(api)({
    initData: "bob-signed",
    platform: "android",
    isMiniAppLaunch: true,
  });

  assert.equal(result.kind, "account-switch");
});

test("ordinary browser bootstrap uses the existing cookie only after config", async () => {
  const calls = [];
  const api = {
    config: async () => { calls.push("config"); return config(); },
    loginMiniApp: async () => { calls.push("login"); throw new Error("must not run"); },
    me: async () => { calls.push("me"); return { display_name: "Иван", csrf_token: "csrf" }; },
  };
  const result = await createPortalBootstrap(api)({
    initData: "",
    platform: "unknown",
    isMiniAppLaunch: false,
  });

  assert.equal(result.kind, "ready");
  assert.deepEqual(calls, ["config", "me"]);
});

test("disabled portal and unauthenticated browser produce actionable states", async () => {
  const disabled = await createPortalBootstrap({
    config: async () => config({ enabled: false }),
    loginMiniApp: async () => { throw new Error("unused"); },
    me: async () => { throw new Error("unused"); },
  })({ initData: "", platform: "unknown", isMiniAppLaunch: false });
  assert.equal(disabled.kind, "disabled");

  const loginRequired = await createPortalBootstrap({
    config: async () => config(),
    loginMiniApp: async () => { throw new Error("unused"); },
    me: async () => { throw portalError(401); },
  })({ initData: "", platform: "unknown", isMiniAppLaunch: false });
  assert.equal(loginRequired.kind, "login-required");

  const reopenMiniApp = await createPortalBootstrap({
    config: async () => config(),
    loginMiniApp: async () => { throw new Error("unused"); },
    me: async () => { throw portalError(401); },
  })({ initData: "", platform: "android", isMiniAppLaunch: false });
  assert.equal(reopenMiniApp.kind, "reopen-mini-app");
});

test("session generations invalidate late authenticated responses", () => {
  const generation = new SessionGeneration();
  const first = generation.current();
  assert.equal(generation.isCurrent(first), true);

  generation.invalidate();

  assert.equal(generation.isCurrent(first), false);
  assert.equal(generation.isCurrent(generation.current()), true);
});

test("bootstrap coordinator releases settled identity data while retaining inflight deduplication", async () => {
  let configCalls = 0;
  const api = {
    config: async () => { configCalls += 1; return config(); },
    loginMiniApp: async () => { throw new Error("unused"); },
    me: async () => ({ display_name: "Иван", csrf_token: "csrf" }),
  };
  const bootstrap = createPortalBootstrap(api);
  const launch = { initData: "", platform: "unknown", isMiniAppLaunch: false };

  await bootstrap(launch);
  await bootstrap(launch);

  assert.equal(configCalls, 2);
});
