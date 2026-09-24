# VPN strict node deployment and migration-rehearsal plan

> Status: planned only. Every production mutation requires a new execution turn,
> current-state revalidation and explicit operator approval. This document does
> not authorize deployment by itself.

**Goal:** install the already reviewed node zipapp, literal Ed25519 trust and
private node configuration through a bounded one-shot path, and rehearse the
control migration on a closed copy. Leave runtime scheduling and enqueue absent.

**Non-goals:** no dispatcher scheduler/startup hook, API/lifecycle cutover,
protected REALITY inbound, TCP 443 change, owner-key/link change, invitation,
public access or payment. Do not run the legacy `known_hosts=None` helper.

## Release gates before any write

- Re-read `docs/current-state.md` and confirm the branch/commit, production
  revision, node identity, panel/Xray versions, current owner link and service
  health have not changed.
- Reconfirm the exact control-server and VPN-node Ed25519 fingerprints through
  their existing strict `known_hosts` files. Refuse missing, duplicate, hashed,
  certificate-authority, wildcard, non-literal-host or non-Ed25519 entries.
- Build the deterministic zipapp from a clean committed tree. Record the commit,
  manifest and SHA-256. A source worktree, uncommitted file or dependency download
  must not enter the artifact.
- Snapshot hashes, ownership, modes and sizes of the active node code, config,
  token and journal. Back up code only to a new root-private regular file. Never
  copy, print, archive or transfer token/config/journal contents off the node.
- Use a separate root-private, fsynced deployment-state file to record the exact
  pre-state and each activation step. A caught failure/cancellation rolls back
  synchronously. After process/power loss, the next invocation may perform only
  idempotent rollback to the recorded pre-state before accepting a new deploy.
- Take a fresh root-private control database backup and restore it into a
  closed temporary PostgreSQL. Run the exact migration plus rollback rehearsal,
  schema comparison and full relevant queue/dispatcher/reservation tests there.
  Delete the copy and prove its listener is closed before production work.

## Task 1: Implement the one-shot strict-pinned deployer

- [ ] Accept only explicit fixed paths and the expected commit/artifact hashes.
- [ ] Reuse the reviewed strict transport and literal Ed25519 pin. Disable SSH
  config, agents, default keys, X.509 trust and interactive authentication.
- [ ] Upload to a new same-filesystem regular candidate file with `O_NOFOLLOW`/
  exclusive creation. Require root ownership, private mode and bounded size.
- [ ] Open the fixed deployment directory with `O_DIRECTORY|O_NOFOLLOW`; verify
  every ancestor and the parent are real directories with expected owner/mode,
  then use that held directory descriptor for create, replace and parent `fsync`.
- [ ] Verify candidate SHA-256 on both sides and run the node interpreter as
  `/usr/bin/python3 -I -S candidate.pyz --import-probe`. Accept only the exact
  stdout bytes `veltrix-vpn-node-import-ok-v1\n`, empty stderr and exit status 0.
- [ ] `fsync` the candidate, atomically replace the fixed regular active file
  `/opt/veltrix-vpn/current/vpn-node.pyz`, then `fsync` its parent directory.
  Refuse symlinks and unexpected inode types.
- [ ] Keep the previous regular zipapp for code-only rollback. That code rollback
  repeats hash/probe/fsync/atomic-replace checks and never modifies a pre-existing
  config, token or journal object.
- [ ] Produce only bounded static status codes; never log commands containing
  payloads, file contents, credentials, raw stdout/stderr or exception strings.

## Task 2: Install trust and node configuration safely

- [ ] Install the exact private control-side `known_hosts` file atomically from a
  pre-reviewed literal line. Its owner must equal the future dispatcher process
  effective UID and its mode must be `0600`; verify bytes and metadata afterward.
- [ ] For trust and config, validate every ancestor/parent without following
  symlinks, hold the verified directory descriptor, `fsync` the complete candidate,
  rename relative to that descriptor and `fsync` the parent before success.
- [ ] Create the node config only if absent, through a new root-owned private
  regular file and atomic rename. It may contain only the pinned loopback panel
  URL and expected existing x-ui database path.
- [ ] If node config already exists, require exact expected bytes and metadata;
  do not rewrite or normalize it.
- [ ] Require the existing API token file byte hash and metadata to remain
  unchanged. Never rotate or recreate the service token.
- [ ] Initialize a new empty journal only when its fixed path is absent and its
  parent is the expected private root-owned directory. If any journal component
  exists, validate it and refuse initialization/replacement/reset.
- [ ] The token and every pre-existing config/journal object must remain byte- and
  metadata-identical. An absent config or journal may change only to the exact
  planned newly created state on overall success. On failure/cancellation, remove
  only the recorded newly created unchanged inode(s), restore their prior absence
  and restore active code; if identity changed, stop for manual reconciliation.
- [ ] Do not report deployment failure as complete until synchronous rollback has
  restored the exact recorded pre-state. An abrupt-loss state remains explicitly
  incomplete until the next invocation's rollback-only recovery finishes.

## Task 3: Rehearse the control migration on a closed copy only

- [ ] Generate the migration SQL from the committed application and compare it
  with the migration rehearsed on the closed database copy.
- [ ] Prove idempotence, forward schema/data preservation and the documented
  rollback boundary on the closed copy with real PostgreSQL locks/timeouts.
- [ ] Verify the restored copy has no dispatcher scheduler/enqueue caller and run
  the full queue/dispatcher/reservation acceptance there without any network call.
- [ ] Record the exact backup hash, migration output and schema/data comparison,
  then stop and delete only the closed copy and prove its listener is closed.
- [ ] Do not migrate the production control database in this plan. A later
  production-migration runbook requires a fresh review and explicit approval.

## Task 4: Read-only deployment acceptance

- [ ] Verify active zipapp hash, exact import sentinel, regular-file inode type,
  root ownership and private mode through strict SSH.
- [ ] Run only the fixed active zipapp with `-I -S` and `--import-probe`; accept
  the exact sentinel. Do not send any request document, enter normal entrypoint
  execution or contact panel routes.
- [ ] Verify the token and every pre-existing config/journal object are unchanged;
  any newly created config/journal exactly matches the recorded intended state.
  Verify Xray PID/start generation is unchanged and the existing owner 8443
  endpoint/link still works.
- [ ] Verify the control service is active and its deployed application revision
  is unchanged. Verify no runtime/API/lifecycle caller or queue row was added.
- [ ] Remove only exact disposable candidates, staging files and the closed test
  database approved by the runbook. Preserve the one explicitly named previous
  zipapp rollback file until a separately approved retention/cleanup step. Prove
  no temporary listener, archive or process remains.

## Task 5: Required evidence and independent reviews

- [ ] Unit/integration tests prove candidate hash/probe failure leaves active
  code unchanged; interrupted replacement yields either old or new complete code.
- [ ] Tests prove existing config/token/journal are byte-identical across success,
  failure and rollback, and journal absence is the only initialization path.
- [ ] A disposable Linux filesystem rehearsal proves regular-file, symlink,
  owner/mode, fsync and atomic-rename behavior with the real Python 3.11 interpreter.
- [ ] Independent specification review rejects legacy SSH, TOFU, secret/raw-output
  logging, non-atomic activation, implicit journal creation and runtime wiring.
- [ ] Independent quality review inspects filesystem TOCTOU, cleanup, rollback,
  migration idempotence and exact production-boundary evidence.
- [ ] Run the complete backend suite with a fresh isolated PostgreSQL, whole-
  backend Ruff, zipapp import probe and `git diff --check`; record exact counts.

## Later runtime prerequisites — explicitly blocked here

No scheduler, startup hook, enqueue or controlled end-to-end mutation may be
enabled by this plan. A separate design and implementation must first:

1. configure and prove bounded PostgreSQL `statement_timeout` for claim/finalize;
2. configure and prove the database driver's command timeout and cancellation
   cleanup without leaked sessions or late commits;
3. implement and test explicit ambiguous-COMMIT reconciliation for both an
   already-finalized row and a still-claimed row, with no automatic resend;
4. independently review a single controlled operation and rollback/suspend path.

Only after those gates may another plan consider runtime scheduling, protected
inbound creation, TCP 443, external connection acceptance or friend invitations.
Payments remain a final separate stage.
