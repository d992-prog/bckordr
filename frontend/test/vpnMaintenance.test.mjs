import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { isVpnConfigurationActionDisabled } from "../src/vpnMaintenance.ts";

test("VPN configuration actions stay disabled until installation is confirmed", () => {
  assert.equal(
    isVpnConfigurationActionDisabled({
      vpnInstalled: false,
      vpnMaintenanceBlocked: false,
    }),
    true,
  );
});

test("VPN configuration actions are enabled only for an installed idle node", () => {
  assert.equal(
    isVpnConfigurationActionDisabled({
      vpnInstalled: true,
      vpnMaintenanceBlocked: false,
    }),
    false,
  );
  assert.equal(
    isVpnConfigurationActionDisabled({
      vpnInstalled: true,
      vpnMaintenanceBlocked: true,
    }),
    true,
  );
});

test("legacy VPN actions remain available inside collapsed diagnostics", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.ok((source.match(/<details className="vpn-node-diagnostics">/g) ?? []).length >= 2);
  assert.ok((source.match(/<summary>Диагностика<\/summary>/g) ?? []).length >= 2);
  for (const action of [
    "checkWorkerVpn",
    "installWorkerVpn",
    "autoconfigWorkerVpn",
    "createWorkerVpnInbound",
    "updateWorkerVpn",
    "restartWorkerVpn",
  ]) {
    assert.match(source, new RegExp(action));
  }
});
