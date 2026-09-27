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

## Validation and failure behavior

The builder rejects booleans, zero, negative values and integers above the
existing positive-ID ceiling before writing a bundle. Existing bundle size,
import-probe, atomic publication and strict pinned-SSH checks remain unchanged.
An invalid worker request continues to fail before any 3x-UI mutation.

Tests prove the new argument is required, invalid IDs fail without publishing an
artifact, identical worker bundles are deterministic, different worker IDs
produce different bundles, and a worker-2 bundle accepts worker 2 while rejecting
worker 15. The focused endpoint deployment and installer suites, Ruff and the
broader VPN regression suite must pass before production onboarding resumes.

## Production rollout boundary

This code change only removes the per-node source-code edit. Production worker 2
is still onboarded separately through the reviewed sequence: backup, exact
Ed25519 pin installation, 3x-UI installation, worker-bound candidate deployment,
REALITY endpoint creation, strict health, capacity and a real external client
test. Public trial, ready notifications and payment remain disabled throughout.
