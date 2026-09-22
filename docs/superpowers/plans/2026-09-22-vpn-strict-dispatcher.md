# VPN strict SSH dispatcher implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development with TDD,
> specification review and code-quality review after each task. This plan does
> not connect API/lifecycle staging, runtime scheduling or production deployment.

**Goal:** Execute the completed durable VPN control queue through a fixed,
strict-host-key-pinned SSH path and the reviewed node-local executor, without
holding database transactions during network I/O or exposing node secrets.

**Architecture:** A small stdlib node entrypoint is packaged as one zipapp and
accepts one immutable request on stdin. A separate AsyncSSH transport validates a
private known_hosts file, runs a fixed command and accepts one exact receipt. A
sequential callable dispatcher commits claim before SSH and finalizes in a fresh
transaction. Existing attack/maintenance/decommission paths share the durable
worker reservation. No runtime caller is added.

**Scope boundary:** No endpoint-bound API/lifecycle caller or background runtime
is cut over here and nothing is deployed. No REALITY inbound, TCP 443, owner-key
change, invitation, public access or payment.

---

## Task 1: Bounded node entrypoint and exact bundle

**Files:**
- Create `backend/app/services/vpn_node_entrypoint.py`
- Create `backend/app/services/vpn_node_bundle.py`
- Create `backend/tests/test_vpn_node_entrypoint.py`
- Create `backend/tests/test_vpn_node_bundle.py`

- [x] **Step 1: Write RED protocol and trust tests**

Test one bounded strict UTF-8 JSON document, duplicate/nonfinite/trailing/unknown
input rejection, exact request parsing and exact versioned receipt. Verify config,
token, database and journal paths are regular root-owned private objects, the
entrypoint requires effective UID zero, there are no symlinks or oversized reads,
and invalid input performs no config/auth/journal I/O. Synthetic secrets must not
occur in repr, stdout, stderr or formatted errors.

- [x] **Step 2: Implement the minimal entrypoint**

Reuse `parse_node_request`, `node_request_digest`, `NodePanelSession` token mode
and `execute_node_client_operation`. Keep token/config paths fixed, support
dependency injection only at the Python function boundary for tests, and output
no raw exception. Executor receipts preserve the existing allowlisted state/code
pairs; unexpected post-parse failures are never reported as observed.

- [x] **Step 3: Add the zipapp manifest and executable proof**

Build one archive containing only package initializers, entrypoint and the eight
reviewed stdlib node modules. Test the manifest, artifact hash, no third-party
imports and an actual `python -I -S bundle.pyz` invocation. A failed build/import
must not replace an existing target artifact.

- [x] **Step 4: Run focused GREEN, Ruff and commit**

Run entrypoint plus all node request/journal/HTTP/observation/identity/runtime/
executor tests and commit only Task 1.

## Task 2: Strict SSH transport and transaction-separated dispatcher

**Files:**
- Create `backend/app/services/vpn_node_transport.py`
- Create `backend/app/services/vpn_control_dispatcher.py`
- Create `backend/tests/test_vpn_node_transport.py`
- Create `backend/tests/test_vpn_control_dispatcher.py`
- Create `backend/tests/test_vpn_control_dispatcher_postgres.py`

- [x] **Step 1: Write RED strict-transport tests**

Use a real loopback AsyncSSH server with an ephemeral Ed25519 host key. Prove
correct exact literal host/port pin succeeds and missing/wrong/changed/wildcard/
hashed/CA pins fail before stdin or runner invocation. Assert secure ancestors,
effective-UID-owned `0600` trust, `config=None`, and no agent/PKCS11/default-key/
GSS/hostbased/kbd-interactive/trivial-auth fallback using hostile HOME, `.ssh`
and `SSH_AUTH_SOCK`. Disable X.509 trusted certificates/paths explicitly and
offer only the raw `ssh-ed25519` server host-key algorithm; hostile user CA and
certificate paths must never be read. Require username root, fixed command, no
PTY, binary stdin, no request in argv/env/logs and no retry.
Read stdout/stderr concurrently with limits and reject wrong ID/digest, duplicate/
extra/trailing JSON, nonzero exit and any ambiguous output.

- [x] **Step 2: Implement transport failure phases**

Validate/read the known_hosts file before importing/connecting. Accept exactly
one credential mode. Password mode disables client keys/publickey; key mode opens
one absolute private file with nofollow/owner/`0600`/ancestor/size checks, imports
its bytes before network and disables password. Both disable every ambient auth
source and use one explicit preferred method. Build a frozen credential snapshot
with secret-free repr. Do not store or stringify AsyncSSH exceptions.

- [x] **Step 3: Write RED dispatcher transaction tests**

Prove claim commits before the injected transport sees the operation, request
bytes equal the committed snapshot, the claim session is closed during network,
and finalize uses a new session/token. While the worker row is still locked in the
claim transaction, detach the password value or already nofollow-read/validated/
imported private-key bytes into the frozen secret-redacted credential snapshot.
After commit, transport receives only that snapshot and never reopens the DB key
path; replacing the path/file after commit must not change the used credential.
A no-claim call still commits superseded rows. Claim commit failure sends nothing.
Valid receipts finalize once; finalize failure never resends. Add barriers for
cancellation during claim commit, post-commit/pre-write, post-write/pre-receipt
and post-receipt/pre-finalize.
Post-commit cancellation uses a separately created, bounded, shielded finalize
task with failed or uncertain according to phase; exact validated receipts keep
their state. Re-raise cancellation after cleanup; process loss before finalize
COMMIT leaves a durable claimed/uncertain reservation with no lease. Treat
`finalize_timeout` only as the pre-COMMIT gate deadline: timeout may revoke
before COMMIT and must await
rollback/session close. Once the atomic gate enters `COMMITTING`, wait for the
definitive driver outcome and session close even beyond that deadline. A lost
COMMIT response may leave the row finalized or claimed, but never permits an
automatic resend.

- [x] **Step 4: Add actual PostgreSQL dispatcher races**

Using independent backend PIDs, prove two dispatchers cannot execute the same
operation/worker, different workers can progress, committed claims survive
process/session loss, and ambiguous execution cannot be reclaimed by time.

- [x] **Step 5: Implement, verify and commit**

Keep the dispatcher callable but not wired to application runtime. Run transport,
dispatcher, node protocol and control-intent suites with actual PostgreSQL, Ruff
and diff check; commit only Task 2. Runtime/deployment scheduling remains blocked
until PostgreSQL `statement_timeout`, a driver command timeout and explicit
commit-ambiguity reconciliation are separately configured and tested.

Evidence: commits `d9e1548`, `3326910`, `0525839` and `aa855bc`; independent
specification and quality reviews approved the final code. Focused local
verification passed `69` tests with `11` expected PostgreSQL skips, and the
related node/control regression passed `998` tests with `30` environment skips.
All `11` dispatcher PostgreSQL acceptance tests then passed against a fresh
loopback-only PostgreSQL 14 cluster on the managing server in `12.45s`. The exact
temporary directory `/tmp/veltrix-strict-dispatcher-test-TOdHK1cg`, uploaded
archive and database were removed, port `56675` was closed, and
`domain-drop-control.service` remained active. No production database, VPN node,
runtime scheduler, route, TCP 443 setting, invitation or payment was changed.

## Task 3: Cross-system worker reservations

**Files:**
- Modify `backend/app/services/vpn_control_intents.py`
- Modify `backend/app/services/vpn_policy.py`
- Modify `backend/app/services/attack_runtime.py`
- Modify `backend/app/services/worker_maintenance.py`
- Modify `backend/app/services/worker_decommission.py`
- Modify `backend/app/api/routes/control.py`
- Modify `backend/tests/test_attack_runtime.py`
- Modify matching intent, policy, maintenance, decommission, control and
  PostgreSQL tests

- [ ] **Step 1: Write RED guard tests**

Queued/claimed/uncertain operations must be included in the VPN mutation worker
set. After worker `FOR UPDATE`, attack allocation requeries reservations before
using its candidate. Stage and claim skip a worker with active domain attack or
queued/running mutating VPN maintenance after their worker lock. Every single and
bulk maintenance creation path locks the worker; the last pre-SSH check repeats
the control reservation. Sensitive worker update and decommission refuse while
any reservation exists and do not clear credentials.

- [ ] **Step 2: Prove real PostgreSQL races**

Use two independent connections to interleave stage/claim with attack assignment,
single and bulk maintenance queue/run, worker edit and decommission. Force attack
to pre-read an empty reservation, block on the worker while stage commits queued,
then prove its post-lock requery excludes that worker. Test the reverse ordering:
attack/maintenance commits first, then stage/claim refuses it. No SSH callback
occurs in the losing path. Add an inverse-order bulk-maintenance versus attack
race which would deadlock if either side used route or business-priority order.

- [ ] **Step 3: Implement minimal shared guards**

Reuse `active_vpn_control_worker_ids`; do not add another reservation table or
in-memory lock. Preserve the canonical customer/subscription/key/worker/endpoint/
operation order and the existing active-attack semantics. Multi-worker attack
and bulk paths first lock all candidate `WorkerNode` rows by ascending ID,
requery reservations, and only then apply target-RPS/name business ordering;
never lock a batch one-by-one in route iteration order.

- [ ] **Step 4: Verify, review and commit**

Run focused actual-PostgreSQL tests, relevant endpoint/lifecycle/maintenance/
decommission/control suites, Ruff and diff check. Obtain spec then quality review.

## Task 4: Independent final review and local evidence

- [ ] **Step 1: Independent spec review**

Reject hidden legacy SSH use, trust-on-first-use, command/payload interpolation,
secret/raw-output logging, network inside database transactions, clock reclaim,
implicit journal creation, API/lifecycle cutover or production mutation.

- [ ] **Step 2: Independent quality review**

Inspect AsyncSSH cancellation/output/auth handling, literal pin parsing, receipt
parsing, zipapp manifest, filesystem ownership/modes, SQLAlchemy transaction
boundaries, PostgreSQL race proof and all reservation call sites. Fix important
findings and rerun reviews.

- [ ] **Step 3: Full verification**

Run the complete backend suite with fresh isolated PostgreSQL, whole-backend Ruff,
zipapp Python 3.11 execution and `git diff --check`. Record exact counts and delete
only the exact temporary cluster.

## Task 5: Record the undeployed boundary and next runbook

- [ ] **Step 1: Update current-state evidence**

State that the callable dispatcher and bundle are locally verified but have no
runtime caller and are not deployed. Production remains on the legacy path; TCP
443, REALITY, owner identity/link, invitations, public access and payments are
unchanged.

- [ ] **Step 2: Write the next deployment-plan prerequisites**

Require a separately reviewed one-shot strict-pinned deployer/runbook. It must
upload a new regular file, verify hash and exact interpreter import probe, fsync
and atomically replace a fixed regular active zipapp, preserve config/token/
journal byte-for-byte, support code-only rollback, create node config safely and
initialize a new empty journal only when absent. It must rehearse control DB
migrations on a closed copy and deploy with no runtime scheduling or enqueue.
Before any later scheduler is enabled, it must also prove PostgreSQL
`statement_timeout`, the driver command timeout and reconciliation of an
ambiguous COMMIT response without automatic resend.
