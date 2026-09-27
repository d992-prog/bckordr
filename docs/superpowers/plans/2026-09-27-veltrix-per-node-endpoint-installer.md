# Per-node protected endpoint installer implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind each protected REALITY endpoint operation to one explicit VPN worker from deterministic bundle construction through SSH execution and transactional registration.

**Architecture:** Keep the isolated remote entrypoint's existing worker-binding global and set it in the generated `__main__.py`. In the control process, require the same reviewed positive worker ID explicitly at request encoding, SSH execution, cleanup, staging and promotion; never mutate the control process global or infer the binding from untrusted request data.

**Tech Stack:** Python 3.11+, standard-library `zipfile`, pytest, Ruff.

---

### Task 1: Prove worker-bound deterministic bundles

**Files:**
- Modify: `backend/tests/test_vpn_reality_endpoint_deployment.py`

- [ ] **Step 1: Write the failing tests**

Pass `worker_id=15` to the five existing builder calls, then add:

```python
@pytest.mark.parametrize("worker_id", [True, 0, -1, 2**63])
def test_endpoint_candidate_bundle_rejects_invalid_worker_id(
    tmp_path: Path,
    worker_id: object,
) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    target = tmp_path / "candidate.pyz"

    with pytest.raises(
        module.EndpointInstallerDeploymentError,
        match="^vpn_endpoint_installer_transport_failed$",
    ):
        module.build_endpoint_installer_bundle(
            BACKEND,
            target,
            worker_id=worker_id,
        )

    assert not target.exists()


def test_endpoint_candidate_bundle_is_bound_to_one_worker(tmp_path: Path) -> None:
    module = importlib.import_module("app.services.vpn_reality_endpoint_deployment")
    worker_15 = tmp_path / "worker-15.pyz"
    worker_2 = tmp_path / "worker-2.pyz"

    digest_15 = module.build_endpoint_installer_bundle(
        BACKEND, worker_15, worker_id=15
    )
    digest_2 = module.build_endpoint_installer_bundle(
        BACKEND, worker_2, worker_id=2
    )

    assert digest_15 != digest_2
    with zipfile.ZipFile(worker_2) as archive:
        entrypoint = archive.read("__main__.py")
    assert b"installer.CONTROLLED_WORKER_ID = 2\n" in entrypoint
    assert b"installer.CONTROLLED_WORKER_ID = 15\n" not in entrypoint
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py -q
```

Expected: failures because `build_endpoint_installer_bundle` does not accept `worker_id`.

- [ ] **Step 3: Commit the red tests**

```powershell
git add backend/tests/test_vpn_reality_endpoint_deployment.py
git commit -m "test(vpn): require worker-bound endpoint bundles"
```

### Task 2: Generate the minimal worker-bound entrypoint

**Files:**
- Modify: `backend/app/services/vpn_reality_endpoint_deployment.py`

- [ ] **Step 1: Replace the fixed entrypoint bytes with a generator**

```python
def _main(worker_id: int) -> bytes:
    return (
        b"import sys\n"
        b"import app.services.vpn_endpoint_types\n"
        b"import app.services.vpn_xui_identity\n"
        b"import app.services.vpn_xui_node_http\n"
        b"import app.services.vpn_xui_node_observation\n"
        b"import app.services.vpn_xray_runtime\n"
        b"import app.services.vpn_reality_endpoint_installer as installer\n"
        + f"installer.CONTROLLED_WORKER_ID = {worker_id}\n".encode("ascii")
        + b"if sys.argv[1:] == ['--import-probe']:\n"
        + b"    sys.stdout.buffer.write("
        + repr(ENDPOINT_IMPORT_PROBE_SENTINEL).encode("ascii")
        + b")\n"
        + b"    raise SystemExit(0)\n"
        + b"if sys.argv[1:]:\n"
        + b"    raise SystemExit(2)\n"
        + b"raise SystemExit(installer.main())\n"
    )
```

- [ ] **Step 2: Require and validate the worker ID at the builder boundary**

Change the signature and entrypoint write to:

```python
def build_endpoint_installer_bundle(
    source_root: Path,
    target: Path,
    *,
    worker_id: int,
) -> str:
    if (
        type(worker_id) is not int
        or not 1 <= worker_id <= 2**63 - 1
        or not isinstance(source_root, Path)
        or not isinstance(target, Path)
        or not source_root.is_dir()
        or not target.is_absolute()
        or not target.parent.is_dir()
    ):
        _fail()
    # existing source validation remains unchanged
```

Write `_main(worker_id)` as `__main__.py`; do not change the request parser, receipt, SSH transport or node mutation code.

- [ ] **Step 3: Run focused tests and verify GREEN**

```powershell
python -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit the implementation**

```powershell
git add backend/app/services/vpn_reality_endpoint_deployment.py
git commit -m "fix(vpn): bind endpoint bundles per worker"
```

### Task 3: Verify the worker-bound bundle slice

**Files:**
- Verify only; no additional production file is required.

- [ ] **Step 1: Run scoped lint and VPN regressions**

```powershell
python -m ruff check backend/app/services/vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_deployment.py
python -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_registration.py backend/tests/test_vpn_node_deployment.py backend/tests/test_vpn_node_transport.py -q
```

Expected: Ruff clean and all selected tests pass.

- [ ] **Step 2: Review the complete diff and repository state**

```powershell
git diff origin/main...HEAD --check
git status --short
```

Expected: no whitespace errors and a clean worktree.

- [ ] **Step 3: Continue to the end-to-end binding tasks**

Do not push or deploy this partial slice. The control-side encoder and registration path must be worker-bound first.

### Task 4: Prove end-to-end control-process worker binding

**Files:**
- Modify: `backend/tests/test_vpn_reality_endpoint_installer.py`
- Modify: `backend/tests/test_vpn_reality_endpoint_deployment.py`
- Modify: `backend/tests/test_vpn_reality_endpoint_registration.py`

- [ ] **Step 1: Add failing installer encoder tests**

Update existing worker-15 encoder calls to pass `controlled_worker_id=15`. Add tests proving that the keyword is required, worker 2 encodes with `controlled_worker_id=2`, and the same request is rejected with `controlled_worker_id=15`.

- [ ] **Step 2: Add failing strict SSH tests**

Pass `controlled_worker_id=15` at existing strict executor and cleanup call sites. Add a test that leaves the module default at 15, sends a worker-2 request with `controlled_worker_id=2`, and proves the pinned test server is reached. Prove a mismatched binding fails before the connector is called.

- [ ] **Step 3: Add failing registration tests**

Let the worker seed helper accept a worker ID and pass `controlled_worker_id=15` in existing stage and promotion tests. Add worker-2 stage-and-promote coverage and a mismatch case proving no endpoint or deployment marker is written.

- [ ] **Step 4: Run the focused tests and verify RED**

```powershell
python -m pytest backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_registration.py -q
```

Expected: failures because the public control-process APIs do not yet require or propagate `controlled_worker_id`.

- [ ] **Step 5: Commit the red tests**

```powershell
git add backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_registration.py
git commit -m "test(vpn): bind endpoint control to worker"
```

### Task 5: Bind request encoding and strict SSH execution

**Files:**
- Modify: `backend/app/services/vpn_reality_endpoint_installer.py`
- Modify: `backend/app/services/vpn_reality_endpoint_deployment.py`

- [ ] **Step 1: Accept an explicit parser binding without changing the remote default**

Add optional keyword `controlled_worker_id` to `parse_install_request`. Resolve the expected worker as the existing global only when the keyword is absent, validate it as an exact positive 64-bit integer, and compare the parsed worker ID to that value.

- [ ] **Step 2: Require the binding at the control-process boundaries**

Require keyword `controlled_worker_id` in `encode_install_request`, `execute_endpoint_installer_over_ssh`, and `remove_endpoint_installer_over_ssh`. Pass it through to parsing and execution. Do not derive it from the request and do not mutate the module global.

- [ ] **Step 3: Run focused tests and lint**

```powershell
python -m ruff check backend/app/services/vpn_reality_endpoint_installer.py backend/app/services/vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py
python -m pytest backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py -q
```

Expected: Ruff clean and all selected tests pass.

- [ ] **Step 4: Commit the implementation**

```powershell
git add backend/app/services/vpn_reality_endpoint_installer.py backend/app/services/vpn_reality_endpoint_deployment.py
git commit -m "fix(vpn): bind endpoint SSH requests to worker"
```

### Task 6: Bind transactional endpoint registration

**Files:**
- Modify: `backend/app/services/vpn_reality_endpoint_registration.py`

- [ ] **Step 1: Require one explicit worker binding**

Remove the worker-15 registration constant. Require keyword `controlled_worker_id` in staging and promotion, validate it as an exact positive 64-bit integer, and reject any mismatch before querying or mutating the database.

- [ ] **Step 2: Preserve existing transactional checks**

Lock the exact requested worker and leave the existing readiness, role, endpoint, marker and transaction rules unchanged.

- [ ] **Step 3: Run focused tests and lint**

```powershell
python -m ruff check backend/app/services/vpn_reality_endpoint_registration.py backend/tests/test_vpn_reality_endpoint_registration.py
python -m pytest backend/tests/test_vpn_reality_endpoint_registration.py -q
```

Expected: Ruff clean and all selected tests pass.

- [ ] **Step 4: Commit the implementation**

```powershell
git add backend/app/services/vpn_reality_endpoint_registration.py
git commit -m "fix(vpn): bind endpoint registration to worker"
```

### Task 7: Verify and deliver before production onboarding

**Files:**
- Verify only; no additional production file is required.

- [ ] **Step 1: Run scoped lint and VPN regressions**

```powershell
python -m ruff check backend/app/services/vpn_reality_endpoint_installer.py backend/app/services/vpn_reality_endpoint_deployment.py backend/app/services/vpn_reality_endpoint_registration.py backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_registration.py
python -m pytest backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_registration.py backend/tests/test_vpn_node_deployment.py backend/tests/test_vpn_node_transport.py -q
```

- [ ] **Step 2: Review the complete diff and repository state**

```powershell
git diff origin/main...HEAD --check
git status --short
```

Expected: no whitespace errors and a clean worktree.

- [ ] **Step 3: Request fresh specification, quality and integrated reviews**

Resolve every blocking finding, then rerun the relevant checks with fresh output.

- [ ] **Step 4: Push, merge and deploy**

```powershell
git push -u origin codex/veltrix-multi-node-installer
```

Open the PR, wait for CI, merge only after it is green, then deploy the merge commit to the control server. Production worker 2 onboarding resumes only after that deployment; public trial, notifications and payment stay disabled.
