import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const htmlUrl = new URL("../vpn/index.html", import.meta.url);
const siteUrl = new URL("../src/vpn-site/main.tsx", import.meta.url);
const cssUrl = new URL("../src/vpn-site/site.css", import.meta.url);
const typesUrl = new URL("../src/vpn-portal/types.ts", import.meta.url);
const portalUrl = new URL("../src/vpn-portal/Portal.tsx", import.meta.url);

test("public VPN HTML is a local Russian entry with safe metadata", async () => {
  const html = await readFile(htmlUrl, "utf8");

  assert.match(html, /<html lang="ru">/);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /<meta name="referrer" content="no-referrer"/);
  assert.match(html, /name="description"\s+content="[^"]+"/);
  assert.match(html, /<link rel="icon" type="image\/svg\+xml" href="\/favicon\.svg"/);
  assert.match(html, /<script type="module" src="\/src\/vpn-site\/main\.tsx"><\/script>/);
  assert.match(html, /<div id="root"><\/div>/);
  assert.doesNotMatch(html, /telegram-web-app\.js|https?:\/\/telegram\.org\/js/i);
});

test("public site loads only safe public APIs with independent retry states", async () => {
  const [source, types] = await Promise.all([
    readFile(siteUrl, "utf8"),
    readFile(typesUrl, "utf8"),
  ]);

  assert.match(types, /bot_url:\s*string \| null/);
  assert.doesNotMatch(types, /trial_enabled|bot_token|release_id|admin_route/i);
  assert.match(source, /portalApi\.config\(\)/);
  assert.match(source, /portalApi\.plans\(\)/);
  assert.match(source, /void loadConfig\(\);\s*void loadPlans\(\);/);
  assert.match(source, /onClick=\{\(\) => void loadConfig\(\)\}/);
  assert.match(source, /onClick=\{\(\) => void loadPlans\(\)\}/);
  assert.match(source, /href=\{config\.bot_url\}/);
  assert.match(source, /aria-disabled="true"/);

  for (const forbidden of [
    "portalApi.me",
    "portalApi.trial",
    "portalApi.activateTrial",
    "portalApi.subscriptions",
    "portalApi.profiles",
    "localStorage",
    "sessionStorage",
    "/api/control",
    "/api/auth",
  ]) {
    assert.equal(source.includes(forbidden), false, forbidden);
  }
});

test("public site contains honest product, trial, support, privacy, and terms copy", async () => {
  const [source, css, portal] = await Promise.all([
    readFile(siteUrl, "utf8"),
    readFile(cssUrl, "utf8"),
    readFile(portalUrl, "utf8"),
  ]);

  for (const required of [
    "Veltrix VPN",
    "Как это работает",
    "Happ для iPhone (iOS) и Android",
    "7 дней",
    "Оплата пока не подключена",
    "Покупка скоро будет доступна",
    "Поддержка",
    'id="privacy"',
    'id="terms"',
  ]) {
    assert.ok(source.includes(required), required);
  }
  assert.match(source, /config\?\.support_text/);
  assert.doesNotMatch(source, /24\s*\/\s*7|полная анонимность|абсолютная анонимность|безлимитн(?:ая|ые) мощност/i);
  assert.doesNotMatch(source, /Windows|macOS/);
  assert.doesNotMatch(source, /bot_token|release_id|admin_route|\/api\/control/i);
  assert.match(css, /--green-900:\s*#173f2d/);
  assert.match(css, /@media \(max-width:\s*700px\)/);
  assert.match(portal, /href="\/vpn\/#privacy"/);
  assert.match(portal, /href="\/vpn\/#terms"/);
  assert.match(portal, /bootstrapResult\.config\?\.support_text/);
});

test("public trial copy stays honest without duplicating readiness", async () => {
  const source = await readFile(siteUrl, "utf8");
  const trialSection = source.slice(
    source.indexOf('id="trial"'),
    source.indexOf('id="support"'),
  );

  assert.match(trialSection, /Пробный доступ на 7 дней предоставляется поэтапно/);
  assert.match(trialSection, /Актуальную доступность проверьте в боте/);
  assert.match(trialSection, /выдача зависит от свободной мощности/);
  assert.match(trialSection, /config\?\.bot_url\s*&&[\s\S]*label="Проверить доступность в боте"/);
  assert.doesNotMatch(trialSection, /trial_enabled|сейчас доступен|может получить|configBusy\s*\?/i);
});

test("public mobile hero lets long Russian copy shrink within the viewport", async () => {
  const css = await readFile(cssUrl, "utf8");

  assert.match(css, /\.hero\s*>\s*\*\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;/);
  assert.match(
    css,
    /@media \(max-width:\s*900px\)\s*\{[\s\S]*?\.hero\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);/,
  );
});
