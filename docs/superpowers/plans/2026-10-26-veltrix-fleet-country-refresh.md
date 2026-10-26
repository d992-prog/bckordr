# VPN Fleet Country Refresh Implementation Plan

> **For agentic workers:** Execute this bounded repair inline with superpowers:executing-plans; request independent code review before release.

**Goal:** Keep every healthy published country available when the customer refreshes the country selector, without accepting stale or unhealthy nodes.

**Architecture:** Reuse the existing serialized, pinned one-endpoint probe. Add a fleet pass with a fixed start-time watermark so every eligible endpoint is attempted once per scheduled cycle, rather than one endpoint per two-minute cycle. Preserve advisory/row locks, worker exclusions, timeout/cancellation behavior, health freshness thresholds and staged-node acceptance.

**Tech Stack:** Python, SQLAlchemy, asyncio, pytest, the existing Linux/PostgreSQL release gate.

## Scope and evidence

- Live configuration: one probe per 120 seconds, with a 300-second health freshness limit. Four healthy published endpoints therefore take over 480 seconds to revisit.
- A read-only snapshot reproduced a healthy published Netherlands endpoint with no errors but a 373-second-old check and `selected=false`; other country checks had different ages. This explains countries rotating out of the selector.
- Modify `backend/app/services/vpn_fleet_health.py` and the default health callable in `backend/app/services/control_runtime.py`; extend `backend/tests/test_vpn_fleet_health.py` and `backend/tests/test_vpn_fleet_health_postgres.py`. Existing runtime fakes and callers retain their two positional arguments and bool result.
- Do not change country publication, customer keys, application credentials, frontend, firewall, dependencies, schema or the 300-second freshness guard.

## Task 1: Regression before implementation

- [x] Add a parameterized fleet-pass regression for 4 and 12 eligible endpoints, using the existing SQLite fixture and fake remote transport. Seed published ready endpoints with capacity 20, and verify one call updates every endpoint, terminates without rechecking the same endpoint and keeps all eligible through `select_public_vpn_endpoint` for repeated refreshes before the next cycle.
- [x] Add cases for a failed transport followed by a healthy node, cancellation rollback and a fixed watermark excluding endpoints already checked during the current pass.
- [x] Run the new tests before implementation:

  ```powershell
  $env:PYTHONPATH='backend'
  & 'D:\паразитное seo\backorder\project\.worktrees\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_vpn_fleet_health.py -k fleet_pass
  ```

  Expect failure because the existing implementation has no complete fleet pass.

## Task 2: Smallest shared fix

- [x] Add optional `checked_before: datetime | None = None` to `probe_next_vpn_endpoint`. When supplied, candidates must have null health timestamps or timestamps strictly before the watermark. Keep its default behavior and all locking/rechecks unchanged. Its internal implementation distinguishes a candidate lost while locking (`None`) from exhaustion/lease contention (`False`) and a completed probe (`True`); the public compatibility entry retains its boolean contract.
- [x] Add the scheduled fleet operation, forwarding existing dependency-injection parameters:

  ```python
  checked_before = _as_utc(now())
  if checked_before is None:
      raise TypeError("vpn_fleet_health_time_invalid")
  probed = False
  while True:
      result = await _probe_next_vpn_endpoint(
          session_factory, known_hosts_path,
          snapshot_loader=snapshot_loader, transport=transport, now=now,
          checked_before=checked_before,
      )
      if result is False:
          return probed
      if result is True:
          probed = True
  ```

- [x] Use the fleet operation as the existing runtime's default health callable. Keep the existing single in-flight task, 90-second batch timeout, cancellation cleanup and inter-cycle cooldown.
- [x] Run new regressions, full fleet/runtime/onboarding/policy/customer-device suites and PostgreSQL lease tests; run Ruff on changed files and `git diff --check`. Locally unavailable PostgreSQL cases must run in CI.

## Task 3: Review, release and persistent verification

- [x] Obtain independent review of the fixed-watermark traversal, cancellation/leases and unchanged fail-closed selection.
- [ ] Publish only the bounded fix to a `codex/` branch based on the current production branch, create/attach a PR, and wait for the exact source SHA to pass the full Linux/PostgreSQL release gate.
- [ ] Back up previous Python sources privately on the controller, verify the reviewed merge tree and fast-forward. Preserve .env, known-hosts, frontend and the existing modified build cache. Restart only the controller service; no VPN/Xray restart or database migration.
- [ ] Observe the real country selector service and health ages over more than 300 seconds and at least two scheduled passes. All four healthy countries must remain available; a genuinely failed endpoint must still be rejected. Compare pre-existing key identities and protected-file hashes before/after, and confirm public health/cabinet HTTP 200 and anonymous admin HTTP 401.
- [ ] Record final revision, CI evidence, backups and limitations in `docs/current-state.md` and this plan.

## Verification before release

- Original regressions: nine failed before implementation; all passed after the first bounded traversal fix.
- Independent review found that a concurrent candidate update could end the pass early. Three new committed-concurrency cases (health timestamp, worker disabled, queued maintenance) failed before the retry distinction and pass afterward.
- The final focused fleet/runtime/onboarding/policy/device-location/device-API suite passed 328 tests with six locally unavailable PostgreSQL skips. The new real PostgreSQL worker-row-lock race runs in CI.
- Fleet service/tests and PostgreSQL tests pass Ruff. The control runtime has five identical pre-existing diagnostics; baseline/current rule, line, column and message sets match exactly. Diff check passes. No unrelated lint cleanup was applied.
- Final independent review found no remaining issue, including a no-database check that the PostgreSQL advisory-lock TextClause is handled by the new test hook.
- The existing 90-second whole-pass timeout remains a bound: a sufficiently slow/large fleet can defer unfinished endpoints. No stale-health or failure guards were relaxed; the four live nodes require only a few seconds for normal probes.
