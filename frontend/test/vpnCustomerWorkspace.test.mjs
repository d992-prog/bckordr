import test from "node:test";
import assert from "node:assert/strict";

import {
  calculateExtendedExpiration,
  classifyVpnCustomer,
  filterVpnCustomers,
  selectPrimarySubscription,
} from "../src/vpnCustomerWorkspace.ts";

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
