import type { DeviceIntent, DeviceRequestState } from "./deviceOperations";

type JournalStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;
interface JournalRecord { key: string; requestId: string; intent: DeviceIntent; }
const storageKey = "veltrix.device-requests.v1";
const maxRecords = 32;
const maxLength = 32768;
const uuid = /^(?!00000000-0000-0000-0000-000000000000$)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const id = (value: unknown): value is number => typeof value === "number" && Number.isInteger(value) && value > 0 && value <= 2147483647;
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const keys = (value: Record<string, unknown>, expected: string[]) => Object.keys(value).sort().join(",") === [...expected].sort().join(",");

function validRecord(value: unknown): value is JournalRecord {
  if (!object(value) || !keys(value,["key","requestId","intent"]) || typeof value.key !== "string" || typeof value.requestId !== "string" || !uuid.test(value.requestId) || !object(value.intent)) return false;
  const intent = value.intent;
  if (intent.action === "create") return keys(intent,["action","subscription_id","endpoint_id","display_name"])
    && id(intent.subscription_id) && id(intent.endpoint_id) && typeof intent.display_name === "string"
    && intent.display_name.length >= 1 && intent.display_name.length <= 64 && intent.display_name.trim() === intent.display_name && !/[\u0000-\u001f\u007f]/.test(intent.display_name)
    && (value.key === "create" || (/^create:[1-9][0-9]*$/.test(value.key) && id(Number(value.key.slice(7)))));
  return (intent.action === "revoke" || intent.action === "replace") && keys(intent,["action","deviceId"]) && id(intent.deviceId) && value.key === `${intent.action}:${intent.deviceId}`;
}

// Only an allowlisted request identity/intent is durable. No auth or VPN material.
export class DeviceOperationJournal {
  private scope: string | null = null;
  private ready = false;
  private storage: () => JournalStorage;
  constructor(storage: () => JournalStorage = () => sessionStorage) { this.storage = storage; }

  async bind(csrf: string, current: () => boolean): Promise<JournalRecord[]> {
    this.ready = false;
    const digest = await crypto.subtle.digest("SHA-256",new TextEncoder().encode(`veltrix-device-requests-v1:${csrf}`));
    if (!current()) return [];
    this.scope = Array.from(new Uint8Array(digest),byte=>byte.toString(16).padStart(2,"0")).join("");
    const raw = this.storage().getItem(storageKey);
    let records: JournalRecord[] = [];
    if (raw !== null) {
      if (raw.length > maxLength) throw new Error("Invalid request journal");
      const envelope: unknown = JSON.parse(raw);
      if (!object(envelope) || !keys(envelope,["version","scope","requests"]) || envelope.version !== 1 || typeof envelope.scope !== "string" || !/^[0-9a-f]{64}$/.test(envelope.scope)) throw new Error("Invalid request journal");
      if (envelope.scope !== this.scope) {
        this.storage().removeItem(storageKey);
        if (this.storage().getItem(storageKey) !== null) throw new Error("Request journal unavailable");
      } else {
        if (!Array.isArray(envelope.requests) || envelope.requests.length > maxRecords || !envelope.requests.every(validRecord)) throw new Error("Invalid request journal");
        records = envelope.requests;
        if (new Set(records.map(record=>record.key)).size !== records.length || new Set(records.map(record=>record.requestId)).size !== records.length) throw new Error("Invalid request journal");
      }
    }
    this.ready = true;
    return records;
  }

  write(entries: Record<string, DeviceRequestState>): boolean {
    if (!this.ready || !this.scope) return false;
    try {
      const records = Object.entries(entries).filter(([,entry])=>!entry.definitive).map(([key,entry])=>({key,requestId:entry.requestId,intent:entry.intent}));
      if (records.length > maxRecords || !records.every(validRecord)) throw new Error("Invalid request journal");
      const storage = this.storage();
      if (records.length === 0) {
        storage.removeItem(storageKey);
        if (storage.getItem(storageKey) !== null) throw new Error("Request journal unavailable");
      } else {
        const raw = JSON.stringify({version:1,scope:this.scope,requests:records});
        if (raw.length > maxLength) throw new Error("Invalid request journal");
        storage.setItem(storageKey,raw);
        if (storage.getItem(storageKey) !== raw) throw new Error("Request journal unavailable");
      }
      return true;
    } catch { this.ready = false; return false; }
  }

  clear(): void {
    this.ready = false; this.scope = null;
    try { this.storage().removeItem(storageKey); } catch { /* Storage may be disabled; mutations remain blocked. */ }
  }
}
