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
    reducedMotion: options.reducedMotion || "no-preference",
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
  await page.addInitScript(({ seedCache, cacheData, removeClipboard, captureClipboard, delayClipboard }) => {
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
    if (captureClipboard) {
      window.__portalClipboardWrites = [];
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
  }, {
    seedCache: options.seedCache ?? true,
    cacheData: initData || "cached-secret",
    removeClipboard: options.removeClipboard ?? false,
    captureClipboard: options.captureClipboard ?? false,
    delayClipboard: options.delayClipboard ?? false,
  });
  return { context, page, diagnostics };
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

const accessibilityAuditCounts = {};

async function assertVisibleInteractiveAccessibility(page, label, fixedNavigationSelector = null) {
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
    window.scrollTo(0, 0);
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
    ".portal-status-lens, .portal-status-lens *, .vx-atmosphere, .vx-atmosphere *",
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
  const layout = await page.locator(".portal-access .card-grid").evaluate((grid) => {
    const gridBox = grid.getBoundingClientRect();
    const style = getComputedStyle(grid);
    const cards = [...grid.querySelectorAll(".subscription-card")].map((card) => {
      const box = card.getBoundingClientRect();
      return {
        left: box.left,
        right: box.right,
        width: box.width,
        height: box.height,
        display: getComputedStyle(card).display,
        visibility: getComputedStyle(card).visibility,
      };
    });
    return {
      cards,
      clientWidth: grid.clientWidth,
      scrollWidth: grid.scrollWidth,
      overflowX: style.overflowX,
      gridLeft: gridBox.left,
      gridRight: gridBox.right,
    };
  });

  assert.ok(layout.cards.length >= 2, "subscription layout fixture must contain two cards");
  assert.ok(
    layout.scrollWidth <= layout.clientWidth + 1,
    `subscriptions require hidden horizontal scrolling: ${JSON.stringify(layout)}`,
  );
  assert.notEqual(layout.overflowX, "scroll");
  assert.notEqual(layout.overflowX, "auto");
  assert.equal(
    layout.cards.every((card) => (
      card.display !== "none"
      && card.visibility !== "hidden"
      && card.width > 0
      && card.height > 0
      && card.left >= layout.gridLeft - 1
      && card.right <= layout.gridRight + 1
    )),
    true,
    `a subscription is not visibly discoverable: ${JSON.stringify(layout)}`,
  );
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
    const lastContent = document.querySelector(".portal-content")?.lastElementChild;
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
    geometry.navHeight >= 68 && geometry.navHeight <= 80,
    `navigation height is ${geometry.navHeight}px at ${geometry.viewportWidth}px wide`,
  );
  assert.ok(geometry.safeBottom >= expectedSafeBottom, `resolved safe bottom is ${geometry.safeBottom}px`);
  assert.ok(
    geometry.navBottomGap >= geometry.safeBottom + 12,
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
  const result = await page.evaluate(() => {
    const transformed = [
      ".portal-header .vx-brand",
      ".portal-header .button--ghost",
      ".portal-status-lens__badge",
      ".portal-status-lens h1",
      ".portal-access .subscription-card .status",
      ".portal-access .subscription-card .facts div:nth-child(2) dd",
      '.portal-nav a[aria-current="page"]',
    ].map((selector) => {
      const element = document.querySelector(selector);
      return { selector, transform: element ? getComputedStyle(element).transform : "missing" };
    });
    const reorderedFact = document.querySelector(".portal-access .subscription-card .facts div:nth-child(2)");
    const content = document.querySelector(".portal-content");
    const actionLineCounts = [...document.querySelectorAll(".portal-actions .button")].map((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return {
        label: element.textContent?.trim() || "",
        lines: new Set([...range.getClientRects()].map(({ top }) => Math.round(top))).size,
      };
    });
    return {
      transformed,
      factOrder: reorderedFact ? getComputedStyle(reorderedFact).order : "missing",
      factGridColumn: reorderedFact ? getComputedStyle(reorderedFact).gridColumnStart : "missing",
      contentZoom: content ? getComputedStyle(content).zoom : "missing",
      actionLineCounts,
    };
  });

  assert.deepEqual(
    result.transformed.filter(({ transform }) => transform !== "none"),
    [],
    `${label}: wide layout uses positional transforms: ${JSON.stringify(result.transformed)}`,
  );
  assert.equal(result.factOrder, "0", `${label}: a data fact is positionally reordered`);
  assert.equal(result.factGridColumn, "auto", `${label}: a data fact is forced across the grid`);
  assert.equal(result.contentZoom, "1", `${label}: content uses layout zoom ${result.contentZoom}`);
  assert.equal(
    result.actionLineCounts.every(({ lines }) => lines <= 2),
    true,
    `${label}: a quick action wraps excessively: ${JSON.stringify(result.actionLineCounts)}`,
  );
  assert.equal(
    result.actionLineCounts.every(({ label: actionLabel, lines }) => /\s/u.test(actionLabel) || lines === 1),
    true,
    `${label}: a one-word quick action splits mid-word: ${JSON.stringify(result.actionLineCounts)}`,
  );
}

async function assertPortalVisualContract(page, viewportWidth) {
  const contract = await page.evaluate(() => {
    const shell = document.querySelector(".portal-shell");
    const content = document.querySelector(".portal-content");
    const lens = document.querySelector(".portal-status-lens");
    const primary = document.querySelector(".portal-primary");
    const actions = document.querySelector(".portal-actions");
    const nav = document.querySelector(".portal-nav");
    const title = lens?.querySelector("h1");
    if (!shell || !content || !lens || !primary || !actions || !nav || !title) {
      throw new Error("portal visual contract is missing a required element");
    }

    const shellStyle = getComputedStyle(shell);
    const contentBox = content.getBoundingClientRect();
    const lensStyle = getComputedStyle(lens);
    const primaryStyle = getComputedStyle(primary);
    const actionsStyle = getComputedStyle(actions);
    const navStyle = getComputedStyle(nav);
    const navBox = nav.getBoundingClientRect();
    const controls = [...document.querySelectorAll(
      ".portal-primary, .portal-actions :is(a, button), .portal-nav a",
    )].map((element) => {
      const box = element.getBoundingClientRect();
      return { label: element.textContent?.trim(), width: box.width, height: box.height };
    });
    const tracked = [...document.querySelectorAll(
      ".portal-header, .portal-content, .portal-status-lens, .portal-actions, .portal-access, .portal-nav",
    )].map((element) => {
      const box = element.getBoundingClientRect();
      return { className: element.className, left: box.left, right: box.right };
    });

    const channel = (value) => {
      const normalized = value / 255;
      return normalized <= 0.04045
        ? normalized / 12.92
        : ((normalized + 0.055) / 1.055) ** 2.4;
    };
    const luminance = (color) => {
      const values = color.match(/[\d.]+/g)?.slice(0, 3).map(Number);
      if (!values || values.length !== 3) return null;
      return 0.2126 * channel(values[0]) + 0.7152 * channel(values[1]) + 0.0722 * channel(values[2]);
    };
    const foreground = luminance(shellStyle.color);
    const background = luminance(shellStyle.backgroundColor);
    const contrast = foreground === null || background === null
      ? 0
      : (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);

    return {
      contentWidth: contentBox.width,
      lensMinHeight: parseFloat(lensStyle.minHeight),
      primaryMinHeight: parseFloat(primaryStyle.minHeight),
      actionsBackground: actionsStyle.backgroundColor,
      navPosition: navStyle.position,
      navHeight: navBox.height,
      navBottomGap: innerHeight - navBox.bottom,
      shellBottomPadding: parseFloat(shellStyle.paddingBottom),
      backdropFilter: navStyle.backdropFilter || navStyle.webkitBackdropFilter || "none",
      titleVisible: title.getBoundingClientRect().height > 0 && getComputedStyle(title).visibility !== "hidden",
      titleClipped: title.scrollWidth > title.clientWidth + 1,
      controls,
      tracked,
      contrast,
    };
  });

  const expectedContentWidth = viewportWidth >= 1200 ? 1120.5 : viewportWidth >= 760 ? 810.5 : 720.5;
  assert.ok(
    contract.contentWidth <= expectedContentWidth,
    `portal content is too wide: ${contract.contentWidth}`,
  );
  assert.ok(contract.lensMinHeight >= 280, `status lens min-height is ${contract.lensMinHeight}`);
  assert.ok(contract.primaryMinHeight >= 64, `primary action min-height is ${contract.primaryMinHeight}`);
  assert.notEqual(contract.actionsBackground, "rgba(0, 0, 0, 0)");
  assert.equal(contract.navPosition, "fixed");
  assert.ok(
    contract.shellBottomPadding >= contract.navHeight + contract.navBottomGap + 8,
    `bottom content padding ${contract.shellBottomPadding} does not clear the ${contract.navHeight}px navigation`,
  );
  if (await page.evaluate(() => CSS.supports("backdrop-filter", "blur(1px)"))) {
    assert.notEqual(contract.backdropFilter, "none");
  }
  assert.ok(contract.contrast >= 7, `shell text contrast is only ${contract.contrast.toFixed(2)}:1`);
  assert.equal(contract.titleVisible, true);
  assert.equal(contract.titleClipped, false);
  assert.equal(
    contract.controls.every(({ width, height }) => width >= 44 && height >= 44),
    true,
    `undersized controls: ${JSON.stringify(contract.controls)}`,
  );
  if (viewportWidth === 320) {
    assert.equal(
      contract.tracked.every(({ left, right }) => left >= -0.5 && right <= viewportWidth + 0.5),
      true,
      `clipped 320px layout: ${JSON.stringify(contract.tracked)}`,
    );
  }

  await page.keyboard.press("Tab");
  const focus = await page.evaluate(() => {
    const element = document.activeElement;
    const style = element ? getComputedStyle(element) : null;
    return {
      tagName: element?.tagName,
      visible: element?.matches(":focus-visible") ?? false,
      outlineStyle: style?.outlineStyle,
      outlineWidth: parseFloat(style?.outlineWidth || "0"),
    };
  });
  assert.equal(focus.visible, true, `keyboard focus is not visible on ${focus.tagName}`);
  assert.notEqual(focus.outlineStyle, "none");
  assert.ok(focus.outlineWidth >= 2);
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

async function verifyFullPortal(browser, origin) {
  const { context, page, diagnostics } = await newPortalPage(browser, { removeClipboard: true });
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
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles);
    if (pathname.endsWith("/profiles/21/connection")) {
      connectionCalls += 1;
      return connectionCalls <= 2
        ? responseJson(route, { uri: `vless://full-secret-${connectionCalls}@example.test:443?security=tls&very=long#iPhone` })
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
  await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
  assert.deepEqual(calls.slice(0, 2), ["GET /api/vpn-portal/config", "GET /api/vpn-portal/me"]);
  assert.equal(calls.some((call) => call.includes("auth/mini-app")), false);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppData, undefined);
  assert.equal(JSON.parse(cacheAtFirstRequest.telegram).tgWebAppThemeParams, "{\"bg_color\":\"#fff\"}");
  assert.equal(cacheAtFirstRequest.unrelated, "preserved");
  assert.equal(await page.locator(".portal-nav a").count(), 3);
  assert.equal(await page.getByRole("link", { name: "Открыть профиль" }).count(), 1);
  assert.equal(await page.getByRole("button", { name: "Скопировать ссылку" }).count(), 1);
  assert.equal(await page.getByRole("link", { name: "Инструкция" }).count(), 1);
  assert.equal(await page.locator(".portal-status-lens").count(), 1);
  assert.equal(/подключено|защищено/i.test(await page.locator(".portal-status-lens").innerText()), false);
  assert.equal(await page.locator("main .portal-home").count(), 1);
  assert.equal(await page.locator("#home").count(), 1);
  await page.getByText("Статистика пока недоступна").first().waitFor();
  await page.getByText("Дата начала не указана").waitFor();
  assert.equal(/(^|\s)0 ГБ(?:\s|$)/m.test(await page.locator("body").innerText()), false);
  await assertNoHorizontalOverflow(page);

  await page.getByRole("button", { name: "Скопировать ссылку" }).click();
  const homeCopyError = page.getByRole("alert").filter({ hasText: "Не удалось скопировать автоматически." });
  await homeCopyError.waitFor();
  assert.equal(await homeCopyError.count(), 1, "home copy failure must have one assertive announcement");
  await assertSecretAbsent(
    page,
    diagnostics,
    "vless://full-secret-1@example.test:443?security=tls&very=long#iPhone",
  );

  await page.getByRole("link", { name: "Инструкция" }).click();
  await page.getByRole("heading", { name: "Как подключиться" }).waitFor();
  assert.equal(await page.locator("main .portal-section").count(), 2);
  assert.equal(await page.locator('.portal-nav a[aria-current="page"][href="#profiles"]').count(), 1);
  await page.locator('.portal-nav a[href="#account"]').click();
  await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
  await page.getByText(me.display_name).waitFor();
  await page.getByRole("button", { name: "Выйти" }).waitFor();
  await page.getByText("Тарифы ещё не опубликованы").waitFor();
  await page.getByRole("link", { name: "Помощь" }).click();
  await page.locator("#help").waitFor();
  assert.equal(await page.getByText("<b>Это текст, не HTML.</b>").count(), 1);
  assert.equal(await page.locator("#help b").count(), 0);
  await page.locator('.portal-nav a[href="#profiles"]').click();
  await page.getByRole("heading", { name: "Профили", exact: true }).waitFor();
  await page.getByText("Ссылка пока недоступна. Попробуйте через несколько минут").waitFor();
  await page.getByText(/Veltrix VPN ·/).first().waitFor();

  const firstProfile = page.locator(".profile-card").first();
  assert.equal(await page.locator("textarea.connection-uri").count(), 0);
  await firstProfile.getByRole("button", { name: "Показать ссылку" }).click();
  const uri = firstProfile.locator("textarea.connection-uri");
  await uri.waitFor();
  assert.match(await uri.inputValue(), /^vless:\/\//);
  await firstProfile.getByRole("button", { name: "Показать QR-код" }).click();
  const qrCode = firstProfile.getByRole("img", { name: /QR-код для подключения/ });
  await qrCode.waitFor();
  assert.match(await qrCode.getAttribute("src"), /^data:image\/svg\+xml/);
  await firstProfile.getByRole("button", { name: "Скрыть QR-код" }).click();
  assert.equal(await firstProfile.locator(".qr-code").count(), 0);
  await firstProfile.getByRole("button", { name: "Скопировать" }).click();
  const profileCopyError = firstProfile.getByRole("alert").filter({ hasText: "Не удалось скопировать автоматически." });
  await profileCopyError.waitFor();
  assert.equal(await profileCopyError.count(), 1, "profile copy failure must have one assertive announcement");

  await firstProfile.getByRole("button", { name: "Переименовать" }).click();
  await firstProfile.getByLabel("Название профиля").fill("Личный iPhone");
  await firstProfile.getByRole("button", { name: "Сохранить" }).click();
  await firstProfile.getByRole("heading", { name: "Личный iPhone" }).waitFor();
  assert.equal(await firstProfile.locator("textarea").count(), 0);
  await firstProfile.getByRole("button", { name: "Показать ссылку" }).click();
  await firstProfile.getByRole("alert").filter({ hasText: "Не удалось выполнить запрос." }).waitFor();
  await firstProfile.getByRole("button", { name: "Переименовать" }).click();
  await firstProfile.getByLabel("Название профиля").fill("Ошибка");
  await firstProfile.getByRole("button", { name: "Сохранить" }).click();
  await firstProfile.getByText("Не удалось выполнить запрос.").waitFor();

  await page.screenshot({ path: path.join(outputRoot, "portal-390-light.png"), fullPage: true });
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
      if (pathname.endsWith("/profiles")) { pendingProfiles = route; return; }
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
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await context.close();
  }

  {
    const { context, page } = await newPortalPage(browser);
    let failData = true;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/plans")) return responseJson(route, []);
      if (pathname.endsWith("/subscriptions") || pathname.endsWith("/profiles")) {
        return failData ? responseJson(route, {}, 500) : responseJson(route, []);
      }
      return responseJson(route, {}, 404);
    });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "Не удалось загрузить данные" }).waitFor();
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
      if (pathname.endsWith("/profiles")) {
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
    if (pathname.endsWith("/subscriptions") || pathname.endsWith("/profiles")) {
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
      if (pathname.endsWith("/profiles")) {
        return responseJson(route, trialReads >= 3 ? [trialProfile] : []);
      }
      return responseJson(route, {}, 404);
    }, { trial: null });

    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Получить 7 дней" }).click();
    await page.getByRole("heading", { name: "Готовим VPN‑профиль" }).waitFor();
    await page.locator(".trial-card").getByText("Это может занять несколько минут", { exact: true }).waitFor();
    await page.getByRole("heading", { name: "Пробный доступ готов" }).waitFor({ timeout: 5_000 });
    assert.equal(activationCalls, 1);
    assert.equal(trialReads, 3);
    await page.locator(".portal-primary").click();
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
    await page.getByRole("heading", { name: "Готовим VPN‑профиль" }).waitFor();
    await page.waitForFunction(() => document.body.innerText.includes("Готовим VPN‑профиль"));
    await page.locator('.portal-nav a[href="#account"]').click();
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
    await page.getByRole("heading", { name: "Профили", exact: true }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    await page.getByText("Bob", { exact: true }).waitFor();
    assert.deepEqual(calls.slice(0, 3), [
      "/api/vpn-portal/config",
      "/api/vpn-portal/auth/mini-app",
      "/api/vpn-portal/me",
    ]);
    assert.equal(page.url().includes("tgWebApp"), false);
    await page.locator('.portal-nav a[href="#profiles"]').click();
    await page.getByRole("heading", { name: "Профили", exact: true }).waitFor();
    calls.length = 0;
    await page.reload();
    await page.getByRole("heading", { name: "Профили", exact: true }).waitFor();
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

async function verifySessionInvalidation(browser, origin) {
  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, { uri: "vless://quick-copy-secret" });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await page.getByRole("button", { name: "Скопировать ссылку" }).click();
    const copySuccess = page.getByRole("status").filter({ hasText: "Ссылка скопирована" });
    await copySuccess.getByText("Ссылка скопирована", { exact: true }).waitFor();
    assert.equal(await copySuccess.count(), 1, "home copy success must have one polite announcement");
    assert.deepEqual(
      await page.evaluate(() => window.__portalClipboardWrites),
      ["vless://quick-copy-secret"],
    );
    await assertSecretAbsent(page, diagnostics, "vless://quick-copy-secret");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
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
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await page.getByRole("button", { name: "Скопировать ссылку" }).click();
    await page.getByRole("button", { name: "Копируем…" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { uri: "vless://must-never-copy" });
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "vless://must-never-copy");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, {}, 401);
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await page.getByRole("button", { name: "Скопировать ссылку" }).click();
    await page.getByRole("heading", { name: "Сессия завершена" }).waitFor();
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "vless://");
    await context.close();
  }

  {
    const { context, page, diagnostics } = await newPortalPage(browser, { captureClipboard: true });
    let pendingConnection;
    await installApi(page, async (route, pathname) => {
      if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
      if (pathname.endsWith("/me")) return responseJson(route, me);
      if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions.slice(0, 1));
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Скопировать ссылку" }).click();
    await page.getByRole("button", { name: "Копируем…" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { uri: "vless://must-not-survive-home-unmount" });
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), []);
    await assertSecretAbsent(page, diagnostics, "vless://must-not-survive-home-unmount");
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
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) return responseJson(route, { uri: "vless://delayed-clipboard-secret" });
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("button", { name: "Скопировать ссылку" }).click();
    await page.waitForFunction(() => typeof window.__completePortalClipboard === "function");
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    await page.evaluate(() => window.__completePortalClipboard());
    await page.waitForTimeout(100);
    assert.deepEqual(await page.evaluate(() => window.__portalClipboardWrites), [
      "vless://delayed-clipboard-secret",
    ]);
    assert.equal(await page.getByText("Ссылка скопирована", { exact: true }).count(), 0);
    await assertSecretAbsent(page, diagnostics, "vless://delayed-clipboard-secret");
    await context.close();
  }

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
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
    await page.getByText("Анна Ветрова", { exact: true }).waitFor();
    await page.locator('.portal-nav a[href="#profiles"]').click();
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
      if (pathname.endsWith("/profiles")) return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) {
        pendingConnection = route;
        return;
      }
      if (pathname.endsWith("/logout")) return responseJson(route, { logged_out: true });
      return responseJson(route, {}, 404);
    });
    await page.goto(`${origin}/cabinet/`);
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    await page.locator('.portal-nav a[href="#profiles"]').click();
    await page.getByRole("button", { name: "Показать ссылку" }).click();
    await page.waitForFunction(() => document.body.innerText.includes("Получаем ссылку"));
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.getByRole("button", { name: "Выйти", exact: true }).click();
    await page.getByRole("heading", { name: "Вы вышли из аккаунта" }).waitFor();
    assert.ok(pendingConnection);
    await responseJson(pendingConnection, { uri: "vless://must-never-render" });
    await page.waitForTimeout(100);
    await assertSecretAbsent(page, diagnostics, "vless://must-never-render");
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
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") {
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
    await responseJson(pendingConnection, { uri: "vless://obsolete-after-rename" });
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await assertSecretAbsent(page, diagnostics, "vless://obsolete-after-rename");
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
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    await card.locator("textarea").waitFor();
    await card.getByRole("button", { name: "Переименовать" }).click();
    await card.getByLabel("Название профиля").fill("Имя после");
    await card.getByRole("button", { name: "Сохранить" }).click();
    await page.locator('.portal-nav a[href="#account"]').click();
    await page.locator('.portal-nav a[href="#profiles"]').click();
    card = page.locator(".profile-card").first();
    const reveal = card.getByRole("button", { name: "Показать ссылку" });
    assert.equal(await reveal.isDisabled(), true);
    assert.equal(await card.locator("textarea").count(), 0);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Имя после" });
    await card.getByRole("heading", { name: "Имя после" }).waitFor();
    assert.equal(await card.locator("textarea").count(), 0);
    await card.getByRole("button", { name: "Показать ссылку" }).click();
    assert.equal(await card.locator("textarea").inputValue(), "vless://connection-2");
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
      if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles.slice(0, 1));
      if (pathname.endsWith("/connection")) { pendingConnection = route; return; }
      if (pathname.endsWith("/profiles/21") && request.method() === "PATCH") { pendingRename = route; return; }
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
    await page.locator('.portal-nav a[href="#profiles"]').click();
    card = page.locator(".profile-card").first();
    assert.ok(pendingConnection);
    assert.ok(pendingRename);
    await responseJson(pendingRename, { ...profiles[0], display_name: "Ещё имя" });
    await responseJson(pendingConnection, { uri: "vless://obsolete-pending-reveal" });
    await card.getByRole("heading", { name: "Ещё имя" }).waitFor();
    await page.waitForTimeout(100);
    assert.equal(await card.locator("textarea").count(), 0);
    await assertSecretAbsent(page, diagnostics, "vless://obsolete-pending-reveal");
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
  await page.locator('.portal-nav a[href="#profiles"]').click();
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
    { hash: "#tgWebAppData=secret&keep=yes", expectedHash: "#keep=yes", heading: "VPN‑профиль готов" },
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
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    const geometry = await page.evaluate(() => {
      const brand = document.querySelector(".portal-header .vx-brand").getBoundingClientRect();
      const account = document.querySelector(".account").getBoundingClientRect();
      const shell = getComputedStyle(document.querySelector(".portal-shell"));
      return { brandTop: brand.top, brandLeft: brand.left, accountRight: account.right, shellBottom: parseFloat(shell.paddingBottom) };
    });
    assert.ok(geometry.brandTop >= 72);
    assert.ok(geometry.brandLeft >= 40);
    assert.ok(geometry.accountRight <= 344);
    assert.ok(geometry.shellBottom >= 96);
    await assertNavSafeAreaAndReserve(page, 68);

    await page.evaluate(() => {
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-top", "55px");
      document.documentElement.style.setProperty("--tg-content-safe-area-inset-left", "30px");
    });
    await page.waitForFunction(() => document.querySelector(".portal-header .vx-brand").getBoundingClientRect().left >= 62);
    const shiftedTop = await page.locator(".portal-header .vx-brand").evaluate((element) => element.getBoundingClientRect().top);
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
        if (pathname.endsWith("/profiles")) return responseJson(route, []);
        if (pathname.endsWith("/plans")) return responseJson(route, []);
        return responseJson(route, {}, 404);
      }, { trial: fixture.trial });
      await page.goto(`${origin}/cabinet/#home`);
      await page.getByRole("heading", { name: fixture.title }).waitFor();
      await assertNoHorizontalOverflow(page);
      const geometry = await page.locator(".portal-status-lens").evaluate((lens) => {
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
        geometry.titleBottom + 8 <= geometry.detailTop,
        `${fixture.name} title overlaps detail at ${width}px: ${JSON.stringify(geometry)}`,
      );
      assert.ok(
        geometry.titleTop >= geometry.lensTop && geometry.detailBottom <= geometry.lensBottom,
        `${fixture.name} copy is clipped at ${width}px: ${JSON.stringify(geometry)}`,
      );
      await page.screenshot({
        path: path.join(outputRoot, `portal-${fixture.name}-${width}-light.png`),
        fullPage: true,
      });
      await context.close();
    }
  }
}

async function captureResponsiveMatrix(browser, origin) {
  const portalCss = await readFile(path.join(frontendRoot, "src/vpn-portal/portal.css"), "utf8");
  const portalSource = await readFile(path.join(frontendRoot, "src/vpn-portal/Portal.tsx"), "utf8");
  const brandCss = await readFile(path.join(frontendRoot, "src/brand/veltrix-brand.css"), "utf8");
  assert.equal((portalCss.match(/@import\s+["']\.\.\/brand\/veltrix-brand\.css["']/g) || []).length, 1);
  assert.doesNotMatch(portalSource, /import\s+["']\.\.\/brand\/veltrix-brand\.css["']/);
  assert.match(brandCss, /@supports not \(backdrop-filter:\s*blur\(1px\)\)[\s\S]*\.vx-glass/);

  const cabinetHtml = await readFile(path.join(distRoot, "cabinet/index.html"), "utf8");
  const cabinetCssHref = cabinetHtml.match(/<link[^>]+href="([^"]+\.css)"/)?.[1];
  assert.ok(cabinetCssHref, "built cabinet page has no stylesheet");
  const cabinetCssPath = path.join(
    distRoot,
    new URL(cabinetCssHref, "http://cabinet.test").pathname.replace(/^\/+/, ""),
  );
  const cabinetCss = await readFile(cabinetCssPath, "utf8");
  assert.equal(
    (cabinetCss.match(/\.vx-atmosphere\s*\{\s*background:\s*radial-gradient\(circle at 16% 8%/g) || []).length,
    1,
    "Veltrix brand CSS was emitted more than once",
  );

  const installFixture = (page) => installApi(page, async (route, pathname, request) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles);
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    return responseJson(route, {}, 404);
  });

  const scenarios = [
    { name: "portal-390-light", viewport: { width: 390, height: 844 }, colorScheme: "light" },
    { name: "portal-390-dark", viewport: { width: 390, height: 844 }, colorScheme: "dark" },
    { name: "portal-320-light", viewport: { width: 320, height: 700 }, colorScheme: "light" },
    { name: "portal-853-light", viewport: { width: 853, height: 1844 }, colorScheme: "light" },
    { name: "portal-1024-light", viewport: { width: 1024, height: 900 }, colorScheme: "light" },
    { name: "portal-1200-light", viewport: { width: 1200, height: 900 }, colorScheme: "light" },
    { name: "portal-1440-light", viewport: { width: 1440, height: 900 }, colorScheme: "light" },
    { name: "portal-1440-tall-light", viewport: { width: 1440, height: 1200 }, colorScheme: "light" },
  ];

  for (const scenario of scenarios) {
    const { context, page } = await newPortalPage(browser, scenario);
    await installFixture(page);
    await page.goto(`${origin}/cabinet/#home`);
    await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
    assert.equal(/подключено|защищено/i.test(await page.locator(".portal-status-lens").innerText()), false);
    await assertNoHorizontalOverflow(page);
    await assertPortalVisualContract(page, scenario.viewport.width);
    await assertAccessibleSubscriptionLayout(page);
    await assertPrimaryActionContrast(page);
    await assertNavSafeAreaAndReserve(page);
    if (scenario.viewport.width >= 1200) {
      await assertWideNavigationGeometry(page, scenario.name);
      await assertWideLayoutUsesNativeFlow(page, scenario.name);
    }
    await assertVisibleInteractiveAccessibility(page, scenario.name, ".portal-nav");
    await page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
    await page.screenshot({
      path: path.join(outputRoot, `${scenario.name}.png`),
      fullPage: true,
    });
    if (scenario.name === "portal-390-light") {
      await page.screenshot({ path: path.join(outputRoot, "mobile.png"), fullPage: true });
    }
    if (scenario.name === "portal-1440-light") {
      await page.screenshot({ path: path.join(outputRoot, "desktop.png"), fullPage: true });
    }
    await context.close();
  }

  const wideScaled = await newPortalPage(browser, {
    viewport: { width: 1440, height: 900 },
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  await installFixture(wideScaled.page);
  await wideScaled.page.goto(`${origin}/cabinet/#home`);
  await wideScaled.page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
  await assertTwoHundredPercentTextReflow(
    wideScaled.page,
    "portal-home-1440-text-200",
    ".portal-nav",
  );
  await assertWideNavigationGeometry(wideScaled.page, "portal-home-1440-text-200");
  await assertWideLayoutUsesNativeFlow(wideScaled.page, "portal-home-1440-text-200");
  await wideScaled.page.evaluate(() => document.activeElement instanceof HTMLElement && document.activeElement.blur());
  await wideScaled.page.screenshot({
    path: path.join(outputRoot, "portal-home-1440-text-200.png"),
    fullPage: true,
  });
  await wideScaled.context.close();

  const { context, page } = await newPortalPage(browser, {
    viewport: { width: 390, height: 844 },
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  await installFixture(page);
  await page.goto(`${origin}/cabinet/#home`);
  await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
  const reducedMotion = await page.locator(".portal-status-lens").evaluate((element) => ({
    animationDuration: getComputedStyle(element).animationDuration,
    titleVisible: element.querySelector("h1")?.getBoundingClientRect().height > 0,
    scrollBehavior: getComputedStyle(document.documentElement).scrollBehavior,
  }));
  assert.ok(["0.01ms", "0s"].includes(reducedMotion.animationDuration));
  assert.equal(reducedMotion.titleVisible, true);
  assert.equal(reducedMotion.scrollBehavior, "auto");
  await assertReducedMotionStopsContinuousDecoration(page);
  await context.close();

  for (const scaledScenario of [
    { hash: "home", heading: "VPN‑профиль готов", name: "home" },
    { hash: "profiles", heading: "Профили", name: "profiles" },
    { hash: "account", heading: "Аккаунт", name: "account" },
  ]) {
    const scaled = await newPortalPage(browser, {
      viewport: { width: 390, height: 844 },
      colorScheme: "light",
      reducedMotion: "reduce",
    });
    await installFixture(scaled.page);
    await scaled.page.goto(`${origin}/cabinet/#${scaledScenario.hash}`);
    await scaled.page.getByRole("heading", { name: scaledScenario.heading, exact: true }).waitFor();
    await assertTwoHundredPercentTextReflow(
      scaled.page,
      `portal-${scaledScenario.name}-390-text-200`,
      ".portal-nav",
    );
    await scaled.page.screenshot({
      path: path.join(outputRoot, `portal-${scaledScenario.name}-390-text-200.png`),
      fullPage: true,
    });
    await scaled.context.close();
  }

  const legacyPlans = await newPortalPage(browser, {
    viewport: { width: 853, height: 1844 },
    colorScheme: "light",
  });
  await installFixture(legacyPlans.page);
  await legacyPlans.page.goto(`${origin}/cabinet/#plans`);
  await legacyPlans.page.getByRole("heading", { name: "Тарифы", exact: true }).waitFor();
  await assertVisibleInteractiveAccessibility(legacyPlans.page, "portal-plans-853", ".portal-nav");
  await legacyPlans.context.close();

  const states = await newPortalPage(browser, {
    viewport: { width: 390, height: 844 },
    colorScheme: "light",
  });
  await installFixture(states.page);
  await states.page.goto(`${origin}/cabinet/#profiles`);
  await states.page.getByRole("heading", { name: "Профили", exact: true }).waitFor();
  await assertVisibleInteractiveAccessibility(states.page, "portal-profiles-390", ".portal-nav");
  await states.page.screenshot({ path: path.join(outputRoot, "portal-profiles-390-light.png"), fullPage: true });
  await assertHoveredPrimaryActionContrast(states.page);
  await states.page.locator('.portal-nav a[href="#account"]').click();
  await states.page.getByRole("heading", { name: "Аккаунт" }).waitFor();
  await assertVisibleInteractiveAccessibility(states.page, "portal-account-390", ".portal-nav");
  await states.page.screenshot({ path: path.join(outputRoot, "portal-account-390-light.png"), fullPage: true });
  await states.context.close();

  const signedOut = await newPortalPage(browser, {
    viewport: { width: 390, height: 844 },
    colorScheme: "light",
    platform: "unknown",
  });
  await installApi(signedOut.page, async (route, pathname) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, {}, 401);
    return responseJson(route, {}, 404);
  });
  await signedOut.page.goto(`${origin}/cabinet/`);
  await signedOut.page.getByRole("heading", { name: "Войдите в личный кабинет" }).waitFor();
  await signedOut.page.screenshot({ path: path.join(outputRoot, "portal-login-390-light.png"), fullPage: true });
  await signedOut.context.close();

  const failed = await newPortalPage(browser, {
    viewport: { width: 390, height: 844 },
    colorScheme: "dark",
  });
  await installApi(failed.page, (route, pathname) => pathname.endsWith("/config")
    ? responseJson(route, {}, 503)
    : responseJson(route, {}, 500));
  await failed.page.goto(`${origin}/cabinet/`);
  await failed.page.getByRole("heading", { name: "Не удалось открыть кабинет" }).waitFor();
  await failed.page.screenshot({ path: path.join(outputRoot, "portal-error-390-dark.png"), fullPage: true });
  await failed.context.close();

  const hero = await newPortalPage(browser, {
    viewport: { width: 853, height: 1844 },
    colorScheme: "light",
    reducedMotion: "reduce",
  });
  await installFixture(hero.page);
  await hero.page.goto(`${origin}/cabinet/#home`);
  await hero.page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
  await hero.page.getByRole("link", { name: "Veltrix VPN" }).waitFor();
  await hero.page.getByRole("link", { name: "Помощь" }).waitFor();
  await hero.page.evaluate(async () => {
    document.documentElement.style.scrollBehavior = "auto";
    window.scrollTo({ top: 0, behavior: "instant" });
    await document.fonts.ready;
    await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  });
  await hero.page.screenshot({
    path: path.join(outputRoot, "hero-repro.png"),
  });
  await hero.context.close();
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
  await verifyInitialPortalStates(browser, server.origin);
  await verifyLegacyPlansRoute(browser, server.origin);
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
  await verifyLongStatusLensStates(browser, server.origin);
  await captureResponsiveMatrix(browser, server.origin);
  const sdkResult = await verifyRealSdkCacheCleanup(browser, server.origin);
  console.log(`Portal browser QA passed. accessibility=${JSON.stringify(accessibilityAuditCounts)} Screenshots: ${outputRoot}`);
  console.log(`Official SDK cache spot-check: ${sdkResult}`);
} finally {
  await browser.close();
  await server.close();
}
