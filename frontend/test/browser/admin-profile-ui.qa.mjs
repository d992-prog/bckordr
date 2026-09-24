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
  page.on("pageerror", (error) => errors.push(error.message));
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
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const pathname = new URL(request.url()).pathname;
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
    if (["/api/control/vpn/overview", "/api/control/vpn/lifecycle/status",
      "/api/admin/diagnostic-telegram", "/api/control/discovery/runtime-settings",
      "/api/control/zone-scanner/settings"].includes(pathname)) return json(route, {});
    return json(route, []);
  });
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.getByRole("button", { name: "VPN", exact: true }).click();
  await page.locator(".vpn-customer-row").filter({ hasText: "Борис" }).click();
  const key = page.locator(".vpn-subscription-card").filter({ hasText: "Подписка #12" }).locator(".vpn-key-card");
  await key.getByText("Телефон 2", { exact: true }).waitFor();
  assert.equal(await key.getByText(/dropcatch-old/).count(), 0);
  await key.getByRole("button", { name: "Показать полностью", exact: true }).click();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Телефон 2"));
  await page.setViewportSize({ width: 390, height: 844 });
  const initialNarrowBounds = await key.boundingBox();
  assert.ok(initialNarrowBounds.x >= 0 && initialNarrowBounds.x + initialNarrowBounds.width <= 391,
    "the profile card must fit the narrow viewport, not merely its oversized parent");
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
  await page.locator(".toast").filter({ hasText: "Название профиля сохранено" }).waitFor();
  assert.match(await page.locator(".vpn-customer-row.active-chip").innerText(), /Борис/);
  await page.getByRole("heading", { name: "Изменить подписку #13", exact: true }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Рабочий iPhone"));
  await key.getByRole("button", { name: "Копировать ссылку", exact: true }).click();
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
  await key.getByRole("button", { name: "Копировать ссылку", exact: true }).click();
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
  assert.equal(await key.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 0);
  assert.equal(await key.getByRole("button", { name: /^(Скрыть|Показать полностью)$/ }).count(), 0);
  assert.equal(await key.locator("textarea").count(), 0);
  holdReload = false;
  await Promise.all(heldReloads.splice(0).map((release) => release()));
  await key.getByText("Домашний Mac", { exact: true }).waitFor();
  await key.getByRole("button", { name: "Копировать ссылку", exact: true }).click();
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
  assert.equal(await key.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 0);
  await heldRenames.get(22)();
  await key.getByText("Основной Mac", { exact: true }).waitFor();
  if (allowsSecondRename) {
    assert.equal(await secondKey.getByRole("textbox", { name: /^Название профиля / }).inputValue(), "Планшет");
    assert.equal(await secondKey.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 0);
    await heldRenames.get(23)();
    await secondKey.getByText("Планшет", { exact: true }).waitFor();
  }
  heldRenameIds.clear();

  renameFailure = true;
  await rename("Основной Mac", "Отказанное имя");
  await page.locator(".toast").filter({ hasText: "Имя отклонено" }).waitFor();
  await key.getByRole("button", { name: "Отмена", exact: true }).click();
  await key.getByText("Основной Mac", { exact: true }).waitFor();
  assert.equal(await key.locator("textarea").inputValue(), labelled("Основной Mac"));
  await page.evaluate(() => {
    navigator.clipboard.writeText = async () => { throw new Error("Synthetic clipboard denial"); };
  });
  await key.getByRole("button", { name: "Копировать ссылку", exact: true }).click();
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
  const output = path.resolve(root, "../.pytest_cache/admin-profile-ui-qa");
  await mkdir(output, { recursive: true });
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
  reloadFailure = true;
  const failedFollowup = page.waitForResponse((response) =>
    new URL(response.url()).pathname === "/api/control/vpn/access-keys" && response.status() === 503);
  await releaseCommittedReply();
  await failedFollowup;
  await key.getByText("Имя администратора", { exact: true }).waitFor();
  await key.getByText("Ключ отозван, ссылка больше недоступна.", { exact: true }).waitFor();
  assert.equal(await key.getByRole("button", { name: "Копировать ссылку", exact: true }).count(), 0);
  assert.equal(await key.locator("textarea").count(), 0);
  assert.deepEqual(errors, []);
  console.log("PASS: actual admin bundle rename/cancel/selection/reload-failure/stale-GET/delayed-PATCH/external-rename/revoke/two-key-pending/copy-fallback/error/narrow-profile-bounds, synthetic HTTP only");
} finally {
  await browser?.close();
  await new Promise((resolve, reject) => server.close((error) => error ? reject(error) : resolve()));
}
