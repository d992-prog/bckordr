import type { VpnAccessKey, VpnCustomer, VpnSubscription } from "./api";

export const VPN_ABUSE_PAGE_SIZE = 100;

export type VpnAbuseState = "open" | "acknowledged";

export type VpnAbuseIncident = {
  id: number;
  device_id: number;
  customer_id: number;
  display_name: string;
  started_at: string;
  last_observed_at: string;
  peak_source_count: number;
  acknowledged_at: string | null;
};

export type VpnAbuseTransport = {
  listIncidents: (state: VpnAbuseState, options: { afterId: number; limit: 100 }) => Promise<VpnAbuseIncident[]>;
  acknowledgeIncident: (incidentId: number) => Promise<VpnAbuseIncident>;
};

export type VpnAbuseDeviceContext = {
  device_id: number;
  customer: VpnCustomer;
  subscription: VpnSubscription;
  access_key: VpnAccessKey | null;
};

export function matchesAbuseDeviceContext(
  context: VpnAbuseDeviceContext,
  selection: { customerId: number; deviceId: number },
) {
  return context.device_id === selection.deviceId
    && context.customer.id === selection.customerId
    && context.subscription.customer_id === selection.customerId
    && (context.access_key === null || (
      context.access_key.device_id === selection.deviceId
      && context.access_key.subscription_id === context.subscription.id
    ));
}

export function mergeAbuseIncidents(
  previous: ReadonlyArray<VpnAbuseIncident>,
  incoming: ReadonlyArray<VpnAbuseIncident>,
) {
  return [...new Map([...previous, ...incoming].map((incident) => [incident.id, incident])).values()]
    .sort((left, right) => left.id - right.id);
}

export function incidentMatchesAbuseState(incident: VpnAbuseIncident, state: VpnAbuseState) {
  return state === "open"
    ? incident.acknowledged_at === null
    : typeof incident.acknowledged_at === "string";
}
