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

async function capture(page, options) {
  if (process.argv.includes("--functional-only")) return;
  await page.evaluate(async () => {
    if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    document.documentElement.style.scrollBehavior = "auto";
    window.scrollTo({top:0,behavior:"instant"});
    await document.fonts.ready;
    await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  });
  await page.screenshot(options);
}

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
  support_url: null, notice: null, reminders_enabled: false,
  support_text: "Напишите нам в Telegram. <b>Это текст, не HTML.</b>",
};
const me = { display_name: "Анна Ветрова", csrf_token: "csrf-anna" };
const subscriptions = [
  {
    id: 11,
    service_name: "Veltrix VPN",
    state: "active",
    starts_at: "2026-09-01T00:00:00Z",
    expires_at: "2027-01-06T00:00:00Z",
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
  if(Array.isArray(body)) body=body.map(row=>row && "can_connect" in row ? {credential_generation:1,operation:null,...row}:row);
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
    reducedMotion: options.reducedMotion || "no-preference",
    ...(options.userAgent ? { userAgent: options.userAgent } : {}),
  });
  const page = await context.newPage();
  const diagnostics = [];
  page.on("console", (message) => diagnostics.push(message.text()));
  page.on("pageerror", (error) => diagnostics.push(error.message));
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
  await page.addInitScript(({ seedCache, cacheData, removeClipboard, captureClipboard, delayClipboard, strictGestureCopy }) => {
    if (seedCache && sessionStorage.getItem("portal-qa-seeded") !== "yes") {
      sessionStorage.setItem("__telegram__initParams", JSON.stringify({
        tgWebAppData: cacheData,
        tgWebAppThemeParams: "{\"bg_color\":\"#fff\"}",
      }));
      sessionStorage.setItem("unrelated", "preserved");
      sessionStorage.setItem("portal-qa-seeded", "yes");
    }
    if (removeClipboard) {
      window.__portalLegacyClipboardWrites = [];
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
      document.execCommand = (command) => {
        if (command !== "copy") return false;
        const fields = document.querySelectorAll("input, textarea");
        window.__portalLegacyClipboardWrites.push(fields[fields.length - 1]?.value || "");
        return true;
      };
    }
    if (captureClipboard) {
      window.__portalClipboardWrites = [];
      document.execCommand = () => false;
      Object.defineProperty(navigator, "clipboard", {
        configurable: true,
        value: {
          writeText: delayClipboard
            ? (value) => new Promise((resolve) => {
              window.__completePortalClipboard = () => {
                window.__portalClipboardWrites.push(value);
                resolve();
              };
            })
            : async (value) => { window.__portalClipboardWrites.push(value); },
        },
      });
    }
    if (strictGestureCopy) {
      window.__portalLegacyClipboardWrites = [];
      window.__portalCopyGestureActive = false;
      Object.defineProperty(navigator, "clipboard", { configurable: true, value: undefined });
      document.addEventListener("click", () => {
        window.__portalCopyGestureActive = true;
      }, true);
      document.addEventListener("click", () => { window.__portalCopyGestureActive = false; });
      document.execCommand = (command) => {
        if (command !== "copy" || !window.__portalCopyGestureActive) return false;
        const fields = document.querySelectorAll("input, textarea");
        window.__portalLegacyClipboardWrites.push(fields[fields.length - 1]?.value || "");
        return true;
      };
    }
  }, {
    seedCache: options.seedCache ?? true,
    cacheData: initData || "cached-secret",
    removeClipboard: options.removeClipboard ?? false,
    captureClipboard: options.captureClipboard ?? false,
    delayClipboard: options.delayClipboard ?? false,
    strictGestureCopy: options.strictGestureCopy ?? false,
  });
  return { context, page, diagnostics };
}

async function installApi(page, handler, { trial = disabledTrial, locations = { current: null, pending_endpoint_id: null, selection_state: "unavailable", available: [] }, usage = true, preferences = true } = {}) {
  await page.route("**/api/vpn-portal/**", async (route) => {
    const url = new URL(route.request().url());
    if (trial !== null && route.request().method() === "GET" && url.pathname.endsWith("/trial")) {
      return responseJson(route, trial);
    }
    if(usage && url.pathname.endsWith("/usage")) return responseJson(route,{upload_bytes:null,download_bytes:null,measured_since:null,last_sample_at:null,coverage:"none",days:Number(url.searchParams.get("days")),devices:[]});
    if(preferences && url.pathname.endsWith("/preferences")) return responseJson(route,{expiry_reminders:true});
    if (locations !== null && url.pathname.endsWith("/v2/locations")) return responseJson(route, []);
    if (locations !== null && url.pathname.endsWith("/locations")) return responseJson(route, locations);
    await handler(route, url.pathname, route.request());
  });
}

async function verifyDeviceLocations(browser, origin) {
  const germany = { endpoint_id: 7, name: "Германия", country_code: "DE", is_published: true };
  const netherlands = { endpoint_id: 8, name: "Нидерланды", country_code: "NL", is_published: true };
  const ready = { current: germany, pending_endpoint_id: null, selection_state: "ready", available: [germany, netherlands] };
  const devices = profiles.map((profile) => ({ ...profile, id: profile.id + 100, legacy_profile_id: profile.id }));
  const stableUrl = "https://vpn.example/api/vpn-portal/subscription/synthetic-device-secret";
  const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
  let currentLocations = ready;
  let pendingSelection;
  let posts = 0;
  let deviceLoads = 0;
  const requests = [];
  await installApi(page, async (route, pathname, request) => {
    requests.push(pathname);
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    if (pathname.endsWith("/v2/devices")) { deviceLoads += 1; return responseJson(route, devices); }
    if (pathname.endsWith("/locations")) return responseJson(route, currentLocations);
    if (pathname.endsWith("/121/location")) {
      posts += 1;
      assert.equal(request.headers()["x-csrf-token"], me.csrf_token);
      assert.deepEqual(request.postDataJSON(), { endpoint_id: 8 });
      pendingSelection = route;
      return;
    }
    if (pathname.endsWith("/121/connection")) return responseJson(route, { subscription_url: stableUrl, direct_uri: "https://vpn.example/subscription/must-not-render" });
    return responseJson(route, {}, 404);
  }, { locations: null });
  await page.goto(`${origin}/cabinet/#profiles`);
  await page.locator(".profile-card").first().waitFor({ timeout: 3000 });
  assert.equal(await page.locator(".profile-card").count(), 2, "logical devices, not child credentials, determine card count");
  const card = page.locator(".profile-card").first();
  await card.getByLabel("Сервер").selectOption("8");
  assert.equal(posts, 0, "native selection alone never mutates");
  await card.getByRole("button", { name: "Показать ссылку" }).click();
  assert.equal(await card.locator("textarea").inputValue(), stableUrl);
  await card.getByRole("button", { name: "Скопировать", exact: true }).click();
  assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), [stableUrl]);
  await card.getByRole("button", { name: "Показать QR-код" }).click();
  await card.locator("img.qr-code").waitFor();
  await card.getByRole("button", { name: "Выбрать сервер" }).evaluate((button) => { button.click(); button.click(); });
  await page.waitForFunction(() => document.querySelector(".device-location-picker button[type=submit]")?.disabled);
  assert.equal(posts, 1, "held POST has a synchronous duplicate guard");
  currentLocations = { ...ready, pending_endpoint_id: 8, selection_state: "preparing" };
  await responseJson(pendingSelection, currentLocations, 202);
  await card.getByText("Готовим подключение. Текущий сервер остаётся доступен.").waitFor();
  assert.equal(await card.locator("textarea").count(), 0, "country mutation clears prepared credentials");
  assert.equal(await card.getByLabel("Сервер").isDisabled(), false, "another server remains selectable while provisioning");
  currentLocations = { ...ready, current: netherlands };
  await card.getByText("Текущий сервер: NL · Нидерланды", { exact: true }).waitFor({ timeout: 5000 });
  await page.waitForFunction(() => document.querySelector(".device-location-picker")?.textContent.includes("После смены сервера"));
  assert.ok(deviceLoads >= 2, "ready transition refreshes parent device metadata");
  await card.getByRole("button", { name: "Показать ссылку", exact: true }).click();
  assert.equal(await card.locator("textarea").inputValue(), stableUrl);
  assert.equal(requests.some((pathname) => /\/profiles(?:\/|$)/.test(pathname)), false);
  assert.equal((await page.locator("body").innerText()).includes("https://vpn.example/subscription/must-not-render"), false);
  await assertNoHorizontalOverflow(page);
  await assertVisibleInteractiveAccessibility(page, "device-locations-390", ".portal-nav");
  await capture(page, { path: path.join(outputRoot, "device-locations-390.png"), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await assertNoHorizontalOverflow(page);
  await capture(page, { path: path.join(outputRoot, "device-locations-desktop.png"), fullPage: true });
  await assertTwoHundredPercentTextReflow(page, "device-locations-desktop-200", ".portal-nav");
  await capture(page, { path: path.join(outputRoot, "device-locations-desktop-200.png"), fullPage: true });
  await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/must-not-render");
  await context.close();
}

async function verifyInitialDeviceLocationStates(browser, origin) {
  const a = { endpoint_id: 7, name: "Германия", country_code: "DE", is_published: true };
  const b = { endpoint_id: 8, name: "Нидерланды", country_code: "NL", is_published: true };
  const device = { ...profiles[0], id: 121, legacy_profile_id: 21, can_connect: false };
  for (const scenario of [
    { state: "pending_sync", selection: "preparing", pollToReady: true },
    { state: "sync_failed", selection: "failed" },
    { state: "unavailable", selection: "unavailable" },
    { state: "revoked", selection: "unavailable", blocked: true },
    { state: "active", selection: "unavailable", blocked: true, subscriptionState: "expired" },
  ]) {
    const { context, page } = await newPortalPage(browser);
    let gets = 0;
    let posts = 0;
    let deviceLoads = 0;
    await installApi(page, (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, [{ ...subscriptions[0], state: scenario.subscriptionState || "active" }]);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/v2/devices")) {
        deviceLoads += 1;
        const ready = scenario.pollToReady && gets >= 2;
        return responseJson(route, [{ ...device, state: ready ? "active" : scenario.state, can_connect: ready }]);
      }
      if (pathname.endsWith("/locations")) {
        gets += 1;
        const ready = scenario.pollToReady && gets >= 2;
        return responseJson(route, {
          current: ready ? a : null, pending_endpoint_id: ready ? null : 7,
          selection_state: ready ? "ready" : scenario.selection, available: scenario.blocked ? [] : [a, b],
        });
      }
      if (pathname.endsWith("/location") && request.method() === "POST") {
        posts += 1;
        assert.deepEqual(request.postDataJSON(), { endpoint_id: 8 });
        return responseJson(route, { current: null, pending_endpoint_id: 8, selection_state: "preparing", available: [a, b] }, 202);
      }
      return responseJson(route, {}, 404);
    }, { locations: null });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card");
    const select = card.getByLabel("Сервер");
    await card.locator(".device-location-picker [role]").filter({ hasText: /Готовим подключение|Не удалось подготовить|Сейчас нет доступного/ }).waitFor();
    assert.equal(await card.getByRole("button", { name: "Показать ссылку" }).isDisabled(), true, "no usable current credential means copying stays blocked");
    assert.equal(await select.isDisabled(), Boolean(scenario.blocked), `${scenario.state}: server-authorized choices do not depend on current credential readiness`);
    if (scenario.blocked) {
      assert.equal(await card.getByRole("button", { name: "Выбрать сервер" }).isDisabled(), true);
      await card.locator("form.device-location-picker").evaluate((form) => form.requestSubmit());
      assert.equal(posts, 0, "revoked or expired access cannot submit without an available endpoint");
    } else if (scenario.pollToReady) {
      assert.equal(await card.getByRole("button", { name: "Обновить статус", exact: true }).isDisabled(), false);
      await card.getByText("Текущий сервер: DE · Германия", { exact: true }).waitFor({ timeout: 5000 });
      await page.waitForFunction(() => ![...document.querySelectorAll("button")].find((button) => button.textContent === "Показать ссылку")?.disabled);
      assert.equal(gets, 2, "initial preparation polls without an active A credential");
      assert.equal(deviceLoads, 2, "initial ready promotion refreshes parent connectability");
    } else {
      await select.selectOption("8");
      await card.getByRole("button", { name: "Выбрать сервер" }).click();
      await card.getByText("Готовим подключение. Текущий сервер остаётся доступен.").waitFor();
      assert.equal(posts, 1, "another country can be selected after initial failure or unavailability");
      assert.equal(await select.isDisabled(), false);
      await card.getByRole("button", { name: "Переименовать" }).click();
      assert.equal(await select.isDisabled(), true, "renaming still blocks the picker");
      assert.equal(await card.getByRole("button", { name: "Обновить статус", exact: true }).isDisabled(), true);
      await card.getByRole("button", { name: "Отмена", exact: true }).click();
      assert.equal(await select.isDisabled(), false);
    }
    await context.close();
  }
}

async function verifyDeviceLocationEdges(browser, origin) {
  const a = { endpoint_id: 7, name: "Германия", country_code: "DE", is_published: true };
  const b = { endpoint_id: 8, name: "Нидерланды", country_code: "NL", is_published: true };
  const device = { ...profiles[0], id: 121, legacy_profile_id: 21 };
  const ready = { current: a, pending_endpoint_id: null, selection_state: "ready", available: [a, b] };
  const stableUrl = "https://vpn.example/subscription/synthetic-stable-secret";
  async function fixture(page, handler, options) {
    return installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/connection")) return responseJson(route, { subscription_url: stableUrl, direct_uri: "https://vpn.example/subscription/never-reveal-child" });
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return handler(route, pathname, request);
    }, { locations: null, ...options });
  }
  {
    const { context, page } = await newPortalPage(browser);
    let deviceLoads = 0;
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) { deviceLoads += 1; return responseJson(route, [device]); }
      if (pathname.endsWith("/locations")) return responseJson(route, ready);
      if (pathname.endsWith("/location")) return responseJson(route, { ...ready, current: b }, 202);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card");
    await card.getByLabel("Сервер").selectOption("8");
    await card.getByRole("button", { name: "Выбрать сервер" }).click();
    await card.getByText("Текущий сервер: NL · Нидерланды", { exact: true }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(deviceLoads, 2, "reusing an already-ready child also refreshes parent connectability");
    await context.close();
  }
  {
    const { context, page, diagnostics } = await newPortalPage(browser);
    let heldQr;
    await page.route("**/assets/index-*.js", (route) => { heldQr = route; });
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) return responseJson(route, ready);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("button", { name: "Показать ссылку" }).click();
    await page.locator("textarea").waitFor();
    await page.getByRole("button", { name: "Показать QR-код" }).click();
    await page.getByRole("button", { name: "Создаём QR-код…" }).waitFor();
    assert.ok(heldQr, "QR module is held locally before logout");
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await heldQr.continue();
    await page.waitForTimeout(100);
    assert.equal(await page.locator(".qr-code").count(), 0);
    await assertSecretAbsent(page, diagnostics, stableUrl);
    await context.close();
  }
  {
    const { context, page } = await newPortalPage(browser);
    let gets = 0;
    let posts = 0;
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) return ++gets === 1 ? responseJson(route, {}, 503) : responseJson(route, ready);
      if (pathname.endsWith("/location")) { posts += 1; return responseJson(route, {}, 409); }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("alert").filter({ hasText: "Не удалось загрузить серверы" }).waitFor();
    assert.equal(await page.getByRole("button", { name: "Выбрать сервер" }).isDisabled(), true);
    await page.getByRole("button", { name: "Обновить статус", exact: true }).click();
    await page.getByLabel("Сервер").selectOption("8");
    await page.getByRole("button", { name: "Выбрать сервер" }).click();
    await page.getByRole("alert").filter({ hasText: "Не удалось выбрать сервер" }).waitFor();
    await page.getByText("Текущий сервер: DE · Германия", { exact: true }).waitFor();
    assert.equal(await page.getByLabel("Сервер").inputValue(), "8");
    await page.waitForTimeout(2100);
    assert.equal(posts, 1, "failed mutation is never retried automatically");
    await context.close();
  }
  for (const state of ["failed", "verification_required"]) {
    const { context, page } = await newPortalPage(browser);
    let gets = 0;
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) { gets += 1; return responseJson(route, ready); }
      if (pathname.endsWith("/location")) return responseJson(route, { ...ready, selection_state: state, pending_endpoint_id: 8 }, 202);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card");
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    await card.locator("textarea").waitFor();
    await card.getByLabel("Сервер").selectOption("8");
    await card.getByRole("button", { name: "Выбрать сервер" }).click();
    await card.getByText(state === "failed"
      ? "Не удалось подготовить сервер. Выберите его снова, чтобы повторить попытку."
      : "Результат подготовки проверяется. Можно выбрать другой доступный сервер.").waitFor();
    if (state === "failed") assert.equal(await card.getByRole("alert").filter({ hasText: "Не удалось подготовить сервер" }).count(), 1, "failed preparation is announced as a failed action");
    assert.equal(await card.locator("textarea").count(), 0);
    await card.getByText("Текущий сервер: DE · Германия", { exact: true }).waitFor();
    assert.equal(await card.getByLabel("Сервер").isDisabled(), false);
    await page.waitForTimeout(2100);
    assert.equal(gets, 1, "terminal preparation states stop polling");
    await context.close();
  }
  {
    const { context, page } = await newPortalPage(browser);
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) return responseJson(route, {
        ...ready, current: { ...a, is_published: false }, pending_endpoint_id: 99,
        selection_state: "preparing", available: [b],
      });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByText("Готовим подключение. Текущий сервер остаётся доступен.").waitFor();
    assert.equal(await page.getByLabel("Сервер").inputValue(), "");
    assert.equal(await page.getByRole("button", { name: "Выбрать сервер" }).isDisabled(), true, "a no-longer-selectable pending server cannot enable a blank submit");
    await page.getByLabel("Сервер").selectOption("8");
    assert.equal(await page.getByRole("button", { name: "Выбрать сервер" }).isDisabled(), false);
    await context.close();
  }
  {
    const { context, page } = await newPortalPage(browser);
    let gets = 0;
    let oldGet;
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) {
        gets += 1;
        if (gets === 2) { oldGet = route; return; }
        return responseJson(route, { ...ready, current: { ...a, name: "Ранее выданное подключение", country_code: null, is_published: false }, available: [b] });
      }
      if (pathname.endsWith("/location")) return responseJson(route, { ...ready, current: b }, 202);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card");
    await card.getByText("Текущий сервер: Ранее выданное подключение", { exact: true }).waitFor();
    assert.deepEqual(await card.getByLabel("Сервер").locator("option").allTextContents(), ["Выберите сервер", "NL · Нидерланды"]);
    await card.getByLabel("Сервер").selectOption("8");
    await card.getByRole("button", { name: "Обновить статус" }).click();
    await page.waitForTimeout(50);
    assert.ok(oldGet);
    // A submit already queued before a refresh must supersede that older GET.
    await card.locator("form.device-location-picker").evaluate((form) => form.requestSubmit());
    await card.getByText("Текущий сервер: NL · Нидерланды", { exact: true }).waitFor();
    await responseJson(oldGet, ready);
    await page.waitForTimeout(100);
    assert.equal(await card.getByText("Текущий сервер: DE · Германия", { exact: true }).count(), 0);
    await context.close();
  }
  {
    const { context, page } = await newPortalPage(browser);
    await page.addInitScript(() => {
      const schedule = window.setTimeout.bind(window);
      const cancel = window.clearTimeout.bind(window);
      const pending = new Map();
      let id = 100000;
      window.setTimeout = (callback, delay, ...args) => {
        if (delay !== 2000) return schedule(callback, delay, ...args);
        pending.set(++id, () => callback(...args));
        return id;
      };
      window.clearTimeout = (timer) => { pending.delete(timer); cancel(timer); };
      window.__runLocationPoll = () => {
        const next = pending.entries().next().value;
        if (!next) return false;
        pending.delete(next[0]); next[1](); return true;
      };
    });
    let gets = 0;
    await fixture(page, (route, pathname) => {
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith("/locations")) { gets += 1; return responseJson(route, { ...ready, selection_state: "preparing", pending_endpoint_id: 8 }); }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByText("Готовим подключение. Текущий сервер остаётся доступен.").waitFor();
    for (let attempt = 0; attempt < 30; attempt += 1) {
      assert.equal(await page.evaluate(() => window.__runLocationPoll()), true);
      await page.waitForFunction(() => !document.querySelector(".device-location-picker select")?.disabled);
    }
    await page.getByText("Подготовка ещё не завершена. Обновите статус вручную.").waitFor();
    assert.equal(gets, 31, "initial GET plus exactly thirty automatic attempts");
    assert.equal(await page.evaluate(() => window.__runLocationPoll()), false);
    assert.equal(gets, 31);
    await page.getByRole("button", { name: "Обновить статус", exact: true }).click();
    await page.waitForTimeout(50);
    assert.equal(gets, 32);
    assert.equal(await page.evaluate(() => window.__runLocationPoll()), false, "manual status refresh does not silently restart the exhausted automatic budget");
    await context.close();
  }
  for (const delayed of ["locations", "location", "connection"]) {
    const { context, page, diagnostics } = await newPortalPage(browser);
    let held;
    await installApi(page, (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/v2/devices")) return responseJson(route, [device]);
      if (pathname.endsWith(`/${delayed}`)) { held = route; return; }
      if (pathname.endsWith("/locations")) return responseJson(route, ready);
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    }, { locations: null });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.locator(".profile-card").waitFor();
    if (delayed === "location") {
      await page.getByLabel("Сервер").selectOption("8");
      await page.getByRole("button", { name: "Выбрать сервер" }).click();
    }
    if (delayed === "connection") await page.getByRole("button", { name: "Показать ссылку" }).click();
    await page.waitForTimeout(50);
    assert.ok(held, `held ${delayed} request must exist before logout`);
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await responseJson(held, delayed === "connection" ? { subscription_url: stableUrl, direct_uri: "https://vpn.example/subscription/never" } : { ...ready, current: b });
    await page.waitForTimeout(100);
    assert.equal(await page.locator(".profile-card, textarea, .qr-code").count(), 0);
    await assertSecretAbsent(page, diagnostics, stableUrl);
    await context.close();
  }
  {
    const { context, page } = await newPortalPage(browser);
    let deviceLoads = 0;
    let pendingBulk;
    let pendingRename;
    await fixture(page, (route, pathname, request) => {
      if (pathname.endsWith("/v2/devices")) {
        deviceLoads += 1;
        if (deviceLoads === 2) { pendingBulk = route; return; }
        return responseJson(route, [device]);
      }
      if (pathname.endsWith("/locations")) return responseJson(route, ready);
      if (pathname.endsWith("/121") && request.method() === "PATCH") { pendingRename = route; return; }
      return responseJson(route, {}, 404);
    }, { trial: { ...disabledTrial, state: "preparing" } });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card");
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Имя после обновления");
    await card.getByRole("button", { name: "Сохранить", exact: true }).click();
    await page.waitForTimeout(2200);
    assert.ok(pendingBulk);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...device, display_name: "Имя после обновления" });
    await card.getByRole("heading", { name: "Имя после обновления", exact: true }).waitFor();
    await responseJson(pendingBulk, [device]);
    await page.waitForTimeout(100);
    assert.equal(await card.getByRole("heading", { name: "Имя после обновления", exact: true }).count(), 1, "bulk started during rename cannot overwrite the saved result");
    await context.close();
  }
}

function assertNoHorizontalOverflow(page) {
  return page.evaluate(() => {
    const root = document.documentElement;
    if (root.scrollWidth > root.clientWidth) {
      throw new Error(`horizontal overflow: ${root.scrollWidth} > ${root.clientWidth}`);
    }
  });
}

const accessibilityAuditCounts = {};

async function assertVisibleInteractiveAccessibility(page, label, fixedNavigationSelector = null) {
  const initial = await page.evaluate((auditLabel) => {
    const isVisible = (element) => {
      const closed = element.closest("details:not([open])");
      if (closed && !closed.querySelector("summary")?.contains(element)) return false;
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden"
        && box.width > 0 && box.height > 0;
    };
    const accessibleName = (element) => {
      const labelledBy = element.getAttribute("aria-labelledby");
      const labelledText = labelledBy
        ? labelledBy.split(/\s+/).map((id) => document.getElementById(id)?.textContent || "").join(" ")
        : "";
      const nativeLabels = "labels" in element
        ? [...(element.labels || [])].map((item) => item.textContent || "").join(" ")
        : "";
      return (element.getAttribute("aria-label") || labelledText || nativeLabels
        || element.closest("label")?.textContent || element.textContent || element.getAttribute("title") || "").trim();
    };
    const selector = 'a[href], button, input, select, textarea, [contenteditable="true"], [tabindex]';
    const controls = [...document.querySelectorAll(selector)].filter(isVisible);
    const tabbableControls = controls.filter((element) => element.tabIndex >= 0
      && !("disabled" in element && element.disabled));
    tabbableControls.forEach((element, index) => {
      element.setAttribute("data-portal-a11y-audit-id", `${auditLabel}-${index + 1}`);
    });
    const unnamed = controls.filter((element) => !accessibleName(element)).map((element) => element.outerHTML);
    const exposedDecorativeSvg = [...document.querySelectorAll("svg")]
      .filter((element) => isVisible(element) && element.getAttribute("role") !== "img"
        && element.getAttribute("aria-hidden") !== "true")
      .map((element) => element.outerHTML);
    return {
      ids: tabbableControls.map((element) => element.getAttribute("data-portal-a11y-audit-id")),
      visibleCount: controls.length,
      cap: Math.max(32, (tabbableControls.length + 1) * 2),
      unnamed,
      exposedDecorativeSvg,
    };
  }, label);

  assert.equal(initial.unnamed.length, 0, `visible controls without accessible names: ${initial.unnamed.join(" | ")}`);
  assert.equal(
    initial.exposedDecorativeSvg.length,
    0,
    `decorative SVGs exposed to accessibility tree: ${initial.exposedDecorativeSvg.join(" | ")}`,
  );

  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
    window.scrollTo({top:0,behavior:"instant"});
  });
  const reached = new Set();
  let firstId = null;
  let cycled = false;
  for (let index = 0; index < initial.cap; index += 1) {
    await page.keyboard.press("Tab");
    const activeId = await page.evaluate(() => document.activeElement?.getAttribute("data-portal-a11y-audit-id"));
    if (!activeId) continue;
    if (firstId === null) firstId = activeId;
    else if (activeId === firstId) {
      cycled = true;
      break;
    }
    assert.equal(reached.has(activeId), false, `${label}: ${activeId} repeated before traversal wrapped`);
    await page.waitForFunction(({ navigationSelector, auditId }) => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement)
        || element.getAttribute("data-portal-a11y-audit-id") !== auditId) return false;
      const box = element.getBoundingClientRect();
      const navigation = navigationSelector ? document.querySelector(navigationSelector) : null;
      const navigationBox = navigation?.getBoundingClientRect() || null;
      const insideNavigation = Boolean(navigation?.contains(element));
      const obscured = navigationBox && !insideNavigation
        && box.top < navigationBox.bottom && box.bottom > navigationBox.top;
      return !obscured && box.top >= -1 && box.left >= -1
        && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1;
    }, { navigationSelector: fixedNavigationSelector, auditId: activeId }, { timeout: 1000 }).catch(() => {});
    const focus = await page.evaluate(({ navigationSelector, auditId }) => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement)) return { error: "active element is not HTML" };
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      const navigation = navigationSelector ? document.querySelector(navigationSelector) : null;
      const navigationBox = navigation?.getBoundingClientRect() || null;
      const insideNavigation = Boolean(navigation?.contains(element));
      const belowFixedNavigation = Boolean(navigationBox && !insideNavigation
        && box.top < navigationBox.bottom && box.bottom > navigationBox.top);
      const visibleFocus = element.matches(":focus-visible")
        && ((style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2) || style.boxShadow !== "none");
      return {
        name: (element.getAttribute("aria-label") || element.textContent || "").trim(),
        auditId: element.getAttribute("data-portal-a11y-audit-id"),
        tagName: element.tagName,
        withinViewport: box.top >= -1 && box.left >= -1
          && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1,
        belowFixedNavigation,
        visibleFocus,
      };
    }, { navigationSelector: fixedNavigationSelector, auditId: activeId });
    assert.equal(focus.error, undefined, `keyboard traversal ${index + 1}: ${focus.error}`);
    assert.equal(focus.auditId, activeId, `${label}: focus changed before it was audited`);
    assert.equal(focus.withinViewport, true, `keyboard traversal ${index + 1}: focused ${focus.tagName} "${focus.name}" is outside the viewport`);
    assert.equal(focus.belowFixedNavigation, false, `focused ${focus.tagName} "${focus.name}" is obscured by fixed navigation`);
    assert.equal(focus.visibleFocus, true, `focused ${focus.tagName} "${focus.name}" has no visible focus indication`);
    reached.add(activeId);
  }
  assert.equal(cycled, true, `${label}: keyboard traversal did not wrap within ${initial.cap} steps`);
  assert.deepEqual([...reached].sort(), [...initial.ids].sort(), `${label}: not every visible tabbable was reached`);
  accessibilityAuditCounts[label] = { visible: initial.visibleCount, tabbable: initial.ids.length };
  await page.evaluate(() => document.querySelectorAll("[data-portal-a11y-audit-id]").forEach(
    (element) => element.removeAttribute("data-portal-a11y-audit-id"),
  ));
}

async function applyTwoHundredPercentTextScaling(page, rootSelector = "body") {
  // Deterministic browser text-only zoom: snapshot every computed size before applying 2× inline values.
  await page.evaluate((selector) => {
    const root = document.querySelector(selector);
    if (!(root instanceof HTMLElement)) throw new Error(`missing text-scale root: ${selector}`);
    const isVisible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const values = [root, ...root.querySelectorAll("*")]
      .filter((element) => element instanceof HTMLElement && isVisible(element))
      .map((element) => {
        const style = getComputedStyle(element);
        return { element, fontSize: parseFloat(style.fontSize), lineHeight: parseFloat(style.lineHeight) };
      });
    values.forEach(({ element, fontSize, lineHeight }) => {
      if (Number.isFinite(fontSize)) element.style.setProperty("font-size", `${fontSize * 2}px`, "important");
      if (Number.isFinite(lineHeight)) element.style.setProperty("line-height", `${lineHeight * 2}px`, "important");
    });
  }, rootSelector);
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function assertTwoHundredPercentTextReflow(page, label, fixedNavigationSelector = null) {
  await applyTwoHundredPercentTextScaling(page);
  await page.evaluate(() => {
    window.scrollTo(0, 0);
  });
  await assertNoHorizontalOverflow(page);
  const result = await page.evaluate((navigationSelector) => {
    const isVisible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      const visuallyHidden = style.position === "absolute" && box.width <= 1 && box.height <= 1
        && (style.clip !== "auto" || style.clipPath !== "none");
      return style.display !== "none" && style.visibility !== "hidden"
        && box.width > 0 && box.height > 0 && !visuallyHidden;
    };
    const isContainedByHorizontalScroller = (element) => {
      for (let ancestor = element.parentElement; ancestor; ancestor = ancestor.parentElement) {
        const ancestorStyle = getComputedStyle(ancestor);
        if (["auto", "scroll"].includes(ancestorStyle.overflowX)
          && ancestor.scrollWidth > ancestor.clientWidth + 1) return true;
      }
      return false;
    };
    const hasDirectText = (element) => [...element.childNodes]
      .some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim());
    const elements = [...document.querySelectorAll("*")]
      .filter((element) => element instanceof HTMLElement && isVisible(element))
      .filter((element) => hasDirectText(element)
        || element.matches("input, textarea, select, button, a, h1, h2, h3, p, span, strong, small, label, dt, dd, th, td"));
    const clipped = elements.filter((element) => {
      const style = getComputedStyle(element);
      const clips = ["hidden", "clip"].includes(style.overflowX)
        || ["hidden", "clip"].includes(style.overflowY)
        || element.matches("input, textarea, select, button, .button");
      return clips && (element.scrollWidth > element.clientWidth + 1
        || element.scrollHeight > element.clientHeight + 1);
    }).map((element) => `${element.tagName}:${element.textContent?.trim().slice(0, 80)}`);
    const outsidePage = elements.filter((element) => {
      const box = element.getBoundingClientRect();
      return (box.left < -1 || box.right > document.documentElement.clientWidth + 1)
        && !isContainedByHorizontalScroller(element);
    }).map((element) => `${element.tagName}:${element.textContent?.trim().slice(0, 80)}`);
    const navigation = navigationSelector ? document.querySelector(navigationSelector) : null;
    const navigationBox = navigation?.getBoundingClientRect() || null;
    const navigationOcclusion = navigationBox ? elements.filter((element) => !navigation.contains(element)).filter((element) => {
      const box = element.getBoundingClientRect();
      const documentTop = box.top + scrollY;
      const documentBottom = box.bottom + scrollY;
      const maxScrollY = Math.max(0, document.documentElement.scrollHeight - innerHeight);
      const firstScrollThatClearsNavigation = Math.max(0, documentBottom - navigationBox.top);
      const lastScrollThatKeepsTopVisible = Math.min(maxScrollY, documentTop);
      const canFitAboveNavigation = box.height <= navigationBox.top + 1
        && firstScrollThatClearsNavigation <= lastScrollThatKeepsTopVisible + 1;
      return !canFitAboveNavigation;
    }).map((element) => `${element.tagName}:${element.textContent?.trim().slice(0, 80)}`) : [];
    const overlapCandidates = elements.filter((element) => hasDirectText(element)
      || element.matches("input, textarea, select, button, a"));
    const overlaps = [];
    for (let leftIndex = 0; leftIndex < overlapCandidates.length; leftIndex += 1) {
      const left = overlapCandidates[leftIndex];
      const leftBox = left.getBoundingClientRect();
      for (let rightIndex = leftIndex + 1; rightIndex < overlapCandidates.length; rightIndex += 1) {
        const right = overlapCandidates[rightIndex];
        if (left.contains(right) || right.contains(left)) continue;
        if (navigation && navigation.contains(left) !== navigation.contains(right)) continue;
        if (left.parentElement !== right.parentElement
          && !(left.matches("input, textarea, select, button, a") && right.matches("input, textarea, select, button, a"))) continue;
        const rightBox = right.getBoundingClientRect();
        if (Math.min(leftBox.right, rightBox.right) - Math.max(leftBox.left, rightBox.left) > 1
          && Math.min(leftBox.bottom, rightBox.bottom) - Math.max(leftBox.top, rightBox.top) > 1) {
          overlaps.push(`${left.tagName}:${left.textContent?.trim().slice(0, 32)} <> ${right.tagName}:${right.textContent?.trim().slice(0, 32)}`);
        }
      }
    }
    return { clipped, outsidePage, navigationOcclusion, overlaps };
  }, fixedNavigationSelector);
  assert.deepEqual(result.clipped, [], `${label}: text/control clipping at 200%: ${result.clipped.join(" | ")}`);
  assert.deepEqual(result.outsidePage, [], `${label}: text/control page overflow at 200%: ${result.outsidePage.join(" | ")}`);
  assert.deepEqual(result.navigationOcclusion, [], `${label}: text is obscured by fixed navigation: ${result.navigationOcclusion.join(" | ")}`);
  assert.deepEqual(result.overlaps, [], `${label}: overlapping text/control boxes at 200%: ${result.overlaps.join(" | ")}`);
  await assertVisibleInteractiveAccessibility(page, `${label}-keyboard`, fixedNavigationSelector);
}

async function assertReducedMotionStopsContinuousDecoration(page) {
  const animated = await page.evaluate(() => [...document.querySelectorAll(
    ".cabinet-home, .cabinet-home *, .vx-atmosphere, .vx-atmosphere *",
  )].filter((element) => {
    const style = getComputedStyle(element);
    return style.display !== "none" && style.visibility !== "hidden"
      && style.animationName !== "none"
      && style.animationIterationCount === "infinite"
      && parseFloat(style.animationDuration) > 0;
  }).map((element) => `${element.tagName}.${element.className}`));
  assert.deepEqual(animated, [], `continuous decoration remains under reduced motion: ${animated.join(" | ")}`);
}

async function assertAccessibleSubscriptionLayout(page) {
  const viewport = await page.evaluate(() => innerWidth);
  const cards = await page.locator(".cabinet-home > .vx-matte").evaluateAll(elements => elements.map(element => {
    const b = element.getBoundingClientRect();
    return { left: b.left, right: b.right, width: b.width, scroll: element.scrollWidth, client: element.clientWidth };
  }));
  assert.equal(cards.length, 3, "overview, connection and device preview are discoverable");
  assert.ok(cards.every(c => c.left >= 0 && c.right <= viewport && c.width > 0 && c.scroll <= c.client + 1), "cabinet card overflow: " + JSON.stringify(cards));
}

async function primaryActionContrastSnapshot(locator) {
  return locator.evaluateAll((elements) => {
    const parseColor = (value) => {
      const match = value.match(/^rgba?\(([^)]+)\)$/);
      if (match) {
        const values = match[1].split(/[\s,\/]+/).filter(Boolean).map(Number);
        if (values.length < 3 || values.slice(0, 3).some(Number.isNaN)) return null;
        return { rgb: values.slice(0, 3), alpha: values[3] ?? 1 };
      }
      const srgb = value.match(/^color\(srgb\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s*\/\s*([\d.]+))?\)$/);
      if (srgb) {
        return {
          rgb: srgb.slice(1, 4).map((channel) => Number(channel) * 255),
          alpha: srgb[4] === undefined ? 1 : Number(srgb[4]),
        };
      }
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      if (!context) return null;
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = value;
      context.fillRect(0, 0, 1, 1);
      const [red, green, blue, alpha] = context.getImageData(0, 0, 1, 1).data;
      return { rgb: [red, green, blue], alpha: alpha / 255 };
    };
    const channel = (value) => {
      const normalized = value / 255;
      return normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4;
    };
    const luminance = (rgb) => (
      0.2126 * channel(rgb[0]) + 0.7152 * channel(rgb[1]) + 0.0722 * channel(rgb[2])
    );
    return elements.map((element) => {
      const style = getComputedStyle(element);
      const foreground = parseColor(style.color);
      const background = parseColor(style.backgroundColor);
      const ratio = foreground && background && background.alpha === 1
        ? (Math.max(luminance(foreground.rgb), luminance(background.rgb)) + 0.05)
          / (Math.min(luminance(foreground.rgb), luminance(background.rgb)) + 0.05)
        : 0;
      return {
        label: element.textContent?.trim(),
        foreground: style.color,
        foregroundRgb: foreground?.rgb ?? null,
        background: style.backgroundColor,
        backgroundRgb: background?.rgb ?? null,
        backgroundImage: style.backgroundImage,
        ratio,
      };
    });
  });
}

async function assertPrimaryActionContrast(page) {
  const actions = await primaryActionContrastSnapshot(page.locator(".button--primary:not(:disabled)"));

  assert.ok(actions.length > 0, "no enabled primary actions were rendered");
  assert.equal(
    actions.every(({ backgroundImage }) => backgroundImage === "none"),
    true,
    `primary action contrast cannot be measured through a gradient: ${JSON.stringify(actions)}`,
  );
  assert.equal(
    actions.every(({ ratio }) => ratio >= 4.5),
    true,
    `primary action contrast is below 4.5:1: ${JSON.stringify(actions)}`,
  );
}

async function assertHoveredPrimaryActionContrast(page) {
  const action = page.locator(".profile-card .button--primary:not(:disabled)").first();
  await action.waitFor();
  await action.hover();
  await page.waitForTimeout(220);
  const [hovered] = await primaryActionContrastSnapshot(action);
  assert.ok(hovered, "hovered primary action was not rendered");
  assert.equal(
    hovered.backgroundImage,
    "none",
    `hovered primary action contrast cannot be measured through a gradient: ${JSON.stringify(hovered)}`,
  );
  assert.ok(
    hovered.ratio >= 4.5,
    `hovered primary action contrast is below 4.5:1: ${JSON.stringify(hovered)}`,
  );
}

async function assertNavSafeAreaAndReserve(page, expectedSafeBottom = 0) {
  const geometry = await page.evaluate(async () => {
    const shell = document.querySelector(".portal-shell");
    const nav = document.querySelector(".portal-nav");
    const lastContent = document.querySelector(".cabinet-content")?.lastElementChild;
    if (!shell || !nav || !lastContent) throw new Error("portal navigation geometry is incomplete");
    const shellStyle = getComputedStyle(shell);
    const safeBottomProbe = document.createElement("span");
    safeBottomProbe.style.cssText = "position:fixed;bottom:var(--portal-safe-bottom);width:1px;height:1px;";
    document.querySelector(".veltrix-portal").append(safeBottomProbe);
    const safeBottom = parseFloat(getComputedStyle(safeBottomProbe).bottom) || 0;
    safeBottomProbe.remove();
    const initialScrollY = scrollY;
    const initialScrollBehavior = document.documentElement.style.scrollBehavior;
    document.documentElement.style.scrollBehavior = "auto";
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: "instant" });
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
    const navBox = nav.getBoundingClientRect();
    const lastBox = lastContent.getBoundingClientRect();
    const result = {
      viewportWidth: innerWidth,
      navHeight: navBox.height,
      navTop: navBox.top,
      navBottomGap: innerHeight - navBox.bottom,
      shellBottomPadding: parseFloat(shellStyle.paddingBottom),
      safeBottom,
      lastContentBottom: lastBox.bottom,
    };
    window.scrollTo(0, initialScrollY);
    document.documentElement.style.scrollBehavior = initialScrollBehavior;
    await new Promise((resolve) => requestAnimationFrame(resolve));
    return result;
  });

  assert.ok(
    geometry.navHeight >= 44 + geometry.safeBottom && geometry.navHeight <= 120 + geometry.safeBottom,
    `navigation height is ${geometry.navHeight}px at ${geometry.viewportWidth}px wide`,
  );
  assert.ok(geometry.safeBottom >= expectedSafeBottom, `resolved safe bottom is ${geometry.safeBottom}px`);
  assert.ok(
    geometry.navBottomGap >= -1 && geometry.navHeight >= geometry.safeBottom + 44,
    `navigation bottom ${geometry.navBottomGap}px does not honor safe bottom ${geometry.safeBottom}px`,
  );
  assert.ok(
    geometry.shellBottomPadding >= geometry.navHeight + geometry.navBottomGap + 16,
    `shell reserve ${geometry.shellBottomPadding}px does not clear navigation ${geometry.navHeight}px + ${geometry.navBottomGap}px`,
  );
  assert.ok(
    geometry.lastContentBottom <= geometry.navTop - 16,
    `last content remains behind navigation: ${JSON.stringify(geometry)}`,
  );
}

async function assertWideNavigationGeometry(page, label) {
  const result = await page.locator(".portal-nav").evaluate((navigation) => {
    const rect = (element) => {
      const box = element.getBoundingClientRect();
      return { top: box.top, right: box.right, bottom: box.bottom, left: box.left };
    };
    const contains = (outer, inner) => inner.top >= outer.top - 1
      && inner.right <= outer.right + 1
      && inner.bottom <= outer.bottom + 1
      && inner.left >= outer.left - 1;
    const intersects = (left, right) => Math.min(left.right, right.right) - Math.max(left.left, right.left) > 1
      && Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top) > 1;
    const navBox = rect(navigation);
    const links = [...navigation.querySelectorAll("a")].map((link) => {
      const linkBox = rect(link);
      const icon = link.querySelector("svg");
      const labelElement = link.querySelector("span");
      return {
        name: link.textContent?.trim() || "",
        box: linkBox,
        containedByNavigation: contains(navBox, linkBox),
        iconContained: icon ? contains(linkBox, rect(icon)) : false,
        labelContained: labelElement ? contains(linkBox, rect(labelElement)) : false,
        clipped: link.scrollWidth > link.clientWidth + 1 || link.scrollHeight > link.clientHeight + 1,
      };
    });
    const intersections = [];
    for (let leftIndex = 0; leftIndex < links.length; leftIndex += 1) {
      for (let rightIndex = leftIndex + 1; rightIndex < links.length; rightIndex += 1) {
        if (intersects(links[leftIndex].box, links[rightIndex].box)) {
          intersections.push(`${links[leftIndex].name} <> ${links[rightIndex].name}`);
        }
      }
    }
    return {
      navBox,
      navClipped: navigation.scrollWidth > navigation.clientWidth + 1
        || navigation.scrollHeight > navigation.clientHeight + 1,
      links,
      intersections,
      viewport: { width: innerWidth, height: innerHeight },
    };
  });

  assert.equal(result.navBox.left >= -1 && result.navBox.right <= result.viewport.width + 1, true, `${label}: navigation exits viewport`);
  assert.equal(result.navClipped, false, `${label}: navigation clips its contents`);
  assert.deepEqual(result.intersections, [], `${label}: navigation links intersect: ${result.intersections.join(" | ")}`);
  assert.equal(result.links.every(({ containedByNavigation }) => containedByNavigation), true, `${label}: link exits navigation: ${JSON.stringify(result.links)}`);
  assert.equal(result.links.every(({ iconContained }) => iconContained), true, `${label}: icon exits link: ${JSON.stringify(result.links)}`);
  assert.equal(result.links.every(({ labelContained }) => labelContained), true, `${label}: label exits link: ${JSON.stringify(result.links)}`);
  assert.equal(result.links.every(({ clipped }) => !clipped), true, `${label}: link clips its icon or label: ${JSON.stringify(result.links)}`);
}

async function assertWideLayoutUsesNativeFlow(page, label) {
  const result = await page.evaluate(() => ({
    overview: document.querySelector(".cabinet-overview").getBoundingClientRect().toJSON(),
    connect: document.querySelector(".cabinet-connect").getBoundingClientRect().toJSON(),
    zoom: getComputedStyle(document.querySelector(".cabinet-content")).zoom,
    positions: [...document.querySelectorAll(".cabinet-home h1,.cabinet-home h2,.cabinet-traffic-value")].map(e => getComputedStyle(e).position),
  }));
  assert.equal(result.zoom, "1", label + ": content uses zoom");
  assert.ok(result.positions.every(p => p !== "absolute"), label + ": essential content uses absolute positions");
  assert.ok(result.overview.right <= result.connect.left && Math.abs(result.overview.top - result.connect.top) < 1, label + ": desktop overview/connect must share a native grid row: " + JSON.stringify(result));
}

async function assertPortalVisualContract(page, viewportWidth) {
  const layout = await page.evaluate(() => ({
    content: document.querySelector(".cabinet-content").getBoundingClientRect().toJSON(),
    title: document.querySelector(".cabinet-access-heading h1").getBoundingClientRect().toJSON(),
    nav: getComputedStyle(document.querySelector(".portal-nav")).position,
    targets: [...document.querySelectorAll(".cabinet-import,.cabinet-connection-actions button,.portal-nav a,.cabinet-account,.cabinet-brand")].map(e => ({ label: e.textContent, b: e.getBoundingClientRect().toJSON() })),
  }));
  assert.ok(layout.content.width <= 1120.5);
  assert.ok(layout.title.height > 0 && layout.title.left >= 0 && layout.title.right <= viewportWidth);
  assert.equal(layout.nav, "fixed");
  assert.ok(layout.targets.every(({ b }) => b.width >= 44 && b.height >= 44), "undersized controls: " + JSON.stringify(layout.targets));
  await page.keyboard.press("Tab");
  const focus = await page.evaluate(() => { const e = document.activeElement, s = getComputedStyle(e); return { visible: e.matches(":focus-visible"), outline: s.outlineStyle, width: parseFloat(s.outlineWidth) }; });
  assert.equal(focus.visible, true); assert.notEqual(focus.outline, "none"); assert.ok(focus.width >= 2);
}

async function assertSecretAbsent(page, diagnostics, secret) {
  const exposure = await page.evaluate((needle) => {
    const controls = [...document.querySelectorAll("input, textarea, select")];
    const attributed = [...document.querySelectorAll("*")];
    return {
      html: document.documentElement.outerHTML.includes(needle),
      control: controls.some((element) => element.value.includes(needle)),
      attribute: attributed.some((element) => [...element.attributes].some((attribute) => (
        (attribute.name === "href" || attribute.name === "src" || attribute.name.startsWith("data-"))
        && attribute.value.includes(needle)
      ))),
    };
  }, secret);
  assert.deepEqual(exposure, { html: false, control: false, attribute: false });
  assert.equal(diagnostics.some((message) => message.includes(secret)), false);
}

function operationFixture(requestId, action, fields = {}) {
  return {id:requestId,request_id:requestId,device_id:21,action,stage:"uncertain",credential_generation:2,endpoint_id:7,public_error:null,created_at:"2026-10-26T00:00:00Z",updated_at:"2026-10-26T00:00:00Z",finished_at:null,...fields};
}

async function verifyCabinetOperations(browser, origin) {
  for (const action of ["replace", "revoke"]) {
    const {context,page,diagnostics} = await newPortalPage(browser,{captureClipboard:true});
    let device = {...profiles[0],credential_generation:1,operation:null};
    let heldConnection, heldPost, heldDevices, connectionCalls=0, delayDevices=false;
    const posts=[];
    await installApi(page,async(route,pathname,request)=>{
      if(pathname.endsWith("/config"))return responseJson(route,portalConfig);
      if(pathname.endsWith("/me"))return responseJson(route,me);
      if(pathname.endsWith("/plans"))return responseJson(route,[]);
      if(pathname.endsWith("/subscriptions"))return responseJson(route,subscriptions.slice(0,1));
      if(pathname.endsWith("/v2/devices")){if(delayDevices){heldDevices=route;return;}return responseJson(route,[device]);}
      if(pathname.endsWith("/connection")){connectionCalls++;if(connectionCalls===1){heldConnection=route;return;}return responseJson(route,{subscription_url:"https://vpn.example/subscription/new-generation"});}
      if(pathname.endsWith("/"+action)){
        assert.equal(request.headers()["x-csrf-token"],me.csrf_token);
        posts.push(request.postDataJSON());
        if(posts.length===1){heldPost=route;return;}
        delayDevices=true;
        return responseJson(route,operationFixture(posts[0].request_id,action,{stage:"completed",finished_at:"2026-10-26T00:01:00Z"}));
      }
      if(pathname.includes("/device-operations/"))return responseJson(route,operationFixture(posts[0].request_id,action));
      return responseJson(route,{},404);
    });
    await page.goto(origin+"/cabinet/#devices");
    const card=page.locator(".profile-card");
    await card.getByRole("button",{name:"Показать ссылку",exact:true}).click();
    await card.getByRole("button",{name:action==="replace"?"Заменить доступ":"Отключить устройство",exact:true}).click();
    await card.getByRole("heading",{name:action==="replace"?"Заменить доступ для «iPhone»":"Отключить «iPhone»",exact:true}).waitFor();
    const submit=card.getByRole("button",{name:action==="replace"?"Подтвердить замену":"Подтвердить отключение",exact:true});
    await submit.evaluate(button=>{button.click();button.click();});
    await page.waitForTimeout(50);
    assert.equal(posts.length,1,"immediate duplicate gesture must send one mutation");
    assert.ok(heldPost);assert.ok(heldConnection);
    await page.locator('.portal-nav a[href="#home"]').click();
    assert.equal(await page.getByRole("button",{name:"Скопировать",exact:true}).isDisabled(),true);
    await responseJson(heldConnection,{subscription_url:"https://vpn.example/subscription/obsolete-generation"});
    await page.waitForTimeout(50);
    assert.equal(connectionCalls,1,"held mutation must block Home prefetch across remount");
    await assertSecretAbsent(page,diagnostics,"https://vpn.example/subscription/obsolete-generation");
    await responseJson(heldPost,operationFixture(posts[0].request_id,action),202);
    await page.locator('.portal-nav a[href="#devices"]').click();
    await card.getByText("Результат проверяется. Действие ещё не завершено",{exact:true}).waitFor();
    await card.getByRole("button",{name:"Повторить запрос",exact:true}).click();
    await page.waitForTimeout(50);
    assert.equal(posts.length,2);assert.deepEqual(posts[0],posts[1],"uncertain retry must replay the same request UUID");
    await page.locator('.portal-nav a[href="#home"]').click();
    assert.equal(await page.getByRole("button",{name:"Скопировать",exact:true}).isDisabled(),true,"completed POST must wait for the refreshed generation");
    assert.equal(connectionCalls,1);
    assert.ok(heldDevices);
    device={...device,credential_generation:2,state:action==="revoke"?"revoked":"active",can_connect:action!=="revoke",operation:operationFixture(posts[0].request_id,action,{stage:"completed",finished_at:"2026-10-26T00:01:00Z"})};
    delayDevices=false;await responseJson(heldDevices,[device]);
    if(action==="replace"){
      await page.getByRole("button",{name:"Скопировать",exact:true}).click();
      assert.deepEqual(await page.evaluate(()=>window.__portalClipboardWrites),["https://vpn.example/subscription/new-generation"]);
    }else assert.equal(await page.getByRole("button",{name:"Скопировать",exact:true}).isDisabled(),true);
    await context.close();
  }
}

async function verifyHomeCountryPreparation(browser, origin) {
  for (const state of ["preparing","ready"]) {
    const {context,page}=await newPortalPage(browser);
    let heldCountry, connectionCalls=0;
    const location={endpoint_id:7,name:"Германия",country_code:"DE",is_published:true};
    await installApi(page,async(route,pathname)=>{
      if(pathname.endsWith("/config"))return responseJson(route,portalConfig);
      if(pathname.endsWith("/me"))return responseJson(route,me);
      if(pathname.endsWith("/plans"))return responseJson(route,[]);
      if(pathname.endsWith("/subscriptions"))return responseJson(route,subscriptions.slice(0,1));
      if(pathname.endsWith("/v2/devices"))return responseJson(route,profiles.slice(0,1));
      if(pathname.endsWith("/locations")){heldCountry=route;return;}
      if(pathname.endsWith("/connection")){connectionCalls++;return responseJson(route,{subscription_url:"https://vpn.example/subscription/country-pending"});}
      return responseJson(route,{},404);
    },{locations:null});
    await page.goto(origin+"/cabinet/#home");
    await page.getByRole("heading",{name:"Доступ активен",exact:true}).waitFor();
    await page.waitForTimeout(50);
    assert.ok(heldCountry);assert.equal(connectionCalls,0,"Home must check country preparation before preparing a credential");
    const before=await page.evaluate(()=>({height:document.documentElement.scrollHeight,importText:document.querySelector(".cabinet-import")?.textContent}));
    await responseJson(heldCountry,{current:location,pending_endpoint_id:state==="preparing"?8:null,selection_state:state,available:[location]});
    if(state==="preparing"){
      await page.waitForTimeout(50);
      assert.equal(connectionCalls,0);
      assert.equal(await page.getByRole("button",{name:"Скопировать",exact:true}).isDisabled(),true);
    }else{
      await page.waitForFunction(()=>document.querySelector(".cabinet-connection-actions button")?.disabled===false);
      const after=await page.evaluate(()=>({height:document.documentElement.scrollHeight,importText:document.querySelector(".cabinet-import")?.textContent}));
      if(process.argv.includes("--trace-settlement"))console.log("CONTROLLED SETTLEMENT "+JSON.stringify({before,after}));
      assert.ok(after.height<=before.height,"readiness hint must leave normal document flow when prepared");
    }
    await context.close();
  }
}

async function verifyCountryGenerationScope(browser, origin) {
  const {context,page}=await newPortalPage(browser);
  let generation=1,heldLocations;
  const germany={endpoint_id:7,name:"Германия",country_code:"DE",is_published:true};
  const netherlands={endpoint_id:8,name:"Нидерланды",country_code:"NL",is_published:true};
  await installApi(page,async(route,pathname,request)=>{
    if(pathname.endsWith("/config"))return responseJson(route,portalConfig);
    if(pathname.endsWith("/me"))return responseJson(route,me);
    if(pathname.endsWith("/plans"))return responseJson(route,[]);
    if(pathname.endsWith("/subscriptions"))return responseJson(route,subscriptions.slice(0,1));
    if(pathname.endsWith("/v2/devices"))return responseJson(route,[{...profiles[0],credential_generation:generation}]);
    if(pathname.endsWith("/locations")){
      if(generation===1){heldLocations=route;return;}
      return responseJson(route,{current:netherlands,pending_endpoint_id:null,selection_state:"ready",available:[netherlands]});
    }
    if(pathname.endsWith("/replace")){generation=2;return responseJson(route,operationFixture(request.postDataJSON().request_id,"replace",{stage:"completed",finished_at:"2026-10-26T00:01:00Z"}));}
    return responseJson(route,{},404);
  },{locations:null});
  await page.goto(origin+"/cabinet/#devices");
  const card=page.locator(".profile-card");
  await card.getByRole("button",{name:"Заменить доступ",exact:true}).click();
  await card.getByRole("button",{name:"Подтвердить замену",exact:true}).click();
  await card.getByText("Текущий сервер: NL · Нидерланды",{exact:true}).waitFor({timeout:1000});
  assert.ok(heldLocations);
  await responseJson(heldLocations,{current:germany,pending_endpoint_id:7,selection_state:"preparing",available:[germany]});
  await page.waitForTimeout(50);
  assert.equal(await card.getByText("Текущий сервер: NL · Нидерланды",{exact:true}).count(),1,"old-generation country completion must not overwrite the current row");
  await context.close();
}

async function verifyCabinetCreation(browser, origin) {
  const {context,page}=await newPortalPage(browser);
  const posts=[],countries=[];
  let created=null;
  const active=[subscriptions[0],{...subscriptions[0],id:13,profile_limit:1,profiles_used:1}];
  const country={endpoint_id:7,name:"Германия",country_code:"DE",is_published:true};
  await installApi(page,async(route,pathname,request)=>{
    if(pathname.endsWith("/config"))return responseJson(route,portalConfig);
    if(pathname.endsWith("/me"))return responseJson(route,me);
    if(pathname.endsWith("/plans"))return responseJson(route,[]);
    if(pathname.endsWith("/subscriptions"))return responseJson(route,active);
    if(pathname.endsWith("/v2/locations")){countries.push(Number(new URL(request.url()).searchParams.get("subscription_id")));return responseJson(route,[country]);}
    if(pathname.endsWith("/locations"))return responseJson(route,{current:country,pending_endpoint_id:null,selection_state:"ready",available:[country]});
    if(pathname.endsWith("/v2/devices")){
      if(request.method()==="GET")return responseJson(route,created?[profiles[0],created]:[profiles[0]]);
      posts.push(request.postDataJSON());assert.equal(request.headers()["x-csrf-token"],me.csrf_token);
      if(posts.length===1)return route.abort("failed");
      const done=posts.length>=3;
      const operation=operationFixture(posts[0].request_id,"create",{device_id:31,credential_generation:1,stage:done?"completed":"uncertain",finished_at:done?"2026-10-26T00:01:00Z":null});
      created={id:31,subscription_id:11,display_name:"Ноутбук",state:done?"active":"pending_sync",can_connect:done,credential_generation:1,operation};
      return responseJson(route,operation,done?200:202);
    }
    if(pathname.includes("/device-operations/"))return responseJson(route,created.operation);
    return responseJson(route,{},404);
  },{locations:null});
  await page.goto(origin+"/cabinet/#devices");
  await page.getByRole("button",{name:"Добавить устройство",exact:true}).click();
  const form=page.locator("#add-device");
  await form.getByLabel("Название устройства").fill("Ноутбук");
  assert.equal(await form.getByLabel("Подписка").inputValue(),"","multiple owned active subscriptions require an explicit choice");
  await form.getByLabel("Подписка").selectOption("13");
  await form.getByText(/Все места заняты/).waitFor();
  assert.equal(await form.getByRole("button",{name:"Добавить устройство",exact:true}).isDisabled(),true);
  await form.getByLabel("Подписка").selectOption("11");
  await form.getByLabel("Страна").selectOption("7");
  await form.getByRole("button",{name:"Добавить устройство",exact:true}).evaluate(button=>{button.click();button.click();});
  await form.getByText(/Результат запроса неизвестен/).waitFor();
  assert.equal(posts.length,1);
  await page.locator('.portal-nav a[href="#help"]').click();
  await page.locator('.portal-nav a[href="#devices"]').click();
  assert.equal(await form.getByLabel("Название устройства").inputValue(),"Ноутбук");
  assert.equal(await form.getByLabel("Название устройства").isDisabled(),true);
  await form.getByRole("button",{name:"Повторить запрос",exact:true}).click();
  await page.locator(".profile-card").filter({has:page.getByRole("heading",{name:"Ноутбук",exact:true})}).waitFor();
  assert.deepEqual(posts[0],posts[1]);
  await page.reload();
  const card=page.locator(".profile-card").filter({has:page.getByRole("heading",{name:"Ноутбук",exact:true})});
  await card.getByRole("button",{name:"Повторить запрос",exact:true}).click();
  await card.getByText("Устройство готово",{exact:true}).waitFor();
  assert.deepEqual(posts[1],posts[2],"metadata recovery replays the original creation payload and UUID");
  assert.ok(countries.includes(11)&&countries.includes(13));
  assert.equal(await page.locator(".profile-card").count(),2);
  await context.close();
}

async function verifyCabinetPreferencesUsageNotice(browser, origin) {
  const {context,page}=await newPortalPage(browser);
  let preference=true, heldUsage;
  const writes=[];
  const notice={title:"Проверка уведомления",body:"<b>Обычный текст</b>",starts_at:new Date(Date.now()-10000).toISOString(),ends_at:new Date(Date.now()+2500).toISOString()};
  await installApi(page,async(route,pathname,request)=>{
    if(pathname.endsWith("/config"))return responseJson(route,{...portalConfig,notice});
    if(pathname.endsWith("/me"))return responseJson(route,me);
    if(pathname.endsWith("/plans"))return responseJson(route,[]);
    if(pathname.endsWith("/subscriptions"))return responseJson(route,subscriptions.slice(0,1));
    if(pathname.endsWith("/v2/devices"))return responseJson(route,profiles.slice(0,1));
    if(pathname.endsWith("/connection"))return responseJson(route,{subscription_url:"https://vpn.example/subscription/usage-fixture"});
    if(pathname.endsWith("/usage")){
      const days=Number(new URL(request.url()).searchParams.get("days"));
      if(days===30){heldUsage=route;return;}
      return responseJson(route,{days,upload_bytes:1024,download_bytes:2048,coverage:"partial",measured_since:"2026-10-26T00:00:00Z",last_sample_at:"2026-10-26T00:01:00Z",devices:[{device_id:21,display_name:"iPhone",upload_bytes:1024,download_bytes:2048,coverage:"partial",measured_since:"2026-10-26T00:00:00Z",last_sample_at:"2026-10-26T00:01:00Z"}]});
    }
    if(pathname.endsWith("/preferences")){
      if(request.method()==="PATCH"){writes.push(request.postDataJSON());assert.equal(request.headers()["x-csrf-token"],me.csrf_token);preference=request.postDataJSON().expiry_reminders;}
      return responseJson(route,{expiry_reminders:preference});
    }
    return responseJson(route,{},404);
  },{usage:false,preferences:false});
  await page.goto(origin+"/cabinet/#home");
  await page.getByRole("heading",{name:notice.title}).waitFor();
  assert.equal(await page.locator(".cabinet-notice b").count(),0);
  await page.getByLabel("Период суммарного трафика аккаунта").selectOption("30");
  await page.waitForTimeout(50);assert.ok(heldUsage);
  assert.equal(await page.locator(".cabinet-traffic-value").innerText(),"—","previous report must never be relabelled as another period");
  await responseJson(heldUsage,{days:30,upload_bytes:null,download_bytes:null,coverage:"unavailable",measured_since:null,last_sample_at:null,devices:[]});
  await page.getByText("Сбор данных пока недоступен").waitFor();
  await page.getByRole("heading",{name:notice.title}).waitFor({state:"hidden",timeout:4000});
  await page.locator('.portal-nav a[href="#account"]').click();
  const toggle=page.getByRole("checkbox");await toggle.waitFor();assert.equal(await toggle.isChecked(),true);
  await toggle.click();await page.getByText("Настройка сохранена").waitFor();
  assert.deepEqual(writes,[{expiry_reminders:false}]);
  assert.ok((await page.locator("#account").innerText()).includes("пока отключены"));
  await page.locator('.portal-nav a[href="#help"]').click();
  await page.locator('.portal-nav a[href="#account"]').click();
  await toggle.waitFor();assert.equal(await toggle.isChecked(),false);
  await context.close();
}

async function verifyFullPortal(browser, origin) {
  const { context, page, diagnostics } = await newPortalPage(browser, { removeClipboard: true });
  const calls = [];
  let renameCalls = 0, connectionCalls = 0, cacheAtFirstRequest;
  await installApi(page, async (route, pathname, request) => {
    calls.push(request.method() + " " + pathname);
    cacheAtFirstRequest ??= await page.evaluate(() => ({ telegram: sessionStorage.getItem("__telegram__initParams"), unrelated: sessionStorage.getItem("unrelated") }));
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/v2/devices") && request.method() === "GET") return responseJson(route, profiles);
    if (pathname.endsWith("/21/connection")) {
      connectionCalls++;
      return connectionCalls <= 2 ? responseJson(route, { subscription_url: "https://vpn.example/subscription/full-secret-" + connectionCalls }) : responseJson(route, {}, 404);
    }
    if (pathname.endsWith("/v2/devices/21") && request.method() === "PATCH") {
      renameCalls++;
      return renameCalls === 1 ? responseJson(route, { ...profiles[0], display_name: "Личный iPhone" }) : responseJson(route, {}, 500);
    }
    return responseJson(route, {}, 404);
  });
  await page.goto(origin + "/cabinet/#subscription");
  await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
  assert.deepEqual(calls.slice(0, 2), ["GET /api/vpn-portal/config", "GET /api/vpn-portal/me"]);
  assert.equal(calls.some(call => call.includes("auth/mini-app")), false);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppData, undefined);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppThemeParams, '{"bg_color":"#fff"}');
  assert.equal(cacheAtFirstRequest.unrelated, "preserved");
  assert.equal(await page.locator(".portal-nav a").count(), 4);
  assert.equal(/подключено|защищено/i.test(await page.locator(".cabinet-overview").innerText()), false);
  await page.getByText("Данные появятся после измерения").first().waitFor();
  assert.equal(/(^|\s)0 ГБ(?:\s|$)/m.test(await page.locator("body").innerText()), false);
  await page.getByRole("button", { name: "Скопировать", exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Ссылка скопирована" }).waitFor();
  assert.deepEqual(await page.evaluate(() => window.__portalLegacyClipboardWrites), ["https://vpn.example/subscription/full-secret-1"]);
  await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/full-secret-1");
  await page.getByRole("link", { name: "Инструкция и помощь" }).click();
  await page.getByRole("heading", { name: "Как подключиться" }).waitFor();
  const platform = page.getByLabel("Ваша платформа");
  assert.equal(await platform.locator("option").count(), 5);
  for (const [id, download] of [
    ["iphone", "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru"],
    ["android", "https://play.google.com/store/apps/details?id=com.happproxy"],
    ...["windows", "macos", "linux"].map(id => [id, "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1"]),
  ]) {
    await platform.selectOption(id);
    assert.equal(await page.getByRole("link", { name: id === "iphone" || id === "android" ? "Скачать Happ" : "Скачать Hiddify", exact: true }).getAttribute("href"), download);
  }
  assert.equal(await page.locator(".help-scenario").count(), 7);
  assert.equal(await page.getByRole("link", { name: "Написать в поддержку" }).count(), 0);
  assert.equal(await page.locator("#help b").count(), 0);
  assert.ok((await page.locator("#help").innerText()).includes("<b>Это текст, не HTML.</b>"));
  await page.locator('.portal-nav a[href="#account"]').click();
  await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
  await page.getByText(me.display_name, { exact: true }).waitFor();
  await page.getByRole("checkbox").waitFor();
  assert.equal(await page.getByText("Тарифы ещё не опубликованы").count(), 0);
  await page.locator('.portal-nav a[href="#devices"]').click();
  const first = page.locator(".profile-card").first();
  await first.getByRole("button", { name: "Показать ссылку", exact: true }).click();
  const uri = first.locator("textarea.connection-uri");
  await uri.waitFor();
  assert.equal(await uri.inputValue(), "https://vpn.example/subscription/full-secret-2");
  await first.getByRole("button", { name: "Показать QR-код" }).click();
  assert.match(await first.getByRole("img", { name: /QR-код для подключения/ }).getAttribute("src"), /^data:image\/svg\+xml/);
  await first.getByRole("button", { name: "Скрыть QR-код" }).click();
  assert.equal(await first.locator(".qr-code").count(), 0);
  await first.getByRole("button", { name: "Скопировать", exact: true }).click();
  await first.getByRole("status").filter({ hasText: "Ссылка скопирована" }).waitFor();
  assert.equal((await page.evaluate(() => window.__portalLegacyClipboardWrites)).length, 2);
  await first.getByRole("button", { name: "Переименовать", exact: true }).click();
  await first.getByLabel("Название профиля").fill("Личный iPhone");
  await first.getByRole("button", { name: "Сохранить", exact: true }).click();
  await first.getByRole("heading", { name: "Личный iPhone", exact: true }).waitFor();
  assert.equal(await first.locator("textarea").count(), 0);
  await first.getByRole("button", { name: "Показать ссылку", exact: true }).click();
  await first.getByRole("alert").filter({ hasText: "Не удалось выполнить запрос." }).waitFor();
  await first.getByRole("button", { name: "Переименовать", exact: true }).click();
  await first.getByLabel("Название профиля").fill("Ошибка");
  await first.getByRole("button", { name: "Сохранить", exact: true }).click();
  await first.getByText("Не удалось выполнить запрос.").last().waitFor();
  await assertNoHorizontalOverflow(page);
  await context.close();
}

async function verifyInitialPortalStates(browser, origin) {
  {
    const { context, page } = await newPortalPage(browser);
    let pendingSubscriptions;
    let pendingProfiles;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/subscriptions")) { pendingSubscriptions = route; return; }
      if (pathname.endsWith("/v2/devices")) { pendingProfiles = route; return; }
      return responseJson(route, {}, 404);
    });

    await page.goto(`${origin}/cabinet/`);
    const loadingStatus = page.getByRole("status").filter({ hasText: "Загружаем данные" });
    await loadingStatus.getByRole("heading", { name: "Загружаем данные" }).waitFor();
    assert.equal(await loadingStatus.count(), 1, "initial private-data loading must have one polite announcement");
    assert.equal(await page.getByText("Нет доступа", { exact: true }).count(), 0);
    assert.equal(await page.getByRole("heading", { name: "VPN‑профиля пока нет" }).count(), 0);
    assert.equal(await page.getByRole("button", { name: "Получить 7 дней" }).count(), 0);
    assert.ok(pendingSubscriptions);
    assert.ok(pendingProfiles);
    await responseJson(pendingSubscriptions, subscriptions.slice(0, 1));
    await responseJson(pendingProfiles, profiles.slice(0, 1));
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let failData = true;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices")) {
        return failData ? responseJson(route, {}, 500) : responseJson(route, []);
      }
      return responseJson(route, {}, 404);
    });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Данные недоступны" }).waitFor();
    assert.equal(await page.getByRole("heading", { name: "VPN‑профиля пока нет" }).count(), 0);
    assert.equal(await page.getByText("Нет доступа", { exact: true }).count(), 0);
    failData = false;
    await page.getByRole("button", { name: "Повторить" }).click();
    await page.getByRole("heading", { name: "Пробный доступ недоступен" }).waitFor();
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let firstLoad = true;
    let pendingProfiles;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/subscriptions")) {
        return firstLoad ? responseJson(route, {}, 503) : responseJson(route, subscriptions.slice(0, 1));
      }
      if (pathname.endsWith("/v2/devices")) {
        if (firstLoad) {
          firstLoad = false;
          return responseJson(route, {}, 503);
        }
        pendingProfiles = route;
        return;
      }
      return responseJson(route, {}, 404);
    });

    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("button", { name: "Повторить", exact: true }).click();
    const profilesLoading = page.getByRole("status").filter({ hasText: "Загружаем профили…" });
    await profilesLoading.waitFor();
    assert.equal(await profilesLoading.count(), 1, "profile reload must have one polite announcement");
    assert.ok(pendingProfiles);
    await responseJson(pendingProfiles, profiles.slice(0, 1));
    await page.locator(".profile-card").getByRole("heading", { name: profiles[0].display_name, exact: true }).waitFor();
    await context.close();
  }
}

async function verifyLegacyPlansRoute(browser, origin) {
  const { context, page } = await newPortalPage(browser);
  await installApi(page, async (route, pathname) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/plans")) {
      return responseJson(route, [{
        id: 71,
        name: "Базовый",
        description: "Для одного устройства",
        duration_days: 30,
        max_devices: 1,
        traffic_limit_gb: null,
        price_amount: "299.00",
        currency: "RUB",
        is_trial: false,
      }]);
    }
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices")) {
      return responseJson(route, []);
    }
    return responseJson(route, {}, 404);
  }, { trial: { ...disabledTrial, state: "used" } });

  await page.goto(`${origin}/cabinet/#home`);
  await page.getByRole("link", { name: "Посмотреть тарифы" }).click();
  await page.getByRole("heading", { name: "Тарифы", exact: true }).waitFor();
  assert.equal(new URL(page.url()).hash, "#plans");
  assert.equal(await page.locator("#plans").count(), 1);
  assert.equal(await page.locator('.portal-nav a[href="#home"][aria-current="page"]').count(), 1);
  await page.getByRole("heading", { name: "Базовый" }).waitFor();
  await page.waitForFunction(() => {
    const plansSection = document.getElementById("plans");
    return plansSection !== null && plansSection.getBoundingClientRect().top < window.innerHeight;
  });
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
      if (pathname.endsWith("/v2/devices")) {
        return responseJson(route, trialReads >= 3 ? [trialProfile] : []);
      }
      return responseJson(route, {}, 404);
    }, { trial: null });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Получить 7 дней" }).click();
    await page.getByRole("heading", { name: "Готовим VPN‑профиль" }).waitFor();
    await page.locator(".trial-card").getByText("Это может занять несколько минут", { exact: true }).waitFor();
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor({ timeout: 5_000 });
    assert.equal(activationCalls, 1);
    assert.equal(trialReads, 3);
    await page.locator('.portal-nav a[href="#devices"]').click();
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
      if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices")) {
        return responseJson(route, []);
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    }, { trial: null });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Получить 7 дней" }).click();
    await page.getByRole("heading", { name: "Готовим VPN‑профиль" }).waitFor();
    await page.waitForFunction(() => document.body.innerText.includes("Готовим VPN‑профиль"));
    await page.locator('.portal-nav a[href="#account"]').click();
    assert.ok(pendingTrial);
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await responseJson(pendingTrial, { ...disabledTrial, state: "active" });
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole("heading", { name: "Доступ активен" }).count(), 0);
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
      if (pathname.endsWith("/v2/devices")) return responseJson(route, []);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("heading", { name: "Устройства", exact: true }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    await page.getByText("Bob", { exact: true }).waitFor();
    assert.deepEqual(calls.slice(0, 3), [
      "/api/vpn-portal/config",
      "/api/vpn-portal/auth/mini-app",
      "/api/vpn-portal/me",
    ]);
    assert.equal(page.url().includes("tgWebApp"), false);
    await page.locator('.portal-nav a[href="#devices"]').click();
    await page.getByRole("heading", { name: "Устройства", exact: true }).waitFor();
    calls.length = 0;
    await page.reload();
    await page.getByRole("heading", { name: "Устройства", exact: true }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    await page.getByText("Bob", { exact: true }).waitFor();
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

async function verifyIOSGestureBoundCopy(browser, origin) {
  const { context, page, diagnostics } = await newPortalPage(browser, {
    platform: "ios",
    strictGestureCopy: true,
  });
  await installApi(page, async (route, pathname) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
    if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
    if (pathname.endsWith("/connection")) return responseJson(route, { subscription_url: "https://vpn.example/subscription/ios-gesture-secret" });
    return responseJson(route, {}, 404);
  });
  await page.goto(`${origin}/cabinet/`);
  await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
  await page.getByRole("button", { name: "Скопировать", exact: true, exact: true }).click();
  await page.getByRole("status").filter({ hasText: "Ссылка скопирована" }).waitFor();
  assert.deepEqual(
    await page.evaluate(() => window.__portalLegacyClipboardWrites),
    ["https://vpn.example/subscription/ios-gesture-secret"],
  );
  await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/ios-gesture-secret");
  await context.close();
}

async function verifySessionInvalidation(browser, origin) {
  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, { subscription_url: "https://vpn.example/subscription/quick-copy-secret" });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    await page.getByRole("button", { name: "Скопировать", exact: true }).click();
    const copySuccess = page.getByRole("status").filter({ hasText: "Ссылка скопирована" });
    await copySuccess.getByText("Ссылка скопирована", { exact: true }).waitFor();
    assert.equal(await copySuccess.count(), 1, "home copy success must have one polite announcement");
    assert.deepEqual(
      await page.evaluate(() => window.__portalClipboardWrites),
      ["https://vpn.example/subscription/quick-copy-secret"],
    );
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/quick-copy-secret");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    let pendingConnection;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        pendingConnection = route;
        return;
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    await page.getByRole("button", { name: "Готовим ссылку…" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { subscription_url: "https://vpn.example/subscription/must-never-copy" });
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/must-never-copy");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    let pendingConnection;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Готовим ссылку…" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { subscription_url: "https://vpn.example/subscription/must-not-survive-home-unmount" });
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/must-not-survive-home-unmount");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, {
      captureClipboard: true,
      delayClipboard: true,
    });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, { subscription_url: "https://vpn.example/subscription/delayed-clipboard-secret" });
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Скопировать", exact: true }).click();
    await page.waitForFunction(() => typeof window.__completePortalClipboard === "function");
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await page.evaluate(() => window.__completePortalClipboard());
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), [
      "https://vpn.example/subscription/delayed-clipboard-secret",
    ]);
    assert.equal(await page.getByText("Ссылка скопирована", { exact: true }).count(), 0);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/delayed-clipboard-secret");
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let connectionCalls = 0;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        connectionCalls += 1;
        return connectionCalls === 1
          ? responseJson(route, { subscription_url: "https://vpn.example/subscription/home-prefetch" })
          : responseJson(route, {}, 401);
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    await page.getByText("Анна Ветрова", { exact: true }).waitFor();
    await page.locator('.portal-nav a[href="#devices"]').click();
    await page.getByRole("button", { name: "Показать ссылку" }).click();
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    assert.equal((await page.locator("body").innerText()).includes("Анна Ветрова"), false);
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser);
    let pendingConnection;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        pendingConnection = route;
        return;
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    await page.locator('.portal-nav a[href="#devices"]').click();
    await page.getByRole("button", { name: "Показать ссылку" }).click();
    await page.waitForFunction(() => document.body.innerText.includes("Получаем ссылку"));
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { subscription_url: "https://vpn.example/subscription/must-never-render" });
    await page.waitForTimeout(100);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/must-never-render");
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let logoutCalls = 0;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/logout")) {
        logoutCalls += 1;
        return responseJson(route, {}, 401);
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.locator('.portal-nav a[href="#account"]').click();
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
    const { context, page, diagnostics } = await newPortalPage(browser);
    let pendingConnection;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/v2/devices/21") && request.method() === "PATCH") {
        return responseJson(route, { ...profiles[0], display_name: "После гонки" });
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    await card.getByText("Получаем ссылку…").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("После гонки");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await card.getByRole("heading", { name: "После гонки" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { subscription_url: "https://vpn.example/subscription/obsolete-after-rename" });
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/obsolete-after-rename");
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let pendingRename;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/v2/devices/21") && request.method() === "PATCH") { pendingRename = route; return; }
      if (pathname.endsWith("/connection")) return responseJson(route, { subscription_url: "https://vpn.example/subscription/must-not-start" });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    const card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Новое имя");
    await card.getByRole("button", { name: "Сохранить" }).click();
    const reveal = card.getByRole("button", { name: "Показать ссылку" });
    assert.equal(await reveal.isDisabled(), true);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Новое имя" });
    await card.getByRole("heading", { name: "Новое имя" }).waitFor();
    assert.equal(await reveal.isDisabled(), false);
    await context.close();
  }
}

async function verifyParallelLoadUnauthorized(browser, origin) {
  for (const failingFirst of ["subscriptions", "v2/devices"]) {
    const { context, page } = await newPortalPage(browser);
    let heldRoute;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith(`/${failingFirst}`)) return responseJson(route, {}, 503);
      if (pathname.endsWith(failingFirst === "subscriptions" ? "/v2/devices" : "/subscriptions")) {
        heldRoute = route;
        return;
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Повторить" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
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
      if (pathname.endsWith("/v2/devices")) {
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
    await page.getByText("Данные появятся после измерения").first().waitFor();
    assert.ok(staleProfilesRoute);
    await responseJson(staleProfilesRoute, {}, 401);
    await page.waitForTimeout(100);
    assert.equal(await page.getByRole("heading", { name: "Сессия завершена" }).count(), 0);
    await page.locator('.portal-nav a[href="#account"]').click();
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
      if (pathname.endsWith("/v2/devices") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        connectionCalls += 1;
        return responseJson(route, { subscription_url: `https://vpn.example/subscription/connection-${connectionCalls}` });
      }
      if (pathname.endsWith("/v2/devices/21") && request.method() === "PATCH") {
        pendingRename = route;
        return;
      }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    let card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    await card.locator("textarea").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Имя после");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.locator('.portal-nav a[href="#devices"]').click();
    card = page.locator(".profile-card").first();
    const reveal = card.getByRole("button", { name: "Показать ссылку" });
    assert.equal(await reveal.isDisabled(), true);
    assert.equal(await card.locator("textarea").count(), 0);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Имя после" });
    await card.getByRole("heading", { name: "Имя после" }).waitFor();
    assert.equal(await card.locator("textarea").count(), 0);
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    assert.equal(await card.locator("textarea").inputValue(), "https://vpn.example/subscription/connection-2");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser);
    let pendingConnection;
    let pendingRename;
    await installApi(page, async (route, pathname, request) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/v2/devices") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/v2/devices/21") && request.method() === "PATCH") { pendingRename = route; return; }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    let card = page.locator(".profile-card").first();
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    await card.getByText("Получаем ссылку…").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Ещё имя");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.locator('.portal-nav a[href="#devices"]').click();
    card = page.locator(".profile-card").first();
    assert.ok(pendingConnection);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Ещё имя" });
    await responseJson(pendingConnection, { subscription_url: "https://vpn.example/subscription/obsolete-pending-reveal" });
    await card.getByRole("heading", { name: "Ещё имя" }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await assertSecretAbsent(page, diagnostics, "https://vpn.example/subscription/obsolete-pending-reveal");
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
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices")) return responseJson(route, []);
    return responseJson(route, {}, 404);
  });
  await page.goto(`${origin}/cabinet/`);
  await page.getByRole("heading", {name:"Пробный доступ недоступен",exact:true}).waitFor();
  await page.locator('.portal-nav a[href="#devices"]').click();
  await page.getByText("Устройств пока нет. Добавьте устройство к действующей подписке").waitFor();
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
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/#profiles`);
    await page.getByRole("button", { name: "Показать ссылку" }).click();
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
    { hash: "#tgWebAppData=secret&keep=yes", expectedHash: "#keep=yes", heading: "Доступ активен" },
    { hash: "#/profiles?keep=yes", expectedHash: "#/profiles?keep=yes", heading: "Устройства" },
  ]) {
    const { context, page } = await newPortalPage(browser, { seedCache: false });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/auth/mini-app")) return responseJson(route, me);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles);
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
      if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles.slice(0, 1));
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Доступ активен" }).waitFor();
    const geometry = await page.evaluate(() => {
      const brand = document.querySelector(".cabinet-brand-mark").getBoundingClientRect();
      const account = document.querySelector(".cabinet-account").getBoundingClientRect();
      const shell = getComputedStyle(document.querySelector(".portal-shell"));
      return { brandTop: brand.top, brandLeft: brand.left, accountRight: account.right, shellBottom: parseFloat(shell.paddingBottom) };
    });
    assert.ok(geometry.brandTop >= 72);
    assert.ok(geometry.brandLeft >= 20);
    assert.ok(geometry.accountRight <= 364);
    assert.ok(geometry.shellBottom >= 96);
    await assertNavSafeAreaAndReserve(page, 68);

    await page.evaluate(() => {
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-top", "55px");
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-left", "30px");
    });
    await page.waitForFunction(() => document.querySelector(".cabinet-brand-mark").getBoundingClientRect().left >= 42);
    const shiftedTop = await page.locator(".cabinet-brand-mark").evaluate((element) => element.getBoundingClientRect().top);
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
      const brand = document.querySelector(".state-page .vx-brand").getBoundingClientRect();
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
    const brand = await page.locator(".state-page .vx-brand").evaluate((element) => element.getBoundingClientRect().toJSON());
    assert.ok(brand.top >= 88);
    assert.ok(brand.left >= 40);
    await context.close();
  }
}

async function verifyLongStatusLensStates(browser, origin) {
  const trialBase = {
    duration_days: 7,
    profile_limit: 1,
    subscription_id: null,
    access_key_id: null,
    expires_at: null,
  };
  const fixtures = [
    {
      name: "capacity-paused",
      title: "Выдача доступа временно приостановлена",
      trial: { ...trialBase, state: "capacity_paused" },
      subscriptions: [],
    },
    {
      name: "trial-used",
      title: "Пробный доступ уже использован",
      trial: { ...trialBase, state: "used" },
      subscriptions: [],
    },
    {
      name: "preparing",
      title: "Профиль готовится",
      trial: { ...trialBase, state: "preparing" },
      subscriptions: [],
    },
    {
      name: "expired",
      title: "Срок доступа закончился",
      trial: disabledTrial,
      subscriptions: subscriptions.slice(1),
    },
    {
      name: "empty",
      title: "Пробный доступ недоступен",
      trial: disabledTrial,
      subscriptions: [],
    },
  ];

  for (const width of [320, 390]) {
    for (const fixture of fixtures) {
      const { context, page } = await newPortalPage(browser, {
        viewport: { width, height: 844 },
        colorScheme: "light",
      });
      await installApi(page, async (route, pathname) => {
        if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
        if (pathname.endsWith("/me")) return responseJson(route, me);
        if (pathname.endsWith("/subscriptions")) return responseJson(route, fixture.subscriptions);
        if (pathname.endsWith("/v2/devices")) return responseJson(route, []);
        if (pathname.endsWith("/plans")) return responseJson(route, []);
        return responseJson(route, {}, 404);
      }, { trial: fixture.trial });
      await page.goto(`${origin}/cabinet/#home`);
      await page.getByRole("heading", { name: fixture.title }).waitFor();
      await assertNoHorizontalOverflow(page);
      const geometry = await page.locator(".cabinet-overview").evaluate((lens) => {
        const title = lens.querySelector("h1");
        const detail = lens.querySelector("p");
        if (!title || !detail) throw new Error("status lens copy is incomplete");
        const lensBox = lens.getBoundingClientRect();
        const titleBox = title.getBoundingClientRect();
        const detailBox = detail.getBoundingClientRect();
        return {
          lensTop: lensBox.top,
          lensBottom: lensBox.bottom,
          titleTop: titleBox.top,
          titleBottom: titleBox.bottom,
          detailTop: detailBox.top,
          detailBottom: detailBox.bottom,
          titlePosition: getComputedStyle(title).position,
          detailPosition: getComputedStyle(detail).position,
        };
      });
      assert.notEqual(geometry.titlePosition, "absolute", `${fixture.name} title uses brittle absolute positioning`);
      assert.notEqual(geometry.detailPosition, "absolute", `${fixture.name} detail uses brittle absolute positioning`);
      assert.ok(
        geometry.titleBottom <= geometry.detailTop,
        `${fixture.name} title overlaps detail at ${width}px: ${JSON.stringify(geometry)}`,
      );
      assert.ok(
        geometry.titleTop >= geometry.lensTop && geometry.detailBottom <= geometry.lensBottom,
        `${fixture.name} copy is clipped at ${width}px: ${JSON.stringify(geometry)}`,
      );
      await capture(page, {
        path: path.join(outputRoot, `portal-${fixture.name}-${width}-light.png`),
        fullPage: true,
      });
      await context.close();
    }
  }
}

async function assertHeaderContrast(page) {
  const values=await page.evaluate(()=>{
    const shell=getComputedStyle(document.querySelector(".portal-cabinet"));
    const rgba=value=>{const channels=value.match(/[\d.]+/g)?.map(Number)||[];return channels.length>=3?[...channels.slice(0,3),channels[3]??1]:[0,0,0,1];};
    const overlay=shell.backgroundImage.startsWith("linear-gradient")?shell.backgroundImage.match(/rgba?\([^)]+\)/)?.[0]:null;
    // The atmosphere is a light raster. Check even its worst-case white pixel,
    // composed through the authored dark overlay rather than assuming body color.
    const paint=overlay?rgba(overlay):[255,255,255,0];
    const ground=paint.slice(0,3).map(channel=>channel*paint[3]+255*(1-paint[3]));
    const luminance=rgb=>rgb.map(v=>v/255).map(v=>v<=.04045?v/12.92:((v+.055)/1.055)**2.4).reduce((sum,v,i)=>sum+v*[.2126,.7152,.0722][i],0);
    const background=luminance(ground);
    return [...document.querySelectorAll(".cabinet-brand strong,.cabinet-account")].map(element=>{
      const foreground=luminance(rgba(getComputedStyle(element).color).slice(0,3));
      return {label:element.textContent,contrast:(Math.max(foreground,background)+.05)/(Math.min(foreground,background)+.05),ground};
    });
  });
  assert.ok(values.every(item=>item.contrast>=4.5),"header text/ground contrast: "+JSON.stringify(values));
}

async function assertPlatformLabelFits(page) {
  const value=await page.locator(".cabinet-desktop-platform select").evaluate(select=>{
    const style=getComputedStyle(select),canvas=document.createElement("canvas"),context=canvas.getContext("2d");
    context.font=style.font;
    const label=select.selectedOptions[0]?.textContent||"";
    return {label,width:context.measureText(label).width,available:select.clientWidth-parseFloat(style.paddingLeft)-parseFloat(style.paddingRight)};
  });
  assert.ok(value.available>=value.width,"native platform label is truncated: "+JSON.stringify(value));
}

async function assertShortLabelsStayWhole(page,label) {
  const broken=await page.evaluate(()=>{
    const selectors=[".cabinet-slots > span",".cabinet-connection-actions button",".cabinet-navigation a > span"];
    const result=[];
    for(const element of document.querySelectorAll(selectors.join(","))){
      const walker=document.createTreeWalker(element,NodeFilter.SHOW_TEXT);
      while(walker.nextNode()){
        const node=walker.currentNode,text=node.textContent||"";
        if(!text.trim()||/\s/u.test(text.trim()))continue;
        const start=text.indexOf(text.trim()),range=document.createRange();
        range.setStart(node,start);range.setEnd(node,start+text.trim().length);
        if(new Set([...range.getClientRects()].map(rect=>Math.round(rect.top))).size>1)result.push(text.trim());
      }
    }
    return result;
  });
  assert.deepEqual(broken,[],label+": ordinary short labels split inside a word");
}

async function captureResponsiveMatrix(browser, origin) {
  const installFixture = page => installApi(page, async (route, pathname) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/v2/devices")) return responseJson(route, profiles);
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    if (pathname.endsWith("/connection")) return responseJson(route, {subscription_url:"https://vpn.example/subscription/responsive-fixture"});
    return responseJson(route, {}, 404);
  });
  const scenarios = [
    {name:"portal-320-light",viewport:{width:320,height:700},colorScheme:"light"},
    {name:"portal-390-light",viewport:{width:390,height:844},colorScheme:"light"},
    {name:"portal-390-dark",viewport:{width:390,height:844},colorScheme:"dark"},
    {name:"portal-853-light",viewport:{width:853,height:1844},colorScheme:"light"},
    ...[1024,1280,1440,1600].map(width=>({name:"portal-"+width+"-light",viewport:{width,height:900},colorScheme:"light"})),
  ];
  for (const scenario of scenarios) {
    const {context,page}=await newPortalPage(browser,scenario);
    await installFixture(page); await page.goto(origin+"/cabinet/#home");
    await page.getByRole("heading",{name:"Доступ активен",exact:true}).waitFor();
    const beforeSettlement=process.argv.includes("--trace-settlement")?await page.evaluate(()=>({height:document.documentElement.scrollHeight,prepared:!document.querySelector(".cabinet-connection-actions button")?.disabled,importText:document.querySelector(".cabinet-import")?.textContent})):null;
    await page.waitForFunction(()=>document.querySelector(".cabinet-connection-actions button")?.disabled===false);
    await page.evaluate(async()=>{await document.fonts.ready;await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));});
    if(beforeSettlement)console.log("SETTLEMENT "+scenario.name+" "+JSON.stringify({before:beforeSettlement,after:await page.evaluate(()=>({height:document.documentElement.scrollHeight,prepared:!document.querySelector(".cabinet-connection-actions button")?.disabled,importText:document.querySelector(".cabinet-import")?.textContent}))}));
    await assertNoHorizontalOverflow(page);
    await assertPortalVisualContract(page,scenario.viewport.width);
    await assertAccessibleSubscriptionLayout(page);
    await assertPrimaryActionContrast(page);
    await assertHeaderContrast(page);
    await assertPlatformLabelFits(page);
    await assertNavSafeAreaAndReserve(page);
    await assertWideNavigationGeometry(page,scenario.name);
    if(scenario.viewport.width>=900)await assertWideLayoutUsesNativeFlow(page,scenario.name);
    await assertVisibleInteractiveAccessibility(page,scenario.name,".portal-nav");
    await capture(page,{path:path.join(outputRoot,scenario.name+".png"),fullPage:true});
    await context.close();
  }
  for(const width of [390,1440]){
    for(const [hash,title] of [["devices","Устройства"],["help","Помощь"],["account","Аккаунт"]]){
      const {context,page}=await newPortalPage(browser,{viewport:{width,height:900}});
      await installFixture(page);await page.goto(origin+"/cabinet/#"+hash);
      await page.getByRole("heading",{name:title,exact:true}).waitFor();
      if(hash==="devices")await page.locator(".profile-card").first().waitFor();
      if(hash==="account")await page.waitForFunction(()=>document.querySelector(".reminder-preference input")?.disabled===false);
      await page.waitForLoadState("networkidle");
      await page.evaluate(()=>document.fonts.ready);
      await assertVisibleInteractiveAccessibility(page,"portal-"+hash+"-"+width,".portal-nav");
      await capture(page,{path:path.join(outputRoot,"portal-"+hash+"-"+width+"-light.png"),fullPage:true});
      await context.close();
    }
  }
  for(const width of [390,1440]){
    for(const [hash,title] of [["home","Доступ активен"],["devices","Устройства"],["help","Помощь"],["account","Аккаунт"]]){
      const {context,page}=await newPortalPage(browser,{viewport:{width,height:900},reducedMotion:"reduce"});
      await installFixture(page); await page.goto(origin+"/cabinet/#"+hash);
      await page.getByRole("heading",{name:title,exact:true}).waitFor();
      if(hash==="home")await page.waitForFunction(()=>document.querySelector(".cabinet-connection-actions button")?.disabled===false);
      await page.waitForLoadState("networkidle");
      if(hash==="help")await page.locator(".help-scenario").evaluateAll(items=>items.forEach(item=>{item.open=true;}));
      await assertTwoHundredPercentTextReflow(page,"portal-"+hash+"-"+width+"-text-200",".portal-nav");
      await assertWideNavigationGeometry(page,"portal-"+hash+"-"+width+"-text-200");
      await assertShortLabelsStayWhole(page,"portal-"+hash+"-"+width+"-text-200");
      await assertReducedMotionStopsContinuousDecoration(page);
      await capture(page,{path:path.join(outputRoot,"portal-"+hash+"-"+width+"-text-200.png"),fullPage:true});
      await context.close();
    }
  }
  const ios16=await newPortalPage(browser,{viewport:{width:375,height:667},colorScheme:"dark",userAgent:"Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_12 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148"});
  await installFixture(ios16.page);await ios16.page.goto(origin+"/cabinet/#home");
  await ios16.page.getByRole("heading",{name:"Доступ активен",exact:true}).waitFor();
  const lite=await ios16.page.evaluate(()=>{
    const nav=getComputedStyle(document.querySelector(".cabinet-navigation"));
    return {enabled:document.documentElement.classList.contains("portal-compat-lite"),background:getComputedStyle(document.querySelector(".portal-cabinet")).backgroundImage,raster:getComputedStyle(document.querySelector(".cabinet-brand-mark")).display,vector:getComputedStyle(document.querySelector(".cabinet-flat-mark")).display,blur:nav.backdropFilter||nav.webkitBackdropFilter||"none",transition:getComputedStyle(document.querySelector(".cabinet-import")).transitionDuration};
  });
  assert.deepEqual(lite,{enabled:true,background:"none",raster:"none",vector:"block",blur:"none",transition:"0s"});
  await assertNoHorizontalOverflow(ios16.page);
  await assertVisibleInteractiveAccessibility(ios16.page,"portal-ios16-compat",".portal-nav");
  await capture(ios16.page,{path:path.join(outputRoot,"portal-ios16-compat-dark.png"),fullPage:true});
  await ios16.context.close();
  const failure=await newPortalPage(browser);
  await failure.page.addInitScript(()=>{Intl.DateTimeFormat=class{constructor(){throw new Error("synthetic render failure");}};});
  await installFixture(failure.page);await failure.page.goto(origin+"/cabinet/#home");
  await failure.page.getByRole("heading",{name:"Не удалось отобразить кабинет"}).waitFor();
  await failure.context.close();
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
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices")) return responseJson(route, []);
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

async function verifyTelegramSdkOutageResilience(browser, origin) {
  const context = await browser.newContext({ viewport: { width: 390, height: 844 } });
  const page = await context.newPage();
  const diagnostics = [];
  let exchangedInitData = null;
  page.on("console", (message) => diagnostics.push(message.text()));
  page.on("pageerror", (error) => diagnostics.push(error.message));
  await page.route("https://telegram.org/js/telegram-web-app.js", () => new Promise(() => {}));
  await installApi(page, async (route, pathname, request) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/auth/mini-app")) {
      exchangedInitData = (await request.postDataJSON()).init_data;
      return responseJson(route, { display_name: "Synthetic QA", csrf_token: "csrf-qa" });
    }
    if (pathname.endsWith("/me")) return responseJson(route, { display_name: "Synthetic QA", csrf_token: "csrf-qa" });
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/v2/devices") || pathname.endsWith("/plans")) {
      return responseJson(route, []);
    }
    return responseJson(route, {}, 404);
  });
  const signed = "query_id=synthetic-qa&user=%7B%22id%22%3A1%7D&auth_date=1&hash=synthetic";
  const hash = new URLSearchParams({
    tgWebAppData: signed,
    tgWebAppVersion: "9.1",
    tgWebAppPlatform: "ios",
  });

  await page.goto(`${origin}/cabinet/#${hash}`, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { name: "Пробный доступ недоступен" }).waitFor();
  assert.equal(exchangedInitData, signed);
  assert.equal((await page.url()).includes("tgWebAppData"), false);
  assert.deepEqual(diagnostics, []);
  await context.close();
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
  const locationCases = [verifyInitialDeviceLocationStates, verifyDeviceLocations, verifyDeviceLocationEdges];
  const cabinetCases = [verifyCabinetOperations, verifyHomeCountryPreparation, verifyCountryGenerationScope, verifyCabinetCreation, verifyCabinetPreferencesUsageNotice];
  const allCases = [
    ...cabinetCases, ...locationCases, verifyFullPortal, verifyInitialPortalStates,
    verifyLegacyPlansRoute, verifyPublicTrialFlow, verifyMiniAppStates, verifyIOSGestureBoundCopy,
    verifySessionInvalidation, verifyProfileRequestRaces, verifyParallelLoadUnauthorized,
    verifyRenameAcrossHashRemount, verifyStatePages, verifyPlatformAwareUnauthenticatedStates,
    verifyMountedFragmentPreservation, verifyTelegramSafeAreaGeometry, verifyLongStatusLensStates,
    captureResponsiveMatrix, verifyTelegramSdkOutageResilience,
  ];
  let cases = process.argv.includes("--cabinet-contracts-only") ? cabinetCases
    : process.argv.includes("--country-preparation-only") ? [verifyHomeCountryPreparation]
    : process.argv.includes("--country-generation-only") ? [verifyCountryGenerationScope]
    : process.argv.includes("--device-locations-only") ? locationCases
    : process.argv.includes("--responsive-only") ? [captureResponsiveMatrix]
    : allCases;
  const fromCase=process.argv.find(value=>value.startsWith("--from="))?.slice(7);
  if(fromCase){const first=cases.findIndex(scenario=>scenario.name===fromCase);assert.ok(first>=0,"unknown scenario: "+fromCase);cases=cases.slice(first);}
  for (const scenario of cases) {
    await scenario(browser, server.origin);
    console.log("PASS " + scenario.name);
  }
  if (cases === allCases) {
    console.log("Official SDK cache spot-check: " + await verifyRealSdkCacheCleanup(browser, server.origin));
  }
  console.log("Portal browser QA passed. accessibility=" + JSON.stringify(accessibilityAuditCounts));
} finally {
  await browser.close();
  await server.close();
}
