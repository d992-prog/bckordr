import { useEffect, useRef, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
import { copyConnectionUri, loadConnectionUri } from "./connection";
import { createQrDataUrl } from "./qr";
import type { PortalProfile } from "./types";
import { stateLabel } from "./view";

interface ProfileCardProps {
  profile: PortalProfile;
  subscriptionLabel: string;
  csrfToken: string;
  sessionGeneration: SessionGeneration;
  onProfileChange: (profile: PortalProfile) => void;
  onUnauthorized: () => void;
  connectionVersion: number;
  renamePending: boolean;
  onRenameStart: (profileId: number) => void;
  onRenameSettled: (profileId: number) => void;
}

type ConnectionMessage = { kind: "success" | "error"; text: string };

function errorText(error: unknown): string {
  return error instanceof PortalError
    ? error.message
    : "Не удалось выполнить запрос. Попробуйте ещё раз.";
}

export default function ProfileCard({
  profile,
  subscriptionLabel,
  csrfToken,
  sessionGeneration,
  onProfileChange,
  onUnauthorized,
  connectionVersion,
  renamePending,
  onRenameStart,
  onRenameSettled,
}: ProfileCardProps) {
  const [connectionUri, setConnectionUri] = useState<string | null>(null);
  const [connectionBusy, setConnectionBusy] = useState(false);
  const [connectionMessage, setConnectionMessage] = useState<ConnectionMessage | null>(null);
  const [qrDataUrl, setQrDataUrl] = useState<string | null>(null);
  const [qrBusy, setQrBusy] = useState(false);
  const [qrError, setQrError] = useState("");
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(profile.display_name);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState("");
  const connectionEpoch = useRef(0);
  const qrRequestInFlight = useRef(false);
  const renameEpoch = useRef(0);

  useEffect(() => {
    setRenameValue(profile.display_name);
  }, [profile.display_name]);

  useEffect(() => {
    connectionEpoch.current += 1;
    setConnectionUri(null);
    setConnectionBusy(false);
    setConnectionMessage(null);
    qrRequestInFlight.current = false;
    setQrDataUrl(null);
    setQrBusy(false);
    setQrError("");
  }, [connectionVersion]);

  const canReveal = profile.state === "active" && profile.can_connect;
  const unavailableHint = profile.state === "active" && !profile.can_connect
    ? "Ссылка пока недоступна. Попробуйте через несколько минут"
    : profile.state !== "active"
      ? "Профиль неактивен. Подключение недоступно"
      : "";

  async function revealConnection(): Promise<void> {
    if (!canReveal || renameBusy || renamePending) {
      return;
    }
    const generation = sessionGeneration.current();
    const epoch = ++connectionEpoch.current;
    setConnectionBusy(true);
    setConnectionMessage(null);
    qrRequestInFlight.current = false;
    setQrDataUrl(null);
    setQrBusy(false);
    setQrError("");
    try {
      const uri = await loadConnectionUri(profile.id);
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setConnectionUri(uri);
    } catch (error) {
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      if (error instanceof PortalError && error.status === 401) {
        onUnauthorized();
        return;
      }
      setConnectionMessage({ kind: "error", text: errorText(error) });
    } finally {
      if (sessionGeneration.isCurrent(generation) && connectionEpoch.current === epoch) {
        setConnectionBusy(false);
      }
    }
  }

  async function copyConnection(): Promise<void> {
    if (connectionUri === null) {
      return;
    }
    const generation = sessionGeneration.current();
    const epoch = connectionEpoch.current;
    try {
      await copyConnectionUri(connectionUri);
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setConnectionMessage({ kind: "success", text: "Ссылка скопирована" });
    } catch {
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setConnectionMessage({
        kind: "error",
        text: "Не удалось скопировать автоматически. Выделите ссылку в поле и скопируйте вручную.",
      });
    }
  }

  async function showQrCode(): Promise<void> {
    if (connectionUri === null || qrRequestInFlight.current) {
      return;
    }
    const generation = sessionGeneration.current();
    const epoch = connectionEpoch.current;
    qrRequestInFlight.current = true;
    setQrBusy(true);
    setQrError("");
    try {
      const dataUrl = await createQrDataUrl(connectionUri);
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setQrDataUrl(dataUrl);
    } catch {
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setQrError("Не удалось создать QR-код. Попробуйте ещё раз.");
    } finally {
      if (sessionGeneration.isCurrent(generation) && connectionEpoch.current === epoch) {
        qrRequestInFlight.current = false;
        setQrBusy(false);
      }
    }
  }

  function hideQrCode(): void {
    setQrDataUrl(null);
    setQrError("");
  }

  async function saveName(): Promise<void> {
    const displayName = renameValue.trim();
    if (displayName.length === 0) {
      setRenameError("Введите название профиля.");
      return;
    }
    const generation = sessionGeneration.current();
    const epoch = ++renameEpoch.current;
    connectionEpoch.current += 1;
    qrRequestInFlight.current = false;
    onRenameStart(profile.id);
    setConnectionBusy(false);
    setQrDataUrl(null);
    setQrBusy(false);
    setQrError("");
    setRenameBusy(true);
    setRenameError("");
    try {
      const updated = await portalApi.rename(profile.id, displayName, csrfToken);
      if (!sessionGeneration.isCurrent(generation) || renameEpoch.current !== epoch) {
        return;
      }
      setConnectionUri(null);
      setConnectionMessage(null);
      setIsRenaming(false);
      onProfileChange(updated);
    } catch (error) {
      if (!sessionGeneration.isCurrent(generation) || renameEpoch.current !== epoch) {
        return;
      }
      if (error instanceof PortalError && error.status === 401) {
        onUnauthorized();
        return;
      }
      setRenameError(errorText(error));
    } finally {
      if (sessionGeneration.isCurrent(generation)) {
        onRenameSettled(profile.id);
        if (renameEpoch.current === epoch) {
          setRenameBusy(false);
        }
      }
    }
  }

  function cancelRename(): void {
    setRenameValue(profile.display_name);
    setRenameError("");
    setIsRenaming(false);
  }

  function beginRename(): void {
    setRenameValue(profile.display_name);
    setRenameError("");
    setConnectionMessage(null);
    setIsRenaming(true);
  }

  return (
    <article className="profile-card">
      <div className="profile-card__heading">
        <div>
          <p className="eyebrow">Профиль</p>
          <h3>{profile.display_name}</h3>
          <p className="profile-card__subscription">{subscriptionLabel}</p>
        </div>
        <span className={`status status--${profile.can_connect ? "good" : "quiet"}`}>
          {stateLabel(profile.state)}
        </span>
      </div>

      {unavailableHint && <p className="hint">{unavailableHint}</p>}

      {isRenaming ? (
        <div className="rename-form">
          <label htmlFor={`profile-name-${profile.id}`}>Название профиля</label>
          <input
            id={`profile-name-${profile.id}`}
            value={renameValue}
            maxLength={64}
            disabled={renameBusy}
            onChange={(event) => setRenameValue(event.target.value)}
          />
          {renameError && <p className="message message--error" role="alert">{renameError}</p>}
          <div className="button-row">
            <button type="button" className="button button--primary" disabled={renameBusy} onClick={saveName}>
              {renameBusy ? "Сохраняем…" : "Сохранить"}
            </button>
            <button type="button" className="button button--ghost" disabled={renameBusy} onClick={cancelRename}>
              Отмена
            </button>
          </div>
        </div>
      ) : (
        <button type="button" className="button button--ghost" disabled={renamePending} onClick={beginRename}>
          {renamePending ? "Переименование…" : "Переименовать"}
        </button>
      )}

      <div className="connection-box">
        {connectionUri === null ? (
          <button
            type="button"
            className="button button--primary"
            disabled={!canReveal || connectionBusy || renameBusy || renamePending}
            onClick={revealConnection}
          >
            {connectionBusy ? "Получаем ссылку…" : "Показать ссылку"}
          </button>
        ) : (
          <>
            <label htmlFor={`connection-${profile.id}`}>Ссылка профиля</label>
            <textarea
              id={`connection-${profile.id}`}
              className="connection-uri"
              readOnly
              value={connectionUri}
              rows={4}
              onFocus={(event) => event.currentTarget.select()}
            />
            <div className="button-row">
              <button type="button" className="button button--primary" onClick={copyConnection}>
                Скопировать
              </button>
              {qrDataUrl === null ? (
                <button type="button" className="button button--ghost" disabled={qrBusy} onClick={showQrCode}>
                  {qrBusy ? "Создаём QR-код…" : "Показать QR-код"}
                </button>
              ) : (
                <button type="button" className="button button--ghost" onClick={hideQrCode}>
                  Скрыть QR-код
                </button>
              )}
            </div>
            {qrDataUrl !== null && (
              <img
                className="qr-code"
                src={qrDataUrl}
                alt={`QR-код для подключения профиля ${profile.display_name}`}
              />
            )}
            {qrError && <p className="message message--error" role="alert">{qrError}</p>}
          </>
        )}
        {connectionMessage && (
          <p
            className={`message ${connectionMessage.kind === "error" ? "message--error" : ""}`}
            role={connectionMessage.kind === "error" ? "alert" : "status"}
          >
            {connectionMessage.text}
          </p>
        )}
      </div>
    </article>
  );
}
