import assert from "node:assert/strict";
import test from "node:test";

import { portalApi, PortalError } from "../src/vpn-portal/api.ts";
import {
  copyConnectionUri,
  loadConnectionUri,
} from "../src/vpn-portal/connection.ts";

test("loadConnectionUri returns the connection URI without exposing response details", async () => {
  const originalConnection = portalApi.connection;
  portalApi.connection = async (profileId) => ({ uri: `vless://profile-${profileId}` });
  try {
    assert.equal(await loadConnectionUri(21), "vless://profile-21");
  } finally {
    portalApi.connection = originalConnection;
  }
});

test("loadConnectionUri rejects an empty connection URI", async () => {
  const originalConnection = portalApi.connection;
  portalApi.connection = async () => ({ uri: "" });
  try {
    await assert.rejects(
      () => loadConnectionUri(21),
      (error) => error instanceof PortalError
        && error.status === 0
        && error.message === "Ссылка подключения недоступна.",
    );
  } finally {
    portalApi.connection = originalConnection;
  }
});

test("copyConnectionUri uses the browser clipboard and reports unavailable support", async () => {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis.navigator, "clipboard");
  const writes = [];
  try {
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { writes.push(value); } },
    });
    await copyConnectionUri("vless://secret");
    assert.deepEqual(writes, ["vless://secret"]);

    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    await assert.rejects(() => copyConnectionUri("vless://secret"), /clipboard unavailable/);
  } finally {
    if (descriptor) {
      Object.defineProperty(globalThis.navigator, "clipboard", descriptor);
    } else {
      delete globalThis.navigator.clipboard;
    }
  }
});
