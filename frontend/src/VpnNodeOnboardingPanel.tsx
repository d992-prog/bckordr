import { useEffect, useRef, useState } from "react";

import { api, type VpnNodeOnboarding } from "./api";
import {
  copyVpnNodeTestProfile,
  createVpnNodeOnboardingActionGuard,
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
  onPatchWorker: (patch: { vpn_enabled: true } | { vpn_role: "vpn_node" }) => Promise<void>;
  onOpenWorker: () => Promise<void>;
  onConfigureSsh: () => Promise<void>;
};

export function VpnNodeOnboardingPanel({
  worker,
  status: loadedStatus,
  loadError = null,
  onStatusChange,
  onRetry,
  onPatchWorker,
  onOpenWorker,
  onConfigureSsh,
}: Props) {
  const actionGuardRef = useRef(createVpnNodeOnboardingActionGuard());
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

  const runExclusive = async (action: () => Promise<void>) => {
    await actionGuardRef.current.run(async () => {
      setBusy(true);
      try {
        await action();
      } finally {
        setBusy(false);
      }
    });
  };

  const run = async (action: () => Promise<VpnNodeOnboarding>) => {
    await runExclusive(async () => {
      setError(null);
      setMessage(null);
      try {
        publishStatus(await action());
      } catch (caught) {
        setError(onboardingErrorMessage(caught instanceof Error ? caught.message : null));
      }
    });
  };

  const loadTestProfile = async () => {
    await runExclusive(async () => {
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
      }
    });
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
    await runExclusive(async () => {
      setError(null);
      setMessage(null);
      try {
        await onRetry();
      } catch {
        // App owns the bounded loading error for this worker.
      }
    });
  };

  if (setupAction !== null) {
    const setupCopy = setupAction.kind === "wait_for_worker"
      ? ["Сервер ещё не готов", "Дождитесь восстановления воркера и обновите данные.", null]
      : setupAction.kind === "open_worker"
        ? ["Воркер выключен", "Проверьте настройки воркера перед включением.", "Открыть воркер"]
        : setupAction.kind === "configure_ssh"
          ? ["Нужен SSH‑доступ", "Заполните SSH, чтобы продолжить безопасную настройку.", "Заполнить SSH"]
          : "vpn_enabled" in setupAction.patch
            ? ["VPN на сервере выключен", "Включите VPN без изменения роли воркера.", "Включить VPN-ноду"]
            : ["Роль VPN не назначена", "Назначьте серверу роль VPN без изменения остальных настроек.", "Назначить роль VPN"];
    const runSetupAction = async () => {
      await runExclusive(async () => {
        setError(null);
        try {
          if (setupAction.kind === "patch_worker") {
            await onPatchWorker(setupAction.patch);
          } else if (setupAction.kind === "open_worker") {
            await onOpenWorker();
          } else if (setupAction.kind === "configure_ssh") {
            await onConfigureSsh();
          }
        } catch (caught) {
          setError(onboardingErrorMessage(caught instanceof Error ? caught.message : null));
        }
      });
    };
    return (
      <section className="vpn-node-onboarding is-setup" aria-label="Настройка VPN‑ноды">
        <div className="vpn-node-onboarding-copy" role="status">
          <strong>{setupCopy[0]}</strong>
          <p>{setupCopy[1]}</p>
        </div>
        {setupCopy[2] ? (
          <button
            type="button"
            className="vpn-node-onboarding-primary"
            onClick={() => void runSetupAction()}
            disabled={busy}
          >
            {busy ? "Подождите…" : setupCopy[2]}
          </button>
        ) : null}
        {error ? <p role="alert" className="vpn-node-onboarding-error">{error}</p> : null}
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
