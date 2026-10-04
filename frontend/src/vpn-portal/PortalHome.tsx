import { useEffect, useMemo, useRef, useState } from "react";

import { PortalError } from "./api";
import type { SessionGeneration } from "./bootstrap";
import { copyConnectionUri, loadConnectionUri } from "./connection";
import { DataError } from "./DataError";
import { buildPortalHomeView } from "./homeView";
import PlanCatalog from "./PlanCatalog";
import TrialCard from "./TrialCard";
import type { PortalPlan, PortalProfile, PortalSubscription, PortalTrial } from "./types";
import { portalDate, stateLabel } from "./view";

interface PortalHomeProps {
  busy: boolean;
  error: string;
  subscriptions: PortalSubscription[];
  profiles: PortalProfile[];
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  showPlans: boolean;
  trial: PortalTrial | null;
  trialBusy: boolean;
  trialError: string;
  sessionGeneration: SessionGeneration;
  onRetry: () => void;
  onRetryPlans: () => void;
  onActivateTrial: () => void;
  onRefreshTrial: () => void;
  onUnauthorized: () => void;
}

const STATUS_BADGES = {
  ready: "Активен",
  preparing: "Подготавливается",
  paused: "Приостановлен",
  expired: "Срок закончился",
  empty: "Нет доступа",
} as const;

type CopyMessage = { kind: "success" | "error"; text: string };

function AccessSummary({
  busy,
  error,
  subscriptions,
  trial,
  trialBusy,
  trialError,
  onRetry,
  onActivateTrial,
  onRefreshTrial,
}: Pick<PortalHomeProps,
  | "busy"
  | "error"
  | "subscriptions"
  | "trial"
  | "trialBusy"
  | "trialError"
  | "onRetry"
  | "onActivateTrial"
  | "onRefreshTrial"
>) {
  return (
    <section className="portal-access" aria-labelledby="access-summary-title">
      <h2 id="access-summary-title">Доступ</h2>
      <TrialCard
        trial={trial}
        busy={trialBusy || busy}
        error={trialError}
        onActivate={onActivateTrial}
        onRefresh={onRefreshTrial}
      />
      {busy && <p className="card" role="status">Загружаем данные…</p>}
      {error && <DataError message={error} onRetry={onRetry} />}
      {!busy && !error && subscriptions.length === 0 && (
        <p className="card">Подписок пока нет. Если вы ожидали доступ, напишите в поддержку</p>
      )}
      <div className="card-grid">
        {subscriptions.map((subscription) => (
          <article className="card subscription-card" key={subscription.id}>
            <div className="card-row">
              <h3>Доступ Veltrix</h3>
              <span className={`status status--${subscription.state === "active" ? "good" : "quiet"}`}>
                {stateLabel(subscription.state)}
              </span>
            </div>
            <dl className="facts">
              <div>
                <dt>Начало</dt>
                <dd>{subscription.starts_at === null ? "Дата начала не указана" : portalDate(subscription.starts_at)}</dd>
              </div>
              <div><dt>Окончание</dt><dd>{portalDate(subscription.expires_at)}</dd></div>
              <div><dt>Профили</dt><dd>{subscription.profiles_used} из {subscription.profile_limit}</dd></div>
              {subscription.traffic_limit_gb_per_profile !== null && (
                <div><dt>Трафик</dt><dd>{subscription.traffic_limit_gb_per_profile} ГБ на профиль</dd></div>
              )}
            </dl>
            <p className="hint">Статистика пока недоступна</p>
          </article>
        ))}
      </div>
    </section>
  );
}

export function PortalHome(props: PortalHomeProps) {
  const view = useMemo(
    () => buildPortalHomeView(props.subscriptions, props.profiles, props.trial),
    [props.profiles, props.subscriptions, props.trial],
  );
  const [copyBusy, setCopyBusy] = useState(false);
  const [copyMessage, setCopyMessage] = useState<CopyMessage | null>(null);
  const copyEpoch = useRef(0);
  const awaitingFirstLoad = props.busy
    && props.subscriptions.length === 0
    && props.profiles.length === 0
    && props.trial === null;
  const failedFirstLoad = !props.busy
    && Boolean(props.error)
    && props.subscriptions.length === 0
    && props.profiles.length === 0
    && props.trial === null;

  useEffect(() => {
    copyEpoch.current += 1;
    setCopyBusy(false);
    setCopyMessage(null);
  }, [view.profileId]);

  useEffect(() => () => {
    copyEpoch.current += 1;
  }, []);

  async function copyProfile(profileId: number | null): Promise<void> {
    if (profileId === null || copyBusy) {
      return;
    }
    const generation = props.sessionGeneration.current();
    const epoch = ++copyEpoch.current;
    const isCurrent = () => (
      props.sessionGeneration.isCurrent(generation) && copyEpoch.current === epoch
    );
    setCopyBusy(true);
    setCopyMessage(null);
    try {
      const uri = await loadConnectionUri(profileId);
      if (!isCurrent()) {
        return;
      }
      await copyConnectionUri(uri);
      if (isCurrent()) {
        setCopyMessage({ kind: "success", text: "Ссылка скопирована" });
      }
    } catch (error) {
      if (!isCurrent()) {
        return;
      }
      if (error instanceof PortalError && error.status === 401) {
        props.onUnauthorized();
        return;
      }
      setCopyMessage({
        kind: "error",
        text: "Не удалось скопировать автоматически. Откройте профиль и скопируйте ссылку вручную",
      });
    } finally {
      if (isCurrent()) {
        setCopyBusy(false);
      }
    }
  }

  return (
    <section id="home" className="portal-home">
      {awaitingFirstLoad ? (
        <div className="portal-status-lens portal-status-lens--neutral" role="status">
          <span className="portal-status-lens__badge">Загрузка</span>
          <h1>Загружаем данные</h1>
          <p>Получаем подписку и VPN‑профили</p>
        </div>
      ) : failedFirstLoad ? (
        <div className="portal-status-lens portal-status-lens--error" role="alert">
          <span className="portal-status-lens__badge">Ошибка загрузки</span>
          <h1>Не удалось загрузить данные</h1>
          <p>Попробуйте ещё раз</p>
          <button className="button button--primary" onClick={props.onRetry}>Повторить</button>
        </div>
      ) : (
        <>
          <div
            className={`portal-status-lens portal-status-lens--${view.kind}`}
            aria-live="polite"
          >
            <span className="portal-status-lens__badge">{STATUS_BADGES[view.kind]}</span>
            <h1>{view.title}</h1>
            <p>{view.detail}</p>
          </div>
          {view.profileId !== null && (
            <a className="portal-primary button button--primary vx-glass" href="#profiles">
              Открыть профиль <span aria-hidden="true">→</span>
            </a>
          )}
          <section className="portal-actions" aria-labelledby="quick-actions-title">
            <h2 id="quick-actions-title">Быстрые действия</h2>
            <button
              className="button button--primary"
              disabled={view.profileId === null || copyBusy}
              onClick={() => void copyProfile(view.profileId)}
            >
              {copyBusy ? "Копируем…" : "Скопировать ссылку"}
            </button>
            <a className="button button--ghost" href="#connect">Инструкция</a>
            {copyMessage && (
              <p
                className={`message ${copyMessage.kind === "error" ? "message--error" : ""}`}
                role={copyMessage.kind === "error" ? "alert" : "status"}
              >
                {copyMessage.text}
              </p>
            )}
          </section>
          <AccessSummary
            busy={props.busy}
            error={props.error}
            subscriptions={props.subscriptions}
            trial={props.trial}
            trialBusy={props.trialBusy}
            trialError={props.trialError}
            onRetry={props.onRetry}
            onActivateTrial={props.onActivateTrial}
            onRefreshTrial={props.onRefreshTrial}
          />
        </>
      )}
      {props.showPlans && (
        <PlanCatalog
          plans={props.plans}
          busy={props.plansBusy}
          error={props.plansError}
          onRetry={props.onRetryPlans}
        />
      )}
    </section>
  );
}
