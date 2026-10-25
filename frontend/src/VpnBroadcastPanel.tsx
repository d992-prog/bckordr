import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { api, type VpnBroadcastConfig, type VpnBroadcastCounts, type VpnBroadcastDetail, type VpnBroadcastSummary } from "./api";
import { displayMetric } from "./vpnAdminView";
import { broadcastCharacters, broadcastErrorText, broadcastStateLabel, canConfirmBroadcast, validateBroadcastBody } from "./vpnBroadcasts";

function summary(detail: VpnBroadcastDetail): VpnBroadcastSummary {
  const { id, revision, state, created_at, confirmed_at, cancelled_at } = detail;
  return { id, revision, state, created_at, confirmed_at, cancelled_at };
}
function mergeCampaigns(...pages: VpnBroadcastSummary[][]) {
  return [...new Map(pages.flat().map((row) => [row.id, row])).values()].sort((a, b) => b.id - a.id);
}
const countLabels: [keyof VpnBroadcastCounts, string][] = [
  ["recipient_count", "Всего получателей"], ["pending", "В очереди"], ["claimed", "Подготовка отправки"],
  ["sending", "Отправляется сейчас"], ["sent", "Отправлено"], ["failed", "Ошибки"],
  ["skipped", "Пропущено: бот заблокирован"], ["uncertain", "Неопределено"], ["cancelled", "Отменено"],
];

export function VpnBroadcastPanel() {
  const [config, setConfig] = useState<VpnBroadcastConfig | null>(null);
  const [campaigns, setCampaigns] = useState<VpnBroadcastSummary[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selected = useRef<number | null>(null);
  const [detail, setDetail] = useState<VpnBroadcastDetail | null>(null);
  const [body, setBody] = useState("");
  const [preview, setPreview] = useState<VpnBroadcastDetail | null>(null);
  const [busy, setBusy] = useState(false);
  const [reading, setReading] = useState(false);
  const [listBusy, setListBusy] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [configError, setConfigError] = useState("");
  const [listError, setListError] = useState("");
  const [readError, setReadError] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [confirmOpen, setConfirmOpen] = useState(false);
  const configRequest = useRef<object | null>(null);
  const listRequest = useRef<object | null>(null);
  const detailRequest = useRef<object | null>(null);
  const actionRequest = useRef<object | null>(null);
  const cursor = useRef<number | undefined>(undefined);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const cancelButton = useRef<HTMLButtonElement | null>(null);
  const sendButton = useRef<HTMLButtonElement | null>(null);

  async function loadConfig() {
    const pending = {}; configRequest.current = pending;
    try {
      const response = await api.getVpnBroadcastConfig();
      if (configRequest.current === pending) { setConfig(response); setConfigError(""); }
    } catch { if (configRequest.current === pending) setConfigError("Не удалось проверить настройки рассылок. Обновите настройки и список."); }
  }
  async function loadList(append = false) {
    if (listRequest.current) return;
    const pending = {}; listRequest.current = pending; setListBusy(true);
    try {
      const rows = await api.getVpnBroadcasts(append ? { beforeId: cursor.current } : undefined);
      if (listRequest.current !== pending) return;
      setCampaigns((current) => mergeCampaigns(append ? current : current.filter((row) => row.id === selected.current), rows));
      cursor.current = rows[rows.length - 1]?.id;
      setHasMore(rows.length === 50);
      setListError("");
    } catch { if (listRequest.current === pending) setListError("Не удалось загрузить список рассылок. Обновите настройки и список."); }
    finally { if (listRequest.current === pending) { listRequest.current = null; setListBusy(false); } }
  }
  async function loadDetail(id: number, replaceBody = false) {
    if (actionRequest.current) return;
    const pending = {}; detailRequest.current = pending; setReading(true); setReadError("");
    try {
      const response = await api.getVpnBroadcast(id);
      if (detailRequest.current !== pending || selected.current !== id) return;
      setDetail(response);
      setPreview((current) => current?.id === response.id && response.state === "draft"
        && current.revision === response.revision && current.body === response.body ? current : null);
      if (replaceBody || response.state !== "draft") setBody(response.body);
      setCampaigns((current) => mergeCampaigns(current, [summary(response)]));
    } catch { if (detailRequest.current === pending) setReadError("Не удалось обновить состояние рассылки. Повторите проверку."); }
    finally { if (detailRequest.current === pending) { detailRequest.current = null; setReading(false); } }
  }

  useLayoutEffect(() => {
    void loadConfig(); void loadList();
    return () => { configRequest.current = listRequest.current = detailRequest.current = actionRequest.current = null; };
  }, []);

  function chooseCampaign(id: number | null) {
    detailRequest.current = actionRequest.current = null;
    selected.current = id; setSelectedId(id); setDetail(null); setBody(""); setPreview(null);
    setBusy(false); setReading(false); setConfirmOpen(false); setError(""); setReadError(""); setNotice("");
    if (id !== null) void loadDetail(id, true);
  }

  useEffect(() => {
    if (detail?.state !== "running" || busy || reading || readError) return;
    const timer = window.setTimeout(() => { if (!actionRequest.current) void loadDetail(detail.id); }, 2000);
    return () => window.clearTimeout(timer);
  }, [detail, busy, reading, readError]);

  const confirmable = canConfirmBroadcast(config, preview, body);
  useLayoutEffect(() => {
    if (!confirmOpen) return;
    const element = dialog.current;
    element?.showModal(); cancelButton.current?.focus();
    return () => { element?.close(); if (sendButton.current?.isConnected) sendButton.current.focus(); };
  }, [confirmOpen]);
  useEffect(() => { if (!confirmable) setConfirmOpen(false); }, [confirmable]);

  let validBody = false;
  try { validateBroadcastBody(body); validBody = true; } catch { /* Validation is shown beside the plain-text counter. */ }
  const draft = selectedId === null || detail?.state === "draft";
  const dirty = detail === null || body !== detail.body;

  async function act(action: "save" | "preview" | "confirm" | "cancel") {
    if (actionRequest.current) return;
    if (action === "save" && (!draft || !validBody)) return;
    if (action === "preview" && (!detail || detail.state !== "draft" || dirty || config?.registry_state !== "ready")) return;
    if (action === "confirm" && (!confirmOpen || !confirmable || !preview)) return;
    if (action === "cancel" && detail?.state !== "running") return;
    const pending = {}; actionRequest.current = pending; detailRequest.current = null;
    setBusy(true); setReading(false); setError(""); setNotice("");
    if (action !== "confirm") setPreview(null);
    try {
      let response: VpnBroadcastDetail;
      if (action === "save") response = selectedId === null
        ? await api.createVpnBroadcast(validateBroadcastBody(body))
        : await api.updateVpnBroadcast(selectedId, validateBroadcastBody(body));
      else if (action === "preview") response = await api.previewVpnBroadcast(detail!.id);
      else if (action === "confirm") response = await api.confirmVpnBroadcast(preview!.id, {
        expected_revision: preview!.revision, expected_recipient_count: preview!.preview_recipient_count!, confirm: true,
      });
      else response = await api.cancelVpnBroadcast(detail!.id);
      if (actionRequest.current !== pending) return;
      selected.current = response.id; setSelectedId(response.id); setDetail(response); setBody(response.body);
      setCampaigns((current) => mergeCampaigns(current, [summary(response)]));
      setPreview(action === "preview" ? response : null); setConfirmOpen(false);
      if (action === "save") setNotice("Черновик сохранён");
    } catch (cause) {
      if (actionRequest.current !== pending) return;
      setPreview(null); setConfirmOpen(false); setError(broadcastErrorText(cause));
      actionRequest.current = null; setBusy(false);
      void loadConfig(); if (selected.current !== null) void loadDetail(selected.current, action !== "save");
    } finally { if (actionRequest.current === pending) { actionRequest.current = null; setBusy(false); } }
  }

  return <section className="card full-span vpn-broadcast-panel">
    <div className="card-head"><div><h2>Новости VPN-бота</h2><p className="muted">Обычный текст без HTML и вложений. Сохранение и предпросмотр ничего не отправляют.</p></div>
      <button type="button" className="ghost" disabled={listBusy} onClick={() => { void loadConfig(); void loadList(); }}>Обновить настройки и список</button></div>
    {config ? <div className="inline-alert info"><p>{config.explanation}</p><p>Получателей в реестре: {displayMetric(config.eligible_recipients)}</p>{!config.enabled && <p>Отправка отключена. Черновики можно сохранять.</p>}</div> : <p role="status">Проверяем настройки рассылок…</p>}
    {configError && <p className="inline-alert error" role="alert">{configError}</p>}
    {listError && <p className="inline-alert error" role="alert">{listError}</p>}
    <div className="actions"><div className="form"><label htmlFor="vpn-news-campaign">Рассылка</label><select id="vpn-news-campaign" value={selectedId ?? ""} onChange={(event) => chooseCampaign(event.target.value ? Number(event.target.value) : null)}>
      <option value="">Новый черновик</option>{campaigns.map((row) => <option key={row.id} value={row.id}>#{row.id} · {broadcastStateLabel(row.state)}</option>)}
    </select></div><button type="button" className="ghost" onClick={() => chooseCampaign(null)}>Новый черновик</button>
      {hasMore && <button type="button" className="ghost" disabled={listBusy} onClick={() => void loadList(true)}>Показать ещё рассылки</button>}</div>
    {reading && <p role="status">Обновляем состояние…</p>}
    {readError && <p className="inline-alert error" role="alert">{readError}</p>}
    {detail && <p><strong>#{detail.id}</strong> · <span>{broadcastStateLabel(detail.state)}</span> · Версия {detail.revision}</p>}
    <div className="form"><label htmlFor="vpn-news-body">Текст новости</label><textarea id="vpn-news-body" rows={8} autoComplete="off" aria-describedby="vpn-news-count" value={body} disabled={!draft || busy || reading && !detail} onChange={(event) => { detailRequest.current = null; setReading(false); setBody(event.target.value); setPreview(null); setConfirmOpen(false); setNotice(""); }} /></div>
    <p id="vpn-news-count" className="muted">Осталось символов: {4096 - broadcastCharacters(body)}</p>
    {body && !validBody && <p className="inline-alert error" role="alert">Введите текст новости: от 1 до 4096 символов.</p>}
    <div className="actions">
      <button type="button" disabled={!draft || !validBody || busy || !config || selectedId === null && config.registry_state === "unconfigured"} onClick={() => void act("save")}>Сохранить черновик</button>
      <button type="button" className="ghost" disabled={!detail || !draft || dirty || busy || reading || config?.registry_state !== "ready"} onClick={() => void act("preview")}>Предпросмотр</button>
      <button type="button" ref={sendButton} disabled={!confirmable || busy || reading} onClick={() => setConfirmOpen(true)}>Отправить новость</button>
      {selectedId !== null && <button type="button" className="ghost" disabled={busy || reading} onClick={() => void loadDetail(selectedId, !detail || !dirty)}>Обновить состояние</button>}
      {detail?.state === "running" && <button type="button" className="danger" disabled={busy} onClick={() => void act("cancel")}>Остановить оставшуюся очередь</button>}
    </div>
    {draft && dirty && selectedId !== null && <p className="muted">Сохраните изменения перед предпросмотром.</p>}
    {notice && <p role="status">{notice}</p>}
    {error && <p className="inline-alert error" role="alert">{error}</p>}
    {preview && <div><h3>Предпросмотр</h3><p>Получателей: {displayMetric(preview.preview_recipient_count)}</p><div className="vpn-broadcast-preview" tabIndex={0} aria-label="Точный текст новости">{preview.body}</div></div>}
    {detail && detail.state !== "draft" && <div><h3>Ход рассылки</h3><dl className="vpn-broadcast-counts">{countLabels.map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{displayMetric(detail.counts[key])}</dd></div>)}</dl>
      <p>Остановка затрагивает оставшуюся очередь. Уже начатая отправка может завершиться после остановки; отправленные сообщения не отзываются.</p>
      <p>Неопределено: Telegram мог доставить сообщение. Автоматического повтора не будет</p></div>}
    {confirmOpen && preview && <dialog ref={dialog} className="vpn-admin-shell vpn-node-dialog vpn-broadcast-dialog" aria-modal="true" aria-labelledby="vpn-news-confirm-title" tabIndex={-1}
      onCancel={(event) => { event.preventDefault(); if (!actionRequest.current) setConfirmOpen(false); }}
      onKeyDown={(event) => { if (busy && event.key === "Tab") { event.preventDefault(); dialog.current?.focus(); } }}>
      <h2 id="vpn-news-confirm-title">Подтвердить отправку новости?</h2><p>Получателей: {displayMetric(preview.preview_recipient_count)} · Версия {preview.revision}</p>
      <div className="vpn-broadcast-preview" tabIndex={0} aria-label="Точный текст новости">{preview.body}</div><p>Сообщения нельзя отозвать после отправки.</p>
      <div className="vpn-node-dialog-actions"><button type="button" className="ghost" ref={cancelButton} disabled={busy} onClick={() => setConfirmOpen(false)}>Отмена</button>
        <button type="button" disabled={!confirmable || busy} onClick={() => void act("confirm")}>{busy ? "Подтверждаем…" : "Подтвердить отправку"}</button></div>
    </dialog>}
  </section>;
}
