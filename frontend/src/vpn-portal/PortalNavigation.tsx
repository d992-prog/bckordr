import type { PortalSection } from "./navigation";
import { PortalIcon, type PortalIconName } from "./PortalIcon";

const ITEMS: ReadonlyArray<[PortalSection & PortalIconName, string]> = [
  ["home", "Главная"],
  ["devices", "Устройства"],
  ["help", "Помощь"],
  ["account", "Аккаунт"],
];

export function PortalNavigation({ active }: { active: PortalSection }) {
  return (
    <nav className="portal-nav cabinet-navigation vx-glass" aria-label="Разделы кабинета">
      {ITEMS.map(([id, label]) => (
        <a key={id} href={`#${id}`} aria-current={active === id ? "page" : undefined}>
          <PortalIcon name={id} />
          <span>{label}</span>
        </a>
      ))}
    </nav>
  );
}
