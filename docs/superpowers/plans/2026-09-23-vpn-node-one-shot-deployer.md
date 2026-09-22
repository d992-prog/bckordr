# VPN node one-shot deployer runbook

> Status: implemented and verified locally only. This file does not authorize a
> production write. Worker 15 remains unchanged until a fresh production
> preflight, backup and explicit execution decision.

## Scope

`backend/app/services/vpn_node_deployment.py` is a tracked administrative tool,
not a daemon and not a runtime auto-deployer. It provides four bounded steps:

1. `install_control_known_hosts()` installs one pre-reviewed literal
   `ssh-ed25519` host line atomically. The final file must be owned by the actual
   control-service effective UID with mode `0600`. Existing different trust is
   refused, never replaced implicitly.
2. `build_node_deployment_helper()` creates a deterministic root-private helper
   zipapp containing the exact node bundle, exact `node.json`, deployment
   transaction and no API token.
3. `deploy_prebuilt_node_release()` reloads the worker through the reviewed
   `load_transport_snapshot()` contract, reads a private helper with exact
   SHA-256 and invokes `deploy_node_helper_over_ssh()`.
4. The fixed strict-SSH command receives only a bounded digest/size/helper frame
   on stdin. It admits only a raw Ed25519 host key, root SSH, exactly one explicit
   credential, no SSH config/agent/default keys/X.509/GSS/interactive fallback,
   no PTY and no payload in argv.

The node-local `install_node_release()` requires UID 0 on production, validates
the loopback panel URL and existing private x-ui database/token, probes both an
existing active zipapp and the new candidate with `/usr/bin/python3 -I -S`, and
uses same-filesystem exclusive candidates, parent-directory fsync and atomic
replacement. Existing config, token and journal are preserved byte-for-byte and
by identity/metadata. Config and journal are created only when absent. A previous
active zipapp is retained as `vpn-node.pyz.previous`.

Every transaction writes a root-private state record before mutation. Caught
failures roll back synchronously. After abrupt loss, the next invocation accepts
only the same reviewed helper and performs rollback-only recovery, returning
`vpn_node_deployment_recovered`; the operator must invoke the reviewed helper a
second time to install. Unexpected identity, token or state changes return only
`vpn_node_deployment_incomplete` and require manual reconciliation.

## Required production inputs

- A node bundle built from a clean archive of the exact reviewed commit with
  `build_node_bundle()`, plus its recorded SHA-256 and exact manifest.
- The newly captured literal Ed25519 host line for worker 15. Its accepted public
  fingerprint is `SHA256:/rAY4jmkHBS0RZnVjJfn0ZP/C1FgBgncM3FvY17SlZs`.
  The older ignored cache file is RSA and is not compatible with strict transport.
- The actual production control-service effective UID. Do not infer it from the
  repository service template: the last live checkpoint said root while the
  tracked template says `www-data`.
- Exact canonical node configuration containing only version 1, the verified
  loopback 3x-UI URL and the verified existing x-ui database path.
- A fresh read-only snapshot of active code, token/config/journal metadata, Xray
  generation, owner UUID/link and current port 8443 health.

## Execution order

1. Confirm a clean source archive and record commit, node-bundle hash,
   helper hash and both exact manifests.
2. Run the complete deployment test file and the existing bundle, journal,
   entrypoint and transport suites on disposable Linux with the node's real
   `/usr/bin/python3` version.
3. Install the durable control `known_hosts` through
   `install_control_known_hosts()` and verify exact bytes, owner and `0600` mode.
4. Build the helper through `build_node_deployment_helper()` into a private
   `0600` file; do not put it in the repository or logs.
5. Load worker 15 and call `deploy_prebuilt_node_release()` with the exact helper
   SHA-256. Accept only `installed`. On `recovered`, stop, verify the restored
   pre-state, then explicitly run again. On `failed` or `incomplete`, stop.
6. Read-only acceptance must verify active hash/import sentinel, regular-file
   owner/mode, unchanged token and pre-existing config/journal, unchanged Xray
   generation and the existing owner 8443 connection.

Do not use `execute_worker_ssh_commands()`; it still uses `known_hosts=None` and
logs arbitrary command output. Do not create TCP 443/REALITY, enable dispatcher
flags, create invitations or migrate the production database as part of this
node-only installation.

## Remaining gates after node installation

- Rehearse the current application migration on a fresh closed production copy.
- Complete and verify ambiguous-COMMIT reconciliation.
- Deploy the application with friend beta, dispatcher and public portal flags
  still false.
- Create and externally accept the protected REALITY endpoint on TCP 443.
- Enable one release marker and one friend invitation, then prove individual
  connection and revoke before opening the remaining beta slots.
