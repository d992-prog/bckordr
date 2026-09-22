# Veltrix Friend Invitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a closed ten-person Telegram beta in which an admin creates a one-use link and the invited friend automatically receives one seven-day Veltrix VPN profile.

**Architecture:** Add one `VpnFriendInvitation` row per fixed slot and one focused service for issuing, rotating, redeeming, listing, retrying and disabling invitations. Redemption reuses the existing Telegram identity resolver, `VpnCustomer`/`VpnSubscription`/`VpnAccessKey` models and endpoint-bound durable control intent; portal access becomes an async database admission check while public access remains disabled. The feature and the sequential strict dispatcher remain off by default, and production links stay blocked until a verified ready REALITY endpoint and the separately reviewed node deployment/reconciliation gates exist.

**Tech Stack:** Python 3.11+, FastAPI, SQLAlchemy asyncio, PostgreSQL/SQLite tests, React 18, TypeScript, Vite, Node's built-in test runner.

---

## Delivery boundary

This plan implements and verifies the application feature behind disabled flags. It does not deploy the node zipapp, migrate production, open TCP 443, create the production REALITY inbound, enable the dispatcher or issue a real invitation. Those remain production mutations under `docs/superpowers/plans/2026-09-22-vpn-strict-node-deployment.md` and require fresh validation plus explicit approval.

No task may change the owner's existing UUID, 8443 link, Xray generation, payment state or `VPN_PORTAL_PUBLIC_ACCESS=false` policy. Never call `provision_vpn_access_key()` from the invitation path.

## File map

- `backend/app/db/models.py`: one invitation ORM model and constraints.
- `backend/app/db/migrations.py`: idempotent PostgreSQL startup DDL for the invitation table.
- `backend/app/core/config.py`: disabled friend-beta and strict-dispatch runtime settings.
- `backend/app/db/session.py`: lazy dedicated dispatcher session factory with bounded PostgreSQL timeouts; the ordinary application engine remains unchanged.
- `backend/app/services/app_settings.py`: private release-readiness marker lookup; no public setter.
- `backend/app/services/vpn_friend_invitations.py`: all invitation policy and state derivation.
- `backend/app/services/vpn_telegram.py`: strict deep-link parsing, sanitized update persistence and commit-before-send delivery.
- `backend/app/services/vpn_portal_auth.py`: database-backed identity admission at every session boundary.
- `backend/app/api/routes/vpn_portal.py`: await database admission during browser/OIDC callback.
- `backend/app/services/control_runtime.py`: one-at-a-time strict dispatcher caller behind a disabled flag.
- `backend/app/services/vpn_control_intents.py`: idempotent pending-operation guard and explicit safe-failure retry policy.
- `backend/app/services/vpn_lifecycle.py`: durable control intents for endpoint-bound keys; legacy behavior only for unbound keys.
- `backend/app/main.py`: own and dispose the optional dedicated dispatcher engine during application shutdown.
- `backend/app/schemas/control.py`: admin invitation response contracts.
- `backend/app/api/routes/control.py`: admin list/issue/rotate/retry/disable endpoints and bounded audit records.
- `frontend/src/api.ts`: invitation types and typed admin calls.
- `frontend/src/VpnCustomerWorkspacePanel.tsx`: ten-slot invitation panel with copy-once link handling.
- `frontend/src/App.tsx`: invitation loading and workspace props.
- `frontend/src/styles.css`: compact invitation layout using existing visual tokens.
- `backend/tests/test_vpn_friend_invitation_schema.py`: model/config/migration coverage.
- `backend/tests/test_vpn_friend_invitations.py`: service policy and lifecycle coverage.
- `backend/tests/test_vpn_friend_invitations_postgres.py`: real concurrency races.
- `backend/tests/test_vpn_friend_invitation_api.py`: admin route, no-store and redaction coverage.
- `backend/tests/test_vpn_telegram.py`: redemption, replay, sanitization and delivery-crash coverage.
- `backend/tests/test_vpn_portal_auth.py`: database admission and session revocation coverage.
- `backend/tests/test_vpn_portal_api.py`: Mini App and OIDC friend admission coverage.
- `backend/tests/test_vpn_control_runtime.py`: disabled/enabled sequential dispatcher scheduling coverage.
- `backend/tests/test_vpn_control_intents.py`: no automatic resend and explicit safe-failure retry coverage.
- `frontend/test/vpnFriendInvitationApi.test.mjs`: typed endpoint contract coverage.
- `frontend/test/vpnFriendInvitationWorkspace.test.mjs`: copy-once, states and confirmation coverage.
- `docs/current-state.md`: verified implementation state and still-blocked production gates.

### Task 1: Add the invitation schema and disabled configuration

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/migrations.py`
- Modify: `backend/app/core/config.py`
- Create: `backend/tests/test_vpn_friend_invitation_schema.py`
- Modify: `backend/tests/test_vpn_portal_schema.py`

- [x] **Step 1: Write failing ORM and settings tests**

Create tests which assert the exact table contract and disabled defaults:

```python
from sqlalchemy import CheckConstraint, UniqueConstraint

from app.core.config import Settings
from app.db.models import VpnFriendInvitation


def test_friend_invitation_schema_is_fixed_and_fail_closed() -> None:
    table = VpnFriendInvitation.__table__
    assert set(table.columns) == {
        "slot", "token_digest", "created_by_user_id", "created_at",
        "redeem_expires_at", "redeemed_at", "telegram_user_id",
        "access_key_id", "revoked_at",
    }
    assert any(
        isinstance(item, CheckConstraint) and item.name == "ck_vpn_friend_invitation_slot"
        for item in table.constraints
    )
    assert any(
        isinstance(item, CheckConstraint) and item.name == "ck_vpn_friend_invitation_redemption"
        for item in table.constraints
    )
    assert any(
        isinstance(item, UniqueConstraint)
        and tuple(column.name for column in item.columns) == ("access_key_id",)
        for item in table.constraints
    )


def test_friend_beta_and_dispatcher_default_off() -> None:
    settings = Settings(_env_file=None)
    assert settings.vpn_friend_beta_enabled is False
    assert settings.vpn_friend_beta_release_id == ""
    assert settings.vpn_telegram_bot_username == ""
    assert settings.vpn_control_dispatch_enabled is False
    assert settings.vpn_control_known_hosts_path == ""
```

- [x] **Step 2: Run the focused tests and observe the missing model/settings failure**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitation_schema.py -q`

Expected: collection fails because `VpnFriendInvitation` and the new settings do not exist.

- [x] **Step 3: Add the minimal model and settings**

Add this model after `VpnAccessKey` so the terminal foreign key is explicit:

```python
class VpnFriendInvitation(Base):
    __tablename__ = "vpn_friend_invitations"
    __table_args__ = (
        CheckConstraint("slot BETWEEN 1 AND 10", name="ck_vpn_friend_invitation_slot"),
        CheckConstraint(
            "(redeemed_at IS NULL AND telegram_user_id IS NULL AND access_key_id IS NULL) OR "
            "(redeemed_at IS NOT NULL AND telegram_user_id IS NOT NULL AND access_key_id IS NOT NULL)",
            name="ck_vpn_friend_invitation_redemption",
        ),
        UniqueConstraint("access_key_id", name="uq_vpn_friend_invitation_access_key"),
    )

    slot: Mapped[int] = mapped_column(SmallInteger, primary_key=True)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    created_by_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, server_default=func.now()
    )
    redeem_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    access_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_access_keys.id", ondelete="RESTRICT"), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

Add `SmallInteger` to the existing SQLAlchemy imports in `models.py`; do not add
a new dependency.

Add these fields to `Settings`:

```python
vpn_friend_beta_enabled: bool = Field(default=False, alias="VPN_FRIEND_BETA_ENABLED")
vpn_friend_beta_release_id: str = Field(default="", alias="VPN_FRIEND_BETA_RELEASE_ID")
vpn_telegram_bot_username: str = Field(default="", alias="VPN_TELEGRAM_BOT_USERNAME")
vpn_control_dispatch_enabled: bool = Field(default=False, alias="VPN_CONTROL_DISPATCH_ENABLED")
vpn_control_known_hosts_path: str = Field(default="", alias="VPN_CONTROL_KNOWN_HOSTS_PATH")
vpn_control_dispatch_interval_seconds: float = Field(
    default=1.0, alias="VPN_CONTROL_DISPATCH_INTERVAL_SECONDS"
)
vpn_control_finalize_timeout_seconds: float = Field(
    default=15.0, alias="VPN_CONTROL_FINALIZE_TIMEOUT_SECONDS"
)
vpn_control_db_command_timeout_seconds: float = Field(
    default=30.0, alias="VPN_CONTROL_DB_COMMAND_TIMEOUT_SECONDS"
)
vpn_control_db_statement_timeout_ms: int = Field(
    default=30_000, alias="VPN_CONTROL_DB_STATEMENT_TIMEOUT_MS"
)
```

- [x] **Step 4: Add idempotent migration DDL and migration assertions**

Append one `CREATE TABLE IF NOT EXISTS` statement after the access-key/endpoint DDL and assert its exact constraints in `test_vpn_friend_invitation_schema.py`. Following the existing endpoint-migration harness, build a real pre-invitation PostgreSQL schema from the exact preceding migration slice, insert representative customer/subscription/key rows, run only the new migration, verify old rows byte-for-byte, then run the migration again. Add a separate fresh-schema test. Do not use `Base.metadata.create_all()` to stand in for the upgrade path. The SQL must use `SMALLINT PRIMARY KEY`, `CHECK (slot BETWEEN 1 AND 10)`, the all-null/all-non-null redemption check, `UNIQUE (token_digest)`, `UNIQUE (access_key_id)`, `ON DELETE SET NULL` for the actor and `ON DELETE RESTRICT` for the key.

- [x] **Step 5: Run schema/config tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitation_schema.py tests/test_vpn_portal_schema.py -q
.venv/Scripts/python.exe -m ruff check app/db/models.py app/db/migrations.py app/core/config.py tests/test_vpn_friend_invitation_schema.py
```

Expected: all selected tests, including the mandatory isolated PostgreSQL
upgrade/repeat/fresh cases, pass and Ruff reports no errors. A skipped PostgreSQL
migration test does not complete this task.

Commit: `git commit -m "feat(vpn): add friend invitation schema"`

### Task 2: Implement issue, list and rotation policy

**Files:**
- Create: `backend/app/services/vpn_friend_invitations.py`
- Create: `backend/tests/test_vpn_friend_invitations.py`

- [x] **Step 1: Write failing token, readiness, cap and rotation tests**

The tests must prove:

```python
assert issue.link.startswith("https://t.me/veltrix_vpn_official_bot?start=i_")
assert raw_token not in invitation.token_digest
assert len(invitation.token_digest) == 64
assert len(views) == 10
assert view.slot == 1
assert view.invite_state == "unused"
assert not hasattr(view, "token")
```

Also assert invalid bot usernames, a disabled flag, disabled dispatcher, `VPN_PORTAL_PUBLIC_ACCESS=true`, absent/mismatched release-readiness marker, invalid strict transport snapshot and absence of a `ready`/verified/`reality` endpoint all raise the same bounded `FriendInvitationUnavailable("friend_beta_unavailable")`. Issue eleven invitations sequentially and assert the eleventh raises `FriendInvitationConflict("friend_invitation_cohort_full")`. Rotation must keep the slot, replace the digest, reset the seven-day invite window and reject redeemed/revoked rows. Losing a raw link is tested as rotation; no read/list call may recover it.

- [x] **Step 2: Run the focused test and observe the missing-service failure**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py -q`

Expected: collection fails because `vpn_friend_invitations` does not exist.

- [x] **Step 3: Implement the minimal service surface**

Use only stdlib `hashlib`, `re`, `secrets`, dataclasses and existing SQLAlchemy models. Define:

```python
INVITE_LIFETIME = timedelta(days=7)
TRIAL_LIFETIME = timedelta(days=7)
TOKEN_PREFIX = "veltrix-friend-invite-v1\0"
BOT_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}")


class FriendInvitationError(RuntimeError):
    pass


class FriendInvitationUnavailable(FriendInvitationError):
    pass


class FriendInvitationConflict(FriendInvitationError):
    pass


def digest_invite_token(token: str) -> str:
    return hashlib.sha256((TOKEN_PREFIX + token).encode("ascii")).hexdigest()
```

Implement `list_friend_invitations(db, now)`, `issue_friend_invitation(db, settings, actor_user_id, now)`, and `rotate_friend_invitation(db, settings, slot, actor_user_id, now)`. Generate raw tokens only with `secrets.token_urlsafe(32)`, return them only in an immutable `IssuedFriendInvitation(view, link)` value, and never attach them to ORM rows or exception messages. Require the username to match `BOT_USERNAME` and end with `bot` case-insensitively. List always returns slots 1 through 10 in order and synthesizes an `unused` view for rows not issued yet. A never-issued slot has `can_rotate=False`; an issued unredeemed slot has `can_rotate=True`.

Define a private `AppSetting` key `vpn_friend_beta_release_ready_v1`. Readiness
requires all of these at the same DB snapshot:

```python
release_id = settings.vpn_friend_beta_release_id
ready = (
    settings.vpn_friend_beta_enabled
    and settings.vpn_control_dispatch_enabled
    and not settings.vpn_portal_public_access
    and re.fullmatch(r"[0-9a-f]{64}", release_id) is not None
    and await get_app_setting(db, VPN_FRIEND_BETA_READINESS_KEY) == release_id
    and endpoint.status == "ready"
    and endpoint.security == "reality"
    and endpoint.verified_at is not None
)
```

There is no control API which writes this key. A separately approved production
runbook may write the exact reviewed release digest only after node deployment,
timeouts, reconciliation and external acceptance pass. Both issue/rotate and
redemption call this same readiness function. It also calls the existing strict
`load_transport_snapshot(worker, Path(settings.vpn_control_known_hosts_path))`
and discards the returned snapshot, thereby reusing exact owner/mode, literal
Ed25519 pin, worker-host and private-key validation instead of a weaker path check.

Select the first free slot from 1 through 10 using one savepoint per candidate so
a conflicting insert does not abort the outer transaction:

```python
for slot in range(1, 11):
    try:
        async with db.begin_nested():
            invitation = VpnFriendInvitation(
                slot=slot,
                token_digest=digest,
                created_by_user_id=actor_user_id,
                created_at=current,
                redeem_expires_at=current + INVITE_LIFETIME,
            )
            db.add(invitation)
            await db.flush()
        break
    except IntegrityError:
        continue
else:
    raise FriendInvitationConflict("friend_invitation_cohort_full")
```

Regenerate the raw token if its digest conflicts. Rotation must lock its exact
row with `FOR UPDATE` before replacing only digest and issue/expiry timestamps.

- [x] **Step 4: Derive display state through the exact ownership chain**

List rows using one joined query from invitation to access key to subscription to customer and latest control operation. Reject a broken chain by returning `failed`; never infer ownership from Telegram ID alone. Derive exactly `unused`, `preparing`, `active`, `failed`, `needs_verification`, `expired`, or `disabled`, and never include raw link/token in the view dataclass.

- [x] **Step 5: Run focused tests and commit**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py -q`

Expected: all issue/list/rotate/state tests pass.

Commit: `git commit -m "feat(vpn): add friend invitation policy"`

### Task 3: Expose safe admin list, issue and rotation endpoints

**Files:**
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Create: `backend/tests/test_vpn_friend_invitation_api.py`

- [x] **Step 1: Write failing API contract tests**

Test `GET /api/control/vpn/friend-invitations`, `POST /api/control/vpn/friend-invitations`, and `POST /api/control/vpn/friend-invitations/{slot}/rotate`. Assert admin auth, mutation serialization, status 201 for issue, status 200 for rotation, and:

```python
assert response.headers["cache-control"] == "no-store"
created = response.json()
assert set(created) == {"invitation", "invite_link"}
assert "invite_link" not in client.get(list_url, headers=admin_headers).text
assert raw_token not in audit.details
```

- [x] **Step 2: Run the route tests and observe 404 failures**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitation_api.py -q`

Expected: the new routes return 404.

- [x] **Step 3: Add exact schemas and route mappings**

Add response models with no extra fields:

```python
class VpnFriendInvitationResponse(BaseModel):
    slot: int
    invite_state: Literal[
        "unused", "preparing", "active", "failed",
        "needs_verification", "expired", "disabled",
    ]
    telegram_user_id: str | None
    telegram_username: str | None
    display_name: str | None
    subscription_expires_at: datetime | None
    provisioning_error_code: str | None
    can_rotate: bool
    can_retry: bool
    can_disable: bool


class VpnFriendInvitationIssuedResponse(BaseModel):
    invitation: VpnFriendInvitationResponse
    invite_link: str
```

Map service exceptions to bounded 409/503 details, add `Cache-Control: no-store` on create/rotate, and write audit details only as `details=f"slot={slot}"`. Never pass service exception strings containing user data to HTTP responses.

- [x] **Step 4: Run API tests and commit**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitation_api.py -q`

Expected: all route, auth, no-store, and redaction tests pass.

Commit: `git commit -m "feat(api): manage friend invitations"`

### Task 4: Redeem an invitation atomically into the durable control queue

**Files:**
- Modify: `backend/app/services/vpn_friend_invitations.py`
- Modify: `backend/app/services/vpn_lifecycle.py`
- Modify: `backend/tests/test_vpn_friend_invitations.py`
- Create: `backend/tests/test_vpn_friend_invitations_postgres.py`

- [x] **Step 1: Write failing redemption and PostgreSQL race tests**

Cover one successful redemption, same-user replay, different-user rejection, expired/revoked token, endpoint disappearing at the lock recheck, and two real PostgreSQL races:

```python
assert first.customer_id == replay.customer_id
assert first.subscription_id == replay.subscription_id
assert first.access_key_id == replay.access_key_id
assert first.external_uuid == replay.external_uuid
assert first.starts_at == replay.starts_at
assert first.expires_at == replay.expires_at
assert await scalar_count(VpnSubscription) == 1
assert await scalar_count(VpnAccessKey) == 1
assert await scalar_count(VpnControlOperation) == 1
```

Eleven concurrent issue transactions must persist exactly ten slots. Two Telegram identities racing one raw token must yield one bound row and one generic rejection.

- [x] **Step 2: Run focused tests and observe missing redemption failure**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py -k redeem -q`

Expected: failure because `redeem_friend_invitation` is absent.

- [x] **Step 3: Implement lock-first redemption**

Define `redeem_friend_invitation(db, settings, token, identity, now)` with this exact order:

1. Validate bounded base64url token shape in memory and compute its digest.
2. Lock the invitation row by digest.
3. Return its existing exact chain only when the immutable Telegram ID matches.
4. Recheck flag/runtime readiness and expiry/revoke, then select the first
   `ready`, verified, `reality` endpoint ordered by ID without taking an endpoint
   row lock. This is only a candidate: `stage_vpn_control_operation()` owns the
   canonical customer → subscriptions → keys → worker → endpoint lock order and
   must re-read and validate the persisted binding while locked.
5. Resolve/create the customer using `resolve_telegram_customer()`.
6. Create a `trial` subscription with `max_devices=1`, `starts_at=now`, `expires_at=now + TRIAL_LIFETIME`.
7. Create one endpoint-bound VLESS key with `worker_id=endpoint.worker_id`,
   `endpoint_id=endpoint.id`, a stable `external_uuid=str(uuid4())`,
   `verified_client_email=f"veltrix-beta-{invitation.slot}"`,
   `panel_sub_id=uuid4().hex`, `display_name="Veltrix VPN"`, the same expiry and
   no URI yet. The email and sub-ID satisfy the existing node-request allowlists
   and are persisted once so every retry/reconciliation uses identical identity.
8. Flush, call `stage_vpn_control_operation(db, key.id, "provision", now=now)`,
   which rechecks and locks the endpoint in canonical order, bind the invitation,
   then flush again.

Catch policy/readiness failures outside the nested transaction so the token remains unbound and its seven days do not start. Tests must assert the immutable worker/endpoint/email/sub-ID fields appear unchanged in the staged request. Add a PostgreSQL race in which the selected candidate endpoint changes before staging: the locked recheck must fail and roll back the entire redemption instead of persisting an invalid binding. Do not call legacy provisioning, perform network I/O or commit inside this service.

- [x] **Step 4: Run SQLite policy tests and opt-in PostgreSQL races**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py -q
.venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations_postgres.py -q
```

Expected: service and PostgreSQL race tests pass. Before this step, create the
separately approved isolated PostgreSQL using the repository's existing test-DB
harness and set `VPN_PORTAL_TEST_PG_URL` to that harness output. A skip does not
complete the task. Never point this variable at production.

- [x] **Step 5: Commit**

Commit: `git commit -m "feat(vpn): redeem friend invitations atomically"`

### Task 5: Integrate secure Telegram deep-link processing and recovery

**Files:**
- Modify: `backend/app/services/vpn_telegram.py`
- Modify: `backend/tests/test_vpn_telegram.py`

- [x] **Step 1: Write failing parser, redaction and crash/replay tests**

Add tests accepting only the ASCII form matched by `/start i_([A-Za-z0-9_-]{43})` in a private sender-matching chat. Assert every persisted `/start` payload stores `"text": "<redacted-start-payload>"`. Persist only the exact known commands `/start`, `/status`, `/keys`, `/support`, `статус`, `ключи`, `поддержка`, `моя подписка`, `мои профили` and `помощь`; every other text becomes the static `"<redacted-message>"`. Capture DB rows, logs, audit and errors and prove neither the raw token nor full link occurs.

Add adversarial payloads which place the bearer or complete deep link in unknown
message text, `caption`, nested reply objects, forwarded-message metadata and
future unknown fields. The allowlisted persisted projection must contain none of
them. Also assert malformed, expired, revoked and already-bound-to-another-user
tokens all produce the exact same bot text and the same successful webhook HTTP
behavior, with no state changes:

```python
INVITATION_REJECTED_TEXT = (
    "Приглашение недействительно. Попросите владельца создать новую ссылку."
)
```

Add a sender which fails after activation commit. On the duplicate update ID, assert no business row changes, the already committed status is sent, and `processed_at` becomes non-null. A new update ID with the same token must return the same IDs and dates.

- [x] **Step 2: Run the Telegram tests and observe the secret-persistence/replay failures**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_telegram.py -k "invite or redaction or duplicate" -q`

Expected: raw `/start` text is currently persisted and the duplicate is currently returned without delivery.

- [x] **Step 3: Add strict parsing and sanitized persistence before any insert**

Add pure helpers:

```python
INVITE_START = re.compile(r"/start i_([A-Za-z0-9_-]{43})", re.ASCII)


def friend_invite_token(text: str) -> str | None:
    match = INVITE_START.fullmatch(text)
    return match.group(1) if match else None


def sanitized_telegram_payload(payload: dict, text: str) -> dict:
    message = payload.get("message")
    if not isinstance(message, dict):
        return {"update_id": payload.get("update_id")}
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    chat = message.get("chat") if isinstance(message.get("chat"), dict) else {}
    return {
        "update_id": payload.get("update_id"),
        "message": {
            "message_id": message.get("message_id"),
            "date": message.get("date"),
            "from": {"id": sender.get("id")},
            "chat": {"id": chat.get("id"), "type": chat.get("type")},
            "text": sanitized_telegram_text(text),
        },
    }
```

`sanitized_telegram_text()` returns `"<redacted-start-payload>"` for every
`/start` carrying any payload, preserves only the exact allowlisted commands
named in Step 1, and returns `"<redacted-message>"` for everything else. It must
never preserve an arbitrary prefix or slice. This is an allowlist: `caption`,
reply/forward objects, media and unknown future Telegram fields are never
persisted. The sanitized object, never `payload`, is passed to
`VpnTelegramUpdate`.

- [x] **Step 4: Commit activation before outbound delivery and recover duplicates**

For an invitation update, insert/claim the update, redeem and bind its `customer_id` in one transaction, then `commit()` before invoking the sender. On an existing unprocessed update, load the exact customer/invitation chain, render `Access is being prepared` unless the profile is active, send that derived status without business mutation, mark `processed_at`, and let the route commit. Preserve current generic command behavior and private-chat protection.

Map every invitation rejection—including token parse failure, expired, revoked
and wrong Telegram owner—to `INVITATION_REJECTED_TEXT`; do not expose the reason
through text, HTTP status/body, logs or audit details. Tests assert all four paths
have identical outward behavior and leave invitation, customer, subscription,
key and control-operation counts unchanged.

- [x] **Step 5: Run Telegram tests and commit**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_telegram.py -q`

Expected: all Telegram tests pass, including existing command behavior.

Commit: `git commit -m "feat(bot): activate friend invitations safely"`

### Task 6: Make portal admission database-backed and fail closed

**Files:**
- Modify: `backend/app/services/vpn_portal_auth.py`
- Modify: `backend/app/api/routes/vpn_portal.py`
- Modify: `backend/app/services/vpn_telegram.py`
- Modify: `backend/tests/test_vpn_portal_auth.py`
- Modify: `backend/tests/test_vpn_portal_api.py`
- Modify: `backend/tests/test_vpn_telegram.py`

- [x] **Step 1: Write failing admission tests at every boundary**

Create an invited chain and prove the friend can exchange Mini App initData and OIDC code, look up a cookie, survive the locked CSRF/session recheck and receive the cabinet button. Break each join, revoke the invitation, archive the customer and expire the subscription; every case must fail closed. Keep the configured owner allowlist working and public access false.

- [x] **Step 2: Run the focused tests and observe allowlist-only failures**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_portal_auth.py tests/test_vpn_portal_api.py tests/test_vpn_telegram.py -k "admission or invited or cabinet" -q
```

Expected: invited users are rejected because `identity_allowed()` is settings-only.

- [x] **Step 3: Add one async admission helper and use it everywhere**

Keep `identity_allowed()` for static owners. Add:

```python
async def identity_admitted(
    db: AsyncSession,
    settings: Settings,
    user_id: object,
    *,
    now: datetime | None = None,
) -> bool:
    if not settings.vpn_portal_enabled:
        return False
    try:
        normalized = telegram_user_id(user_id)
    except ValueError:
        return False
    if identity_allowed(settings, normalized):
        return True
    current = as_utc(now or utcnow())
    return bool(await db.scalar(friend_admission_query(normalized, current)))
```

`friend_admission_query()` must join invitation → key → subscription → customer, require matching immutable IDs, non-revoked invitation, active customer, `status IN ('active','trial')`, and `starts_at <= now < expires_at`. Replace settings-only checks in `lookup_session`, `_lock_current_principal`, `exchange_mini_app_session` and OIDC callback with awaited admission.

Change the production sender contract explicitly to
`send_telegram_message(settings, chat_id, text, *, cabinet_allowed=False)` and
make `telegram_cabinet_keyboard()` depend on that boolean instead of calling the
static allowlist itself. `process_telegram_update` awaits `identity_admitted()`
and passes the result for the real sender; injected three-argument test senders
continue through a tiny adapter which sends text only. Thus DB eligibility
actually controls the button while a URL alone remains non-credential.

- [x] **Step 4: Run all portal and Telegram auth tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_portal_auth.py tests/test_vpn_portal_api.py tests/test_vpn_telegram.py -q
```

Expected: all existing owner paths and new invited-user paths pass.

Commit: `git commit -m "feat(portal): admit invited VPN customers"`

### Task 7: Add reversible expiry, safe retry and permanent disable

**Files:**
- Modify: `backend/app/services/vpn_friend_invitations.py`
- Modify: `backend/app/services/vpn_control_intents.py`
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Modify: `backend/tests/test_vpn_friend_invitations.py`
- Modify: `backend/tests/test_vpn_friend_invitation_api.py`
- Modify: `backend/tests/test_vpn_control_intents.py`
- Modify: `backend/tests/test_vpn_control_intents_postgres.py`
- Modify: `backend/tests/test_vpn_lifecycle.py`

- [x] **Step 1: Write failing retry/expiry/disable tests**

Prove:

- only `failed` operations with `vpn_node_preflight_failed` or `vpn_node_interrupted_before_mutation` can retry;
- `uncertain` returns `friend_invitation_reconciliation_required` and never stages another generation;
- lifecycle encounters of an existing operation for the same requested action
  never create a new generation: `queued`/`claimed` are reused, while
  `uncertain`/`failed` are not retried automatically;
- expiry closes portal admission and existing lifecycle stages reversible `suspend`;
- extending the same subscription stages `provision` and preserves UUID/URI;
- explicit disable sets `revoked_at`, revokes current sessions and stages sticky `revoke` for only the invited key;
- later extension cannot restore an explicitly disabled participant.
- endpoint-bound invited keys never call `provision_vpn_access_key`,
  `suspend_vpn_access_key` or `revoke_vpn_access_key`; unbound legacy keys keep
  their current behavior.
- concurrent disable and lifecycle/staging transactions on real PostgreSQL
  complete without a deadlock and leave exactly one authoritative operation at
  the current generation.

- [x] **Step 2: Run focused tests and observe missing actions**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py tests/test_vpn_control_intents.py tests/test_vpn_lifecycle.py -k "retry or expire or disable or pending_guard" -q`

Expected: retry/disable service functions and routes are absent.

- [x] **Step 3: Add one guarded, explicitly retryable staging primitive**

Extend `stage_vpn_control_operation(..., retry_failed=False)` without changing
the canonical customer → subscriptions → keys → worker → endpoint → operations
lock order. After those rows are locked, inspect the authoritative latest
operation for the key:

- if its action equals the requested action, `queued`/`claimed` return the same
  row, `uncertain` raises `vpn_control_reconciliation_required`, and `failed`
  raises `vpn_control_retry_required` unless `retry_failed=True` with exact error
  code `vpn_node_preflight_failed` or
  `vpn_node_interrupted_before_mutation`;
- if the requested action differs because authoritative policy changed, stage
  exactly one next-generation transition rather than treating it as a retry.
  Supersede an older `queued` row, leave `claimed`/`uncertain` untouched, and
  allow worker reservation to keep the newer row unclaimable behind a protected
  operation until it is reconciled. Repeated lifecycle passes see that newest
  same-action row and do not create more generations;
- safe terminal success/superseded with a newly required policy: create exactly
  one next generation as today.

Only the authenticated admin retry path may pass `retry_failed=True`. Lifecycle,
redemption and ordinary staging use the default. Add unit tests proving repeated
lifecycle calls for the same action after `queued`, `claimed`, `uncertain` and
`failed` never create a new generation; explicit retry of either allowlisted
pre-mutation error creates exactly one generation; arbitrary failure codes and
uncertain operations cannot retry. Also prove a genuine `provision → suspend`,
`suspend → provision` or `* → revoke` policy transition creates one newer row,
then remains idempotent on the next lifecycle pass.

- [x] **Step 4: Implement retry/disable without lock inversion**

Add `retry_friend_invitation(db, slot, now)` and
`disable_friend_invitation(db, slot, now)`. Discover the bound access-key ID with
one non-locking exact invitation → key → subscription → customer join. Call the
guarded `stage_vpn_control_operation()` first so it exclusively owns the
canonical lock order; retry passes `retry_failed=True`, while disable requests
`revoke` and relies on staging to make revoke sticky. Then lock the invitation
row, re-read and validate every immutable binding ID against the discovered
chain, and only then update invitation/session state. Disable sets `revoked_at`
and revokes every non-revoked `VpnCustomerSession` for that customer at the same
timestamp. If the non-locking discovery already sees `revoked_at`, lock and
revalidate only that invitation chain and return without staging. If another
transaction wins the disable race after discovery, roll back this call's nested
savepoint before returning the already-disabled view, so repeated disable cannot
create another revoke generation. Never pre-lock invitation, key, subscription
or endpoint before staging.

Run each action in one nested transaction. Any staging, reconciliation or binding
failure rolls back the staged operation, sticky key mutation, invitation and
session changes together. Add a mandatory real-PostgreSQL race between disable
and lifecycle/staging; assert no deadlock, one authoritative operation/current
generation, immutable binding and no partial invitation/session mutation.
Explicitly test disable while the previous provision is `failed` and while it is
`uncertain`: both must commit local `revoked_at`, revoke portal sessions, persist
sticky revoke, and create exactly one revoke transition. In the uncertain case
the new revoke stays queued and cannot be claimed until reconciliation resolves
the older protected operation; this is a policy transition, never a resend.

In `vpn_lifecycle.py`, branch on `access_key.endpoint_id is not None` before
calling `lock_vpn_subscription()` or any legacy provisioning function. For an
endpoint-bound candidate, discover its key ID without locks and call guarded
staging directly; staging re-reads and locks all policy rows canonically. Count a
returned queued/claimed operation as already pending, and count
retry/reconciliation-required results without resending. Only the unbound branch
uses `lock_vpn_subscription()` and the existing legacy helpers. Tests patch all
three legacy helpers to raise if an invited endpoint-bound key reaches them.

- [x] **Step 5: Expose bounded admin actions**

Add:

```text
POST /api/control/vpn/friend-invitations/{slot}/retry
POST /api/control/vpn/friend-invitations/{slot}/disable
```

Both use the existing admin dependency, mutation serialization and slot-only audit details. Disable returns the updated invitation view. Retry returns 409 for uncertain state and never leaks remote error text.

- [x] **Step 6: Run lifecycle/API tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitations.py tests/test_vpn_friend_invitation_api.py tests/test_vpn_control_intents.py tests/test_vpn_control_intents_postgres.py tests/test_vpn_lifecycle.py -q
```

Expected: all selected tests pass.

Commit: `git commit -m "feat(vpn): manage friend invitation lifecycle"`

### Task 8: Wire the strict dispatcher sequentially behind a disabled flag

**Files:**
- Modify: `backend/app/db/session.py`
- Modify: `backend/app/services/app_settings.py`
- Modify: `backend/app/services/control_runtime.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_vpn_control_runtime.py`

- [x] **Step 1: Write failing timeout, readiness and runtime scheduling tests**

Capture dedicated-dispatcher `create_async_engine()` arguments and assert
PostgreSQL receives a finite positive asyncpg `command_timeout` plus a
millisecond `statement_timeout`, while SQLite receives no asyncpg-only options.
The existing module-level `engine` and `AsyncSessionLocal` construction must
remain byte-for-byte equivalent and receive no new timeout/connect arguments.
Against isolated PostgreSQL, cancel a blocked dispatcher query and prove the
transaction rolls back, the connection returns cleanly to the pool, and no late
commit appears.

Inject a dispatcher callable and assert zero calls when any one of these is
false: friend-beta flag, dispatcher flag, `VPN_PORTAL_PUBLIC_ACCESS=false`, valid
64-hex release ID, matching private DB readiness marker, configured known-hosts
path, valid strict transport snapshot, or strictly positive DB
command/statement/finalize timeouts. Prove changing env flags alone cannot
dispatch and that every disabled/missing-marker path creates zero dedicated
engines. When every gate matches, assert exactly one dedicated engine is created
lazily, at most one dispatcher task exists, the fixed known-hosts path and
finalize timeout are passed, cancellation is awaited before its engine is
disposed on shutdown, and exceptions produce a static log message without
request/credential data.

- [x] **Step 2: Run the tests and observe zero dispatcher calls**

Run: `cd backend && .venv/Scripts/python.exe -m pytest tests/test_vpn_control_runtime.py -k vpn_control_dispatch -q`

Expected: the enabled case fails because the runtime does not call the dispatcher.

- [x] **Step 3: Add a lazy dedicated dispatcher session factory**

Leave the ordinary application engine and `AsyncSessionLocal` exactly as they
are. Add a separate factory used only by the strict dispatcher:

```python
@dataclass(frozen=True)
class VpnControlDatabase:
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]


def vpn_control_engine_options(settings: Settings) -> dict[str, object]:
    options: dict[str, object] = {
        "hide_parameters": True,
        "pool_pre_ping": True,
        "future": True,
    }
    if settings.db_url.startswith(("postgresql+asyncpg://", "postgresql://")):
        options["connect_args"] = {
            "command_timeout": settings.vpn_control_db_command_timeout_seconds,
            "server_settings": {
                "statement_timeout": str(settings.vpn_control_db_statement_timeout_ms)
            },
        }
    return options


def create_vpn_control_database(settings: Settings) -> VpnControlDatabase:
    if (
        settings.vpn_control_db_command_timeout_seconds <= 0
        or settings.vpn_control_db_statement_timeout_ms <= 0
    ):
        raise ValueError("vpn_control_database_timeout_invalid")
    dedicated_engine = create_async_engine(
        settings.db_url,
        **vpn_control_engine_options(settings),
    )
    return VpnControlDatabase(
        engine=dedicated_engine,
        session_factory=async_sessionmaker(
            dedicated_engine, expire_on_commit=False, class_=AsyncSession
        ),
    )
```

Do not call `create_vpn_control_database()` at import time, application startup,
or merely because the env flag is true. The cancellation integration test is
mandatory before the DB readiness marker can be set in any environment.

- [x] **Step 4: Add one fully gated task, not a new daemon**

Reuse `dispatch_next_vpn_control_operation()` inside
`ControlRuntimeOrchestrator`. Before any dedicated-engine construction, validate
both feature flags, disabled public portal access, exact 64-lowercase-hex release
ID and configured positive timeouts.
Then, in one ordinary application-session snapshot, require the exact matching
`AppSetting('vpn_friend_beta_release_ready_v1')`, select the first `ready`,
verified, `reality` `VpnEndpoint` ordered by ID joined to its non-archived
`WorkerNode`, reject a missing/mismatched pair, and pass that exact loaded worker
to `load_transport_snapshot(worker, known_hosts_path)`. Only after every gate passes may the
orchestrator invoke an injected/default `create_vpn_control_database()` callback
once, cache its dedicated session factory, and start one
`_vpn_control_dispatch_task` if no prior task is running.

Execute one queue item per interval. The dispatcher itself owns claim/commit,
network-without-session and finalize-new-session separation. Do not add a setter
endpoint, worker pools, leases or new dependencies. `ControlRuntimeOrchestrator`
owns the optional dedicated database handle. Its `shutdown()` must cancel and
await the dispatcher task, close any checked-out session, and only then dispose
the dedicated engine; `main.py` must continue awaiting `monitoring.shutdown()`
before disposing the ordinary engine. Feature-off and missing/mismatched-marker
startup therefore leave global database behavior completely unchanged.

- [x] **Step 5: Run runtime plus dispatcher tests and commit**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_control_runtime.py tests/test_vpn_control_dispatcher.py tests/test_vpn_control_dispatcher_postgres.py -q
```

Expected: unit tests and mandatory isolated PostgreSQL timeout/cancellation tests
pass. A skipped PostgreSQL result does not satisfy this task's completion gate.

Commit: `git commit -m "feat(vpn): schedule strict control dispatch"`

### Task 9: Add the ten-slot admin interface with copy-once links

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/VpnCustomerWorkspacePanel.tsx`
- Modify: `frontend/src/styles.css`
- Create: `frontend/test/vpnFriendInvitationApi.test.mjs`
- Create: `frontend/test/vpnFriendInvitationWorkspace.test.mjs`

- [x] **Step 1: Write failing API and workspace tests**

Using the existing source-inspection and import helpers, assert typed methods call the exact five endpoints, the list type cannot contain `invite_link`, and only create/rotate responses expose it. UI tests must assert ten durable slots, Russian user-facing labels, copy action only from transient component state, rotation only when `can_rotate`, retry only when `can_retry`, and `window.confirm` before disable.

- [x] **Step 2: Run frontend tests and observe missing types/actions**

Run: `cd frontend && npm test`

Expected: the two new test files fail because invitation APIs/UI do not exist.

- [x] **Step 3: Add typed API contracts and load invitations**

Define:

```typescript
export type VpnFriendInvitation = {
  slot: number;
  invite_state: "unused" | "preparing" | "active" | "failed" |
    "needs_verification" | "expired" | "disabled";
  telegram_user_id: string | null;
  telegram_username: string | null;
  display_name: string | null;
  subscription_expires_at: string | null;
  provisioning_error_code: string | null;
  can_rotate: boolean;
  can_retry: boolean;
  can_disable: boolean;
};

export type VpnFriendInvitationIssued = {
  invitation: VpnFriendInvitation;
  invite_link: string;
};
```

Add list/issue/rotate/retry/disable methods. Load only list views in `App.loadAll()` and pass them to the workspace; never save `invite_link` in app-wide state, storage, URL or logs.

- [x] **Step 4: Implement the compact panel**

At the top of the customer workspace detail column, render `Тестовые приглашения · ${usedCount}/10`, ten rows and state badges. Hold the one-time link in local component state keyed by slot, copy with `navigator.clipboard.writeText()`, and delete it from state after rotation or navigation away. Use existing button classes and confirmation pattern. Display `Требует проверки` for uncertain state with no automatic retry.

- [x] **Step 5: Run tests/build and commit**

Run:

```powershell
cd frontend
npm test
npm run build
```

Expected: all Node tests pass and Vite build exits 0.

Commit: `git commit -m "feat(ui): manage friend beta invitations"`

### Task 10: Verify the closed-beta implementation and document rollout truthfully

**Files:**
- Modify: `docs/current-state.md`
- Modify: `docs/superpowers/plans/2026-09-22-veltrix-friend-invitations.md`

- [x] **Step 1: Run the focused backend acceptance set**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest tests/test_vpn_friend_invitation_schema.py tests/test_vpn_friend_invitations.py tests/test_vpn_friend_invitations_postgres.py tests/test_vpn_friend_invitation_api.py tests/test_vpn_telegram.py tests/test_vpn_portal_auth.py tests/test_vpn_portal_api.py tests/test_vpn_lifecycle.py tests/test_vpn_control_intents.py tests/test_vpn_control_intents_postgres.py tests/test_vpn_control_runtime.py tests/test_vpn_control_dispatcher.py tests/test_vpn_control_dispatcher_postgres.py -q
```

Expected: all tests pass against the isolated PostgreSQL database with zero skips
for invitation migration/race, control-intent no-resend/deadlock,
timeout/cancellation and dispatcher cases. Any skip in those required groups
leaves verification incomplete.

- [x] **Step 2: Run whole-project verification**

Run:

```powershell
cd backend
.venv/Scripts/python.exe -m pytest -q
.venv/Scripts/python.exe -m ruff check app tests
cd ../frontend
npm test
npm run build
cd ..
git diff --check
git status --short
```

Expected: zero test/lint/build failures; status contains only intended commits plus the pre-existing user-owned `frontend/tsconfig.tsbuildinfo` modification.

- [x] **Step 3: Independently review specification compliance and code quality**

Dispatch a fresh specification reviewer against `docs/superpowers/specs/2026-09-22-veltrix-friend-invitations-design.md`, then a separate code-quality/security reviewer against the complete implementation range. Fix every Critical or Important finding, rerun affected tests, and request re-review until both approve.

- [x] **Step 4: Update current state without claiming production readiness**

Record exact test counts/commands, commit range and disabled settings. State explicitly that no production invitation exists and that node deployment, migration rehearsal, timeout/reconciliation gates, protected REALITY endpoint, TCP 443 acceptance and controlled first-friend connection/revoke remain pending approved production work.

- [x] **Step 5: Commit documentation**

Commit: `git commit -m "docs(vpn): record friend beta verification"`
