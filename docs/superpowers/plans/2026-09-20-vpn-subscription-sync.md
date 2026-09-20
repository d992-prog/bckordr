# VPN Subscription Synchronization Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development for the isolated provisioning task and reviews. Execute the integration steps in this session with TDD.

**Goal:** Apply subscription edits to existing node clients and reversibly suspend expired/disabled access without changing the Happ link or resetting usage.

**Architecture:** Stage key transitions atomically in a focused subscription service. The existing serialized lifecycle applies current policy. Add pending_suspend/suspended states and a non-destructive remote disable operation; permanent revoke stays permanent.

**Tech Stack:** Python, FastAPI, SQLAlchemy, SQLite/PostgreSQL, 3x-UI via SSH, React/TypeScript.

## Workspace and verification

Work in `.worktrees/vpn-subscription-sync`, branch `codex/vpn-subscription-sync`.
Use global Python from the worktree backend cwd so this project's `app` wins over
unrelated editable installs. Every pytest run uses a fresh workspace-local
`--basetemp .pytest_cache/<unique-run-id>` (default Windows temp is inaccessible).

## Task 1: Remote policy upsert and suspension

Files: `backend/app/services/vpn_provisioning.py`,
`backend/tests/test_vpn_provisioning.py`, new
`backend/tests/test_vpn_remote_policy.py` if needed to isolate SQL fixtures.

- [ ] Write remote-script regression tests by executing the generated Python
  function definitions against in-memory legacy and normalized 3x-UI schemas.
  Assert actual row contents, not source substrings:

```python
assert stored_client_uuid == original_uuid
assert stored_expiry == new_expiry
assert stored_usage == (123, 456)
assert unrelated_client == original_unrelated_client
```

- [ ] Run tests and observe failures for reset counters and missing suspension.
- [ ] Preserve existing traffic counters during upsert; initialize only inserts.
  Preserve existing non-policy fields (flow/subId/etc.) when editing a client.
- [ ] Add `build_vpn_client_suspend_command(worker, access_key)` and
  `async suspend_vpn_access_key(db, access_key, *, worker=None)`.
  Disable matching inbound JSON and normalized client/traffic rows without deleting
  them. Use output marker `DROPCATCH_VPN_CLIENT_SUSPEND_STATUS=suspended` only after
  confirmed restart. Treat a genuinely absent client as already suspended.
- [ ] Test SSH failure/invalid confirmation -> pending_suspend, successful disable
  -> suspended; preserve UUID/URI and revoke metadata.
- [ ] Run focused tests and Ruff. Commit only task-owned files.

## Task 2: Atomic subscription policy staging

Files: new `backend/app/services/vpn_subscription_sync.py`,
`backend/app/services/vpn_policy.py`, `backend/app/api/routes/control.py`,
`backend/app/schemas/control.py`, new `backend/tests/test_vpn_subscription_sync.py`.

- [ ] Write API tests using real ASGI/SQLite, patching only SSH or lifecycle remote
  boundary. Renewal must stage active keys with new expiry and unchanged identity:

```python
assert renewed.status == 'pending_sync'
assert renewed.expires_at == subscription.expires_at
assert (renewed.external_uuid, renewed.config_uri, renewed.worker_id) == identity
assert manually_revoked.status == 'revoked'
```

- [ ] Cover notes-only no-op, limit updates, unlimited expiry, shortening, future
  starts, inactive customers, pending manual revocation, and device reduction below
  retained-key count. Run to observe the missing behavior.
- [ ] Implement focused `stage_subscription_update(db, subscription, updates)`.
  Validate proposed values before mutations; use retained slot states including
  suspended/pending_suspend. Reject invalid reduction with HTTP 409 and invalid
  null/time ranges with HTTP 422. Apply policy changes and key staging in one
  transaction, using subscription lock and the shared mutation lock.
- [ ] Define a reusable `vpn_mutation_lock` in a focused module or policy service.
  Use it before loading mutable ORM rows in subscription/key/customer writes and
  node decommission; lifecycle shares it. Keep DB row-lock ordering consistent.
- [ ] Extend archive/decommission handling for suspended/pending_suspend keys;
  their permanent revoke semantics must not change.
- [ ] Run API/policy/archive/decommission tests and commit verified integration.

## Task 3: Lifecycle reconciliation

Files: `backend/app/services/vpn_lifecycle.py`,
`backend/app/services/vpn_customer_lifecycle.py`,
`backend/app/services/worker_decommission.py`,
`backend/tests/test_vpn_lifecycle.py`,
`backend/tests/test_vpn_customer_archive.py`,
`backend/tests/test_worker_decommission.py`.

- [ ] Add failing tests for automatic expiry -> suspended, explicit disable ->
  suspended, cancellation -> revoked, resume at future start, renewal while
  pending_suspend, and manual revoke never restored.
- [ ] Add suspension counters to lifecycle results. Select due suspensions and
  permanent revocations before syncs, with bounded/fair pending retries.
- [ ] Re-evaluate current customer/subscription state before each operation.
  Use subscription expiry as policy, not a stale copied key expiry. Suspended
  eligible keys can resume; no revoked/pending_revoke key can auto-resume.
- [ ] Lock conflicting admin transitions against lifecycle. Preserve existing
  per-key commits, cancellation behavior, sanitization and safe-node checks.
- [ ] Test timeout/unavailable node leaves correct pending state, recovery applies
  the latest policy, and archive/manual revoke cannot race a stale resume.
- [ ] Run all backend tests plus Ruff and review state-machine invariants.

## Task 4: Clear UI state and save feedback

Files: `frontend/src/vpnCustomerWorkspace.ts`,
`frontend/src/VpnCustomerWorkspacePanel.tsx`, `frontend/src/api.ts`,
`frontend/test/vpnCustomerWorkspace.test.mjs`.

- [ ] Write failing label tests:

```javascript
assert.equal(accessKeyStatusLabel('pending_suspend'), 'ожидает приостановки');
assert.equal(accessKeyStatusLabel('suspended'), 'приостановлен');
assert.equal(accessKeyStatusLabel('pending_sync'), 'ожидает синхронизации');
```

- [ ] Add suspension response counters and Russian state labels. Make manual
  permanent revoke available for suspended/pending_suspend keys, without implying
  they are active. Keep full URI copy available with explicit suspended explanation.
- [ ] On edit/renew/save reload authoritative key data and optionally request
  maintenance. A failed maintenance request after successful save must say the
  subscription was saved and sync is pending, not claim save failed.
- [ ] Replace misleading suspend/revoke copy with reversible-pause explanations.
  No payment UI, layout redesign or automatic key rotation.
- [ ] Run frontend tests and production build.

## Task 5: Review, documentation and production verification

- [ ] Spec review against approved design; fix all material discrepancies.
- [ ] Code-quality/security review after spec review; fix and re-run verification.
- [ ] Update `docs/current-state.md` and `docs/vpn-service.md` with state semantics,
  historical-key compatibility, preserved traffic and retry behavior.
- [ ] Full backend suite, Ruff, frontend tests/build, `git diff --check`.
- [ ] Merge verified branch into main using existing user authorization, push and
  deploy with a backup/checkpoint of current production revision. Preserve unrelated
  server changes. Check local/public health.
- [ ] Use a new temporary test customer/subscription/key, not test1, to verify
  renewal, suspension and resumption with unchanged UUID/URI and preserved usage.
  Verify node config plus actual VPN traffic. Permanently revoke temporary access
  and confirm remote removal. Report actual verified result and any limitations.
