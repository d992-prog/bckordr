import test, { afterEach } from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { execFileSync } from "node:child_process";
import { api } from "../src/api.ts";

const originalFetch = globalThis.fetch;
afterEach(() => { globalThis.fetch = originalFetch; });
async function helpers() {
  try { return await import("../src/vpnCabinetSettings.ts"); }
  catch (cause) { if (cause.code === "ERR_MODULE_NOT_FOUND") return {}; throw cause; }
}
const settings = { revision: 4, support_contact: null, notice: null, reminders_enabled: false };

test("cabinet settings use exact no-store GET/PUT and whole known revision without broadcast calls", async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => { calls.push({ url: String(url), init }); return { ok: true, json: async () => settings }; };
  assert.equal(typeof api.getVpnCabinetSettings, "function");
  assert.deepEqual(await api.getVpnCabinetSettings(), settings);
  assert.deepEqual(await api.updateVpnCabinetSettings({ ...settings, reminders_enabled: true }), settings);
  assert.deepEqual(calls.map(({ url }) => url), ["/api/control/vpn/cabinet-settings", "/api/control/vpn/cabinet-settings"]);
  assert.deepEqual(calls.map(({ init }) => init.method ?? "GET"), ["GET", "PUT"]);
  assert.deepEqual(JSON.parse(calls[1].init.body), { ...settings, reminders_enabled: true });
  for (const { init } of calls) { assert.equal(init.cache, "no-store"); assert.equal(init.credentials, "include"); }
});

test("cabinet draft starts from server gates, allows null support/notice, and requires explicit reminder intent", async () => {
  const { cabinetSettingsDraft, cabinetSettingsPayload } = await helpers();
  assert.equal(typeof cabinetSettingsDraft, "function");
  const draft = cabinetSettingsDraft(settings);
  assert.equal(draft.remindersEnabled, false);
  assert.deepEqual(cabinetSettingsPayload(draft, settings.revision), settings);
  assert.deepEqual(cabinetSettingsPayload({ ...draft, supportContact: "  ", remindersEnabled: true }, 4), { ...settings, reminders_enabled: true });
  assert.equal(cabinetSettingsDraft({ ...settings, reminders_enabled: true }).remindersEnabled, true);
});

test("notice validation preserves literal Unicode text with code point limits and rejects invalid windows", async () => {
  const { cabinetSettingsDraft, cabinetSettingsPayload } = await helpers();
  assert.equal(typeof cabinetSettingsPayload, "function");
  const draft = { ...cabinetSettingsDraft(settings), noticeEnabled: true, title: "😀".repeat(100), body: "<b>Текст</b>\n\t😀", startsAt: "2026-10-26T10:00", endsAt: "2026-10-26T11:00" };
  const payload = cabinetSettingsPayload(draft, 4);
  assert.equal(payload.notice.title, draft.title);
  assert.equal(payload.notice.body, draft.body);
  for (const patch of [{ title: "😀".repeat(101) }, { body: "😀".repeat(1001) }, { body: "x\u200by" }, { body: "x\u0000" }, { body: "x\ud800" }, { title: "" }, { endsAt: draft.startsAt }, { startsAt: "2026-02-30T10:00" }, { endsAt: "" }]) {
    assert.throws(() => cabinetSettingsPayload({ ...draft, ...patch }, 4));
  }
  assert.equal(cabinetSettingsPayload({ ...draft, noticeEnabled: false, title: "" }, 4).notice, null);
});

test("datetime-local displays actual local time and round-trips the instant including seconds", async () => {
  const { cabinetLocalDateTime, cabinetSettingsDraft, cabinetSettingsPayload } = await helpers();
  assert.equal(typeof cabinetLocalDateTime, "function");
  const iso = "2026-10-26T10:15:32.123Z";
  const date = new Date(iso);
  const local = cabinetLocalDateTime(iso);
  assert.equal(Number(local.slice(11, 13)), date.getHours());
  const draft = cabinetSettingsDraft({ ...settings, notice: { title: "Заголовок", body: "Текст", starts_at: iso, ends_at: "2026-10-26T11:15:32.123Z" } });
  assert.equal(cabinetSettingsPayload(draft, 4).notice.starts_at, iso);
});

test("support-only saves preserve the server's DST fold and sub-millisecond notice instants", () => {
  const moduleUrl = new URL("../src/vpnCabinetSettings.ts", import.meta.url).href;
  execFileSync(process.execPath, ["--experimental-strip-types", "--input-type=module", "-e", `
    import assert from "node:assert/strict";
    import { cabinetSettingsDraft, cabinetSettingsPayload } from ${JSON.stringify(moduleUrl)};
    const notice = { title: "Объявление", body: "Текст", starts_at: "2026-11-01T06:30:00.123456Z", ends_at: "2026-11-01T07:30:00.654321Z" };
    const draft = cabinetSettingsDraft({ revision: 2, support_contact: null, notice, reminders_enabled: false });
    assert.equal(draft.startsAt, "2026-11-01T01:30:00.123");
    assert.deepEqual(cabinetSettingsPayload({ ...draft, supportContact: "@support_test" }, 2).notice, notice);
    assert.throws(() => cabinetSettingsPayload({ ...draft, startsAt: "2026-03-08T02:30", endsAt: "2026-03-08T04:00" }, 2), /существующие/);
    const edited = cabinetSettingsPayload({ ...draft, startsAt: "2026-11-01T02:00" }, 2);
    assert.equal(edited.notice.starts_at, "2026-11-01T07:00:00.000Z");
  `], { env: { ...process.env, TZ: "America/New_York" }, stdio: "pipe" });
});

test("only a bounded revision conflict is exposed; unknown upstream errors stay private", async () => {
  const { cabinetSettingsErrorText } = await helpers();
  assert.equal(typeof cabinetSettingsErrorText, "function");
  assert.match(cabinetSettingsErrorText(new Error("vpn_cabinet_settings_revision_conflict")), /изменил|изменены/);
  assert.doesNotMatch(cabinetSettingsErrorText(new Error("<html>private upstream</html>")), /private|upstream|html/);
});

test("settings mount only on active cabinet and are isolated by admin session", async () => {
  const source = await readFile(new URL("../src/App.tsx", import.meta.url), "utf8");
  assert.match(source, /vpnAdminSection === "cabinet" \?[^\n]*<VpnCabinetSettingsPanel key=\{session\?\.user\.id\}/);
});
