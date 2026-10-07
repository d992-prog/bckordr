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
  let field: HTMLTextAreaElement | undefined;
  try {
    field = document.createElement("textarea");
    field.value = uri;
    field.readOnly = true;
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.focus();
    field.setSelectionRange(0, field.value.length);
    if (document.execCommand("copy")) {
      return;
    }
  } catch {
    // Some WebViews omit the legacy command but still expose Clipboard API.
  } finally {
    field?.remove();
  }

  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(uri);
      return;
    } catch {
      // Restricted WebViews can expose the API while rejecting every write.
    }
  }

  throw new Error("clipboard unavailable");
}
