import type { VpnCustomer, VpnSubscription } from "./api";

export type AccessKeyDisplay = {
  display_name: string;
  config_uri: string | null;
  updated_at: string;
};

export type VpnCustomerFilter = "all" | "active" | "expiring" | "suspended" | "archived";
export type VpnCustomerOperationalStatus = Exclude<VpnCustomerFilter, "all">;

const USABLE_SUBSCRIPTION_STATUSES = new Set(["active", "trial"]);
const SUSPENDED_SUBSCRIPTION_STATUSES = new Set(["disabled", "cancelled", "expired"]);
const EXPIRING_WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
const ACCESS_KEY_STATUS_LABELS: Record<string, string> = {
  pending_sync: "ожидает синхронизации",
  syncing: "синхронизируется",
  pending_suspend: "ожидает приостановки",
  suspended: "приостановлен",
  active: "активен",
  pending_revoke: "ожидает отзыва",
  revoked: "отозван",
  failed: "ошибка",
};

export function accessKeyStatusLabel(status: string) {
  return ACCESS_KEY_STATUS_LABELS[status] ?? status;
}

export async function saveSubscriptionAndRequestSync(
  save: () => Promise<unknown>,
  requestSync: () => Promise<unknown>,
  reload: () => Promise<void>,
) {
  await save();
  let syncRequested = true;
  let refreshed = true;
  try {
    await requestSync();
  } catch {
    // The durable backend queue still owns the saved update.
    syncRequested = false;
  }
  try {
    await reload();
  } catch {
    refreshed = false;
  }
  return { syncRequested, refreshed };
}

export function applyAccessKeyDisplay<T extends AccessKeyDisplay>(
  accessKey: T,
  display: AccessKeyDisplay,
): T {
  if (isNewerAccessKeyVersion(accessKey.updated_at, display.updated_at)) {
    return accessKey;
  }
  return {
    ...accessKey,
    display_name: display.display_name,
    config_uri: display.config_uri,
    updated_at: display.updated_at,
  };
}

const BACKEND_TIMESTAMP_PATTERN =
  /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})?$/;

function backendTimestampParts(value: string) {
  const match = BACKEND_TIMESTAMP_PATTERN.exec(value);
  if (!match) {
    return null;
  }
  const epochMillis = Date.parse(`${match[1]}${match[3] ?? "Z"}`);
  if (!Number.isFinite(epochMillis)) {
    return null;
  }
  return {
    epochMillis,
    fraction: (match[2] ?? "").padEnd(9, "0"),
  };
}

function isNewerAccessKeyVersion(incoming: string, current: string) {
  const incomingParts = backendTimestampParts(incoming);
  const currentParts = backendTimestampParts(current);
  if (incomingParts && currentParts) {
    if (incomingParts.epochMillis !== currentParts.epochMillis) {
      return incomingParts.epochMillis > currentParts.epochMillis;
    }
    return incomingParts.fraction > currentParts.fraction;
  }
  const incomingMillis = Date.parse(incoming);
  const currentMillis = Date.parse(current);
  if (
    Number.isFinite(incomingMillis) &&
    Number.isFinite(currentMillis) &&
    incomingMillis !== currentMillis
  ) {
    return incomingMillis > currentMillis;
  }
  return incoming > current;
}

export function reconcileAccessKeyDisplayOverrides(
  current: Map<number, AccessKeyDisplay>,
  accessKeys: Array<AccessKeyDisplay & { id: number }>,
) {
  const next = new Map(current);
  for (const accessKey of accessKeys) {
    const display = next.get(accessKey.id);
    if (
      display &&
      ((display.display_name === accessKey.display_name &&
        display.config_uri === accessKey.config_uri) ||
        isNewerAccessKeyVersion(accessKey.updated_at, display.updated_at))
    ) {
      next.delete(accessKey.id);
    }
  }
  return next.size === current.size ? current : next;
}

export function shouldApplyLoadGeneration(
  incomingGeneration: number,
  lastAppliedGeneration: number,
) {
  return incomingGeneration >= lastAppliedGeneration;
}

export function nextAccessKeyEditorAfterRename(
  currentAccessKeyId: number | null,
  completedAccessKeyId: number,
) {
  return currentAccessKeyId === completedAccessKeyId ? null : currentAccessKeyId;
}

export async function saveAccessKeyDisplayName<T extends AccessKeyDisplay>(
  rename: () => Promise<T>,
  reload: () => Promise<void>,
  publish?: (accessKey: T) => void,
) {
  const accessKey = await rename();
  publish?.(accessKey);
  let refreshed = true;
  try {
    await reload();
  } catch {
    refreshed = false;
  }
  return { accessKey, refreshed };
}

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
    if (remaining < 0) {
      return "suspended";
    }
    if (remaining <= EXPIRING_WINDOW_MS) {
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
