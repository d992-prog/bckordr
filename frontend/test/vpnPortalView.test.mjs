import test from "node:test";
import assert from "node:assert/strict";

import { portalDate, stateLabel } from "../src/vpn-portal/view.ts";

test("maps every supported portal state to its Russian label", () => {
  const expectedLabels = new Map([
    ["active", "Активна"],
    ["trial", "Пробный доступ"],
    ["scheduled", "Начнётся позже"],
    ["disabled", "Приостановлена"],
    ["expired", "Истекла"],
    ["cancelled", "Отменена"],
    ["pending_sync", "Подготавливается"],
    ["syncing", "Подготавливается"],
    ["suspended", "Приостановлен"],
    ["pending_suspend", "Отключается"],
    ["pending_revoke", "Отзывается"],
    ["revoked", "Отозван"],
    ["failed", "Нужна помощь"],
  ]);

  for (const [state, label] of expectedLabels) {
    assert.equal(stateLabel(state), label);
  }
});

test("uses the unknown fallback for arbitrary and inherited object property names", () => {
  for (const state of ["unknown", "", "constructor", "__proto__", "toString"]) {
    assert.equal(stateLabel(state), "Статус уточняется");
  }
});

test("formats missing, invalid, and valid portal dates safely", () => {
  const validValue = "2026-09-20T12:00:00";
  const expectedDate = new Intl.DateTimeFormat("ru-RU", {
    day: "numeric",
    month: "long",
    year: "numeric",
  }).format(new Date(validValue));

  assert.equal(portalDate(null), "Без срока окончания");
  assert.equal(portalDate("not-a-date"), "Дата недоступна");
  assert.equal(portalDate(validValue), expectedDate);
});
