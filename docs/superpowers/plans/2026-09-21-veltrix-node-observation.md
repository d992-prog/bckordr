# Veltrix node-local configuration observation implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Connect the authenticated node-local HTTP session to the existing identity checker and return a secret-free, explicitly limited configuration observation.

**Architecture:** Shared endpoint value types become standard-library-only. A node collector compares the panel user's API inventory with two read-only local database ID inventories, checks the exact profile identity, and compares recorded transport parameters. It returns only a frozen allowlisted observation; no raw inbound/private key crosses this boundary. This is not an activation, mutation authorization, runtime proof, or import action.

**Tech Stack:** Python 3.11 stdlib, SQLite read-only URI, existing node HTTP session, pytest with real loopback HTTP and synthetic SQLite.

---

## Scope

No production connection, new listener, database migration, endpoint binding,
credential issuance, provisioning barrier removal, or change to existing lifecycle.
This collector is intended for packaging on the node, not fetching its database
or raw API responses onto the control host. The fake databases and servers in tests
contain only fictional data. Partial API inventory must fail closed.

## Task 1: Reusable types and connected read-only collector

**Files:**
- Create `backend/app/services/vpn_endpoint_types.py`.
- Modify `backend/app/services/vpn_endpoints.py` (reexport unchanged value types).
- Modify `backend/app/services/vpn_xui_identity.py` (import types only).
- Create `backend/app/services/vpn_xui_node_observation.py`.
- Create `backend/tests/test_vpn_xui_node_observation.py`.

- [x] Write RED tests before moving implementation. Prove a subprocess started
  with `python -S` can import the shared types, identity and collector modules;
  these modules must not import SQLAlchemy, application configuration or HTTPX.

```python
def test_shared_types_remain_identical_for_existing_callers():
    from app.services import vpn_endpoint_types, vpn_endpoints

    assert vpn_endpoints.VpnEndpointError is vpn_endpoint_types.VpnEndpointError
    assert vpn_endpoints.VpnEndpointTarget is vpn_endpoint_types.VpnEndpointTarget
    assert vpn_endpoints.EndpointOperation is vpn_endpoint_types.EndpointOperation
```

- [x] Move `EndpointOperation`, `VpnEndpointError`, `VpnEndpointTarget` without
  changing fields/semantics; reexport using explicit `as` aliases to satisfy Ruff.
  Update the identity helper's import; existing 205 identity tests must still pass.

- [x] Add connected collector tests using an actual `NodePanelSession`, real
  loopback API server, and SQLite `inbounds(id INTEGER PRIMARY KEY)` table. Start
  with a protected inbound and an unrelated legacy inbound, and verify that the
  legacy row cannot silently disappear from the API inventory.

Public contract:

```python
@dataclass(frozen=True, slots=True)
class NodeClientObservation:
    state: Literal["matched", "not_observed"]
    record_id: int | None
    enabled: bool | None
    transport: Literal["matched", "mismatch", "unsupported"]
    runtime: Literal["running", "stop", "error"]


def observe_node_client(
    panel: NodePanelSession,
    *,
    target: VpnEndpointTarget,
    client_uuid: UUID,
    client_email: str,
    database_path: Path,
) -> NodeClientObservation:
    before_ids = read_local_inbound_ids(database_path)
    status = panel.request("GET", "panel/api/server/status")
    version, runtime = inspect_panel_status(status)
    inbounds = panel.request("GET", "panel/api/inbounds/list")
    clients = panel.request("GET", "panel/api/clients/list")
    after_ids = read_local_inbound_ids(database_path)
    require_inventory_coverage(before_ids, inbounds, after_ids)
    identity = inspect_xui_client_identity(
        panel_version=version, target=target, client_uuid=client_uuid,
        client_email=client_email, inbound_response=inbounds, client_response=clients,
    )
    transport = observe_transport(target, inbounds, client_uuid, client_email)
    return NodeClientObservation(
        identity.state, identity.record_id, identity.enabled, transport, runtime,
    )
```

All helper functions are private unless otherwise required by tests through real
public behavior. The pseudocode names describe the following complete contracts:

1. **Local IDs:** require an absolute concrete Path, existing regular file (reject
   symlink); no creation. Open `path.as_uri() + "?mode=ro"`, `uri=True`, timeout 2s;
   set `PRAGMA query_only=ON`; execute only `SELECT id FROM inbounds ORDER BY id`.
   Positive strict integers, unique; SQL LIMIT 10001, reject >10000. Use a bounded
   SQLite progress handler (2s monotonic budget) in addition to lock timeout. Close
   connection on all paths. Convert filesystem/SQLite failures to static
   `vpn_xui_inventory_unavailable`, suppress underlying exception chain. Never
   select private settings or write the panel database. Reject path inputs that
   aren't a Path or are relative with `vpn_xui_inventory_unavailable` before HTTP.
2. **Coverage:** API success IS True, nodePending absent/False, obj list of dicts;
   each ID positive strict int, unique. Both local ID tuples must equal the sorted
   API IDs. Missing/extra/changing IDs raise `vpn_xui_inventory_incomplete`. This
   checks coverage at collection, not transaction-level atomicity against panel
   operators. Do not relabel `not_observed` as global absence or permission to add.
3. **Status:** envelope success IS True, obj dict, panelVersion exactly `3.8.5`;
   xray dict with state `running`, `stop` or `error`, and string errorMsg. Unknown
   version raises `vpn_xui_version_unsupported`; malformed status raises
   `vpn_xui_status_invalid`. A nonempty errorMsg with state running returns runtime
   error. No errorMsg contents escape. Runtime is the panel's cached observation,
   never proof of credential application or effective disconnection.
4. **Transport:** exact target inbound only, no first/default selection. Compare
   positive strict int port, protocol vless, `enable IS True`, `streamSettings`
   object, network tcp/raw aliases, security exactly recorded. Identity/protocol
   defects already rejected by the existing identity checker keep its typed errors;
   other transport shape/value mismatches return mismatch. Unsupported recorded
   transport/security returns unsupported. Supported security is none or reality;
   TLS fails unsupported until its certificate contract is separately implemented.
   Reject custom TCP/RAW header types (anything except absent/`none`) and enabled
   PROXY-protocol expectations which the current endpoint URI does not represent.
   Target None/empty flow normalizes to empty; allowed none flow empty, allowed
   REALITY flow `xtls-rprx-vision`. For an observed matching client compare its
   embedded flow (missing means empty), not the global record flow. For a missing
   client no flow equality is claimed, but target flow must be supported.
5. **REALITY:** require object `realitySettings`, object `settings` inside it;
   compare target server_name membership in nonempty string `serverNames` list,
   target short_id membership in `shortIds` (canonical lower hex, even length
   2..16), and target public_key/fingerprint exact equality to nested settings.
   Require canonical unpadded base64url 32-byte public key and privateKey shape
   (nonempty string; do not export, derive or log it). Valid fingerprints are
   printable nonempty ASCII <=32. Require no contradictory nested serverName:
   absent/empty or exactly target server_name. Reject nonempty mldsa65Verify or
   spiderX because current endpoint schema cannot represent them. No silent
   loss of required URI options. Require decryption `none` in VLESS settings.
   This is declared-configuration comparison, not cryptographic proof that public
   and private keys match; that remains an end-to-end connection acceptance gate.
6. **Safe result/errors:** only the five dataclass fields above; no target/URL,
   UUID/email, raw dictionary, cookie, config URI or private key retained. Frozen
   repr/asdict must contain no synthetic secret sentinel. Input data unchanged;
   return values/errors produce no stdout/logs. NodePanelError retains its static
   code unchanged; collector-generated errors use VpnEndpointError with static
   strings and `from None`. No broad BaseException wrapping.

- [x] Test the public collector with successful REALITY and legacy observations,
  client absent/disabled, wrong identity, missing/extra/duplicate/noninteger API
  IDs, DB ID drift between reads, malformed DB/missing table/missing path without
  file creation, SQL read-only enforcement, status/cache semantics, all transport
  mismatches, TLS unsupported, flow override handling, secret sentinel inputs,
  unchanged database bytes/identity, and preserved existing module import API.
- [x] Verify RED then GREEN with fresh worktree/backend `.pytest_cache` basetemp.
  Run new tests + identity + endpoint resolver tests, then backend Ruff and diff
  whitespace check. Review specification first, quality second; parent commits
  only owned files and this reviewed plan.

### Verified checkpoint

Implementer observed the initial missing-module RED, then 344 passing observer,
identity and endpoint tests. Separate specification and quality reviewers each
independently ran those 344 tests and approved without findings. Parent ran the
same group plus node HTTP tests: **465 passed in 59.16s**. Backend Ruff passed;
diff whitespace check passed (only repository LF/CRLF conversion warnings).
No production installation, mutation or bound-profile barrier removal occurred.
The separate real-node authentication probe stopped before login because no
panel password was saved; local tests do not substitute for live authentication.

## Next integration boundary

Mutation code must additionally compare the full preserved client policy, durably
record intent, serialize on-node writes, fence old generations, and perform a
postcondition/runtime check. A node ledger must keep interrupted/ambiguous writes
unresolved instead of retrying automatically. This collector does not bypass that
boundary or turn the current legacy barrier into a deployable endpoint adapter.
