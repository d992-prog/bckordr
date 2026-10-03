# Veltrix REALITY Target Compatibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make protected Veltrix profiles connect reliably from Hiddify by using a stable REALITY camouflage target and an explicit third-party client version floor.

**Architecture:** Keep the existing VLESS TCP REALITY Vision endpoint and identity material. Enforce one stable camouflage identity in the node-local installer, migrate the live endpoint and its saved profile URIs without changing UUIDs, then re-run health and real client acceptance checks.

**Tech Stack:** Python 3.12, pytest, Ruff, FastAPI/SQLAlchemy, PostgreSQL, 3x-UI SQLite, Xray-core, Hiddify/sing-box.

---

### Task 1: Lock the Compatible REALITY Policy With Failing Tests

**Files:**
- Modify: `backend/tests/test_vpn_reality_endpoint_installer.py`
- Test: `backend/tests/test_vpn_reality_endpoint_installer.py`

- [ ] **Step 1: Change the test request fixture to the approved stable server name**

Use `gateway.icloud.com` for the fixture's `server_name`, typed request, expected
canonical encoding, exact inbound rows, and expected receipts. Keep arbitrary
hosts only in tests that explicitly verify host syntax.

- [ ] **Step 2: Write the failing policy tests**

```python
def test_request_parser_rejects_known_unstable_reality_target() -> None:
    module = _module()

    with pytest.raises(
        module.EndpointInstallError,
        match="^vpn_endpoint_install_request_invalid$",
    ):
        module.parse_install_request(_request(server_name="www.microsoft.com"))


def test_inbound_payload_sets_hiddify_compatible_reality_policy() -> None:
    module = _module()

    payload = module._inbound_payload(
        server_name="gateway.icloud.com",
        short_id="0123456789abcdef",
        private_key=PRIVATE_KEY,
        public_key=PUBLIC_KEY,
    )
    reality = payload["streamSettings"]["realitySettings"]

    assert reality["target"] == "gateway.icloud.com:443"
    assert reality["serverNames"] == ["gateway.icloud.com"]
    assert reality["minClientVer"] == "1.0.0"
```

- [ ] **Step 3: Run the two tests and verify RED**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_installer.py -k "known_unstable or hiddify_compatible" -q
```

Expected: two assertion failures because the parser still accepts
`www.microsoft.com` and the payload still emits an empty `minClientVer`.

### Task 2: Apply the Minimal Installer Policy

**Files:**
- Modify: `backend/app/services/vpn_reality_endpoint_installer.py`
- Test: `backend/tests/test_vpn_reality_endpoint_installer.py`

- [ ] **Step 1: Add fixed compatibility constants**

```python
REALITY_SERVER_NAME = "gateway.icloud.com"
MIN_CLIENT_VERSION = "1.0.0"
```

- [ ] **Step 2: Reject other server names at the request boundary**

Validate `server_name` with the existing `_host` helper and require it to equal
`REALITY_SERVER_NAME` before constructing `EndpointInstallRequest`. This keeps
future automated node installation from reintroducing the known-bad target.

```python
server_name = _host(value["server_name"])
if server_name != REALITY_SERVER_NAME:
    _fail("vpn_endpoint_install_request_invalid")
```

- [ ] **Step 3: Emit the explicit compatibility floor**

```python
"minClientVer": MIN_CLIENT_VERSION,
```

- [ ] **Step 4: Run the focused test file and verify GREEN**

Run:

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_installer.py -q
```

Expected: all tests in the file pass with zero failures.

- [ ] **Step 5: Commit the code and tests**

```powershell
git add backend/app/services/vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_installer.py
git commit -m "fix: use stable REALITY target for Hiddify"
```

### Task 3: Verify the Release Candidate

**Files:**
- Verify: `backend/app/services/vpn_reality_endpoint_installer.py`
- Verify: `backend/tests/test_vpn_reality_endpoint_installer.py`

- [ ] **Step 1: Run protected endpoint tests**

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests/test_vpn_reality_endpoint_installer.py backend/tests/test_vpn_reality_endpoint_deployment.py backend/tests/test_vpn_reality_endpoint_registration.py -q
```

Expected: zero failures.

- [ ] **Step 2: Run the complete backend suite**

```powershell
backend\.venv\Scripts\python.exe -m pytest backend/tests -q
```

Expected: zero failures.

- [ ] **Step 3: Run Ruff and repository checks**

```powershell
backend\.venv\Scripts\python.exe -m ruff check backend
git diff --check
```

Expected: both commands exit zero with no diagnostics.

### Task 4: Migrate Production Without Rotating Access

**Files:**
- Deploy: `backend/app/services/vpn_reality_endpoint_installer.py`
- Temporary production migration: `/tmp/veltrix-reality-target-migration.py`

- [ ] **Step 1: Run read-only preflight**

Confirm the control service is healthy, no VPN mutation is running, worker 2 is
ready, endpoint 3 is the only protected endpoint on port 443, Xray is active,
and the expected client identity matches by SHA-256. Print counts, states, and
hashes only.

- [ ] **Step 2: Create private backups**

Create a timestamped PostgreSQL custom-format dump under `/opt/backups` and a
root-only copy of the live 3x-UI SQLite database under
`/var/lib/veltrix-vpn/database-backups`. Validate both backups before mutation.

- [ ] **Step 3: Deploy the tested commit**

Fast-forward `/opt/domain-drop-catcher` to the tested commit, install the backend
package if required, restart `domain-drop-control.service`, and verify local and
public `/api/health` return HTTP 200.

- [ ] **Step 4: Update the node and control records transactionally**

The migration must assert the old values before writing. On the node, change only:

```json
{
  "target": "gateway.icloud.com:443",
  "serverNames": ["gateway.icloud.com"],
  "minClientVer": "1.0.0"
}
```

Restart 3x-UI and require Xray to listen on TCP 443. In PostgreSQL, change the
bound endpoint's `server_name`, clear stale external-proof fields, and rewrite
only the `sni` query parameter in bound non-revoked `config_uri` values. Preserve
UUID, host, port, public key, short ID, flow, labels, subscriptions, expiry, and
traffic counters. Roll back both databases if any assertion or restart fails.

- [ ] **Step 5: Re-run node health and regenerate proof**

Verify the live inbound and endpoint metadata match, run strict fleet health,
and keep the external-proof readiness check false until real external traffic is
confirmed for the new fingerprint.

### Task 5: Prove the User-Visible Fix

**Files:**
- Inspect: `C:\Users\user\AppData\Roaming\Hiddify\hiddify\data\box.log`

- [ ] **Step 1: Refresh the profile without changing the customer identity**

Copy the newly generated profile from the cabinet, delete or replace the old
Hiddify profile, and connect once. Do not expose the URI in task output.

- [ ] **Step 2: Verify real traffic**

Require a fresh Hiddify log with no `reality verification failed`, no repeated
`EOF`, and successful HTTPS traffic through the tunnel. Confirm the public exit
IP differs from the direct connection without printing the full access URI.

- [ ] **Step 3: Restore release evidence and clean temporary files**

After successful external traffic, record fresh acceptance evidence for the new
endpoint fingerprint through the existing readiness mechanism. Remove temporary
diagnostic and migration scripts from the control server, node, and worktree.
Keep the timestamped backups according to the existing retention policy.
