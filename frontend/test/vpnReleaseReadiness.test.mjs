import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";
import * as releaseHelpers from "../src/vpnReleaseReadiness.ts";

const {
  VPN_RELEASE_STATUS_LABELS,
  formatVpnReleaseCheckedAt,
  formatVpnReleaseEntityLabel,
  groupVpnReleaseChecks,
  summarizeVpnRelease,
} = releaseHelpers;

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function check(code, state = "pass", entity_id = null) {
  return { code, state, message: code, entity_id, observed_at: null };
}

test("groups release checks in the fixed operational order with a safe fallback", () => {
  const groups = groupVpnReleaseChecks([
    check("z_unknown"),
    check("dispatch"),
    check("endpoint_health", "warn", 9),
    check("system"),
    check("payment_disabled"),
    check("aggregate_capacity"),
    check("worker_active", "pass", 3),
    check("a_unknown"),
    check("endpoint_configuration", "pass", 7),
  ]);

  assert.deepEqual(
    groups.map(({ key, title, checks }) => ({
      key,
      title,
      codes: checks.map((item) => item.code),
    })),
    [
      { key: "infrastructure", title: "Инфраструктура", codes: ["system"] },
      {
        key: "nodes",
        title: "Ноды",
        codes: ["worker_active", "endpoint_configuration", "endpoint_health"],
      },
      { key: "capacity", title: "Ёмкость", codes: ["aggregate_capacity"] },
      { key: "queue", title: "Очередь и обслуживание", codes: ["dispatch"] },
      { key: "product", title: "Продукт", codes: ["payment_disabled"] },
      { key: "other", title: "Прочее", codes: ["a_unknown", "z_unknown"] },
    ],
  );
});

test("keeps the backend health-code variants in the infrastructure section", () => {
  const groups = groupVpnReleaseChecks([
    check("backup_health"),
    check("public_health"),
    check("system_health"),
    check("disk_health"),
    check("control_health"),
    check("cabinet_health"),
    check("local_health"),
  ]);

  assert.deepEqual(groups.map(({ title }) => title), ["Инфраструктура"]);
  assert.deepEqual(
    groups[0].checks.map(({ code }) => code),
    [
      "system_health",
      "control_health",
      "local_health",
      "public_health",
      "cabinet_health",
      "disk_health",
      "backup_health",
    ],
  );
});

test("uses fixed Russian status labels and formats checked time safely", () => {
  assert.deepEqual(VPN_RELEASE_STATUS_LABELS, {
    pass: "Пройдено",
    warn: "Предупреждение",
    fail: "Ошибка",
  });
  assert.equal(formatVpnReleaseCheckedAt(null), "—");
  assert.equal(formatVpnReleaseCheckedAt("not-a-date"), "—");
  assert.match(formatVpnReleaseCheckedAt("2026-09-24T10:00:00Z"), /MSK$/);
});

test("counts every state and permits commit only for a ready report without failures", () => {
  const readyWithWarning = {
    ready: true,
    checked_at: "2026-09-24T10:00:00Z",
    checks: [check("system"), check("backup", "warn")],
  };
  assert.deepEqual(summarizeVpnRelease(readyWithWarning), {
    pass: 1,
    warn: 1,
    fail: 0,
    canCommit: true,
  });
  assert.equal(summarizeVpnRelease({ ...readyWithWarning, ready: false }).canCommit, false);
  assert.equal(
    summarizeVpnRelease({
      ...readyWithWarning,
      checks: [...readyWithWarning.checks, check("known_hosts", "fail")],
    }).canCommit,
    false,
  );
});

test("uses code-aware labels only for positive safe entity identifiers", () => {
  assert.equal(formatVpnReleaseEntityLabel(check("worker_health", "pass", 42)), "Нода #42");
  assert.equal(formatVpnReleaseEntityLabel(check("endpoint_capacity", "pass", 7)), "Нода #7");
  assert.equal(formatVpnReleaseEntityLabel(check("maintenance", "pass", 3)), "Нода #3");
  assert.equal(formatVpnReleaseEntityLabel(check("public_trial_plan", "pass", 9)), "Тариф #9");
  assert.equal(formatVpnReleaseEntityLabel(check("release_id", "pass", 2)), null);

  for (const entityId of [null, 0, -1, 4.2, Number.MAX_SAFE_INTEGER + 1]) {
    assert.equal(
      formatVpnReleaseEntityLabel(check("endpoint_health", "pass", entityId)),
      null,
    );
  }
});

test("external proof confirmation accepts only a positive safe endpoint id", () => {
  assert.equal(
    releaseHelpers.canConfirmVpnExternalProof(check("endpoint_external_proof", "fail", 11)),
    true,
  );
  for (const candidate of [
    check("endpoint_external_proof", "pass", 11),
    check("endpoint_health", "fail", 11),
    check("endpoint_external_proof", "fail", 0),
    check("endpoint_external_proof", "fail", -1),
    check("endpoint_external_proof", "fail", Number.MAX_SAFE_INTEGER + 1),
  ]) {
    assert.equal(releaseHelpers.canConfirmVpnExternalProof(candidate), false);
  }
});

test("readiness types expose no sensitive fields", async () => {

  const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const readinessTypes = source.match(
    /export type VpnReleaseCheck[\s\S]*?export type VpnReleaseReadiness[\s\S]*?\n};/,
  )?.[0] ?? "";
  assert.match(readinessTypes, /entity_id: number \| null/);
  assert.match(readinessTypes, /observed_at: string \| null/);
  assert.doesNotMatch(readinessTypes, /fingerprint|config_uri|public_host|panel_url/i);
});

test("maps readiness codes to fixed Russian labels with a safe fallback", () => {
  assert.equal(releaseHelpers.formatVpnReleaseCheckLabel("system_health"), "Состояние системы");
  assert.equal(releaseHelpers.formatVpnReleaseCheckLabel("endpoint_external_proof"), "Внешняя проверка подключения");
  assert.equal(releaseHelpers.formatVpnReleaseCheckLabel("public_trial_plan"), "Тариф пробного доступа");
  assert.equal(releaseHelpers.formatVpnReleaseCheckLabel("unexpected_code"), "Проверка");
});

test("readiness API uses exact methods, paths, and confirmation body", async () => {
  const report = {
    ready: false,
    checked_at: "2026-09-24T10:00:00Z",
    checks: [],
  };
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => calls.length === 1
        ? report
        : calls.length === 2
          ? { endpoint_id: 7, confirmed: true, confirmed_at: report.checked_at }
          : { detail: "VPN release readiness committed" },
    };
  };

  assert.deepEqual(await api.getVpnReleaseReadiness(), report);
  await api.confirmVpnEndpointExternalVerification(7);
  await api.commitVpnReleaseReadiness();

  assert.deepEqual(
    calls.map(({ url, init }) => [url, init?.method ?? "GET", init?.body ?? null]),
    [
      ["/api/control/vpn/release-readiness", "GET", null],
      [
        "/api/control/vpn/endpoints/7/external-verification",
        "POST",
        JSON.stringify({ confirmed: true }),
      ],
      ["/api/control/vpn/release-readiness/commit", "POST", null],
    ],
  );
});

test("release panel guards both mutations and does not duplicate node operations", async () => {
  const source = await readFile(
    new URL("../src/VpnReleaseReadinessPanel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /canConfirmVpnExternalProof/);
  assert.match(source, /Подтвердить внешний тест/);
  assert.match(source, /window\.confirm/);
  assert.match(source, /ГОТОВО К РЕЛИЗУ/);
  assert.match(source, /commitPhrase !== RELEASE_COMMIT_PHRASE/);
  assert.match(
    source,
    /disabled=\{commitPhrase !== RELEASE_COMMIT_PHRASE \|\| actionInFlight !== null \|\| loading\}/,
  );
  assert.match(source, /disabled=\{actionInFlight !== null \|\| loading\}/);
  assert.match(source, /api\.confirmVpnEndpointExternalVerification/);
  assert.match(source, /api\.commitVpnReleaseReadiness/);
  assert.match(source, /НЕ включает оплату и пробный доступ/);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML/);
  assert.match(source, /Релизный контур/);
  assert.match(source, /\{check\.message\}/);
  assert.doesNotMatch(source, /Release gate/);
  assert.doesNotMatch(source, /\{check\.code\}/);
  assert.doesNotMatch(source, /updateWorkerVpn|checkWorkerVpn|vpn-update|\/health/);
  assert.doesNotMatch(source, /fingerprint|config_uri|public_host|panel_url/i);
});

test("VPN workspace starts readiness outside the shared admin Promise.all", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const refreshStart = source.indexOf("async function refreshVpnReadiness");
  const loadStart = source.indexOf("async function loadAll");
  const nextFunction = source.indexOf("async function loadStrategyDetails", loadStart);

  assert.ok(refreshStart >= 0);
  assert.ok(loadStart > refreshStart);
  const refreshSource = source.slice(refreshStart, loadStart);
  const loadSource = source.slice(loadStart, nextFunction);

  assert.match(refreshSource, /api\.getVpnReleaseReadiness\(\)/);
  assert.match(refreshSource, /vpnReadinessRequestGenerationRef/);
  assert.match(refreshSource, /readinessGeneration === vpnReadinessRequestGenerationRef\.current/);
  assert.match(loadSource, /void refreshVpnReadiness\(\{ silent: options\?\.silent \}\)/);
  assert.doesNotMatch(loadSource, /api\.getVpnReleaseReadiness/);
  assert.doesNotMatch(loadSource, /setVpn(?:ReleaseReadiness|ReadinessLoading|ReadinessError)/);
});

test("silent readiness refresh still owns the loading guard", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const refreshStart = source.indexOf("async function refreshVpnReadiness");
  const loadStart = source.indexOf("async function loadAll");
  const refreshSource = source.slice(refreshStart, loadStart);

  assert.match(refreshSource, /setVpnReadinessLoading\(true\)/);
  assert.match(refreshSource, /finally[\s\S]*setVpnReadinessLoading\(false\)/);
  assert.match(refreshSource, /if \(!options\?\.silent\)[\s\S]*setVpnReadinessError\(null\)/);
});

test("VPN workspace renders readiness and refreshes it after capacity changes", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(source, /<VpnReleaseReadinessPanel/);
  assert.match(
    source,
    /onUpdated=\{\(updated\)[\s\S]*?refreshVpnReadiness/,
  );
  assert.match(source, /id="vpn-nodes-section"/);
  assert.match(source, /id="vpn-capacity-section"/);
  assert.match(source, /id="vpn-maintenance-section"/);
  assert.match(css, /\.vpn-release-readiness\s*\{/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.vpn-release/);
});
