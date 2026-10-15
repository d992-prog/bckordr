# Unified Veltrix VPN Node Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fragmented legacy VPN maintenance buttons with one resumable, Ed25519-pinned onboarding flow that creates and externally accepts a release-standard REALITY/TCP/443 endpoint.

**Architecture:** Persist only the confirmed public SSH identity on `WorkerNode`, reuse `WorkerMaintenanceJob` as the background lease, and derive onboarding state from the worker, latest job, and `VpnEndpoint`. A small node-local bootstrap zipapp installs/verifies 3x-UI and creates its API token without exporting the token; the existing node runner, endpoint installer, registration, health, and external-verification services remain the authority for the protected endpoint.

**Tech Stack:** Python 3.11, FastAPI, SQLAlchemy async/PostgreSQL, AsyncSSH, pytest, React 18, TypeScript, Vite, Node test runner.

---

## File structure

Backend responsibilities:

- Create `backend/app/services/vpn_node_trust.py`: untrusted read-only SSH probe, Ed25519 comparison, and confirmed known-host installation.
- Create `backend/app/services/vpn_node_bootstrap.py`: deterministic ephemeral root helper which installs/verifies 3x-UI, backs up its database, and creates the node-local API token/config without exporting secrets.
- Create `backend/app/services/vpn_node_onboarding.py`: state derivation, resumable orchestration, acceptance profile, cleanup, and promotion.
- Modify `backend/app/db/models.py`, `backend/app/db/migrations.py`, and `backend/app/schemas/control.py`: persist/serialize public trust and onboarding API values.
- Modify `backend/app/services/vpn_policy.py`: treat `vpn_onboard` as a VPN mutation lease.
- Modify `backend/app/services/vpn_control_intents.py`: extract one reusable REALITY URI builder instead of duplicating connection-link formatting.
- Modify `backend/app/api/routes/control.py`: six narrow admin-only onboarding endpoints and background job dispatch.
- Modify `backend/app/services/worker_maintenance.py`: stop treating bare `vpn_install` as `ready` and keep the legacy actions diagnostic-only.

Frontend responsibilities:

- Create `frontend/src/vpnNodeOnboarding.ts`: pure state-to-copy/action mapping.
- Create `frontend/src/VpnNodeOnboardingPanel.tsx`: one primary action, trust confirmation, bounded messages, and collapsed diagnostics.
- Modify `frontend/src/api.ts`, `frontend/src/App.tsx`, and `frontend/src/styles.css`: typed API, data loading, replacement of the legacy button group, and responsive styles.

Focused tests:

- Create `backend/tests/test_vpn_node_onboarding_schema.py`.
- Create `backend/tests/test_vpn_node_trust.py`.
- Create `backend/tests/test_vpn_node_bootstrap.py`.
- Create `backend/tests/test_vpn_node_onboarding.py`.
- Create `backend/tests/test_vpn_node_onboarding_api.py`.
- Create `frontend/test/vpnNodeOnboarding.test.mjs`.
- Extend existing endpoint installer, control-intent, worker-maintenance, and PostgreSQL migration tests where their contracts change.

## Task 1: Persist confirmed public SSH identity

**Files:**

- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/migrations.py`
- Modify: `backend/app/schemas/control.py`
- Create: `backend/tests/test_vpn_node_onboarding_schema.py`
- Modify: `backend/tests/test_vpn_endpoint_migrations.py`

- [ ] **Step 1: Write failing model and schema tests**

```python
def test_worker_model_persists_public_ssh_identity_only():
    worker = WorkerNode(
        name="polish-server",
        ssh_host_key="203.0.113.10 ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITest",
        ssh_host_key_fingerprint="SHA256:" + "a" * 43,
    )
    assert worker.ssh_host_key.startswith("203.0.113.10 ssh-ed25519 ")
    assert worker.ssh_host_key_fingerprint.startswith("SHA256:")


def test_worker_response_exposes_fingerprint_but_not_raw_host_key():
    fields = WorkerNodeResponse.model_fields
    assert "ssh_host_key_fingerprint" in fields
    assert "ssh_host_key" not in fields
```

Add a migration assertion that the startup migration list contains exactly:

```sql
ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS ssh_host_key TEXT NULL
ALTER TABLE worker_nodes ADD COLUMN IF NOT EXISTS ssh_host_key_fingerprint VARCHAR(64) NULL
```

- [ ] **Step 2: Run the new tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding_schema.py tests/test_vpn_endpoint_migrations.py -q
```

Expected: failures because the columns and response field do not exist.

- [ ] **Step 3: Add the two nullable columns and safe response field**

Add to `WorkerNode`:

```python
ssh_host_key: Mapped[str | None] = mapped_column(Text, nullable=True)
ssh_host_key_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
```

Add the two idempotent PostgreSQL statements to `MIGRATIONS`. Add only this field to `WorkerNodeResponse`, not `WorkerNodeBase`:

```python
ssh_host_key_fingerprint: str | None = None
```

The raw key remains server-side and cannot be supplied by the generic create/update worker forms.

- [ ] **Step 4: Run schema tests and verify GREEN**

Run the command from Step 2.

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/db/models.py backend/app/db/migrations.py backend/app/schemas/control.py backend/tests/test_vpn_node_onboarding_schema.py backend/tests/test_vpn_endpoint_migrations.py
git commit -m "feat: persist trusted VPN node host keys"
```

## Task 2: Add read-only SSH trust preflight

**Files:**

- Create: `backend/app/services/vpn_node_trust.py`
- Create: `backend/tests/test_vpn_node_trust.py`

- [ ] **Step 1: Write failing tests for exact Ed25519 identity checks**

Cover password and private-key credentials, root-only access, `ssh-ed25519` only, canonical host target, handshake/local-key equality, port inventory, changed-key refusal, bounded output, timeout, cancellation, and secret-free exceptions.

The public contract is:

```python
@dataclass(frozen=True, slots=True)
class VpnNodeTrustProbe:
    known_hosts_line: str = field(repr=False)
    fingerprint: str
    architecture: str
    xui_installed: bool
    occupied_ports: tuple[int, ...]


class VpnNodeTrustError(RuntimeError):
    code: str
```

Expose `probe_vpn_node_trust(worker, *, connector=asyncssh.connect) ->
VpnNodeTrustProbe` and `trust_vpn_node(worker, expected_fingerprint, *,
known_hosts_path, reader_gid, connector=asyncssh.connect) -> VpnNodeTrustProbe`.

Assert that `trust_vpn_node()` probes again, refuses a different fingerprint before writing, calls `install_control_known_hosts()` with one canonical literal line, and only then returns the probe.

- [ ] **Step 2: Run trust tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_trust.py -q
```

Expected: import failure for `app.services.vpn_node_trust`.

- [ ] **Step 3: Implement the minimum read-only probe**

Use the worker's exactly-one credential rule and AsyncSSH options matching `vpn_node_transport`: no config, agent, GSS, keyboard-interactive, host-based, X.509, forwarding, or PTY; `server_host_key_algs=["ssh-ed25519"]`; 10-second connect/login bounds.

Run one fixed read-only command which emits a bounded canonical JSON object with
exact keys `version`, `uid`, `architecture`, `host_key`, `xui_installed`, and
`occupied_ports`. Tests generate a real Ed25519 key with AsyncSSH and put its full
OpenSSH public value in `host_key`; no shortened/example key is accepted.

Compare `connection.get_server_host_key().export_public_key("openssh")` byte-for-byte with `/etc/ssh/ssh_host_ed25519_key.pub` from that response. Build the known-hosts target using `_known_hosts_target()`, calculate `SHA256:<base64-without-padding>` over the decoded SSH public-key blob, and collapse all failures into fixed codes:

```python
VPN_NODE_TRUST_ERRORS = frozenset({
    "vpn_node_trust_credentials_invalid",
    "vpn_node_trust_connection_failed",
    "vpn_node_trust_host_key_mismatch",
    "vpn_node_trust_environment_invalid",
})
```

Never include host, key, credential path, command, stdout, or raw exception text in `str(error)` or `repr(error)`.

- [ ] **Step 4: Run trust tests and existing strict transport tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_trust.py tests/test_vpn_control_dispatcher.py tests/test_vpn_node_deployment.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/vpn_node_trust.py backend/tests/test_vpn_node_trust.py
git commit -m "feat: verify VPN node SSH identity"
```

## Task 3: Build the node-local 3x-UI bootstrap helper

**Files:**

- Create: `backend/app/services/vpn_node_bootstrap.py`
- Create: `backend/tests/test_vpn_node_bootstrap.py`
- Reuse without changing public behavior: `backend/app/services/worker_maintenance.py`

- [ ] **Step 1: Write failing deterministic-helper tests**

Define this control-side contract:

```python
@dataclass(frozen=True, slots=True)
class VpnNodeBootstrapReceipt:
    state: Literal["prepared", "already_prepared"]
    panel_url: str
    database_path: str
    xui_version: str
```

Expose `build_vpn_node_bootstrap_bundle(source_root, target, *, worker_id,
panel_url) -> str` and `execute_vpn_node_bootstrap(snapshot, bundle, *,
expected_sha256, connector=asyncssh.connect) -> VpnNodeBootstrapReceipt`.

Tests prove identical inputs produce identical bytes, different worker IDs change the digest, the archive imports under `python -I -S`, and neither a generated token nor a password can appear in stdout/stderr/receipt/exception.

Node-local tests run the helper logic against a temporary filesystem and fake subprocesses. Assert:

- exact existing token/config is accepted idempotently;
- missing 3x-UI runs the current pinned install command generated from `XUI_INSTALL_COMMIT`, `XUI_INSTALL_SHA256`, and `XUI_VERSION`;
- unknown architecture, pre-existing inbound, foreign token/config, unsafe ownership/mode/symlink, or occupied TCP 443 fails before mutation;
- the x-ui database is backed up to a root-only regular file before token creation;
- token creation uses `setting -getApiToken=true -tokenName <unique-name>` and never direct SQL or password reset;
- an ambiguous token attempt writes a private reconciliation marker and blocks automatic retry;
- success writes `api-token` mode 0600 and canonical `node.json` beneath mode-0700 directories, fsyncs, verifies token-mode status, and emits only the public receipt.

- [ ] **Step 2: Run bootstrap tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_bootstrap.py -q
```

Expected: import failure for the new module.

- [ ] **Step 3: Implement the deterministic helper and fixed SSH executor**

Build one zipapp whose generated entrypoint contains the already-reviewed 3x-UI install/ownership/Xray-compatibility commands from `worker_maintenance.py` as fixed source, plus dependency-free token/config preparation logic. The remote executor accepts only:

```text
<64-lowercase-hex-sha256>\n
<decimal-size>\n
<exact-zipapp-bytes>
```

It writes an exclusive root-only temporary file under `/var/lib/veltrix-vpn`, verifies the digest, runs `/usr/bin/python3 -I -S <temporary>`, caps stdout/stderr, and removes the helper. Treat any disconnect after process creation as `vpn_node_bootstrap_mutation_uncertain`; a retry must inspect the private marker rather than create another API token.

Return only the canonical public receipt. Keep the token in node memory and node filesystem; it must never cross SSH.

- [ ] **Step 4: Run bootstrap, maintenance, and token-session tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_bootstrap.py tests/test_worker_maintenance.py tests/test_vpn_xui_node_http.py tests/test_vpn_node_deployment.py -q
```

Expected: all selected tests pass and legacy install command digests remain unchanged.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/services/vpn_node_bootstrap.py backend/tests/test_vpn_node_bootstrap.py
git commit -m "feat: prepare VPN nodes without exporting secrets"
```

## Task 4: Implement resumable endpoint staging

**Files:**

- Create: `backend/app/services/vpn_node_onboarding.py`
- Create: `backend/tests/test_vpn_node_onboarding.py`
- Modify: `backend/app/services/vpn_policy.py`
- Modify: `backend/app/services/worker_maintenance.py`
- Modify: `backend/tests/test_worker_maintenance.py`

- [ ] **Step 1: Write failing orchestration tests**

Define the safe view and stage contract:

```python
@dataclass(frozen=True, slots=True)
class VpnNodeOnboardingView:
    worker_id: int
    state: Literal[
        "needs_preflight", "needs_trust", "ready_to_start", "queued",
        "running", "awaiting_test", "ready", "failed",
    ]
    fingerprint: str | None
    job_id: int | None
    endpoint_id: int | None
    error_code: str | None
```

Expose `get_vpn_node_onboarding_view(session, worker_id) ->
VpnNodeOnboardingView` and `run_vpn_node_onboarding_job(job_id, *,
session_factory, bootstrap, deploy_node, install_endpoint_helper,
execute_endpoint, health_transport) -> None`; production defaults are the real
services named in the signature description, while tests inject fakes.

Tests cover exact repeats, restart after each stage, changed pin, port conflict, foreign inbound, active attack/control/maintenance lease, archived/disabled worker, bootstrap uncertainty, endpoint mutation uncertainty followed by inspect, endpoint-registration conflict, and safe fixed error persistence.

Assert the happy path calls in order:

```text
bootstrap -> node bundle/helper deploy -> worker-bound endpoint helper install
-> ensure REALITY/443 -> stage endpoint -> strict staged health
```

Assert the result is `awaiting_test`, `VpnEndpoint.status == "staged"`, `max_active_profiles == 20`, health is fresh, and no customer profile can select it.

- [ ] **Step 2: Run orchestration tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding.py tests/test_worker_maintenance.py -q
```

Expected: import and missing-action failures.

- [ ] **Step 3: Add `vpn_onboard` to the existing mutation lease**

Add `"vpn_onboard"` to `VPN_MUTATION_ACTIONS`, maintenance labels, and running-status mapping. Do not route it through `execute_worker_ssh_commands()`; `run_vpn_node_onboarding_job()` owns the strict flow.

Change the legacy completion branch so `vpn_install` ends as `needs_config`, never `ready`:

```python
if job.action == "vpn_install":
    worker.vpn_runtime_status = "needs_config"
    worker.vpn_last_error = None
```

Other legacy maintenance behavior remains unchanged and available only under diagnostics.

- [ ] **Step 4: Implement state derivation and staging orchestrator**

Use the exact confirmed `worker.ssh_host_key`, `VPN_CONTROL_KNOWN_HOSTS_PATH`, and `load_transport_snapshot()`. Build artifacts in a private `TemporaryDirectory`, using:

```python
source_root = Path(__file__).resolve().parents[2]
```

Derive a stable 16-hex short ID from HMAC-SHA256 of `SESSION_SECRET_KEY` and `f"vpn-endpoint:{worker.id}:{public_host}"`; reject the development default secret in non-test onboarding. Create:

```python
EndpointInstallRequest(
    action="ensure",
    worker_id=worker.id,
    public_host=worker.vpn_public_host or worker.ssh_host or worker.ip_address,
    server_name=REALITY_SERVER_NAME,
    short_id=short_id,
)
```

After `stage_protected_endpoint()`, set capacity 20 only when it is `None`. Run `VpnNodeHealthRequest` directly through the strict transport so staged endpoints are checked immediately. Store only codes such as `vpn_onboarding_host_key_changed`, `vpn_onboarding_port_conflict`, and `vpn_onboarding_reconciliation_required`; no raw exception text.

- [ ] **Step 5: Run focused backend tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding.py tests/test_worker_maintenance.py tests/test_vpn_reality_endpoint_deployment.py tests/test_vpn_reality_endpoint_registration.py tests/test_vpn_fleet_health.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/vpn_node_onboarding.py backend/app/services/vpn_policy.py backend/app/services/worker_maintenance.py backend/tests/test_vpn_node_onboarding.py backend/tests/test_worker_maintenance.py
git commit -m "feat: stage protected VPN nodes in one job"
```

## Task 5: Add a recoverable one-time acceptance profile

**Files:**

- Modify: `backend/app/services/vpn_node_onboarding.py`
- Modify: `backend/app/services/vpn_control_intents.py`
- Modify: `backend/tests/test_vpn_node_onboarding.py`
- Modify: `backend/tests/test_vpn_control_intents.py`

- [ ] **Step 1: Write failing URI and acceptance tests**

Extract this pure helper and keep the existing output byte-for-byte:

```python
def build_control_config_uri(request: VpnNodeRequest) -> str:
    serialize_node_request(request)
    return build_reality_config_uri(request.target, request.client_uuid)
```

Add `build_reality_config_uri(target: VpnEndpointTarget, client_uuid: UUID) ->
str` as the pure validated helper called above.

Add onboarding tests for:

Add `create_vpn_node_test_profile(session, worker_id, *, secret,
known_hosts_path) -> str` and `confirm_vpn_node_test_profile(session, worker_id,
*, admin_id, release_id, secret, known_hosts_path) -> VpnEndpoint`.

Prove the test UUID is deterministic via HMAC, has UUID version 4 bits, is never persisted/logged, and produces the same link after refresh. Test-profile creation must require a healthy staged endpoint and exact inspect receipt before adding the acceptance client.

Confirmation must remove that exact client, require an `acceptance_client_removed` receipt, create `ExternalEndpointAcceptance` with a digest of the canonical admin attestation, call `promote_protected_endpoint()`, perform one fresh strict health check, set `external_verified_at` and `external_config_fingerprint`, and leave the endpoint staged on any failed proof or cleanup.

- [ ] **Step 2: Run acceptance tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_control_intents.py tests/test_vpn_node_onboarding.py -q
```

Expected: missing helper and onboarding functions.

- [ ] **Step 3: Implement shared URI formatting**

Move the existing target validation, IDNA host handling, IPv6 brackets, and `urlencode()` block into `build_reality_config_uri()`. Do not change query names, escaping, or ordering.

- [ ] **Step 4: Implement acceptance add/cleanup/promotion**

First reconstruct the public receipt from persisted endpoint fields:

```python
observed_receipt = make_endpoint_receipt(
    state="observed",
    worker_id=endpoint.worker_id,
    inbound_id=endpoint.inbound_id,
    public_host=endpoint.public_host,
    server_name=endpoint.server_name,
    public_key=endpoint.public_key,
    short_id=endpoint.short_id,
)
```

Then derive the private identity only in memory:

```python
digest = hmac.new(
    secret.encode("utf-8"),
    f"vpn-onboarding-test:{endpoint.worker_id}:{endpoint.inbound_id}:{observed_receipt.receipt_digest}".encode("ascii"),
    hashlib.sha256,
).digest()
acceptance_uuid = UUID(bytes=digest[:16], version=4)
acceptance_email = f"veltrix-check-{worker.id}@local"
```

Use `add_acceptance_client` and `remove_acceptance_client` requests with the exact inbound/receipt digest. The test-profile API returns the URI but no UUID field. Use a canonical JSON admin attestation containing only worker ID, endpoint ID, admin ID, receipt digest, and UTC timestamp; hash it for `evidence_digest` and never claim an automated traffic measurement.

- [ ] **Step 5: Run focused acceptance and registration tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_vpn_control_intents.py tests/test_vpn_node_onboarding.py tests/test_vpn_reality_endpoint_installer.py tests/test_vpn_reality_endpoint_registration.py tests/test_vpn_release_readiness_api.py -q
```

Expected: all selected tests pass.

- [ ] **Step 6: Commit**

```powershell
git add backend/app/services/vpn_node_onboarding.py backend/app/services/vpn_control_intents.py backend/tests/test_vpn_node_onboarding.py backend/tests/test_vpn_control_intents.py
git commit -m "feat: verify VPN nodes with one-time profiles"
```

## Task 6: Expose the admin-only onboarding API

**Files:**

- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Create: `backend/tests/test_vpn_node_onboarding_api.py`
- Modify: `backend/tests/test_vpn_control_api.py`

- [ ] **Step 1: Write failing API tests**

Test all six routes from the design, anonymous/non-admin rejection, archived/non-VPN/missing-SSH workers, fixed safe error mapping, idempotent job start, active attack/control/job conflicts, audit rows, and `Cache-Control: no-store` for test-profile responses.

Use exact schemas:

```python
class VpnNodeTrustRequest(BaseModel):
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$", max_length=50)


class VpnNodeOnboardingResponse(BaseModel):
    worker_id: int
    state: str
    fingerprint: str | None
    job_id: int | None
    endpoint_id: int | None
    error_code: str | None


class VpnNodeTestProfileResponse(BaseModel):
    config_uri: str


class VpnNodeConfirmRequest(BaseModel):
    confirmed: Literal[True]
```

Assert no schema contains the host key, SSH credential, panel token, acceptance UUID, private REALITY key, remote command, or raw logs.

- [ ] **Step 2: Run API tests and verify RED**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding_api.py -q
```

Expected: 404 or missing-schema failures.

- [ ] **Step 3: Implement the routes and background dispatch**

Route behavior:

```text
POST preflight     -> probe_vpn_node_trust, no mutation
POST trust         -> re-probe, pin, persist public identity, audit
POST start         -> lock worker, create/reuse vpn_onboard job, dispatch background task
GET status         -> get_vpn_node_onboarding_view
POST test-profile  -> add/reuse deterministic acceptance client, no-store
POST confirm       -> clean, promote, fresh health, audit
```

Use `require_admin`, `lock_vpn_worker()`, `active_vpn_control_worker_ids()`, `evaluate_vpn_node()`, and existing audit helpers. Return HTTP 409 for state conflicts, 422 for an invalid trust fingerprint, 503 for safe transport failures, and never return `job.log` from onboarding endpoints.

- [ ] **Step 4: Run API and policy tests**

Run:

```powershell
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding_api.py tests/test_vpn_control_api.py tests/test_vpn_policy.py -q
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/schemas/control.py backend/app/api/routes/control.py backend/tests/test_vpn_node_onboarding_api.py backend/tests/test_vpn_control_api.py
git commit -m "feat: add VPN node onboarding API"
```

## Task 7: Replace the legacy admin button cluster

**Files:**

- Create: `frontend/src/vpnNodeOnboarding.ts`
- Create: `frontend/src/VpnNodeOnboardingPanel.tsx`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Create: `frontend/test/vpnNodeOnboarding.test.mjs`
- Modify: `frontend/test/vpnMaintenance.test.mjs`

- [ ] **Step 1: Write failing pure-state, API, and source-safety tests**

Define the frontend types and pure mapping:

```typescript
export type VpnNodeOnboardingState =
  | "needs_preflight" | "needs_trust" | "ready_to_start"
  | "queued" | "running" | "awaiting_test" | "ready" | "failed";

export function onboardingPresentation(state: VpnNodeOnboardingState) {
  return {
    needs_preflight: ["Проверить сервер", "Проверяем только настройки и SSH‑ключ."],
    needs_trust: ["Подтвердить SSH‑ключ", "Сверьте fingerprint в панели хостинга."],
    ready_to_start: ["Настроить VPN‑ноду", "Установим защищённое подключение на TCP 443."],
    queued: ["Настройка в очереди", "Можно закрыть страницу — работа продолжится."],
    running: ["Настраиваем VPN‑ноду…", "Не выключайте сервер."],
    awaiting_test: ["Проверить подключение", "Скопируйте временный профиль и подключитесь."],
    ready: ["VPN‑нода готова", "Нода участвует в выдаче новых профилей."],
    failed: ["Повторить безопасно", "Перед повтором проверим текущее состояние."],
  }[state] as const;
}
```

Test exact API paths/methods/bodies, `navigator.clipboard` plus textarea fallback for the temporary link, `role="status"`, `role="alert"`, disabled running actions, fingerprint visibility, explicit trust confirmation, and the absence of token/private-key/raw-log fields.

- [ ] **Step 2: Run frontend tests and verify RED**

Run:

```powershell
Set-Location frontend
npm test -- --test-name-pattern="VPN node onboarding"
```

Expected: missing module/component and API methods.

- [ ] **Step 3: Add typed API methods**

Add:

```typescript
getVpnNodeOnboarding(id)
preflightVpnNode(id)
trustVpnNode(id, { fingerprint })
startVpnNodeOnboarding(id)
getVpnNodeTestProfile(id)
confirmVpnNodeOnboarding(id, { confirmed: true })
```

Use the six exact `/control/workers/${id}/vpn-onboarding` paths declared in Task 6
and the existing `request()` wrapper.

- [ ] **Step 4: Build the focused panel and integrate it**

`VpnNodeOnboardingPanel` owns only its worker status and action state. `App.tsx` loads the status for visible VPN workers, refreshes while queued/running, and replaces both legacy button clusters at the current desktop/mobile locations with the panel. Put the old check/install/update/restart/autoconfig/create-inbound buttons inside a native `<details><summary>Диагностика</summary>…</details>`.

Show safe error copy from a fixed dictionary, for example:

```typescript
const onboardingErrors = {
  vpn_node_trust_host_key_mismatch: "SSH‑ключ сервера изменился. Настройка остановлена.",
  vpn_onboarding_port_conflict: "TCP 443 уже занят другой службой.",
  vpn_onboarding_reconciliation_required: "Нужно проверить незавершённое изменение на сервере.",
  vpn_node_bootstrap_mutation_uncertain: "Связь оборвалась во время настройки. Повторная проверка не создаст дубликат.",
} as const;
```

Unknown codes map to one bounded generic message and are not printed verbatim.

- [ ] **Step 5: Add responsive styles and run frontend verification**

Run:

```powershell
npm test
npm run build
```

Expected: all Node tests pass and TypeScript/Vite build exits 0.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/vpnNodeOnboarding.ts frontend/src/VpnNodeOnboardingPanel.tsx frontend/src/api.ts frontend/src/App.tsx frontend/src/styles.css frontend/test/vpnNodeOnboarding.test.mjs frontend/test/vpnMaintenance.test.mjs
git commit -m "feat: guide admins through VPN node onboarding"
```

## Task 8: Run migration, concurrency, and security regression checks

**Files:**

- Create: `backend/tests/test_vpn_node_onboarding_postgres.py`

- [ ] **Step 1: Add PostgreSQL race tests**

Test two concurrent `start` calls create one active job, onboarding versus customer provisioning cannot share a worker, promotion versus capacity/health updates preserves the exact endpoint, and migration can run twice without changing existing worker/endpoint data.

- [ ] **Step 2: Run focused PostgreSQL tests**

Run with the repository's disposable PostgreSQL test URL:

```powershell
Set-Location backend
$env:VPN_PORTAL_TEST_PG_URL='<closed disposable test database URL>'
./.venv/Scripts/python.exe -m pytest tests/test_vpn_node_onboarding_postgres.py tests/test_vpn_endpoint_migrations.py tests/test_vpn_fleet_health_postgres.py -q
```

Expected: all selected tests pass; the database is not a production database.

- [ ] **Step 3: Run complete local verification**

Run:

```powershell
Set-Location backend
./.venv/Scripts/python.exe -m pytest -q
./.venv/Scripts/python.exe -m ruff check app tests
Set-Location ../frontend
npm test
npm run build
```

Expected: backend tests and Ruff pass; frontend tests/build pass. Record exact counts in the final handoff.

- [ ] **Step 4: Scan for secrets and forbidden legacy execution**

Run:

```powershell
rg -n "api-token|privateKey|ssh_password|known_hosts=None|execute_worker_ssh_commands" backend/app/services/vpn_node_onboarding.py backend/app/services/vpn_node_bootstrap.py backend/app/services/vpn_node_trust.py backend/app/api/routes/control.py
```

Expected: only constant paths/validation references; no secret output; onboarding never calls the legacy unpinned executor.

- [ ] **Step 5: Commit PostgreSQL tests or any minimal verified correction**

```powershell
git add backend/tests/test_vpn_node_onboarding_postgres.py
git commit -m "test: cover concurrent VPN node onboarding"
```

## Task 9: Deploy and onboard `polish-server`

**Files:**

- No source changes expected
- Operational evidence remains on the production servers, not in Git

- [ ] **Step 1: Take read-only production inventory**

Verify current app commit, service status, database migration level, existing ready endpoints, `polish-server` worker ID, current Ed25519 fingerprint, free TCP 443, 3x-UI version/state, and absence of an endpoint. Stop if the fingerprint differs from:

```text
SHA256:mhKbdqg44ln3ZCYjiHDCzAHie8TwJZ5Vp5V/GhmoiTc
```

- [ ] **Step 2: Back up before deployment**

Use the existing verified backup workflow for the control PostgreSQL database and a root-only local backup of `/etc/x-ui/x-ui.db` on `polish-server`. Verify both artifacts are nonempty and restorable before changing services.

- [ ] **Step 3: Deploy the reviewed commit**

Deploy only after Task 8 passes. Run startup migrations, rebuild the frontend, restart the control service, and verify `/api/health` plus existing portal/bot smoke checks. Existing ready endpoints must remain unchanged.

- [ ] **Step 4: Run the new flow on `polish-server`**

In the admin UI:

1. run «Проверить сервер»;
2. compare the displayed fingerprint to the value from Step 1 and confirm trust;
3. run «Настроить VPN-ноду»;
4. wait for «Нужна проверка подключения»;
5. copy the temporary profile, import it into Happ/Hiddify, and verify a real HTTPS site plus DNS;
6. press «Подключение работает».

- [ ] **Step 5: Verify production postconditions**

Confirm exactly one `polish-server` REALITY/TCP/443 endpoint, `status=ready`, fresh `verified_at`, `health_checked_at`, `external_verified_at`, capacity 20, no acceptance client, fresh node health, and unchanged endpoint/config/profile data on other servers. Confirm a newly issued user profile selects the new node only when fleet policy allows it.

- [ ] **Step 6: Roll back safely on failure**

If application deployment fails, restore the prior app release without restoring an old database over new events. If onboarding fails before promotion, leave the endpoint `staged` and remove only the exact temporary acceptance client after strict inspection. Never delete an unknown inbound, rotate an unknown host key, or alter existing ready endpoints.

