import type { PortalSection } from "./navigation";

const ITEMS: ReadonlyArray<[PortalSection, string, string]> = [
  ["home", "Главная", "M4 11.5 12 4l8 7.5V20h-5v-5H9v5H4z"],
  ["profiles", "Профили", "M4 4h6v6H4zm10 0h6v6h-6zM4 14h6v6H4zm10 0h6v6h-6z"],
  ["account", "Аккаунт", "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-7 8a7 7 0 0 1 14 0z"],
];

export function PortalNavigation({ active }: { active: PortalSection }) {
  return (
    <nav className="portal-nav section-nav vx-glass" aria-label="Разделы кабинета">
      {ITEMS.map(([id, label, path]) => (
        <a key={id} href={`#${id}`} aria-current={active === id ? "page" : undefined}>
          <svg width="20" height="20" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
            <path d={path} />
          </svg>
          <span>{label}</span>
        </a>
      ))}
    </nav>
  );
}
