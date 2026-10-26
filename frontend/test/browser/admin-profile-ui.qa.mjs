// Actual admin bundle, synthetic HTTP responses only; no production connection.
// From frontend: npm run build, then node test/browser/admin-profile-ui.qa.mjs.
// Race regression only: node test/browser/admin-profile-ui.qa.mjs --profile-create-only.
// Abuse review only: node test/browser/admin-profile-ui.qa.mjs --abuse-only.
// Invitation regressions only: node test/browser/admin-profile-ui.qa.mjs --invitations-only.
// Publication/device usage: node test/browser/admin-profile-ui.qa.mjs --admin-operations-only.
// Measured traffic: node test/browser/admin-profile-ui.qa.mjs --usage-only.
// Plain-text news: node test/browser/admin-profile-ui.qa.mjs --broadcasts-only.
// Cabinet operator settings: node test/browser/admin-profile-ui.qa.mjs --cabinet-settings-only.
// Uses existing PLAYWRIGHT_MODULE_PATH / CHROMIUM_EXECUTABLE_PATH overrides when needed.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { readFile, mkdir } from "node:fs/promises";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE_PATH || "playwright");
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const dist = path.join(root, "dist");
const stamp = "2026-09-01T00:00:00Z";
const timestamps = { created_at: stamp, updated_at: stamp };
const customers = ["Анна", "Борис"].map((first_name, index) => ({
  id: index + 1, first_name, last_name: null, telegram_user_id: String(10001 + index),
  telegram_username: null, status: "active", notes: null, ...timestamps,
}));
const subscriptions = [
  { id: 11, customer_id: 1 }, { id: 12, customer_id: 2 }, { id: 13, customer_id: 2 },
].map((item) => ({ ...item, plan_id: null, status: "active", starts_at: stamp,
  expires_at: "2030-01-01T00:00:00Z", traffic_limit_gb: null, max_devices: 3,
  notes: null, ...timestamps }));
const prefix = "vless://11111111-1111-4111-8111-111111111111@vpn.example:8443?type=tcp&security=none";
const labelled = (name) => `${prefix}#${encodeURIComponent(`Veltrix VPN · ${name}`)}`;
const fixtureKeys = () => [11, 12, 13].map((subscription_id, index) => ({
  id: 21 + index, subscription_id, worker_id: null, protocol: "vless",
  public_name: `dropcatch-old-test${index}`, display_name: `Телефон ${index + 1}`,
  config_uri: labelled(`Телефон ${index + 1}`), external_uuid: "11111111-1111-4111-8111-111111111111",
  status: "active", issued_at: stamp, expires_at: null, revoked_at: null,
  last_synced_at: stamp, last_error: null, ...timestamps,
}));
const fixtureUsages = () => new Map([11, 12, 13].map((subscription_id) => [subscription_id,
  { subscription_id, devices_used: 1, max_devices: 3 }]));
const worker = {
  id: 41, name: "Frankfurt 1", registrar_slug: "gandi",
  assigned_registrar_account_id: null, api_base_url: null, control_token: null,
  status: "online", is_enabled: true, ip_address: "203.0.113.41", region: "DE",
  notes: null, max_rps: 16, target_rps: 16, current_rps: 0,
  current_capacity_rps: 16, cpu_load: 0, ram_usage_percent: 0, clock_drift_ms: 0,
  runtime_mode: "live", registration_concurrency_multiplier: 1,
  registration_max_concurrency: 1, vpn_role: "primary", vpn_enabled: true,
  vpn_runtime_status: "ready", vpn_public_host: "vpn.example",
  vpn_panel_url: null, vpn_panel_username: null, vpn_inbound_id: 7,
  vpn_inbound_port: 443, vpn_inbound_protocol: "vless", vpn_inbound_transport: "tcp",
  vpn_inbound_security: "reality", vpn_listener_status: "listening",
  vpn_last_checked_at: stamp, vpn_last_error: null, current_domain_count: 0,
  ssh_host: "203.0.113.41", ssh_port: 22, ssh_username: "root", ssh_key_path: null,
  ssh_last_check_status: "ready", ssh_last_check_message: null,
  ssh_last_checked_at: stamp, ssh_access_configured: true, last_seen_at: stamp,
  last_heartbeat_at: stamp, ...timestamps,
};
const replacementWorker = {
  ...worker,
  id: 42,
  name: "Amsterdam 2",
  ip_address: "203.0.113.42",
  region: "NL",
  vpn_public_host: "nl.vpn.example",
};
const endpointCapacity = {
  endpoint_id: 7,
  worker_id: 41,
  label: "Frankfurt Reality",
  status: "ready",
  occupied_profiles: 2,
  max_active_profiles: 20,
  capacity_warning_percent: 80,
  is_published: false,
  public_name: null,
  country_code: null,
};
const json = (route, body, status = 200) => route.fulfill({
  status, contentType: "application/json", body: JSON.stringify(body),
});
const server = createServer(async (request, response) => {
  try {
    const relative = new URL(request.url, "http://localhost").pathname.replace(/^\/+/, "") || "index.html";
    const resolved = path.resolve(dist, relative);
    if (!resolved.startsWith(dist + path.sep)) return response.writeHead(404).end();
    const mime = resolved.endsWith(".js") ? "text/javascript" : resolved.endsWith(".css") ? "text/css" : "text/html";
    response.writeHead(200, { "content-type": `${mime}; charset=utf-8` });
    response.end(await readFile(resolved));
  } catch {
    response.writeHead(404).end();
  }
});

await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
let browser;
try {
  browser = await chromium.launch({
    executablePath: process.env.CHROMIUM_EXECUTABLE_PATH || undefined,
    headless: true,
  });
  const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
  page.setDefaultTimeout(10000);
  const errors = [];
  const consoleMessages = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("console", (message) => consoleMessages.push(message.text()));
  await page.addInitScript(() => {
    window.__copies = [];
    Object.defineProperty(navigator, "clipboard", { configurable: true,
      value: { writeText: async (value) => { window.__copies.push(value); } } });
  });
  let keys = fixtureKeys();
  let deviceUsages = fixtureUsages();
  let version = Date.parse(stamp);
  const nextVersion = () => new Date(version += 1000).toISOString();
  let reloadFailure = false;
  let renameFailure = false;
  let holdReload = false;
  const heldReloads = [];
  const heldRenameIds = new Set();
  const heldRenames = new Map();
  let holdCommittedReply = false;
  let releaseCommittedReply = null;
  const patches = [];
  let workerDeleted = false;
  let workerDeleteCalls = 0;
  let workerDeleteFailure = false;
  let workerRefreshFailures = 0;
  let replacementWorkerAvailable = false;
  let releaseWorkerDelete = null;
  let capacitySaveFailure = false;
  const createRequests = [];
  const heldCreates = [];
  const handleApiRoute = async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
    if (request.method() === "POST" && pathname === "/api/control/vpn/access-keys") {
      const payload = request.postDataJSON();
      assert.deepEqual(Object.keys(payload).sort(), ["display_name", "protocol", "subscription_id", "worker_id"]);
      createRequests.push(payload);
      return new Promise((resolve) => heldCreates.push(async (body, status = 200) => {
        if (status === 200) {
          keys.push(body);
          deviceUsages.get(body.subscription_id).devices_used += 1;
        }
        await json(route, body, status);
        resolve();
      }));
    }
    if (request.method() === "DELETE" && pathname === "/api/control/workers/41") {
      workerDeleteCalls += 1;
      if (workerDeleteFailure) return json(route, { detail: "Нода занята. Повторите позже" }, 503);
      return new Promise((resolve) => {
        releaseWorkerDelete = async () => {
          workerDeleted = true;
          await json(route, { detail: "Нода удалена" });
          resolve();
        };
      });
    }
    if (request.method() === "PATCH" && /^\/api\/control\/vpn\/access-keys\/(22|23)\/display-name$/.test(pathname)) {
      const id = Number(pathname.split("/").at(-2));
      const payload = request.postDataJSON();
      patches.push(payload);
      assert.deepEqual(Object.keys(payload), ["display_name"]);
      if (renameFailure) return json(route, { detail: "Имя отклонено" }, 422);
      const complete = async () => {
        keys = keys.map((key) => key.id === id
          ? { ...key, display_name: payload.display_name, config_uri: labelled(payload.display_name),
            updated_at: nextVersion() } : key);
        const committed = structuredClone(keys.find((key) => key.id === id));
        if (holdCommittedReply) {
          holdCommittedReply = false;
          await new Promise((resolve) => {
            releaseCommittedReply = async () => { await json(route, committed); resolve(); };
          });
        } else {
          await json(route, committed);
        }
      };
      if (heldRenameIds.has(id)) return new Promise((resolve) => heldRenames.set(id,
        async () => { await complete(); resolve(); }));
      return complete();
    }
    if (request.method() === "PATCH" && pathname === "/api/control/vpn/endpoints/7/capacity") {
      const payload = request.postDataJSON();
      if (capacitySaveFailure) return json(route, { detail: "Не удалось сохранить лимиты" }, 503);
      return json(route, { ...endpointCapacity, ...payload });
    }
    if (request.method() !== "GET") throw new Error(`Unexpected mutation: ${request.method()} ${pathname}`);
    if (pathname === "/api/auth/me") return json(route, { has_feature_access: true, user: {
      id: 1, username: "synthetic-admin", role: "owner", status: "active", language: "ru", ...timestamps,
    } });
    if (pathname === "/api/control/overview") return json(route, { checked_at: stamp, capacity: {} });
    if (pathname === "/api/control/vpn/customers") return json(route, customers);
    if (pathname === "/api/control/vpn/subscriptions") return json(route, subscriptions);
    if (/\/api\/control\/vpn\/customers\/\d+\/usage$/.test(pathname)) {
      return json(route, { customer_id: Number(pathname.split("/").at(-2)), days: Number(new URL(request.url()).searchParams.get("days")),
        upload_bytes: null, download_bytes: null, measured_since: null, last_sample_at: null, coverage: "none", devices: [] });
    }
    if (/\/api\/control\/vpn\/subscriptions\/\d+\/device-usage$/.test(pathname)) {
      return json(route, deviceUsages.get(Number(pathname.split("/").at(-2))));
    }
    if (pathname === "/api/control/vpn/access-keys") {
      if (holdReload) {
        const captured = structuredClone(keys);
        return new Promise((resolve) => heldReloads.push(async () => { await json(route, captured); resolve(); }));
      }
      return reloadFailure ? json(route, { detail: "Не удалось обновить список" }, 503) : json(route, keys);
    }
    if (pathname === "/api/control/workers") {
      if (workerDeleted && workerRefreshFailures > 0) {
        workerRefreshFailures -= 1;
        return json(route, { detail: "Список временно недоступен" }, 503);
      }
      return json(route, workerDeleted ? (replacementWorkerAvailable ? [replacementWorker] : []) : [worker]);
    }
    if (pathname === "/api/control/vpn/release-readiness") return json(route, {
      ready: true, checked_at: stamp, release_id: null, checks: [],
    });
    if (pathname === "/api/control/vpn/endpoints/capacity") return json(route, [endpointCapacity]);
    if (["/api/control/vpn/overview", "/api/control/vpn/lifecycle/status",
      "/api/admin/diagnostic-telegram", "/api/control/discovery/runtime-settings",
      "/api/control/zone-scanner/settings"].includes(pathname)) return json(route, {});
    return json(route, []);
  };
  await page.route("**/api/**", handleApiRoute);
  const appUrl = `http://127.0.0.1:${server.address().port}/`;
  const output = path.resolve(root, "../.pytest_cache/admin-profile-ui-qa");
  await mkdir(output, { recursive: true });
  const waitForHash = (expected) => page.waitForFunction(
    (hash) => window.location.hash === hash,
    expected,
  );
  const until = async (condition, message) => {
    const deadline = Date.now() + 10000;
    while (!condition()) {
      assert.ok(Date.now() < deadline, message);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  };
  const contrastRatio = (locator) => locator.evaluate((element) => {
    const readColor = (value) => {
      const canvas = document.createElement("canvas");
      canvas.width = 1;
      canvas.height = 1;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.clearRect(0, 0, 1, 1);
      context.fillStyle = value;
      context.fillRect(0, 0, 1, 1);
      return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
    };
    const luminance = (rgb) => {
      const channels = rgb.map((value) => {
        const channel = value / 255;
        return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
      });
      return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722;
    };
    const computed = getComputedStyle(element);
    const foreground = luminance(readColor(computed.color));
    const background = luminance(readColor(computed.backgroundColor));
    return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05);
  });
  const assertContrast = async (locator, label) => {
    const ratio = await contrastRatio(locator);
    assert.ok(ratio >= 4.5, `${label} contrast ${ratio.toFixed(2)} must be at least 4.5:1`);
    return ratio;
  };
  const assertFocusVisible = async (locator, label) => {
    await locator.focus();
    await page.keyboard.press("Tab");
    await page.keyboard.press("Shift+Tab");
    const focus = await locator.evaluate((element) => {
      const box = element.getBoundingClientRect();
      const style = getComputedStyle(element);
      return {
        active: document.activeElement === element,
        visible: element.matches(":focus-visible")
          && ((style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2) || style.boxShadow !== "none"),
        withinViewport: box.top >= -1 && box.left >= -1
          && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1,
      };
    });
    assert.deepEqual(focus, { active: true, visible: true, withinViewport: true }, `${label} must have visible unobscured focus`);
  };
  const accessibilityAuditCounts = {};
  const assertVisibleVpnInteractiveTraversal = async (targetPage, label) => {
    const audit = await targetPage.locator(".vpn-admin-shell").evaluate((root, auditLabel) => {
      const isVisible = (element) => {
        if (element.checkVisibility && !element.checkVisibility()) return false;
        for (let current=element;current&&root.contains(current);current=current.parentElement) {
          if (current!==element&&current.matches("details:not([open])")&&!current.querySelector(":scope > summary")?.contains(element)) return false;
        }
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
        const wrappingLabel = element.closest("label")?.textContent || "";
        return (element.getAttribute("aria-label") || labelledText || nativeLabels
          || wrappingLabel || element.textContent || element.getAttribute("title") || "").trim();
      };
      const selector = 'a[href], button, input, select, textarea, [contenteditable="true"], [tabindex]';
      const controls = [...root.querySelectorAll(selector)].filter(isVisible);
      const tabbable = controls.filter((element) => element.tabIndex >= 0
        && !("disabled" in element && element.disabled));
      tabbable.forEach((element, index) => {
        element.setAttribute("data-vpn-a11y-audit-id", `${auditLabel}-${index + 1}`);
      });
      const globalTabbableCount = [...document.querySelectorAll(selector)]
        .filter(isVisible)
        .filter((element) => element.tabIndex >= 0 && !("disabled" in element && element.disabled))
        .length;
      return {
        unnamed: controls.filter((element) => !accessibleName(element)).map((element) => element.outerHTML),
        tabbableIds: tabbable.map((element) => element.getAttribute("data-vpn-a11y-audit-id")),
        visibleCount: controls.length,
        deterministicCap: Math.max(32, (globalTabbableCount + 1) * 2),
        exposedDecorativeSvg: [...root.querySelectorAll("svg")]
          .filter((element) => isVisible(element) && element.getAttribute("role") !== "img"
            && element.getAttribute("aria-hidden") !== "true")
          .map((element) => element.outerHTML),
      };
    }, label);
    assert.deepEqual(audit.unnamed, [], `${label}: every visible VPN admin interactive must have an accessible name`);
    assert.deepEqual(audit.exposedDecorativeSvg, [], "decorative VPN admin SVGs must be hidden");
    assert.ok(audit.tabbableIds.length > 0, `${label}: expected visible tabbable controls`);

    await targetPage.evaluate(() => {
      document.body.tabIndex = -1;
      document.body.focus();
      window.scrollTo(0, 0);
      const navigation = document.querySelector(".vpn-admin-nav");
      if (navigation) navigation.scrollLeft = 0;
    });
    const reached = new Set();
    let firstAuditId = null;
    let cycled = false;
    for (let step = 0; step < audit.deterministicCap; step += 1) {
      await targetPage.keyboard.press("Tab");
      const activeAuditId = await targetPage.evaluate(() => document.activeElement?.getAttribute("data-vpn-a11y-audit-id"));
      if (!activeAuditId) continue;
      if (firstAuditId === null) firstAuditId = activeAuditId;
      else if (activeAuditId === firstAuditId) {
        cycled = true;
        break;
      }

      await targetPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => resolve())));
      const focus = await targetPage.evaluate((auditId) => {
        const element = document.querySelector(`[data-vpn-a11y-audit-id="${auditId}"]`);
        if (!(element instanceof HTMLElement)) return { error: "audited element is missing" };
        const box = element.getBoundingClientRect();
        const style = getComputedStyle(element);
        const navigation = document.querySelector(".vpn-admin-nav");
        const navigationBox = navigation?.getBoundingClientRect() || null;
        const obscuredByNavigation = Boolean(navigationBox && !navigation?.contains(element)
          && box.top < navigationBox.bottom && box.bottom > navigationBox.top);
        return {
          error: null,
          name: (element.getAttribute("aria-label") || element.textContent || "").trim(),
          rect: { top: box.top, right: box.right, bottom: box.bottom, left: box.left },
          viewport: { width: innerWidth, height: innerHeight },
          navigation: navigation ? {
            scrollLeft: navigation.scrollLeft,
            clientWidth: navigation.clientWidth,
            scrollWidth: navigation.scrollWidth,
          } : null,
          withinViewport: box.top >= -1 && box.left >= -1
            && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1,
          obscuredByNavigation,
          focusVisibleMatch: element.matches(":focus-visible"),
          outlineStyle: style.outlineStyle,
          outlineWidth: style.outlineWidth,
          boxShadow: style.boxShadow,
          focusVisible: element.matches(":focus-visible")
            && ((style.outlineStyle !== "none" && parseFloat(style.outlineWidth) >= 2) || style.boxShadow !== "none"),
        };
      }, activeAuditId);
      assert.equal(focus.error, null, `${label}: ${focus.error}`);
      assert.equal(focus.withinViewport, true,
        `${label}: ${activeAuditId} "${focus.name}" is outside the viewport: ${JSON.stringify(focus)}`);
      assert.equal(focus.obscuredByNavigation, false,
        `${label}: ${activeAuditId} "${focus.name}" is obscured by sticky navigation`);
      assert.equal(focus.focusVisible, true,
        `${label}: ${activeAuditId} "${focus.name}" has no visible focus: ${JSON.stringify(focus)}`);
      reached.add(activeAuditId);
    }
    assert.equal(cycled, true, `${label}: keyboard traversal did not cycle within ${audit.deterministicCap} steps`);
    assert.deepEqual([...reached].sort(), [...audit.tabbableIds].sort(),
      `${label}: keyboard traversal did not reach every visible tabbable control`);
    accessibilityAuditCounts[label] = {
      visible: audit.visibleCount,
      tabbable: audit.tabbableIds.length,
    };
    await targetPage.evaluate(() => {
      document.querySelectorAll("[data-vpn-a11y-audit-id]").forEach((element) => {
        element.removeAttribute("data-vpn-a11y-audit-id");
      });
      window.scrollTo(0, 0);
      const navigation = document.querySelector(".vpn-admin-nav");
      if (navigation) navigation.scrollLeft = 0;
    });
  };
  const assertAdminTwoHundredPercentTextReflow = async (targetPage, label) => {
    // Deterministic browser text-only zoom: snapshot every rendered computed size, then apply 2× inline values.
    await targetPage.evaluate(() => {
      const root = document.querySelector(".vpn-admin-shell");
      if (!(root instanceof HTMLElement)) throw new Error("missing VPN admin shell");
      const isVisible = (element) => {
        if (element.checkVisibility && !element.checkVisibility()) return false;
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        for (let current = element; current && root.contains(current); current = current.parentElement) {
          if (current !== element && current.matches("details:not([open])") && !current.querySelector(":scope > summary")?.contains(element)) return false;
          const currentStyle = getComputedStyle(current);
          const currentBox = current.getBoundingClientRect();
          const visuallyHidden = currentStyle.position === "absolute"
            && (currentBox.width <= 1 || currentBox.height <= 1)
            && (currentStyle.clip !== "auto" || currentStyle.clipPath !== "none");
          if (currentStyle.display === "none" || currentStyle.visibility === "hidden" || visuallyHidden) return false;
        }
        return box.width > 0 && box.height > 0;
      };
      const probe=document.createElement("details"),summary=document.createElement("summary"),button=document.createElement("button");
      summary.textContent="Visibility probe";button.textContent="Hidden control";button.style.cssText="display:block;width:100px;height:44px";
      probe.append(summary,button);root.append(probe);
      const closed=isVisible(button);probe.open=true;const opened=isVisible(button);probe.remove();
      if(closed||!opened)throw new Error(`native details visibility guard: closed=${closed}, open=${opened}`);
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
      window.scrollTo(0, 0);
    });
    await targetPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const result = await targetPage.locator(".vpn-admin-shell").evaluate((root) => {
      const isVisible = (element) => {
        if (element.checkVisibility && !element.checkVisibility()) return false;
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        for (let current = element; current && root.contains(current); current = current.parentElement) {
          if (current !== element && current.matches("details:not([open])") && !current.querySelector(":scope > summary")?.contains(element)) return false;
          const currentStyle = getComputedStyle(current);
          const currentBox = current.getBoundingClientRect();
          const visuallyHidden = currentStyle.position === "absolute"
            && (currentBox.width <= 1 || currentBox.height <= 1)
            && (currentStyle.clip !== "auto" || currentStyle.clipPath !== "none");
          if (currentStyle.display === "none" || currentStyle.visibility === "hidden" || visuallyHidden) return false;
        }
        return box.width > 0 && box.height > 0;
      };
      const hasDirectText = (element) => [...element.childNodes]
        .some((node) => node.nodeType === Node.TEXT_NODE && node.textContent?.trim());
      const textAndControls = [...root.querySelectorAll("*")]
        .filter((element) => element instanceof HTMLElement && isVisible(element))
        .filter((element) => hasDirectText(element)
          || element.matches("input, textarea, select, button, a, h1, h2, h3, p, span, strong, small, label, dt, dd, th, td"));
      const clipped = textAndControls.filter((element) => {
        const style = getComputedStyle(element);
        const clipsOverflow = ["hidden", "clip"].includes(style.overflowX)
          || ["hidden", "clip"].includes(style.overflowY)
          || element.matches("input, textarea, select, button");
        return clipsOverflow
          && (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1);
      }).map((element) => `${element.tagName}:${element.textContent?.trim().slice(0, 80)}`);
      const isContainedByHorizontalScroller = (element) => {
        for (let ancestor = element.parentElement; ancestor && ancestor !== root.parentElement; ancestor = ancestor.parentElement) {
          const style = getComputedStyle(ancestor);
          if (["auto", "scroll", "hidden", "clip"].includes(style.overflowX)
            && ancestor.scrollWidth > ancestor.clientWidth + 1) return true;
        }
        return false;
      };
      const overflowing = textAndControls
        .map((element) => {
          const style = getComputedStyle(element);
          const box = element.getBoundingClientRect();
          const controlClipped = element.matches("a, button, input, select, textarea")
            && (["hidden", "clip"].includes(style.overflowX) || ["hidden", "clip"].includes(style.overflowY))
            && (element.scrollWidth > element.clientWidth + 1 || element.scrollHeight > element.clientHeight + 1);
          const escapesPage = (box.left < -1 || box.right > document.documentElement.clientWidth + 1)
            && !isContainedByHorizontalScroller(element);
          return {
            tag: element.tagName,
            className: typeof element.className === "string" ? element.className : "",
            text: element.textContent?.trim().slice(0, 60) || "",
            reason: controlClipped ? "control-clipped" : escapesPage ? "outside-page" : null,
          };
        })
        .filter((element) => element.reason !== null);
      const navigation = root.querySelector(".vpn-admin-nav");
      const overlapCandidates = textAndControls.filter((element) => hasDirectText(element)
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
            && !(left.matches("input, textarea, select, button, a")
              && right.matches("input, textarea, select, button, a"))) continue;
          const rightBox = right.getBoundingClientRect();
          if (Math.min(leftBox.right, rightBox.right) - Math.max(leftBox.left, rightBox.left) > 1
            && Math.min(leftBox.bottom, rightBox.bottom) - Math.max(leftBox.top, rightBox.top) > 1) {
            overlaps.push(`${left.tagName}:${left.textContent?.trim().slice(0, 32)} <> ${right.tagName}:${right.textContent?.trim().slice(0, 32)}`);
          }
        }
      }
      const layoutOverflow = [root, ...root.querySelectorAll("*")]
        .filter((element) => element instanceof HTMLElement && isVisible(element))
        .map((element) => {
          const box = element.getBoundingClientRect();
          return {
            tag: element.tagName,
            className: typeof element.className === "string" ? element.className : "",
            left: Math.round(box.left),
            right: Math.round(box.right),
            clientWidth: element.clientWidth,
            scrollWidth: element.scrollWidth,
          };
        })
        .filter((element) => element.left < -1 || element.right > document.documentElement.clientWidth + 1
          || element.scrollWidth > element.clientWidth + 1)
        .slice(0, 24);
      return {
        pageFits: document.documentElement.scrollWidth <= innerWidth,
        rootFits: root.scrollWidth <= root.clientWidth,
        dimensions: {
          viewport: innerWidth,
          pageScrollWidth: document.documentElement.scrollWidth,
          rootClientWidth: root.clientWidth,
          rootScrollWidth: root.scrollWidth,
        },
        overflowing,
        layoutOverflow,
        clipped,
        overlaps,
      };
    });
    assert.equal(result.pageFits, true, `${label}: VPN admin page must not overflow at 200% text size: ${JSON.stringify(result)}`);
    assert.equal(result.rootFits, true, `${label}: VPN admin workspace must not overflow at 200% text size: ${JSON.stringify(result)}`);
    assert.deepEqual(result.overflowing, [],
      `${label}: VPN admin has true page/control overflow at 200% text size: ${JSON.stringify(result.overflowing)}`);
    assert.deepEqual(result.clipped, [], `${label}: VPN admin copy is clipped at 200% text size: ${result.clipped.join(" | ")}`);
    assert.deepEqual(result.overlaps, [], `${label}: VPN admin text/control boxes overlap at 200%: ${result.overlaps.join(" | ")}`);
    await assertVisibleVpnInteractiveTraversal(targetPage, `${label}-keyboard`);
    return result;
  };
  const auditScaledAdminSection = async (section, navigationLabel) => {
    const scaledPage = await browser.newPage({
      viewport: { width: 390, height: 844 },
      reducedMotion: "reduce",
    });
    const scaledErrors = [];
    scaledPage.setDefaultTimeout(10000);
    scaledPage.on("pageerror", (error) => scaledErrors.push(error.message));
    try {
      await scaledPage.route("**/api/**", handleApiRoute);
      await scaledPage.goto(`${appUrl}#vpn/${section}`, { waitUntil: "domcontentloaded" });
      await scaledPage.waitForFunction((label) =>
        [...document.querySelectorAll('nav[aria-label="Разделы управления VPN"] a')]
          .some((link) => link.textContent?.trim() === label && link.getAttribute("aria-current") === "page"),
      navigationLabel);
      const result = await assertAdminTwoHundredPercentTextReflow(
        scaledPage,
        `${section}-390-text-200`,
      );
      assert.deepEqual(result.overflowing, [], `${section}: strict overflow detector must stay empty`);
      await scaledPage.locator(".vpn-admin-shell").screenshot({
        path: path.join(output, `admin-${section}-390-text-200.png`),
        fullPage: true,
      });
      assert.deepEqual(scaledErrors, [], `${section}: no page errors are expected during 200% audit`);
    } finally {
      await scaledPage.close();
    }
  };
  const assertSecretAbsent = async (secret, label) => {
    const exposure = await page.evaluate((candidate) => {
      const elements = [...document.querySelectorAll("*")];
      const attributeValues = elements.flatMap((element) =>
        [...element.attributes].map((attribute) => attribute.value));
      const formValues = [...document.querySelectorAll("input, textarea, select")]
        .map((element) => element.value);
      const urlAndDataValues = elements.flatMap((element) =>
        [...element.attributes]
          .filter((attribute) => /^(href|src|data-)/.test(attribute.name))
          .map((attribute) => attribute.value));
      return {
        outerHTML: document.documentElement.outerHTML.includes(candidate),
        attributes: attributeValues.some((value) => value.includes(candidate)),
        formValues: formValues.some((value) => value.includes(candidate)),
        urlAndData: urlAndDataValues.some((value) => value.includes(candidate)),
      };
    }, secret);
    assert.deepEqual(exposure, {
      outerHTML: false,
      attributes: false,
      formValues: false,
      urlAndData: false,
    }, `${label}: the full URI must stay out of DOM serialization, attributes, and form values`);
    assert.equal(consoleMessages.some((message) => message.includes(secret)), false,
      `${label}: the full URI must stay out of console output`);
  };

  if (process.argv.includes("--visibility-only")) {
    for(const fallback of [false,true]){
      await page.setContent('<style>:focus-visible{outline:3px solid #0b57d0;outline-offset:3px}</style><main class="vpn-admin-shell"><details><summary>Native disclosure</summary><button style="display:block;width:100px;height:44px">Hidden action</button></details><button>Visible action</button></main>');
      if(fallback)await page.evaluate(()=>Object.defineProperty(Element.prototype,"checkVisibility",{value:undefined,configurable:true}));
      await assertAdminTwoHundredPercentTextReflow(page,"native-details-regression-"+(fallback?"fallback":"native"));
    }
    console.log("PASS: closed details excluded and open details included by native rendered-visibility guard");
  } else if (process.argv.includes("--cabinet-settings-only")) {
    const cabinet = await browser.newPage({ viewport: { width: 1280, height: 1000 }, timezoneId: "Europe/Moscow" });
    cabinet.setDefaultTimeout(10000);
    const cabinetErrors = [];
    cabinet.on("pageerror", (error) => cabinetErrors.push(error.message));
    const endpoint = "/api/control/vpn/cabinet-settings";
    let current = { revision: 0, support_contact: null, notice: null, reminders_enabled: false };
    let sessionId = 1;
    let gets = 0;
    let saveFailure = "";
    let holdGet = false;
    let holdPut = false;
    const heldGets = [];
    const heldPuts = [];
    const mutations = [];
    const session = () => ({ has_feature_access: true, user: { id: sessionId, username: `synthetic-admin-${sessionId}`, role: "owner", status: "active", language: "ru", ...timestamps } });
    await cabinet.route("**/api/**", async (route) => {
      const request = route.request();
      const pathname = new URL(request.url()).pathname;
      if (pathname === "/api/auth/me" || pathname === "/api/auth/login") return json(route, session());
      if (pathname === "/api/auth/logout") return json(route, {});
      if (pathname !== endpoint) {
        assert.equal(request.method(), "GET", `settings must never send or create a campaign: ${pathname}`);
        return handleApiRoute(route);
      }
      if (request.method() === "GET") {
        gets += 1;
        if (holdGet) { holdGet = false; heldGets.push(route); return; }
        return json(route, current);
      }
      assert.equal(request.method(), "PUT");
      const payload = request.postDataJSON();
      assert.deepEqual(Object.keys(payload).sort(), ["notice", "reminders_enabled", "revision", "support_contact"]);
      mutations.push(payload);
      if (/(?:\/|@)joinchat$/i.test(payload.support_contact ?? "")) return json(route, { detail: "vpn_cabinet_support_contact_invalid" }, 422);
      if (holdPut) { holdPut = false; heldPuts.push(route); return; }
      if (saveFailure) {
        const failure = saveFailure; saveFailure = "";
        return json(route, { detail: failure }, failure === "vpn_cabinet_settings_revision_conflict" ? 409 : 503);
      }
      assert.equal(payload.revision, current.revision);
      current = { ...payload, revision: payload.revision + 1, support_contact: payload.support_contact?.replace(/^@/, "https://t.me/") ?? null };
      return json(route, current);
    });
    const panel = cabinet.locator(".vpn-cabinet-settings-panel");
    const save = () => panel.getByRole("button", { name: "Сохранить настройки", exact: true });
    const reload = () => panel.getByRole("button", { name: "Отбросить изменения и загрузить настройки", exact: true });
    const active = () => cabinet.locator('.vpn-admin-nav a[href="#vpn/cabinet"]').click();
    const leave = () => cabinet.locator('.vpn-admin-nav a[href="#vpn/plans"]').click();
    const settle = () => cabinet.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const signIn = async () => {
      const form = cabinet.locator("form").filter({ has: cabinet.getByRole("button", { name: "Войти", exact: true }) });
      await form.getByLabel("Логин", { exact: true }).fill("synthetic-admin");
      await form.getByLabel("Пароль", { exact: true }).fill("synthetic-password");
      await cabinet.getByRole("button", { name: "Войти", exact: true }).click();
      await panel.getByLabel("Контакт поддержки", { exact: true }).waitFor();
    };
    try {
      await cabinet.goto(`${appUrl}#vpn/plans`);
      await cabinet.locator('.vpn-admin-nav a[href="#vpn/plans"][aria-current="page"]').waitFor();
      assert.equal(gets, 0, "inactive settings must not load");
      await active();
      await panel.getByLabel("Контакт поддержки", { exact: true }).waitFor({ timeout: 3000 });
      assert.equal(mutations.length, 0, "mount must never save or enable reminders");
      assert.equal(await panel.getByLabel("Напоминания в Telegram", { exact: true }).isChecked(), false);
      await panel.getByLabel("Контакт поддержки", { exact: true }).fill("@support_test");
      await panel.getByLabel("Объявление в кабинете", { exact: true }).check();
      await panel.getByLabel("Заголовок объявления", { exact: true }).fill("<b>Обновление</b>");
      await panel.getByLabel("Текст объявления", { exact: true }).fill("Обычный текст\n😀");
      await panel.getByLabel("Начало показа", { exact: true }).fill("2026-10-26T10:00");
      await panel.getByLabel("Конец показа", { exact: true }).fill("2026-10-26T11:00");
      await panel.getByLabel("Напоминания в Telegram", { exact: true }).check();
      assert.equal(mutations.length, 0, "field edits are not saves");
      for (const contact of ["https://t.me/joinchat", "@JOInchat"]) {
        await panel.getByLabel("Контакт поддержки", { exact: true }).fill(contact);
        await save().click();
        await settle();
        assert.equal(mutations.length, 0, "reserved Telegram actions are rejected before PUT");
        await panel.getByRole("alert").filter({ hasText: "Контакт поддержки" }).waitFor();
        assert.equal(await panel.getByLabel("Контакт поддержки", { exact: true }).isEditable(), true, "local validation must leave the draft editable");
        assert.equal(await panel.getByLabel("Объявление в кабинете", { exact: true }).isChecked(), true, "local validation must preserve the notice switch");
        assert.equal(await panel.locator("#vpn-cabinet-body").inputValue(), "Обычный текст\n😀", "unrelated draft text is retained");
        assert.equal(await panel.locator("#vpn-cabinet-body").isEditable(), true);
        assert.equal(await panel.getByLabel("Напоминания в Telegram", { exact: true }).isChecked(), true);
        assert.equal(await reload().count(), 0, "no discard/recovery flow is required for a local error");
      }
      await panel.getByLabel("Контакт поддержки", { exact: true }).fill("@support_test");
      await save().evaluate((button) => { button.click(); button.click(); });
      await panel.getByRole("status").filter({ hasText: "Настройки сохранены" }).waitFor();
      assert.equal(mutations.length, 1, "same-tick double save is guarded");
      assert.deepEqual(mutations[0], { revision: 0, support_contact: "@support_test", reminders_enabled: true, notice: { title: "<b>Обновление</b>", body: "Обычный текст\n😀", starts_at: "2026-10-26T07:00:00.000Z", ends_at: "2026-10-26T08:00:00.000Z" } });
      assert.equal(await panel.getByLabel("Контакт поддержки", { exact: true }).inputValue(), "https://t.me/support_test", "canonical server values replace the saved draft");
      assert.equal(await panel.getByLabel("Начало показа", { exact: true }).inputValue(), "2026-10-26T10:00", "server UTC is shown in local time");
      assert.equal(await panel.locator("b").count(), 0, "notice is plain text");
      await panel.getByText(/не отправляет сообщение/).waitFor();
      await panel.getByText(/будущих событий/).waitFor();
      for (const width of [1280, 390]) {
        await cabinet.setViewportSize({ width, height: 1000 });
        await panel.scrollIntoViewIfNeeded();
        assert.equal(await cabinet.evaluate(() => document.documentElement.scrollWidth > innerWidth), false, "settings must not overflow horizontally");
        assert.equal(await save().evaluate((button) => button.getBoundingClientRect().height >= 44), true);
        await panel.locator("#vpn-cabinet-support").focus();
        assert.equal(await panel.locator("#vpn-cabinet-support").evaluate((input) => getComputedStyle(input).outlineStyle !== "none"), true);
        await cabinet.evaluate(async () => {
          await document.fonts.ready;
          document.activeElement?.blur();
          window.scrollTo({ top: 0, left: 0, behavior: "instant" });
          await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
        });
        await cabinet.screenshot({ path: path.join(output, `admin-cabinet-settings-${width}.png`), fullPage: true });
      }
      await cabinet.setViewportSize({ width: 1280, height: 1000 });
      await panel.getByLabel("Контакт поддержки", { exact: true }).fill("@unsaved_draft");
      current = { ...current, revision: 9, support_contact: "https://t.me/fresh_server" };
      saveFailure = "vpn_cabinet_settings_revision_conflict";
      const getsBeforeConflict = gets;
      await save().click();
      await panel.getByRole("alert").filter({ hasText: /изменил|изменены/ }).waitFor();
      assert.equal(await panel.getByLabel("Контакт поддержки", { exact: true }).inputValue(), "@unsaved_draft");
      assert.equal(await save().isDisabled(), true, "conflict cannot blind-resave");
      assert.equal(gets, getsBeforeConflict, "conflict does not silently reload or replace the draft");
      await reload().click();
      await cabinet.waitForFunction(() => document.querySelector("#vpn-cabinet-support")?.value === "https://t.me/fresh_server");
      await panel.getByLabel("Контакт поддержки", { exact: true }).fill("");
      await panel.getByLabel("Объявление в кабинете", { exact: true }).uncheck();
      await panel.getByLabel("Напоминания в Telegram", { exact: true }).uncheck();
      saveFailure = "<html>private upstream details</html>";
      await save().click();
      await panel.getByRole("alert").waitFor();
      assert.equal(await save().isDisabled(), true, "uncertain save requires explicit state check");
      assert.equal((await panel.innerText()).includes("private upstream"), false);
      await reload().click();
      await cabinet.waitForFunction(() => document.querySelector("#vpn-cabinet-support")?.value === "https://t.me/fresh_server");
      await panel.getByLabel("Контакт поддержки", { exact: true }).fill("");
      await panel.getByLabel("Объявление в кабинете", { exact: true }).uncheck();
      await panel.getByLabel("Напоминания в Telegram", { exact: true }).uncheck();
      await save().click();
      await panel.getByRole("status").filter({ hasText: "Настройки сохранены" }).waitFor();
      assert.deepEqual(mutations.at(-1), { revision: 9, support_contact: null, notice: null, reminders_enabled: false });

      // Old GET and PUT responses must not cross an unmount or another admin login.
      for (const kind of ["GET", "PUT"]) {
        if (kind === "GET") { await leave(); holdGet = true; await active(); await until(() => heldGets.length === 1, "old GET not held"); }
        else { await panel.getByLabel("Контакт поддержки", { exact: true }).fill("@old_admin"); holdPut = true; await save().click(); await until(() => heldPuts.length === 1, "old PUT not held"); }
        await leave();
        current = { ...current, revision: current.revision + 1, support_contact: "https://t.me/section_current" };
        await active();
        await cabinet.waitForFunction(() => document.querySelector("#vpn-cabinet-support")?.value === "https://t.me/section_current");
        await json((kind === "GET" ? heldGets : heldPuts).shift(), { ...current, support_contact: "https://t.me/stale_reply" });
        await settle();
        assert.equal(await panel.getByLabel("Контакт поддержки", { exact: true }).inputValue(), "https://t.me/section_current");

        if (kind === "GET") { await leave(); holdGet = true; await active(); await until(() => heldGets.length === 1, "session GET not held"); }
        else { holdPut = true; await save().click(); await until(() => heldPuts.length === 1, "session PUT not held"); }
        await cabinet.getByRole("button", { name: "Выйти", exact: true }).click();
        await cabinet.getByRole("heading", { name: "Вход", exact: true }).waitFor();
        sessionId += 1;
        current = { ...current, revision: current.revision + 1, support_contact: "https://t.me/next_admin" };
        await signIn();
        await json((kind === "GET" ? heldGets : heldPuts).shift(), { ...current, support_contact: "https://t.me/old_admin_reply" });
        await settle();
        assert.equal(await panel.getByLabel("Контакт поддержки", { exact: true }).inputValue(), "https://t.me/next_admin");
        assert.equal(await save().isDisabled(), false);
      }
      assert.deepEqual(cabinetErrors, []);
      console.log(`PASS: cabinet settings explicit whole PUT/gate/no sends, no save on mount, local dates, canonical response, duplicate save, 409/unknown recovery, late GET/PUT across sections and admin sessions; captures: ${output}`);
    } finally { await cabinet.close(); }
  } else if (process.argv.includes("--abuse-only")) {
    const review = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    review.setDefaultTimeout(10000);
    const reviewErrors = [];
    const reviewLogs = [];
    review.on("pageerror", (error) => reviewErrors.push(error.message));
    review.on("console", (message) => reviewLogs.push(message.text()));
    const signal = (id, acknowledged_at = null) => ({ id, device_id: id === 1 ? 9001 : 9101,
      customer_id: id === 1 ? 9002 : 9102, display_name: `Логический профиль ${id}`, peak_source_count: 4,
      started_at: "2026-10-25T12:00:00Z", last_observed_at: "2026-10-25T12:04:05Z", acknowledged_at });
    let signals = Array.from({ length: 101 }, (_, index) => signal(index + 1));
    const initialDeviceContext = (deviceId) => {
      const first = deviceId === 9001;
      const customerId = first ? 9002 : 9102;
      const subscriptionId = first ? 9003 : 9103;
      return { device_id: deviceId, customer: { ...customers[0], id: customerId, first_name: first ? "Дальний клиент А" : "Дальний клиент Б" },
        subscription: { ...subscriptions[0], id: subscriptionId, customer_id: customerId },
        access_key: { ...fixtureKeys()[0], id: first ? 9004 : 9104, device_id: deviceId, subscription_id: subscriptionId,
          display_name: first ? "Целевой профиль А" : "Целевой профиль Б" } };
    };
    const contexts = new Map([9001, 9101].map((id) => [id, initialDeviceContext(id)]));
    const deviceContext = (id) => structuredClone(contexts.get(id));
    let contextRevision = 0;
    const revisedAt = () => `2026-10-25T12:20:${String(++contextRevision).padStart(2, "0")}.000001`;
    let holdCustomerWrite = false;
    let holdSubscriptionWrite = false;
    const heldCustomerWrites = [];
    const heldSubscriptionWrites = [];
    let holdList = false;
    let failList = false;
    const heldLists = [];
    const heldAcks = [];
    const heldContexts = [];
    const holdContextIds = new Set();
    const listQueries = [];
    const mutations = [];
    const customerPatches = [];
    const subscriptionCreates = [];
    const subscriptionPatches = [];
    let userId = 1;
    await review.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.pathname === "/api/auth/logout") { mutations.push(url.pathname); return json(route, { detail: "ok" }); }
      if (url.pathname === "/api/auth/login" || url.pathname === "/api/auth/me") {
        return json(route, { has_feature_access: true, user: { id: userId, username: `synthetic-${userId}`, role: "owner", status: "active", language: "ru", ...timestamps } });
      }
      if (request.method() === "PATCH" && /^\/api\/control\/vpn\/customers\/[0-9]+$/.test(url.pathname)) {
        const id = Number(url.pathname.split("/").at(-1));
        const payload = request.postDataJSON();
        customerPatches.push({ id, payload });
        const context = [...contexts.values()].find((item) => item.customer.id === id);
        const saved = { ...(context?.customer ?? customers.find((customer) => customer.id === id)), ...payload, id, updated_at: revisedAt() };
        if (context) context.customer = saved;
        if (holdCustomerWrite) { heldCustomerWrites.push({ route, saved }); return; }
        return json(route, saved);
      }
      if (request.method() === "PATCH" && /^\/api\/control\/vpn\/subscriptions\/[0-9]+$/.test(url.pathname)) {
        const id = Number(url.pathname.split("/").at(-1));
        const payload = request.postDataJSON();
        subscriptionPatches.push({ id, payload });
        const context = [...contexts.values()].find((item) => item.subscription.id === id);
        assert.ok(context, "abuse fixture only permits explicit outside-list subscription mutations");
        const saved = { ...context.subscription, ...payload, updated_at: revisedAt() };
        context.subscription = saved;
        if (holdSubscriptionWrite) { heldSubscriptionWrites.push({ route, saved }); return; }
        return json(route, saved);
      }
      if (request.method() === "POST" && /^\/api\/control\/vpn\/customers\/[0-9]+\/archive$/.test(url.pathname)) {
        const id = Number(url.pathname.split("/").at(-2));
        const context = [...contexts.values()].find((item) => item.customer.id === id);
        assert.ok(context);
        const updated_at = revisedAt();
        context.customer = { ...context.customer, status: "archived", updated_at };
        context.subscription = { ...context.subscription, status: "disabled", updated_at };
        context.access_key = { ...context.access_key, status: "pending_revoke", updated_at };
        return json(route, { customer: context.customer, disabled_subscriptions: 1, revoked_keys: 0, pending_revoke_keys: 1 });
      }
      if (request.method() === "POST" && url.pathname === "/api/control/vpn/lifecycle/run") return json(route, {});
      if (request.method() === "POST" && url.pathname === "/api/control/vpn/subscriptions") {
        const payload = request.postDataJSON();
        subscriptionCreates.push(payload);
        return json(route, { ...subscriptions[0], ...payload, id: 9900 }, 201);
      }
      if (url.pathname.startsWith("/api/control/vpn/abuse/devices/")) {
        assert.equal(request.method(), "GET");
        const deviceId = Number(url.pathname.split("/").at(-2));
        const context = deviceContext(deviceId);
        assert.equal(Number(url.searchParams.get("customer_id")), context.customer.id);
        if (holdContextIds.has(deviceId)) { heldContexts.push({ route, context }); return; }
        return json(route, context);
      }
      if (url.pathname === "/api/control/vpn/abuse/incidents") {
        assert.equal(request.method(), "GET");
        assert.equal(url.searchParams.get("limit"), "100");
        listQueries.push(Object.fromEntries(url.searchParams));
        const acknowledged = url.searchParams.get("state") === "acknowledged";
        const body = structuredClone(signals.filter((item) => Boolean(item.acknowledged_at) === acknowledged
          && item.id > Number(url.searchParams.get("after_id"))).slice(0, 100));
        if (holdList) { holdList = false; heldLists.push({ route, body }); return; }
        return failList ? json(route, { detail: "synthetic-private-error" }, 503) : json(route, body);
      }
      if (url.pathname.endsWith("/acknowledge")) {
        assert.equal(request.method(), "POST");
        assert.deepEqual(request.postDataJSON(), {});
        mutations.push(url.pathname);
        heldAcks.push({ route, id: Number(url.pathname.split("/").at(-2)) });
        return;
      }
      if (request.method() !== "GET") throw new Error(`Unexpected abuse mutation: ${request.method()} ${url.pathname}`);
      return handleApiRoute(route);
    });
    const panel = review.locator(".vpn-abuse-panel");
    const row = (id) => panel.locator("tbody tr").filter({ hasText: new RegExp(`Логический профиль ${id}(?:[^0-9]|$)`) });
    const refresh = () => panel.getByRole("button", { name: "Обновить список", exact: true }).click();
    const ack = async (id) => {
      await row(id).getByRole("button", { name: "Отметить проверенным", exact: true }).click();
      await until(() => heldAcks.length === 1, "acknowledgement was not held");
    };
    const finishAck = async (success = true, commit = success) => {
      const pending = heldAcks.shift();
      const saved = { ...signals.find((item) => item.id === pending.id), acknowledged_at: "2026-10-25T12:10:00Z" };
      if (commit) signals = signals.map((item) => item.id === pending.id ? saved : item);
      await json(pending.route, success ? saved : { detail: "synthetic-private-error" }, success ? 200 : 503);
    };
    const events = () => review.locator('.vpn-admin-nav a[href="#vpn/events"]').click();
    const openTarget = async (id) => {
      await events();
      await row(id).getByRole("button", { name: "Открыть клиента", exact: true }).click();
      await review.locator(`#vpn-access-key-${id === 1 ? 9004 : 9104}`).waitFor();
    };
    const releaseWrite = async (queue) => {
      const pending = queue.shift();
      const reloaded = review.waitForResponse((reply) => reply.request().method() === "GET"
        && new URL(reply.url()).pathname === "/api/control/vpn/subscriptions");
      await json(pending.route, pending.saved);
      await reloaded;
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    try {
      await review.goto(`${appUrl}#vpn/events`);
      await panel.getByRole("heading", { name: "Подозрение на совместное использование", exact: true }).waitFor();
      await panel.locator("tbody tr").nth(99).waitFor();
      await panel.getByText("Это сигнал для проверки, а не число физических устройств. Доступ не блокируется автоматически", { exact: true }).waitFor();
      assert.match(await row(1).innerText(), /Сетевых источников: 4[\s\S]*максимум за период/);
      assert.match(await row(1).innerText(), /25\.10\.2026[\s\S]*12:00[\s\S]*UTC[\s\S]*12:04/);
      await panel.getByRole("button", { name: "Показать ещё сигналы", exact: true }).click();
      await row(101).waitFor();
      assert.equal(await panel.locator("tbody tr").count(), 101);
      assert.deepEqual(listQueries.at(-1), { state: "open", after_id: "100", limit: "100" });
      holdList = true;
      await refresh();
      await until(() => heldLists.length === 1, "pre-ack list was not held");
      await row(1).getByRole("button", { name: "Отметить проверенным", exact: true }).evaluate((button) => { button.click(); button.click(); });
      await until(() => heldAcks.length === 1, "double acknowledgement was not held");
      assert.equal(mutations.filter((url) => url.endsWith("/acknowledge")).length, 1);
      await finishAck();
      await panel.getByRole("status").filter({ hasText: "Доступ клиента не изменён" }).waitFor();
      await json(heldLists[0].route, heldLists.shift().body);
      assert.equal(await row(1).count(), 0, "old list must not resurrect an acknowledged signal");

      await ack(2);
      holdList = true;
      await refresh();
      await until(() => heldLists.length === 1, "uncertain-ack list was not held");
      await finishAck(false, true);
      await panel.getByRole("alert").filter({ hasText: "сохранена ли отметка" }).waitFor();
      await json(heldLists[0].route, heldLists.shift().body);
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await row(2).getByRole("button", { name: "Отметить проверенным", exact: true }).isDisabled(), true);
      await refresh();
      await row(2).waitFor({ state: "detached" });
      assert.equal(await panel.getByRole("alert").count(), 0);

      holdList = true;
      await refresh();
      await until(() => heldLists.length === 1, "old filter list was not held");
      await panel.getByLabel("Статус проверки").selectOption("acknowledged");
      await row(1).waitFor();
      await json(heldLists[0].route, heldLists.shift().body);
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await panel.locator("tbody tr").count(), 2, "old open response must not replace the acknowledged filter");
      for (const success of [true, false]) {
        await panel.getByLabel("Статус проверки").selectOption("open");
        const id = success ? 3 : 4;
        await row(id).waitFor();
        await ack(id);
        await panel.getByLabel("Статус проверки").selectOption("acknowledged");
        await row(1).waitFor();
        await finishAck(success);
        await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        assert.equal(await panel.getByRole("alert").count(), 0, "late ACK feedback must stay out of another filter");
        assert.equal(await panel.getByRole("status").filter({ hasText: "Сигнал отмечен" }).count(), 0);
      }
      signals = [signal(1), signal(2)];
      await review.locator('.vpn-admin-nav a[href="#vpn/customers"]').click();
      await review.getByRole("textbox", { name: "Поиск", exact: true }).fill("Борис");
      await review.getByRole("button", { name: "Архив", exact: true }).click();
      await events();
      await row(1).waitFor();
      holdContextIds.add(9001);
      await row(1).getByRole("button", { name: "Открыть клиента", exact: true }).click();
      await until(() => heldContexts.length === 1, "customer context was not held");
      assert.equal(await review.getByRole("textbox", { name: "Поиск", exact: true }).inputValue(), "");
      await review.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
      await json(heldContexts[0].route, heldContexts.shift().context);
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.match(await review.locator(".vpn-customer-row.active-chip").innerText(), /Борис/);
      assert.equal(await review.getByText("Целевой профиль А", { exact: true }).count(), 0);
      for (const action of ["edit-customer", "create-subscription"]) {
        await review.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
        await events();
        await row(1).getByRole("button", { name: "Открыть клиента", exact: true }).click();
        await until(() => heldContexts.length === 1, `${action}: context A was not held`);
        if (action === "edit-customer") {
          await review.locator(".vpn-workspace-stack > .vpn-workspace-section")
            .filter({ has: review.getByRole("heading", { name: "Борис", exact: true }) })
            .getByRole("button", { name: "Изменить", exact: true }).click();
          await review.getByRole("textbox", { name: "Имя", exact: true }).fill("Борис после проверки");
        } else {
          await review.getByRole("button", { name: "Новая подписка", exact: true }).click();
        }
        await json(heldContexts[0].route, heldContexts.shift().context);
        await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
        const response = review.waitForResponse((reply) => action === "edit-customer"
          ? reply.request().method() === "PATCH" && /\/vpn\/customers\/[0-9]+$/.test(new URL(reply.url()).pathname)
          : reply.request().method() === "POST" && new URL(reply.url()).pathname === "/api/control/vpn/subscriptions");
        if (action === "edit-customer") {
          const form = review.locator(".vpn-customer-detail form").filter({ has: review.getByRole("textbox", { name: "Имя", exact: true }) });
          await form.getByRole("button", { name: "Сохранить", exact: true }).click();
        } else {
          const form = review.locator("form").filter({ has: review.getByRole("heading", { name: /^Новая подписка для / }) });
          await form.getByRole("button", { name: "Сохранить подписку", exact: true }).click();
        }
        await response;
        await review.locator(".toast").filter({ hasText: action === "edit-customer" ? "Данные клиента сохранены" : "VPN подписка добавлена" }).waitFor();
      }
      assert.deepEqual({
        customerPatchTargets: customerPatches.map((patch) => patch.id),
        subscriptionCustomerTargets: subscriptionCreates.map((payload) => payload.customer_id),
      }, { customerPatchTargets: [2], subscriptionCustomerTargets: [2] },
      "manual forms for B must cancel held navigation to A before saving; A must never receive B's fields or subscription");
      assert.equal(customerPatches[0].payload.first_name, "Борис после проверки");
      holdContextIds.clear();
      await events();
      await row(1).getByRole("button", { name: "Открыть клиента", exact: true }).click();
      const target = review.locator("#vpn-access-key-9004");
      await target.getByText("Целевой профиль А", { exact: true }).waitFor();
      await review.waitForFunction(() => document.activeElement?.id === "vpn-access-key-9004");
      assert.match(await review.locator(".vpn-customer-row.active-chip").innerText(), /Дальний клиент А/);
      assert.equal(await target.getAttribute("data-vpn-device-id"), "9001");
      await target.getByRole("button", { name: "Отозвать и сохранить", exact: true }).waitFor();
      await review.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
      await target.getByText("Целевой профиль А", { exact: true }).waitFor();
      const customerSection = review.locator(".vpn-workspace-stack > .vpn-workspace-section").first();
      const outsideSubscription = review.locator("#vpn-subscription-9003");
      await customerSection.getByRole("button", { name: "Изменить", exact: true }).click();
      await customerSection.getByRole("textbox", { name: "Имя", exact: true }).fill("Дальний клиент А обновлён");
      await customerSection.getByRole("button", { name: "Сохранить", exact: true }).click();
      await review.locator(".toast").filter({ hasText: "Данные клиента сохранены" }).waitFor();
      const savedCustomerName = await customerSection.getByRole("heading").innerText();
      for (let count = 0; count < 2; count += 1) {
        review.once("dialog", (dialog) => dialog.accept());
        const patched = review.waitForResponse((reply) => reply.request().method() === "PATCH"
          && new URL(reply.url()).pathname === "/api/control/vpn/subscriptions/9003");
        await outsideSubscription.getByRole("button", { name: "+7 дней", exact: true }).click();
        await patched;
        await review.waitForFunction(() => !document.querySelector("#vpn-subscription-9003 button")?.disabled);
      }
      assert.deepEqual({ savedCustomerName, expirations: subscriptionPatches.map((patch) => patch.payload.expires_at) }, {
        savedCustomerName: "Дальний клиент А обновлён", expirations: ["2030-01-08T00:00:00.000Z", "2030-01-15T00:00:00.000Z"],
      }, "outside-list records must reflect authoritative saved responses; consecutive extensions must accumulate");
      assert.match(await outsideSubscription.locator(":scope > .vpn-workspace-section-head .row-hint").innerText(), /15\.01\.2030/);

      review.once("dialog", (dialog) => dialog.accept());
      await customerSection.getByRole("button", { name: "В архив", exact: true }).click();
      await customerSection.getByRole("button", { name: "Восстановить", exact: true }).waitFor();
      await outsideSubscription.getByText("приостановлена", { exact: true }).waitFor();
      await target.getByText("ожидает отзыва", { exact: true }).waitFor();
      await customerSection.getByRole("button", { name: "Восстановить", exact: true }).click();
      await customerSection.getByRole("button", { name: "В архив", exact: true }).waitFor();
      assert.equal(await outsideSubscription.getByText("приостановлена", { exact: true }).count(), 1,
        "restoring the customer must not invent a subscription reactivation");

      holdSubscriptionWrite = true;
      review.once("dialog", (dialog) => dialog.accept());
      await outsideSubscription.getByRole("button", { name: "+7 дней", exact: true }).click();
      await until(() => heldSubscriptionWrites.length === 1, "older subscription write was not held");
      contexts.get(9001).subscription = { ...contexts.get(9001).subscription, expires_at: "2030-02-01T00:00:00Z",
        updated_at: heldSubscriptionWrites[0].saved.updated_at.replace(".000001", ".000002") };
      await openTarget(1);
      await outsideSubscription.locator(".row-hint").filter({ hasText: "01.02.2030" }).waitFor();
      await releaseWrite(heldSubscriptionWrites);
      assert.match(await outsideSubscription.locator(":scope > .vpn-workspace-section-head .row-hint").innerText(), /01\.02\.2030/,
        "a late mutation result must not replace a newer subscription version, even within the same millisecond");
      holdSubscriptionWrite = false;
      review.once("dialog", (dialog) => dialog.accept());
      await outsideSubscription.getByRole("button", { name: "+7 дней", exact: true }).click();
      await outsideSubscription.locator(".row-hint").filter({ hasText: "08.02.2030" }).waitFor();
      assert.equal(subscriptionPatches.at(-1).payload.expires_at, "2030-02-08T00:00:00.000Z");

      holdCustomerWrite = true;
      await customerSection.getByRole("button", { name: "Изменить", exact: true }).click();
      await customerSection.getByRole("textbox", { name: "Имя", exact: true }).fill("Старое сохранённое имя");
      await customerSection.getByRole("button", { name: "Сохранить", exact: true }).click();
      await until(() => heldCustomerWrites.length === 1, "older customer write was not held");
      contexts.get(9001).customer = { ...contexts.get(9001).customer, first_name: "Новейшее имя клиента А",
        updated_at: heldCustomerWrites[0].saved.updated_at.replace(".000001", ".000002") };
      await openTarget(1);
      await customerSection.getByRole("heading", { name: "Новейшее имя клиента А", exact: true }).waitFor();
      await releaseWrite(heldCustomerWrites);
      assert.equal(await customerSection.getByRole("heading").innerText(), "Новейшее имя клиента А",
        "an older customer mutation response must not replace newer retained data");

      await customerSection.getByRole("button", { name: "Изменить", exact: true }).click();
      await customerSection.getByRole("textbox", { name: "Имя", exact: true }).fill("Запоздалое имя клиента А");
      await customerSection.getByRole("button", { name: "Сохранить", exact: true }).click();
      await until(() => heldCustomerWrites.length === 1, "late customer write was not held");
      await openTarget(2);
      await customerSection.getByRole("button", { name: "Изменить", exact: true }).click();
      await customerSection.getByRole("textbox", { name: "Имя", exact: true }).fill("Не трогать форму клиента Б");
      await releaseWrite(heldCustomerWrites);
      assert.match(await review.locator(".vpn-customer-row.active-chip").innerText(), /Дальний клиент Б/);
      assert.equal(await customerSection.getByRole("textbox", { name: "Имя", exact: true }).inputValue(), "Не трогать форму клиента Б",
        "a late save for A must not retarget or close B's manual editor");
      holdCustomerWrite = false;

      await openTarget(1);
      holdSubscriptionWrite = true;
      review.once("dialog", (dialog) => dialog.accept());
      await outsideSubscription.getByRole("button", { name: "+7 дней", exact: true }).click();
      await until(() => heldSubscriptionWrites.length === 1, "late subscription write was not held");
      await openTarget(2);
      await customerSection.getByRole("button", { name: "Изменить", exact: true }).click();
      await customerSection.getByRole("textbox", { name: "Имя", exact: true }).fill("Ещё одна форма клиента Б");
      await releaseWrite(heldSubscriptionWrites);
      assert.match(await review.locator(".vpn-customer-row.active-chip").innerText(), /Дальний клиент Б/);
      assert.equal(await customerSection.getByRole("textbox", { name: "Имя", exact: true }).inputValue(), "Ещё одна форма клиента Б",
        "late subscription responses must remain scoped to their original retained subscription");
      holdSubscriptionWrite = false;
      holdContextIds.add(9001);
      await events();
      await row(1).getByRole("button", { name: "Открыть клиента", exact: true }).click();
      await until(() => heldContexts.length === 1, "older target context was not held");
      await events();
      await row(2).getByRole("button", { name: "Открыть клиента", exact: true }).click();
      await review.locator("#vpn-access-key-9104").getByText("Целевой профиль Б", { exact: true }).waitFor();
      await json(heldContexts[0].route, heldContexts.shift().context);
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.match(await review.locator(".vpn-customer-row.active-chip").innerText(), /Дальний клиент Б/);
      assert.equal(await review.getByText("Целевой профиль А", { exact: true }).count(), 0);
      holdContextIds.clear();
      await events();
      await row(1).waitFor();
      for (const width of [1280, 390]) {
        await review.setViewportSize({ width, height: 1000 });
        await panel.scrollIntoViewIfNeeded();
        assert.equal(await panel.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), true, `${width}px abuse panel must fit`);
        await panel.screenshot({ path: path.join(output, `admin-abuse-${width}.png`), style: ".vpn-admin-nav { visibility: hidden !important; }" });
      }
      failList = true;
      await refresh();
      await panel.getByRole("alert").waitFor();
      assert.equal((await panel.innerText()).includes("synthetic-private-error"), false);
      failList = false;
      await refresh();
      await row(1).waitFor();
      await ack(1);
      holdList = true;
      await refresh();
      await until(() => heldLists.length === 1, "logout list was not held");
      await review.getByRole("button", { name: "Выйти", exact: true }).click();
      await review.getByRole("heading", { name: "Вход", exact: true }).waitFor();
      userId = 2;
      const login = review.locator("form").filter({ has: review.getByRole("button", { name: "Войти", exact: true }) });
      await login.getByLabel("Логин", { exact: true }).fill("synthetic-2");
      await login.getByLabel("Пароль", { exact: true }).fill("synthetic-password");
      await login.getByRole("button", { name: "Войти", exact: true }).click();
      await row(1).waitFor();
      await finishAck();
      await json(heldLists[0].route, heldLists.shift().body);
      await review.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      await row(1).waitFor();
      assert.equal(await panel.getByRole("status").filter({ hasText: "Сигнал отмечен" }).count(), 0, "old session ACK must not update the new session");
      assert.equal((await review.content()).includes("synthetic-private-error"), false);
      assert.equal(reviewLogs.some((value) => value.includes("synthetic-private-error")), false);
      assert.deepEqual(mutations.filter((url) => !url.endsWith("/acknowledge") && url !== "/api/auth/logout"), []);
      assert.deepEqual(reviewErrors, []);
      console.log("PASS: mounted abuse review cursor/duplicate-ACK/uncertain-refresh/stale-filter/logout/outside-list-target/manual-form-targets/saved-context-mutations/version-order/late-writes/navigation-races/privacy/desktop-mobile; zero direct revoke/suspend/restart calls, synthetic HTTP only");
    } finally {
      await review.close();
    }
  } else if (process.argv.includes("--broadcasts-only")) {
    const news = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    news.setDefaultTimeout(10000);
    const newsErrors = [];
    news.on("pageerror", (error) => newsErrors.push(error.message));
    let recipients = 6;
    let config = { enabled: true, registry_state: "ready", eligible_recipients: recipients, explanation: "Новость получат пользователи, которые уже писали боту и не заблокировали его" };
    const emptyCounts = { recipient_count: 0, pending: 0, claimed: 0, sending: 0, sent: 0, failed: 0, skipped: 0, uncertain: 0, cancelled: 0 };
    let campaign = null;
    let conflict = true;
    let revisionConflict = false;
    let nextCampaignId = 11;
    let detailGets = 0;
    let holdPoll = false;
    let heldPoll;
    let heldPollSnapshot;
    const mutations = [];
    await news.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      if (!pathname.startsWith("/api/control/vpn/broadcasts")) return handleApiRoute(route);
      if (pathname.endsWith("/config")) return json(route, { ...config, eligible_recipients: config.registry_state === "ready" ? recipients : null });
      if (request.method() === "GET" && pathname === "/api/control/vpn/broadcasts") return json(route, campaign ? [{ id: campaign.id, state: campaign.state, revision: campaign.revision, created_at: stamp, confirmed_at: null, cancelled_at: null }] : []);
      if (request.method() === "GET") {
        detailGets += 1;
        if (holdPoll && campaign?.state === "running") { holdPoll = false; heldPoll = route; heldPollSnapshot = structuredClone(campaign); return; }
        return json(route, campaign);
      }
      const payload = request.postDataJSON();
      mutations.push({ pathname, method: request.method(), payload });
      if (pathname === "/api/control/vpn/broadcasts") {
        campaign = { id: nextCampaignId++, body: payload.body, state: "draft", revision: 1, preview_revision: null, preview_recipient_count: null, previewed_at: null, created_at: stamp, confirmed_at: null, cancelled_at: null, counts: { ...emptyCounts } };
        return json(route, campaign, 201);
      }
      if (request.method() === "PATCH") {
        campaign = { ...campaign, body: payload.body, revision: campaign.revision + 1, preview_revision: null, preview_recipient_count: null, previewed_at: null };
        return json(route, campaign);
      }
      if (pathname.endsWith("/preview")) {
        assert.deepEqual(payload, {});
        campaign = { ...campaign, preview_revision: campaign.revision, preview_recipient_count: recipients, previewed_at: stamp };
        return json(route, campaign);
      }
      if (pathname.endsWith("/confirm")) {
        assert.deepEqual(payload, { expected_revision: campaign.revision, expected_recipient_count: campaign.preview_recipient_count, confirm: true });
        if (conflict) { conflict = false; recipients = 7; return json(route, { detail: "vpn_broadcast_audience_changed" }, 409); }
        if (revisionConflict) {
          revisionConflict = false;
          campaign = { ...campaign, body: "Новая серверная версия", revision: campaign.revision + 1, preview_revision: null, preview_recipient_count: null, previewed_at: null };
          return json(route, { detail: "vpn_broadcast_preview_changed" }, 409);
        }
        campaign = { ...campaign, state: "running", counts: { ...emptyCounts, recipient_count: 7, pending: 2, sending: 1, sent: 1, failed: 1, skipped: 1, uncertain: 1 } };
        return json(route, campaign, 202);
      }
      if (pathname.endsWith("/cancel")) {
        assert.deepEqual(payload, {});
        campaign = { ...campaign, state: "cancelled", counts: { ...campaign.counts, pending: 0, cancelled: 2 } };
        return json(route, campaign);
      }
      throw new Error(`Unexpected news mutation ${pathname}`);
    });
    await news.goto(`${appUrl}#vpn/broadcasts`);
    const panel = news.locator(".vpn-broadcast-panel");
    await panel.getByRole("heading", { name: "Новости VPN-бота", exact: true }).waitFor({ timeout: 3000 });
    const originalBody = "<b>Новости</b>\n😀";
    await panel.getByLabel("Текст новости", { exact: true }).fill(originalBody);
    assert.equal(await panel.getByLabel("Текст новости", { exact: true }).getAttribute("maxlength"), null);
    await panel.getByText(`Осталось символов: ${4096 - Array.from(originalBody).length}`, { exact: true }).waitFor();
    await panel.getByRole("button", { name: "Сохранить черновик", exact: true }).evaluate((button) => { button.click(); button.click(); });
    await panel.getByRole("status").filter({ hasText: "Черновик сохранён" }).waitFor();
    assert.equal(mutations.filter((item) => item.pathname === "/api/control/vpn/broadcasts").length, 1);
    await panel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await panel.locator(".vpn-broadcast-preview").waitFor();
    assert.equal(await panel.locator(".vpn-broadcast-preview").textContent(), originalBody);
    assert.equal(await panel.locator(".vpn-broadcast-preview b").count(), 0);
    assert.equal(mutations.filter((item) => item.pathname.endsWith("/confirm")).length, 0);
    const body = `${originalBody}\nПравка`;
    await panel.getByLabel("Текст новости", { exact: true }).fill(body);
    assert.equal(await panel.getByRole("button", { name: "Отправить новость", exact: true }).isDisabled(), true);
    await panel.getByRole("button", { name: "Сохранить черновик", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "Черновик сохранён" }).waitFor();
    await panel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await panel.getByRole("button", { name: "Отправить новость", exact: true }).click();
    let dialog = news.getByRole("dialog", { name: "Подтвердить отправку новости?", exact: true });
    await dialog.waitFor();
    assert.equal(await dialog.locator(".vpn-broadcast-preview").textContent(), body);
    assert.equal(mutations.filter((item) => item.pathname.endsWith("/confirm")).length, 0, "opening confirmation never sends");
    await dialog.getByRole("button", { name: "Подтвердить отправку", exact: true }).evaluate((button) => { button.click(); button.click(); });
    await panel.getByRole("alert").filter({ hasText: "Список получателей изменился" }).waitFor();
    assert.equal(await news.getByRole("dialog").count(), 0);
    assert.equal(await panel.getByRole("button", { name: "Отправить новость", exact: true }).isDisabled(), true);
    assert.equal(mutations.filter((item) => item.pathname.endsWith("/confirm")).length, 1, "409 never auto-retries confirmation");
    await panel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await panel.getByRole("button", { name: "Отправить новость", exact: true }).click();
    dialog = news.getByRole("dialog");
    await dialog.getByRole("button", { name: "Подтвердить отправку", exact: true }).click();
    await panel.getByText("Отправляется", { exact: true }).waitFor();
    await panel.getByText("Неопределено: Telegram мог доставить сообщение. Автоматического повтора не будет", { exact: true }).waitFor();
    const getsBeforePoll = detailGets;
    holdPoll = true;
    await until(() => detailGets > getsBeforePoll, "running news campaign was not polled in 2s");
    await panel.getByRole("button", { name: "Остановить оставшуюся очередь", exact: true }).click();
    await panel.getByText("Остановлена", { exact: true }).waitFor();
    assert.ok(heldPoll);
    await json(heldPoll, heldPollSnapshot);
    await news.waitForTimeout(100);
    await panel.getByText("Остановлена", { exact: true }).waitFor();
    assert.equal(campaign.counts.sent, 1); assert.equal(campaign.counts.sending, 1); assert.equal(campaign.counts.cancelled, 2);
    const getsAfterCancel = detailGets;
    await news.waitForTimeout(2200);
    assert.equal(detailGets, getsAfterCancel, "terminal campaign stops automatic polling");
    assert.equal(await panel.getByRole("button", { name: /повтор.*отправ/i }).count(), 0);
    campaign = { ...campaign, counts: { ...campaign.counts, sending: 0, sent: 2 } };
    await panel.getByRole("button", { name: "Обновить состояние", exact: true }).click();
    await panel.locator(".vpn-broadcast-counts > div").filter({ has: news.getByText("Отправлено", { exact: true }) }).locator("dd").getByText("2", { exact: true }).waitFor();
    await panel.getByRole("button", { name: "Новый черновик", exact: true }).click();
    await panel.getByLabel("Текст новости", { exact: true }).fill("Проверка версии");
    await panel.getByRole("button", { name: "Сохранить черновик", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "Черновик сохранён" }).waitFor();
    await panel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await panel.getByRole("button", { name: "Отправить новость", exact: true }).click();
    revisionConflict = true;
    await news.getByRole("dialog").getByRole("button", { name: "Подтвердить отправку", exact: true }).click();
    await panel.getByRole("alert").filter({ hasText: "Версия черновика изменилась" }).waitFor();
    await news.waitForTimeout(100);
    assert.equal(await panel.getByLabel("Текст новости", { exact: true }).inputValue(), "Новая серверная версия", "revision conflict reloads authoritative text, not a ghost old draft");
    assert.equal(await panel.getByRole("button", { name: "Отправить новость", exact: true }).isDisabled(), true);
    for (const registry_state of ["ready", "history_unverified", "bot_changed", "unconfigured"]) {
      config = { ...config, enabled: false, registry_state, explanation: `Проверка реестра: ${registry_state}` };
      await panel.getByRole("button", { name: "Обновить настройки и список", exact: true }).click();
      await panel.getByText(config.explanation, { exact: true }).waitFor();
      assert.equal(await panel.getByRole("button", { name: "Отправить новость", exact: true }).isDisabled(), true);
      if (registry_state !== "ready") {
        await panel.getByText("Получателей в реестре: Нет данных", { exact: true }).waitFor();
        assert.equal(await panel.getByRole("button", { name: "Предпросмотр", exact: true }).isDisabled(), true);
      } else {
        assert.equal(await panel.getByRole("button", { name: "Сохранить черновик", exact: true }).isDisabled(), false, "disabled sender still allows drafts");
      }
    }
    config = { ...config, enabled: true, registry_state: "ready", explanation: "Реестр готов" };
    await panel.getByRole("button", { name: "Обновить настройки и список", exact: true }).click();
    await panel.getByText("Реестр готов", { exact: true }).waitFor();
    await panel.getByRole("button", { name: "Новый черновик", exact: true }).click();
    const longBody = "😀".repeat(4096);
    await panel.getByLabel("Текст новости", { exact: true }).fill(longBody);
    await panel.getByText("Осталось символов: 0", { exact: true }).waitFor();
    await panel.getByRole("button", { name: "Сохранить черновик", exact: true }).click();
    await panel.getByRole("status").filter({ hasText: "Черновик сохранён" }).waitFor();
    await panel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await panel.getByRole("button", { name: "Отправить новость", exact: true }).click();
    dialog = news.getByRole("dialog");
    assert.equal(await dialog.locator(".vpn-broadcast-preview").textContent(), longBody);
    assert.equal(await dialog.locator(".vpn-broadcast-preview").evaluate((element) => element.clientHeight <= innerHeight / 2), true, "long exact preview has a bounded keyboard-scrollable region");
    await dialog.getByRole("button", { name: "Отмена", exact: true }).click();
    for (const width of [1280, 390]) {
      await news.setViewportSize({ width, height: 1000 });
      await assertVisibleVpnInteractiveTraversal(news, `news-${width}`);
      assert.equal(await news.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
      await panel.screenshot({ path: path.join(output, `news-${width}.png`), style: ".vpn-admin-nav { visibility: hidden !important; }" });
    }
    await panel.getByRole("button", { name: "Отправить новость", exact: true }).click();
    dialog = news.getByRole("dialog");
    await dialog.getByLabel("Точный текст новости", { exact: true }).focus();
    await news.keyboard.press("Tab"); await news.keyboard.press("Shift+Tab");
    assert.equal(await dialog.getByLabel("Точный текст новости", { exact: true }).evaluate((element) => element.matches(":focus-visible")), true);
    await dialog.screenshot({ path: path.join(output, "news-confirm-390.png") });
    await news.keyboard.press("Escape");
    assert.equal(await news.getByRole("dialog").count(), 0);
    for (const width of [1280, 390]) {
      await news.setViewportSize({ width, height: 1000 });
      await news.reload();
      await panel.getByLabel("Текст новости", { exact: true }).fill("Новость о работе VPN-бота\nПодключения продолжат работать.");
      await assertAdminTwoHundredPercentTextReflow(news, `news-${width}-text-200`);
      await panel.screenshot({ path: path.join(output, `news-${width}-text-200.png`) });
    }
    assert.deepEqual(newsErrors, []);
    await news.close();

    const racePage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    racePage.on("pageerror", (error) => newsErrors.push(error.message));
    racePage.setDefaultTimeout(10000);
    const fixtureCampaign = (id) => ({ id, revision: 1, state: "draft", body: `Кампания ${id}`, preview_revision: null, preview_recipient_count: null, previewed_at: null, created_at: stamp, confirmed_at: null, cancelled_at: null, counts: { ...emptyCounts } });
    const summaries = Array.from({ length: 50 }, (_, index) => { const { body, counts, preview_revision, preview_recipient_count, previewed_at, ...row } = fixtureCampaign(100 - index); return row; });
    const heldNews = [];
    let pages = 0;
    let holdDetail98 = false;
    let failDetail97 = true;
    let campaign98 = fixtureCampaign(98);
    await racePage.route("**/api/**", async (route) => {
      const request = route.request(); const url = new URL(request.url()); const path = url.pathname;
      if (path === "/api/auth/logout") return json(route, {});
      if (!path.startsWith("/api/control/vpn/broadcasts")) return handleApiRoute(route);
      if (path.endsWith("/config")) return json(route, { ...config, enabled: true, registry_state: "ready", eligible_recipients: 7 });
      if (path === "/api/control/vpn/broadcasts" && request.method() === "GET") {
        if (url.searchParams.has("before_id")) { pages += 1; assert.equal(url.searchParams.get("before_id"), "51"); return json(route, [fixtureCampaign(50)]); }
        return json(route, summaries);
      }
      if (path === "/api/control/vpn/broadcasts" && request.method() === "POST") { heldNews.push(route); return; }
      if (path === "/api/control/vpn/broadcasts/98/preview") return json(route, { ...campaign98, preview_revision: campaign98.revision, preview_recipient_count: 7, previewed_at: stamp });
      const id = Number(path.split("/").pop());
      if (id === 97 && failDetail97) { failDetail97 = false; return json(route, { detail: "synthetic detail error" }, 503); }
      if ((request.method() === "GET" && (id === 100 || id === 98 && holdDetail98)) || request.method() === "PATCH") { heldNews.push(route); return; }
      if (id === 98) return json(route, campaign98);
      return json(route, fixtureCampaign(id));
    });
    await racePage.goto(`${appUrl}#vpn/broadcasts`);
    const racePanel = racePage.locator(".vpn-broadcast-panel");
    await racePanel.getByRole("button", { name: "Показать ещё рассылки", exact: true }).evaluate((button) => { button.click(); button.click(); });
    await racePanel.getByRole("button", { name: "Показать ещё рассылки", exact: true }).waitFor({ state: "detached" });
    assert.equal(pages, 1, "campaign cursor page has a synchronous duplicate guard");
    await racePanel.getByLabel("Рассылка", { exact: true }).selectOption("100");
    await until(() => heldNews.length === 1, "old selected campaign detail was not held");
    await racePanel.getByLabel("Рассылка", { exact: true }).selectOption("99");
    await racePage.waitForFunction(() => document.querySelector('textarea[aria-describedby="vpn-news-count"]')?.value === "Кампания 99");
    await json(heldNews.shift(), { ...fixtureCampaign(100), body: "Старая секретная кампания" });
    await racePage.waitForTimeout(100);
    assert.equal(await racePanel.getByLabel("Текст новости", { exact: true }).inputValue(), "Кампания 99");
    await racePanel.getByLabel("Текст новости", { exact: true }).fill("Отложенная правка");
    await racePanel.getByRole("button", { name: "Сохранить черновик", exact: true }).click();
    await until(() => heldNews.length === 1, "old draft PATCH was not held");
    await racePanel.getByLabel("Рассылка", { exact: true }).selectOption("98");
    await racePage.waitForFunction(() => document.querySelector('textarea[aria-describedby="vpn-news-count"]')?.value === "Кампания 98");
    await json(heldNews.shift(), { ...fixtureCampaign(99), body: "Отложенная правка", revision: 2 });
    await racePage.waitForTimeout(100);
    assert.equal(await racePanel.getByLabel("Текст новости", { exact: true }).inputValue(), "Кампания 98", "old mutation cannot replace new campaign");
    await racePanel.getByRole("button", { name: "Предпросмотр", exact: true }).click();
    await racePage.waitForFunction(() => ![...document.querySelectorAll("button")].find((button) => button.textContent === "Отправить новость")?.disabled);
    holdDetail98 = true;
    await racePanel.getByRole("button", { name: "Обновить состояние", exact: true }).click();
    await until(() => heldNews.length === 1, "manual campaign refresh was not held");
    campaign98 = { ...campaign98, body: "Новая версия другого администратора", revision: 2 };
    await json(heldNews.shift(), campaign98);
    await racePage.waitForTimeout(100);
    assert.equal(await racePanel.getByRole("button", { name: "Отправить новость", exact: true }).isDisabled(), true, "fresh server revision invalidates local consent preview");
    assert.equal(await racePanel.locator(".vpn-broadcast-preview").count(), 0);
    assert.equal(await racePanel.getByLabel("Текст новости", { exact: true }).inputValue(), campaign98.body, "clean editor follows an authoritative manual refresh");
    await racePanel.getByRole("button", { name: "Обновить состояние", exact: true }).click();
    await until(() => heldNews.length === 1, "editable refresh was not held");
    await racePanel.getByLabel("Текст новости", { exact: true }).fill("Новая локальная правка");
    await json(heldNews.shift(), { ...campaign98, body: "Запоздавшая серверная версия", revision: 3 });
    await racePage.waitForTimeout(100);
    assert.equal(await racePanel.getByLabel("Текст новости", { exact: true }).inputValue(), "Новая локальная правка", "late read cannot erase a newly typed edit");
    await racePanel.getByLabel("Рассылка", { exact: true }).selectOption("97");
    await racePanel.getByRole("alert").filter({ hasText: "Не удалось обновить состояние рассылки" }).waitFor();
    assert.equal(await racePanel.getByRole("button", { name: "Обновить состояние", exact: true }).count(), 1, "initial detail failure retains an explicit retry action");
    await racePanel.getByRole("button", { name: "Обновить состояние", exact: true }).click();
    await racePage.waitForFunction(() => document.querySelector('textarea[aria-describedby="vpn-news-count"]')?.value === "Кампания 97");
    await racePanel.getByRole("button", { name: "Новый черновик", exact: true }).click();
    await racePanel.getByLabel("Текст новости", { exact: true }).fill("Секрет после выхода");
    await racePanel.getByRole("button", { name: "Сохранить черновик", exact: true }).click();
    await until(() => heldNews.length === 1, "logout create was not held");
    await racePage.getByRole("button", { name: "Выйти", exact: true }).click();
    await racePage.getByRole("heading", { name: "Вход", exact: true }).waitFor();
    await json(heldNews.shift(), { ...fixtureCampaign(101), body: "Секрет после выхода" }, 201);
    await racePage.waitForTimeout(100);
    assert.equal((await racePage.locator("body").innerText()).includes("Секрет после выхода"), false);
    assert.equal(await racePage.locator(".vpn-broadcast-panel").count(), 0);
    await racePage.close();
    const recoveryPage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    recoveryPage.setDefaultTimeout(10000);
    recoveryPage.on("pageerror", (error) => newsErrors.push(error.message));
    let recoveredCampaign = null;
    let failRecoveryList = false;
    let failRecoveryDetail = false;
    let uncertainCreates = 0;
    let uncertainUpdates = 0;
    await recoveryPage.route("**/api/**", async (route) => {
      const request = route.request(); const path = new URL(request.url()).pathname;
      if (!path.startsWith("/api/control/vpn/broadcasts")) return handleApiRoute(route);
      if (path.endsWith("/config")) return json(route, { ...config, enabled: true, registry_state: "ready" });
      if (path === "/api/control/vpn/broadcasts" && request.method() === "GET") {
        if (failRecoveryList) return json(route, { detail: "synthetic list failure" }, 503);
        return json(route, recoveredCampaign ? [recoveredCampaign] : []);
      }
      if (request.method() === "POST") {
        uncertainCreates += 1;
        recoveredCampaign = { ...fixtureCampaign(71), body: request.postDataJSON().body };
        return route.abort("failed");
      }
      if (request.method() === "PATCH") {
        uncertainUpdates += 1;
        recoveredCampaign = { ...recoveredCampaign, body: request.postDataJSON().body, revision: recoveredCampaign.revision + 1 };
        failRecoveryDetail = true;
        return route.abort("failed");
      }
      if (failRecoveryDetail) return json(route, { detail: "synthetic detail failure" }, 503);
      return json(route, recoveredCampaign);
    });
    await recoveryPage.goto(`${appUrl}#vpn/broadcasts`);
    const recoveryPanel = recoveryPage.locator(".vpn-broadcast-panel");
    const recoverySave = recoveryPanel.getByRole("button", { name: "Сохранить черновик", exact: true });
    await recoveryPanel.getByLabel("Текст новости", { exact: true }).fill("Сохранённый черновик с потерянным ответом");
    await recoverySave.click();
    await recoveryPanel.getByRole("alert").waitFor();
    assert.equal(await recoverySave.isDisabled(), true, "lost POST response blocks immediate duplicate creation");
    failRecoveryList = true;
    await recoveryPanel.getByRole("button", { name: "Обновить настройки и список", exact: true }).click();
    await recoveryPanel.getByRole("alert").filter({ hasText: "Не удалось загрузить список" }).waitFor();
    assert.equal(await recoverySave.isDisabled(), true, "failed reconciliation cannot permit a repeat POST");
    failRecoveryList = false;
    await recoveryPanel.getByRole("button", { name: "Обновить настройки и список", exact: true }).click();
    await recoveryPanel.getByLabel("Рассылка", { exact: true }).selectOption("71");
    await recoveryPage.waitForFunction(() => document.querySelector('textarea[aria-describedby="vpn-news-count"]')?.value === "Сохранённый черновик с потерянным ответом");
    assert.equal(uncertainCreates, 1);
    await recoveryPanel.getByLabel("Текст новости", { exact: true }).fill("Сохранённая правка с потерянным ответом");
    await recoverySave.click();
    await recoveryPanel.getByRole("alert").filter({ hasText: "Не удалось обновить состояние" }).waitFor();
    assert.equal(await recoverySave.isDisabled(), true, "failed recovery GET cannot permit a repeat PATCH");
    assert.equal(await recoveryPanel.getByRole("button", { name: "Предпросмотр", exact: true }).isDisabled(), true);
    failRecoveryDetail = false;
    await recoveryPanel.getByRole("button", { name: "Обновить состояние", exact: true }).click();
    await recoveryPage.waitForFunction(() => [...document.querySelectorAll(".vpn-broadcast-panel button")].find((button) => button.textContent === "Сохранить черновик")?.disabled === false);
    assert.equal(uncertainUpdates, 1);
    await recoveryPage.close();
    const setupPage = await browser.newPage({ viewport: { width: 390, height: 1000 } });
    let heldConfig;
    await setupPage.route("**/api/**", (route) => {
      const path = new URL(route.request().url()).pathname;
      if (path === "/api/control/vpn/broadcasts/config") { heldConfig = route; return; }
      if (path === "/api/control/vpn/broadcasts") return json(route, { detail: "synthetic list failure" }, 503);
      return handleApiRoute(route);
    });
    await setupPage.goto(`${appUrl}#vpn/broadcasts`);
    await setupPage.getByRole("alert").filter({ hasText: "Не удалось загрузить список рассылок" }).waitFor();
    await json(heldConfig, { ...config, enabled: true, registry_state: "ready", eligible_recipients: 7 });
    await setupPage.waitForTimeout(100);
    assert.equal(await setupPage.getByRole("alert").filter({ hasText: "Не удалось загрузить список рассылок" }).count(), 1, "successful config response cannot erase an independent list error");
    await setupPage.close();
    assert.deepEqual(newsErrors, []);
    console.log("PASS: plain-text news draft/preview/edit/explicit confirmation/409/progress/poll/cancel; synthetic HTTP only");
  } else if (process.argv.includes("--usage-only")) {
    const usagePage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    usagePage.setDefaultTimeout(10000);
    const usageErrors = [];
    usagePage.on("pageerror", (error) => usageErrors.push(error.message));
    const usageRow = (worker_id, overrides = {}) => ({ worker_id, upload_bytes: null, download_bytes: null,
      measured_since: null, last_sample_at: null, coverage: "none", active_devices: 2,
      occupied_profiles: 3, max_active_profiles: 10, cpu_load: 0.75, ram_usage_percent: 25, ...overrides });
    const rows = Array.from({ length: 100 }, (_, index) => usageRow(41 + index));
    rows[1] = usageRow(42, { upload_bytes: 0, download_bytes: 0, coverage: "partial", last_sample_at: stamp });
    rows[2] = usageRow(43, { upload_bytes: 1073741824, download_bytes: 2147483648, coverage: "stale", measured_since: stamp, last_sample_at: stamp, cpu_load: null, ram_usage_percent: null });
    rows[3] = usageRow(44, { upload_bytes: 4096, download_bytes: 8192, coverage: "unavailable", measured_since: stamp, last_sample_at: stamp });
    const nodeCalls = [];
    let failNodes = false;
    let holdNodePeriod = null;
    let holdNodeCursor = false;
    let holdCustomer = null;
    const heldStats = [];
    const customerStats = (customerId, days) => ({ customer_id: customerId, days,
      upload_bytes: 3221225472, download_bytes: 4294967296, measured_since: stamp, last_sample_at: stamp, coverage: "partial",
      devices: [{ device_id: customerId === 1 ? 121 : 122, display_name: customerId === 1 ? "Телефон: две страны" : "Ноутбук Бориса", upload_bytes: 3221225472, download_bytes: 4294967296, measured_since: stamp, last_sample_at: stamp, coverage: "partial" }] });
    await usagePage.route("**/api/**", async (route) => {
      const url = new URL(route.request().url());
      if (url.pathname === "/api/auth/logout") return json(route, { detail: "logged out" });
      if (url.pathname === "/api/control/vpn/usage/nodes") {
        nodeCalls.push(url.search);
        if (holdNodePeriod === Number(url.searchParams.get("days")) || holdNodeCursor && Number(url.searchParams.get("after_id")) > 0) { heldStats.push(route); return; }
        if (failNodes) return json(route, { detail: "synthetic private upstream details" }, 503);
        if (Number(url.searchParams.get("days")) !== 1) return json(route, [usageRow(41, { upload_bytes: Number(url.searchParams.get("days")) * 1073741824, download_bytes: 0, coverage: "partial", measured_since: stamp, last_sample_at: stamp }), rows[2], rows[3]]);
        return json(route, Number(url.searchParams.get("after_id")) ? [usageRow(141)] : rows);
      }
      if (/\/customers\/\d+\/usage$/.test(url.pathname)) {
        const customerId = Number(url.pathname.split("/").at(-2));
        if (holdCustomer === customerId) { heldStats.push(route); return; }
        return json(route, customerStats(customerId, Number(url.searchParams.get("days"))));
      }
      return handleApiRoute(route);
    });
    await usagePage.goto(`${appUrl}#vpn/overview`);
    const nodes = usagePage.locator('[data-vpn-usage="nodes"]');
    await nodes.getByRole("heading", { name: "Трафик нод", exact: true }).waitFor({ timeout: 3000 });
    await nodes.getByText("Frankfurt 1", { exact: true }).waitFor();
    assert.equal(await nodes.locator("tbody tr").count(), 100);
    assert.match(await nodes.locator("tbody tr").nth(0).innerText(), /Нет данных/);
    assert.match(await nodes.locator("tbody tr").nth(1).innerText(), /0 Б/);
    assert.match(await nodes.locator("tbody tr").nth(2).innerText(), /1 ГиБ.*2 ГиБ/s);
    assert.match(await nodes.locator("tbody tr").nth(2).innerText(), /Данные устарели/);
    assert.match(await nodes.locator("tbody tr").nth(2).innerText(), /Последнее измерение/);
    assert.match(await nodes.locator("tbody tr").nth(3).innerText(), /Сбор недоступен/);
    assert.match(await nodes.locator("tbody tr").nth(3).innerText(), /4 КиБ/);
    assert.equal(await nodes.locator('tbody tr').first().locator('[data-label="Нагрузка CPU"]').innerText(), "0.75");
    assert.equal(await nodes.locator('tbody tr').first().locator('[data-label="RAM"]').innerText(), "25 %");
    assert.equal(await nodes.locator('tbody tr').first().locator('[data-label="Занято профилей / лимит"]').innerText(), "3 / 10");
    await nodes.getByRole("button", { name: "Показать ещё ноды", exact: true }).evaluate((button) => { button.click(); button.click(); });
    await nodes.locator("tbody tr").nth(100).waitFor();
    assert.equal(nodeCalls.filter((query) => query.includes("after_id=140")).length, 1);
    assert.equal(await nodes.getByRole("button", { name: "Показать ещё ноды" }).count(), 0);
    holdNodePeriod = 7;
    await nodes.getByLabel("Период статистики").selectOption("7");
    await until(() => heldStats.length === 1, "held older period was not requested");
    await nodes.getByLabel("Период статистики").selectOption("30");
    await nodes.getByText("30 ГиБ", { exact: true }).waitFor();
    await json(heldStats.shift(), [usageRow(41, { upload_bytes: 7516192768 })]);
    await usagePage.waitForTimeout(100);
    assert.equal(await nodes.getByText("30 ГиБ", { exact: true }).count(), 1, "late period cannot overwrite selected period");
    failNodes = true;
    await nodes.getByRole("button", { name: "Обновить статистику", exact: true }).click();
    await nodes.getByRole("alert").waitFor();
    assert.equal(await nodes.getByText("30 ГиБ", { exact: true }).count(), 1, "failed refresh retains measured totals");
    assert.equal((await nodes.innerText()).includes("private upstream"), false);
    failNodes = false;
    await nodes.getByRole("button", { name: "Повторить загрузку статистики", exact: true }).click();
    await nodes.getByRole("alert").waitFor({ state: "detached" });
    holdNodePeriod = null;
    await nodes.getByLabel("Период статистики").selectOption("1");
    await nodes.locator("tbody tr").nth(99).waitFor();
    holdNodeCursor = true;
    await nodes.getByRole("button", { name: "Показать ещё ноды", exact: true }).click();
    await until(() => heldStats.length === 1, "held older page was not requested");
    await nodes.getByLabel("Период статистики").selectOption("7");
    await nodes.getByText("7 ГиБ", { exact: true }).waitFor();
    await json(heldStats.shift(), [usageRow(999)]);
    await usagePage.waitForTimeout(100);
    assert.equal(await nodes.getByText("Нода #999", { exact: true }).count(), 0, "late pagination cannot cross a period reset");
    assert.equal(await nodes.locator("tbody tr").count(), 3);
    holdNodeCursor = false;
    for (const width of [1280, 390]) {
      await usagePage.setViewportSize({ width, height: 1000 });
      const dimensions = await nodes.evaluate((element) => ({ width: element.getBoundingClientRect().width, viewport: innerWidth, scrollWidth: document.documentElement.scrollWidth }));
      assert.ok(dimensions.width <= width && dimensions.scrollWidth <= width, `usage ${width}px overflow ${JSON.stringify(dimensions)}`);
      await nodes.getByLabel("Период статистики").focus();
      assert.equal(await nodes.getByLabel("Период статистики").evaluate((element) => element.labels.length), 1);
      for (const control of await nodes.locator("button,select").all()) {
        if (await control.isDisabled()) continue;
        await control.focus();
        await usagePage.keyboard.press("Tab"); await usagePage.keyboard.press("Shift+Tab");
        assert.equal(await control.evaluate((element) => document.activeElement === element && element.matches(":focus-visible") && getComputedStyle(element).outlineStyle !== "none"), true, "usage control has visible keyboard focus");
      }
      await nodes.screenshot({ path: path.join(output, `usage-nodes-${width}.png`), style: ".vpn-admin-nav { visibility: hidden !important; }" });
      await nodes.evaluate((root) => {
        const sizes = [root, ...root.querySelectorAll("*")].filter((element) => element instanceof HTMLElement).map((element) => ({ element, size: parseFloat(getComputedStyle(element).fontSize), line: parseFloat(getComputedStyle(element).lineHeight) }));
        for (const { element, size, line } of sizes) {
          element.style.fontSize = `${size * 2}px`;
          if (Number.isFinite(line)) element.style.lineHeight = `${line * 2}px`;
        }
      });
      assert.equal(await usagePage.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, "usage 200% text does not overflow page");
      await nodes.screenshot({ path: path.join(output, `usage-nodes-${width}-200.png`), style: ".vpn-admin-nav { visibility: hidden !important; }" });
      await nodes.evaluate((root) => [root, ...root.querySelectorAll("*")].forEach((element) => { element.style.removeProperty("font-size"); element.style.removeProperty("line-height"); }));
    }
    await usagePage.locator('.vpn-admin-nav a[href="#vpn/customers"]').click();
    const customerUsage = usagePage.locator('[data-vpn-usage="customer"]');
    await customerUsage.getByRole("heading", { name: "Трафик клиента", exact: true }).waitFor();
    await customerUsage.getByText("Телефон: две страны", { exact: true }).waitFor();
    assert.equal(await customerUsage.locator("tbody tr").count(), 1, "child countries remain one logical profile");
    assert.match(await customerUsage.innerText(), /3 ГиБ/);
    assert.match(await customerUsage.innerText(), /4 ГиБ/);
    await customerUsage.screenshot({ path: path.join(output, "usage-customer-390.png") });
    holdCustomer = 1;
    await customerUsage.getByRole("button", { name: "Обновить статистику", exact: true }).click();
    await until(() => heldStats.length === 1, "old customer refresh was not held");
    await usagePage.getByRole("button", { name: /Борис/, exact: false }).first().click();
    await customerUsage.getByText("Ноутбук Бориса", { exact: true }).waitFor();
    await json(heldStats.shift(), { ...customerStats(1, 1), upload_bytes: 999 });
    await usagePage.waitForTimeout(100);
    assert.equal(await customerUsage.getByText("Телефон: две страны", { exact: true }).count(), 0, "late previous-customer reply is ignored");
    failNodes = true;
    await usagePage.locator('.vpn-admin-nav a[href="#vpn/nodes"]').click();
    await nodes.getByRole("alert").waitFor();
    assert.equal(await nodes.locator("tbody tr").count(), 0, "initial load failure is not rendered as measured zeros");
    failNodes = false;
    await nodes.getByRole("button", { name: "Повторить загрузку статистики", exact: true }).click();
    await nodes.locator("tbody tr").nth(99).waitFor();
    await usagePage.locator('.vpn-admin-nav a[href="#vpn/customers"]').click();
    await customerUsage.getByText("Ноутбук Бориса", { exact: true }).waitFor();
    holdCustomer = 2;
    await customerUsage.getByLabel("Период статистики").selectOption("30");
    await until(() => heldStats.length === 1, "logout usage reply was not held");
    await usagePage.getByRole("button", { name: "Выйти", exact: true }).click();
    await usagePage.getByRole("heading", { name: "Вход", exact: true }).waitFor();
    await json(heldStats.shift(), customerStats(2, 30));
    await usagePage.waitForTimeout(100);
    assert.equal(await usagePage.locator("[data-vpn-usage]").count(), 0, "late usage after logout cannot restore private data");
    assert.deepEqual(usageErrors, []);
    await usagePage.close();
    console.log("PASS: measured node/customer usage with coverage, exact zero, cursor and logical profiles; synthetic HTTP only");
  } else {
  if (!process.argv.includes("--profile-create-only") && !process.argv.includes("--invitations-only")) {
    const operations = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    operations.setDefaultTimeout(10000);
    operations.on("pageerror", (error) => errors.push(error.message));
    let capacity = { ...endpointCapacity };
    let includeCapacity = true;
    let readinessCalls = 0;
    let operationSubscriptions = structuredClone(subscriptions);
    let usage = { subscription_id: 11, devices_used: 2, max_devices: 3 };
    let usageFailure = false;
    let holdUsage = false;
    let failListRefresh = false;
    const usageCalls = [];
    const heldUsage = [];
    const publications = [];
    const heldPublications = [];
    const capacityRequests = [];
    const heldCapacities = [];
    let holdCapacity = false;
    let releaseProfile;
    const cappedKeys = Array.from({ length: 1000 }, (_, index) => ({ ...fixtureKeys()[0], id: 21 + index, config_uri: null }));
    await operations.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const pathname = url.pathname;
      if (/\/subscriptions\/\d+\/device-usage$/.test(pathname)) {
        const id = Number(pathname.split("/").at(-2));
        usageCalls.push(id);
        if (holdUsage && id === 11) return new Promise((resolve) => heldUsage.push(async (body) => { await json(route, body); resolve(); }));
        return usageFailure && id === 11 ? json(route, { detail: "Unavailable" }, 503)
          : json(route, id === 11 ? usage : { subscription_id: id, devices_used: 1, max_devices: 3 });
      }
      if (pathname === "/api/control/vpn/access-keys") {
        if (request.method() === "GET") return failListRefresh ? json(route, { detail: "Список не обновился" }, 503) : json(route, cappedKeys);
        return new Promise((resolve) => { releaseProfile = async (status = 200) => {
          usage = { ...usage, devices_used: 3 };
          await json(route, status === 200 ? { ...fixtureKeys()[0], id: 2001, display_name: "Новое устройство", status: "pending_sync", config_uri: null }
            : { detail: "VPN device limit reached" }, status);
          resolve();
        }; });
      }
      if (pathname === "/api/control/vpn/access-keys/21/revoke") {
        usage = { ...usage, devices_used: 2 };
        return json(route, { ...cappedKeys[0], status: "revoked" });
      }
      if (request.method() === "PATCH" && pathname === "/api/control/vpn/subscriptions/11") {
        const payload = request.postDataJSON();
        usage = { ...usage, max_devices: payload.max_devices ?? usage.max_devices };
        operationSubscriptions = operationSubscriptions.map((item) => item.id === 11 ? { ...item, ...payload } : item);
        return json(route, operationSubscriptions[0]);
      }
      if (pathname === "/api/control/vpn/lifecycle/run") return json(route, {});
      if (pathname === "/api/control/vpn/subscriptions") return json(route, operationSubscriptions);
      if (pathname === "/api/control/vpn/endpoints/capacity") return json(route, includeCapacity ? [capacity] : []);
      if (pathname === "/api/control/vpn/release-readiness") {
        readinessCalls += 1;
        return json(route, { ready: true, checked_at: stamp, release_id: null, checks: [] });
      }
      if (request.method() === "PATCH" && pathname.endsWith("/publication")) {
        publications.push(request.postDataJSON());
        return new Promise((resolve) => heldPublications.push(async (body, status = 200) => { await json(route, body, status); resolve(); }));
      }
      if (request.method() === "PATCH" && pathname.endsWith("/capacity")) {
        const payload = request.postDataJSON();
        capacityRequests.push(payload);
        assert.deepEqual(Object.keys(payload).sort(), ["capacity_warning_percent", "max_active_profiles"]);
        if (holdCapacity) return new Promise((resolve) => heldCapacities.push(async (body, status = 200) => {
          await json(route, body, status); resolve();
        }));
        capacity = { ...capacity, ...payload };
        return json(route, capacity);
      }
      if (pathname === "/api/auth/logout") return json(route, {});
      if (pathname === "/api/auth/login") return json(route, { has_feature_access: true,
        user: { id: 99, username: "other-admin", role: "owner", status: "active", language: "ru", ...timestamps } });
      return handleApiRoute(route);
    });
    const subscription = operations.locator(".vpn-subscription-card").filter({ hasText: "Подписка #11 ·" });
    const refresh = async () => {
      const response = operations.waitForResponse((response) => new URL(response.url()).pathname === "/api/control/vpn/subscriptions");
      await operations.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
      await response;
      await operations.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    const openProfile = async () => {
      await subscription.getByRole("button", { name: "Выдать новый профиль", exact: true }).click();
      return subscription.locator("form").filter({ has: operations.getByRole("heading", { name: "Новый VPN‑профиль для подписки #11", exact: true }) });
    };
    try {
      await operations.goto(`${appUrl}#vpn/customers`);
      await subscription.getByText("2 из 3", { exact: true }).waitFor();
      assert.equal(await subscription.locator(".vpn-key-card").count(), 1000);
      assert.deepEqual(usageCalls, [11], "usage must load only for the selected customer's subscriptions");
      usageFailure = true;
      await refresh();
      await subscription.getByText("Не удалось проверить занятые места", { exact: true }).first().waitFor();
      let profile = await openProfile();
      assert.equal(await profile.getByRole("button", { name: "Выдать профиль", exact: true }).isDisabled(), true);
      assert.equal(await profile.getByRole("button", { name: "Изменить лимит подписки", exact: true }).isEnabled(), true);
      usageFailure = false;
      await profile.getByRole("button", { name: "Проверить ещё раз", exact: true }).click();
      await profile.getByText("Занято мест для устройств: 2 из 3", { exact: true }).waitFor();
      assert.equal(await profile.getByRole("button", { name: "Выдать профиль", exact: true }).isEnabled(), true);
      holdUsage = true;
      await refresh();
      await until(() => heldUsage.length > 0, "old customer usage was not held");
      await operations.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
      await operations.locator(".vpn-subscription-card").first().getByText("1 из 3", { exact: true }).waitFor();
      holdUsage = false;
      while (heldUsage.length) await heldUsage.shift()({ ...usage, devices_used: 3 });
      assert.equal(await operations.getByText("3 из 3", { exact: true }).count(), 0, "old customer responses must be ignored");
      await operations.locator(".vpn-customer-row").filter({ hasText: "Анна" }).click();
      await subscription.getByText("2 из 3", { exact: true }).waitFor();
      profile = await openProfile();
      await profile.getByRole("textbox", { name: "Название", exact: true }).fill("Новое устройство");
      const callsBeforeCreate = usageCalls.length;
      await profile.getByRole("button", { name: "Выдать профиль", exact: true }).click();
      await until(() => Boolean(releaseProfile), "device create was not held");
      await refresh();
      await profile.getByText("Занято мест для устройств: 2 из 3", { exact: true }).waitFor();
      failListRefresh = true;
      await releaseProfile();
      await subscription.getByText("3 из 3", { exact: true }).waitFor();
      assert.ok(usageCalls.length > callsBeforeCreate, "saved creation must refresh authoritative usage even if list refresh fails");
      await subscription.getByText("Новое устройство", { exact: true }).waitFor();
      profile = await openProfile();
      assert.equal(await profile.getByRole("button", { name: "Выдать профиль", exact: true }).isDisabled(), true);
      await profile.getByRole("button", { name: "Отмена", exact: true }).click();
      failListRefresh = false;
      const beforeRevoke = usageCalls.length;
      operations.once("dialog", (dialog) => dialog.accept());
      await subscription.locator(".vpn-key-card").first().getByRole("button", { name: "Отозвать и сохранить", exact: true }).click();
      await subscription.getByText("2 из 3", { exact: true }).waitFor();
      assert.ok(usageCalls.length > beforeRevoke, "revoke must refresh usage");
      await subscription.getByRole("button", { name: "Изменить", exact: true }).click();
      const edit = subscription.locator("form").filter({ has: operations.getByRole("heading", { name: "Изменить подписку #11", exact: true }) });
      await edit.getByLabel("Устройств", { exact: true }).fill("4");
      await edit.getByRole("button", { name: "Сохранить подписку", exact: true }).click();
      await subscription.getByText("2 из 4", { exact: true }).waitFor();
      const beforeExtend = usageCalls.length;
      operations.once("dialog", (dialog) => dialog.accept());
      await subscription.getByRole("button", { name: "+7 дней", exact: true }).click();
      await until(() => usageCalls.length > beforeExtend, "extending a subscription must refresh usage");
      await subscription.getByText("2 из 4", { exact: true }).waitFor();
      usage = { ...usage, max_devices: 3 };
      await refresh();
      profile = await openProfile();
      await profile.getByRole("textbox", { name: "Название", exact: true }).fill("Конкурирующая выдача");
      releaseProfile = null;
      await profile.getByRole("button", { name: "Выдать профиль", exact: true }).click();
      await until(() => Boolean(releaseProfile), "racing create was not held");
      await releaseProfile(409);
      await profile.getByText("Занято мест для устройств: 3 из 3", { exact: true }).waitFor();
      assert.equal(await profile.getByRole("button", { name: "Выдать профиль", exact: true }).isDisabled(), true,
        "a backend conflict remains authoritative and refreshes usage");

      await operations.locator(".vpn-admin-nav").getByRole("link", { name: "Обзор", exact: true }).click();
      const card = operations.locator(".vpn-capacity-card");
      const publication = card.locator("form").filter({ has: operations.getByRole("button", { name: "Сохранить публикацию", exact: true }) });
      for (const name of ["", "Сервер\u200B"]) {
        await publication.getByLabel("Название для клиентов", { exact: true }).fill(name);
        await publication.getByLabel("Код страны", { exact: true }).fill("DE");
        await publication.evaluate((element) => element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
        await publication.getByRole("alert").waitFor();
        assert.equal(publications.length, 0, "empty and invisible names must not be submitted");
      }
      await publication.getByLabel("Название для клиентов", { exact: true }).fill("Германия");
      await publication.getByLabel("Код страны", { exact: true }).fill("ДЕ");
      await publication.evaluate((element) => element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true })));
      await publication.getByRole("alert").waitFor();
      assert.equal(publications.length, 0, "country code must be two ASCII letters");
      await publication.getByLabel("Код страны", { exact: true }).fill("de");
      await publication.getByLabel("Показывать клиентам", { exact: true }).check();
      await publication.evaluate((element) => {
        element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
        element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      });
      await until(() => heldPublications.length === 1, "publication was not held");
      assert.deepEqual(publications, [{ is_published: true, public_name: "Германия", country_code: "DE" }]);
      capacity = { ...capacity, ...publications[0] };
      await heldPublications.shift()(capacity);
      await publication.getByRole("status").waitFor();
      assert.equal(await card.locator(".status").textContent(), "ready", "publication must not alter health");
      await publication.getByLabel("Название для клиентов", { exact: true }).fill("Черновик названия");
      await card.getByRole("spinbutton", { name: /Лимит профилей/ }).fill("30");
      await card.getByRole("button", { name: "Сохранить лимиты", exact: true }).click();
      await card.getByText("Лимиты сохранены.", { exact: true }).waitFor();
      assert.equal(publications.length, 1, "saving capacity must not publish");
      assert.equal(await publication.getByLabel("Название для клиентов", { exact: true }).inputValue(), "Черновик названия",
        "saving capacity must preserve the separate publication draft");
      await publication.getByLabel("Название для клиентов", { exact: true }).fill("Старый запрос");
      await publication.getByRole("button", { name: "Сохранить публикацию", exact: true }).click();
      await until(() => heldPublications.length === 1, "stale publication was not held");
      capacity = { ...capacity, worker_id: 42, public_name: "Новый сервер", country_code: "NL", status: "unhealthy" };
      await refresh();
      await heldPublications.shift()({ ...capacity, worker_id: 41, public_name: "Старый запрос", country_code: "DE", status: "ready" });
      await operations.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await publication.getByLabel("Название для клиентов", { exact: true }).inputValue(), "Новый сервер", "old row responses must not replace fresh metadata");
      assert.equal(await card.locator(".status").textContent(), "unhealthy");
      await card.getByText("Точка подключения #7 · VPN‑нода #42", { exact: true }).waitFor();
      await publication.getByRole("button", { name: "Сохранить публикацию", exact: true }).click();
      await until(() => heldPublications.length === 1, "removed-row publication was not held");
      includeCapacity = false;
      await refresh();
      const readinessBeforeRemovedReply = readinessCalls;
      await heldPublications.shift()(capacity);
      await operations.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await card.count(), 0, "a removed endpoint must not return after an old publication reply");
      assert.equal(readinessCalls, readinessBeforeRemovedReply, "removed rows must not call the old onUpdated");
      includeCapacity = true;
      await refresh();
      await publication.getByRole("button", { name: "Сохранить публикацию", exact: true }).click();
      await until(() => heldPublications.length === 1, "old-session publication was not held");
      await operations.getByRole("button", { name: "Выйти", exact: true }).click();
      capacity = { ...capacity, public_name: "Сервер новой сессии" };
      const login = operations.locator(".auth-panel").filter({ has: operations.getByRole("heading", { name: "Вход", exact: true }) });
      await login.getByRole("textbox", { name: "Логин", exact: true }).fill("other-admin");
      await login.getByLabel("Пароль", { exact: true }).fill("fixture-password");
      await login.getByRole("button", { name: "Войти", exact: true }).click();
      await publication.getByLabel("Название для клиентов", { exact: true }).waitFor();
      await heldPublications.shift()({ ...capacity, public_name: "Ответ старой сессии" });
      await operations.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await publication.getByLabel("Название для клиентов", { exact: true }).inputValue(), "Сервер новой сессии");
      assert.equal(await operations.locator(".toast").innerText(), "Вход выполнен");
      await publication.getByLabel("Показывать клиентам", { exact: true }).uncheck();
      await publication.getByRole("button", { name: "Сохранить публикацию", exact: true }).click();
      await until(() => heldPublications.length === 1, "hide failure was not held");
      await heldPublications.shift()({ detail: "Не удалось сохранить публикацию" }, 503);
      await publication.getByRole("alert").waitFor();
      assert.equal(await publication.getByLabel("Показывать клиентам", { exact: true }).isChecked(), false,
        "publication errors must preserve the draft for retry");
      await publication.getByRole("button", { name: "Сохранить публикацию", exact: true }).click();
      await until(() => heldPublications.length === 1, "hide retry was not held");
      capacity = { ...capacity, is_published: false };
      await heldPublications.shift()(capacity);
      await publication.getByRole("status").waitFor();
      assert.equal(await publication.getByLabel("Показывать клиентам", { exact: true }).isChecked(), false);
      assert.equal(await card.locator(".status").textContent(), "unhealthy", "hiding must not alter health");
      for (const action of ["publication", "capacity"]) {
        for (const outcome of ["success", "error"]) {
          const form = card.locator("form").nth(action === "capacity" ? 0 : 1);
          const captured = { ...capacity };
          const patch = action === "capacity"
            ? { max_active_profiles: capacity.max_active_profiles + 1, capacity_warning_percent: 75 }
            : { is_published: !capacity.is_published, public_name: `Сервер ${outcome}`, country_code: "DE" };
          if (action === "capacity") {
            holdCapacity = true;
            await form.getByRole("spinbutton", { name: /Лимит профилей/ }).fill(String(patch.max_active_profiles));
            await form.getByRole("spinbutton", { name: /Порог предупреждения/ }).fill(String(patch.capacity_warning_percent));
          } else {
            await form.getByLabel("Название для клиентов", { exact: true }).fill(patch.public_name);
            await form.getByLabel("Код страны", { exact: true }).fill(patch.country_code);
            await form.getByLabel("Показывать клиентам", { exact: true }).setChecked(patch.is_published);
          }
          const held = action === "capacity" ? heldCapacities : heldPublications;
          const requests = action === "capacity" ? capacityRequests : publications;
          const before = requests.length;
          await form.getByRole("button", { name: action === "capacity" ? "Сохранить лимиты" : "Сохранить публикацию", exact: true }).click();
          await until(() => held.length === 1, `${action}/${outcome} response was not held`);
          await refresh();
          assert.equal(await card.getByRole("checkbox").isDisabled(), true,
            `${action}/${outcome}: identical-row refresh must retain the mutation lock`);
          await form.evaluate((element) => {
            element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
            element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
          });
          assert.equal(requests.length, before + 1, `${action}/${outcome}: refresh must not allow a second mutation`);
          capacity = { ...capacity, status: "maintenance", occupied_profiles: capacity.occupied_profiles + 1,
            ...(action === "publication" ? { max_active_profiles: capacity.max_active_profiles + 5 }
              : { public_name: "Обновлено другим администратором", country_code: "NL" }) };
          await refresh();
          assert.equal(await card.getByRole("checkbox").isDisabled(), true,
            `${action}/${outcome}: health refresh must retain the mutation lock`);
          if (outcome === "success") capacity = { ...capacity, ...patch };
          await held.shift()(outcome === "success" ? { ...captured, ...patch }
            : { detail: "Сохранение временно недоступно" }, outcome === "success" ? 200 : 503);
          await form.getByRole(outcome === "success" ? "status" : "alert").waitFor();
          assert.equal(await card.getByRole("checkbox").isDisabled(), false);
          assert.equal(await card.locator(".status").textContent(), "maintenance",
            `${action}/${outcome}: late replies must preserve newer health`);
          assert.equal(await card.getByRole("spinbutton", { name: /Лимит профилей/ }).inputValue(),
            String(outcome === "error" && action === "capacity" ? patch.max_active_profiles : capacity.max_active_profiles));
          assert.equal(await publication.getByLabel("Название для клиентов", { exact: true }).inputValue(),
            outcome === "error" && action === "publication" ? patch.public_name : capacity.public_name);
          holdCapacity = false;
        }
      }
      await publication.getByLabel("Код страны", { exact: true }).focus();
      await operations.keyboard.press("Tab");
      assert.equal(await publication.getByLabel("Показывать клиентам", { exact: true }).evaluate((element) =>
        element === document.activeElement && element.matches(":focus-visible")), true);
      assert.ok(await publication.getByLabel("Показывать клиентам", { exact: true }).evaluate((element) =>
        element.closest("label").getBoundingClientRect().height >= 44), "publication checkbox needs a usable touch target");
      const inspectPanel = async (target, prefix) => {
      for (const width of [1280, 390]) {
        await operations.setViewportSize({ width, height: 1000 });
        assert.equal(await target.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), true);
        await target.screenshot({ path: path.join(output, `${prefix}-${width}.png`), style: ".vpn-admin-nav { visibility:hidden !important; }" });
        await target.evaluate((root) => {
          const sizes = [root, ...root.querySelectorAll("*")].map((element) => ({ element,
            font: parseFloat(getComputedStyle(element).fontSize), line: parseFloat(getComputedStyle(element).lineHeight) }));
          sizes.forEach(({ element, font, line }) => {
            element.style.setProperty("font-size", `${font * 2}px`, "important");
            if (Number.isFinite(line)) element.style.setProperty("line-height", `${line * 2}px`, "important");
          });
        });
        assert.equal(await target.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), true, `${width}px ${prefix} must reflow at 200% text`);
        const status = target.locator(".status");
        if (await status.count()) assert.equal(await status.evaluate((element) => {
          const range = document.createRange(); range.selectNodeContents(element); return range.getClientRects().length;
        }), 1, "the endpoint status must stay readable as one word");
        await target.screenshot({ path: path.join(output, `${prefix}-${width}-text-200.png`), style: ".vpn-admin-nav { visibility:hidden !important; }" });
        await target.evaluate((root) => [root, ...root.querySelectorAll("*")].forEach((element) => {
          element.style.removeProperty("font-size"); element.style.removeProperty("line-height");
        }));
      }
      };
      await inspectPanel(card, "admin-publication");
      await operations.locator(".vpn-admin-nav").getByRole("link", { name: "Клиенты", exact: true }).click();
      await subscription.getByText("3 из 3", { exact: true }).waitFor();
      profile = await openProfile();
      await inspectPanel(profile, "admin-device-usage");
      assert.deepEqual(errors, []);
      console.log("PASS: authoritative logical-device usage with 1000-key cap, failure/retry/customer epochs/create/revoke/edit; separate publication validation/duplicate/stale/health/capacity; synthetic HTTP only");
    } finally { await operations.close(); }
  }

  if (!process.argv.includes("--profile-create-only") && !process.argv.includes("--admin-operations-only")) {
    const invitationPage = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    invitationPage.setDefaultTimeout(10000);
    invitationPage.on("pageerror", (error) => errors.push(error.message));
    const invitationLogs = [];
    invitationPage.on("console", (message) => invitationLogs.push(message.text()));
    await invitationPage.addInitScript(() => {
      window.__invitationPosts = 0;
      window.__copies = [];
      const originalFetch = window.fetch;
      window.fetch = (input, init) => {
        if (String(input).endsWith("/api/control/vpn/friend-invitations") && init?.method === "POST") {
          window.__invitationPosts += 1;
        }
        return originalFetch(input, init);
      };
      Object.defineProperty(navigator, "clipboard", { configurable: true,
        value: { writeText: async (value) => { window.__copies.push(value); } } });
    });
    const invitation = (slot, state = "unused") => ({
      slot, invite_state: state, telegram_user_id: null, telegram_username: null,
      display_name: null, subscription_expires_at: null, provisioning_error_code: null,
      can_rotate: state === "unused", can_retry: false, can_disable: state === "active",
    });
    let firstPage = [];
    let failRefresh = false;
    let releaseIssue;
    let releaseRotation;
    let holdOlder = false;
    let olderFailure = false;
    let olderRows = [];
    const heldOlder = [];
    const cursors = [];
    let disableCalls = 0;
    let firstPageCalls = 0;
    let authUserId = 2;
    let holdMutation = false;
    const heldMutations = [];
    await invitationPage.route("**/api/**", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (holdMutation && request.method() === "POST" && url.pathname.startsWith("/api/control/vpn/friend-invitations")) {
        return new Promise((resolve) => heldMutations.push(async (body, status = 200) => {
          await json(route, body, status); resolve();
        }));
      }
      if (url.pathname === "/api/auth/login") return json(route, { has_feature_access: true,
        user: { id: authUserId++, username: "next-synthetic-admin", role: "owner", status: "active", language: "ru", ...timestamps } });
      if (url.pathname === "/api/control/vpn/friend-invitations" && request.method() === "POST") {
        return new Promise((resolve) => { releaseIssue = async (slot) => {
          await json(route, { invitation: invitation(slot), invite_link: `https://t.me/synthetic_bot?start=i_fixture_${slot}` });
          resolve();
        }; });
      }
      if (url.pathname.endsWith("/rotate")) return new Promise((resolve) => {
        releaseRotation = async () => { await json(route, { detail: "Не удалось обновить приглашение" }, 503); resolve(); };
      });
      if (url.pathname === "/api/auth/logout") return json(route, { detail: "Вы вышли" });
      if (url.pathname.endsWith("/disable")) {
        disableCalls += 1;
        return json(route, invitation(Number(url.pathname.split("/").at(-2)), "disabled"));
      }
      if (url.pathname === "/api/control/vpn/friend-invitations") {
        const cursor = url.searchParams.get("before_slot");
        if (!cursor) { firstPageCalls += 1; return json(route, firstPage); }
        cursors.push(Number(cursor));
        if (holdOlder) return new Promise((resolve) => heldOlder.push(async (rows) => {
          await json(route, rows); resolve();
        }));
        return olderFailure ? json(route, { detail: "Приглашения временно недоступны" }, 503) : json(route, olderRows);
      }
      if (failRefresh && url.pathname === "/api/control/vpn/access-keys") {
        return json(route, { detail: "Не удалось обновить список" }, 503);
      }
      return handleApiRoute(route);
    });
    const section = invitationPage.locator(".vpn-friend-invitations");
    const rows = section.locator(".vpn-friend-invitation-row");
    const create = section.getByRole("button", { name: "Создать приглашение", exact: true });
    const older = section.getByRole("button", { name: "Загрузить ещё", exact: true });
    const refresh = async () => {
      const response = invitationPage.waitForResponse((response) => new URL(response.url()).pathname === "/api/control/vpn/friend-invitations"
        && !new URL(response.url()).search);
      await invitationPage.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
      await response;
      await invitationPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    };
    const signInAsNextUser = async () => {
      const login = invitationPage.locator(".auth-panel").filter({ has: invitationPage.getByRole("heading", { name: "Вход", exact: true }) });
      await login.getByRole("textbox", { name: "Логин", exact: true }).fill("synthetic-admin");
      await login.getByLabel("Пароль", { exact: true }).fill("fixture-password");
      await login.getByRole("button", { name: "Войти", exact: true }).click();
      await rows.filter({ hasText: `#${firstPage[0].slot}` }).waitFor();
    };
    try {
      await invitationPage.goto(`${appUrl}#vpn/customers`);
      await section.getByRole("heading", { name: "Приглашения", exact: true }).waitFor();
      assert.equal(await rows.count(), 0);
      assert.equal(await create.isEnabled(), true, "empty lists must allow creation");
      const postCount = await create.evaluate((element) => { element.click(); element.click(); return window.__invitationPosts; });
      assert.equal(postCount, 1, "two synchronous clicks must issue only one invitation POST");
      await until(() => Boolean(releaseIssue), "invitation POST was not held");
      failRefresh = true;
      await releaseIssue(301);
      await section.getByRole("button", { name: "Копировать ссылку", exact: true }).waitFor();
      await invitationPage.locator(".toast").filter({ hasText: "Приглашение создано, но список не обновился" }).waitFor();
      assert.equal(await rows.count(), 1, "successful issue must merge its safe view before a failed refresh");
      await section.getByRole("button", { name: "Копировать ссылку", exact: true }).click();
      assert.deepEqual(await invitationPage.evaluate(() => window.__copies), ["https://t.me/synthetic_bot?start=i_fixture_301"]);
      assert.equal((await invitationPage.locator(".toast").innerText()).includes("i_fixture_"), false);
      failRefresh = false;
      firstPage = [invitation(501)];
      await refresh();
      assert.equal(await section.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 1,
        "a link absent from the first page must remain available");
      await rows.filter({ hasText: "#301" }).getByRole("button", { name: "Обновить ссылку", exact: true }).click();
      await until(() => Boolean(releaseRotation), "rotation was not held");
      assert.equal(await section.locator(".vpn-friend-invitation-link").count(), 0, "rotation must forget the old link before its reply");
      await releaseRotation();
      await invitationPage.locator(".toast").filter({ hasText: "Не удалось обновить приглашение" }).waitFor();
      assert.equal(await section.locator(".vpn-friend-invitation-link").count(), 0, "failed rotation must not restore the old link");
      releaseIssue = null;
      await create.click();
      await until(() => Boolean(releaseIssue), "replacement invitation POST was not held");
      failRefresh = true;
      await releaseIssue(302);
      await section.getByRole("button", { name: "Копировать ссылку", exact: true }).waitFor();
      failRefresh = false;
      firstPage = [invitation(501), invitation(302, "active")];
      await refresh();
      assert.equal(await section.locator(".vpn-friend-invitation-link").count(), 0, "redeemed state must remove the link");
      invitationPage.once("dialog", (dialog) => dialog.dismiss());
      await section.getByRole("button", { name: "Отключить", exact: true }).click();
      assert.equal(disableCalls, 0, "dismissed confirmation must not disable access");

      firstPage = Array.from({ length: 11 }, (_, index) => invitation(600 - index));
      await refresh();
      assert.equal(await rows.count(), 11);
      assert.equal(await create.isEnabled(), true, "eleven rows must allow more creation");
      releaseIssue = null;
      await create.click();
      await until(() => Boolean(releaseIssue), "twelfth invitation POST was not held");
      failRefresh = true;
      await releaseIssue(601);
      await section.getByRole("button", { name: "Копировать ссылку", exact: true }).waitFor();
      failRefresh = false;
      firstPage = [invitation(601, "disabled"), ...firstPage];
      await refresh();
      assert.equal(await section.locator(".vpn-friend-invitation-link").count(), 0, "revoked state must remove the link");

      firstPage = [];
      await refresh();
      releaseIssue = null;
      await create.click();
      await until(() => Boolean(releaseIssue), "old retained invitation POST was not held");
      failRefresh = true;
      await releaseIssue(1);
      await section.getByRole("button", { name: "Копировать ссылку", exact: true }).waitFor();
      failRefresh = false;
      firstPage = Array.from({ length: 100 }, (_, index) => invitation(200 - index));
      await refresh();
      assert.equal(await rows.count(), 101, "a retained older link stays visible beside the first page");
      olderRows = Array.from({ length: 99 }, (_, index) => invitation(100 - index));
      await older.click();
      await rows.filter({ hasText: "#2" }).last().waitFor();
      assert.deepEqual(cursors, [101], "an isolated retained ID must not advance the contiguous server cursor");
      assert.equal(await rows.count(), 200, "retaining ID1 must not skip IDs100..2");
      assert.equal(await section.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 1);
      cursors.length = 0;

      firstPage = Array.from({ length: 100 }, (_, index) => invitation(300 - index));
      await invitationPage.reload();
      await older.waitFor();
      holdOlder = true;
      await older.evaluate((element) => { element.click(); element.click(); });
      await until(() => heldOlder.length === 1, "older page was not requested");
      assert.deepEqual(cursors, [201], "older page uses the lowest ID and prevents duplicate requests");
      await heldOlder.shift()([invitation(201, "disabled"), ...Array.from({ length: 99 }, (_, index) => invitation(200 - index))]);
      await rows.filter({ hasText: "#102" }).waitFor();
      assert.equal(await rows.count(), 199, "page merge must deduplicate IDs");
      holdOlder = false;
      olderFailure = true;
      await older.click();
      await section.getByRole("alert").waitFor();
      assert.equal(await rows.count(), 199, "load failure must retain rows");
      olderFailure = false;
      olderRows = [invitation(101), invitation(100)];
      await older.click();
      await rows.filter({ hasText: "#100" }).waitFor();
      assert.equal(await older.count(), 0, "a short page ends pagination");
      const displayed = await section.locator(".vpn-friend-invitation-slot").allTextContents();
      assert.deepEqual(displayed.map((text) => Number(text.slice(1))), Array.from({ length: 201 }, (_, index) => 300 - index));

      await refresh();
      holdOlder = true;
      await older.click();
      await until(() => heldOlder.length === 1, "stale page was not held");
      firstPage = Array.from({ length: 100 }, (_, index) => invitation(800 - index));
      await refresh();
      await heldOlder.shift()([invitation(200)]);
      await invitationPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert.equal(await rows.count(), 100, "a stale page after reset must be ignored");
      assert.equal(await rows.filter({ hasText: "#200" }).count(), 0);
      assert.equal(await older.isEnabled(), true, "a stale response must not block current pagination");

      await older.click();
      await until(() => heldOlder.length === 1, "session-change page was not held");
      await invitationPage.getByRole("button", { name: "Выйти", exact: true }).click();
      await section.waitFor({ state: "detached" });
      await heldOlder.shift()([invitation(700)]);
      assert.equal(await section.count(), 0, "a page completed after logout must not restore the workspace");
      firstPage = [invitation(1500)];
      await signInAsNextUser();
      for (const action of ["issue", "rotate", "retry", "disable"]) {
        for (const outcome of ["success", "failure"]) {
          firstPage = [{ ...invitation(901, action === "retry" ? "failed" : action === "disable" ? "active" : "unused"), can_retry: action === "retry" }];
          await refresh();
          holdMutation = true;
          if (action === "disable") invitationPage.once("dialog", (dialog) => dialog.accept());
          const label = { issue: "Создать приглашение", rotate: "Обновить ссылку", retry: "Повторить выдачу", disable: "Отключить" }[action];
          await section.getByRole("button", { name: label, exact: true }).click();
          await until(() => heldMutations.length === 1, `${action} response was not held`);
          await invitationPage.getByRole("button", { name: "Выйти", exact: true }).click();
          await section.waitFor({ state: "detached" });
          firstPage = [invitation(1500)];
          await signInAsNextUser();
          const reloadsBeforeReply = firstPageCalls;
          const mutationReply = invitationPage.waitForResponse((response) => response.request().method() === "POST"
            && new URL(response.url()).pathname.startsWith("/api/control/vpn/friend-invitations"));
          const result = ["issue", "rotate"].includes(action)
            ? { invitation: invitation(902), invite_link: "https://t.me/synthetic_bot?start=i_late_fixture" }
            : invitation(901, "disabled");
          await heldMutations.shift()(outcome === "success" ? result : { detail: "Поздний ответ старой сессии" }, outcome === "success" ? 200 : 503);
          await mutationReply;
          await invitationPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
          assert.equal(firstPageCalls, reloadsBeforeReply, `${action}/${outcome}: an old session must not reload the new workspace`);
          assert.equal(await rows.count(), 1, `${action}/${outcome}: an old row must not enter the new workspace`);
          assert.equal(await section.locator(".vpn-friend-invitation-link").count(), 0, `${action}/${outcome}: an old secret must not enter the new workspace`);
          assert.equal(await invitationPage.locator(".toast").innerText(), "Вход выполнен", `${action}/${outcome}: old feedback must not overwrite the new session`);
          holdMutation = false;
        }
      }
      firstPage = [invitation(800), invitation(799, "active"), invitation(798, "disabled")];
      await refresh();
      await rows.first().waitFor();
      for (const width of [1280, 390]) {
        await invitationPage.setViewportSize({ width, height: 1000 });
        await section.scrollIntoViewIfNeeded();
        const bounds = await section.evaluate((element) => ({ width: element.clientWidth, content: element.scrollWidth,
          buttons: [...element.querySelectorAll("button")].filter((button) => button.getBoundingClientRect().height < 40).length }));
        assert.ok(bounds.content <= bounds.width + 1, `${width}px invitations must not overflow`);
        assert.equal(bounds.buttons, 0, `${width}px invitation actions must retain usable targets`);
        await section.screenshot({ path: path.join(output, `admin-invitations-${width}.png`),
          style: ".vpn-admin-nav { visibility: hidden !important; }" });
        await section.evaluate((root) => {
          const sizes = [root, ...root.querySelectorAll("*")].map((element) => ({ element,
            fontSize: parseFloat(getComputedStyle(element).fontSize), lineHeight: parseFloat(getComputedStyle(element).lineHeight) }));
          sizes.forEach(({ element, fontSize, lineHeight }) => {
            element.style.setProperty("font-size", `${fontSize * 2}px`, "important");
            if (Number.isFinite(lineHeight)) element.style.setProperty("line-height", `${lineHeight * 2}px`, "important");
          });
        });
        assert.equal(await section.evaluate((element) => element.scrollWidth <= element.clientWidth + 1), true,
          `${width}px invitations must reflow at 200% text`);
        await section.screenshot({ path: path.join(output, `admin-invitations-${width}-text-200.png`),
          style: ".vpn-admin-nav { visibility: hidden !important; }" });
        await section.evaluate((root) => [root, ...root.querySelectorAll("*")].forEach((element) => {
          element.style.removeProperty("font-size"); element.style.removeProperty("line-height");
        }));
      }
      assert.equal(invitationLogs.some((message) => message.includes("i_fixture_")), false);
      assert.deepEqual(await invitationPage.evaluate(() => Object.values(localStorage).concat(Object.values(sessionStorage))
        .filter((value) => value.includes("i_fixture_"))), []);
      assert.deepEqual(errors, []);
      console.log("PASS: mounted invitations empty/11+ creation, synchronous duplicate guard, failed-refresh link retention, rotation/redeemed/revoked cleanup, cursor merge/retry/end/stale-reset/logout, desktop/mobile/200% text; synthetic HTTP only");
    } finally {
      await invitationPage.close();
    }
  }

  if (!process.argv.includes("--invitations-only") && !process.argv.includes("--admin-operations-only")) {
  const creationPage = await browser.newPage();
  creationPage.setDefaultTimeout(10000);
  creationPage.on("pageerror", (error) => errors.push(error.message));
  await creationPage.route("**/api/**", handleApiRoute);
  await creationPage.addInitScript(() => {
    window.__profileCreateFetches = 0;
    const originalFetch = window.fetch;
    window.fetch = (input, init) => {
      if (String(input).endsWith("/api/control/vpn/access-keys") && init?.method === "POST") {
        window.__profileCreateFetches += 1;
      }
      return originalFetch(input, init);
    };
  });
  const creationSubscription = (id) => creationPage.locator(".vpn-subscription-card")
    .filter({ hasText: `Подписка #${id} ·` });
  const openCreationForm = async (id, name) => {
    const subscription = creationSubscription(id);
    await subscription.getByRole("button", { name: "Выдать новый профиль", exact: true }).click();
    const form = subscription.locator("form").filter({
      has: creationPage.getByRole("heading", { name: `Новый VPN‑профиль для подписки #${id}`, exact: true }),
    });
    await form.getByRole("textbox", { name: "Название", exact: true }).fill(name);
    return form;
  };
  const createdProfile = (name) => ({
    ...fixtureKeys()[0], id: 101, display_name: name, status: "pending_sync", config_uri: null,
    updated_at: nextVersion(),
  });
  try {
    await creationPage.goto(`${appUrl}#vpn/customers`);
    const form = await openCreationForm(11, "Сохранённый профиль");
    const synchronousCalls = await form.evaluate((element) => {
      element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      element.dispatchEvent(new Event("submit", { bubbles: true, cancelable: true }));
      return window.__profileCreateFetches;
    });
    assert.equal(synchronousCalls, 1, "two synchronous submits must start exactly one create request");
    await until(() => heldCreates.length === 1, "profile creation request was not held");
    assert.equal(createRequests.length, 1);
    assert.equal(await form.getByRole("button", { name: "Выдать профиль", exact: true }).isDisabled(), true);
    reloadFailure = true;
    const failedRefresh = creationPage.waitForResponse((response) =>
      new URL(response.url()).pathname === "/api/control/vpn/access-keys" && response.status() === 503);
    await heldCreates.shift()(createdProfile("Сохранённый профиль"));
    await failedRefresh;
    const acknowledgement = creationPage.locator(".toast").filter({ hasText: "Профиль сохранён" });
    await acknowledgement.waitFor();
    assert.match(await acknowledgement.innerText(), /Список не обновился[\s\S]*Обновите страницу/);
    assert.equal(await form.count(), 0, "acknowledged creation must close its original form even if refresh fails");
    await creationSubscription(11).getByText("Сохранённый профиль", { exact: true }).waitFor();
    await creationSubscription(11).getByText("2 из 3", { exact: true }).waitFor();
    const reopened = await openCreationForm(11, "Следующий профиль");
    await reopened.getByText("Занято мест для устройств: 2 из 3", { exact: true }).waitFor();
    assert.equal(await creationSubscription(11).locator(".vpn-key-card").count(), 2,
      "a failed GET must retain the returned profile and its reserved slot");
    assert.equal(createRequests.length, 1, "refresh failure must not retry creation");

    for (const outcome of ["success", "failure"]) {
      for (const destination of ["new-form", "other-customer"]) {
        keys = fixtureKeys();
        deviceUsages = fixtureUsages();
        reloadFailure = false;
        await creationPage.reload({ waitUntil: "domcontentloaded" });
        const original = await openCreationForm(11, `Поздний ответ: ${outcome}`);
        const requestsBefore = createRequests.length;
        await original.getByRole("button", { name: "Выдать профиль", exact: true }).click();
        await until(() => heldCreates.length === 1, "delayed creation was not held");
        assert.equal(createRequests.length, requestsBefore + 1);
        let currentSubscriptionId = 11;
        if (destination === "new-form") {
          await original.getByRole("button", { name: "Отмена", exact: true }).click();
        } else {
          await creationPage.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
          currentSubscriptionId = 12;
        }
        const draftName = `Новый черновик: ${outcome}, ${destination}`;
        const current = await openCreationForm(currentSubscriptionId, draftName);
        const completedRequest = creationPage.waitForResponse((response) =>
          new URL(response.url()).pathname === "/api/control/vpn/access-keys"
          && response.request().method() === "POST");
        if (outcome === "success") {
          reloadFailure = true;
          await heldCreates.shift()(createdProfile("Поздний сохранённый профиль"));
          await creationPage.locator(".toast").filter({ hasText: "Профиль сохранён для подписки #11" }).waitFor();
        } else {
          await heldCreates.shift()({ detail: "private_key=synthetic-secret-must-not-leak" }, 422);
        }
        await completedRequest;
        await creationPage.waitForFunction(() => {
          const submit = document.querySelector(".vpn-key-section .vpn-inline-form button[type=submit]");
          return submit && !submit.disabled;
        });
        assert.equal(await current.getByRole("textbox", { name: "Название", exact: true }).inputValue(), draftName,
          `${outcome}/${destination}: a late response must preserve the current form and its draft`);
        assert.equal(await current.getByRole("alert").count(), 0,
          `${outcome}/${destination}: old feedback must not overwrite a new form`);
        assert.equal((await creationPage.content()).includes("synthetic-secret-must-not-leak"), false);
        if (outcome === "success") {
          const expectedSlots = destination === "new-form" ? 2 : 1;
          await current.getByText(`Занято мест для устройств: ${expectedSlots} из 3`, { exact: true }).waitFor();
          if (destination === "other-customer") {
            assert.equal(await creationPage.getByText("Поздний сохранённый профиль", { exact: true }).count(), 0,
              "a profile returned for another customer must stay out of the current customer");
            await creationPage.locator(".vpn-customer-row").filter({ hasText: "Анна" }).click();
          }
          await creationSubscription(11).getByText("Поздний сохранённый профиль", { exact: true }).waitFor();
        }
        assert.equal(createRequests.length, requestsBefore + 1);
      }
    }
    assert.deepEqual(errors, []);
    console.log("PASS: profile create duplicate-submit/saved-with-failed-refresh/late-success-and-failure/new-form-and-other-customer, synthetic HTTP only");
  } finally {
    await creationPage.close();
    keys = fixtureKeys();
    deviceUsages = fixtureUsages();
    reloadFailure = false;
  }
  }

  if (!process.argv.includes("--profile-create-only") && !process.argv.includes("--invitations-only") && !process.argv.includes("--admin-operations-only")) {
  await page.goto(appUrl);
  assert.equal(await page.evaluate(() => window.location.hash), "");
  assert.match(await page.getByRole("button", { name: "домены", exact: true }).getAttribute("class"), /active-chip/);

  await page.getByRole("button", { name: "воркеры", exact: true }).click();
  const workerCard = page.locator("article.user-card").filter({ hasText: "Frankfurt 1" });
  const workerCardDelete = workerCard.getByRole("button", { name: "Удалить", exact: true });
  await workerCardDelete.click();
  const workerCardDialog = page.getByRole("dialog", { name: "Удалить VPN‑ноду «Frankfurt 1»?", exact: true });
  await workerCardDialog.getByText(
    "Она перестанет принимать новые профили. Активные профили будут обработаны по текущим правилам безопасного удаления.",
    { exact: true },
  ).waitFor();
  assert.equal(await workerCardDialog.evaluate((element) => element.tagName), "DIALOG");
  assert.equal(await workerCardDialog.evaluate((element) => element.open && element.matches(":modal")), true);
  await page.getByRole("button", { name: "домены", exact: true }).focus();
  assert.equal(await workerCardDialog.evaluate((element) => element.contains(document.activeElement)), true,
    "a modal node-deletion dialog must keep background controls inert");
  await workerCardDialog.getByRole("button", { name: "Отмена", exact: true }).click();
  assert.equal(workerDeleteCalls, 0);
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "Удалить");
  assert.equal(await workerCardDelete.evaluate((element) => document.activeElement === element), true);
  await page.getByRole("button", { name: "домены", exact: true }).click();
  assert.match(await page.getByRole("button", { name: "домены", exact: true }).getAttribute("class"), /active-chip/);

  await page.getByRole("button", { name: "VPN", exact: true }).click();
  await waitForHash("#vpn/overview");
  const vpnNavigation = page.getByRole("navigation", { name: "Разделы управления VPN" });
  const waitForCurrentVpnLink = (label) => page.waitForFunction((expectedLabel) =>
    [...document.querySelectorAll('nav[aria-label="Разделы управления VPN"] a')]
      .some((link) => link.textContent?.trim() === expectedLabel && link.getAttribute("aria-current") === "page"),
  label);
  await waitForCurrentVpnLink("Обзор");
  await page.locator(".vpn-admin-shell").screenshot({ path: path.join(output, "admin-overview-1280.png") });
  const overviewLink = vpnNavigation.getByRole("link", { name: "Обзор", exact: true });
  const readyStatus = page.getByText("Пройдено: 0", { exact: true });
  const warningStatus = page.getByText("Предупреждения: 0", { exact: true });
  const errorStatus = page.getByText("Ошибки: 0", { exact: true });
  const commitReadiness = page.getByRole("button", { name: "Зафиксировать готовность", exact: true });
  assert.ok((await overviewLink.boundingBox()).height >= 44, "VPN navigation links must meet a 44px touch target");
  await assertContrast(overviewLink, "light active navigation");
  await overviewLink.hover();
  await assertContrast(overviewLink, "light active navigation hover");
  await overviewLink.focus();
  await assertContrast(overviewLink, "light active navigation focus");
  await assertContrast(readyStatus, "light success status");
  await assertContrast(warningStatus, "light warning status");
  await assertContrast(errorStatus, "light error status");
  assert.equal(await commitReadiness.isDisabled(), true);
  await assertContrast(commitReadiness, "light disabled primary action");
  await page.emulateMedia({ colorScheme: "dark" });
  await assertContrast(overviewLink, "dark active navigation");
  await overviewLink.hover();
  await assertContrast(overviewLink, "dark active navigation hover");
  await assertContrast(readyStatus, "dark success status");
  await assertContrast(warningStatus, "dark warning status");
  await assertContrast(errorStatus, "dark error status");
  await assertContrast(commitReadiness, "dark disabled primary action");
  await page.locator(".vpn-admin-shell").screenshot({ path: path.join(output, "admin-overview-dark-1280.png") });
  await page.emulateMedia({ colorScheme: "light" });
  await assertVisibleVpnInteractiveTraversal(page, "overview-desktop");

  const capacityCard = page.locator(".vpn-capacity-card").filter({ hasText: "Frankfurt Reality" });
  await capacityCard.waitFor();
  const maximumCapacity = capacityCard.getByRole("spinbutton", { name: "Лимит профилей для Frankfurt Reality", exact: true });
  capacitySaveFailure = true;
  await maximumCapacity.fill("25");
  await capacityCard.getByRole("button", { name: "Сохранить лимиты", exact: true }).click();
  const capacityError = capacityCard.getByRole("alert").filter({ hasText: "Не удалось сохранить лимиты" });
  await capacityError.waitFor();
  assert.equal(await capacityCard.getByRole("alert").count(), 1,
    "capacity validation must have exactly one assertive announcement");
  capacitySaveFailure = false;
  await capacityCard.getByRole("button", { name: "Сохранить лимиты", exact: true }).click();
  const capacitySuccess = capacityCard.getByRole("status").filter({ hasText: "Лимиты сохранены." });
  await capacitySuccess.waitFor();
  assert.equal(await capacityCard.getByRole("status").filter({ hasText: "Лимиты сохранены." }).count(), 1,
    "capacity success must have exactly one polite announcement");
  assert.equal(await capacityCard.getByRole("alert").count(), 0,
    "capacity success must clear the earlier assertive announcement");

  await page.evaluate(() => {
    window.__scrollIntoViewCalls = [];
    window.__originalScrollIntoView = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = function scrollIntoView(options) {
      window.__scrollIntoViewCalls.push(options);
    };
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });
  await page.evaluate(() => { window.__scrollIntoViewCalls = []; });
  await maximumCapacity.click();
  assert.equal(await page.evaluate(() => window.__scrollIntoViewCalls.length > 0
    && window.__scrollIntoViewCalls.every((options) => options?.behavior === "auto")), true,
    "mouse focus accommodation must be instant even when motion is allowed");
  await page.evaluate(() => { window.__scrollIntoViewCalls = []; });
  await page.getByRole("navigation", { name: "Быстрые переходы проверки" })
    .getByRole("button", { name: "Ёмкость", exact: true }).click();
  await page.waitForFunction(() => window.__scrollIntoViewCalls.some((options) => options?.behavior === "smooth"));
  await page.emulateMedia({ reducedMotion: "reduce" });
  await page.evaluate(() => { window.__scrollIntoViewCalls = []; });
  await page.getByRole("navigation", { name: "Быстрые переходы проверки" })
    .getByRole("button", { name: "Ёмкость", exact: true }).click();
  await page.waitForFunction(() => window.__scrollIntoViewCalls.some((options) => options?.behavior === "auto"));
  assert.equal(await page.evaluate(() => window.__scrollIntoViewCalls.every(
    (options) => !options || options.behavior === "auto",
  )), true, "reduced-motion admin scrolling must never request smooth behavior");
  await page.evaluate(() => {
    Element.prototype.scrollIntoView = window.__originalScrollIntoView;
    delete window.__originalScrollIntoView;
    delete window.__scrollIntoViewCalls;
  });
  await page.emulateMedia({ reducedMotion: "no-preference" });

  await vpnNavigation.getByRole("link", { name: "Клиенты", exact: true }).click();
  await waitForHash("#vpn/customers");
  await waitForCurrentVpnLink("Клиенты");
  await page.goBack();
  await waitForHash("#vpn/overview");
  await waitForCurrentVpnLink("Обзор");
  await page.goBack();
  await waitForHash("");
  assert.match(await page.getByRole("button", { name: "домены", exact: true }).getAttribute("class"), /active-chip/);
  await page.goForward();
  await waitForHash("#vpn/overview");
  await page.goForward();
  await waitForHash("#vpn/customers");

  await page.getByRole("button", { name: "домены", exact: true }).click();
  await waitForHash("");
  assert.match(await page.getByRole("button", { name: "домены", exact: true }).getAttribute("class"), /active-chip/);
  await page.goBack();
  await waitForHash("#vpn/customers");
  await waitForCurrentVpnLink("Клиенты");
  await page.goForward();
  await waitForHash("");

  await page.goto(`${appUrl}#vpn/customers`);
  await waitForHash("#vpn/customers");
  await waitForCurrentVpnLink("Клиенты");

  const customerSearch = page.getByRole("textbox", { name: "Поиск", exact: true });
  const newCustomer = page.getByRole("button", { name: "Новый клиент", exact: true });
  const allCustomersFilter = page.getByRole("button", { name: "Все", exact: true });
  await assertContrast(newCustomer, "light primary action");
  await newCustomer.hover();
  await assertContrast(newCustomer, "light primary action hover");
  await newCustomer.focus();
  await assertContrast(newCustomer, "light primary action focus");
  await assertContrast(allCustomersFilter, "light active filter");
  await allCustomersFilter.hover();
  await assertContrast(allCustomersFilter, "light active filter hover");
  await page.emulateMedia({ colorScheme: "dark" });
  await assertContrast(newCustomer, "dark primary action");
  await newCustomer.hover();
  await assertContrast(newCustomer, "dark primary action hover");
  await assertContrast(allCustomersFilter, "dark active filter");
  await allCustomersFilter.hover();
  await assertContrast(allCustomersFilter, "dark active filter hover");
  await page.emulateMedia({ colorScheme: "light" });
  const firstProfileAction = page.getByRole("button", { name: "Изменить название", exact: true }).first();
  await assertVisibleVpnInteractiveTraversal(page, "customers-desktop");
  await assertFocusVisible(vpnNavigation.getByRole("link", { name: "Клиенты", exact: true }), "VPN navigation");
  await assertFocusVisible(customerSearch, "customer search");
  await assertFocusVisible(firstProfileAction, "profile action");
  await vpnNavigation.getByRole("link", { name: "Клиенты", exact: true }).focus();
  let searchFocusStep = -1;
  let profileFocusStep = -1;
  for (let step = 0; step < 48 && profileFocusStep < 0; step += 1) {
    if (await customerSearch.evaluate((element) => document.activeElement === element)) searchFocusStep = step;
    if (await firstProfileAction.evaluate((element) => document.activeElement === element)) profileFocusStep = step;
    await page.keyboard.press("Tab");
  }
  assert.ok(searchFocusStep > 0, "Tab order must reach customer search after VPN navigation");
  assert.ok(profileFocusStep > searchFocusStep, "Tab order must reach a profile action after customer search");

  for (const [label, hash, auditLabel] of [
    ["Тарифы", "#vpn/plans", "plans-desktop"],
    ["События", "#vpn/events", "events-desktop"],
  ]) {
    await vpnNavigation.getByRole("link", { name: label, exact: true }).click();
    await waitForHash(hash);
    await waitForCurrentVpnLink(label);
    await assertVisibleVpnInteractiveTraversal(page, auditLabel);
  }
  await vpnNavigation.getByRole("link", { name: "Клиенты", exact: true }).click();
  await waitForHash("#vpn/customers");
  await waitForCurrentVpnLink("Клиенты");

  for (const [section, label] of [
    ["overview", "Обзор"],
    ["customers", "Клиенты"],
    ["nodes", "Ноды"],
    ["plans", "Тарифы"],
    ["events", "События"],
  ]) {
    await auditScaledAdminSection(section, label);
  }

  await page.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
  assert.notEqual(
    await page.locator(".vpn-customer-row").filter({ hasText: "Анна" }).evaluate(
      (element) => getComputedStyle(element).backgroundColor,
    ),
    "rgb(37, 109, 255)",
    "an unselected customer row must not look like a primary action",
  );
  await page.getByRole("button", { name: "Новый клиент", exact: true }).click();
  const customerDraft = page.getByRole("textbox", { name: "Имя", exact: true });
  await customerDraft.fill("Черновик клиента");
  await vpnNavigation.getByRole("link", { name: "Обзор", exact: true }).click();
  await waitForHash("#vpn/overview");
  await waitForCurrentVpnLink("Обзор");
  assert.equal(await page.locator('[data-vpn-admin-section="customers"]').isHidden(), true);
  assert.equal(await page.getByRole("button", { name: "Новый клиент", exact: true }).count(), 0);
  await vpnNavigation.getByRole("link", { name: "Клиенты", exact: true }).click();
  await waitForHash("#vpn/customers");
  await waitForCurrentVpnLink("Клиенты");
  assert.equal(await customerDraft.inputValue(), "Черновик клиента");
  await page.getByRole("button", { name: "Отмена", exact: true }).click();
  assert.match(await page.locator(".vpn-customer-row.active-chip").innerText(), /Борис/);

  await page.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
  const key = page.locator(".vpn-subscription-card").filter({ hasText: "Подписка #12" }).locator(".vpn-key-card");
  await key.getByText("Телефон 2", { exact: true }).waitFor();
  assert.equal(await key.getByText(/dropcatch-old/).count(), 0);
  const initialSecret = labelled("Телефон 2");
  assert.equal(await key.evaluate((element, secret) => element.textContent.includes(secret), initialSecret), false);
  await assertSecretAbsent(initialSecret, "before reveal");
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  assert.equal(await page.evaluate(() => window.__copies.at(-1)), initialSecret);
  await assertSecretAbsent(initialSecret, "after hidden copy");
  await key.getByRole("button", { name: "Показать полную ссылку", exact: true }).click();
  assert.equal(await key.locator("textarea").inputValue(), initialSecret);
  assert.equal(await key.locator("textarea").getAttribute("readonly"), "");
  await page.setViewportSize({ width: 390, height: 844 });
  const initialNarrowBounds = await key.boundingBox();
  assert.ok(initialNarrowBounds.x >= 0 && initialNarrowBounds.x + initialNarrowBounds.width <= 391,
    "the profile card must fit the narrow viewport, not merely its oversized parent");
  assert.ok(initialNarrowBounds.width >= 280,
    `the nested profile card must remain usable at 390px (actual ${initialNarrowBounds.width}px)`);
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    "the VPN admin page must not overflow the narrow viewport");
  await page.setViewportSize({ width: 1280, height: 1000 });
  await key.getByRole("button", { name: "Изменить название", exact: true }).click();
  assert.equal(await key.getByRole("textbox", { name: "Название профиля Телефон 2", exact: true }).getAttribute("maxlength"), "64");
  await key.getByRole("textbox", { name: "Название профиля Телефон 2", exact: true }).fill("Отменённое имя");
  await key.getByRole("button", { name: "Отмена", exact: true }).click();
  assert.equal(patches.length, 0);
  await key.getByText("Телефон 2", { exact: true }).waitFor();

  const otherSubscription = page.locator(".vpn-subscription-card").filter({ hasText: "Подписка #13" });
  await otherSubscription.getByRole("button", { name: "Изменить", exact: true }).click();
  const rename = async (oldName, newName) => {
    await key.getByRole("button", { name: "Изменить название", exact: true }).click();
    await key.getByRole("textbox", { name: `Название профиля ${oldName}`, exact: true }).fill(newName);
    await key.getByRole("button", { name: "Сохранить", exact: true }).click();
  };
  await rename("Телефон 2", "Рабочий iPhone");
  const successToast = page.locator(".toast").filter({ hasText: "Название профиля сохранено" });
  await successToast.waitFor();
  assert.equal(await successToast.getAttribute("role"), "status");
  assert.match(await page.locator(".vpn-customer-row.active-chip").innerText(), /Борис/);
  await page.getByRole("heading", { name: "Изменить подписку #13", exact: true }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Рабочий iPhone"));
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  assert.equal(await page.evaluate(() => window.__copies.at(-1)), labelled("Рабочий iPhone"));

  holdReload = true;
  await page.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
  await until(() => heldReloads.length > 0, "older reload not requested");
  holdReload = false;
  await rename("Рабочий iPhone", "Рабочий iPhone 2");
  await key.getByText("Рабочий iPhone 2", { exact: true }).waitFor();
  await page.locator(".toast").filter({ hasText: "Название профиля сохранено" }).waitFor();
  await Promise.all(heldReloads.splice(0).map((release) => release()));
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await key.getByText("Рабочий iPhone 2", { exact: true }).count(), 1,
    "an older GET arriving after the matching reload must not restore the previous label");

  reloadFailure = true;
  await rename("Рабочий iPhone 2", "Ноутбук");
  await page.locator(".toast").filter({ hasText: "но список не обновлён" }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Ноутбук"));
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  assert.equal(await page.evaluate(() => window.__copies.at(-1)), labelled("Ноутбук"));

  reloadFailure = false;
  keys = keys.map((item) => item.id === 22
    ? { ...item, display_name: "Имя из кабинета", config_uri: labelled("Имя из кабинета"),
      updated_at: nextVersion() } : item);
  await page.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
  await key.getByText("Имя из кабинета", { exact: true }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Имя из кабинета"));
  holdReload = true;
  await rename("Имя из кабинета", "Домашний Mac");
  await page.waitForFunction(() => document.querySelector(".vpn-key-rename-form button[type=submit]")?.disabled);
  const reloadDeadline = Date.now() + 10000;
  while (heldReloads.length === 0) {
    assert.ok(Date.now() < reloadDeadline, "rename did not request reload");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  await key.getByText("Обновляем подпись ссылки…", { exact: true }).waitFor();
  assert.equal(await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).count(), 0);
  assert.equal(await key.getByRole("button", { name: /^(Скрыть ссылку|Показать полную ссылку)$/ }).count(), 0);
  assert.equal(await key.locator("textarea").count(), 0);
  holdReload = false;
  await Promise.all(heldReloads.splice(0).map((release) => release()));
  await key.getByText("Домашний Mac", { exact: true }).waitFor();
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  assert.equal(await page.evaluate(() => window.__copies.at(-1)), labelled("Домашний Mac"));

  heldRenameIds.add(22);
  heldRenameIds.add(23);
  await rename("Домашний Mac", "Основной Mac");
  await until(() => heldRenames.has(22), "held first rename not requested");
  const secondKey = otherSubscription.locator(".vpn-key-card");
  const secondRenameButton = secondKey.getByRole("button", { name: "Изменить название", exact: true });
  const allowsSecondRename = await secondRenameButton.isEnabled();
  if (allowsSecondRename) {
    await secondRenameButton.click();
    await secondKey.getByRole("textbox", { name: "Название профиля Телефон 3", exact: true }).fill("Планшет");
    await secondKey.getByRole("button", { name: "Сохранить", exact: true }).click();
    await until(() => heldRenames.has(23), "held second rename not requested");
  }
  assert.equal(await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).count(), 0);
  await heldRenames.get(22)();
  await key.getByText("Основной Mac", { exact: true }).waitFor();
  if (allowsSecondRename) {
    assert.equal(await secondKey.getByRole("textbox", { name: /^Название профиля / }).inputValue(), "Планшет");
    assert.equal(await secondKey.getByRole("button", { name: "Скопировать ссылку", exact: true }).count(), 0);
    await heldRenames.get(23)();
    await secondKey.getByText("Планшет", { exact: true }).waitFor();
  }
  heldRenameIds.clear();

  renameFailure = true;
  await rename("Основной Mac", "Отказанное имя");
  const errorToast = page.locator(".toast").filter({ hasText: "Имя отклонено" });
  await errorToast.waitFor();
  assert.equal(await errorToast.getAttribute("role"), "alert");
  await key.getByRole("button", { name: "Отмена", exact: true }).click();
  await key.getByText("Основной Mac", { exact: true }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Основной Mac"));
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => { throw new Error("Synthetic clipboard denial"); };
  });
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  await page.locator(".toast").filter({ hasText: "Полная ссылка выделена" }).waitFor();
  await page.waitForFunction(() => {
    const field = document.getElementById("vpn-key-uri-22");
    return field && document.activeElement === field && field.selectionStart === 0
      && field.selectionEnd === field.value.length;
  });
  assert.deepEqual(await key.locator("textarea").evaluate((field) =>
    [document.activeElement === field, field.selectionStart, field.selectionEnd]),
  [true, 0, labelled("Основной Mac").length]);
  assert.deepEqual(errors, []);
  await page.locator(".vpn-customer-workspace").screenshot({ path: path.join(output, "admin-profile-1280.png") });
  renameFailure = false;
  reloadFailure = true;
  const longName = "Р".repeat(64);
  await rename("Основной Mac", longName);
  await page.locator(".toast").filter({ hasText: "но список не обновлён" }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  await key.getByText(longName, { exact: true }).waitFor();
  assert.equal(await key.evaluate((element) => element.scrollWidth <= element.clientWidth), true);
  await key.getByRole("button", { name: "Изменить название", exact: true }).click();
  assert.equal(await key.evaluate((element) => element.scrollWidth <= element.clientWidth), true);
  const renamedNarrowBounds = await key.boundingBox();
  assert.ok(renamedNarrowBounds.x >= 0 && renamedNarrowBounds.x + renamedNarrowBounds.width <= 391);
  await key.screenshot({ path: path.join(output, "admin-profile-390.png") });
  await key.getByRole("button", { name: "Отмена", exact: true }).click();
  await page.evaluate(() => {
    window.__rejectCopy = null;
    navigator.clipboard.writeText = () => new Promise((_, reject) => {
      window.__rejectCopy = () => reject(new Error("Delayed clipboard denial"));
    });
  });
  await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).click();
  await page.waitForFunction(() => typeof window.__rejectCopy === "function");
  holdCommittedReply = true;
  await rename(longName, "Более старое имя");
  await until(() => releaseCommittedReply !== null, "committed PATCH reply not held");
  reloadFailure = false;
  keys = keys.map((item) => item.id === 22 ? { ...item, display_name: "Имя администратора", status: "revoked", config_uri: null,
    updated_at: nextVersion() } : item);
  const authoritativeGet = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/control/vpn/access-keys" && response.status() === 200);
  await page.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
  await authoritativeGet;
  await key.getByText("отозван", { exact: true }).waitFor();
  await page.evaluate(() => window.__rejectCopy());
  await page.locator(".toast").filter({ hasText: "Не удалось скопировать или выделить ссылку" }).waitFor();
  assert.equal(await key.locator("textarea").count(), 0);
  reloadFailure = true;
  const failedFollowup = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/control/vpn/access-keys" && response.status() === 503);
  await releaseCommittedReply();
  await failedFollowup;
  await key.getByText("Имя администратора", { exact: true }).waitFor();
  await key.getByText("Профиль отозван, ссылка больше недоступна.", { exact: true }).waitFor();
  assert.equal(await key.getByRole("button", { name: "Скопировать ссылку", exact: true }).count(), 0);
  assert.equal(await key.locator("textarea").count(), 0);
  await page.setViewportSize({ width: 1280, height: 1000 });
  await page.goto(`${appUrl}#vpn/nodes`);
  await waitForHash("#vpn/nodes");
  await waitForCurrentVpnLink("Ноды");
  await assertVisibleVpnInteractiveTraversal(page, "nodes-desktop");
  const deleteTrigger = page.getByRole("button", { name: "Удалить ноду", exact: true });
  await page.locator(".vpn-admin-shell").screenshot({ path: path.join(output, "admin-nodes-1280.png") });
  await page.setViewportSize({ width: 1280, height: 700 });
  await page.evaluate(() => window.scrollTo(0, document.querySelector(".vpn-admin-shell").offsetTop + 700));
  await page.waitForFunction(() => {
    const nav = document.querySelector(".vpn-admin-nav");
    return nav && nav.getBoundingClientRect().top <= 13;
  });
  const stickyNavTop = (await vpnNavigation.boundingBox()).y;
  assert.ok(stickyNavTop >= 0 && stickyNavTop <= 13,
    `VPN navigation must stay sticky across the active section (actual top ${stickyNavTop}px)`);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true,
    "the VPN admin page must not scroll horizontally");
  assert.equal(await page.locator('[data-vpn-admin-section="nodes"] .simple-table').evaluate(
    (element) => element.scrollWidth <= element.clientWidth,
  ), true, "the mobile node view must stack instead of hiding actions in a horizontal table");
  const mobileNodeRow = page.locator('[data-vpn-admin-section="nodes"] tbody tr').first();
  const mobileNodeBounds = await mobileNodeRow.boundingBox();
  assert.ok(mobileNodeBounds.x >= 0 && mobileNodeBounds.x + mobileNodeBounds.width <= 390,
    "the first mobile node card must fit the viewport");
  const mobileNodeActions = mobileNodeRow.getByRole("button");
  assert.ok(await mobileNodeActions.count() >= 7, "all important node operations must remain visible on mobile");
  for (let index = 0; index < await mobileNodeActions.count(); index += 1) {
    const bounds = await mobileNodeActions.nth(index).boundingBox();
    assert.ok(bounds && bounds.x >= 0 && bounds.x + bounds.width <= 390,
      `mobile node action ${index + 1} must be inside the viewport`);
  }
  for (const link of await vpnNavigation.getByRole("link").all()) {
    assert.ok((await link.boundingBox()).height >= 44, "each VPN navigation link must have a 44px touch target");
  }
  await assertVisibleVpnInteractiveTraversal(page, "nodes-mobile");
  await page.locator(".vpn-admin-shell").screenshot({ path: path.join(output, "admin-nodes-390.png") });
  await page.setViewportSize({ width: 1280, height: 1000 });
  await deleteTrigger.click();
  const deleteDialog = page.getByRole("dialog", { name: "Удалить VPN‑ноду «Frankfurt 1»?", exact: true });
  await deleteDialog.getByText(
    "Она перестанет принимать новые профили. Активные профили будут обработаны по текущим правилам безопасного удаления.",
    { exact: true },
  ).waitFor();
  assert.equal(await deleteDialog.evaluate((element) => element.tagName), "DIALOG");
  assert.equal(await deleteDialog.evaluate((element) => element.open && element.matches(":modal")), true);
  await deleteDialog.screenshot({ path: path.join(output, "admin-node-delete-dialog-1280.png") });
  const cancelDelete = deleteDialog.getByRole("button", { name: "Отмена", exact: true });
  const confirmDelete = deleteDialog.getByRole("button", { name: "Да, удалить ноду", exact: true });
  await page.waitForFunction(() => document.activeElement?.textContent?.trim() === "Отмена");
  assert.equal(await cancelDelete.evaluate((element) => document.activeElement === element), true);
  await cancelDelete.click();
  assert.equal(workerDeleteCalls, 0);
  await page.waitForFunction(() => document.activeElement?.classList.contains("vpn-node-delete-trigger"));
  assert.equal(await deleteTrigger.evaluate((element) => document.activeElement === element), true);
  await deleteTrigger.click();
  await page.keyboard.press("Escape");
  assert.equal(workerDeleteCalls, 0);
  await page.waitForFunction(() => document.activeElement?.classList.contains("vpn-node-delete-trigger"));
  assert.equal(await deleteTrigger.evaluate((element) => document.activeElement === element), true);
  workerDeleteFailure = true;
  await deleteTrigger.click();
  await confirmDelete.click();
  await deleteDialog.getByRole("alert").getByText("Нода занята. Повторите позже", { exact: true }).waitFor();
  assert.equal(await page.getByRole("alert").filter({ hasText: "Нода занята. Повторите позже" }).count(), 1,
    "a failed DELETE must be announced only by the modal alert");
  assert.equal(await page.locator(".toast").filter({ hasText: "Нода занята. Повторите позже" }).count(), 0,
    "a failed DELETE must not create a duplicate global alert");
  assert.equal(workerDeleteCalls, 1);
  assert.equal(await confirmDelete.isEnabled(), true);
  workerDeleteFailure = false;
  await cancelDelete.click();
  await page.waitForFunction(() => document.activeElement?.classList.contains("vpn-node-delete-trigger"));
  reloadFailure = false;
  holdReload = true;
  await page.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
  await until(() => heldReloads.length > 0, "pre-delete stale load was not held");
  holdReload = false;
  const deleteCallsBeforeRace = workerDeleteCalls;
  await page.evaluate(() => {
    window.__nodeDeletionFocusSnapshots = [];
    document.addEventListener("focusin", (event) => {
      if (event.target?.getAttribute?.("href") !== "#vpn/nodes") return;
      const dialog = document.querySelector(".vpn-node-dialog");
      window.__nodeDeletionFocusSnapshots.push({
        open: dialog?.open ?? false,
        modal: dialog?.matches(":modal") ?? false,
      });
    });
  });
  await deleteTrigger.click();
  await confirmDelete.evaluate((element) => { element.click(); element.click(); });
  await until(() => workerDeleteCalls === deleteCallsBeforeRace + 1,
    "node deletion was not requested exactly once per confirmation");
  assert.equal(await confirmDelete.isDisabled(), true);
  await deleteDialog.getByRole("status").getByText("Удаляем ноду…", { exact: true }).waitFor();
  await page.getByRole("button", { name: "Обновить", exact: true }).first().focus();
  assert.equal(await deleteDialog.evaluate((element) => element.contains(document.activeElement)), true,
    "background focus must stay inert while DELETE is pending");
  await page.keyboard.press("Tab");
  assert.equal(await deleteDialog.evaluate((element) => element.contains(document.activeElement)), true,
    "Tab must stay inside the dialog while DELETE is pending");
  await page.keyboard.press("Escape");
  assert.equal(await deleteDialog.isVisible(), true, "Escape must not close a busy destructive dialog");
  workerRefreshFailures = 1;
  await releaseWorkerDelete();
  const deletionRefreshAlert = page.locator(".toast").filter({ hasText: "Нода удалена, но список не удалось обновить" });
  await deletionRefreshAlert.waitFor();
  assert.equal(await deletionRefreshAlert.getAttribute("role"), "alert");
  assert.equal(await page.getByRole("alert").filter({ hasText: "Нода удалена, но список не удалось обновить" }).count(), 1,
    "a post-delete refresh failure must have one global alert after the dialog closes");
  assert.equal(workerDeleteCalls - deleteCallsBeforeRace, 1);
  assert.equal(await page.getByRole("dialog").count(), 0);
  assert.equal(await page.getByRole("button", { name: "Удалить ноду", exact: true }).count(), 0,
    "a successfully deleted node must not retain a stale repeat action after refresh failure");
  await page.waitForFunction(() => document.activeElement?.getAttribute("href") === "#vpn/nodes");
  assert.equal(await vpnNavigation.getByRole("link", { name: "Ноды", exact: true }).evaluate(
    (element) => document.activeElement === element,
  ), true, "successful deletion must return focus to a deterministic Nodes destination");
  assert.deepEqual(await page.evaluate(() => window.__nodeDeletionFocusSnapshots.at(-1)), {
    open: false,
    modal: false,
  }, "the native modal must close before focus returns to the Nodes navigation");
  const staleLoadResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/control/vpn/access-keys" && response.status() === 200);
  await Promise.all(heldReloads.splice(0).map((release) => release()));
  await staleLoadResponse;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  assert.equal(await page.getByRole("button", { name: "Удалить ноду", exact: true }).count(), 0,
    "a pre-delete stale load must never restore the deleted node");
  assert.equal(workerDeleteCalls - deleteCallsBeforeRace, 1,
    "settling the stale load must not repeat DELETE");
  replacementWorkerAvailable = true;
  const freshWorkerResponse = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/control/workers" && response.status() === 200);
  await page.locator(".toolbar-actions").getByRole("button", { name: "Обновить", exact: true }).click();
  await freshWorkerResponse;
  await page.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  await page.getByText("Amsterdam 2", { exact: true }).waitFor();
  assert.equal(await page.getByText("Frankfurt 1", { exact: true }).count(), 0,
    "a later successful refresh must not restore the deleted node");
  assert.equal(await page.getByRole("button", { name: "Удалить ноду", exact: true }).count(), 1,
    "the worker barrier must still allow a later fresh node list to apply");
  assert.equal(workerDeleteCalls - deleteCallsBeforeRace, 1);
  assert.deepEqual(errors, []);
  console.log(`PASS: actual admin bundle accessibility=${JSON.stringify(accessibilityAuditCounts)} rename/cancel/selection/reload-failure/stale-GET/delayed-PATCH/external-rename/revoke/two-key-pending/copy-fallback/error/narrow-profile-bounds, synthetic HTTP only`);
  }
  }
} finally {
  await browser?.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
