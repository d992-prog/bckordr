import { useLayoutEffect, useRef, useState } from "react";

import {
  incidentMatchesAbuseState,
  mergeAbuseIncidents,
  VPN_ABUSE_PAGE_SIZE,
  type VpnAbuseIncident,
  type VpnAbuseState,
  type VpnAbuseTransport,
} from "./vpnAbuse";
import { formatUsageTime } from "./vpnUsage";

type Props = {
  transport: VpnAbuseTransport;
  contextKey: string | number;
  onOpenCustomer: (customerId: number, deviceId: number) => void;
};

export function VpnAbusePanel({ transport, contextKey, onOpenCustomer }: Props) {
  const [state, setState] = useState<VpnAbuseState>("open");
  const [incidents, setIncidents] = useState<VpnAbuseIncident[]>([]);
  const [loading, setLoading] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  const [acknowledgingId, setAcknowledgingId] = useState<number | null>(null);
  const [acknowledgementError, setAcknowledgementError] = useState<{ id: number; text: string } | null>(null);
  const contextRef = useRef<object>({});
  const requestRef = useRef<object | null>(null);
  const acknowledgementRef = useRef<object | null>(null);
  const cursorRef = useRef(0);

  async function load(append = false) {
    if (requestRef.current) return;
    const context = contextRef.current;
    const pending = {};
    requestRef.current = pending;
    const isCurrent = () => contextRef.current === context && requestRef.current === pending;
    const afterId = append ? cursorRef.current : 0;
    setLoading(true);
    setError("");
    setMessage("");
    try {
      const page = await transport.listIncidents(state, { afterId, limit: VPN_ABUSE_PAGE_SIZE });
      if (!isCurrent()) return;
      if (!page.every((incident) => incidentMatchesAbuseState(incident, state))) {
        throw new Error("abuse review state mismatch");
      }
      setIncidents((previous) => mergeAbuseIncidents(append ? previous : [], page));
      cursorRef.current = page[page.length - 1]?.id ?? afterId;
      setHasMore(page.length === VPN_ABUSE_PAGE_SIZE);
      setAcknowledgementError(null);
    } catch {
      if (isCurrent()) setError("Не удалось загрузить сигналы. Повторите загрузку списка");
    } finally {
      if (isCurrent()) { requestRef.current = null; setLoading(false); }
    }
  }

  useLayoutEffect(() => {
    contextRef.current = {};
    requestRef.current = null;
    cursorRef.current = 0;
    setIncidents([]);
    setHasMore(false);
    setError("");
    setMessage("");
    setAcknowledgementError(null);
    void load();
    return () => { contextRef.current = {}; requestRef.current = null; };
  }, [contextKey, state]);

  async function acknowledge(incident: VpnAbuseIncident) {
    if (acknowledgementRef.current) return;
    const context = contextRef.current;
    const pending = {};
    acknowledgementRef.current = pending;
    setAcknowledgingId(incident.id);
    setAcknowledgementError(null);
    setMessage("");
    try {
      const saved = await transport.acknowledgeIncident(incident.id);
      if (contextRef.current !== context) return;
      if (saved.id !== incident.id || saved.device_id !== incident.device_id
        || saved.customer_id !== incident.customer_id || saved.acknowledged_at === null) {
        throw new Error("abuse acknowledgement mismatch");
      }
      requestRef.current = null;
      setLoading(false);
      setIncidents((previous) => previous.filter((row) => row.id !== incident.id));
      setMessage("Сигнал отмечен проверенным. Доступ клиента не изменён");
    } catch {
      if (contextRef.current === context) {
        requestRef.current = null;
        setLoading(false);
        setAcknowledgementError({
          id: incident.id,
          text: "Не удалось подтвердить результат проверки. Обновите список, чтобы узнать, сохранена ли отметка",
        });
      }
    } finally {
      if (acknowledgementRef.current === pending) {
        acknowledgementRef.current = null;
        setAcknowledgingId(null);
      }
    }
  }

  function openCustomer(incident: VpnAbuseIncident) {
    contextRef.current = {};
    requestRef.current = null;
    onOpenCustomer(incident.customer_id, incident.device_id);
  }

  return (
    <section className="card full-span vpn-abuse-panel" aria-label="Проверка совместного использования VPN">
      <div className="card-head">
        <div>
          <h2>Подозрение на совместное использование</h2>
          <p className="muted">Это сигнал для проверки, а не число физических устройств. Доступ не блокируется автоматически</p>
          <p className="muted">Основание сигнала: более 2 сетевых источников за 5 полных последовательных минутных измерений. Текущая полнота сбора здесь не показана</p>
        </div>
        <div className="actions">
          <label>
            <span>Статус проверки</span>
            <select value={state} onChange={(event) => setState(event.target.value as VpnAbuseState)}>
              <option value="open">Требуют проверки</option>
              <option value="acknowledged">Проверенные</option>
            </select>
          </label>
          <button type="button" className="ghost" disabled={loading} onClick={() => void load()}>Обновить список</button>
        </div>
      </div>
      {loading && <p role="status">Загружаем сигналы…</p>}
      {acknowledgingId !== null && <p role="status">Сохраняем результат проверки…</p>}
      {message && <p role="status">{message}</p>}
      {error && <div role="alert"><p>{error}</p>{incidents.length > 0 && <p>Показаны ранее загруженные сигналы</p>}</div>}
      {acknowledgementError && <p role="alert">{acknowledgementError.text}</p>}
      <div className="simple-table vpn-usage-table">
        <table>
          <caption className="sr-only">Сигналы для проверки совместного использования VPN</caption>
          <thead><tr><th scope="col">Клиент</th><th scope="col">Логический профиль</th><th scope="col">Период наблюдения</th><th scope="col">Сетевые источники</th><th scope="col">Проверка</th><th scope="col">Действия</th></tr></thead>
          <tbody>{incidents.map((incident) => <tr key={incident.id}>
            <td data-label="Клиент">Клиент #{incident.customer_id}</td>
            <td data-label="Логический профиль">{incident.display_name}<div className="row-hint">Профиль #{incident.device_id}</div></td>
            <td data-label="Период наблюдения">{formatUsageTime(incident.started_at)}<div className="row-hint">Последнее наблюдение: {formatUsageTime(incident.last_observed_at)}</div></td>
            <td data-label="Сетевые источники">Сетевых источников: {incident.peak_source_count}<div className="row-hint">максимум за период</div></td>
            <td data-label="Проверка">{incident.acknowledged_at === null ? "Требует проверки" : `Проверен: ${formatUsageTime(incident.acknowledged_at)}`}</td>
            <td data-label="Действия"><div className="actions">
              {state === "open" && <button type="button" className="ghost"
                disabled={acknowledgingId !== null || acknowledgementError?.id === incident.id}
                onClick={() => void acknowledge(incident)}>Отметить проверенным</button>}
              <button type="button" className="ghost" onClick={() => openCustomer(incident)}>Открыть клиента</button>
            </div></td>
          </tr>)}</tbody>
        </table>
      </div>
      {!loading && !error && incidents.length === 0 && <p className="empty">{state === "open" ? "Сигналов для проверки пока нет" : "Проверенных сигналов пока нет"}</p>}
      {hasMore && <button type="button" className="ghost" disabled={loading} onClick={() => void load(true)}>Показать ещё сигналы</button>}
    </section>
  );
}
