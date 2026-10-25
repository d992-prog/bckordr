# Veltrix Admin Access Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make device-limit errors and SSH trust prerequisites actionable without weakening access or host-key checks.

**Architecture:** Keep backend enforcement and node protocols unchanged in this first independently releasable slice. Add a frontend slot-count presentation matching backend policy, bounded Russian errors next to the issuance form, and explicit SSH verification instructions. Logical devices, country selection, publication, invitations, telemetry and broadcasts remain subsequent slices of the approved product specification, not claims of this slice.

**Tech Stack:** Existing React 18, TypeScript, CSS, Node test runner, pytest. No new packages.

**Spec:** `docs/superpowers/specs/2026-10-25-veltrix-product-operations-design.md`, sections 1–2.

## Files and responsibilities

- `frontend/src/vpnCustomerWorkspace.ts`: pure slot-count and bounded issuance-error presentation
- `frontend/src/VpnCustomerWorkspacePanel.tsx`: usage, local feedback and existing subscription editor recovery action
- `frontend/src/vpnNodeOnboarding.ts`: public fingerprint command and concise state copy
- `frontend/src/VpnNodeOnboardingPanel.tsx`: visible trust instructions and disabled-state explanation
- `frontend/src/styles.css`: only narrowly scoped wrapping, spacing and existing-token state styling if required
- `frontend/test/vpnCustomerWorkspace.test.mjs`: policy presentation and source integration checks
- `frontend/test/vpnNodeOnboarding.test.mjs`: trust guidance and existing fingerprint guard regression

## Baseline

Backend focused baseline: 133 passed, 2 PostgreSQL tests skipped. Frontend focused baseline: 41 passed, 1 pre-existing failure in the unrelated node-deletion source regex on Windows line endings. Preserve that behavior; if normalization is needed, fix the test's CRLF sensitivity without weakening its assertions.

### Task 1: Device-limit feedback and SSH guidance — completed

Verified commits: `a9a5bc3`, `204bb97`. Spec review passed; quality review passed after adding durable controlled browser regressions. Root reran 180 frontend tests; detector returned no findings. Backend baseline: 133 passed, 2 PostgreSQL-only skips. No production rollout yet. Existing unrelated full-browser audit still reports node-row button overlap at 390px with 200% text; the focused creation gate passes.

- [x] Add failing tests for slot counts, error safety and visible recovery controls

Use structural inputs so fixtures can contain only relevant fields:

```js
assert.equal(countReservedVpnDeviceSlots([
  { subscription_id: 5, status: "active" },
  { subscription_id: 5, status: "failed" },
  { subscription_id: 5, status: "suspended" },
  { subscription_id: 5, status: "revoked" },
  { subscription_id: 6, status: "active" },
], 5), 3);
assert.equal(countReservedVpnDeviceSlots([], 5), 0);
assert.match(vpnProfileIssueError("VPN device limit reached"), /лимит устройств/i);
assert.doesNotMatch(vpnProfileIssueError("private bearer material"), /private bearer/);
```

Also cover all reserving statuses (`pending_sync`, `syncing`, `active`, `pending_revoke`, `pending_suspend`, `suspended`, `failed`), unknown statuses, exact limit and missing keys. Source integration checks must prove the actual form uses the helpers and local `role="alert"`, shows usage and links to the existing editor. Preserve backend authority when frontend data is stale.

- [x] Add failing trust-guidance tests

```js
assert.equal(SSH_FINGERPRINT_COMMAND,
  "ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub -E sha256");
assert.equal(isVpnNodeFingerprintConfirmed(null, "SHA256:sample"), false);
assert.equal(isVpnNodeFingerprintConfirmed("SHA256:old", "SHA256:new"), false);
```

Integration checks must locate instructions for the hosting console, command copy/manual selection, matching `SHA256` value, checkbox, and the reason the primary button is unavailable. Keep existing tests for strict API bodies, stale-response guards and fingerprint matching.

- [x] Run focused tests and observe the new assertions fail

From `frontend`:

```powershell
node --experimental-strip-types --test test/vpnCustomerWorkspace.test.mjs test/vpnNodeOnboarding.test.mjs test/vpnCustomerApi.test.mjs
```

Expected: new missing-helper or missing-UI assertions fail before implementation, independently of the recorded baseline failure.

- [x] Implement the smallest shared presentation helpers

```ts
const RESERVED_DEVICE_STATUSES = new Set([
  "pending_sync", "syncing", "active", "pending_revoke",
  "pending_suspend", "suspended", "failed",
]);

export function countReservedVpnDeviceSlots(
  keys: ReadonlyArray<{ subscription_id: number; status: string }>,
  subscriptionId: number,
) {
  return keys.filter((key) => key.subscription_id === subscriptionId
    && RESERVED_DEVICE_STATUSES.has(key.status)).length;
}

export function vpnProfileIssueError(error: string | null | undefined) {
  if (error === "VPN device limit reached") {
    return "Достигнут лимит устройств. Измените лимит подписки или отзовите ненужный профиль";
  }
  return "Не удалось выдать профиль. Обновите данные и повторите попытку";
}
```

Known subscription/customer policy errors may receive equally bounded existing-language messages. Unknown errors must not echo raw network responses or credentials.

- [x] Connect the helpers to the real issuance flow

Show `used` and `max_devices` beside the subscription and inside the creation form. Keep the form open when issuance fails; show the local error and an explicit action opening `beginSubscriptionEdit(subscription)`. Disable submit while full or in flight, not the action that opens explanatory guidance. Existing backend device limits remain unchanged. Guard duplicate in-flight submission. A successful create followed by failed reload must not be reported as a failed creation or invite another create; close the form and give refresh guidance instead. Reserved failed/suspended records must not be deleted or excluded just to free a slot.

- [x] Implement the SSH prerequisite explanation

Export the exact public command above. Show a short ordered console verification guide, a selectable command and a copy control using the existing synchronous WebView copy path. Set the checkbox label to «Отпечатки совпадают». Give the primary action an `aria-describedby` hint explaining that comparison and marking the checkbox are required. Changing worker/fingerprint must clear confirmation as in existing guards. No automatic trust, server-key mutation, shell execution, or new public endpoint is introduced.

- [x] Run focused tests, all frontend tests and the frontend build

```powershell
npm test
npm run build
```

Expected: no new failures; diagnose any failures against the recorded baseline. Install only existing lockfile dependencies with `npm ci` if the clean worktree lacks them. Run the Impeccable detector once on changed UI targets and inspect desktop/mobile together once, followed by at most one correction round.

- [x] Review spec compliance, then code quality; commit only the reviewed slice

Review for wrong slot accounting, stale form state, duplicate requests, post-save reload errors, hidden guidance, missing keyboard labels, unknown-error leakage, and weakened SSH trust. Backend smoke command from `backend`:

```powershell
python -m pytest tests/test_vpn_policy.py tests/test_vpn_node_onboarding.py tests/test_vpn_node_onboarding_api.py tests/test_vpn_control_api.py -q
```

Expected: same 133 passing baseline tests, with PostgreSQL-only skips reported honestly. Do not deploy or claim the remaining product slices are implemented as part of this commit.
