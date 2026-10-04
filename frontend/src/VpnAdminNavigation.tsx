import type { VpnAdminSection } from "./vpnAdminView";

const VPN_ADMIN_SECTIONS: ReadonlyArray<{
  key: VpnAdminSection;
  href: string;
  label: string;
}> = [
  { key: "overview", href: "#vpn/overview", label: "Обзор" },
  { key: "customers", href: "#vpn/customers", label: "Клиенты" },
  { key: "nodes", href: "#vpn/nodes", label: "Ноды" },
  { key: "plans", href: "#vpn/plans", label: "Тарифы" },
  { key: "events", href: "#vpn/events", label: "События" },
];

export function VpnAdminNavigation({ activeSection }: { activeSection: VpnAdminSection }) {
  return (
    <nav className="tab-strip vpn-admin-navigation" aria-label="Разделы управления VPN">
      {VPN_ADMIN_SECTIONS.map((item) => (
        <a
          key={item.key}
          className={item.key === activeSection ? "button-link ghost active-chip" : "button-link ghost"}
          href={item.href}
          aria-current={item.key === activeSection ? "page" : undefined}
        >
          {item.label}
        </a>
      ))}
    </nav>
  );
}
