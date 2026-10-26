export type PortalSection = "home" | "devices" | "help" | "account";

const PORTAL_SECTIONS = new Map<string, PortalSection>([
  ["home", "home"],
  ["subscription", "home"],
  ["plans", "home"],
  ["devices", "devices"],
  ["profiles", "devices"],
  ["connect", "home"],
  ["account", "account"],
  ["help", "help"],
]);

export function portalSectionFromHash(hash: string): PortalSection {
  const route = hash.replace(/^#\/?/, "").split("?", 1)[0];
  return PORTAL_SECTIONS.get(route) ?? "home";
}
