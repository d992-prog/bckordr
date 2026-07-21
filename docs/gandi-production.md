# Gandi Production Notes

## Current Integration

The worker now uses Gandi's domain create flow as documented in the official API:

1. `POST /v5/domain/domains`
2. if the response is `202 Accepted`, stop the hot window and report the request as accepted by Gandi
3. optional createstatus polling can be enabled with `GANDI_CREATE_STATUS_POLL_ENABLED=true` for slower diagnostics
4. when polling is enabled, treat `303` from `createstatus` as a successful registration handoff
5. when polling is enabled, treat `ERROR` or `SUPPORT` create steps as failures

## Contact Payload

The worker now sends:

- `owner`
- `admin`
- `bill`
- `tech`

All four are currently built from the single contact profile assigned through control.

The control panel can now also store and send:

- contact `mobile`
- contact `fax`
- contact `lang`
- contact `data_obfuscated`
- contact `mail_obfuscated`
- contact `icann_contract_accept`
- contact-level `extra_parameters` as JSON text
- domain-level `registration_extra_parameters` as JSON text

This means the worker and the control-side dry-run both operate on the same expanded Gandi payload model.

## Worker Settings

Useful production knobs:

```env
GANDI_CREATE_STATUS_POLL_ENABLED=false
GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5
GANDI_STATUS_POLL_MAX_ATTEMPTS=8
```

With `GANDI_CREATE_STATUS_POLL_ENABLED=false`, the hot registration window stops as soon as Gandi returns `202 Creation operation launched`. This avoids wasting the rest of the window on duplicate create requests while Gandi is already processing the order.

## Control-Side Dry-Run

There is now a control endpoint for per-domain create validation:

- `POST /api/control/domains/{id}/dry-run`

Behavior:

- uses the assigned registrar account PAT
- uses the assigned or default contact profile
- sends `Dry-Run: 1`
- stores result fields on the domain:
  - `dry_run_checked_at`
  - `dry_run_status`
  - `dry_run_http_status`
  - `dry_run_message`

Use this before first live runs with a real PAT.

## Contact Prefill

There is now a control endpoint for importing a contact draft from Gandi:

- `POST /api/control/registrar-accounts/{id}/prefill-contact`

Behavior:

- reads Gandi `organization/user-info`
- if `sharing_id` is set, also tries `organization/organizations/{sharing_id}`
- returns a draft for the contact form, it does not silently overwrite an existing saved contact

This is intended to reduce manual typing and mistakes, not to remove human review.

## Sandbox

Sandbox is useful exactly as a training and integration-check environment.

If account `api_base_url` is set to sandbox, for example:

- `https://api.sandbox.gandi.net/v5/domain/domains`

then:

- domain `Dry run` uses sandbox
- contact `Prefill` uses sandbox organization endpoints on the same host root

This gives a safer way to validate request shape and API wiring before using production.

## Important Behavioral Choice

The system currently treats an accepted-but-still-pending Gandi create request as a stop-worthy result.

Reason:

- once Gandi has accepted the create request, continuing to hammer the same registrar account for the same domain is usually not useful
- this matches the project rule that once there is a registration result, workers should move on

If you later want stricter behavior, this can be changed so that only a final success redirect stops the domain.

## What Is Still Not Covered

- separate contact profiles for `owner/admin/bill/tech`
- sandbox-specific account management in the UI
- real live proof against production Gandi from this workspace

Per-TLD `extra_parameters` are now supported, but as raw JSON text rather than typed UI forms.

The current implementation is strong enough for:

- one Gandi PAT
- one shared contact profile
- production endpoint
- simulate-load validation before live use
