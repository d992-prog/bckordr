import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { api } from "../src/api.ts";
import {
  copyVpnNodeTestProfile,
  onboardingErrorMessage,
  onboardingPresentation,
} from "../src/vpnNodeOnboarding.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

test("VPN node onboarding maps every state to the exact operator copy", () => {
  assert.deepEqual(onboardingPresentation("needs_preflight"), [
    "Проверить сервер",
    "Проверяем только настройки и SSH‑ключ.",
  ]);
  assert.deepEqual(onboardingPresentation("needs_trust"), [
    "Подтвердить SSH‑ключ",
    "Сверьте fingerprint в панели хостинга.",
  ]);
  assert.deepEqual(onboardingPresentation("ready_to_start"), [
    "Настроить VPN‑ноду",
    "Установим защищённое подключение на TCP 443.",
  ]);
  assert.deepEqual(onboardingPresentation("queued"), [
    "Настройка в очереди",
    "Можно закрыть страницу — работа продолжится.",
  ]);
  assert.deepEqual(onboardingPresentation("running"), [
    "Настраиваем VPN‑ноду…",
    "Не выключайте сервер.",
  ]);
  assert.deepEqual(onboardingPresentation("awaiting_test"), [
    "Проверить подключение",
    "Скопируйте временный профиль и подключитесь.",
  ]);
  assert.deepEqual(onboardingPresentation("ready"), [
    "VPN‑нода готова",
    "Нода участвует в выдаче новых профилей.",
  ]);
  assert.deepEqual(onboardingPresentation("failed"), [
    "Повторить безопасно",
    "Перед повтором проверим текущее состояние.",
  ]);
});

test("VPN node onboarding API uses exact paths, methods, and bodies", async () => {
  const status = {
    worker_id: 17,
    state: "needs_preflight",
    fingerprint: null,
    job_id: null,
    endpoint_id: null,
    error_code: null,
  };
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push({ url: String(url), init });
    return {
      ok: true,
      json: async () => calls.length === 5 ? { config_uri: "vless://temporary" } : status,
    };
  };

  await api.getVpnNodeOnboarding(17);
  await api.preflightVpnNode(17);
  await api.trustVpnNode(17, { fingerprint: "SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGH123456789" });
  await api.startVpnNodeOnboarding(17);
  await api.getVpnNodeTestProfile(17);
  await api.confirmVpnNodeOnboarding(17, { confirmed: true });

  assert.deepEqual(
    calls.map(({ url, init }) => [url, init?.method ?? "GET", init?.body ?? null]),
    [
      ["/api/control/workers/17/vpn-onboarding", "GET", null],
      ["/api/control/workers/17/vpn-onboarding/preflight", "POST", null],
      [
        "/api/control/workers/17/vpn-onboarding/trust",
        "POST",
        JSON.stringify({ fingerprint: "SHA256:abcdefghijklmnopqrstuvwxyzABCDEFGH123456789" }),
      ],
      ["/api/control/workers/17/vpn-onboarding/start", "POST", null],
      ["/api/control/workers/17/vpn-onboarding/test-profile", "POST", null],
      [
        "/api/control/workers/17/vpn-onboarding/confirm",
        "POST",
        JSON.stringify({ confirmed: true }),
      ],
    ],
  );
  assert.equal(calls[4].init?.cache, "no-store");
});

test("temporary profile copy prefers Clipboard API", async () => {
  const writes = [];
  const result = await copyVpnNodeTestProfile("vless://temporary", {
    clipboard: { writeText: async (value) => writes.push(value) },
  });

  assert.equal(result, true);
  assert.deepEqual(writes, ["vless://temporary"]);
});

test("temporary profile copy falls back to a selected textarea", async () => {
  const events = [];
  const textarea = {
    value: "",
    readOnly: false,
    style: {},
    setAttribute(name, value) { events.push(["attribute", name, value]); },
    focus() { events.push(["focus"]); },
    select() { events.push(["select"]); },
    setSelectionRange(start, end) { events.push(["range", start, end]); },
    remove() { events.push(["remove"]); },
  };
  const documentRef = {
    body: { appendChild(node) { events.push(["append", node]); } },
    createElement(name) {
      assert.equal(name, "textarea");
      return textarea;
    },
    execCommand(command) {
      assert.equal(command, "copy");
      events.push(["copy"]);
      return true;
    },
  };

  const result = await copyVpnNodeTestProfile("vless://temporary", {
    clipboard: { writeText: async () => { throw new Error("blocked"); } },
    documentRef,
  });

  assert.equal(result, true);
  assert.equal(textarea.value, "vless://temporary");
  assert.deepEqual(events.at(-2), ["copy"]);
  assert.deepEqual(events.at(-1), ["remove"]);
});

test("onboarding errors are fixed Russian copy and unknown codes stay bounded", () => {
  assert.equal(
    onboardingErrorMessage("vpn_node_trust_host_key_mismatch"),
    "SSH‑ключ сервера изменился. Настройка остановлена.",
  );
  assert.equal(
    onboardingErrorMessage("vpn_onboarding_port_conflict"),
    "TCP 443 уже занят другой службой.",
  );
  assert.equal(
    onboardingErrorMessage("unexpected secret-shaped backend detail"),
    "Не удалось продолжить настройку. Повторите проверку позже.",
  );
});

test("onboarding panel exposes safe progressive actions and accessible feedback", async () => {
  const source = await readFile(
    new URL("../src/VpnNodeOnboardingPanel.tsx", import.meta.url),
    "utf8",
  );

  assert.match(source, /role="status"/);
  assert.match(source, /role="alert"/);
  assert.match(source, /type="checkbox"/);
  assert.match(source, /fingerprint/);
  assert.match(source, /trustConfirmed/);
  assert.match(source, /disabled=\{[^}]*isProgressing/);
  assert.match(source, /api\.getVpnNodeTestProfile/);
  assert.match(source, /copyVpnNodeTestProfile/);
  assert.match(source, /api\.confirmVpnNodeOnboarding/);
  assert.match(source, /confirmed: true/);
  assert.match(source, /readOnly/);
  assert.doesNotMatch(source, /dangerouslySetInnerHTML/);
  assert.doesNotMatch(source, /api[_-]?token|private[_-]?key|raw[_-]?log|job\.log/i);
});

test("App loads and polls onboarding only for visible VPN workers", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.match(source, /visibleVpnOnboardingWorkers/);
  assert.match(source, /api\.getVpnNodeOnboarding/);
  assert.match(source, /state === "queued" \|\| status\.state === "running"/);
  assert.match(source, /window\.setTimeout/);
  assert.match(source, /window\.clearTimeout/);
  assert.equal((source.match(/<VpnNodeOnboardingPanel/g) ?? []).length, 2);
});

test("onboarding remains matte, keyboard-visible, and narrow-screen safe", async () => {
  const css = await readFile(new URL("../src/styles.css", import.meta.url), "utf8");

  assert.match(css, /\.vpn-node-onboarding\s*\{[\s\S]*background: var\(--vpn-admin-surface/);
  assert.match(css, /\.vpn-node-onboarding-primary\s*\{[\s\S]*min-height: 44px/);
  assert.match(css, /\.vpn-node-onboarding \.inline-check\s*\{[\s\S]*min-height: 44px/);
  assert.match(css, /\.vpn-node-trust code\s*\{[\s\S]*overflow-wrap: anywhere/);
  assert.match(css, /\.vpn-node-test-profile textarea\s*\{[\s\S]*max-width: 100%/);
  assert.match(css, /\.vpn-node-diagnostics summary\s*\{[\s\S]*min-height: 44px/);
  assert.doesNotMatch(
    css.match(/\.vpn-node-onboarding\s*\{[\s\S]*?\n\}/)?.[0] ?? "",
    /backdrop-filter|linear-gradient|radial-gradient/,
  );
});
