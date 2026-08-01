# VPN Service Module Design

**Date:** 2026-08-01

## Goal

Add a commercial VPN product to the existing Veltrix control platform while reusing the already connected server fleet safely.

The VPN module must not weaken or destabilize the domain drop-catching runtime. Drop-catching remains the priority workload. VPN is an additional product that can use the same infrastructure only when a server is explicitly allowed to run VPN workloads.

## Product Shape

- The existing control server remains the single admin panel.
- Servers can have one or more roles:
  - `drop_worker`
  - `vpn_node`
  - `drop_worker + vpn_node`
- VPN management is exposed as a separate panel section.
- Telegram becomes the customer-facing surface for VPN users.
- YadrenoVPN is used as a reference implementation for Telegram-first VPN subscriptions, key delivery, and operational flows, not copied into the project as an opaque monolith.

## Scope Decisions

- First version supports manual/admin subscription management before automatic payments.
- First version uses the existing SSH worker-maintenance channel to install and update VPN software on selected nodes.
- VPN traffic is never enabled on a server by default.
- A server must be explicitly marked as `VPN enabled`.
- Drop-catching attack windows may reserve server capacity and prevent VPN provisioning changes on those nodes.
- VPN customers do not get access to the drop-catching panel.
- Drop-catching users, VPN customers, and admin users are separate concepts even if they share the same database.

## Recommended First Slice

Build the smallest useful commercial VPN foundation:

1. VPN node role and node inventory in the admin panel.
2. Server-side installation/update action for VPN runtime.
3. VPN plans and manual subscriptions.
4. Customer records linked to Telegram users.
5. VPN access keys/config links.
6. Telegram bot commands for status, subscription, and config delivery.
7. Admin visibility for active users, expiring subscriptions, node health, and issued keys.

Payments are intentionally deferred until the operational loop works reliably.

## Core Entities

### WorkerNode Extension

Existing worker nodes receive VPN-related fields:

- `vpn_enabled`
- `vpn_status`
- `vpn_public_host`
- `vpn_install_status`
- `vpn_last_seen_at`
- `vpn_capacity_limit`
- `vpn_active_clients`
- `vpn_reserved_for_dropcatching`

This keeps one server inventory while preserving separate operational state for drop-catching and VPN.

### VpnPlan

Defines the product being sold:

- name
- duration
- traffic limit, optional
- device/client limit
- speed limit, optional
- price metadata
- enabled/disabled state

### VpnCustomer

Stores Telegram/customer identity:

- Telegram user ID
- username
- display name
- language
- status
- notes
- created/updated timestamps

### VpnSubscription

Stores active access:

- customer ID
- plan ID
- start/end timestamps
- status
- assigned node strategy
- manual/admin source for first version
- payment reference for later versions

### VpnAccessKey

Stores issued VPN credentials:

- subscription ID
- node ID
- protocol/runtime type
- client identifier
- config URL or encoded config
- revoked state
- created/revoked timestamps

Secrets and raw configs must be treated as sensitive values in API responses and UI rendering.

### VpnNodeEvent

Stores operational history:

- install/update/check actions
- node health changes
- key create/revoke actions
- Telegram delivery events
- admin actions

## Runtime Design

### Control Server

The control server owns:

- VPN nodes
- plans
- customers
- subscriptions
- key lifecycle
- Telegram bot integration
- admin actions
- audit logs

It should expose internal admin endpoints under `/api/control/vpn/...`.

### VPN Node Runtime

VPN software runs on selected worker servers. The control server installs and updates it over SSH using the same maintenance pattern already used for worker updates.

The VPN runtime should be isolated from the drop worker process:

- separate systemd service
- separate config directory
- separate ports
- separate firewall rules
- separate logs

### Telegram Bot

Telegram bot responsibilities:

- identify customer by Telegram user ID
- show subscription status
- show remaining time and limits
- send VPN config/link
- show renewal instructions
- optionally support promo code activation

Admin-only bot commands can be added later, but first version should keep admin controls in the web panel.

## Server Capacity Rules

Drop-catching has priority over VPN.

Initial rule:

- Existing VPN clients may continue using a node during drop windows.
- New VPN provisioning, key rotation, and maintenance actions should not run on nodes currently assigned to active attack runs.
- If a server is marked `drop_worker + vpn_node`, the panel must show both roles and warn when both are active.

Later rule:

- Add automatic per-node VPN capacity reservation, for example reserving CPU/network headroom for drop-catching windows.

## UI Design

### Workers Page

Add role controls:

- `Роль сервера`
- `Перехват`
- `VPN`
- `Перехват + VPN`

Show VPN status compactly on each worker card:

- VPN enabled/disabled
- VPN runtime installed/not installed
- active VPN clients
- last VPN health check

### New VPN Page

Sections:

- `Сводка`
- `VPN-ноды`
- `Тарифы`
- `Клиенты`
- `Подписки`
- `Ключи`
- `Telegram-бот`
- `События`

First version can keep these as simple tables/forms, not a complex dashboard.

### Settings Page

Add VPN/Telegram settings:

- bot token
- bot username
- default plan
- default node assignment mode
- support contact text

## API Design

Admin endpoints:

- `GET /api/control/vpn/overview`
- `GET /api/control/vpn/nodes`
- `POST /api/control/vpn/nodes/{worker_id}/enable`
- `POST /api/control/vpn/nodes/{worker_id}/disable`
- `POST /api/control/vpn/nodes/{worker_id}/install`
- `POST /api/control/vpn/nodes/{worker_id}/update`
- `GET /api/control/vpn/plans`
- `POST /api/control/vpn/plans`
- `GET /api/control/vpn/customers`
- `POST /api/control/vpn/customers`
- `GET /api/control/vpn/subscriptions`
- `POST /api/control/vpn/subscriptions`
- `POST /api/control/vpn/subscriptions/{id}/revoke`
- `POST /api/control/vpn/subscriptions/{id}/issue-key`
- `GET /api/control/vpn/events`

Telegram webhook endpoints:

- `POST /api/vpn-telegram/webhook`

## Error Handling

- Failed VPN install/update jobs create visible maintenance events.
- Failed key creation must not activate the subscription until a key is usable.
- Expired subscriptions must revoke or disable access predictably.
- Telegram delivery failures must be logged without breaking subscription creation.
- If a VPN node is unavailable, new subscriptions should be assigned to another healthy VPN node when possible.

## Security Requirements

- VPN customer access must not expose admin APIs.
- SSH credentials and VPN secrets must never be returned in plaintext after save.
- Telegram webhook must validate the bot token or use a secret path.
- Public VPN config links must be long-lived only if they are unguessable and revocable.
- Admin actions must be audit logged.
- Firewall changes must be scoped to VPN ports and must not open the drop-catching worker runtime.

## Testing Strategy

Backend tests:

- VPN model migrations
- role changes on worker nodes
- plan/subscription CRUD
- key lifecycle
- Telegram command handling
- maintenance job creation

Frontend tests/build:

- VPN page renders
- worker role controls render
- subscription creation form validates
- Telegram settings form works

Manual smoke test:

1. Enable VPN role on one non-critical worker.
2. Run install action from panel.
3. Create a test customer.
4. Create a manual subscription.
5. Issue a key.
6. Send config through Telegram.
7. Revoke subscription and verify access is disabled.

## Out Of Scope For First Slice

- Automatic card/crypto payments.
- Mobile apps.
- Advanced reseller panel.
- Complex traffic accounting.
- Automatic VPN load balancing across every node.
- Public customer web dashboard.

These can be added after the core Telegram-first VPN flow is working.
