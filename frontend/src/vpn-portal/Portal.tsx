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
import { DeviceCreateForm, type DeviceDraft } from "./DeviceActions";
import { DeviceOperationRequests, deviceConnectionBlocked, operationFinished, type DeviceIntent, type DeviceRequestState } from "./deviceOperations";
import { PortalAccount } from "./PortalAccount";
import { PortalHome } from "./PortalHome";
import { PortalHelp } from "./PortalHelp";
import type { PortalUsageProps } from "./PortalUsage";
import { PortalNavigation } from "./PortalNavigation";
import ProfileCard from "./ProfileCard";
import { portalSectionFromHash, type PortalSection } from "./navigation";
import { nextTrialPoll, updateTrialPollCount } from "./trialPolling";
import type {
  PortalConfig,
  PortalMe,
  PortalPlan,
  PortalDevice,
  PortalSubscription,
  PortalTrial,
  PortalUsage,
  PortalUsageDays,
  PortalPreferences,
} from "./types";
import { portalDate, portalInitials, portalNoticeVisible } from "./view";

interface PortalProps {
  launch: TelegramLaunch;
  bootstrap: (launch: TelegramLaunch) => Promise<PortalBootstrapResult>;
}

type ManualScreen = "ready" | "signed-out" | "logging-out" | "logout-failed" | "unauthorized";

function isUnauthorized(error: unknown): boolean {
  return error instanceof PortalError && error.status === 401;
}

function profileSubscriptionLabel(
  profile: PortalDevice,
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
  profiles: PortalDevice[];
  subscriptions: PortalSubscription[];
  csrfToken: string;
  sessionGeneration: SessionGeneration;
  onUnauthorized: () => void;
  onProfileChange: (profile: PortalDevice) => void;
  profileOperations: Record<number, ProfileOperation>;
  onRenameStart: (profileId: number) => void;
  onRenameSettled: (profileId: number) => void;
  onLocationMutationStart: (id: number) => void;
  onLocationReady: () => void;
  onLocationStateChange: (id: number, pending: boolean) => void;
  requests: Record<string, DeviceRequestState>;
  pendingDeviceIds: number[];
  onDeviceAction: (intent: DeviceIntent, key?: string) => void;
  onRefreshOperation: (key: string) => void;
  onAddDevice: () => void;
  usage: PortalUsageProps;
  onConnect: (id: number) => void;
  creationForm?: ReactNode;
}

interface ProfileOperation {
  connectionVersion: number;
  renamePending: boolean;
  locationPending?: boolean;
}

function ProfilesSection(props: ProfilesSectionProps) {
  return (
    <section id="profiles" className="portal-section">
      <div id="devices" className="section-heading cabinet-devices-heading"><h1>Устройства</h1><button type="button" className="button button--primary" onClick={props.onAddDevice}>Добавить устройство</button></div>
      {props.creationForm}
      {props.busy && <p className="card" role="status">Загружаем профили…</p>}
      {props.error && <DataError message={props.error} onRetry={props.onRetry} />}
      {!props.busy && !props.error && props.profiles.length === 0 && <p className="card">Устройств пока нет. Добавьте устройство к действующей подписке</p>}
      <div className="profile-grid">
        {props.profiles.map((profile) => {
          const operation = props.profileOperations[profile.id] ?? {
            connectionVersion: 0,
            renamePending: false,
          };
          const matchingRequests = Object.entries(props.requests).filter(([, entry]) => entry.intent.action !== "create" ? entry.intent.deviceId === profile.id : entry.operation?.device_id === profile.id);
          const [requestKey, request] = matchingRequests.find(([, entry]) => !entry.definitive) ?? matchingRequests[matchingRequests.length - 1] ?? ["", null];
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
            onLocationMutationStart={() => props.onLocationMutationStart(profile.id)}
            onLocationReady={props.onLocationReady}
            onLocationStateChange={(pending) => props.onLocationStateChange(profile.id, pending)}
            locationPending={Boolean(operation.locationPending)}
            operationPending={props.pendingDeviceIds.includes(profile.id)}
            operationRequest={request}
            operationRequestKey={requestKey}
            onDeviceAction={props.onDeviceAction}
            onRefreshOperation={props.onRefreshOperation}
            usage={props.usage}
            onConnect={() => props.onConnect(profile.id)}
          />
          );
        })}
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
  const [profiles, setProfiles] = useState<PortalDevice[]>([]);
  const [selectedDeviceId, setSelectedDeviceId] = useState<number | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [deviceDraft, setDeviceDraft] = useState<DeviceDraft>({display_name:"",subscription_id:0,endpoint_id:0});
  const [deviceRequests, setDeviceRequests] = useState<Record<string, DeviceRequestState>>({});
  const [usageDays, setUsageDays] = useState<PortalUsageDays>(7);
  const [usageReport, setUsageReport] = useState<PortalUsage | null>(null);
  const [usageBusy, setUsageBusy] = useState(false);
  const [usageError, setUsageError] = useState(false);
  const [preferences, setPreferences] = useState<PortalPreferences | null>(null);
  const [preferencesBusy, setPreferencesBusy] = useState(false);
  const [preferencesError, setPreferencesError] = useState("");
  const [preferencesSaved, setPreferencesSaved] = useState(false);
  const [noticeNow, setNoticeNow] = useState(Date.now);
  const [trial, setTrial] = useState<PortalTrial | null>(null);
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialError, setTrialError] = useState("");
  const [trialPollCount, setTrialPollCount] = useState(0);
  const [dataBusy, setDataBusy] = useState(false);
  const [dataError, setDataError] = useState("");
  const [activeSection, setActiveSection] = useState<PortalSection>(() => portalSectionFromHash(window.location.hash));
  const [activeAnchor, setActiveAnchor] = useState(() => window.location.hash.replace(/^#\/?/, "").split("?", 1)[0]);
  const [profileOperations, setProfileOperations] = useState<Record<number, ProfileOperation>>({});
  const sessionGeneration = useMemo(() => new SessionGeneration(), []);
  const operationRequests = useMemo(() => new DeviceOperationRequests(sessionGeneration, setDeviceRequests, handleUnauthorized, startDeviceMutation), [sessionGeneration]);
  const logoutCsrf = useRef<string | null>(null);
  const plansLoadEpoch = useRef(0);
  const dataLoadEpoch = useRef(0);
  const usageLoadEpoch = useRef(0);
  const preferencesEpoch = useRef(0);
  const preferenceRequest = useRef<"read" | "save" | null>(null);
  const operationPolls = useRef<Record<string, number>>({});
  const trialRequestInFlight = useRef(false);

  function clearPrivateData(nextScreen: ManualScreen): void {
    sessionGeneration.invalidate();
    plansLoadEpoch.current += 1;
    dataLoadEpoch.current += 1;
    usageLoadEpoch.current += 1;
    preferencesEpoch.current += 1;
    preferenceRequest.current = null;
    operationRequests.clear();
    operationPolls.current = {};
    setBootstrapResult((current) => current === null
      ? null
      : { kind: "login-required", config: current.config });
    setMe(null);
    setPlans([]);
    setPlansBusy(false);
    setPlansError("");
    setSubscriptions([]);
    setProfiles([]);
    setSelectedDeviceId(null);
    setShowCreate(false);
    setDeviceDraft({display_name:"",subscription_id:0,endpoint_id:0});
    setUsageReport(null); setUsageBusy(false); setUsageError(false);
    setPreferences(null); setPreferencesBusy(false); setPreferencesError(""); setPreferencesSaved(false);
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
    if (renamePending) invalidateDeviceLoad();
    setProfileOperations((current) => {
      const operation = current[profileId] ?? { connectionVersion: 0, renamePending: false };
      return {
        ...current,
        [profileId]: {
          ...operation,
          connectionVersion: operation.connectionVersion + 1,
          renamePending,
        },
      };
    });
  }

  function invalidateDeviceLoad(): void {
    dataLoadEpoch.current += 1;
    setDataBusy(false);
  }

  function invalidateConnection(id: number): void {
    setProfileOperations((current) => {
      const operation = current[id] ?? {connectionVersion:0,renamePending:false};
      return {...current,[id]:{...operation,connectionVersion:operation.connectionVersion+1}};
    });
  }

  function startDeviceMutation(intent: DeviceIntent): void {
    invalidateDeviceLoad();
    if (intent.action !== "create") invalidateConnection(intent.deviceId);
  }

  function locationState(id: number, pending: boolean): void {
    setProfileOperations((current) => ({...current,[id]:{...(current[id] ?? {connectionVersion:0,renamePending:false}),locationPending:pending}}));
  }

  function acceptDeviceOperation(operation: import("./types").PortalDeviceOperation | null): void {
    if (!operation) return;
    if (operation.action === "create" && operation.stage === "completed") { setShowCreate(false); setDeviceDraft({display_name:"",subscription_id:0,endpoint_id:0}); }
    void loadPrivateData(true);
    if (operationFinished(operation)) void loadUsage();
  }

  function submitDeviceAction(intent: DeviceIntent, key?: string): void {
    if (!me) return;
    if (intent.action !== "create" && (profileOperations[intent.deviceId]?.renamePending || profileOperations[intent.deviceId]?.locationPending)) return;
    void operationRequests.submit(intent, me.csrf_token, key).then(acceptDeviceOperation);
  }

  async function refreshDeviceOperation(key: string): Promise<void> {
    const entry = operationRequests.entries[key];
    if (!entry || !me) return;
    acceptDeviceOperation(await (entry.operation ? operationRequests.refresh(key) : operationRequests.submit(entry.intent, me.csrf_token, key)));
  }

  function openCreate(): void {
    operationRequests.dismiss("create");
    const previous = operationRequests.entries.create;
    if (previous?.intent.action === "create") setDeviceDraft({display_name:previous.intent.display_name,subscription_id:previous.intent.subscription_id,endpoint_id:previous.intent.endpoint_id});
    setShowCreate(true);
    window.location.hash = "devices";
  }

  async function loadPreferences(): Promise<void> {
    if (!me || preferenceRequest.current) return;
    preferenceRequest.current = "read";
    const generation = sessionGeneration.current(); const epoch = ++preferencesEpoch.current;
    const isCurrent = () => sessionGeneration.isCurrent(generation) && preferencesEpoch.current === epoch;
    setPreferencesBusy(true); setPreferencesError(""); setPreferencesSaved(false);
    try {
      const result = await portalApi.preferences();
      if (isCurrent()) setPreferences(result);
    } catch (cause) {
      if (!isCurrent()) return;
      if (isUnauthorized(cause)) handleUnauthorized(); else setPreferencesError("Не удалось загрузить настройку напоминаний");
    } finally { if (isCurrent()) { preferenceRequest.current = null; setPreferencesBusy(false); } }
  }

  async function savePreferences(value: boolean): Promise<void> {
    if (!me || preferenceRequest.current || !preferences) return;
    preferenceRequest.current = "save";
    const generation = sessionGeneration.current(); const epoch = ++preferencesEpoch.current;
    const isCurrent = () => sessionGeneration.isCurrent(generation) && preferencesEpoch.current === epoch;
    setPreferencesBusy(true); setPreferencesError(""); setPreferencesSaved(false);
    try {
      const result = await portalApi.updatePreferences(value, me.csrf_token);
      if (isCurrent()) { setPreferences(result); setPreferencesSaved(true); }
    } catch (cause) {
      if (!isCurrent()) return;
      if (isUnauthorized(cause)) handleUnauthorized(); else setPreferencesError("Не удалось сохранить настройку. Повторите попытку");
    } finally { if (isCurrent()) { preferenceRequest.current = null; setPreferencesBusy(false); } }
  }

  async function loadUsage(): Promise<void> {
    const generation = sessionGeneration.current(); const epoch = ++usageLoadEpoch.current;
    const isCurrent = () => sessionGeneration.isCurrent(generation) && usageLoadEpoch.current === epoch;
    setUsageBusy(true); setUsageError(false); setUsageReport(null);
    try {
      const result = await portalApi.usage(usageDays);
      if (!isCurrent()) return;
      if (result.days !== usageDays) { setUsageError(true); return; }
      setUsageReport(result);
    } catch (error) {
      if (!isCurrent()) return;
      if (isUnauthorized(error)) handleUnauthorized(); else setUsageError(true);
    } finally { if (isCurrent()) setUsageBusy(false); }
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
        observe(portalApi.devices()),
      ]);
      if (!isCurrentLoad()) {
        return;
      }
      setTrial(nextTrial);
      setSubscriptions(nextSubscriptions);
      setProfiles(nextProfiles);
      operationRequests.restore(nextProfiles);
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
    usageLoadEpoch.current += 1;
    preferencesEpoch.current += 1;
    preferenceRequest.current = null;
    trialRequestInFlight.current = false;
  }, [sessionGeneration]);

  useEffect(() => { if (me) void loadUsage(); }, [me, usageDays]);

  useEffect(() => {
    if (me && activeSection === "account" && !preferences && !preferencesError) void loadPreferences();
    // Section changes may trigger the first read, but never reset a session-owned save.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [me, activeSection]);

  useEffect(() => {
    const keys = Object.keys(deviceRequests).filter((key) => {
      const entry = deviceRequests[key];
      return !entry.definitive && !entry.busy && entry.operation && (operationPolls.current[entry.requestId] ?? 0) < 30;
    });
    if (!keys.length || !me) return;
    const timer = window.setTimeout(() => {
      for (const key of keys) {
        const entry = deviceRequests[key]; operationPolls.current[entry.requestId] = (operationPolls.current[entry.requestId] ?? 0) + 1;
        void refreshDeviceOperation(key);
      }
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [deviceRequests, me]);

  const notice = bootstrapResult?.config?.notice;
  useEffect(() => {
    if (!notice) return;
    let timer: number;
    const check = () => {
      window.clearTimeout(timer);
      const now = Date.now(); setNoticeNow(now);
      const remaining = Date.parse(notice.ends_at) - now;
      if (Number.isFinite(remaining) && remaining > 0) timer = window.setTimeout(check, Math.min(60000, Math.max(1, remaining)));
    };
    check(); document.addEventListener("visibilitychange", check);
    return () => { window.clearTimeout(timer); document.removeEventListener("visibilitychange", check); };
  }, [notice]);

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

  const usage: PortalUsageProps = {days:usageDays,report:usageReport,busy:usageBusy,error:usageError,onDaysChange:setUsageDays,onRetry:() => void loadUsage()};
  const pendingDeviceIds = profiles.filter((device) => deviceConnectionBlocked(device, deviceRequests)).map(({id}) => id);
  return (
    <div className="portal-shell portal-cabinet">
      <header className="cabinet-header">
        <a className="cabinet-brand" href="#home" aria-label="Veltrix VPN"><img className="cabinet-brand-mark" src="/brand/brand-mark.webp" alt="" /><VeltrixMark className="cabinet-flat-mark" withName={false} decorative /><strong>Veltrix VPN</strong></a>
        <a className="cabinet-account" href="#account" aria-label={`Аккаунт: ${me.display_name}`}><span aria-hidden="true">{portalInitials(me.display_name)}</span></a>
      </header>
      <main className="cabinet-content">
        {notice && portalNoticeVisible(notice, noticeNow) && <aside className="cabinet-notice vx-matte" aria-labelledby="cabinet-notice-title"><h2 id="cabinet-notice-title">{notice.title}</h2><p>{notice.body}</p></aside>}
        {activeSection === "home" && <PortalHome
          busy={dataBusy} error={dataError} subscriptions={subscriptions} profiles={profiles}
          selectedDeviceId={selectedDeviceId} onSelectDevice={setSelectedDeviceId} profileOperations={profileOperations}
          pendingDeviceIds={pendingDeviceIds} usage={usage} onAddDevice={openCreate}
          onLocationStateChange={locationState}
          plans={plans} plansBusy={plansBusy} plansError={plansError} showPlans={activeAnchor === "plans"}
          onRetry={() => void loadPrivateData()} onRetryPlans={() => void loadPlans()}
          trial={trial} trialBusy={trialBusy || dataBusy} trialError={trialError} sessionGeneration={sessionGeneration}
          onActivateTrial={() => void activateTrial()} onRefreshTrial={() => void refreshTrial()} onUnauthorized={handleUnauthorized}
        />}
        {activeSection === "devices" && <ProfilesSection
          busy={dataBusy} error={dataError} profiles={profiles} subscriptions={subscriptions} csrfToken={me.csrf_token}
          sessionGeneration={sessionGeneration} onRetry={() => void loadPrivateData()} onUnauthorized={handleUnauthorized}
          profileOperations={profileOperations} onRenameStart={(id) => setProfileRenamePending(id, true)}
          onRenameSettled={(id) => setProfileRenamePending(id, false)} onLocationMutationStart={(id) => { invalidateDeviceLoad(); invalidateConnection(id); }}
          onLocationStateChange={locationState} requests={deviceRequests} pendingDeviceIds={pendingDeviceIds}
          onDeviceAction={submitDeviceAction} onRefreshOperation={(key) => void refreshDeviceOperation(key)} onAddDevice={openCreate} usage={usage} onConnect={setSelectedDeviceId}
          creationForm={showCreate ? <DeviceCreateForm draft={deviceDraft} onDraft={setDeviceDraft} subscriptions={subscriptions} request={deviceRequests.create ?? null} sessionGeneration={sessionGeneration} onUnauthorized={handleUnauthorized} onSubmit={submitDeviceAction} onRefresh={(key) => void refreshDeviceOperation(key)} onClose={() => setShowCreate(false)} /> : undefined}
          onLocationReady={() => void loadPrivateData(true)} onProfileChange={(updated) => {
            invalidateDeviceLoad();
            setProfiles((current) => current.map((item) => item.id === updated.id ? updated : item));
          }}
        />}
        {activeSection === "help" && <PortalHelp supportUrl={bootstrapResult.config?.support_url ?? null} supportText={bootstrapResult.config?.support_text ?? ""} />}
        {activeSection === "account" && <PortalAccount
          displayName={me.display_name} remindersEnabled={bootstrapResult.config?.reminders_enabled ?? false}
          preferences={preferences} preferencesBusy={preferencesBusy} preferencesError={preferencesError} preferencesSaved={preferencesSaved}
          onPreferenceChange={(value) => void savePreferences(value)} onPreferencesRetry={() => void loadPreferences()}
          plans={plans} plansBusy={plansBusy} plansError={plansError} onPlansRetry={() => void loadPlans()} onLogout={startLogout}
        />}
      </main>
      <PortalNavigation active={activeSection} />
    </div>
  );
}
