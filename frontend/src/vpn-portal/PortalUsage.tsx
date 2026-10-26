import { formatTrafficBytes, formatUsageTime, usageCoverageLabel } from "../vpnUsage";
import type { PortalUsage as UsageReport, PortalUsageDays } from "./types";

export interface PortalUsageProps {
  days: PortalUsageDays;
  report: UsageReport | null;
  busy: boolean;
  error: boolean;
  onDaysChange: (days: PortalUsageDays) => void;
  onRetry: () => void;
  deviceId?: number;
}

export function PortalUsage({ days, report, busy, error, onDaysChange, onRetry, deviceId }: PortalUsageProps) {
  const currentReport = report?.days === days ? report : null;
  const totals = deviceId === undefined ? currentReport : currentReport?.devices.find((item) => item.device_id === deviceId);
  const total = totals?.upload_bytes != null && totals.download_bytes != null ? totals.upload_bytes + totals.download_bytes : null;
  return <div className={`cabinet-traffic ${deviceId === undefined ? "" : "cabinet-traffic--device"}`} aria-label={deviceId === undefined ? "Суммарный трафик аккаунта" : "Трафик устройства"}>
    <label className="cabinet-traffic-label">{deviceId === undefined ? "Трафик за " : "Трафик устройства за "}<select aria-label={deviceId === undefined ? "Период суммарного трафика аккаунта" : "Период трафика устройства"} value={days} onChange={(event) => onDaysChange(Number(event.target.value) as PortalUsageDays)}><option value={1}>24 часа</option><option value={7}>7 дней</option><option value={30}>30 дней</option></select></label>
    <strong className="cabinet-traffic-value">{total === null ? "—" : formatTrafficBytes(total)}</strong>
    {busy ? <p className="cabinet-traffic-hint" role="status">Получаем измерения…</p> : error ? <p className="cabinet-traffic-hint" role="alert">Не удалось загрузить трафик <button type="button" onClick={onRetry}>Повторить</button></p> : total === null ? <p className="cabinet-traffic-hint">{totals?.coverage === "unavailable" ? "Сбор данных пока недоступен" : "Данные появятся после измерения"}</p> : <details className="cabinet-traffic-hint"><summary>{usageCoverageLabel(totals?.coverage ?? "none")}</summary><p>{deviceId === undefined ? "Суммарно по аккаунту" : "Только это устройство"}</p><p>Отправлено: {formatTrafficBytes(totals?.upload_bytes)}<br />Получено: {formatTrafficBytes(totals?.download_bytes)}</p><p>Измеряем с: {formatUsageTime(totals?.measured_since ?? null)}<br />Последнее измерение: {formatUsageTime(totals?.last_sample_at ?? null)}</p></details>}
  </div>;
}
