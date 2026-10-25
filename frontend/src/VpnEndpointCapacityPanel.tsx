import { FormEvent, useEffect, useRef, useState } from "react";

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
  const [publicName, setPublicName] = useState(endpoint.public_name ?? "");
  const [countryCode, setCountryCode] = useState(endpoint.country_code ?? "");
  const [published, setPublished] = useState(endpoint.is_published);
  const [publishing, setPublishing] = useState(false);
  const [publicationError, setPublicationError] = useState<string | null>(null);
  const [publicationMessage, setPublicationMessage] = useState<string | null>(null);
  const requestRef = useRef<object | null>(null);

  useEffect(() => {
    requestRef.current = null;
    setSaving(false);
    setPublishing(false);
    return () => { requestRef.current = null; };
  }, [endpoint]);

  useEffect(() => {
    setMaximum(endpoint.max_active_profiles?.toString() ?? "");
    setWarning(endpoint.capacity_warning_percent.toString());
  }, [endpoint.endpoint_id, endpoint.worker_id, endpoint.max_active_profiles, endpoint.capacity_warning_percent]);

  useEffect(() => {
    setPublicName(endpoint.public_name ?? "");
    setCountryCode(endpoint.country_code ?? "");
    setPublished(endpoint.is_published);
  }, [endpoint.endpoint_id, endpoint.worker_id, endpoint.public_name, endpoint.country_code, endpoint.is_published]);

  const percentage = endpoint.max_active_profiles === null
    ? null
    : Math.round((endpoint.occupied_profiles / endpoint.max_active_profiles) * 100);

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (requestRef.current) return;
    const request = {};
    requestRef.current = request;
    setError(null);
    setMessage(null);
    try {
      const payload = validateVpnEndpointCapacity(maximum, warning);
      setSaving(true);
      const updated = await api.updateVpnEndpointCapacity(endpoint.endpoint_id, payload);
      if (requestRef.current !== request) return;
      onUpdated(updated);
      setMessage("Лимиты сохранены.");
    } catch (caught) {
      if (requestRef.current !== request) return;
      setError((caught instanceof Error ? caught.message : "Не удалось сохранить лимиты.").slice(0, 160));
    } finally {
      if (requestRef.current === request) {
        requestRef.current = null;
        setSaving(false);
      }
    }
  }

  async function submitPublication(event: FormEvent) {
    event.preventDefault();
    if (requestRef.current) return;
    const request = {};
    requestRef.current = request;
    setPublicationError(null);
    setPublicationMessage(null);
    try {
      const name = publicName.trim();
      const country = countryCode.trim().toUpperCase();
      if (!name || [...name].length > 64 || /[\p{Cc}\p{Cf}\p{Cs}]/u.test(name)) {
        throw new Error("Введите название для клиентов: от 1 до 64 символов без управляющих знаков.");
      }
      if (!/^[A-Z]{2}$/.test(country)) throw new Error("Код страны — две латинские буквы, например DE.");
      setPublishing(true);
      const updated = await api.updateVpnEndpointPublication(endpoint.endpoint_id, {
        is_published: published, public_name: name, country_code: country,
      });
      if (requestRef.current !== request) return;
      setPublicName(updated.public_name ?? "");
      setCountryCode(updated.country_code ?? "");
      setPublished(updated.is_published);
      onUpdated(updated);
      setPublicationMessage("Публикация сохранена.");
    } catch (caught) {
      if (requestRef.current !== request) return;
      setPublicationError((caught instanceof Error ? caught.message : "Не удалось сохранить публикацию. Попробуйте ещё раз.").slice(0, 160));
    } finally {
      if (requestRef.current === request) {
        requestRef.current = null;
        setPublishing(false);
      }
    }
  }

  return (
    <article className="vpn-capacity-card vpn-admin-panel">
      <div className="vpn-capacity-head">
        <div>
          <strong>{endpoint.label}</strong>
          <div className="row-hint">Точка подключения #{endpoint.endpoint_id} · VPN‑нода #{endpoint.worker_id}</div>
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
              disabled={saving || publishing}
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
              disabled={saving || publishing}
              required
            />
          </label>
        </div>
        <button type="submit" disabled={saving || publishing}>
          {saving ? "Сохраняем…" : "Сохранить лимиты"}
        </button>
        <div className="vpn-capacity-message">
          {error ? <span className="error-text" role="alert">{error}</span> : null}
          {message ? <span role="status">{message}</span> : null}
        </div>
      </form>
      <form className="vpn-capacity-form" onSubmit={submitPublication}>
        <div className="vpn-capacity-fields">
          <label>
            <span>Название для клиентов</span>
            <input value={publicName} onChange={(event) => setPublicName(event.target.value)}
              maxLength={64} required disabled={publishing || saving} />
          </label>
          <label>
            <span>Код страны</span>
            <input value={countryCode} onChange={(event) => setCountryCode(event.target.value)}
              maxLength={2} required disabled={publishing || saving} autoCapitalize="characters" />
          </label>
        </div>
        <label className="checkbox">
          <input type="checkbox" checked={published} onChange={(event) => setPublished(event.target.checked)}
            disabled={publishing || saving} />
          <span>Показывать клиентам</span>
        </label>
        <p className="muted">Скрытие убирает сервер из нового выбора. Выданные подключения продолжат работать.</p>
        <button type="submit" disabled={publishing || saving}>
          {publishing ? "Сохраняем публикацию…" : "Сохранить публикацию"}
        </button>
        <div className="vpn-capacity-message">
          {publicationError ? <span className="error-text" role="alert">{publicationError}</span> : null}
          {publicationMessage ? <span role="status">{publicationMessage}</span> : null}
        </div>
      </form>
    </article>
  );
}

export function VpnEndpointCapacityPanel({ endpoints, onUpdated }: Props) {
  if (endpoints.length === 0) {
    return <p className="empty">Точки подключения пока не зарегистрированы.</p>;
  }
  return (
    <div className="vpn-capacity-grid">
      {endpoints.map((endpoint) => (
        <CapacityRow key={endpoint.endpoint_id} endpoint={endpoint} onUpdated={onUpdated} />
      ))}
    </div>
  );
}
