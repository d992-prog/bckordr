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

function trial(state) {
  return {
    state,
    duration_days: 7,
    profile_limit: 1,
    subscription_id: null,
    access_key_id: null,
    expires_at: null,
  };
}

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
  assert.equal(buildPortalHomeView([], [], trial("preparing")).kind, "preparing");
  assert.equal(buildPortalHomeView([activeSubscription], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([{ ...activeSubscription, state: "trial" }], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([{ ...activeSubscription, state: "expired" }], [], null).kind, "expired");
  assert.deepEqual(buildPortalHomeView([], [], null), {
    kind: "empty",
    title: "VPN‑профиля пока нет",
    detail: "Получите пробный доступ или напишите в поддержку",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([{ ...activeSubscription, state: "expired" }], [], null), {
    kind: "expired",
    title: "Срок доступа закончился",
    detail: "Выберите доступный тариф или напишите в поддержку",
    profileId: null,
  });
});

test("uses honest copy for every trial state when no profile is ready", () => {
  assert.deepEqual(buildPortalHomeView([], [], trial("available")), {
    kind: "empty",
    title: "VPN‑профиля пока нет",
    detail: "Получите пробный доступ или напишите в поддержку",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([], [], trial("capacity_paused")), {
    kind: "paused",
    title: "Выдача доступа временно приостановлена",
    detail: "Попробуйте позже или напишите в поддержку",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([], [], trial("preparing")), {
    kind: "preparing",
    title: "Профиль готовится",
    detail: "Это может занять несколько минут",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([], [], trial("active")), {
    kind: "preparing",
    title: "Обновляем данные профиля",
    detail: "Попробуйте открыть кабинет через несколько секунд",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([], [], trial("used")), {
    kind: "empty",
    title: "Пробный доступ уже использован",
    detail: "Выберите доступный тариф или напишите в поддержку",
    profileId: null,
  });
  assert.deepEqual(buildPortalHomeView([], [], trial("disabled")), {
    kind: "empty",
    title: "Пробный доступ недоступен",
    detail: "Напишите в поддержку, чтобы уточнить доступные варианты",
    profileId: null,
  });
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
  assert.equal(view.detail, "Доступ без ограничения по сроку");
});

test("refreshes an orphaned active profile instead of claiming that no profile exists", () => {
  const view = buildPortalHomeView([], [
    { id: 50, subscription_id: 404, display_name: "Laptop", state: "active", can_connect: true },
  ], null);

  assert.deepEqual(view, {
    kind: "preparing",
    title: "Обновляем данные профиля",
    detail: "Попробуйте открыть кабинет через несколько секунд",
    profileId: null,
  });
});

test("uses recency to distinguish a current terminal subscription from history", () => {
  const historicalExpired = {
    ...activeSubscription,
    id: 30,
    state: "expired",
    expires_at: "2025-01-01T00:00:00Z",
  };
  const currentPaused = {
    ...activeSubscription,
    id: 31,
    state: "disabled",
    expires_at: "2026-01-01T00:00:00Z",
  };
  const historicalPaused = { ...currentPaused, id: 32, expires_at: "2025-01-01T00:00:00Z" };
  const currentExpired = { ...historicalExpired, id: 33, expires_at: "2026-01-01T00:00:00Z" };

  assert.equal(buildPortalHomeView([historicalExpired, currentPaused], [], null).kind, "paused");
  assert.equal(buildPortalHomeView([historicalPaused, currentExpired], [], null).kind, "expired");
});

test("uses the higher id as a deterministic tie-break for equally dated terminal records", () => {
  const expiry = "2026-01-01T00:00:00Z";
  const paused = { ...activeSubscription, id: 40, state: "disabled", expires_at: expiry };
  const expired = { ...activeSubscription, id: 41, state: "expired", expires_at: expiry };

  assert.equal(buildPortalHomeView([expired, paused], [], null).kind, "expired");
  assert.equal(buildPortalHomeView([paused, expired], [], null).kind, "expired");
});

test("prefers a subscription associated with an existing inactive profile", () => {
  const associatedExpired = {
    ...activeSubscription,
    id: 60,
    state: "expired",
    expires_at: "2025-01-01T00:00:00Z",
  };
  const unassociatedPaused = {
    ...activeSubscription,
    id: 61,
    state: "disabled",
    expires_at: "2026-01-01T00:00:00Z",
  };
  const inactiveProfile = {
    id: 62,
    subscription_id: 60,
    display_name: "Old phone",
    state: "revoked",
    can_connect: false,
  };

  assert.equal(
    buildPortalHomeView([unassociatedPaused, associatedExpired], [inactiveProfile], null).kind,
    "expired",
  );
});

test("an active subscription outranks a historical terminal subscription with an inactive profile", () => {
  const historicalSubscription = {
    ...activeSubscription,
    id: 70,
    state: "expired",
    expires_at: "2025-01-01T00:00:00Z",
  };
  const currentSubscription = { ...activeSubscription, id: 71 };
  const historicalProfile = {
    id: 72,
    subscription_id: 70,
    display_name: "Old phone",
    state: "revoked",
    can_connect: false,
  };

  assert.deepEqual(
    buildPortalHomeView([historicalSubscription, currentSubscription], [historicalProfile], null),
    {
      kind: "preparing",
      title: "Профиль готовится",
      detail: "Это может занять несколько минут",
      profileId: null,
    },
  );
});

test("reports scheduled access with a safe start date", () => {
  for (const [startsAt, detail] of [
    ["2026-12-10T00:00:00Z", "Начало — 10 декабря 2026 г."],
    [null, "Дата начала уточняется"],
    ["not-a-date", "Дата начала уточняется"],
  ]) {
    const view = buildPortalHomeView([
      { ...activeSubscription, state: "scheduled", starts_at: startsAt },
    ], [], null);

    assert.deepEqual(view, {
      kind: "preparing",
      title: "Доступ начнётся позже",
      detail,
      profileId: null,
    });
    assert.doesNotMatch(`${view.title} ${view.detail}`, /Получите пробный доступ|подключено|защищено/i);
  }
});

test("scheduled access outranks a terminal subscription with an inactive profile", () => {
  const scheduled = {
    ...activeSubscription,
    id: 80,
    state: "scheduled",
    starts_at: "2026-12-10T00:00:00Z",
  };
  const historicalPaused = {
    ...activeSubscription,
    id: 81,
    state: "disabled",
    expires_at: "2025-01-01T00:00:00Z",
  };
  const historicalProfile = {
    id: 82,
    subscription_id: 81,
    display_name: "Old phone",
    state: "revoked",
    can_connect: false,
  };

  const view = buildPortalHomeView([historicalPaused, scheduled], [historicalProfile], null);

  assert.equal(view.kind, "preparing");
  assert.equal(view.title, "Доступ начнётся позже");
  assert.doesNotMatch(`${view.title} ${view.detail}`, /Получите пробный доступ|подключено|защищено/i);
});

test("reuses safe date formatting for an invalid expiry", () => {
  const view = buildPortalHomeView(
    [{ ...activeSubscription, expires_at: "not-a-date" }],
    [{ id: 40, subscription_id: 1, display_name: "Laptop", state: "active", can_connect: true }],
    null,
  );

  assert.equal(view.detail, "Срок доступа уточняется");
});
