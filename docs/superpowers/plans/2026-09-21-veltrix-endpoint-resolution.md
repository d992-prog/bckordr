# Veltrix Recorded Endpoint Resolution and Legacy Safety Barrier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Resolve a bound profile exclusively through its recorded endpoint and prevent legacy mutation code from corrupting endpoint-bound identities.

**Architecture:** A small read-only resolver returns an immutable addressing snapshot or a static error code. Existing mutators remain compatible with unbound production records, but explicitly refuse bound records until the exact-endpoint remote adapter and durable intents are implemented. This is an intermediate safety gate, not a deployable endpoint-aware release.

**Tech Stack:** Python, SQLAlchemy async, pytest, real SQLite for local integration tests.

---

## Scope and evidence

Approved parent design: `docs/superpowers/specs/2026-09-21-veltrix-secure-vpn-endpoints-design.md`.
Starting revision `9d6f28e`, existing worktree `.worktrees/veltrix-customer-portal`.
Run Python commands from **backend**, not repository root: the shared environment
has another editable `app` package and root-level imports can select it.
Backend policy/provision baseline: 27 passed. Preserve the pre-existing generated
frontend/tsconfig.tsbuildinfo change. No package installation or server action.

Source inspection established that changing only the payload inbound is unsafe:
legacy remote revoke deletes normalized clients/attachments globally by UUID OR
email; provision/suspend can update global clients. The old revoke restart path
also does not reliably confirm restart success. Therefore this increment must
NOT send bound profiles into those scripts, even after local resolution.

The resolver is only recorded-state validation. It does not establish live inbound
identity, SSH trust, subscription authorization, transaction durability, or remote
mutation safety. Those are explicit subsequent requirements of the approved design.
No automatic backfill, endpoint creation, invite, new port, or deployment here.
Before any endpoint-aware release, replace the temporary unbound compatibility
path with verified legacy binding / fail-closed unresolved records, and implement
durable operation intents, exact remote identity checks and hot API operations.

## File boundaries

- Create `backend/app/services/vpn_endpoints.py`: immutable target, static errors,
  non-mutating recorded-binding resolver and lifecycle admission policy.
- Create `backend/tests/test_vpn_endpoints.py`: policy matrix and real DB lookup.
- Modify `backend/app/services/vpn_provisioning.py`: barrier before all identity
  changes/remote commands, direct UUID helper protection, revoked suspend no-op.
- Modify `backend/app/services/worker_decommission.py`: reject retirement while
  non-revoked bound keys exist; preserve their credentials and recovery ability.
- Create `backend/tests/test_vpn_endpoint_legacy_barrier.py`: real DB integration
  tests with only SSH mocked, plus decommission tests and direct UUID guard.

## Task 1: Read-only recorded endpoint resolution

- [ ] Write a missing-contract RED test before creating the module:

```python
from importlib.util import find_spec

def test_recorded_endpoint_resolver_is_available():
    assert find_spec("app.services.vpn_endpoints") is not None
```

Run `.\.venv\Scripts\python.exe -m pytest tests/test_vpn_endpoints.py -q`;
expect the assertion to fail, not collection/import failure.

- [ ] Implement the following contract in `vpn_endpoints.py`.

```python
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import VpnAccessKey, VpnEndpoint, WorkerNode

EndpointOperation = Literal["provision", "suspend", "revoke"]

class VpnEndpointError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)

@dataclass(frozen=True, slots=True)
class VpnEndpointTarget:
    endpoint_id: int
    worker_id: int
    inbound_id: int
    public_host: str
    port: int
    protocol: str
    transport: str
    security: str
    server_name: str | None
    public_key: str | None
    short_id: str | None
    fingerprint: str | None
    flow: str | None

def require_bound_client_uuid(access_key: VpnAccessKey) -> UUID:
    try:
        return UUID(access_key.external_uuid or "")
    except (ValueError, TypeError, AttributeError):
        raise VpnEndpointError("vpn_endpoint_client_identity_invalid") from None

async def resolve_recorded_endpoint(
    db: AsyncSession,
    access_key: VpnAccessKey,
    *,
    worker: WorkerNode | None,
    operation: EndpointOperation,
) -> VpnEndpointTarget:
    """Resolve recorded state only; caller must separately authorize/lock/verify remotely."""
    if operation not in {"provision", "suspend", "revoke"}:
        raise VpnEndpointError("vpn_endpoint_operation_invalid")
    if access_key.endpoint_id is None:
        raise VpnEndpointError("vpn_endpoint_binding_required")
    if worker is None or worker.id != access_key.worker_id:
        raise VpnEndpointError("vpn_endpoint_worker_mismatch")
    if worker.archived_at is not None:
        raise VpnEndpointError("vpn_endpoint_worker_archived")
    # No implicit flush: failed resolution must not persist caller mutations.
    with db.no_autoflush:
        endpoint = await db.scalar(select(VpnEndpoint).where(
            VpnEndpoint.id == access_key.endpoint_id
        ).execution_options(populate_existing=True))
    if endpoint is None:
        raise VpnEndpointError("vpn_endpoint_not_found")
    if endpoint.worker_id != worker.id:
        raise VpnEndpointError("vpn_endpoint_worker_mismatch")
    if endpoint.verified_at is None:
        raise VpnEndpointError("vpn_endpoint_unverified")
    if endpoint.status not in {"ready", "draining", "disabled"}:
        raise VpnEndpointError("vpn_endpoint_unavailable")
    if operation == "provision":
        if access_key.status in {"revoked", "pending_revoke"} or access_key.revoked_at is not None:
            raise VpnEndpointError("vpn_endpoint_key_revoked")
        if endpoint.status == "disabled":
            raise VpnEndpointError("vpn_endpoint_disabled")
        if endpoint.status == "draining" and not access_key.config_uri:
            raise VpnEndpointError("vpn_endpoint_existing_profile_required")
    if endpoint.security not in {"none", "tls", "reality"}:
        raise VpnEndpointError("vpn_endpoint_transport_invalid")
    if endpoint.status == "ready" and endpoint.security == "none":
        raise VpnEndpointError("vpn_endpoint_transport_invalid")
    if endpoint.protocol != "vless" or endpoint.transport not in {"tcp", "raw"}:
        raise VpnEndpointError("vpn_endpoint_transport_unsupported")
    if (not isinstance(endpoint.inbound_id, int) or isinstance(endpoint.inbound_id, bool)
            or endpoint.inbound_id < 1 or not isinstance(endpoint.port, int)
            or isinstance(endpoint.port, bool) or not 1 <= endpoint.port <= 65535
            or not endpoint.public_host or endpoint.public_host != endpoint.public_host.strip()
            or any(character.isspace() or character in "/?#@" for character in endpoint.public_host)):
        raise VpnEndpointError("vpn_endpoint_address_invalid")
    if access_key.protocol != endpoint.protocol:
        raise VpnEndpointError("vpn_endpoint_protocol_mismatch")
    require_bound_client_uuid(access_key)
    return VpnEndpointTarget(**{
        name: getattr(endpoint, "id" if name == "endpoint_id" else name)
        for name in VpnEndpointTarget.__dataclass_fields__
    })
```

This snapshot intentionally carries public metadata only. Full supported-version
REALITY/TLS parameter validation belongs to the remote adapter, which must not
accept this result as a complete verification certificate. Do not create an
endpoint ORM relationship or read worker VPN defaults. Do not mutate keys/rows,
commit, obtain locks, perform SSH or generate UUIDs in this resolver.

- [ ] Add RED/GREEN behavior tests using actual in-memory SQLite sessions, valid
  seeded worker/customer/subscription/endpoints and synthetic UUID/URI. Use detached
  access-key objects for mismatch/missing-binding tests rather than corrupting DB FKs.
  A core test must contain these assertions:

```python
before = (key.external_uuid, key.config_uri, key.worker_id, key.endpoint_id, key.status)
worker.vpn_inbound_id = 999
worker.vpn_public_host = "changed.example"
worker.vpn_inbound_port = 1234
target = await resolve_recorded_endpoint(db, key, worker=worker, operation="provision")
assert (target.inbound_id, target.public_host, target.port) == (10, "vpn.example", 8443)
assert before == (key.external_uuid, key.config_uri, key.worker_id, key.endpoint_id, key.status)
```

Include two endpoints on one worker (old draining/none and new ready/reality),
changed/cleared worker defaults, frozen snapshot fields, each static error branch,
all status/operation combinations, no URI on draining issue, revoked/pending revoke,
valid UUID preserved and invalid/missing UUID rejected without leaking URI/UUID in
errors. Test stale identity-map refresh using a second committed session; test no
autoflush does not write unrelated dirty worker values during resolution. SQLite
tests prove recorded resolution only, not inter-process locking or live transport.

- [ ] Run focused tests and configured Ruff from backend; self-review, commit only
  the two files as `feat(vpn): resolve recorded endpoint identity without defaults`.
  Specification review then quality review before Task 2.

## Task 2: Prevent legacy operations from mutating bound identities

- [ ] RED: with real SQLite seed a verified bound key on inbound10 and worker default
  inbound99; mock only `execute_worker_ssh_commands` to count calls and fail if used.
  Parameterize provision/suspend/revoke. Snapshot external_uuid, config_uri, issued_at,
  expires_at, revoked_at, endpoint_id, worker_id. After each call assert no SSH,
  unchanged identity, and pending action with a static error. Start with existing
  code so this assertion fails because SSH is called / identity is regenerated.

- [ ] Import resolver/error/UUID validation into `vpn_provisioning.py`. Add this
  helper (forward references to the existing sanitize helper are unnecessary):

```python
async def _hold_bound_key_for_endpoint_adapter(db, access_key, worker, operation):
    if access_key.endpoint_id is None:
        return False
    error_code = "vpn_endpoint_remote_adapter_required"
    try:
        await resolve_recorded_endpoint(db, access_key, worker=worker, operation=operation)
    except VpnEndpointError as exc:
        error_code = exc.code
    access_key.status = {
        "provision": "pending_sync", "suspend": "pending_suspend", "revoke": "pending_revoke",
    }[operation]
    access_key.last_error = error_code
    access_key.updated_at = utcnow()
    return True
```

Use explicit type annotations. A provision request on an already revoked or pending
revoke key must never replace its permanent-revoke status: preserve status and
revoked_at, set only static error/updated_at. Suspend of such a key is a no-op.
Revoke of already revoked is a no-op (existing behavior). These checks precede
the helper. Generic DB errors propagate to normal caller handling; never convert
an unavailable database into permission to execute the legacy path.

In `ensure_vpn_client_uuid`, before any existing behavior, insert:

```python
if access_key.endpoint_id is not None:
    return require_bound_client_uuid(access_key)
```

In each async mutator, resolve worker as currently done, then call the new helper
and return immediately if it returned True, **before** status timestamps, UUID/URI,
subscription expiry or remote-command construction are changed. No success events
or worker-health changes on the barrier path. No fallback/catch which calls SSH.
Unbound paths keep current behavior until the later verified migration gate.

Direct `build_vpn_client_revoke_command` and `build_vpn_client_suspend_command` must
also reject bound keys before creating a command:

```python
if access_key.endpoint_id is not None:
    raise VpnEndpointError("vpn_endpoint_remote_adapter_required")
```

The provision command builder does not accept a key; guard its caller before UUID
or payload construction. Do not extend old script payloads to pretend they are
endpoint-aware, and do not dispatch bound keys into any legacy remote script.

In `decommission_worker`, after loading keys but before changing them or worker:

```python
if any(key.endpoint_id is not None for key in keys):
    raise WorkerDecommissionConflictError(
        "Endpoint-bound VPN profiles must be remotely revoked before node removal"
    )
```

Also include endpoint-bound keys in non-revoked unknown statuses when collecting
the guard candidates: use an independent `SELECT id ... endpoint_id IS NOT NULL
AND status != 'revoked' LIMIT 1` under the locked worker before mutations, instead
of assuming DEVICE_SLOT_STATUSES is exhaustive. Keep existing unbound retirement
behavior intact. Final disabled endpoint policy is a later release concern.

- [ ] Extend tests: invalid/missing UUID never regenerated, no changed URI/times,
  unbound baseline retained, disabled bound revoke remains pending not false success,
  resolver mismatch surfaced safely, no worker default needed, manual revoke remains
  final on direct provision/suspend, direct builders reject, decommission refuses
  active/pending/unknown bound records without clearing SSH secrets or profiles,
  confirmed-revoked bound history does not block retirement. No raw credential in
  event/errors. Existing test files remain the unbound compatibility regression.
- [ ] Run `python -m pytest tests/test_vpn_endpoints.py tests/test_vpn_endpoint_legacy_barrier.py tests/test_vpn_provisioning.py tests/test_vpn_policy.py tests/test_vpn_lifecycle.py tests/test_vpn_subscription_sync.py tests/test_worker_decommission.py -q` from backend using its venv.
  Check actual decommission test filename before running. Run configured Ruff.
  Commit only Task 2 code/tests as `fix(vpn): block legacy mutations for bound profiles`.
  Specification review then quality review; fix and re-review findings.

## Final verification and handoff

- [ ] Run full backend suite from backend and record optional PostgreSQL skips
  honestly; this increment does not introduce lock/transaction/concurrency claims.
  Existing real PostgreSQL migration proof is recorded in the preceding plan.
- [ ] Independent final integrated review, `git diff --check`, configured Ruff.
- [ ] Record exact commits/results and unimplemented runtime/verification pieces in
  `docs/current-state.md`. Preserve worktree; do not deploy this barrier-only version
  or create production endpoint rows. No temporary server DB is needed here.
