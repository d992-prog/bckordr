import test, { afterEach } from "node:test";
import assert from "node:assert/strict";

import * as portalModule from "../src/vpn-portal/api.ts";

const { PortalError, portalApi } = portalModule;

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function successfulResponse(payload) {
  return {
    ok: true,
    status: 200,
    json: async () => payload,
  };
}

function installFetchRecorder(payloads) {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return successfulResponse(payloads[calls.length - 1]);
  };
  return calls;
}

function headerValue(headers, name) {
  return new Headers(headers).get(name);
}

function forbidBrowserStorage() {
  const descriptors = new Map();
  for (const name of ["localStorage", "sessionStorage"]) {
    descriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, {
      configurable: true,
      value: new Proxy({}, {
        get() {
          throw new Error(`${name} must not be accessed`);
        },
      }),
    });
  }
  return () => {
    for (const [name, descriptor] of descriptors) {
      if (descriptor) {
        Object.defineProperty(globalThis, name, descriptor);
      } else {
        delete globalThis[name];
      }
    }
  };
}

test("exports only the typed portal client and safe error class", () => {
  assert.deepEqual(Object.keys(portalModule).sort(), ["PortalError", "portalApi"]);
  assert.deepEqual(Object.keys(portalApi).sort(), [
    "config",
    "connection",
    "loginMiniApp",
    "logout",
    "me",
    "profiles",
    "rename",
    "subscriptions",
  ]);
});

test("portal reads use only the customer namespace and omit CSRF headers", async () => {
  const config = {
    enabled: true,
    browser_login_enabled: true,
    mini_app_enabled: false,
    login_path: "/api/vpn-portal/auth/telegram/start",
    support_text: "Напишите в поддержку.",
  };
  const me = { display_name: "Анна", csrf_token: "csrf-read-result" };
  const subscriptions = [{
    id: 11,
    service_name: "Veltrix VPN",
    state: "active",
    starts_at: "2026-09-01T00:00:00Z",
    expires_at: null,
    profile_limit: 3,
    profiles_used: 1,
    traffic_limit_gb_per_profile: 100,
  }];
  const profiles = [{
    id: 21,
    subscription_id: 11,
    display_name: "Телефон",
    state: "active",
    can_connect: true,
  }];
  const connection = { uri: "vless://connection-secret" };
  const calls = installFetchRecorder([config, me, subscriptions, profiles, connection]);

  assert.deepEqual(await portalApi.config(), config);
  assert.deepEqual(await portalApi.me(), me);
  assert.deepEqual(await portalApi.subscriptions(), subscriptions);
  assert.deepEqual(await portalApi.profiles(), profiles);
  assert.deepEqual(await portalApi.connection(21), connection);

  assert.deepEqual(calls.map((call) => call.url), [
    "/api/vpn-portal/config",
    "/api/vpn-portal/me",
    "/api/vpn-portal/subscriptions",
    "/api/vpn-portal/profiles",
    "/api/vpn-portal/profiles/21/connection",
  ]);
  for (const call of calls) {
    assert.equal(call.init?.method ?? "GET", "GET");
    assert.equal(call.init?.credentials, "same-origin");
    assert.equal(call.init?.cache, "no-store");
    assert.equal(headerValue(call.init?.headers, "Content-Type"), "application/json");
    assert.equal(headerValue(call.init?.headers, "X-CSRF-Token"), null);
    assert.equal(headerValue(call.init?.headers, "Authorization"), null);
    assert.equal(headerValue(call.init?.headers, "Origin"), null);
    assert.equal(call.url.startsWith("/api/control"), false);
    assert.equal(call.url.startsWith("/api/auth"), false);
  }
});

test("Mini App exchange sends init data only in its JSON body", async () => {
  const result = { display_name: "Иван", csrf_token: "issued-csrf" };
  const calls = installFetchRecorder([result]);
  const initData = "query_id=private-query&hash=private-hash";
  const restoreStorage = forbidBrowserStorage();

  try {
    assert.deepEqual(await portalApi.loginMiniApp(initData), result);
  } finally {
    restoreStorage();
  }

  assert.equal(calls[0].url, "/api/vpn-portal/auth/mini-app");
  assert.equal(calls[0].url.includes(initData), false);
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.body, JSON.stringify({ init_data: initData }));
  assert.equal(calls[0].init?.credentials, "same-origin");
  assert.equal(calls[0].init?.cache, "no-store");
  assert.equal(headerValue(calls[0].init?.headers, "Content-Type"), "application/json");
  assert.equal(headerValue(calls[0].init?.headers, "X-CSRF-Token"), null);
  assert.equal(headerValue(calls[0].init?.headers, "Authorization"), null);
  assert.equal(headerValue(calls[0].init?.headers, "Origin"), null);
});

test("logout and rename attach CSRF only as a header and return server JSON", async () => {
  const logout = { logged_out: true };
  const renamed = {
    id: 21,
    subscription_id: 11,
    display_name: "Рабочий ноутбук",
    state: "active",
    can_connect: true,
  };
  const calls = installFetchRecorder([logout, renamed]);
  const restoreStorage = forbidBrowserStorage();

  try {
    assert.deepEqual(await portalApi.logout("logout-csrf-secret"), logout);
    assert.deepEqual(
      await portalApi.rename(21, "Рабочий ноутбук", "rename-csrf-secret"),
      renamed,
    );
  } finally {
    restoreStorage();
  }

  assert.equal(calls[0].url, "/api/vpn-portal/logout");
  assert.equal(calls[0].init?.method, "POST");
  assert.equal(calls[0].init?.body, undefined);
  assert.equal(headerValue(calls[0].init?.headers, "X-CSRF-Token"), "logout-csrf-secret");

  assert.equal(calls[1].url, "/api/vpn-portal/profiles/21");
  assert.equal(calls[1].init?.method, "PATCH");
  assert.equal(
    calls[1].init?.body,
    JSON.stringify({ display_name: "Рабочий ноутбук" }),
  );
  assert.equal(headerValue(calls[1].init?.headers, "X-CSRF-Token"), "rename-csrf-secret");

  for (const call of calls) {
    assert.equal(call.init?.credentials, "same-origin");
    assert.equal(call.init?.cache, "no-store");
    assert.equal(headerValue(call.init?.headers, "Content-Type"), "application/json");
    assert.equal(headerValue(call.init?.headers, "Authorization"), null);
    assert.equal(headerValue(call.init?.headers, "Origin"), null);
    assert.equal(call.url.includes("csrf-secret"), false);
    assert.equal(call.url.startsWith("/api/control"), false);
    assert.equal(call.url.startsWith("/api/auth"), false);
  }
});

test("HTTP errors use static Russian messages without reading unsafe response data", async () => {
  const cases = [
    [401, "Нужно войти снова."],
    [403, "Действие недоступно."],
    [429, "Слишком много попыток. Повторите позже."],
    [503, "Не удалось выполнить запрос. Попробуйте ещё раз."],
  ];

  for (const [status, expectedMessage] of cases) {
    let bodyRead = false;
    globalThis.fetch = async () => ({
      ok: false,
      status,
      statusText: "provider secret status text",
      json: async () => {
        bodyRead = true;
        throw new Error("secret JSON body");
      },
      text: async () => {
        bodyRead = true;
        return "secret text body";
      },
    });

    await assert.rejects(portalApi.me(), (error) => {
      assert.equal(error instanceof PortalError, true);
      assert.equal(error.status, status);
      assert.equal(error.message, expectedMessage);
      assert.equal(error.message.includes("secret"), false);
      assert.equal(error.message.includes("provider"), false);
      return true;
    });
    assert.equal(bodyRead, false);
  }
});

test("network failures are wrapped without exposing the original exception", async () => {
  globalThis.fetch = async () => {
    throw new Error("upstream URL and private response body");
  };

  await assert.rejects(portalApi.profiles(), (error) => {
    assert.equal(error instanceof PortalError, true);
    assert.equal(error.status, 0);
    assert.equal(error.message, "Не удалось выполнить запрос. Попробуйте ещё раз.");
    assert.equal(error.message.includes("upstream"), false);
    assert.equal(error.message.includes("private"), false);
    return true;
  });
});

test("invalid success JSON is wrapped without exposing the parser exception", async () => {
  globalThis.fetch = async () => ({
    ok: true,
    status: 200,
    json: async () => {
      throw new Error("secret bytes from malformed response");
    },
  });

  await assert.rejects(portalApi.config(), (error) => {
    assert.equal(error instanceof PortalError, true);
    assert.equal(error.message, "Не удалось выполнить запрос. Попробуйте ещё раз.");
    assert.equal(error.message.includes("secret bytes"), false);
    return true;
  });
});
