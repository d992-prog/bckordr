import { FormEvent, useEffect, useState } from "react";

import { api, VpnEndpointCapacity } from "./api";
import { validateVpnEndpointCapacity } from "./vpnEndpointCapacity";

type Props = {
  endpoints: VpnEndpointCapacity[];
  onUpdated: (endpoint: VpnEndpointCapacity) => void;
};

function CapacityRow({ endpoint, onUpdated }: { endpoint: VpnEndpointCapacity } & Pick<Props, "onUpdated">) {
  const [maximum, setMaximum] = useState(endpoint.max_active_profiles?.toString() ?? "");
  const [warning, setWarning] = useState(endpoint.capacity_warning_percent.toString());
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setMaximum(endpoint.max_active_profiles?.toString() ?? "");
    setWarning(endpoint.capacity_warning_percent.toString());
  }, [endpoint.endpoint_id, endpoint.max_active_profiles, endpoint.capacity_warning_percent]);

  const percentage = endpoint.max_active_profiles === null
    ? null
    : Math.round((endpoint.occupied_profiles / endpoint.max_active_profiles) * 100);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setMessage(null);
    try {
      const payload = validateVpnEndpointCapacity(maximum, warning);
      setSaving(true);
      const updated = await api.updateVpnEndpointCapacity(endpoint.endpoint_id, payload);
      onUpdated(updated);
      setMessage("Лимиты сохранены.");
    } catch (caught) {
      setError((caught instanceof Error ? caught.message : "Не удалось сохранить лимиты.").slice(0, 160));
    } finally {
      setSaving(false);
    }
  }

  return (
    <article className="vpn-capacity-card">
      <div className="vpn-capacity-head">
        <div>
          <strong>{endpoint.label}</strong>
          <div className="row-hint">Endpoint #{endpoint.endpoint_id} · воркер #{endpoint.worker_id}</div>
        </div>
        <span className={`status ${endpoint.status === "ready" ? "available" : "info"}`}>
          {endpoint.status}
        </span>
      </div>
      <div className="vpn-capacity-usage">
        <span>Занято профилей</span>
        <strong>
          {endpoint.occupied_profiles} / {endpoint.max_active_profiles ?? "лимит не задан"}
        </strong>
        <small>{percentage === null ? "Лимит не задан" : `${percentage}% занято`}</small>
      </div>
      <form className="vpn-capacity-form" onSubmit={submit}>
        <div className="vpn-capacity-fields">
          <label>
            <span>Максимум активных профилей</span>
            <input
              type="number"
              min="1"
              max="100000"
              step="1"
              value={maximum}
              onChange={(event) => setMaximum(event.target.value)}
              placeholder="Пусто — лимит не задан"
              aria-label={`Лимит профилей для ${endpoint.label}`}
              disabled={saving}
            />
          </label>
          <label>
            <span>Предупреждать при, %</span>
            <input
              type="number"
              min="1"
              max="100"
              step="1"
              value={warning}
              onChange={(event) => setWarning(event.target.value)}
              aria-label={`Порог предупреждения для ${endpoint.label}`}
              disabled={saving}
              required
            />
          </label>
        </div>
        <button type="submit" disabled={saving}>
          {saving ? "Сохраняем…" : "Сохранить лимиты"}
        </button>
        <div className="vpn-capacity-message" aria-live="polite">
          {error ? <span className="error-text">{error}</span> : message}
        </div>
      </form>
    </article>
  );
}

export function VpnEndpointCapacityPanel({ endpoints, onUpdated }: Props) {
  if (endpoints.length === 0) {
    return <p className="empty">VPN endpoint’ы пока не зарегистрированы.</p>;
  }
  return (
    <div className="vpn-capacity-grid">
      {endpoints.map((endpoint) => (
        <CapacityRow key={endpoint.endpoint_id} endpoint={endpoint} onUpdated={onUpdated} />
      ))}
    </div>
  );
}
