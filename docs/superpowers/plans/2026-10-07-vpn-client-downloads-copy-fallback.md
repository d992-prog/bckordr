# VPN Client Downloads and Copy Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add direct client downloads for five platforms and make VPN-link copying work in restrictive Telegram and Nicegram WebViews.

**Architecture:** Keep `copyConnectionUri` as the single copy path used by the home screen and profile cards, adding a DOM-native fallback only when the Clipboard API is unavailable or rejected. Expand the existing platform configuration in `Portal.tsx`; reuse existing button styling and change CSS only where five tabs need responsive layout.

**Tech Stack:** React 18, TypeScript, native Clipboard/DOM APIs, Node test runner, Playwright browser QA, Vite.

---

### Task 1: Add and implement the shared clipboard fallback

**Files:**
- Modify: `frontend/test/vpnPortalConnection.test.mjs`
- Modify: `frontend/src/vpn-portal/connection.ts`

- [ ] **Step 1: Replace the existing clipboard test with failing fallback coverage**

Test three cases in the existing test file: Clipboard API success does not touch the DOM fallback; rejected or missing Clipboard API copies through a temporary `textarea` and removes it; both methods failing rejects with `clipboard unavailable`. Restore all global descriptors in `finally`.

```js
const originalDocument = globalThis.document;
const copied = [];
globalThis.document = {
  body: { append: (element) => { element.isConnected = true; } },
  createElement: () => ({ style: {}, select() {}, remove() { this.isConnected = false; } }),
  execCommand: (command) => command === "copy" && copied.push("fallback"),
};
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm --prefix frontend test -- --test-name-pattern="copyConnectionUri"`

Expected: FAIL because the current helper throws when Clipboard API is missing or rejected.

- [ ] **Step 3: Implement the minimum native fallback**

Update `copyConnectionUri` so it first awaits `navigator.clipboard.writeText` inside `try`; on failure, append an off-screen read-only `textarea`, select it, call `document.execCommand("copy")`, and remove it in `finally`. Throw `Error("clipboard unavailable")` only when the native copy returns false or throws.

- [ ] **Step 4: Re-run the focused test and verify GREEN**

Run: `npm --prefix frontend test -- --test-name-pattern="copyConnectionUri"`

Expected: PASS for primary, fallback, cleanup and total-failure cases.

### Task 2: Add official downloads to the connection instructions

**Files:**
- Modify: `frontend/test/browser/portal-ui.qa.mjs`
- Modify: `frontend/src/vpn-portal/Portal.tsx`
- Modify: `frontend/src/vpn-portal/portal.css`

- [ ] **Step 1: Change browser QA expectations first**

Make `verifyFullPortal` expect successful copying when `navigator.clipboard` is removed, capture the value passed to the fallback, and retain `assertSecretAbsent` after the copy. Expect these platform buttons in order:

```js
["iPhone", "Android", "Windows", "macOS", "Linux"]
```

Assert each platform exposes exactly one external download link with its exact URL:

```text
iPhone: https://apps.apple.com/us/app/happ-proxy-utility/id6504287215?l=ru
Android: https://play.google.com/store/apps/details?id=com.happproxy
Windows/macOS/Linux: https://github.com/hiddify/hiddify-app/releases/#release-v4.1.1
```

- [ ] **Step 2: Run browser QA and verify RED**

Run from `frontend`: `node test/browser/portal-ui.qa.mjs`

Expected: FAIL because only two platform tabs exist and clipboard fallback is not yet observed by the browser fixture.

- [ ] **Step 3: Expand the existing client configuration and render the download action**

Rename `VERIFIED_CLIENTS` to `VPN_CLIENTS` and give every entry `platform`, `app`, `downloadUrl`, and `downloadLabel`. Render the first instruction as a normal sentence followed by an external anchor:

```tsx
<a className="button button--ghost client-download" href={client.downloadUrl} target="_blank" rel="noreferrer">
  {client.downloadLabel}
</a>
```

Use release-ready Russian labels: «Скачать Happ в App Store», «Скачать Happ в Google Play» and «Скачать Hiddify».

- [ ] **Step 4: Apply the smallest responsive CSS change**

Keep two platform columns on narrow screens. At `min-width: 640px`, use five equal columns, and make `.client-download` align to the start while keeping the existing 44-pixel button target.

- [ ] **Step 5: Re-run browser QA and verify GREEN**

Run from `frontend`: `node test/browser/portal-ui.qa.mjs`

Expected: PASS, including fallback copy, secret cleanup, five platform tabs, external URLs, mobile overflow and accessibility checks.

### Task 3: Run release verification

**Files:**
- Verify all modified frontend files.

- [ ] **Step 1: Run Impeccable detector once on the changed UI targets**

Run:

```powershell
C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd detect --json frontend/src/vpn-portal/Portal.tsx frontend/src/vpn-portal/portal.css
```

Expected: no unresolved blocking design findings.

- [ ] **Step 2: Run the complete frontend test suite**

Run: `npm --prefix frontend test`

Expected: all tests pass.

- [ ] **Step 3: Build the production frontend**

Run: `npm --prefix frontend run build`

Expected: TypeScript and Vite production build complete successfully.

- [ ] **Step 4: Run final browser QA against the fresh build**

Run from `frontend`: `node test/browser/portal-ui.qa.mjs`

Expected: QA passes at all configured mobile and desktop viewports with no console errors, horizontal overflow, accessibility regressions or VPN-link exposure.

- [ ] **Step 5: Review the diff and commit only task files**

Run: `git diff --check` and inspect `git diff -- frontend/src/vpn-portal frontend/test/vpnPortalConnection.test.mjs frontend/test/browser/portal-ui.qa.mjs`.

Commit only the implementation and tests; leave existing temporary files and `frontend/tsconfig.tsbuildinfo` untouched.
