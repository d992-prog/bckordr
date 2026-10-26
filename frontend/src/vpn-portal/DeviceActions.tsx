import { useEffect, useRef, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
import { operationLabel, type DeviceIntent, type DeviceRequestState } from "./deviceOperations";
import type { PortalDevice, PortalDeviceLocation, PortalSubscription } from "./types";

export interface DeviceDraft { display_name: string; subscription_id: number; endpoint_id: number; }

export function OperationStatus({ request, onRetry, onRefresh }: { request: DeviceRequestState; onRetry: () => void; onRefresh: () => void }) {
  const operation = request.operation;
  return <div className="device-operation" aria-live="polite">
    {operation && <p>{operationLabel(operation)}</p>}
    {request.error && <p className="message message--error" role="alert">{request.error}</p>}
    {!request.definitive && <div className="button-row"><button type="button" className="button button--ghost" disabled={request.busy} onClick={onRefresh}>{request.busy ? "Проверяем…" : "Обновить статус"}</button>{(!operation || ["failed", "uncertain"].includes(operation.stage)) && <button type="button" className="button button--ghost" disabled={request.busy} onClick={onRetry}>Повторить запрос</button>}</div>}
  </div>;
}

export function DeviceActions({ device, request, requestKey, disabled, onSubmit, onRefresh }: { device: PortalDevice; request: DeviceRequestState | null; requestKey: string; disabled: boolean; onSubmit: (intent: DeviceIntent, key?: string) => void; onRefresh: (key: string) => void }) {
  const [confirm, setConfirm] = useState<"revoke" | "replace" | null>(null);
  const pending = Boolean(request && !request.definitive);
  useEffect(() => { setConfirm(null); }, [device.id, device.credential_generation, request?.requestId]);
  return <div className="device-actions">
    {request && <OperationStatus request={request} onRetry={() => onSubmit(request.intent, requestKey)} onRefresh={() => onRefresh(requestKey)} />}
    {!confirm ? <div className="button-row"><button type="button" className="button button--ghost" disabled={disabled || pending || device.state === "revoked"} onClick={() => setConfirm("replace")}>Заменить доступ</button><button type="button" className="button button--quiet" disabled={disabled || pending || device.state === "revoked"} onClick={() => setConfirm("revoke")}>Отключить устройство</button></div> : <form className="device-confirm" aria-label={`${confirm === "revoke" ? "Отключение" : "Замена доступа"}: ${device.display_name}`} onSubmit={(event) => { event.preventDefault(); onSubmit({action:confirm,deviceId:device.id}); }}>
      <h4>{confirm === "revoke" ? "Отключить" : "Заменить доступ для"} «{device.display_name}»</h4>
      <p>{confirm === "revoke" ? "Доступ этого устройства отключится во всех странах. Место освободится после подтверждения отключения" : "Прежняя ссылка перестанет работать. После замены подключите это устройство заново. Другие устройства сохранят свой доступ"}</p>
      <div className="button-row"><button type="submit" className={`button ${confirm === "revoke" ? "button--quiet" : "button--primary"}`} disabled={disabled || pending}>{confirm === "revoke" ? "Подтвердить отключение" : "Подтвердить замену"}</button><button type="button" className="button button--ghost" onClick={() => setConfirm(null)}>Отмена</button></div>
    </form>}
  </div>;
}

export function DeviceCreateForm({ draft, onDraft, subscriptions, request, sessionGeneration, onUnauthorized, onSubmit, onRefresh, onClose }: { draft: DeviceDraft; onDraft: (draft: DeviceDraft) => void; subscriptions: PortalSubscription[]; request: DeviceRequestState | null; sessionGeneration: SessionGeneration; onUnauthorized: () => void; onSubmit: (intent: DeviceIntent) => void; onRefresh: (key: string) => void; onClose: () => void }) {
  const choices = subscriptions.filter((item) => ["active", "trial"].includes(item.state));
  const selected = choices.find((item) => item.id === draft.subscription_id) ?? (choices.length === 1 ? choices[0] : null);
  const [locations, setLocations] = useState<PortalDeviceLocation[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [retry, setRetry] = useState(0);
  const epoch = useRef(0);
  const pending = Boolean(request && !request.definitive);
  const full = !selected || selected.profiles_used >= selected.profile_limit;
  useEffect(() => {
    const generation = sessionGeneration.current(); const current = ++epoch.current;
    const isCurrent = () => epoch.current === current && sessionGeneration.isCurrent(generation);
    setLocations([]); setError("");
    if (!selected) { setBusy(false); return; }
    setBusy(true);
    void portalApi.creationLocations(selected.id).then((result) => { if (isCurrent()) setLocations(result); }).catch((cause) => {
      if (!isCurrent()) return;
      if (cause instanceof PortalError && cause.status === 401) onUnauthorized();
      else setError("Не удалось загрузить страны. Повторите попытку");
    }).finally(() => { if (isCurrent()) setBusy(false); });
    return () => { epoch.current += 1; };
    // The selected owned subscription and session define the request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected?.id, sessionGeneration, retry]);
  function submit(): void {
    const name = draft.display_name.trim();
    if (!name || name.length > 64) { setError("Название должно содержать от 1 до 64 символов"); return; }
    if (!selected || full || !locations.some((item) => item.endpoint_id === draft.endpoint_id)) return;
    onSubmit({action:"create",subscription_id:selected.id,endpoint_id:draft.endpoint_id,display_name:name});
  }
  return <section id="add-device" className="card device-create" aria-labelledby="create-device-title"><div className="card-row"><h2 id="create-device-title">Добавить устройство</h2><button type="button" className="button button--ghost" onClick={onClose}>Закрыть</button></div>
    {request && <OperationStatus request={request} onRetry={() => onSubmit(request.intent)} onRefresh={() => onRefresh("create")} />}
    <form onSubmit={(event) => { event.preventDefault(); submit(); }}>
      <label>Название устройства<input value={draft.display_name} maxLength={64} disabled={pending} onChange={(event) => onDraft({...draft,display_name:event.target.value})} required /></label>
      <label>Подписка<select value={selected?.id ?? ""} disabled={pending || choices.length === 0} onChange={(event) => onDraft({...draft,subscription_id:Number(event.target.value),endpoint_id:0})}><option value="">Выберите подписку</option>{choices.map((item) => <option key={item.id} value={item.id}>Veltrix VPN · {item.profiles_used} из {item.profile_limit} мест занято</option>)}</select></label>
      {selected ? <p className="hint">Занято мест: {selected.profiles_used} из {selected.profile_limit}{full ? ". Все места заняты" : ""}</p> : <p className="hint">{choices.length ? "Выберите действующую подписку" : "Нет действующей подписки для нового устройства"}</p>}
      <label>Страна<select value={locations.some((item) => item.endpoint_id === draft.endpoint_id) ? draft.endpoint_id : ""} disabled={pending || busy || !locations.length} onChange={(event) => onDraft({...draft,endpoint_id:Number(event.target.value)})}><option value="">{busy ? "Загружаем страны…" : "Выберите страну"}</option>{locations.map((item) => <option key={item.endpoint_id} value={item.endpoint_id}>{item.name}</option>)}</select></label>
      {!busy && selected && locations.length === 0 && !error && <p className="hint">Доступных стран пока нет. Обновите список позже</p>}
      {error && <p className="message message--error" role="alert">{error}</p>}
      <div className="button-row"><button type="submit" className="button button--primary" disabled={pending || busy || full || !draft.endpoint_id || !draft.display_name.trim()}>Добавить устройство</button><button type="button" className="button button--ghost" disabled={pending || busy || !selected} onClick={() => setRetry((value) => value + 1)}>Обновить страны</button></div>
    </form>
  </section>;
}
