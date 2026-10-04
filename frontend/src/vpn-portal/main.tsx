import React from "react";
import ReactDOM from "react-dom/client";

import { portalApi } from "./api";
import { captureTelegramLaunch, createPortalBootstrap } from "./bootstrap";
import { applyPortalCompatibilityMode } from "./compatibility";
import Portal from "./Portal";
import { PortalErrorBoundary } from "./PortalErrorBoundary";
import "./portal.css";

applyPortalCompatibilityMode(document.documentElement, navigator.userAgent);

const launch = captureTelegramLaunch();
const bootstrap = createPortalBootstrap(portalApi);

function activateTelegramWebApp(): void {
  window.Telegram?.WebApp?.ready?.();
  window.Telegram?.WebApp?.expand?.();
}

activateTelegramWebApp();
document.querySelector("[data-telegram-sdk]")?.addEventListener(
  "load",
  activateTelegramWebApp,
  { once: true },
);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <div className="veltrix-portal">
      <PortalErrorBoundary>
        <Portal launch={launch} bootstrap={bootstrap} />
      </PortalErrorBoundary>
    </div>
  </React.StrictMode>,
);
