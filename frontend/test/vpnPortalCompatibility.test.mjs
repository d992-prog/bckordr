import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { isLegacyIosWebView } from "../src/vpn-portal/compatibility.ts";

const portalCssUrl = new URL("../src/vpn-portal/portal.css", import.meta.url);
const mainUrl = new URL("../src/vpn-portal/main.tsx", import.meta.url);
const boundaryUrl = new URL("../src/vpn-portal/PortalErrorBoundary.tsx", import.meta.url);

test("iOS 16 and older use the lightweight portal renderer", () => {
  assert.equal(isLegacyIosWebView(
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_7_12 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
  ), true);
  assert.equal(isLegacyIosWebView(
    "Mozilla/5.0 (iPad; CPU OS 15_8 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
  ), true);
});

test("modern iOS and non-iOS clients keep the full renderer", () => {
  assert.equal(isLegacyIosWebView(
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_6 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
  ), false);
  assert.equal(isLegacyIosWebView(
    "Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/129.0 Mobile Safari/537.36",
  ), false);
  assert.equal(isLegacyIosWebView(""), false);
});

test("compatibility class is installed before React starts rendering", async () => {
  const main = await readFile(mainUrl, "utf8");

  const compatibilityCall = main.indexOf("applyPortalCompatibilityMode(");
  const renderCall = main.indexOf("ReactDOM.createRoot(");
  assert.ok(compatibilityCall >= 0, "compatibility mode is applied");
  assert.ok(renderCall > compatibilityCall, "compatibility mode precedes React rendering");
});

test("lightweight renderer removes GPU-heavy layers while preserving readable content", async () => {
  const css = await readFile(portalCssUrl, "utf8");
  const compatStart = css.indexOf("html.portal-compat-lite");

  assert.ok(compatStart >= 0, "lightweight compatibility rules exist");
  const compatCss = css.slice(compatStart);
  assert.match(compatCss, /\.portal-shell\s*\{[^}]*background:\s*var\(--vx-pearl\)/s);
  assert.match(compatCss, /\.portal-status-lens\s*\{[^}]*animation:\s*none[^}]*isolation:\s*auto/s);
  assert.match(compatCss, /\.portal-status-lens::before,[\s\S]*\.portal-status-lens::after\s*\{[^}]*display:\s*none/s);
  assert.match(compatCss, /\.vx-glass\s*\{[^}]*-webkit-backdrop-filter:\s*none[^}]*backdrop-filter:\s*none/s);
  assert.match(compatCss, /\.portal-brand-link \.vx-brand::before\s*\{[^}]*display:\s*none/s);
  assert.match(compatCss, /\.portal-brand-link \.vx-mark\s*\{[^}]*position:\s*static[^}]*clip-path:\s*none/s);
});

test("render failures have a visible Russian recovery screen", async () => {
  const [main, boundary] = await Promise.all([
    readFile(mainUrl, "utf8"),
    readFile(boundaryUrl, "utf8"),
  ]);

  assert.match(main, /<PortalErrorBoundary>[\s\S]*<Portal/);
  assert.match(boundary, /Не удалось отобразить кабинет/);
  assert.match(boundary, /Закройте это окно и откройте Mini App снова/);
  assert.match(boundary, /role="alert"/);
});
