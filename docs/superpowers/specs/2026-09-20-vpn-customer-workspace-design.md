# VPN Customer Workspace UX Design

## Goal

Turn the existing read-only VPN customer, subscription, and access-key tables into a practical admin workspace where an operator can find a customer, edit their details, control subscription access, copy the complete client configuration link, and safely archive obsolete customers.

The chosen direction is the customer-centric layout from visual option A: a searchable customer list on the left and one selected customer's details, subscriptions, and keys on the right.

## Scope

This feature covers:

- creating and editing VPN customers;
- searching and filtering customers by operational state;
- creating and editing subscriptions;
- quick subscription activation, suspension, and extension;
- viewing and copying complete VPN configuration URIs;
- issuing, retrying, and revoking access keys from the selected customer's workspace;
- safely archiving and restoring customers.

Payment collection, billing-provider integration, invoices, automatic renewals, and customer-facing checkout remain explicitly out of scope. VPN-node management and domain-drop controls are unchanged.

## Chosen Layout

The current separate creation forms and read-only tables are replaced within the VPN section by a two-pane workspace.

### Customer list

The left pane contains:

- a text search over name, Telegram username, and Telegram user ID;
- status filters for all, active, expiring soon, suspended, and archived customers;
- a `Новый клиент` action;
- one compact row per customer showing display name, Telegram identity, current subscription summary, and derived operational status.

`Истекает скоро` means an active or trial subscription whose expiration is within the next seven days. A customer with several subscriptions is summarized using the currently usable subscription first, then the most recently updated subscription. Selecting a customer opens the workspace without navigating away from the VPN page.

### Selected customer workspace

The right pane contains three ordered sections:

1. Customer profile: name, Telegram fields, status, and notes, with explicit edit, save, and cancel actions.
2. Subscriptions: every subscription belonging to the customer, with usable subscriptions first and history retained below.
3. Access keys: keys grouped under their subscription so the operator does not need to match numeric IDs across separate tables.

When no customer is selected, the pane explains how to select or create one. On narrow screens the panes stack, with the customer list above the selected customer.

The existing tariff editor remains a separate VPN card. The customer creation form opens only when requested instead of permanently consuming page space.

## Customer Editing and Archive

Normal profile edits use the existing `PATCH /control/vpn/customers/{customer_id}` endpoint. The form sends only supported fields and keeps the current row visible until the server confirms success. A successful save reloads the VPN datasets and preserves the selected customer.

Customer removal is deliberately non-destructive. The UI action is named `В архив`, not `Удалить навсегда`, and requires confirmation describing the consequences.

A new `POST /control/vpn/customers/{customer_id}/archive` operation performs the coordinated archive flow:

- set the customer status to `archived`;
- change all `active` and `trial` subscriptions for the customer to `disabled`;
- move usable access keys into the existing revoke flow;
- preserve customer, subscription, key, maintenance, and audit history;
- record one admin audit entry containing the customer ID and affected row counts.

The archive response reports disabled subscriptions, successfully revoked keys, and keys still in `pending_revoke`. Remote revoke failures do not erase history or falsely report success: the customer remains blocked locally, and pending revocations remain visible with their existing retry action.

Archived customers remain returned by the existing customer list endpoint so the archive filter can display them. Their Telegram identity remains reserved, preventing an accidental duplicate customer. Restoring uses the existing customer update endpoint to set the customer back to `active`; it does not reactivate subscriptions or restore revoked credentials. The operator must deliberately reactivate or create a subscription and issue a new key.

## Subscription Controls

Subscription fields already supported by `PATCH /control/vpn/subscriptions/{subscription_id}` become editable in the workspace:

- tariff;
- status;
- start and expiration timestamps;
- traffic allowance;
- device limit;
- notes.

The normal status choices exposed by the UI are `active`, `trial`, `disabled`, `expired`, and `cancelled`, with Russian labels and short explanations. `Приостановить` maps to `disabled`, which is already a terminal subscription state for VPN lifecycle processing and therefore causes usable keys to enter the revoke flow. Reactivating a disabled subscription does not restore an old revoked key; the UI prompts the operator to issue a new key.

Quick extension buttons add 7, 30, or 90 days. The calculation uses the later of the current expiration time and the current time, sends the resulting ISO timestamp through the existing patch endpoint, and activates the subscription when the operator confirms renewal. The calculated new date is shown in the confirmation before saving.

Creating a subscription remains supported and is pre-bound to the selected customer. Server-side tariff defaults continue to determine duration, traffic, and device limits when the operator leaves those values unset.

## Access-Key Experience

Every access key displays its public name, protocol, node, status, dates, and any actionable error. The client configuration URI is treated as sensitive data but is no longer unusably clipped:

- a compact row shows a shortened preview;
- `Показать полностью` expands a read-only, wrapping text area containing the exact URI;
- `Копировать ссылку` copies the original complete `config_uri`, never the shortened preview;
- successful copy shows a short confirmation, and clipboard failure selects the full text and explains that the operator can copy it manually.

The URI is not written to logs, toast messages, error details, or browser storage. Revoked keys and keys without a URI show an explicit explanation instead of an empty copy control.

Existing retry and revoke endpoints remain the source of truth. Active keys can be revoked only after confirmation. Failed or pending keys expose the relevant retry action. Issuing a replacement is presented as two explicit states: revoke the old key first, then create a new key for the same subscription. The UI does not claim that an old key was replaced while remote revocation is still pending.

## API and Service Boundary

Most management behavior uses the existing typed frontend calls:

- `updateVpnCustomer`;
- `updateVpnSubscription`;
- `createVpnSubscription`;
- `createVpnAccessKey`;
- `provisionVpnAccessKey`;
- `revokeVpnAccessKey`;
- `deleteVpnAccessKey`.

Only coordinated customer archive requires a new backend operation because changing a customer, several subscriptions, and several keys through unrelated browser requests could leave a partial state.

The archive transaction and row locking live in a focused VPN customer lifecycle service. It validates that the customer exists and is not already archived, updates the local policy state atomically, and records the audit entry. Revoke attempts then use the existing provisioning service. Each failure remains `pending_revoke` with its current diagnostic text. Repeating archive on an archived customer returns a clear conflict instead of duplicating work.

The frontend API adds a typed archive response and an `archiveVpnCustomer` call. No database migration is required: customer status, subscription status, update timestamps, key revoke states, and audit logs already provide the required durable state.

## State, Feedback, and Error Handling

The page keeps only presentation state locally: selected customer, search text, filter, expanded key URI, and edit drafts. Server responses remain authoritative.

Actions that write data disable their own control while running to prevent duplicate requests. Success reloads the VPN datasets, preserves the active filter and selected customer when possible, and shows a concise Russian confirmation. API validation, conflicts, and revoke problems use the existing error surface, while field-specific validation stays next to the affected input.

Empty states distinguish between no customers, no search results, no subscriptions, and no keys. Dangerous actions use confirmation copy that names the customer, subscription, or key. Keyboard focus, labeled form controls, visible button text, and mobile stacking remain required.

## Testing

Backend tests are written first for the coordinated archive behavior:

- the customer becomes `archived` without physical deletion;
- active and trial subscriptions become `disabled`, while historical terminal subscriptions remain unchanged;
- active and provisioning keys enter the revoke path;
- successful revocations and `pending_revoke` failures are counted accurately;
- restore through the existing patch endpoint does not reactivate subscriptions or keys;
- a missing customer returns `404` and a repeated archive returns `409`;
- the audit entry records the archive and affected counts;
- no payment or unrelated VPN-node state is changed.

Frontend verification covers pure view-model helpers for customer search, status filtering, expiry classification, current-subscription selection, and extension-date calculation. Manual browser checks cover editing, action loading states, archive confirmation, archive restoration, full URI expansion, exact clipboard copy, clipboard fallback, empty states, and the stacked narrow-screen layout.

Before completion, run the focused backend tests, the full backend suite, backend Ruff, the frontend unit tests, and the frontend production build. Deployment follows the existing production process, followed by authenticated API and browser smoke checks without exposing a real configuration URI in logs or screenshots.

## Non-Goals

- physical deletion of customers, subscriptions, or access-key history;
- automatic restoration of revoked credentials;
- payment-provider or billing automation;
- redesigning VPN-node maintenance;
- changing VPN protocols or 3x-UI provisioning architecture;
- changing customer-facing Telegram behavior beyond the effects of existing customer, subscription, and key statuses.
