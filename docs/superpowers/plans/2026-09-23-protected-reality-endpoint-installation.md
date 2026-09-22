# Protected REALITY Endpoint Installation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a temporary token-only node CLI and an atomic control-side registration service for one externally accepted protected REALITY endpoint.

**Architecture:** The node module owns strict stdin/stdout, exact 3x-UI mutation and public-only receipts; it is deliberately absent from the permanent dispatcher bundle. A separate deployment helper builds a deterministic disposable zipapp and invokes it through pinned SSH with one fixed command. The admin module validates that receipt, stages one database row, and atomically promotes it with the exact release marker after digest-bound external acceptance.

**Tech Stack:** Python 3.11 stdlib, SQLAlchemy asyncio, pytest/pytest-asyncio, existing node panel transport.

---

### Task 1: Node request, receipt, and key boundary

**Files:**
- Create: `backend/app/services/vpn_reality_endpoint_installer.py`
- Create: `backend/tests/test_vpn_reality_endpoint_installer.py`

- [x] **Step 1: Write failing contract tests**

Add tests that import the missing module, parse only the exact version-1 request,
reject duplicate/unknown/oversized input, validate canonical host/SNI/short ID, and
assert the frozen receipt exposes only public endpoint fields plus its digest.

- [x] **Step 2: Run RED**

Run: `python -m pytest backend/tests/test_vpn_reality_endpoint_installer.py -q`
Expected: collection/import failure for the missing module.

- [x] **Step 3: Implement minimal strict types and parsing**

Implement `EndpointInstallRequest`, `EndpointInstallReceipt`, static
`EndpointInstallError`, strict JSON helpers, public receipt hashing/encoding, and
fixed constants for port/protocol/transport/security/fingerprint/flow.

- [x] **Step 4: Run GREEN**

Run the same focused pytest command. Expected: contract tests pass.

### Task 2: Exact node mutation and guarded inverse

**Files:**
- Modify: `backend/app/services/vpn_reality_endpoint_installer.py`
- Modify: `backend/tests/test_vpn_reality_endpoint_installer.py`

- [x] **Step 1: Write failing behavior tests**

Use a fake token-only panel and temporary complete inventory DB to assert: exact
empty-endpoint idempotency; port conflict refusal; trusted bounded `xray x25519`
parsing; exact add payload; reread after add; inspect-only behavior; removal refused
with embedded or attached clients; exact delete and absence reread; optional
disposable acceptance UUID/email add and exclusive cleanup. Put secret
sentinels in keys, panel responses, and exceptions and assert none enter receipts,
stdout, stderr, or formatted public exceptions.

- [x] **Step 2: Run RED**

Run the focused module tests. Expected: missing ensure/inspect/remove behavior.

- [x] **Step 3: Implement minimal node service and CLI**

Add a one-time-only panel adapter over the existing loopback transport, complete
inventory checks against the local 3x-UI database, strict endpoint matching,
bounded Xray subprocess execution, exact inbound create/delete, and root/private-file
CLI gates. Never add the module to `vpn_node_bundle.BUNDLE_MEMBERS`.

- [x] **Step 4: Run GREEN**

Run the focused module tests. Expected: all pass with no production IO.

### Task 3: Transactional staging and release promotion

**Files:**
- Create: `backend/app/services/vpn_reality_endpoint_registration.py`
- Create: `backend/tests/test_vpn_reality_endpoint_registration.py`

- [x] **Step 1: Write failing database tests**

Create isolated async database tests for staged creation, exact idempotency, worker
archive/mismatch, endpoint/port/ready conflicts, strict receipt parsing, UTC external
acceptance, receipt/release/evidence digest validation, ready promotion, exact marker
creation/confirmation, different-marker refusal, and rollback atomicity.

- [x] **Step 2: Run RED**

Run: `python -m pytest backend/tests/test_vpn_reality_endpoint_registration.py -q`
Expected: import failure for the missing registration module.

- [x] **Step 3: Implement the admin service**

Add frozen `ExternalEndpointAcceptance`, `stage_protected_endpoint`, and
`promote_protected_endpoint`. Use `SELECT ... FOR UPDATE` with `populate_existing`,
stable worker/endpoint/marker order, caller-owned commit semantics, exact public-field
comparison, and static errors without secret-bearing values.

- [x] **Step 4: Run GREEN**

Run the focused database tests. Expected: all pass.

### Task 4: Deterministic disposable bundle and strict SSH runner

**Files:**
- Create: `backend/app/services/vpn_reality_endpoint_deployment.py`
- Create: `backend/tests/test_vpn_reality_endpoint_deployment.py`

- [x] **Step 1: Write failing deployment-boundary tests**

Assert byte-for-byte deterministic minimal bundles, isolated import-probe success,
permanent dispatcher exclusion, pinned-host loopback SSH, exact fixed command and
stdin/stdout, timeout bounds, static errors, and uncertain classification after a
mutating process has been created. Add local candidate-admin tests for exclusive
bounded install, root:0600 mode on POSIX, exact hash/probe, idempotency, foreign-target
refusal, parent fsync behavior, and exact-hash removal after read-only inspection.

- [x] **Step 2: Implement the minimal builder and runner**

Build, compile, probe, fsync, and atomically publish the candidate. Reuse the strict
transport snapshot/options without changing deployer or dispatcher files. Execute
only the fixed candidate path, disable PTY, validate the canonical receipt against
the request, and never surface remote stderr or exception text. Add a second fixed
isolated command which receives the bounded bundle on stdin, uses an exclusive temp
and atomic hard link without overwriting mismatches, and removes only the expected
hash after a successful exact `inspect` action.

- [x] **Step 3: Run GREEN**

Run the three focused endpoint suites together. Expected: all pass without remote or
production IO.

### Task 5: Regression and handoff verification

**Files:**
- Modify only the new files above if verification exposes a defect.

- [x] **Step 1: Run focused suites together**

Run: `python -m pytest backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_registration.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_control_runtime.py backend/tests/test_vpn_friend_invitations.py -q`
Expected: all pass.

- [x] **Step 2: Run lint and diff safety checks**

Run: `python -m ruff check backend/app/services/vpn_reality_endpoint_installer.py backend/app/services/vpn_reality_endpoint_registration.py backend/app/services/vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_registration.py backend/tests/test_vpn_reality_endpoint_deployment.py`
Run: `git diff --check`
Expected: both pass; permanent dispatcher/deployer files are absent from the diff.

- [ ] **Step 3: Commit the focused change**

Stage only the two docs, three new service modules, and three new test modules. Commit
with `feat: add protected reality endpoint installer` while preserving unrelated
worktree changes, including `frontend/tsconfig.tsbuildinfo` and node-journal edits.

## Self-review

- Spec coverage: node trust boundary, exact endpoint, idempotent inspect, guarded
  inverse, public receipt, deterministic candidate execution, staged/ready
  transaction, acceptance digest, and release marker each map to Tasks 1-4.
- Placeholder scan: no production behavior is deferred inside the promised scope;
  future deployment/execution is explicitly outside it.
- Type consistency: both modules share the canonical receipt JSON schema and digest;
  admin acceptance binds that exact digest and release ID.
