import { useLayoutEffect, useRef, useState } from "react";

import { api, type VpnCustomerUsage, type VpnNodeUsage, type VpnUsageDays, type VpnUsageTotals, type WorkerNode } from "./api";
import { displayMetric } from "./vpnAdminView";
import { formatTrafficBytes, formatUsageTime, usageCoverageLabel } from "./vpnUsage";

type Props = {
  customerId?: number;
  workers?: Pick<WorkerNode, "id" | "name">[];
};

function Coverage({ totals }: { totals: VpnUsageTotals }) {
  return <div>
    <span>{usageCoverageLabel(totals.coverage)}</span>
    <div className="row-hint">Последнее измерение: {formatUsageTime(totals.last_sample_at)}</div>
    <div className="row-hint">Измеряем с: {formatUsageTime(totals.measured_since)}</div>
  </div>;
}

export function VpnUsagePanel({ customerId, workers = [] }: Props) {
  const [days, setDays] = useState<VpnUsageDays>(1);
  const [nodes, setNodes] = useState<VpnNodeUsage[]>([]);
  const [customer, setCustomer] = useState<VpnCustomerUsage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [hasMore, setHasMore] = useState(false);
  const cursor = useRef(0);
  const request = useRef<object | null>(null);

  async function load(append = false): Promise<void> {
    if (request.current) return;
    const pending = {};
    request.current = pending;
    const isCurrent = () => request.current === pending;
    setLoading(true);
    setError("");
    try {
      if (customerId !== undefined) {
        const response = await api.getVpnCustomerUsage(customerId, days);
        if (!isCurrent()) return;
        if (response.customer_id !== customerId || response.days !== days) throw new Error("usage context mismatch");
        setCustomer(response);
      } else {
        const page = await api.getVpnNodeUsage(days, { afterId: append ? cursor.current : 0 });
        if (!isCurrent()) return;
        setNodes((previous) => [...new Map([...(append ? previous : []), ...page].map((row) => [row.worker_id, row])).values()].sort((a, b) => a.worker_id - b.worker_id));
        cursor.current = page[page.length - 1]?.worker_id ?? cursor.current;
        setHasMore(page.length === 100);
      }
    } catch {
      if (isCurrent()) setError("Не удалось загрузить статистику. Повторите попытку.");
    } finally {
      if (isCurrent()) { request.current = null; setLoading(false); }
    }
  }

  useLayoutEffect(() => {
    request.current = null;
    cursor.current = 0;
    setNodes([]);
    setCustomer(null);
    setHasMore(false);
    void load();
    return () => { request.current = null; };
  }, [customerId, days]);

  const Heading = customerId === undefined ? "h2" : "h3";
  return (
    <section className={customerId === undefined ? "card full-span vpn-usage-panel" : "vpn-workspace-section vpn-usage-panel"} data-vpn-usage={customerId === undefined ? "nodes" : "customer"}>
      <div className="card-head">
        <div>
          <Heading>{customerId === undefined ? "Трафик нод" : "Трафик клиента"}</Heading>
          <p className="muted">Измеренный трафик, не скорость канала. Пропуски измерений не считаются нулём.</p>
        </div>
        <div className="actions">
          <label><span>Период статистики</span><select value={days} onChange={(event) => setDays(Number(event.target.value) as VpnUsageDays)}>
            <option value={1}>1 день</option><option value={7}>7 дней</option><option value={30}>30 дней</option>
          </select></label>
          <button type="button" className="ghost" disabled={loading} onClick={() => void load()}>Обновить статистику</button>
        </div>
      </div>
      {loading && <p role="status">Загружаем статистику…</p>}
      {error && <div role="alert"><p>{error}</p>{nodes.length || customer ? <p>Показаны предыдущие данные за выбранный период.</p> : null}<button type="button" className="ghost" disabled={loading} onClick={() => void load()}>Повторить загрузку статистики</button></div>}
      {customer && <div className="vpn-usage-totals">
        <p>Всего отправлено: <strong>{formatTrafficBytes(customer.upload_bytes)}</strong> · Получено: <strong>{formatTrafficBytes(customer.download_bytes)}</strong></p>
        <Coverage totals={customer} />
      </div>}
      {customerId === undefined ? <>
        <div className="simple-table vpn-usage-table"><table>
          <caption className="sr-only">Измеренный трафик и отдельные показатели нод</caption>
          <thead><tr><th scope="col">Нода</th><th scope="col">Отправлено</th><th scope="col">Получено</th><th scope="col">Покрытие измерений</th><th scope="col">Активных логических профилей</th><th scope="col">Занято профилей / лимит</th><th scope="col">Нагрузка CPU</th><th scope="col">RAM</th></tr></thead>
          <tbody>{nodes.map((row) => <tr key={row.worker_id}>
            <td data-label="Нода">{workers.find((worker) => worker.id === row.worker_id)?.name ?? `Нода #${row.worker_id}`}</td>
            <td data-label="Отправлено">{formatTrafficBytes(row.upload_bytes)}</td><td data-label="Получено">{formatTrafficBytes(row.download_bytes)}</td>
            <td data-label="Покрытие измерений"><Coverage totals={row} /></td>
            <td data-label="Активных логических профилей">{displayMetric(row.active_devices)}</td>
            <td data-label="Занято профилей / лимит">{displayMetric(row.occupied_profiles)} / {displayMetric(row.max_active_profiles)}</td>
            <td data-label="Нагрузка CPU">{displayMetric(row.cpu_load)}</td><td data-label="RAM">{row.ram_usage_percent === null ? displayMetric(null) : `${displayMetric(row.ram_usage_percent)} %`}</td>
          </tr>)}</tbody>
        </table></div>
        {!loading && !error && !nodes.length && <p className="empty">Нет данных о нодах.</p>}
        {hasMore && <button type="button" className="ghost" disabled={loading} onClick={() => void load(true)}>Показать ещё ноды</button>}
      </> : customer ? <>
        <div className="simple-table vpn-usage-table"><table>
          <caption className="sr-only">Трафик логических профилей клиента, включая все их страны</caption>
          <thead><tr><th scope="col">Логический профиль</th><th scope="col">Отправлено</th><th scope="col">Получено</th><th scope="col">Покрытие измерений</th></tr></thead>
          <tbody>{customer.devices.map((device) => <tr key={device.device_id}>
            <td data-label="Логический профиль">{device.display_name}</td><td data-label="Отправлено">{formatTrafficBytes(device.upload_bytes)}</td>
            <td data-label="Получено">{formatTrafficBytes(device.download_bytes)}</td><td data-label="Покрытие измерений"><Coverage totals={device} /></td>
          </tr>)}</tbody>
        </table></div>
        {!customer.devices.length && <p className="empty">Логических профилей пока нет.</p>}
      </> : null}
    </section>
  );
}
