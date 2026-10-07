import { useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";

import { VeltrixMark } from "../brand/VeltrixMark";
import { PortalError, portalApi } from "./api";
import {
  SessionGeneration,
  type PortalBootstrapResult,
  type TelegramLaunch,
} from "./bootstrap";
import { DataError } from "./DataError";
import { PortalAccount } from "./PortalAccount";
import { PortalHome } from "./PortalHome";
import { PortalNavigation } from "./PortalNavigation";
import ProfileCard from "./ProfileCard";
import { portalSectionFromHash, type PortalSection } from "./navigation";
import { nextTrialPoll, updateTrialPollCount } from "./trialPolling";
import type {
  PortalConfig,
  PortalMe,
  PortalPlan,
  PortalProfile,
  PortalSubscription,
  PortalTrial,
} from "./types";
import { portalDate } from "./view";

interface PortalProps {
  launch: TelegramLaunch;
  bootstrap: (launch: TelegramLaunch) => Promise<PortalBootstrapResult>;
}

type ManualScreen = "ready" | "signed-out" | "logging-out" | "logout-failed" | "unauthorized";

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
      <VeltrixMark decorative={title === "Veltrix VPN"} />
      <section className="card state-card vx-matte">
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

const VPN_CLIENTS = [
  {
    platform: "iPhone",
    app: "Happ",
    downloadUrl: "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru",
    downloadLabel: "Скачать Happ в App Store",
  },
  {
    platform: "Android",
    app: "Happ",
    downloadUrl: "https://play.google.com/store/apps/details?id=com.happproxy",
    downloadLabel: "Скачать Happ в Google Play",
  },
  {
    platform: "Windows",
    app: "Hiddify",
    downloadUrl: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1",
    downloadLabel: "Скачать Hiddify",
  },
  {
    platform: "macOS",
    app: "Hiddify",
    downloadUrl: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1",
    downloadLabel: "Скачать Hiddify",
  },
  {
    platform: "Linux",
    app: "Hiddify",
    downloadUrl: "https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1",
    downloadLabel: "Скачать Hiddify",
  },
] as const;

type VpnPlatform = (typeof VPN_CLIENTS)[number]["platform"];

function ProfilesSection(props: ProfilesSectionProps) {
  return (
    <section id="profiles" className="portal-section">
      <div className="section-heading"><p className="eyebrow">Доступ</p><h1>Профили</h1></div>
      {props.busy && <p className="card" role="status">Загружаем профили…</p>}
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
  platform: VpnPlatform;
  onPlatformChange: (platform: VpnPlatform) => void;
}) {
  const client = VPN_CLIENTS.find((item) => item.platform === platform) ?? VPN_CLIENTS[0];

  return (
    <section id="connect" className="portal-section">
      <div className="section-heading"><p className="eyebrow">Инструкция</p><h2>Как подключиться</h2></div>
      <div className="card connect-card">
        <div className="platforms" role="group" aria-label="Выберите платформу">
          {VPN_CLIENTS.map((item) => (
            <button
              type="button"
              key={item.platform}
              className={`platform ${platform === item.platform ? "platform--active" : ""}`}
              aria-pressed={platform === item.platform}
              onClick={() => onPlatformChange(item.platform)}
            >{item.platform}</button>
          ))}
        </div>
        <h3>{platform}</h3>
        <ol className="steps">
          <li>
            Установите приложение {client.app}.
            <a
              className="button button--ghost client-download"
              href={client.downloadUrl}
              target="_blank"
              rel="noreferrer"
            >{client.downloadLabel}</a>
          </li>
          <li>В разделе <a href="#profiles">«Профили»</a> откройте и скопируйте ссылку.</li>
          <li>В {client.app} выберите импорт по ссылке и вставьте её.</li>
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
  const [plans, setPlans] = useState<PortalPlan[]>([]);
  const [plansBusy, setPlansBusy] = useState(false);
  const [plansError, setPlansError] = useState("");
  const [subscriptions, setSubscriptions] = useState<PortalSubscription[]>([]);
  const [profiles, setProfiles] = useState<PortalProfile[]>([]);
  const [trial, setTrial] = useState<PortalTrial | null>(null);
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialError, setTrialError] = useState("");
  const [trialPollCount, setTrialPollCount] = useState(0);
  const [dataBusy, setDataBusy] = useState(false);
  const [dataError, setDataError] = useState("");
  const [platform, setPlatform] = useState<VpnPlatform>("iPhone");
  const [activeSection, setActiveSection] = useState<PortalSection>(() => portalSectionFromHash(window.location.hash));
  const [activeAnchor, setActiveAnchor] = useState(() => window.location.hash.replace(/^#\/?/, "").split("?", 1)[0]);
  const [profileOperations, setProfileOperations] = useState<Record<number, ProfileOperation>>({});
  const sessionGeneration = useMemo(() => new SessionGeneration(), []);
  const logoutCsrf = useRef<string | null>(null);
  const plansLoadEpoch = useRef(0);
  const dataLoadEpoch = useRef(0);
  const trialRequestInFlight = useRef(false);

  function clearPrivateData(nextScreen: ManualScreen): void {
    sessionGeneration.invalidate();
    plansLoadEpoch.current += 1;
    dataLoadEpoch.current += 1;
    setBootstrapResult((current) => current === null
      ? null
      : { kind: "login-required", config: current.config });
    setMe(null);
    setPlans([]);
    setPlansBusy(false);
    setPlansError("");
    setSubscriptions([]);
    setProfiles([]);
    setTrial(null);
    setTrialBusy(false);
    setTrialError("");
    setTrialPollCount((current) => updateTrialPollCount(current, "session"));
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

  async function loadPlans(): Promise<void> {
    const generation = sessionGeneration.current();
    const loadEpoch = ++plansLoadEpoch.current;
    const isCurrentLoad = () => (
      sessionGeneration.isCurrent(generation) && plansLoadEpoch.current === loadEpoch
    );
    setPlansBusy(true);
    setPlansError("");
    try {
      const nextPlans = await portalApi.plans();
      if (isCurrentLoad()) {
        setPlans(nextPlans);
      }
    } catch (error) {
      if (isCurrentLoad()) {
        setPlansError(error instanceof PortalError
          ? error.message
          : "Не удалось загрузить тарифы. Попробуйте ещё раз.");
      }
    } finally {
      if (isCurrentLoad()) {
        setPlansBusy(false);
      }
    }
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
    async function observe<T>(request: Promise<T>): Promise<T> {
      try {
        return await request;
      } catch (error) {
        if (isCurrentLoad() && isUnauthorized(error)) {
          handleUnauthorized();
        }
        throw error;
      }
    }
    try {
      const [nextTrial, nextSubscriptions, nextProfiles] = await Promise.all([
        observe(portalApi.trial()),
        observe(portalApi.subscriptions()),
        observe(portalApi.profiles()),
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
        : "Не удалось загрузить данные. Попробуйте снова";
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
      setTrialPollCount((current) => updateTrialPollCount(current, "activation"));
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
    setTrialPollCount((current) => updateTrialPollCount(current, "manual"));
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
        void loadPlans();
      }
    });
    return () => { active = false; };
    // The bootstrap function is a stable module-level coordinator in production.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [bootstrap, launch]);

  useEffect(() => () => {
    sessionGeneration.invalidate();
    plansLoadEpoch.current += 1;
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
          setTrialPollCount((current) => updateTrialPollCount(current, "automatic"));
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
      setActiveAnchor(window.location.hash.replace(/^#\/?/, "").split("?", 1)[0]);
      const section = portalSectionFromHash(window.location.hash);
      setActiveSection(section);
    };
    window.addEventListener("hashchange", updateSection);
    updateSection();
    return () => window.removeEventListener("hashchange", updateSection);
  }, []);

  useEffect(() => {
    if (!activeAnchor) {
      return;
    }
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(activeAnchor)?.scrollIntoView({ block: "start" });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [activeAnchor, activeSection, dataBusy, me, plansBusy]);

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
        <button type="button" className="button button--primary" onClick={() => void performLogout(logoutCsrf.current)}>
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
    return <StatePage title="Veltrix VPN"><p role="status">Загружаем личный кабинет…</p></StatePage>;
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
        <button type="button" className="button button--primary" onClick={() => void performLogout(null)}>
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
        <a className="portal-brand-link" href="#home" aria-label="Veltrix VPN">
          <VeltrixMark decorative />
        </a>
        <div className="account">
          <a className="button button--ghost" href="#help">Помощь</a>
        </div>
      </header>

      <PortalNavigation active={activeSection} />

      <main className="portal-content">
        {activeSection === "home" && (
          <PortalHome
            busy={dataBusy}
            error={dataError}
            subscriptions={subscriptions}
            profiles={profiles}
            plans={plans}
            plansBusy={plansBusy}
            plansError={plansError}
            showPlans={activeAnchor === "plans"}
            onRetry={() => void loadPrivateData()}
            onRetryPlans={() => void loadPlans()}
            trial={trial}
            trialBusy={trialBusy || dataBusy}
            trialError={trialError}
            sessionGeneration={sessionGeneration}
            onActivateTrial={() => void activateTrial()}
            onRefreshTrial={() => void refreshTrial()}
            onUnauthorized={handleUnauthorized}
          />
        )}

        {activeSection === "profiles" && (
          <>
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
            <ConnectionSection platform={platform} onPlatformChange={setPlatform} />
          </>
        )}

        {activeSection === "account" && (
          <PortalAccount
            displayName={me.display_name}
            supportText={bootstrapResult.config?.support_text ?? ""}
            plans={plans}
            plansBusy={plansBusy}
            plansError={plansError}
            onPlansRetry={() => void loadPlans()}
            onLogout={startLogout}
          />
        )}
      </main>
    </div>
  );
}
