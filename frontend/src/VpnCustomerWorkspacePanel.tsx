import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  api,
  type VpnAccessKey,
  type VpnCustomer,
  type VpnPlan,
  type VpnSubscription,
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
  type VpnCustomerFilter,
  type VpnCustomerOperationalStatus,
} from "./vpnCustomerWorkspace";

type NoticeType = "success" | "error";

type Props = {
  customers: VpnCustomer[];
  subscriptions: VpnSubscription[];
  accessKeys: VpnAccessKey[];
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

export function VpnCustomerWorkspace({
  customers,
  subscriptions,
  accessKeys,
  plans,
  workers,
  reload,
  notify,
}: Props) {
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState<VpnCustomerFilter>("all");
  const [selectedCustomerId, setSelectedCustomerId] = useState<number | null>(null);
  const [customerForm, setCustomerForm] = useState<CustomerForm>(EMPTY_CUSTOMER_FORM);
  const [editingCustomer, setEditingCustomer] = useState(false);
  const [creatingCustomer, setCreatingCustomer] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [subscriptionForm, setSubscriptionForm] = useState<SubscriptionForm>(
    EMPTY_SUBSCRIPTION_FORM,
  );
  const [editingSubscriptionId, setEditingSubscriptionId] = useState<number | null>(null);
  const [creatingSubscription, setCreatingSubscription] = useState(false);
  const [accessKeyForm, setAccessKeyForm] = useState<AccessKeyForm>(EMPTY_ACCESS_KEY_FORM);
  const [creatingKeyForSubscriptionId, setCreatingKeyForSubscriptionId] = useState<
    number | null
  >(null);
  const [expandedKeyIds, setExpandedKeyIds] = useState<Set<number>>(() => new Set());
  const [editingAccessKeyId, setEditingAccessKeyId] = useState<number | null>(null);
  const [accessKeyName, setAccessKeyName] = useState("");
  const [pendingAccessKeyRenameIds, setPendingAccessKeyRenameIds] = useState<Set<number>>(
    () => new Set(),
  );
  const [accessKeyDisplayOverrides, setAccessKeyDisplayOverrides] = useState<
    Map<number, AccessKeyDisplayOverride>
  >(() => new Map());

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
      accessKeys
        .filter((accessKey) => selectedSubscriptionIds.has(accessKey.subscription_id))
        .map((accessKey) => {
          const display = accessKeyDisplayOverrides.get(accessKey.id);
          return display ? applyAccessKeyDisplay(accessKey, display) : accessKey;
        }),
    [accessKeys, accessKeyDisplayOverrides, selectedSubscriptionIds],
  );
  const planMap = useMemo(() => new Map(plans.map((plan) => [plan.id, plan])), [plans]);
  const workerMap = useMemo(
    () => new Map(workers.map((worker) => [worker.id, worker])),
    [workers],
  );

  useEffect(() => {
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
  }, [accessKeys]);

  function beginCustomerCreate() {
    setCustomerForm(EMPTY_CUSTOMER_FORM);
    setEditingCustomer(false);
    setCreatingCustomer(true);
  }

  function beginCustomerEdit(customer: VpnCustomer) {
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
        `Перенести ${customerName(customer)} в архив? Подписки будут отключены, а ключи отправлены на отзыв.`,
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
        `Клиент перенесён в архив. Отозвано ключей: ${result.revoked_keys}${pending}`,
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
        "Клиент восстановлен. Подписки и ключи нужно активировать отдельно.",
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
    setSubscriptionForm(EMPTY_SUBSCRIPTION_FORM);
    setEditingSubscriptionId(null);
    setCreatingSubscription(true);
  }

  function beginSubscriptionEdit(subscription: VpnSubscription) {
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
    const result = await saveSubscriptionAndRequestSync(
      () => api.updateVpnSubscription(subscriptionId, payload),
      () => api.runVpnLifecycleMaintenance(),
      reload,
    );
    const feedback = !result.refreshed
      ? "Обновите страницу, чтобы увидеть актуальный статус ключей."
      : !result.syncRequested
        ? "Синхронизация будет повторена автоматически. Проверьте статус ключей."
        : "Результат применения на ноде — в статусе ключей ниже.";
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
    setAccessKeyForm(EMPTY_ACCESS_KEY_FORM);
    setCreatingKeyForSubscriptionId(subscription.id);
  }

  async function saveAccessKey(event: FormEvent) {
    event.preventDefault();
    if (!creatingKeyForSubscriptionId) {
      return;
    }
    setBusyAction(`key-create-${creatingKeyForSubscriptionId}`);
    try {
      const accessKey = await api.createVpnAccessKey({
        subscription_id: creatingKeyForSubscriptionId,
        worker_id: optionalNumber(accessKeyForm.workerId),
        protocol: accessKeyForm.protocol,
        display_name: accessKeyForm.displayName.trim(),
      });
      await reload();
      setCreatingKeyForSubscriptionId(null);
      setAccessKeyForm(EMPTY_ACCESS_KEY_FORM);
      const accessReady = accessKey.status === "active" && Boolean(accessKey.config_uri);
      notify(
        accessReady ? "success" : "error",
        accessReady
          ? "VPN доступ выдан"
          : accessKey.last_error || "VPN ключ сохранён и ожидает выдачи",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось создать VPN ключ",
      );
    } finally {
      setBusyAction(null);
    }
  }

  async function retryAccessKeyProvision(accessKey: VpnAccessKey) {
    setBusyAction(`key-provision-${accessKey.id}`);
    try {
      const provisioned = await api.provisionVpnAccessKey(accessKey.id);
      await reload();
      notify(
        provisioned.status === "active" ? "success" : "error",
        provisioned.status === "active"
          ? "VPN доступ выдан"
          : provisioned.last_error || "Ключ пока не выдан на ноду",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось повторить выдачу VPN ключа",
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
        `Отключить VPN ключ ${keyName} на 3x-UI? Запись останется в панели для истории.`,
      )
    ) {
      return;
    }
    setBusyAction(`key-revoke-${accessKey.id}`);
    try {
      const revoked = await api.revokeVpnAccessKey(accessKey.id);
      await reload();
      notify(
        revoked.status === "pending_revoke" ? "error" : "success",
        revoked.status === "pending_revoke"
          ? revoked.last_error || "Ключ ожидает повторного отзыва"
          : "VPN ключ отключён",
      );
    } catch (error) {
      notify(
        "error",
        error instanceof Error ? error.message : "Не удалось отключить VPN ключ",
      );
    } finally {
      setBusyAction(null);
    }
  }

  function beginAccessKeyRename(accessKey: VpnAccessKey) {
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
      notify("success", "Полная VPN-ссылка скопирована");
    } catch {
      setExpandedKeyIds((current) => new Set(current).add(accessKey.id));
      window.requestAnimationFrame(() => {
        const field = document.getElementById(
          `vpn-key-uri-${accessKey.id}`,
        ) as HTMLTextAreaElement | null;
        field?.focus();
        field?.select();
      });
      notify(
        "error",
        "Буфер обмена недоступен. Полная ссылка выделена — скопируйте её вручную.",
      );
    }
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
    return (
      <form className="form vpn-inline-form" onSubmit={saveAccessKey}>
        <h4>Новый ключ для подписки #{subscription.id}</h4>
        <div className="form two-columns">
          <label>
            <span>VPN-нода</span>
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
            disabled={busyAction === `key-create-${subscription.id}`}
          >
            Выдать ключ
          </button>
          <button
            type="button"
            className="ghost"
            onClick={() => setCreatingKeyForSubscriptionId(null)}
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
      <article key={accessKey.id} className="vpn-key-card">
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
            <code className="vpn-key-preview">{accessKey.config_uri}</code>
            <div className="actions">
              <button type="button" className="ghost" onClick={() => void copyAccessKey(accessKey)}>
                Копировать ссылку
              </button>
              <button type="button" className="ghost" onClick={() => toggleAccessKeyUri(accessKey.id)}>
                {expanded ? "Скрыть" : "Показать полностью"}
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
              ? "Ключ отозван, ссылка больше недоступна."
              : "Ссылка появится после успешной выдачи ключа на VPN-ноду."}
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
    <div className="vpn-customer-workspace">
      <aside className="vpn-customer-sidebar">
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
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Имя, @username или Telegram ID"
          />
        </label>
        <div className="vpn-customer-filters" aria-label="Фильтр клиентов">
          {CUSTOMER_FILTERS.map((item) => (
            <button
              key={item.value}
              type="button"
              className={filter === item.value ? "secondary active-chip" : "ghost"}
              onClick={() => setFilter(item.value)}
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

      <section className="vpn-customer-detail">
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

            <section className="vpn-workspace-section">
              <div className="vpn-workspace-section-head">
                <div>
                  <h3>Подписки и ключи</h3>
                  <p className="muted">
                    Изменяйте срок и лимиты, затем управляйте ключами конкретной подписки.
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
                  const isUsable = ["active", "trial"].includes(subscription.status);
                  return (
                    <article key={subscription.id} className="vpn-subscription-card">
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
                          <strong>{subscription.max_devices}</strong>
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
                          <strong>Ключи доступа</strong>
                          {selectedCustomer.status !== "archived" &&
                          isUsable &&
                          creatingKeyForSubscriptionId !== subscription.id ? (
                            <button
                              type="button"
                              className="ghost"
                              onClick={() => beginAccessKeyCreate(subscription)}
                            >
                              Выдать новый ключ
                            </button>
                          ) : null}
                        </div>
                        {creatingKeyForSubscriptionId === subscription.id
                          ? renderAccessKeyForm(subscription)
                          : null}
                        <div className="vpn-key-list">
                          {subscriptionKeys.map(renderAccessKey)}
                          {subscriptionKeys.length === 0 ? (
                            <p className="empty">У этой подписки пока нет ключей.</p>
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
