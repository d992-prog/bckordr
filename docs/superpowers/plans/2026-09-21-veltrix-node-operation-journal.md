# Veltrix node operation journal implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist node-side mutation intent and prevent replay or stale re-enabling after an interrupted remote command.

**Architecture:** A private directory contains an application-owned journal SQLite file and a separate SQLite writer-gate file. Holding `BEGIN IMMEDIATE` on the gate serializes entire executions while the journal can independently commit intent and receipts. This does not modify 3x-UI's database and requires no daemon or public port. Interrupted/ambiguous mutations block further node writes until an explicit reconciliation procedure; killing SSH is never treated as proof that the panel handler stopped.

**Tech Stack:** Python 3.11 stdlib SQLite, dataclasses, hashlib, UUID; real independent SQLite connections and subprocess tests.

---

## Boundary and rationale

This journal is packaged on the VPN node. Control-side durable staging and latest
policy validation remain mandatory before invocation. A node journal is not a
replacement for those database transactions. No application route or lifecycle
uses this journal until the complete adapter is connected and tested.

A synchronous executor is sufficient: it holds the gate for its lifetime. If the
process dies, SQLite releases the gate, but the committed journal phase remains.
The next process sees that phase and refuses mutation rather than assuming the
unfinished HTTP request cannot complete. No background child, daemon, PID-stealing
lock recovery, persistent panel password, or invented lease-expiry guarantee.

**Explicit recovery boundary:** this is crash recovery, not anti-rollback storage.
Matching installation IDs reject mismatched pairs, but cannot detect restoration
of a valid older snapshot from the same installation (of one file or both files).
Independent quality review reproduced an old journal restore erasing a revoke
fence. Never restore, replace, delete or reinitialize these ledger files as a
recovery procedure. After operator restore, node replacement or suspected history
loss, the control-side reservation must remain blocked until explicit reconciliation
against durable current control intent and actual node state. A reviewed recovery
procedure and control-side staging are release gates; they do not exist yet.

## Task 1: Standard-library journal and node-wide gate

**Files:**
- Create `backend/app/services/vpn_node_journal.py`.
- Create `backend/tests/test_vpn_node_journal.py`.

- [x] Write RED tests for the public behavior below before implementation.

```python
@dataclass(frozen=True, slots=True)
class NodeOperation:
    operation_id: UUID
    access_key_id: int
    generation: int
    action: Literal["provision", "suspend", "revoke"]
    request_digest: str  # canonical SHA-256, no raw credentials/policy stored


@dataclass(frozen=True, slots=True)
class NodeOperationReceipt:
    state: Literal["observed", "failed", "uncertain", "stale", "blocked"]
    error_code: str | None


class NodeJournalError(ValueError):
    # Always one of the fixed codes listed below, never a SQLite message/path.
    code: str


def initialize_node_journal(directory: Path) -> None:
    # Explicit installation only, in an existing empty private directory.
    # Exclusive creation; no overwriting or repair of a partial installation.


def execute_node_operation(
    directory: Path,
    operation: NodeOperation,
    perform: Callable[[Callable[[], None]], None],
    *,
    lock_timeout_seconds: float = 2.0,
) -> NodeOperationReceipt:
    # validate -> acquire gate -> check replay/fences/uncertainty -> journal queued
    # -> perform(mark_mutating) -> journal observed -> release gate
    # No callback is entered on replay, stale, blocked, or invalid input.
```

`observed` means the supplied operation's documented postcondition observation
completed, not a blanket runtime-ready assertion. The later executor is responsible
for performing that observation. The callback cannot return arbitrary data into
the journal. The callback receives only an idempotent `mark_mutating()` hook, which
commits phase `mutating` BEFORE a potentially mutating HTTP send. Repeated hook
calls are harmless. Hooks used after callback completion must fail without writes.

- [x] Implement exactly these state rules:

1. Validate operation UUID object, positive integer IDs/generation excluding bool
   and bounded to SQLite signed64 range 1..2^63-1 (overflow is invalid before I/O),
   action literal, digest exactly 64 lowercase hex. Validate finite numeric lock
   timeout `0<t<=30`, not bool. Directory must be absolute Path, existing real
   directory with no symlink; on POSIX require owner=current uid and no group/other
   permissions. Invalid input raises `vpn_node_journal_invalid` before opening
   files or calling perform. Caller prepares private directory; no recursive mkdir.
2. Only files `gate.sqlite3` and `operations.sqlite3` beneath that validated
   directory; reject preexisting symlinks/nonregular files and on POSIX wrong owner
   or group/other permissions. Explicit `initialize_node_journal` requires an empty
   private directory and creates files with exclusive OS open and 0600; it never
   overwrites or repairs a partial pair. Store matching random journal IDs and
   format version 1 in both files; mark gate metadata initialized only after the
   journal schema/metadata commit. A second initializer refuses existing files.
   Execution requires the complete initialized pair with matching metadata; it
   NEVER creates/reinitializes either missing/empty database. Missing, truncated or
   mismatched-installation history fails unavailable rather than forgetting
   revoke/uncertainty. Same-installation snapshot rollback is excluded above.
   Never chmod an existing arbitrary file. Use SQLite default DELETE journal mode,
   synchronous FULL, foreign_keys ON; don't put databases on remote/network FS.
3. Gate connection takes `BEGIN IMMEDIATE` with the bounded lock timeout and holds
   it through all callback work and final journal commit. Opening/checking schema
   happens under the gate. Busy/locked gate raises `vpn_node_journal_busy` without
   callback. All connections close/roll back the gate even on BaseException.
4. Journal table operations stores operation_id TEXT primary key, access_key_id,
   generation, action, request_digest, phase (`queued`,`mutating`,`observed`,
   `failed`,`uncertain`), error_code. Table key_fences stores access_key_id primary
   key, latest_generation and permanently_revoked bool. These tables are owned by
   this module; no panel tables or secrets. Use parameterized statements only.
5. For an existing operation ID require all identity fields equal; mismatch raises
   `vpn_node_operation_conflict`. Terminal observed/failed receipts replay exactly
   with zero callback. An existing queued phase from a previous process becomes
   failed with `vpn_node_interrupted_before_mutation`; it is not auto-executed.
   Existing mutating phase becomes uncertain with `vpn_node_mutation_uncertain`.
   Existing uncertain stays uncertain. Do not infer remote success from timeout.
6. Before accepting a new operation, any mutating/uncertain operation anywhere on
   this node blocks it with receipt blocked / `vpn_node_reconciliation_required`.
   This is intentionally node-wide because an old panel handler may still finish.
   Do not clear it automatically after a wall-clock interval. Queued operations
   abandoned by a prior process can safely become failed (no mutation hook ran).
7. A generation <= latest_generation with a different operation ID returns stale /
   `vpn_node_operation_stale`; no callback, no fence rollback. A provision after a
   permanent revoke fence returns blocked / `vpn_node_key_revoked`. Suspend/revoke
   with newer generations remain allowed. Accepting revoke sets the permanent
   fence before callback; even an auth failure does not cancel that intent.
8. Accepted new operation and advanced key fence commit atomically as queued.
   `mark_mutating` commits mutating with synchronous FULL before returning. If that
   commit fails, callback must not proceed with sending; raise static journal error.
9. Callback success stores observed. Exception before hook stores failed /
   `vpn_node_preflight_failed`; exception after hook stores uncertain /
   `vpn_node_mutation_uncertain`. Do not interpolate or retain original exception.
   Ordinary Exception is converted to the safe receipt. KeyboardInterrupt and
   SystemExit are recorded with the same phase rule and re-raised after cleanup;
   no original secret message is stored. Hard process death leaves the committed
   phase for rule 5. No automatic callback retry under any condition.
10. Any journal I/O/corruption/schema error becomes `vpn_node_journal_unavailable`
    with suppressed raw exception chain. Failure writing a receipt cannot become
    observed in memory; the durable queued/mutating phase remains for recovery.
    Existing unrecognized schema or row values must fail closed, not be repaired
    by dropping tables. No reset/delete/resolve-all convenience API.
    Validate fence consistency on reopen: every stored operation has a fence whose
    generation is at least that operation's generation, and any stored revoke
    requires the permanent bit. Missing/inconsistent fences fail unavailable.

Fixed exception codes: `vpn_node_journal_invalid`, `vpn_node_journal_busy`,
`vpn_node_journal_unavailable`, `vpn_node_operation_conflict`. Receipt codes are
only the fixed strings above. All result representations, database rows, stdout
and logs are secret-free by construction; callback exception strings never escape
as ordinary returned/raised journal errors. The callback itself is trusted internal
code, not a remotely supplied callable.

- [x] Implement these real tests with synthetic IDs/digests: observed replay and
  one callback invocation; changed request under same ID refused; lower/equal
  generation stale; permanent revoke blocks later provision even after preflight
  failure; revoke/suspend newer allowed; exception before/after mark produces
  different states; idempotent mark and expired hook; callback BaseException;
  hard subprocess exit before/after mark and next-process recovery; two independent
  processes serialized with gate busy timeout; another key blocked by ambiguity;
  unrelated keys allowed after successful operation; journal receipt write failure
  is never reported as success; malformed schema/invalid stored phase; missing/
  relative/symlink paths; secret sentinel never in stored text or errors; Python -S
  import succeeds. Test setup alone creates private temp directories/files.

Additional recovery tests required by independent design review:

- Remove or truncate the journal after a revoke and after a mutating checkpoint;
  subsequent execution fails closed and does not recreate history. Partial initial
  installation and mismatched metadata IDs likewise cannot execute operations.
- A local synthetic HTTP handler accepts a mutation, survives executor subprocess
  termination, then completes late. New revoke and unrelated-key operations stay
  blocked both before and after that completion, across another journal reopen.
- Inject a failed mark_mutating commit and assert zero sends. Kill after durable
  queued revoke before callback and assert provision remains fenced. Fail receipt
  commit after a send; a new process still blocks further mutation.
- Historical replay is NOT current policy: provision observed -> revoke accepted
  -> old provision receipt replay performs no callback. The future control runner
  must revalidate generation and permanent revoke before saving results, so that
  historical observed receipt can never activate a revoked key or return its URI.

Example replay assertion:

```python
def test_replay_does_not_mutate_twice(private_directory, operation):
    calls = []
    def perform(mark_mutating):
        mark_mutating()
        calls.append("sent")
    first = execute_node_operation(private_directory, operation, perform)
    second = execute_node_operation(private_directory, operation, perform)
    assert first == second == NodeOperationReceipt("observed", None)
    assert calls == ["sent"]
```

- [x] Run RED/GREEN under unique worktree/backend `.pytest_cache` basetemp, then
  all new node modules and existing endpoint/identity tests; Ruff and whitespace
  checks. Spec review precedes quality review. Do not deploy or remove application
  barriers as part of this standalone journal task.

## Verified local checkpoint

The implementer completed RED/GREEN and the independent specification review
found three defects before approval: SQL LIKE's underscore wildcard hid unknown
schema names, receipt/cleanup faults could mask callback process interruptions,
and NUL paths could leak a raw ValueError. Regression tests failed before each
fix. The reviewed implementation uses an exact GLOB prefix check, preserves the
callback interruption after both cleanup attempts, and returns a static path error.

Final journal suite: **83 passed, 5 platform skips**, independently repeated by
both reviewers; quality run took 4.73s. Four skips require Windows symlink creation
privilege; one exercises POSIX file ownership/modes. Actual Windows junction and
ancestor rejection tests passed. Quality review approved the normal crash-recovery
contract with the explicit anti-rollback exclusion documented above.

Parent full backend on the final code: **1311 passed, 13 skipped in 201.15s**.
Those 13 are the five journal platform skips plus eight PostgreSQL-only tests;
there is no new PostgreSQL concurrency proof. Whole-backend Ruff and whitespace
checks passed. Existing HTTP/observer/identity/endpoint/barrier suites are included.

A separately reviewed, synthetic POSIX check ran through saved-host SSH on the
control server, using only a private `/tmp/veltrix-journal-smoke-*` directory and
the transmitted stdlib module. All **14 checks passed** on the application Python
3.11.0 (also on system Python3.10.12): exclusive mode0600 files, mode/owner refusals
without silent chmod, file/directory/ancestor symlink refusal, replay, revoke fence
and SQLite integrity. Each run removed its own temporary directory and verified
it absent. No application imports, production data, node API, listener or service
changes were involved. This covers Linux-specific behavior, not the unrun PG tests.

The module has not been installed or wired into production. There is still no
control-side durable queue, runtime mutation executor or reconciliation workflow;
do not remove endpoint barriers or treat this checkpoint as beta readiness.
