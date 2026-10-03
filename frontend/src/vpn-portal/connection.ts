// @ts-expect-error Node's strip-types runner requires the source extension.
import { PortalError, portalApi } from "./api.ts";

export async function loadConnectionUri(profileId: number): Promise<string> {
  const result = await portalApi.connection(profileId);
  if (!result.uri) {
    throw new PortalError(0, "Ссылка подключения недоступна.");
  }
  return result.uri;
}

export async function copyConnectionUri(uri: string): Promise<void> {
  if (!navigator.clipboard?.writeText) {
    throw new Error("clipboard unavailable");
  }
  await navigator.clipboard.writeText(uri);
}
