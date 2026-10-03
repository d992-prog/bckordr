import PlanCatalog from "./PlanCatalog";
import type { PortalPlan } from "./types";

interface PortalAccountProps {
  displayName: string;
  supportText: string;
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  onPlansRetry: () => void;
  onLogout: () => void;
}

export function PortalAccount(props: PortalAccountProps) {
  return (
    <section id="account" className="portal-account">
      <header className="section-heading">
        <p className="eyebrow">Настройки</p>
        <h1>Аккаунт</h1>
      </header>

      <article className="vx-matte">
        <span>Telegram</span>
        <strong>{props.displayName}</strong>
      </article>

      <PlanCatalog
        plans={props.plans}
        busy={props.plansBusy}
        error={props.plansError}
        onRetry={props.onPlansRetry}
      />

      <article id="help" className="vx-matte">
        <h2>Помощь</h2>
        <p>{props.supportText}</p>
      </article>

      <nav aria-label="Документы">
        <a href="/vpn/#privacy">Конфиденциальность</a>
        <a href="/vpn/#terms">Условия использования</a>
      </nav>

      <button type="button" className="button button--quiet" onClick={props.onLogout}>
        Выйти
      </button>
    </section>
  );
}
