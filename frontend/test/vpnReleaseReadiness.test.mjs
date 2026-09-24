import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";
import {
  VPN_RELEASE_STATUS_LABELS,
  formatVpnReleaseCheckedAt,
  formatVpnReleaseEntityLabel,
  groupVpnReleaseChecks,
  summarizeVpnRelease,
} from "../src/vpnReleaseReadiness.ts";

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

test("builds only an integer node label and exposes no sensitive readiness fields", async () => {
  assert.equal(formatVpnReleaseEntityLabel(42), "Нода #42");
  assert.equal(formatVpnReleaseEntityLabel(4.2), null);
  assert.equal(formatVpnReleaseEntityLabel(null), null);

  const source = await readFile(new URL("../src/api.ts", import.meta.url), "utf8");
  const readinessTypes = source.match(
    /export type VpnReleaseCheck[\s\S]*?export type VpnReleaseReadiness[\s\S]*?\n};/,
  )?.[0] ?? "";
  assert.match(readinessTypes, /entity_id: number \| null/);
  assert.match(readinessTypes, /observed_at: string \| null/);
  assert.doesNotMatch(readinessTypes, /fingerprint|config_uri|public_host|panel_url/i);
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

  assert.match(source, /endpoint_external_proof/);
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
  assert.doesNotMatch(source, /updateWorkerVpn|checkWorkerVpn|vpn-update|\/health/);
  assert.doesNotMatch(source, /fingerprint|config_uri|public_host|panel_url/i);
});

test("VPN workspace loads readiness independently and refreshes after capacity changes", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(source, /<VpnReleaseReadinessPanel/);
  assert.match(source, /api\.getVpnReleaseReadiness\(\)/);
  assert.match(source, /vpnReadinessRequestGenerationRef/);
  assert.match(source, /readinessGeneration === vpnReadinessRequestGenerationRef\.current/);
  assert.match(source, /getVpnReleaseReadiness\(\)[\s\S]*?\.catch/);
  assert.match(
    source,
    /onUpdated=\{\(updated\)[\s\S]*?refreshVpnReleaseReadiness/,
  );
  assert.match(source, /id="vpn-nodes-section"/);
  assert.match(source, /id="vpn-capacity-section"/);
  assert.match(source, /id="vpn-maintenance-section"/);
  assert.match(css, /\.vpn-release-readiness\s*\{/);
  assert.match(css, /@media \(max-width: 720px\)[\s\S]*\.vpn-release/);
});
