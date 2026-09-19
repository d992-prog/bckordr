# VPN Operational Hardening And Telegram MVP Design

**Date:** 2026-09-19

## Goal

Turn the existing VPN administration module into a dependable, payment-free VPN service that can be operated from the current control panel and used by customers through Telegram.

The release is complete when an administrator can create a customer and subscription, issue a working 3x-UI key on a safe node, deliver and inspect that key through Telegram, and rely on automatic expiration, revocation, and retry behavior.

## Scope

This iteration includes:

- subscription and access-key validation;
- healthy VPN-node selection;
- drop-catching safety gates;
- automatic VPN lifecycle maintenance;
- reliable key provisioning and revocation retries;
- a minimal Telegram customer interface;
- admin-panel visibility and retry controls;
- environment and operations documentation;
- regression tests for the VPN and existing domain runtimes.

This iteration excludes:

- payment providers;
- automatic paid renewals;
- referral, coupon, reseller, and affiliate systems;
- customer-facing web or mobile applications;
- traffic accounting beyond passing the configured limit to 3x-UI;
- importing the YadrenoVPN application or its database into this project.

The local YadrenoVPN checkout remains a behavior and UX reference only.

## Architecture

The existing FastAPI control server remains the single source of truth for VPN nodes, plans, customers, subscriptions, keys, Telegram updates, and events. PostgreSQL remains the authoritative database. The existing React panel remains the administrative interface.

3x-UI continues to run on explicitly VPN-enabled worker servers. Control reaches those servers through the existing SSH maintenance channel. The first production iteration does not introduce a second VPN microservice or a second database.

The VPN implementation is separated into focused services:

- `vpn_policy.py` decides whether a node is healthy and safe for provisioning or maintenance.
- `vpn_provisioning.py` owns 3x-UI client creation and revocation.
- `vpn_lifecycle.py` owns subscription expiration and retryable key-state transitions.
- `vpn_telegram.py` owns Telegram update parsing and customer responses.
- a dedicated Telegram router validates and accepts webhook updates.

The control runtime invokes lifecycle maintenance periodically. API endpoints reuse the same policy and lifecycle functions instead of implementing parallel behavior.

## VPN Node Eligibility And Drop-Catching Safety

A node is eligible for new VPN keys only when all of the following are true:

- `vpn_enabled` is true;
- `vpn_runtime_status` is `ready`;
- the node is enabled and not offline or disabled;
- SSH access is configured;
- a VPN inbound ID is configured;
- a public VPN host is configured;
- the node is not assigned to a queued or running task in a planned or running attack run.

Node selection is deterministic. Explicitly requested eligible nodes are used as requested. Without an explicit node, control selects the eligible node with the fewest active VPN keys, then the oldest last-check timestamp, then the lowest worker ID.

VPN install, update, restart, autoconfiguration, inbound creation, and new key provisioning are rejected while the node is assigned to an active drop-catching run. A health check may still run because it is read-only. Existing VPN clients are not stopped or throttled during domain attacks.

If no safe node exists, the access key remains `pending_sync` with a clear error. Lifecycle maintenance retries it later.

## Subscription Rules

A subscription may issue keys only when:

- its status is `active` or `trial`;
- `starts_at` is absent or not later than the current time;
- `expires_at` is absent or later than the current time;
- its customer exists and has status `active`;
- the number of keys in `pending_sync`, `syncing`, `active`, or `pending_revoke` is below `max_devices`.

When a subscription is created from a plan and values are omitted:

- `starts_at` defaults to the current time;
- `expires_at` is calculated from `starts_at + duration_days` when the plan has a duration;
- `traffic_limit_gb` and `max_devices` inherit from the plan.

An explicitly supplied subscription value overrides the plan default.

Updating a subscription to `cancelled`, `disabled`, or `expired` makes all active or retryable keys eligible for revocation during the same transaction or the next lifecycle cycle.

## Access-Key State Machine

The supported key states are:

- `pending_sync`: the key exists in control but is not confirmed in 3x-UI;
- `syncing`: provisioning is in progress;
- `active`: the client exists in 3x-UI and has a usable config URI;
- `pending_revoke`: access must be removed but removal is not yet confirmed;
- `revoked`: 3x-UI removal is confirmed;
- `failed`: the record cannot be retried without administrator correction.

Provisioning is idempotent by stable external UUID and stable client email. Retrying a `pending_sync` key updates or recreates the same logical 3x-UI client instead of issuing a second identity.

Revocation is idempotent. A missing client in 3x-UI is treated as already revoked. Transport, SSH, and node-availability errors result in `pending_revoke`, retain the control record, and store a bounded diagnostic message.

The legacy delete endpoint becomes a revoke-and-retain compatibility action and never removes the key record. It returns the retained key as `revoked` when 3x-UI removal is confirmed or `pending_revoke` when removal must be retried. Historical revoked keys remain visible in the panel. No force-delete action is included in this release.

## Automatic Lifecycle

The control runtime runs VPN lifecycle maintenance on a configurable interval, defaulting to 60 seconds. Only one lifecycle cycle may run inside a control process at a time.

Each cycle:

1. marks elapsed `active` and `trial` subscriptions as `expired`;
2. selects keys belonging to terminal or elapsed subscriptions for revocation;
3. retries `pending_revoke` keys on safe reachable nodes;
4. retries `pending_sync` keys for valid subscriptions, automatically selecting a safe node when needed;
5. records aggregate results and per-node events;
6. catches and records failure per key so one failing node does not abort the entire batch.

Automatic retries use a bounded batch size. A single cycle must not monopolize the scheduler or run concurrent SSH changes against the same node.

The manual `Run lifecycle` endpoint calls the same service and returns counts for expired subscriptions, provisioned keys, revoked keys, pending keys, skipped unsafe nodes, and failures.

## Telegram MVP

Telegram uses an HTTPS webhook on the control server. The route contains a configured secret and also validates Telegram's secret-token header when configured. An invalid secret receives an authorization error without parsing or storing the payload.

Supported customer actions:

- `/start`: create or update the VPN customer using Telegram identity and display the current status;
- `/status`: show active or trial subscriptions and their expiration dates;
- `/keys`: show active config URIs for valid subscriptions only;
- `/support`: show the configured support message;
- Russian reply-keyboard buttons for status, keys, and support map to the same handlers.

The bot does not create paid subscriptions. If a customer has no usable subscription, it explains that access must be enabled by the administrator.

Every Telegram update is stored in `vpn_telegram_updates` by unique `update_id`. Duplicate updates return success without repeating database mutations or messages. Processing failures are stored on the update and return a successful webhook acknowledgement after logging, preventing an endless Telegram retry loop. Authentication failures still return an error.

Telegram delivery failures are stored on `VpnTelegramUpdate.error_message` and exposed in admin telemetry without rolling back customer, subscription, or key state. When delivery concerns a key with an assigned worker, a node event is also recorded.

## Configuration

The backend settings add:

- `VPN_LIFECYCLE_ENABLED`, default `true`;
- `VPN_LIFECYCLE_INTERVAL_SECONDS`, default `60`;
- `VPN_LIFECYCLE_BATCH_SIZE`, default `50`;
- `VPN_TELEGRAM_BOT_TOKEN`, default empty;
- `VPN_TELEGRAM_WEBHOOK_SECRET`, default empty;
- `VPN_TELEGRAM_SECRET_TOKEN`, default empty;
- `VPN_SUPPORT_TEXT`, with a short Russian default message.

An empty bot token disables outbound Telegram calls. The webhook route is always registered, but returns `503` until both the bot token and webhook secret are configured. Production documentation requires non-empty secrets.

Secrets are never returned by API response schemas. Existing stored worker SSH and 3x-UI passwords remain write-only in the admin API.

## Admin API And UI

The existing VPN page remains the administrative surface. It gains:

- eligible/blocked state and reason for every VPN node;
- explicit retry buttons for `pending_sync` and `pending_revoke` keys;
- lifecycle summary including last run time and counts;
- recent Telegram update status and processing errors;
- clear subscription validation messages;
- retained revoked-key history;
- confirmation that deletion could not complete when remote revocation is pending.

The API returns conflict responses for unsafe maintenance and invalid subscription actions. Expected remote provisioning failures remain state transitions with readable diagnostics, not unhandled server errors.

The large existing `App.tsx` is not broadly refactored in this release. VPN-only helper extraction is allowed when it directly reduces duplication introduced by these changes.

## Error Handling And Recovery

- Input and state validation failures return `400` or `409` without starting SSH work.
- Missing records return `404`.
- Telegram authentication failures return `401` or `403`.
- Remote SSH or 3x-UI failures retain retryable key state and create node events.
- Lifecycle processes keys independently and reports partial success.
- Database commits occur only after coherent state transitions.
- Error messages stored in the database are length-bounded and do not include credentials.

## Migration And Compatibility

No destructive migration is allowed. Existing plans, customers, subscriptions, and keys remain usable. Startup migrations add only fields or indexes required for lifecycle visibility or retry metadata.

Existing `active` keys remain active. Existing `pending_sync` keys enter the retry flow. Existing expired subscriptions are detected on the first automatic cycle.

The worker protocol used for domain registration is unchanged. Existing VPN clients continue operating while control is upgraded.

## Testing

Backend tests cover:

- subscription defaults inherited from plans;
- subscription validity boundaries;
- `max_devices` enforcement;
- deterministic safe-node selection;
- exclusion of nodes with active domain tasks;
- maintenance safety gates;
- provisioning and revocation idempotency;
- retained records after failed deletion/revocation;
- automatic expiration and retry behavior;
- lifecycle isolation when one key fails;
- Telegram authentication, commands, duplicate updates, and delivery failures;
- existing attack runtime behavior.

Frontend verification covers TypeScript compilation and production build. API contract changes are reflected in `frontend/src/api.ts` and the VPN page.

Final verification includes the complete backend test suite, backend and worker Ruff checks, frontend production build, application imports, and a documented manual smoke flow against one non-critical 3x-UI node.

## Operational Acceptance

The release is accepted when:

1. an administrator can create a plan, customer, and subscription without payment;
2. a valid subscription can issue a real key on a safe configured 3x-UI node;
3. Telegram `/keys` returns that active key to the matching customer;
4. an expired or disabled subscription causes its key to be revoked automatically;
5. an unavailable node leaves a retryable record instead of losing it;
6. lifecycle later completes the pending operation when the node recovers;
7. VPN mutations are blocked on nodes participating in active domain attacks;
8. existing domain scheduling and worker registration tests remain green.
