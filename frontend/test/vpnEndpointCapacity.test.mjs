import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";
import * as endpointCapacity from "../src/vpnEndpointCapacity.ts";
import {
  replaceVpnEndpointCapacity,
  validateVpnEndpointCapacity,
} from "../src/vpnEndpointCapacity.ts";

test("capacity saves retain newer publication, health and occupancy from a refreshed row", () => {
  assert.equal(typeof endpointCapacity.applyVpnEndpointCapacityUpdate, "function");
  const current = { endpoint_id: 7, worker_id: 3, status: "maintenance", occupied_profiles: 8,
    max_active_profiles: 20, capacity_warning_percent: 80, is_published: true, public_name: "Новое имя", country_code: "NL" };
  const response = { ...current, status: "ready", occupied_profiles: 2, public_name: "Старое имя", country_code: "DE",
    is_published: false, max_active_profiles: 30, capacity_warning_percent: 70 };
  assert.deepEqual(endpointCapacity.applyVpnEndpointCapacityUpdate(current, response, { ...current }), {
    ...current, max_active_profiles: 30, capacity_warning_percent: 70,
  });
});

test("publication saves retain newer capacity, health and occupancy from a refreshed row", () => {
  assert.equal(typeof endpointCapacity.applyVpnEndpointPublicationUpdate, "function");
  const current = { endpoint_id: 7, worker_id: 3, status: "maintenance", occupied_profiles: 8,
    max_active_profiles: 50, capacity_warning_percent: 90, is_published: false, public_name: null, country_code: null };
  const response = { ...current, status: "ready", occupied_profiles: 2, max_active_profiles: 10, capacity_warning_percent: 80,
    is_published: true, public_name: "Германия", country_code: "DE" };
  assert.deepEqual(endpointCapacity.applyVpnEndpointPublicationUpdate(current, response, { ...current }), {
    ...current, is_published: true, public_name: "Германия", country_code: "DE",
  });
});

test("without an intervening refresh both save replies apply their fresh full snapshot", () => {
  const original = { endpoint_id: 7, worker_id: 3, status: "ready", occupied_profiles: 2,
    max_active_profiles: 50, capacity_warning_percent: 80, is_published: false, public_name: null, country_code: null };
  const saved = { ...original, status: "maintenance", occupied_profiles: 8, public_name: "Германия", country_code: "DE" };
  assert.equal(endpointCapacity.applyVpnEndpointCapacityUpdate(original, saved, original), saved);
  assert.equal(endpointCapacity.applyVpnEndpointPublicationUpdate(original, saved, original), saved);
});

test("row mutation lifetime follows endpoint identity rather than each polling snapshot", async () => {
  const source = (await readFile(new URL("../src/VpnEndpointCapacityPanel.tsx", import.meta.url), "utf8")).replace(/\r\n/g, "\n");
  assert.match(source, /requestRef\.current = null;[\s\S]*?\}, \[endpoint\.endpoint_id, endpoint\.worker_id\]\)/);
  assert.doesNotMatch(source, /requestRef\.current = null;[^]*?return \(\) => \{ requestRef\.current = null; \};\n  \}, \[endpoint\]\)/);
});

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("capacity API uses the exact typed list and update endpoints", async () => {
  const row = {
    endpoint_id: 7,
    worker_id: 3,
    label: "edge.example.net:443",
    status: "ready",
    occupied_profiles: 2,
    max_active_profiles: 50,
    capacity_warning_percent: 80,
  };
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, json: async () => (calls.length === 1 ? [row] : row) };
  };

  assert.deepEqual(await api.getVpnEndpointCapacities(), [row]);
  assert.deepEqual(
    await api.updateVpnEndpointCapacity(7, {
      max_active_profiles: 50,
      capacity_warning_percent: 80,
    }),
    row,
  );
  assert.deepEqual(
    calls.map(({ url, init }) => [url, init?.method ?? "GET", init?.body ?? null]),
    [
      ["/api/control/vpn/endpoints/capacity", "GET", null],
      [
        "/api/control/vpn/endpoints/7/capacity",
        "PATCH",
        JSON.stringify({ max_active_profiles: 50, capacity_warning_percent: 80 }),
      ],
    ],
  );
});

test("publication has its own admin JSON mutation and preserves endpoint health", async () => {
  let call;
  const row = { endpoint_id: 7, status: "unhealthy", is_published: true, public_name: "Германия", country_code: "DE" };
  globalThis.fetch = async (url, init) => {
    call = { url: String(url), init };
    return { ok: true, json: async () => row };
  };
  const payload = { is_published: true, public_name: "Германия", country_code: "DE" };
  assert.deepEqual(await api.updateVpnEndpointPublication(7, payload), row);
  assert.equal(call.url, "/api/control/vpn/endpoints/7/publication");
  assert.equal(call.init.method, "PATCH");
  assert.equal(call.init.credentials, "include");
  assert.equal(call.init.headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(call.init.body), payload);
});

test("publication controls are a separate form with visible consequences", async () => {
  const source = await readFile(new URL("../src/VpnEndpointCapacityPanel.tsx", import.meta.url), "utf8");
  assert.equal((source.match(/<form /g) ?? []).length, 2);
  for (const label of ["Название для клиентов", "Код страны", "Показывать клиентам", "Сохранить публикацию",
    "Скрытие убирает сервер из нового выбора. Выданные подключения продолжат работать"]) {
    assert.ok(source.includes(label), label);
  }
  assert.match(source, /api\.updateVpnEndpointPublication/);
});

test("capacity validation accepts an unset limit and matches backend integer ranges", () => {
  assert.deepEqual(validateVpnEndpointCapacity("", "80"), {
    max_active_profiles: null,
    capacity_warning_percent: 80,
  });
  assert.deepEqual(validateVpnEndpointCapacity("100000", "1"), {
    max_active_profiles: 100_000,
    capacity_warning_percent: 1,
  });
  for (const [maximum, warning] of [
    ["0", "80"],
    ["-1", "80"],
    ["100001", "80"],
    ["1.5", "80"],
    ["10", "0"],
    ["10", "101"],
    ["10", "1.5"],
    ["10", ""],
  ]) {
    assert.throws(
      () => validateVpnEndpointCapacity(maximum, warning),
      /Введите целое число/,
    );
  }
});

test("a saved endpoint replaces only its own local row", () => {
  const first = { endpoint_id: 1, max_active_profiles: 10 };
  const second = { endpoint_id: 2, max_active_profiles: 20 };
  const updated = { endpoint_id: 1, max_active_profiles: 50 };
  assert.deepEqual(
    replaceVpnEndpointCapacity([first, second], updated),
    [updated, second],
  );
});

test("capacity panel exposes accessible native controls and bounded errors", async () => {
  const source = await readFile(
    new URL("../src/VpnEndpointCapacityPanel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /type="number"/);
  assert.match(source, /min="1"/);
  assert.match(source, /max="100000"/);
  assert.match(source, /max="100"/);
  assert.match(source, /Пусто — лимит не задан/);
  assert.doesNotMatch(source, /Пусто — без лимита/);
  assert.match(source, /Точка подключения #\{endpoint\.endpoint_id\}/);
  assert.match(source, /VPN‑нода #\{endpoint\.worker_id\}/);
  assert.match(source, /Точки подключения пока не зарегистрированы/);
  assert.doesNotMatch(source, /VPN‑нода #\{endpoint\.endpoint_id\}/);
  assert.doesNotMatch(source, /Endpoint #|VPN endpoint’ы/);
  assert.match(source, /aria-label=/);
  assert.match(source, /role="alert">\{error\}/);
  assert.match(source, /role="status">\{message\}/);
  assert.match(source, /disabled=\{saving \|\| publishing\}/);
  assert.match(source, /api\.updateVpnEndpointCapacity/);
  assert.match(source, /\.slice\(0, 160\)/);
  assert.doesNotMatch(source, /public_key|short_id|config_uri|external_uuid/);
});

test("VPN workspace loads capacity safely and preserves a newer saved row", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(source, /api\.getVpnEndpointCapacities\(\)/);
  assert.match(source, /vpnCapacityMutationGenerationRef/);
  assert.match(
    source,
    /capacityMutationGeneration === vpnCapacityMutationGenerationRef\.current/,
  );
  assert.match(source, /replaceVpnEndpointCapacity/);
  assert.match(source, /<VpnEndpointCapacityPanel/);
  assert.match(source, />Ёмкость точек подключения</);
  assert.doesNotMatch(source, />Ёмкость VPN‑нод</);
  assert.doesNotMatch(source, />VPN endpoint<|>Endpoint<|data-label="Endpoint"/);
  assert.match(css, /\.vpn-capacity-grid\s*\{/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.vpn-capacity-fields/);
});
