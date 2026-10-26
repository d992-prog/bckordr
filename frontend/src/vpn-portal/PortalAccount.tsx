import PlanCatalog from "./PlanCatalog";
import type { PortalPlan, PortalPreferences } from "./types";

interface PortalAccountProps {
  displayName: string;
  preferences: PortalPreferences | null;
  preferencesBusy: boolean;
  preferencesError: string;
  preferencesSaved: boolean;
  onPreferenceChange: (value: boolean) => void;
  onPreferencesRetry: () => void;
  remindersEnabled: boolean;
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  onPlansRetry: () => void;
  onLogout: () => void;
}

export function PortalAccount(props: PortalAccountProps) {
  const {preferences, preferencesBusy: busy, preferencesError: error, preferencesSaved: saved} = props;

  return <section id="account" className="portal-account">
    <header className="section-heading"><h1>Аккаунт</h1></header>
    <article className="vx-matte"><span>Telegram</span><strong>{props.displayName}</strong></article>
    <section className="vx-matte"><h2>Напоминания</h2><label className="reminder-preference"><input type="checkbox" checked={preferences?.expiry_reminders ?? false} disabled={busy || !preferences} onChange={(event) => props.onPreferenceChange(event.target.checked)} />Напоминания об окончании доступа</label><p className="hint">{props.remindersEnabled ? "Ваш выбор действует для будущих напоминаний об окончании подписки" : "Напоминания пока отключены оператором. Ваш выбор сохранится, но сам по себе не включает отправку"}</p>{busy && <p role="status">{preferences ? "Сохраняем…" : "Загружаем настройку…"}</p>}{error && <p className="message message--error" role="alert">{error}</p>}{error && !preferences && <button type="button" className="button button--ghost" onClick={props.onPreferencesRetry}>Повторить загрузку</button>}{saved && <p role="status">Настройка сохранена</p>}</section>
    {(props.plans.length > 0 || props.plansBusy || props.plansError) && <PlanCatalog plans={props.plans} busy={props.plansBusy} error={props.plansError} onRetry={props.onPlansRetry} />}
    <nav aria-label="Документы"><a href="/vpn/#privacy">Конфиденциальность</a><a href="/vpn/#terms">Условия использования</a></nav>
    <button type="button" className="button button--quiet" onClick={props.onLogout}>Выйти</button>
  </section>;
}
