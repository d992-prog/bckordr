export type PortalSection = "home" | "profiles" | "account";

const PORTAL_SECTIONS = new Map<string, PortalSection>([
  ["home", "home"],
  ["subscription", "home"],
  ["plans", "home"],
  ["profiles", "profiles"],
  ["connect", "profiles"],
  ["account", "account"],
  ["help", "account"],
]);

export function portalSectionFromHash(hash: string): PortalSection {
  const route = hash.replace(/^#\/?/, "").split("?", 1)[0];
  return PORTAL_SECTIONS.get(route) ?? "home";
}
