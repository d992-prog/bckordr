import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";

const frontendRoot = fileURLToPath(new URL("../", import.meta.url));
const cssUrl = new URL("../src/vpn-portal/portal.css", import.meta.url);
const portalUrl = new URL("../src/vpn-portal/Portal.tsx", import.meta.url);

test("cabinet HTML is an independent safe Russian Mini App entry", async () => {
  const html = await readFile(new URL("../cabinet/index.html", import.meta.url), "utf8");

  assert.match(html, /<html lang="ru">/);
  assert.match(html, /<title>Veltrix VPN<\/title>/);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /<meta name="referrer" content="no-referrer"/);
  assert.match(
    html,
    /<script\s+async\s+data-telegram-sdk\s+src="https:\/\/telegram\.org\/js\/telegram-web-app\.js"><\/script>/,
  );
  assert.match(html, /<div id="root">[\s\S]*role="status"[\s\S]*Открываем личный кабинет…[\s\S]*<\/div>/);
  assert.doesNotMatch(html, /src\/main\.tsx/);
});

test("Vite declares admin, cabinet, and public VPN pages with URL-based paths", async () => {
  const source = await readFile(new URL("../vite.config.ts", import.meta.url), "utf8");

  assert.match(source, /fileURLToPath/);
  assert.match(source, /new URL\("index\.html", import\.meta\.url\)/);
  assert.match(source, /new URL\("cabinet\/index\.html", import\.meta\.url\)/);
  assert.match(source, /vpn:\s*fileURLToPath\(new URL\("vpn\/index\.html", import\.meta\.url\)\)/);
  assert.match(source, /port:\s*5173/);
  assert.match(source, /target:\s*"http:\/\/localhost:8000"/);
  assert.ok(frontendRoot.endsWith("frontend\\") || frontendRoot.endsWith("frontend/"));
});

test("mobile cabinet has native brand flow and shrinking controls", async () => {
  const css = await readFile(cssUrl, "utf8");
  assert.match(css, /\.veltrix-portal \.button\s*\{[^}]*max-width:\s*100%;[^}]*min-width:\s*0;[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere;/);
  assert.match(css, /\.cabinet-brand\s*\{[^}]*display:\s*block;[^}]*min-height:/s);
  assert.match(css, /\.cabinet-brand strong\s*\{[^}]*position:\s*static/s);
  assert.match(css, /\.cabinet-platforms\s*\{[^}]*flex-wrap:\s*wrap/s);
});

test("wide cabinet uses a native overview and connection grid, without scaling content", async () => {
  const css = await readFile(cssUrl, "utf8");
  const wide = css.slice(css.indexOf("@media (min-width: 900px)"));
  assert.match(wide, /\.cabinet-home\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)\s+minmax\(0,\s*1\.15fr\)/s);
  assert.match(wide, /\.cabinet-devices-preview\s*\{[^}]*grid-column:\s*1;/s);
  assert.match(wide, /\.cabinet-navigation\s*\{[^}]*max-width:\s*720px/s);
  assert.doesNotMatch(wide, /\bzoom\s*:|scale\s*\(/);
  assert.doesNotMatch(css, /portal-status-lens|\.portal-home\s*\{/);
});

test("portal brand styles have one CSS ownership path", async () => {
  const [css, portal] = await Promise.all([
    readFile(cssUrl, "utf8"),
    readFile(portalUrl, "utf8"),
  ]);

  assert.equal(
    (css.match(/@import\s+["']\.\.\/brand\/veltrix-brand\.css["']/g) || []).length,
    1,
  );
  assert.doesNotMatch(portal, /import\s+["']\.\.\/brand\/veltrix-brand\.css["']/);
});

test("portal loading and connection outcomes use one correctly typed live region", async () => {
  const [portal, connection, profile, css] = await Promise.all([
    readFile(portalUrl, "utf8"),
    readFile(new URL("../src/vpn-portal/ConnectDevice.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/vpn-portal/ProfileCard.tsx", import.meta.url), "utf8"),
    readFile(cssUrl, "utf8"),
  ]);

  assert.match(portal, /props\.busy && <p className="card" role="status">Загружаем профили…<\/p>/);
  assert.match(portal, /bootstrapResult === null[\s\S]*role="status"[\s\S]*Загружаем личный кабинет…/);
  assert.match(connection, /type ConnectionMessage = \{ kind: "success" \| "error"; text: string \}/);
  assert.match(connection, /role=\{message\.kind === "error" \? "alert" : "status"\}/);
  assert.match(profile, /type ConnectionMessage = \{ kind: "success" \| "error"; text: string \}/);
  assert.match(profile, /role=\{connectionMessage\.kind === "error" \? "alert" : "status"\}/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?html\s*\{\s*scroll-behavior:\s*auto;/);
});
