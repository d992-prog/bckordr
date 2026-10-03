import type { PortalProfile, PortalSubscription, PortalTrial } from "./types";
// @ts-expect-error Node's strip-types runner requires the source extension.
import { portalDate } from "./view.ts";

export interface PortalHomeView {
  kind: "ready" | "preparing" | "paused" | "expired" | "empty";
  title: string;
  detail: string;
  profileId: number | null;
}

const USABLE_SUBSCRIPTION_STATES = new Set(["active", "trial"]);
const TERMINAL_SUBSCRIPTION_STATES = new Set(["disabled", "suspended", "expired"]);

function readyDetail(expiresAt: string | null): string {
  if (expiresAt === null) {
    return "Доступ без ограничения по сроку";
  }
  if (Number.isNaN(new Date(expiresAt).getTime())) {
    return "Срок доступа уточняется";
  }
  return `Доступ действует до ${portalDate(expiresAt)}`;
}

function subscriptionStatePriority(state: string): number {
  if (USABLE_SUBSCRIPTION_STATES.has(state)) {
    return 0;
  }
  if (state === "scheduled") {
    return 1;
  }
  return TERMINAL_SUBSCRIPTION_STATES.has(state) ? 2 : 3;
}

function subscriptionRecency(subscription: PortalSubscription): number {
  for (const value of [subscription.expires_at, subscription.starts_at]) {
    if (value !== null) {
      const timestamp = new Date(value).getTime();
      if (!Number.isNaN(timestamp)) {
        return timestamp;
      }
    }
  }
  return 0;
}

function selectRelevantSubscription(
  subscriptions: PortalSubscription[],
  profiles: PortalProfile[],
): PortalSubscription | null {
  const subscriptionsWithProfiles = new Set(profiles.map(({ subscription_id }) => subscription_id));
  return [...subscriptions].sort((left, right) => (
    Number(subscriptionsWithProfiles.has(right.id)) - Number(subscriptionsWithProfiles.has(left.id))
    || subscriptionStatePriority(left.state) - subscriptionStatePriority(right.state)
    || subscriptionRecency(right) - subscriptionRecency(left)
    || right.id - left.id
  ))[0] ?? null;
}

export function buildPortalHomeView(
  subscriptions: PortalSubscription[],
  profiles: PortalProfile[],
  trial: PortalTrial | null,
): PortalHomeView {
  for (const profile of profiles) {
    if (profile.state !== "active" || !profile.can_connect) {
      continue;
    }
    const subscription = subscriptions.find(({ id }) => id === profile.subscription_id);
    if (subscription && USABLE_SUBSCRIPTION_STATES.has(subscription.state)) {
      return {
        kind: "ready",
        title: "VPN‑профиль готов",
        detail: readyDetail(subscription.expires_at),
        profileId: profile.id,
      };
    }
  }

  if (profiles.some((profile) => (
    profile.state === "active"
    && profile.can_connect
    && !subscriptions.some(({ id }) => id === profile.subscription_id)
  ))) {
    return {
      kind: "preparing",
      title: "Обновляем данные профиля",
      detail: "Попробуйте открыть кабинет через несколько секунд",
      profileId: null,
    };
  }

  if (trial?.state === "active") {
    return {
      kind: "preparing",
      title: "Обновляем данные профиля",
      detail: "Попробуйте открыть кабинет через несколько секунд",
      profileId: null,
    };
  }

  if (trial?.state === "preparing") {
    return {
      kind: "preparing",
      title: "Профиль готовится",
      detail: "Это может занять несколько минут",
      profileId: null,
    };
  }

  const subscription = selectRelevantSubscription(subscriptions, profiles);
  if (subscription && USABLE_SUBSCRIPTION_STATES.has(subscription.state)) {
    return {
      kind: "preparing",
      title: "Профиль готовится",
      detail: "Это может занять несколько минут",
      profileId: null,
    };
  }

  if (subscription?.state === "disabled" || subscription?.state === "suspended") {
    return {
      kind: "paused",
      title: "Доступ приостановлен",
      detail: "Напишите в поддержку, чтобы уточнить причину",
      profileId: null,
    };
  }

  if (subscription?.state === "expired") {
    return {
      kind: "expired",
      title: "Срок доступа закончился",
      detail: "Выберите доступный тариф или напишите в поддержку",
      profileId: null,
    };
  }

  if (trial?.state === "available") {
    return {
      kind: "empty",
      title: "VPN‑профиля пока нет",
      detail: "Получите пробный доступ или напишите в поддержку",
      profileId: null,
    };
  }

  if (trial?.state === "capacity_paused") {
    return {
      kind: "paused",
      title: "Выдача доступа временно приостановлена",
      detail: "Попробуйте позже или напишите в поддержку",
      profileId: null,
    };
  }

  if (trial?.state === "used") {
    return {
      kind: "empty",
      title: "Пробный доступ уже использован",
      detail: "Выберите доступный тариф или напишите в поддержку",
      profileId: null,
    };
  }

  if (trial?.state === "disabled") {
    return {
      kind: "empty",
      title: "Пробный доступ недоступен",
      detail: "Напишите в поддержку, чтобы уточнить доступные варианты",
      profileId: null,
    };
  }

  return {
    kind: "empty",
    title: "VPN‑профиля пока нет",
    detail: "Напишите в поддержку, чтобы получить доступ",
    profileId: null,
  };
}
