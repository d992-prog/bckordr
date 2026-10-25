import type { VpnEndpointCapacity, VpnEndpointCapacityUpdate } from "./api";

export function validateVpnEndpointCapacity(
  maximumInput: string,
  warningInput: string,
): VpnEndpointCapacityUpdate {
  const maximum = maximumInput.trim() === "" ? null : Number(maximumInput);
  const warning = Number(warningInput);
  if (
    (maximum !== null && (!Number.isInteger(maximum) || maximum < 1 || maximum > 100_000))
    || !Number.isInteger(warning)
    || warning < 1
    || warning > 100
  ) {
    throw new Error("Введите целое число: лимит 1–100000, предупреждение 1–100%.");
  }
  return {
    max_active_profiles: maximum,
    capacity_warning_percent: warning,
  };
}

export function replaceVpnEndpointCapacity<T extends { endpoint_id: number }>(
  rows: T[],
  updated: T,
): T[] {
  return rows.map((row) => row.endpoint_id === updated.endpoint_id ? updated : row);
}

export function applyVpnEndpointCapacityUpdate(current: VpnEndpointCapacity, updated: VpnEndpointCapacity, startedFrom: VpnEndpointCapacity): VpnEndpointCapacity {
  if (current === startedFrom) return updated;
  return { ...current, max_active_profiles: updated.max_active_profiles, capacity_warning_percent: updated.capacity_warning_percent };
}

export function applyVpnEndpointPublicationUpdate(current: VpnEndpointCapacity, updated: VpnEndpointCapacity, startedFrom: VpnEndpointCapacity): VpnEndpointCapacity {
  if (current === startedFrom) return updated;
  return { ...current, is_published: updated.is_published, public_name: updated.public_name, country_code: updated.country_code };
}
