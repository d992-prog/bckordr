import { useEffect, useRef, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
import PlanCatalog from "./PlanCatalog";
import type { PortalPlan, PortalPreferences } from "./types";

interface PortalAccountProps {
  displayName: string;
  csrfToken: string;
  sessionGeneration: SessionGeneration;
  remindersEnabled: boolean;
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  onPlansRetry: () => void;
  onLogout: () => void;
  onUnauthorized: () => void;
}

export function PortalAccount(props: PortalAccountProps) {
  const [preferences, setPreferences] = useState<PortalPreferences | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  const [retry, setRetry] = useState(0);
  const epoch = useRef(0);
  const saving = useRef(false);

  useEffect(() => {
    const generation = props.sessionGeneration.current(); const requestEpoch = ++epoch.current;
    const isCurrent = () => props.sessionGeneration.isCurrent(generation) && epoch.current === requestEpoch;
    setBusy(true); setError(""); setPreferences(null);
    void portalApi.preferences().then((result) => { if (isCurrent()) setPreferences(result); }).catch((cause) => {
      if (!isCurrent()) return;
      if (cause instanceof PortalError && cause.status === 401) props.onUnauthorized(); else setError("Не удалось загрузить настройку напоминаний");
    }).finally(() => { if (isCurrent()) setBusy(false); });
    return () => { epoch.current += 1; };
    // Preferences belong to the authenticated session, not a render callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.csrfToken, props.sessionGeneration, retry]);

  async function save(value: boolean): Promise<void> {
    if (saving.current || busy || !preferences) return;
    saving.current = true;
    const generation = props.sessionGeneration.current(); const requestEpoch = ++epoch.current;
    const isCurrent = () => props.sessionGeneration.isCurrent(generation) && epoch.current === requestEpoch;
    setBusy(true); setError(""); setSaved(false);
    try {
      const result = await portalApi.updatePreferences(value, props.csrfToken);
      if (isCurrent()) { setPreferences(result); setSaved(true); }
    } catch (cause) {
      if (!isCurrent()) return;
      if (cause instanceof PortalError && cause.status === 401) props.onUnauthorized(); else setError("Не удалось сохранить настройку. Повторите попытку");
    } finally { if (isCurrent()) { saving.current = false; setBusy(false); } }
  }

  return <section id="account" className="portal-account">
    <header className="section-heading"><h1>Аккаунт</h1></header>
    <article className="vx-matte"><span>Telegram</span><strong>{props.displayName}</strong></article>
    <section className="vx-matte"><h2>Напоминания</h2><label className="reminder-preference"><input type="checkbox" checked={preferences?.expiry_reminders ?? false} disabled={busy || !preferences} onChange={(event) => void save(event.target.checked)} />Напоминания об окончании доступа</label><p className="hint">{props.remindersEnabled ? "Ваш выбор действует для будущих напоминаний об окончании подписки" : "Напоминания пока отключены оператором. Ваш выбор сохранится, но сам по себе не включает отправку"}</p>{busy && <p role="status">{preferences ? "Сохраняем…" : "Загружаем настройку…"}</p>}{error && <p className="message message--error" role="alert">{error}</p>}{error && !preferences && <button type="button" className="button button--ghost" onClick={() => setRetry((value) => value + 1)}>Повторить загрузку</button>}{saved && <p role="status">Настройка сохранена</p>}</section>
    {(props.plans.length > 0 || props.plansBusy || props.plansError) && <PlanCatalog plans={props.plans} busy={props.plansBusy} error={props.plansError} onRetry={props.onPlansRetry} />}
    <nav aria-label="Документы"><a href="/vpn/#privacy">Конфиденциальность</a><a href="/vpn/#terms">Условия использования</a></nav>
    <button type="button" className="button button--quiet" onClick={props.onLogout}>Выйти</button>
  </section>;
}
