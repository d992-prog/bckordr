import { type FormEvent, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import {
  api,
  type VpnAccessKey,
  type VpnCustomer,
  type VpnFriendInvitation,
  type VpnFriendInvitationIssued,
  type VpnPlan,
  type VpnSubscription,
  type VpnSubscriptionDeviceUsage,
  type WorkerNode,
} from "./api";
import {
  accessKeyStatusLabel,
  applyAccessKeyDisplay,
  calculateExtendedExpiration,
  classifyVpnCustomer,
  customerStatusOptions,
  filterVpnCustomers,
  nextAccessKeyEditorAfterRename,
  reconcileAccessKeyDisplayOverrides,
  selectPrimarySubscription,
  saveSubscriptionAndRequestSync,
  saveAccessKeyDisplayName,
  vpnProfileIssueError,
  type VpnCustomerFilter,
  type VpnCustomerOperationalStatus,
} from "./vpnCustomerWorkspace";
import { VpnUsagePanel } from "./VpnUsagePanel";
import { matchesAbuseDeviceContext, type VpnAbuseDeviceContext } from "./vpnAbuse";

type NoticeType = "success" | "error";

type Props = {
  requestedSelection?: { customerId: number; deviceId: number } | null;
  usageEnabled?: boolean;
  customers: VpnCustomer[];
  subscriptions: VpnSubscription[];
  accessKeys: VpnAccessKey[];
  friendInvitations: VpnFriendInvitation[];
  plans: VpnPlan[];
  workers: WorkerNode[];
  reload: () => Promise<void>;
  notify: (type: NoticeType, text: string) => void;
};

type CustomerForm = {
  telegramUserId: string;
  telegramUsername: string;
  firstName: string;
  lastName: string;
  status: string;
  notes: string;
};

type SubscriptionForm = {
  planId: string;
  status: string;
  startsAt: string;
  expiresAt: string;
  trafficLimitGb: string;
  maxDevices: string;
  notes: string;
};

type AccessKeyForm = {
  workerId: string;
  protocol: string;
  displayName: string;
};

type AccessKeyDisplayOverride = Pick<
  VpnAccessKey,
  "display_name" | "config_uri" | "updated_at"
>;

const EMPTY_CUSTOMER_FORM: CustomerForm = {
  telegramUserId: "",
  telegramUsername: "",
  firstName: "",
  lastName: "",
  status: "active",
  notes: "",
};

const EMPTY_SUBSCRIPTION_FORM: SubscriptionForm = {
  planId: "",
  status: "active",
  startsAt: "",
  expiresAt: "",
  trafficLimitGb: "",
  maxDevices: "",
  notes: "",
};

const EMPTY_ACCESS_KEY_FORM: AccessKeyForm = {
  workerId: "",
  protocol: "vless",
  displayName: "",
};

const SUBSCRIPTION_STATUS_OPTIONS = [
  { value: "active", label: "Активна" },
  { value: "trial", label: "Тестовая" },
  { value: "disabled", label: "Приостановлена" },
  { value: "expired", label: "Истекла" },
  { value: "cancelled", label: "Отменена" },
];

const SUBSCRIPTION_STATUS_LABELS: Record<string, string> = Object.fromEntries(
  SUBSCRIPTION_STATUS_OPTIONS.map((item) => [item.value, item.label.toLocaleLowerCase("ru")]),
);

const CUSTOMER_FILTERS: Array<{ value: VpnCustomerFilter; label: string }> = [
  { value: "all", label: "Все" },
  { value: "active", label: "Активные" },
  { value: "expiring", label: "Скоро истекут" },
  { value: "suspended", label: "Приостановлены" },
  { value: "archived", label: "Архив" },
];

const CUSTOMER_STATUS_LABELS: Record<VpnCustomerOperationalStatus, string> = {
  active: "активен",
  expiring: "скоро истекает",
  suspended: "приостановлен",
  archived: "в архиве",
};

const CUSTOMER_RECORD_STATUS_LABELS: Record<string, string> = {
  active: "Активен",
  blocked: "Заблокирован",
  archived: "Архив",
};

const FRIEND_INVITATION_STATE_LABELS: Record<
  VpnFriendInvitation["invite_state"],
  string
> = {
  unused: "Не использовано",
  preparing: "Готовится",
  active: "Активно",
  failed: "Ошибка",
  needs_verification: "Требует проверки",
  expired: "Истекло",
  disabled: "Отключено",
};

function customerName(customer: VpnCustomer) {
  const name = [customer.first_name, customer.last_name].filter(Boolean).join(" ").trim();
  if (name) {
    return name;
  }
  if (customer.telegram_username) {
    return `@${customer.telegram_username}`;
  }
  if (customer.telegram_user_id) {
    return `Telegram ID ${customer.telegram_user_id}`;
  }
  return `клиент #${customer.id}`;
}

function customerStatusClass(status: VpnCustomerOperationalStatus) {
  if (status === "active") {
    return "status available";
  }
  if (status === "expiring") {
    return "status checking";
  }
  return "status inactive";
}

function customerFormFromRecord(customer: VpnCustomer): CustomerForm {
  return {
    telegramUserId: customer.telegram_user_id ?? "",
    telegramUsername: customer.telegram_username ?? "",
    firstName: customer.first_name ?? "",
    lastName: customer.last_name ?? "",
    status: customer.status,
    notes: customer.notes ?? "",
  };
}

function toDateTimeLocal(value: string | null) {
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  const local = new Date(parsed.getTime() - parsed.getTimezoneOffset() * 60_000);
  return local.toISOString().slice(0, 16);
}

function toIsoDateTime(value: string) {
  if (!value.trim()) {
    return null;
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? null : parsed.toISOString();
}

function optionalNumber(value: string) {
  return value.trim() ? Number(value) : null;
}

function subscriptionFormFromRecord(subscription: VpnSubscription): SubscriptionForm {
  return {
    planId: subscription.plan_id ? String(subscription.plan_id) : "",
    status: subscription.status,
    startsAt: toDateTimeLocal(subscription.starts_at),
    expiresAt: toDateTimeLocal(subscription.expires_at),
    trafficLimitGb:
      subscription.traffic_limit_gb === null ? "" : String(subscription.traffic_limit_gb),
    maxDevices: String(subscription.max_devices),
    notes: subscription.notes ?? "",
  };
}

function formatDateTime(value: string | null) {
  if (!value) {
    return "не ограничено";
  }
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString("ru-RU");
}

function maskAccessKeyUri(value: string) {
  const schemeEnd = value.indexOf("://");
  return schemeEnd === -1 ? "••••••••••••" : `${value.slice(0, schemeEnd + 3)}••••••••••••`;
}

function subscriptionStatusClass(status: string) {
  if (status === "active") {
    return "status available";
  }
  if (["trial", "pending_sync", "syncing", "pending_revoke"].includes(status)) {
    return "status checking";
  }
  if (status === "failed") {
    return "status error";
  }
  return "status inactive";
}

function friendInvitationStatusClass(state: VpnFriendInvitation["invite_state"]) {
  if (state === "active") {
    return "status available";
  }
  if (["preparing", "needs_verification"].includes(state)) {
    return "status checking";
  }
  if (state === "failed") {
    return "status error";
  }
  if (["expired", "disabled"].includes(state)) {
    return "status inactive";
  }
  return "status";
}

function friendInvitationIdentity(invitation: VpnFriendInvitation) {
  if (invitation.display_name) {
    return invitation.display_name;
  }
  if (invitation.telegram_username) {
    return `@${invitation.telegram_username.replace(/^@/, "")}`;
  }
  if (invitation.telegram_user_id) {
    return `Telegram ID ${invitation.telegram_user_id}`;
  }
  return invitation.can_rotate ? "Ожидает друга" : "Приглашение";
}

function mergeFriendInvitations(...pages: VpnFriendInvitation[][]) {
  return [...new Map(pages.flat().map((invitation) => [invitation.slot, invitation])).values()]
    .sort((left, right) => right.slot - left.slot);
}

export function VpnCustomerWorkspace({
  requestedSelection = null,
  usageEnabled = true,
  customers: loadedCustomers,
  subscriptions: loadedSubscriptions,
  accessKeys: loadedAccessKeys,
  friendInvitations,
  plans,
  workers,
  reload,
  notify,
}: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<VpnCustomerFilter>("all");
  const [selectedCustomerId, setSelectedCustomerId] = useState<number | null>(null);
  const [requestedContext, setRequestedContext] = useState<VpnAbuseDeviceContext | null>(null);
  const [selectionLoading, setSelectionLoading] = useState(false);
  const [selectionError, setSelectionError] = useState("");
  const selectionRequestRef = useRef<object | null>(null);
  const lastSelectionRef = useRef<Props["requestedSelection"]>(null);
  const selectionFocusRef = useRef<string | null>(null);
  const [customerForm, setCustomerForm] = useState<CustomerForm>(EMPTY_CUSTOMER_FORM);
  const [editingCustomer, setEditingCustomer] = useState(false);
  const [creatingCustomer, setCreatingCustomer] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [invitationLinks, setInvitationLinks] = useState<Record<number, string>>({});
  const [invitationRows, setInvitationRows] = useState(() => mergeFriendInvitations(friendInvitations));
  const [hasOlderInvitations, setHasOlderInvitations] = useState(friendInvitations.length === 100);
  const [loadingOlderInvitations, setLoadingOlderInvitations] = useState(false);
  const [olderInvitationsError, setOlderInvitationsError] = useState<string | null>(null);
  const issuingInvitationRef = useRef(false);
  const invitationPageRequestRef = useRef<object | null>(null);
  const invitationCursorRef = useRef<number | undefined>(undefined);
  const invitationSessionRef = useRef<object | null>(null);
  const [subscriptionForm, setSubscriptionForm] = useState<SubscriptionForm>(
    EMPTY_SUBSCRIPTION_FORM,
  );
  const [editingSubscriptionId, setEditingSubscriptionId] = useState<number | null>(null);
  const [creatingSubscription, setCreatingSubscription] = useState(false);
  const [accessKeyForm, setAccessKeyForm] = useState<AccessKeyForm>(EMPTY_ACCESS_KEY_FORM);
  const [creatingKeyForSubscriptionId, setCreatingKeyForSubscriptionId] = useState<
    number | null
  >(null);
  const [accessKeyIssueError, setAccessKeyIssueError] = useState<string | null>(null);
  const [createdAccessKeys, setCreatedAccessKeys] = useState<VpnAccessKey[]>([]);
  const [subscriptionDeviceUsage, setSubscriptionDeviceUsage] = useState<Record<number, VpnSubscriptionDeviceUsage | undefined>>({});
  const [deviceUsageErrors, setDeviceUsageErrors] = useState<Record<number, boolean>>({});
  const deviceUsageEpochRef = useRef(0);
  const deviceUsageRequestsRef = useRef(new Map<number, object>());
  const deviceUsageContextRef = useRef<{ customerId: number | null; subscriptionIds: Set<number> }>({
    customerId: null, subscriptionIds: new Set(),
  });
  const creatingAccessKeyRef = useRef(false);
  const accessKeyFormVersionRef = useRef(0);
  const [expandedKeyIds, setExpandedKeyIds] = useState<Set<number>>(() => new Set());
  const [editingAccessKeyId, setEditingAccessKeyId] = useState<number | null>(null);
  const [accessKeyName, setAccessKeyName] = useState("");
  const [pendingAccessKeyRenameIds, setPendingAccessKeyRenameIds] = useState<Set<number>>(
    () => new Set(),
  );
  const [accessKeyDisplayOverrides, setAccessKeyDisplayOverrides] = useState<
    Map<number, AccessKeyDisplayOverride>
  >(() => new Map());

  const customers = useMemo(() => requestedContext && !loadedCustomers.some((row) => row.id === requestedContext.customer.id)
    ? [...loadedCustomers, requestedContext.customer] : loadedCustomers, [loadedCustomers, requestedContext]);
  const subscriptions = useMemo(() => requestedContext && !loadedSubscriptions.some((row) => row.id === requestedContext.subscription.id)
    ? [...loadedSubscriptions, requestedContext.subscription] : loadedSubscriptions, [loadedSubscriptions, requestedContext]);
  const accessKeys = useMemo(() => {
    const key = requestedContext?.access_key;
    if (!key) return loadedAccessKeys;
    const loaded = loadedAccessKeys.find((row) => row.id === key.id);
    if (loaded && Date.parse(loaded.updated_at) >= Date.parse(key.updated_at)) return loadedAccessKeys;
    return [...loadedAccessKeys.filter((row) => row.id !== key.id), key];
  }, [loadedAccessKeys, requestedContext]);

  const filteredCustomers = useMemo(
    () => filterVpnCustomers(customers, subscriptions, filter, query),
    [customers, subscriptions, filter, query],
  );
  const selectedCustomer =
    customers.find((customer) => customer.id === selectedCustomerId) ?? null;
  const selectedSubscriptions = useMemo(
    () =>
      subscriptions
        .filter((subscription) => subscription.customer_id === selectedCustomerId)
        .sort((left, right) => Date.parse(right.updated_at) - Date.parse(left.updated_at)),
    [selectedCustomerId, subscriptions],
  );
  const selectedSubscriptionIds = useMemo(
    () => new Set(selectedSubscriptions.map((subscription) => subscription.id)),
    [selectedSubscriptions],
  );
  const selectedAccessKeys = useMemo(
    () =>
      [...accessKeys, ...createdAccessKeys.filter((created) => !accessKeys.some((key) => key.id === created.id))]
        .filter((accessKey) => selectedSubscriptionIds.has(accessKey.subscription_id))
        .map((accessKey) => {
          const display = accessKeyDisplayOverrides.get(accessKey.id);
          return display ? applyAccessKeyDisplay(accessKey, display) : accessKey;
        }),
    [accessKeys, createdAccessKeys, accessKeyDisplayOverrides, selectedSubscriptionIds],
  );
  const planMap = useMemo(() => new Map(plans.map((plan) => [plan.id, plan])), [plans]);
  const workerMap = useMemo(
    () => new Map(workers.map((worker) => [worker.id, worker])),
    [workers],
  );

  useLayoutEffect(() => {
    if (!usageEnabled || !requestedSelection || lastSelectionRef.current === requestedSelection) return;
    lastSelectionRef.current = requestedSelection;
    const pending = {};
    selectionRequestRef.current = pending;
    setSelectionLoading(true);
    setSelectionError("");
    setFilter("all");
    setQuery("");
    setCreatingCustomer(false);
    setEditingCustomer(false);
    setCreatingSubscription(false);
    setEditingSubscriptionId(null);
    setCreatingKeyForSubscriptionId(null);
    void api.getVpnAbuseDeviceContext(requestedSelection.deviceId, requestedSelection.customerId)
      .then((context) => {
        if (selectionRequestRef.current !== pending) return;
        if (!matchesAbuseDeviceContext(context, requestedSelection)) throw new Error("device context mismatch");
        selectionFocusRef.current = context.access_key
          ? `vpn-access-key-${context.access_key.id}` : `vpn-subscription-${context.subscription.id}`;
        setRequestedContext(context);
        setSelectedCustomerId(context.customer.id);
      })
      .catch(() => {
        if (selectionRequestRef.current === pending) setSelectionError("Не удалось открыть клиента и профиль. Повторите переход из сигнала");
      })
      .finally(() => {
        if (selectionRequestRef.current === pending) { selectionRequestRef.current = null; setSelectionLoading(false); }
      });
    return () => {
      if (selectionRequestRef.current === pending) lastSelectionRef.current = null;
      selectionRequestRef.current = null;
      selectionFocusRef.current = null;
      setSelectionLoading(false);
    };
  }, [requestedSelection, usageEnabled]);

  useLayoutEffect(() => {
    if (!selectionFocusRef.current || !usageEnabled) return;
    const destination = document.getElementById(selectionFocusRef.current);
    if (destination) {
      destination.focus({ preventScroll: true });
      destination.scrollIntoView({ block: "center", behavior: "auto" });
      selectionFocusRef.current = null;
    }
  }, [requestedContext, selectedCustomerId, usageEnabled]);

  function cancelRequestedSelection() {
    selectionRequestRef.current = null;
    selectionFocusRef.current = null;
    setSelectionLoading(false);
    setSelectionError("");
  }

  useEffect(() => {
    if (selectionRequestRef.current) return;
    if (
      selectedCustomerId &&
      filteredCustomers.some((customer) => customer.id === selectedCustomerId)
    ) {
      return;
    }
    setSelectedCustomerId(filteredCustomers[0]?.id ?? null);
    setEditingCustomer(false);
    setCreatingSubscription(false);
    setEditingSubscriptionId(null);
    setCreatingKeyForSubscriptionId(null);
  }, [filteredCustomers, selectedCustomerId]);

  useEffect(() => {
    setAccessKeyDisplayOverrides((current) =>
      reconcileAccessKeyDisplayOverrides(current, accessKeys),
    );
    setCreatedAccessKeys((current) => current.filter((created) => !accessKeys.some((key) => key.id === created.id)));
  }, [accessKeys]);

  useEffect(() => {
    accessKeyFormVersionRef.current += 1;
    setCreatingKeyForSubscriptionId(null);
    setAccessKeyIssueError(null);
  }, [selectedCustomerId]);

  useEffect(() => {
    deviceUsageEpochRef.current += 1;
    deviceUsageContextRef.current = { customerId: selectedCustomerId, subscriptionIds: selectedSubscriptionIds };
    deviceUsageRequestsRef.current.clear();
    setSubscriptionDeviceUsage({});
    setDeviceUsageErrors({});
    for (const subscription of selectedSubscriptions) {
      void refreshSubscriptionDeviceUsage(subscription.id, selectedCustomerId);
    }
    return () => {
      deviceUsageEpochRef.current += 1;
      deviceUsageRequestsRef.current.clear();
      deviceUsageContextRef.current = { customerId: null, subscriptionIds: new Set() };
    };
  }, [selectedCustomerId, selectedSubscriptions]);

  async function refreshSubscriptionDeviceUsage(subscriptionId: number, customerId = selectedCustomerId) {
    const context = deviceUsageContextRef.current;
    if (context.customerId !== customerId || !context.subscriptionIds.has(subscriptionId)) return;
    const epoch = deviceUsageEpochRef.current;
    const request = {};
    deviceUsageRequestsRef.current.set(subscriptionId, request);
    setSubscriptionDeviceUsage((current) => ({ ...current, [subscriptionId]: undefined }));
    setDeviceUsageErrors((current) => ({ ...current, [subscriptionId]: false }));
    try {
      const usage = await api.getVpnSubscriptionDeviceUsage(subscriptionId);
      if (deviceUsageEpochRef.current !== epoch || deviceUsageRequestsRef.current.get(subscriptionId) !== request) return;
      setSubscriptionDeviceUsage((current) => ({ ...current, [subscriptionId]: usage }));
    } catch {
      if (deviceUsageEpochRef.current !== epoch || deviceUsageRequestsRef.current.get(subscriptionId) !== request) return;
      setDeviceUsageErrors((current) => ({ ...current, [subscriptionId]: true }));
    }
  }

  useEffect(() => {
    invitationSessionRef.current = {};
    return () => { invitationSessionRef.current = null; };
  }, []);

  useEffect(() => {
    invitationPageRequestRef.current = null;
    invitationCursorRef.current = friendInvitations[friendInvitations.length - 1]?.slot;
    setLoadingOlderInvitations(false);
    setOlderInvitationsError(null);
    setHasOlderInvitations(friendInvitations.length === 100);
    setInvitationRows((current) => mergeFriendInvitations(
      current.filter((invitation) => invitationLinks[invitation.slot]),
      friendInvitations,
    ));
    forgetUnusableInvitationLinks(friendInvitations);
    return () => { invitationPageRequestRef.current = null; };
  }, [friendInvitations]);

  function forgetUnusableInvitationLinks(rows: VpnFriendInvitation[]) {
    const unusableSlots = new Set(
      rows.filter((invitation) => !(invitation.invite_state === "unused" && invitation.can_rotate))
        .map((invitation) => invitation.slot),
    );
    setInvitationLinks((current) => {
      const retained = Object.fromEntries(
        Object.entries(current).filter(([slot]) => !unusableSlots.has(Number(slot))),
      );
      return Object.keys(retained).length === Object.keys(current).length
        ? current
        : retained;
    });
  }

  async function loadOlderInvitations() {
    const beforeSlot = invitationCursorRef.current;
    if (invitationPageRequestRef.current || !hasOlderInvitations || beforeSlot === undefined) return;
    const request = {};
    invitationPageRequestRef.current = request;
    setLoadingOlderInvitations(true);
    setOlderInvitationsError(null);
    try {
      const page = await api.getVpnFriendInvitations({ beforeSlot });
      if (invitationPageRequestRef.current !== request) return;
      invitationCursorRef.current = page[page.length - 1]?.slot;
      setInvitationRows((current) => mergeFriendInvitations(current, page));
      forgetUnusableInvitationLinks(page);
      setHasOlderInvitations(page.length === 100);
    } catch {
      if (invitationPageRequestRef.current !== request) return;
      setOlderInvitationsError("Не удалось загрузить приглашения. Попробуйте ещё раз.");
    } finally {
      if (invitationPageRequestRef.current === request) {
        invitationPageRequestRef.current = null;
        setLoadingOlderInvitations(false);
      }
    }
  }

  async function reloadAfterSavedInvitation(failureMessage?: string) {
    const session = invitationSessionRef.current;
    if (!session) return;
    try {
      await reload();
    } catch {
      if (invitationSessionRef.current !== session) return;
      notify(
        "error",
        failureMessage ?? "Изменение сохранено, но список не обновился",
      );
    }
  }

  async function rememberIssuedInvitation(
    issued: VpnFriendInvitationIssued,
    successMessage: string,
  ) {
    setInvitationRows((current) => mergeFriendInvitations(current, [issued.invitation]));
    setInvitationLinks((current) => ({
      ...current,
      [issued.invitation.slot]: issued.invite_link,
    }));
    notify("success", successMessage);
    await reloadAfterSavedInvitation(
      "Приглашение создано, но список не обновился. Скопируйте показанную ссылку.",
    );
  }

  async function issueFriendInvitation() {
    const session = invitationSessionRef.current;
    if (!session || issuingInvitationRef.current || busyAction !== null) {
      return;
    }
    issuingInvitationRef.current = true;
    setBusyAction("friend-invitation-issue");
    try {
      const issued = await api.issueVpnFriendInvitation();
      if (invitationSessionRef.current !== session) return;
      await rememberIssuedInvitation(issued, "Приглашение создано");
    } catch (error) {
      if (invitationSessionRef.current !== session) return;
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось создать приглашение",
      );
    } finally {
      issuingInvitationRef.current = false;
      if (invitationSessionRef.current === session) setBusyAction(null);
    }
  }

  function forgetInvitationLink(slot: number) {
    setInvitationLinks((current) => {
      const next = { ...current };
      delete next[slot];
      return next;
    });
  }

  async function rotateFriendInvitation(invitation: VpnFriendInvitation) {
    const session = invitationSessionRef.current;
    if (!session || !invitation.can_rotate) {
      return;
    }
    setBusyAction(`friend-invitation-rotate-${invitation.slot}`);
    forgetInvitationLink(invitation.slot);
    try {
      const issued = await api.rotateVpnFriendInvitation(invitation.slot);
      if (invitationSessionRef.current !== session) return;
      await rememberIssuedInvitation(issued, `Ссылка приглашения #${invitation.slot} обновлена`);
    } catch (error) {
      if (invitationSessionRef.current !== session) return;
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось обновить приглашение",
      );
    } finally {
      if (invitationSessionRef.current === session) setBusyAction(null);
    }
  }

  async function retryFriendInvitation(invitation: VpnFriendInvitation) {
    const session = invitationSessionRef.current;
    if (!session || !invitation.can_retry) {
      return;
    }
    setBusyAction(`friend-invitation-retry-${invitation.slot}`);
    try {
      const saved = await api.retryVpnFriendInvitation(invitation.slot);
      if (invitationSessionRef.current !== session) return;
      setInvitationRows((current) => mergeFriendInvitations(current, [saved]));
      forgetUnusableInvitationLinks([saved]);
      notify("success", `Повторная выдача по приглашению #${invitation.slot} запланирована`);
      await reloadAfterSavedInvitation();
    } catch (error) {
      if (invitationSessionRef.current !== session) return;
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось повторить выдачу",
      );
    } finally {
      if (invitationSessionRef.current === session) setBusyAction(null);
    }
  }

  async function disableFriendInvitation(invitation: VpnFriendInvitation) {
    const session = invitationSessionRef.current;
    if (
      !session ||
      !invitation.can_disable ||
      !window.confirm(
        `Отключить участника по приглашению #${invitation.slot}? Он потеряет доступ к личному кабинету и VPN.`,
      )
    ) {
      return;
    }
    setBusyAction(`friend-invitation-disable-${invitation.slot}`);
    try {
      const saved = await api.disableVpnFriendInvitation(invitation.slot);
      if (invitationSessionRef.current !== session) return;
      setInvitationRows((current) => mergeFriendInvitations(current, [saved]));
      forgetInvitationLink(invitation.slot);
      notify("success", `Доступ по приглашению #${invitation.slot} отключён`);
      await reloadAfterSavedInvitation();
    } catch (error) {
      if (invitationSessionRef.current !== session) return;
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось отключить участника",
      );
    } finally {
      if (invitationSessionRef.current === session) setBusyAction(null);
    }
  }

  async function copyInvitationLink(slot: number) {
    const inviteLink = invitationLinks[slot];
    if (!inviteLink) {
      return;
    }
    try {
      await navigator.clipboard.writeText(inviteLink);
      notify("success", "Ссылка приглашения скопирована");
    } catch {
      notify("error", "Не удалось скопировать ссылку. Выделите её вручную.");
    }
  }

  function beginCustomerCreate() {
    cancelRequestedSelection();
    setCustomerForm(EMPTY_CUSTOMER_FORM);
    setEditingCustomer(false);
    setCreatingCustomer(true);
  }

  function beginCustomerEdit(customer: VpnCustomer) {
    cancelRequestedSelection();
    setCustomerForm(customerFormFromRecord(customer));
    setCreatingCustomer(false);
    setEditingCustomer(true);
  }

  async function saveCustomer(event: FormEvent) {
    event.preventDefault();
    const isEditing = Boolean(selectedCustomer && editingCustomer);
    setBusyAction(isEditing ? "customer-update" : "customer-create");
    try {
      const payload = {
        telegram_user_id: customerForm.telegramUserId.trim() || null,
        telegram_username:
          customerForm.telegramUsername.replace(/^@/, "").trim() || null,
        first_name: customerForm.firstName.trim() || null,
        last_name: customerForm.lastName.trim() || null,
        status: customerForm.status,
        notes: customerForm.notes.trim() || null,
      };
      const saved = isEditing
        ? await api.updateVpnCustomer(selectedCustomer!.id, payload)
        : await api.createVpnCustomer(payload);
      await reload();
      setSelectedCustomerId(saved.id);
      setEditingCustomer(false);
      setCreatingCustomer(false);
      setCustomerForm(EMPTY_CUSTOMER_FORM);
      notify("success", isEditing ? "Данные клиента сохранены" : "VPN клиент добавлен");
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось сохранить клиента",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function archiveCustomer(customer: VpnCustomer) {
    if (
      !window.confirm(
        `Перенести ${customerName(customer)} в архив? Подписки будут отключены, а VPN‑профили отправлены на отзыв.`,
      )
    ) {
      return;
    }
    setBusyAction("customer-archive");
    try {
      const result = await api.archiveVpnCustomer(customer.id);
      await reload();
      const pending = result.pending_revoke_keys
        ? `, ожидают отзыва: ${result.pending_revoke_keys}`
        : "";
      notify(
        "success",
        `Клиент перенесён в архив. Отозвано профилей: ${result.revoked_keys}${pending}`,
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось архивировать клиента",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function restoreCustomer(customer: VpnCustomer) {
    setBusyAction("customer-restore");
    try {
      await api.updateVpnCustomer(customer.id, { status: "active" });
      await reload();
      notify(
        "success",
        "Клиент восстановлен. Подписки и VPN‑профили нужно активировать отдельно.",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось восстановить клиента",
      );
    } finally {
      setBusyAction(null);
    }
  }

  function beginSubscriptionCreate() {
    cancelRequestedSelection();
    setSubscriptionForm(EMPTY_SUBSCRIPTION_FORM);
    setEditingSubscriptionId(null);
    setCreatingSubscription(true);
  }

  function beginSubscriptionEdit(subscription: VpnSubscription) {
    cancelRequestedSelection();
    setSubscriptionForm(subscriptionFormFromRecord(subscription));
    setCreatingSubscription(false);
    setEditingSubscriptionId(subscription.id);
  }

  async function saveSubscription(event: FormEvent) {
    event.preventDefault();
    if (!selectedCustomer) {
      return;
    }
    const editedSubscription = editingSubscriptionId
      ? selectedSubscriptions.find((subscription) => subscription.id === editingSubscriptionId) ??
        null
      : null;
    const actionId = editedSubscription
      ? `subscription-${editedSubscription.id}`
      : "subscription-create";
    setBusyAction(actionId);
    try {
      const planId = optionalNumber(subscriptionForm.planId);
      const startsAt = toIsoDateTime(subscriptionForm.startsAt);
      const expiresAt = toIsoDateTime(subscriptionForm.expiresAt);
      const trafficLimitGb = optionalNumber(subscriptionForm.trafficLimitGb);
      const maxDevices = optionalNumber(subscriptionForm.maxDevices);
      const payload: Record<string, unknown> = {
        plan_id: planId,
        status: subscriptionForm.status,
        notes: subscriptionForm.notes.trim() || null,
      };
      if (editedSubscription) {
        payload.starts_at = startsAt;
        payload.expires_at = expiresAt;
        payload.traffic_limit_gb = trafficLimitGb;
        payload.max_devices = maxDevices ?? editedSubscription.max_devices;
        await updateSubscriptionPolicy(editedSubscription.id, payload, "Подписка сохранена");
      } else {
        payload.customer_id = selectedCustomer.id;
        if (startsAt) {
          payload.starts_at = startsAt;
        }
        if (expiresAt) {
          payload.expires_at = expiresAt;
        }
        if (trafficLimitGb !== null) {
          payload.traffic_limit_gb = trafficLimitGb;
        }
        if (maxDevices !== null) {
          payload.max_devices = maxDevices;
        }
        await api.createVpnSubscription(payload);
        await reload();
        notify("success", "VPN подписка добавлена");
      }
      setCreatingSubscription(false);
      setEditingSubscriptionId(null);
      setSubscriptionForm(EMPTY_SUBSCRIPTION_FORM);
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось сохранить подписку",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function updateSubscriptionPolicy(
    subscriptionId: number,
    payload: Record<string, unknown>,
    savedMessage: string,
  ) {
    const usageCustomerId = selectedCustomerId;
    const result = await saveSubscriptionAndRequestSync(
      async () => {
        const saved = await api.updateVpnSubscription(subscriptionId, payload);
        void refreshSubscriptionDeviceUsage(subscriptionId, usageCustomerId);
        return saved;
      },
      () => api.runVpnLifecycleMaintenance(),
      reload,
    );
    const feedback = !result.refreshed
      ? "Обновите страницу, чтобы увидеть актуальный статус профилей."
      : !result.syncRequested
        ? "Синхронизация будет повторена автоматически. Проверьте статус профилей."
        : "Результат применения на VPN‑ноде — в статусе профилей ниже.";
    notify(result.refreshed && result.syncRequested ? "success" : "error", `${savedMessage}. ${feedback}`);
  }

  async function extendSubscription(subscription: VpnSubscription, days: 7 | 30 | 90) {
    const expiresAt = calculateExtendedExpiration(subscription.expires_at, days);
    const formatted = new Date(expiresAt).toLocaleString("ru-RU");
    if (
      !window.confirm(
        `Продлить подписку #${subscription.id} на ${days} дней, до ${formatted}, и активировать её?`,
      )
    ) {
      return;
    }
    setBusyAction(`subscription-${subscription.id}`);
    try {
      await updateSubscriptionPolicy(subscription.id, {
        expires_at: expiresAt,
        status: "active",
      }, `Подписка продлена до ${formatted}`);
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось продлить подписку",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function suspendSubscription(subscription: VpnSubscription) {
    if (
      !window.confirm(
        `Приостановить подписку #${subscription.id}? Доступ отключится после синхронизации. При продлении прежние ссылки заработают снова.`,
      )
    ) {
      return;
    }
    setBusyAction(`subscription-${subscription.id}`);
    try {
      await updateSubscriptionPolicy(subscription.id, { status: "disabled" }, "Приостановка подписки сохранена");
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось приостановить подписку",
      );
    } finally {
      setBusyAction(null);
    }
  }

  function beginAccessKeyCreate(subscription: VpnSubscription) {
    cancelRequestedSelection();
    accessKeyFormVersionRef.current += 1;
    setAccessKeyForm(EMPTY_ACCESS_KEY_FORM);
    setAccessKeyIssueError(null);
    setCreatingKeyForSubscriptionId(subscription.id);
  }

  async function saveAccessKey(event: FormEvent) {
    event.preventDefault();
    if (!creatingKeyForSubscriptionId || creatingAccessKeyRef.current) {
      return;
    }
    const subscription = selectedSubscriptions.find((item) => item.id === creatingKeyForSubscriptionId);
    if (!subscription) {
      return;
    }
    const usage = subscriptionDeviceUsage[subscription.id];
    if (!usage) return;
    if (usage.devices_used >= usage.max_devices) {
      setAccessKeyIssueError(vpnProfileIssueError("VPN device limit reached"));
      return;
    }
    creatingAccessKeyRef.current = true;
    const formVersion = accessKeyFormVersionRef.current;
    const usageCustomerId = selectedCustomerId;
    setAccessKeyIssueError(null);
    setBusyAction(`key-create-${subscription.id}`);
    try {
      let accessKey: VpnAccessKey;
      try {
        accessKey = await api.createVpnAccessKey({
          subscription_id: subscription.id,
          worker_id: optionalNumber(accessKeyForm.workerId),
          protocol: accessKeyForm.protocol,
          display_name: accessKeyForm.displayName.trim(),
        });
      } catch (error) {
        void refreshSubscriptionDeviceUsage(subscription.id, usageCustomerId);
        if (accessKeyFormVersionRef.current === formVersion) {
          setAccessKeyIssueError(vpnProfileIssueError(error));
        }
        return;
      }
      void refreshSubscriptionDeviceUsage(subscription.id, usageCustomerId);
      setCreatedAccessKeys((current) => [...current, accessKey]);
      if (accessKeyFormVersionRef.current === formVersion) {
        setCreatingKeyForSubscriptionId(null);
        setAccessKeyForm(EMPTY_ACCESS_KEY_FORM);
      }
      const accessReady = accessKey.status === "active" && Boolean(accessKey.config_uri);
      const savedMessage = accessReady
        ? `VPN доступ выдан для подписки #${subscription.id}`
        : `Профиль сохранён для подписки #${subscription.id}. Статус: ${accessKeyStatusLabel(accessKey.status)}`;
      let refreshed = true;
      try {
        await reload();
      } catch {
        refreshed = false;
      }
      notify(
        refreshed && accessReady ? "success" : "error",
        refreshed
          ? savedMessage
          : `${savedMessage}. Список не обновился. Обновите страницу, чтобы проверить состояние профиля`,
      );
    } finally {
      creatingAccessKeyRef.current = false;
      setBusyAction((current) => current === `key-create-${subscription.id}` ? null : current);
    }
  }

  async function retryAccessKeyProvision(accessKey: VpnAccessKey) {
    setBusyAction(`key-provision-${accessKey.id}`);
    try {
      const provisioned = await api.provisionVpnAccessKey(accessKey.id);
      setRequestedContext((current) => current?.access_key?.id === provisioned.id ? { ...current, access_key: provisioned } : current);
      await reload();
      notify(
        provisioned.status === "active" ? "success" : "error",
        provisioned.status === "active"
          ? "VPN доступ выдан"
          : provisioned.last_error || "Профиль пока не выдан на VPN‑ноду",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось повторить выдачу VPN‑профиля",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function revokeAccessKey(accessKey: VpnAccessKey, requireConfirmation = true) {
    const keyName = accessKey.display_name;
    if (
      requireConfirmation &&
      !window.confirm(
        `Отключить VPN‑профиль ${keyName} на 3x-UI? Запись останется в панели для истории.`,
      )
    ) {
      return;
    }
    setBusyAction(`key-revoke-${accessKey.id}`);
    const usageCustomerId = selectedCustomerId;
    try {
      const revoked = await api.revokeVpnAccessKey(accessKey.id);
      setRequestedContext((current) => current?.access_key?.id === revoked.id ? { ...current, access_key: revoked } : current);
      void refreshSubscriptionDeviceUsage(accessKey.subscription_id, usageCustomerId);
      await reload();
      notify(
        revoked.status === "pending_revoke" ? "error" : "success",
        revoked.status === "pending_revoke"
          ? revoked.last_error || "Профиль ожидает повторного отзыва"
          : "VPN‑профиль отключён",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось отключить VPN‑профиль",
      );
    } finally {
      setBusyAction(null);
    }
  }

  function beginAccessKeyRename(accessKey: VpnAccessKey) {
    cancelRequestedSelection();
    setEditingAccessKeyId(accessKey.id);
    setAccessKeyName(accessKey.display_name);
  }

  function cancelAccessKeyRename() {
    setEditingAccessKeyId(null);
    setAccessKeyName("");
  }

  async function renameAccessKey(event: FormEvent, accessKey: VpnAccessKey) {
    event.preventDefault();
    const displayName = accessKeyName.trim();
    if (!displayName) {
      return;
    }
    setPendingAccessKeyRenameIds((current) => new Set(current).add(accessKey.id));
    try {
      const result = await saveAccessKeyDisplayName(
        () => api.renameVpnAccessKey(accessKey.id, displayName),
        reload,
        (renamedAccessKey) => {
          setAccessKeyDisplayOverrides((current) => {
            const next = new Map(current);
            next.set(renamedAccessKey.id, {
              display_name: renamedAccessKey.display_name,
              config_uri: renamedAccessKey.config_uri,
              updated_at: renamedAccessKey.updated_at,
            });
            return next;
          });
        },
      );
      setEditingAccessKeyId((current) =>
        nextAccessKeyEditorAfterRename(current, accessKey.id),
      );
      notify(
        result.refreshed ? "success" : "error",
        result.refreshed
          ? "Название профиля сохранено"
          : "Название профиля сохранено, но список не обновлён. Повторите обновление страницы.",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось изменить название профиля",
      );
    } finally {
      setPendingAccessKeyRenameIds((current) => {
        const next = new Set(current);
        next.delete(accessKey.id);
        return next;
      });
    }
  }

  function toggleAccessKeyUri(accessKeyId: number) {
    setExpandedKeyIds((current) => {
      const next = new Set(current);
      if (next.has(accessKeyId)) {
        next.delete(accessKeyId);
      } else {
        next.add(accessKeyId);
      }
      return next;
    });
  }

  async function copyAccessKey(accessKey: VpnAccessKey) {
    if (!accessKey.config_uri) {
      return;
    }
    try {
      await navigator.clipboard.writeText(accessKey.config_uri);
      notify("success", "Полная VPN‑ссылка скопирована");
    } catch {
      setExpandedKeyIds((current) => new Set(current).add(accessKey.id));
      await new Promise<void>((resolve) => window.requestAnimationFrame(() => resolve()));
      const field = document.getElementById(
        `vpn-key-uri-${accessKey.id}`,
      ) as HTMLTextAreaElement | null;
      const canSelect = field?.value === accessKey.config_uri;
      if (canSelect) {
        field.focus();
        field.select();
      }
      const selected = canSelect
        && document.activeElement === field
        && field.selectionStart === 0
        && field.selectionEnd === field.value.length;
      notify(
        "error",
        selected
          ? "Буфер обмена недоступен. Полная ссылка выделена — скопируйте её вручную."
          : "Не удалось скопировать или выделить ссылку. Обновите профиль и попробуйте снова.",
      );
    }
  }

  function renderFriendInvitations() {
    return (
      <section className="vpn-workspace-section vpn-friend-invitations">
        <div className="vpn-workspace-section-head">
          <div>
            <h3>Приглашения</h3>
            <p className="muted">
              На активацию — 7 дней, доступ после активации — 7 дней.
              Ссылка показывается только после создания или обновления. Скопируйте её до выхода из раздела.
            </p>
          </div>
          <button
            type="button"
            disabled={busyAction !== null}
            onClick={() => void issueFriendInvitation()}
          >
            Создать приглашение
          </button>
        </div>
        <div className="vpn-friend-invitation-list">
          {invitationRows.length === 0 ? (
            <p className="empty">Приглашений пока нет.</p>
          ) : null}
          {invitationRows.map((invitation) => {
            const inviteLink = invitationLinks[invitation.slot];
            const actionBusy = busyAction !== null;
            return (
              <article key={invitation.slot} className="vpn-friend-invitation-row">
                <strong className="vpn-friend-invitation-slot">#{invitation.slot}</strong>
                <div className="vpn-friend-invitation-main">
                  <strong>{friendInvitationIdentity(invitation)}</strong>
                  {invitation.telegram_username && invitation.display_name ? (
                    <span className="row-hint">
                      @{invitation.telegram_username.replace(/^@/, "")}
                    </span>
                  ) : null}
                  {invitation.subscription_expires_at ? (
                    <span className="row-hint">
                      Доступ до {formatDateTime(invitation.subscription_expires_at)}
                    </span>
                  ) : null}
                  {invitation.provisioning_error_code ? (
                    <span className="error-text">
                      Код выдачи: {invitation.provisioning_error_code}
                    </span>
                  ) : null}
                </div>
                <div className="vpn-friend-invitation-controls">
                  <span className={friendInvitationStatusClass(invitation.invite_state)}>
                    {FRIEND_INVITATION_STATE_LABELS[invitation.invite_state]}
                  </span>
                  <div className="actions">
                    {invitation.can_rotate ? (
                      <button
                        type="button"
                        className="ghost"
                        disabled={actionBusy}
                        onClick={() => void rotateFriendInvitation(invitation)}
                      >
                        Обновить ссылку
                      </button>
                    ) : null}
                    {invitation.can_retry ? (
                      <button
                        type="button"
                        className="ghost"
                        disabled={actionBusy}
                        onClick={() => void retryFriendInvitation(invitation)}
                      >
                        Повторить выдачу
                      </button>
                    ) : null}
                    {invitation.can_disable ? (
                      <button
                        type="button"
                        className="danger"
                        disabled={actionBusy}
                        onClick={() => void disableFriendInvitation(invitation)}
                      >
                        Отключить
                      </button>
                    ) : null}
                  </div>
                </div>
                {inviteLink ? (
                  <div className="vpn-friend-invitation-link-block">
                    <code className="vpn-friend-invitation-link">{inviteLink}</code>
                    <div className="actions">
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => void copyInvitationLink(invitation.slot)}
                      >
                        Копировать ссылку
                      </button>
                    </div>
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
        {olderInvitationsError ? <p className="error-text" role="alert">{olderInvitationsError}</p> : null}
        {hasOlderInvitations ? (
          <button type="button" className="ghost" disabled={loadingOlderInvitations}
            onClick={() => void loadOlderInvitations()} aria-busy={loadingOlderInvitations}>
            {loadingOlderInvitations ? "Загружаем приглашения…" : "Загрузить ещё"}
          </button>
        ) : null}
      </section>
    );
  }

  function renderCustomerFields() {
    const statusOptions = customerStatusOptions(
      editingCustomer ? selectedCustomer?.status : null,
    );
    return (
      <>
        <div className="form two-columns">
          <label>
            <span>Telegram ID</span>
            <input
              value={customerForm.telegramUserId}
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  telegramUserId: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>Telegram username</span>
            <input
              value={customerForm.telegramUsername}
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  telegramUsername: event.target.value,
                }))
              }
              placeholder="@username"
            />
          </label>
          <label>
            <span>Имя</span>
            <input
              value={customerForm.firstName}
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  firstName: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>Фамилия</span>
            <input
              value={customerForm.lastName}
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  lastName: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>Статус</span>
            <select
              value={customerForm.status}
              disabled={statusOptions.length === 1}
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  status: event.target.value,
                }))
              }
            >
              {statusOptions.map((status) => (
                <option key={status} value={status}>
                  {CUSTOMER_RECORD_STATUS_LABELS[status]}
                </option>
              ))}
            </select>
          </label>
        </div>
        <label>
          <span>Заметки</span>
          <textarea
            rows={3}
            value={customerForm.notes}
            onChange={(event) =>
              setCustomerForm((current) => ({ ...current, notes: event.target.value }))
            }
          />
        </label>
      </>
    );
  }

  function renderSubscriptionForm(title: string) {
    const busyId = editingSubscriptionId
      ? `subscription-${editingSubscriptionId}`
      : "subscription-create";
    return (
      <form className="form vpn-inline-form" onSubmit={saveSubscription}>
        <h4>{title}</h4>
        <div className="form two-columns">
          <label>
            <span>Тариф</span>
            <select
              value={subscriptionForm.planId}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  planId: event.target.value,
                }))
              }
            >
              <option value="">Без тарифа</option>
              {plans.map((plan) => (
                <option key={plan.id} value={plan.id}>
                  {plan.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Статус</span>
            <select
              value={subscriptionForm.status}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  status: event.target.value,
                }))
              }
            >
              {SUBSCRIPTION_STATUS_OPTIONS.map((item) => (
                <option key={item.value} value={item.value}>
                  {item.label}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Начало</span>
            <input
              type="datetime-local"
              value={subscriptionForm.startsAt}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  startsAt: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>Окончание</span>
            <input
              type="datetime-local"
              value={subscriptionForm.expiresAt}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  expiresAt: event.target.value,
                }))
              }
            />
          </label>
          <label>
            <span>Трафик, GB</span>
            <input
              type="number"
              min="1"
              value={subscriptionForm.trafficLimitGb}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  trafficLimitGb: event.target.value,
                }))
              }
              placeholder="Без лимита"
            />
          </label>
          <label>
            <span>Устройств</span>
            <input
              type="number"
              min="1"
              max="100"
              value={subscriptionForm.maxDevices}
              onChange={(event) =>
                setSubscriptionForm((current) => ({
                  ...current,
                  maxDevices: event.target.value,
                }))
              }
              placeholder="Из тарифа / 1"
            />
          </label>
        </div>
        <label>
          <span>Заметки</span>
          <textarea
            rows={2}
            value={subscriptionForm.notes}
            onChange={(event) =>
              setSubscriptionForm((current) => ({
                ...current,
                notes: event.target.value,
              }))
            }
          />
        </label>
        <div className="actions">
          <button type="submit" disabled={busyAction === busyId}>
            Сохранить подписку
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              setCreatingSubscription(false);
              setEditingSubscriptionId(null);
              setSubscriptionForm(EMPTY_SUBSCRIPTION_FORM);
            }}
          >
            Отмена
          </button>
        </div>
      </form>
    );
  }

  function renderAccessKeyForm(subscription: VpnSubscription) {
    const usage = subscriptionDeviceUsage[subscription.id];
    const deviceLimitReached = Boolean(usage && usage.devices_used >= usage.max_devices);
    const usageError = deviceUsageErrors[subscription.id];
    return (
      <form className="form vpn-inline-form" onSubmit={saveAccessKey}>
        <h4>Новый VPN‑профиль для подписки #{subscription.id}</h4>
        {usage ? <p className="muted">Занято мест для устройств: {usage.devices_used} из {usage.max_devices}</p> : (
          <p className="muted" role={usageError ? "alert" : "status"}>
            {usageError ? "Не удалось проверить занятые места" : "Проверяем занятые места"}
          </p>
        )}
        {usageError ? <button type="button" className="ghost"
          onClick={() => void refreshSubscriptionDeviceUsage(subscription.id)}>Проверить ещё раз</button> : null}
        {accessKeyIssueError || deviceLimitReached || !usage ? (
          <div role={accessKeyIssueError || deviceLimitReached ? "alert" : undefined}>
            {accessKeyIssueError || deviceLimitReached ? <p>{accessKeyIssueError ?? vpnProfileIssueError("VPN device limit reached")}</p> : null}
            <button type="button" className="ghost" onClick={() => beginSubscriptionEdit(subscription)}>
              Изменить лимит подписки
            </button>
          </div>
        ) : null}
        <div className="form two-columns">
          <label>
            <span>VPN‑нода</span>
            <select
              value={accessKeyForm.workerId}
              onChange={(event) =>
                setAccessKeyForm((current) => ({
                  ...current,
                  workerId: event.target.value,
                }))
              }
            >
              <option value="">Автоматически</option>
              {workers.map((worker) => (
                <option key={worker.id} value={worker.id}>
                  {worker.name} · {worker.ip_address ?? "без IP"}
                </option>
              ))}
            </select>
          </label>
          <label>
            <span>Протокол</span>
            <select
              value={accessKeyForm.protocol}
              onChange={(event) =>
                setAccessKeyForm((current) => ({
                  ...current,
                  protocol: event.target.value,
                }))
              }
            >
              <option value="vless">VLESS</option>
              <option value="vmess">VMess</option>
            </select>
          </label>
          <label>
            <span>Название</span>
            <input
              value={accessKeyForm.displayName}
              required
              maxLength={64}
              onChange={(event) =>
                setAccessKeyForm((current) => ({
                  ...current,
                  displayName: event.target.value,
                }))
              }
              placeholder="Например: телефон"
            />
          </label>
        </div>
        <div className="actions">
          <button
            type="submit"
            disabled={!usage || deviceLimitReached || creatingAccessKeyRef.current}
          >
            Выдать профиль
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => {
              accessKeyFormVersionRef.current += 1;
              setCreatingKeyForSubscriptionId(null);
              setAccessKeyIssueError(null);
            }}
          >
            Отмена
          </button>
        </div>
      </form>
    );
  }

  function renderAccessKey(accessKey: VpnAccessKey) {
    const expanded = expandedKeyIds.has(accessKey.id);
    const renameBusy = pendingAccessKeyRenameIds.has(accessKey.id);
    const workerName = accessKey.worker_id
      ? workerMap.get(accessKey.worker_id)?.name ?? `нода #${accessKey.worker_id}`
      : "автовыбор / не назначена";
    return (
      <article key={accessKey.id} className="vpn-key-card" id={`vpn-access-key-${accessKey.id}`} tabIndex={-1}
        data-vpn-device-id={accessKey.device_id ?? (accessKey.id === requestedContext?.access_key?.id ? requestedContext.device_id : undefined)}>
        <div className="vpn-workspace-section-head">
          <div>
            {editingAccessKeyId === accessKey.id ? (
              <form
                className="vpn-key-rename-form"
                onSubmit={(event) => void renameAccessKey(event, accessKey)}
              >
                <input
                  aria-label={`Название профиля ${accessKey.display_name}`}
                  value={accessKeyName}
                  required
                  maxLength={64}
                  onChange={(event) => setAccessKeyName(event.target.value)}
                />
                <div className="actions">
                  <button
                    type="submit"
                    disabled={renameBusy}
                  >
                    Сохранить
                  </button>
                  <button
                    type="button"
                    className="ghost"
                    disabled={renameBusy}
                    onClick={cancelAccessKeyRename}
                  >
                    Отмена
                  </button>
                </div>
              </form>
            ) : (
              <div className="vpn-key-title-row">
                <strong>{accessKey.display_name}</strong>
                <button
                  type="button"
                  className="ghost"
                  onClick={() => beginAccessKeyRename(accessKey)}
                >
                  Изменить название
                </button>
              </div>
            )}
            <div className="row-hint">
              {accessKey.protocol.toUpperCase()} · {workerName}
            </div>
          </div>
          <span className={subscriptionStatusClass(accessKey.status)}>
            {accessKeyStatusLabel(accessKey.status)}
          </span>
        </div>
        <div className="vpn-key-meta">
          <div>
            <span className="muted">Выдан</span>
            <strong>{formatDateTime(accessKey.issued_at)}</strong>
          </div>
          <div>
            <span className="muted">Действует до</span>
            <strong>{formatDateTime(accessKey.expires_at)}</strong>
          </div>
          <div>
            <span className="muted">UUID</span>
            <strong>{accessKey.external_uuid ?? "будет создан"}</strong>
          </div>
        </div>
        {renameBusy ? (
          <p className="muted">Обновляем подпись ссылки…</p>
        ) : accessKey.config_uri ? (
          <div className="vpn-key-link-block">
            <code className="vpn-key-preview">{maskAccessKeyUri(accessKey.config_uri)}</code>
            <div className="actions">
              <button type="button" className="ghost" onClick={() => void copyAccessKey(accessKey)}>
                Скопировать ссылку
              </button>
              <button
                type="button"
                className="ghost"
                aria-expanded={expanded}
                aria-controls={`vpn-key-uri-${accessKey.id}`}
                onClick={() => toggleAccessKeyUri(accessKey.id)}
              >
                {expanded ? "Скрыть ссылку" : "Показать полную ссылку"}
              </button>
            </div>
            {expanded ? (
              <textarea
                id={`vpn-key-uri-${accessKey.id}`}
                className="vpn-key-uri"
                value={accessKey.config_uri}
                readOnly
                spellCheck={false}
                aria-label={`Полная ссылка профиля ${accessKey.display_name}`}
              />
            ) : null}
          </div>
        ) : (
          <p className="muted">
            {accessKey.status === "revoked"
              ? "Профиль отозван, ссылка больше недоступна."
              : "Ссылка появится после успешной выдачи профиля на VPN‑ноду."}
          </p>
        )}
        {["suspended", "pending_suspend"].includes(accessKey.status) ? (
          <p className="muted">
            {accessKey.status === "suspended"
              ? "Доступ приостановлен. После продления подписки эта же ссылка восстановится; повторный импорт не нужен."
              : "Отключение ещё не подтверждено нодой. Система повторит попытку автоматически."}
          </p>
        ) : null}
        {accessKey.last_error && accessKey.status !== "revoked" ? (
          <p className="error-text">{accessKey.last_error}</p>
        ) : null}
        <div className="actions">
          {["pending_sync", "failed"].includes(accessKey.status) ? (
            <button
              type="button"
              className="ghost"
              disabled={renameBusy || busyAction === `key-provision-${accessKey.id}`}
              onClick={() => void retryAccessKeyProvision(accessKey)}
            >
              Повторить синхронизацию
            </button>
          ) : null}
          {accessKey.status === "pending_revoke" ? (
            <button
              type="button"
              className="ghost"
              disabled={renameBusy || busyAction === `key-revoke-${accessKey.id}`}
              onClick={() => void revokeAccessKey(accessKey, false)}
            >
              Повторить отзыв
            </button>
          ) : null}
          {["active", "pending_sync", "failed", "suspended", "pending_suspend"].includes(accessKey.status) ? (
            <button
              type="button"
              className="danger"
              disabled={renameBusy || busyAction === `key-revoke-${accessKey.id}`}
              onClick={() => void revokeAccessKey(accessKey)}
            >
              Отозвать и сохранить
            </button>
          ) : null}
        </div>
      </article>
    );
  }

  return (
    <div className="vpn-customer-workspace vpn-admin-workspace">
      <aside className="vpn-customer-sidebar vpn-admin-panel">
        <div className="vpn-workspace-toolbar">
          <strong>Клиенты</strong>
          <button type="button" onClick={beginCustomerCreate}>
            Новый клиент
          </button>
        </div>
        <label className="vpn-customer-search">
          <span>Поиск</span>
          <input
            value={query}
            onChange={(event) => { cancelRequestedSelection(); setQuery(event.target.value); }}
            placeholder="Имя, @username или Telegram ID"
          />
        </label>
        <div className="vpn-customer-filters" aria-label="Фильтр клиентов">
          {CUSTOMER_FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={filter === item.value ? "secondary active-chip" : "ghost"}
              onClick={() => { cancelRequestedSelection(); setFilter(item.value); }}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="vpn-customer-list">
          {filteredCustomers.map((customer) => {
            const primary = selectPrimarySubscription(customer.id, subscriptions);
            const operationalStatus = classifyVpnCustomer(customer, subscriptions);
            return (
              <button
                key={customer.id}
                type="button"
                className={`vpn-customer-row ${selectedCustomerId === customer.id ? "active-chip" : ""}`}
                onClick={() => {
                  cancelRequestedSelection();
                  setSelectedCustomerId(customer.id);
                  setCreatingCustomer(false);
                  setEditingCustomer(false);
                }}
              >
                <span className="vpn-customer-row-main">
                  <strong>{customerName(customer)}</strong>
                  <span className="row-hint">
                    {customer.telegram_username
                      ? `@${customer.telegram_username}`
                      : customer.telegram_user_id ?? "без Telegram"}
                  </span>
                  <span className="row-hint">
                    {primary ? `подписка #${primary.id}` : "без подписки"}
                  </span>
                </span>
                <span className={customerStatusClass(operationalStatus)}>
                  {CUSTOMER_STATUS_LABELS[operationalStatus]}
                </span>
              </button>
            );
          })}
          {filteredCustomers.length === 0 ? (
            <p className="empty">По этому фильтру клиентов нет.</p>
          ) : null}
        </div>
      </aside>

      <section className="vpn-customer-detail vpn-admin-panel">
        {selectionLoading && <p role="status">Открываем клиента и логический профиль…</p>}
        {selectionError && <p role="alert">{selectionError}</p>}
        {renderFriendInvitations()}
        {creatingCustomer ? (
          <form className="form vpn-workspace-section" onSubmit={saveCustomer}>
            <h3>Новый клиент</h3>
            {renderCustomerFields()}
            <div className="actions">
              <button type="submit" disabled={busyAction === "customer-create"}>
                Создать
              </button>
              <button
                type="button"
                className="ghost"
                onClick={() => setCreatingCustomer(false)}
              >
                Отмена
              </button>
            </div>
          </form>
        ) : selectedCustomer ? (
          <div className="vpn-workspace-stack">
            <section className="vpn-workspace-section">
              <div className="vpn-workspace-section-head">
                <div>
                  <h3>{customerName(selectedCustomer)}</h3>
                  <p className="muted">
                    {selectedCustomer.telegram_username
                      ? `@${selectedCustomer.telegram_username}`
                      : selectedCustomer.telegram_user_id ?? "Telegram не указан"}
                  </p>
                </div>
                <div className="actions">
                  {!editingCustomer ? (
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => beginCustomerEdit(selectedCustomer)}
                    >
                      Изменить
                    </button>
                  ) : null}
                  {selectedCustomer.status === "archived" ? (
                    <button
                      type="button"
                      className="ghost"
                      disabled={busyAction === "customer-restore"}
                      onClick={() => void restoreCustomer(selectedCustomer)}
                    >
                      Восстановить
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="danger"
                      disabled={busyAction === "customer-archive"}
                      onClick={() => void archiveCustomer(selectedCustomer)}
                    >
                      В архив
                    </button>
                  )}
                </div>
              </div>
              {editingCustomer ? (
                <form className="form" onSubmit={saveCustomer}>
                  {renderCustomerFields()}
                  <div className="actions">
                    <button type="submit" disabled={busyAction === "customer-update"}>
                      Сохранить
                    </button>
                    <button
                      type="button"
                      className="ghost"
                      onClick={() => setEditingCustomer(false)}
                    >
                      Отмена
                    </button>
                  </div>
                </form>
              ) : (
                <p>{selectedCustomer.notes || "Заметок нет"}</p>
              )}
            </section>

            {usageEnabled ? <VpnUsagePanel key={selectedCustomer.id} customerId={selectedCustomer.id} /> : null}
            <section className="vpn-workspace-section">
              <div className="vpn-workspace-section-head">
                <div>
                  <h3>Подписки и профили</h3>
                  <p className="muted">
                    Изменяйте срок и лимиты, затем управляйте VPN‑профилями конкретной подписки.
                  </p>
                </div>
                {selectedCustomer.status !== "archived" && !creatingSubscription ? (
                  <button type="button" onClick={beginSubscriptionCreate}>
                    Новая подписка
                  </button>
                ) : null}
              </div>

              {creatingSubscription
                ? renderSubscriptionForm(`Новая подписка для ${customerName(selectedCustomer)}`)
                : null}

              <div className="vpn-subscription-list">
                {selectedSubscriptions.map((subscription) => {
                  const plan = subscription.plan_id
                    ? planMap.get(subscription.plan_id) ?? null
                    : null;
                  const subscriptionKeys = selectedAccessKeys.filter(
                    (accessKey) => accessKey.subscription_id === subscription.id,
                  );
                  const subscriptionBusy = busyAction === `subscription-${subscription.id}`;
                  const usage = subscriptionDeviceUsage[subscription.id];
                  const isUsable = ["active", "trial"].includes(subscription.status);
                  return (
                    <article key={subscription.id} className="vpn-subscription-card" id={`vpn-subscription-${subscription.id}`} tabIndex={-1}>
                      <div className="vpn-workspace-section-head">
                        <div>
                          <strong>
                            Подписка #{subscription.id} · {plan?.name ?? "без тарифа"}
                          </strong>
                          <div className="row-hint">
                            {formatDateTime(subscription.starts_at)} → {formatDateTime(subscription.expires_at)}
                          </div>
                        </div>
                        <span className={subscriptionStatusClass(subscription.status)}>
                          {SUBSCRIPTION_STATUS_LABELS[subscription.status] ?? subscription.status}
                        </span>
                      </div>

                      <div className="vpn-subscription-meta">
                        <div>
                          <span className="muted">Трафик</span>
                          <strong>
                            {subscription.traffic_limit_gb === null
                              ? "без лимита"
                              : `${subscription.traffic_limit_gb} GB`}
                          </strong>
                        </div>
                        <div>
                          <span className="muted">Устройств</span>
                          <strong>{usage ? `${usage.devices_used} из ${usage.max_devices}`
                            : deviceUsageErrors[subscription.id] ? "Не удалось проверить занятые места" : "Проверяем занятые места"}</strong>
                          {deviceUsageErrors[subscription.id] ? <button type="button" className="ghost"
                            onClick={() => void refreshSubscriptionDeviceUsage(subscription.id)}>Проверить ещё раз</button> : null}
                        </div>
                        <div>
                          <span className="muted">Заметки</span>
                          <strong>{subscription.notes || "—"}</strong>
                        </div>
                      </div>

                      {selectedCustomer.status !== "archived" ? (
                        <div className="actions">
                          <button
                            type="button"
                            className="ghost"
                            disabled={subscriptionBusy}
                            onClick={() => beginSubscriptionEdit(subscription)}
                          >
                            Изменить
                          </button>
                          {([7, 30, 90] as const).map((days) => (
                            <button
                              key={days}
                              type="button"
                              className="ghost"
                              disabled={subscriptionBusy}
                              onClick={() => void extendSubscription(subscription, days)}
                            >
                              +{days} дней
                            </button>
                          ))}
                          {isUsable ? (
                            <button
                              type="button"
                              className="danger"
                              disabled={subscriptionBusy}
                              onClick={() => void suspendSubscription(subscription)}
                            >
                              Приостановить
                            </button>
                          ) : null}
                        </div>
                      ) : null}

                      {editingSubscriptionId === subscription.id
                        ? renderSubscriptionForm(`Изменить подписку #${subscription.id}`)
                        : null}

                      <div className="vpn-key-section">
                        <div className="vpn-workspace-section-head">
                          <strong>VPN‑профили</strong>
                          {selectedCustomer.status !== "archived" &&
                          isUsable &&
                          creatingKeyForSubscriptionId !== subscription.id ? (
                            <button
                              type="button"
                              className="ghost"
                              onClick={() => beginAccessKeyCreate(subscription)}
                            >
                              Выдать новый профиль
                            </button>
                          ) : null}
                        </div>
                        {creatingKeyForSubscriptionId === subscription.id
                          ? renderAccessKeyForm(subscription)
                          : null}
                        <div className="vpn-key-list">
                          {requestedContext?.subscription.id === subscription.id && requestedContext.access_key === null
                            ? <p className="muted">У логического профиля #{requestedContext.device_id} нет сохранённых подключений</p> : null}
                          {subscriptionKeys.map(renderAccessKey)}
                          {subscriptionKeys.length === 0 ? (
                            <p className="empty">У этой подписки пока нет VPN‑профилей.</p>
                          ) : null}
                        </div>
                      </div>
                    </article>
                  );
                })}
                {selectedSubscriptions.length === 0 && !creatingSubscription ? (
                  <p className="empty">У клиента пока нет подписок.</p>
                ) : null}
              </div>
            </section>
          </div>
        ) : (
          <p className="empty">Выберите клиента или создайте нового.</p>
        )}
      </section>
    </div>
  );
}
