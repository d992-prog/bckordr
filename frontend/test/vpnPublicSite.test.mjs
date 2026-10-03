import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const htmlUrl = new URL("../vpn/index.html", import.meta.url);
const siteUrl = new URL("../src/vpn-site/main.tsx", import.meta.url);
const cssUrl = new URL("../src/vpn-site/site.css", import.meta.url);
const typesUrl = new URL("../src/vpn-portal/types.ts", import.meta.url);
const portalUrl = new URL("../src/vpn-portal/Portal.tsx", import.meta.url);
const portalAccountUrl = new URL("../src/vpn-portal/PortalAccount.tsx", import.meta.url);

test("public VPN HTML is a local Russian entry with safe metadata", async () => {
  const html = await readFile(htmlUrl, "utf8");

  assert.match(html, /<html lang="ru">/);
  assert.match(html, /viewport-fit=cover/);
  assert.match(html, /<meta name="referrer" content="no-referrer"/);
  assert.match(html, /name="description"\s+content="[^"]+"/);
  assert.match(html, /<link rel="icon" type="image\/svg\+xml" href="\/favicon\.svg"/);
  assert.match(html, /<link rel="canonical" href="https:\/\/veltrix\.qzz\.io\/vpn\/"/);
  assert.match(html, /property="og:title" content="Veltrix VPN — простой старт через Telegram"/);
  assert.match(html, /property="og:description"\s+content="Veltrix VPN — готовый профиль и понятное подключение через официальный Telegram-бот\."/);
  assert.match(html, /property="og:type" content="website"/);
  assert.match(html, /property="og:url" content="https:\/\/veltrix\.qzz\.io\/vpn\/"/);
  assert.match(html, /property="og:image" content="https:\/\/veltrix\.qzz\.io\/brand\/brand-mark\.webp"/);
  assert.match(html, /property="og:image:alt" content="Логотип Veltrix VPN"/);
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
  assert.match(source, /onClick=\{\(\) => void onConfigRetry\(\)\}/);
  assert.match(source, /onClick=\{\(\) => void onRetry\(\)\}/);
  assert.match(source, /href=\{config\.bot_url\}/);
  assert.match(source, /role="status" aria-live="polite">Загружаем ссылку…<\/span>/);
  assert.match(source, /<button className="button button--disabled" type="button" disabled>Бот временно недоступен<\/button>/);
  assert.doesNotMatch(source, /aria-disabled="true"/);

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
  const [source, css, portal, account] = await Promise.all([
    readFile(siteUrl, "utf8"),
    readFile(cssUrl, "utf8"),
    readFile(portalUrl, "utf8"),
    readFile(portalAccountUrl, "utf8"),
  ]);

  for (const required of [
    "Veltrix VPN",
    "VPN без сложных настроек",
    "Открыть в Telegram",
    "Как подключиться",
    "Поддерживаемые устройства",
    "Happ на iPhone (iOS)",
    "Hiddify на Windows",
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
  assert.doesNotMatch(source, /<li><strong>Android<\/strong>/);
  assert.doesNotMatch(source, /гарантирован|самый быстрый|без ограничений|полная анонимность/i);
  assert.doesNotMatch(source, /<span>V<\/span>/);
  assert.doesNotMatch(source, /bot_token|release_id|admin_route|\/api\/control/i);
  assert.match(source, /import\s+\{\s*VeltrixMark\s*\}\s+from\s+"\.\.\/brand\/VeltrixMark"/);
  assert.match(css, /@import\s+"\.\.\/brand\/veltrix-brand\.css"/);
  assert.match(css, /--vx-blue/);
  assert.match(css, /\.site-connection-route/);
  assert.match(css, /@media \(max-width:\s*900px\)/);
  assert.match(account, /href="\/vpn\/#privacy"/);
  assert.match(account, /href="\/vpn\/#terms"/);
  assert.match(portal, /bootstrapResult\.config\?\.support_text/);
});

test("public site keeps the release sections in the intended reading order", async () => {
  const source = await readFile(siteUrl, "utf8");
  const main = source.slice(source.indexOf('<main id="top">'), source.indexOf("</main>"));
  const sections = [
    "<Hero",
    "<ConnectionRoute",
    "<SupportedApps",
    "<PlansSection",
    "<TrialSection",
    "<SupportSection",
    "<Policies",
  ];
  const positions = sections.map((section) => main.indexOf(section));

  assert.ok(positions.every((position) => position >= 0), positions);
  assert.deepEqual(positions, [...positions].sort((left, right) => left - right));
});

test("public sections keep the approved component and fragment contract", async () => {
  const source = await readFile(siteUrl, "utf8");

  assert.match(source, /className="eyebrow">\u041f\u0440\u043e\u0441\u0442\u043e\u0439 \u0441\u0442\u0430\u0440\u0442 \u0447\u0435\u0440\u0435\u0437 Telegram<\/p>/);
  assert.match(source, /className="lead">\s*\u041f\u043e\u043b\u0443\u0447\u0438\u0442\u0435 \u0433\u043e\u0442\u043e\u0432\u044b\u0439 \u043f\u0440\u043e\u0444\u0438\u043b\u044c/);
  assert.match(source, /function Hero\(\{ config, configBusy, configError, onConfigRetry \}: HeroProps\)/);
  assert.match(source, /<Hero config=\{config\} configBusy=\{configBusy\} configError=\{configError\} onConfigRetry=\{loadConfig\} \/>/);
  assert.match(source, /function PlansSection\(\{ config, plans, busy, error, onRetry \}: PlansSectionProps\)/);
  assert.match(source, /<PlansSection config=\{config\} plans=\{plans\} busy=\{plansBusy\} error=\{plansError\} onRetry=\{loadPlans\} \/>/);
  assert.match(source, /function SupportSection\(\{ config, configBusy \}: \{ config: PortalConfig \| null; configBusy: boolean \}\)/);
  assert.match(source, /<SupportSection config=\{config\} configBusy=\{configBusy\} \/>/);
  assert.match(source, /function useFragmentNavigation\(/);
  assert.match(source, /decodeURIComponent\(/);
  assert.match(source, /window\.addEventListener\("hashchange", reconcile\)/);
  assert.match(source, /prefers-reduced-motion: reduce/);
  assert.match(source, /target\.scrollIntoView/);
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
  assert.match(
    css,
    /@media \(max-width:\s*900px\)\s*\{[\s\S]*?\.hero-actions[\s\S]*?\.button[^}]*width:\s*100%/,
  );
});
