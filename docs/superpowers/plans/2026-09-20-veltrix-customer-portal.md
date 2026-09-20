# Veltrix Customer Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver a real customer cabinet in browsers and Telegram, with isolated Telegram authentication, own subscriptions/profiles, safe profile naming and working access-link copying, without payments or changes to VPN transport.

**Architecture:** Add a separate `/cabinet/` frontend entry and `/api/vpn-portal` backend surface over the existing VPN records. Browser OIDC and signed Mini App data converge on the same Telegram identity service and separate customer sessions. Keep shared display/policy helpers independent from auth and HTTP; the existing admin and bot reuse them without granting customer access to admin routes.

**Tech Stack:** Existing FastAPI, SQLAlchemy async/PostgreSQL, React 18, TypeScript and Vite. Add PyJWT with its cryptography extra for RS256 verification. Use existing pytest/httpx/aiosqlite and Node test tooling; browser verification supplements, not replaces, API tests.

---

## Scope, workspace and evidence

- Approved design: `docs/superpowers/specs/2026-09-20-veltrix-customer-portal-design.md`.
- Main checkout: `D:\паразитное seo\backorder\project`.
- Implementation checkout: `D:\паразитное seo\backorder\project\.worktrees\veltrix-customer-portal`.
- Branch: `codex/veltrix-customer-portal`; starting revision `f111271`.
- `.worktrees/` is ignored. Do not touch the unrelated CSV files or `.tmp-yadrenovpn/`.
- Fresh baseline: backend **332 passed** in 58.48 seconds; frontend **12 passed**;
  `npm run build` passed. These are baseline results, not verification of this feature.
- The old main `backend/.venv` has no pytest. Use the already available `python`
  executable (`C:\Users\user\.codex\python\Python314\python.exe`) with the worktree
  backend on `PYTHONPATH`, or a new correctly installed worktree environment.
- Execution environment now prepared at worktree `backend/.venv` using Python
  3.14.4 with system test packages; PyJWT 2.14.0 installed only in this venv.
  Prefer `.\.venv\Scripts\python.exe` from worktree/backend for subsequent checks.
- Production was inspected read-only: Python `3.11.0rc1`, PostgreSQL `14.24`.
  Keep implementation compatible with Python 3.11; do not upgrade the server runtime
  implicitly during this feature. Rehearse it separately before public launch.
- Frontend dependencies installed with `npm ci --offline --ignore-scripts`; no new
  packages are needed for the portal UI.
- All relative paths below are relative to the implementation checkout. Run backend
  commands in its `backend` directory and frontend commands in `frontend`.
- This is a plan, not completed production code. Checkboxes change only after the
  stated evidence exists. Commit each task's named files, never `git add .`.

Statistics ingestion/history, commercial plan publication and public-launch transport
hardening remain separate projects listed in the approved design. This plan delivers
the cabinet now, with honest unavailable/unpublished states for those capabilities.

### Commands used throughout

```powershell
# Backend, from worktree/backend:
$env:PYTHONPATH = (Get-Location).Path
python -m pytest -q
python -m ruff check app tests

# Frontend, from worktree/frontend:
npm test
npm run build
```

Run focused tests red before implementation, green afterward, then related existing
tests. Do not treat import/setup errors as a meaningful red test for business logic.

## File and responsibility map

| Unit | Files | Responsibility |
|---|---|---|
| Additive schema | `backend/app/db/models.py`, `backend/app/db/migrations.py` | Display names; portal sessions; single-use OIDC attempts and Mini App exchanges |
| Display | `backend/app/services/vpn_display.py` | Name validation and credential-preserving URI labels |
| Name assignment | `backend/app/services/vpn_profile_names.py` | Stable customer-local numbering and idempotent backfill |
| Identity | `backend/app/services/vpn_telegram_identity.py` | Normalize verified Telegram identities and resolve one customer |
| Telegram verification | `backend/app/services/vpn_portal_telegram.py` | Pure Mini App verification and bounded official OIDC/JWKS client |
| Sessions | `backend/app/services/vpn_portal_auth.py` | Session issuance, lookup, revocation, browser binding, replay claims, cleanup |
| Customer data | `backend/app/services/vpn_customer_view.py` | Ownership-filtered queries, entitlement and safe client DTOs |
| HTTP | `backend/app/api/routes/vpn_portal.py`, `backend/app/schemas/vpn_portal.py`, `backend/app/services/vpn_portal_http.py` | Public configuration, auth, authenticated customer endpoints and scoped middleware |
| Runtime integration | `backend/app/api/__init__.py`, `backend/app/main.py`, `backend/app/core/config.py`, `backend/app/services/control_runtime.py` | Router, secure defaults, scheduled cleanup, no-store and log hygiene |
| Admin integration | `backend/app/api/routes/control.py`, `backend/app/schemas/control.py`, `frontend/src/api.ts`, `frontend/src/VpnCustomerWorkspacePanel.tsx` | Display-name edit and labelled copy URI, unchanged node identity |
| Bot integration | `backend/app/services/vpn_telegram.py` | Shared identity and status, private-chat cabinet button, labelled links |
| Customer frontend | `frontend/cabinet/index.html`, `frontend/src/vpn-portal/{main.tsx,Portal.tsx,api.ts,types.ts,view.ts,portal.css}` | Independent responsive cabinet; no admin imports |
| Deployment | `deploy/nginx-vpn-portal-http.conf`, `deploy/nginx-vpn-portal-locations.conf`, `deploy/domain-drop-control.service`, `docs/vpn-customer-portal-runbook.md` | Auth throttling, secret-safe logs and controlled enablement |

## Fixed contracts

Use these names consistently across tasks. All response bodies are explicit DTOs,
never arbitrary ORM serialization.

| HTTP endpoint | Auth | Result |
|---|---|---|
| `GET /api/vpn-portal/config` | None | enabled capabilities, same-origin login path, safe support text; no secrets |
| `GET /api/vpn-portal/auth/telegram/start` | None | Bound single-use attempt, redirect to Telegram |
| `GET /api/vpn-portal/auth/telegram/callback` | State + browser binding | Code exchange, session cookie, redirect to `/cabinet/` |
| `POST /api/vpn-portal/auth/mini-app` | Exact Origin + signed initData | Session cookie, customer session DTO |
| `GET /api/vpn-portal/me` | Customer cookie | Name and CSRF token, no admin or node fields |
| `POST /api/vpn-portal/logout` | Customer cookie + CSRF + Origin | Revoke own session, clear cookie |
| `GET /api/vpn-portal/subscriptions` | Customer cookie | Own subscriptions, effective state, used profile slots |
| `GET /api/vpn-portal/profiles` | Customer cookie | Own profile metadata, no URI/UUID |
| `GET /api/vpn-portal/profiles/{id}/connection` | Customer cookie + current entitlement | One labelled URI; 404 for foreign/missing ID |
| `PATCH /api/vpn-portal/profiles/{id}` | Customer cookie + CSRF + Origin | Change only display_name; never queue node work |
| `PATCH /api/control/vpn/access-keys/{id}/display-name` | Existing admin | Same display-name validation, audited |

Session cookies: `veltrix_customer_session` and short-lived
`veltrix_login_binding`, host-only, HttpOnly, SameSite=Lax, Path `/`; Secure unless
explicit localhost development. Cookie names intentionally differ from `frdm_session`.
Session lifetime 7 days; login attempt 10 minutes; Mini App acceptance 5 minutes
plus at most 30 seconds future clock skew. Use aware UTC and normalize SQLite dates
in tests. Application code never stores sessions, initData or connection URIs in
localStorage/sessionStorage. Inspect the official SDK's launch-data cache during
browser verification; clear its authentication payload after the one-time exchange
without deleting unrelated storage, and recheck Mini App reopening behavior.

Settings to add, with fail-closed defaults:

```python
vpn_portal_enabled: bool = Field(default=False, alias="VPN_PORTAL_ENABLED")
vpn_portal_public_origin: str = Field(default="", alias="VPN_PORTAL_PUBLIC_ORIGIN")
vpn_portal_allow_local_http: bool = Field(default=False, alias="VPN_PORTAL_ALLOW_LOCAL_HTTP")
vpn_portal_public_access: bool = Field(default=False, alias="VPN_PORTAL_PUBLIC_ACCESS")
vpn_portal_allowed_telegram_ids: str = Field(default="", alias="VPN_PORTAL_ALLOWED_TELEGRAM_IDS")
vpn_portal_oidc_client_id: str = Field(default="", alias="VPN_PORTAL_OIDC_CLIENT_ID")
vpn_portal_oidc_client_secret: str = Field(default="", alias="VPN_PORTAL_OIDC_CLIENT_SECRET")
```

When enabled, an empty pilot allowlist denies portal sign-in unless public_access is
explicitly true. Closed pilot is the deployment default. This does not revoke existing
VPN subscriptions or alter the existing bot's ability to receive `/start`.

## Task 1: Display transformations as pure, tested functions

**Files:** Create `backend/app/services/vpn_display.py`,
`backend/tests/test_vpn_display.py`.

- [x] Write the following tests before the helper exists:

```python
import base64
import json
from urllib.parse import unquote

import pytest

from app.services.vpn_display import InvalidVpnDisplay, connection_label, display_uri, validate_display_name


def test_vless_changes_only_fragment():
    original = "vless://11111111-1111-4111-8111-111111111111@vpn.example:8443?type=tcp&security=none#dropcatch-8-test1"
    result = display_uri(original, "iPhone & работа")
    assert result.partition("#")[0] == original.partition("#")[0]
    assert unquote(result.partition("#")[2]) == "Veltrix VPN · iPhone & работа"


def test_vmess_changes_only_ps():
    data = {"v": "2", "ps": "old", "add": "vpn.example", "port": "443",
            "id": "11111111-1111-4111-8111-111111111111", "net": "tcp", "tls": "tls"}
    original = "vmess://" + base64.b64encode(json.dumps(data).encode()).decode()
    result = display_uri(original, "Ноутбук")
    decoded = json.loads(base64.b64decode(result.removeprefix("vmess://")))
    assert decoded == dict(data, ps="Veltrix VPN · Ноутбук")


@pytest.mark.parametrize("name", ["", "  ", "x" * 65, "a\nb", "a\u202eb"])
def test_rejects_invalid_name(name):
    with pytest.raises(InvalidVpnDisplay):
        validate_display_name(name)


@pytest.mark.parametrize("uri", ["https://example.com", "vmess://bad", "vless://no-host", ""])
def test_rejects_bad_configuration_without_echoing_it(uri):
    with pytest.raises(InvalidVpnDisplay, match="invalid_configuration"):
        display_uri(uri, "Телефон")


def test_label_adds_service_name():
    assert connection_label("Телефон") == "Veltrix VPN · Телефон"


def test_name_length_is_checked_after_trimming():
    assert validate_display_name("  " + "я" * 64 + "  ") == "я" * 64


def test_ipv6_and_encoded_query_survive():
    original = "vless://11111111-1111-4111-8111-111111111111@[2001:db8::1]:443?path=%2Fa%3Fb#old"
    assert display_uri(original, "Работа").partition("#")[0] == original.partition("#")[0]
```

- [x] Run `python -m pytest tests/test_vpn_display.py -q`; confirm missing helper is
  the initial failure, then implement and rerun the behavioral assertions.
- [x] Add the complete pure helper below. No ORM, SSH or runtime imports:

```python
from __future__ import annotations

import base64
import binascii
import json
import unicodedata
from urllib.parse import quote, urlsplit
from uuid import UUID


class InvalidVpnDisplay(ValueError):
    pass


def validate_display_name(value: str) -> str:
    name = value.strip()
    if not 1 <= len(name) <= 64 or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in name):
        raise InvalidVpnDisplay("invalid_display_name")
    return name


def connection_label(name: str) -> str:
    return "Veltrix VPN · " + validate_display_name(name)


def display_uri(uri: str, name: str) -> str:
    label = connection_label(name)
    try:
        if len(uri) > 16384 or any(unicodedata.category(c) in {"Cc", "Cs"} for c in uri):
            raise ValueError("format")
        if uri.startswith("vless://"):
            parsed = urlsplit(uri)
            if not parsed.username or not parsed.hostname or not parsed.port or parsed.password is not None:
                raise ValueError("vless")
            UUID(parsed.username)
            return uri.partition("#")[0] + "#" + quote(label, safe="")
        if uri.startswith("vmess://"):
            payload = uri.removeprefix("vmess://")
            data = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True))
            if not isinstance(data, dict) or not all(data.get(k) for k in ("id", "add", "port")):
                raise ValueError("vmess")
            UUID(str(data["id"]))
            if not isinstance(data["add"], str) or not 1 <= int(data["port"]) <= 65535:
                raise ValueError("vmess_address")
            data["ps"] = label
            return "vmess://" + base64.b64encode(json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()).decode()
        raise ValueError("scheme")
    except (ValueError, TypeError, UnicodeError, binascii.Error):
        raise InvalidVpnDisplay("invalid_configuration") from None
```

- [x] Add IPv6, existing encoded query/fragment, padded/unpadded VMess and missing URI
  tests with real-shaped synthetic credentials; verify equality of every non-label field.
- [x] Run focused tests and Ruff; commit named files as
  `feat: separate VPN display labels from connection identity`.

## Task 2: Add display_name and persistent auth records

**Files:** Modify `backend/app/db/models.py`, `backend/app/db/migrations.py`,
`backend/app/core/config.py`, `backend/pyproject.toml`;
create `backend/tests/test_vpn_portal_schema.py`.

- [x] Add a schema test that creates all tables in SQLite, inserts a customer/session,
  and rejects duplicate token_hash, attempt state_hash and exchange digest. Assert
  existing VpnAccessKey construction still works with `display_name=None`.
- [x] Run the test red against absent models.
- [x] Add `display_name: Mapped[str | None] = mapped_column(String(64), nullable=True)`
  to VpnAccessKey, without changing public_name, external_uuid or config_uri.
- [x] Add the following models using existing Base/import conventions:

```python
class VpnCustomerSession(Base):
    __tablename__ = "vpn_customer_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("vpn_customers.id", ondelete="CASCADE"), index=True)
    telegram_user_id: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VpnPortalLoginAttempt(Base):
    __tablename__ = "vpn_portal_login_attempts"
    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    binding_hash: Mapped[str] = mapped_column(String(64))
    code_verifier: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VpnPortalMiniAppExchange(Base):
    __tablename__ = "vpn_portal_mini_app_exchanges"
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    customer_id: Mapped[int] = mapped_column(ForeignKey("vpn_customers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
```

- [x] Add the idempotent PostgreSQL migration:

```python
"ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS display_name VARCHAR(64) NULL",
```

  New tables are created by the existing `Base.metadata.create_all` startup before
  MIGRATIONS. Do not add a second schema migration framework. Test upgrading an
  old PostgreSQL schema, not only fresh SQLite metadata, in the release rehearsal.
- [x] Insert the settings block from Fixed contracts. Add
  `"PyJWT[crypto]>=2.10,<3"` to backend dependencies; install into the selected local
  interpreter/environment, not production, with `python -m pip install 'PyJWT[crypto]>=2.10,<3'`.
- [x] Run schema test, existing VPN tests and Ruff. Commit named files as
  `feat: add isolated VPN portal persistence and configuration`.

## Task 3: Assign names without changing node identity

**Files:** Create `backend/app/services/vpn_profile_names.py`,
`backend/tests/test_vpn_profile_names.py`; modify `backend/app/main.py` startup,
`backend/app/api/routes/control.py` key issuance and
`backend/app/schemas/control.py` creation request.

- [x] Seed two customers, several subscriptions each, keys with names `test1`, null,
  `Ноутбук` and `dropcatch-8-test1`. Assert numbering is across all keys of each
  customer ordered by ID, not global ID and not limited to active keys. Rerun assignment
  and assert names, public_name, URI and UUID remain unchanged.
- [x] Implement the initialization helpers:

```python
import re
import unicodedata

from sqlalchemy import select

from app.db.models import VpnAccessKey, VpnCustomer, VpnSubscription


def initial_display_name(public_name: str | None, ordinal: int) -> str:
    original = (public_name or "").strip()
    name = "".join(c for c in original if unicodedata.category(c) not in {"Cc", "Cf", "Cs"})[:64].strip()
    if not name or re.fullmatch(r"test\d*", name, flags=re.I) or name.lower().startswith("dropcatch-"):
        return f"Профиль {ordinal}"
    return name


async def initialize_customer_names(db, customer_id: int) -> int:
    await db.scalar(select(VpnCustomer).where(VpnCustomer.id == customer_id).with_for_update())
    rows = list(await db.scalars(select(VpnAccessKey).join(VpnSubscription)
        .where(VpnSubscription.customer_id == customer_id).order_by(VpnAccessKey.id)))
    for ordinal, key in enumerate(rows, start=1):
        if key.display_name is None:
            key.display_name = initial_display_name(key.public_name, ordinal)
    await db.flush()
    return len(rows) + 1


async def backfill_profile_names(session_factory) -> None:
    last_id = 0
    while True:
        async with session_factory() as db:
            customer_ids = list(await db.scalars(select(VpnCustomer.id)
                .where(VpnCustomer.id > last_id).order_by(VpnCustomer.id).limit(100)))
            if not customer_ids:
                return
            for customer_id in customer_ids:
                await initialize_customer_names(db, customer_id)
            await db.commit()
            last_id = customer_ids[-1]
```

- [x] After startup migrations, backfill customer IDs in batches of 100 using this
  helper and commit per batch. Use a keyset cursor (`id > last_id`) and never renumber
  non-null values. Include a zero-customer test and an interruption/restart test.
  In main.py import backfill_profile_names and call
  `await backfill_profile_names(AsyncSessionLocal)` immediately after
  `await run_startup_migrations(engine)` and before runtime bootstrap.
- [x] In key issuance, acquire customer lock **before** subscription/worker locks,
  then assign `display_name` using the returned next ordinal before inserting the key.
  Follow the existing global mutation lock. Do not introduce subscription→customer
  locking opposite to archive's customer→subscription ordering.
- [x] Add the optional display_name creation field now, before the admin UI uses it.
  Extend the existing VpnAccessKeyCreateRequest with the field/validator below; retain
  existing fields and protocol validator unchanged. Import validate_display_name.

```python
display_name: str | None = None

@field_validator("display_name")
@classmethod
def validate_new_display_name(cls, value: str | None) -> str | None:
    return None if value is None else validate_display_name(value)
```

  Before acquiring any subscription/worker row lock, resolve its customer ID, lock
  that customer, then reread/lock the subscription and verify its customer did not
  change. A changed association returns 409; a missing subscription/customer returns
  404. Retry only on a new request.
  Once existing issuance policy passes, assign this expression before `db.add(access_key)`:

```python
next_ordinal = await initialize_customer_names(db, subscription.customer_id)
access_key.display_name = (
    payload.display_name if payload.display_name is not None
    else initial_display_name(payload.public_name, next_ordinal)
)
```

  The existing route already names these objects `db`, `payload`, `subscription`
  and `access_key`; do not rename them as part of this task.
- [x] Keep the previous `public_name` in the provision/revoke/suspend payload. Add a
  regression test capturing these payloads before and after display_name changes.
- [x] Run `python -m pytest tests/test_vpn_profile_names.py tests/test_vpn_control_api.py tests/test_vpn_remote_policy.py -q`;
  commit as `feat: assign stable customer-facing VPN profile names`.

## Task 4: Canonical verified identity and Mini App validation

**Files:** Create `backend/app/services/vpn_telegram_identity.py`,
`backend/app/services/vpn_portal_telegram.py`,
`backend/tests/test_vpn_portal_telegram.py`.

- [x] Test valid signed data, wrong token/hash, timestamp older than 300 seconds,
  timestamp over 30 seconds ahead, duplicate query keys, malformed user JSON,
  boolean/negative/too-large ID and a reordered but equivalent query string.
- [x] Use this signing fixture with a fake token only:

```python
import hashlib
import hmac
import json
from urllib.parse import urlencode


def signed_init_data(user_id: int, timestamp: int, token: str = "12345:test-only") -> str:
    fields = {"auth_date": str(timestamp), "query_id": "test-query",
              "user": json.dumps({"id": user_id, "first_name": "Тест"}, separators=(",", ":"))}
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)
```

- [x] Define the identity object and normalization in `vpn_telegram_identity.py`:

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class TelegramIdentity:
    user_id: str
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None


def telegram_user_id(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("invalid_identity")
    raw = str(value)
    if not raw.isascii() or not raw.isdecimal() or not 0 < int(raw) < 2**52:
        raise ValueError("invalid_identity")
    return str(int(raw))


def optional_text(value: object, length: int) -> str | None:
    return value[:length] if isinstance(value, str) and value else None


def identity_from_user(user: dict) -> TelegramIdentity:
    return TelegramIdentity(telegram_user_id(user.get("id")),
        optional_text(user.get("username"), 128), optional_text(user.get("first_name"), 128),
        optional_text(user.get("last_name"), 128))
```

- [x] Implement Mini App verification, returning identity plus **canonical** replay
  digest; hashing the original query string alone would allow parameter-order replay:

```python
import hashlib
import hmac
import json
import re
from urllib.parse import parse_qsl

from app.services.vpn_telegram_identity import identity_from_user


class TelegramAuthenticationError(ValueError):
    pass


def verify_mini_app(raw: str, bot_token: str, now_seconds: int):
    try:
        if not bot_token or not raw or len(raw.encode()) > 16384:
            raise ValueError("size")
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("duplicates")
        received = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", received):
            raise ValueError("hash")
        check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, received):
            raise ValueError("signature")
        if not re.fullmatch(r"[0-9]{1,12}", fields["auth_date"]):
            raise ValueError("timestamp")
        timestamp = int(fields["auth_date"])
        if not now_seconds - 300 <= timestamp <= now_seconds + 30:
            raise ValueError("age")
        user = json.loads(fields["user"])
        if not isinstance(user, dict):
            raise ValueError("user")
        identity = identity_from_user(user)
        digest = hashlib.sha256((check + "\n" + received).encode()).hexdigest()
        return identity, digest, timestamp
    except (ValueError, TypeError, KeyError, OverflowError):
        raise TelegramAuthenticationError("telegram_authentication_failed") from None
```

- [x] Add `resolve_telegram_customer(db, identity)` to the identity module: SELECT
  by canonical ID; if absent INSERT inside `db.begin_nested()`, catch IntegrityError
  outside that savepoint and SELECT the winning row. Preserve status/subscriptions;
  update only username/first_name/last_name. Do not roll back the bot's durable update.
  Test both pre-existing archived customer and concurrent insert using PostgreSQL.

```python
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.db.models import VpnCustomer


async def resolve_telegram_customer(db, identity: TelegramIdentity):
    statement = select(VpnCustomer).where(
        VpnCustomer.telegram_user_id == identity.user_id
    ).with_for_update().execution_options(populate_existing=True)
    customer = await db.scalar(statement)
    if customer is None:
        try:
            async with db.begin_nested():
                customer = VpnCustomer(telegram_user_id=identity.user_id, status="active")
                db.add(customer)
                await db.flush()
        except IntegrityError:
            customer = await db.scalar(statement)
            if customer is None:
                raise
    customer.telegram_username = identity.username
    customer.first_name = identity.first_name
    customer.last_name = identity.last_name
    await db.flush()
    return customer
```
- [x] Run focused test and existing Telegram tests; commit as
  `feat: verify Telegram identities for customer portal access`.

## Task 5: Sessions, allowlist, CSRF and durable one-use records

**Files:** Create `backend/app/services/vpn_portal_auth.py`,
`backend/tests/test_vpn_portal_auth.py`.

- [x] Write tests for expired/revoked sessions, archived customer, changed Telegram
  binding, wrong cookie namespace, wrong CSRF, wrong/missing Origin, closed pilot,
  duplicate Mini App digest, and OIDC attempts claimed once across two DB connections.
- [x] Define the session and policy helpers:

  Validate configuration before using it in redirects or Origin comparisons:
  reject whitespace/control characters, backslashes, credentials, query/fragment
  delimiters (including empty ones), non-root paths, and invalid/zero/out-of-range
  ports. Canonicalize hostname case and default ports. Plain HTTP is allowed only
  for localhost/127.0.0.1 with the explicit development setting. Fail with the safe
  `portal_configuration_invalid` message, not a parser exception containing input.
  The sketch below is illustrative; these stricter checks are required.
  Database-backed session/attempt lookups must refresh identity-map values with
  populate_existing=True. Normalize supplied timestamps to UTC consistently.

```python
import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db.base import utcnow
from app.db.models import VpnCustomer, VpnCustomerSession

SESSION_COOKIE = "veltrix_customer_session"
BINDING_COOKIE = "veltrix_login_binding"


def digest_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def csrf_token(raw_session: str) -> str:
    return hmac.new(raw_session.encode(), b"veltrix-portal-csrf-v1", hashlib.sha256).hexdigest()


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def public_origin(settings) -> str:
    raw = settings.vpn_portal_public_origin.rstrip("/")
    parsed = urlsplit(raw)
    local = parsed.hostname in {"localhost", "127.0.0.1"}
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path
            or parsed.query or parsed.fragment
            or (parsed.scheme != "https" and not (local and parsed.scheme == "http"
                and settings.vpn_portal_allow_local_http))):
        raise ValueError("portal_configuration_invalid")
    return raw


def identity_allowed(settings, user_id: str) -> bool:
    allowed = {part.strip() for part in settings.vpn_portal_allowed_telegram_ids.split(",") if part.strip()}
    return settings.vpn_portal_enabled and (settings.vpn_portal_public_access or user_id in allowed)


@dataclass(frozen=True)
class PortalPrincipal:
    customer: VpnCustomer
    session: VpnCustomerSession
    csrf: str


async def issue_session(db, customer_id: int, verified_user_id: str, now=None) -> str:
    current = now or utcnow()
    customer = await db.scalar(select(VpnCustomer).where(VpnCustomer.id == customer_id)
        .with_for_update().execution_options(populate_existing=True))
    if (customer is None or customer.status != "active"
            or customer.telegram_user_id != verified_user_id):
        raise ValueError("customer_unavailable")
    raw = secrets.token_urlsafe(32)
    db.add(VpnCustomerSession(token_hash=digest_token(raw), customer_id=customer.id,
        telegram_user_id=customer.telegram_user_id, created_at=current,
        expires_at=current + timedelta(days=7)))
    await db.flush()
    return raw


async def lookup_session(db, raw: str | None, settings, now=None):
    if not raw or len(raw) > 256 or not settings.vpn_portal_enabled:
        return None
    row = (await db.execute(select(VpnCustomerSession, VpnCustomer)
        .join(VpnCustomer, VpnCustomer.id == VpnCustomerSession.customer_id)
        .where(VpnCustomerSession.token_hash == digest_token(raw)))).first()
    if row is None:
        return None
    session, customer = row
    if (session.revoked_at is not None or as_utc(session.expires_at) <= (now or utcnow())
            or customer.status != "active" or session.telegram_user_id != customer.telegram_user_id
            or not identity_allowed(settings, session.telegram_user_id)):
        return None
    return PortalPrincipal(customer, session, csrf_token(raw))


def valid_mutation(origin: str | None, token: str | None, principal, settings) -> bool:
    return (origin == public_origin(settings) and token is not None and len(token) == 64
        and token.isascii() and hmac.compare_digest(token, principal.csrf))
```

- [x] OIDC attempt creation generates three independent random values (state,
  browser binding, verifier). Save only state/binding digests; verifier is temporary
  server-side data. Return raw values to the HTTP layer, never log them.

```python
from app.db.models import VpnPortalLoginAttempt


async def create_login_attempt(db, now=None):
    current = now or utcnow()
    state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    db.add(VpnPortalLoginAttempt(state_hash=digest_token(state),
        binding_hash=digest_token(binding), code_verifier=verifier,
        created_at=current, expires_at=current + timedelta(minutes=10)))
    await db.flush()
    return state, binding, verifier
```
- [x] Implement atomic claim with this transaction order:

```python
from sqlalchemy import select
from app.db.models import VpnPortalLoginAttempt


async def consume_login_attempt(db, state: str, binding: str, now):
    if not state or not binding or len(state) > 256 or len(binding) > 256:
        return None
    row = await db.scalar(select(VpnPortalLoginAttempt).where(
        VpnPortalLoginAttempt.state_hash == digest_token(state),
        VpnPortalLoginAttempt.binding_hash == digest_token(binding),
        VpnPortalLoginAttempt.expires_at > now,
        VpnPortalLoginAttempt.consumed_at.is_(None),
    ).with_for_update())
    if row is None or not row.code_verifier:
        return None
    verifier = row.code_verifier
    row.code_verifier = None
    row.consumed_at = now
    await db.commit()
    return verifier
```

  Commit the claim **before** calling Telegram. Failed exchanges require a fresh
  login. Never retry consumed attempts by resetting consumed_at.
- [x] Mini App exchange: verify first, enforce pilot/status, then insert canonical
  digest and issue session in one transaction. Unique-conflict means replay. A
  previously valid session for the same customer may return its current session
  DTO without inserting a new session; a replay with a different/no session gets 401.
  For a fresh payload with a different active customer cookie, return 409 and require
  explicit logout; never silently replace one customer's session with another.
  Call `issue_session(db, customer.id, identity.user_id)` only after verified identity,
  allowlist and customer resolution, in that same transaction. Recheck archive/rebind
  races with two PostgreSQL connections: stale ORM objects must not issue a new session.
  Session reuse must also lock/refetch the existing customer and verify the session's
  current binding/status, including when a fresh payload resolves to a different
  customer after concurrent rebinding. Reject the stale session and roll back all
  exchange-side mutations; committing the caller's error path must not leave an
  orphan customer, replay claim or profile update.
  Existing sessions still fail their next lookup immediately after archive/rebinding.
  Retain a successful Mini App digest for 10 minutes from exchange time. This exceeds
  its entire remaining acceptance window, including future skew and integer-second
  rounding; cleanup must not make a still-acceptable signed payload reusable.
- [x] Cleanup helper deletes up to 100 expired attempts, exchanges and sessions per
  call, ordered by expires_at/primary key. Only expires_at <= now qualifies. Hook it
  into existing scheduled VPN maintenance with a separate short transaction; a
  cleanup error must not cancel key suspension/revocation maintenance.
  The limit is 100 per table; cleanup must never remove an unexpired replay claim.
- [x] Test cookie issuance (HttpOnly, Secure, no Domain, Max-Age 604800), deletion
  with the same Path and namespace, and single-use claims on real PostgreSQL.
- [x] Run tests and Ruff; commit as `feat: isolate customer sessions and authentication replay protection`.

## Task 6: Official browser login client with bounded JWKS cache

**Files:** Extend `backend/app/services/vpn_portal_telegram.py`;
create `backend/tests/test_vpn_portal_oidc.py`.

- [x] Generate a test-only RSA key in the test process. Sign short-lived JWT fixtures
  using PyJWT; test invalid signature, HS256/none algorithm, wrong issuer/audience,
  absent id/exp/iat/sub and expired/future token. Add a positive `id != sub` case:
  these claims represent different identifiers, so a valid token must succeed and
  return identity from `id`, without requiring equality with `sub`. Never use production tokens.
- [x] Pin endpoints in the module, not from request data:

```python
TELEGRAM_ISSUER = "https://oauth.telegram.org"
TELEGRAM_AUTHORIZE_URL = TELEGRAM_ISSUER + "/auth"
TELEGRAM_TOKEN_URL = TELEGRAM_ISSUER + "/token"
TELEGRAM_JWKS_URL = TELEGRAM_ISSUER + "/.well-known/jwks.json"
```

- [x] Add code challenge and authorization URL builder:

```python
import base64
from urllib.parse import urlencode


def authorization_url(client_id: str, callback: str, state: str, verifier: str) -> str:
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return TELEGRAM_AUTHORIZE_URL + "?" + urlencode({
        "client_id": client_id, "redirect_uri": callback, "response_type": "code",
        "scope": "openid profile", "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256",
    })
```

- [x] Exchange code with httpx AsyncClient(timeout=10, follow_redirects=False),
  BasicAuth(client_id, client_secret), form fields grant_type=authorization_code,
  code, fixed redirect_uri, client_id and code_verifier. Cap received JSON at 1 MiB;
  require a string id_token <= 16 KiB. Enforce the response cap while streaming,
  including decompressed bytes, rather than only after buffering the whole response.
  Apply the same bounded reader to JWKS. Discard access_token immediately; no userinfo call.
- [x] Use an async lock and monotonic clock to cache official JWKS for 300 seconds.
  On missing kid, allow one forced refresh no more often than once per 30 seconds.
  Bound the document to at most 20 keys; select only eligible RSA/RS256 signing keys.
  Reject duplicate kid values and never select unapproved alg/use, but ignore keys
  for other supported Telegram algorithms rather than rejecting the whole document.
  Live public JWKS inspection on 2026-09-20 returned RSA/RS256 (`oidc-1`, no `use`
  field), EC/ES256, OKP/EdDSA and EC/ES256K. Missing `use` is valid for the RSA key;
  accept use absent or `sig`, alg absent or `RS256`. Mixed-key and missing-use
  fixtures must pass for an RS256 token; this does not enable other token algorithms.
  No redirects, token-provided jku/x5u downloads, stale-key fallback after expiry or
  unverified JWT claims. Test cache hits, rotation, throttled unknown kid and outages.
- [x] Verify token using the selected key with explicit algorithms/claims:

```python
import jwt


def verify_id_token(token: str, jwk: dict, client_id: str):
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != "RS256" or header.get("kid") != jwk.get("kid"):
            raise ValueError("header")
        key = jwt.PyJWK.from_dict(jwk, algorithm="RS256").key
        claims = jwt.decode(token, key=key, algorithms=["RS256"],
            issuer=TELEGRAM_ISSUER, audience=client_id, leeway=30,
            options={"require": ["iss", "aud", "sub", "iat", "exp", "id"]})
        return identity_from_user({"id": claims["id"],
            "username": claims.get("preferred_username"),
            "first_name": claims.get("given_name") or claims.get("name"),
            "last_name": claims.get("family_name")})
    except (jwt.PyJWTError, ValueError, TypeError, KeyError):
        raise TelegramAuthenticationError("telegram_authentication_failed") from None
```

- [x] Inject MockTransport/key provider in tests. Assert no request leaves the three
  fixed official URLs and all network/parser errors become a generic safe failure.
- Integration contract: `TelegramJWKSProvider(http_client, monotonic=...)` exposes
  async `get_key(kid)`. `exchange_authorization_code(client_id, client_secret,
  callback, code, verifier, *, http_client, jwks_provider)` returns verified
  TelegramIdentity. Task 8 owns one app-lifespan client/provider pair and closes the
  client at shutdown; a per-request provider would discard the bounded shared cache.
  Requests explicitly enforce timeout=10 and follow_redirects=False even for an
  injected client. Failed unknown-kid refresh attempts also consume the cooldown.
- [x] Run focused tests and commit as `feat: support Telegram OIDC browser sign-in`.

## Task 7: Shared own-customer read model and entitlement

**Files:** Create `backend/app/services/vpn_customer_view.py`,
`backend/app/schemas/vpn_portal.py`, `backend/tests/test_vpn_customer_view.py`.

- [x] Test two customers, several subscriptions, expired/future/paused/cancelled
  states and every key status. Assert lists contain no URI, UUID, notes, worker
  credentials or Telegram administration fields. Foreign and missing IDs are both absent.
- [x] Define explicit Pydantic response/request models:

```python
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.services.vpn_display import validate_display_name


class PortalMe(BaseModel):
    display_name: str
    csrf_token: str


class PortalSubscription(BaseModel):
    id: int
    service_name: str = "Veltrix VPN"
    state: str
    starts_at: datetime | None
    expires_at: datetime | None
    profile_limit: int
    profiles_used: int
    traffic_limit_gb_per_profile: int | None


class PortalProfile(BaseModel):
    id: int
    subscription_id: int
    display_name: str
    state: str
    can_connect: bool


class PortalConnection(BaseModel):
    uri: str


class RenamePortalProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str

    @field_validator("display_name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        return validate_display_name(value)


class MiniAppLogin(BaseModel):
    model_config = ConfigDict(extra="forbid")
    init_data: str = Field(min_length=1, max_length=16384)
```

- [x] Add pure effective state and entitlement functions. Status precedence is
  explicit; no device-slot check when reading an already issued key:

```python
from app.services.vpn_portal_auth import as_utc


def subscription_state(subscription, now):
    if subscription.status not in {"active", "trial"}:
        return subscription.status
    if subscription.starts_at and as_utc(subscription.starts_at) > now:
        return "scheduled"
    if subscription.expires_at and as_utc(subscription.expires_at) <= now:
        return "expired"
    return subscription.status


def may_read_connection(customer, subscription, key, now):
    return (customer.status == "active" and subscription_state(subscription, now) in {"active", "trial"}
        and key.status == "active" and bool(key.config_uri)
        and (key.expires_at is None or as_utc(key.expires_at) > now))
```

- [x] List subscriptions only by customer_id, count reserved key slots using existing
  DEVICE_SLOT_STATUSES, order usable first and then by expiry/ID. List profiles via
  join to subscriptions with that same filter; key metadata never includes URI.
- [x] Use the following ownership query for both connection and rename, adding
  `with_for_update(of=VpnAccessKey)` only for rename:

```python
select(VpnAccessKey, VpnSubscription).join(
    VpnSubscription, VpnSubscription.id == VpnAccessKey.subscription_id
).where(VpnAccessKey.id == profile_id, VpnSubscription.customer_id == customer_id)
```

- [x] Connection request rechecks current entitlement, calls display_uri, and returns
  409 with a generic code for unavailable/malformed own credentials. Unknown internal
  statuses display an unavailable state, never a claim that VPN is connected.
  The metadata can_connect flag must not promise a usable link when parsing the
  stored configuration fails. Refresh queried ORM state; do not trust a previously
  loaded key/subscription after expiry, revocation or an ownership change. Rename
  only flushes its display-name change; the HTTP caller commits. The read model
  does not replace Task 5/8's session/Telegram-binding authentication checks.
- Integration contract: `list_customer_subscriptions(db, customer_id, now=None)`,
  `list_customer_profiles(db, customer, now=None)`,
  `customer_connection(db, customer, profile_id, now=None)` and
  `rename_customer_profile(db, customer, profile_id, display_name, now=None)` return
  explicit portal DTOs. Use distinct safe missing-profile/unavailable-connection
  errors that Task 8 maps to identical foreign/missing 404 and own-unavailable 409.
- [x] Run view tests plus existing subscription/lifecycle tests; commit as
  `feat: expose ownership-scoped VPN customer data`.

## Task 8: Portal HTTP routes and request guards

**Files:** Create `backend/app/api/routes/vpn_portal.py`,
`backend/app/services/vpn_portal_http.py`, `backend/tests/test_vpn_portal_api.py`;
modify `backend/app/api/__init__.py`, `backend/app/main.py`.

- [x] Build a fixture following test_vpn_telegram.py: fresh SQLite metadata,
  AsyncSession factory, test Settings with HTTPS origin, fake OIDC transport,
  both portal and existing control routers. Do not override require_admin for
  authorization-boundary tests. Make separate clients with independent cookie jars.
- [x] Add read-only config DTO `{enabled, browser_login_enabled, mini_app_enabled,
  login_path, support_text}`. Flags reflect valid configuration, never secret values.
- [x] Router prefix `/vpn-portal`; register once under existing `/api` prefix.
  Use a customer dependency based on lookup_session, not get_current_user.
  Apply no-store to all portal responses, including errors, without affecting workers.
- [x] Exclude portal requests from the legacy configurable CORS middleware, rather
  than inheriting wildcard or externally configured admin origins. Put this scoped
  class in vpn_portal_http.py; replace only the middleware class in main.py and pass
  `portal_prefix=settings.api_prefix + "/vpn-portal"`. Preserve all existing CORS
  arguments for non-portal routes. Same-origin portal fetch does not need CORS headers.

```python
from starlette.middleware.cors import CORSMiddleware


class ControlCorsMiddleware(CORSMiddleware):
    def __init__(self, app, *, portal_prefix: str, **kwargs):
        super().__init__(app, **kwargs)
        self.portal_prefix = portal_prefix.rstrip("/")

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if scope["type"] == "http" and (
            path == self.portal_prefix or path.startswith(self.portal_prefix + "/")
        ):
            await self.app(scope, receive, send)
            return
        await super().__call__(scope, receive, send)
```

  Test with the actual application middleware stack and both wildcard/admin CORS
  configurations. A foreign-origin portal request must never receive
  Access-Control-Allow-Origin/Access-Control-Allow-Credentials. Do not override the
  customer/admin dependencies in these integration tests.
- [x] FastAPI's default validation error can echo the submitted initData or an
  unexpected credential-bearing field. Register this handler in main.py, retaining
  default behavior outside the portal. Never include validation `input` or `ctx`.

```python
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse


async def portal_validation_error(request, exc: RequestValidationError):
    prefix = settings.api_prefix + "/vpn-portal"
    if request.url.path == prefix or request.url.path.startswith(prefix + "/"):
        return JSONResponse(status_code=422, content={"detail": "invalid_customer_request"},
                            headers={"Cache-Control": "no-store"})
    return await request_validation_exception_handler(request, exc)


app.add_exception_handler(RequestValidationError, portal_validation_error)
```

  Here registration goes inside create_app, after constructing its local `app`;
  the handler uses main.py's existing settings. Assert oversized/malformed initData
  and forbidden request fields never appear in the response body or captured logs.
- [x] Implement the mutation guard as a dependency of rename/logout only:

```python
from fastapi import HTTPException, Request


def require_customer_mutation(request: Request, principal, settings):
    if not valid_mutation(request.headers.get("origin"),
            request.headers.get("x-csrf-token"), principal, settings):
        raise HTTPException(403, "customer_request_rejected")
```

- [x] For Mini App require exact origin, application/json (charset allowed), bounded
  body and settings capability before parsing. For browser start, generate attempt
  and cookie and redirect; for callback reject duplicate code/state, missing/wrong
  binding and provider errors, consume attempt before exchange, enforce allowlist,
  resolve customer and commit session before setting cookie. Clear binding cookie
  on terminal success/failure. Do not return provider diagnostics to the browser.
- [x] Cookie helper uses constants from Task 5 and `public_origin(settings)` to choose
  Secure; callback's only target is `/cabinet/` with optional fixed safe error code
  in the fragment. No user-supplied next/return URL.
- [x] Implement each data endpoint from the contract table using Task 7 helpers.
  GET connection never changes key status. PATCH accepts RenamePortalProfile only.
  Logout updates only current session.revoked_at and deletes its cookie, even if
  another administrative cookie is present.
- [x] Add this exact API assertion pattern with fixture-provided alice/bob keys:

```python
async def assert_customer_isolation(client, own_key_id, other_key_id, csrf):
    own = await client.get(f"/api/vpn-portal/profiles/{own_key_id}/connection")
    assert own.status_code == 200
    foreign = await client.get(f"/api/vpn-portal/profiles/{other_key_id}/connection")
    missing = await client.get("/api/vpn-portal/profiles/99999999/connection")
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()
    rename = await client.patch(f"/api/vpn-portal/profiles/{other_key_id}",
        headers={"Origin": "https://vpn.example", "X-CSRF-Token": csrf},
        json={"display_name": "Чужой профиль"})
    assert rename.status_code == 404
    assert (await client.get("/api/control/vpn/customers")).status_code in {401, 403}
```

- [x] Test all guards without CSRF, with another session's CSRF, foreign Origin,
  invalid content type, oversized body and extra writable fields. Revoke/archive
  and expire data after initial page load, then assert the next fetch cannot reveal URI.
- [x] Verify valid sessions cannot bypass disabled feature/pilot setting. Raw admin
  cookie alone returns 401 on portal/me. Run API/auth/view tests; commit as
  `feat: add secure VPN customer portal API`.

## Task 9: Independent frontend API and view helpers

**Files:** Create `frontend/src/vpn-portal/{types.ts,api.ts,view.ts}`,
`frontend/test/vpnPortalApi.test.mjs`, `frontend/test/vpnPortalView.test.mjs`.

- [x] Mirror Task 7 DTOs in types.ts, keeping nullable dates explicit and numeric
  IDs as number. Config interface matches Task 8. Do not import `../api.ts` admin types.

```typescript
export interface PortalConfig {
  enabled: boolean;
  browser_login_enabled: boolean;
  mini_app_enabled: boolean;
  login_path: string | null;
  support_text: string;
}
export interface PortalMe { display_name: string; csrf_token: string }
export interface PortalSubscription {
  id: number;
  service_name: string;
  state: string;
  starts_at: string | null;
  expires_at: string | null;
  profile_limit: number;
  profiles_used: number;
  traffic_limit_gb_per_profile: number | null;
}
export interface PortalProfile {
  id: number;
  subscription_id: number;
  display_name: string;
  state: string;
  can_connect: boolean;
}
export interface PortalConnection { uri: string }
```
- [x] Use a private generic request helper; no persistent token storage:

```typescript
export class PortalError extends Error {
  readonly status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function portalRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`/api/vpn-portal${path}`, {
    ...options,
    credentials: "same-origin",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    const message = response.status === 401 ? "Нужно войти снова."
      : response.status === 403 ? "Действие недоступно."
      : response.status === 429 ? "Слишком много попыток. Повторите позже."
      : "Не удалось выполнить запрос. Попробуйте ещё раз.";
    throw new PortalError(response.status, message);
  }
  return await response.json() as T;
}
```

- [x] Add typed methods for config/me/loginMiniApp/logout/subscriptions/profiles/
  connection/rename using contract paths; mutations attach X-CSRF-Token, not a
  manually set Origin header. Do not include real URLs, credentials or demo statistics.

```typescript
import type {
  PortalConfig, PortalMe, PortalSubscription, PortalProfile, PortalConnection,
} from "./types";

export const portalApi = {
  config: () => portalRequest<PortalConfig>("/config"),
  me: () => portalRequest<PortalMe>("/me"),
  loginMiniApp: (initData: string) => portalRequest<PortalMe>("/auth/mini-app", {
    method: "POST", body: JSON.stringify({ init_data: initData }),
  }),
  logout: (csrf: string) => portalRequest<{ logged_out: boolean }>("/logout", {
    method: "POST", headers: { "X-CSRF-Token": csrf },
  }),
  subscriptions: () => portalRequest<PortalSubscription[]>("/subscriptions"),
  profiles: () => portalRequest<PortalProfile[]>("/profiles"),
  connection: (id: number) => portalRequest<PortalConnection>(`/profiles/${id}/connection`),
  rename: (id: number, displayName: string, csrf: string) => portalRequest<PortalProfile>(`/profiles/${id}`, {
    method: "PATCH", headers: { "X-CSRF-Token": csrf },
    body: JSON.stringify({ display_name: displayName }),
  }),
};
```

  Logout returns JSON `{logged_out: true}` with HTTP 200, not an empty 204, so the
  common response parser remains valid. Subscription/profile endpoints return arrays.
- [x] Test calls using a fake global fetch. Assert credentials/cache, exact URI,
  no CSRF on read, correct CSRF on writes, safe error text ignoring server secrets,
  and no calls to `/control/` or `/auth/`.
  This means legacy admin `/api/control` and `/api/auth`, not the intentional portal
  `/api/vpn-portal/auth/mini-app` path. Keep the general request helper private. Wrap
  fetch failures and invalid successful JSON in safe errors too; raw parser/network
  exception messages can contain upstream response content. Do not log these values.
- [x] Add a total Russian status mapping with an unknown fallback, and date formatter:

```typescript
const labels: Record<string, string> = {
  active: "Активна", trial: "Пробный доступ", scheduled: "Начнётся позже",
  disabled: "Приостановлена", expired: "Истекла", cancelled: "Отменена",
  pending_sync: "Подготавливается", syncing: "Подготавливается",
  suspended: "Приостановлен", pending_suspend: "Отключается",
  pending_revoke: "Отзывается", revoked: "Отозван", failed: "Нужна помощь",
};

export function stateLabel(state: string): string {
  return Object.prototype.hasOwnProperty.call(labels, state)
    ? labels[state] : "Статус уточняется";
}

export function portalDate(value: string | null): string {
  if (value === null) return "Без срока окончания";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Дата недоступна";
  return new Intl.DateTimeFormat("ru-RU", {
    day: "numeric", month: "long", year: "numeric",
  }).format(date);
}
```

- [x] Run `npm test`; commit as `feat: add typed customer portal client and presentation helpers`.

## Task 10: Customer cabinet and Mini App lifecycle

**Files:** Create `frontend/cabinet/index.html`,
`frontend/src/vpn-portal/{main.tsx,Portal.tsx,bootstrap.ts,ProfileCard.tsx,portal.css}`;
modify `frontend/vite.config.ts`. Keep launch/auth bootstrap in its small module and
profile reveal/rename/copy UI in ProfileCard rather than growing one monolithic view.
Add focused Node tests for bootstrap helpers and a repeatable headless browser QA
script; use the available external test runtime, not new application dependencies.

- [ ] Add independent HTML entry with title Veltrix VPN, Russian lang, viewport and
  no-referrer meta. Load official Telegram WebApp SDK and own main.tsx; no admin
  script or shared admin CSS. Render React StrictMode into its own root.
- [ ] Configure Vite multi-page build preserving the main entry:

```typescript
import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      input: {
        admin: fileURLToPath(new URL("./index.html", import.meta.url)),
        cabinet: fileURLToPath(new URL("./cabinet/index.html", import.meta.url)),
      },
    },
  },
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8000", changeOrigin: true } },
  },
});
```

- [ ] Keep five local tab states: subscription, profiles, connect, plans, help. Use
  `/cabinet/#profiles` hash navigation; no backend catch-all that swallows unknown APIs.
  Each tab has real loading/error/empty states and one clear primary action.
- [ ] Bootstrap config, then verify nonempty original Mini App initData on the server
  before displaying any cookie-authenticated customer data. A previous customer's
  valid cookie must not bypass this identity check after switching Telegram accounts.
  Same-customer session is reusable; different customer returns 409 and requires an
  explicit logout/account switch before a fresh launch. Never compare trusted identity
  with initDataUnsafe in the browser. With no launch payload, use `/me`; on 401 show
  configured browser login or «Закройте и снова откройте кабинет» for Mini App.
  A module-level in-flight promise deduplicates bootstrap under StrictMode. Handle
  invalid/expired launch data without falling back to displaying a previous account.
  The SDK also defines Telegram.WebApp in an ordinary browser: its mere existence
  does not prove Mini App context. With no signed payload and the default unknown
  platform, show normal browser login; test this case with the SDK object present.
  In the 409 state, an explicit logout action may fetch `/me` solely to obtain the
  existing session's CSRF token; discard its display name and never fetch/render its
  subscriptions or profiles. After logout require a fresh launch, not a silent retry
  with the captured payload. This keeps logout usable on a fresh page with no cached
  CSRF while avoiding a previous-account data flash.
  The official SDK source was inspected: it persists launch parameters in
  `sessionStorage["__telegram__initParams"]`, including tgWebAppData. Capture the
  original signed string in memory, remove that property from this specific cache
  immediately at bootstrap (before the first network await), and remove launch
  authentication parameters from the address bar with history.replaceState. Retain
  unrelated SDK theme/platform values and unrelated application storage. Test both
  reload with a valid cookie and fresh launch without one; never synthesize initData.
  Browser QA must check this actual SDK cache, not only search our source for storage.
  After a successful exchange, confirm a cookie-authenticated request succeeds. A
  third-party iframe may block SameSite=Lax cookies: do not weaken cookie policy or
  loop through repeated exchanges. Offer the same cabinet in an external browser
  and record which Telegram clients actually passed the live pilot.
- [ ] Use a session generation counter or AbortController for authenticated requests.
  Logout and 401 clear CSRF, profile URIs and customer state, increment generation,
  and prevent late responses restoring old data. Do not auto-login again immediately
  after explicit logout. An account mismatch requires a fresh session, not data mixing.
- [ ] Subscription tab renders own cards with name Veltrix VPN, dates, status and
  reserved profile slots. Display `Статистика пока недоступна`, not 0 GB. Explain
  per-profile traffic cap only if the actual subscription has one.
- [ ] Profiles tab fetches URI on explicit action only. Full URI uses a wrapping,
  read-only textarea; copy calls Clipboard API from a user click after the URI is
  loaded. Failed clipboard permission retains selectable text and truthful feedback.
  Editable name form has save/cancel/busy/error states and maxLength=64; success
  replaces returned metadata, clears cached URI and requires a fresh reveal.
  A raw active key with `can_connect=false` is not usable: show an unavailable hint
  and disable reveal. Do not infer an established VPN connection from any API state.
- [ ] Connect tab has explicit platform choice (iPhone, Android, Windows, macOS)
  and manual Happ instructions: install official application, open profile, copy
  and import link, connect. Do not invent app-store URLs or unverified deep links.
- [ ] Plans tab says `Тарифы ещё не опубликованы`; Help shows configured text as
  text. Do not publish raw test plans, zero prices, fake speed/security guarantees
  or enable payment buttons. No self-issue/revoke/change-plan UI in this delivery.
- [ ] Root-scope CSS to `.veltrix-portal`, use CSS light-dark colors, visible focus,
  44px touch controls, max-width content, wrapping links and safe-area padding.
  Use system font, restrained green accent and 1-column layout below 640px. Avoid
  horizontal scroll and fixed-height card content; Telegram safe-area changes must
  not hide controls. Match the approved concept, not its demo numbers.
- [ ] Browser tests mock API responses, not UI internals: independent users, sign-in
  states, all five sections, rename success/error, copy fallback, expired subscription,
  logout during in-flight URI request. Check 320/390/768/1280px and light/dark themes.
  Include a new signed Mini App launch for Bob while Alice's valid cookie is present;
  no Alice subscription/profile data may render before explicit account switching.
- [ ] `npm test` and `npm run build`; verify both dist/index.html and
  dist/cabinet/index.html exist and direct GET `/cabinet/` works through FastAPI.
  Commit as `feat: add responsive Veltrix customer cabinet`.

## Task 11: Bot and admin use the same names and customer rules

**Files:** Modify `backend/app/services/vpn_telegram.py`,
`backend/app/api/routes/control.py`, `backend/app/schemas/control.py`,
`frontend/src/api.ts`, `frontend/src/VpnCustomerWorkspacePanel.tsx`;
extend Telegram/control/frontend tests.

- [ ] Replace bot's lookup/create fragment with resolve_telegram_customer, keeping
  durable update claiming and outgoing-message idempotency. Do not move network
  sending inside a retried DB transaction.
- [ ] Keep `/start`, `/status`, `/keys`, `/support`. Add button text aliases for
  «Моя подписка», «Мои профили», «Помощь». Return friendly service/date states instead
  of raw IDs/English status; use the same entitlement/display helpers as portal.
  Preserve existing support configuration; do not claim payment processing exists.
- [ ] Build keyboard separately and test it as a pure helper. Add cabinet WebApp
  button only for a private positive chat ID allowed by enabled portal settings:

```python
{"text": "Личный кабинет", "web_app": {"url": public_origin(settings) + "/cabinet/"}}
```

  Group responses must not include web_app buttons or customer credentials. If
  feature/config is unavailable, existing command keyboard continues to work.
- [ ] Add display_name to admin key DTO and display-name-only request. Preserve
  raw persisted config_uri, but serialize its labelled representation through one
  helper for every admin key response. Malformed stored URI becomes unavailable
  without crashing the entire key list. No arbitrary raw URI echo in errors.
- [ ] Add admin PATCH endpoint from Fixed contracts with require_admin, name-only
  validation, audit action `vpn_access_key_display_name_update` and key ID only.
  Do not log URI/name in audit detail, run SSH or change public_name.
- [ ] Admin UI title/copy/expanded link use new display fields. Add inline name edit
  with save/cancel; preserve selection after reload. Creation form passes desired
  display_name separately, keeping legacy public_name compatibility for old callers.
- [ ] Update old tests asserting `VPN-бот готов`, subscription numeric headings or
  opaque fake `vless://test-1` to assert intended Russian output and structurally
  valid synthetic URIs. Do not weaken unrelated safety assertions.
- [ ] Run full backend/frontend suites; commit as
  `feat: unify Veltrix profile presentation across bot and admin`.

## Task 12: Secret-safe logging and production request limits

**Files:** Create `backend/app/services/vpn_portal_logging.py`,
`backend/tests/test_vpn_portal_logging.py`, `deploy/nginx-vpn-portal-http.conf`,
`deploy/nginx-vpn-portal-locations.conf`; modify `backend/app/main.py` integration.
Inspect `deploy/domain-drop-control.service` but change it only if the logging test
proves the application filter cannot cover its launcher; do not change its User.

- [ ] Capture logs in tests with synthetic bot token, OAuth code/state, initData,
  URI and Authorization header. Verify none appear after failed auth/provider calls.
  Include a database/flush failure with a synthetic PKCE verifier in SQLAlchemy
  exception parameters. A portal-scoped unexpected-error boundary must emit a
  generic no-store failure and a static diagnostic (optionally exception class),
  without bubbling raw exception text/traceback into Uvicorn logs. Keep errors
  observable; preserve normal non-portal error handling. Do not log request bodies,
  cookies or database parameter values when diagnosing authentication failures.
  Cover failures after response headers have already been sent as well (for example
  dependency teardown): do not send a second response, but do not re-raise the raw
  exception/chain into Uvicorn either. Task 8's basic boundary currently re-raises
  this late case; it must be made secret-safe here before deployment.
- [ ] Suppress HTTPX/HTTPCORE INFO request URLs in the application (bot URLs embed
  the existing bot secret). Add a Uvicorn access-log filter stripping query strings
  on portal auth routes and replacing webhook secret path segments with `<redacted>`.
  Do not disable error logging or interpolate exception payloads in safe failures.
- [ ] Add HTTP-context Nginx declarations, to be included only after inspecting the
  actual production configuration and confirming context:

```nginx
limit_req_zone $binary_remote_addr zone=veltrix_auth:10m rate=10r/m;
map $uri $veltrix_safe_uri {
    ~^/api/vpn-telegram/webhook/ /api/vpn-telegram/webhook/redacted;
    default $uri;
}
log_format veltrix_safe '$remote_addr - $request_method $veltrix_safe_uri $status $body_bytes_sent';
```

- [ ] New server location for `/api/vpn-portal/auth/` sets `limit_req zone=veltrix_auth
  burst=10 nodelay`, `limit_req_status 429`, `client_max_body_size 16k`, safe log format,
  proxy to existing 127.0.0.1:8000 and trusted forwarded headers. Use `$uri`, never
  `$request`/`$request_uri`, in the callback access log. Add safe logging to webhook
  route without changing its secret or dropping Telegram pending updates.
- [ ] Add/verify no-store headers for portal APIs and no-referrer for cabinet, keep
  strict same-origin credentials. Do not add global X-Frame-Options DENY that breaks
  Telegram Web Mini App; check native iOS and web embedding before enabling.
- [ ] Test Nginx config and rate-limit/cookie/error headers in a local/rehearsal
  environment. Never copy the repository's www-data service User over production:
  production presently runs root with private root-owned .env, and changing that is
  an independent operational migration.
- [ ] Run logging tests and full suites; commit as `fix: protect VPN portal authentication secrets and endpoints`.

## Task 13: Verification, closed pilot and reversible deployment

**Files:** Create `docs/vpn-customer-portal-runbook.md`; update
`docs/current-state.md` only with observed results.

- [ ] Run full backend tests/Ruff, frontend tests/build. Record exact commands,
  counts and revision. Run a reviewer pass focused on auth isolation, replay,
  display-name identity preservation and uncaught credential logging.
- [ ] Rehearse additive migration from a pre-feature PostgreSQL snapshot, then rerun
  migration/backfill twice. Assert no UUID/public_name/URI/traffic changes, stable
  display names and usable previous application with extra columns left in place.
- [ ] Explicit browser checks: API A/B ownership, successful browser callback and
  denial/cancel, Mini App payload replay/permutation, stale session, admin/customer
  cookie coexistence, refresh at `/cabinet/`, narrow viewport and copy fallback.
- [ ] Prepare a runbook with these exact BotFather values (public values only):

```text
Bot: @veltrix_vpn_official_bot
Website origin: https://veltrix.qzz.io
OIDC callback: https://veltrix.qzz.io/api/vpn-portal/auth/telegram/callback
Mini App URL: https://veltrix.qzz.io/cabinet/
VPN_PORTAL_PUBLIC_ORIGIN=https://veltrix.qzz.io
VPN_PORTAL_PUBLIC_ACCESS=false
```

  User obtains Client ID/Secret in BotFather and enters them directly in the private
  server environment. Never ask to paste them in chat. Confirm the pilot's Telegram
  IDs explicitly; do not bind `test1` to an arbitrary bot visitor. Missing credentials
  block live browser login, not unit tests or build.
- [ ] Before production changes inspect current revision, dirty generated files,
  active domain jobs, node maintenance, actual Nginx include layout and service unit.
  Make and validate a new full DB/config backup. Do not reuse an old partial dump.
- [ ] Deploy feature disabled, migrate, build, control-only restart and verify
  local/public health and existing admin login. No VPN-node restart, transport change,
  credential regeneration, payment settings or unrelated generated-file overwrite.
- [ ] Enable only confirmed pilot IDs, perform two-user ownership checks plus
  browser and iPhone Mini App login. Verify original VPN link still transports
  certificate-valid HTTPS and DNS, and renamed export has unchanged credential/host.
- [ ] If live identity/config requires user action, report the exact outstanding
  step and keep public access false. Do not mark the whole feature production-verified
  based only on synthetic auth or mock screenshots.
- [ ] Rollback rehearsal: disable portal and bot button, revoke customer sessions,
  return previous application build while retaining compatible additive schema.
  Never restore an old whole DB over new customer changes.
- [ ] Commit verified runbook/status; use finishing-a-development-branch to choose
  integration after review. No automatic broad cleanup of main's user files.

## Acceptance-to-task coverage

| Approved requirement | Tasks and evidence |
|---|---|
| Browser and Mini App, shared customer | 4–6, 8, 10–11; verified ID and concurrent lookup tests |
| Separate customer/admin sessions | 2, 5, 8; real authorization-boundary API tests |
| Own subscriptions/profiles only | 7–8; two independently authenticated clients |
| Rename without changing VPN identity | 1–3, 7, 11; protocol round trip and captured remote payloads |
| No fake usage/commercial plan data | 7, 9–10; explicit empty/unavailable state tests |
| Compatible bot/admin experience | 11; old commands and existing workspace regression suite |
| No session/URI secrets in browser URLs/storage; redacted auth logs | 5–6, 8–10, 12; callback-query redaction and browser storage checks |
| Replay, CSRF, provider failure, concurrency | 4–6, 8, 12–13; negative and PostgreSQL tests |
| Additive migration, closed pilot, rollback | 2–3, 13; snapshot rehearsal and live evidence |
| Existing VPN remains working, no payments | 11–13; E2E tunnel probe and scoped deployment diff |

## Primary references for implementation

- [Telegram Login](https://core.telegram.org/bots/telegram-login): registered callbacks,
  PKCE flow and signed identity token. Do not substitute OIDC sub for Telegram id.
- [Mini App validation](https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app):
  validate the original initData on the server, not initDataUnsafe.
- [PyJWT usage](https://pyjwt.readthedocs.io/en/stable/usage.html): explicit signature,
  algorithm and required-claim verification. Library versions should be resolved and
  recorded during the dependency installation task, not assumed from this document.

## Handoff state

User selected delegated task-by-task implementation with controller review in this
same task on 2026-09-20. Use a fresh implementer per task, specification review and
then quality review before the next task. The prepared worktree has passing baseline
tests. The plan's 26 Python snippets were parsed; 14 pure display examples and the
trimmed-name DTO example were exercised in memory. These are plan checks, not feature
verification. Implementation progress is recorded below; production remains unchanged.

Browser QA environment probe succeeded on 2026-09-21: bundled Playwright 1.62.1
launched the already-installed Chromium headless 148.0.7778.96 with an explicit
executable path and rendered a synthetic 320px page, then closed cleanly. This proves
the test runtime is available, not that the not-yet-built cabinet has passed QA.

## Execution ledger

| Task | State | Evidence |
|---|---|---|
| 1 | Complete | 48 focused tests pass, no skips; Ruff clean; both reviews approved; 6 in-memory compatibility checks passed on production Python 3.11.0rc1 without deployment |
| 2 | Complete | 10 schema tests; 77 related/schema/display tests pass, Ruff clean; both reviews approved. Synthetic pre-feature PostgreSQL schema passed two real migrations/backfills and old-ORM compatibility; production-snapshot rehearsal remains in Task 13 |
| 3 | Complete | 26 profile-name tests; parent ran 145 related tests; full Ruff clean; both reviews approved. Real PG blocking PID observed for two customer-name transactions, yielding ordinals 2 then 3 |
| 4 | Complete | 70 new/existing Telegram tests and full backend 471 tests pass, including synchronized real PG uniqueness race preserving outer writes; full Ruff clean; both reviews approved |
| 5 | Complete | Both reviews approved; parent full backend 519 passed in 150.71s, full Ruff clean; reviewer 116 focused tests with real PG contention/rebind. Reuse locks/refetches customer then session; error-side commit leaves no orphan changes; cleanup transaction timeout cannot stall VPN maintenance |
| 6 | Complete | 56 OIDC tests; parent combined 111 OIDC/Telegram tests including PG passed without skips, Ruff clean, both reviews approved; streamed caps, fixed endpoints, real signed synthetic JWTs, mixed JWKS/cache/rotation tests |
| 7 | Complete | 19 view tests; parent related 58 passed and final full backend 594 passed in 135.27s with real PG/no skips; full Ruff clean; both reviews approved. SQLite behavior + compiled PG locking SQL, not a PG runtime rename-lock proof |
| 8 | Complete | Both reviews approved. Parent full backend 611 passed in 140.61s with real PG/no skips; final API 17 passed in 7.70s after test-only review amendments; full Ruff clean. Scoped CORS/no-store/pre-parse guards, real admin/customer isolation and shared OIDC lifespan; late-response exception sanitization remains Task 12 |
| 9 | Complete | Both reviews approved; parent 22 frontend tests and TypeScript/Vite build passed; reviewer 10 focused tests. Independent DTOs/client, static HTTP/network/parser errors and total Russian state/date helpers |
| 10–13 | Pending | No application changes for these tasks yet |

Verification repair: the old partial-cycle durability test raced a 0.1-second
effective timeout against initial SQLite work (configured 0.05 is clamped). A
controlled 0.15-second checkpoint delay reproduced the failure. The test now gates
cycle cancellation on a real committed first key and the second operation, verifies
durability from another session, rollback, cancellation and exact queue resumption.
Production code/timeouts were unchanged; real wall-clock timeout enforcement remains
in its companion test. Five repeated target runs, all 15 lifecycle tests, an independent
test review and the parent's full 594-test run passed.

Nonblocking Task 5 review note: unusual configured IPv6 origins are not normalized
to browser-compressed form, and scoped IPv6 addresses are accepted by the parser.
The intended `https://veltrix.qzz.io` DNS-origin deployment is unaffected; do not
claim arbitrary IPv6-origin support without fixing and testing that edge.

PostgreSQL rehearsal used only a separately initialized disposable test cluster,
with explicit user approval and synthetic data. No production DB/app/VPN settings
were changed. The current tunnel must be stopped and its exact test-cluster directory
removed when verification finishes; parent owns cleanup.
