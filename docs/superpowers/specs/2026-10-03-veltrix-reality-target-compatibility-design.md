# Veltrix REALITY Target Compatibility

## Problem

The protected VLESS TCP REALITY profile reaches the VPN node, but Hiddify on
Windows closes tunneled traffic with `EOF`. The issued client parameters and the
running Xray inbound match, the node can reach its camouflage destination, and
REALITY verification succeeds after allowing third-party client versions.

The remaining failure matches the upstream Xray report for
`www.microsoft.com`: its TLS Certificate record can exceed REALITY's 8192-byte
parser limit. The failure affects sing-box-based clients such as Hiddify and is
not a bad UUID, blocked port, or unavailable destination.

## Decision

Use `gateway.icloud.com:443` as the default REALITY target and
`gateway.icloud.com` as the server name for protected endpoints. This is the
smallest upstream-verified compatibility change and keeps the current Xray
version, transport, UUIDs, REALITY keys, short IDs, subscriptions, and limits.

Do not add target selection to the admin UI, client-specific profiles, or an
automatic fallback system. Those would add configuration surface without being
required to restore the supported Hiddify path.

## Components and Data Flow

The endpoint installer owns the default inbound payload. It must generate the
new target and server name together. The endpoint registration and profile
generation paths continue to read the installed endpoint metadata, so newly
issued links contain the same server name used by the node.

For the existing production endpoint, update the 3x-UI inbound and the stored
endpoint metadata in one controlled rollout. Regenerate affected saved profile
URIs without changing UUIDs or ownership. Existing clients must refresh or
reimport the profile once because the REALITY server name changes.

## Failure Handling

Create backups of the control database and the node's 3x-UI database before the
production mutation. Validate the exact endpoint and inbound before changing
them. If the service does not restart, port 443 stops listening, or the generated
profile no longer matches the live inbound, restore the backups and the previous
configuration.

Do not print configuration URIs, UUIDs, private keys, short IDs, Telegram data,
or server credentials in tests, logs, or task output.

## Verification

Add a focused regression test that fails while the installer emits
`www.microsoft.com` and passes only when both the target and server name use
`gateway.icloud.com`. Run the focused endpoint tests, the complete backend test
suite, and Ruff before deployment.

After deployment, verify the service health, Xray version, active inbound
metadata by non-secret hashes, port 443, and destination HTTPS reachability.
Finally, refresh the Hiddify profile and prove real HTTPS traffic through the
tunnel. A listening TCP port or a connected UI state alone is not sufficient.

## Non-goals

- Payment integration.
- Changing tariffs or subscription policy.
- Rotating customer UUIDs or REALITY key material.
- Adding another VPN protocol or client application.
- Refactoring unrelated VPN lifecycle or admin UI code.
