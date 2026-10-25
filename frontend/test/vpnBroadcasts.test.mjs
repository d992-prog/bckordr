import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
async function helpers() {
  try { return await import("../src/vpnBroadcasts.ts"); }
  catch (cause) { if (cause.code === "ERR_MODULE_NOT_FOUND") return {}; throw cause; }
}

test("news routes use bounded cursor and exact plain-text mutation bodies", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url: String(url), init }); return { ok: true, json: async () => ({}) }; };
  assert.equal(typeof api.getVpnBroadcastConfig, "function");
  await api.getVpnBroadcastConfig();
  await api.getVpnBroadcasts();
  await api.getVpnBroadcasts({ beforeId: 51 });
  await api.getVpnBroadcast(12);
  await api.createVpnBroadcast("<b>Новости</b>");
  await api.updateVpnBroadcast(12, "Правка");
  await api.previewVpnBroadcast(12);
  await api.confirmVpnBroadcast(12, { expected_revision: 2, expected_recipient_count: 0, confirm: true });
  await api.cancelVpnBroadcast(12);
  assert.deepEqual(calls.map(({ url }) => url), [
    "/api/control/vpn/broadcasts/config", "/api/control/vpn/broadcasts?limit=50", "/api/control/vpn/broadcasts?limit=50&before_id=51",
    "/api/control/vpn/broadcasts/12", "/api/control/vpn/broadcasts", "/api/control/vpn/broadcasts/12",
    "/api/control/vpn/broadcasts/12/preview", "/api/control/vpn/broadcasts/12/confirm", "/api/control/vpn/broadcasts/12/cancel",
  ]);
  assert.deepEqual(calls.map(({ init }) => init.method ?? "GET"), ["GET", "GET", "GET", "GET", "POST", "PATCH", "POST", "POST", "POST"]);
  assert.deepEqual(calls.slice(4).map(({ init }) => JSON.parse(init.body)), [
    { body: "<b>Новости</b>" }, { body: "Правка" }, {}, { expected_revision: 2, expected_recipient_count: 0, confirm: true }, {},
  ]);
  for (const { init } of calls) { assert.equal(init.credentials, "include"); assert.equal(init.cache, "no-store"); }
});

test("news validation counts code points, preserves literal text and normalizes only CRLF", async () => {
  const { validateBroadcastBody, broadcastCharacters } = await helpers();
  assert.equal(typeof validateBroadcastBody, "function");
  assert.equal(broadcastCharacters("😀\nя"), 3);
  assert.equal(validateBroadcastBody(" <b>Новости</b>\r\nВторая строка "), " <b>Новости</b>\nВторая строка ");
  assert.equal(validateBroadcastBody("😀".repeat(4096)), "😀".repeat(4096));
  for (const body of ["", " \n\t", "😀".repeat(4097), "x\ud800", "\udc00x"]) assert.throws(() => validateBroadcastBody(body));
});

test("news confirmation requires an unedited fresh preview and explicit ready feature gates", async () => {
  const { canConfirmBroadcast, broadcastErrorText, broadcastStateLabel } = await helpers();
  assert.equal(typeof canConfirmBroadcast, "function");
  const config = { enabled: true, registry_state: "ready" };
  const preview = { id: 1, state: "draft", body: "Новости", revision: 2, preview_revision: 2, preview_recipient_count: 0, previewed_at: "2026-10-25T00:00:00Z" };
  assert.equal(canConfirmBroadcast(config, preview, "Новости"), true, "zero audience is not an absent preview");
  for (const invalid of [{ ...preview, preview_revision: 1 }, { ...preview, preview_recipient_count: null }, { ...preview, state: "running" }]) assert.equal(canConfirmBroadcast(config, invalid, "Новости"), false);
  assert.equal(canConfirmBroadcast(config, null, "Новости"), false);
  assert.equal(canConfirmBroadcast(config, preview, "Правка"), false);
  assert.equal(canConfirmBroadcast({ ...config, enabled: false }, preview, "Новости"), false);
  for (const registry_state of ["history_unverified", "bot_changed", "unconfigured"]) assert.equal(canConfirmBroadcast({ ...config, registry_state }, preview, "Новости"), false);
  assert.match(broadcastErrorText(new Error("vpn_broadcast_audience_changed")), /получателей.*изменился/i);
  assert.doesNotMatch(broadcastErrorText(new Error("private upstream text")), /private|upstream/);
  assert.equal(broadcastStateLabel("toString"), "Нет данных");
});
