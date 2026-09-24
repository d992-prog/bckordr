# Veltrix Strict Fleet Health Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically refresh safe health for every registered VPN endpoint through pinned strict transport, without per-node code, implicit restart, or the legacy `known_hosts=None` path.

**Architecture:** Add a separate read-only node health protocol and entrypoint to the existing deterministic node bundle, transport it through the existing pinned-host SSH snapshot, and schedule one fleet probe at a time from control runtime. Successful probes refresh existing worker/endpoint health; failures only block new public allocations and create bounded events.

**Tech Stack:** Python 3.11+, asyncssh strict transport, SQLAlchemy/PostgreSQL, existing deterministic zipapp bundle and control runtime.

---

### Task 1: Define the immutable health protocol

**Files:**
- Create: `backend/app/services/vpn_node_health.py`
- Create: `backend/tests/test_vpn_node_health.py`

- [ ] **Step 1: Write failing parse/serialize tests**

Cover exact fields, canonical JSON, size limit, timestamp bounds, hostname and
REALITY validation, unknown-field rejection and secret-free `repr`.

- [ ] **Step 2: Implement request and receipt types**

```python
@dataclass(frozen=True, slots=True)
class VpnNodeHealthRequest:
    worker_id: int
    target: VpnEndpointTarget = field(repr=False)
    checked_at_ms: int

@dataclass(frozen=True, slots=True)
class VpnNodeHealthReceipt:
    state: Literal["healthy", "unhealthy"]
    error_code: str | None
    runtime: Literal["running", "stopped", "error"] | None
```

Use exact canonical JSON with `version=1`; never include panel credentials,
client UUIDs or access URIs.

- [ ] **Step 3: Run the protocol tests**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_node_health.py -q
```

- [ ] **Step 4: Commit**

```powershell
git add backend/app/services/vpn_node_health.py backend/tests/test_vpn_node_health.py
git commit -m "feat(vpn): define strict node health protocol"
```

### Task 2: Add read-only endpoint observation to the node bundle

**Files:**
- Modify: `backend/app/services/vpn_xui_node_observation.py`
- Create: `backend/app/services/vpn_node_health_entrypoint.py`
- Modify: `backend/app/services/vpn_node_bundle.py`
- Create: `backend/tests/test_vpn_node_health_entrypoint.py`
- Modify: `backend/tests/test_vpn_node_bundle.py`

- [ ] **Step 1: Write failing node-local observation tests**

Use a disposable SQLite 3x-UI database and loopback fake panel. Cover running
matching REALITY, missing inbound, stopped Xray, transport mismatch, oversized
input and empty stdout on malformed requests.

- [ ] **Step 2: Extract a public endpoint-only observer**

Add `observe_node_endpoint(panel, target, database_path)` beside the existing
client observer. Reuse the bounded inventory and transport validation; do not
duplicate parsers.

- [ ] **Step 3: Implement a fixed health entrypoint**

The entrypoint reads one bounded request from stdin, loads the existing private
node config/token, performs no mutation and writes exactly one bounded receipt.
Raw panel responses and exception text go nowhere.

- [ ] **Step 4: Include the module in the deterministic bundle manifest**

Update the expected manifest/hash tests; do not add runtime downloads.

- [ ] **Step 5: Run node-local tests**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_node_health_entrypoint.py backend/tests/test_vpn_node_bundle.py backend/tests/test_vpn_xui_node_observation.py -q
```

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/vpn_xui_node_observation.py backend/app/services/vpn_node_health_entrypoint.py backend/app/services/vpn_node_bundle.py backend/tests/test_vpn_node_health_entrypoint.py backend/tests/test_vpn_node_bundle.py
git commit -m "feat(vpn): observe endpoint health on nodes"
```

### Task 3: Transport health over pinned strict SSH

**Files:**
- Modify: `backend/app/services/vpn_node_transport.py`
- Modify: `backend/tests/test_vpn_node_transport.py`

- [ ] **Step 1: Write failing strict-health transport tests**

Assert raw Ed25519 `known_hosts`, fixed remote command, no agent/config/default
keys/PTY, bounded stdin/stdout, empty stderr and exact receipt parsing.

- [ ] **Step 2: Implement `execute_vpn_node_health_over_ssh`**

Reuse `VpnNodeTransportSnapshot` and the same connection construction as mutation
transport. The only remote command is the installed health entrypoint. Return a
typed receipt or one secret-free `VpnNodeTransportError` code.

- [ ] **Step 3: Run transport tests**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_node_transport.py -q
```

- [ ] **Step 4: Commit**

```powershell
git add backend/app/services/vpn_node_transport.py backend/tests/test_vpn_node_transport.py
git commit -m "feat(vpn): transport strict node health"
```

### Task 4: Implement one-at-a-time fleet probing

**Files:**
- Create: `backend/app/services/vpn_fleet_health.py`
- Modify: `backend/app/core/config.py`
- Modify: `backend/app/services/control_runtime.py`
- Modify: `backend/app/services/vpn_policy.py`
- Create: `backend/tests/test_vpn_fleet_health.py`
- Modify: `backend/tests/test_vpn_control_runtime.py`
- Modify: `backend/tests/test_vpn_policy.py`

- [ ] **Step 1: Write failing fleet-selection tests**

Cover oldest-check-first deterministic order, distinct workers,
archived/disabled skip, busy maintenance/control skip, one in-flight task,
one-node failure followed by next-cycle progress and interval validation.

- [ ] **Step 2: Add fail-closed settings**

```python
vpn_fleet_health_enabled: bool = Field(default=False, alias="VPN_FLEET_HEALTH_ENABLED")
vpn_fleet_health_interval_seconds: float = Field(
    default=120.0, ge=30.0, le=3600.0,
    alias="VPN_FLEET_HEALTH_INTERVAL_SECONDS",
)
```

- [ ] **Step 3: Implement `probe_next_vpn_endpoint`**

Select one eligible endpoint not reserved by maintenance/control work, load its
strict transport snapshot, send the health request and update only health fields.
On success set worker runtime `ready`, refresh both timestamps, clear bounded
errors and set endpoint `verified_at=now`. On failure set worker runtime `error`,
refresh its check timestamp and set endpoint `last_error_code`; keep endpoint
identity, its last successful verification, status and existing clients
unchanged, and add one `VpnNodeEvent`.

Update public endpoint selection to require a fresh endpoint `verified_at` and
an empty `last_error_code` in addition to the existing fresh worker health.
This blocks a failed endpoint immediately without revoking existing profiles.

- [ ] **Step 4: Preserve or clear external verification correctly**

Compare the stored external fingerprint with the current public fingerprint.
Clear external proof only on mismatch; ordinary health failure or node update
must not clear it.

- [ ] **Step 5: Integrate one task into `ControlRuntimeOrchestrator`**

Follow the existing lifecycle/dispatcher task pattern. Shutdown cancels and
awaits the task; exceptions log only the static code.

- [ ] **Step 6: Run focused tests and Ruff**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_fleet_health.py backend/tests/test_vpn_control_runtime.py -q
backend/.venv/Scripts/python -m ruff check backend/app backend/tests/test_vpn_fleet_health.py
```

- [ ] **Step 7: Commit**

```powershell
git add backend/app/services/vpn_fleet_health.py backend/app/core/config.py backend/app/services/control_runtime.py backend/app/services/vpn_policy.py backend/tests/test_vpn_fleet_health.py backend/tests/test_vpn_control_runtime.py backend/tests/test_vpn_policy.py
git commit -m "feat(vpn): schedule strict fleet health"
```

### Task 5: Verify and document fleet rollout

**Files:**
- Modify: `backend/.env.example`
- Modify: `docs/vpn-service.md`
- Modify: `docs/current-state.md`

- [ ] **Step 1: Document that updates remain fleet-wide**

Explain one-time node onboarding, bulk updates, strict health and why the legacy
SSH check is excluded from release readiness. The readiness panel is the single
health view; do not add a second update endpoint or per-node update workflow.

- [ ] **Step 2: Run all node/control focused tests, Ruff, frontend tests and build**

- [ ] **Step 3: Run the full backend suite and `git diff --check`**

- [ ] **Step 4: Commit docs**

```powershell
git add backend/.env.example docs/vpn-service.md docs/current-state.md
git commit -m "docs(vpn): document strict fleet health"
```
