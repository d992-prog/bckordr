const PATHS = {
  home: "M3 10.5 12 3l9 7.5M5 9v12h5v-7h4v7h5V9",
  devices: "M4 4h16v12H4zM2 20h20M9 16v4m6-4v4",
  help: "M9.2 8a3 3 0 1 1 4.9 2.3c-1.4.8-2.1 1.3-2.1 3M12 17h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0",
  account: "M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0M3 21a9 9 0 0 1 18 0z",
  link: "m10 14 4-4M8 16l-1 1a4 4 0 0 1-6-6l5-5a4 4 0 0 1 6 0m0 12a4 4 0 0 0 6 0l5-5a4 4 0 0 0-6-6l-1 1",
  qr: "M3 3h6v6H3zm12 0h6v6h-6zM3 15h6v6H3zm12 0h2v2h-2zm4 0h2v2h-2zm-4 4h2v2h-2zm4 0h2v2h-2z",
  arrow: "M3 12h18m-6-6 6 6-6 6",
  chevron: "m9 4 8 8-8 8",
  document: "M5 2h10l5 5v15H5zM15 2v6h5M9 12h7m-7 4h7",
  plus: "M12 4v16M4 12h16",
  iphone: "M7 2h10v20H7zM10 5h4M11 19h2",
  android: "M5 9h14v10H5zM5 8a7 7 0 0 1 14 0M8 2 6 0m10 2 2-2M2 10v8m20-8v8M8 19v4m8-4v4M9 5h.01M15 5h.01",
  windows: "M3 4h8v8H3zm10-1h8v9h-8zM3 14h8v7H3zm10 0h8v8h-8z",
} as const;

export type PortalIconName = keyof typeof PATHS;

export function PortalIcon({ name, className = "" }: { name: PortalIconName; className?: string }) {
  return <svg className={`portal-icon ${className}`} width="24" height="24" viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path d={PATHS[name]} /></svg>;
}
