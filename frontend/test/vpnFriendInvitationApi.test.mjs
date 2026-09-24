import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("friend invitation API uses the exact five typed control endpoints", async () => {
  const invitation = {
    slot: 1,
    invite_state: "unused",
    telegram_user_id: null,
    telegram_username: null,
    display_name: null,
    subscription_expires_at: null,
    provisioning_error_code: null,
    can_rotate: true,
    can_retry: false,
    can_disable: false,
  };
  const issued = {
    invitation,
    invite_link: "https://t.me/veltrix_vpn_official_bot?start=i_private",
  };
  const responses = [[invitation], issued, issued, invitation, invitation];
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => responses[calls.length - 1],
    };
  };

  assert.deepEqual(await api.getVpnFriendInvitations(), [invitation]);
  assert.deepEqual(await api.issueVpnFriendInvitation(), issued);
  assert.deepEqual(await api.rotateVpnFriendInvitation(3), issued);
  assert.deepEqual(await api.retryVpnFriendInvitation(4), invitation);
  assert.deepEqual(await api.disableVpnFriendInvitation(5), invitation);

  assert.deepEqual(
    calls.map(({ url, init }) => [url, init?.method ?? "GET"]),
    [
      ["/api/control/vpn/friend-invitations", "GET"],
      ["/api/control/vpn/friend-invitations", "POST"],
      ["/api/control/vpn/friend-invitations/3/rotate", "POST"],
      ["/api/control/vpn/friend-invitations/4/retry", "POST"],
      ["/api/control/vpn/friend-invitations/5/disable", "POST"],
    ],
  );
  assert.equal(calls[1].init?.cache, "no-store");
  assert.equal(calls[2].init?.cache, "no-store");
});

test("only create and rotate response types expose the one-time invite link", async () => {
  const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const invitationType = source.match(
    /export type VpnFriendInvitation = \{([\s\S]*?)\n\};/,
  );
  const issuedType = source.match(
    /export type VpnFriendInvitationIssued = \{([\s\S]*?)\n\};/,
  );

  assert.ok(invitationType, "VpnFriendInvitation type must exist");
  assert.ok(issuedType, "VpnFriendInvitationIssued type must exist");
  assert.doesNotMatch(invitationType[1], /invite_link/);
  assert.match(issuedType[1], /invitation: VpnFriendInvitation;/);
  assert.match(issuedType[1], /invite_link: string;/);
  assert.match(
    source,
    /getVpnFriendInvitations: \(\) =>\s*request<VpnFriendInvitation\[\]>/,
  );
  assert.match(
    source,
    /issueVpnFriendInvitation: \(\) =>\s*request<VpnFriendInvitationIssued>/,
  );
  assert.match(
    source,
    /rotateVpnFriendInvitation: \(slot: number\) =>\s*request<VpnFriendInvitationIssued>/,
  );
  assert.match(
    source,
    /retryVpnFriendInvitation: \(slot: number\) =>\s*request<VpnFriendInvitation>/,
  );
  assert.match(
    source,
    /disableVpnFriendInvitation: \(slot: number\) =>\s*request<VpnFriendInvitation>/,
  );
});
