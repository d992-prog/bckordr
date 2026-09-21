# VPN Control Intent Queue Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist, claim and finalize endpoint-bound VPN client intentions without performing network I/O, so a later strict-SSH dispatcher can execute an exact immutable request without stale results overwriting newer policy.

**Architecture:** Extend `VpnAccessKey` with monotonic intent/identity fields and add one append-only `VpnControlOperation` row per generation. A staging service locks customer → subscription → key → worker → endpoint → prior operations, derives the existing typed `VpnNodeRequest`, and flushes it without committing. A separate PostgreSQL-safe claim/finalize state machine uses `FOR UPDATE SKIP LOCKED`, durable claim tokens and generation compare-and-set; it never performs SSH or retries uncertain work.

**Tech Stack:** Python 3.11+, SQLAlchemy async, PostgreSQL 14, SQLite unit tests, existing `VpnNodeRequest`, pytest/pytest-asyncio, Ruff. No new dependency.

**Scope boundary:** This plan deliberately stops before SSH dispatch, API/lifecycle cutover, endpoint creation, TCP 443 changes or invitations. The existing legacy paths remain the active production behavior. A claimed/uncertain operation is never released by a clock lease; reconciliation is a later explicit operation.

---

## File map

- Modify `backend/app/db/models.py`: durable key fields and operation model.
- Modify `backend/app/db/vpn_endpoint_migrations.py`: idempotent PostgreSQL schema upgrade.
- Modify `backend/tests/test_vpn_endpoint_schema.py`: SQLite metadata/default/constraint coverage.
- Modify `backend/tests/test_vpn_endpoint_migrations.py`: real PostgreSQL idempotency/catalog/data preservation.
- Create `backend/app/services/vpn_control_intents.py`: pure request derivation, stage, claim, finalize and reservation query. Keeping this in one file avoids speculative repository/dispatcher layers; split only if the final implementation becomes difficult to review.
- Create `backend/tests/test_vpn_control_intents.py`: fast SQLite behavior and no-I/O/TDD coverage.
- Create `backend/tests/test_vpn_control_intents_postgres.py`: actual two-connection contention, rollback and stale-finalization proof.
- Modify `docs/veltrix-endpoint-api-findings.md`: record evidence and remaining dispatch boundary.

## Task 1: Durable schema and idempotent migration

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/vpn_endpoint_migrations.py`
- Modify: `backend/tests/test_vpn_endpoint_schema.py`
- Modify: `backend/tests/test_vpn_endpoint_migrations.py`

- [x] **Step 1: Write failing metadata tests**

Assert `VpnAccessKey` has these backward-compatible columns:

```python
operation_generation: int = 0
revoke_requested_at: datetime | None = None
verified_client_email: str | None = None
panel_sub_id: str | None = None  # NULL unknown; empty string is a verified legacy value
```

Assert `VpnControlOperation` exposes exactly:

```python
id, access_key_id, worker_id, endpoint_id, generation, action,
request_snapshot, request_digest, state, claim_token, claimed_at,
finished_at, error_code, created_at, updated_at
```

Test defaults, signed JSON round-trip, `ON DELETE RESTRICT` references, unique
`(access_key_id, generation)`, positive generation, action/state checks, unique
non-null claim token and one `claimed`/`uncertain` row per worker. SQLite tests
must reject invalid rows with `IntegrityError`; they do not claim concurrency.

- [x] **Step 2: Run the focused schema test and confirm RED**

Run from `backend/` with a unique directory below `.pytest_cache`:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_endpoint_schema.py -q --basetemp .pytest_cache/control-schema-red-<guid>
```

Expected: failures because the four key fields/table do not exist.

- [x] **Step 3: Add the minimal ORM model**

Use existing SQLAlchemy types (`String(36)` for UUID strings and `JSON` for the
snapshot). Add named constraints:

```python
ck_vpn_access_key_operation_generation
uq_vpn_control_operation_key_generation
fk_vpn_control_operation_endpoint_worker
ck_vpn_control_operation_generation
ck_vpn_control_operation_action
ck_vpn_control_operation_state
```

Add indexes for `state`, `worker_id`, `access_key_id`, unique `claim_token`, and
a partial unique index on `worker_id` where state is `claimed` or `uncertain`.
Do not add relationships, cascade deletion, lease expiry or retry counters.

- [x] **Step 4: Add the PostgreSQL migration and migration tests**

Append idempotent statements to `VPN_ENDPOINT_MIGRATIONS`: four `ADD COLUMN IF
NOT EXISTS`, the operation table, schema-local named constraints and indexes.
The partial reservation index must be:

```sql
CREATE UNIQUE INDEX IF NOT EXISTS uq_vpn_control_operations_worker_reserved
ON vpn_control_operations(worker_id)
WHERE state IN ('claimed','uncertain')
```

Real PostgreSQL tests run the migration twice from a legacy schema and a fresh
metadata schema. They verify old UUID/URI/status/endpoint rows byte-for-byte,
NULL-versus-empty `panel_sub_id`, defaults, catalog parity, exact FK delete
actions and cross-schema decoys.

- [x] **Step 5: Run GREEN verification and commit**

Run schema + migration tests (PostgreSQL tests skip locally without the explicit
safe test URL), Ruff on changed files, and `git diff --check`. Commit only Task 1:

```text
feat(vpn): persist endpoint control operations
```

Evidence: commits `ab78ede`, `dec5723`, `1ddd5bf`; independent PostgreSQL
verification `47 passed`; temporary synthetic cluster removed, listener closed,
control service remained active; spec and code-quality reviews approved.

## Task 2: Immutable request derivation and transactional staging

**Files:**
- Create: `backend/app/services/vpn_control_intents.py`
- Create: `backend/tests/test_vpn_control_intents.py`

- [ ] **Step 1: Write failing pure derivation tests**

Define static `VpnControlIntentError(code)` and test:

```python
def build_control_request(
    access_key: VpnAccessKey,
    subscription: VpnSubscription,
    endpoint: VpnEndpoint,
    *,
    operation_id: UUID,
    generation: int,
    action: EndpointOperation,
    allow_create: bool,
    allow_shared_restart: bool,
) -> VpnNodeRequest: ...

def build_control_config_uri(request: VpnNodeRequest) -> str: ...
```

The request uses only persisted values: strict UUID, `verified_client_email`,
`panel_sub_id`, endpoint snapshot, UTC expiry milliseconds, GiB→bytes and stable
creation milliseconds. Reject NULL identity fields, overflow, unsupported TLS/
none creation and a revoke-sticky key. Existing empty legacy subId remains empty
but cannot be used with `allow_create=True`. The REALITY URI uses the exact UUID,
host, port, `security=reality`, transport type, SNI, public key, short ID,
fingerprint and flow; no display fragment is stored. Values are percent-encoded
with `urllib.parse`, never hand-concatenated without validation.

- [ ] **Step 2: Run the pure tests and confirm RED**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_control_intents.py -q -k 'build_control' --basetemp .pytest_cache/control-request-red-<guid>
```

Expected: import/function failures.

- [ ] **Step 3: Implement the pure functions minimally**

Reuse `VpnEndpointTarget`, `VpnNodeRequest`, `serialize_node_request` and
`node_request_digest`. Do not duplicate node-request validation, add a URI
library or import application settings/network code.

- [ ] **Step 4: Write failing staging tests**

Define:

```python
async def stage_vpn_control_operation(
    db: AsyncSession,
    access_key_id: int,
    action: EndpointOperation,
    *,
    now: datetime | None = None,
) -> VpnControlOperation: ...
```

Tests prove it:

- locks and refreshes customer, subscription, key, worker, endpoint and prior
  operations in that order, with ordered key/operation queries;
- performs no commit, SSH, panel HTTP or application-setting read;
- refuses an unbound/mismatched/unverified/unsupported key;
- permits new creation only on a `ready` REALITY endpoint with no existing URI;
- permits resume on `ready` or `draining`, and disable on ready/draining/disabled;
- derives the desired action from persisted policy and rejects provision when the
  subscription/customer is not currently usable;
- increments generation exactly once, sets `revoke_requested_at` before staging
  revoke, and never clears it during later provision attempts;
- sets key status to `pending_sync`, `pending_suspend` or `pending_revoke`;
- supersedes only older queued rows for the same key; claimed/uncertain rows remain
  immutable and continue to reserve the worker;
- stores an exact detached serialized request/digest, safe static error codes and
  never includes `config_uri`, SSH/panel credentials or arbitrary exception text.

`allow_create` is derived as provision with no URI; `allow_shared_restart` is true
only for suspend/revoke under the already approved closed-beta policy.

- [ ] **Step 5: Implement staging and run GREEN**

Use `select(...).with_for_update().execution_options(populate_existing=True)` for
every authoritative row. Begin from a non-locking ID lookup only to discover the
customer ID, then revalidate every relationship after locks. Call `flush()` but
never `commit()`; the caller owns durability before any future network dispatch.

- [ ] **Step 6: Verify and commit**

Run the full new SQLite test file, relevant endpoint/subscription/customer tests,
Ruff and diff check. Commit:

```text
feat(vpn): stage durable endpoint intentions
```

## Task 3: PostgreSQL-safe claim, reservation and generation-aware finalization

**Files:**
- Modify: `backend/app/services/vpn_control_intents.py`
- Modify: `backend/tests/test_vpn_control_intents.py`
- Create: `backend/tests/test_vpn_control_intents_postgres.py`

- [ ] **Step 1: Write failing claim/finalize unit tests**

Define:

```python
async def claim_next_vpn_control_operation(
    db: AsyncSession,
    *,
    claim_token: UUID,
    now: datetime | None = None,
) -> VpnControlOperation | None: ...

async def finalize_vpn_control_operation(
    db: AsyncSession,
    operation_id: UUID,
    claim_token: UUID,
    *,
    receipt_state: Literal['observed','failed','uncertain','stale','blocked'],
    error_code: str | None,
    now: datetime | None = None,
) -> VpnControlOperation: ...

async def active_vpn_control_worker_ids(db: AsyncSession) -> set[int]: ...
```

Claim uses `FOR UPDATE SKIP LOCKED`, oldest generation order, ignores workers
reserved by claimed/uncertain rows, refreshes key generation/revoke intent, marks
stale queued rows superseded and assigns the caller token. It never commits and
never reclaims by elapsed time.

Finalize first reads IDs without a lock, then locks in the canonical order and
locks the operation last. It requires the exact claim token. `observed` changes
the key only when operation generation and current desired intent still match:
provision→active plus derived URI, suspend→suspended, revoke→revoked plus timestamp.
Old results become `superseded` and never alter the key. `failed` leaves the
current key pending with a static safe code. `uncertain` and reconciliation-
required `blocked` become `uncertain`, keep the worker reserved and cannot be
automatically retried. Terminal replay with the same token is idempotent; a
different token is rejected.

- [ ] **Step 2: Run focused tests and confirm RED**

Expected failures: missing claim/finalize/reservation functions.

- [ ] **Step 3: Implement the minimal state machine**

Keep SQLAlchemy queries in the existing service; do not add a repository,
background scheduler, lease, retry framework or SSH adapter. Accept only static
allowlisted receipt/error codes from the node contract.

- [ ] **Step 4: Add real PostgreSQL concurrency tests**

Using the existing validated `VPN_PORTAL_TEST_PG_URL` fixture and independent
backend PIDs, prove:

1. two claimers cannot claim the same operation;
2. two operations on one worker cannot both become claimed;
3. different workers can be claimed concurrently;
4. a rolled-back claim is claimable again;
5. a newer revoke staged while provision is claimed prevents the old observed
   finalizer from activating or writing a URI;
6. archive/suspend versus provision finalization preserves the newer intent;
7. uncertain remains reserved across later timestamps and process/session changes;
8. endpoint disable after staging prevents provision finalization but does not
   turn a confirmed revoke into a provision;
9. migration is idempotent with queued/claimed/history rows present.

Use `pg_backend_pid()` and `pg_blocking_pids()` where waiting is part of the claim;
events alone are not proof of database locking.

- [ ] **Step 5: Run GREEN verification and commit**

Run new SQLite tests, actual PostgreSQL tests in the isolated approved cluster,
all endpoint/lifecycle/subscription/decommission tests, Ruff and diff check. Stop
and delete only the exact synthetic cluster after the run. Commit:

```text
feat(vpn): claim and finalize durable control intents
```

## Task 4: Independent review and release-boundary evidence

**Files:**
- Modify: `docs/veltrix-endpoint-api-findings.md`
- Modify: `docs/superpowers/plans/2026-09-21-vpn-control-intent-queue.md`

- [ ] **Step 1: Spec review**

Verify schema, stage, claim and finalize against the secure-endpoint design and
the exact task text. Reject any hidden network call, auto-reclaim of uncertainty,
stale success mutation, secret logging or legacy production cutover.

- [ ] **Step 2: Code-quality review**

Review query lock order, PostgreSQL/SQLite differences, migration catalog parity,
error sanitization, URI correctness and test validity. Fix important findings and
rerun review.

- [ ] **Step 3: Full verification**

Run the complete backend suite with actual isolated PostgreSQL, whole-backend
Ruff and `git diff --check`. Record exact pass/skip counts and cleanup facts.

- [ ] **Step 4: Document the boundary**

State plainly that the queue is locally implemented but not yet dispatched over
SSH or deployed; TCP 443, protected inbound, friend invitations and payments are
unchanged. The next plan must add strict host-key-pinned transport and node module
deployment before any API/lifecycle path can enqueue production work.
