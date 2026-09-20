# VPN subscription synchronization and reversible suspension

## Approved intent

Changing an existing subscription must update its issued keys and the assigned
VPN node. Ordinary renewal must not require reimporting a link into Happ. The
user also explicitly chose to restore the same link after subscription expiry or
suspension, while manually revoked keys must remain revoked.

Payment, new protocols, node migration, connectivity monitoring and customer
subscription-feed URLs are out of scope.

## Findings

- `update_vpn_subscription` currently changes only the subscription row.
- Lifecycle revokes using the copied key expiry and provisions only pending keys.
- Provisioning already upserts a stable UUID, but resets existing traffic counters.
- Subscription expiry, suspension and manual revoke currently share destructive
  deletion and the same `revoked` state. Historical revoked rows cannot safely be
  classified as automatically restorable.

## Chosen approach

Use the existing durable lifecycle queue, adding reversible key suspension.
Updating subscription policy atomically stages the affected keys; the regular
lifecycle worker applies the latest policy with the existing safety checks and
bounded retries. The UI may request a cycle for prompt application, but correct
behavior must not depend on that extra browser request.

Synchronous SSH-only updates were considered but would tie saving a subscription
to node availability and leave partially updated subscriptions. Reissuing keys
was rejected because it changes the client's link and does not meet the approved
restoration behavior.

## Key states and safety

- `active`: the latest queued policy has been applied successfully.
- `pending_sync` / `syncing`: creation, renewal, limit update or restoration is
  waiting for successful application. Keep an existing UUID, URI and node.
- `pending_suspend`: reversible subscription-driven disable is not yet confirmed
  on the node.
- `suspended`: the client is disabled on the node, while its UUID, URI and traffic
  counters are retained.
- `pending_revoke` / `revoked`: permanent/manual revoke or customer archive; never
  automatically restore these keys.

Only future subscription-driven disable operations use the new states. Existing
`revoked` and `pending_revoke` rows are not automatically reclassified or restored.
This intentionally favors security when historical intent is unknown.

Expiry and the subscription's `disabled` status are reversible. Cancellation and
customer archive remain permanent revocation. Restoring a customer alone does not
restore subscriptions or credentials. A blocked or archived customer cannot gain
access through renewal.

Manual revoke must also work on pending/suspended keys. Customer archive must
permanently revoke them. Node removal must account for suspended clients and
cannot silently orphan credentials that the operator expects to restore.

## Subscription changes

Policy fields are status, start/end timestamps, traffic allowance and device
limit. Editing notes or a tariff reference alone must not rewrite the node.
Tariff defaults remain the existing creation-time behavior; explicit subscription
limits are authoritative.

For an eligible active/trial subscription, a policy change stages its live and
subscription-suspended keys for synchronization. Copy the new expiry while staging
so the previous key expiry cannot win over a newly renewed subscription. The
worker must re-evaluate current subscription/customer policy immediately before a
remote operation, not act on an old snapshot.

For expiry/disable, stage reversible suspension. For cancellation/archive, stage
permanent revoke. Do not clear a pending manual revoke during renewal. A future
start time must not enable access early; lifecycle can resume an eligible
subscription-suspended key when the start time arrives.

Keep the current meaning of the device limit (issued-key slots plus the existing
node client limit). Reject a reduction below the subscription's retained,
non-permanently-revoked key count with a clear instruction to revoke excess keys
first. Do not arbitrarily choose which client loses access.

## Remote application

Upsert an existing client with the same UUID and assigned inbound. Update expiry,
traffic allowance, device limit and enabled state. Preserve upload/download and
other usage counters on updates; initialize counters only for a new client.
Renewal does not implicitly reset traffic allowance consumption.

Reversible suspension disables the existing client in all supported 3x-UI schema
layouts instead of deleting it. Handle both legacy inbound JSON and normalized
client/traffic tables. A never-created key can be suspended locally. Missing or
unavailable nodes leave remotely issued keys pending with a sanitized error.

Existing protocol/transport configuration and other clients must remain unchanged.
Confirm remote command success before marking the local key applied. Keep
timeouts, domain-worker safety checks and durable per-key checkpoints. Serialize
conflicting subscription/key/lifecycle mutations in the control process and use
the existing database locking conventions; re-read state after acquiring locks.

## UI and API feedback

Keep the existing subscription update response compatible. Reload keys after
saving/renewing and clearly label synchronization and suspension states in
Russian. A saved subscription is not proof of successful node synchronization.
Pending errors remain visible and retryable. Update suspension/renewal copy to
explain that the existing link will resume after successful synchronization.

The lifecycle response gains suspension counts without removing current fields.
Extend existing frontend types and tests. No payment controls or new UI layout.
The new string states do not require a database schema migration.

## Verification and rollout

Write failing regression tests before production changes, covering:

1. Extension, shortening, unlimited expiry, traffic/device changes and notes-only
   updates; unchanged UUID/URI/node and no unintended traffic reset.
2. Renewal after the old key expiry does not trigger stale-expiry revocation.
3. Expiry/disable followed by renewal restores the same key, including renewal
   while suspension is pending; manual revoke and cancellation never restore.
4. Inactive customers, invalid device reductions, future starts, node failures,
   safe-node restrictions and retry recovery.
5. Legacy and normalized remote schemas preserve counters and other clients,
   actually disable/resume the selected client, and report remote failure.
6. Customer archive, node removal and conflicting lifecycle/admin operations do
   not resurrect a permanently revoked key.
7. Frontend state labels and save/renew feedback.

Run focused then full backend tests, Ruff, frontend tests and production build.
After authorized deployment, verify health and use an isolated temporary test
subscription/key for renewal, disable and restore checks. Do not suspend the
user's working `test1` profile for testing. Remove/revoke temporary test access
after verification and confirm its absence from the node.

## Compatibility

This supersedes the earlier customer-workspace design's blanket requirement to
issue a new key after subscription expiry/disable. Permanent manual revocation
and customer-archive semantics remain unchanged. If the assigned node itself has
been removed or its address/port changed, retaining the old client link cannot be
guaranteed; node migration is a separate task.
