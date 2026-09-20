# VPN Service Operations

## Current Scope

The service supports manually created plans and subscriptions, automatic lifecycle maintenance, 3x-UI access keys, and Telegram delivery. Payments are intentionally disabled until the rest of the product is operational and verified.

The Telegram bot supports `/start`, `/status`, `/keys`, and `/support`. A VPN URI is a credential: only use the bot in a private chat, protect the webhook with both configured secrets, and never paste a URI into logs or public channels.

## Safe Node Rollout

1. Enable the VPN role on one non-critical worker.
2. Configure its SSH access, public host, 3x-UI panel, and inbound.
3. Run VPN check, install or autoconfigure, and create the inbound when required.
4. Confirm the node says `готова к выдаче` in the VPN screen. Read every blocking reason if it does not.
5. Create one test customer, subscription, and key.
6. Test the VLESS/VMess URI on a client device.
7. Revoke the key and confirm it remains in history as `revoked`.

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

- `pending_sync`: restore or configure a safe ready node, then run lifecycle or press `Повторить выдачу`.
- `pending_revoke`: restore the assigned node, then run lifecycle or press `Повторить отзыв`.
- Active domain attack: wait for the run to finish; existing VPN clients continue working.
- Telegram error: inspect the Telegram table and control logs, correct the token or network issue, then send a new command. The bot token is redacted from persisted errors.
- Duplicate Telegram update: no action is required; the update ID is stored once and the response is not resent.

Revocation never deletes an access-key row. A successful remote revoke produces `revoked`; an unsafe or unavailable assigned node produces `pending_revoke` for a later retry.

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
11. Expected: the subscription becomes `expired`; its key becomes `revoked`, or `pending_revoke` if the assigned node cannot be changed safely.
12. If it is `pending_revoke`, restore node safety and press `Повторить отзыв`. Expected: the key becomes `revoked` and remains visible in history.
13. Send `/keys` again. Expected: the revoked URI is no longer returned.
