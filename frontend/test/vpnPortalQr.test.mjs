import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const qrModuleUrl = new URL("../src/vpn-portal/qr.ts", import.meta.url);

async function loadQrModule() {
  try {
    return await import(qrModuleUrl);
  } catch (error) {
    if (error?.code === "ERR_MODULE_NOT_FOUND" && String(error.url).endsWith("/qr.ts")) {
      return {};
    }
    throw error;
  }
}

test("renders a VLESS URI into an opaque local SVG data URL", async () => {
  const { createQrDataUrl } = await loadQrModule();
  assert.equal(typeof createQrDataUrl, "function");

  const uuid = "11111111-2222-4333-8444-555555555555";
  const uri = `vless://${uuid}@vpn.example:443?security=tls#Veltrix`;
  const result = await createQrDataUrl(uri);

  assert.match(result, /^data:image\/svg\+xml;charset=utf-8,/);
  assert.equal(result.includes(uri), false);
  assert.equal(result.includes(uuid), false);
});

test("rejects blank and unreasonably large QR input", async () => {
  const { createQrDataUrl } = await loadQrModule();
  assert.equal(typeof createQrDataUrl, "function");

  await assert.rejects(() => createQrDataUrl("   "), /QR/);
  await assert.rejects(() => createQrDataUrl("x".repeat(4097)), /QR/);
});

test("loads uqr only on demand and has no side channels", async () => {
  const source = await readFile(qrModuleUrl, "utf8").catch(() => "");

  assert.match(source, /await import\("uqr"\)/);
  assert.equal(source.match(/["']uqr["']/g)?.length, 1);
  assert.doesNotMatch(source, /^import\s.+["']uqr["']/m);
  assert.doesNotMatch(source, /fetch|XMLHttpRequest|localStorage|sessionStorage|console\.|indexedDB/);
});

test("profile QR controls are reveal-only, race-gated, and clear with connection state", async () => {
  const source = await readFile(
    new URL("../src/vpn-portal/ProfileCard.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /import \{ createQrDataUrl \} from "\.\/qr"/);
  assert.match(source, /Показать QR-код/);
  assert.match(source, /Создаём QR-код…/);
  assert.match(source, /Скрыть QR-код/);
  assert.match(source, /alt={`QR-код для подключения профиля \$\{profile\.display_name\}`\}/);
  assert.match(source, /const epoch = connectionEpoch\.current/);
  assert.match(source, /connectionEpoch\.current !== epoch/);
  assert.match(source, /qrRequestInFlight\.current/);
  assert.match(source, /setQrDataUrl\(null\)/);
  assert.match(source, /setQrBusy\(false\)/);
  assert.match(source, /setQrError\(""\)/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|console\./);
});

test("QR image is centered, bounded, and padded on a white quiet zone", async () => {
  const css = await readFile(new URL("../src/vpn-portal/portal.css", import.meta.url), "utf8");

  assert.match(css, /\.qr-code\s*\{[\s\S]*width:\s*min\(100%,\s*320px\)/);
  assert.match(css, /\.qr-code\s*\{[\s\S]*margin:\s*0 auto/);
  assert.match(css, /\.qr-code\s*\{[\s\S]*background:\s*#fff/);
  assert.match(css, /\.qr-code\s*\{[\s\S]*padding:/);
});
