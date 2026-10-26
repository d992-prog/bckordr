import { useLayoutEffect, useRef, useState } from "react";

import { api } from "./api";
import { cabinetSettingsDraft, cabinetSettingsErrorText, cabinetSettingsPayload, type CabinetSettingsDraft } from "./vpnCabinetSettings";

export function VpnCabinetSettingsPanel() {
  const [draft, setDraft] = useState<CabinetSettingsDraft | null>(null);
  const [revision, setRevision] = useState(0);
  const [busy, setBusy] = useState(false);
  const [recheckRequired, setRecheckRequired] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const request = useRef<object | null>(null);
  const recovery = useRef(false);

  async function load() {
    if (request.current) return;
    const pending = {}; request.current = pending; setBusy(true); setError(""); setNotice("");
    try {
      const response = await api.getVpnCabinetSettings();
      if (request.current !== pending) return;
      setDraft(cabinetSettingsDraft(response)); setRevision(response.revision);
      recovery.current = false; setRecheckRequired(false);
    } catch {
      if (request.current === pending) setError("Не удалось загрузить настройки. Повторите загрузку");
    } finally { if (request.current === pending) { request.current = null; setBusy(false); } }
  }

  useLayoutEffect(() => {
    void load();
    return () => { request.current = null; };
  }, []);

  function edit(patch: Partial<CabinetSettingsDraft>) {
    setDraft((current) => current ? { ...current, ...patch } : current); setError(""); setNotice("");
  }

  async function save() {
    if (!draft || request.current || recovery.current) return;
    let payload;
    try { payload = cabinetSettingsPayload(draft, revision); }
    catch (cause) { setError((cause as Error).message); return; }
    const pending = {}; request.current = pending; setBusy(true); setError(""); setNotice("");
    try {
      const response = await api.updateVpnCabinetSettings(payload);
      if (request.current !== pending) return;
      setDraft(cabinetSettingsDraft(response)); setRevision(response.revision); setNotice("Настройки сохранены");
    } catch (cause) {
      if (request.current !== pending) return;
      recovery.current = true; setRecheckRequired(true); setError(cabinetSettingsErrorText(cause));
    } finally { if (request.current === pending) { request.current = null; setBusy(false); } }
  }

  return <section className="card full-span form vpn-cabinet-settings-panel" aria-labelledby="vpn-cabinet-settings-title">
    <div><h2 id="vpn-cabinet-settings-title">Настройки кабинета</h2><p className="muted">Поддержка, объявление и напоминания для клиентов</p></div>
    {!draft && busy && <p role="status">Загружаем настройки…</p>}
    {draft && <>
      <label htmlFor="vpn-cabinet-support">Контакт поддержки
        <input id="vpn-cabinet-support" value={draft.supportContact} placeholder="@username" autoComplete="off" disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-support-hint" onChange={(event) => edit({ supportContact: event.target.value })} />
      </label>
      <p id="vpn-cabinet-support-hint" className="muted">@username или https://t.me/username. Пустое поле убирает кнопку поддержки из кабинета</p>
      <label className="checkbox" htmlFor="vpn-cabinet-notice" style={{ minHeight: 44 }}>
        <input id="vpn-cabinet-notice" type="checkbox" checked={draft.noticeEnabled} disabled={busy || recheckRequired} onChange={(event) => edit({ noticeEnabled: event.target.checked })} />Объявление в кабинете
      </label>
      <p className="muted">Публикация объявления не отправляет сообщение от бота. Обычный текст без форматирования</p>
      {draft.noticeEnabled && <>
        <label htmlFor="vpn-cabinet-title">Заголовок объявления
          <input id="vpn-cabinet-title" value={draft.title} disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-title-limit" onChange={(event) => edit({ title: event.target.value })} />
        </label>
        <p className="muted" id="vpn-cabinet-title-limit">{Array.from(draft.title).length} из 100 символов</p>
        <label htmlFor="vpn-cabinet-body">Текст объявления
          <textarea id="vpn-cabinet-body" rows={5} value={draft.body} disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-body-limit" onChange={(event) => edit({ body: event.target.value })} />
        </label>
        <p className="muted" id="vpn-cabinet-body-limit">{Array.from(draft.body).length} из 1000 символов</p>
        <div className="form two-columns">
          <label htmlFor="vpn-cabinet-start">Начало показа<input id="vpn-cabinet-start" type="datetime-local" step="0.001" value={draft.startsAt} disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-timezone" onChange={(event) => edit({ startsAt: event.target.value })} /></label>
          <label htmlFor="vpn-cabinet-end">Конец показа<input id="vpn-cabinet-end" type="datetime-local" step="0.001" value={draft.endsAt} disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-timezone" onChange={(event) => edit({ endsAt: event.target.value })} /></label>
        </div>
        <p id="vpn-cabinet-timezone" className="muted">Время в часовом поясе вашего устройства: {Intl.DateTimeFormat().resolvedOptions().timeZone}</p>
      </>}
      <label className="checkbox" htmlFor="vpn-cabinet-reminders" style={{ minHeight: 44 }}>
        <input id="vpn-cabinet-reminders" type="checkbox" checked={draft.remindersEnabled} disabled={busy || recheckRequired} aria-describedby="vpn-cabinet-reminders-hint" onChange={(event) => edit({ remindersEnabled: event.target.checked })} />Напоминания в Telegram
      </label>
      <p id="vpn-cabinet-reminders-hint" className="muted">Включение после сохранения действует для будущих событий. Напоминания получат только клиенты, которые разрешили их в кабинете</p>
      <div className="actions"><button type="button" disabled={busy || recheckRequired} onClick={() => void save()}>{busy ? "Сохраняем…" : "Сохранить настройки"}</button></div>
    </>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="inline-alert error" role="alert">{error}</p>}
    {recheckRequired && <button type="button" className="ghost" disabled={busy} onClick={() => void load()}>Отбросить изменения и загрузить настройки</button>}
    {!draft && !busy && <button type="button" className="ghost" onClick={() => void load()}>Повторить загрузку</button>}
  </section>;
}
