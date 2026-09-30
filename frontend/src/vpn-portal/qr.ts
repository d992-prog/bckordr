const MAX_QR_BYTES = 2953;

export async function createQrDataUrl(value: string): Promise<string> {
  if (value.trim().length === 0 || new TextEncoder().encode(value).length > MAX_QR_BYTES) {
    throw new Error("Invalid QR data");
  }

  const { renderSVG } = await import("uqr");
  const svg = renderSVG(value, { border: 4, ecc: "L" });
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
}
