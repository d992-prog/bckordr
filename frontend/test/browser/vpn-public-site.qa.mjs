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

const accessibilityAuditCounts = {};

async function assertVisibleInteractiveAccessibility(page, label) {
  const initial = await page.evaluate((auditLabel) => {
    const isVisible = (element) => {
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
      element.setAttribute("data-public-a11y-audit-id", `${auditLabel}-${index + 1}`);
    });
    return {
      ids: tabbableControls.map((element) => element.getAttribute("data-public-a11y-audit-id")),
      visibleCount: controls.length,
      cap: Math.max(32, (tabbableControls.length + 1) * 2),
      unnamed: controls.filter((element) => !accessibleName(element)).map((element) => element.outerHTML),
      exposedDecorativeSvg: [...document.querySelectorAll("svg")]
        .filter((element) => isVisible(element) && element.getAttribute("role") !== "img"
          && element.getAttribute("aria-hidden") !== "true")
        .map((element) => element.outerHTML),
    };
  }, label);
  assert.deepEqual(initial.unnamed, [], "every visible button and link must have an accessible name");
  assert.deepEqual(initial.exposedDecorativeSvg, [], "decorative SVGs must be hidden from the accessibility tree");

  await page.evaluate(() => {
    document.body.tabIndex = -1;
    document.body.focus();
    window.scrollTo(0, 0);
  });
  const reached = new Set();
  let firstId = null;
  let cycled = false;
  for (let index = 0; index < initial.cap; index += 1) {
    await page.keyboard.press("Tab");
    const activeId = await page.evaluate(() => document.activeElement?.getAttribute("data-public-a11y-audit-id"));
    if (!activeId) continue;
    if (firstId === null) firstId = activeId;
    else if (activeId === firstId) {
      cycled = true;
      break;
    }
    assert.equal(reached.has(activeId), false, `${label}: ${activeId} repeated before traversal wrapped`);
    await page.waitForFunction((auditId) => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement)
        || element.getAttribute("data-public-a11y-audit-id") !== auditId) return false;
      const box = element.getBoundingClientRect();
      return box.top >= -1 && box.left >= -1
        && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1;
    }, activeId, { timeout: 1000 }).catch(() => {});
    const focus = await page.evaluate((auditId) => {
      const element = document.activeElement;
      if (!(element instanceof HTMLElement)) return { error: "active element is not HTML" };
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        name: (element.getAttribute("aria-label") || element.textContent || "").trim(),
        auditId: element.getAttribute("data-public-a11y-audit-id"),
        tagName: element.tagName,
        withinViewport: box.top >= -1 && box.left >= -1
          && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1,
        visibleFocus: element.matches(":focus-visible")
          && ((style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2) || style.boxShadow !== "none"),
      };
    }, activeId);
    assert.equal(focus.error, undefined, `keyboard traversal ${index + 1}: ${focus.error}`);
    assert.equal(focus.auditId, activeId, `${label}: focus changed before it was audited`);
    assert.equal(focus.withinViewport, true, `focused ${focus.tagName} "${focus.name}" is outside the viewport`);
    assert.equal(focus.visibleFocus, true, `focused ${focus.tagName} "${focus.name}" has no visible focus indication`);
    reached.add(activeId);
  }
  assert.equal(cycled, true, `${label}: keyboard traversal did not wrap within ${initial.cap} steps`);
  assert.deepEqual([...reached].sort(), [...initial.ids].sort(), `${label}: not every visible tabbable was reached`);
  accessibilityAuditCounts[label] = { visible: initial.visibleCount, tabbable: initial.ids.length };
  await page.evaluate(() => document.querySelectorAll("[data-public-a11y-audit-id]").forEach(
    (element) => element.removeAttribute("data-public-a11y-audit-id"),
  ));
}

async function applyTwoHundredPercentTextScaling(page) {
  // Deterministic browser text-only zoom: snapshot computed sizes before applying 2× inline values.
  await page.evaluate(() => {
    const isVisible = (element) => {
      const style = getComputedStyle(element);
      const box = element.getBoundingClientRect();
      return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0;
    };
    const values = [document.body, ...document.body.querySelectorAll("*")]
      .filter((element) => element instanceof HTMLElement && isVisible(element))
      .map((element) => {
        const style = getComputedStyle(element);
        return { element, fontSize: parseFloat(style.fontSize), lineHeight: parseFloat(style.lineHeight) };
      });
    values.forEach(({ element, fontSize, lineHeight }) => {
      if (Number.isFinite(fontSize)) element.style.setProperty("font-size", `${fontSize * 2}px`, "important");
      if (Number.isFinite(lineHeight)) element.style.setProperty("line-height", `${lineHeight * 2}px`, "important");
    });
  });
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
}

async function assertTwoHundredPercentTextReflow(page, label) {
  await applyTwoHundredPercentTextScaling(page);
  await assertNoHorizontalOverflow(page);
  const result = await page.evaluate(() => {
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
        const style = getComputedStyle(ancestor);
        if (["auto", "scroll"].includes(style.overflowX)
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
    const overlapCandidates = elements.filter((element) => hasDirectText(element)
      || element.matches("input, textarea, select, button, a"));
    const overlaps = [];
    for (let leftIndex = 0; leftIndex < overlapCandidates.length; leftIndex += 1) {
      const left = overlapCandidates[leftIndex];
      const leftBox = left.getBoundingClientRect();
      for (let rightIndex = leftIndex + 1; rightIndex < overlapCandidates.length; rightIndex += 1) {
        const right = overlapCandidates[rightIndex];
        if (left.contains(right) || right.contains(left)) continue;
        if (left.parentElement !== right.parentElement
          && !(left.matches("input, textarea, select, button, a") && right.matches("input, textarea, select, button, a"))) continue;
        const rightBox = right.getBoundingClientRect();
        if (Math.min(leftBox.right, rightBox.right) - Math.max(leftBox.left, rightBox.left) > 1
          && Math.min(leftBox.bottom, rightBox.bottom) - Math.max(leftBox.top, rightBox.top) > 1) {
          overlaps.push(`${left.tagName}:${left.textContent?.trim().slice(0, 32)} <> ${right.tagName}:${right.textContent?.trim().slice(0, 32)}`);
        }
      }
    }
    return { clipped, outsidePage, overlaps };
  });
  assert.deepEqual(result.clipped, [], `${label}: public-site text/control clipping at 200%: ${result.clipped.join(" | ")}`);
  assert.deepEqual(result.outsidePage, [], `${label}: public-site page overflow at 200%: ${result.outsidePage.join(" | ")}`);
  assert.deepEqual(result.overlaps, [], `${label}: public-site overlapping text/control boxes at 200%: ${result.overlaps.join(" | ")}`);
  await assertVisibleInteractiveAccessibility(page, `${label}-keyboard`);
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
    await assertVisibleInteractiveAccessibility(page, "public-desktop");
    await page.setViewportSize({ width: 390, height: 844 });
    await assertNoHorizontalOverflow(page);
    await assertVisibleInteractiveAccessibility(page, "public-mobile");
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

async function verifyTextScaling(browser, origin) {
  const { context, page, diagnostics, attemptedPaths } = await newPublicPage(browser, {
    viewport: { width: 390, height: 844 },
  });
  try {
    await page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { level: 3, name: "Базовый", exact: true }).waitFor();
    await assertTwoHundredPercentTextReflow(page, "public-mobile-text-200");
    if (outputRoot) await page.screenshot({ path: path.join(outputRoot, "vpn-public-390-text-200.png"), fullPage: true });
    assertOnlyPublicRequests(attemptedPaths);
    assertNoUnexpectedDiagnostics(diagnostics);
  } finally {
    await context.close();
  }
}

async function verifyDarkContrast(browser, origin) {
  const { context, page, diagnostics, attemptedPaths } = await newPublicPage(browser, {
    viewport: { width: 390, height: 844 },
    colorScheme: "dark",
  });
  try {
    await page.goto(`${origin}/vpn/`, { waitUntil: "domcontentloaded" });
    await page.getByRole("heading", { level: 3, name: "Базовый", exact: true }).waitFor();
    const ratios = await page.locator("body").evaluate(() => {
      const parse = (value) => {
        const canvas = document.createElement("canvas");
        canvas.width = 1;
        canvas.height = 1;
        const context = canvas.getContext("2d", { willReadFrequently: true });
        context.clearRect(0, 0, 1, 1);
        context.fillStyle = value;
        context.fillRect(0, 0, 1, 1);
        return [...context.getImageData(0, 0, 1, 1).data];
      };
      const opaqueBackground = (element) => {
        for (let current = element; current; current = current.parentElement) {
          const rgba = parse(getComputedStyle(current).backgroundColor);
          if (rgba[3] === 255) return rgba;
        }
        return parse(getComputedStyle(document.documentElement).backgroundColor);
      };
      const luminance = (rgb) => {
        const channels = rgb.slice(0, 3).map((value) => {
          const channel = value / 255;
          return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
        });
        return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
      };
      const ratio = (selector) => {
        const element = document.querySelector(selector);
        if (!(element instanceof HTMLElement)) throw new Error(`missing contrast target: ${selector}`);
        const foreground = luminance(parse(getComputedStyle(element).color));
        const background = luminance(opaqueBackground(element));
        return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
      };
      return {
        heading: ratio("h1"),
        body: ratio(".hero-copy p"),
        link: ratio(".site-nav__cabinet"),
        status: ratio(".eyebrow"),
      };
    });
    for (const [label, ratio] of Object.entries(ratios)) {
      assert.ok(ratio >= 4.5, `dark public ${label} contrast ${ratio.toFixed(2)} must be at least 4.5:1`);
    }
    await assertVisibleInteractiveAccessibility(page, "public-mobile-dark");
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
  await verifyTextScaling(browser, server.origin);
  await verifyDarkContrast(browser, server.origin);
  await verifyColdFragments(browser, server.origin);
  await verifyConfigStates(browser, server.origin);
  await verifyPlanRetry(browser, server.origin);
  console.log(`PASS: public VPN browser QA accessibility=${JSON.stringify(accessibilityAuditCounts)} (success, fragments, API states, payment safety, responsive, dark contrast and 200% text)`);
} finally {
  await browser.close();
  await server.close();
}
