import { useId } from "react";

import type { PortalPlan } from "./types";
import {
  formatPlanDevices,
  formatPlanDuration,
  formatPlanPrice,
  formatPlanTraffic,
} from "./planFormatting";

interface PlanCatalogProps {
  plans: PortalPlan[];
  busy: boolean;
  error: string;
  onRetry: () => void;
}

export default function PlanCatalog({ plans, busy, error, onRetry }: PlanCatalogProps) {
  const headingId = useId();

  return (
    <section id="plans" className="portal-section" aria-labelledby={headingId}>
      <div className="section-heading">
        <p className="eyebrow">Варианты</p>
        <h2 id={headingId}>Тарифы</h2>
      </div>
      {busy && <p className="card" role="status">Загружаем тарифы…</p>}
      {error && (
        <div className="card message message--error" role="alert">
          <p>{error}</p>
          <button type="button" className="button button--primary" onClick={onRetry}>Повторить</button>
        </div>
      )}
      {!busy && !error && plans.length === 0 && <p className="card">Тарифы ещё не опубликованы</p>}
      {!busy && !error && plans.length > 0 && (
        <div className="card-grid">
          {plans.map((plan) => (
            <article className="card plan-card" key={plan.id} aria-labelledby={`plan-${plan.id}-title`}>
              <div className="card-row">
                <h3 id={`plan-${plan.id}-title`}>{plan.name}</h3>
                <strong className="plan-card__price">
                  {formatPlanPrice(plan.price_amount, plan.currency)}
                </strong>
              </div>
              {plan.description && <p className="plan-card__description">{plan.description}</p>}
              <dl className="facts">
                <div><dt>Срок</dt><dd>{formatPlanDuration(plan.duration_days)}</dd></div>
                <div><dt>Устройства</dt><dd>{formatPlanDevices(plan.max_devices)}</dd></div>
                <div><dt>Трафик</dt><dd>{formatPlanTraffic(plan.traffic_limit_gb)}</dd></div>
              </dl>
              {plan.is_trial ? (
                <a className="button button--primary plan-card__action" href="#subscription">
                  Перейти к пробному доступу
                </a>
              ) : (
                <button type="button" className="button button--ghost plan-card__action" disabled>
                  Покупка скоро будет доступна
                </button>
              )}
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
