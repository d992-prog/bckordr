import type { ReactNode } from "react";

import type { PortalTrial } from "./types";

interface TrialCardProps {
  trial: PortalTrial | null;
  busy: boolean;
  error: string;
  onActivate: () => void;
  onRefresh: () => void;
}

export default function TrialCard({
  trial,
  busy,
  error,
  onActivate,
  onRefresh,
}: TrialCardProps) {
  if (trial === null || trial.state === "disabled") {
    return null;
  }

  let content: ReactNode;
  switch (trial.state) {
    case "available":
      content = (
        <>
          <h2>Попробуйте Veltrix VPN</h2>
          <p className="trial-card__meta">
            Пробный доступ на {trial.duration_days} дней для {trial.profile_limit} устройства.
          </p>
          <div className="trial-card__actions">
            <button className="button button--primary" disabled={busy} onClick={onActivate}>
              {busy ? "Активируем…" : "Получить 7 дней"}
            </button>
          </div>
        </>
      );
      break;
    case "capacity_paused":
      content = (
        <>
          <h2>Пробный доступ</h2>
          <p className="trial-card__meta">Новые подключения временно приостановлены</p>
          <div className="trial-card__actions">
            <button className="button button--ghost" disabled={busy} onClick={onRefresh}>
              Обновить статус
            </button>
          </div>
        </>
      );
      break;
    case "preparing":
      content = (
        <>
          <h2>Готовим VPN-профиль</h2>
          <p className="trial-card__meta" role="status">
            Обычно это занимает несколько минут. Статус обновится автоматически.
          </p>
          <div className="trial-card__actions">
            <button className="button button--ghost" disabled={busy} onClick={onRefresh}>
              {busy ? "Обновляем…" : "Обновить статус"}
            </button>
          </div>
        </>
      );
      break;
    case "active":
      content = (
        <>
          <h2>Пробный доступ готов</h2>
          <p className="trial-card__meta">Профиль можно подключить прямо сейчас.</p>
          <div className="trial-card__actions">
            <a className="button button--primary" href="#profiles">Открыть профиль</a>
          </div>
        </>
      );
      break;
    case "used":
      content = (
        <>
          <h2>Пробный период уже использован</h2>
          <p className="trial-card__meta">Повторная активация пробного доступа недоступна.</p>
          <div className="trial-card__actions">
            <a className="button button--ghost" href="#plans">Посмотреть тарифы</a>
          </div>
        </>
      );
      break;
  }

  return (
    <article className="card trial-card">
      <p className="eyebrow">7 дней бесплатно</p>
      {content}
      {error && <p className="message message--error" role="alert">{error}</p>}
    </article>
  );
}
