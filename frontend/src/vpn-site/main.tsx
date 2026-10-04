import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";

import { VeltrixMark } from "../brand/VeltrixMark";
import { portalApi } from "../vpn-portal/api";
import {
  formatPlanDevices,
  formatPlanDuration,
  formatPlanPrice,
  formatPlanTraffic,
} from "../vpn-portal/planFormatting";
import type { PortalConfig, PortalPlan } from "../vpn-portal/types";
import "./site.css";

const SUPPORT_FALLBACK = "Контакт поддержки временно недоступен. Попробуйте позже.";

function currentHashTarget(): HTMLElement | null {
  const rawHash = window.location.hash.slice(1);
  if (!rawHash) return null;
  try {
    return document.getElementById(decodeURIComponent(rawHash));
  } catch {
    return null;
  }
}

function useFragmentNavigation(revision: string): void {
  useEffect(() => {
    let frame = 0;
    let active = true;
    const reconcile = () => {
      window.cancelAnimationFrame(frame);
      frame = window.requestAnimationFrame(() => {
        const target = currentHashTarget();
        if (!active || !target) return;
        const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        target.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
      });
    };
    reconcile();
    window.addEventListener("hashchange", reconcile);
    window.addEventListener("load", reconcile, { once: true });
    void document.fonts?.ready.then(reconcile);
    return () => {
      active = false;
      window.cancelAnimationFrame(frame);
      window.removeEventListener("hashchange", reconcile);
      window.removeEventListener("load", reconcile);
    };
  }, [revision]);
}

function BrandLink({ footer = false }: { footer?: boolean }) {
  return (
    <a className="site-brand" href="#top" aria-label="Veltrix VPN, в начало">
      <VeltrixMark decorative withName />
      {footer && <span className="site-brand__caption">Простой старт через Telegram</span>}
    </a>
  );
}

function BotAction({ config, label }: { config: PortalConfig | null; label: string }) {
  if (config?.bot_url) {
    return (
      <a className="button button--primary" href={config.bot_url} target="_blank" rel="noreferrer">
        {label}
      </a>
    );
  }
  return <button className="button button--disabled" type="button" disabled>Бот временно недоступен</button>;
}

interface HeroProps {
  config: PortalConfig | null;
  configBusy: boolean;
  configError: string;
  onConfigRetry: () => Promise<void>;
}

function Hero({ config, configBusy, configError, onConfigRetry }: HeroProps) {
  return (
    <section className="hero vx-motion" aria-labelledby="hero-title">
      <div className="hero-copy">
        <p className="eyebrow">Простой старт через Telegram</p>
        <h1 id="hero-title">VPN без сложных настроек</h1>
        <p className="lead">
          Получите готовый профиль в официальном боте и добавьте его в поддерживаемое приложение
        </p>
        <div className="hero-actions">
          {configBusy ? (
            <span className="button button--disabled" role="status" aria-live="polite">Загружаем ссылку…</span>
          ) : <BotAction config={config} label="Открыть в Telegram" />}
          <a className="button button--ghost" href="#how">Как подключиться</a>
        </div>
        {configError && (
          <div className="inline-error" role="alert">
            <span>{configError}</span>
            <button type="button" onClick={() => void onConfigRetry()}>Повторить</button>
          </div>
        )}
      </div>

      <div className="hero-visual vx-glass" aria-label="Путь от Telegram к приложению">
        <div className="hero-visual__mark" aria-hidden="true">
          <VeltrixMark decorative withName={false} />
        </div>
        <p className="hero-visual__title">Один профиль для подключения</p>
        <div className="hero-visual__route" aria-hidden="true">
          <span>Telegram</span>
          <i className="hero-visual__trace" />
          <span>Профиль</span>
          <i className="hero-visual__trace" />
          <span>Приложение</span>
        </div>
        <p className="hero-visual__caption">Ссылка и пошаговая инструкция появятся в личном кабинете</p>
      </div>
    </section>
  );
}

function ConnectionRoute() {
  return (
    <section id="how" className="site-section site-connection-route" aria-labelledby="how-title">
      <div className="section-heading">
        <h2 id="how-title">Как подключиться</h2>
        <p>Три понятных шага — без ручной настройки сервера и протокола</p>
      </div>
      <ol className="connection-steps">
        <li>
          <span className="step-number" aria-hidden="true">1</span>
          <div><h3>Откройте бота</h3><p>Перейдите в официальный Telegram-бот Veltrix VPN</p></div>
        </li>
        <li>
          <span className="step-number" aria-hidden="true">2</span>
          <div><h3>Получите профиль</h3><p>Бот откроет кабинет с готовой ссылкой подключения</p></div>
        </li>
        <li>
          <span className="step-number" aria-hidden="true">3</span>
          <div><h3>Добавьте в приложение</h3><p>Импортируйте ссылку и включите созданный профиль</p></div>
        </li>
      </ol>
    </section>
  );
}

function SupportedApps() {
  return (
    <section className="site-section supported-apps" aria-labelledby="apps-title">
      <div className="section-heading">
        <h2 id="apps-title">Поддерживаемые устройства</h2>
        <p>Профиль проверен в Happ на iPhone (iOS) и в Hiddify на Windows</p>
      </div>
      <ul className="device-list" aria-label="Поддерживаемые платформы">
        <li><strong>iPhone</strong><span>Happ · iOS</span></li>
        <li><strong>Windows</strong><span>Hiddify</span></li>
      </ul>
      <p className="supported-apps__note">Инструкция для каждой платформы доступна в личном кабинете</p>
    </section>
  );
}

interface PlansSectionProps {
  config: PortalConfig | null;
  plans: PortalPlan[];
  busy: boolean;
  error: string;
  onRetry: () => Promise<void>;
}

function PlansSection({ config, plans, busy, error, onRetry }: PlansSectionProps) {
  return (
    <section id="plans" className="site-section plans-section" aria-labelledby="plans-title">
      <div className="section-heading section-heading--split">
        <div><h2 id="plans-title">Тарифы</h2><p>Публичные условия сервиса без скрытых технических параметров</p></div>
        <p className="payment-notice">Оплата пока не подключена</p>
      </div>
      {busy && <p className="state-card" role="status">Загружаем тарифы…</p>}
      {error && (
        <div className="state-card state-card--error" role="alert">
          <p>{error}</p>
          <button className="button button--ghost" type="button" onClick={() => void onRetry()}>Повторить</button>
        </div>
      )}
      {!busy && !error && plans.length === 0 && <p className="state-card">Тарифы ещё не опубликованы</p>}
      {!busy && !error && plans.length > 0 && (
        <div className="plans-list">
          {plans.map((plan) => (
            <article className="plan-card" key={plan.id}>
              <div className="plan-card__intro">
                <h3>{plan.name}</h3>
                {plan.description && <p>{plan.description}</p>}
              </div>
              <dl className="plan-facts">
                <div><dt>Срок</dt><dd>{formatPlanDuration(plan.duration_days)}</dd></div>
                <div><dt>Устройства</dt><dd>{formatPlanDevices(plan.max_devices)}</dd></div>
                <div><dt>Трафик</dt><dd>{formatPlanTraffic(plan.traffic_limit_gb)}</dd></div>
              </dl>
              <div className="plan-card__action">
                <strong>{formatPlanPrice(plan.price_amount, plan.currency)}</strong>
                {plan.is_trial ? (
                  <BotAction config={config} label="Узнать о пробном доступе" />
                ) : (
                  <button className="button button--disabled" type="button" disabled>
                    Покупка скоро будет доступна
                  </button>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}

function TrialSection({ config }: { config: PortalConfig | null }) {
  return (
    <section id="trial" className="site-section feature-section" aria-labelledby="trial-title">
      <div><h2 id="trial-title">Пробный доступ</h2></div>
      <div>
        <p><strong>Пробный доступ на 7 дней предоставляется поэтапно.</strong> Актуальную доступность проверьте в боте; выдача зависит от свободной мощности.</p>
        {config?.bot_url && <BotAction config={config} label="Проверить доступность в боте" />}
      </div>
    </section>
  );
}

function SupportSection({ config, configBusy }: { config: PortalConfig | null; configBusy: boolean }) {
  return (
    <section id="support" className="site-section feature-section" aria-labelledby="support-title">
      <div><h2 id="support-title">Поддержка</h2></div>
      <div>
        <p>{config?.support_text || SUPPORT_FALLBACK}</p>
        {!configBusy && <BotAction config={config} label="Написать в Telegram" />}
      </div>
    </section>
  );
}

function Policies() {
  return (
    <section className="site-section policies" aria-label="Документы Veltrix VPN">
      <article id="privacy" aria-labelledby="privacy-title">
        <h2 id="privacy-title">Конфиденциальность</h2>
        <p>Для работы используются данные Telegram-профиля, состояние подписки и устройств, а также агрегированные счётчики трафика. Ссылка подключения доступна только после входа в кабинет и не размещается на этой странице.</p>
      </article>
      <article id="terms" aria-labelledby="terms-title">
        <h2 id="terms-title">Условия использования</h2>
        <p>Используйте сервис законно, не передавайте личную ссылку доступа другим людям и не применяйте подключение для спама, атак или действий, нарушающих права третьих лиц. Доступность зависит от текущей ёмкости и технического состояния сервиса.</p>
      </article>
    </section>
  );
}

function PublicVpnSite() {
  const [config, setConfig] = useState<PortalConfig | null>(null);
  const [configBusy, setConfigBusy] = useState(true);
  const [configError, setConfigError] = useState("");
  const [plans, setPlans] = useState<PortalPlan[]>([]);
  const [plansBusy, setPlansBusy] = useState(true);
  const [plansError, setPlansError] = useState("");
  const configEpoch = useRef(0);
  const plansEpoch = useRef(0);
  useFragmentNavigation([
    configBusy,
    configError,
    config?.support_text.length ?? 0,
    plansBusy,
    plansError,
    plans.length,
  ].join(":"));

  async function loadConfig(): Promise<void> {
    const epoch = ++configEpoch.current;
    setConfigBusy(true);
    setConfigError("");
    try {
      const nextConfig = await portalApi.config();
      if (configEpoch.current === epoch) setConfig(nextConfig);
    } catch {
      if (configEpoch.current === epoch) setConfigError("Не удалось загрузить ссылку на бота и контакт поддержки");
    } finally {
      if (configEpoch.current === epoch) setConfigBusy(false);
    }
  }

  async function loadPlans(): Promise<void> {
    const epoch = ++plansEpoch.current;
    setPlansBusy(true);
    setPlansError("");
    try {
      const nextPlans = await portalApi.plans();
      if (plansEpoch.current === epoch) setPlans(nextPlans);
    } catch {
      if (plansEpoch.current === epoch) setPlansError("Не удалось загрузить тарифы");
    } finally {
      if (plansEpoch.current === epoch) setPlansBusy(false);
    }
  }

  useEffect(() => {
    void loadConfig();
    void loadPlans();
    return () => {
      configEpoch.current += 1;
      plansEpoch.current += 1;
    };
    // Initial public requests are intentionally independent.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="site-shell vx-atmosphere">
      <header className="site-header">
        <div className="site-nav vx-glass">
          <BrandLink />
          <nav aria-label="Основная навигация">
            <a href="#how">Как подключиться</a>
            <a href="#apps-title">Устройства</a>
            <a href="#plans">Тарифы</a>
            <a href="#support">Поддержка</a>
            <a className="site-nav__cabinet" href="/cabinet/">Личный кабинет</a>
          </nav>
        </div>
      </header>

      <main id="top">
        <Hero config={config} configBusy={configBusy} configError={configError} onConfigRetry={loadConfig} />
        <ConnectionRoute />
        <SupportedApps />
        <PlansSection config={config} plans={plans} busy={plansBusy} error={plansError} onRetry={loadPlans} />
        <TrialSection config={config} />
        <SupportSection config={config} configBusy={configBusy} />
        <Policies />
      </main>

      <footer className="site-footer">
        <BrandLink footer />
        <nav aria-label="Ссылки в подвале">
          <a href="/cabinet/">Личный кабинет</a>
          <a href="#privacy">Конфиденциальность</a>
          <a href="#terms">Условия</a>
        </nav>
      </footer>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode><PublicVpnSite /></React.StrictMode>,
);
