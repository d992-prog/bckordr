import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const frontendRoot = fileURLToPath(new URL("../", import.meta.url));

test("cabinet HTML is an independent safe Russian Mini App entry", async () => {
  const html = await readFile(new URL("../cabinet/index.html", import.meta.url), "utf8");

  assert.match(html, /<html lang="ru">/);
  assert.match(html, /<title>Veltrix VPN<\/title>/);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /<meta name="referrer" content="no-referrer"/);
  const sdkPosition = html.indexOf("https://telegram.org/js/telegram-web-app.js");
  const entryPosition = html.indexOf("/src/vpn-portal/main.tsx");
  assert.ok(sdkPosition >= 0);
  assert.ok(entryPosition > sdkPosition);
  assert.match(html, /<div id="root"><\/div>/);
  assert.doesNotMatch(html, /src\/main\.tsx/);
});

test("Vite declares both admin and cabinet pages with URL-based paths", async () => {
  const source = await readFile(new URL("../vite.config.ts", import.meta.url), "utf8");

  assert.match(source, /fileURLToPath/);
  assert.match(source, /new URL\("index\.html", import\.meta\.url\)/);
  assert.match(source, /new URL\("cabinet\/index\.html", import\.meta\.url\)/);
  assert.match(source, /port:\s*5173/);
  assert.match(source, /target:\s*"http:\/\/localhost:8000"/);
  assert.ok(frontendRoot.endsWith("frontend\\") || frontendRoot.endsWith("frontend/"));
});
