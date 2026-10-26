import test from "node:test";
import assert from "node:assert/strict";

import { portalDate, stateLabel } from "../src/vpn-portal/view.ts";
import * as view from "../src/vpn-portal/view.ts";

test("account initials are bounded without losing the accessible full name", () => {
  assert.equal(typeof view.portalInitials, "function");
  assert.equal(view.portalInitials("Анна Ветрова"), "АВ");
  assert.equal(view.portalInitials("ВК"), "ВК");
  assert.equal(view.portalInitials("  "), "Я");
});

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

test("notices have an exclusive end boundary and invalid times never publish", () => {
  assert.equal(typeof view.portalNoticeVisible,"function");
  const notice={starts_at:"2026-10-26T00:00:00Z",ends_at:"2026-10-26T01:00:00Z"};
  assert.equal(view.portalNoticeVisible(notice,Date.parse(notice.starts_at)-1),false);
  assert.equal(view.portalNoticeVisible(notice,Date.parse(notice.starts_at)),true);
  assert.equal(view.portalNoticeVisible(notice,Date.parse(notice.ends_at)),false);
  assert.equal(view.portalNoticeVisible({...notice,ends_at:"invalid"},0),false);
});

test("remaining days are truthful for expired, invalid and unlimited access", () => {
  assert.equal(typeof view.portalRemainingTime,"function");
  const now=Date.parse("2026-10-26T00:00:00Z");
  assert.equal(view.portalRemainingTime("2026-10-27T00:01:00Z",now),"Осталось 2 дня");
  assert.equal(view.portalRemainingTime("2026-10-25T00:00:00Z",now),"Срок закончился");
  assert.equal(view.portalRemainingTime(null,now),"Без ограничения по сроку");
  assert.equal(view.portalRemainingTime("invalid",now),"Срок уточняется");
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
