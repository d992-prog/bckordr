# Persistent cabinet entry implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Admit verified Telegram users to their own cabinet independently of VPN entitlements and keep the bot entry discoverable.

**Architecture:** Reuse shared portal admission and the existing signed inline Mini App launch. Add one opt-in registration flag without changing legacy public-access provisioning interlocks. Keep all session, identity, subscription and ownership checks.

**Tech Stack:** Python, FastAPI, Pydantic settings, SQLAlchemy, pytest, Telegram Bot API.

---

## Task 1: Cabinet registration and persistent command (one coupled change)

**Modify:** `backend/app/core/config.py`, `backend/app/services/vpn_portal_auth.py`, `backend/app/services/vpn_portal_http.py`, `backend/app/services/vpn_telegram.py`, `backend/.env.example`.

**Tests:** `backend/tests/test_vpn_telegram.py`, `backend/tests/test_vpn_portal_auth.py`, `backend/tests/test_vpn_portal_api.py`, `backend/tests/test_vpn_portal_schema.py`.

- [ ] Add failing settings tests for the independent flag, default false and explicit alias true.

```python
assert Settings(_env_file=None).vpn_portal_open_registration is False
assert Settings(_env_file=None, VPN_PORTAL_OPEN_REGISTRATION=True).vpn_portal_open_registration is True
```

- [ ] Add failing public-registration tests: fresh signed Mini App and OIDC identities with no whitelist/invite can log in; expired subscriptions and manually added accounts do not prevent login. A fresh identity sees empty own subscription/profile lists, cannot read another customer's profile, cannot create a device under another subscription, cannot activate a disabled trial; no subscription/key/operation is created. Tampered/old initData and blocked/archived customers fail. Leave closed-mode tests intact.
- [ ] Add failing keyboard tests: configured private chat includes plain text `Личный кабинет` and `is_persistent=True`, no reply `web_app`; `/cabinet` and text alias return an inline launcher from real update processing. Test after `/status`, `/keys`, `/support`, new and expired customers, unavailable portal and group/malformed chat behavior. Command text is retained in sanitized update metadata.
- [ ] Run new tests before implementation; confirm expected assertion failures rather than collection/import errors.

```powershell
$env:PYTHONPATH='backend'
& '..\veltrix-backup-nginx-symlinks\backend\.venv\Scripts\python.exe' -m pytest -q backend/tests/test_vpn_telegram.py backend/tests/test_vpn_portal_auth.py backend/tests/test_vpn_portal_api.py backend/tests/test_vpn_portal_schema.py
```

- [ ] Add the setting and use it in shared admission/capability checks, preserving their existing guards.

```python
vpn_portal_open_registration: bool = Field(default=False, alias="VPN_PORTAL_OPEN_REGISTRATION")
# In identity_allowed, after enabled/valid Telegram ID checks:
if settings.vpn_portal_open_registration or settings.vpn_public_trial_enabled or settings.vpn_portal_public_access:
    return True
# Include settings.vpn_portal_open_registration in portal_capabilities access_configured.
```

- [ ] Extend the existing bot keyboard/commands, not the transport. Use `telegram_user_id` validation for positive private chat ID; only show the cabinet command when Mini App capabilities are available. Keep the current inline launcher gated by shared admission. Both command aliases map to `cabinet`; add both to `_PERSISTED_COMMANDS`. Render an explicit instruction to open the inline cabinet button; when unavailable, say it is temporarily unavailable. No new generic wrappers or dependencies.
- [ ] Add `VPN_PORTAL_OPEN_REGISTRATION=false` to example settings and document that production may opt in after approval, without enabling trial, legacy public access or provisioning. Keep historical dated beta documents unchanged.
- [ ] Run the four test modules and related friend invitation, dispatcher, customer-device and trial tests; Ruff touched Python files, comparing baseline errors if needed. Fix regressions, commit only task files.

## Task 2: Review and safe release

- [ ] Spec reviewer reads the actual diff against the approved spec; fix gaps before quality review.
- [ ] Independent quality/security review covers signature, replay, blocked identity, ownership, entitlement and old provisioning interlocks.
- [ ] Run repository release gate on exact PR source; no dependency installs or unrelated changes.
- [ ] Back up changed production source and `.env` root-private on controller, verify expected deployed revision and unchanged protected files. Deploy only reviewed source after the gate passes; set only `VPN_PORTAL_OPEN_REGISTRATION=true`, preserve all other settings. Restart controller service only; do not restart VPN nodes or Xray.
- [ ] Verify service, public capabilities, anonymous authentication boundary, aggregate admission and unchanged profile identifiers. No synthetic production user records and no unsolicited bot messages. Existing chats receive updated keyboard on their next command.
- [ ] Save the current default bot menu (`commands`) in the private release backup, then set the global `MenuButtonWebApp` to the existing HTTPS cabinet URL with text `Личный кабинет`. Verify with `getChatMenuButton`; do not send messages or change bot tokens/webhook/commands. This provides an entry even before an old chat refreshes its reply keyboard. Telegram documents this launch as equivalent to the signed inline launch: https://core.telegram.org/bots/webapps#launching-mini-apps-from-the-menu-button.

## Parallel incident: Polish node

- [ ] Finish read-only disk/CPU/network comparison. Report VM/storage evidence separately from actual client-tunnel throughput.
- [ ] Do not hide/delete the node, reboot it, kill blocked processes, change kernel settings or promise a software speed fix without evidence and appropriate authority. If storage is stalled, prepare a concise provider incident report and recommend a healthy existing country while the provider resolves it.
