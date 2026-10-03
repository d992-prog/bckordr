import test from "node:test";
import assert from "node:assert/strict";

import { buildPortalHomeView } from "../src/vpn-portal/homeView.ts";

const activeSubscription = {
  id: 1,
  service_name: "Veltrix VPN",
  state: "active",
  starts_at: null,
  expires_at: "2026-10-30T00:00:00Z",
  profile_limit: 1,
  profiles_used: 1,
  traffic_limit_gb_per_profile: null,
};

test("reports a ready profile without claiming an active tunnel", () => {
  const view = buildPortalHomeView(
    [activeSubscription],
    [{ id: 2, subscription_id: 1, display_name: "iPhone", state: "active", can_connect: true }],
    null,
  );

  assert.equal(view.kind, "ready");
  assert.equal(view.title, "VPN‑профиль готов");
  assert.equal(view.profileId, 2);
  assert.equal(view.detail, "Доступ действует до 30 октября 2026 г.");
  assert.doesNotMatch(`${view.title} ${view.detail}`, /подключено|защищено/i);
});

test("keeps preparing, expired, and empty states explicit", () => {
  assert.equal(buildPortalHomeView([], [], {
    state: "preparing",
    duration_days: 7,
    profile_limit: 1,
    subscription_id: null,
    access_key_id: null,
    expires_at: null,
  }).kind, "preparing");
  assert.equal(buildPortalHomeView([activeSubscription], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([{ ...activeSubscription, state: "trial" }], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([{ ...activeSubscription, state: "expired" }], [], null).kind, "expired");
  assert.equal(buildPortalHomeView([], [], null).kind, "empty");
});

test("reports disabled and suspended subscriptions as paused", () => {
  for (const state of ["disabled", "suspended"]) {
    const view = buildPortalHomeView([{ ...activeSubscription, state }], [], null);

    assert.deepEqual(view, {
      kind: "paused",
      title: "Доступ приостановлен",
      detail: "Напишите в поддержку, чтобы уточнить причину",
      profileId: null,
    });
  }
});

test("uses only a connectable active profile from an active entitlement", () => {
  const staleSubscriptions = [
    { ...activeSubscription, id: 8, state: "expired" },
    { ...activeSubscription, id: 9, state: "disabled" },
  ];
  const currentSubscription = { ...activeSubscription, id: 10, state: "trial", expires_at: null };
  const profiles = [
    { id: 20, subscription_id: 8, display_name: "Old", state: "active", can_connect: true },
    { id: 21, subscription_id: 10, display_name: "Pending", state: "pending_sync", can_connect: true },
    { id: 22, subscription_id: 10, display_name: "Ready", state: "active", can_connect: true },
  ];

  const view = buildPortalHomeView([...staleSubscriptions, currentSubscription], profiles, null);

  assert.equal(view.kind, "ready");
  assert.equal(view.profileId, 22);
  assert.equal(view.detail, "Доступ действует до Без срока окончания");
});

test("prefers a current preparing or paused state over historical expiry", () => {
  const expired = { ...activeSubscription, id: 30, state: "expired" };
  const paused = { ...activeSubscription, id: 31, state: "disabled" };

  assert.equal(buildPortalHomeView([expired, activeSubscription], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([expired, paused], [], null).kind, "paused");
});

test("reuses safe date formatting for an invalid expiry", () => {
  const view = buildPortalHomeView(
    [{ ...activeSubscription, expires_at: "not-a-date" }],
    [{ id: 40, subscription_id: 1, display_name: "Laptop", state: "active", can_connect: true }],
    null,
  );

  assert.equal(view.detail, "Доступ действует до Дата недоступна");
});
