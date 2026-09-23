import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { PortalError, portalApi } from "./api";
import {
  SessionGeneration,
  type PortalBootstrapResult,
  type TelegramLaunch,
} from "./bootstrap";
import ProfileCard from "./ProfileCard";
import TrialCard from "./TrialCard";
import { nextTrialPoll } from "./trialPolling";
import type {
  PortalConfig,
  PortalMe,
  PortalProfile,
  PortalSubscription,
  PortalTrial,
} from "./types";
import { portalDate, stateLabel } from "./view";

interface PortalProps {
  launch: TelegramLaunch;
  bootstrap: (launch: TelegramLaunch) => Promise<PortalBootstrapResult>;
}

type ManualScreen = "ready" | "signed-out" | "logging-out" | "logout-failed" | "unauthorized";

const NAVIGATION = [
  ["subscription", "Подписка"],
  ["profiles", "Профили"],
  ["connect", "Подключение"],
  ["plans", "Тарифы"],
  ["help", "Помощь"],
] as const;
type SectionId = (typeof NAVIGATION)[number][0];

function sectionFromHash(hash: string): SectionId {
  const requested = hash.replace(/^#\/?/, "").split("?", 1)[0];
  return NAVIGATION.some(([id]) => id === requested)
    ? requested as SectionId
    : "subscription";
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof PortalError && error.status === 401;
}

function profileSubscriptionLabel(
  profile: PortalProfile,
  subscriptions: PortalSubscription[],
): string {
  const subscription = subscriptions.find((item) => item.id === profile.subscription_id);
  if (!subscription) {
    return "Подписка не найдена";
  }
  const expiry = subscription.expires_at === null
    ? "без срока окончания"
    : `до ${portalDate(subscription.expires_at)}`;
  return `Veltrix VPN · ${expiry}`;
}

function LoginAction({ config }: { config: PortalConfig | null }) {
  if (config?.browser_login_enabled && config.login_path) {
    return <a className="button button--primary" href={config.login_path}>Войти через Telegram</a>;
  }
  return <p className="hint">Откройте личный кабинет заново из Mini App в Telegram.</p>;
}

function ReopenMiniAppGuidance() {
  return <p className="hint">Закройте это окно и откройте Mini App из Telegram заново.</p>;
}

function StatePage({
  title,
  children,
}: {
  title: string;
  children: ReactNode;
}) {
  return (
    <main className="state-page">
      <div className="brand" aria-label="Veltrix VPN"><span>V</span> Veltrix VPN</div>
      <section className="card state-card">
        <h1>{title}</h1>
        {children}
      </section>
    </main>
  );
}

interface DataSectionProps {
  busy: boolean;
  error: string;
  onRetry: () => void;
}

function DataError({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <div className="card message message--error" role="alert">
      <p>{message}</p>
      <button className="button button--primary" onClick={onRetry}>Повторить</button>
    </div>
  );
}

function SubscriptionSection({
  busy,
  error,
  onRetry,
  subscriptions,
  trial,
  trialBusy,
  trialError,
  onActivateTrial,
  onRefreshTrial,
}: DataSectionProps & {
  subscriptions: PortalSubscription[];
  trial: PortalTrial | null;
  trialBusy: boolean;
  trialError: string;
  onActivateTrial: () => void;
  onRefreshTrial: () => void;
}) {
  return (
    <section id="subscription" className="portal-section">
      <div className="section-heading">
        <p className="eyebrow">Личный кабинет</p>
        <h1>Ваша подписка</h1>
      </div>
      <TrialCard
        trial={trial}
        busy={trialBusy}
        error={trialError}
        onActivate={onActivateTrial}
        onRefresh={onRefreshTrial}
      />
      {busy && <p className="card">Загружаем подписки…</p>}
      {error && <DataError message={error} onRetry={onRetry} />}
      {!busy && !error && subscriptions.length === 0 && (
        <p className="card">Подписок пока нет. Если вы ожидали доступ, напишите в поддержку.</p>
      )}
      <div className="card-grid">
        {subscriptions.map((subscription) => (
          <article className="card subscription-card" key={subscription.id}>
            <div className="card-row">
              <h2>Veltrix VPN</h2>
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
                <div><dt>Трафик</dt><dd>{subscription.traffic_limit_gb_per_profile} ГБ на профиль</dd></div>
              )}
            </dl>
            <p className="hint">Статистика пока недоступна</p>
          </article>
        ))}
      </div>
      {!busy && !error && subscriptions.length > 0 && (
        <div className="section-actions">
          <a className="button button--primary" href="#connect">Подключить VPN</a>
          <a className="button button--ghost" href="#profiles">Мои профили</a>
        </div>
      )}
    </section>
  );
}

interface ProfilesSectionProps extends DataSectionProps {
  profiles: PortalProfile[];
  subscriptions: PortalSubscription[];
  csrfToken: string;
  sessionGeneration: SessionGeneration;
  onUnauthorized: () => void;
  onProfileChange: (profile: PortalProfile) => void;
  profileOperations: Record<number, ProfileOperation>;
  onRenameStart: (profileId: number) => void;
  onRenameSettled: (profileId: number) => void;
}

interface ProfileOperation {
  connectionVersion: number;
  renamePending: boolean;
}

function ProfilesSection(props: ProfilesSectionProps) {
  return (
    <section id="profiles" className="portal-section">
      <div className="section-heading"><p className="eyebrow">Доступ</p><h2>Профили</h2></div>
      {props.busy && <p className="card">Загружаем профили…</p>}
      {props.error && <DataError message={props.error} onRetry={props.onRetry} />}
      {!props.busy && !props.error && props.profiles.length === 0 && <p className="card">Профилей пока нет.</p>}
      <div className="profile-grid">
        {props.profiles.map((profile) => {
          const operation = props.profileOperations[profile.id] ?? {
            connectionVersion: 0,
            renamePending: false,
          };
          return (
          <ProfileCard
            key={profile.id}
            profile={profile}
            subscriptionLabel={profileSubscriptionLabel(profile, props.subscriptions)}
            csrfToken={props.csrfToken}
            sessionGeneration={props.sessionGeneration}
            onUnauthorized={props.onUnauthorized}
            onProfileChange={props.onProfileChange}
            connectionVersion={operation.connectionVersion}
            renamePending={operation.renamePending}
            onRenameStart={props.onRenameStart}
            onRenameSettled={props.onRenameSettled}
          />
          );
        })}
      </div>
    </section>
  );
}

function ConnectionSection({
  platform,
  onPlatformChange,
}: {
  platform: string;
  onPlatformChange: (platform: string) => void;
}) {
  return (
    <section id="connect" className="portal-section">
      <div className="section-heading"><p className="eyebrow">Инструкция</p><h2>Как подключиться</h2></div>
      <div className="card connect-card">
        <div className="platforms" role="group" aria-label="Выберите платформу">
          {["iPhone", "Android", "Windows", "macOS"].map((item) => (
            <button
              key={item}
              className={`platform ${platform === item ? "platform--active" : ""}`}
              aria-pressed={platform === item}
              onClick={() => onPlatformChange(item)}
            >{item}</button>
          ))}
        </div>
        <h3>{platform}</h3>
        <ol className="steps">
          <li>Установите официальное приложение Happ.</li>
          <li>В разделе <a href="#profiles">«Профили»</a> откройте и скопируйте ссылку.</li>
          <li>В Happ выберите импорт по ссылке и вставьте её.</li>
          <li>Выберите импортированный профиль и включите подключение.</li>
        </ol>
        <p className="hint">Нужна помощь? Перейдите в <a href="#help">раздел поддержки</a>.</p>
      </div>
    </section>
  );
}

export default function Portal({ launch, bootstrap }: PortalProps) {
  const [bootstrapResult, setBootstrapResult] = useState<PortalBootstrapResult | null>(null);
  const [screen, setScreen] = useState<ManualScreen>("ready");
  const [me, setMe] = useState<PortalMe | null>(null);
  const [subscriptions, setSubscriptions] = useState<PortalSubscription[]>([]);
  const [profiles, setProfiles] = useState<PortalProfile[]>([]);
  const [trial, setTrial] = useState<PortalTrial | null>(null);
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialError, setTrialError] = useState("");
  const [trialPollCount, setTrialPollCount] = useState(0);
  const [dataBusy, setDataBusy] = useState(false);
  const [dataError, setDataError] = useState("");
  const [platform, setPlatform] = useState("iPhone");
  const [activeSection, setActiveSection] = useState<SectionId>(() => sectionFromHash(window.location.hash));
  const [profileOperations, setProfileOperations] = useState<Record<number, ProfileOperation>>({});
  const sessionGeneration = useMemo(() => new SessionGeneration(), []);
  const logoutCsrf = useRef<string | null>(null);
  const dataLoadEpoch = useRef(0);
  const trialRequestInFlight = useRef(false);

  function clearPrivateData(nextScreen: ManualScreen): void {
    sessionGeneration.invalidate();
    dataLoadEpoch.current += 1;
    setBootstrapResult((current) => current === null
      ? null
      : { kind: "login-required", config: current.config });
    setMe(null);
    setSubscriptions([]);
    setProfiles([]);
    setTrial(null);
    setTrialBusy(false);
    setTrialError("");
    setTrialPollCount(0);
    trialRequestInFlight.current = false;
    setProfileOperations({});
    setDataError("");
    setDataBusy(false);
    setScreen(nextScreen);
  }

  function handleUnauthorized(): void {
    logoutCsrf.current = null;
    clearPrivateData("unauthorized");
  }

  function setProfileRenamePending(profileId: number, renamePending: boolean): void {
    setProfileOperations((current) => {
      const operation = current[profileId] ?? { connectionVersion: 0, renamePending: false };
      return {
        ...current,
        [profileId]: {
          connectionVersion: operation.connectionVersion + 1,
          renamePending,
        },
      };
    });
  }

  async function loadPrivateData(silent = false): Promise<void> {
    const generation = sessionGeneration.current();
    const loadEpoch = ++dataLoadEpoch.current;
    if (!silent) {
      setDataBusy(true);
      setDataError("");
    }

    const isCurrentLoad = () => (
      sessionGeneration.isCurrent(generation) && dataLoadEpoch.current === loadEpoch
    );
    try {
      const [nextTrial, nextSubscriptions, nextProfiles] = await Promise.all([
        portalApi.trial(),
        portalApi.subscriptions(),
        portalApi.profiles(),
      ]);
      if (!isCurrentLoad()) {
        return;
      }
      setTrial(nextTrial);
      setSubscriptions(nextSubscriptions);
      setProfiles(nextProfiles);
      setTrialError("");
    } catch (error) {
      if (!isCurrentLoad()) {
        return;
      }
      if (isUnauthorized(error)) {
        handleUnauthorized();
        return;
      }
      const message = error instanceof PortalError
        ? error.message
        : "Не удалось загрузить данные. Попробуйте ещё раз.";
      if (silent) {
        setTrialError(message);
      } else {
        setDataError(message);
      }
    } finally {
      if (!silent && isCurrentLoad()) {
        setDataBusy(false);
      }
    }
  }

  async function activateTrial(): Promise<void> {
    if (trialRequestInFlight.current || dataBusy || me === null) {
      return;
    }
    const generation = sessionGeneration.current();
    trialRequestInFlight.current = true;
    setTrialBusy(true);
    setTrialError("");
    try {
      const activatedTrial = await portalApi.activateTrial(me.csrf_token);
      if (!sessionGeneration.isCurrent(generation)) {
        return;
      }
      setTrial(activatedTrial);
      setTrialPollCount(0);
      await loadPrivateData(true);
    } catch (error) {
      if (!sessionGeneration.isCurrent(generation)) {
        return;
      }
      if (isUnauthorized(error)) {
        handleUnauthorized();
        return;
      }
      if (error instanceof PortalError && error.status === 409) {
        await loadPrivateData(true);
        return;
      }
      setTrialError(
        error instanceof PortalError
          ? error.message
          : "Не удалось активировать пробный доступ. Попробуйте ещё раз.",
      );
    } finally {
      trialRequestInFlight.current = false;
      if (sessionGeneration.isCurrent(generation)) {
        setTrialBusy(false);
      }
    }
  }

  async function refreshTrial(): Promise<void> {
    if (trialRequestInFlight.current || dataBusy) {
      return;
    }
    const generation = sessionGeneration.current();
    trialRequestInFlight.current = true;
    setTrialBusy(true);
    setTrialError("");
    setTrialPollCount(0);
    try {
      await loadPrivateData(true);
    } finally {
      trialRequestInFlight.current = false;
      if (sessionGeneration.isCurrent(generation)) {
        setTrialBusy(false);
      }
    }
  }

  useEffect(() => {
    let active = true;
    void bootstrap(launch).then((result) => {
      if (!active) {
        return;
      }
      setBootstrapResult(result);
      if (result.kind === "ready") {
        setMe(result.me);
        void loadPrivateData();
      }
    });
    return () => { active = false; };
    // The bootstrap function is a stable module-level coordinator in production.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootstrap, launch]);

  useEffect(() => () => {
    sessionGeneration.invalidate();
    dataLoadEpoch.current += 1;
    trialRequestInFlight.current = false;
  }, [sessionGeneration]);

  useEffect(() => {
    if (trial?.state !== "preparing" || trialBusy || dataBusy) {
      return;
    }
    const generation = sessionGeneration.current();
    const timer = nextTrialPoll(trialPollCount, () => {
      if (!sessionGeneration.isCurrent(generation) || trialRequestInFlight.current) {
        return;
      }
      trialRequestInFlight.current = true;
      setTrialBusy(true);
      void loadPrivateData(true).finally(() => {
        trialRequestInFlight.current = false;
        if (sessionGeneration.isCurrent(generation)) {
          setTrialBusy(false);
          setTrialPollCount((current) => current + 1);
        }
      });
    });
    if (timer === null) {
      return;
    }
    return () => window.clearTimeout(timer);
    // loadPrivateData is intentionally invoked only by the scheduled generation.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dataBusy, sessionGeneration, trial?.state, trialBusy, trialPollCount]);

  useEffect(() => {
    const updateSection = () => {
      const section = sectionFromHash(window.location.hash);
      setActiveSection(section);
    };
    window.addEventListener("hashchange", updateSection);
    updateSection();
    return () => window.removeEventListener("hashchange", updateSection);
  }, []);

  async function performLogout(csrf: string | null): Promise<void> {
    clearPrivateData("logging-out");
    try {
      let usableCsrf = csrf;
      if (usableCsrf === null) {
        const csrfOnly = await portalApi.me();
        usableCsrf = csrfOnly.csrf_token;
      }
      logoutCsrf.current = usableCsrf;
      const result = await portalApi.logout(usableCsrf);
      if (!result.logged_out) {
        throw new Error("logout rejected");
      }
      logoutCsrf.current = null;
      setScreen("signed-out");
    } catch (error) {
      if (isUnauthorized(error)) {
        logoutCsrf.current = null;
        setScreen("signed-out");
        return;
      }
      setScreen("logout-failed");
    }
  }

  function startLogout(): void {
    const csrf = me?.csrf_token ?? null;
    logoutCsrf.current = csrf;
    void performLogout(csrf);
  }

  if (screen === "logging-out") {
    return <StatePage title="Выходим из аккаунта"><p>Завершаем сессию…</p></StatePage>;
  }
  if (screen === "signed-out") {
    return (
      <StatePage title="Вы вышли из аккаунта">
        {launch.platform !== "unknown" ? (
          <p>Для входа снова закройте это окно и откройте Mini App из Telegram.</p>
        ) : (
          <LoginAction config={bootstrapResult?.config ?? null} />
        )}
      </StatePage>
    );
  }
  if (screen === "logout-failed") {
    return (
      <StatePage title="Не удалось выйти">
        <p>Сервер не подтвердил выход. Личные данные скрыты на этом экране.</p>
        <button className="button button--primary" onClick={() => void performLogout(logoutCsrf.current)}>
          Повторить выход
        </button>
      </StatePage>
    );
  }
  if (screen === "unauthorized") {
    return (
      <StatePage title="Сессия завершена">
        <p>Мы скрыли данные кабинета. Войдите снова.</p>
        {launch.platform === "unknown"
          ? <LoginAction config={bootstrapResult?.config ?? null} />
          : <ReopenMiniAppGuidance />}
      </StatePage>
    );
  }

  if (bootstrapResult === null) {
    return <StatePage title="Veltrix VPN"><p>Загружаем личный кабинет…</p></StatePage>;
  }
  if (bootstrapResult.kind === "disabled") {
    return <StatePage title="Личный кабинет пока недоступен"><p>{bootstrapResult.config?.support_text}</p></StatePage>;
  }
  if (bootstrapResult.kind === "login-required") {
    return (
      <StatePage title="Войдите в личный кабинет">
        <LoginAction config={bootstrapResult.config} />
      </StatePage>
    );
  }
  if (bootstrapResult.kind === "reopen-mini-app") {
    return (
      <StatePage title="Войдите в личный кабинет">
        <ReopenMiniAppGuidance />
      </StatePage>
    );
  }
  if (bootstrapResult.kind === "fresh-launch-required") {
    return (
      <StatePage title="Нужно открыть Mini App заново">
        <p>Данные запуска устарели или неверны. Закройте это окно и откройте кабинет из Telegram ещё раз.</p>
      </StatePage>
    );
  }
  if (bootstrapResult.kind === "account-switch") {
    return (
      <StatePage title="Открыт другой аккаунт">
        <p>Сначала выйдите из текущей сессии, затем заново откройте Mini App из Telegram.</p>
        <button className="button button--primary" onClick={() => void performLogout(null)}>
          Выйти из текущего аккаунта
        </button>
      </StatePage>
    );
  }
  if (bootstrapResult.kind === "cookie-unavailable") {
    return (
      <StatePage title="Браузер не сохранил вход">
        <p>Откройте кабинет во внешнем браузере или заново запустите Mini App. Мы не будем повторять вход автоматически.</p>
        <a className="button button--primary" href="/cabinet/" target="_blank" rel="noreferrer">
          Открыть во внешнем браузере
        </a>
      </StatePage>
    );
  }
  if (bootstrapResult.kind === "error" || me === null) {
    return (
      <StatePage title="Не удалось открыть кабинет">
        <p>Попробуйте позже или обратитесь в поддержку.</p>
      </StatePage>
    );
  }

  return (
    <div className="portal-shell">
      <header className="portal-header">
        <a className="brand" href="#subscription" aria-label="Veltrix VPN">
          <span>V</span> Veltrix VPN
        </a>
        <div className="account">
          <span>{me.display_name}</span>
          <button className="button button--ghost" onClick={startLogout}>Выйти</button>
        </div>
      </header>

      <nav className="section-nav" aria-label="Разделы кабинета">
        {NAVIGATION.map(([id, label]) => (
          <a key={id} href={`#${id}`} aria-current={activeSection === id ? "page" : undefined}>
            {label}
          </a>
        ))}
      </nav>

      <main className="portal-content">
        {activeSection === "subscription" && (
          <SubscriptionSection
            busy={dataBusy}
            error={dataError}
            subscriptions={subscriptions}
            onRetry={() => void loadPrivateData()}
            trial={trial}
            trialBusy={trialBusy || dataBusy}
            trialError={trialError}
            onActivateTrial={() => void activateTrial()}
            onRefreshTrial={() => void refreshTrial()}
          />
        )}

        {activeSection === "profiles" && (
          <ProfilesSection
            busy={dataBusy}
            error={dataError}
            profiles={profiles}
            subscriptions={subscriptions}
            csrfToken={me.csrf_token}
            sessionGeneration={sessionGeneration}
            onRetry={() => void loadPrivateData()}
            onUnauthorized={handleUnauthorized}
            profileOperations={profileOperations}
            onRenameStart={(profileId) => setProfileRenamePending(profileId, true)}
            onRenameSettled={(profileId) => setProfileRenamePending(profileId, false)}
            onProfileChange={(updated) => {
              setProfiles((current) => current.map((item) => item.id === updated.id ? updated : item));
            }}
          />
        )}

        {activeSection === "connect" && (
          <ConnectionSection platform={platform} onPlatformChange={setPlatform} />
        )}

        {activeSection === "plans" && (
        <section id="plans" className="portal-section">
          <div className="section-heading"><p className="eyebrow">Варианты</p><h2>Тарифы</h2></div>
          <div className="card">
            <p>Тарифы ещё не опубликованы</p>
            <a className="button button--ghost" href="#help">Перейти в помощь</a>
          </div>
        </section>
        )}

        {activeSection === "help" && (
        <section id="help" className="portal-section">
          <div className="section-heading"><p className="eyebrow">Поддержка</p><h2>Помощь</h2></div>
          <p className="card support-text">{bootstrapResult.config?.support_text ?? ""}</p>
        </section>
        )}
      </main>
    </div>
  );
}
