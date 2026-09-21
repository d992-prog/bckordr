# Veltrix strict VPN dispatcher and node bundle design

Date: 2026-09-22.

Status: local implementation design for the next protected-endpoint gate. It
extends the approved secure-endpoint design and the completed durable control
queue. It does not authorize production deployment, runtime scheduling,
API/lifecycle cutover, a REALITY inbound, TCP 443 changes, friend invitations,
public access or payments.

## Outcome and boundary

The control application will be able to claim one immutable VPN operation,
commit that claim, execute the exact saved request through strictly pinned SSH,
and finalize a small allowlisted receipt in a new transaction. The existing
node-local panel session, journal, identity observer, Xray reader and executor
are packaged behind one bounded stdin/stdout entrypoint. The 3x-UI token and raw
panel data never leave the VPN node.

The dispatcher is sequential and remains an explicit callable with no runtime
caller in this increment. No current API, lifecycle, archive, subscription or
provisioning path may enqueue production work. The endpoint-bound legacy
mutation barrier remains in force until a separate PostgreSQL-tested cutover.

## Alternatives rejected

- The legacy `execute_worker_ssh_commands` helper is rejected because it passes
  `known_hosts=None`, interpolates shell commands and retains raw output/errors.
- An SSH tunnel to the panel is rejected because full inbound responses contain
  REALITY private material which must stay on the node.
- A new node daemon, public API or open management port is unnecessary. An
  on-demand SSH process is enough for the closed beta.
- A mutable host-key field in the admin UI is deferred. A dedicated file owned
  by the effective control-service UID is simpler and makes trust replacement an
  explicit operational action.
- A broker, retry framework, lease and parallel worker pool are deferred. One
  claimed operation per worker plus the existing durable reservation is enough.

## Durable trust and configuration

The future control deployment supplies an absolute private known_hosts path.
Before connection, the dispatcher walks ancestors which are non-symlinks, owned
by root or the effective service UID and not group/other-writable, opens
the file without following symlinks, verifies a regular file owned by the
effective service UID with mode `0600`, bounds its size and requires nonempty OpenSSH
known_hosts bytes. It parses only literal raw `ssh-ed25519` entries: the target
must be exactly `host` on port 22 or `[host]:port` otherwise. Markers, hashed
hosts, wildcard/negated/multi-host patterns, CAs and host-only fallback for a
nondefault port are rejected before AsyncSSH sees the data. Changing trust is not
exposed through the application UI.

The exact validated bytes are passed to AsyncSSH; `None`, default files, user SSH
config, agent, PKCS11, GSS key exchange/auth, host-based, keyboard-interactive
and trivial/none authentication are forbidden. Exactly one explicit credential
mode is accepted. Password mode sets password-only authentication and disables
client keys. Key mode opens one absolute private-key file without symlinks,
verifies effective-UID ownership, `0600`, secure ancestors and bounded size,
imports those bytes before network I/O and disables password fallback. Hostile
`HOME`, `.ssh` and `SSH_AUTH_SOCK` state must be irrelevant.

The remote command is a code constant, not configuration assembled from database
values. It starts a versioned zipapp with the node Python in isolated/no-site
mode. The SSH username must be exactly `root`; there is no sudo or alternate-user
fallback. Host, port and the one loaded credential are snapshotted while the
worker row is locked; representations and errors hide credentials. The request
travels only over stdin and never appears in argv, environment, events or logs.

On the node, `/var/lib/veltrix-vpn/control-auth/node.json` contains exactly the
version, validated panel URL and actual x-ui database path. The runner rejects
duplicate/unknown keys and reads it with the same bounded non-symlink root-owned
`0600` contract as the already-created `api-token` file. It requires effective
UID zero before reading secrets. The x-ui database and every journal component
must be regular root-owned objects with their module-specific private modes. The
journal is initialized explicitly once; the runner never creates, replaces,
restores or resets it implicitly.

## Node entrypoint and receipt

The stdlib entrypoint reads one bounded UTF-8 JSON document from stdin, rejects
duplicate keys, nonfinite numbers, invalid/trailing data and unknown fields, then
calls the existing `parse_node_request` and `execute_node_client_operation`.
It opens the token only on the node and constructs `NodePanelSession` in Bearer
mode. Inventory, HTTP bodies, UUIDs, credentials and arbitrary exceptions are
never written to stdout or stderr.

A successful invocation emits exactly one bounded JSON object:

```json
{
  "version": 1,
  "operation_id": "canonical-uuid",
  "request_digest": "lowercase-sha256",
  "state": "observed|failed|uncertain|stale|blocked",
  "error_code": null
}
```

The state/error pair must match the existing node/control allowlist. The control
side compares the operation ID and digest to its committed snapshot. Missing,
extra, duplicate or mismatched fields are never accepted as success. Unexpected
failure after request transmission is uncertain, even if the SSH command exits.

The bundle contains only the entrypoint plus the eight reviewed stdlib modules:
endpoint types, request, journal, panel HTTP, observation, identity, Xray runtime
and executor, with package initializers. A manifest test prevents accidental
dependency expansion. This increment only builds, hashes and executes the zipapp
locally; it does not upload or select it on a server. A later reviewed one-shot
deployment plan must use the same pin, upload a new regular file, verify hash and
an import probe, fsync and atomically rename it over a fixed regular active file.
It must not turn the dispatcher into an arbitrary-command API. Token,
configuration and journal paths remain outside releases and must be preserved
byte-for-byte; rollback is code-only. No node systemd service is needed.

## Dispatcher transaction protocol

For each attempt the dispatcher:

1. creates a fresh claim token;
2. opens a short database transaction and calls
   `claim_next_vpn_control_operation`;
3. detaches canonical request bytes plus operation/worker/digest identity;
4. always commits and closes the session, including the no-claim case because
   stale candidates may have been superseded;
5. executes the fixed node command through strict SSH with no database session;
6. parses one exact receipt or conservatively classifies transport failure;
7. opens a new transaction, calls `finalize_vpn_control_operation` with the same
   operation and claim token, commits and closes it.

Failure or cancellation before/during claim commit performs no SSH; rollback is
best-effort and an ambiguously committed claim simply remains reserved. After a
confirmed claim commit but before the first stdin write, cancellation or failure
uses a separately created, bounded and shielded finalize task for
`failed/vpn_node_preflight_failed`, then re-raises cancellation. From the first
attempted stdin write until a validated receipt, failure/cancellation uses the
same mechanism for `uncertain/vpn_node_mutation_uncertain`. After a validated
receipt but before finalize commit, cancellation shields finalization of that
exact receipt, never substitutes another state, then re-raises. If any bounded
finalize attempt fails or times out, the row remains claimed and reserved; no
path resends automatically. Sessions always rollback/close on failed finalization.

If the control process dies after claim commit, the row remains claimed and the
worker remains reserved; it is never reclaimed by time. If a valid receipt is
received but finalize commit fails, the dispatcher does not resend. Existing
generation/policy comparison turns a late observed receipt into superseded rather
than changing a newer key. Reconciliation remains a later explicit workflow.

## Cross-system worker exclusion

Queue safety requires the same worker not be mutated by domain work or legacy
maintenance. Queued, claimed and uncertain control operations are added to the
existing VPN-mutation worker set used by attack allocation. After locking worker
rows, the attack allocator must query reservations again and discard newly
reserved workers before assignment. Staging and claim both recheck active attack
and queued/running VPN-mutation maintenance after locking their worker.

Every single and bulk VPN-maintenance creation path acquires the worker lock and
rechecks control reservations. Every multi-worker path first locks the complete
candidate set by ascending worker ID, requeries reservations, then applies its
business priority ordering; route iteration and RPS/name order never determine
lock order. Execution repeats the reservation check immediately before legacy
SSH. Sensitive worker updates and decommission refuse to change or erase SSH/VPN
data while an operation is queued, claimed or uncertain. One check alone leaves
a race.

Endpoint disable is not proof that a remote client disappeared. A provision
receipt arriving after disable is already superseded by finalization, but any
remote side effect still requires a later suspend/revoke or reconciliation.

## Runtime and deployment follow-up

This increment does not modify the control orchestrator and has no interval,
startup hook or background task. Startup therefore cannot contact SSH or create
trust/config/journal files. Runtime scheduling is YAGNI until a controlled
end-to-end operation has been reviewed.

The next plan must define and test a bounded one-shot strict-pinned deployer or
equivalent reviewed runbook. It must back up/rehearse the database migration,
install the exact trust file, atomically install the regular zipapp and node
config, explicitly initialize a new empty journal only when absent, and verify
all ownership/modes before any request. A failed deploy must leave active code,
config, token and journal byte-identical. The old 8443 endpoint, owner UUID/link
and Xray generation must remain unchanged. Only after a separate controlled
end-to-end proof may a later plan add runtime scheduling or API/lifecycle staging.

## Required evidence

- Actual loopback AsyncSSH tests with an ephemeral Ed25519 server: correct pin
  succeeds; missing/wrong/changed pin fails before runner input; config/agent and
  default-known-hosts fallback are absent.
- Bounded concurrent stdout/stderr, fixed command, stdin-only request, exact
  receipt matching and secret-free errors/logs.
- Node entrypoint integration with synthetic loopback panel, real temporary x-ui
  SQLite and the real durable journal, including interruption after a mutation.
- Real PostgreSQL tests prove claim commit before network, separate finalize
  transaction, no-claim commit, two dispatchers, every cancellation barrier and
  attack/maintenance/decommission/staging races.
- The local zipapp manifest/hash/isolated execution are proved. Server staging,
  atomic activation and journal initialization are explicitly deferred to the
  next reviewed deployment plan.
- Independent spec and quality reviews, complete backend suite with isolated
  PostgreSQL, whole-backend Ruff and diff check.

Payments, invitations, TCP 443, protected inbound creation and public access stay
unchanged throughout this design.
