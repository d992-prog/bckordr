import { FormEvent, useEffect, useMemo, useState } from "react";

import {
  api,
  AttackEvent,
  AttackRun,
  ContactProfile,
  ContactProfilePrefill,
  DiagnosticTelegramSettings,
  DomainOverrideRule,
  DomainOverrideRulePhase,
  DomainOverrideSettings,
  DropDomain,
  Overview,
  RegistrarAccount,
  SessionResponse,
  StrategyPreview,
  WorkerNode,
  WorkerTask,
  ZoneRule,
  ZoneRulePhase,
  ZoneStrategy,
} from "./api";

type Toast = { type: "success" | "error"; text: string } | null;
type Tab = "domains" | "strategies" | "workers" | "accounts" | "contacts" | "attacks" | "settings";

const DEFAULT_DOMAIN_FORM = {
  domainsText: "",
  dropDate: "",
  zone: "fr",
  timezoneName: "Europe/Paris",
  registrarSlug: "gandi",
  zoneStrategyId: "",
  strategyMode: "inherit_zone",
  registrarAccountId: "",
  contactProfileId: "",
  priority: "100",
  requestedDurationYears: "1",
  registrationExtraParameters: "",
  attackEnabled: true,
  overrideMinGuaranteedRps: "",
  windowStartMinute: "31",
  windowStartSecond: "59",
  windowDurationSeconds: "61",
  notes: "",
};

const DEFAULT_STRATEGY_FORM = {
  zone: "fr",
  name: "France Default",
  timezoneName: "Europe/Paris",
  ruleResolutionMode: "priority",
  defaultMinGuaranteedRps: "1",
  defaultRegistrarSlug: "gandi",
  isActive: true,
  notes: "",
};

const DEFAULT_RULE_FORM = {
  name: "",
  scheduleType: "hourly",
  hour: "",
  minute: "31",
  second: "59",
  weekdays: "",
  specificDate: "",
  windowDurationSeconds: "61",
  priority: "100",
  executionProfileMode: "flat",
  isEnabled: true,
  notes: "",
};

const DEFAULT_PHASE_FORM = {
  ruleId: "",
  name: "burst",
  sortOrder: "0",
  startOffsetSeconds: "0",
  durationSeconds: "0",
  rpsMode: "percent",
  rpsValue: "100",
  stopOnSuccess: true,
};

const DEFAULT_DOMAIN_OVERRIDE_FORM = {
  timezoneName: "Europe/Paris",
  ruleResolutionMode: "priority",
  defaultMinGuaranteedRps: "1",
  notes: "",
};

const DEFAULT_DOMAIN_OVERRIDE_RULE_FORM = {
  name: "",
  scheduleType: "hourly",
  hour: "",
  minute: "31",
  second: "59",
  weekdays: "",
  specificDate: "",
  windowDurationSeconds: "61",
  priority: "100",
  executionProfileMode: "flat",
  isEnabled: true,
  notes: "",
};

const DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM = {
  ruleId: "",
  name: "burst",
  sortOrder: "0",
  startOffsetSeconds: "0",
  durationSeconds: "0",
  rpsMode: "percent",
  rpsValue: "100",
  stopOnSuccess: true,
};

const DEFAULT_WORKER_FORM = {
  name: "",
  registrarSlug: "gandi",
  assignedRegistrarAccountId: "",
  apiBaseUrl: "",
  controlToken: "",
  status: "provisioning",
  ipAddress: "",
  region: "",
  maxRps: "16",
  targetRps: "16",
  currentRps: "0",
  currentCapacityRps: "0",
  cpuLoad: "0",
  ramUsagePercent: "0",
  clockDriftMs: "0",
  notes: "",
};

const DEFAULT_ACCOUNT_FORM = {
  name: "",
  registrarSlug: "gandi",
  apiToken: "",
  apiBaseUrl: "",
  sharingId: "",
  defaultContactProfileId: "",
  supportsDryRun: true,
  isActive: true,
  notes: "",
};

const DEFAULT_CONTACT_FORM = {
  label: "",
  personType: "individual",
  givenName: "",
  familyName: "",
  organizationName: "",
  email: "",
  phone: "",
  mobile: "",
  fax: "",
  lang: "fr",
  streetAddress: "",
  city: "",
  state: "",
  zipCode: "",
  countryCode: "FR",
  dataObfuscated: false,
  mailObfuscated: false,
  icannContractAccept: true,
  extraParameters: "",
  isDefault: false,
  notes: "",
};

function makeWorkerForm(worker?: WorkerNode | null) {
  if (!worker) {
    return { ...DEFAULT_WORKER_FORM };
  }
  return {
    name: worker.name,
    registrarSlug: worker.registrar_slug,
    assignedRegistrarAccountId: worker.assigned_registrar_account_id ? String(worker.assigned_registrar_account_id) : "",
    apiBaseUrl: worker.api_base_url ?? "",
    controlToken: worker.control_token ?? "",
    status: worker.status,
    ipAddress: worker.ip_address ?? "",
    region: worker.region ?? "",
    maxRps: String(worker.max_rps),
    targetRps: String(worker.target_rps),
    currentRps: String(worker.current_rps),
    currentCapacityRps: String(worker.current_capacity_rps),
    cpuLoad: String(worker.cpu_load),
    ramUsagePercent: String(worker.ram_usage_percent),
    clockDriftMs: String(worker.clock_drift_ms),
    notes: worker.notes ?? "",
  };
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "—";
  }
  return new Intl.DateTimeFormat("ru-RU", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(value));
}

function statusClass(value: string) {
  if (["ready", "success", "scheduled"].includes(value)) {
    return "status available";
  }
  if (["running", "attacking", "busy", "planned"].includes(value)) {
    return "status checking";
  }
  if (["invalid", "error", "failed", "stopped", "cancelled", "offline"].includes(value)) {
    return "status error";
  }
  return "status inactive";
}

function parseNumber(value: string) {
  if (!value.trim()) {
    return null;
  }
  return Number(value);
}

function formatRps(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "—";
  }
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function splitDomains(value: string) {
  return value
    .split(/[\r\n,;\t ]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

export default function App() {
  const [tab, setTab] = useState<Tab>("domains");
  const [session, setSession] = useState<SessionResponse | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [toast, setToast] = useState<Toast>(null);

  const [overview, setOverview] = useState<Overview | null>(null);
  const [strategies, setStrategies] = useState<ZoneStrategy[]>([]);
  const [strategyRules, setStrategyRules] = useState<ZoneRule[]>([]);
  const [rulePhases, setRulePhases] = useState<Record<number, ZoneRulePhase[]>>({});
  const [strategyPreview, setStrategyPreview] = useState<StrategyPreview | null>(null);
  const [domains, setDomains] = useState<DropDomain[]>([]);
  const [workers, setWorkers] = useState<WorkerNode[]>([]);
  const [accounts, setAccounts] = useState<RegistrarAccount[]>([]);
  const [contacts, setContacts] = useState<ContactProfile[]>([]);
  const [attacks, setAttacks] = useState<AttackRun[]>([]);
  const [tasks, setTasks] = useState<WorkerTask[]>([]);
  const [events, setEvents] = useState<AttackEvent[]>([]);

  const [loginForm, setLoginForm] = useState({ username: "", password: "", remember_me: true });
  const [domainForm, setDomainForm] = useState(DEFAULT_DOMAIN_FORM);
  const [strategyForm, setStrategyForm] = useState(DEFAULT_STRATEGY_FORM);
  const [ruleForm, setRuleForm] = useState(DEFAULT_RULE_FORM);
  const [phaseForm, setPhaseForm] = useState(DEFAULT_PHASE_FORM);
  const [selectedStrategyId, setSelectedStrategyId] = useState<number | null>(null);
  const [previewDate, setPreviewDate] = useState(new Date().toISOString().slice(0, 10));
  const [selectedOverrideDomainId, setSelectedOverrideDomainId] = useState<number | null>(null);
  const [domainOverrideSettings, setDomainOverrideSettings] = useState<DomainOverrideSettings | null>(null);
  const [domainOverrideRules, setDomainOverrideRules] = useState<DomainOverrideRule[]>([]);
  const [domainOverridePhases, setDomainOverridePhases] = useState<Record<number, DomainOverrideRulePhase[]>>({});
  const [domainOverridePreview, setDomainOverridePreview] = useState<StrategyPreview | null>(null);
  const [domainOverrideForm, setDomainOverrideForm] = useState(DEFAULT_DOMAIN_OVERRIDE_FORM);
  const [domainOverrideRuleForm, setDomainOverrideRuleForm] = useState(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
  const [domainOverridePhaseForm, setDomainOverridePhaseForm] = useState(DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM);
  const [workerForm, setWorkerForm] = useState(DEFAULT_WORKER_FORM);
  const [editingWorkerId, setEditingWorkerId] = useState<number | null>(null);
  const [accountForm, setAccountForm] = useState(DEFAULT_ACCOUNT_FORM);
  const [contactForm, setContactForm] = useState(DEFAULT_CONTACT_FORM);
  const [passwordForm, setPasswordForm] = useState({ current_password: "", new_password: "" });
  const [telegramForm, setTelegramForm] = useState({ telegram_token: "", telegram_chat_id: "" });
  const [diagnosticTelegram, setDiagnosticTelegram] = useState<DiagnosticTelegramSettings>({
    telegram_token: "",
    telegram_chat_id: "",
  });
  const [importFile, setImportFile] = useState<File | null>(null);

  const accountMap = useMemo(() => new Map(accounts.map((item) => [item.id, item])), [accounts]);
  const contactMap = useMemo(() => new Map(contacts.map((item) => [item.id, item])), [contacts]);
  const domainMap = useMemo(() => new Map(domains.map((item) => [item.id, item])), [domains]);
  const strategyMap = useMemo(() => new Map(strategies.map((item) => [item.id, item])), [strategies]);
  const selectedStrategy = useMemo(
    () => strategies.find((item) => item.id === selectedStrategyId) ?? null,
    [selectedStrategyId, strategies],
  );
  const manualOverrideDomains = useMemo(
    () => domains.filter((item) => item.strategy_mode === "manual_override"),
    [domains],
  );
  const selectedOverrideDomain = useMemo(
    () => manualOverrideDomains.find((item) => item.id === selectedOverrideDomainId) ?? null,
    [manualOverrideDomains, selectedOverrideDomainId],
  );
  const matchingDomainStrategies = useMemo(
    () => strategies.filter((item) => item.zone.toLowerCase() === domainForm.zone.trim().toLowerCase()),
    [domainForm.zone, strategies],
  );

  useEffect(() => {
    let mounted = true;
    api
      .getSession()
      .then((payload) => {
        if (!mounted) {
          return;
        }
        setSession(payload);
        setTelegramForm({
          telegram_token: payload.user.telegram_token ?? "",
          telegram_chat_id: payload.user.telegram_chat_id ?? "",
        });
      })
      .catch(() => {
        if (mounted) {
          setSession(null);
        }
      })
      .finally(() => {
        if (mounted) {
          setAuthLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, []);

  useEffect(() => {
    if (!session) {
      return;
    }
    void loadAll();
    const timer = window.setInterval(() => {
      void loadAll({ silent: true });
    }, 15000);
    return () => window.clearInterval(timer);
  }, [session?.user.id]);

  useEffect(() => {
    if (!selectedStrategyId && strategies.length > 0) {
      setSelectedStrategyId(strategies[0].id);
    }
  }, [selectedStrategyId, strategies]);

  useEffect(() => {
    if (!selectedStrategyId || !session) {
      return;
    }
    void loadStrategyDetails(selectedStrategyId, previewDate);
  }, [selectedStrategyId, previewDate, session?.user.id]);

  useEffect(() => {
    if (!selectedOverrideDomainId && manualOverrideDomains.length > 0) {
      setSelectedOverrideDomainId(manualOverrideDomains[0].id);
      return;
    }
    if (selectedOverrideDomainId && !manualOverrideDomains.some((item) => item.id === selectedOverrideDomainId)) {
      setSelectedOverrideDomainId(manualOverrideDomains[0]?.id ?? null);
    }
  }, [manualOverrideDomains, selectedOverrideDomainId]);

  useEffect(() => {
    if (!session || !selectedOverrideDomainId) {
      setDomainOverrideSettings(null);
      setDomainOverrideRules([]);
      setDomainOverridePhases({});
      setDomainOverridePreview(null);
      return;
    }
    void loadDomainOverrideDetails(selectedOverrideDomainId, previewDate);
  }, [previewDate, selectedOverrideDomainId, session?.user.id]);

  async function loadAll(options?: { silent?: boolean }) {
    try {
      const [
        overviewData,
        strategiesData,
        domainsData,
        workersData,
        accountsData,
        contactsData,
        attacksData,
        tasksData,
        eventsData,
        diagnosticData,
      ] = await Promise.all([
        api.getOverview(),
        api.getZoneStrategies(),
        api.getDomains(),
        api.getWorkers(),
        api.getRegistrarAccounts(),
        api.getContactProfiles(),
        api.getAttacks(),
        api.getTasks(),
        api.getEvents(),
        api.getDiagnosticTelegram(),
      ]);
      setOverview(overviewData);
      setStrategies(strategiesData);
      setDomains(domainsData);
      setWorkers(workersData);
      setAccounts(accountsData);
      setContacts(contactsData);
      setAttacks(attacksData);
      setTasks(tasksData);
      setEvents(eventsData);
      setDiagnosticTelegram(diagnosticData);
    } catch (error) {
      if (!options?.silent) {
        setToast({ type: "error", text: error instanceof Error ? error.message : "Не удалось загрузить данные control-панели" });
      }
    }
  }

  async function loadStrategyDetails(strategyId: number, targetDate: string) {
    try {
      const rules = await api.getZoneRules(strategyId);
      setStrategyRules(rules);
      const phasesByRuleEntries = await Promise.all(
        rules.map(async (rule) => [rule.id, await api.getZoneRulePhases(rule.id)] as const),
      );
      setRulePhases(Object.fromEntries(phasesByRuleEntries));
      setStrategyPreview(await api.previewZoneStrategy(strategyId, targetDate));
      setPhaseForm((current) => ({
        ...current,
        ruleId: current.ruleId || (rules[0] ? String(rules[0].id) : ""),
      }));
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Strategy details error" });
    }
  }

  function syncDomainOverrideForm(domain: DropDomain, settings?: DomainOverrideSettings | null) {
    setDomainOverrideForm({
      timezoneName: settings?.timezone_name ?? domain.timezone_name,
      ruleResolutionMode: settings?.rule_resolution_mode ?? "priority",
      defaultMinGuaranteedRps: String(settings?.default_min_guaranteed_rps ?? domain.override_min_guaranteed_rps ?? 1),
      notes: settings?.notes ?? domain.notes ?? "",
    });
  }

  async function loadDomainOverrideDetails(domainId: number, targetDate: string) {
    const domain = domains.find((item) => item.id === domainId);
    if (!domain) {
      return;
    }
    try {
      const settings = await api.getDomainOverride(domainId);
      const rules = await api.getDomainOverrideRules(domainId);
      const phasesByRuleEntries = await Promise.all(
        rules.map(async (rule) => [rule.id, await api.getDomainOverrideRulePhases(rule.id)] as const),
      );
      const preview = await api.previewDomainOverride(domainId, targetDate);
      setDomainOverrideSettings(settings);
      setDomainOverrideRules(rules);
      setDomainOverridePhases(Object.fromEntries(phasesByRuleEntries));
      setDomainOverridePreview(preview);
      syncDomainOverrideForm(domain, settings);
      setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
      setDomainOverridePhaseForm((current) => ({
        ...DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM,
        ruleId: current.ruleId || (rules[0] ? String(rules[0].id) : ""),
      }));
    } catch (error) {
      if (error instanceof Error && error.message.includes("Domain override not found")) {
        setDomainOverrideSettings(null);
        setDomainOverrideRules([]);
        setDomainOverridePhases({});
        setDomainOverridePreview(null);
        syncDomainOverrideForm(domain, null);
        setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
        setDomainOverridePhaseForm(DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM);
        return;
      }
      setToast({ type: "error", text: error instanceof Error ? error.message : "Domain override error" });
    }
  }

  async function submitLogin(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.login(loginForm);
      setSession(payload);
      setTelegramForm({
        telegram_token: payload.user.telegram_token ?? "",
        telegram_chat_id: payload.user.telegram_chat_id ?? "",
      });
      setToast({ type: "success", text: "Вход выполнен" });
      await loadAll();
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка входа" });
    }
  }

  async function logout() {
    await api.logout();
    setSession(null);
    setOverview(null);
    setStrategies([]);
    setDomains([]);
    setWorkers([]);
    setAccounts([]);
    setContacts([]);
    setAttacks([]);
    setTasks([]);
    setEvents([]);
  }

  function renderAuth() {
    return (
      <div className="auth-shell">
        <section className="hero">
          <div>
            <p className="eyebrow">Control Server</p>
            <h1>Domain Drop Catcher</h1>
            <p className="subtitle">
              Панель управления для доменов с известной датой дропа, workers-серверов, аккаунтов регистраторов и
              атакующих окон по времени реестра.
            </p>
          </div>
          <div className="stats">
            <article><span>Режим</span><strong>Control</strong></article>
            <article><span>Стратегия</span><strong>Priority</strong></article>
            <article><span>Zone Default</span><strong>.fr</strong></article>
            <article><span>Окно</span><strong>31:59</strong></article>
          </div>
        </section>

        <section className="auth-grid">
          <div className="auth-panel">
            <h2>Вход</h2>
            <form className="form" onSubmit={submitLogin}>
              <label>
                <span>Логин</span>
                <input value={loginForm.username} onChange={(event) => setLoginForm((current) => ({ ...current, username: event.target.value }))} />
              </label>
              <label>
                <span>Пароль</span>
                <input type="password" value={loginForm.password} onChange={(event) => setLoginForm((current) => ({ ...current, password: event.target.value }))} />
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={loginForm.remember_me} onChange={(event) => setLoginForm((current) => ({ ...current, remember_me: event.target.checked }))} />
                <span>Запомнить сессию</span>
              </label>
              <button type="submit">Войти</button>
            </form>
          </div>

          <div className="auth-panel">
            <h2>Регистрация</h2>
            <p className="muted">
              Окно оставлено видимым, но в текущем control-only проекте регистрация новых пользователей отключена.
            </p>
            <form className="form">
              <label><span>Логин</span><input disabled placeholder="disabled" /></label>
              <label><span>Пароль</span><input disabled placeholder="disabled" /></label>
              <button type="button" className="ghost" disabled>Регистрация отключена</button>
            </form>
          </div>
        </section>
      </div>
    );
  }

  async function submitDomains(event: FormEvent) {
    event.preventDefault();
    try {
      const payloadBase = {
        zone: domainForm.zone,
        timezone_name: domainForm.timezoneName,
        registrar_slug: domainForm.registrarSlug,
        zone_strategy_id: parseNumber(domainForm.zoneStrategyId),
        strategy_mode: domainForm.strategyMode,
        registrar_account_id: parseNumber(domainForm.registrarAccountId),
        contact_profile_id: parseNumber(domainForm.contactProfileId),
        drop_date: domainForm.dropDate,
        priority: Number(domainForm.priority),
        requested_duration_years: Number(domainForm.requestedDurationYears),
        registration_extra_parameters: domainForm.registrationExtraParameters || null,
        attack_enabled: domainForm.attackEnabled,
        override_min_guaranteed_rps: parseNumber(domainForm.overrideMinGuaranteedRps),
        window_start_minute: Number(domainForm.windowStartMinute),
        window_start_second: Number(domainForm.windowStartSecond),
        window_duration_seconds: Number(domainForm.windowDurationSeconds),
        notes: domainForm.notes || null,
      };
      const result = importFile
        ? await api.importDomains(importFile, payloadBase)
        : await api.createDomain({ domains: splitDomains(domainForm.domainsText), ...payloadBase });
      setDomainForm(DEFAULT_DOMAIN_FORM);
      setImportFile(null);
      await loadAll();
      setToast({
        type: "success",
        text: `Добавлено: ${result.inserted.length}${result.skipped.length ? `, пропущено: ${result.skipped.join(", ")}` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления доменов" });
    }
  }

  async function submitStrategy(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createZoneStrategy({
        zone: strategyForm.zone,
        name: strategyForm.name,
        timezone_name: strategyForm.timezoneName,
        rule_resolution_mode: strategyForm.ruleResolutionMode,
        default_min_guaranteed_rps: Number(strategyForm.defaultMinGuaranteedRps),
        default_registrar_slug: strategyForm.defaultRegistrarSlug,
        is_active: strategyForm.isActive,
        notes: strategyForm.notes || null,
      });
      setStrategyForm(DEFAULT_STRATEGY_FORM);
      await loadAll();
      setToast({ type: "success", text: "Zone strategy added" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Zone strategy error" });
    }
  }

  async function submitRule(event: FormEvent) {
    event.preventDefault();
    if (!selectedStrategyId) {
      setToast({ type: "error", text: "Select a zone strategy first" });
      return;
    }
    try {
      await api.createZoneRule(selectedStrategyId, {
        name: ruleForm.name,
        schedule_type: ruleForm.scheduleType,
        hour: parseNumber(ruleForm.hour),
        minute: Number(ruleForm.minute),
        second: Number(ruleForm.second),
        weekdays: ruleForm.weekdays || null,
        specific_date: ruleForm.specificDate || null,
        window_duration_seconds: Number(ruleForm.windowDurationSeconds),
        priority: Number(ruleForm.priority),
        execution_profile_mode: ruleForm.executionProfileMode,
        is_enabled: ruleForm.isEnabled,
        notes: ruleForm.notes || null,
      });
      setRuleForm(DEFAULT_RULE_FORM);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: "Zone rule added" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Zone rule error" });
    }
  }

  async function submitPhase(event: FormEvent) {
    event.preventDefault();
    const ruleId = parseNumber(phaseForm.ruleId);
    if (!ruleId || !selectedStrategyId) {
      setToast({ type: "error", text: "Choose a rule for the phase" });
      return;
    }
    try {
      await api.createZoneRulePhase(ruleId, {
        name: phaseForm.name,
        sort_order: Number(phaseForm.sortOrder),
        start_offset_seconds: Number(phaseForm.startOffsetSeconds),
        duration_seconds: Number(phaseForm.durationSeconds),
        rps_mode: phaseForm.rpsMode,
        rps_value: Number(phaseForm.rpsValue),
        stop_on_success: phaseForm.stopOnSuccess,
      });
      setPhaseForm((current) => ({ ...DEFAULT_PHASE_FORM, ruleId: current.ruleId }));
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: "Zone phase added" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Zone phase error" });
    }
  }

  async function removeZoneRule(ruleId: number) {
    if (!selectedStrategyId) {
      return;
    }
    try {
      const payload = await api.deleteZoneRule(ruleId);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Delete zone rule error" });
    }
  }

  async function removeZonePhase(phaseId: number) {
    if (!selectedStrategyId) {
      return;
    }
    try {
      const payload = await api.deleteZoneRulePhase(phaseId);
      await loadStrategyDetails(selectedStrategyId, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Delete zone phase error" });
    }
  }

  async function saveDomainOverrideSettings(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для manual override" });
      return;
    }
    try {
      await api.updateDomainOverride(selectedOverrideDomain.id, {
        timezone_name: domainOverrideForm.timezoneName,
        rule_resolution_mode: domainOverrideForm.ruleResolutionMode,
        default_min_guaranteed_rps: Number(domainOverrideForm.defaultMinGuaranteedRps),
        notes: domainOverrideForm.notes || null,
      });
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Domain override settings saved" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Domain override settings error" });
    }
  }

  async function submitDomainOverrideRule(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для override rule" });
      return;
    }
    try {
      await api.createDomainOverrideRule(selectedOverrideDomain.id, {
        name: domainOverrideRuleForm.name,
        schedule_type: domainOverrideRuleForm.scheduleType,
        hour: parseNumber(domainOverrideRuleForm.hour),
        minute: Number(domainOverrideRuleForm.minute),
        second: Number(domainOverrideRuleForm.second),
        weekdays: domainOverrideRuleForm.weekdays || null,
        specific_date: domainOverrideRuleForm.specificDate || null,
        window_duration_seconds: Number(domainOverrideRuleForm.windowDurationSeconds),
        priority: Number(domainOverrideRuleForm.priority),
        execution_profile_mode: domainOverrideRuleForm.executionProfileMode,
        is_enabled: domainOverrideRuleForm.isEnabled,
        notes: domainOverrideRuleForm.notes || null,
      });
      setDomainOverrideRuleForm(DEFAULT_DOMAIN_OVERRIDE_RULE_FORM);
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Domain override rule added" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Domain override rule error" });
    }
  }

  async function submitDomainOverridePhase(event: FormEvent) {
    event.preventDefault();
    if (!selectedOverrideDomain) {
      setToast({ type: "error", text: "Выбери домен для override phase" });
      return;
    }
    const ruleId = parseNumber(domainOverridePhaseForm.ruleId);
    if (!ruleId) {
      setToast({ type: "error", text: "Выбери override rule для фазы" });
      return;
    }
    try {
      await api.createDomainOverrideRulePhase(ruleId, {
        name: domainOverridePhaseForm.name,
        sort_order: Number(domainOverridePhaseForm.sortOrder),
        start_offset_seconds: Number(domainOverridePhaseForm.startOffsetSeconds),
        duration_seconds: Number(domainOverridePhaseForm.durationSeconds),
        rps_mode: domainOverridePhaseForm.rpsMode,
        rps_value: Number(domainOverridePhaseForm.rpsValue),
        stop_on_success: domainOverridePhaseForm.stopOnSuccess,
      });
      setDomainOverridePhaseForm((current) => ({
        ...DEFAULT_DOMAIN_OVERRIDE_PHASE_FORM,
        ruleId: current.ruleId,
      }));
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: "Domain override phase added" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Domain override phase error" });
    }
  }

  async function removeDomainOverrideRule(ruleId: number) {
    if (!selectedOverrideDomain) {
      return;
    }
    try {
      const payload = await api.deleteDomainOverrideRule(ruleId);
      await loadAll({ silent: true });
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Delete domain override rule error" });
    }
  }

  async function removeDomainOverridePhase(phaseId: number) {
    if (!selectedOverrideDomain) {
      return;
    }
    try {
      const payload = await api.deleteDomainOverrideRulePhase(phaseId);
      await loadDomainOverrideDetails(selectedOverrideDomain.id, previewDate);
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Delete domain override phase error" });
    }
  }

  async function submitWorker(event: FormEvent) {
    event.preventDefault();
    const payload = {
      name: workerForm.name,
      registrar_slug: workerForm.registrarSlug,
      assigned_registrar_account_id: parseNumber(workerForm.assignedRegistrarAccountId),
      api_base_url: workerForm.apiBaseUrl || null,
      control_token: workerForm.controlToken || null,
      status: workerForm.status,
      ip_address: workerForm.ipAddress || null,
      region: workerForm.region || null,
      max_rps: Number(workerForm.maxRps),
      target_rps: Number(workerForm.targetRps),
      current_rps: Number(workerForm.currentRps),
      current_capacity_rps: Number(workerForm.currentCapacityRps),
      cpu_load: Number(workerForm.cpuLoad),
      ram_usage_percent: Number(workerForm.ramUsagePercent),
      clock_drift_ms: Number(workerForm.clockDriftMs),
      notes: workerForm.notes || null,
    };
    try {
      if (editingWorkerId) {
        await api.updateWorker(editingWorkerId, payload);
      } else {
        await api.createWorker(payload);
      }
      setWorkerForm(makeWorkerForm());
      setEditingWorkerId(null);
      await loadAll();
      setToast({ type: "success", text: editingWorkerId ? "Worker обновлен" : "Worker добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : editingWorkerId ? "Ошибка обновления worker" : "Ошибка добавления worker" });
    }
  }

  function startEditWorker(worker: WorkerNode) {
    setEditingWorkerId(worker.id);
    setWorkerForm(makeWorkerForm(worker));
  }

  function resetWorkerForm() {
    setEditingWorkerId(null);
    setWorkerForm(makeWorkerForm());
  }

  async function submitAccount(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createRegistrarAccount({
        name: accountForm.name,
        registrar_slug: accountForm.registrarSlug,
        api_token: accountForm.apiToken || null,
        api_base_url: accountForm.apiBaseUrl || null,
        sharing_id: accountForm.sharingId || null,
        default_contact_profile_id: parseNumber(accountForm.defaultContactProfileId),
        supports_dry_run: accountForm.supportsDryRun,
        is_active: accountForm.isActive,
        notes: accountForm.notes || null,
      });
      setAccountForm(DEFAULT_ACCOUNT_FORM);
      await loadAll();
      setToast({ type: "success", text: "Аккаунт регистратора добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления аккаунта" });
    }
  }

  async function submitContact(event: FormEvent) {
    event.preventDefault();
    try {
      await api.createContactProfile({
        label: contactForm.label,
        person_type: contactForm.personType,
        given_name: contactForm.givenName,
        family_name: contactForm.familyName,
        organization_name: contactForm.organizationName || null,
        email: contactForm.email,
        phone: contactForm.phone,
        mobile: contactForm.mobile || null,
        fax: contactForm.fax || null,
        lang: contactForm.lang || null,
        street_address: contactForm.streetAddress,
        city: contactForm.city,
        state: contactForm.state || null,
        zip_code: contactForm.zipCode,
        country_code: contactForm.countryCode,
        data_obfuscated: contactForm.dataObfuscated,
        mail_obfuscated: contactForm.mailObfuscated,
        icann_contract_accept: contactForm.icannContractAccept,
        extra_parameters: contactForm.extraParameters || null,
        is_default: contactForm.isDefault,
        notes: contactForm.notes || null,
      });
      setContactForm(DEFAULT_CONTACT_FORM);
      await loadAll();
      setToast({ type: "success", text: "Contact profile добавлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка добавления contact profile" });
    }
  }

  async function saveTelegram(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.updateTelegram(telegramForm);
      setSession(payload);
      setToast({ type: "success", text: "Личный Telegram обновлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения Telegram" });
    }
  }

  async function saveDiagnosticTelegram(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.updateDiagnosticTelegram(diagnosticTelegram);
      setDiagnosticTelegram(payload);
      setToast({ type: "success", text: "Диагностический Telegram обновлен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка сохранения diagnostic Telegram" });
    }
  }

  async function savePassword(event: FormEvent) {
    event.preventDefault();
    try {
      const payload = await api.changePassword(passwordForm);
      setSession(payload);
      setPasswordForm({ current_password: "", new_password: "" });
      setToast({ type: "success", text: "Пароль изменен" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка смены пароля" });
    }
  }

  async function startTodayAttacks(force_rebuild = false) {
    try {
      const payload = await api.startAttacks({ force_rebuild });
      await loadAll();
      setToast({ type: "success", text: `Спланировано атак: ${payload.length}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка старта атак" });
    }
  }

  async function startDomainAttack(domainId: number) {
    try {
      await api.startAttacks({ domain_ids: [domainId], force_rebuild: true });
      await loadAll();
      setToast({ type: "success", text: "Атака по домену перепланирована" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка старта атаки" });
    }
  }

  async function stopAllAttacks() {
    try {
      const payload = await api.stopAttacks({ reason: "Остановлено из панели" });
      await loadAll();
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка остановки атак" });
    }
  }

  async function rebalanceAttacksNow() {
    try {
      const payload = await api.rebalanceAttacks();
      await loadAll();
      setToast({ type: "success", text: payload.detail });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка перераспределения" });
    }
  }

  async function toggleDomain(domain: DropDomain) {
    try {
      await api.updateDomain(domain.id, {
        attack_enabled: !domain.attack_enabled,
        status: !domain.attack_enabled ? "queued" : "paused",
      });
      await loadAll();
      setToast({ type: "success", text: `Домен ${!domain.attack_enabled ? "включен" : "поставлен на паузу"}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления домена" });
    }
  }

  async function toggleWorker(worker: WorkerNode) {
    try {
      await api.updateWorker(worker.id, {
        is_enabled: !worker.is_enabled,
        status: !worker.is_enabled ? "ready" : "disabled",
      });
      await loadAll();
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка обновления worker" });
    }
  }

  async function validateAccount(id: number) {
    try {
      const payload = await api.validateRegistrarAccount(id);
      await loadAll();
      setToast({
        type: payload.last_validation_status === "ready" ? "success" : "error",
        text: payload.last_validation_message ?? "Проверка завершена",
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка проверки аккаунта" });
    }
  }

  async function prefillContactFromAccount(account: RegistrarAccount) {
    try {
      const payload = await api.prefillContactFromRegistrarAccount(account.id);
      applyPrefilledContact(payload);
      setTab("contacts");
      setToast({ type: "success", text: `Contact draft imported from ${account.name}` });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка prefill из Gandi" });
    }
  }

  async function dryRunDomain(domain: DropDomain) {
    try {
      const payload = await api.dryRunDomain(domain.id);
      await loadAll();
      setToast({
        type: payload.status === "ready" ? "success" : "error",
        text: `Dry-run ${domain.fqdn}: ${payload.status}${payload.http_status ? ` (HTTP ${payload.http_status})` : ""}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка dry-run проверки домена" });
    }
  }

  async function dryRunReadyDueTodayDomains() {
    try {
      const payload = await api.dryRunDomainsBatch({ due_today_only: true, only_ready: true });
      await loadAll();
      setToast({
        type: payload.error === 0 && payload.invalid === 0 ? "success" : "error",
        text: `Dry run batch: total ${payload.total}, ready ${payload.ready}, invalid ${payload.invalid}, error ${payload.error}`,
      });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка batch dry-run" });
    }
  }

  async function deleteItem(kind: "domain" | "worker" | "account" | "contact", id: number) {
    try {
      if (kind === "domain") {
        await api.deleteDomain(id);
      }
      if (kind === "worker") {
        await api.deleteWorker(id);
      }
      if (kind === "account") {
        await api.deleteRegistrarAccount(id);
      }
      if (kind === "contact") {
        await api.deleteContactProfile(id);
      }
      await loadAll();
      setToast({ type: "success", text: "Удалено" });
    } catch (error) {
      setToast({ type: "error", text: error instanceof Error ? error.message : "Ошибка удаления" });
    }
  }

  function formatDomainReadiness(domain: DropDomain) {
    if (!domain.readiness_reasons) {
      return "ready";
    }
    return domain.readiness_reasons;
  }

  function applyPrefilledContact(payload: ContactProfilePrefill) {
    setContactForm({
      label: payload.label,
      personType: payload.person_type,
      givenName: payload.given_name,
      familyName: payload.family_name,
      organizationName: payload.organization_name ?? "",
      email: payload.email,
      phone: payload.phone,
      mobile: payload.mobile ?? "",
      fax: payload.fax ?? "",
      lang: payload.lang ?? "fr",
      streetAddress: payload.street_address,
      city: payload.city,
      state: payload.state ?? "",
      zipCode: payload.zip_code,
      countryCode: payload.country_code,
      dataObfuscated: payload.data_obfuscated ?? false,
      mailObfuscated: payload.mail_obfuscated ?? false,
      icannContractAccept: payload.icann_contract_accept ?? true,
      extraParameters: payload.extra_parameters ?? "",
      isDefault: payload.is_default,
      notes: payload.notes ?? "",
    });
  }

  function renderDomains() {
    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Домены дропа</h2>
              <p className="muted">У каждого домена обязательна дата дропа. Внутри дня атака работает только по окну зоны.</p>
            </div>
            <div className="actions">
              <button type="button" className="ghost" onClick={() => void dryRunReadyDueTodayDomains()}>Dry run due today</button>
              <button type="button" onClick={() => void startTodayAttacks(false)}>Старт due today</button>
              <button type="button" className="ghost" onClick={() => void startTodayAttacks(true)}>Перестроить атаки</button>
            </div>
          </div>

          <form className="form" onSubmit={submitDomains}>
            <p className="muted">
              `inherit_zone` использует общую стратегию зоны. `manual_override` переводит домен на собственные
              rules/phases, которые дальше настраиваются ниже в этой вкладке.
            </p>
            <label>
              <span>Домены по одному или пачкой</span>
              <textarea
                rows={5}
                value={domainForm.domainsText}
                onChange={(event) => setDomainForm((current) => ({ ...current, domainsText: event.target.value }))}
                placeholder="example.fr&#10;example2.fr"
              />
            </label>
            <label>
              <span>Или импорт файла csv/txt/xlsx</span>
              <input type="file" accept=".txt,.csv,.xlsx" onChange={(event) => setImportFile(event.target.files?.[0] ?? null)} />
            </label>
            <div className="form two-columns">
              <label><span>Дата дропа</span><input type="date" value={domainForm.dropDate} onChange={(event) => setDomainForm((current) => ({ ...current, dropDate: event.target.value }))} /></label>
              <label><span>Приоритет</span><input value={domainForm.priority} onChange={(event) => setDomainForm((current) => ({ ...current, priority: event.target.value }))} /></label>
              <label><span>Zone</span><input value={domainForm.zone} onChange={(event) => setDomainForm((current) => ({ ...current, zone: event.target.value }))} /></label>
              <label><span>Timezone</span><input value={domainForm.timezoneName} onChange={(event) => setDomainForm((current) => ({ ...current, timezoneName: event.target.value }))} /></label>
              <label><span>Registrar</span><input value={domainForm.registrarSlug} onChange={(event) => setDomainForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
              <label><span>Duration years</span><input value={domainForm.requestedDurationYears} onChange={(event) => setDomainForm((current) => ({ ...current, requestedDurationYears: event.target.value }))} /></label>
              <label>
                <span>Strategy mode</span>
                <select value={domainForm.strategyMode} onChange={(event) => setDomainForm((current) => ({ ...current, strategyMode: event.target.value }))}>
                  <option value="inherit_zone">inherit_zone</option>
                  <option value="manual_override">manual_override</option>
                </select>
              </label>
              <label>
                <span>Zone strategy</span>
                <select value={domainForm.zoneStrategyId} onChange={(event) => setDomainForm((current) => ({ ...current, zoneStrategyId: event.target.value }))}>
                  <option value="">Auto by zone</option>
                  {matchingDomainStrategies.map((strategy) => <option key={strategy.id} value={strategy.id}>{strategy.zone} | {strategy.name}</option>)}
                </select>
              </label>
              <label>
                <span>Registrar account</span>
                <select value={domainForm.registrarAccountId} onChange={(event) => setDomainForm((current) => ({ ...current, registrarAccountId: event.target.value }))}>
                  <option value="">Автовыбор</option>
                  {accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}
                </select>
              </label>
              <label>
                <span>Contact profile</span>
                <select value={domainForm.contactProfileId} onChange={(event) => setDomainForm((current) => ({ ...current, contactProfileId: event.target.value }))}>
                  <option value="">Автовыбор</option>
                  {contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.label}</option>)}
                </select>
              </label>
              <label><span>Min guaranteed RPS override</span><input value={domainForm.overrideMinGuaranteedRps} onChange={(event) => setDomainForm((current) => ({ ...current, overrideMinGuaranteedRps: event.target.value }))} placeholder="empty = use zone default" /></label>
              <label><span>Window minute</span><input value={domainForm.windowStartMinute} onChange={(event) => setDomainForm((current) => ({ ...current, windowStartMinute: event.target.value }))} /></label>
              <label><span>Window second</span><input value={domainForm.windowStartSecond} onChange={(event) => setDomainForm((current) => ({ ...current, windowStartSecond: event.target.value }))} /></label>
              <label><span>Window duration sec</span><input value={domainForm.windowDurationSeconds} onChange={(event) => setDomainForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
              <label className="checkbox"><input type="checkbox" checked={domainForm.attackEnabled} onChange={(event) => setDomainForm((current) => ({ ...current, attackEnabled: event.target.checked }))} /><span>Атака активна</span></label>
            </div>
            <label>
              <span>Gandi registration extra parameters (JSON)</span>
              <textarea
                rows={3}
                value={domainForm.registrationExtraParameters}
                onChange={(event) => setDomainForm((current) => ({ ...current, registrationExtraParameters: event.target.value }))}
                placeholder='{"fr_lock": true}'
              />
            </label>
            <label><span>Заметки</span><textarea rows={3} value={domainForm.notes} onChange={(event) => setDomainForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить домены</button>
          </form>
        </div>

        <div className="card">
          <h2>Активный пул</h2>
          <div className="key-value compact">
            <div><span>Всего доменов</span><strong>{overview?.total_domains ?? 0}</strong></div>
            <div><span>Due today</span><strong>{overview?.due_today_domains ?? 0}</strong></div>
            <div><span>Сейчас в атаке</span><strong>{overview?.active_attack_domains ?? 0}</strong></div>
            <div><span>Успешно сегодня</span><strong>{overview?.success_today_domains ?? 0}</strong></div>
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Domain Manual Override</h2>
              <p className="muted">Для доменов в режиме `manual_override` здесь задаются собственные settings, rules, phases и preview.</p>
            </div>
            <div className="actions">
              <label>
                <span>Preview date</span>
                <input type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} />
              </label>
              <button type="button" className="ghost" onClick={() => selectedOverrideDomainId ? void loadDomainOverrideDetails(selectedOverrideDomainId, previewDate) : undefined}>Обновить override</button>
            </div>
          </div>

          <div className="form two-columns">
            <label>
              <span>Manual override domain</span>
              <select value={selectedOverrideDomainId ?? ""} onChange={(event) => setSelectedOverrideDomainId(event.target.value ? Number(event.target.value) : null)}>
                <option value="">Выбери домен</option>
                {manualOverrideDomains.map((domain) => (
                  <option key={domain.id} value={domain.id}>
                    {domain.fqdn} | {domain.drop_date} | {domain.status}
                  </option>
                ))}
              </select>
            </label>
            <div className="key-value compact">
              <div><span>Override object</span><strong>{domainOverrideSettings ? `#${domainOverrideSettings.id}` : "not initialized"}</strong></div>
              <div><span>Rules</span><strong>{domainOverrideRules.length}</strong></div>
              <div><span>Preview windows</span><strong>{domainOverridePreview?.windows.length ?? 0}</strong></div>
              <div><span>Readiness</span><strong>{selectedOverrideDomain ? formatDomainReadiness(selectedOverrideDomain) : "—"}</strong></div>
            </div>
          </div>

          {selectedOverrideDomain ? (
            <>
              <div className="grid two">
                <div className="card">
                  <h3>Override Settings</h3>
                  <form className="form" onSubmit={saveDomainOverrideSettings}>
                    <div className="form two-columns">
                      <label><span>Timezone</span><input value={domainOverrideForm.timezoneName} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, timezoneName: event.target.value }))} /></label>
                      <label>
                        <span>Resolution</span>
                        <select value={domainOverrideForm.ruleResolutionMode} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, ruleResolutionMode: event.target.value }))}>
                          <option value="priority">priority</option>
                          <option value="merge">merge</option>
                        </select>
                      </label>
                      <label><span>Default min RPS</span><input value={domainOverrideForm.defaultMinGuaranteedRps} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, defaultMinGuaranteedRps: event.target.value }))} /></label>
                    </div>
                    <label><span>Notes</span><textarea rows={2} value={domainOverrideForm.notes} onChange={(event) => setDomainOverrideForm((current) => ({ ...current, notes: event.target.value }))} /></label>
                    <button type="submit">{domainOverrideSettings ? "Сохранить override" : "Инициализировать override"}</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Override Rule</h3>
                  <form className="form" onSubmit={submitDomainOverrideRule}>
                    <div className="form two-columns">
                      <label><span>Name</span><input value={domainOverrideRuleForm.name} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, name: event.target.value }))} /></label>
                      <label>
                        <span>Schedule</span>
                        <select value={domainOverrideRuleForm.scheduleType} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, scheduleType: event.target.value }))}>
                          <option value="hourly">hourly</option>
                          <option value="daily">daily</option>
                          <option value="weekly">weekly</option>
                          <option value="one_time">one_time</option>
                        </select>
                      </label>
                      <label><span>Hour</span><input value={domainOverrideRuleForm.hour} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, hour: event.target.value }))} placeholder="empty for hourly" /></label>
                      <label><span>Minute</span><input value={domainOverrideRuleForm.minute} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, minute: event.target.value }))} /></label>
                      <label><span>Second</span><input value={domainOverrideRuleForm.second} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, second: event.target.value }))} /></label>
                      <label><span>Priority</span><input value={domainOverrideRuleForm.priority} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, priority: event.target.value }))} /></label>
                      <label><span>Weekdays</span><input value={domainOverrideRuleForm.weekdays} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, weekdays: event.target.value }))} placeholder="1,3,5" /></label>
                      <label><span>Specific date</span><input type="date" value={domainOverrideRuleForm.specificDate} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, specificDate: event.target.value }))} /></label>
                      <label><span>Duration sec</span><input value={domainOverrideRuleForm.windowDurationSeconds} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
                      <label>
                        <span>Execution</span>
                        <select value={domainOverrideRuleForm.executionProfileMode} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, executionProfileMode: event.target.value }))}>
                          <option value="flat">flat</option>
                          <option value="phased">phased</option>
                        </select>
                      </label>
                      <label className="checkbox"><input type="checkbox" checked={domainOverrideRuleForm.isEnabled} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, isEnabled: event.target.checked }))} /><span>Enabled</span></label>
                    </div>
                    <label><span>Notes</span><textarea rows={2} value={domainOverrideRuleForm.notes} onChange={(event) => setDomainOverrideRuleForm((current) => ({ ...current, notes: event.target.value }))} /></label>
                    <button type="submit">Add override rule</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Override Phase</h3>
                  <form className="form" onSubmit={submitDomainOverridePhase}>
                    <div className="form two-columns">
                      <label>
                        <span>Rule</span>
                        <select value={domainOverridePhaseForm.ruleId} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, ruleId: event.target.value }))}>
                          <option value="">Выбери rule</option>
                          {domainOverrideRules.map((rule) => <option key={rule.id} value={rule.id}>{rule.name}</option>)}
                        </select>
                      </label>
                      <label><span>Name</span><input value={domainOverridePhaseForm.name} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, name: event.target.value }))} /></label>
                      <label><span>Sort</span><input value={domainOverridePhaseForm.sortOrder} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, sortOrder: event.target.value }))} /></label>
                      <label><span>Offset sec</span><input value={domainOverridePhaseForm.startOffsetSeconds} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, startOffsetSeconds: event.target.value }))} /></label>
                      <label><span>Duration sec</span><input value={domainOverridePhaseForm.durationSeconds} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, durationSeconds: event.target.value }))} /></label>
                      <label>
                        <span>RPS mode</span>
                        <select value={domainOverridePhaseForm.rpsMode} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, rpsMode: event.target.value }))}>
                          <option value="percent">percent</option>
                          <option value="fixed">fixed</option>
                        </select>
                      </label>
                      <label><span>RPS value</span><input value={domainOverridePhaseForm.rpsValue} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, rpsValue: event.target.value }))} /></label>
                      <label className="checkbox"><input type="checkbox" checked={domainOverridePhaseForm.stopOnSuccess} onChange={(event) => setDomainOverridePhaseForm((current) => ({ ...current, stopOnSuccess: event.target.checked }))} /><span>Stop on success</span></label>
                    </div>
                    <button type="submit" disabled={domainOverrideRules.length === 0}>Add override phase</button>
                  </form>
                </div>

                <div className="card">
                  <h3>Override Preview</h3>
                  <div className="key-value compact">
                    <div><span>Timezone</span><strong>{domainOverridePreview?.timezone_name ?? domainOverrideForm.timezoneName}</strong></div>
                    <div><span>Resolution</span><strong>{domainOverridePreview?.resolution_mode ?? domainOverrideForm.ruleResolutionMode}</strong></div>
                  </div>
                  <div className="table-wrap">
                    <table>
                      <thead>
                        <tr>
                          <th>Rule</th>
                          <th>Priority</th>
                          <th>Start</th>
                          <th>End</th>
                        </tr>
                      </thead>
                      <tbody>
                        {(domainOverridePreview?.windows ?? []).map((window) => (
                          <tr key={`${window.rule_id}-${window.start_at}`}>
                            <td>{window.rule_name ?? `rule #${window.rule_id}`}</td>
                            <td>{window.priority}</td>
                            <td>{formatDateTime(window.start_at)}</td>
                            <td>{formatDateTime(window.end_at)}</td>
                          </tr>
                        ))}
                        {(domainOverridePreview?.windows ?? []).length === 0 ? (
                          <tr>
                            <td colSpan={4} className="empty">No windows resolved for this date</td>
                          </tr>
                        ) : null}
                      </tbody>
                    </table>
                  </div>
                </div>
              </div>

              <div className="promo-list">
                {domainOverrideRules.map((rule) => (
                  <article key={rule.id} className="strategy-rule-card">
                    <div className="domain-card-head compact-head">
                      <div>
                        <div className="domain-title-row">
                          <h3>{rule.name}</h3>
                          <span className={statusClass(rule.is_enabled ? "ready" : "inactive")}>{rule.schedule_type}</span>
                        </div>
                        <p className="muted">
                          priority {rule.priority} | time {rule.hour ?? "*"}:{String(rule.minute).padStart(2, "0")}:{String(rule.second).padStart(2, "0")} | duration {rule.window_duration_seconds}s | mode {rule.execution_profile_mode}
                        </p>
                      </div>
                      <div className="actions">
                        <button type="button" className="danger" onClick={() => void removeDomainOverrideRule(rule.id)}>Delete rule</button>
                      </div>
                    </div>
                    <div className="table-wrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Phase</th>
                            <th>Sort</th>
                            <th>Offset</th>
                            <th>Duration</th>
                            <th>Mode</th>
                            <th>Value</th>
                            <th>Stop</th>
                            <th></th>
                          </tr>
                        </thead>
                        <tbody>
                          {(domainOverridePhases[rule.id] ?? []).map((phase) => (
                            <tr key={phase.id}>
                              <td>{phase.name}</td>
                              <td>{phase.sort_order}</td>
                              <td>{phase.start_offset_seconds}</td>
                              <td>{phase.duration_seconds}</td>
                              <td>{phase.rps_mode}</td>
                              <td>{phase.rps_value}</td>
                              <td>{phase.stop_on_success ? "yes" : "no"}</td>
                              <td><button type="button" className="danger" onClick={() => void removeDomainOverridePhase(phase.id)}>Delete</button></td>
                            </tr>
                          ))}
                          {(domainOverridePhases[rule.id] ?? []).length === 0 ? (
                            <tr>
                              <td colSpan={8} className="empty">No phases yet</td>
                            </tr>
                          ) : null}
                        </tbody>
                      </table>
                    </div>
                  </article>
                ))}
                {domainOverrideRules.length === 0 ? <p className="empty">No domain override rules yet</p> : null}
              </div>
            </>
          ) : (
            <p className="empty">Пока нет доменов в режиме `manual_override`.</p>
          )}
        </div>

        <div className="card full-span">
          <div className="card-head">
            <h2>Список доменов</h2>
            <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Домен</th>
                  <th>Статус</th>
                  <th>Дата дропа</th>
                  <th>Readiness</th>
                  <th>Priority</th>
                  <th>Strategy</th>
                  <th>Runtime RPS</th>
                  <th>Аккаунт</th>
                  <th>Контакт</th>
                  <th>Окно</th>
                  <th>Успех</th>
                  <th>Действия</th>
                </tr>
              </thead>
              <tbody>
                {domains.map((domain) => (
                  <tr key={domain.id}>
                    <td>
                      <strong>{domain.fqdn}</strong>
                        <div className="row-hint">{domain.zone} | {domain.registrar_slug} | {domain.strategy_mode}</div>
                      </td>
                      <td>
                        <span className={statusClass(domain.status)}>{domain.status}</span>
                        {domain.runtime_attack_status ? <div className="row-hint">run: {domain.runtime_attack_status}</div> : null}
                      </td>
                      <td>{domain.drop_date}</td>
                      <td>
                        <div>{formatDomainReadiness(domain)}</div>
                        {domain.override_min_guaranteed_rps !== null ? <div className="row-hint">min override: {domain.override_min_guaranteed_rps}</div> : null}
                        {domain.dry_run_status ? <div className="row-hint">dry-run: {domain.dry_run_status}{domain.dry_run_http_status ? ` / ${domain.dry_run_http_status}` : ""}</div> : null}
                      </td>
                      <td>{domain.priority}</td>
                      <td>
                        <div>{domain.zone_strategy_id ? strategyMap.get(domain.zone_strategy_id)?.name ?? `#${domain.zone_strategy_id}` : "auto"}</div>
                        {domain.runtime_phase_name ? <div className="row-hint">phase: {domain.runtime_phase_name}</div> : null}
                      </td>
                      <td>
                        <div>min {formatRps(domain.runtime_minimum_rps)}</div>
                        <div>desired {formatRps(domain.runtime_desired_rps)}</div>
                        <div>allocated {formatRps(domain.runtime_allocated_rps)}</div>
                        <div className="row-hint">workers: {domain.runtime_assigned_worker_count}</div>
                      </td>
                      <td>{domain.registrar_account_id ? accountMap.get(domain.registrar_account_id)?.name ?? `#${domain.registrar_account_id}` : "auto"}</td>
                    <td>{domain.contact_profile_id ? contactMap.get(domain.contact_profile_id)?.label ?? `#${domain.contact_profile_id}` : "auto"}</td>
                    <td>{String(domain.window_start_minute).padStart(2, "0")}:{String(domain.window_start_second).padStart(2, "0")} + {domain.window_duration_seconds}s</td>
                    <td>
                      <div>{domain.success_at ? formatDateTime(domain.success_at) : "—"}</div>
                      {domain.dry_run_checked_at ? <div className="row-hint">checked: {formatDateTime(domain.dry_run_checked_at)}</div> : null}
                    </td>
                    <td>
                      <div className="actions">
                        <button type="button" className="ghost" onClick={() => void startDomainAttack(domain.id)}>Старт</button>
                        <button type="button" className="ghost" onClick={() => void dryRunDomain(domain)}>Dry run</button>
                        <button type="button" className="ghost" onClick={() => void toggleDomain(domain)}>{domain.attack_enabled ? "Пауза" : "Вкл"}</button>
                        {domain.strategy_mode === "manual_override" ? (
                          <button type="button" className="ghost" onClick={() => setSelectedOverrideDomainId(domain.id)}>Override</button>
                        ) : null}
                        <button type="button" className="danger" onClick={() => void deleteItem("domain", domain.id)}>Удалить</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    );
  }

  function renderWorkers() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>{editingWorkerId ? `Редактировать worker #${editingWorkerId}` : "Добавить worker"}</h2>
          <form className="form" onSubmit={submitWorker}>
            <div className="form two-columns">
              <label><span>Имя</span><input value={workerForm.name} onChange={(event) => setWorkerForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>Registrar</span><input value={workerForm.registrarSlug} onChange={(event) => setWorkerForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
              <label>
                <span>Assigned account</span>
                <select value={workerForm.assignedRegistrarAccountId} onChange={(event) => setWorkerForm((current) => ({ ...current, assignedRegistrarAccountId: event.target.value }))}>
                  <option value="">Не закреплен</option>
                  {accounts.map((account) => <option key={account.id} value={account.id}>{account.name}</option>)}
                </select>
              </label>
              <label><span>API base URL</span><input value={workerForm.apiBaseUrl} onChange={(event) => setWorkerForm((current) => ({ ...current, apiBaseUrl: event.target.value }))} /></label>
              <label><span>Control token</span><input value={workerForm.controlToken} onChange={(event) => setWorkerForm((current) => ({ ...current, controlToken: event.target.value }))} /></label>
              <label><span>Status</span><input value={workerForm.status} onChange={(event) => setWorkerForm((current) => ({ ...current, status: event.target.value }))} /></label>
              <label><span>IP</span><input value={workerForm.ipAddress} onChange={(event) => setWorkerForm((current) => ({ ...current, ipAddress: event.target.value }))} /></label>
              <label><span>Region</span><input value={workerForm.region} onChange={(event) => setWorkerForm((current) => ({ ...current, region: event.target.value }))} /></label>
              <label><span>Max RPS</span><input value={workerForm.maxRps} onChange={(event) => setWorkerForm((current) => ({ ...current, maxRps: event.target.value }))} /></label>
              <label><span>Target RPS</span><input value={workerForm.targetRps} onChange={(event) => setWorkerForm((current) => ({ ...current, targetRps: event.target.value }))} /></label>
              <label><span>Current RPS</span><input value={workerForm.currentRps} onChange={(event) => setWorkerForm((current) => ({ ...current, currentRps: event.target.value }))} /></label>
              <label><span>Capacity now</span><input value={workerForm.currentCapacityRps} onChange={(event) => setWorkerForm((current) => ({ ...current, currentCapacityRps: event.target.value }))} /></label>
              <label><span>CPU %</span><input value={workerForm.cpuLoad} onChange={(event) => setWorkerForm((current) => ({ ...current, cpuLoad: event.target.value }))} /></label>
              <label><span>RAM %</span><input value={workerForm.ramUsagePercent} onChange={(event) => setWorkerForm((current) => ({ ...current, ramUsagePercent: event.target.value }))} /></label>
              <label><span>Clock drift ms</span><input value={workerForm.clockDriftMs} onChange={(event) => setWorkerForm((current) => ({ ...current, clockDriftMs: event.target.value }))} /></label>
            </div>
            <label><span>Notes</span><textarea rows={3} value={workerForm.notes} onChange={(event) => setWorkerForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">{editingWorkerId ? "Обновить worker" : "Сохранить worker"}</button>
              {editingWorkerId ? <button type="button" className="ghost" onClick={resetWorkerForm}>Отмена</button> : null}
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Суммарный RPS</h2>
          <div className="key-value compact">
            <div><span>Current</span><strong>{overview?.capacity.current_rps ?? 0}</strong></div>
            <div><span>Target</span><strong>{overview?.capacity.target_rps ?? 0}</strong></div>
            <div><span>Max</span><strong>{overview?.capacity.max_rps ?? 0}</strong></div>
            <div><span>Workers online</span><strong>{overview?.capacity.online_workers ?? 0} / {overview?.capacity.enabled_workers ?? 0}</strong></div>
          </div>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <h2>Workers</h2>
            <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          </div>
          <div className="user-list">
            {workers.map((worker) => (
              <article key={worker.id} className="user-card">
                <div className="user-card-head">
                  <div>
                    <strong>{worker.name}</strong>
                    <p>
                      <span className={statusClass(worker.status)}>{worker.status}</span>
                      <span className="muted"> {worker.ip_address ?? "no-ip"} | {worker.region ?? "no-region"} | {worker.registrar_slug}</span>
                    </p>
                  </div>
                  <div className="muted">seen: {formatDateTime(worker.last_seen_at)}</div>
                </div>
                <div className="key-value">
                  <div><span>Current / Target / Max RPS</span><strong>{worker.current_rps} / {worker.target_rps} / {worker.max_rps}</strong></div>
                  <div><span>Capacity now</span><strong>{worker.current_capacity_rps}</strong></div>
                  <div><span>CPU / RAM</span><strong>{worker.cpu_load}% / {worker.ram_usage_percent}%</strong></div>
                  <div><span>Clock drift</span><strong>{worker.clock_drift_ms} ms</strong></div>
                  <div><span>Domains on worker</span><strong>{worker.current_domain_count}</strong></div>
                  <div><span>Assigned account</span><strong>{worker.assigned_registrar_account_id ? accountMap.get(worker.assigned_registrar_account_id)?.name ?? worker.assigned_registrar_account_id : "not pinned"}</strong></div>
                  <div><span>Worker ID</span><strong>{worker.id}</strong></div>
                  <div><span>Control token</span><strong>{worker.control_token ?? "auto-generate on create"}</strong></div>
                </div>
                <div className="actions">
                  <button type="button" className="ghost" onClick={() => startEditWorker(worker)}>Редактировать</button>
                  <button type="button" className="ghost" onClick={() => void toggleWorker(worker)}>{worker.is_enabled ? "Выключить" : "Включить"}</button>
                  <button type="button" className="danger" onClick={() => void deleteItem("worker", worker.id)}>Удалить</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderAccounts() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Аккаунт регистратора</h2>
          <form className="form" onSubmit={submitAccount}>
            <div className="form two-columns">
              <label><span>Имя</span><input value={accountForm.name} onChange={(event) => setAccountForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>Registrar</span><input value={accountForm.registrarSlug} onChange={(event) => setAccountForm((current) => ({ ...current, registrarSlug: event.target.value }))} /></label>
              <label><span>API token</span><input value={accountForm.apiToken} onChange={(event) => setAccountForm((current) => ({ ...current, apiToken: event.target.value }))} /></label>
              <label><span>API base URL</span><input value={accountForm.apiBaseUrl} onChange={(event) => setAccountForm((current) => ({ ...current, apiBaseUrl: event.target.value }))} placeholder="https://api.gandi.net/v5/domain/domains" /></label>
              <label><span>sharing_id</span><input value={accountForm.sharingId} onChange={(event) => setAccountForm((current) => ({ ...current, sharingId: event.target.value }))} /></label>
              <label>
                <span>Default contact</span>
                <select value={accountForm.defaultContactProfileId} onChange={(event) => setAccountForm((current) => ({ ...current, defaultContactProfileId: event.target.value }))}>
                  <option value="">Не назначен</option>
                  {contacts.map((contact) => <option key={contact.id} value={contact.id}>{contact.label}</option>)}
                </select>
              </label>
              <label className="checkbox"><input type="checkbox" checked={accountForm.supportsDryRun} onChange={(event) => setAccountForm((current) => ({ ...current, supportsDryRun: event.target.checked }))} /><span>Dry-Run</span></label>
              <label className="checkbox"><input type="checkbox" checked={accountForm.isActive} onChange={(event) => setAccountForm((current) => ({ ...current, isActive: event.target.checked }))} /><span>Активен</span></label>
            </div>
            <label><span>Notes</span><textarea rows={3} value={accountForm.notes} onChange={(event) => setAccountForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить аккаунт</button>
          </form>
        </div>

        <div className="card">
          <h2>Подсказка по Gandi</h2>
          <p className="muted">
            Для Gandi нужен Bearer PAT, корректный contact profile и при необходимости свой `api_base_url` для sandbox
            или альтернативной среды. Проверка аккаунта в панели сейчас проверяет авторизацию, а реальную create-схему
            лучше подтверждать через `Dry run` на конкретном домене.
          </p>
        </div>

        <div className="card full-span">
          <div className="proxy-list">
            {accounts.map((account) => (
              <article key={account.id} className="proxy-row">
                <div>
                  <strong>{account.name}</strong>
                  <p>{account.registrar_slug} | status: <span className={statusClass(account.last_validation_status)}>{account.last_validation_status}</span></p>
                  <p>contact: {account.default_contact_profile_id ? contactMap.get(account.default_contact_profile_id)?.label ?? account.default_contact_profile_id : "none"}</p>
                  <p>validated: {formatDateTime(account.last_validated_at)}</p>
                  {account.last_validation_message ? <p className="row-hint">{account.last_validation_message}</p> : null}
                </div>
                <div className="actions">
                  <button type="button" className="ghost" onClick={() => void validateAccount(account.id)}>Проверить</button>
                  <button type="button" className="ghost" onClick={() => void prefillContactFromAccount(account)}>Prefill contact</button>
                  <button type="button" className="danger" onClick={() => void deleteItem("account", account.id)}>Удалить</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderStrategies() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Zone Strategy</h2>
          <form className="form" onSubmit={submitStrategy}>
            <div className="form two-columns">
              <label>
                <span>Zone</span>
                <input value={strategyForm.zone} onChange={(event) => setStrategyForm((current) => ({ ...current, zone: event.target.value }))} />
              </label>
              <label>
                <span>Name</span>
                <input value={strategyForm.name} onChange={(event) => setStrategyForm((current) => ({ ...current, name: event.target.value }))} />
              </label>
              <label>
                <span>Timezone</span>
                <input value={strategyForm.timezoneName} onChange={(event) => setStrategyForm((current) => ({ ...current, timezoneName: event.target.value }))} />
              </label>
              <label>
                <span>Resolution</span>
                <select value={strategyForm.ruleResolutionMode} onChange={(event) => setStrategyForm((current) => ({ ...current, ruleResolutionMode: event.target.value }))}>
                  <option value="priority">priority</option>
                  <option value="merge">merge</option>
                </select>
              </label>
              <label>
                <span>Min guaranteed RPS</span>
                <input value={strategyForm.defaultMinGuaranteedRps} onChange={(event) => setStrategyForm((current) => ({ ...current, defaultMinGuaranteedRps: event.target.value }))} />
              </label>
              <label>
                <span>Registrar</span>
                <input value={strategyForm.defaultRegistrarSlug} onChange={(event) => setStrategyForm((current) => ({ ...current, defaultRegistrarSlug: event.target.value }))} />
              </label>
              <label className="checkbox">
                <input type="checkbox" checked={strategyForm.isActive} onChange={(event) => setStrategyForm((current) => ({ ...current, isActive: event.target.checked }))} />
                <span>Active</span>
              </label>
            </div>
            <label>
              <span>Notes</span>
              <textarea rows={3} value={strategyForm.notes} onChange={(event) => setStrategyForm((current) => ({ ...current, notes: event.target.value }))} />
            </label>
            <button type="submit">Add strategy</button>
          </form>
        </div>

        <div className="card">
          <h2>Strategy Model</h2>
          <p className="muted">
            Strategies are now first-class control objects. Each zone can get its own timezone,
            resolution mode, and default guaranteed RPS before we attach richer rules and phases.
          </p>
          <div className="key-value compact">
            <div><span>Total strategies</span><strong>{strategies.length}</strong></div>
            <div><span>Active zones</span><strong>{strategies.filter((item) => item.is_active).length}</strong></div>
          </div>
          <label>
            <span>Selected strategy</span>
            <select value={selectedStrategyId ?? ""} onChange={(event) => setSelectedStrategyId(Number(event.target.value))}>
              {strategies.map((strategy) => (
                <option key={strategy.id} value={strategy.id}>
                  {strategy.zone} | {strategy.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        <div className="card full-span">
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Zone</th>
                  <th>Name</th>
                  <th>Timezone</th>
                  <th>Resolution</th>
                  <th>Min RPS</th>
                  <th>Registrar</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {strategies.map((strategy) => (
                  <tr key={strategy.id}>
                    <td>{strategy.zone}</td>
                    <td>{strategy.name}</td>
                    <td>{strategy.timezone_name}</td>
                    <td>{strategy.rule_resolution_mode}</td>
                    <td>{strategy.default_min_guaranteed_rps}</td>
                    <td>{strategy.default_registrar_slug}</td>
                    <td><span className={statusClass(strategy.is_active ? "ready" : "inactive")}>{strategy.is_active ? "active" : "inactive"}</span></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h2>Zone Rule</h2>
          <form className="form" onSubmit={submitRule}>
            <div className="form two-columns">
              <label><span>Name</span><input value={ruleForm.name} onChange={(event) => setRuleForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label>
                <span>Schedule</span>
                <select value={ruleForm.scheduleType} onChange={(event) => setRuleForm((current) => ({ ...current, scheduleType: event.target.value }))}>
                  <option value="hourly">hourly</option>
                  <option value="daily">daily</option>
                  <option value="weekly">weekly</option>
                  <option value="one_time">one_time</option>
                </select>
              </label>
              <label><span>Hour</span><input value={ruleForm.hour} onChange={(event) => setRuleForm((current) => ({ ...current, hour: event.target.value }))} placeholder="empty for hourly" /></label>
              <label><span>Minute</span><input value={ruleForm.minute} onChange={(event) => setRuleForm((current) => ({ ...current, minute: event.target.value }))} /></label>
              <label><span>Second</span><input value={ruleForm.second} onChange={(event) => setRuleForm((current) => ({ ...current, second: event.target.value }))} /></label>
              <label><span>Priority</span><input value={ruleForm.priority} onChange={(event) => setRuleForm((current) => ({ ...current, priority: event.target.value }))} /></label>
              <label><span>Weekdays</span><input value={ruleForm.weekdays} onChange={(event) => setRuleForm((current) => ({ ...current, weekdays: event.target.value }))} placeholder="1,3,5" /></label>
              <label><span>Specific date</span><input type="date" value={ruleForm.specificDate} onChange={(event) => setRuleForm((current) => ({ ...current, specificDate: event.target.value }))} /></label>
              <label><span>Duration sec</span><input value={ruleForm.windowDurationSeconds} onChange={(event) => setRuleForm((current) => ({ ...current, windowDurationSeconds: event.target.value }))} /></label>
              <label>
                <span>Execution</span>
                <select value={ruleForm.executionProfileMode} onChange={(event) => setRuleForm((current) => ({ ...current, executionProfileMode: event.target.value }))}>
                  <option value="flat">flat</option>
                  <option value="phased">phased</option>
                </select>
              </label>
              <label className="checkbox"><input type="checkbox" checked={ruleForm.isEnabled} onChange={(event) => setRuleForm((current) => ({ ...current, isEnabled: event.target.checked }))} /><span>Enabled</span></label>
            </div>
            <label><span>Notes</span><textarea rows={2} value={ruleForm.notes} onChange={(event) => setRuleForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Add rule</button>
          </form>
        </div>

        <div className="card">
          <h2>Rule Phase</h2>
          <form className="form" onSubmit={submitPhase}>
            <div className="form two-columns">
              <label>
                <span>Rule</span>
                <select value={phaseForm.ruleId} onChange={(event) => setPhaseForm((current) => ({ ...current, ruleId: event.target.value }))}>
                  <option value="">Select rule</option>
                  {strategyRules.map((rule) => (
                    <option key={rule.id} value={rule.id}>{rule.name}</option>
                  ))}
                </select>
              </label>
              <label><span>Name</span><input value={phaseForm.name} onChange={(event) => setPhaseForm((current) => ({ ...current, name: event.target.value }))} /></label>
              <label><span>Sort</span><input value={phaseForm.sortOrder} onChange={(event) => setPhaseForm((current) => ({ ...current, sortOrder: event.target.value }))} /></label>
              <label><span>Start offset sec</span><input value={phaseForm.startOffsetSeconds} onChange={(event) => setPhaseForm((current) => ({ ...current, startOffsetSeconds: event.target.value }))} /></label>
              <label><span>Duration sec</span><input value={phaseForm.durationSeconds} onChange={(event) => setPhaseForm((current) => ({ ...current, durationSeconds: event.target.value }))} /></label>
              <label>
                <span>RPS mode</span>
                <select value={phaseForm.rpsMode} onChange={(event) => setPhaseForm((current) => ({ ...current, rpsMode: event.target.value }))}>
                  <option value="percent">percent</option>
                  <option value="fixed">fixed</option>
                </select>
              </label>
              <label><span>RPS value</span><input value={phaseForm.rpsValue} onChange={(event) => setPhaseForm((current) => ({ ...current, rpsValue: event.target.value }))} /></label>
              <label className="checkbox"><input type="checkbox" checked={phaseForm.stopOnSuccess} onChange={(event) => setPhaseForm((current) => ({ ...current, stopOnSuccess: event.target.checked }))} /><span>Stop on success</span></label>
            </div>
            <button type="submit">Add phase</button>
          </form>
        </div>

        <div className="card full-span">
          <div className="card-head">
            <div>
              <h2>Rules & Phases</h2>
              <p className="muted">Operational rule editor for the selected zone strategy.</p>
            </div>
            <label className="preview-date">
              <span>Preview date</span>
              <input type="date" value={previewDate} onChange={(event) => setPreviewDate(event.target.value)} />
            </label>
          </div>
          <div className="strategy-rule-list">
            {strategyRules.map((rule) => (
              <article key={rule.id} className="strategy-rule-card">
                <div className="domain-card-head compact-head">
                  <div>
                    <div className="domain-title-row">
                      <h3>{rule.name}</h3>
                      <span className={statusClass(rule.is_enabled ? "ready" : "inactive")}>{rule.schedule_type}</span>
                    </div>
                    <p className="muted">
                      priority {rule.priority} | time {rule.hour ?? "*"}:{String(rule.minute).padStart(2, "0")}:{String(rule.second).padStart(2, "0")} | duration {rule.window_duration_seconds}s | mode {rule.execution_profile_mode}
                    </p>
                  </div>
                  <div className="actions">
                    <button type="button" className="danger" onClick={() => void removeZoneRule(rule.id)}>Delete rule</button>
                  </div>
                </div>
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Phase</th>
                        <th>Sort</th>
                        <th>Offset</th>
                        <th>Duration</th>
                        <th>Mode</th>
                        <th>Value</th>
                        <th>Stop</th>
                        <th></th>
                      </tr>
                    </thead>
                    <tbody>
                      {(rulePhases[rule.id] ?? []).map((phase) => (
                        <tr key={phase.id}>
                          <td>{phase.name}</td>
                          <td>{phase.sort_order}</td>
                          <td>{phase.start_offset_seconds}</td>
                          <td>{phase.duration_seconds}</td>
                          <td>{phase.rps_mode}</td>
                          <td>{phase.rps_value}</td>
                          <td>{phase.stop_on_success ? "yes" : "no"}</td>
                          <td><button type="button" className="danger" onClick={() => void removeZonePhase(phase.id)}>Delete</button></td>
                        </tr>
                      ))}
                      {(rulePhases[rule.id] ?? []).length === 0 ? (
                        <tr>
                          <td colSpan={8} className="empty">No phases yet</td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              </article>
            ))}
            {strategyRules.length === 0 ? <p className="empty">No rules yet for this strategy</p> : null}
          </div>
        </div>

        <div className="card full-span">
          <h2>Schedule Preview</h2>
          <div className="key-value compact">
            <div><span>Timezone</span><strong>{strategyPreview?.timezone_name ?? selectedStrategy?.timezone_name ?? "—"}</strong></div>
            <div><span>Resolution</span><strong>{strategyPreview?.resolution_mode ?? selectedStrategy?.rule_resolution_mode ?? "—"}</strong></div>
          </div>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Rule</th>
                  <th>Priority</th>
                  <th>Start</th>
                  <th>End</th>
                </tr>
              </thead>
              <tbody>
                {(strategyPreview?.windows ?? []).map((window) => (
                  <tr key={`${window.rule_id}-${window.start_at}`}>
                    <td>{window.rule_name ?? `rule #${window.rule_id}`}</td>
                    <td>{window.priority}</td>
                    <td>{formatDateTime(window.start_at)}</td>
                    <td>{formatDateTime(window.end_at)}</td>
                  </tr>
                ))}
                {(strategyPreview?.windows ?? []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="empty">No windows resolved for this date</td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </div>
      </section>
    );
  }

  function renderContacts() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Contact profile</h2>
          <form className="form" onSubmit={submitContact}>
            <div className="form two-columns">
              <label><span>Label</span><input value={contactForm.label} onChange={(event) => setContactForm((current) => ({ ...current, label: event.target.value }))} /></label>
              <label><span>Type</span><input value={contactForm.personType} onChange={(event) => setContactForm((current) => ({ ...current, personType: event.target.value }))} /></label>
              <label><span>Given</span><input value={contactForm.givenName} onChange={(event) => setContactForm((current) => ({ ...current, givenName: event.target.value }))} /></label>
              <label><span>Family</span><input value={contactForm.familyName} onChange={(event) => setContactForm((current) => ({ ...current, familyName: event.target.value }))} /></label>
              <label><span>Organization</span><input value={contactForm.organizationName} onChange={(event) => setContactForm((current) => ({ ...current, organizationName: event.target.value }))} /></label>
              <label><span>Email</span><input value={contactForm.email} onChange={(event) => setContactForm((current) => ({ ...current, email: event.target.value }))} /></label>
              <label><span>Phone</span><input value={contactForm.phone} onChange={(event) => setContactForm((current) => ({ ...current, phone: event.target.value }))} /></label>
              <label><span>Mobile</span><input value={contactForm.mobile} onChange={(event) => setContactForm((current) => ({ ...current, mobile: event.target.value }))} /></label>
              <label><span>Fax</span><input value={contactForm.fax} onChange={(event) => setContactForm((current) => ({ ...current, fax: event.target.value }))} /></label>
              <label><span>Lang</span><input value={contactForm.lang} onChange={(event) => setContactForm((current) => ({ ...current, lang: event.target.value }))} /></label>
              <label><span>Street</span><input value={contactForm.streetAddress} onChange={(event) => setContactForm((current) => ({ ...current, streetAddress: event.target.value }))} /></label>
              <label><span>City</span><input value={contactForm.city} onChange={(event) => setContactForm((current) => ({ ...current, city: event.target.value }))} /></label>
              <label><span>State</span><input value={contactForm.state} onChange={(event) => setContactForm((current) => ({ ...current, state: event.target.value }))} /></label>
              <label><span>ZIP</span><input value={contactForm.zipCode} onChange={(event) => setContactForm((current) => ({ ...current, zipCode: event.target.value }))} /></label>
              <label><span>Country</span><input value={contactForm.countryCode} onChange={(event) => setContactForm((current) => ({ ...current, countryCode: event.target.value }))} /></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.dataObfuscated} onChange={(event) => setContactForm((current) => ({ ...current, dataObfuscated: event.target.checked }))} /><span>data_obfuscated</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.mailObfuscated} onChange={(event) => setContactForm((current) => ({ ...current, mailObfuscated: event.target.checked }))} /><span>mail_obfuscated</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.icannContractAccept} onChange={(event) => setContactForm((current) => ({ ...current, icannContractAccept: event.target.checked }))} /><span>ICANN accept</span></label>
              <label className="checkbox"><input type="checkbox" checked={contactForm.isDefault} onChange={(event) => setContactForm((current) => ({ ...current, isDefault: event.target.checked }))} /><span>По умолчанию</span></label>
            </div>
            <label><span>Contact extra parameters (JSON)</span><textarea rows={3} value={contactForm.extraParameters} onChange={(event) => setContactForm((current) => ({ ...current, extraParameters: event.target.value }))} placeholder='{"local_presence":"fr"}' /></label>
            <label><span>Notes</span><textarea rows={3} value={contactForm.notes} onChange={(event) => setContactForm((current) => ({ ...current, notes: event.target.value }))} /></label>
            <button type="submit">Добавить contact profile</button>
          </form>
        </div>

        <div className="card">
          <h2>Логика контактов</h2>
          <p className="muted">
            Проект уже поддерживает несколько contact profiles. Один можно сделать дефолтным, остальные назначать под
            аккаунты или домены отдельно, без необходимости решать это один раз и навсегда прямо сейчас.
          </p>
        </div>

        <div className="card full-span">
          <div className="promo-list">
            {contacts.map((contact) => (
              <article key={contact.id} className="promo-row">
                <strong>{contact.label}</strong>
                <p>{contact.given_name} {contact.family_name} | {contact.person_type}{contact.is_default ? " | default" : ""}</p>
                <p>{contact.email} | {contact.phone}{contact.mobile ? ` | mobile ${contact.mobile}` : ""}</p>
                <p>{contact.street_address}, {contact.city}, {contact.zip_code}, {contact.country_code}</p>
                {contact.lang || contact.icann_contract_accept !== null || contact.extra_parameters ? (
                  <p className="row-hint">
                    {contact.lang ? `lang ${contact.lang}` : "lang —"}
                    {contact.icann_contract_accept !== null ? ` | icann ${String(contact.icann_contract_accept)}` : ""}
                    {contact.extra_parameters ? " | extra json set" : ""}
                  </p>
                ) : null}
                <div className="actions">
                  <button type="button" className="danger" onClick={() => void deleteItem("contact", contact.id)}>Удалить</button>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderAttacks() {
    return (
      <section className="grid two">
        <div className="card">
          <div className="card-head">
            <div>
              <h2>Управление атаками</h2>
              <p className="muted">По умолчанию используется приоритетная стратегия: сначала насыщаем мощностью более важные домены.</p>
            </div>
            <div className="actions">
              <button type="button" onClick={() => void startTodayAttacks(false)}>Старт due today</button>
              <button type="button" className="ghost" onClick={() => void startTodayAttacks(true)}>Force rebuild</button>
              <button type="button" className="ghost" onClick={() => void rebalanceAttacksNow()}>Rebalance</button>
              <button type="button" className="danger" onClick={() => void stopAllAttacks()}>Стоп всё</button>
            </div>
          </div>
          <div className="key-value compact">
            <div><span>Scheduled runs</span><strong>{overview?.scheduled_runs ?? 0}</strong></div>
            <div><span>Running runs</span><strong>{overview?.running_runs ?? 0}</strong></div>
            <div><span>Capacity target</span><strong>{overview?.capacity.target_rps ?? 0}</strong></div>
            <div><span>Capacity max</span><strong>{overview?.capacity.max_rps ?? 0}</strong></div>
          </div>
        </div>

        <div className="card">
          <h2>Поведение scheduler</h2>
          <p className="muted">
            При старте control строит `attack runs` и `worker tasks` для доменов due today. Освобождение workers после
            успешной регистрации и их повторное распределение будет реализовано на worker/runtime этапе.
          </p>
        </div>

        <div className="card full-span">
          <h2>Attack Runs</h2>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>ID</th>
                  <th>Domain</th>
                  <th>Status</th>
                  <th>Phase</th>
                  <th>Start</th>
                  <th>End</th>
                  <th>Workers</th>
                  <th>Runtime RPS</th>
                  <th>Planned RPS</th>
                  <th>Current RPS</th>
                  <th>Max RPS</th>
                </tr>
              </thead>
              <tbody>
                {attacks.map((attack) => (
                  <tr key={attack.id}>
                    <td>{attack.id}</td>
                    <td>{domainMap.get(attack.domain_id)?.fqdn ?? attack.domain_id}</td>
                    <td><span className={statusClass(attack.status)}>{attack.status}</span></td>
                    <td>{attack.runtime_phase_name ?? domainMap.get(attack.domain_id)?.runtime_phase_name ?? "—"}</td>
                    <td>{formatDateTime(attack.planned_start_at)}</td>
                    <td>{formatDateTime(attack.planned_end_at)}</td>
                    <td>{attack.assigned_worker_count}</td>
                    <td>
                      <div>min {formatRps(attack.runtime_minimum_rps ?? domainMap.get(attack.domain_id)?.runtime_minimum_rps)}</div>
                      <div>desired {formatRps(attack.runtime_desired_rps ?? domainMap.get(attack.domain_id)?.runtime_desired_rps)}</div>
                      <div>allocated {formatRps(attack.runtime_allocated_rps ?? domainMap.get(attack.domain_id)?.runtime_allocated_rps ?? attack.planned_rps)}</div>
                    </td>
                    <td>{attack.planned_rps}</td>
                    <td>{attack.current_rps}</td>
                    <td>{attack.max_rps}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="card">
          <h2>Worker Tasks</h2>
          <div className="log-list">
            {tasks.slice(0, 20).map((task) => (
              <article key={task.id} className="log-row">
                <span className={statusClass(task.status)}>{task.status}</span>
                <div>
                  <strong>run #{task.attack_run_id} | worker #{task.worker_id}</strong>
                  <p>domain: {domainMap.get(task.domain_id)?.fqdn ?? task.domain_id} | planned_rps: {task.planned_rps} | actual_rps: {task.actual_rps}</p>
                </div>
              </article>
            ))}
          </div>
        </div>

        <div className="card">
          <h2>Attack Events</h2>
          <div className="log-list">
            {events.slice(0, 20).map((event) => (
              <article key={event.id} className="log-row">
                <span className={statusClass(event.level)}>{event.level}</span>
                <div>
                  <strong>{event.event_type}</strong>
                  <p>{event.message}</p>
                  <p>{formatDateTime(event.created_at)}</p>
                </div>
              </article>
            ))}
          </div>
        </div>
      </section>
    );
  }

  function renderSettings() {
    return (
      <section className="grid two">
        <div className="card">
          <h2>Личный Telegram</h2>
          <form className="form" onSubmit={saveTelegram}>
            <label><span>Bot token</span><input value={telegramForm.telegram_token} onChange={(event) => setTelegramForm((current) => ({ ...current, telegram_token: event.target.value }))} /></label>
            <label><span>Chat ID</span><input value={telegramForm.telegram_chat_id} onChange={(event) => setTelegramForm((current) => ({ ...current, telegram_chat_id: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">Сохранить</button>
              <button type="button" className="ghost" onClick={() => void api.testTelegram().then((payload) => setToast({ type: "success", text: payload.detail })).catch((error: Error) => setToast({ type: "error", text: error.message }))}>Тест</button>
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Диагностический Telegram</h2>
          <form className="form" onSubmit={saveDiagnosticTelegram}>
            <label><span>Bot token</span><input value={diagnosticTelegram.telegram_token ?? ""} onChange={(event) => setDiagnosticTelegram((current) => ({ ...current, telegram_token: event.target.value }))} /></label>
            <label><span>Chat ID</span><input value={diagnosticTelegram.telegram_chat_id ?? ""} onChange={(event) => setDiagnosticTelegram((current) => ({ ...current, telegram_chat_id: event.target.value }))} /></label>
            <div className="actions">
              <button type="submit">Сохранить</button>
              <button type="button" className="ghost" onClick={() => void api.testDiagnosticTelegram().then((payload) => setToast({ type: "success", text: payload.detail })).catch((error: Error) => setToast({ type: "error", text: error.message }))}>Тест</button>
            </div>
          </form>
        </div>

        <div className="card">
          <h2>Смена пароля</h2>
          <form className="form" onSubmit={savePassword}>
            <label><span>Текущий пароль</span><input type="password" value={passwordForm.current_password} onChange={(event) => setPasswordForm((current) => ({ ...current, current_password: event.target.value }))} /></label>
            <label><span>Новый пароль</span><input type="password" value={passwordForm.new_password} onChange={(event) => setPasswordForm((current) => ({ ...current, new_password: event.target.value }))} /></label>
            <button type="submit">Изменить пароль</button>
          </form>
        </div>

        <div className="card">
          <h2>Статус билда</h2>
          <div className="key-value compact">
            <div><span>Публичная регистрация</span><strong>disabled</strong></div>
            <div><span>User create в админке</span><strong>disabled</strong></div>
            <div><span>Control API</span><strong>enabled</strong></div>
            <div><span>Worker-side runtime</span><strong>next phase</strong></div>
          </div>
        </div>
      </section>
    );
  }

  if (authLoading) {
    return <div className="shell loading">Загрузка...</div>;
  }

  if (!session) {
    return renderAuth();
  }

  return (
    <div className="app-shell">
      <header className="hero hero-top">
        <div>
          <p className="eyebrow">Control Server</p>
          <h1>Domain Drop Catcher</h1>
          <p className="subtitle">
            Один control-сервер управляет списком доменов, workers, контактами, аккаунтами регистраторов и планированием
            атакующих окон по времени реестра.
          </p>
          <div className="hero-meta">
            <span className={statusClass(session.user.status)}>{session.user.status}</span>
            <span className="muted">@{session.user.username}</span>
            <span className="muted">checked: {formatDateTime(overview?.checked_at ?? null)}</span>
          </div>
        </div>
        <div className="stats">
          <article><span>Domains</span><strong>{overview?.total_domains ?? 0}</strong></article>
          <article><span>Due Today</span><strong>{overview?.due_today_domains ?? 0}</strong></article>
          <article><span>Target RPS</span><strong>{overview?.capacity.target_rps ?? 0}</strong></article>
          <article><span>Max RPS</span><strong>{overview?.capacity.max_rps ?? 0}</strong></article>
        </div>
      </header>

      <div className="toolbar">
        <div className="tab-strip">
          {(["domains", "strategies", "workers", "accounts", "contacts", "attacks", "settings"] as Tab[]).map((item) => (
            <button key={item} type="button" className={tab === item ? "ghost active-chip" : "ghost"} onClick={() => setTab(item)}>
              {item}
            </button>
          ))}
        </div>
        <div className="toolbar-actions">
          <button type="button" className="ghost" onClick={() => void loadAll()}>Обновить</button>
          <button type="button" className="ghost" onClick={() => void logout()}>Выйти</button>
        </div>
      </div>

      {toast ? <div className={`toast ${toast.type}`}>{toast.text}</div> : null}

      {tab === "domains" ? renderDomains() : null}
      {tab === "strategies" ? renderStrategies() : null}
      {tab === "workers" ? renderWorkers() : null}
      {tab === "accounts" ? renderAccounts() : null}
      {tab === "contacts" ? renderContacts() : null}
      {tab === "attacks" ? renderAttacks() : null}
      {tab === "settings" ? renderSettings() : null}
    </div>
  );
}
