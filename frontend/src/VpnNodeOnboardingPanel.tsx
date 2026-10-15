import { useEffect, useState } from "react";

import { api, type VpnNodeOnboarding } from "./api";
import {
  copyVpnNodeTestProfile,
  onboardingErrorMessage,
  onboardingPresentation,
} from "./vpnNodeOnboarding";

type Props = {
  workerId: number;
  status: VpnNodeOnboarding | null;
  loadError?: string | null;
  onStatusChange: (status: VpnNodeOnboarding) => void;
};

export function VpnNodeOnboardingPanel({
  workerId,
  status: loadedStatus,
  loadError = null,
  onStatusChange,
}: Props) {
  const [status, setStatus] = useState(loadedStatus);
  const [busy, setBusy] = useState(false);
  const [trustConfirmed, setTrustConfirmed] = useState(false);
  const [testConfirmed, setTestConfirmed] = useState(false);
  const [testProfileUri, setTestProfileUri] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    setStatus(loadedStatus);
    if (loadedStatus?.state !== "needs_trust") {
      setTrustConfirmed(false);
    }
    if (loadedStatus?.state !== "awaiting_test") {
      setTestProfileUri(null);
      setTestConfirmed(false);
    }
  }, [loadedStatus]);

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
      const profile = await api.getVpnNodeTestProfile(workerId);
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
    await run(() => api.confirmVpnNodeOnboarding(workerId, { confirmed: true }));
  };

  if (!status) {
    return (
      <section className="vpn-node-onboarding" aria-label="Настройка VPN‑ноды">
        <p role="status" className="vpn-node-onboarding-loading">Загружаем состояние настройки…</p>
        {loadError ? <p role="alert" className="vpn-node-onboarding-error">{onboardingErrorMessage(loadError)}</p> : null}
      </section>
    );
  }

  const [title, description] = onboardingPresentation(status.state);
  const isProgressing = status.state === "queued" || status.state === "running";
  const canTrust = status.state === "needs_trust" && Boolean(status.fingerprint) && trustConfirmed;
  const showMainAction = status.state !== "ready" && !(status.state === "awaiting_test" && testProfileUri);

  const runPrimaryAction = () => {
    if (status.state === "needs_preflight") {
      void run(() => api.preflightVpnNode(workerId));
    } else if (status.state === "needs_trust" && status.fingerprint && canTrust) {
      void run(() => api.trustVpnNode(workerId, { fingerprint: status.fingerprint! }));
    } else if (status.state === "ready_to_start" || status.state === "failed") {
      void run(() => api.startVpnNodeOnboarding(workerId));
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
              checked={trustConfirmed}
              onChange={(event) => setTrustConfirmed(event.target.checked)}
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
            <textarea readOnly rows={3} value={testProfileUri} />
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
      {error || loadError || status.error_code ? (
        <p role="alert" className="vpn-node-onboarding-error">
          {error ?? onboardingErrorMessage(loadError ?? status.error_code)}
        </p>
      ) : null}
    </section>
  );
}
