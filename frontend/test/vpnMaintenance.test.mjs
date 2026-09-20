import test from "node:test";
import assert from "node:assert/strict";

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
