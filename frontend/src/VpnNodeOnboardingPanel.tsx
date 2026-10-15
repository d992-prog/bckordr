import { useEffect, useState } from "react";

import { api, type VpnNodeOnboarding } from "./api";
import {
  copyVpnNodeTestProfile,
  isVpnNodeFingerprintConfirmed,
  onboardingErrorMessage,
  onboardingPresentation,
  reconcileVpnNodeTestProfileUri,
  vpnNodeOnboardingSetupAction,
  type VpnNodeOnboardingWorker,
} from "./vpnNodeOnboarding";

type Props = {
  worker: VpnNodeOnboardingWorker & { id: number };
  status: VpnNodeOnboarding | null;
  loadError?: string | null;
  onStatusChange: (status: VpnNodeOnboarding) => void;
  onRetry: () => Promise<void>;
  onEnableVpnNode: () => void;
  onConfigureSsh: () => void;
};

export function VpnNodeOnboardingPanel({
  worker,
  status: loadedStatus,
  loadError = null,
  onStatusChange,
  onRetry,
  onEnableVpnNode,
  onConfigureSsh,
}: Props) {
  const [status, setStatus] = useState(loadedStatus);
  const [busy, setBusy] = useState(false);
  const [confirmedFingerprint, setConfirmedFingerprint] = useState<string | null>(null);
  const [testConfirmed, setTestConfirmed] = useState(false);
  const [testProfileUri, setTestProfileUri] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const setupAction = vpnNodeOnboardingSetupAction(worker);

  useEffect(() => {
    setStatus(loadedStatus);
    setConfirmedFingerprint((current) => (
      loadedStatus?.state === "needs_trust" && current === loadedStatus.fingerprint
        ? current
        : null
    ));
    setTestProfileUri((current) => (
      setupAction === null && loadedStatus
        ? reconcileVpnNodeTestProfileUri(current, loadedStatus.state)
        : null
    ));
    if (loadedStatus?.state !== "awaiting_test" || setupAction !== null) {
      setTestConfirmed(false);
    }
  }, [loadedStatus, setupAction]);

  const publishStatus = (next: VpnNodeOnboarding) => {
    setStatus(next);
    onStatusChange(next);
  };

  const run = async (action: () => Promise<VpnNodeOnboarding>) => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      publishStatus(await action());
    } catch (caught) {
      setError(onboardingErrorMessage(caught instanceof Error ? caught.message : null));
    } finally {
      setBusy(false);
    }
  };

  const loadTestProfile = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      const profile = await api.getVpnNodeTestProfile(worker.id);
      setTestProfileUri(profile.config_uri);
      setMessage(
        await copyVpnNodeTestProfile(profile.config_uri)
          ? "Временный профиль скопирован. Проверьте подключение в клиенте."
          : "Временный профиль готов. Скопируйте его из поля ниже.",
      );
    } catch (caught) {
      setError(onboardingErrorMessage(caught instanceof Error ? caught.message : null));
    } finally {
      setBusy(false);
    }
  };

  const copyTestProfile = async () => {
    if (!testProfileUri) {
      return;
    }
    setError(null);
    setMessage(
      await copyVpnNodeTestProfile(testProfileUri)
        ? "Временный профиль скопирован."
        : "Не удалось скопировать автоматически. Скопируйте профиль из поля.",
    );
  };

  const confirmTest = async () => {
    await run(() => api.confirmVpnNodeOnboarding(worker.id, { confirmed: true }));
  };

  const retryLoad = async () => {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      await onRetry();
    } catch {
      // App owns the bounded loading error for this worker.
    } finally {
      setBusy(false);
    }
  };

  if (setupAction !== null) {
    const needsSsh = setupAction === "configure_ssh";
    return (
      <section className="vpn-node-onboarding is-setup" aria-label="Настройка VPN‑ноды">
        <div className="vpn-node-onboarding-copy" role="status">
          <strong>{needsSsh ? "Нужен SSH‑доступ" : "VPN‑нода не включена"}</strong>
          <p>{needsSsh ? "Заполните SSH, чтобы продолжить безопасную настройку." : "Включите сервер и роль VPN для начала настройки."}</p>
        </div>
        <button
          type="button"
          className="vpn-node-onboarding-primary"
          onClick={needsSsh ? onConfigureSsh : onEnableVpnNode}
        >
          {needsSsh ? "Заполнить SSH" : "Включить VPN-ноду"}
        </button>
      </section>
    );
  }

  if (loadError) {
    return (
      <section className="vpn-node-onboarding is-load-error" aria-label="Настройка VPN‑ноды">
        <p role="alert" className="vpn-node-onboarding-error">{onboardingErrorMessage(loadError)}</p>
        <button
          type="button"
          className="vpn-node-onboarding-primary"
          onClick={() => void retryLoad()}
          disabled={busy}
        >
          {busy ? "Подождите…" : "Повторить загрузку"}
        </button>
      </section>
    );
  }

  if (!status) {
    return (
      <section className="vpn-node-onboarding" aria-label="Настройка VPN‑ноды">
        <p role="status" className="vpn-node-onboarding-loading">Загружаем состояние настройки…</p>
      </section>
    );
  }

  const [title, description] = onboardingPresentation(status.state);
  const fingerprint = status.fingerprint;
  const isProgressing = status.state === "queued" || status.state === "running";
  const canTrust = status.state === "needs_trust"
    && isVpnNodeFingerprintConfirmed(confirmedFingerprint, fingerprint);
  const showMainAction = status.state !== "ready" && !(status.state === "awaiting_test" && testProfileUri);

  const runPrimaryAction = () => {
    if (status.state === "needs_preflight") {
      void run(() => api.preflightVpnNode(worker.id));
    } else if (status.state === "needs_trust" && fingerprint && canTrust) {
      void run(() => api.trustVpnNode(worker.id, { fingerprint }));
    } else if (status.state === "ready_to_start" || status.state === "failed") {
      void run(() => api.startVpnNodeOnboarding(worker.id));
    } else if (status.state === "awaiting_test") {
      void loadTestProfile();
    }
  };

  return (
    <section className={`vpn-node-onboarding is-${status.state}`} aria-label="Настройка VPN‑ноды">
      <div className="vpn-node-onboarding-copy" role="status" aria-live="polite">
        <strong>{title}</strong>
        <p>{description}</p>
      </div>

      {status.state === "needs_trust" && status.fingerprint ? (
        <div className="vpn-node-trust">
          <span>Fingerprint Ed25519</span>
          <code>{status.fingerprint}</code>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={isVpnNodeFingerprintConfirmed(confirmedFingerprint, status.fingerprint)}
              onChange={(event) => setConfirmedFingerprint(event.target.checked ? status.fingerprint ?? null : null)}
            />
            <span>Fingerprint совпадает с панелью хостинга</span>
          </label>
        </div>
      ) : null}

      {showMainAction ? (
        <button
          type="button"
          className="vpn-node-onboarding-primary"
          onClick={runPrimaryAction}
          disabled={busy || isProgressing || (status.state === "needs_trust" && !canTrust)}
        >
          {busy ? "Подождите…" : title}
        </button>
      ) : null}

      {status.state === "awaiting_test" && testProfileUri ? (
        <div className="vpn-node-test-profile">
          <label>
            <span>Временный профиль</span>
            <textarea readOnly spellCheck={false} rows={3} value={testProfileUri} />
          </label>
          <button type="button" className="ghost" onClick={() => void copyTestProfile()} disabled={busy}>
            Скопировать профиль
          </button>
          <label className="inline-check">
            <input
              type="checkbox"
              checked={testConfirmed}
              onChange={(event) => setTestConfirmed(event.target.checked)}
            />
            <span>Подключение проверено на реальном устройстве</span>
          </label>
          <button
            type="button"
            className="vpn-node-onboarding-primary"
            onClick={() => void confirmTest()}
            disabled={busy || !testConfirmed}
          >
            Подтвердить и включить ноду
          </button>
        </div>
      ) : null}

      {message ? <p role="status" className="vpn-node-onboarding-message">{message}</p> : null}
      {error || status.error_code ? (
        <p role="alert" className="vpn-node-onboarding-error">
          {error ?? onboardingErrorMessage(status.error_code)}
        </p>
      ) : null}
    </section>
  );
}
