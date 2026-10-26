import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
import type { PortalDeviceLocation, PortalDeviceLocations } from "./types";

interface DeviceLocationPickerProps {
  deviceId: number;
  credentialGeneration: number;
  csrfToken: string;
  sessionGeneration: SessionGeneration;
  onUnauthorized: () => void;
  onMutationStart: () => void;
  onReady: () => void;
  disabled: boolean;
  onStateChange?: (pending: boolean) => void;
}

const messages: Record<PortalDeviceLocations["selection_state"], string> = {
  ready: "После смены сервера обновите подписку в Happ или Hiddify и переподключитесь.",
  preparing: "Готовим подключение. Текущий сервер остаётся доступен.",
  failed: "Не удалось подготовить сервер. Выберите его снова, чтобы повторить попытку.",
  verification_required: "Результат подготовки проверяется. Можно выбрать другой доступный сервер.",
  unavailable: "Сейчас нет доступного подключения. Обновите статус позже.",
};

function locationLabel(location: PortalDeviceLocation): string {
  return location.country_code ? `${location.country_code} · ${location.name}` : location.name;
}

export default function DeviceLocationPicker(props: DeviceLocationPickerProps) {
  const [locations, setLocations] = useState<PortalDeviceLocations | null>(null);
  const [selected, setSelected] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [pollCount, setPollCount] = useState(0);
  const epoch = useRef(0);
  const selecting = useRef(false);
  const currentLocations = useRef<PortalDeviceLocations | null>(null);
  const latest = useRef(props);
  useLayoutEffect(() => { latest.current = props; });

  function accept(result: PortalDeviceLocations, afterSelection = false): void {
    const previous = currentLocations.current;
    currentLocations.current = result;
    setLocations(result);
    latest.current.onStateChange?.(["preparing", "verification_required"].includes(result.selection_state));
    setSelected((value) => {
      const choice = result.available.find((item) => String(item.endpoint_id) === value)
        ?? result.available.find((item) => item.endpoint_id === result.pending_endpoint_id)
        ?? result.available.find((item) => item.endpoint_id === result.current?.endpoint_id);
      return choice ? String(choice.endpoint_id) : "";
    });
    if (result.selection_state === "ready" && (afterSelection || (previous && previous.selection_state !== "ready"))) {
      latest.current.onReady();
    }
  }

  async function load(): Promise<void> {
    if (selecting.current) return;
    const requestEpoch = ++epoch.current;
    const generation = props.sessionGeneration.current();
    const isCurrent = () => epoch.current === requestEpoch && props.sessionGeneration.isCurrent(generation);
    setBusy(true);
    setError("");
    try {
      const result = await portalApi.deviceLocations(props.deviceId);
      if (isCurrent()) accept(result);
    } catch (cause) {
      if (!isCurrent()) return;
      if (cause instanceof PortalError && cause.status === 401) latest.current.onUnauthorized();
      else setError("Не удалось загрузить серверы. Обновите статус, чтобы повторить попытку.");
    } finally {
      if (isCurrent()) setBusy(false);
    }
  }

  useLayoutEffect(() => {
    epoch.current += 1;
    selecting.current = false;
    currentLocations.current = null;
    setLocations(null);
    setSelected("");
    setPollCount(0);
    void load();
    return () => { epoch.current += 1; selecting.current = false; };
    // Requests belong to this device and authenticated session, not a refreshed row object.
  }, [props.deviceId, props.credentialGeneration, props.csrfToken, props.sessionGeneration]);

  useEffect(() => {
    if (locations?.selection_state !== "preparing" || busy || error || props.disabled || pollCount >= 30) return;
    const timer = window.setTimeout(() => {
      setPollCount((count) => count + 1);
      void load();
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [locations, busy, error, props.disabled, pollCount]);

  async function select(): Promise<void> {
    const endpointId = Number(selected);
    if (props.disabled || selecting.current || !locations?.available.some((item) => item.endpoint_id === endpointId)) return;
    selecting.current = true;
    const requestEpoch = ++epoch.current;
    const generation = props.sessionGeneration.current();
    const isCurrent = () => epoch.current === requestEpoch && props.sessionGeneration.isCurrent(generation);
    latest.current.onMutationStart();
    latest.current.onStateChange?.(true);
    setBusy(true);
    setError("");
    setPollCount(0);
    try {
      const result = await portalApi.selectDeviceLocation(props.deviceId, endpointId, props.csrfToken);
      if (isCurrent()) accept(result, true);
    } catch (cause) {
      if (!isCurrent()) return;
      if (cause instanceof PortalError && cause.status === 401) latest.current.onUnauthorized();
      else setError("Не удалось выбрать сервер. Обновите статус и попробуйте ещё раз.");
    } finally {
      if (isCurrent()) { selecting.current = false; setBusy(false); }
    }
  }

  return (
    <form className="device-location-picker" onSubmit={(event) => { event.preventDefault(); void select(); }}>
      {locations?.current && <p className="hint">Текущий сервер: {locationLabel(locations.current)}</p>}
      <label htmlFor={`device-server-${props.deviceId}`}>Сервер</label>
      <select id={`device-server-${props.deviceId}`} value={selected} disabled={props.disabled || busy || !locations?.available.length} onChange={(event) => setSelected(event.target.value)}>
        <option value="">Выберите сервер</option>
        {locations?.available.map((location) => <option key={location.endpoint_id} value={location.endpoint_id}>{locationLabel(location)}</option>)}
      </select>
      <div className="button-row">
        <button type="submit" className="button button--primary" disabled={props.disabled || busy || !selected}>Выбрать сервер</button>
        <button type="button" className="button button--ghost" disabled={props.disabled || busy} onClick={() => void load()}>Обновить статус</button>
      </div>
      {busy && !locations && <p className="hint" role="status">Загружаем серверы…</p>}
      {locations && <p className="hint" role={locations.selection_state === "failed" ? "alert" : "status"}>{messages[locations.selection_state]}</p>}
      {locations?.selection_state === "preparing" && pollCount >= 30 && <p className="hint">Подготовка ещё не завершена. Обновите статус вручную.</p>}
      {error && <p className="message message--error" role="alert">{error}</p>}
    </form>
  );
}
