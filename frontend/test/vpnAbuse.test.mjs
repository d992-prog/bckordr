import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });

async function helpers() {
  try { return await import("../src/vpnAbuse.ts"); }
  catch (error) {
    if (error.code === "ERR_MODULE_NOT_FOUND") return {};
    throw error;
  }
}

const incident = (id, acknowledged_at = null) => ({
  id, device_id: id + 100, customer_id: 7, display_name: `Профиль ${id}`,
  started_at: "2026-10-25T12:00:00Z", last_observed_at: "2026-10-25T12:04:05Z",
  peak_source_count: 3, acknowledged_at,
});

async function componentSource() {
  return readFile(new URL("../src/VpnAbusePanel.tsx", import.meta.url), "utf8")
    .catch((error) => error.code === "ENOENT" ? "" : Promise.reject(error));
}

test("abuse API reads use bounded cursor routes and acknowledgement sends only an empty body", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, json: async () => calls.length === 3 ? incident(8, "2026-10-25T12:10:00Z") : [] };
  };
  assert.equal(typeof api.getVpnAbuseIncidents, "function");
  assert.equal(typeof api.acknowledgeVpnAbuseIncident, "function");
  await api.getVpnAbuseIncidents();
  await api.getVpnAbuseIncidents("acknowledged", { afterId: 100, limit: 100 });
  await api.acknowledgeVpnAbuseIncident(8);
  assert.deepEqual(calls.map(({ url, init }) => [url, init.method ?? "GET", init.body ?? null]), [
    ["/api/control/vpn/abuse/incidents?state=open&after_id=0&limit=100", "GET", null],
    ["/api/control/vpn/abuse/incidents?state=acknowledged&after_id=100&limit=100", "GET", null],
    ["/api/control/vpn/abuse/incidents/8/acknowledge", "POST", "{}"],
  ]);
  for (const { init } of calls) {
    assert.equal(init.cache, "no-store");
    assert.equal(init.credentials, "include");
  }
});

test("events mounts abuse review only in its active session and customer navigation targets the workspace", async () => {
  const app = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(app, /vpnAdminSection === "events"[\s\S]*?<VpnAbusePanel/);
  assert.match(app, /contextKey=\{session\?\.user\.id \?\? 0\}/);
  assert.match(app, /onOpenCustomer=\{openVpnAbuseCustomer\}/);
  assert.match(app, /requestedSelection=\{vpnCustomerSelection\}/);
  const workspace = await readFile(new URL("../src/VpnCustomerWorkspacePanel.tsx", import.meta.url), "utf8");
  assert.match(workspace, /requestedSelection\?:/);
  assert.match(workspace, /setFilter\("all"\)/);
  assert.match(workspace, /setQuery\(""\)/);
});

test("device context lookup uses both bounded identities and no cache", async () => {
  let call;
  globalThis.fetch = async (url, init) => {
    call = { url: String(url), init };
    return { ok: true, json: async () => ({ device_id: 107 }) };
  };
  assert.equal(typeof api.getVpnAbuseDeviceContext, "function");
  await api.getVpnAbuseDeviceContext(107, 7);
  assert.equal(call.url, "/api/control/vpn/abuse/devices/107/context?customer_id=7");
  assert.equal(call.init.method ?? "GET", "GET");
  assert.equal(call.init.body, undefined);
  assert.equal(call.init.cache, "no-store");
});

test("device context must bind its customer, subscription and representative child to the requested device", async () => {
  const { matchesAbuseDeviceContext } = await helpers();
  assert.equal(typeof matchesAbuseDeviceContext, "function");
  const selection = { customerId: 7, deviceId: 107 };
  const context = { device_id: 107, customer: { id: 7 }, subscription: { id: 27, customer_id: 7 },
    access_key: { id: 99, device_id: 107, subscription_id: 27 } };
  assert.equal(matchesAbuseDeviceContext(context, selection), true);
  assert.equal(matchesAbuseDeviceContext({ ...context, access_key: null }, selection), true);
  assert.equal(matchesAbuseDeviceContext({ ...context, device_id: 108 }, selection), false);
  assert.equal(matchesAbuseDeviceContext({ ...context, customer: { id: 8 } }, selection), false);
  assert.equal(matchesAbuseDeviceContext({ ...context, subscription: { id: 27, customer_id: 8 } }, selection), false);
  assert.equal(matchesAbuseDeviceContext({ ...context, access_key: { ...context.access_key, subscription_id: 28 } }, selection), false);
  assert.equal(matchesAbuseDeviceContext({ ...context, access_key: { ...context.access_key, device_id: 108 } }, selection), false);
});

test("abuse pages merge by incident ID in cursor order without mutating prior evidence", async () => {
  const { mergeAbuseIncidents, VPN_ABUSE_PAGE_SIZE } = await helpers();
  assert.equal(typeof mergeAbuseIncidents, "function");
  assert.equal(VPN_ABUSE_PAGE_SIZE, 100);
  const original = Object.freeze([Object.freeze(incident(2)), Object.freeze(incident(1))]);
  const updated = { ...incident(2), peak_source_count: 5 };
  const incoming = Object.freeze([Object.freeze(incident(3)), Object.freeze(updated)]);
  assert.deepEqual(mergeAbuseIncidents(original, incoming), [incident(1), updated, incident(3)]);
  assert.deepEqual(original.map((row) => row.id), [2, 1]);
  assert.equal(original[0].peak_source_count, 3);
  assert.deepEqual(mergeAbuseIncidents([], []), []);
});

test("incident review state distinguishes a recorded acknowledgement from an open signal", async () => {
  const { incidentMatchesAbuseState } = await helpers();
  assert.equal(typeof incidentMatchesAbuseState, "function");
  const reviewed = incident(2, "2026-10-25T12:10:00Z");
  assert.equal(incidentMatchesAbuseState(incident(1), "open"), true);
  assert.equal(incidentMatchesAbuseState(incident(1), "acknowledged"), false);
  assert.equal(incidentMatchesAbuseState(reviewed, "open"), false);
  assert.equal(incidentMatchesAbuseState(reviewed, "acknowledged"), true);
  assert.equal(incidentMatchesAbuseState({ ...reviewed, acknowledged_at: undefined }, "acknowledged"), false);
});

test("abuse panel uses the bounded transport and explains historical network-source evidence", async () => {
  const source = await componentSource();
  assert.match(source, /Подозрение на совместное использование/);
  assert.match(source, /Сетевых источников:/);
  assert.match(source, /максимум за период/);
  assert.match(source, /Это сигнал для проверки, а не число физических устройств/);
  assert.match(source, /Доступ не блокируется автоматически/);
  assert.match(source, /5 полных последовательных минутных измерений/);
  assert.match(source, /formatUsageTime\(incident\.started_at\)/);
  assert.match(source, /formatUsageTime\(incident\.last_observed_at\)/);
  assert.match(source, /transport\.listIncidents\(state, \{ afterId, limit: VPN_ABUSE_PAGE_SIZE \}\)/);
  assert.match(source, /page\.length === VPN_ABUSE_PAGE_SIZE/);
  assert.match(source, /mergeAbuseIncidents\(append \? previous : \[\], page\)/);
  assert.match(source, /incidentMatchesAbuseState\(incident, state\)/);
  assert.match(source, /Требуют проверки/);
  assert.match(source, /Проверенные/);
  assert.match(source, /role="status"/);
  assert.match(source, /role="alert"/);
  assert.doesNotMatch(source, /source_hash|source_ip|private_key|dangerouslySetInnerHTML|JSON\.stringify/);
  assert.doesNotMatch(source, /from "\.\/api"|revokeVpn|suspendVpn|restartVpn/);
});

test("list and acknowledgement responses cannot cross filter, session or customer navigation contexts", async () => {
  const source = await componentSource();
  assert.match(source, /useLayoutEffect\([\s\S]*?\[contextKey, state\]/);
  assert.match(source, /return \(\) => \{ contextRef\.current = \{\}; requestRef\.current = null; \}/);
  assert.match(source, /contextRef\.current === context && requestRef\.current === pending/);
  assert.match(source, /contextRef\.current !== context/);
  assert.match(source, /onOpenCustomer\(incident\.customer_id, incident\.device_id\)/);
  const openCustomer = source.match(/function openCustomer[\s\S]*?\n  \}/)?.[0] ?? "";
  assert.ok(openCustomer.indexOf("contextRef.current = {}") < openCustomer.indexOf("onOpenCustomer("));
});

test("acknowledgement has a synchronous guard and uncertain failures require a fresh list", async () => {
  const source = await componentSource();
  const acknowledge = source.match(/async function acknowledge\([\s\S]*?\n  \}\n\n  function openCustomer/)?.[0] ?? "";
  assert.match(acknowledge, /if \(acknowledgementRef\.current\) return/);
  assert.ok(acknowledge.indexOf("acknowledgementRef.current = pending") < acknowledge.indexOf("await transport.acknowledgeIncident(incident.id)"));
  assert.match(acknowledge, /saved\.id !== incident\.id/);
  assert.match(acknowledge, /saved\.device_id !== incident\.device_id/);
  assert.match(acknowledge, /saved\.customer_id !== incident\.customer_id/);
  assert.match(acknowledge, /saved\.acknowledged_at === null/);
  assert.match(acknowledge, /requestRef\.current = null/);
  assert.match(acknowledge, /previous\.filter\(\(row\) => row\.id !== incident\.id\)/);
  assert.match(acknowledge, /Обновите список, чтобы узнать, сохранена ли отметка/);
  const failure = acknowledge.match(/\} catch \{[\s\S]*?\} finally \{/)?.[0] ?? "";
  assert.match(failure, /requestRef\.current = null/);
  assert.match(source, /acknowledgementError\?\.id === incident\.id/);
  assert.match(source, /Обновить список/);
  assert.doesNotMatch(source, /error\.message|caught\.message|String\(error\)/);
});
