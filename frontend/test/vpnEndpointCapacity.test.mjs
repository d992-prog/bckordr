import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";
import {
  replaceVpnEndpointCapacity,
  validateVpnEndpointCapacity,
} from "../src/vpnEndpointCapacity.ts";

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
  assert.match(source, /aria-label=/);
  assert.match(source, /aria-live="polite"/);
  assert.match(source, /disabled=\{saving\}/);
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
  assert.match(css, /\.vpn-capacity-grid\s*\{/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.vpn-capacity-fields/);
});
