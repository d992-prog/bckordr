# Protected REALITY Endpoint Installation Design

Date: 2026-09-23. This design covers local tooling only. It does not authorize a
production execution, deployment, firewall change, client creation, or beta release.

## Goal and boundary

Install and inspect one protected endpoint on the already selected VPN node, then
register it on the control server only after independent external acceptance. The
node tool is a temporary root-only candidate, separate from the permanent dispatcher
zipapp. The control-side helper builds that candidate deterministically and invokes
it through the existing pinned SSH transport and fixed commands. A future rollout
uses the helper to install the candidate at its fixed path and remove it after
acceptance. Dispatcher, reconciliation, and node-deployer code are out of scope.

The endpoint contract is fixed: VLESS, RAW/TCP, REALITY, TCP port 443,
`fingerprint=chrome`, and `flow=xtls-rprx-vision`. The existing owner endpoint on
8443 is never selected or changed.

## Node-local contract

`app.services.vpn_reality_endpoint_installer` is a dependency-free library and CLI.
It reads at most 128 KiB of strict UTF-8 JSON from stdin, rejects duplicate keys and
unknown fields, requires root, and reads the existing root-owned node config and API
token files. Authentication is bearer-token only; there is no username/password or
session-cookie fallback. The panel socket remains loopback-only through the existing
pinned panel transport.

The request is version 1 and has exactly these public fields:

```json
{"version":1,"action":"ensure","worker_id":15,"public_host":"vpn.example","server_name":"front.example","short_id":"0123456789abcdef"}
```

`action` is `ensure`, `inspect`, `remove`, `add_acceptance_client`, or
`remove_acceptance_client`. Endpoint policy fields are constants,
not caller choices. `ensure` first proves complete local/panel inventory. It returns
the existing endpoint only for an exact empty-client match. A port-443 conflict,
ambiguous identity, unsupported panel/runtime state, or incomplete inventory fails
closed. If absent, it obtains a new X25519 keypair from the pinned, trusted local
Xray executable, submits the exact 3x-UI inbound payload, and rereads the complete
inventory. The private key exists only in node memory and the loopback panel request.

`inspect` is read-only and emits the same public endpoint observation. `remove` is
the guarded inverse: it requires the exact public identity and proves zero embedded
clients and zero global client attachments before deleting the exact inbound. It
then rereads complete inventory and proves absence. It cannot target port 8443.

The two acceptance-client actions are optional and disposable. They additionally
require the exact inbound ID and public receipt digest plus a canonical version-4
UUID and bounded ASCII email supplied by the control-side rollout runner. Add is
allowed only on the exact empty endpoint with no global identity collision. Remove
requires the exact UUID/email pair, exclusive attachment to this endpoint, and no
other client; it then proves the endpoint is empty again. UUID, email, generated
subscription metadata, and other client credentials never appear in stdout. The
public endpoint receipt digest excludes only the transient state field, so ensure,
external proof, and post-cleanup receipts bind the same endpoint identity.

Successful stdout is exactly one bounded canonical JSON line containing only:

```json
{"version":1,"state":"staged","worker_id":15,"inbound_id":22,"public_host":"vpn.example","port":443,"protocol":"vless","transport":"raw","security":"reality","server_name":"front.example","public_key":"...","short_id":"0123456789abcdef","fingerprint":"chrome","flow":"xtls-rprx-vision","receipt_digest":"..."}
```

States are `staged`, `already_present`, `observed`, `removed`,
`acceptance_client_present`, or `acceptance_client_removed`. Failures return a
nonzero exit and no stdout. Errors are static codes; raw panel responses, tokens,
private keys, disposable client identities, exception text, and stderr diagnostics
never cross the boundary.

## Temporary bundle and SSH execution

`app.services.vpn_reality_endpoint_deployment` creates a minimal deterministic
zipapp containing only the installer and its node-local dependencies. It compiles
every source, verifies the completed archive with an isolated import probe, fsyncs,
and atomically publishes it with a SHA-256 digest. It deliberately excludes the
permanent dispatcher entrypoint and bundle.

The strict runner reuses the existing `VpnNodeTransportSnapshot` host-key pin,
authentication options, and connect/login limits. It executes only
`/usr/bin/python3 -I -S /var/lib/veltrix-vpn/endpoint-installer-candidate.pyz`, sends
one canonical request on stdin, disables the PTY, bounds the operation and output,
and accepts only one canonical receipt whose public identity matches the request.
No remote stderr is surfaced. A failed mutating action after process creation is
reported as uncertain so rollout must inspect before retrying; an inspect failure is
reported as a transport failure. Candidate upload and deletion remain explicit
rollout steps, not hidden side effects of an endpoint action.

Candidate installation and removal use a separate fixed, isolated Python command.
Install accepts at most 512 KiB and requires the precomputed reviewed builder digest
to match before SSH. The node validates the same exact SHA-256, writes an exclusive
random temporary file in the root-owned non-writable ancestor chain, enforces root
ownership and mode 0600, fsyncs it, and runs the exact isolated import probe. It then publishes
with an atomic hard link only when the fixed target is absent; an existing target is
accepted only when mode, ownership, size, hash, and probe all match. It never
overwrites a foreign target, and every namespace change is followed by parent
directory fsync. Removal first runs an exact read-only endpoint inspection, then
removes the candidate only if the fixed target still has the expected hash, mode,
ownership, and successful import probe. Admin receipts are bounded canonical public
JSON; failures emit nothing and mutating transport ambiguity is marked uncertain.

## Control-side registration

`app.services.vpn_reality_endpoint_registration` accepts a strict parsed public node
receipt. Staging locks the worker, rejects archived or mismatched workers, refuses
any conflicting endpoint or alternate ready endpoint, and creates or confirms one
exact `vpn_endpoints` row in `staged` state. Exact repeats are idempotent; partial
matches and changed public keys fail closed.

External acceptance is a strict value object containing the public receipt digest,
the 64-hex release ID, a UTC-aware `checked_at`, and a 64-hex evidence digest produced
by the rollout runner after a real external connection. It contains no link, UUID,
private key, token, or raw probe output.

Promotion locks the worker, endpoint, acceptance-metadata setting, and release-marker
row in a stable order and
revalidates every public field. In one caller-owned transaction it changes only that
endpoint from `staged` to `ready`, sets `verified_at=checked_at`, clears the static
error field, persists only receipt/evidence digests and public IDs under
`vpn_endpoint_external_acceptance_v1`, and sets
`vpn_friend_beta_release_ready_v1` to the exact release ID.
The marker may be created or confirmed with the same value; a different existing
value is a conflict. A rollback therefore leaves both endpoint and marker unchanged.

## Safety and verification

Tests cover strict JSON, root/private-file gates, token-only construction, exact
payload shape, bounded key generation, private-material non-export, idempotent
inspection and drift refusal, port conflicts, client-protected inverse, and
post-mutation rereads. Bundle tests cover deterministic minimal contents and isolated
import. A loopback AsyncSSH server proves host-key pinning, exact fixed command,
canonical stdin/stdout, timeout bounds, uncertainty classification, bounded atomic
candidate install, foreign-target refusal, exact-hash removal, and the inspection
gate before cleanup.
Database tests cover exact idempotency, worker/endpoint conflicts, timezone-aware
acceptance, receipt binding, stale acceptance, marker mismatch, row locks, and atomic
rollback. No production command is run as part of implementation or verification.
