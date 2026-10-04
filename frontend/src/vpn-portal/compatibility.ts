const LEGACY_IOS_MAX_MAJOR = 16;

export function isLegacyIosWebView(userAgent: string): boolean {
  const match = userAgent.match(/\b(?:CPU(?: iPhone)? OS|iPhone OS) (\d+)(?:[._]\d+)?/i);
  if (match === null) {
    return false;
  }
  return Number.parseInt(match[1], 10) <= LEGACY_IOS_MAX_MAJOR;
}

export function applyPortalCompatibilityMode(
  root: HTMLElement,
  userAgent: string,
): void {
  root.classList.toggle("portal-compat-lite", isLegacyIosWebView(userAgent));
}
