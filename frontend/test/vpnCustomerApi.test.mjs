import test from "node:test";
import assert from "node:assert/strict";

test("device usage is read from the authoritative subscription endpoint", async () => {
  const calls = [];
  const usage = { subscription_id: 11, devices_used: 2, max_devices: 3 };
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return { ok: true, json: async () => usage };
  };
  const { api } = await import("../src/api.ts");
  assert.deepEqual(await api.getVpnSubscriptionDeviceUsage(11), usage);
  assert.equal(calls[0].url, "/api/control/vpn/subscriptions/11/device-usage");
  assert.equal(calls[0].init.credentials, "include");
  assert.equal(calls[0].init.cache, "no-store");
  assert.equal(calls[0].init.method ?? "GET", "GET");
  assert.equal(calls[0].init.body, undefined);
});

test("archives a VPN customer through the typed control endpoint", async () => {
  let capturedUrl = "";
  let capturedInit = null;
  globalThis.fetch = async (url, init) => {
    capturedUrl = String(url);
    capturedInit = init;
    return {
      ok: true,
      json: async () => ({
        customer: { id: 42, status: "archived" },
        disabled_subscriptions: 1,
        revoked_keys: 1,
        pending_revoke_keys: 0,
      }),
    };
  };
  const { api } = await import("../src/api.ts");

  const result = await api.archiveVpnCustomer(42);

  assert.equal(capturedUrl, "/api/control/vpn/customers/42/archive");
  assert.equal(capturedInit?.method, "POST");
  assert.equal(result.customer.status, "archived");
});

test("renames a VPN profile through the display-name-only endpoint", async () => {
  let capturedUrl = "";
  let capturedInit = null;
  globalThis.fetch = async (url, init) => {
    capturedUrl = String(url);
    capturedInit = init;
    return {
      ok: true,
      json: async () => ({
        id: 7,
        display_name: "Личный ноутбук",
        config_uri: "vless://labelled",
      }),
    };
  };
  const { api } = await import("../src/api.ts");

  const result = await api.renameVpnAccessKey(7, "Личный ноутбук");

  assert.equal(capturedUrl, "/api/control/vpn/access-keys/7/display-name");
  assert.equal(capturedInit?.method, "PATCH");
  assert.equal(
    capturedInit?.body,
    JSON.stringify({ display_name: "Личный ноутбук" }),
  );
  assert.equal(result.config_uri, "vless://labelled");
});
