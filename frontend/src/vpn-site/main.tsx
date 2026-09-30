import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";

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

function BotAction({ config, label }: { config: PortalConfig | null; label: string }) {
  if (config?.bot_url) {
    return (
      <a className="button button--primary" href={config.bot_url} target="_blank" rel="noreferrer">
        {label}
      </a>
    );
  }
  return <span className="button button--disabled" aria-disabled="true">Бот временно недоступен</span>;
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

  async function loadConfig(): Promise<void> {
    const epoch = ++configEpoch.current;
    setConfigBusy(true);
    setConfigError("");
    try {
      const nextConfig = await portalApi.config();
      if (configEpoch.current === epoch) setConfig(nextConfig);
    } catch {
      if (configEpoch.current === epoch) setConfigError("Не удалось загрузить ссылку на бота и контакт поддержки.");
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
      if (plansEpoch.current === epoch) setPlansError("Не удалось загрузить тарифы.");
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
    <div className="site-shell">
      <header className="site-header">
        <a className="brand" href="#top" aria-label="Veltrix VPN, в начало"><span>V</span> Veltrix VPN</a>
        <nav aria-label="Основная навигация">
          <a href="#how">Как работает</a>
          <a href="#plans">Тарифы</a>
          <a href="#support">Поддержка</a>
          <a href="/cabinet/">Кабинет</a>
        </nav>
      </header>

      <main id="top">
        <section className="hero" aria-labelledby="hero-title">
          <div>
            <p className="eyebrow">Простой старт через Telegram</p>
            <h1 id="hero-title">Veltrix VPN для повседневного подключения</h1>
            <p className="lead">Получите профиль в официальном боте, добавьте его в Happ и управляйте доступом в личном кабинете.</p>
            <div className="hero-actions">
              {configBusy ? (
                <span className="button button--disabled" aria-disabled="true">Загружаем ссылку…</span>
              ) : <BotAction config={config} label="Открыть Telegram-бот" />}
              <a className="button button--ghost" href="/cabinet/">Личный кабинет</a>
            </div>
            {configError && (
              <div className="inline-error" role="alert">
                <span>{configError}</span>
                <button onClick={() => void loadConfig()}>Повторить</button>
              </div>
            )}
          </div>
          <aside className="hero-card" aria-label="Проверенная платформа">
            <span className="signal" aria-hidden="true">✓</span>
            <strong>Проверено с Happ</strong>
            <p>Happ для iPhone (iOS) и Android</p>
            <small>Инструкции для других платформ появятся после проверки.</small>
          </aside>
        </section>

        <section id="how" className="section" aria-labelledby="how-title">
          <p className="eyebrow">Три шага</p>
          <h2 id="how-title">Как это работает</h2>
          <div className="steps">
            <article><span>1</span><h3>Откройте бота</h3><p>Начните диалог с официальным Telegram-ботом Veltrix VPN.</p></article>
            <article><span>2</span><h3>Получите профиль</h3><p>После подтверждения доступ появится в кабинете. Скопируйте ссылку или покажите QR-код.</p></article>
            <article><span>3</span><h3>Подключите Happ</h3><p>Импортируйте профиль в Happ на iPhone или Android и включите соединение.</p></article>
          </div>
        </section>

        <section id="plans" className="section" aria-labelledby="plans-title">
          <p className="eyebrow">Публичный каталог</p>
          <h2 id="plans-title">Тарифы</h2>
          <p className="section-copy"><strong>Оплата пока не подключена.</strong> Платные тарифы нельзя приобрести на сайте или в кабинете.</p>
          {plansBusy && <p className="state-card" role="status">Загружаем тарифы…</p>}
          {plansError && (
            <div className="state-card state-card--error" role="alert">
              <p>{plansError}</p>
              <button className="button button--ghost" onClick={() => void loadPlans()}>Повторить</button>
            </div>
          )}
          {!plansBusy && !plansError && plans.length === 0 && <p className="state-card">Тарифы ещё не опубликованы.</p>}
          {!plansBusy && !plansError && plans.length > 0 && (
            <div className="plans-grid">
              {plans.map((plan) => (
                <article className="plan-card" key={plan.id}>
                  <div className="plan-heading"><h3>{plan.name}</h3><strong>{formatPlanPrice(plan.price_amount, plan.currency)}</strong></div>
                  {plan.description && <p>{plan.description}</p>}
                  <dl>
                    <div><dt>Срок</dt><dd>{formatPlanDuration(plan.duration_days)}</dd></div>
                    <div><dt>Устройства</dt><dd>{formatPlanDevices(plan.max_devices)}</dd></div>
                    <div><dt>Трафик</dt><dd>{formatPlanTraffic(plan.traffic_limit_gb)}</dd></div>
                  </dl>
                  {plan.is_trial ? (
                    <BotAction config={config} label="Узнать о пробном доступе" />
                  ) : (
                    <button className="button button--disabled" disabled>Покупка скоро будет доступна</button>
                  )}
                </article>
              ))}
            </div>
          )}
        </section>

        <section id="trial" className="section split" aria-labelledby="trial-title">
          <div>
            <p className="eyebrow">Знакомство с сервисом</p>
            <h2 id="trial-title">Пробный доступ</h2>
          </div>
          {configBusy ? (
            <p>Уточняем доступность пробного периода…</p>
          ) : config?.trial_enabled ? (
            <div>
              <p><strong>Пробный доступ на 7 дней сейчас доступен.</strong> Он выдаётся один раз и включает один профиль; при отсутствии свободной мощности новые подключения могут быть временно приостановлены.</p>
              <BotAction config={config} label="Получить пробный доступ" />
            </div>
          ) : (
            <p><strong>Пробный запуск готовится.</strong> Сейчас получить пробный доступ нельзя.</p>
          )}
        </section>

        <section id="support" className="section split" aria-labelledby="support-title">
          <div><p className="eyebrow">Связь</p><h2 id="support-title">Поддержка</h2></div>
          <div>
            <p>{config?.support_text || SUPPORT_FALLBACK}</p>
            {!configBusy && <BotAction config={config} label="Написать в Telegram" />}
          </div>
        </section>

        <section id="privacy" className="section policy" aria-labelledby="privacy-title">
          <p className="eyebrow">Приватность</p>
          <h2 id="privacy-title">Какие данные нужны сервису</h2>
          <p>Для работы используются данные Telegram-профиля, состояние подписки и устройств, а также агрегированные счётчики трафика. Ссылка подключения доступна только после входа в кабинет и не размещается на этой странице.</p>
        </section>

        <section id="terms" className="section policy" aria-labelledby="terms-title">
          <p className="eyebrow">Допустимое использование</p>
          <h2 id="terms-title">Условия использования</h2>
          <p>Используйте сервис законно, не передавайте личную ссылку доступа другим людям и не применяйте подключение для спама, атак или действий, нарушающих права третьих лиц. Доступность зависит от текущей ёмкости и технического состояния сервиса.</p>
        </section>
      </main>

      <footer>
        <a className="brand" href="#top"><span>V</span> Veltrix VPN</a>
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
