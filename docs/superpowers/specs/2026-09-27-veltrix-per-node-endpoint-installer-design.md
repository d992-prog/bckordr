# Per-node protected endpoint installer design

## Goal

Allow the existing protected REALITY endpoint installer to onboard any explicitly
selected VPN worker without weakening its worker-binding security property.

## Design

`build_endpoint_installer_bundle` receives a required positive `worker_id` and
generates that bundle's deterministic `__main__.py` with the exact ID embedded.
The entrypoint imports `vpn_reality_endpoint_installer`, sets its existing
`CONTROLLED_WORKER_ID` before calling `main`, and therefore keeps all current
request validation unchanged. Bundles built for the same source and worker stay
byte-for-byte deterministic; bundles for different workers have different
digests and reject each other's requests.

The source module keeps its current default ID for direct unit tests and the
already deployed worker-15 artifact. No mutable node-side config, environment
override, new dependency, database migration or public API is introduced.

The control process must carry the same explicit selection independently of the
remote process. `encode_install_request`, the strict SSH executor and candidate
cleanup therefore require a positive `controlled_worker_id`; encoding rejects a
request for any other worker before opening SSH. The parser keeps its current
module binding only for the isolated remote entrypoint, whose generated bundle
sets that binding before reading stdin. Control code never changes this module
global, so concurrent onboarding of different workers cannot race.

Staging and promotion require the same explicit `controlled_worker_id` and lock
that exact enabled, ready VPN worker. A receipt for another worker fails before
an endpoint or release marker can be written. The registration module no longer
contains a worker-15 policy constant; worker 15 remains supported by passing 15
at each boundary.

Registration permits one protected endpoint per worker/inbound identity instead
of treating every ready REALITY endpoint as a global singleton. Conflicts on the
same worker and inbound or port remain rejected. The release marker stays global
and must contain the same reviewed release ID for every endpoint in that rollout.
External acceptance evidence is stored under a deterministic per-endpoint key,
`vpn_endpoint_external_acceptance_v1:<worker_id>:<inbound_id>`, so promotion of a
second worker cannot overwrite or conflict with the first worker's evidence.
The legacy unscoped evidence row is preserved but ignored; no migration or
destructive rewrite of the existing worker-15 proof is required.

## Validation and failure behavior

The builder rejects booleans, zero, negative values and integers above the
existing positive-ID ceiling before writing a bundle. Existing bundle size,
import-probe, atomic publication and strict pinned-SSH checks remain unchanged.
An invalid worker request continues to fail before any 3x-UI mutation.

Tests prove the new argument is required, invalid IDs fail without publishing an
artifact, identical worker bundles are deterministic, different worker IDs
produce different bundles, and a worker-2 bundle accepts worker 2 while rejecting
worker 15. Separate control-process tests keep its module default at 15 while
proving an explicit worker-2 request reaches the strict SSH boundary and a
mismatched request does not. Registration tests prove worker 2 can be staged and
promoted only when the explicit selection is 2, and that mismatches write
nothing. A production-topology regression test starts with a ready worker-15
REALITY endpoint, its legacy evidence and the shared release marker, then proves
worker 2 can be staged and promoted without changing the first endpoint or its
evidence. The focused endpoint deployment, installer and registration suites,
Ruff and the broader VPN regression suite must pass before production onboarding
resumes.

## Production rollout boundary

This code change only removes the per-node source-code edit. Production worker 2
is still onboarded separately through the reviewed sequence: backup, exact
Ed25519 pin installation, 3x-UI installation, worker-bound candidate deployment,
REALITY endpoint creation, strict health, capacity and a real external client
test. Public trial, ready notifications and payment remain disabled throughout.
