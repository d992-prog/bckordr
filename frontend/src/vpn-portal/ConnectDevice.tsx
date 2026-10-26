import { useLayoutEffect, useRef, useState } from "react";

import { PortalError } from "./api";
import type { SessionGeneration } from "./bootstrap";
import { buildAppImportUrl, CLIENT_PLATFORMS, detectClientPlatform, type ClientPlatform } from "./clientApps";
import { copyConnectionUri, loadConnectionUri } from "./connection";
import { PortalIcon } from "./PortalIcon";
import { createQrDataUrl } from "./qr";
import type { PortalDevice } from "./types";

interface ConnectDeviceProps {
  devices: PortalDevice[];
  selectedDeviceId: number | null;
  connectionVersion: number;
  disabled: boolean;
  sessionGeneration: SessionGeneration;
  onSelectDevice: (id: number) => void;
  onUnauthorized: () => void;
}

type ConnectionMessage = { kind: "success" | "error"; text: string };

export function ConnectDevice(props: ConnectDeviceProps) {
  const device = props.devices.find(({ id }) => id === props.selectedDeviceId) ?? props.devices[0] ?? null;
  const [platform, setPlatform] = useState<ClientPlatform>(() => detectClientPlatform(navigator.userAgent, navigator.maxTouchPoints));
  const [preparation, setPreparation] = useState<"preparing" | "ready" | "error" | "empty">("empty");
  const [message, setMessage] = useState<ConnectionMessage | null>(null);
  const [copyBusy, setCopyBusy] = useState(false);
  const [qrBusy, setQrBusy] = useState(false);
  const [qr, setQr] = useState<string | null>(null);
  const [manual, setManual] = useState(false);
  const epoch = useRef(0);
  const busy = useRef(false);
  const prepared = useRef<{ scope: string; uri: string } | null>(null);
  const client = CLIENT_PLATFORMS.find(({ id }) => id === platform)!;
  const generation = props.sessionGeneration.current();
  const scope = `${generation}:${device?.id}:${device?.credential_generation}:${props.connectionVersion}`;
  const currentScope = useRef(scope);
  const canConnect = Boolean(!props.disabled && device?.state === "active" && device.can_connect && device.operation?.finished_at !== null);

  async function prepare(): Promise<void> {
    if (!device || !canConnect) return;
    const requestEpoch = ++epoch.current;
    const requestScope = scope;
    const isCurrent = () => props.sessionGeneration.isCurrent(generation) && epoch.current === requestEpoch && currentScope.current === requestScope;
    prepared.current = null;
    setPreparation("preparing");
    setMessage(null);
    try {
      const uri = await loadConnectionUri(device.id);
      if (!isCurrent()) return;
      prepared.current = { scope: requestScope, uri };
      setPreparation("ready");
    } catch (error) {
      if (!isCurrent()) return;
      if (error instanceof PortalError && error.status === 401) { props.onUnauthorized(); return; }
      setPreparation("error");
      setMessage({ kind: "error", text: "Не удалось подготовить ссылку. Повторите попытку" });
    }
  }

  useLayoutEffect(() => {
    currentScope.current = scope;
    epoch.current += 1;
    prepared.current = null;
    busy.current = false;
    setCopyBusy(false);
    setQrBusy(false);
    setQr(null);
    setManual(false);
    setMessage(null);
    setPreparation(canConnect ? "preparing" : "empty");
    if (canConnect) void prepare();
    return () => { epoch.current += 1; prepared.current = null; };
    // Fetch before the tap so app import and iOS copy stay inside the user gesture.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [scope, canConnect]);

  function readyUri(): string | null {
    return props.sessionGeneration.isCurrent(generation) && prepared.current?.scope === scope ? prepared.current.uri : null;
  }

  function openApp(): void {
    const uri = readyUri();
    if (!uri || !device) return;
    try { window.location.assign(buildAppImportUrl(platform, uri, `Veltrix VPN · ${device.display_name}`)); }
    catch { setMessage({ kind: "error", text: "Не удалось открыть приложение. Скопируйте ссылку или используйте QR-код" }); }
  }

  async function copy(): Promise<void> {
    const uri = readyUri();
    if (!uri || busy.current) return;
    busy.current = true;
    const requestEpoch = epoch.current;
    const isCurrent = () => props.sessionGeneration.isCurrent(generation) && epoch.current === requestEpoch;
    setCopyBusy(true);
    setMessage(null);
    try {
      await copyConnectionUri(uri);
      if (isCurrent()) setMessage({ kind: "success", text: "Ссылка скопирована" });
    } catch {
      if (isCurrent()) setMessage({ kind: "error", text: "Не удалось скопировать автоматически. Покажите ссылку и скопируйте вручную" });
    } finally {
      if (isCurrent()) { busy.current = false; setCopyBusy(false); }
    }
  }

  async function showQr(): Promise<void> {
    const uri = readyUri();
    if (!uri || busy.current) return;
    if (qr) { setQr(null); return; }
    busy.current = true;
    const requestEpoch = epoch.current;
    const isCurrent = () => props.sessionGeneration.isCurrent(generation) && epoch.current === requestEpoch;
    setQrBusy(true);
    setMessage(null);
    try {
      const image = await createQrDataUrl(uri);
      if (isCurrent()) setQr(image);
    } catch {
      if (isCurrent()) setMessage({ kind: "error", text: "Не удалось создать QR-код. Повторите попытку" });
    } finally {
      if (isCurrent()) { busy.current = false; setQrBusy(false); }
    }
  }

  return (
    <section id="connect" className="cabinet-connect vx-matte" aria-labelledby="connect-title">
      <h2 id="connect-title">Подключить VPN</h2>
      {props.devices.length > 1 && <label className="cabinet-device-choice">Устройство<select value={device?.id ?? ""} onChange={(event) => props.onSelectDevice(Number(event.target.value))}>{props.devices.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label>}
      <div className="cabinet-platforms" role="group" aria-label="Выберите платформу">
        {CLIENT_PLATFORMS.slice(0, 2).map((item) => <button key={item.id} type="button" aria-pressed={platform === item.id} onClick={() => setPlatform(item.id)}><PortalIcon name={item.id as "iphone" | "android"} /><span>{item.label}</span></button>)}
        <label className={`cabinet-desktop-platform ${["windows", "macos", "linux"].includes(platform) ? "is-selected" : ""}`}><PortalIcon name="windows" /><select aria-label="Windows, macOS или Linux" value={["windows", "macos", "linux"].includes(platform) ? platform : ""} onChange={(event) => setPlatform(event.target.value as ClientPlatform)}><option value="" hidden>Windows</option>{CLIENT_PLATFORMS.slice(2).map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select><PortalIcon name="chevron" className="cabinet-select-chevron" /></label>
      </div>
      <a className="cabinet-app-row" href={client.download} target="_blank" rel="noreferrer">
        <span className="cabinet-app-icon"><PortalIcon name={platform === "iphone" || platform === "android" ? "iphone" : "devices"} /></span>
        <span><strong>{client.app}</strong><span>{client.detail}</span></span><PortalIcon name="chevron" />
      </a>
      <button type="button" className="cabinet-import button button--primary" disabled={preparation === "preparing" || preparation === "empty"} onClick={preparation === "error" ? () => void prepare() : openApp}>
        {preparation === "preparing" ? "Готовим ссылку…" : preparation === "error" ? "Повторить подготовку" : `Открыть в ${client.app}`}<PortalIcon name="arrow" />
      </button>
      <div className="cabinet-connection-actions">
        <button type="button" className="button button--ghost" disabled={preparation !== "ready" || copyBusy} onClick={() => void copy()}><PortalIcon name="link" />{copyBusy ? "Копируем…" : "Скопировать"}</button>
        <button type="button" className="button button--ghost" disabled={preparation !== "ready" || qrBusy} onClick={() => void showQr()}><PortalIcon name="qr" />{qrBusy ? "Создаём…" : qr ? "Скрыть QR-код" : "QR-код"}</button>
      </div>
      <a className="cabinet-download" href={client.download} target="_blank" rel="noreferrer">Скачать {client.app}</a>
      {preparation === "empty" && <p className="hint">{props.disabled ? <>Проверяем готовность устройства. Если действие ещё выполняется, <a href="#devices">проверьте его статус</a></> : "Для подключения нужно готовое устройство с действующим доступом"}</p>}
      {message && <p className={`message ${message.kind === "error" ? "message--error" : ""}`} role={message.kind === "error" ? "alert" : "status"}>{message.text}</p>}
      {message?.kind === "error" && readyUri() && <button type="button" className="button button--ghost" onClick={() => setManual((value) => !value)}>{manual ? "Скрыть ссылку" : "Показать ссылку вручную"}</button>}
      {manual && readyUri() && <label className="cabinet-manual">Ссылка устройства<textarea readOnly value={readyUri() ?? ""} rows={3} onFocus={(event) => event.currentTarget.select()} /></label>}
      {qr && <img className="qr-code" src={qr} alt={`QR-код для подключения устройства ${device?.display_name}`} />}
    </section>
  );
}
