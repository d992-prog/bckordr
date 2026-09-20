# Safe VPN Node Decommission Design

## Goal

Allow an administrator to remove an obsolete VPN node from the active control panel without deleting operational history or leaving its credentials able to access the control server.

The first production use will be decommissioning `automatic-ivory`, which is currently both a drop worker and a VPN node but no longer represents a paid server.

## Chosen Approach

Worker deletion becomes a non-destructive soft archive rather than a physical row deletion. The existing `DELETE /control/workers/{worker_id}` endpoint remains the public operation so the worker screen and the VPN screen use one safety path. Restoring an archived row is not part of this feature because its credentials are intentionally erased.

An archived worker remains in PostgreSQL for foreign-key integrity and audit history, but it is excluded from active worker and VPN-node lists. The operation does not connect to, uninstall, or destroy the remote VPS. Hosting cancellation and remote server destruction remain provider-side actions.

## Data Model

`worker_nodes` gains a nullable `archived_at` timestamp.

When a worker is archived, the transaction sets:

- `archived_at` to the current UTC time;
- `is_enabled` to `false`;
- `status` to `archived`;
- `control_token` to `NULL`;
- `vpn_enabled` to `false`;
- `vpn_role` to `none`;
- `vpn_runtime_status` to `decommissioned`;
- stored SSH and 3x-UI authentication secrets to `NULL`.

The worker name, IP addresses, non-secret VPN endpoint metadata, maintenance records, task history, and node events remain available in the database for audit. Archived worker names remain reserved by the existing uniqueness constraint.

## Attached VPN Keys

Keys attached to the worker with status `pending_sync`, `syncing`, `active`, or `pending_revoke` are locally retired in the same transaction:

- status becomes `revoked`;
- `revoked_at` is populated;
- `config_uri` is cleared so the panel can no longer copy or display the credential;
- `last_error` records that the node was decommissioned and remote revoke was not confirmed;
- `worker_id` remains attached to the archived worker for audit.

This is deliberately a local retirement operation. It does not claim that a still-running remote 3x-UI installation removed the client. The confirmation dialog must state that the administrator must separately destroy or secure the VPS at the hosting provider. Telegram and API delivery already return only active keys, so retired keys stop being delivered immediately.

Subscriptions and customers are not deleted or expired. A valid subscription can receive a new key on another ready node after decommissioning.

## Safety Gates

The API obtains a row lock for the worker and rejects the operation with HTTP `409` when either condition is true:

- the worker has a queued or running task belonging to a planned or running domain attack;
- the worker has any queued or running maintenance job.

No partial changes are committed when a safety gate fails. A missing or already archived worker returns `404`.

Worker update, setup, and single-node maintenance endpoints reject archived workers. Clearing `control_token` prevents an old worker process from authenticating after archive. Setting `is_enabled=false` removes the IP from the generated worker-runtime allowlist during the existing post-delete synchronization step.

## API and Service Boundary

The archival transaction lives in a focused backend service instead of expanding the already large control route module. The service owns row locking, safety checks, key retirement, credential clearing, and creation of a `node_decommissioned` VPN event.

The route maps service errors to `404` or `409`, writes the existing admin audit log with action `worker_decommission`, commits once, synchronizes the worker allowlist, and returns a success message.

Active queries are updated to exclude `archived_at IS NOT NULL`, including:

- the worker list;
- VPN node eligibility;
- worker selection for new VPN keys.

Runtime scheduling already requires `is_enabled=true`, and VPN overview already requires `vpn_enabled=true`; the archive state therefore also removes the node from allocation and aggregate counts.

## Admin UI

The VPN-node table gains a red `Удалить ноду` button beside the existing maintenance actions. The worker-card delete button uses the same dedicated handler.

Before sending the request, the browser shows a confirmation that names the node and explains that:

- it is removed from both drop-worker and VPN use;
- attached active keys are retired locally;
- history remains;
- the remote VPS is not modified.

On success the application reloads all datasets and shows `Нода удалена из активной системы`. API errors, including active attack or maintenance conflicts, are displayed through the existing error toast.

## Audit and Observability

The transaction adds a `VpnNodeEvent` with type `node_decommissioned`, including counts of retired keys and the previous worker/VPN status. The normal admin audit log records the actor and worker ID.

Archived records do not appear in active panels. Existing task, key, and event rows keep their worker reference, allowing database-level incident review without exposing archived credentials.

## Testing

Backend tests are written first and must demonstrate the missing behavior before implementation. Coverage includes:

- a worker is archived instead of physically deleted;
- credentials and control token are cleared;
- attached usable keys become revoked, their URI is cleared, and history remains linked;
- subscriptions and customers remain unchanged;
- active worker and VPN-node queries exclude the archive;
- active attack and active maintenance each produce `409` with no partial mutation;
- allowlist synchronization still runs after a successful archive and sees no active worker;
- archived workers cannot be updated or receive new maintenance jobs.

The frontend currently has no component-test runner. Its change is limited to the typed API already present, a confirmation handler, and one additional button; it is verified through the production TypeScript/Vite build and a manual browser smoke test.

The full backend suite, backend Ruff, worker Ruff, frontend production build, and post-deployment production health checks remain required before completion.

## Production Rollout

Deployment follows the existing safe process: verify no planned or running attacks, pull the tested commit, build the frontend, restart control, and verify local/public health plus worker traffic.

After deployment, `automatic-ivory` is archived through the new API only if it has no active attack or maintenance job. The result is verified in PostgreSQL: the worker remains archived, its token and secrets are absent, its existing key is revoked with no URI, the active VPN-node count drops, and the production service remains healthy.
