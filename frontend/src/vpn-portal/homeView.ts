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
        detail: `Доступ действует до ${portalDate(subscription.expires_at)}`,
        profileId: profile.id,
      };
    }
  }

  if (trial?.state === "preparing" || subscriptions.some(({ state }) => (
    USABLE_SUBSCRIPTION_STATES.has(state)
  ))) {
    return {
      kind: "preparing",
      title: "Профиль готовится",
      detail: "Это может занять несколько минут",
      profileId: null,
    };
  }

  if (subscriptions.some(({ state }) => state === "disabled" || state === "suspended")) {
    return {
      kind: "paused",
      title: "Доступ приостановлен",
      detail: "Напишите в поддержку, чтобы уточнить причину",
      profileId: null,
    };
  }

  if (subscriptions.some(({ state }) => state === "expired")) {
    return {
      kind: "expired",
      title: "Срок доступа закончился",
      detail: "Выберите доступный тариф или напишите в поддержку",
      profileId: null,
    };
  }

  return {
    kind: "empty",
    title: "VPN‑профиля пока нет",
    detail: "Получите пробный доступ или напишите в поддержку",
    profileId: null,
  };
}
