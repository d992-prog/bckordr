export type VpnAdminSection = "overview" | "customers" | "nodes" | "plans" | "events";

const SECTION_KEYS = new Set<VpnAdminSection>([
  "overview",
  "customers",
  "nodes",
  "plans",
  "events",
]);

export function vpnAdminSectionFromHash(hash: string): VpnAdminSection {
  const [path] = hash.replace(/^#\/?/, "").split("?", 1);
  const segments = path.split("/").filter(Boolean);
  const [root, candidate] = segments;
  return segments.length === 2 && root === "vpn" && SECTION_KEYS.has(candidate as VpnAdminSection)
    ? candidate as VpnAdminSection
    : "overview";
}

export function displayMetric(value: number | null | undefined): string {
  return value == null ? "Нет данных" : String(value);
}
