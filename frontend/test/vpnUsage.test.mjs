import test, { afterEach } from "node:test";
import assert from "node:assert/strict";

import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

async function usageHelpers() {
  try { return await import("../src/vpnUsage.ts"); }
  catch (cause) {
    if (cause.code === "ERR_MODULE_NOT_FOUND") return {};
    throw cause;
  }
}

test("usage reads use exact admin routes, periods, worker cursor and no-store", async () => {
  const calls = [];
  const customer = { customer_id: 42, days: 30, upload_bytes: 300, download_bytes: 900, measured_since: null, last_sample_at: null, coverage: "partial", devices: [
    { device_id: 121, display_name: "Телефон", upload_bytes: 300, download_bytes: 900, coverage: "partial" },
  ] };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, json: async () => calls.length < 3 ? [] : customer };
  };
  assert.equal(typeof api.getVpnNodeUsage, "function");
  assert.equal(typeof api.getVpnCustomerUsage, "function");
  await api.getVpnNodeUsage();
  await api.getVpnNodeUsage(7, { afterId: 100 });
  assert.deepEqual(await api.getVpnCustomerUsage(42, 30), customer, "server child-key aggregates remain authoritative");
  assert.deepEqual(calls.map((call) => call.url), [
    "/api/control/vpn/usage/nodes?days=1&after_id=0&limit=100",
    "/api/control/vpn/usage/nodes?days=7&after_id=100&limit=100",
    "/api/control/vpn/customers/42/usage?days=30",
  ]);
  for (const { init } of calls) {
    assert.equal(init.credentials, "include");
    assert.equal(init.cache, "no-store");
    assert.equal(init.method ?? "GET", "GET");
    assert.equal(init.body, undefined);
    assert.equal(new Headers(init.headers).get("X-CSRF-Token"), null);
  }
});

test("traffic formatter never turns absent measurement into zero", async () => {
  const { formatTrafficBytes } = await usageHelpers();
  assert.equal(typeof formatTrafficBytes, "function");
  assert.equal(formatTrafficBytes(null), "Нет данных");
  assert.equal(formatTrafficBytes(undefined), "Нет данных");
  assert.equal(formatTrafficBytes(0), "0 Б");
  assert.equal(formatTrafficBytes(1024), "1 КиБ");
  assert.equal(formatTrafficBytes(1610612736), "1,5 ГиБ");
  for (const invalid of [-1, NaN, Infinity]) assert.equal(formatTrafficBytes(invalid), "Нет данных");
});

test("coverage and timestamps have bounded Russian labels with no invented freshness", async () => {
  const { usageCoverageLabel, formatUsageTime } = await usageHelpers();
  assert.equal(typeof usageCoverageLabel, "function");
  assert.equal(usageCoverageLabel("none"), "Нет данных");
  assert.equal(usageCoverageLabel("partial"), "Частичные данные");
  assert.equal(usageCoverageLabel("stale"), "Данные устарели");
  assert.equal(usageCoverageLabel("unavailable"), "Сбор недоступен");
  assert.equal(usageCoverageLabel("current"), "Данные актуальны");
  assert.equal(usageCoverageLabel("toString"), "Нет данных");
  assert.equal(formatUsageTime(null), "Нет данных");
  assert.equal(formatUsageTime("not-a-date"), "Нет данных");
  assert.match(formatUsageTime("2026-10-25T12:34:00Z"), /25\.10\.2026.*12:34.*UTC/);
});
