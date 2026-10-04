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

test("mobile cabinet header and buttons can shrink without horizontal overflow", async () => {
  const css = await readFile(cssUrl, "utf8");

  assert.match(
    css,
    /\.veltrix-portal \.button\s*\{[^}]*max-width:\s*100%;[^}]*min-width:\s*0;[^}]*white-space:\s*normal;[^}]*overflow-wrap:\s*anywhere;/,
  );
  assert.match(
    css,
    /@media \(max-width:\s*639px\)\s*\{[\s\S]*?\.veltrix-portal \.portal-header\s*\{[^}]*flex-wrap:\s*wrap;/,
  );
  assert.match(
    css,
    /@media \(max-width:\s*639px\)\s*\{[\s\S]*?\.veltrix-portal \.account\s*\{[^}]*max-width:\s*100%;/,
  );
});

test("wide cabinet uses a native two-column layout and compact navigation", async () => {
  const css = await readFile(cssUrl, "utf8");

  assert.match(
    css,
    /@media \(min-width:\s*1200px\)\s*\{[\s\S]*?--portal-nav-height:\s*76px;/,
  );
  assert.match(css, /@media \(min-width:\s*760px\) and \(max-width:\s*1199px\) and \(min-height:\s*1200px\)/);
  assert.match(
    css,
    /@media \(min-width:\s*1200px\)\s*\{[\s\S]*?\.veltrix-portal \.portal-home\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1\.75fr\)\s+minmax\(400px,\s*1fr\);/,
  );
  assert.match(
    css,
    /@media \(min-width:\s*1200px\)\s*\{[\s\S]*?\.veltrix-portal \.portal-nav\s*\{[^}]*width:\s*min\(720px,\s*calc\(100vw - 64px\)\);/,
  );
  const wideRules = css.slice(css.indexOf("@media (min-width: 1200px)"), css.indexOf("@media (prefers-color-scheme: dark)"));
  assert.doesNotMatch(wideRules, /\bzoom\s*:/);
  assert.doesNotMatch(wideRules, /scale\s*\(/);
  assert.doesNotMatch(wideRules, /translate(?:X|Y)?\s*\(/);
  assert.doesNotMatch(wideRules, /nth-child/);
  assert.doesNotMatch(wideRules, /84px|font-size:\s*38px/);
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
  const [portal, home, profile, css] = await Promise.all([
    readFile(portalUrl, "utf8"),
    readFile(new URL("../src/vpn-portal/PortalHome.tsx", import.meta.url), "utf8"),
    readFile(new URL("../src/vpn-portal/ProfileCard.tsx", import.meta.url), "utf8"),
    readFile(cssUrl, "utf8"),
  ]);

  assert.match(portal, /props\.busy && <p className="card" role="status">Загружаем профили…<\/p>/);
  assert.match(portal, /bootstrapResult === null[\s\S]*role="status"[\s\S]*Загружаем личный кабинет…/);
  assert.match(home, /type CopyMessage = \{ kind: "success" \| "error"; text: string \}/);
  assert.match(home, /role=\{copyMessage\.kind === "error" \? "alert" : "status"\}/);
  assert.match(profile, /type ConnectionMessage = \{ kind: "success" \| "error"; text: string \}/);
  assert.match(profile, /role=\{connectionMessage\.kind === "error" \? "alert" : "status"\}/);
  assert.match(css, /@media \(prefers-reduced-motion: reduce\) \{[\s\S]*?html\s*\{\s*scroll-behavior:\s*auto;/);
});
