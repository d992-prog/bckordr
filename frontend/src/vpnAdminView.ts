export type VpnAdminSection = "overview" | "customers" | "nodes" | "plans" | "events";

const SECTION_KEYS = new Set<VpnAdminSection>([
  "overview",
  "customers",
  "nodes",
  "plans",
  "events",
]);

function hashSegments(hash: string): string[] {
  const [path] = hash.replace(/^#\/?/, "").split("?", 1);
  return path.split("/").filter(Boolean);
}

export function isVpnAdminHash(hash: string): boolean {
  return hashSegments(hash)[0] === "vpn";
}

export function vpnAdminSectionFromHash(hash: string): VpnAdminSection {
  const segments = hashSegments(hash);
  const [root, candidate] = segments;
  return segments.length === 2 && root === "vpn" && SECTION_KEYS.has(candidate as VpnAdminSection)
    ? candidate as VpnAdminSection
    : "overview";
}

export function displayMetric(value: number | null | undefined): string {
  return value == null ? "Нет данных" : String(value);
}
