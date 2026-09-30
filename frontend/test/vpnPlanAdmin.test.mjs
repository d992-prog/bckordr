import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("VPN plans expose publication fields and PATCH only public visibility", async () => {
  let call;
  globalThis.fetch = async (url, init) => {
    call = { url: String(url), init };
    return { ok: true, json: async () => ({ id: 17 }) };
  };

  await api.updateVpnPlan(17, { is_public: true });

  assert.equal(call.url, "/api/control/vpn/plans/17");
  assert.equal(call.init?.method, "PATCH");
  assert.deepEqual(JSON.parse(call.init?.body), { is_public: true });

  const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const planType = source.match(/export type VpnPlan = \{[\s\S]*?\n\};/)?.[0] ?? "";
  assert.match(planType, /is_public: boolean;/);
  assert.match(planType, /display_order: number;/);
});

test("admin plan form and table manage public visibility without a second editor", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.match(source, /is_public: vpnPlanForm\.isPublic/);
  assert.match(source, /display_order: Number\(vpnPlanForm\.displayOrder \|\| 0\)/);
  assert.match(source, /<span>Порядок показа<\/span><input type="number" min="0"/);
  assert.match(
    source,
    /api\.updateVpnPlan\(plan\.id, \{ is_public: !plan\.is_public \}\)/,
  );
  assert.match(source, /plan\.is_public \? "Публичный" : "Скрыт"/);
  assert.match(source, /plan\.display_order/);
  assert.match(source, /plan\.is_public \? "Скрыть" : "Опубликовать"/);
  assert.match(source, /disabled=\{publishingVpnPlanId !== null\}/);
});
