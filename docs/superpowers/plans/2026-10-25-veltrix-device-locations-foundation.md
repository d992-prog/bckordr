# Veltrix Device Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce logical devices without changing existing key IDs, UUIDs, expiry, endpoint bindings or direct connection links, and make slot accounting and manual revocation correct before adding country selection.

**Architecture:** `VpnDevice` owns a subscription and one or more `VpnAccessKey` credentials. Existing customer → subscriptions → access keys → worker → endpoint locking remains authoritative; the customer lock also serializes device writes. Device operational state and reservation derive from child keys, while a separate sticky device revoke timestamp prevents restoration after manual revoke.

**Tech Stack:** Existing FastAPI, SQLAlchemy async, handwritten PostgreSQL migrations, SQLite unit tests and isolated PostgreSQL concurrency tests. No new dependency.

**Implementation checkpoint:** Foundation completed and independently reviewed for specification and quality. Logical-device backfill, reservation counting, renewal/revocation and PostgreSQL races passed. Existing key IDs, credentials and direct links are preserved. Production migration has not run.

---

This is the next independently releasable backend slice of approved spec §§1, 3. Its task commits are review checkpoints within one tightly coupled slice: deploy the completed foundation together, never a model-only or migration-only intermediate commit. It does not expose country selection or subscription tokens. Follow `2026-10-25-veltrix-device-locations-integration.md` after this slice. Work only in `.worktrees/veltrix-product-operations`; preserve the uncommitted spec and other plans. Do not deploy, connect to production, change payment or send Telegram messages.

## File map and invariants

| File | Responsibility |
| --- | --- |
| `backend/app/db/models.py` | Device row and nullable additive child association; same-subscription and per-endpoint uniqueness |
| `backend/app/db/vpn_device_migrations.py` (new) | Restart-safe legacy backfill and constraints |
| `backend/app/db/migrations.py` | Append device migration tuple after existing migrations |
| `backend/app/services/vpn_devices.py` (new) | Attach initial credentials; propagate device name; stage sticky all-child revoke |
| `backend/app/services/vpn_policy.py` | Count distinct device reservations with legacy fallback; distinguish new issuance from resync |
| `backend/app/services/vpn_subscription_sync.py` | Permit lower limits without revoking existing devices |
| `backend/app/services/vpn_lifecycle.py` | Existing-key resync ignores new-device limit |
| `backend/app/api/routes/control.py` | Initial attachment, existing-key resync, rename/revoke aliases, authoritative usage API |
| `backend/app/services/vpn_public_trial.py`, `vpn_friend_invitations.py` | Attach the initial trial/invitation credential in the issuing transaction |
| `backend/app/services/vpn_customer_view.py`, `vpn_profile_names.py`, `vpn_customer_lifecycle.py` | Compatible old profile routes, names and archive revocation |
| `backend/app/schemas/control.py` | Additive usage response; optional `device_id` on key response |
| `backend/tests/test_vpn_devices.py` (new), `test_vpn_device_migrations_postgres.py` (new) | Model, fallback accounting, all-child behavior, migration safety |

Do not add a device status column that mirrors child status. Reserve a slot if **any** child is in `DEVICE_SLOT_STATUSES`, including failed, suspended and uncertain operations represented by pending key states. Release only when all children have confirmed `revoked`; never commit a newly issued device without its initial child. Existing unlinked rows in SQLite fixtures count individually, so hundreds of unrelated fixtures need no weakening or wholesale rewrite.

`legacy_profile_id` is the initial key ID and remains the alias used by `/vpn-portal/profiles/{key_id}`. Migrated device IDs equal their initial key IDs; newly created device IDs use their own sequence. No code assumes those sequences stay equal.

## Atomic implementation task: logical-device backend foundation

The checkpoints below are tightly coupled and form one release/review unit. Intermediate commits are recovery points, not deployable releases. Review the complete foundation for spec compliance and code quality before country-selection integration.

### Checkpoint 1: Add the model and prove real constraints

- [ ] **Step 1: Add failing schema assertions** in `backend/tests/test_vpn_devices.py`:

```python
from app.db.models import VpnAccessKey, VpnDevice


def test_device_model_has_separate_identity_and_credential_binding():
    table = VpnDevice.__table__
    assert {"subscription_id", "legacy_profile_id", "display_name", "preferred_key_id",
            "pending_key_id", "token_generation", "revoke_requested_at"} <= set(table.c.keys())
    assert VpnAccessKey.__table__.c.device_id.nullable
    assert any(c.name == "uq_vpn_access_key_device_endpoint"
               for c in VpnAccessKey.__table__.constraints)
    assert any(c.name == "fk_vpn_access_key_device_subscription"
               for c in VpnAccessKey.__table__.constraints)
```

- [ ] **Step 2: Run RED** from `backend`:

```powershell
& '..\..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest tests/test_vpn_devices.py -q
```

Expected: import failure for `VpnDevice`.

- [ ] **Step 3: Add this model before `VpnAccessKey`** in `backend/app/db/models.py`; existing imports already include SQLAlchemy field/constraint types:

```python
class VpnDevice(Base):
    __tablename__ = "vpn_devices"
    __table_args__ = (
        UniqueConstraint("id", "subscription_id", name="uq_vpn_device_id_subscription"),
        UniqueConstraint("legacy_profile_id", name="uq_vpn_device_legacy_profile"),
        CheckConstraint("token_generation >= 0", name="ck_vpn_device_token_generation"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("vpn_subscriptions.id", ondelete="CASCADE"), index=True)
    legacy_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_access_keys.id", use_alter=True,
                   name="fk_vpn_device_legacy_profile", ondelete="RESTRICT"), nullable=True)
    display_name: Mapped[str] = mapped_column(String(64), nullable=False)
    preferred_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_access_keys.id", use_alter=True,
                   name="fk_vpn_device_preferred_key", ondelete="RESTRICT"), nullable=True)
    pending_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("vpn_access_keys.id", use_alter=True,
                   name="fk_vpn_device_pending_key", ondelete="RESTRICT"), nullable=True)
    token_generation: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revoke_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, server_default=func.now())
```

Add `device_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)` to `VpnAccessKey`. Add these two entries to its existing `__table_args__` without removing endpoint constraints:

```python
ForeignKeyConstraint(
    ["device_id", "subscription_id"], ["vpn_devices.id", "vpn_devices.subscription_id"],
    name="fk_vpn_access_key_device_subscription", ondelete="RESTRICT"),
UniqueConstraint("device_id", "endpoint_id", name="uq_vpn_access_key_device_endpoint"),
```

No new ORM relationships are needed; explicit service queries avoid ambiguous circular joins. Preferred/pending references use `use_alter=True` because startup creates metadata **before** migrations and the legacy key table lacks `device_id` at that moment. Read services must additionally check preferred/pending keys have the same device and subscription; the single-key FKs do not prove that ownership.

- [ ] **Step 4: Run GREEN** with the Step 2 command; also run `tests/test_vpn_models.py tests/test_vpn_portal_schema.py`.
- [ ] **Step 5: Commit only model and new tests**: `git add backend/app/db/models.py backend/tests/test_vpn_devices.py`; `git commit -m "feat(vpn): introduce logical device model"`.

### Checkpoint 2: Migrate legacy keys without credential changes

- [ ] **Step 1: Add `backend/tests/test_vpn_device_migrations_postgres.py`**, reusing `postgres_schema` from `test_vpn_endpoint_migrations`. Build the pre-device shape using the existing migration suite, insert keys `11` active, `12` suspended, `13` revoked in one subscription and `21` in another. Snapshot `id, subscription_id, worker_id, endpoint_id, external_uuid, config_uri, expires_at, status, revoked_at` before migration. Apply the new migration twice and assert this exact comparison:

```python
assert after_credentials == before_credentials
assert [(row.id, row.legacy_profile_id) for row in devices] == [(11, 11), (12, 12), (13, 13), (21, 21)]
assert [(row.id, row.device_id) for row in keys] == [(11, 11), (12, 12), (13, 13), (21, 21)]
assert devices[2].revoke_requested_at is not None
assert next_inserted_device_id > 21
```

Also run `Base.metadata.create_all` against the pre-device database **before** migrations, matching `app/main.py`, then run both migrations twice. Test same-subscription FK and duplicate `(device_id, endpoint_id)` with real inserts that raise `IntegrityError`; nullable legacy endpoint bindings remain permitted. A second independent unlinked old key always becomes its own device.

- [ ] **Step 2: Run RED**: the PostgreSQL command below must execute, not skip. The root runner supplies `VPN_PORTAL_TEST_PG_URL` for the approved disposable cluster; never substitute application `DB_URL`.

```powershell
& '..\..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest tests/test_vpn_device_migrations_postgres.py -q -rs
```

- [ ] **Step 3: Create `backend/app/db/vpn_device_migrations.py`** with `VPN_DEVICE_MIGRATIONS: tuple[str, ...]`, appended to `MIGRATIONS` in `backend/app/db/migrations.py`. Use the exact model's table definition in `CREATE TABLE IF NOT EXISTS vpn_devices`; create all three named key FKs and the named unique/check constraints also on the handwritten-create path. Add child column before backfill:

```sql
ALTER TABLE vpn_access_keys ADD COLUMN IF NOT EXISTS device_id INTEGER NULL;
```

Backfill with these statements, in this order:

```sql
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM vpn_access_keys k JOIN vpn_devices d ON d.id = k.id
    WHERE k.device_id IS NULL AND d.legacy_profile_id IS DISTINCT FROM k.id
  ) THEN RAISE EXCEPTION 'vpn_device_backfill_identity_conflict'; END IF;
END $$;

INSERT INTO vpn_devices
  (id, subscription_id, legacy_profile_id, display_name, preferred_key_id,
   pending_key_id, token_generation, revoke_requested_at, created_at, updated_at)
SELECT id, subscription_id, id, COALESCE(NULLIF(display_name, ''), 'Профиль ' || id),
       id, NULL, 0,
       CASE WHEN status IN ('pending_revoke', 'revoked') THEN COALESCE(revoke_requested_at, revoked_at, created_at, NOW()) END,
       COALESCE(created_at, NOW()), COALESCE(updated_at, NOW())
FROM vpn_access_keys WHERE device_id IS NULL
ON CONFLICT (legacy_profile_id) DO NOTHING;

UPDATE vpn_access_keys k SET device_id = d.id
FROM vpn_devices d WHERE k.device_id IS NULL AND d.legacy_profile_id = k.id
AND d.subscription_id = k.subscription_id;

DO $$ DECLARE seq_name TEXT; seq_value BIGINT; row_max BIGINT; BEGIN
  seq_name := pg_get_serial_sequence('vpn_devices', 'id');
  EXECUTE format('SELECT last_value FROM %s', seq_name) INTO seq_value;
  SELECT COALESCE(MAX(id), 1) INTO row_max FROM vpn_devices;
  PERFORM setval(seq_name::regclass, GREATEST(seq_value, row_max), true);
END $$;
```

For each new named constraint, use the existing migration pattern `IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'name' AND conrelid = 'table_name'::regclass) THEN ALTER TABLE ... ADD CONSTRAINT ...; END IF`. Add indexes with `CREATE INDEX IF NOT EXISTS`. Never update any credential field in this migration. A collision fails closed; do not guess a merge or delete the conflicting device.

- [ ] **Step 4: Run GREEN**, including `tests/test_vpn_endpoint_migrations.py`, confirming all migration cases execute against isolated PostgreSQL. Missing cluster access is a release gate, not a reason to weaken test URL validation.
- [ ] **Step 5: Commit** the migration module, registration and migration tests with `feat(vpn): preserve legacy credentials in device migration`.

### Checkpoint 3: Count one slot per device and separate resync from new issuance

- [ ] **Step 1: Add this parametrized regression** to `backend/tests/test_vpn_devices.py`, using that file's async SQLite session fixture and seed rows. Include two subscriptions, two devices and an unlinked legacy key; add two children to the first device on different endpoints. Expected cases are:

```python
import pytest


@pytest.mark.parametrize("statuses, expected", [
    (("active", "pending_sync"), 1), (("revoked", "suspended"), 1),
    (("revoked", "pending_revoke"), 1), (("failed", "revoked"), 1),
    (("revoked", "revoked"), 0),
])
async def test_multiple_country_credentials_reserve_one_slot(device_session, statuses, expected):
    db, subscription, children = device_session
    for child, status in zip(children, statuses, strict=True):
        child.status = status
    await db.flush()
    assert await count_device_slots(db, subscription.id) == expected
```

Add an unlinked active key to the same subscription and assert `expected + 1`; a foreign subscription's child must not change either result. Seed through real `VpnDevice`/`VpnAccessKey` rows rather than disabling foreign keys.

- [ ] **Step 2: Run RED** using `-m pytest tests/test_vpn_devices.py -q`.
- [ ] **Step 3: Replace the count query** in `vpn_policy.count_device_slots`; retain the existing optional `exclude_key_id` compatibility argument:

```python
legacy_id = case((VpnAccessKey.device_id.is_(None), VpnAccessKey.id), else_=None)
query = select(VpnAccessKey.device_id, legacy_id.label("legacy_id")).where(
    VpnAccessKey.subscription_id == subscription_id,
    VpnAccessKey.status.in_(DEVICE_SLOT_STATUSES),
)
if exclude_key_id is not None:
    query = query.where(VpnAccessKey.id != exclude_key_id)
reserved = query.group_by(VpnAccessKey.device_id, legacy_id).subquery()
return int(await session.scalar(select(func.count()).select_from(reserved)) or 0)
```

Import `case` from SQLAlchemy. Add keyword argument `enforce_device_limit: bool = True` to `validate_subscription_access`, changing only its last check:

```python
if enforce_device_limit and device_slots >= subscription.max_devices:
    return "VPN device limit reached"
```

In `control._validate_vpn_key_issue`, pass `enforce_device_limit=exclude_key_id is None`; in `vpn_lifecycle._process_provision`, pass `enforce_device_limit=False`. Keep customer/status/date validation in both. Endpoint controller resync already uses subscription policy rather than the device-count check; keep that behavior.

Delete only the two-line `max_devices < subscription.max_devices ...` rejection in `stage_subscription_update`. Limit-only edits must not enqueue credential reconfiguration: compute `remote_policy_changed` from changed `POLICY_FIELDS - {"max_devices"}` and return after saving the subscription if false. Existing lifetime, traffic and access-state edits continue staging all children. Do not map `max_devices` into any per-key IP cap.

- [ ] **Step 4: Add and run GREEN regressions** in `test_vpn_subscription_sync.py` and `test_vpn_lifecycle.py`: two existing devices, lower `max_devices` to one, assert both credentials unchanged for the limit-only edit; resync/renewal still works; attempting a third device raises `VPN device limit reached`. `disabled`, expired and foreign-customer cases remain denied even with the resync flag.
- [ ] **Step 5: Commit** affected policy/services/tests with `fix(vpn): count logical devices and preserve over-limit renewals`.

### Checkpoint 4: Attach every issued key and maintain legacy names

- [ ] **Step 1: Add failing issuance assertions** to existing tests in `test_vpn_control_api.py`, `test_vpn_public_trial.py`, `test_vpn_friend_invitations.py`: after successful creation, load the returned key and its `VpnDevice`, assert matching `subscription_id`, `legacy_profile_id == key.id`, `preferred_key_id == key.id`, unchanged UUID/expiry/URI and exactly one device after replay/retry. Failed allocation rolls back both child and device.
- [ ] **Step 2: Run RED** on those three test files.
- [ ] **Step 3: Add `attach_initial_device`** to new `backend/app/services/vpn_devices.py`:

```python
from datetime import datetime
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.base import utcnow
from app.db.models import VpnAccessKey, VpnDevice
from app.services.vpn_customer_view import profile_display_name


async def attach_initial_device(db: AsyncSession, key: VpnAccessKey) -> VpnDevice:
    # The caller holds the customer/subscription locks and has flushed the key.
    if key.device_id is not None:
        device = await db.get(VpnDevice, key.device_id)
        if device is None or device.subscription_id != key.subscription_id:
            raise ValueError("vpn_device_binding_invalid")
        return device
    device = VpnDevice(
        subscription_id=key.subscription_id, legacy_profile_id=key.id,
        preferred_key_id=key.id, display_name=profile_display_name(key),
        token_generation=0,
        revoke_requested_at=(key.revoke_requested_at or key.revoked_at or utcnow())
        if key.status in {"pending_revoke", "revoked"} else None,
    )
    db.add(device)
    await db.flush()
    key.device_id = device.id
    await db.flush()
    return device
```

Avoid an import cycle: move `profile_display_name` import inside this function if `vpn_customer_view` imports the device service for rename. Do not move existing display validation into a new abstraction.

Insert `await attach_initial_device(db, access_key)` immediately after initial key flush in `control.create_vpn_access_key` and `vpn_friend_invitations.redeem_friend_invitation`; insert `await attach_initial_device(db, key)` after initial flush in `vpn_public_trial.activate_public_trial`. All three insertions are **after** endpoint allocation (`lock=True` rejects dirty ORM state) and **before** staging the control operation. No attach call on ordinary existing-key retries.

Add a service function that accepts an already locked device and validated display name, assigns `device.display_name`, and updates **every** same-subscription child in ID order with `key.display_name = name; key.updated_at = now`. Route both existing admin and portal key-rename handlers through their key's device, preserving the old response model and key ID; unlinked legacy fixture rows retain their one-key behavior. Before rename, take customer → all customer subscriptions → all customer keys locks, using the same ordered queries as `stage_vpn_control_operation`, without acquiring worker locks. Keep the raw stored `config_uri` unchanged; existing `display_uri` applies the display fragment on reads.

For `initialize_customer_names`, keep existing cleanup but return the number of logical names, using this set in place of `len(access_keys)`:

```python
identities = {("device", key.device_id) if key.device_id is not None else ("key", key.id)
              for key in access_keys}
return len(identities) + 1
```

- [ ] **Step 4: Run GREEN**: `test_vpn_control_api.py test_vpn_public_trial.py test_vpn_friend_invitations.py test_vpn_profile_names.py test_vpn_customer_view.py test_vpn_profile_names.py`. Add a two-child rename case: one device name, both response fragments change, both stored URIs/UUIDs stay byte-for-byte equal.
- [ ] **Step 5: Commit** these bounded caller changes with `feat(vpn): attach logical devices across issuance paths`.

### Checkpoint 5: Sticky all-child manual revoke and authoritative usage

- [ ] **Step 1: Add failing device-revoke tests** in `test_vpn_devices.py`: active A + suspended B; request manual revoke through initial key alias; assert device latch and generation increment once, both keys pending revoke with `revoke_requested_at`, no URI/UUID changes; replay does not increment again; one confirmed child leaves one reserved slot; both confirmed revoked release it; subscription renewal cannot clear either child or device latch.
- [ ] **Step 2: Run RED** on `test_vpn_devices.py`.
- [ ] **Step 3: Implement local staging** in `vpn_devices.py`, called only while canonical customer/subscription/key locks are held:

```python
async def stage_device_revoke(db: AsyncSession, device: VpnDevice, *, now: datetime) -> None:
    if device.revoke_requested_at is None:
        device.revoke_requested_at = now
        device.token_generation += 1
    device.pending_key_id = None
    device.updated_at = now
    keys = (await db.scalars(select(VpnAccessKey).where(
        VpnAccessKey.device_id == device.id,
        VpnAccessKey.subscription_id == device.subscription_id,
    ).order_by(VpnAccessKey.id))).all()
    for key in keys:
        if key.status == "revoked":
            continue
        key.revoke_requested_at = key.revoke_requested_at or now
        key.status = "pending_revoke"
        key.updated_at = now
    await db.flush()
```

Use this before existing explicit admin revoke/delete actions; acquire canonical customer/subscription/key locks before their existing worker checks, preserving audit entries and legacy response identities. `disable_friend_invitation` already calls `stage_vpn_control_operation` first, which acquires those locks: latch its device and siblings after that call and `_locked_friend_chain`, before the invitation revoke marker is flushed; do not reverse invitation lock ordering. The explicit action may execute the addressed legacy key using its current transport; siblings remain durably queued for lifecycle. Bound endpoint credentials use the existing strict queued controller, never legacy SSH scripts. In `stage_vpn_customer_archive`, after its existing ordered key locks, latch every associated device before returning staged keys. On permanent subscription cancellation, latch associated devices; temporary disabled/expired subscription states do **not** latch devices and remain restorable. If a legacy alias is still unlinked, attach its single device before staging. Keep invitation retry/rotation rejection for revoked keys; retry never clears device revocation.

Add `VpnSubscriptionDeviceUsage` to `schemas/control.py`:

```python
class VpnSubscriptionDeviceUsage(BaseModel):
    subscription_id: int
    devices_used: int
    max_devices: int
```

Add admin-only `GET /vpn/subscriptions/{subscription_id}/device-usage`, 404 on absent subscription, returning `VpnSubscriptionDeviceUsage(subscription_id=subscription.id, devices_used=await count_device_slots(db, subscription.id), max_devices=subscription.max_devices)`. Add nullable `device_id` to `VpnAccessKeyResponse`, whose response builder already enumerates model fields. Do not change the global key-list 1000 cap or report a partial key-list count as authoritative. Update portal `list_customer_subscriptions` to call `count_device_slots` per owned subscription, replacing its old key-count scalar subquery; response JSON fields stay the same.

- [ ] **Step 4: Run GREEN**: `test_vpn_devices.py test_vpn_policy.py test_vpn_control_api.py test_vpn_customer_archive.py test_vpn_friend_invitation_api.py test_vpn_friend_invitations.py test_vpn_subscription_sync.py test_vpn_lifecycle.py test_vpn_customer_view.py`. Add usage regression with 1001 credentials belonging to one device on 1001 endpoints, plus an unlinked fixture key; authoritative usage must be two regardless of list truncation. Small SQLite cases may use fewer children for speed, but the API-cap regression must cross the cap.
- [ ] **Step 5: Commit** with `feat(vpn): revoke all device credentials and expose slot usage`.

### Checkpoint 6: Verify races on isolated PostgreSQL and hand off

- [ ] Extend `backend/tests/test_vpn_lifecycle_postgres.py` / new `test_vpn_devices_postgres.py` using `postgres_schema`: two concurrent initial issuances for one remaining slot → exactly one new device; concurrent revoke/renewal → sticky revoke wins; over-limit renewal → all existing usable devices resync; any failed/uncertain child → slot remains reserved. Use real independent sessions and `asyncio.Event` barriers around the customer/subscription lock, not SQLite to assert row-lock behavior.
- [ ] Run affected SQLite tests above, then PostgreSQL device/migration tests plus `test_vpn_control_intents_postgres.py`, `test_vpn_lifecycle_postgres.py`, `test_vpn_public_trial_postgres.py`, `test_vpn_friend_invitations_postgres.py`. Confirm no relevant PostgreSQL case skipped. Do not print a URL containing credentials.
- [ ] Run `git diff --check`; inspect `git diff --stat` and ensure existing data-changing startup SQL contains no update of key UUID/URI/expiry/binding. Commit only slice files; leave root's spec/plan edits alone.
- [ ] Hand off measured results and any remaining test failure. Next slice may allocate multiple child keys only after these gates pass.

## Review decisions and limits

The foundation intentionally keeps `device_id` nullable for legacy fixtures/rolling transition, and counts unlinked keys conservatively. Production writers all attach devices before commit. A future non-null migration requires a separate backfill audit and is not part of this slice.

Existing explicit revoke/suspend controller policy can restart shared Xray; this plan preserves that already approved behavior. Country selection in the next plan uses provision only and must never introduce a global restart. No new device restoration endpoint is added: reissuing a manually revoked profile creates a fresh logical device and secret.

Not checked by this planning pass: production data shape or restore drill; no production migration is authorized by a passing local test. Production release still requires the approved backup/restore procedure.
