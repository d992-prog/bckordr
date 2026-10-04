// Actual admin bundle, synthetic HTTP responses only; no production connection.
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
  const handleApiRoute = async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
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

      await targetPage.waitForFunction((auditId) => {
        const element = document.querySelector(`[data-vpn-a11y-audit-id="${auditId}"]`);
        if (!(element instanceof HTMLElement) || document.activeElement !== element) return false;
        const box = element.getBoundingClientRect();
        const navigation = document.querySelector(".vpn-admin-nav");
        const navigationBox = navigation?.getBoundingClientRect() || null;
        const obscuredByNavigation = navigationBox && !navigation.contains(element)
          && box.top < navigationBox.bottom && box.bottom > navigationBox.top;
        return !obscuredByNavigation && box.top >= -1 && box.left >= -1
          && box.bottom <= innerHeight + 1 && box.right <= innerWidth + 1;
      }, activeAuditId, { timeout: 750 }).catch(() => {});
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
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        for (let current = element; current && root.contains(current); current = current.parentElement) {
          const currentStyle = getComputedStyle(current);
          const currentBox = current.getBoundingClientRect();
          const visuallyHidden = currentStyle.position === "absolute"
            && (currentBox.width <= 1 || currentBox.height <= 1)
            && (currentStyle.clip !== "auto" || currentStyle.clipPath !== "none");
          if (currentStyle.display === "none" || currentStyle.visibility === "hidden" || visuallyHidden) return false;
        }
        return box.width > 0 && box.height > 0;
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
      window.scrollTo(0, 0);
    });
    await targetPage.evaluate(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const result = await targetPage.locator(".vpn-admin-shell").evaluate((root) => {
      const isVisible = (element) => {
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        for (let current = element; current && root.contains(current); current = current.parentElement) {
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

  const until = async (condition, message) => {
    const deadline = Date.now() + 10000;
    while (!condition()) {
      assert.ok(Date.now() < deadline, message);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  };
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
} finally {
  await browser?.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
