# Veltrix VPN Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the owner's unstable legacy Hiddify profile with a verified REALITY/443 profile and debounce automatic watchdog alerts.

**Architecture:** Keep VPN provisioning on the existing production path and leave the legacy profile active until the replacement is accepted. Reuse the watchdog's existing JSON state: the first failing observation becomes an unnotified pending state, the second identical observation sends one alert, and recovery is sent only for an outage that was reported.

**Tech Stack:** Python 3.11, pytest, Ruff, PostgreSQL, systemd, existing asyncssh/3x-UI integration, Telegram Bot API.

---

### Task 1: Debounce and clarify watchdog notifications

**Files:**
- Modify: `backend/tests/test_vpn_operations_watchdog.py:300-510`
- Modify: `backend/app/operations/watchdog.py:244-313`

- [ ] **Step 1: Write the failing state-sequence tests**

Replace the immediate-alert expectations with these explicit sequences:

```python
def test_first_failure_is_pending_and_silent(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    pending = watchdog.run_alert_cycle(
        ["public_health", "control_health"],
        state_path=state_path,
        environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert pending == watchdog.build_alert_state(
        ["control_health", "public_health"], notified=False
    )
    assert messages == []
    assert watchdog.load_alert_state(state_path) == pending


def test_second_identical_failure_sends_clear_alert(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    for _ in range(2):
        state = watchdog.run_alert_cycle(
            ["control_health"],
            state_path=state_path,
            environ=ALERT_ENV,
            sender=lambda _token, _user_id, message: messages.append(message),
        )

    assert state.notified is True
    assert messages == [
        (
            "Veltrix VPN: мониторинг обнаружил устойчивую проблему.\n"
            "Подключение может работать нестабильно. Проверьте раздел «Готовность» в панели.\n"
            "Коды проверки: control_health"
        )
    ]


def test_pending_failure_recovers_without_message(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    messages: list[str] = []

    watchdog.run_alert_cycle(
        ["endpoint_health"], state_path=state_path, environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )
    healthy = watchdog.run_alert_cycle(
        [], state_path=state_path, environ=ALERT_ENV,
        sender=lambda _token, _user_id, message: messages.append(message),
    )

    assert healthy == watchdog.build_alert_state((), notified=True)
    assert messages == []
```

Update the existing changed-reason, recovery and delivery-retry tests so they
also exercise two observations before an initial delivery. A changed pending
reason must restart confirmation. A changed reason during a notified outage must
remain silent but keep `notified=True`, so later recovery is still delivered.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
cd backend
.venv\Scripts\python.exe -m pytest tests/test_vpn_operations_watchdog.py -k "first_failure or second_identical or pending_failure or changed_failure or recovery or telegram_failure" -q
```

Expected: failures show that the first observation currently sends immediately
and that the old Russian copy is returned.

- [ ] **Step 3: Implement the minimum state transition**

Use the existing `AlertState`; add no field and no migration. Replace the two
messages with:

```python
def _failure_message(codes: tuple[str, ...]) -> str:
    return (
        "Veltrix VPN: мониторинг обнаружил устойчивую проблему.\n"
        "Подключение может работать нестабильно. "
        "Проверьте раздел «Готовность» в панели.\n"
        f"Коды проверки: {', '.join(codes)}"
    )


_RECOVERY_MESSAGE = (
    "Veltrix VPN: работа сервиса восстановлена.\n"
    "Проверки снова проходят. Дополнительных действий не требуется."
)
```

In `run_alert_cycle()` implement only these branches:

```python
if current.failing_codes:
    if state_valid and previous.digest == current.digest:
        if previous.notified:
            return previous
        pending = current
        message = _failure_message(current.failing_codes)
    elif state_valid and previous.notified and previous.failing_codes:
        reported = build_alert_state(current.failing_codes, notified=True)
        save_alert_state(state_path, reported)
        return reported
    else:
        save_alert_state(state_path, current)
        return current
elif previous.failing_codes:
    healthy = build_alert_state((), notified=True)
    if not previous.notified:
        save_alert_state(state_path, healthy)
        return healthy
    pending = build_alert_state((), notified=False)
    message = _RECOVERY_MESSAGE
elif not previous.notified:
    pending = previous
    message = _RECOVERY_MESSAGE
else:
    healthy = build_alert_state((), notified=True)
    if not state_valid:
        save_alert_state(state_path, healthy)
    return healthy
```

Save `pending` before delivery. On sender failure return `pending`; on success
save the same code set with `notified=True`.

- [ ] **Step 4: Run the focused and complete watchdog tests**

Run:

```powershell
cd backend
.venv\Scripts\python.exe -m pytest tests/test_vpn_operations_watchdog.py -q
.venv\Scripts\python.exe -m ruff check app/operations/watchdog.py tests/test_vpn_operations_watchdog.py
```

Expected: all watchdog tests pass and Ruff reports no errors.

- [ ] **Step 5: Commit the watchdog fix**

```powershell
git add backend/app/operations/watchdog.py backend/tests/test_vpn_operations_watchdog.py
git commit -m "fix: debounce Veltrix watchdog alerts"
```

### Task 2: Verify and integrate the code change

**Files:**
- Modify: `docs/current-state.md`
- Modify: `docs/vpn-service.md`

- [ ] **Step 1: Document the incident and new alert rule**

Record the legacy 8443 timeout evidence, absence of Xray restarts, the transient
health transport error, and the two-cycle watchdog rule. Do not include Telegram
IDs, access URIs, UUIDs, passwords or raw Hiddify destinations.

- [ ] **Step 2: Run repository verification**

Run:

```powershell
cd backend
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check app tests
cd ..
git diff --check
```

Expected: the complete backend suite passes, Ruff is clean, and diff-check emits
no output.

- [ ] **Step 3: Commit documentation**

```powershell
git add docs/current-state.md docs/vpn-service.md docs/superpowers/specs/2026-10-03-veltrix-vpn-stability-design.md docs/superpowers/plans/2026-10-03-veltrix-vpn-stability.md
git commit -m "docs: record Veltrix stability response"
```

- [ ] **Step 4: Push, open a pull request and wait for CI**

Push `codex/veltrix-vpn-stability`, open a PR to `main`, attach it to the task,
and merge only when `linux-operations` completes successfully and GitHub reports
the PR mergeable.

### Task 3: Deploy the watchdog fix safely

**Files:**
- Production checkout: `/opt/domain-drop-catcher`

- [ ] **Step 1: Run production preflight and backup**

Require a clean tracked production tree, no active attack or VPN mutation, healthy
control/Nginx, sufficient disk space, and a successful fresh `veltrix-backup.service`
run with a validated manifest.

- [ ] **Step 2: Fast-forward to the merged commit**

Fetch `origin/main`, confirm the diff contains only the approved watchdog tests,
watchdog implementation and documentation, then run `git merge --ff-only
origin/main`.

- [ ] **Step 3: Verify the live timer behavior without generating Telegram messages**

Run the watchdog test module on the deployed checkout. Confirm the current state
file is healthy and private, then let the next scheduled one-shot service run.
Require successful systemd completion and unchanged healthy state. Do not inject
a production failure or send a synthetic Telegram alert.

### Task 4: Issue and verify the replacement Hiddify profile

**Files:**
- No repository code changes
- Production database and worker 2 through existing application services

- [ ] **Step 1: Confirm mutation safety**

Require subscription 2 active with a free device slot, worker 2 and endpoint 3
ready, no active attack or maintenance on worker 2, and no queued/claimed control
operation for that worker.

- [ ] **Step 2: Issue the replacement through existing application code**

Call `create_vpn_access_key()` with subscription 2, worker 2, protocol `vless`
and display name `Windows · Hiddify`, using the active admin only for the existing
audit log. Print only the new key ID, status, worker and display name. Never print
the response URI or UUID.

- [ ] **Step 3: Rename the legacy profile without changing its connection**

Call the existing display-name update path for key 8 with
`Старый профиль · 8443`. Do not revoke, suspend or rewrite that key.

- [ ] **Step 4: Verify the new connection**

Require the new row to be active on worker 2, a REALITY URI on port 443, and an
unchanged legacy key 8 identity/status. Confirm the new client exists in the
configured inbound, then use a temporary local client to obtain HTTP 200, HTTPS
200 with certificate validation, and a UDP DNS response. Redact credentials and
remove every temporary config/process afterward.

- [ ] **Step 5: Final health checks**

Require control health and `/vpn/` to return 200, both VPN endpoints ready,
control/Nginx/backup/watchdog timers active, no new failed VPN operation, and no
unexpected Xray restart beyond the single expected worker-2 provisioning restart.
Keep key 8 active until the owner confirms PUBG on `Windows · Hiddify`.
