import { useState } from "react";

import { api, type VpnReleaseReadiness } from "./api";
import {
  VPN_RELEASE_STATUS_LABELS,
  canConfirmVpnExternalProof,
  formatVpnReleaseCheckLabel,
  formatVpnReleaseCheckedAt,
  formatVpnReleaseEntityLabel,
  groupVpnReleaseChecks,
  summarizeVpnRelease,
} from "./vpnReleaseReadiness";

const RELEASE_COMMIT_PHRASE = "ГОТОВО К РЕЛИЗУ";

type Props = {
  report: VpnReleaseReadiness | null;
  loading: boolean;
  error: string | null;
  onRefresh: () => Promise<void>;
  onNavigate: (target: "nodes" | "capacity" | "maintenance") => void;
};

function statusClass(state: "pass" | "warn" | "fail") {
  return state === "pass" ? "available" : state === "warn" ? "info" : "error";
}

export function VpnReleaseReadinessPanel({ report, loading, error, onRefresh, onNavigate }: Props) {
  const [actionInFlight, setActionInFlight] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);
  const [commitPhrase, setCommitPhrase] = useState("");

  async function confirmExternal(endpointId: number) {
    const accepted = window.confirm(
      "Подтверждайте только после реального подключения к VPN извне — не из control-сети. Внешний тест действительно выполнен?",
    );
    if (!accepted) {
      return;
    }
    setActionInFlight(`external-${endpointId}`);
    setActionError(null);
    setSuccess(null);
    try {
      await api.confirmVpnEndpointExternalVerification(endpointId);
      await onRefresh();
      setSuccess(`Внешний тест для ноды #${endpointId} подтверждён.`);
    } catch (caught) {
      setActionError((caught instanceof Error ? caught.message : "Не удалось подтвердить внешний тест.").slice(0, 180));
    } finally {
      setActionInFlight(null);
    }
  }

  async function commitRelease() {
    if (commitPhrase !== RELEASE_COMMIT_PHRASE) {
      return;
    }
    const accepted = window.confirm(
      "Установить маркер релизной готовности? Он НЕ включает оплату и пробный доступ.",
    );
    if (!accepted) {
      return;
    }
    setActionInFlight("commit");
    setActionError(null);
    setSuccess(null);
    try {
      await api.commitVpnReleaseReadiness();
      setCommitPhrase("");
      await onRefresh();
      setSuccess("Маркер релизной готовности сохранён. Оплата и пробный доступ не включены.");
    } catch (caught) {
      setActionError((caught instanceof Error ? caught.message : "Не удалось сохранить маркер.").slice(0, 180));
    } finally {
      setActionInFlight(null);
    }
  }

  if (!report) {
    return (
      <section className="card full-span vpn-release-readiness" aria-busy={loading}>
        <div className="card-head">
          <div>
            <h2>Готовность VPN к релизу</h2>
            <p className="muted">{loading ? "Проверяем контур…" : "Отчёт пока не загружен."}</p>
          </div>
          <button type="button" className="ghost" onClick={() => void onRefresh()} disabled={loading}>
            {loading ? "Проверяем…" : "Повторить"}
          </button>
        </div>
        {error ? <p className="inline-alert error" role="alert">{error.slice(0, 180)}</p> : null}
      </section>
    );
  }

  const summary = summarizeVpnRelease(report);
  const tone = summary.fail > 0 ? "fail" : summary.warn > 0 || !report.ready ? "warn" : "pass";
  const groups = groupVpnReleaseChecks(report.checks);

  return (
    <section className={`card full-span vpn-release-readiness is-${tone}`} aria-busy={loading}>
      <div className="vpn-release-summary">
        <div>
          <p className="eyebrow">Релизный контур</p>
          <h2>Готовность VPN к релизу</h2>
          <p className="muted">Проверено: {formatVpnReleaseCheckedAt(report.checked_at)}</p>
        </div>
        <div className="vpn-release-counts" aria-label="Итоги проверки">
          <span className="status available">Пройдено: {summary.pass}</span>
          <span className="status info">Предупреждения: {summary.warn}</span>
          <span className="status error">Ошибки: {summary.fail}</span>
        </div>
        <button type="button" className="ghost" onClick={() => void onRefresh()} disabled={loading || actionInFlight !== null}>
          {loading ? "Проверяем…" : "Обновить отчёт"}
        </button>
      </div>

      <nav className="vpn-release-links" aria-label="Разделы управления VPN">
        <button type="button" className="ghost" onClick={() => onNavigate("nodes")}>Ноды</button>
        <button type="button" className="ghost" onClick={() => onNavigate("capacity")}>Ёмкость</button>
        <button type="button" className="ghost" onClick={() => onNavigate("maintenance")}>Обслуживание</button>
      </nav>

      {error ? <p className="inline-alert error" role="alert">{error.slice(0, 180)}</p> : null}
      {actionError ? <p className="inline-alert error" role="alert">{actionError}</p> : null}
      {success ? <p className="vpn-release-success" role="status">{success}</p> : null}

      <div className="vpn-release-groups">
        {groups.map((group) => (
          <section className="vpn-release-group" key={group.key}>
            <h3>{group.title}</h3>
            <div className="vpn-release-checks">
              {group.checks.map((check, index) => {
                const checkLabel = formatVpnReleaseCheckLabel(check.code);
                const entityLabel = formatVpnReleaseEntityLabel(check);
                const confirmEndpointId = canConfirmVpnExternalProof(check) ? check.entity_id : null;
                return (
                  <article className="vpn-release-check" key={`${group.key}-${check.entity_id ?? "global"}-${index}`}>
                    <div className="vpn-release-check-head">
                      <div>
                        <strong>{checkLabel}</strong>
                        <div className="row-hint">{check.message}</div>
                        {entityLabel || check.observed_at ? (
                          <div className="row-hint">
                            {entityLabel ?? ""}
                            {check.observed_at ? `${entityLabel ? " · " : ""}${formatVpnReleaseCheckedAt(check.observed_at)}` : ""}
                          </div>
                        ) : null}
                      </div>
                      <span className={`status ${statusClass(check.state)}`}>
                        {VPN_RELEASE_STATUS_LABELS[check.state]}
                      </span>
                    </div>
                    {confirmEndpointId !== null ? (
                      <button
                        type="button"
                        className="ghost"
                        onClick={() => void confirmExternal(confirmEndpointId)}
                        disabled={actionInFlight !== null || loading}
                      >
                        {actionInFlight === `external-${confirmEndpointId}` ? "Подтверждаем…" : "Подтвердить внешний тест"}
                      </button>
                    ) : null}
                  </article>
                );
              })}
            </div>
          </section>
        ))}
      </div>

      {summary.canCommit ? (
        <section className="vpn-release-commit">
          <div>
            <h3>Маркер релизной готовности</h3>
            <p className="muted">
              Маркер фиксирует результат проверок и НЕ включает оплату и пробный доступ.
            </p>
          </div>
          <label>
            <span>Введите точную фразу «{RELEASE_COMMIT_PHRASE}»</span>
            <input
              value={commitPhrase}
              onChange={(event) => setCommitPhrase(event.target.value)}
              autoComplete="off"
              disabled={actionInFlight !== null || loading}
            />
          </label>
          <button
            type="button"
            onClick={() => void commitRelease()}
            disabled={commitPhrase !== RELEASE_COMMIT_PHRASE || actionInFlight !== null || loading}
          >
            {actionInFlight === "commit" ? "Сохраняем…" : "Зафиксировать готовность"}
          </button>
        </section>
      ) : null}
    </section>
  );
}
