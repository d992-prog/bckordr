import type { VpnCustomer, VpnSubscription } from "./api";

export type VpnCustomerFilter = "all" | "active" | "expiring" | "suspended" | "archived";
export type VpnCustomerOperationalStatus = Exclude<VpnCustomerFilter, "all">;

const USABLE_SUBSCRIPTION_STATUSES = new Set(["active", "trial"]);
const SUSPENDED_SUBSCRIPTION_STATUSES = new Set(["disabled", "cancelled", "expired"]);
const EXPIRING_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;

export function customerStatusOptions(currentStatus: string | null | undefined) {
  return currentStatus === "archived" ? ["archived"] : ["active", "blocked"];
}

function timestamp(value: string | null | undefined) {
  if (!value) {
    return Number.NEGATIVE_INFINITY;
  }
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? Number.NEGATIVE_INFINITY : parsed;
}

export function selectPrimarySubscription(
  customerId: number,
  subscriptions: VpnSubscription[],
) {
  return (
    subscriptions
      .filter((subscription) => subscription.customer_id === customerId)
      .sort((left, right) => {
        const usableDelta =
          Number(USABLE_SUBSCRIPTION_STATUSES.has(right.status)) -
          Number(USABLE_SUBSCRIPTION_STATUSES.has(left.status));
        if (usableDelta !== 0) {
          return usableDelta;
        }
        return timestamp(right.updated_at) - timestamp(left.updated_at);
      })[0] ?? null
  );
}

export function classifyVpnCustomer(
  customer: VpnCustomer,
  subscriptions: VpnSubscription[],
  now = new Date(),
): VpnCustomerOperationalStatus {
  if (customer.status === "archived") {
    return "archived";
  }
  const primary = selectPrimarySubscription(customer.id, subscriptions);
  if (!primary || SUSPENDED_SUBSCRIPTION_STATUSES.has(primary.status)) {
    return "suspended";
  }
  if (USABLE_SUBSCRIPTION_STATUSES.has(primary.status) && primary.expires_at) {
    const remaining = timestamp(primary.expires_at) - now.getTime();
    if (remaining >= 0 && remaining <= EXPIRING_WINDOW_MS) {
      return "expiring";
    }
  }
  return customer.status === "active" && USABLE_SUBSCRIPTION_STATUSES.has(primary.status)
    ? "active"
    : "suspended";
}

export function filterVpnCustomers(
  customers: VpnCustomer[],
  subscriptions: VpnSubscription[],
  filter: VpnCustomerFilter,
  query: string,
  now = new Date(),
) {
  const needle = query.trim().replace(/^@/, "").toLocaleLowerCase("ru");
  return customers.filter((customer) => {
    const identity = [
      customer.first_name,
      customer.last_name,
      customer.telegram_username,
      customer.telegram_user_id,
      String(customer.id),
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase("ru");
    const matchesQuery = !needle || identity.includes(needle);
    const matchesFilter =
      filter === "all" || classifyVpnCustomer(customer, subscriptions, now) === filter;
    return matchesQuery && matchesFilter;
  });
}

export function calculateExtendedExpiration(
  expiresAt: string | null,
  days: number,
  now = new Date(),
) {
  const currentExpiry = timestamp(expiresAt);
  const base = Math.max(
    Number.isFinite(currentExpiry) ? currentExpiry : now.getTime(),
    now.getTime(),
  );
  return new Date(base + days * 24 * 60 * 60 * 1000).toISOString();
}
