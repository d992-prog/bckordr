import test from "node:test";
import assert from "node:assert/strict";

import {
  applyAccessKeyDisplay,
  accessKeyStatusLabel,
  calculateExtendedExpiration,
  classifyVpnCustomer,
  customerStatusOptions,
  filterVpnCustomers,
  nextAccessKeyEditorAfterRename,
  reconcileAccessKeyDisplayOverrides,
  selectPrimarySubscription,
  saveSubscriptionAndRequestSync,
  saveAccessKeyDisplayName,
  shouldApplyLoadGeneration,
} from "../src/vpnCustomerWorkspace.ts";
import { readFile } from "node:fs/promises";

const now = new Date("2026-09-20T12:00:00.000Z");
const customers = [
  {
    id: 1,
    first_name: "Anna",
    last_name: "Kuznetsova",
    telegram_username: "anna",
    telegram_user_id: "100",
    status: "active",
    notes: null,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
  },
  {
    id: 2,
    first_name: "Max",
    last_name: null,
    telegram_username: "max_s",
    telegram_user_id: "200",
    status: "active",
    notes: null,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
  },
  {
    id: 3,
    first_name: "Old",
    last_name: "Client",
    telegram_username: "old",
    telegram_user_id: "300",
    status: "archived",
    notes: null,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
  },
];
const subscriptions = [
  {
    id: 10,
    customer_id: 1,
    plan_id: 1,
    status: "active",
    starts_at: null,
    expires_at: "2026-09-24T12:00:00.000Z",
    traffic_limit_gb: 100,
    max_devices: 3,
    notes: null,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
  },
  {
    id: 11,
    customer_id: 1,
    plan_id: 1,
    status: "expired",
    starts_at: null,
    expires_at: "2026-08-01T00:00:00.000Z",
    traffic_limit_gb: 100,
    max_devices: 3,
    notes: null,
    created_at: now.toISOString(),
    updated_at: "2026-08-01T00:00:00.000Z",
  },
  {
    id: 20,
    customer_id: 2,
    plan_id: null,
    status: "disabled",
    starts_at: null,
    expires_at: null,
    traffic_limit_gb: null,
    max_devices: 1,
    notes: null,
    created_at: now.toISOString(),
    updated_at: now.toISOString(),
  },
];

test("selects a usable subscription before historical rows", () => {
  assert.equal(selectPrimarySubscription(1, subscriptions)?.id, 10);
});

test("classifies expiring, suspended, and archived customers", () => {
  assert.equal(classifyVpnCustomer(customers[0], subscriptions, now), "expiring");
  assert.equal(
    classifyVpnCustomer(
      customers[0],
      [{ ...subscriptions[0], expires_at: "2026-09-19T12:00:00.000Z" }],
      now,
    ),
    "suspended",
  );
  assert.equal(classifyVpnCustomer(customers[1], subscriptions, now), "suspended");
  assert.equal(classifyVpnCustomer(customers[2], subscriptions, now), "archived");
});

test("searches identity fields and applies the selected operational filter", () => {
  assert.deepEqual(
    filterVpnCustomers(customers, subscriptions, "all", "@ANNA", now).map((item) => item.id),
    [1],
  );
  assert.deepEqual(
    filterVpnCustomers(customers, subscriptions, "all", "200", now).map((item) => item.id),
    [2],
  );
  assert.deepEqual(
    filterVpnCustomers(customers, subscriptions, "expiring", "", now).map((item) => item.id),
    [1],
  );
  assert.deepEqual(
    filterVpnCustomers(customers, subscriptions, "archived", "", now).map((item) => item.id),
    [3],
  );
});

test("extends from the later of current expiry and now", () => {
  assert.equal(
    calculateExtendedExpiration("2026-10-01T12:00:00.000Z", 30, now),
    "2026-10-31T12:00:00.000Z",
  );
  assert.equal(
    calculateExtendedExpiration("2026-09-01T12:00:00.000Z", 7, now),
    "2026-09-27T12:00:00.000Z",
  );
  assert.equal(calculateExtendedExpiration(null, 30, now), "2026-10-20T12:00:00.000Z");
});

test("reserves archive transitions for the safe archive action", () => {
  assert.deepEqual(customerStatusOptions("active"), ["active", "blocked"]);
  assert.deepEqual(customerStatusOptions("blocked"), ["active", "blocked"]);
  assert.deepEqual(customerStatusOptions("archived"), ["archived"]);
});

test("shows access-key states in operator-friendly language", () => {
  assert.equal(accessKeyStatusLabel("pending_sync"), "ожидает синхронизации");
  assert.equal(accessKeyStatusLabel("syncing"), "синхронизируется");
  assert.equal(accessKeyStatusLabel("pending_suspend"), "ожидает приостановки");
  assert.equal(accessKeyStatusLabel("suspended"), "приостановлен");
  assert.equal(accessKeyStatusLabel("pending_revoke"), "ожидает отзыва");
  assert.equal(accessKeyStatusLabel("active"), "активен");
  assert.equal(accessKeyStatusLabel("custom"), "custom");
});

test("a saved subscription survives a failed immediate sync request", async () => {
  const calls = [];
  const result = await saveSubscriptionAndRequestSync(
    async () => { calls.push("save"); },
    async () => { calls.push("sync"); throw new Error("offline"); },
    async () => { calls.push("reload"); },
  );
  assert.deepEqual(calls, ["save", "sync", "reload"]);
  assert.deepEqual(result, { syncRequested: false, refreshed: true });
});

test("failed save never requests synchronization", async () => {
  const calls = [];
  await assert.rejects(saveSubscriptionAndRequestSync(
    async () => { throw new Error("invalid policy"); },
    async () => { calls.push("sync"); },
    async () => { calls.push("reload"); },
  ), /invalid policy/);
  assert.deepEqual(calls, []);
});

test("a reload error does not misreport a completed subscription save", async () => {
  const result = await saveSubscriptionAndRequestSync(
    async () => {}, async () => {}, async () => { throw new Error("offline"); },
  );
  assert.deepEqual(result, { syncRequested: true, refreshed: false });
});

test("a renamed profile keeps the fresh labelled URI when reload fails", async () => {
  const original = {
    id: 7,
    display_name: "Старое имя",
    config_uri: "vless://old-labelled-uri",
    updated_at: "2026-09-21T10:00:00.000Z",
  };
  const renamed = {
    ...original,
    display_name: "Новое имя",
    config_uri: "vless://new-labelled-uri",
    updated_at: "2026-09-21T10:01:00.000Z",
  };
  const result = await saveAccessKeyDisplayName(
    async () => renamed,
    async () => { throw new Error("offline"); },
  );

  assert.deepEqual(result, { accessKey: renamed, refreshed: false });
  assert.deepEqual(
    applyAccessKeyDisplay(original, result.accessKey),
    renamed,
  );
  assert.equal(original.config_uri, "vless://old-labelled-uri");
});

test("a renamed profile publishes its fresh URI before reload settles", async () => {
  let releaseReload;
  const reloadPending = new Promise((resolve) => {
    releaseReload = resolve;
  });
  const published = [];
  const renamed = {
    id: 8,
    display_name: "Новое имя",
    config_uri: "vless://new-labelled-uri",
    updated_at: "2026-09-21T10:01:00.000Z",
  };

  const saving = saveAccessKeyDisplayName(
    async () => renamed,
    async () => reloadPending,
    (accessKey) => published.push(accessKey),
  );
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(published, [renamed]);
  releaseReload();
  assert.deepEqual(await saving, { accessKey: renamed, refreshed: true });
});

test("a failed profile rename does not reload or report a saved value", async () => {
  const calls = [];
  await assert.rejects(
    saveAccessKeyDisplayName(
      async () => { throw new Error("invalid name"); },
      async () => { calls.push("reload"); },
    ),
    /invalid name/,
  );
  assert.deepEqual(calls, []);
});

test("a stale reload cannot replace the optimistic renamed profile", () => {
  const override = {
    display_name: "Имя из админки",
    config_uri: "vless://admin-labelled",
    updated_at: "2026-09-21T10:01:00.900123Z",
  };
  const current = new Map([[7, override]]);
  const stale = [{
    id: 7,
    display_name: "Старое имя",
    config_uri: "vless://old-labelled",
    updated_at: "2026-09-21T10:01:00.100999Z",
  }];

  const reconciled = reconcileAccessKeyDisplayOverrides(current, stale);

  assert.equal(reconciled, current);
  assert.deepEqual(reconciled.get(7), override);
});

test("a late pre-rename load cannot replace a matching post-rename refresh", () => {
  const preRename = [{
    id: 7,
    display_name: "Старое имя",
    config_uri: "vless://old-labelled",
    updated_at: "2026-09-21T10:00:00.000000Z",
  }];
  const renamed = {
    id: 7,
    display_name: "Новое имя",
    config_uri: "vless://new-labelled",
    updated_at: "2026-09-21T10:01:00.000000Z",
  };
  const matchingRefresh = [renamed];
  const override = new Map([[7, renamed]]);

  let displayed = preRename;
  let appliedGeneration = 0;
  if (shouldApplyLoadGeneration(2, appliedGeneration)) {
    displayed = matchingRefresh;
    appliedGeneration = 2;
  }
  const cleared = reconcileAccessKeyDisplayOverrides(override, displayed);
  if (shouldApplyLoadGeneration(1, appliedGeneration)) {
    displayed = preRename;
    appliedGeneration = 1;
  }

  assert.equal(cleared.size, 0);
  assert.equal(appliedGeneration, 2);
  assert.deepEqual(displayed, matchingRefresh);
});

test("an older delayed PATCH cannot replace a newer external profile name", () => {
  const externallyRenamed = {
    id: 7,
    display_name: "Имя из кабинета",
    config_uri: "vless://cabinet-labelled",
    updated_at: "2026-09-21T10:02:00.000001Z",
  };
  const delayedPatch = {
    display_name: "Запоздавшее имя админа",
    config_uri: "vless://older-admin-labelled",
    updated_at: "2026-09-21T10:01:00.900123Z",
  };

  const displayed = applyAccessKeyDisplay(externallyRenamed, delayedPatch);

  assert.equal(displayed, externallyRenamed);
  assert.equal(displayed.display_name, "Имя из кабинета");
  assert.equal(displayed.config_uri, "vless://cabinet-labelled");
});

test("an older delayed PATCH cannot restore a URI after a newer revoke", () => {
  const revoked = {
    id: 7,
    status: "revoked",
    display_name: "Профиль",
    config_uri: null,
    updated_at: "2026-09-21T10:03:00.000001Z",
  };
  const delayedPatch = {
    display_name: "Старое имя",
    config_uri: "vless://must-not-return",
    updated_at: "2026-09-21T10:01:00.900123Z",
  };

  const displayed = applyAccessKeyDisplay(revoked, delayedPatch);

  assert.equal(displayed, revoked);
  assert.equal(displayed.status, "revoked");
  assert.equal(displayed.config_uri, null);
});

test("display precedence compares backend UTC timestamps beyond milliseconds", () => {
  const key = (updatedAt, name = "Авторитетный") => ({
    id: 7,
    display_name: name,
    config_uri: null,
    updated_at: updatedAt,
  });
  const display = (updatedAt) => ({
    display_name: "Переименованный",
    config_uri: "vless://labelled",
    updated_at: updatedAt,
  });

  assert.equal(
    applyAccessKeyDisplay(
      key("2026-09-21T10:00:00Z"),
      display("2026-09-21T10:00:00.000001Z"),
    ).display_name,
    "Переименованный",
    "a microsecond PATCH is newer than the exact second",
  );
  assert.equal(
    applyAccessKeyDisplay(
      key("2026-09-21T10:00:00.000001Z"),
      display("2026-09-21T10:00:00Z"),
    ).display_name,
    "Авторитетный",
    "an exact-second PATCH is older than the microsecond GET",
  );
  assert.equal(
    applyAccessKeyDisplay(
      key("2026-09-21T10:00:00.000999Z"),
      display("2026-09-21T10:00:00.000123Z"),
    ).display_name,
    "Авторитетный",
    "microseconds within one millisecond remain ordered",
  );
  assert.equal(
    applyAccessKeyDisplay(
      key("2026-09-21T10:00:00.901Z"),
      display("2026-09-21T10:00:00.900Z"),
    ).display_name,
    "Авторитетный",
    "ordinary milliseconds remain ordered",
  );
  assert.equal(
    applyAccessKeyDisplay(
      key("2026-09-21T10:00:00Z"),
      display("2026-09-21T12:00:00+02:00"),
    ).display_name,
    "Переименованный",
    "equivalent timezone encodings are not treated as newer",
  );
});

test("completion of one profile rename preserves another profile editor", () => {
  assert.equal(nextAccessKeyEditorAfterRename(8, 7), 8);
  assert.equal(nextAccessKeyEditorAfterRename(7, 7), null);
});

test("a newer external rename replaces the optimistic admin override", () => {
  const current = new Map([[7, {
    display_name: "Имя из админки",
    config_uri: "vless://admin-labelled",
    updated_at: "2026-09-21T10:01:00.900123Z",
  }]]);
  const externallyRenamed = [{
    id: 7,
    display_name: "Имя из кабинета",
    config_uri: "vless://cabinet-labelled",
    updated_at: "2026-09-21T10:02:00.000001Z",
  }];

  const reconciled = reconcileAccessKeyDisplayOverrides(current, externallyRenamed);

  assert.notEqual(reconciled, current);
  assert.equal(reconciled.has(7), false);
});

test("a newer revoked profile clears an optimistic non-null URI", () => {
  const current = new Map([[7, {
    display_name: "Имя из админки",
    config_uri: "vless://must-not-survive-revoke",
    updated_at: "2026-09-21T10:01:00.900123Z",
  }]]);
  const revoked = [{
    id: 7,
    display_name: "Имя из админки",
    config_uri: null,
    updated_at: "2026-09-21T10:03:00.000001Z",
  }];

  const reconciled = reconcileAccessKeyDisplayOverrides(current, revoked);

  assert.equal(reconciled.has(7), false);
});

test("admin profile UI uses display names and exposes inline rename controls", async () => {
  const source = await readFile(
    new URL("../src/VpnCustomerWorkspacePanel.tsx", import.meta.url),
    "utf8",
  );

  assert.equal(source.includes("accessKey.public_name"), false);
  assert.match(source, /display_name: accessKeyForm\.displayName\.trim\(\)/);
  assert.match(source, /api\.renameVpnAccessKey/);
  assert.match(source, /maxLength=\{64\}/);
  assert.match(source, />\s*Изменить название\s*</);
  assert.match(source, />\s*Отмена\s*</);
  assert.match(source, /const renameBusy = pendingAccessKeyRenameIds\.has\(accessKey\.id\)/);
  assert.match(source, /renameBusy \? \(\s*<p className="muted">Обновляем подпись ссылки…<\/p>/);
  assert.match(source, /nextAccessKeyEditorAfterRename\(current, accessKey\.id\)/);
  assert.doesNotMatch(source, /setBusyAction\(`key-rename-/);
});

test("workspace reload opts into load error propagation without changing other callers", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.match(
    source,
    /async function loadAll\(options\?: \{ silent\?: boolean; throwOnError\?: boolean \}\)/,
  );
  assert.match(source, /if \(options\?\.throwOnError\) \{\s*throw error;\s*\}/);
  assert.match(source, /reload=\{\(\) => loadAll\(\{ throwOnError: true \}\)\}/);
  assert.match(source, /shouldApplyLoadGeneration\(generation, lastAppliedLoadGenerationRef\.current\)/);
  assert.doesNotMatch(source, /lastAppliedLoadResultRef/);
});

test("long profile names and rename controls wrap with visible spacing", async () => {
  const [source, appSource] = await Promise.all([
    readFile(new URL("../src/styles.css", import.meta.url), "utf8"),
    readFile(new URL("../src/App.tsx", import.meta.url), "utf8"),
  ]);

  assert.match(appSource, /<section className="stack vpn-stack">/);
  assert.match(
    source,
    /\.vpn-stack \{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*min-width: 0;/s,
  );
  assert.match(
    source,
    /\.vpn-stack > \.card \{[^}]*min-width: 0;[^}]*max-width: 100%;/s,
  );
  assert.match(
    source,
    /\.vpn-customer-workspace \{[^}]*min-width: 0;[^}]*max-width: 100%;/s,
  );
  assert.match(
    source,
    /\.vpn-workspace-stack,[^}]*\.vpn-key-section \{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*min-width: 0;/s,
  );
  assert.match(
    source,
    /\.vpn-workspace-section-head > div \{[^}]*min-width: 0;[^}]*max-width: 100%;/s,
  );

  assert.match(
    source,
    /\.vpn-key-title-row,\s*\.vpn-key-rename-form \{[^}]*display: flex;[^}]*flex-wrap: wrap;[^}]*gap: 10px;/s,
  );
  assert.match(
    source,
    /\.vpn-key-title-row strong \{[^}]*flex: 1 1 12rem;[^}]*min-width: 0;[^}]*overflow-wrap: anywhere;/s,
  );
  assert.match(
    source,
    /\.vpn-key-rename-form input \{[^}]*flex: 1 1 16rem;[^}]*min-width: 0;/s,
  );
});
