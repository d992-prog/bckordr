# Veltrix Unlimited Invitations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the ten-person invitation cohort with individually revocable single-use invitations without a total issuance cap.

**Architecture:** Preserve the token format, hash domain, existing slot IDs, redemption locks, readiness and capacity checks. Change slot allocation to database-generated integer IDs, list actual rows with cursor pagination, and update the existing admin surface without creating a second invitation system. This slice can precede logical-device migration because it leaves the existing redemption/key-creation contract intact.

**Tech Stack:** Existing SQLAlchemy/PostgreSQL, SQLite tests, React/TypeScript and Node test runner; no new packages.

**Implementation checkpoint:** Backend migration/service and invitation interface completed with specification and quality approval. PostgreSQL migration/redemption concurrency and mounted browser flows passed. Single-use tokens and seven-day activation/access periods remain unchanged; production is not updated.

## Scope and files

- `backend/app/db/models.py`: positive integer autoincrement invitation primary key, existing redemption/uniqueness constraints retained
- `backend/app/db/migrations.py`: append named upgrade statements after existing invitation creation; preserve old migrations
- `backend/app/services/vpn_friend_invitations.py`: default-ID insertion and bounded real-row list
- `backend/app/api/routes/control.py`: optional validated cursor/limit on existing invitation list
- `backend/tests/test_vpn_friend_invitations.py`, `test_vpn_friend_invitations_postgres.py`, `test_vpn_friend_invitation_schema.py`, `test_vpn_friend_invitation_api.py`: remove obsolete ten-slot expectations, test unlimited allocation and migration preservation
- `frontend/src/api.ts`: optional list cursor without changing default URL
- `frontend/src/VpnCustomerWorkspacePanel.tsx`: unrestricted creation, current labels, older-row loading and locally retained successful issue response
- `frontend/test/vpnFriendInvitationApi.test.mjs`, `vpnFriendInvitationWorkspace.test.mjs`: API cursor and no-cohort UI contracts

### Task 1: Unlimited, still single-use invitations

- [ ] Write failing backend regression tests before changing implementation

```python
issued = []
for _ in range(11):
    invitation = await issue_friend_invitation(db, settings, actor_id, now)
    issued.append(invitation)
assert len({item.view.slot for item in issued}) == 11
assert max(item.view.slot for item in issued) > 10
```

Replace the PostgreSQL eleven-racers/ten-winners test with eleven successful unique IDs under real concurrent transactions. Keep the one-token/two-identities race requiring exactly one winner. Keep expiry, disabled customer, mismatched binding, retry and rotation safety tests.

Add migration tests that create the old SMALLINT/check-10 table with a redeemed row and token digest, apply the upgrade twice, preserve row fields byte-for-byte, then insert IDs above 10 and 32,767 through the default allocator. Reject nonpositive IDs and duplicate token digests. Fresh metadata then full migrations must work too; replace positional `MIGRATIONS[-1:]` assumptions with named invitation migration groups where necessary.

- [ ] Run the new tests and observe failures at the existing cap

```powershell
python -m pytest tests/test_vpn_friend_invitations.py tests/test_vpn_friend_invitation_schema.py tests/test_vpn_friend_invitation_api.py -q
```

Expected: the eleventh issuance and positive-ID/schema tests fail before implementation. PostgreSQL tests require `VPN_PORTAL_TEST_PG_URL` pointing only to allowlisted `127.0.0.1:<non-5432>/veltrix_portal_test`.

- [ ] Implement positive auto-generated IDs and a preserving upgrade

```python
slot: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
# Keep the existing constraint name; change its expression to slot > 0.
```

Upgrade the existing column to INTEGER, replace the old CHECK with `slot > 0`, create/own `vpn_friend_invitations_slot_seq` only if absent, and set the default to `nextval`. Under a table lock, advance the sequence to at least MAX(slot) without moving a previously advanced sequence backward. An empty fresh sequence must still issue a valid positive ID. Every statement is idempotent and schema-local. Do not recreate redeemed rows, hashes or keys.

- [ ] Replace the scan of ten occupied slots with one INSERT using the default ID

```python
while True:
    token, digest = _new_token()
    try:
        async with db.begin_nested():
            invitation = VpnFriendInvitation(
                token_digest=digest,
                created_by_user_id=actor_user_id,
                created_at=current,
                redeem_expires_at=current + INVITE_LIFETIME,
            )
            db.add(invitation)
            await db.flush()
        return _issued(invitation, username, token, current)
    except IntegrityError:
        if await db.scalar(select(VpnFriendInvitation.slot).where(
            VpnFriendInvitation.token_digest == digest
        )) is None:
            raise
```

Preserve readiness checks before insertion. Retry only a confirmed digest collision; never swallow another integrity failure. Remove the obsolete cohort-full branch. Existing security flag/environment names may remain internally for compatibility; do not bypass node readiness or weaken redemption locks.

- [ ] Add bounded cursor listing and remove fake empty slots

Extend the service with keyword-only `before_slot: int | None = None, limit: int = 100`; query actual rows ordered by `slot DESC`, apply `slot < before_slot` when supplied and `LIMIT limit`. API validates a positive cursor and `1 <= limit <= 100`. Default `GET /vpn/friend-invitations` URL and response array shape stay unchanged. Return an empty array for no invitations, not ten synthetic rows.

- [ ] Write failing frontend regressions, then update the real admin flow

```js
await api.getVpnFriendInvitations({ beforeSlot: 101 });
assert.equal(call.url, "/api/control/vpn/friend-invitations?before_slot=101");
```

Keep the no-argument API call unchanged. The heading is «Приглашения» without `/10`, creation works for empty or eleven-plus rows, and copy says «Приглашение»/«приглашения», not test slots. Display the unchanged 7-day activation and 7-day access terms. Do not remove unrelated public trial features or change existing subscriptions.

Load older pages using the lowest displayed ID as the cursor. Merge by ID, prevent double page requests and stale responses after reset/session change, retain retryable load errors without hiding existing rows, and stop after a short page. Reloading the parent list can reset pagination; do not pretend the current page length is a global count.

Store raw links only inside the workspace as before. Merge the newly issued safe view into visible rows immediately, so a successful issue followed by failed reload still leaves its link available to copy. An authoritative redeemed/revoked state removes its old raw link; rotation forgets the previous link before mutation. Do not leak links to App state, storage, logs or notifications. Add a synchronous mutation guard for creation because unlimited issuance can otherwise create accidental duplicates.

- [ ] Verify and review before integration

```powershell
python -m pytest tests/test_vpn_friend_invitations.py tests/test_vpn_friend_invitation_api.py tests/test_vpn_friend_invitation_schema.py tests/test_vpn_telegram.py tests/test_vpn_portal_auth.py -q
npm test
npm run build
```

Then run the updated invitation PostgreSQL concurrency and schema tests on the authorized isolated test database. Root handles server-side execution; never point tests at the live database. Review spec compliance, then code quality, especially sequence restart safety, one-use redemption, cursor behavior and secret-link cleanup. Commit only this slice's source/tests after verification; production rollout remains a separate verified step.
