# Veltrix WAL-safe fleet health design

## Goal

Allow the existing read-only VPN fleet-health probe to inspect a live 3x-UI
SQLite database in WAL mode without copying customer data off the VPN node,
changing VPN configuration, or weakening the current strict SSH and endpoint
checks.

## Chosen approach

Replace the current main-file byte copy with SQLite's standard online backup
API. The node runner opens the configured database through a read-only URI,
sets `query_only`, and copies one consistent SQLite snapshot into an in-memory
connection. The existing bounded `SELECT id FROM inbounds` then runs against
that in-memory snapshot.

This is the smallest safe option because Python already ships the SQLite backup
API. It needs no package, executable, persistent temporary file, new service, or
new network path. The live database remains logically read-only; SQLite may use
the existing WAL/SHM files for normal reader coordination.

## Safety contract

- Keep the existing absolute-path, ancestor, regular-file, ownership and size
  checks for the main database.
- Accept only regular, non-symlink `-wal` and `-shm` sidecars owned by the same
  UID as the main database. Reject an unexpected rollback journal while WAL is
  present and reject every other malformed sidecar state.
- Open the source with `mode=ro`, `uri=True`, a two-second busy timeout and
  `PRAGMA query_only=ON`.
- Use `Connection.backup()` to a private in-memory connection. Abort when the
  existing monotonic deadline expires.
- Query only inbound IDs from the completed in-memory snapshot. Keep the current
  10,000-row bound, uniqueness checks and positive-integer validation.
- Hold the source connection open for the complete backup. Re-stat the main
  database, its parent and current sidecars afterward. The main database must
  keep the same device/inode, type, owner and mode; size and timestamps may
  legitimately change during a WAL checkpoint. Sidecars may rotate, but every
  observed version must still be a regular non-symlink with the required owner
  and mode. A main-file replacement, unsafe metadata or invalid sidecar state
  fails closed with the existing `vpn_xui_inventory_unavailable` code.
- Never expose database rows, paths, connection URLs or SQLite exception text in
  controller logs, node receipts or Telegram alerts.

## Alternatives rejected

1. Trust only the 3x-UI HTTP response. This removes the local completeness check
   and weakens the release gate.
2. Run the `sqlite3` CLI and create a temporary backup file. This adds a package,
   persistent cleanup and another private file without improving correctness.
3. Delete WAL sidecars or switch the live database to rollback mode. This mutates
   production 3x-UI state and risks downtime or corruption.

## Code and data flow

Only `backend/app/services/vpn_xui_node_observation.py` changes. Its private
snapshot helper returns an in-memory SQLite connection instead of database
bytes; `_local_inbound_ids()` consumes it and closes it in its existing cleanup
path. Callers, node-health request/receipt formats, endpoint state, controller
scheduling and database schemas remain unchanged.

## Tests

Add focused tests to `backend/tests/test_vpn_xui_node_observation.py`:

- a real WAL database with an uncheckpointed inbound is read successfully while
  the writer remains open;
- the same test proves the WAL row, rather than only the main-file rows, is seen;
- symlinked or wrongly owned sidecars fail closed;
- a deadline exceeded during backup fails closed;
- existing rollback-journal and malformed-database tests remain green.

The focused observation, health-entrypoint, bundle and fleet-health suites run
before the full backend checks. Ruff and `git diff --check` remain mandatory.

## Rollout

Merge through normal CI, deploy the exact merge commit with
`VPN_FLEET_HEALTH_ENABLED=false`, rebuild and atomically deploy the common node
runner, then run one manual strict health probe. Enable fleet health only if the
endpoint becomes freshly healthy and the control/public health endpoints remain
green. Payments, public trial and ready notifications stay disabled.

Rollback disables fleet health first and atomically restores the runner's
preserved previous bundle. It never changes the 3x-UI database or VPN clients.
