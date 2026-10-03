# Veltrix Liquid Glass Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship one polished Veltrix VPN visual system across the public site, customer cabinet/Telegram Mini App, and VPN-only admin areas without changing working VPN, session, subscription, or payment behavior.

**Architecture:** Keep the existing React 18/Vite entry points and API contracts. Add a small shared brand layer, pure portal view helpers, and focused presentation components; keep orchestration in the existing containers. Build the approved customer first viewport against the Impeccable comp, then extend the same tokens to the public site and a denser admin translation.

**Tech Stack:** React 18, TypeScript, Vite 6, native CSS, Node test runner, Playwright QA, existing `uqr`, Impeccable build phases. No new runtime dependency or UI framework.

---

## File structure

### Shared brand

- Create `frontend/src/brand/VeltrixMark.tsx` — reusable semantic logo component.
- Create `frontend/src/brand/veltrix-brand.css` — brand tokens, glass primitives, matte surfaces, focus and motion rules shared by the three entries.
- Create `frontend/public/veltrix-mark.svg` — flat vector source of truth.
- Modify `frontend/public/favicon.svg` — small-size mark using the same geometry.
- Create `frontend/test/veltrixBrand.test.mjs` — asset and token contract.

### Customer cabinet

- Create `frontend/src/vpn-portal/navigation.ts` — backward-compatible hash routing.
- Create `frontend/src/vpn-portal/homeView.ts` — pure selection and status copy.
- Create `frontend/src/vpn-portal/connection.ts` — one secure fetch/copy path shared by dashboard and profile cards.
- Create `frontend/src/vpn-portal/PortalHome.tsx` — approved status-lens first viewport.
- Create `frontend/src/vpn-portal/PortalNavigation.tsx` — three-destination navigation.
- Create `frontend/src/vpn-portal/PortalAccount.tsx` — account, support, policy, and logout.
- Modify `frontend/src/vpn-portal/Portal.tsx` — orchestration and three-view shell.
- Modify `frontend/src/vpn-portal/ProfileCard.tsx` — shared connection helper and new presentation.
- Modify `frontend/src/vpn-portal/TrialCard.tsx` and `PlanCatalog.tsx` — new material language, same behavior.
- Replace presentation rules in `frontend/src/vpn-portal/portal.css` without changing data behavior.
- Create `frontend/test/vpnPortalNavigation.test.mjs` and `frontend/test/vpnPortalHome.test.mjs`.
- Modify `frontend/test/browser/portal-ui.qa.mjs`.

### Public site

- Modify `frontend/src/vpn-site/main.tsx` — revised content hierarchy and CTA copy.
- Replace presentation rules in `frontend/src/vpn-site/site.css` using shared brand tokens.
- Modify `frontend/vpn/index.html` — brand metadata only.
- Modify `frontend/test/vpnPublicSite.test.mjs`.

### VPN admin

- Create `frontend/src/VpnAdminNavigation.tsx` — in-page navigation between VPN work areas.
- Create `frontend/src/vpnAdminView.ts` — pure section selection and metric formatting.
- Modify `frontend/src/App.tsx` — arrange existing VPN panels into named areas without changing requests or mutations.
- Modify `frontend/src/VpnCustomerWorkspacePanel.tsx`, `VpnEndpointCapacityPanel.tsx`, and `VpnReleaseReadinessPanel.tsx` — semantic headings and class hooks.
- Extend `frontend/src/styles.css` with scoped `.vpn-admin-*` rules.
- Create `frontend/test/vpnAdminView.test.mjs` and modify existing VPN admin tests.

### Finish

- Create `DESIGN.md` from the shipped system after final review.
- Keep `.impeccable/build/state.json`, measured spec, approved comp, shipping plates, and raster provenance sidecars.
- Modify browser QA scripts only where selectors reflect the approved information architecture.

---

### Task 1: Baseline and measured comp contract

**Files:**
- Create: `.impeccable/build/regions.json`
- Create: `.impeccable/build/spec.json` through Impeccable
- Create: `.impeccable/build/comp-grid.png` through Impeccable
- Modify: `.impeccable/build/state.json`

- [ ] **Step 1: Run the current frontend checks before editing**

Run:

```powershell
npm test
npm run build
```

Working directory: `frontend`

Expected: all Node tests pass and Vite writes the three existing entry points.

- [ ] **Step 2: Produce and inspect the 10×10 comp grid**

Run from the repository root:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --comp .impeccable/mocks/portal-liquid-glass-c.png --grid
```

Open `.impeccable/build/comp-grid.png`. Confirm that the following regions fit their complete visual bounds before writing the region file: background field, flat brand mark, brand wordmark, help action, lens material, signal illustration, status text, hero title, expiry line, primary action, quick-action sheet, two quick-action rows, subscription sheet, subscription labels, and three navigation destinations.

- [ ] **Step 3: Write the measured region map**

Create `.impeccable/build/regions.json` with this semantic inventory, adjusting only grid spans when the grid image proves a boundary:

```json
{
  "regions": [
    { "id": "atmosphere", "kind": "texture", "grid": "A0:J9", "note": "pearl blue spectral atmospheric background" },
    { "id": "brand-mark", "kind": "plate", "grid": "A0:B1", "note": "three translucent Veltrix lenses without text" },
    { "id": "brand-name", "kind": "text", "grid": "B0:E1", "note": "Veltrix VPN wordmark" },
    { "id": "help", "kind": "control", "grid": "H0:J1", "note": "Помощь text action" },
    { "id": "status-lens", "kind": "plate", "grid": "A1:J4", "note": "asymmetric optical glass lens without text or icons" },
    { "id": "signal-orb", "kind": "plate", "grid": "A2:E4", "note": "mint signal orb and radio arcs" },
    { "id": "status-label", "kind": "text", "grid": "E1:H2", "note": "АКТИВЕН status" },
    { "id": "hero-title", "kind": "text", "grid": "E2:J3", "note": "Ваш VPN готов" },
    { "id": "hero-detail", "kind": "text", "grid": "E3:J4", "note": "profile expiry detail" },
    { "id": "primary-action", "kind": "control", "grid": "A3:J5", "note": "Открыть профиль glass control" },
    { "id": "quick-title", "kind": "text", "grid": "A4:G5", "note": "Быстрые действия" },
    { "id": "copy-action", "kind": "control", "grid": "A5:J6", "note": "Скопировать ссылку row" },
    { "id": "guide-action", "kind": "control", "grid": "A6:J7", "note": "Инструкция row" },
    { "id": "access-card", "kind": "chrome", "grid": "A7:J9", "note": "matte access card" },
    { "id": "access-label", "kind": "text", "grid": "B7:F8", "note": "access type label" },
    { "id": "access-value", "kind": "text", "grid": "B8:G9", "note": "access term value" },
    { "id": "access-status", "kind": "text", "grid": "G8:J9", "note": "Активна status" },
    { "id": "navigation", "kind": "chrome", "grid": "A8:J9", "note": "floating optical navigation shell" },
    { "id": "nav-home", "kind": "control", "grid": "A8:D9", "note": "Главная navigation destination" },
    { "id": "nav-profiles", "kind": "control", "grid": "D8:G9", "note": "Профили navigation destination" },
    { "id": "nav-account", "kind": "control", "grid": "G8:J9", "note": "Аккаунт navigation destination" }
  ]
}
```

- [ ] **Step 4: Measure regions and choose the lead typeface by evidence**

Run:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --comp .impeccable/mocks/portal-liquid-glass-c.png --regions .impeccable/build/regions.json
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' font-match --measure hero-title
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' font-match --rank hero-title --text 'Ваш VPN готов'
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase advance
```

Expected: `spec` closes and `plates` opens. If the gate names an unmeasured ink region, add that exact region to `regions.json`; do not weaken or bypass the gate.

- [ ] **Step 5: Commit the measured contract**

```powershell
git add -f .impeccable/build
git commit -m "design: measure Veltrix portal composition"
```

---

### Task 2: Shared logo and brand primitives

**Files:**
- Create: `frontend/src/brand/VeltrixMark.tsx`
- Create: `frontend/src/brand/veltrix-brand.css`
- Create: `frontend/public/veltrix-mark.svg`
- Modify: `frontend/public/favicon.svg`
- Create: `frontend/test/veltrixBrand.test.mjs`

- [ ] **Step 1: Write the failing brand contract test**

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const markUrl = new URL("../src/brand/VeltrixMark.tsx", import.meta.url);
const cssUrl = new URL("../src/brand/veltrix-brand.css", import.meta.url);
const assetUrl = new URL("../public/veltrix-mark.svg", import.meta.url);

test("shared Veltrix brand is vector, accessible, and tokenized", async () => {
  const [mark, css, asset] = await Promise.all([
    readFile(markUrl, "utf8"),
    readFile(cssUrl, "utf8"),
    readFile(assetUrl, "utf8"),
  ]);
  assert.match(mark, /aria-hidden=\{decorative\}/);
  assert.match(mark, /Veltrix VPN/);
  for (const token of ["--vx-pearl", "--vx-ink", "--vx-blue", "--vx-mint", "--vx-lilac"]) {
    assert.ok(css.includes(token), token);
  }
  assert.match(css, /prefers-reduced-motion/);
  assert.match(css, /@supports not \(backdrop-filter:/);
  assert.doesNotMatch(asset, />V<|shield|padlock|globe/i);
});
```

- [ ] **Step 2: Run the test and verify the missing-file failure**

Run: `node --experimental-strip-types --test test/veltrixBrand.test.mjs`

Expected: FAIL with `ENOENT` for `VeltrixMark.tsx`.

- [ ] **Step 3: Create the reusable mark component**

```tsx
interface VeltrixMarkProps {
  className?: string;
  decorative?: boolean;
  withName?: boolean;
}

export function VeltrixMark({
  className = "",
  decorative = false,
  withName = true,
}: VeltrixMarkProps) {
  return (
    <span className={`vx-brand ${className}`.trim()} aria-label={decorative ? undefined : "Veltrix VPN"}>
      <svg className="vx-mark" viewBox="0 0 64 64" aria-hidden={decorative} role={decorative ? undefined : "img"}>
        {!decorative && <title>Veltrix VPN</title>}
        <ellipse className="vx-mark__lens vx-mark__lens--blue" cx="32" cy="20" rx="12" ry="20" transform="rotate(8 32 20)" />
        <ellipse className="vx-mark__lens vx-mark__lens--mint" cx="23" cy="36" rx="11" ry="19" transform="rotate(-48 23 36)" />
        <ellipse className="vx-mark__lens vx-mark__lens--lilac" cx="43" cy="38" rx="10" ry="18" transform="rotate(48 43 38)" />
      </svg>
      {withName && <span className="vx-brand__name">Veltrix VPN</span>}
    </span>
  );
}
```

- [ ] **Step 4: Add the flat SVG and shared tokens**

Use the same three ellipses in `frontend/public/veltrix-mark.svg`; keep the SVG text-free. Add this token base to `veltrix-brand.css` and extend it with the measured font-match `USE` line from Task 1:

```css
:root {
  --vx-pearl: #eef4fa;
  --vx-ink: #101c2c;
  --vx-blue: #256dff;
  --vx-mint: #72e2c0;
  --vx-lilac: #c6b7ff;
  --vx-surface: rgba(255, 255, 255, 0.88);
  --vx-border: rgba(16, 28, 44, 0.1);
  --vx-focus: #0b57d0;
  --vx-radius-control: 18px;
  --vx-radius-card: 28px;
  --vx-radius-lens: 44px;
  color-scheme: light dark;
}

.vx-glass {
  background: linear-gradient(135deg, rgba(255,255,255,.72), rgba(201,222,255,.44));
  border: 1px solid rgba(255,255,255,.8);
  box-shadow: inset 0 1px rgba(255,255,255,.9), 0 18px 50px rgba(37,109,255,.12);
  backdrop-filter: blur(24px) saturate(140%);
}

:focus-visible { outline: 3px solid var(--vx-focus); outline-offset: 3px; }

@supports not (backdrop-filter: blur(1px)) {
  .vx-glass { background: #e7f0ff; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation-duration: .01ms !important; animation-iteration-count: 1 !important; transition-duration: .01ms !important; }
}
```

- [ ] **Step 5: Run the brand test and frontend build**

Run:

```powershell
node --experimental-strip-types --test test/veltrixBrand.test.mjs
npm run build
```

Expected: PASS and a production build with no new package.

- [ ] **Step 6: Produce the four measured raster materials**

For each painted region, obtain its measured prompt:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --plate-prompt atmosphere --background opaque
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --plate-prompt brand-mark --background transparent
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --plate-prompt status-lens --background transparent
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' comp-spec --plate-prompt signal-orb --background transparent
```

Generate each prompt as its own new asset, never as a crop of the approved comp. Save lossless sources and sidecars under:

```text
.impeccable/assets/atmosphere.png
.impeccable/assets/atmosphere.png.json
.impeccable/assets/brand-mark.png
.impeccable/assets/brand-mark.png.json
.impeccable/assets/status-lens.png
.impeccable/assets/status-lens.png.json
.impeccable/assets/signal-orb.png
.impeccable/assets/signal-orb.png.json
```

Each JSON contains the exact prompt, source model/tool, creation date, intended region id, dimensions, and `approved: true` only after visual inspection against the region crop. Optimized shipping copies go to `frontend/public/brand/` and retain a sibling provenance JSON.

- [ ] **Step 7: Close the plates gate**

Run:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase advance
```

Expected: every plate region resolves to a non-crop asset, `plates` closes, and `hero` opens. Fix named missing or mismatched assets; never mark a failed plate approved.

- [ ] **Step 8: Commit**

```powershell
git add frontend/src/brand frontend/public/veltrix-mark.svg frontend/public/favicon.svg frontend/public/brand frontend/test/veltrixBrand.test.mjs .impeccable/assets .impeccable/build
git commit -m "feat: add Veltrix brand primitives"
```

---

### Task 3: Portal navigation and honest home-state model

**Files:**
- Create: `frontend/src/vpn-portal/navigation.ts`
- Create: `frontend/src/vpn-portal/homeView.ts`
- Create: `frontend/test/vpnPortalNavigation.test.mjs`
- Create: `frontend/test/vpnPortalHome.test.mjs`

- [ ] **Step 1: Write failing navigation tests**

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { portalSectionFromHash } from "../src/vpn-portal/navigation.ts";

test("maps old and new portal hashes into three destinations", () => {
  assert.equal(portalSectionFromHash("#home"), "home");
  assert.equal(portalSectionFromHash("#subscription"), "home");
  assert.equal(portalSectionFromHash("#plans"), "home");
  assert.equal(portalSectionFromHash("#profiles"), "profiles");
  assert.equal(portalSectionFromHash("#connect"), "profiles");
  assert.equal(portalSectionFromHash("#help"), "account");
  assert.equal(portalSectionFromHash("#unknown"), "home");
});
```

- [ ] **Step 2: Write failing home-state tests**

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { buildPortalHomeView } from "../src/vpn-portal/homeView.ts";

const activeSubscription = {
  id: 1, service_name: "Veltrix VPN", state: "active",
  starts_at: null, expires_at: "2026-10-30T00:00:00Z",
  profile_limit: 1, profiles_used: 1, traffic_limit_gb_per_profile: null,
};

test("reports a ready profile without claiming an active tunnel", () => {
  const view = buildPortalHomeView([activeSubscription], [
    { id: 2, subscription_id: 1, display_name: "iPhone", state: "active", can_connect: true },
  ], null);
  assert.equal(view.kind, "ready");
  assert.equal(view.title, "VPN‑профиль готов");
  assert.equal(view.profileId, 2);
  assert.doesNotMatch(`${view.title} ${view.detail}`, /подключено|защищено/i);
});

test("keeps preparing, expired, and empty states explicit", () => {
  assert.equal(buildPortalHomeView([], [], { state: "preparing", duration_days: 7, profile_limit: 1, subscription_id: null, access_key_id: null, expires_at: null }).kind, "preparing");
  assert.equal(buildPortalHomeView([activeSubscription], [], null).kind, "preparing");
  assert.equal(buildPortalHomeView([{ ...activeSubscription, state: "expired" }], [], null).kind, "expired");
  assert.equal(buildPortalHomeView([], [], null).kind, "empty");
});
```

- [ ] **Step 3: Run tests and verify missing-module failures**

Run: `node --experimental-strip-types --test test/vpnPortalNavigation.test.mjs test/vpnPortalHome.test.mjs`

Expected: FAIL because the two modules do not exist.

- [ ] **Step 4: Implement the minimal pure helpers**

```typescript
// navigation.ts
export type PortalSection = "home" | "profiles" | "account";

const SECTION_ALIASES: Record<string, PortalSection> = {
  home: "home", subscription: "home", plans: "home",
  profiles: "profiles", connect: "profiles",
  account: "account", help: "account",
};

export function portalSectionFromHash(hash: string): PortalSection {
  const key = hash.replace(/^#\/?/, "").split("?", 1)[0];
  return SECTION_ALIASES[key] ?? "home";
}
```

```typescript
// homeView.ts
import type { PortalProfile, PortalSubscription, PortalTrial } from "./types";
import { portalDate } from "./view";

export interface PortalHomeView {
  kind: "ready" | "preparing" | "paused" | "expired" | "empty";
  title: string;
  detail: string;
  profileId: number | null;
}

export function buildPortalHomeView(
  subscriptions: PortalSubscription[],
  profiles: PortalProfile[],
  trial: PortalTrial | null,
): PortalHomeView {
  const profile = profiles.find((item) => item.state === "active" && item.can_connect);
  const subscription = profile
    ? subscriptions.find((item) => item.id === profile.subscription_id)
    : subscriptions.find((item) => item.state === "active" || item.state === "trial");
  if (profile && subscription) return {
    kind: "ready",
    title: "VPN‑профиль готов",
    detail: `Доступ действует до ${portalDate(subscription.expires_at)}`,
    profileId: profile.id,
  };
  if (trial?.state === "preparing") return { kind: "preparing", title: "Профиль готовится", detail: "Это может занять несколько минут", profileId: null };
  if (subscriptions.some((item) => item.state === "active" || item.state === "trial")) return { kind: "preparing", title: "Профиль готовится", detail: "Это может занять несколько минут", profileId: null };
  if (subscriptions.some((item) => item.state === "expired")) return { kind: "expired", title: "Срок доступа закончился", detail: "Выберите доступный тариф или напишите в поддержку", profileId: null };
  if (subscriptions.some((item) => ["disabled", "suspended"].includes(item.state))) return { kind: "paused", title: "Доступ приостановлен", detail: "Напишите в поддержку, чтобы уточнить причину", profileId: null };
  return { kind: "empty", title: "VPN‑профиля пока нет", detail: "Получите пробный доступ или напишите в поддержку", profileId: null };
}
```

- [ ] **Step 5: Run tests and commit**

Run: `node --experimental-strip-types --test test/vpnPortalNavigation.test.mjs test/vpnPortalHome.test.mjs`

Expected: PASS.

```powershell
git add frontend/src/vpn-portal/navigation.ts frontend/src/vpn-portal/homeView.ts frontend/test/vpnPortalNavigation.test.mjs frontend/test/vpnPortalHome.test.mjs
git commit -m "feat: model Veltrix portal navigation and status"
```

---

### Task 4: Portal shell, status lens, and secure quick actions

**Files:**
- Create: `frontend/src/vpn-portal/connection.ts`
- Create: `frontend/src/vpn-portal/PortalNavigation.tsx`
- Create: `frontend/src/vpn-portal/PortalHome.tsx`
- Modify: `frontend/src/vpn-portal/Portal.tsx:31-44, 656-719`
- Modify: `frontend/src/vpn-portal/ProfileCard.tsx:1-190`
- Modify: `frontend/test/browser/portal-ui.qa.mjs`

- [ ] **Step 1: Add failing browser assertions for the approved first viewport**

Replace the old five-navigation assertion in `verifyFullPortal` with:

```javascript
await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
assert.equal(await page.locator(".portal-nav a").count(), 3);
assert.equal(await page.getByRole("link", { name: "Открыть профиль" }).count(), 1);
assert.equal(await page.getByRole("button", { name: "Скопировать ссылку" }).count(), 1);
assert.equal(await page.getByRole("link", { name: "Инструкция" }).count(), 1);
assert.equal(await page.locator(".portal-status-lens").count(), 1);
assert.equal(/подключено|защищено/i.test(await page.locator(".portal-status-lens").innerText()), false);
```

- [ ] **Step 2: Run QA and verify it fails on the old portal**

Run:

```powershell
npm run build
node test/browser/portal-ui.qa.mjs
```

Expected: FAIL because `.portal-nav`, `.portal-status-lens`, and the new heading do not exist.

- [ ] **Step 3: Extract the secure connection request**

```typescript
// connection.ts
import { PortalError, portalApi } from "./api";

export async function loadConnectionUri(profileId: number): Promise<string> {
  const result = await portalApi.connection(profileId);
  if (!result.uri) throw new PortalError(0, "Ссылка подключения недоступна.");
  return result.uri;
}

export async function copyConnectionUri(uri: string): Promise<void> {
  if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
  await navigator.clipboard.writeText(uri);
}
```

Update `ProfileCard.tsx` to call these two functions while preserving its epoch and session-generation guards. Keep the full URI hidden until `revealConnection()` succeeds.

- [ ] **Step 4: Create three-destination navigation**

```tsx
import type { PortalSection } from "./navigation";

const ITEMS: ReadonlyArray<[PortalSection, string, string]> = [
  ["home", "Главная", "M4 11.5 12 4l8 7.5V20h-5v-5H9v5H4z"],
  ["profiles", "Профили", "M4 4h6v6H4zm10 0h6v6h-6zM4 14h6v6H4zm10 0h6v6h-6z"],
  ["account", "Аккаунт", "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-7 8a7 7 0 0 1 14 0z"],
];

export function PortalNavigation({ active }: { active: PortalSection }) {
  return (
    <nav className="portal-nav vx-glass" aria-label="Разделы кабинета">
      {ITEMS.map(([id, label, path]) => (
        <a key={id} href={`#${id}`} aria-current={active === id ? "page" : undefined}>
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d={path} /></svg><span>{label}</span>
        </a>
      ))}
    </nav>
  );
}
```

The three one-path icons stay under the Impeccable icon budget; visible link text supplies the accessible name.

- [ ] **Step 5: Build the approved home component**

`PortalHome.tsx` receives the already-loaded subscriptions, profiles, trial, errors, and trial callbacks. Its top-level structure is:

```tsx
<section id="home" className="portal-home">
  <div className={`portal-status-lens portal-status-lens--${view.kind}`}>
    <span className="portal-status-lens__badge">{badge}</span>
    <h1>{view.title}</h1>
    <p>{view.detail}</p>
  </div>
  {view.profileId !== null && <a className="portal-primary vx-glass" href="#profiles">Открыть профиль <span aria-hidden="true">→</span></a>}
  <section className="portal-actions" aria-labelledby="quick-actions-title">
    <h2 id="quick-actions-title">Быстрые действия</h2>
    <button disabled={copyBusy} onClick={() => void copyProfile(view.profileId)}>Скопировать ссылку</button>
    <a href="#connect">Инструкция</a>
    {copyMessage && <p role="status">{copyMessage}</p>}
  </section>
  <AccessSummary subscriptions={subscriptions} trial={trial} />
</section>
```

Use `SessionGeneration` before and after loading the connection URI. Success copy is exactly `Ссылка скопирована`. Clipboard fallback is `Не удалось скопировать автоматически. Откройте профиль и скопируйте ссылку вручную`.

- [ ] **Step 6: Rewire the portal shell without changing loaders or logout**

In `Portal.tsx`:

- replace `NAVIGATION` and local `sectionFromHash` with `portalSectionFromHash`;
- render `VeltrixMark`, a top `Помощь` link, and `PortalNavigation`;
- render `PortalHome` for `home`, existing profile cards plus platform instructions for `profiles`, and account/help/plans for `account`;
- keep `loadPrivateData`, trial polling, plan loading, stale-response guards, authorization handling, and `performLogout` unchanged.

- [ ] **Step 7: Re-run browser QA and targeted unit tests**

Run:

```powershell
npm run build
node --experimental-strip-types --test test/vpnPortal*.test.mjs
node test/browser/portal-ui.qa.mjs
```

Expected: PASS; generated screenshots include the ready dashboard and still exercise reveal, QR, rename, stale request, logout, and trial preparation.

- [ ] **Step 8: Commit**

```powershell
git add frontend/src/vpn-portal frontend/test/browser/portal-ui.qa.mjs frontend/test/vpnPortal*.test.mjs
git commit -m "feat: redesign Veltrix customer portal shell"
```

---

### Task 5: Portal profiles, account, and state pages

**Files:**
- Create: `frontend/src/vpn-portal/PortalAccount.tsx`
- Modify: `frontend/src/vpn-portal/ProfileCard.tsx`
- Modify: `frontend/src/vpn-portal/TrialCard.tsx`
- Modify: `frontend/src/vpn-portal/PlanCatalog.tsx`
- Modify: `frontend/src/vpn-portal/Portal.tsx:570-655`
- Modify: `frontend/test/browser/portal-ui.qa.mjs`

- [ ] **Step 1: Add failing assertions for account and secret handling**

```javascript
await page.locator('.portal-nav a[href="#account"]').click();
await page.getByRole("heading", { name: "Аккаунт" }).waitFor();
await page.getByText(me.display_name).waitFor();
await page.getByRole("button", { name: "Выйти" }).waitFor();
await page.locator('.portal-nav a[href="#profiles"]').click();
assert.equal(await page.locator('textarea.connection-uri').count(), 0);
await page.getByRole("button", { name: "Показать ссылку" }).first().click();
assert.match(await page.locator('textarea.connection-uri').first().inputValue(), /^vless:\/\//);
```

- [ ] **Step 2: Implement `PortalAccount` with existing data only**

```tsx
interface PortalAccountProps {
  displayName: string;
  supportText: string;
  plans: PortalPlan[];
  plansBusy: boolean;
  plansError: string;
  onPlansRetry: () => void;
  onLogout: () => void;
}

export function PortalAccount(props: PortalAccountProps) {
  return (
    <section id="account" className="portal-account">
      <header><p className="eyebrow">Настройки</p><h1>Аккаунт</h1></header>
      <article className="vx-matte"><span>Telegram</span><strong>{props.displayName}</strong></article>
      <section aria-labelledby="plans-title"><h2 id="plans-title">Тарифы</h2><PlanCatalog plans={props.plans} busy={props.plansBusy} error={props.plansError} onRetry={props.onPlansRetry} /></section>
      <article className="vx-matte"><h2>Помощь</h2><p>{props.supportText}</p></article>
      <nav aria-label="Документы"><a href="/vpn/#privacy">Конфиденциальность</a><a href="/vpn/#terms">Условия использования</a></nav>
      <button className="button button--quiet" onClick={props.onLogout}>Выйти</button>
    </section>
  );
}
```

- [ ] **Step 3: Polish profile and trial copy without changing behavior**

Use these production strings:

- `Показать ссылку подключения` → `Показать ссылку`;
- `Скопировано.` → `Ссылка скопирована`;
- unavailable active profile → `Ссылка пока недоступна. Попробуйте через несколько минут`;
- inactive profile → `Профиль неактивен. Подключение недоступно`;
- trial preparing → `Готовим VPN‑профиль` and `Это может занять несколько минут`;
- generic load error remains `Не удалось загрузить данные. Попробуйте снова`.

Do not change API errors, CSRF handling, QR generation, rename limits, or async generation guards.

- [ ] **Step 4: Restyle login/logout/error state markup with the new mark**

Keep every state branch and action from `Portal.tsx`. Replace only the old `<span>V</span>` brand and presentation classes. The signed-out screen must not remount private content.

- [ ] **Step 5: Run the full portal test set and QA**

Run:

```powershell
node --experimental-strip-types --test test/vpnPortal*.test.mjs test/vpnPlanCatalog.test.mjs
npm run build
node test/browser/portal-ui.qa.mjs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/vpn-portal frontend/test
git commit -m "feat: polish Veltrix portal profiles and account"
```

---

### Task 6: Portal Liquid Glass styling, responsive behavior, and dark theme

**Files:**
- Modify: `frontend/src/vpn-portal/portal.css`
- Modify: `frontend/test/browser/portal-ui.qa.mjs`

- [ ] **Step 1: Add viewport, dark-mode, fallback, and motion QA cases**

Add captures for:

```javascript
for (const scenario of [
  { name: "portal-390-light", viewport: { width: 390, height: 844 }, colorScheme: "light" },
  { name: "portal-390-dark", viewport: { width: 390, height: 844 }, colorScheme: "dark" },
  { name: "portal-320-light", viewport: { width: 320, height: 700 }, colorScheme: "light" },
  { name: "portal-1024-light", viewport: { width: 1024, height: 900 }, colorScheme: "light" },
]) {
  const { context, page } = await newPortalPage(browser, scenario);
  await installApi(page, async (route, pathname, request) => {
    if (pathname.endsWith("/config")) return responseJson(route, portalConfig);
    if (pathname.endsWith("/me")) return responseJson(route, me);
    if (pathname.endsWith("/subscriptions")) return responseJson(route, subscriptions);
    if (pathname.endsWith("/profiles") && request.method() === "GET") return responseJson(route, profiles);
    if (pathname.endsWith("/plans")) return responseJson(route, []);
    return responseJson(route, {}, 404);
  });
  await page.goto(`${origin}/cabinet/#home`);
  await page.getByRole("heading", { name: "VPN‑профиль готов" }).waitFor();
  await assertNoHorizontalOverflow(page);
  await page.screenshot({ path: path.join(outputRoot, `${scenario.name}.png`), fullPage: true });
  await context.close();
}
```

Also emulate reduced motion and assert computed `animationDuration` for `.portal-status-lens` is `0.01ms` or `0s`.

- [ ] **Step 2: Run QA and verify new visual assertions fail**

Run: `npm run build; node test/browser/portal-ui.qa.mjs`

Expected: FAIL before the new scoped styles exist.

- [ ] **Step 3: Generate the measured scaffold, then replace portal presentation rules**

Run first:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase scaffold
```

Import `../brand/veltrix-brand.css`. Bind the measured regions through the generated `.impeccable/build/scaffold/layout.css`. Required structural rules:

```css
.portal-shell { min-height: 100dvh; color: var(--vx-ink); background: var(--vx-pearl); padding: max(18px, var(--tg-safe-area-inset-top, 0px)) 18px calc(112px + var(--tg-safe-area-inset-bottom, 0px)); }
.portal-content { width: min(100%, 720px); margin-inline: auto; }
.portal-status-lens { position: relative; min-height: 280px; border-radius: var(--vx-radius-lens); isolation: isolate; }
.portal-primary { display: flex; min-height: 64px; align-items: center; justify-content: center; margin: -30px 18px 0; position: relative; }
.portal-actions, .portal-access-card { background: var(--vx-surface); border: 1px solid var(--vx-border); border-radius: var(--vx-radius-card); }
.portal-nav { position: fixed; z-index: 20; inset-inline: max(16px, env(safe-area-inset-left)) max(16px, env(safe-area-inset-right)); bottom: max(14px, env(safe-area-inset-bottom)); min-height: 68px; }
@media (min-width: 760px) { .portal-nav { width: 540px; left: 50%; right: auto; transform: translateX(-50%); } }
@media (prefers-color-scheme: dark) { :root { --vx-pearl: #07111e; --vx-ink: #f4f8ff; --vx-surface: rgba(13,29,47,.92); --vx-border: rgba(205,225,255,.14); } }
```

Do not bake text, dates, controls, or navigation labels into any plate.

- [ ] **Step 4: Run Impeccable hero scaffold and first-viewport gate**

Run:

```powershell
npm run build
node test/browser/portal-ui.qa.mjs
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase record hero
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase advance
```

Expected: hero score reaches the gate threshold with no missing or contradicted region. Open each named crop before any retry.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/vpn-portal/portal.css frontend/test/browser/portal-ui.qa.mjs .impeccable/build
git commit -m "style: match Veltrix portal Liquid Glass composition"
```

---

### Task 7: Public site redesign

**Files:**
- Modify: `frontend/src/vpn-site/main.tsx`
- Modify: `frontend/src/vpn-site/site.css`
- Modify: `frontend/vpn/index.html`
- Modify: `frontend/test/vpnPublicSite.test.mjs`

- [ ] **Step 1: Replace old visual-contract assertions with the approved brand contract**

Keep all public API-safety assertions. Replace the old green-token assertion and add:

```javascript
for (const required of [
  "VPN без сложных настроек",
  "Открыть в Telegram",
  "Как подключиться",
  "Поддерживаемые устройства",
  "Оплата пока не подключена",
]) assert.ok(source.includes(required), required);
assert.match(css, /--vx-blue/);
assert.match(css, /\.site-connection-route/);
assert.doesNotMatch(source, /<span>V<\/span>/);
assert.doesNotMatch(source, /гарантирован|самый быстрый|без ограничений|полная анонимность/i);
```

- [ ] **Step 2: Run the test and verify the old page fails**

Run: `node --experimental-strip-types --test test/vpnPublicSite.test.mjs`

Expected: FAIL on the new heading, brand, and route component.

- [ ] **Step 3: Recompose `main.tsx` while preserving loaders and honest plan behavior**

Use this section order:

```tsx
<main id="top">
  <Hero config={config} configBusy={configBusy} configError={configError} onConfigRetry={loadConfig} />
  <ConnectionRoute />
  <SupportedApps />
  <PlansSection plans={plans} busy={plansBusy} error={plansError} config={config} onRetry={loadPlans} />
  <TrialSection config={config} />
  <SupportSection config={config} configBusy={configBusy} />
  <Policies />
</main>
```

Hero copy:

```tsx
<p className="eyebrow">Простой старт через Telegram</p>
<h1>VPN без сложных настроек</h1>
<p className="lead">Получите готовый профиль в официальном боте и добавьте его в поддерживаемое приложение</p>
<BotAction config={config} label="Открыть в Telegram" />
<a className="button button--ghost" href="#how">Как подключиться</a>
```

Keep public requests limited to `config()` and `plans()`. Keep paid plan actions disabled while payment is absent.

- [ ] **Step 4: Replace site styles with the same world, at marketing density**

Import shared brand CSS. Use a landscape glass hero object, matte content sections, and the same logo; do not copy the mobile dashboard layout. At `max-width: 900px`, stack the hero and keep both actions full-width without horizontal overflow.

- [ ] **Step 5: Test and build**

Run:

```powershell
node --experimental-strip-types --test test/vpnPublicSite.test.mjs
npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/vpn-site frontend/vpn/index.html frontend/test/vpnPublicSite.test.mjs
git commit -m "feat: redesign Veltrix public VPN site"
```

---

### Task 8: VPN admin information architecture

**Files:**
- Create: `frontend/src/vpnAdminView.ts`
- Create: `frontend/src/VpnAdminNavigation.tsx`
- Create: `frontend/test/vpnAdminView.test.mjs`
- Modify: `frontend/src/App.tsx:4462-4800`

- [ ] **Step 1: Write failing section and metric tests**

```javascript
import test from "node:test";
import assert from "node:assert/strict";
import { vpnAdminSectionFromHash, displayMetric } from "../src/vpnAdminView.ts";

test("VPN admin sections are stable and unknown metrics stay unknown", () => {
  assert.equal(vpnAdminSectionFromHash("#vpn/overview"), "overview");
  assert.equal(vpnAdminSectionFromHash("#vpn/customers"), "customers");
  assert.equal(vpnAdminSectionFromHash("#vpn/nodes"), "nodes");
  assert.equal(vpnAdminSectionFromHash("#vpn/plans"), "plans");
  assert.equal(vpnAdminSectionFromHash("#vpn/events"), "events");
  assert.equal(vpnAdminSectionFromHash("#vpn/unknown"), "overview");
  assert.equal(displayMetric(null), "Нет данных");
  assert.equal(displayMetric(0), "0");
  assert.equal(displayMetric(12), "12");
});
```

- [ ] **Step 2: Run the test and verify missing-module failure**

Run: `node --experimental-strip-types --test test/vpnAdminView.test.mjs`

Expected: FAIL with `ERR_MODULE_NOT_FOUND`.

- [ ] **Step 3: Implement the pure helper and navigation**

```typescript
export type VpnAdminSection = "overview" | "customers" | "nodes" | "plans" | "events";
const SECTION_KEYS = new Set<VpnAdminSection>(["overview", "customers", "nodes", "plans", "events"]);
export function vpnAdminSectionFromHash(hash: string): VpnAdminSection {
  const candidate = hash.replace(/^#vpn\/?/, "").split("?", 1)[0] as VpnAdminSection;
  return SECTION_KEYS.has(candidate) ? candidate : "overview";
}
export function displayMetric(value: number | null | undefined): string {
  return value == null ? "Нет данных" : String(value);
}
```

`VpnAdminNavigation.tsx` renders five links with `aria-current` and no local state; `App.tsx` remains the single hash-state owner.

- [ ] **Step 4: Reorganize `renderVpn()` without changing effects or handlers**

Wrap existing blocks into these conditional areas:

- `overview`: current summary, release readiness, lifecycle, capacity;
- `customers`: existing `VpnCustomerWorkspace` including invitations and keys;
- `nodes`: existing node table and maintenance actions;
- `plans`: existing new-plan form and plans table;
- `events`: Telegram updates and node/lifecycle events.

Do not rename API calls or move mutation state. Replace `vpnOverview?.value ?? 0` with `displayMetric(vpnOverview?.value)` so unavailable data is not reported as zero.

- [ ] **Step 5: Run tests and build**

Run:

```powershell
node --experimental-strip-types --test test/vpnAdminView.test.mjs test/vpnCustomerWorkspace.test.mjs test/vpnEndpointCapacity.test.mjs test/vpnReleaseReadiness.test.mjs
npm run build
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/vpnAdminView.ts frontend/src/VpnAdminNavigation.tsx frontend/src/App.tsx frontend/test/vpnAdminView.test.mjs
git commit -m "feat: organize Veltrix VPN admin workspace"
```

---

### Task 9: VPN admin presentation and destructive-action clarity

**Files:**
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/VpnCustomerWorkspacePanel.tsx`
- Modify: `frontend/src/VpnEndpointCapacityPanel.tsx`
- Modify: `frontend/src/VpnReleaseReadinessPanel.tsx`
- Modify: `frontend/test/browser/admin-profile-ui.qa.mjs`
- Modify: `frontend/test/vpnCustomerWorkspace.test.mjs`

- [ ] **Step 1: Add failing source and browser assertions**

```javascript
assert.match(styles, /\.vpn-admin-shell/);
assert.match(styles, /\.vpn-admin-nav/);
assert.match(styles, /\.vpn-admin-metric/);
assert.match(appSource, /Да, удалить ноду/);
assert.match(workspaceSource, /Показать полную ссылку/);
assert.match(workspaceSource, /Скопировать ссылку/);
```

In browser QA, assert keyboard focus reaches VPN navigation, customer search, and the first profile action in that order.

- [ ] **Step 2: Run tests and verify the new contract fails**

Run:

```powershell
node --experimental-strip-types --test test/vpnCustomerWorkspace.test.mjs
npm run build
node test/browser/admin-profile-ui.qa.mjs
```

Expected: FAIL on the new classes and copy.

- [ ] **Step 3: Add scoped admin tokens and layouts**

```css
.vpn-admin-shell { --vpn-admin-surface: rgba(255,255,255,.94); display: grid; gap: 18px; min-width: 0; }
.vpn-admin-nav { position: sticky; top: 12px; z-index: 8; display: flex; gap: 6px; padding: 6px; border-radius: 18px; overflow-x: auto; }
.vpn-admin-nav a { min-height: 40px; display: inline-flex; align-items: center; padding: 0 14px; border-radius: 13px; white-space: nowrap; }
.vpn-admin-nav a[aria-current="page"] { color: white; background: var(--vx-blue); }
.vpn-admin-metrics { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; }
.vpn-admin-metric, .vpn-admin-panel { background: var(--vpn-admin-surface); border: 1px solid var(--vx-border); border-radius: 20px; }
@media (max-width: 720px) { .vpn-admin-nav { margin-inline: -4px; } .vpn-admin-panel { border-radius: 16px; } }
```

Scope every new rule under `.vpn-admin-shell` so Domain Drop Catcher screens retain their current design.

- [ ] **Step 4: Make secret links and dangerous actions explicit**

Keep the masked one-line preview. Rename controls to `Показать полную ссылку`, `Скрыть ссылку`, and `Скопировать ссылку`. Full text uses a wrapping read-only textarea.

Replace node deletion’s one-step `window.confirm` text with the exact consequence and confirmation label in the existing flow:

```text
Удалить VPN‑ноду «{name}»?
Она перестанет принимать новые профили. Активные профили будут обработаны по текущим правилам безопасного удаления.
```

Primary destructive action: `Да, удалить ноду`; secondary: `Отмена`. Do not change the decommission API sequence.

- [ ] **Step 5: Run admin tests, build, and browser QA**

Run:

```powershell
node --experimental-strip-types --test test/vpnCustomerWorkspace.test.mjs test/vpnEndpointCapacity.test.mjs test/vpnReleaseReadiness.test.mjs
npm run build
node test/browser/admin-profile-ui.qa.mjs
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/styles.css frontend/src/VpnCustomerWorkspacePanel.tsx frontend/src/VpnEndpointCapacityPanel.tsx frontend/src/VpnReleaseReadinessPanel.tsx frontend/test
git commit -m "style: polish Veltrix VPN admin experience"
```

---

### Task 10: Cross-surface accessibility and copy audit

**Files:**
- Modify: affected files under `frontend/src/brand`, `frontend/src/vpn-portal`, `frontend/src/vpn-site`, and VPN admin components only when the audit reports a real issue
- Modify: `frontend/test/browser/portal-ui.qa.mjs`
- Modify: `frontend/test/browser/admin-profile-ui.qa.mjs`

- [ ] **Step 1: Run the Russian UI text lint as a review tool**

Run:

```powershell
python C:\Users\user\.codex\skills\sasha\scripts\ui_text_lint.py frontend/src/vpn-portal --format json
python C:\Users\user\.codex\skills\sasha\scripts\ui_text_lint.py frontend/src/vpn-site --format json
```

Review every warning. Preserve identifiers, API strings, variables, and legal meaning. Fix only user-visible copy that violates the approved vocabulary.

- [ ] **Step 2: Add keyboard, zoom, and reduced-motion checks**

For each customer viewport, tab through every interactive element and assert `document.activeElement` is visible. Set viewport to 390×844 and CSS zoom/text scaling equivalent to 200%; assert no horizontal overflow. Emulate reduced motion and assert the lens has no continuous animation.

- [ ] **Step 3: Verify accessible names and state semantics**

Run a DOM assertion that every visible button/link has non-empty accessible text, status messages use `role="status"`, errors use `role="alert"`, and decorative SVGs are hidden. Ensure success/warning/error labels contain text in addition to color.

- [ ] **Step 4: Run targeted checks and commit any fixes**

```powershell
npm run build
node test/browser/portal-ui.qa.mjs
node test/browser/admin-profile-ui.qa.mjs
git add frontend/src frontend/test
git commit -m "fix: harden Veltrix accessibility and UI copy"
```

If the audit requires no source change, skip the commit and record the passing commands in the final verification note.

---

### Task 11: Full regression and Impeccable finish review

**Files:**
- Create: `DESIGN.md`
- Modify: `.impeccable/build/state.json`
- Create or modify: `.impeccable/review/**`
- Create: provenance sidecars beside every shipping raster

- [ ] **Step 1: Run all frontend tests and production build**

Run from `frontend`:

```powershell
npm test
npm run build
```

Expected: all tests PASS; TypeScript and Vite build PASS.

- [ ] **Step 2: Run both browser QA suites**

```powershell
node test/browser/portal-ui.qa.mjs
node test/browser/admin-profile-ui.qa.mjs
```

Expected: PASS with screenshots for light, dark, narrow phone, standard phone, desktop cabinet, public site, and VPN admin.

- [ ] **Step 3: Advance every remaining Impeccable phase in order**

For each phase, record the current render, inspect named diffs/crops, fix the measured issue, and run `build-phase advance`. Required order: `plates`, `hero`, `sections`, `motion`, `responsive`, `review`. Never force a failed gate.

Run status checks with:

```powershell
& 'C:\Users\user\.agents\skills\impeccable\scripts\impeccable.cmd' build-phase status
```

Expected final state: all phases closed and finish verdict recorded.

- [ ] **Step 4: Write `DESIGN.md` from the shipped system**

Document the actual final tokens, logo variants, typography from `font-match`, material rules, component classes, responsive breakpoints, motion, accessibility, approved comp, and raster provenance. Do not copy the development-only direction contract into browser-delivered source.

- [ ] **Step 5: Run repository checks and inspect the diff**

```powershell
git diff --check
git status --short
git diff --stat origin/main...HEAD
```

Expected: no whitespace errors, no accidental secret files, no modification of the five pre-existing `.tmp_*` migration scripts.

- [ ] **Step 6: Commit finish artifacts**

```powershell
git add DESIGN.md .impeccable frontend
git commit -m "docs: finalize Veltrix design system"
```

---

### Task 12: Review, integration, and production rollout

**Files:**
- No source file is intentionally changed during this task; fixes found by review receive their own focused commit.

- [ ] **Step 1: Request correctness and design review**

Review the complete branch against:

- `docs/superpowers/specs/2026-10-03-veltrix-liquid-glass-redesign-design.md`;
- the approved comp;
- portal/public/admin test evidence;
- the no-payment and no-fake-claim constraints.

- [ ] **Step 2: Fix each confirmed finding with a failing test first**

For every accepted finding: add the smallest failing test, run it to see the failure, make the minimal fix, run the targeted test, then run `npm test` and `npm run build` before committing.

- [ ] **Step 3: Create or update the pull request**

Push `codex/veltrix-design-refresh`, open a PR to `main`, attach it to the task, and wait for CI. The PR body includes the three affected surfaces, test commands, screenshots, no-payment boundary, and rollback statement.

- [ ] **Step 4: Merge only after CI and user-facing preview pass**

Expected: required checks green, approved first viewport visually matches, and existing VPN operations remain available.

- [ ] **Step 5: Deploy the existing production procedure without VPN configuration changes**

Deploy only the built frontend assets. Preserve the previous asset directory/release for rollback. Do not restart or reconfigure VPN nodes as part of this UI deployment.

- [ ] **Step 6: Run post-deploy smoke checks**

Verify:

```text
/vpn/ loads and opens the official bot
/cabinet/ authenticates in browser and Telegram Mini App
active profile reveals and copies its full URI
trial preparation transitions to ready
logout hides private data and requires a new session
VPN admin overview, customer workspace, nodes, plans, and events load
no purchase action is enabled
```

If any primary path fails, restore the previous frontend asset release; no database or VPN rollback is required.
