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
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(uri);
      return;
    } catch {
      // Restricted WebViews can expose the API while rejecting every write.
    }
  }

  let field: HTMLTextAreaElement | undefined;
  try {
    field = document.createElement("textarea");
    field.value = uri;
    field.readOnly = true;
    field.style.position = "fixed";
    field.style.left = "-9999px";
    document.body.append(field);
    field.select();
    if (!document.execCommand("copy")) {
      throw new Error("copy rejected");
    }
  } catch {
    throw new Error("clipboard unavailable");
  } finally {
    field?.remove();
  }
}
