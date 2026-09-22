# Veltrix friend invitations design

**Status:** approved design for the first closed beta. Implementation and
production rollout remain separate reviewed steps.

## Goal

The owner creates a one-use Telegram invitation in the existing VPN admin panel.
A friend opens it, presses Start and, without owner approval, receives one VPN
profile for seven days. The owner can see and individually disable every invited
participant. The first cohort is limited to ten people. Payments, public signup
and tariff purchasing remain disabled.

The feature is complete only when the friend can activate the invitation, see the
profile in the bot and Mini App, connect through a separately verified protected
endpoint, and lose that access after owner revoke or expiry.

## Confirmed user experience

1. The admin panel has a `Test invitations` section with ten durable slots.
2. `Create invitation` returns a link shaped like
   `https://t.me/veltrix_vpn_official_bot?start=i_<token>`.
3. The raw link is shown once and can be copied. List/read responses never return
   it. An unused slot can be rotated; a redeemed slot can never be rebound.
4. An unused link expires seven days after issue. Rotation starts a new link
   window without consuming another slot.
5. A friend opens the link in a private bot chat and presses Start. Telegram's
   verified sender ID is the only accepted identity.
6. A successful first redemption atomically binds the slot, admits that Telegram
   identity, creates one trial subscription and one endpoint-bound access key,
   and records one immutable control operation.
7. The subscription starts at that successful database commit and expires exactly
   seven days later. Retry, webhook replay and reconciliation reuse the same dates,
   UUID, customer, subscription and key.
8. The bot initially replies `Access is being prepared`. Once the node operation
   is observed, the single profile appears in the bot and existing Mini App.
9. The admin list shows slot, invite state, Telegram identity, subscription end,
   provisioning state and actions to rotate an unused link, safely retry a known
   pre-mutation failure, or disable an activated participant.

User-facing names use Veltrix/VPN terminology only. Historical project names such
as `dropcatch` never appear in invitation or profile labels.

## Scope and non-goals

This design adds invitation storage, admin API/UI, Telegram redemption, database-
backed portal admission and integration with the already implemented durable VPN
control path. It does not add payment, public registration, tariff checkout,
referrals, multi-device access, invite analytics, email/SMS or multiple cohorts.

The first cohort is deliberately fixed at ten slots and one profile per redeemed
slot. A later product decision can introduce reusable campaigns or paid plans;
the closed beta does not need those abstractions.

## Production prerequisites

Invitation code may be implemented and tested behind a disabled feature flag, but
no real link may be issued until all of these gates pass:

- the reviewed node zipapp, trust, config and new journal are deployed through
  the strict one-shot runbook;
- the production control migration has a separately reviewed backup, closed-copy
  rehearsal and explicit execution approval;
- PostgreSQL `statement_timeout`, the database driver's command timeout and
  cancellation cleanup are configured and tested;
- ambiguous finalize-COMMIT reconciliation is implemented and tested without
  automatic resend;
- one protected REALITY endpoint is `ready`, its public fields are pinned,
  and a controlled external connection test has succeeded;
- the strict dispatcher has a reviewed sequential runtime caller; the legacy
  `known_hosts=None` provisioning path cannot receive endpoint-bound beta keys;
- owner UUID, current 8443 endpoint/link and Xray generation remain unchanged.

`VPN_FRIEND_BETA_ENABLED` defaults to false. Issuing or redeeming an invitation
fails closed unless the feature and every database-visible readiness gate are
enabled. `VPN_PORTAL_PUBLIC_ACCESS` stays false.

`VPN_TELEGRAM_BOT_USERNAME` supplies the deep-link host name independently of the
secret bot token. It is validated as one canonical Telegram bot username before
link creation; an absent or invalid value makes invitation issue fail closed.

## Architecture and reuse

The implementation reuses the existing `VpnCustomer`, `VpnSubscription`,
`VpnAccessKey`, `VpnEndpoint`, `VpnControlOperation`, Telegram identity parser,
Mini App authentication, customer view, admin workspace, strict dispatcher and
customer archive/revoke flows.

Only one invitation model and one focused invitation service are added. The raw
bearer exists only in the create/rotate request process and the Telegram webhook
process. Provisioning uses the durable endpoint-bound control operation; it never
calls the legacy SSH provisioning helper and never returns raw node output.

## Invitation data model

`vpn_friend_invitations` has these fields:

- `slot`: small integer primary key with `1 <= slot <= 10`;
- `token_digest`: unique 64-character domain-separated SHA-256 digest;
- `created_by_user_id`: nullable audit reference to the admin user;
- `created_at` and `redeem_expires_at`;
- `redeemed_at` and immutable `telegram_user_id`;
- unique nullable `access_key_id`;
- `revoked_at`.

The fixed primary-key range is the database-enforced cohort cap. Issuing loops
over slots 1 through 10 using conflict-safe inserts; concurrent calls can occupy
different slots but can never create an eleventh. Redeemed and revoked slots are
not reused in this cohort. Rotation locks an unredeemed row and replaces only its
digest and issue/expiry timestamps.

One check constraint requires `redeemed_at`, `telegram_user_id` and
`access_key_id` to be either all null or all non-null. The invitation stores only
the terminal access-key link: its existing foreign keys lead to exactly one
subscription and customer, avoiding duplicate customer/subscription references
which could drift apart. Admission, presentation and revoke always join this
exact invitation -> key -> subscription -> customer chain and fail closed unless
the immutable invited Telegram ID equals the customer's current Telegram ID.
The API derives presentation states from this chain, timestamps and the linked
control operation instead of storing a second drifting state.

The startup migration must be idempotent for upgrade, repeat and fresh-schema
paths. PostgreSQL is the concurrency authority; SQLite coverage remains useful
for pure validation only.

## Token and secret handling

Tokens use `secrets.token_urlsafe(32)` and a domain-separated SHA-256 digest. The
raw value is never stored in invitation rows, Telegram update JSON, audit details,
application logs, errors, metrics, browser URL state or persistent frontend state.
Create/rotate responses carry `Cache-Control: no-store` and return the raw link
once. Losing an unused link requires rotation.

Telegram payloads are parsed and sanitized in memory before `VpnTelegramUpdate`
is inserted. For any `/start` payload, persisted message text contains only a
static redaction marker. The invite parser accepts only a bounded ASCII
`/start i_<base64url>` form; malformed, expired, revoked and wrong-owner tokens
share one generic response so they cannot be used as an oracle.

Bot token, invite token, VPN UUID, configuration URI, private SSH material and
REALITY private keys never enter admin audit text. Existing customer-facing URI
responses retain their current authenticated ownership and no-store controls.

## Atomic redemption and Telegram delivery

The invite path changes the current Telegram order deliberately:

1. Parse identity and a sanitized payload in memory.
2. Insert/claim the Telegram update and lock the invitation digest in the same
   transaction as business activation.
3. Validate private chat, sender/chat equality, feature readiness, token expiry,
   revoke state and binding.
4. Resolve or create the Telegram customer under the existing uniqueness guard.
5. Create exactly one seven-day subscription with `max_devices=1`, one stable
   access key bound to the selected ready protected endpoint, and one immutable
   provision control operation.
6. Bind the invitation to all created records and commit before any Telegram send.
7. Send the status message after commit.

A repeated delivery from the same Telegram ID returns the existing state and does
not reset seven days. Another Telegram ID can never take a bound slot. A duplicate
Telegram `update_id` performs no business mutation. If its existing update row has
no successful `processed_at`, the duplicate idempotently renders and sends the
already committed status, then records delivery success. A crash after Telegram
accepts the message but before that final commit may produce the same harmless
status twice; it can never duplicate customer/subscription/key/control records.
Pressing Start again creates a new update ID and also returns the same status.

If no protected endpoint is still ready at the locking recheck, redemption does
not bind or start the subscription. The user gets a generic temporary-unavailable
message and may retry the same invitation later.

## Portal admission

The current static owner allowlist remains valid. A new async admission helper
allows either:

- a valid configured owner Telegram ID; or
- a Telegram ID bound to a redeemed, non-revoked invitation whose customer is
  active and whose linked subscription is inside its active seven-day window.

This helper is applied to Mini App exchange, browser/OIDC callback, session lookup,
locked session/CSRF recheck and Telegram cabinet-button decisions. Every check
joins invitation -> key -> subscription -> customer and rejects a broken or
mismatched link. Revoking the invitation or archiving the customer invalidates
existing sessions and blocks new ones. Expiry closes friend admission until the
existing admin extension flow makes that same suspended subscription active
again. The Mini App URL itself is not a credential; signed Telegram data, the
session cookie and database admission remain the boundary.

## Provisioning and lifecycle

Redemption stages an endpoint-bound key through the durable control-intent service.
It never invokes `provision_vpn_access_key()` through the legacy remote helper.
The sequential strict runtime claims and commits, performs SSH without a database
session, then finalizes with a new session.

After an exact observed receipt, application finalization marks the key usable and
builds its customer URI from the protected endpoint's public fields and the stable
UUID. Full inbound responses and REALITY private material remain on the node.

Known failures before mutation may expose an admin `Retry` action which stages a
new generation using the same customer/key/expiry. An uncertain result disables
retry and shows `Needs verification`; reconciliation must observe node/journal
state and must never resend automatically.

Seven-day expiry immediately closes portal admission/sessions and stages the
existing reversible suspend lifecycle; extending that same subscription may
restore the same link. Explicit owner `Disable participant` revokes the invitation,
sets sticky permanent revoke intent and stages strict revoke, which extension can
never undo. Both paths rejoin and validate invitation -> key -> subscription ->
customer before targeting the key. For the first ten-person beta, the already
approved brief shared reconnection during confirmed suspend/revoke is acceptable.
An existing owner key or manually revoked unrelated key is never restored by
friend-subscription lifecycle processing.

## Admin API and UI

Admin endpoints use the existing admin dependency and audit model:

- list all ten invitation slots and derived states;
- create the next unused slot;
- rotate one unredeemed, non-revoked slot;
- retry only a proven pre-mutation failed provisioning state;
- revoke an invitation/participant.

List responses never contain a raw token. Create/rotate return it exactly once.
Audit entries record slot and action only.

The existing VPN customer workspace gains a compact `Test invitations` block.
Each slot shows `Unused`, `Preparing`, `Active`, `Failed`, `Needs verification`,
`Expired` or `Disabled`; it also shows the bound Telegram display identity and
subscription end when available. Copy is available only in the immediate
create/rotate success view. Destructive revoke uses the existing confirmation
pattern and explains that the participant will lose cabinet and VPN access.

## Error behavior

- Invalid/expired/revoked/wrong-owner token: one generic invitation error.
- Feature or protected endpoint not ready: temporary-unavailable, token unconsumed.
- Same user replay: existing subscription/profile status.
- Ten slots occupied: explicit admin-only cohort-full error.
- Known pre-mutation failure: friend sees preparation problem; admin may retry.
- Uncertain remote result: no retry, no second key, admin reconciliation required.
- Telegram delivery failure after commit: durable activation remains; retrying Start
  returns the same status.

Errors stored in the database use bounded static codes. Raw exceptions, remote
stdout/stderr and secrets are never persisted or sent to Telegram.

## Verification

Required automated evidence includes:

- model and idempotent migration tests for upgrade/repeat/fresh schema;
- strict token validation, redaction and no-store response tests;
- PostgreSQL races: eleven concurrent issue calls produce ten slots; two Telegram
  IDs racing one token produce one winner; same-ID replay produces identical IDs,
  UUID and dates;
- crash/retry tests around Telegram update claim, activation commit and send;
- proof that the raw token and full invitation link are absent from invitation
  rows, persisted Telegram JSON, audit, errors and captured logs; the static
  redaction marker is expected in sanitized Telegram JSON;
- DB-backed admission tests at every login/session recheck and revoke/archive;
- provisioning tests proving no legacy SSH call, one immutable control operation,
  safe known-failure retry and uncertain no-resend;
- expiry/revoke tests proving sessions close and only the invited key is targeted;
- admin API/frontend tests for one-time copy, ten slots, states and confirmation;
- complete backend/frontend suites, whole-project Ruff/build checks and independent
  specification plus quality reviews.

The release rehearsal uses a fresh closed copy of production PostgreSQL and a
synthetic node/panel/journal. Production rollout keeps the flag off through schema
and code deployment, proves no invitation can be issued, then enables one
controlled slot for an external connection acceptance test. Only after that test
passes may the remaining slots be issued.

## Rollout boundary

No invitation is ready merely because its UI or database code exists. Readiness
means the strict node runner is deployed, the protected endpoint is externally
accepted, timeout/reconciliation safeguards are active, the full invite flow is
verified, and one real participant has connected and been individually revoked.

Payments remain the final separate stage. Public access stays disabled throughout
the closed beta.
