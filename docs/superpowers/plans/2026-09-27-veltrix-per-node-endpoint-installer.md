# Per-node protected endpoint installer implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build each protected REALITY endpoint installer bundle for one explicit VPN worker instead of hard-coding worker 15 for every future node.

**Architecture:** Keep the existing request parser and its worker-binding check. Parameterize only deterministic bundle construction: the generated `__main__.py` sets the module's existing `CONTROLLED_WORKER_ID` to the reviewed positive worker ID before invoking `main`.

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
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py -q
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
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py -q
```

Expected: all tests pass.

- [ ] **Step 4: Commit the implementation**

```powershell
git add backend/app/services/vpn_reality_endpoint_deployment.py
git commit -m "fix(vpn): bind endpoint bundles per worker"
```

### Task 3: Verify and deliver before production onboarding

**Files:**
- Verify only; no additional production file is required.

- [ ] **Step 1: Run scoped lint and VPN regressions**

```powershell
backend\.venv\Scripts\python.exe -m ruff check backend/app/services/vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_deployment.py
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_registration.py backend/tests/test_vpn_node_deployment.py backend/tests/test_vpn_node_transport.py -q
```

Expected: Ruff clean and all selected tests pass.

- [ ] **Step 2: Review the complete diff and repository state**

```powershell
git diff origin/main...HEAD --check
git status --short
```

Expected: no whitespace errors and a clean worktree.

- [ ] **Step 3: Push and open a pull request**

```powershell
git push -u origin codex/veltrix-multi-node-installer
```

Open the PR, wait for CI, merge only after it is green, then deploy the merge commit to the control server. Production worker 2 onboarding resumes only after that deployment; public trial, notifications and payment stay disabled.
