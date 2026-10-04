import test from "node:test";
import assert from "node:assert/strict";

import { portalSectionFromHash } from "../src/vpn-portal/navigation.ts";

test("maps old and new portal hashes into three destinations", () => {
  assert.equal(portalSectionFromHash("#home"), "home");
  assert.equal(portalSectionFromHash("#subscription"), "home");
  assert.equal(portalSectionFromHash("#plans"), "home");
  assert.equal(portalSectionFromHash("#profiles"), "profiles");
  assert.equal(portalSectionFromHash("#connect"), "profiles");
  assert.equal(portalSectionFromHash("#account"), "account");
  assert.equal(portalSectionFromHash("#help"), "account");
  assert.equal(portalSectionFromHash("#unknown"), "home");
  assert.equal(portalSectionFromHash("#constructor"), "home");
  assert.equal(portalSectionFromHash("#__proto__"), "home");
});

test("accepts slash hashes, ignores queries, and safely defaults empty input", () => {
  assert.equal(portalSectionFromHash("#/profiles?from=bot"), "profiles");
  assert.equal(portalSectionFromHash("#account?tab=help"), "account");
  assert.equal(portalSectionFromHash("#/"), "home");
  assert.equal(portalSectionFromHash(""), "home");
});
