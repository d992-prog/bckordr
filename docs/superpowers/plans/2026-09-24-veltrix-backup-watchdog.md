# Veltrix Backup and Watchdog Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a fully validated daily protected backup and send deduplicated Telegram alert/recovery messages for release-critical failures, independently of the web process.

**Architecture:** Add two small Python modules runnable with the existing backend virtualenv. A root systemd backup timer creates and validates an atomic backup set; a five-minute watchdog timer combines system/service/HTTP checks with the shared readiness evaluator and stores dedupe state in a root-private JSON file. No Prometheus/Grafana or new Python dependency is introduced.

**Tech Stack:** Python standard library, existing SQLAlchemy/httpx backend dependencies, PostgreSQL CLI tools, systemd, Telegram Bot API.

---

### Task 1: Implement atomic validated backup sets

**Files:**
- Create: `backend/app/operations/__init__.py`
- Create: `backend/app/operations/backup.py`
- Create: `backend/tests/test_vpn_operations_backup.py`

- [ ] **Step 1: Write failing filesystem/subprocess tests**

Use temporary directories and a fake command runner. Cover success, failed dump,
failed full restore read, failed file copy, atomic marker update, private modes,
retention and preservation of the previous successful backup.

- [ ] **Step 2: Implement explicit command arrays**

Parse `DB_URL`, pass credentials only through a private subprocess environment,
and use explicit command arrays:

```python
run(["pg_dump", "--format=custom", "--file", dump, "--dbname", database_name], env=pg_env)
run(["pg_restore", "--file", os.devnull, dump], env=pg_env)
```

Never use `shell=True`, put a password in argv, interpolate paths into shell, or
print environment values.

- [ ] **Step 3: Build a `.partial` set and promote atomically**

Copy the configured env, actual unit, nginx directory and frontend `dist`; write
SHA-256 and a safe JSON marker only after every validation passes. Use `0700`
directories and `0600` sensitive files.

- [ ] **Step 4: Add retention**

Delete only successful sets older than the newest seven, only after a new set is
promoted. Leave partial sets visible for diagnosis and never point the success
marker at them.

- [ ] **Step 5: Run tests and Ruff**

```powershell
backend/.venv/Scripts/python -m pytest backend/tests/test_vpn_operations_backup.py -q
backend/.venv/Scripts/python -m ruff check backend/app/operations backend/tests/test_vpn_operations_backup.py
```

- [ ] **Step 6: Commit**

```powershell
git add backend/app/operations backend/tests/test_vpn_operations_backup.py
git commit -m "feat(ops): create validated atomic backups"
```

### Task 2: Implement deduplicated watchdog state

**Files:**
- Create: `backend/app/operations/watchdog.py`
- Create: `backend/tests/test_vpn_operations_watchdog.py`

- [ ] **Step 1: Write failing dedupe tests**

Cover first failure alert, identical repeated failure silence, changed reason
alert, recovery message, corrupt state recovery, Telegram failure retry and
redaction of tokens/UUID/URI/raw exception text.

- [ ] **Step 2: Implement immutable safe alert state**

```python
@dataclass(frozen=True, slots=True)
class AlertState:
    digest: str
    failing_codes: tuple[str, ...]
    notified: bool
```

Write with a same-directory temporary file, `fsync`, `os.replace` and mode
`0600`.

- [ ] **Step 3: Implement bounded Telegram delivery**

Use the existing bot token plus `VPN_ALERT_TELEGRAM_USER_ID`. Send fixed Russian
messages containing check codes and recovery actions only. Set a short timeout,
disable redirects and cap response bytes.

- [ ] **Step 4: Run tests and commit**

```powershell
git add backend/app/operations/watchdog.py backend/tests/test_vpn_operations_watchdog.py
git commit -m "feat(ops): deduplicate release alerts"
```

### Task 3: Collect service, HTTP, disk, backup and database checks

**Files:**
- Modify: `backend/app/operations/watchdog.py`
- Modify: `backend/app/services/vpn_release_readiness.py`
- Modify: `backend/tests/test_vpn_operations_watchdog.py`
- Modify: `backend/tests/test_vpn_release_readiness.py`

- [ ] **Step 1: Write failing collector tests**

Cover inactive control unit, PostgreSQL unavailable, local/public health failure,
cabinet non-200, disk below threshold, missing/stale/failed backup marker and DB
readiness failures.

- [ ] **Step 2: Implement adapters with strict timeouts**

Use explicit `systemctl is-active`, `shutil.disk_usage`, marker JSON validation,
bounded HTTP and the existing async DB session. If the DB fails, retain the
system/HTTP/backup results and emit `control_database_unavailable`.

- [ ] **Step 3: Produce one sorted set of safe check codes**

The same codes shown in the admin panel must be used in Telegram. Raw command or
HTTP bodies are never included.

- [ ] **Step 4: Run focused tests and commit**

```powershell
git add backend/app/operations/watchdog.py backend/app/services/vpn_release_readiness.py backend/tests/test_vpn_operations_watchdog.py backend/tests/test_vpn_release_readiness.py
git commit -m "feat(ops): collect release watchdog checks"
```

### Task 4: Add hardened systemd units and timers

**Files:**
- Create: `deploy/veltrix-backup.service`
- Create: `deploy/veltrix-backup.timer`
- Create: `deploy/veltrix-watchdog.service`
- Create: `deploy/veltrix-watchdog.timer`
- Create: `backend/tests/test_vpn_operations_units.py`

- [ ] **Step 1: Write failing unit-file tests**

Assert exact virtualenv Python plus `-m` module, working directory, no shell,
root-only execution, private umask, `NoNewPrivileges`, protected home/system,
explicit `ReadWritePaths`, bounded runtime, no overlapping runs and timers with
persistent catch-up plus jitter.

- [ ] **Step 2: Add the backup unit/timer**

Run daily around 03:10 UTC with `RandomizedDelaySec`; use a lock file and a
timeout longer than the measured full dump/restore.

- [ ] **Step 3: Add the watchdog unit/timer**

Run every five minutes as root with a narrow writable state directory,
`Persistent=true`, never restart VPN services and do not place secrets in
`ExecStart`.

- [ ] **Step 4: Run tests and `systemd-analyze verify` on Linux**

Windows tests inspect static safety properties; deployment acceptance must run
`systemd-analyze verify` on a disposable Linux host before production.

- [ ] **Step 5: Commit**

```powershell
git add deploy/veltrix-backup.service deploy/veltrix-backup.timer deploy/veltrix-watchdog.service deploy/veltrix-watchdog.timer backend/tests/test_vpn_operations_units.py
git commit -m "feat(ops): schedule backups and watchdog"
```

### Task 5: Add fail-closed configuration and operator docs

**Files:**
- Modify: `backend/app/core/config.py`
- Modify: `backend/.env.example`
- Modify: `docs/vpn-service.md`
- Create: `docs/veltrix-release-operations.md`
- Create: `backend/tests/test_vpn_operations_config.py`

- [ ] **Step 1: Add disabled defaults and validation tests**

```dotenv
VPN_BACKUP_ENABLED=false
VPN_BACKUP_DIRECTORY=/var/backups/domain-drop-catcher
VPN_BACKUP_RETENTION=7
VPN_WATCHDOG_ENABLED=false
VPN_ALERT_TELEGRAM_USER_ID=
VPN_WATCHDOG_STATE_PATH=/var/lib/veltrix-watchdog/state.json
```

Reject relative paths, retention outside 2..31 and enabled alerts without a
numeric Telegram user ID.

- [ ] **Step 2: Write installation, test-alert, restore and rollback procedures**

Document exact unit installation, daemon-reload, timer enablement, dry-run,
manual backup, marker inspection, isolated restore rehearsal, timer disablement
and recovery from failed/partial sets.

- [ ] **Step 3: Run focused tests, full lint and docs scan**

- [ ] **Step 4: Commit**

```powershell
git add backend/app/core/config.py backend/.env.example docs/vpn-service.md docs/veltrix-release-operations.md backend/tests/test_vpn_operations_config.py
git commit -m "docs(ops): document backup and watchdog rollout"
```

### Task 6: Verify the complete operations slice

**Files:**
- Modify: `docs/current-state.md`

- [ ] **Step 1: Run all new tests and full Ruff**

- [ ] **Step 2: Run the full backend suite, frontend suite and production build**

- [ ] **Step 3: Run a disposable PostgreSQL backup/restore rehearsal**

Use synthetic data only locally. Confirm the full dump can be restored and the
watchdog emits one alert/recovery pair through a fake Telegram server.

- [ ] **Step 4: Run `git diff --check` and inspect all unit files**

- [ ] **Step 5: Update current state and commit**

```powershell
git add docs/current-state.md
git commit -m "docs(ops): record release operations verification"
```
