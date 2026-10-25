# Veltrix Bot News Broadcasts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an administrator preview and explicitly confirm plain-text news to known private VPN-bot chats, with durable progress/cancellation and no blind resend after uncertain delivery.

**Architecture:** A bot-scoped private-chat registry supplies recipients; ordinary Telegram browser identities do not. Draft confirmation freezes one SQL delivery row per eligible recipient, while a small runtime dispatcher claims, marks sending, performs one HTTP call outside locks, and finalizes by claim token. A shared rate gate covers news and existing senders only when they use the same VPN bot token.

**Tech Stack:** Existing FastAPI/admin authentication, SQLAlchemy/PostgreSQL, httpx, ControlRuntimeOrchestrator, React and fake HTTP tests. No dependency, generic outbox framework, paid broadcast, HTML, attachment or real message in this implementation/test task.

**Implementation checkpoint:** Tasks 1–5 completed with independent specification and quality approval, including actual PostgreSQL races and recovery tests. Admin API authorization, no-store errors, integer bounds and strict confirmation passed. The mounted interface covers plain-text draft/preview/edit/confirmation, audience conflicts, progress/cancellation, delayed responses and lost POST/PATCH replies; uncertain mutations remain blocked until successful reconciliation. Send switch is false by default; production registry backfill and activation remain rollout steps. The owner confirmed that historical VPN updates belong only to `@veltrix_vpn_official_bot`. No real news was sent.

---

Implements approved spec §8. Execute Tasks 1–2 (registry and queue), then Tasks 3–4 (delivery), then Task5 (admin UI). Intermediate commits are checkpoints, not independently enabled senders. No production backfill, getMe call, deployment or real send is performed while implementing or testing this plan.

## Files and scope

| Files | Responsibility |
| --- | --- |
| `backend/app/db/models.py`; new `backend/app/db/vpn_broadcast_migrations.py`; `backend/app/db/migrations.py` | Registry, campaign/delivery rows and two small rate-gate tables |
| New `backend/app/services/vpn_bot_chats.py`; `backend/app/services/vpn_telegram.py` | Record validated private inbound chats only |
| New `backend/app/cli/__init__.py`, `backend/app/cli/vpn_bot_chats.py` | Explicit provenance-checked historical backfill; no startup network |
| New `backend/app/services/vpn_broadcasts.py`, `vpn_bot_delivery.py` | Campaign workflow, typed one-shot delivery, same-bot rate gate |
| `backend/app/services/notifier.py`, `vpn_ready_notifications.py`, `vpn_telegram.py` | Rate-gate existing same-token HTTP sends while preserving existing text/keyboard/notice policy |
| New `backend/app/api/routes/vpn_broadcasts.py`, `backend/app/schemas/vpn_broadcasts.py`; `backend/app/api/__init__.py` | Admin-only same-origin campaign API |
| `backend/app/services/control_runtime.py`, `backend/app/core/config.py`, `vpn_portal_logging.py` | Bounded dispatcher, safe logs and disabled-by-default send switch |
| `frontend/src/api.ts`, `vpnAdminView.ts`, `VpnAdminNavigation.tsx`, `App.tsx`; new `VpnBroadcastPanel.tsx`, `vpnBroadcasts.ts` | Draft/preview/confirm/progress/cancel in `#vpn/broadcasts` |
| New backend `test_vpn_bot_chats.py`, `test_vpn_broadcasts.py`, `test_vpn_broadcasts_postgres.py`, `test_vpn_bot_delivery.py`, `test_vpn_broadcast_api.py`; new frontend `test/vpnBroadcasts.test.mjs`; existing Telegram/runtime/browser tests | Recipient consent, SQL races, HTTP outcomes and UI confirmation |

One VPN bot is supported. Registry scope is stable bot ID + private chat ID, not token text. Token rotation for the same bot retains recipients; changing bot ID requires an explicit registry migration and cannot silently reuse another bot's audience. The rate gate is applied to the exact configured VPN bot token; another operations bot keeps its current independent sender behavior.

## Task 1: Record genuine bot chats and safely backfill existing users

- [ ] **RED:** Add tests using existing `telegram_message` fixtures and authenticated webhook ASGI harness in `test_vpn_telegram.py`: new private inbound registers one chat; group/channel, forged webhook, parse failure, mismatched sender/chat IDs and browser login do not. Valid private invitation/trial/support messages register even if VPN access is expired or redemption fails. Duplicate update cannot clear a later blocked marker; a new valid inbound clears it. Backfill accepts only sanitized valid private records, is idempotent and retains current blocked state.
- [ ] **GREEN:** Add `VpnBotChat` with composite PK `(bot_id, chat_id)` canonical positive numeric strings, `first_seen_at`, `last_seen_at`, nullable `blocked_at`, nullable `last_update_id`; named ID-format/bounds validation in service, indexes on `(bot_id, blocked_at)`. No customer FK is required: a bot subscriber can exist without active VPN access. Do not store message text or start tokens here.

`configured_vpn_bot_id(settings)` extracts the canonical positive numeric ID before `:` from the configured VPN bot token and returns only that public ID; malformed configuration raises a fixed error without the token. Add `record_private_bot_chat(db, settings, message, now)` only after `process_telegram_update` has successfully parsed a private message and durably reserved a **new** update, before invitation/trial/generic branches commit. Existing duplicate-update replay does not clear `blocked_at`. Canonical matching private sender/chat IDs are required; never call this helper from portal OIDC/Mini App login, `resolve_telegram_customer`, invitation service redemption or an arbitrary admin customer edit. Preserve existing webhook secret validation.

Backfill uses existing `VpnTelegramUpdate.payload`, whose sanitizer preserves numeric `message.from.id`, `message.chat.id/type`, canonical update ID and redacted text. Validate `type(id) is int`, `1 <= id <= 2**52-1`, private chat, equal sender/chat, and matching canonical stored update ID; do not require successful VPN activation or successful bot response. Ignore malformed/group records. Use stored update creation time as seen time. Upsert without clearing existing block state or moving dates backwards.

**Explicit rollout precondition:** historical update rows have no bot identity. Before assigning them to a bot, the operator must establish that deployment/history used this single VPN bot, and a read-only `getMe` must confirm its ID. Implement CLI `python -m app.cli.vpn_bot_chats --confirmed-deployment-bot-id $env:VPN_BOT_CONFIRMED_ID`; the operator-provided numeric argument is an explicit provenance assertion, not inferred from customer IDs. The CLI compares that argument, configured token prefix and fakeable getMe response; mismatch aborts before writes. On success, backfill bounded500-row pages and save existing `AppSetting` marker `vpn_bot_registry_bot_id_v1` to the verified ID. No backfill/getMe runs at import, startup or ordinary page read.

The CLI's argparse help must say: `Use only after confirming that all historical VPN webhook updates belong to this bot. This command does not send messages.` When provenance is unconfirmed, keep old rows unassigned and expose `registry_state="history_unverified"` in admin config with `История чатов ещё не привязана к проверенному VPN-боту. Нужна проверка истории и восстановление реестра перед рассылкой`. This is an operational gate, not omitted implementation. A verified marker for a different current bot ID yields `bot_changed` and blocks confirmation/sending.

- [ ] Verify: from `backend`, run:

```powershell
& '..\..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest tests/test_vpn_bot_chats.py tests/test_vpn_telegram.py tests/test_vpn_portal_api.py -q
```

CLI tests use fake getMe and temporary SQLite/PostgreSQL only, never a live token. Commit `feat(vpn): track verified private bot recipients`.

## Task 2: Draft/preview/confirm/cancel with immutable recipients

- [ ] **RED:** Add campaign tests: saving/editing draft causes zero sender calls and zero delivery rows; nonempty plain text1–4096 characters accepted; empty/whitespace, overlimit, lone-surrogate and extra fields rejected. Preview counts distinct unblocked private chats for the verified bot, including expired VPN subscribers; no browser-only/group/other-bot recipients. Confirm replay creates no duplicates; editing invalidates old preview; changed eligible count requires a refreshed preview; cancel stops pending claims and preserves sent/uncertain outcomes.
- [ ] **GREEN:** Add metadata and idempotent migrations for these tables:

| Table | Fields / invariants |
| --- | --- |
| `vpn_broadcast_campaigns` | integer PK; `bot_id`; `body` Text; `state` in draft/running/completed/cancelled/failed; positive `revision` default1; nullable `preview_revision`, `preview_recipient_count`, `previewed_at`; `created_at`, `created_by_user_id`; nullable `confirmed_at`, `confirmed_by_user_id`, `cancelled_at`, `cancelled_by_user_id`; unique `(id,bot_id)` |
| `vpn_broadcast_deliveries` | integer PK; `campaign_id`, `bot_id`, `chat_id`; composite campaign/bot FK and bot/chat registry FK RESTRICT; unique `(campaign_id,chat_id)`; `state` in pending/claimed/sending/sent/failed/skipped/uncertain/cancelled; `available_at`, nullable `claim_token`, `lease_until`, `sending_at`, `finished_at`, `telegram_message_id`, `error_code`; non-negative `attempt_count` default0; due/lease indexes |
| `vpn_bot_rate_gates` | `token_fingerprint` varchar64 PK; `next_send_at`, `cooldown_until`; no raw token |
| `vpn_bot_chat_rate_gates` | composite PK `(token_fingerprint,chat_id)`; `next_send_at`; FK to global gate; no raw message |

Draft validation uses `body.encode("utf-8", "strict")`, `body.strip()` nonempty and `len(body) <=4096`; preserve actual text/line breaks shown in preview, normalize CRLF to LF before saving. No news splitting. Telegram documents the [plain message limit](https://core.telegram.org/bots/api#sendmessage); omit parse_mode, entities, attachments and paid-broadcast options entirely.

Define `create_broadcast_draft`, `update_broadcast_draft`, `preview_broadcast`, `confirm_broadcast`, `cancel_broadcast` in `vpn_broadcasts.py`. Caller owns commit/rollback. Draft update locks campaign, requires draft state, increments revision and clears preview fields. Preview saves current revision, current eligible count and cutoff timestamp; no delivery rows are created. Confirmation payload has **only** `expected_revision`, `expected_recipient_count`, `confirm: Literal[True]`. Under campaign lock recheck verified bot marker, exact revision/preview, and eligible count for chats `first_seen_at <= previewed_at`; mismatch409 and no jobs. Insert the current eligible snapshot using server registry query, mark running and audit campaign ID/count. New chats after preview join a future campaign, not this confirmed snapshot. Eligibility is checked again before each send because a recipient can block the bot after confirmation.

Use SQL `INSERT ... SELECT` plus the unique delivery constraint, not an in-memory list from the global key/customer API. The draft body is immutable after confirmation. Confirming an already running/completed/cancelled campaign returns its existing state without recreating jobs. Zero-recipient confirmation completes with zero jobs and performs no network call.

Cancellation locks campaign first, sets cancelled, updates pending/claimed-without-sending jobs to cancelled, and prevents new claims. A delivery already marked sending may complete after cancellation; report it honestly. Cancellation cannot recall messages and never resets sending/uncertain to pending. Audit create/confirm/cancel with IDs/revision/count only, never body/token. Draft edit audit also excludes body.

- [ ] Verify tests against metadata SQLite and repeated PostgreSQL upgrade; concurrent confirm/confirm and confirm/cancel use independent sessions/events in `test_vpn_broadcasts_postgres.py`. Run `test_vpn_bot_chats.py test_vpn_broadcasts.py test_vpn_broadcasts_postgres.py`; root supplies the safe `VPN_PORTAL_TEST_PG_URL` runner, no skipped concurrency cases. Commit `feat(vpn): persist explicitly confirmed news campaigns`.

## Task 3: Typed one-shot HTTP results and same-token rate gate

- [ ] **RED:** Add fake-httpx tests in `test_vpn_bot_delivery.py` for valid success, JSON error despite HTTP200,403,429 with valid/missing/invalid retry_after,400,401,5xx, malformed success, connect timeout, write/read timeout and cancellation. Assert exactly one HTTP attempt per call, no auto redirect/retry, no logged body/token or raw response. Use fake clock and independent PostgreSQL sessions to prove same-token send slots are50ms apart and same-chat slots1second apart; another ops token never waits on the VPN gate.
- [ ] **GREEN:** Add `TelegramDeliveryResult` and a single payload sender in `vpn_bot_delivery.py`:

```python
@dataclass(frozen=True, slots=True)
class TelegramDeliveryResult:
    state: Literal["sent", "retry", "blocked", "failed", "uncertain"]
    message_id: int | None = None
    retry_after: int | None = None
    error_code: str | None = None
```

Result classification is exact:

| Observation | Result / queue action |
| --- | --- |
| HTTP2xx JSON `ok is True`, positive integer message ID, matching returned chat ID | sent, persist message ID |
| Telegram error_code429 | retry; validated integer retry_after1–86400, otherwise conservative60s; extend shared global cooldown |
| Telegram error_code403 | blocked; skip recipient and set registry blocked_at |
| explicit Telegram400 rejection | failed; no automatic retry |
| explicit Telegram401 | failed with fixed bot-auth code; mark campaign failed and stop further claims |
| ConnectError/ConnectTimeout/PoolTimeout before request bytes can leave | retry, bounded30s backoff |
| read/write timeout/error, malformed2xx, remote protocol error,5xx or lost response after send | uncertain; never automatically resend |
| cancellation after sending marker or during HTTP | retain sending; expired lease becomes uncertain |

No `raise_for_status` exception carrying a token URL/response body crosses the helper. Catch and return fixed codes; scrub exception context. Use `httpx.AsyncClient(follow_redirects=False, timeout=10)` and no retry transport. News payload is exactly `{"chat_id": chat_id, "text": body, "link_preview_options": {"is_disabled": True}}`. Successful HTTP status alone is insufficient. Source constraints come from Telegram's [retry_after response parameter](https://core.telegram.org/bots/api#responseparameters) and [per-chat/global free send limits](https://core.telegram.org/bots/faq#my-bot-is-hitting-limits-how-do-i-avoid-this); product rate remains20/sec, below the published bulk ceiling.

Gate fingerprint is `sha256(configured_vpn_bot_token.encode()).hexdigest()`, used only as a database identity, never displayed/logged. In a short separate transaction lock global gate → chat gate. If `max(next_send_at, cooldown_until, chat.next_send_at) > now`, return wait duration without reserving; otherwise set global next to now+50ms and chat next to now+1second, commit and permit one request immediately. Wait outside database transactions and recheck cooldown before a call. Use database UTC time/consistent injected clock for concurrent processes. Do not reserve minutes of future slots while holding campaign work; bounded loops check cancellation/shutdown and can yield without sending. A429 updates global cooldown so ready notices/interactive responses sharing the VPN token also respect it.

Route **each actual HTTP POST** in `send_telegram_message` through this gate: both split command chunks and the extra cabinet-keyboard message consume slots. Preserve its existing public signature, keyboard content, `cabinet_allowed` behavior and sender injection in tests. Add a small adapter that turns typed non-sent results into a fixed secret-free delivery exception for the existing call sites; do not rewrite their existing retry policy in this slice.

In `TelegramNotifier.send_message`, use the gated helper only if its explicit `token` exactly matches nonempty `settings.vpn_telegram_bot_token`; otherwise retain its current separate ops-bot code path unchanged. No gate table is shared with another operations token. `deliver_next_ready_notice` already calls the VPN sender, so it inherits rate control; retain the integration slice's initial-child-only claim condition (`device.legacy_profile_id == key.id` or unlinked legacy), preventing country changes from generating extra ready messages.

- [ ] Verify `test_vpn_bot_delivery.py test_vpn_telegram.py test_vpn_ready_notifications.py`, existing notifier tests and fake-clock PostgreSQL gate tests; commit `feat(vpn): share conservative bot delivery limits and outcomes`.

## Task 4: Durable bounded dispatcher and safe recovery

- [ ] **RED:** Add tests for two simultaneous claimers, lease expiry before send, process death after sending marker, timeout after server accepted message, commit uncertainty after success,403 blocked chat later unblocked by valid inbound,429 cooldown, cancel while waiting for a rate slot, cancel during sending and runtime shutdown. All tests use fake HTTP clients/senders; fail tests if a real api.telegram.org connection is attempted.
- [ ] **GREEN:** Implement `dispatch_next_broadcast_delivery(session_factory, settings, sender, now)` in `vpn_broadcasts.py`:

1. Recover bounded expired claims in a short transaction. `claimed` with `sending_at IS NULL` can return to pending if campaign still running; stale `sending` becomes uncertain. Never reset uncertain/sent to pending.
2. Lock running campaign → due delivery with `skip_locked`; validate current bot marker and recipient still in that bot's unblocked registry. Mark blocked recipient skipped, cancelled campaign cancelled, otherwise set claimed with fresh UUID token and30-second lease; commit. Return detached minimal claim data, keeping body/token out of repr/logging.
3. Obtain a rate slot outside that transaction. Before marking sending, reopen and lock campaign → delivery; check same claim token, unexpired lease and running campaign, and recheck recipient eligibility. Set `sending_at`, increment attempt count, state sending; commit **before** HTTP. No database locks are held during HTTP.
4. Perform one typed payload send. Finalize in a new transaction, conditional on the same token/state. `sent`, `failed`, `skipped`, `uncertain` are terminal;429/definitely-prewrite retry returns to pending at available_at, extending cooldown as needed. If campaign was cancelled while the request was in flight, preserve its delivery outcome but leave campaign cancelled. If final commit outcome cannot be established, do not send again; a still-sending row will become uncertain.
5. Complete a running campaign only when no pending/claimed/sending delivery remains; expose sent/failed/skipped/uncertain/cancelled counts computed from delivery rows. Avoid cached counter races or total=sent+pending guesses. Queue rows persist across restart and no campaign can regain recipients after completion.

Use a consistent campaign → delivery → registry lock order; gate rows are reserved in independent completed transactions and never held while acquiring campaign rows. HTTP timeout10s fits the30s sending lease; shutdown cancellation is bounded and leaves durable recovery state. A cancellation racing the final sending marker can allow that one already-started delivery; UI explains remaining queued work stops, while in-flight work may finish.

Add `VPN_BROADCASTS_ENABLED` default false. Add one background task to `ControlRuntimeOrchestrator`, starting only for configured verified VPN bot and enabled switch. It drains at most20 jobs or1second per turn and yields to normal orchestration; no sleeps or HTTP inside the main runtime database transaction. Rate gate still enforces actual shared same-token pacing. Shutdown cancels/awaits the task and does not detach an HTTP operation that could later be retried as pending. A fixed log like `VPN broadcast dispatch failed campaign_id=%s delivery_id=%s` is allowed; never include body/token/recipient chat ID/exception repr.

Extend safe logging with message-body redaction for the new service logger and exception paths; do not add news body or token to `VpnNodeEvent`, `AdminAuditLog` or runtime status. Registry/backfill error codes contain no secret. Existing httpx token URL redaction remains active.

- [ ] Verify backend `test_vpn_broadcasts.py test_vpn_broadcasts_postgres.py test_vpn_bot_delivery.py test_vpn_ready_notifications.py test_vpn_telegram.py test_vpn_control_runtime.py test_vpn_portal_logging.py`; PostgreSQL cancellation/claim and finalization races must execute. Commit `feat(vpn): dispatch durable news without blind retries`.

## Task 5: Admin preview, confirmation, progress and cancellation

- [ ] **RED:** Add ASGI API tests and frontend tests: unauthorized/non-admin rejected; every mutation rejects foreign/missing Origin; draft body renders as text; editing invalidates preview; no send without explicit confirm; confirm409 refreshes audience; progress distinguishes failed/blocked/uncertain; cancel preserves already delivered counts. Check stale API response after switching campaign does not overwrite the selected one.
- [ ] **GREEN:** Add admin router under `/control/vpn/broadcasts` with these exact endpoints:

| Method/path | Request / result |
| --- | --- |
| `GET /config` | `enabled`, `registry_state` ready/history_unverified/bot_changed/unconfigured, `eligible_recipients`, explanatory text |
| `GET /` | newest campaigns, ID cursor, limit50; body omitted from list |
| `POST /` | `{body}` only;201 draft detail with revision; no delivery |
| `GET /{id}` | body/revision/state/preview metadata and authoritative delivery counts |
| `PATCH /{id}` | `{body}` only while draft; clear preview/increment revision |
| `POST /{id}/preview` | `{}`; saved preview revision/count/cutoff + exact canonical text |
| `POST /{id}/confirm` | `{expected_revision,expected_recipient_count,confirm:true}`;202 existing or newly running campaign |
| `POST /{id}/cancel` | `{}`; cancelled detail, idempotent |

All routes use `require_admin`; mutation routes use `_require_same_origin(request)` before service writes. Pydantic models use `extra="forbid"`; no caller supplies recipient IDs, bot token, parse_mode or delivery state. Detail counts are `recipient_count,pending,claimed,sending,sent,failed,skipped,uncertain,cancelled`, not a bare success percentage. Error responses are fixed codes; the admin body appears only in explicit draft/detail/preview responses.

Add `broadcasts` to `VpnAdminSection`, parser allowlist, navigation label `Рассылки`, and App section. `VpnBroadcastPanel` contains a plain textarea with visible remaining count using `Array.from(text).length`, local draft save, server preview and recipient count, then a separate explicit `Отправить новость` confirmation action showing exact preview text. Editing disables confirmation until preview runs again. Rendering uses a text node with `white-space: pre-wrap`, never `dangerouslySetInnerHTML`; no HTML preview parser, attachment or test-send-to-real-user action.

Poll selected running campaign every2seconds with request epoch/unmount cleanup; stop on terminal state and show manual refresh. Cancellation button says `Остановить оставшуюся очередь`. Explain `Неопределено: Telegram мог доставить сообщение. Автоматического повтора не будет`; expose no retry action for uncertain recipients. Progress uses actual counts and says an in-flight send may finish after cancellation. A registry operational gate is visible before draft confirmation rather than silently showing zero subscribers.

Add browser QA to the existing local admin harness (fake endpoints only): draft→preview→edit→preview→confirm, text `<b>Новости</b>` stays literal, recipient changes409, mobile390px, keyboard focus, error alerts, partial progress and cancel with a sending job. Add no frontend dependency or external tracking.

- [ ] Verify from `frontend`: `npm test`, `npm run build`, existing browser QA runtime; backend `test_vpn_broadcast_api.py test_vpn_bot_chats.py test_vpn_broadcasts.py test_vpn_broadcasts_postgres.py`. Run `git diff --check`, then commit `feat(vpn): add explicit admin news workflow`.

## Completion and rollout gates

- [ ] Root supplies isolated PostgreSQL credentials through the existing safe test runner. Exercise metadata-before-migrations startup, repeated migrations, unique recipient insertion, concurrent claim/confirm/cancel and restart recovery. No relevant PostgreSQL test may be silently skipped.
- [ ] Keep send switch false until existing recipient history is explicitly attributed to the single verified VPN bot and backfilled; getMe is read-only but still a deliberate rollout action. A changed bot ID never inherits this registry automatically.
- [ ] No test, fixture, preview or CLI path sends real news. Actual production campaign confirmation remains an explicit later admin action. Passing queue tests does not authorize a real broadcast.

Remaining technical limits: Telegram provides no send idempotency, so crashes/timeouts after the sending marker can produce uncertain delivery that must not be auto-retried. Cancellation stops queued work but cannot recall a message or reliably stop an already-started request. Deployment history provenance is required for historical chat backfill; implementation and fake tests do not establish that operational fact.
