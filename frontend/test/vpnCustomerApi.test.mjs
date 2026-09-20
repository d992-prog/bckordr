import test from "node:test";
import assert from "node:assert/strict";

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
