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

test("copyConnectionUri copies before an iOS user gesture expires", async () => {
  const clipboardDescriptor = Object.getOwnPropertyDescriptor(globalThis.navigator, "clipboard");
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  let gestureActive = true;
  let appendedField;
  const fallbackWrites = [];
  try {
    Object.defineProperty(globalThis, "document", {
      configurable: true,
      value: {
        body: { append: (field) => { appendedField = field; } },
        createElement: () => ({
          value: "",
          readOnly: false,
          selected: false,
          removed: false,
          style: {},
          focus() {},
          select() { this.selected = true; },
          setSelectionRange() { this.selected = true; },
          remove() { this.removed = true; },
        }),
        execCommand: (command) => {
          if (command !== "copy" || !gestureActive || !appendedField?.selected) return false;
          fallbackWrites.push(appendedField.value);
          return true;
        },
      },
    });
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async () => { throw new Error("denied by webview"); } },
    });

    const copying = copyConnectionUri("vless://ios-secret");
    gestureActive = false;
    await copying;

    assert.deepEqual(fallbackWrites, ["vless://ios-secret"]);
    assert.equal(appendedField.removed, true);
  } finally {
    if (clipboardDescriptor) {
      Object.defineProperty(globalThis.navigator, "clipboard", clipboardDescriptor);
    } else {
      delete globalThis.navigator.clipboard;
    }
    if (documentDescriptor) {
      Object.defineProperty(globalThis, "document", documentDescriptor);
    } else {
      delete globalThis.document;
    }
  }
});

test("copyConnectionUri prefers synchronous copy and cleans up its temporary field", async () => {
  const clipboardDescriptor = Object.getOwnPropertyDescriptor(globalThis.navigator, "clipboard");
  const documentDescriptor = Object.getOwnPropertyDescriptor(globalThis, "document");
  const clipboardWrites = [];
  const fallbackWrites = [];
  const fields = [];
  let appendedField;
  try {
    Object.defineProperty(globalThis, "document", {
      configurable: true,
      value: {
        body: { append: (field) => { appendedField = field; } },
        createElement: () => {
          const field = {
            value: "",
            readOnly: false,
            selected: false,
            removed: false,
            style: {},
            focus() {},
            select() { this.selected = true; },
            setSelectionRange() { this.selected = true; },
            remove() { this.removed = true; },
          };
          fields.push(field);
          return field;
        },
        execCommand: (command) => {
          if (command !== "copy" || !appendedField?.selected) return false;
          fallbackWrites.push(appendedField.value);
          return true;
        },
      },
    });
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { clipboardWrites.push(value); } },
    });
    await copyConnectionUri("vless://secret");
    assert.deepEqual(clipboardWrites, []);
    assert.deepEqual(fallbackWrites, ["vless://secret"]);
    assert.equal(fields[0].readOnly, true);
    assert.equal(fields[0].removed, true);

    globalThis.document.execCommand = () => false;
    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: { writeText: async (value) => { clipboardWrites.push(value); } },
    });
    await copyConnectionUri("vless://fallback-secret");
    assert.deepEqual(clipboardWrites, ["vless://fallback-secret"]);
    assert.deepEqual(fallbackWrites, ["vless://secret"]);
    assert.equal(fields[1].removed, true);

    Object.defineProperty(globalThis.navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    await assert.rejects(() => copyConnectionUri("vless://unavailable"), /clipboard unavailable/);
    assert.equal(fields[2].removed, true);
  } finally {
    if (clipboardDescriptor) {
      Object.defineProperty(globalThis.navigator, "clipboard", clipboardDescriptor);
    } else {
      delete globalThis.navigator.clipboard;
    }
    if (documentDescriptor) {
      Object.defineProperty(globalThis, "document", documentDescriptor);
    } else {
      delete globalThis.document;
    }
  }
});
