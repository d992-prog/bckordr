import { useEffect, useState } from "react";

import { PortalError, portalApi } from "./api";
import type { SessionGeneration } from "./bootstrap";
import { ConnectDevice } from "./ConnectDevice";
import { DataError } from "./DataError";
import { buildPortalHomeView } from "./homeView";
import PlanCatalog from "./PlanCatalog";
import { PortalIcon } from "./PortalIcon";
import { PortalUsage, type PortalUsageProps } from "./PortalUsage";
import TrialCard from "./TrialCard";
import type { PortalDevice, PortalDeviceLocations, PortalPlan, PortalSubscription, PortalTrial } from "./types";
import { portalDate, portalRemainingTime } from "./view";

interface PortalHomeProps {
  busy: boolean;
  error: string;
  subscriptions: PortalSubscription[];
  profiles: PortalDevice[];
  selectedDeviceId: number | null;
  profileOperations: Record<number, { connectionVersion: number; renamePending: boolean; locationPending?: boolean }>;
  pendingDeviceIds: number[];
  usage: PortalUsageProps;
  onAddDevice: () => void;
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  showPlans: boolean;
  trial: PortalTrial | null;
  trialBusy: boolean;
  trialError: string;
  sessionGeneration: SessionGeneration;
  onSelectDevice: (id: number) => void;
  onRetry: () => void;
  onRetryPlans: () => void;
  onActivateTrial: () => void;
  onRefreshTrial: () => void;
  onUnauthorized: () => void;
  onLocationStateChange: (id: number, pending: boolean) => void;
}

export function PortalHome(props: PortalHomeProps) {
  const view = buildPortalHomeView(props.subscriptions, props.profiles, props.trial);
  const readyDevices = props.profiles.filter((device) => device.state === "active" && device.can_connect
    && props.subscriptions.some((item) => item.id === device.subscription_id && ["active", "trial"].includes(item.state)));
  const selected = readyDevices.find(({ id }) => id === props.selectedDeviceId)
    ?? readyDevices.find(({ id }) => id === view.profileId) ?? readyDevices[0] ?? null;
  const subscription = props.subscriptions.find(({ id }) => id === selected?.subscription_id)
    ?? props.subscriptions.find(({ state }) => state === "active" || state === "trial") ?? props.subscriptions[0] ?? null;
  const [locations, setLocations] = useState<Record<number, PortalDeviceLocations | null>>({});
  const operation = selected ? props.profileOperations[selected.id] : null;
  const expiry = subscription?.expires_at;
  const title = props.busy && !subscription ? "Загружаем данные" : props.error && !subscription ? "Данные недоступны" : subscription && ["active", "trial"].includes(subscription.state) ? "Доступ активен" : view.title;

  useEffect(() => {
    let active = true;
    const generation = props.sessionGeneration.current();
    setLocations({});
    for (const device of props.profiles) {
      void portalApi.deviceLocations(device.id).then((result) => {
        if (active && props.sessionGeneration.isCurrent(generation)) {
          setLocations((current) => ({ ...current, [device.id]: result }));
          props.onLocationStateChange(device.id, ["preparing", "verification_required"].includes(result.selection_state));
        }
      }).catch((error) => {
        if (active && props.sessionGeneration.isCurrent(generation) && error instanceof PortalError && error.status === 401) props.onUnauthorized();
      });
    }
    return () => { active = false; };
    // Refresh locations with owned rows, and discard every completion on unmount/session change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [props.profiles, props.sessionGeneration]);

  return (
    <section id="home" className="cabinet-home">
      <section id="subscription" className={`cabinet-overview vx-matte cabinet-overview--${view.kind}`} aria-labelledby="access-title">
        <div className="cabinet-access-heading" role={props.busy && !subscription ? "status" : undefined}><span className="cabinet-access-dot" aria-hidden="true" /><div><h1 id="access-title">{title}</h1><p>{subscription ? expiry === null ? "Без срока окончания" : <>{`До ${portalDate(expiry ?? null)}`}<span className="cabinet-remaining"> · {portalRemainingTime(expiry ?? null).toLowerCase()}</span></> : view.detail}</p></div></div>
        <div className="cabinet-overview-rule" />
        <div className="cabinet-access-stats"><PortalUsage {...props.usage} /><div className="cabinet-slots"><span>Устройства</span><strong>{subscription ? `${subscription.profiles_used} из ${subscription.profile_limit}` : "—"}</strong></div></div>
        {props.error && <DataError message={props.error} onRetry={props.onRetry} />}
        {props.trial && ["available", "capacity_paused", "preparing"].includes(props.trial.state) && <TrialCard trial={props.trial} busy={props.trialBusy} error={props.trialError} onActivate={props.onActivateTrial} onRefresh={props.onRefreshTrial} />}
        {props.plans.length > 0 && !props.showPlans && ["empty", "expired"].includes(view.kind) && <a className="button button--ghost" href="#plans">Посмотреть тарифы</a>}
      </section>
      <ConnectDevice devices={readyDevices} selectedDeviceId={selected?.id ?? null} connectionVersion={operation?.connectionVersion ?? 0} disabled={Boolean(operation?.renamePending || operation?.locationPending || (selected && (!locations[selected.id] || props.pendingDeviceIds.includes(selected.id))))} sessionGeneration={props.sessionGeneration} onSelectDevice={props.onSelectDevice} onUnauthorized={props.onUnauthorized} />
      <section className="cabinet-devices-preview vx-matte" aria-labelledby="device-preview-title">
        <header className="cabinet-preview-heading"><h2 id="device-preview-title">Устройства</h2><a href="#devices" onClick={props.onAddDevice}><span className="cabinet-add-ink"><PortalIcon name="plus" />Добавить</span></a></header>
        {props.profiles.slice(0, 2).map((device) => <a className="cabinet-device-row" href="#devices" key={device.id}><span className="cabinet-device-icon"><PortalIcon name="devices" /></span><span><strong>{device.display_name}</strong><span>{locations[device.id]?.current?.country_code && <span className="cabinet-country-code">{locations[device.id]?.current?.country_code}</span>}{locations[device.id]?.current?.name ?? (device.can_connect ? "Страна уточняется" : "Подключение недоступно")}</span></span><PortalIcon name="chevron" /></a>)}
        {!props.busy && props.profiles.length === 0 && <a className="cabinet-device-row" href="#devices">Устройств пока нет<PortalIcon name="chevron" /></a>}
        <a className="cabinet-help-row" href="#help"><PortalIcon name="document" /><span>Инструкция и помощь</span><PortalIcon name="chevron" /></a>
      </section>
      {props.showPlans && <PlanCatalog plans={props.plans} busy={props.plansBusy} error={props.plansError} onRetry={props.onRetryPlans} />}
    </section>
  );
}
