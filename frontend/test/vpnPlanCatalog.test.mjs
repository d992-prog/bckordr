import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const helpersUrl = new URL("../src/vpn-portal/planFormatting.ts", import.meta.url);
const catalogUrl = new URL("../src/vpn-portal/PlanCatalog.tsx", import.meta.url);
const portalUrl = new URL("../src/vpn-portal/Portal.tsx", import.meta.url);
const typesUrl = new URL("../src/vpn-portal/types.ts", import.meta.url);
const cssUrl = new URL("../src/vpn-portal/portal.css", import.meta.url);

test("formats Russian plan facts and prices without throwing on unsafe currency", async () => {
  let helpers;
  await assert.doesNotReject(async () => { helpers = await import(helpersUrl); });
  const {
    formatPlanDevices,
    formatPlanDuration,
    formatPlanPrice,
    formatPlanTraffic,
  } = helpers;
  assert.equal(formatPlanDuration(1), "1 день");
  assert.equal(formatPlanDuration(2), "2 дня");
  assert.equal(formatPlanDuration(5), "5 дней");
  assert.equal(formatPlanDuration(11), "11 дней");
  assert.equal(formatPlanDuration(21), "21 день");
  assert.equal(formatPlanDuration(null), "Без срока");

  assert.equal(formatPlanDevices(1), "1 устройство");
  assert.equal(formatPlanDevices(2), "2 устройства");
  assert.equal(formatPlanDevices(5), "5 устройств");
  assert.equal(formatPlanDevices(12), "12 устройств");

  assert.equal(formatPlanTraffic(null), "Без лимита");
  assert.equal(formatPlanTraffic(100), "100 ГБ");
  assert.match(formatPlanPrice(299, "RUB"), /299[\s ]₽/);
  assert.equal(formatPlanPrice(0, "RUB"), "Бесплатно");
  assert.equal(formatPlanPrice(299, "<script>"), "Цена уточняется");
  assert.equal(formatPlanPrice(Number.NaN, "RUB"), "Цена уточняется");
});

test("catalog cards expose only safe plan fields and reuse the existing trial flow", async () => {
  const [catalog, types] = await Promise.all([
    readFile(catalogUrl, "utf8"),
    readFile(typesUrl, "utf8"),
  ]);

  for (const field of [
    "id",
    "name",
    "description",
    "duration_days",
    "traffic_limit_gb",
    "max_devices",
    "price_amount",
    "currency",
    "is_trial",
  ]) {
    assert.match(types, new RegExp(`\\b${field}\\b`));
  }
  assert.match(catalog, /href="#subscription"/);
  assert.match(catalog, /Перейти к пробному доступу/);
  assert.match(catalog, /disabled/);
  assert.match(catalog, /Покупка скоро будет доступна/);
  assert.match(catalog, /role="status"/);
  assert.match(catalog, /role="alert"/);
  assert.match(catalog, /onRetry/);

  for (const source of [catalog, types]) {
    assert.doesNotMatch(source, /\bslug\b|created_at|updated_at|is_active|is_public|display_order|secret|config_uri|external_uuid|worker_id/i);
  }
});

test("portal loads and retries the catalog independently and rejects stale responses", async () => {
  const source = await readFile(portalUrl, "utf8");
  const loadStart = source.indexOf("async function loadPlans");
  const loadEnd = source.indexOf("async function loadPrivateData", loadStart);
  const loader = source.slice(loadStart, loadEnd);

  assert.notEqual(loadStart, -1);
  assert.notEqual(loadEnd, -1);
  assert.match(loader, /portalApi\.plans\(\)/);
  assert.match(loader, /sessionGeneration\.isCurrent\(generation\)/);
  assert.match(loader, /plansLoadEpoch\.current === loadEpoch/);
  assert.match(loader, /setPlansBusy\(true\)/);
  assert.match(loader, /setPlansError\(""\)/);
  assert.match(loader, /setPlans\(nextPlans\)/);
  assert.doesNotMatch(loader, /handleUnauthorized|clearPrivateData|setDataError|setDataBusy/);

  assert.match(source, /void loadPrivateData\(\);\s*void loadPlans\(\);/);
  assert.match(source, /<PlanCatalog[\s\S]*onRetry=\{\(\) => void loadPlans\(\)\}/);
  assert.match(source, /plansLoadEpoch\.current \+= 1/);
});

test("catalog styling reuses portal tokens and the existing mobile breakpoint", async () => {
  const source = await readFile(cssUrl, "utf8");

  assert.match(source, /\.plan-card\s*\{/);
  assert.match(source, /\.plan-card__description\s*\{/);
  assert.match(source, /\.plan-card__action\s*\{/);
  assert.match(source, /@media \(max-width: 640px\)[\s\S]*\.plan-card__action[\s\S]*width:\s*100%/);
});
