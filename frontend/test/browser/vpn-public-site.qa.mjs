// Actual public VPN bundle with synthetic anonymous API responses only.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const distRoot = path.join(frontendRoot, "dist");
const outputRoot = process.env.VPN_PUBLIC_QA_OUTPUT_DIR
  ? path.resolve(process.env.VPN_PUBLIC_QA_OUTPUT_DIR)
  : null;
const publicApiPaths = new Set([
  "/api/vpn-portal/config",
  "/api/vpn-portal/plans",
]);

const availableConfig = {
  enabled: true,
  browser_login_enabled: true,
  mini_app_enabled: true,
  login_path: "/api/vpn-portal/auth/telegram/start",
  bot_url: "https://t.me/veltrix_vpn_official_bot",
  support_text: "Напишите нам в официальный Telegram-бот.",
};

const unavailableConfig = { ...availableConfig, bot_url: null };
const publicPlans = [
  {
    id: 1,
    name: "Базовый",
    description: "Для повседневного подключения",
    duration_days: 30,
    traffic_limit_gb: 100,
    max_devices: 2,
    price_amount: 299,
    currency: "RUB",
    is_trial: false,
  },
  {
    id: 2,
    name: "Пробный",
    description: "Проверка подключения",
    duration_days: 7,
    traffic_limit_gb: 10,
    max_devices: 1,
    price_amount: 0,
    currency: "RUB",
    is_trial: true,
  },
];

function mimeType(filePath) {
  if (filePath.endsWith(".html")) return "text/html; charset=utf-8";
  if (filePath.endsWith(".js")) return "text/javascript; charset=utf-8";
  if (filePath.endsWith(".css")) return "text/css; charset=utf-8";
  if (filePath.endsWith(".svg")) return "image/svg+xml";
  if (filePath.endsWith(".webp")) return "image/webp";
  return "application/octet-stream";
}

async function startStaticServer() {
  const server = createServer(async (request, response) => {
    try {
      const requestUrl = new URL(request.url || "/", "http://127.0.0.1");
      const relative = requestUrl.pathname === "/vpn/"
        ? "vpn/index.html"
        : requestUrl.pathname.replace(/^\/+/, "");
      const resolved = path.resolve(distRoot, relative || "index.html");
      if (!resolved.startsWith(`${distRoot}${path.sep}`) && resolved !== path.join(distRoot, "index.html")) {
        response.writeHead(404).end("not found");
        return;
      }
      response.writeHead(200, {
        "content-type": mimeType(resolved),
        "cache-control": "no-store",
      });
      response.end(await readFile(resolved));
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

function responseJson(route, body, status = 200) {
  return route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
}

async function newPublicPage(browser, options = {}) {
  const context = await browser.newContext({
    viewport: options.viewport || { width: 1280, height: 900 },
    colorScheme: options.colorScheme || "light",
    reducedMotion: options.reducedMotion || "reduce",
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  const diagnostics = [];
  const attemptedPaths = [];
  page.on("console", (message) => {
    if (message.type() === "error") diagnostics.push(message.text());
  });
  page.on("pageerror", (error) => diagnostics.push(error.message));
  await page.route("**/api/**", async (route) => {
    const pathname = new URL(route.request().url()).pathname;
    attemptedPaths.push(pathname);
    if (!publicApiPaths.has(pathname) || route.request().method() !== "GET") {
      return responseJson(route, { detail: "Unexpected public-site request" }, 404);
    }
    if (pathname.endsWith("/config")) {
      return options.configHandler
        ? options.configHandler(route)
        : responseJson(route, options.config ?? availableConfig);
    }
    return options.plansHandler
      ? options.plansHandler(route)
      : responseJson(route, options.plans ?? publicPlans);
  });
  return { context, page, diagnostics, attemptedPaths };
}

function assertOnlyPublicRequests(attemptedPaths) {
  assert.ok(attemptedPaths.length >= 2, "the public page must request config and plans");
  assert.equal(attemptedPaths.includes("/api/vpn-portal/config"), true);
  assert.equal(attemptedPaths.includes("/api/vpn-portal/plans"), true);
  assert.equal(
    attemptedPaths.every((pathname) => publicApiPaths.has(pathname)),
    true,
    `private or unknown API requested: ${attemptedPaths.join(", ")}`,
  );
}

function assertNoUnexpectedDiagnostics(diagnostics) {
  assert.deepEqual(
    diagnostics.filter((message) => !message.startsWith("Failed to load resource:")),
    [],
  );
}

async function assertNoHorizontalOverflow(page) {
  const dimensions = await page.evaluate(() => ({
    clientWidth: document.documentElement.clientWidth,
    scrollWidth: document.documentElement.scrollWidth,
  }));
  assert.ok(
    dimensions.scrollWidth <= dimensions.clientWidth,
    `horizontal overflow: ${dimensions.scrollWidth} > ${dimensions.clientWidth}`,
  );
}

async function verifySuccess(browser, origin) {
  const { context, page, diagnostics, attemptedPaths } = await newPublicPage(browser);
  try {
    await page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { level: 1, name: "VPN без сложных настроек", exact: true }).waitFor();
    await page.getByRole("heading", { level: 3, name: "Базовый", exact: true }).waitFor();

    const expectedHeadings = [
      "VPN без сложных настроек",
      "Как подключиться",
      "Поддерживаемые устройства",
      "Тарифы",
      "Пробный доступ",
      "Поддержка",
      "Конфиденциальность",
      "Условия использования",
    ];
    const tops = [];
    for (const name of expectedHeadings) {
      const heading = page.getByRole("heading", { name, exact: true });
      assert.equal(await heading.count(), 1, `missing unique heading: ${name}`);
      tops.push(await heading.evaluate((element) => element.getBoundingClientRect().top + window.scrollY));
    }
    assert.deepEqual(tops, [...tops].sort((left, right) => left - right));

    const botLink = page.getByRole("link", { name: "Открыть в Telegram", exact: true });
    assert.equal(await botLink.getAttribute("href"), availableConfig.bot_url);
    assert.equal(await page.getByRole("link", { name: "Как подключиться", exact: true }).first().getAttribute("href"), "#how");
    assert.deepEqual(
      await page.locator(".device-list li").evaluateAll((items) => items.map((item) => item.innerText.replace(/\s+/g, " ").trim())),
      ["iPhone Happ · iOS", "Windows Hiddify"],
    );

    await page.getByText("Оплата пока не подключена", { exact: true }).waitFor();
    const paidCard = page.locator(".plan-card").filter({ hasText: "Базовый" });
    assert.equal(await paidCard.getByRole("button", { name: "Покупка скоро будет доступна", exact: true }).isDisabled(), true);
    const trialCard = page.locator(".plan-card").filter({ hasText: "Пробный" });
    assert.equal(
      await trialCard.getByRole("link", { name: "Узнать о пробном доступе", exact: true }).getAttribute("href"),
      availableConfig.bot_url,
    );

    await page.getByRole("navigation", { name: "Основная навигация", exact: true }).waitFor();
    await page.getByRole("navigation", { name: "Ссылки в подвале", exact: true }).waitFor();
    assert.equal(await page.locator("main").count(), 1);
    await page.evaluate(() => { if (document.activeElement instanceof HTMLElement) document.activeElement.blur(); });
    await page.keyboard.press("Tab");
    assert.equal(await page.evaluate(() => document.activeElement?.getAttribute("aria-label")), "Veltrix VPN, в начало");
    await assertNoHorizontalOverflow(page);
    await page.setViewportSize({ width: 390, height: 844 });
    await assertNoHorizontalOverflow(page);
    if (outputRoot) await page.screenshot({ path: path.join(outputRoot, "vpn-public-390.png"), fullPage: true });
    await page.setViewportSize({ width: 1440, height: 900 });
    await assertNoHorizontalOverflow(page);
    if (outputRoot) await page.screenshot({ path: path.join(outputRoot, "vpn-public-1440.png"), fullPage: true });

    await page.evaluate(() => { window.location.hash = "#plans"; });
    await page.waitForFunction(() => {
      const target = document.getElementById("plans");
      return target && target.getBoundingClientRect().top >= 0 && target.getBoundingClientRect().top < 96;
    });
    assertOnlyPublicRequests(attemptedPaths);
    assertNoUnexpectedDiagnostics(diagnostics);
  } finally {
    await context.close();
  }
}

async function verifyColdFragments(browser, origin) {
  for (const id of ["plans", "privacy", "terms"]) {
    const { context, page, diagnostics, attemptedPaths } = await newPublicPage(browser, {
      viewport: { width: 390, height: 500 },
      reducedMotion: "reduce",
    });
    try {
      await page.goto(`${origin}/vpn/#${id}`, { waitUntil: "domcontentloaded" });
      await page.getByRole("heading", { level: 3, name: "Базовый", exact: true }).waitFor();
      await page.waitForFunction((targetId) => {
        const target = document.getElementById(targetId);
        if (!target) return false;
        const rect = target.getBoundingClientRect();
        return window.scrollY > 0 && rect.top >= 0 && rect.top < 96;
      }, id);
      assert.equal(await page.evaluate(() => window.location.hash), `#${id}`);
      assertOnlyPublicRequests(attemptedPaths);
      assertNoUnexpectedDiagnostics(diagnostics);
    } finally {
      await context.close();
    }
  }
}

async function verifyConfigStates(browser, origin) {
  let releaseConfig;
  const loading = await newPublicPage(browser, {
    plans: publicPlans,
    configHandler: (route) => new Promise((resolve) => {
      releaseConfig = async () => {
        await responseJson(route, unavailableConfig);
        resolve();
      };
    }),
  });
  try {
    await loading.page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    const status = loading.page.getByRole("status").filter({ hasText: "Загружаем ссылку…" });
    await status.waitFor();
    assert.equal(await status.getAttribute("aria-live"), "polite");
    await releaseConfig();
    const unavailable = loading.page.getByRole("button", { name: "Бот временно недоступен", exact: true });
    await unavailable.first().waitFor();
    assert.ok(await unavailable.count() >= 2);
    assert.equal(await unavailable.first().isDisabled(), true);
    assert.equal(await loading.page.getByRole("link", { name: "Проверить доступность в боте", exact: true }).count(), 0);
    assert.equal(await loading.page.getByRole("link", { name: "Узнать о пробном доступе", exact: true }).count(), 0);
    assertOnlyPublicRequests(loading.attemptedPaths);
    assertNoUnexpectedDiagnostics(loading.diagnostics);
  } finally {
    await loading.context.close();
  }

  let configRequests = 0;
  const retry = await newPublicPage(browser, {
    plans: publicPlans,
    configHandler: (route) => {
      configRequests += 1;
      return configRequests === 1
        ? responseJson(route, { detail: "Synthetic config failure" }, 503)
        : responseJson(route, availableConfig);
    },
  });
  try {
    await retry.page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    const alert = retry.page.getByRole("alert").filter({ hasText: "Не удалось загрузить ссылку" });
    await alert.waitFor();
    await alert.getByRole("button", { name: "Повторить", exact: true }).click();
    await retry.page.getByRole("link", { name: "Открыть в Telegram", exact: true }).waitFor();
    assert.equal(configRequests, 2);
    assertOnlyPublicRequests(retry.attemptedPaths);
    assertNoUnexpectedDiagnostics(retry.diagnostics);
  } finally {
    await retry.context.close();
  }
}

async function verifyPlanRetry(browser, origin) {
  let planRequests = 0;
  const state = await newPublicPage(browser, {
    config: availableConfig,
    plansHandler: (route) => {
      planRequests += 1;
      return planRequests === 1
        ? responseJson(route, { detail: "Synthetic plans failure" }, 503)
        : responseJson(route, publicPlans);
    },
  });
  try {
    await state.page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    const alert = state.page.getByRole("alert").filter({ hasText: "Не удалось загрузить тарифы" });
    await alert.waitFor();
    await alert.getByRole("button", { name: "Повторить", exact: true }).click();
    await state.page.getByRole("heading", { level: 3, name: "Базовый", exact: true }).waitFor();
    assert.equal(planRequests, 2);
    assertOnlyPublicRequests(state.attemptedPaths);
    assertNoUnexpectedDiagnostics(state.diagnostics);
  } finally {
    await state.context.close();
  }
}

if (outputRoot) await mkdir(outputRoot, { recursive: true });
const server = await startStaticServer();
const browser = await chromium.launch({
  headless: true,
  ...(process.env.CHROMIUM_EXECUTABLE_PATH
    ? { executablePath: process.env.CHROMIUM_EXECUTABLE_PATH }
    : {}),
});

try {
  await verifySuccess(browser, server.origin);
  await verifyColdFragments(browser, server.origin);
  await verifyConfigStates(browser, server.origin);
  await verifyPlanRetry(browser, server.origin);
  console.log("PASS: public VPN browser QA (success, fragments, API states, payment safety, responsive and AX basics)");
} finally {
  await browser.close();
  await server.close();
}
