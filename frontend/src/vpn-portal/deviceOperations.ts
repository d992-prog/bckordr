// @ts-expect-error Node's strip-types runner requires the source extension.
import { portalApi, PortalError } from "./api.ts";
// @ts-expect-error Node's strip-types runner requires the source extension.
import { DeviceOperationJournal } from "./deviceOperationJournal.ts";
import type { SessionGeneration } from "./bootstrap";
import type { PortalDevice, PortalDeviceOperation } from "./types";

export type DeviceIntent = { action: "create"; subscription_id: number; endpoint_id: number; display_name: string }
  | { action: "revoke" | "replace"; deviceId: number };
export interface DeviceRequestState {
  intent: DeviceIntent;
  requestId: string;
  operation: PortalDeviceOperation | null;
  busy: boolean;
  definitive: boolean;
  error: string;
}

export function operationRequestId(source: Pick<Crypto, "getRandomValues"> & Partial<Pick<Crypto, "randomUUID">> = crypto): string {
  if (source.randomUUID) return source.randomUUID();
  const bytes = source.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
}

export function operationFinished(operation: PortalDeviceOperation): boolean {
  return operation.stage === "completed" || (operation.stage === "failed" && operation.finished_at !== null);
}

export function deviceConnectionBlocked(device: PortalDevice, requests: Record<string, DeviceRequestState>): boolean {
  if (device.operation && !operationFinished(device.operation)) return true;
  return Object.values(requests).some((entry) => {
    const id = entry.intent.action === "create" ? entry.operation?.device_id : entry.intent.deviceId;
    if (id !== device.id) return false;
    if (!entry.definitive || entry.busy) return true;
    const operation = entry.operation;
    if (operation?.stage !== "completed") return false;
    // A completed POST may arrive before the refreshed device row. Never reuse
    // that row's previous credentials while the canonical generation catches up.
    return device.credential_generation < operation.credential_generation
      || (operation.action === "revoke" && device.state !== "revoked");
  });
}

export function operationKey(intent: DeviceIntent): string { return intent.action === "create" ? "create" : `${intent.action}:${intent.deviceId}`; }

export function operationLabel(operation: PortalDeviceOperation): string {
  if (operation.stage === "completed") return operation.action === "revoke" ? "Устройство отключено" : operation.action === "replace" ? "Доступ заменён. Подключите устройство заново" : "Устройство готово";
  if (operation.stage === "uncertain") return "Результат проверяется. Действие ещё не завершено";
  if (operation.stage === "failed") return operation.finished_at === null ? "Действие не завершено. Повторите попытку" : "Не удалось выполнить действие";
  return operation.stage === "revoking" ? "Отключаем прежний доступ во всех странах. Место пока занято" : "Подготавливаем подключение. Дождитесь завершения";
}

export class DeviceOperationRequests {
  entries: Record<string, DeviceRequestState> = {};
  private session: SessionGeneration;
  private changed: (entries: Record<string, DeviceRequestState>) => void;
  private unauthorized: () => void;
  private starting: (intent: DeviceIntent) => void;
  private epoch = 0;
  private journal: DeviceOperationJournal;

  constructor(session: SessionGeneration, changed: (entries: Record<string, DeviceRequestState>) => void, unauthorized: () => void, starting: (intent: DeviceIntent) => void, storage?: () => Pick<Storage, "getItem" | "setItem" | "removeItem">) {
    this.session = session; this.changed = changed; this.unauthorized = unauthorized; this.starting = starting;
    this.journal = new DeviceOperationJournal(storage);
  }
  async initialize(csrf: string): Promise<void> {
    const epoch = ++this.epoch; const generation = this.session.current();
    const current = () => this.epoch === epoch && this.session.isCurrent(generation);
    this.entries = {}; this.changed(this.entries);
    try {
      const records = await this.journal.bind(csrf,current);
      if (!current()) return;
      for (const record of records) this.put(record.key,{intent:record.intent,requestId:record.requestId,operation:null,busy:false,definitive:false,error:"Результат запроса неизвестен. Повторите проверку с теми же данными"});
    } catch { /* A bounded action error is shown on submit; reading and connecting still work. */ }
  }
  private put(key: string, entry: DeviceRequestState): void { this.entries = { ...this.entries, [key]: entry }; this.changed(this.entries); }
  clear(): void { this.epoch += 1; this.journal.clear(); this.entries = {}; this.changed(this.entries); }
  dismiss(key: string): void { if (this.entries[key]?.definitive) { const next = {...this.entries}; delete next[key]; this.entries = next; this.changed(next); } }
  pendingFor(id: number): boolean { return Object.values(this.entries).some((entry) => !entry.definitive && (entry.intent.action !== "create" ? entry.intent.deviceId === id : entry.operation?.device_id === id)); }
  restore(devices: PortalDevice[]): void {
    for (const device of devices) {
      const operation = device.operation;
      if (!operation) continue;
      const recovered = Object.entries(this.entries).find(([,entry])=>entry.requestId === operation.request_id);
      if (recovered) {
        const [key,entry] = recovered;
        if (entry.busy || entry.definitive || operation.action !== entry.intent.action || (entry.intent.action !== "create" && entry.intent.deviceId !== operation.device_id)) continue;
        this.put(key,{...entry,operation,definitive:operationFinished(operation),error:""});
        continue;
      }
      if (operationFinished(operation)) continue;
      const intent: DeviceIntent = operation.action === "create" ? {action:"create",subscription_id:device.subscription_id,endpoint_id:operation.endpoint_id ?? 0,display_name:device.display_name} : {action:operation.action,deviceId:device.id};
      const basicKey = operationKey(intent);
      const key = operation.action === "create" && this.entries[basicKey] && this.entries[basicKey].requestId !== operation.request_id ? `create:${device.id}` : basicKey;
      if (!this.entries[key]) this.put(key, {intent,requestId:operation.request_id,operation,busy:false,definitive:false,error:""});
    }
    this.journal.write(this.entries);
  }
  async submit(intent: DeviceIntent, csrf: string, key = operationKey(intent)): Promise<PortalDeviceOperation | null> {
    const previous = this.entries[key];
    if (previous?.busy) return null;
    if (intent.action !== "create" && Object.entries(this.entries).some(([otherKey, entry]) => otherKey !== key && !entry.definitive && (entry.intent.action !== "create" ? entry.intent.deviceId === intent.deviceId : entry.operation?.device_id === intent.deviceId))) return null;
    if (previous && !previous.definitive && JSON.stringify(previous.intent) !== JSON.stringify(intent)) {
      this.put(key, {...previous,error:"Сначала проверьте результат предыдущего запроса"}); return null;
    }
    const entry: DeviceRequestState = previous && !previous.definitive ? {...previous,busy:true,error:""} : {intent,requestId:operationRequestId(),operation:null,busy:true,definitive:false,error:""};
    if (!this.journal.write({...this.entries,[key]:entry})) {
      this.put(key,{...entry,busy:false,definitive:previous?.definitive ?? !previous,error:"Не удалось сохранить запрос на этом устройстве. Войдите заново и повторите попытку"});
      return null;
    }
    this.put(key,entry);
    this.starting(intent);
    return this.request(key,entry,()=>intent.action === "create"
      ? portalApi.createDevice({subscription_id:intent.subscription_id,endpoint_id:intent.endpoint_id,display_name:intent.display_name,request_id:entry.requestId},csrf)
      : intent.action === "revoke" ? portalApi.revokeDevice(intent.deviceId,entry.requestId,csrf) : portalApi.replaceDevice(intent.deviceId,entry.requestId,csrf), Boolean(previous && !previous.definitive));
  }
  async refresh(key: string): Promise<PortalDeviceOperation | null> {
    const entry = this.entries[key];
    if (!entry || entry.busy || !entry.operation) return null;
    this.put(key,{...entry,busy:true,error:""});
    return this.request(key,entry,()=>portalApi.deviceOperation(entry.operation!.id));
  }
  private async request(key: string, entry: DeviceRequestState, load: () => Promise<PortalDeviceOperation>, unresolvedReplay = false): Promise<PortalDeviceOperation | null> {
    const generation = this.session.current(); const epoch = this.epoch;
    const isCurrent = () => this.session.isCurrent(generation) && this.epoch === epoch && this.entries[key]?.requestId === entry.requestId;
    try {
      const operation = await load();
      if (!isCurrent()) return null;
      if (operation.request_id !== entry.requestId || operation.action !== entry.intent.action || (entry.intent.action !== "create" && operation.device_id !== entry.intent.deviceId)) throw new PortalError(0,"Не удалось проверить результат действия");
      this.put(key,{...entry,operation,busy:false,definitive:operationFinished(operation),error:""});
      this.journal.write(this.entries);
      return operation;
    } catch (error) {
      if (!isCurrent()) return null;
      if (error instanceof PortalError && error.status === 401) { this.unauthorized(); return null; }
      // A rejected replay cannot settle an earlier submission whose response was lost.
      const definitive = !unresolvedReplay && !entry.operation && error instanceof PortalError && error.status >= 400 && error.status < 500;
      this.put(key,{...entry,busy:false,definitive,error:error instanceof PortalError && error.status !== 0 ? error.message : "Результат запроса неизвестен. Повторите проверку с теми же данными"});
      this.journal.write(this.entries);
      return null;
    }
  }
}
