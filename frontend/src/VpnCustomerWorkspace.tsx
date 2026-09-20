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
  classifyVpnCustomer,
  filterVpnCustomers,
  selectPrimarySubscription,
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

const EMPTY_CUSTOMER_FORM: CustomerForm = {
  telegramUserId: "",
  telegramUsername: "",
  firstName: "",
  lastName: "",
  status: "active",
  notes: "",
};

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

export function VpnCustomerWorkspace({
  customers,
  subscriptions,
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

  const filteredCustomers = useMemo(
    () => filterVpnCustomers(customers, subscriptions, filter, query),
    [customers, subscriptions, filter, query],
  );
  const selectedCustomer =
    customers.find((customer) => customer.id === selectedCustomerId) ?? null;

  useEffect(() => {
    if (
      selectedCustomerId &&
      filteredCustomers.some((customer) => customer.id === selectedCustomerId)
    ) {
      return;
    }
    setSelectedCustomerId(filteredCustomers[0]?.id ?? null);
    setEditingCustomer(false);
  }, [filteredCustomers, selectedCustomerId]);

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

  function renderCustomerFields() {
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
              onChange={(event) =>
                setCustomerForm((current) => ({
                  ...current,
                  status: event.target.value,
                }))
              }
            >
              <option value="active">Активен</option>
              <option value="blocked">Заблокирован</option>
              <option value="archived">Архив</option>
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
          </div>
        ) : (
          <p className="empty">Выберите клиента или создайте нового.</p>
        )}
      </section>
    </div>
  );
}
