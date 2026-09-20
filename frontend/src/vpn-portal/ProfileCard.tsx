import { useEffect, useRef, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
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
  const [connectionMessage, setConnectionMessage] = useState("");
  const [isRenaming, setIsRenaming] = useState(false);
  const [renameValue, setRenameValue] = useState(profile.display_name);
  const [renameBusy, setRenameBusy] = useState(false);
  const [renameError, setRenameError] = useState("");
  const connectionEpoch = useRef(0);
  const renameEpoch = useRef(0);

  useEffect(() => {
    setRenameValue(profile.display_name);
  }, [profile.display_name]);

  useEffect(() => {
    connectionEpoch.current += 1;
    setConnectionUri(null);
    setConnectionBusy(false);
    setConnectionMessage("");
  }, [connectionVersion]);

  const canReveal = profile.state === "active" && profile.can_connect;
  const unavailableHint = profile.state === "active" && !profile.can_connect
    ? "Подключение для этого профиля пока недоступно."
    : profile.state !== "active"
      ? "Неактивный профиль можно переименовать, но подключение недоступно."
      : "";

  async function revealConnection(): Promise<void> {
    if (!canReveal || renameBusy || renamePending) {
      return;
    }
    const generation = sessionGeneration.current();
    const epoch = ++connectionEpoch.current;
    setConnectionBusy(true);
    setConnectionMessage("");
    try {
      const connection = await portalApi.connection(profile.id);
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      setConnectionUri(connection.uri);
    } catch (error) {
      if (!sessionGeneration.isCurrent(generation) || connectionEpoch.current !== epoch) {
        return;
      }
      if (error instanceof PortalError && error.status === 401) {
        onUnauthorized();
        return;
      }
      setConnectionMessage(errorText(error));
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
    try {
      if (!navigator.clipboard?.writeText) {
        throw new Error("clipboard unavailable");
      }
      await navigator.clipboard.writeText(connectionUri);
      setConnectionMessage("Скопировано.");
    } catch {
      setConnectionMessage(
        "Не удалось скопировать автоматически. Выделите ссылку в поле и скопируйте вручную.",
      );
    }
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
    onRenameStart(profile.id);
    setConnectionBusy(false);
    setRenameBusy(true);
    setRenameError("");
    try {
      const updated = await portalApi.rename(profile.id, displayName, csrfToken);
      if (!sessionGeneration.isCurrent(generation) || renameEpoch.current !== epoch) {
        return;
      }
      setConnectionUri(null);
      setConnectionMessage("");
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
    setConnectionMessage("");
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
            <button className="button button--primary" disabled={renameBusy} onClick={saveName}>
              {renameBusy ? "Сохраняем…" : "Сохранить"}
            </button>
            <button className="button button--ghost" disabled={renameBusy} onClick={cancelRename}>
              Отмена
            </button>
          </div>
        </div>
      ) : (
        <button className="button button--ghost" disabled={renamePending} onClick={beginRename}>
          {renamePending ? "Переименование…" : "Переименовать"}
        </button>
      )}

      <div className="connection-box">
        {connectionUri === null ? (
          <button
            className="button button--primary"
            disabled={!canReveal || connectionBusy || renameBusy || renamePending}
            onClick={revealConnection}
          >
            {connectionBusy ? "Получаем ссылку…" : "Показать ссылку подключения"}
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
            <button className="button button--primary" onClick={copyConnection}>
              Скопировать
            </button>
          </>
        )}
        {connectionMessage && (
          <p className="message" role="status">{connectionMessage}</p>
        )}
      </div>
    </article>
  );
}
