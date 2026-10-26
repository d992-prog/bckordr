export const CLIENT_PLATFORMS = [
  { id: "iphone", label: "iPhone", detail: "Для iPhone и iPad", app: "Happ", scheme: "happ://add/", download: "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru" },
  { id: "android", label: "Android", detail: "Для Android", app: "Happ", scheme: "happ://add/", download: "https://play.google.com/store/apps/details?id=com.happproxy" },
  { id: "windows", label: "Windows", detail: "Для Windows", app: "Hiddify", scheme: "hiddify://import/", download: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1" },
  { id: "macos", label: "macOS", detail: "Для macOS", app: "Hiddify", scheme: "hiddify://import/", download: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1" },
  { id: "linux", label: "Linux", detail: "Для Linux", app: "Hiddify", scheme: "hiddify://import/", download: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1" },
] as const;

export type ClientPlatform = (typeof CLIENT_PLATFORMS)[number]["id"];

export function detectClientPlatform(userAgent: string, touchPoints = 0): ClientPlatform {
  if (/iPhone|iPad|iPod/i.test(userAgent) || (/Macintosh/i.test(userAgent) && touchPoints > 1)) return "iphone";
  if (/Android/i.test(userAgent)) return "android";
  if (/Windows/i.test(userAgent)) return "windows";
  if (/Macintosh|Mac OS X/i.test(userAgent)) return "macos";
  if (/Linux/i.test(userAgent)) return "linux";
  return "iphone";
}

export function canonicalSubscriptionUrl(value: string): string {
  try {
    if (!value.startsWith("https://") || /[\u0000-\u0020\u007f]/.test(value)) throw new Error();
    const url = new URL(value);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password || url.hash) throw new Error();
    return url.href;
  } catch { throw new Error("invalid subscription URL"); }
}

export function buildAppImportUrl(platform: ClientPlatform, subscription: string, name: string): string {
  const client = CLIENT_PLATFORMS.find((item) => item.id === platform);
  if (!client) throw new Error("unsupported platform");
  return `${client.scheme}${canonicalSubscriptionUrl(subscription)}#${encodeURIComponent(name)}`;
}
