import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const distRoot = path.join(frontendRoot, "dist");
const outputRoot = process.env.PORTAL_QA_OUTPUT_DIR
  ? path.resolve(process.env.PORTAL_QA_OUTPUT_DIR)
  : path.resolve(frontendRoot, "../.pytest_cache/portal-ui-qa");
const playwrightPath = process.env.PLAYWRIGHT_MODULE_PATH || "playwright";
const { chromium } = require(playwrightPath);

function mimeType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".svg")) return "image/svg+xml";
  return "application/octet-stream";
}

async function startStaticServer() {
  const server = createServer(async (request, response) => {
    try {
      const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
      const relative = requestUrl.pathname === "/cabinet/"
        ? "cabinet/index.html"
        : requestUrl.pathname.replace(/^\/+/, "");
      const resolved = path.resolve(distRoot, relative || "index.html");
      if (!resolved.startsWith(`${distRoot}${path.sep}`) && resolved !== path.join(distRoot, "index.html")) {
        response.writeHead(404).end("not found");
        return;
      }
      const body = await readFile(resolved);
      response.writeHead(200, { "content-type": mimeType(resolved), "cache-control": "no-store" });
      response.end(body);
    } catch {
      response.writeHead(404).end("not found");
    }
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  assert.ok(address && typeof address === "object");
  return {
    origin: `http://127.0.0.1:${address.port}`,
    close: () => new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve())),
  };
}

const portalConfig = {
  enabled: true,
  browser_login_enabled: true,
  mini_app_enabled: true,
  login_path: "/api/vpn-portal/auth/telegram/start",
  support_text: "Напишите нам в Telegram. <b>Это текст, не HTML.</b>",
};
const me = { display_name: "Анна Ветрова", csrf_token: "csrf-anna" };
const subscriptions = [
  {
    id: 11,
    service_name: "Veltrix VPN",
    state: "active",
    starts_at: "2026-09-01T00:00:00Z",
    expires_at: "2026-10-01T00:00:00Z",
    profile_limit: 3,
    profiles_used: 2,
    traffic_limit_gb_per_profile: 100,
  },
  {
    id: 12,
    service_name: "Veltrix VPN",
    state: "expired",
    starts_at: null,
    expires_at: "2026-02-01T00:00:00Z",
    profile_limit: 1,
    profiles_used: 1,
    traffic_limit_gb_per_profile: null,
  },
];
const profiles = [
  { id: 21, subscription_id: 11, display_name: "iPhone", state: "active", can_connect: true },
  { id: 22, subscription_id: 12, display_name: "Старый ключ", state: "active", can_connect: false },
];
const disabledTrial = {
  state: "disabled",
  duration_days: 7,
  profile_limit: 1,
  subscription_id: null,
  access_key_id: null,
  expires_at: null,
};

function responseJson(route, body, status = 200) {
  return route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(body),
  });
}

async function newPortalPage(browser, options = {}) {
  const context = await browser.newContext({
    viewport: options.viewport || { width: 390, height: 844 },
    colorScheme: options.colorScheme || "light",
  });
  const page = await context.newPage();
  const initData = options.initData || "";
  const platform = options.platform ?? (initData ? "android" : "unknown");
  const safeInsets = options.safeInsets || null;
  const sdkInitData = options.cacheDrivenSdk
    ? `(JSON.parse(sessionStorage.getItem("__telegram__initParams")||"{}").tgWebAppData||"")`
    : JSON.stringify(initData);
  await page.route("https://telegram.org/js/telegram-web-app.js", (route) => route.fulfill({
    status: 200,
    contentType: "text/javascript",
    body: `window.Telegram={WebApp:{initData:${sdkInitData},platform:${JSON.stringify(platform)},ready(){},expand(){}}};${safeInsets ? `for(const [name,value] of Object.entries(${JSON.stringify(safeInsets)})){document.documentElement.style.setProperty(name,value+"px");}` : ""}`,
  }));
  await page.addInitScript(({ seedCache, cacheData, removeClipboard }) => {
    if (seedCache && sessionStorage.getItem("portal-qa-seeded") !== "yes") {
      sessionStorage.setItem("__telegram__initParams", JSON.stringify({
        tgWebAppData: cacheData,
        tgWebAppThemeParams: "{\"bg_color\":\"#fff\"}",
      }));
      sessionStorage.setItem("unrelated", "preserved");
      sessionStorage.setItem("portal-qa-seeded", "yes");
    }
    if (removeClipboard) {
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
    }
  }, {
    seedCache: options.seedCache ?? true,
    cacheData: initData || "cached-secret",
    removeClipboard: options.removeClipboard ?? false,
  });
  return { context, page };
}

async function installApi(page, handler, { trial = disabledTrial } = {}) {
  await page.route("**/api/vpn-portal/**", async (route) => {
    const url = new URL(route.request().url());
    if (trial !== null && route.request().method() === "GET" && url.pathname.endsWith("/trial")) {
      return responseJson(route, trial);
    }
    await handler(route, url.pathname, route.request());
  });
}

function assertNoHorizontalOverflow(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    if (root.scrollWidth > root.clientWidth) {
      throw new Error(`horizontal overflow: ${root.scrollWidth} > ${root.clientWidth}`);
    }
  });
}

async function verifyFullPortal(browser, origin) {
  const { context, page } = await newPortalPage(browser, { removeClipboard: true });
  const calls = [];
  let renameCalls = 0;
  let connectionCalls = 0;
  let cacheAtFirstRequest;
  await installApi(page, async (route, pathname, request) => {
    calls.push(`${request.method()} ${pathname}`);
    if (!cacheAtFirstRequest) {
      cacheAtFirstRequest = await page.evaluate(() => ({
        telegram: sessionStorage.getItem("__telegram__initParams"),
        unrelated: sessionStorage.getItem("unrelated"),
      }));
    }
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles);
    if (pathname.endsWith("/profiles/21/connection")) {
      connectionCalls += 1;
      return connectionCalls === 1
        ? responseJson(route, { uri: "vless://full-secret-uri@example.test:443?security=tls&very=long#iPhone" })
        : responseJson(route, {}, 404);
    }
    if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") {
      renameCalls += 1;
      return renameCalls === 1
        ? responseJson(route, { ...profiles[0], display_name: "Личный iPhone" })
        : responseJson(route, {}, 500);
    }
    return responseJson(route, {}, 404);
  });

  await page.goto(`${origin}/cabinet/#subscription`);
  await page.getByRole("heading", { name: "Ваша подписка" }).waitFor();
  assert.deepEqual(calls.slice(0, 2), ["GET /api/vpn-portal/config", "GET /api/vpn-portal/me"]);
  assert.equal(calls.some((call) => call.includes("auth/mini-app")), false);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppData, undefined);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppThemeParams, "{\"bg_color\":\"#fff\"}");
  assert.equal(cacheAtFirstRequest.unrelated, "preserved");
  assert.equal(await page.locator(".section-nav a").count(), 5);
  assert.equal(await page.locator("main .portal-section").count(), 1);
  assert.equal(await page.locator("#subscription").count(), 1);
  await page.getByText("Статистика пока недоступна").first().waitFor();
  await page.getByText("Дата начала не указана").waitFor();
  await page.getByRole("link", { name: "Подключить VPN" }).waitFor();
  assert.equal(/(^|\s)0 ГБ(?:\s|$)/m.test(await page.locator("body").innerText()), false);
  await assertNoHorizontalOverflow(page);

  await page.locator('.section-nav a[href="#connect"]').click();
  await page.getByRole("heading", { name: "Как подключиться" }).waitFor();
  assert.equal(await page.locator("main .portal-section").count(), 1);
  assert.equal(await page.locator('.section-nav a[aria-current="page"][href="#connect"]').count(), 1);
  await page.locator('.section-nav a[href="#plans"]').click();
  await page.getByText("Тарифы ещё не опубликованы").waitFor();
  await page.locator('.section-nav a[href="#help"]').click();
  await page.locator("#help").waitFor();
  assert.equal(await page.getByText("<b>Это текст, не HTML.</b>").count(), 1);
  assert.equal(await page.locator("#help b").count(), 0);
  await page.locator('.section-nav a[href="#profiles"]').click();
  await page.getByText("Подключение для этого профиля пока недоступно.").waitFor();
  await page.getByText(/Veltrix VPN ·/).first().waitFor();

  const firstProfile = page.locator(".profile-card").first();
  await firstProfile.getByRole("button", { name: "Показать ссылку подключения" }).click();
  const uri = firstProfile.locator("textarea");
  await uri.waitFor();
  assert.match(await uri.inputValue(), /^vless:\/\//);
  await firstProfile.getByRole("button", { name: "Скопировать" }).click();
  await firstProfile.getByText("Не удалось скопировать автоматически.").waitFor();

  await firstProfile.getByRole("button", { name: "Переименовать" }).click();
  await firstProfile.getByLabel("Название профиля").fill("Личный iPhone");
  await firstProfile.getByRole("button", { name: "Сохранить" }).click();
  await firstProfile.getByRole("heading", { name: "Личный iPhone" }).waitFor();
  assert.equal(await firstProfile.locator("textarea").count(), 0);
  await firstProfile.getByRole("button", { name: "Показать ссылку подключения" }).click();
  await firstProfile.getByText("Не удалось выполнить запрос.").waitFor();
  await firstProfile.getByRole("button", { name: "Переименовать" }).click();
  await firstProfile.getByLabel("Название профиля").fill("Ошибка");
  await firstProfile.getByRole("button", { name: "Сохранить" }).click();
  await firstProfile.getByText("Не удалось выполнить запрос.").waitFor();

  await page.screenshot({ path: path.join(outputRoot, "portal-390-light.png"), fullPage: true });
  await context.close();
}

async function verifyPublicTrialFlow(browser, origin) {
  const trialSubscription = {
    id: 31,
    service_name: "Veltrix VPN",
    state: "trial",
    starts_at: "2026-09-23T00:00:00Z",
    expires_at: "2026-09-30T00:00:00Z",
    profile_limit: 1,
    profiles_used: 1,
    traffic_limit_gb_per_profile: null,
  };
  const trialProfile = {
    id: 41,
    subscription_id: 31,
    display_name: "Пробный профиль",
    state: "active",
    can_connect: true,
  };

  {
    const { context, page } = await newPortalPage(browser);
    let trialReads = 0;
    let activationCalls = 0;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/trial/activate")) {
        activationCalls += 1;
        assert.equal(request.method(), "POST");
        assert.equal(request.headers()["x-csrf-token"], me.csrf_token);
        return responseJson(route, {
          ...disabledTrial,
          state: "preparing",
          subscription_id: 31,
          access_key_id: 41,
          expires_at: "2026-09-30T00:00:00Z",
        });
      }
      if (pathname.endsWith("/trial")) {
        trialReads += 1;
        const state = trialReads === 1 ? "available" : trialReads === 2 ? "preparing" : "active";
        return responseJson(route, {
          ...disabledTrial,
          state,
          subscription_id: state === "available" ? null : 31,
          access_key_id: state === "available" ? null : 41,
          expires_at: state === "available" ? null : "2026-09-30T00:00:00Z",
        });
      }
      if (pathname.endsWith("/subscriptions")) {
        return responseJson(route, trialReads >= 3 ? [trialSubscription] : []);
      }
      if (pathname.endsWith("/profiles")) {
        return responseJson(route, trialReads >= 3 ? [trialProfile] : []);
      }
      return responseJson(route, {}, 404);
    }, { trial: null });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Получить 7 дней" }).click();
    await page.getByRole("heading", { name: "Готовим VPN-профиль" }).waitFor();
    await page.getByRole("heading", { name: "Пробный доступ готов" }).waitFor({ timeout: 5_000 });
    assert.equal(activationCalls, 1);
    assert.equal(trialReads, 3);
    await page.getByRole("link", { name: "Открыть профиль" }).click();
    await page.getByRole("heading", { name: trialProfile.display_name }).waitFor();
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let trialReads = 0;
    let pendingTrial;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/trial/activate")) {
        return responseJson(route, { ...disabledTrial, state: "preparing" });
      }
      if (pathname.endsWith("/trial")) {
        trialReads += 1;
        if (trialReads === 1) return responseJson(route, { ...disabledTrial, state: "available" });
        pendingTrial = route;
        return;
      }
      if (pathname.endsWith("/subscriptions") || pathname.endsWith("/profiles")) {
        return responseJson(route, []);
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    }, { trial: null });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Получить 7 дней" }).click();
    await page.getByRole("heading", { name: "Готовим VPN-профиль" }).waitFor();
    await page.waitForFunction(() => document.body.innerText.includes("Готовим VPN-профиль"));
    assert.ok(pendingTrial);
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await responseJson(pendingTrial, { ...disabledTrial, state: "active" });
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole("heading", { name: "Пробный доступ готов" }).count(), 0);
    assert.equal((await page.locator("body").innerText()).includes(me.display_name), false);
    await context.close();
  }
}

async function verifyMiniAppStates(browser, origin) {
  {
    const { context, page } = await newPortalPage(browser, {
      initData: "signed-bob",
      cacheDrivenSdk: true,
    });
    const calls = [];
    await installApi(page, async (route, pathname) => {
      calls.push(pathname);
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/auth/mini-app")) return responseJson(route, { display_name: "Bob", csrf_token: "csrf-bob" });
      if (pathname.endsWith("/me")) return responseJson(route, { display_name: "Bob", csrf_token: "csrf-bob" });
      if (pathname.endsWith("/subscriptions")) return responseJson(route, []);
      if (pathname.endsWith("/profiles")) return responseJson(route, []);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByText("Bob").waitFor();
    assert.deepEqual(calls.slice(0, 3), [
      "/api/vpn-portal/config",
      "/api/vpn-portal/auth/mini-app",
      "/api/vpn-portal/me",
    ]);
    assert.equal(page.url().includes("tgWebApp"), false);
    calls.length = 0;
    await page.reload();
    await page.getByText("Bob").waitFor();
    assert.deepEqual(calls.slice(0, 2), [
      "/api/vpn-portal/config",
      "/api/vpn-portal/me",
    ]);
    assert.equal(calls.includes("/api/vpn-portal/auth/mini-app"), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser, { initData: "expired" });
    const calls = [];
    await installApi(page, async (route, pathname) => {
      calls.push(pathname);
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/auth/mini-app")) return responseJson(route, {}, 401);
      return responseJson(route, me);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Нужно открыть Mini App заново" }).waitFor();
    assert.deepEqual(calls, ["/api/vpn-portal/config", "/api/vpn-portal/auth/mini-app"]);
    assert.equal((await page.locator("body").innerText()).includes("Анна"), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser, { initData: "bob-signed" });
    const calls = [];
    await installApi(page, async (route, pathname) => {
      calls.push(pathname);
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/auth/mini-app")) return responseJson(route, {}, 409);
      if (pathname.endsWith("/me")) return responseJson(route, { display_name: "Alice secret", csrf_token: "alice-csrf" });
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, [{ display_name: "Alice secret" }]);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Открыт другой аккаунт" }).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("Alice secret"), false);
    assert.deepEqual(calls, ["/api/vpn-portal/config", "/api/vpn-portal/auth/mini-app"]);
    await page.getByRole("button", { name: "Выйти из текущего аккаунта" }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.deepEqual(calls, [
      "/api/vpn-portal/config",
      "/api/vpn-portal/auth/mini-app",
      "/api/vpn-portal/me",
      "/api/vpn-portal/logout",
    ]);
    assert.equal((await page.locator("body").innerText()).includes("Alice secret"), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser, { initData: "signed" });
    const calls = [];
    await installApi(page, async (route, pathname) => {
      calls.push(pathname);
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/auth/mini-app")) return responseJson(route, { display_name: "Bob", csrf_token: "csrf-bob" });
      if (pathname.endsWith("/me")) return responseJson(route, {}, 401);
      return responseJson(route, [], 200);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Браузер не сохранил вход" }).waitFor();
    assert.deepEqual(calls, [
      "/api/vpn-portal/config",
      "/api/vpn-portal/auth/mini-app",
      "/api/vpn-portal/me",
    ]);
    await context.close();
  }
}

async function verifySessionInvalidation(browser, origin) {
  {
    const { context, page } = await newPortalPage(browser);
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByText("Анна Ветрова").waitFor();
    await page.locator('.section-nav a[href="#profiles"]').click();
    await page.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("Анна Ветрова"), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let pendingConnection;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        pendingConnection = route;
        return;
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByText("Анна Ветрова").waitFor();
    await page.locator('.section-nav a[href="#profiles"]').click();
    await page.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await page.waitForFunction(() => document.body.innerText.includes("Получаем ссылку"));
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { uri: "vless://must-never-render" });
    await page.waitForTimeout(100);
    assert.equal((await page.locator("body").innerText()).includes("must-never-render"), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let logoutCalls = 0;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/logout")) {
        logoutCalls += 1;
        return responseJson(route, {}, 401);
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByText("Анна Ветрова").waitFor();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.equal(logoutCalls, 1);
    assert.equal((await page.locator("body").innerText()).includes("Анна Ветрова"), false);
    await context.close();
  }
}

async function verifyProfileRequestRaces(browser, origin) {
  {
    const { context, page } = await newPortalPage(browser);
    let pendingConnection;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") {
        return responseJson(route, { ...profiles[0], display_name: "После гонки" });
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await card.getByText("Получаем ссылку…").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("После гонки");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await card.getByRole("heading", { name: "После гонки" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { uri: "vless://obsolete-after-rename" });
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let pendingRename;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") { pendingRename = route; return; }
      if (pathname.endsWith("/connection")) return responseJson(route, { uri: "vless://must-not-start" });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Новое имя");
    await card.getByRole("button", { name: "Сохранить" }).click();
    const reveal = card.getByRole("button", { name: "Показать ссылку подключения" });
    assert.equal(await reveal.isDisabled(), true);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Новое имя" });
    await card.getByRole("heading", { name: "Новое имя" }).waitFor();
    assert.equal(await reveal.isDisabled(), false);
    await context.close();
  }
}

async function verifyParallelLoadUnauthorized(browser, origin) {
  for (const failingFirst of ["subscriptions", "profiles"]) {
    const { context, page } = await newPortalPage(browser);
    let heldRoute;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith(`/${failingFirst}`)) return responseJson(route, {}, 503);
      if (pathname.endsWith(failingFirst === "subscriptions" ? "/profiles" : "/subscriptions")) {
        heldRoute = route;
        return;
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Повторить" }).waitFor();
    await page.getByText(me.display_name).waitFor();
    assert.ok(heldRoute);
    await responseJson(heldRoute, {}, 401);
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    assert.equal((await page.locator("body").innerText()).includes(me.display_name), false);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let subscriptionsCalls = 0;
    let profilesCalls = 0;
    let staleProfilesRoute;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) {
        subscriptionsCalls += 1;
        return subscriptionsCalls === 1
          ? responseJson(route, {}, 503)
          : responseJson(route, subscriptions.slice(0, 1));
      }
      if (pathname.endsWith("/profiles")) {
        profilesCalls += 1;
        if (profilesCalls === 1) {
          staleProfilesRoute = route;
          return;
        }
        return responseJson(route, profiles.slice(0, 1));
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Повторить" }).click();
    await page.getByText("Статистика пока недоступна").waitFor();
    assert.ok(staleProfilesRoute);
    await responseJson(staleProfilesRoute, {}, 401);
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole("heading", { name: "Сессия завершена" }).count(), 0);
    await page.getByText(me.display_name).waitFor();
    await context.close();
  }
}

async function verifyRenameAcrossHashRemount(browser, origin) {
  {
    const { context, page } = await newPortalPage(browser);
    let pendingRename;
    let connectionCalls = 0;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        connectionCalls += 1;
        return responseJson(route, { uri: `vless://connection-${connectionCalls}` });
      }
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") {
        pendingRename = route;
        return;
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    let card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await card.locator("textarea").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Имя после");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await page.locator('.section-nav a[href="#help"]').click();
    await page.locator('.section-nav a[href="#profiles"]').click();
    card = page.locator(".profile-card").first();
    const reveal = card.getByRole("button", { name: "Показать ссылку подключения" });
    assert.equal(await reveal.isDisabled(), true);
    assert.equal(await card.locator("textarea").count(), 0);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Имя после" });
    await card.getByRole("heading", { name: "Имя после" }).waitFor();
    assert.equal(await card.locator("textarea").count(), 0);
    await card.getByRole("button", { name: "Показать ссылку подключения" }).click();
    assert.equal(await card.locator("textarea").inputValue(), "vless://connection-2");
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let pendingConnection;
    let pendingRename;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") { pendingRename = route; return; }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    let card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await card.getByText("Получаем ссылку…").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Ещё имя");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await page.locator('.section-nav a[href="#help"]').click();
    await page.locator('.section-nav a[href="#profiles"]').click();
    card = page.locator(".profile-card").first();
    assert.ok(pendingConnection);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Ещё имя" });
    await responseJson(pendingConnection, { uri: "vless://obsolete-pending-reveal" });
    await card.getByRole("heading", { name: "Ещё имя" }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await context.close();
  }
}

async function verifyStatePages(browser, origin) {
  for (const scenario of [
    { status: 200, body: { ...portalConfig, enabled: false }, heading: "Личный кабинет пока недоступен" },
    { status: 503, body: {}, heading: "Не удалось открыть кабинет" },
  ]) {
    const { context, page } = await newPortalPage(browser);
    await installApi(page, (route, pathname) => pathname.endsWith("/config")
      ? responseJson(route, scenario.body, scenario.status)
      : responseJson(route, {}, 500));
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: scenario.heading }).waitFor();
    await context.close();
  }

  const { context, page } = await newPortalPage(browser);
  await installApi(page, async (route, pathname) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/profiles")) return responseJson(route, []);
    return responseJson(route, {}, 404);
  });
  await page.goto(`${origin}/cabinet/`);
  await page.getByText("Подписок пока нет.").waitFor();
  await page.locator('.section-nav a[href="#profiles"]').click();
  await page.getByText("Профилей пока нет.").waitFor();
  await context.close();
}

async function verifyPlatformAwareUnauthenticatedStates(browser, origin) {
  for (const platform of ["unknown", "android"]) {
    const { context, page } = await newPortalPage(browser, { platform });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Войдите в личный кабинет" }).waitFor();
    if (platform === "unknown") {
      await page.getByRole("link", { name: "Войти через Telegram" }).waitFor();
    } else {
      await page.getByText("Закройте это окно и откройте Mini App из Telegram заново.").waitFor();
      assert.equal(await page.getByRole("link", { name: "Войти через Telegram" }).count(), 0);
    }
    await context.close();
  }

  for (const platform of ["unknown", "ios"]) {
    const { context, page } = await newPortalPage(browser, { platform });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("button", { name: "Показать ссылку подключения" }).click();
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    if (platform === "unknown") {
      await page.getByRole("link", { name: "Войти через Telegram" }).waitFor();
    } else {
      await page.getByText("Закройте это окно и откройте Mini App из Telegram заново.").waitFor();
      assert.equal(await page.getByRole("link", { name: "Войти через Telegram" }).count(), 0);
    }
    await context.close();
  }
}

async function verifyMountedFragmentPreservation(browser, origin) {
  for (const scenario of [
    { hash: "#tgWebAppData=secret&keep=yes", expectedHash: "#keep=yes", heading: "Ваша подписка" },
    { hash: "#/profiles?keep=yes", expectedHash: "#/profiles?keep=yes", heading: "Профили" },
  ]) {
    const { context, page } = await newPortalPage(browser, { seedCache: false });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/${scenario.hash}`);
    await page.getByRole("heading", { name: scenario.heading }).waitFor();
    assert.equal(await page.evaluate(() => location.hash), scenario.expectedHash);
    await context.close();
  }
}

async function verifyTelegramSafeAreaGeometry(browser, origin) {
  const safeInsets = {
    "--tg-safe-area-inset-top": 20,
    "--tg-safe-area-inset-right": 16,
    "--tg-safe-area-inset-bottom": 18,
    "--tg-safe-area-inset-left": 12,
    "--tg-content-safe-area-inset-top": 40,
    "--tg-content-safe-area-inset-right": 10,
    "--tg-content-safe-area-inset-bottom": 50,
    "--tg-content-safe-area-inset-left": 8,
  };
  {
    const { context, page } = await newPortalPage(browser, {
      platform: "android",
      safeInsets,
      viewport: { width: 390, height: 844 },
    });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByText(me.display_name).waitFor();
    const geometry = await page.evaluate(() => {
      const brand = document.querySelector(".portal-header .brand").getBoundingClientRect();
      const account = document.querySelector(".account").getBoundingClientRect();
      const shell = getComputedStyle(document.querySelector(".portal-shell"));
      return { brandTop: brand.top, brandLeft: brand.left, accountRight: account.right, shellBottom: parseFloat(shell.paddingBottom) };
    });
    assert.ok(geometry.brandTop >= 72);
    assert.ok(geometry.brandLeft >= 40);
    assert.ok(geometry.accountRight <= 344);
    assert.ok(geometry.shellBottom >= 96);

    await page.evaluate(() => {
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-top", "55px");
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-left", "30px");
    });
    await page.waitForFunction(() => document.querySelector(".portal-header .brand").getBoundingClientRect().left >= 62);
    const shiftedTop = await page.locator(".portal-header .brand").evaluate((element) => element.getBoundingClientRect().top);
    assert.ok(shiftedTop >= 87);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser, {
      platform: "unknown",
      safeInsets,
      viewport: { width: 390, height: 844 },
    });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Войдите в личный кабинет" }).waitFor();
    const geometry = await page.evaluate(() => {
      const pageBox = document.querySelector(".state-page").getBoundingClientRect();
      const brand = document.querySelector(".state-page .brand").getBoundingClientRect();
      const login = document.querySelector(".state-page .button").getBoundingClientRect();
      const style = getComputedStyle(document.querySelector(".state-page"));
      return {
        pageLeft: pageBox.left,
        brandTop: brand.top,
        loginRight: login.right,
        paddingTop: parseFloat(style.paddingTop),
        paddingBottom: parseFloat(style.paddingBottom),
      };
    });
    assert.ok(geometry.pageLeft >= 40);
    assert.ok(geometry.brandTop >= 88);
    assert.ok(geometry.loginRight <= 344);
    assert.ok(geometry.paddingTop >= 88);
    assert.ok(geometry.paddingBottom >= 96);
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser, {
      platform: "android",
      safeInsets,
      viewport: { width: 390, height: 844 },
    });
    await installApi(page, (route, pathname) => pathname.endsWith("/config")
      ? responseJson(route, {}, 503)
      : responseJson(route, {}, 500));
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Не удалось открыть кабинет" }).waitFor();
    const brand = await page.locator(".state-page .brand").evaluate((element) => element.getBoundingClientRect().toJSON());
    assert.ok(brand.top >= 88);
    assert.ok(brand.left >= 40);
    await context.close();
  }
}

async function captureResponsiveMatrix(browser, origin) {
  for (const width of [320, 390, 768, 1280]) {
    for (const colorScheme of ["light", "dark"]) {
      const { context, page } = await newPortalPage(browser, {
        viewport: { width, height: width <= 390 ? 720 : 900 },
        colorScheme,
      });
      await installApi(page, async (route, pathname) => {
        if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
        if (pathname.endsWith("/me")) return responseJson(route, me);
        if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
        if (pathname.endsWith("/profiles")) return responseJson(route, profiles);
        return responseJson(route, {}, 404);
      });
      await page.goto(`${origin}/cabinet/`);
      await page.getByText("Анна Ветрова").waitFor();
      await assertNoHorizontalOverflow(page);
      if (width <= 390) {
        const navFits = await page.locator(".section-nav a").evaluateAll((links) => links.every((link) => {
          const rect = link.getBoundingClientRect();
          return rect.left >= 0 && rect.right <= document.documentElement.clientWidth;
        }));
        assert.equal(navFits, true);
      }
      await page.screenshot({
        path: path.join(outputRoot, `portal-${width}-${colorScheme}.png`),
        fullPage: true,
      });
      await context.close();
    }
  }
}

async function verifyRealSdkCacheCleanup(browser, origin) {
  if (!process.env.TELEGRAM_SDK_PATH) {
    return "skipped (set TELEGRAM_SDK_PATH for the downloaded official SDK spot-check)";
  }
  const sdkSource = await readFile(path.resolve(process.env.TELEGRAM_SDK_PATH), "utf8");
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  let observed;
  await page.route("https://telegram.org/js/telegram-web-app.js", (route) => route.fulfill({
    status: 200,
    contentType: "text/javascript",
    body: sdkSource,
  }));
  await installApi(page, async (route, pathname) => {
    if (!observed) {
      observed = await page.evaluate(() => ({
        url: location.href,
        cache: sessionStorage.getItem("__telegram__initParams"),
      }));
    }
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/auth/mini-app")) return responseJson(route, { display_name: "Synthetic QA", csrf_token: "csrf-qa" });
    if (pathname.endsWith("/me")) return responseJson(route, { display_name: "Synthetic QA", csrf_token: "csrf-qa" });
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/profiles")) return responseJson(route, []);
    return responseJson(route, {}, 404);
  });
  const signed = "query_id=synthetic-qa&user=%7B%22id%22%3A1%7D&auth_date=1&hash=synthetic";
  const theme = JSON.stringify({ bg_color: "#ffffff" });
  const hash = new URLSearchParams({
    tgWebAppData: signed,
    tgWebAppVersion: "8.0",
    tgWebAppPlatform: "android",
    tgWebAppThemeParams: theme,
  });
  await page.goto(`${origin}/cabinet/#${hash}`);
  await page.getByText("Synthetic QA").waitFor();
  assert.ok(observed);
  assert.equal(observed.url.includes("tgWebAppData"), false);
  const cache = JSON.parse(observed.cache);
  assert.equal(cache.tgWebAppData, undefined);
  assert.equal(typeof cache.tgWebAppThemeParams, "string");
  await context.close();
  return "passed with downloaded official SDK and synthetic launch data (not a live Telegram client)";
}

await mkdir(outputRoot, { recursive: true });
process.env.CHROME_LOG_FILE = path.join(outputRoot, "chromium.log");
const server = await startStaticServer();
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE_PATH
    ? { executablePath: process.env.CHROMIUM_EXECUTABLE_PATH }
    : {}),
});

try {
  await verifyFullPortal(browser, server.origin);
  await verifyPublicTrialFlow(browser, server.origin);
  await verifyMiniAppStates(browser, server.origin);
  await verifySessionInvalidation(browser, server.origin);
  await verifyProfileRequestRaces(browser, server.origin);
  await verifyParallelLoadUnauthorized(browser, server.origin);
  await verifyRenameAcrossHashRemount(browser, server.origin);
  await verifyStatePages(browser, server.origin);
  await verifyPlatformAwareUnauthenticatedStates(browser, server.origin);
  await verifyMountedFragmentPreservation(browser, server.origin);
  await verifyTelegramSafeAreaGeometry(browser, server.origin);
  await captureResponsiveMatrix(browser, server.origin);
  const sdkResult = await verifyRealSdkCacheCleanup(browser, server.origin);
  console.log(`Portal browser QA passed. Screenshots: ${outputRoot}`);
  console.log(`Official SDK cache spot-check: ${sdkResult}`);
} finally {
  await browser.close();
  await server.close();
}
