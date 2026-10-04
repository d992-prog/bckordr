import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import { displayMetric, vpnAdminSectionFromHash } from "../src/vpnAdminView.ts";

test("VPN admin sections are stable and unknown metrics stay unknown", () => {
  assert.equal(vpnAdminSectionFromHash("#vpn/overview"), "overview");
  assert.equal(vpnAdminSectionFromHash("#vpn/customers"), "customers");
  assert.equal(vpnAdminSectionFromHash("#vpn/nodes"), "nodes");
  assert.equal(vpnAdminSectionFromHash("#vpn/plans"), "plans");
  assert.equal(vpnAdminSectionFromHash("#vpn/events"), "events");
  assert.equal(vpnAdminSectionFromHash("#vpn/unknown"), "overview");
  assert.equal(displayMetric(null), "Нет данных");
  assert.equal(displayMetric(0), "0");
  assert.equal(displayMetric(12), "12");
});

test("VPN admin hash parsing ignores query details and unsafe section names", () => {
  assert.equal(vpnAdminSectionFromHash(""), "overview");
  assert.equal(vpnAdminSectionFromHash("#vpn"), "overview");
  assert.equal(vpnAdminSectionFromHash("#vpn/"), "overview");
  assert.equal(vpnAdminSectionFromHash("#/vpn/nodes/"), "nodes");
  assert.equal(vpnAdminSectionFromHash("#vpn/customers?query=anna"), "customers");
  assert.equal(vpnAdminSectionFromHash("#vpn/nodes/extra"), "overview");
  assert.equal(vpnAdminSectionFromHash("#vpn/__proto__"), "overview");
  assert.equal(vpnAdminSectionFromHash("#other/events"), "overview");
  assert.equal(displayMetric(undefined), "Нет данных");
  assert.equal(displayMetric(-3), "-3");
});

test("VPN admin navigation exposes the five stable sections and current page", async () => {
  const source = await readFile(new URL("../src/VpnAdminNavigation.tsx", import.meta.url), "utf8");
  const hrefs = [...source.matchAll(/href:\s*"(#vpn\/[a-z]+)"/g)].map((match) => match[1]);

  assert.deepEqual(hrefs, [
    "#vpn/overview",
    "#vpn/customers",
    "#vpn/nodes",
    "#vpn/plans",
    "#vpn/events",
  ]);
  assert.match(source, /aria-label="Разделы управления VPN"/);
  assert.match(source, /"button-link ghost active-chip"/);
  assert.match(source, /aria-current=\{item\.key === activeSection \? "page" : undefined\}/);
  for (const label of ["Обзор", "Клиенты", "Ноды", "Тарифы", "События"]) {
    assert.match(source, new RegExp(`label: "${label}"`));
  }
});

test("VPN workspace mounts one hash listener and one section at a time", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");

  assert.match(source, /const \[vpnAdminSection, setVpnAdminSection\] = useState/);
  assert.equal((source.match(/addEventListener\("hashchange", syncVpnAdminSection\)/g) ?? []).length, 1);
  assert.equal((source.match(/removeEventListener\("hashchange", syncVpnAdminSection\)/g) ?? []).length, 1);
  assert.match(source, /<VpnAdminNavigation activeSection=\{vpnAdminSection\} \/>/);
  for (const section of ["overview", "customers", "nodes", "plans", "events"]) {
    assert.match(source, new RegExp(`vpnAdminSection === "${section}"`));
  }
});
