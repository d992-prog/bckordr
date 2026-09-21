# Veltrix 3x-UI identity preflight implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Inspect the pinned panel's two inventory responses and distinguish an exact, exclusively attached VLESS client from ambiguous or inconsistent identity without changing anything.

**Architecture:** A pure, side-effect-free API boundary accepts decoded responses from 3x-UI 3.8.5 and a recorded endpoint target. It validates types and cross-checks normalized global clients against inbound settings. The result is an observation, never mutation authorization or proof of runtime revocation; existing provisioning barriers remain intact.

**Tech Stack:** Python 3.11+, frozen dataclasses, UUID, pytest, existing Ruff configuration.

---

## Scope and source contract

This is the next bounded component of the approved protected-endpoint design,
not the entire remote adapter. The owner's closed-beta reconnect exception is
recorded in the spec. No SSH, HTTP call, API credential, node setting, database
write, production binding, invitation or public port is introduced by this task.
Do not deploy this intermediate checkpoint.

Pinned primary sources:

- [Model JSON](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/database/model/model.go): global client `id` is an integer record ID; `uuid` is the credential. Valid inbound `settings` is emitted as an object, not an escaped string.
- [Client response type](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/client.go): flattened global record plus `inboundIds` and optional traffic.
- [Inbound inventory](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/service/inbound.go): full list is scoped to the authenticated panel user, and stored clients can be stale.
- [Client controller](https://raw.githubusercontent.com/MHSanaei/3x-ui/v3.8.5/internal/web/controller/client.go): global list and mutations are distinct; success envelope alone is not runtime evidence.

The input inventories may be partial or non-atomic. Therefore `not_observed`
**must not** be interpreted as globally absent, safe to create, or revoked.
Even `matched` needs a fresh trusted full collection, locks, durable intent,
transport/configuration checks and postcondition/runtime verification before
a mutation. No Boolean `safe_to_mutate` or `verified_at` is exposed here.
Do not compare global client Flow with inbound Flow: per-inbound overrides exist.
Non-VLESS client credentials can be empty; normalized UUIDs, when nonempty, are
still validated and included in collision checks. Orphan `inboundIds: null`
normalizes to an empty immutable tuple but never counts as an attached match.
Malformed stored settings returned as strings are rejected, not decoded as a
compatibility fallback. No panel message, UUID, email, private key or raw record
may appear in exceptions or observation repr; inputs are neither retained nor
mutated. The transport loader must later discard raw payloads without logging.

## Task 1: Cross-inventory identity observation

**Files:**
- Create: `backend/app/services/vpn_xui_identity.py`
- Create: `backend/tests/test_vpn_xui_identity.py`
- Existing dependency (read only): `backend/app/services/vpn_endpoints.py`

- [x] **Step 1: Add failing synthetic wire-contract tests.** Start with the following
  fixture and successful-case test; then add parametrized cases listed below.
  UUIDs and emails must be fictional. Tests call real parsing/inspection, no mocks.

```python
from uuid import UUID

from app.services.vpn_endpoints import VpnEndpointTarget
from app.services.vpn_xui_identity import inspect_xui_client_identity


def sample():
    target = VpnEndpointTarget(
        endpoint_id=2, worker_id=15, inbound_id=2,
        public_host="vpn.example.test", port=443, protocol="vless",
        transport="tcp", security="reality", server_name="example.test",
        public_key="synthetic-public-key", short_id="abcd",
        fingerprint="chrome", flow="xtls-rprx-vision",
    )
    credential = UUID("11111111-2222-4333-8444-555555555555")
    inbound_response = {"success": True, "obj": [{
        "id": 2, "protocol": "vless", "nodeId": None,
        "settings": {"clients": [{"id": str(credential),
            "email": "synthetic-profile", "enable": True}]},
    }]}
    client_response = {"success": True, "obj": [{
        "id": 19, "uuid": str(credential), "email": "synthetic-profile",
        "enable": True, "inboundIds": [2],
    }]}
    return dict(panel_version="3.8.5", target=target,
                client_uuid=credential, client_email="synthetic-profile",
                inbound_response=inbound_response, client_response=client_response)


def test_observes_exact_single_inbound_identity():
    result = inspect_xui_client_identity(**sample())
    assert result.state == "matched"
    assert result.record_id == 19
    assert result.enabled is True
```

Additional real-data tests (each must fail before its behavior is implemented):
both enabled/disabled matches; canonical UUID equivalence; unrelated VMess with
empty UUID; extra unused secret fields ignored; inputs unchanged and result frozen;
wrong/unknown panel version; malformed envelopes, success values other than literal
true, nodePending, obj null/dict; duplicate/noninteger/Boolean IDs; invalid email,
UUID, enable, attachment values and duplicate attachments; orphan null/empty list;
missing/duplicate/wrong-protocol target; target nodeId set (including zero), fallback
parent; missing/serialized/slim VLESS settings and malformed clients; UUID collision
under another email or email under another UUID; duplicate global credential or
email; same client attached to another/both inbounds; stale inbound UUID/email;
inbound-only and global-only matches; enabled-state mismatch; consistent absence
returns only `not_observed` with no record or enabled flag; unrelated stale pair
does not claim the target matched; transport differences are not validated here;
exceptions/traceback and repr do not contain synthetic secret sentinels. Include
unmatched credentials in a second inbound to prove no first-row/default selection.

- [x] **Step 2: Verify RED.** From the active worktree's `backend`, run:

```powershell
$vpnIdentityTemp = Join-Path (Get-Location).Path ('.pytest_cache/xui-identity-red-' + [guid]::NewGuid().ToString('N'))
if (Test-Path -LiteralPath $vpnIdentityTemp) { throw 'Scratch exists' }
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_xui_identity.py -q --basetemp $vpnIdentityTemp
```

Expected initial missing module/function, then assertion failures when a minimal
stub exists; record evidence of actual missing behavior, not fixture mistakes.

- [x] **Step 3: Implement the observation boundary.** The following is the complete
  intended implementation. Small refactors preserving the contract are permitted;
  unsupported wire behavior must be raised, not invented.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from app.services.vpn_endpoints import VpnEndpointError, VpnEndpointTarget


@dataclass(frozen=True, slots=True)
class XuiClientObservation:
    state: Literal["matched", "not_observed"]
    record_id: int | None = None
    enabled: bool | None = None


def _invalid() -> None:
    raise VpnEndpointError("vpn_xui_inventory_invalid")


def _positive_id(value: object) -> int:
    if type(value) is not int or value <= 0:
        _invalid()
    return value


def _email(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        _invalid()
    if any(character.isspace() or ord(character) < 32 for character in value):
        _invalid()
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        _invalid()
    try:
        return UUID(value)
    except ValueError:
        raise VpnEndpointError("vpn_xui_inventory_invalid") from None


def _enabled(value: object) -> bool:
    if type(value) is not bool:
        _invalid()
    return value


def _rows(response: object) -> list:
    if not isinstance(response, dict) or response.get("success") is not True:
        _invalid()
    if response.get("nodePending", False) is not False:
        _invalid()
    rows = response.get("obj")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        _invalid()
    return rows


def inspect_xui_client_identity(
    *, panel_version: str, target: VpnEndpointTarget, client_uuid: UUID,
    client_email: str, inbound_response: object, client_response: object,
) -> XuiClientObservation:
    """Observe configuration identity, not runtime access or mutation safety.

    Lists may be partial or collected at different times. Neither state proves
    full inventory, endpoint transport, authorization, revocation or readiness.
    This function performs no I/O, retains no raw payload and changes no inputs.
    """
    if panel_version != "3.8.5":
        raise VpnEndpointError("vpn_xui_version_unsupported")
    if not isinstance(client_uuid, UUID) or target.protocol != "vless":
        raise VpnEndpointError("vpn_xui_identity_invalid")
    target_id = _positive_id(target.inbound_id)
    expected_email = _email(client_email)
    inbounds = _rows(inbound_response)
    clients = _rows(client_response)
    seen_inbounds = set()
    target_row = None
    inbound_matches = []
    for inbound in inbounds:
        inbound_id = _positive_id(inbound.get("id"))
        if inbound_id in seen_inbounds:
            _invalid()
        seen_inbounds.add(inbound_id)
        protocol = inbound.get("protocol")
        if not isinstance(protocol, str) or not protocol:
            _invalid()
        if inbound_id == target_id:
            target_row = inbound
        if protocol != "vless":
            continue
        settings = inbound.get("settings")
        if not isinstance(settings, dict) or not isinstance(settings.get("clients"), list):
            _invalid()
        for entry in settings["clients"]:
            if not isinstance(entry, dict):
                _invalid()
            credential = _uuid(entry.get("id"))
            email = _email(entry.get("email"))
            enabled = _enabled(entry.get("enable"))
            if credential == client_uuid or email == expected_email:
                inbound_matches.append((inbound_id, credential, email, enabled))
    if target_row is None:
        raise VpnEndpointError("vpn_xui_inbound_missing")
    if (target_row.get("protocol") != "vless"
            or target_row.get("nodeId") is not None
            or target_row.get("fallbackParent") is not None):
        raise VpnEndpointError("vpn_xui_inbound_unsupported")

    seen_ids, seen_emails, seen_uuids = set(), set(), set()
    matches = []
    for client in clients:
        record_id = _positive_id(client.get("id"))
        email = _email(client.get("email"))
        raw_uuid = client.get("uuid")
        credential = None if raw_uuid == "" else _uuid(raw_uuid)
        enabled = _enabled(client.get("enable"))
        attachments = client.get("inboundIds")
        if "inboundIds" not in client:
            _invalid()
        if attachments is None:
            attachments = []
        if not isinstance(attachments, list):
            _invalid()
        attachment_ids = tuple(_positive_id(value) for value in attachments)
        if len(attachment_ids) != len(set(attachment_ids)):
            _invalid()
        if (record_id in seen_ids or email in seen_emails
                or credential is not None and credential in seen_uuids):
            raise VpnEndpointError("vpn_xui_identity_conflict")
        seen_ids.add(record_id)
        seen_emails.add(email)
        if credential is not None:
            seen_uuids.add(credential)
        if credential == client_uuid or email == expected_email:
            matches.append((record_id, credential, email, enabled, attachment_ids))
    if not matches and not inbound_matches:
        return XuiClientObservation("not_observed")
    if len(matches) != 1 or len(inbound_matches) != 1:
        raise VpnEndpointError("vpn_xui_identity_conflict")
    record_id, credential, email, enabled, attachments = matches[0]
    if (credential != client_uuid or email != expected_email
            or attachments != (target_id,)
            or inbound_matches[0] != (target_id, client_uuid, expected_email, enabled)):
        raise VpnEndpointError("vpn_xui_identity_conflict")
    return XuiClientObservation("matched", record_id, enabled)
```

- [x] **Step 4: Verify GREEN and existing boundaries.** Use a new unique basetemp
  for each invocation of pytest. Run the new file plus `test_vpn_endpoints.py`
  and `test_vpn_endpoint_legacy_barrier.py`; expect all passed. Run
  `.\.venv\Scripts\python.exe -m ruff check app tests` and `git diff --check`.
- [x] **Step 5: Self-review, then specification review, then quality review.**
  No reviewer may treat this observation as a completed adapter or approve deploy.
- [x] **Step 6: Commit only the two files after reviews.**

```powershell
git add -- backend/app/services/vpn_xui_identity.py backend/tests/test_vpn_xui_identity.py
git commit -m "feat(vpn): cross-check pinned panel client identity"
```

## Task 2: Parent integration verification and handoff

- [x] Run full backend pytest with a fresh local basetemp, count PostgreSQL-only
  skips explicitly; no concurrent database proof is claimed from SQLite.
- [x] Re-run backend Ruff and diff check. No frontend source/build changes expected.
- [x] Obtain final integrated independent review of the full increment.
- [x] Update `docs/current-state.md` and this ledger with actual evidence, local-only
  status and next requirement: trusted collection/transport verification and durable
  endpoint-aware mutations, followed by legacy import and protected inbound tests.
- [x] Preserve `frontend/tsconfig.tsbuildinfo`; no deployment, push or merge.

### Verification ledger

- RED: initial missing-module collection failure; stub 203 failures + 87 passes,
  then split-identity regressions brought RED to 205 failures + 87 passes.
- GREEN: 292 related tests passed; after strengthening the secret-sentinel test,
  the implementer repeated 292 passes in 44.78 seconds.
- Parent independent latest focused run: 205 passed in 1.20 seconds.
- Parent full backend: 990 passed, 8 PostgreSQL-only skips in 164.10 seconds.
  The full run began before the final test-only sentinel strengthening; production
  code did not change, and the latest focused/related reruns verify that amendment.
- Parent repeated full backend on committed `a823868`, including the final test
  amendment: **990 passed, 8 PostgreSQL-only skips in 139.83 seconds**.
- Whole-backend Ruff and diff whitespace checks pass. The specification reviewer
  independently ran the latest 205 tests; the quality reviewer also passed 205
  tests and focused Ruff. Both approved this local-only boundary without findings.
- No deployment, server/node query, API login or port change. No frontend source
  change. Existing bound mutation barriers remain in place.
- Code commit: `a823868`. Final integrated review approved the code, amended beta
  policy and documentation as a local checkpoint with no actionable findings.
- Invocation caveat: root-cwd Ruff inferred Python 3.10 and flagged the pre-existing
  `ExceptionGroup` test. `--show-settings` confirmed backend-cwd inference is 3.11
  from `requires-python`; the prescribed backend check passes. Root invocation
  with explicit `--target-version py311` also passes. No source/config change was
  needed. Always run project verification from `backend` as specified above.

## Plan self-review

This increment implements only the source-defined remote identity comparison
portion of the approved spec. Full adapter, transport validation, trustworthy
collection, intent durability, production migration and invitations are explicitly
outside it and still required. Inputs/outputs match the code and tests above;
no operation enum or success marker is introduced that could bypass the existing
pending-state barrier. The implementation stays in the already isolated worktree.
