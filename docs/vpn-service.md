# VPN Service Operations

## Current Scope

The service supports manually created plans and subscriptions, automatic lifecycle maintenance, 3x-UI access keys, and Telegram delivery. Payments are intentionally disabled until the rest of the product is operational and verified.

The Telegram bot supports `/start`, `/status`, `/keys`, and `/support`. A VPN URI is a credential: only use the bot in a private chat, protect the webhook with both configured secrets, and never paste a URI into logs or public channels.

## Public Trial Release Candidate (Disabled)

The public seven-day trial is implemented but is not a production rollout.
Payments are intentionally absent. Keep these exact fail-closed defaults until a
separate release-operations approval:

```dotenv
VPN_PUBLIC_TRIAL_ENABLED=false
VPN_PUBLIC_TRIAL_RELEASE_ID=
VPN_PUBLIC_TRIAL_PLAN_SLUG=trial-7d
VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300
VPN_FLEET_HEALTH_ENABLED=false
VPN_FLEET_HEALTH_INTERVAL_SECONDS=120
VPN_READY_NOTIFICATIONS_ENABLED=false
```

Do not enable the public path merely because the code is deployed. Every item
below is required at the same time:

1. A reviewed migration backup and separate deployment approval exist.
2. A second production VPN node has been externally verified, including real
   HTTPS traffic through its issued client profile.
3. The strict dispatcher is enabled and its node trust/configuration has passed
   the release checks.
4. The configured active plan has slug `trial-7d`, exactly seven days and one
   device.
5. At least one verified ready REALITY endpoint has an explicit positive
   capacity and a healthy, enabled, unarchived, VPN-ready worker checked within
   300 seconds.
6. `VPN_PUBLIC_TRIAL_RELEASE_ID` is a reviewed lowercase 64-hex release ID, and
   the value of the exact database marker `vpn_public_release_ready_v1` matches
   it. The public-trial flag is enabled only after that marker is written.
7. Ready notifications are enabled separately only after Telegram delivery and
   retry monitoring are accepted.

### Release-readiness gate

The admin panel and `GET /api/control/vpn/release-readiness` use the same
authoritative evaluator. It is fail closed: any failed prerequisite keeps the
release unready. Missing operational/watchdog or backup observations are shown
in red and block the marker; this is expected until the later operations tasks
wire those observations.

External endpoint confirmation is evidence that a real client used the VPN from
outside the control network. The server stores the confirmation time and a hash
of the endpoint's public identity. Any change to that identity invalidates the
evidence and requires a new external test. The readiness response and admin
panel expose only fixed check text, state, timestamps and numeric entity IDs;
they never expose a VPN URI, UUID, public host or configuration fingerprint.

All readiness routes are admin-only. State-changing requests additionally
require an exact same-origin `Origin` header and an exact JSON body with no extra
fields: `{"confirmed":true}` for an external test and
`{"confirmation":"ГОТОВО К РЕЛИЗУ"}` for the release marker.
`Зафиксировать готовность` takes the VPN mutation and database table locks,
reloads and re-evaluates the authoritative state, and writes only the database
marker `vpn_public_release_ready_v1` for the configured lowercase 64-hex release
ID (with the normal admin audit record). It does **not** edit `.env`, enable
`VPN_PUBLIC_TRIAL_ENABLED`, enable payment, provision or revoke profiles, or
restart VPN services.

Operator flow after the remaining observation collectors exist:

1. Set explicit positive capacity for each release endpoint.
2. Obtain fresh strict worker and endpoint health for the complete release fleet.
3. Test each endpoint from an external client and confirm that test in the panel.
4. Inspect every panel check; do not continue while any item is red.
5. Type the exact phrase `ГОТОВО К РЕЛИЗУ` and press
   `Зафиксировать готовность` to commit the marker.

Enabling the public trial is a separate release. Take a fresh reviewed backup
and fresh operational observations before that release; do not treat a stored
readiness marker as permission to turn on the flag later.

### Strict fleet health (disabled by default)

Strict fleet health is a read-only release signal, not an update or recovery
mechanism. Keep `VPN_FLEET_HEALTH_ENABLED=false` when code and migrations are
first deployed. The interval defaults to 120 seconds and accepts only finite
values from 30 through 3600 seconds. The separate endpoint freshness window is
`VPN_ENDPOINT_HEALTH_MAX_AGE_SECONDS=300`. Enabling the scheduler also requires
the existing `VPN_CONTROL_KNOWN_HOSTS_PATH` to name a private file containing
the exact literal Ed25519 host-and-port pin for the target node.

Each cycle probes at most one eligible endpoint, choosing the oldest attempted
health check by `health_checked_at` so one failing node cannot starve the rest
of the fleet. PostgreSQL uses one global transaction-scoped advisory lease to
prevent probes from different control processes from overlapping. The
process-local lock is only a development fallback for SQLite and is not a
production coordination mechanism.

The controller uses the pinned strict SSH transport and the single fixed remote
command `/usr/bin/python3 -I -S /opt/veltrix-vpn/current/vpn-node.pyz --health`.
No SSH config, agent, default key, ambient host trust, PTY or caller-supplied
command is accepted. Successful execution proves the installed node runner is
reachable; the read-only command then checks 3x-UI/Xray runtime, the expected
inbound and REALITY identity, and that the public `LISTEN` socket belongs to the
pinned Xray process. It does not restart or update software, change clients or
inbounds, read client identities, or collect traffic content or DNS requests.

The health request and receipt are bounded, exact JSON messages. They contain
only the endpoint target and fixed state/error values: never panel credentials,
SSH material, client UUIDs or VPN URIs. Logs use static messages. A failed probe
creates a bounded node event containing only the numeric endpoint ID and fixed
error code, and the same consecutive error is deduplicated.

A failed health attempt immediately sets a safe error and blocks that endpoint
from **new** public allocation, release readiness and external reconfirmation.
Stale health blocks those same fresh-health gates; an endpoint at capacity also
blocks new allocation and readiness. Existing client profiles are not revoked
or mutated. Failure preserves the last successful `verified_at`, endpoint
identity and existing external proof. External proof is cleared only when its
stored public-identity fingerprint no longer matches the current endpoint.

The old maintenance action `vpn_check` remains a legacy diagnostic path using
`known_hosts=None`. It is deliberately excluded from automated fleet health and
is not accepted as release-readiness evidence. The readiness panel is the one
authoritative health view; do not add a second update endpoint or a per-node
health/update workflow.

The node-side 3x-UI SQLite inventory is observed from a stable rollback-journal
main file without changing the database or its directory. A live WAL, SHM or
journal sidecar fails closed. If the installed 3x-UI version actually switches
the live database to WAL, provision and review a safe read-only filesystem
boundary that can present a consistent snapshot before enabling fleet health.
Do not work around this by marking the live database immutable or by modifying
it during a health check.

### Strict fleet rollout

1. Deploy the reviewed code and additive migration with fleet health disabled.
   Do not enable payment, the public trial, or ready notifications.
2. For every existing node, install the same reviewed deterministic node bundle
   through the existing fleet-wide `vpn-update-all` path (and
   `vpn-autoconfig-all` where the standard configuration must be installed).
   There is no per-node custom implementation or update script.
3. On one existing node, verify its stored credentials, exact Ed25519 pin,
   installed bundle hash, read-only health result and one real external client
   connection. Confirm the external test only after that connection succeeds.
4. Enable only `VPN_FLEET_HEALTH_ENABLED=true` and observe the readiness panel.
   With the current single-node production fleet, readiness is still expected
   to fail for the missing second distinct worker and for any unset capacity,
   stale health or missing external proof.
5. Add the second VPS through the same one-time onboarding: create the worker,
   install credentials and its exact Ed25519 pin, install the common bundle,
   create the REALITY endpoint, set capacity, obtain fresh strict health, and
   complete the external client test. Later nodes use this identical process.
6. Keep payment and public trial activation out of this rollout. They require a
   separate reviewed release after every readiness check is green.

### Customer-facing behavior

- The one-time right is shared by the bot, cabinet and friend-invitation
  history. A redeemed invitation consumes the public trial. Expired, suspended,
  revoked and otherwise used trials are not reissued.
- The bot adds `Получить 7 дней` only when the public flag is enabled.
  `/start` offers without activating. Trial activation is private-chat only and
  commits before reply; a retry after delivery failure returns its stored
  bounded outcome without creating a second chain. The replay data contains no
  UUID, URI or raw error.
- A newly authenticated Telegram identity is admitted to the cabinet only while
  the public flag is enabled. Status uses the existing authenticated portal
  session; activation additionally requires its CSRF token. The card exposes the
  bounded states `disabled`, `available`, `capacity_paused`, `preparing`,
  `active` and `used`. Disabling the admission flag denies fresh identities but
  does not strand an existing, unexpired and non-revoked public trial while its
  valid release ID remains configured. OIDC admission is locked and rechecked
  before session issue.
- Preparing status polls serially every two seconds for no more than 30 attempts.
  The customer can continue with manual refresh after polling stops. Logout or a
  new session invalidates stale polling work.

### Allocation and administration

Capacity includes keys in `pending_sync`, `syncing`, `active`,
`pending_suspend`, `suspended`, `pending_revoke` and `failed`; `revoked` is
excluded. Eligible endpoints are ranked by utilization and then ID. Activation
locks the worker and endpoint and rechecks eligibility and occupancy before
commit. If no slot survives that check, the customer keeps the unused trial
right and sees `capacity_paused`. Friend-invitation redemption uses the same
locked selector and capacity pool, so public and invited identities cannot both
consume a one-slot endpoint.

`VPN_PUBLIC_TRIAL_ENABLED` gates new admission and activation, not queue drain.
After it is turned off, the strict dispatcher can finish existing queued
provision, suspend, revoke and expiry work only while its master switch is
enabled, legacy public portal access is false, the configured public release ID
is valid and `vpn_public_release_ready_v1` matches it exactly. An empty release
ID keeps the dispatcher off and fail closed; new activation also rejects
`VPN_PORTAL_PUBLIC_ACCESS=true`.

The admin capacity editor changes only `max_active_profiles` and
`capacity_warning_percent`. Capacity may be unset or 1..100000; warning is
1..100. Each change is audited with numeric values. Treat an unset limit as
ineligible for public allocation, not as unlimited public capacity.

### Ready notification operations

Notification candidates require an active key with a non-empty URI, an active
and unexpired active/trial subscription, an active customer and a Telegram ID.
The dispatcher claims one row with `FOR UPDATE SKIP LOCKED` and commits before
network I/O. A successful delivery sets `ready_notified_at`. An ordinary failure
clears the claim, records a bounded event and defers retry for one minute; the
send timeout is 30 seconds. Claims older than two minutes may be reclaimed, and
the runtime starts at most one notification task at once.

The message tells the customer to open the cabinet and never contains the VPN
URI. Delivery has the unavoidable at-least-once edge: a process crash after
Telegram accepts the message but before success is recorded can lead to a
duplicate when the stale claim is reclaimed.

### Non-production verification boundary

The release-candidate browser smoke uses only disposable data, a fake endpoint
and a fake plan; it must never target a production node or Telegram webhook. The
completed local smoke covered desktop and mobile admission, the available card,
one activation, preparing and manual refresh, a capacity-paused second user and
an admin capacity edit from 1/80% to 2/75%. Automatic single-flight polling was
observed; the exact 30-attempt ceiling is asserted by the frontend test suite.
Production flags remain off and no production rollout is part of this checkpoint.

The backend release gate used a composite because the external SSH tunnel reset
near the end of the final full run. That current-revision run completed 2,321
node IDs successfully, skipped only the Windows POSIX ownership/mode check and
reported connection-loss setup errors for exactly two Telegram/PostgreSQL node
IDs. Both interrupted node IDs then passed on a fresh disposable PostgreSQL
cluster with no skip. The composite therefore covers all 2,324 collected backend
node IDs with no PostgreSQL failure or PostgreSQL skip; it is not represented as
one uninterrupted green run. The separate local run passed 2,245 tests and
skipped 74 real-PostgreSQL cases covered above, four unavailable Windows symlink
cases and the same POSIX-only check. Full Ruff, all 72 frontend tests, the
two-entry Vite production build and `git diff --check` passed. Final independent
cleanup verification found the temporary PostgreSQL port closed, no temporary
cluster directory and the existing control service active.

Post-integrated-review verification was focused real PostgreSQL plus a fresh
full local run, not another uninterrupted full environment run, so the composite
above remains the broad database evidence. The complete public-trial PostgreSQL
file passed 7/7 with no skip in 203.24 seconds, including public-versus-friend
contention for one capacity slot. Independent cleanup again found the tunnel port
closed, no matching temporary cluster directory and the control service active.
The refreshed local backend passed 2,256 tests and skipped 80: 75
real-PostgreSQL cases, four unavailable Windows symlink cases and the POSIX-only
ownership/mode check. Full Ruff, all 72 frontend tests, the two-entry Vite build
and `git diff --check` passed.

## Safe Node Rollout

1. Enable the VPN role on one non-critical worker and configure its SSH access,
   public host and 3x-UI credentials.
2. Install the exact reviewed Ed25519 host pin and the common deterministic node
   bundle, then create or autoconfigure the expected REALITY inbound.
3. Obtain a fresh strict fleet-health result and inspect every blocking reason in
   the readiness panel. Do not use legacy `vpn_check` as release evidence.
4. Set an explicit capacity, create one test customer/subscription/key, and test
   the profile from a real external client before recording external proof.
5. Revoke the test key and confirm it remains in history as `revoked`.

VPN-changing maintenance and key mutations are blocked while the same worker is running an active domain attack. Health checks remain available, and existing VPN clients continue working. Wait for the attack to finish before retrying a mutation.

## Safe Node Decommission

Use `Удалить ноду` in the VPN screen or `Удалить` in the worker list when a server must no longer participate in the system. Confirm the warning only after checking that the node has no active domain attack or maintenance job. The API rejects the operation with a conflict while either one is active.

Decommissioning archives the worker instead of deleting its history. It disables the worker, removes its control and stored SSH/3x-UI credentials, excludes it from task and VPN selection, and marks its unfinished or active VPN keys as locally `revoked`. The key rows and worker assignment remain available for audit, but their configuration URIs are cleared. Because the control server cannot prove a remote revoke after access is lost, the key history explicitly records that remote removal was not confirmed.

This operation does not contact, erase, or secure the remote VPS and does not remove its 3x-UI inbound. After decommissioning, separately delete the server at the hosting provider or rotate/block its credentials and network access.

## Telegram Webhook

Configure these environment values and restart control:

```dotenv
VPN_TELEGRAM_BOT_TOKEN=<token from BotFather>
VPN_TELEGRAM_WEBHOOK_SECRET=<long random URL secret>
VPN_TELEGRAM_SECRET_TOKEN=<long random Telegram header secret>
VPN_SUPPORT_TEXT=Напишите администратору для подключения или продления VPN.
```

Lifecycle safety defaults are `VPN_LIFECYCLE_KEY_TIMEOUT_SECONDS=30` per key and `VPN_LIFECYCLE_CYCLE_TIMEOUT_SECONDS=300` for the complete background run. Increase them only when measured node latency requires it; lifecycle must never be allowed to block the timing-sensitive attack scheduler.

Register this webhook URL with Telegram:

`https://CONTROL_HOST/api/vpn-telegram/webhook/VPN_TELEGRAM_WEBHOOK_SECRET`

Pass `VPN_TELEGRAM_SECRET_TOKEN` through Telegram's `secret_token` webhook option so Telegram sends the `X-Telegram-Bot-Api-Secret-Token` header. Requests with a wrong path secret or header secret are rejected before their payload is processed.

Example request with placeholders:

```bash
curl --request POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  --data-urlencode "url=https://CONTROL_HOST/api/vpn-telegram/webhook/<WEBHOOK_SECRET>" \
  --data-urlencode "secret_token=<TELEGRAM_SECRET_TOKEN>"
```

## Recovery

- If a client says connected but sites do not load, verify an actual HTTP/HTTPS request through the VPN. An open TCP port alone does not verify the tunnel. On the affected node, plain VLESS payload on port 443 was filtered before reaching Xray although TLS traffic reached it; moving the existing inbound to 8443 restored traffic. New auto-created plain VLESS inbounds therefore avoid 443. After changing an inbound port, update the node metadata and saved client links, then reimport the link in the client app.
- `pending_sync`: restore or configure the assigned safe ready node, then run lifecycle or press `Повторить синхронизацию`. Previously issued keys without their assigned node are not silently moved to another node.
- `pending_suspend`: subscription-driven shutdown is not yet confirmed; restore node safety and run lifecycle again. Until confirmation, the client may still have access.
- `suspended`: the subscription expired, was paused, or has not started. The client is disabled without deleting its UUID or traffic counters. Renew/reactivate the subscription to restore the same link after synchronization.
- `pending_revoke`: restore the assigned node, then run lifecycle or press `Повторить отзыв`.
- Active domain attack: wait for the run to finish; existing VPN clients continue working.
- Telegram error: inspect the Telegram table and control logs, correct the token or network issue, then send a new command. The bot token is redacted from persisted errors.
- Duplicate Telegram update: no action is required; the update ID is stored once and the response is not resent.

Revocation never deletes an access-key row. A successful remote revoke produces `revoked`; an unsafe or unavailable assigned node produces `pending_revoke` for a later retry.

## Subscription policy synchronization

Changing subscription status, dates, traffic allowance or device limit atomically
queues affected existing keys. Notes-only edits do not rewrite node clients.
`pending_sync` means the latest policy has not yet been confirmed on the node;
saving the subscription alone does not imply remote success. The scheduled worker
retries pending operations even when the browser's immediate maintenance request
fails. Automatic and manual VPN mutations share the single control process's lock.
Run one control process for this scheduler; the lock is not a distributed lock.

Ordinary renewal keeps the UUID, assigned node and existing valid client link.
It does not reset consumed traffic. Reducing the device limit below retained keys
is rejected: permanently revoke excess keys first. A key's device setting remains
the existing 3x-UI IP/client limit, not a precise count of physical devices.

Expiry and `disabled` use reversible suspension. Manual revoke, subscription
cancellation and customer archive remain permanent. Neither renewal nor customer
restore reactivates historical `revoked`/`pending_revoke` rows; old rows do not
reliably distinguish a lost-device revoke from expiration. Only keys using the
new suspension states restore automatically. Node decommission also permanently
retires suspended keys and clears their saved links.

3x-UI updates preserve usage and non-policy client fields in both legacy inbound
JSON and normalized schemas. Provision/suspend success requires a confirmed
service restart. These operations can briefly reconnect other clients on the
same node because the current integration restarts the 3x-UI service.

## Smoke Test

1. In the VPN screen, create an active plan with a short test duration and at least one device. Expected: the plan appears in `Тарифы`.
2. Create a customer with the Telegram numeric user ID used for the private bot chat. Expected: the customer is `active`.
3. Create an active subscription for that customer. Expected: start time is set and plan defaults are copied.
4. Create a key with automatic node selection. Expected: a safe node is selected and the key becomes `active`; otherwise it remains `pending_sync` with a useful reason.
5. Import the resulting `vless://` or `vmess://` URI into a test device and confirm connectivity.
6. Send `/start` to the bot. Expected: `VPN-бот готов`, payment-disabled notice, and the subscription ID.
7. Send `/status`. Expected: the active subscription and expiration are shown.
8. Send `/keys`. Expected: only active keys belonging to currently valid subscriptions are returned.
9. Send `/support`. Expected: the configured `VPN_SUPPORT_TEXT` is returned exactly.
10. In admin, set the subscription expiration into the past and run `Обслужить VPN` (or wait for the scheduled lifecycle interval).
11. Expected: the subscription becomes `expired`; its key becomes `suspended`, or `pending_suspend` if the assigned node cannot be changed safely.
12. If it is `pending_suspend`, restore node safety and run lifecycle again. Expected: the key becomes `suspended` and remains visible in history.
13. Send `/keys` again. Expected: the suspended URI is no longer returned.
14. Renew the subscription and run lifecycle. Expected: the same key returns to `active`, UUID/link stay unchanged and the tunnel passes actual HTTPS traffic.
15. Manually revoke the test key and renew again. Expected: the key stays `revoked` and does not return on the node.
