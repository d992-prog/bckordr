import React from "react";
import ReactDOM from "react-dom/client";

import { portalApi } from "./api";
import { captureTelegramLaunch, createPortalBootstrap } from "./bootstrap";
import Portal from "./Portal";
import "./portal.css";

const launch = captureTelegramLaunch();
const bootstrap = createPortalBootstrap(portalApi);

window.Telegram?.WebApp?.ready?.();
window.Telegram?.WebApp?.expand?.();

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <div className="veltrix-portal">
      <Portal launch={launch} bootstrap={bootstrap} />
    </div>
  </React.StrictMode>,
);
