# VPN node preflight recovery

> **For agentic workers:** Use superpowers:executing-plans for this bounded regression fix.

**Goal:** Restore the approved onboarding sequence for an SSH-configured node without a complete trusted host pin.

**Architecture:** Derive `needs_preflight` until both trusted host-key fields exist. The existing read-only preflight returns the observed fingerprint and transient `needs_trust` response; only explicit trust persists the pin. Keep existing job/endpoint precedence and all trust, origin and mutation guards.

**Tech Stack:** Python, FastAPI, SQLAlchemy, pytest, existing React onboarding client.

## Scope

Modify `backend/app/services/vpn_node_onboarding.py`, the preflight response in `backend/app/api/routes/control.py`, their service/API tests, and this execution record. No schema, dependency, client, firewall, credential or VPN-runtime changes are needed for the fix. The existing SSH fingerprint verification remains a separate prerequisite for installing original-coffee-octopus.

## Steps

- [x] Add a regression for missing and partially populated pins. A node with no key, no fingerprint, or neither must return `needs_preflight`, not an unconfirmable trust prompt. Update the former test that expected the broken prompt.
- [x] Add an API regression exercising initial GET, explicit preflight, reload GET and repeated preflight. Assert a usable fingerprint is returned by preflight, no pin is saved by discovery, and starting without trust remains blocked.
- [x] Run the new tests before implementation and confirm assertion failures against the current behavior:

  ```powershell
  $env:PYTHONPATH = 'backend'
  & 'D:\паразитное seo\backorder\project\.worktrees\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_vpn_node_onboarding.py backend/tests/test_vpn_node_onboarding_api.py -k 'view_derives_preflight or incomplete_pin or untrusted_node_can_preflight'
  ```

- [x] Replace the current untrusted branch in `get_vpn_node_onboarding_view` with:

  ```python
  elif (
      not worker.ssh_access_configured
      or not worker.ssh_host_key
      or not worker.ssh_host_key_fingerprint
  ):
      state = "needs_preflight"
  else:
      state = "ready_to_start"
  ```

- [x] Cover the partial-pin API case where a fingerprint exists but the host-key line is missing. Verify its failing regression, then make explicit preflight return `needs_trust` whenever the saved host key is absent or the observed fingerprint differs:

  ```python
  needs_trust = (
      not worker.ssh_host_key
      or worker.ssh_host_key_fingerprint != probe.fingerprint
  )
  ```

- [x] Run the focused regressions, complete onboarding/trust/transport/schema tests and Ruff on changed Python files. Review callers and preserve queued/running/failed/staged/ready behavior.
- [ ] Review the bounded diff. Release the fix through the existing repository/controller workflow; preserve unrelated local changes and the deployed frontend. No database migration is required. Keep a recoverable copy of the prior source and restart only the controller if deployed.
- [ ] Verify the live read-only onboarding view for original-coffee-octopus and service health. Do not persist trust, install VPN, open ports or publish this node without the required independent SSH identity verification.

## Acceptance

Fresh or reloaded untrusted nodes always offer a working preflight action. Explicit preflight can offer confirmation of a concrete fingerprint without saving trust. Starting before trust remains forbidden. Existing ready nodes and user credentials remain unchanged.

## Verification record

- Original bug: five regression assertions failed before changing production code.
- Partial-pin API finding: one new parameterized regression failed before the route fix; the other two cases already passed.
- Expanded onboarding/API/schema/trust/transport suite: 239 passed, 2 PostgreSQL-dependent skips on this Windows run.
- Existing frontend onboarding suite: 16 passed.
- Ruff passes for the service and test files. The large existing control route has 260 baseline diagnostics; baseline/current diagnostic sets are identical and none occur in the changed region. No unrelated route-wide formatting was applied.
- Independent review initially found the partial-pin loop; after its regression and fix, re-review found no remaining issue and confirmed seven focused API cases pass.
- The generic local Python runtime has an unrelated asyncssh/cryptography mismatch. Tests used an already installed, compatible worktree virtualenv; no package/runtime or production dependency was changed.
