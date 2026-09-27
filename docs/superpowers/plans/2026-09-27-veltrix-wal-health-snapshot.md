# Veltrix WAL-safe Fleet Health Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing strict node-health probe read a consistent live 3x-UI SQLite WAL snapshot without changing VPN state or adding dependencies.

**Architecture:** Replace the raw main-file copy with SQLite's standard online backup into an in-memory connection. Keep the existing path and metadata gates, admit only a safe WAL/SHM pair, and fail closed through the existing static inventory error.

**Tech Stack:** Python 3.11 standard-library `sqlite3`, pytest, Ruff, existing deterministic node zipapp and strict SSH deployer.

---

### Task 1: Specify WAL behavior with failing tests

**Files:**
- Modify: `backend/tests/test_vpn_xui_node_observation.py`

- [ ] **Step 1: Replace the WAL rejection test with a real uncheckpointed-WAL acceptance test**

Keep the writer open, prove an immutable main-file reader cannot see ID 8, and require the production helper to see it through WAL:

```python
def test_wal_database_snapshot_includes_uncheckpointed_rows(observation, data):
    writer = sqlite3.connect(data["database"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
        with sqlite3.connect(
            f"{data['database'].as_uri()}?immutable=1", uri=True
        ) as stale:
            assert tuple(stale.execute("SELECT id FROM inbounds ORDER BY id")) == (
                (2,),
                (7,),
            )

        assert observation._local_inbound_ids(data["database"]) == (2, 7, 8)
    finally:
        writer.close()
```

- [ ] **Step 2: Add fail-closed sidecar tests**

Use the real WAL files, then alter only `os.lstat()` metadata so the filesystem is not mutated by the test:

```python
@pytest.mark.parametrize("defect", ["symlink", "wrong_owner"])
def test_unsafe_wal_sidecar_fails_closed(observation, data, monkeypatch, defect):
    writer = sqlite3.connect(data["database"])
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        writer.execute("INSERT INTO inbounds VALUES (8)")
        writer.commit()
        wal = Path(str(data["database"]) + "-wal")
        real_lstat = os.lstat

        def lstat(path):
            info = real_lstat(path)
            if Path(path) != wal:
                return info
            values = list(info)
            if defect == "symlink":
                values[0] = stat.S_IFLNK | 0o777
            else:
                values[4] = info.st_uid + 1
            return os.stat_result(values)

        monkeypatch.setattr(observation.os, "lstat", lstat)
        with pytest.raises(VpnEndpointError, match="^vpn_xui_inventory_unavailable$"):
            observation._local_inbound_ids(data["database"])
    finally:
        writer.close()
```

- [ ] **Step 3: Replace the raw-read drift test with a backup deadline test**

Track real SQLite connection closure and make the monotonic deadline expire from the backup progress callback:

```python
def test_wal_backup_deadline_closes_connections(observation, data, monkeypatch):
    writer = sqlite3.connect(data["database"])
    assert writer.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
    writer.execute("INSERT INTO inbounds VALUES (8)")
    writer.commit()
    real_connect = sqlite3.connect
    closed = []

    class Connection(sqlite3.Connection):
        def close(self):
            closed.append(True)
            super().close()

    monkeypatch.setattr(
        observation.sqlite3,
        "connect",
        lambda *args, **kwargs: real_connect(
            *args, factory=Connection, **kwargs
        ),
    )
    calls = 0

    def monotonic():
        nonlocal calls
        calls += 1
        return 0.0 if calls < 3 else 3.0

    monkeypatch.setattr(observation.time, "monotonic", monotonic)
    try:
        with pytest.raises(VpnEndpointError, match="^vpn_xui_inventory_unavailable$"):
            observation._local_inbound_ids(data["database"])
        assert calls >= 3
        assert closed == [True, True]
    finally:
        writer.close()
```

- [ ] **Step 4: Update the connection-contract test**

Require two source connections with `mode=ro&uri=True`, two in-memory destinations, `PRAGMA query_only=ON`, the bounded ID query, and closure of all four connections across the before/after observations.

- [ ] **Step 5: Run the focused tests and confirm RED**

Run:

```powershell
python -m pytest tests/test_vpn_xui_node_observation.py -q -k "wal or local_reads"
```

from `backend/`.

Expected: the WAL acceptance test fails with `vpn_xui_inventory_unavailable`; no collection or fixture error is acceptable.

- [ ] **Step 6: Commit the failing tests**

```powershell
git add backend/tests/test_vpn_xui_node_observation.py
git commit -m "test(vpn): require consistent WAL health snapshots"
```

### Task 2: Implement the minimum online snapshot

**Files:**
- Modify: `backend/app/services/vpn_xui_node_observation.py`

- [ ] **Step 1: Remove the obsolete raw-copy dependency**

Delete the now-unused `hashlib` import. Keep `os`, `sqlite3`, `stat`, `time` and `Path`.

- [ ] **Step 2: Replace `_sidecars_absent()` with bounded metadata validation**

```python
def _wal_sidecars_are_safe(path: Path, database: os.stat_result) -> None:
    found: dict[str, os.stat_result] = {}
    for suffix in ("-wal", "-shm"):
        try:
            found[suffix] = os.lstat(str(path) + suffix)
        except FileNotFoundError:
            pass
    try:
        os.lstat(str(path) + "-journal")
    except FileNotFoundError:
        pass
    else:
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    if len(found) not in {0, 2}:
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    for info in found.values():
        if (
            not stat.S_ISREG(info.st_mode)
            or stat.S_ISLNK(info.st_mode)
            or _reparse(info)
            or info.st_uid != database.st_uid
            or stat.S_IMODE(info.st_mode) != stat.S_IMODE(database.st_mode)
            or not 0 <= info.st_size <= _DATABASE_LIMIT
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
```

- [ ] **Step 3: Narrow `_same_file()` to stable identity**

Compare only device, inode, mode and UID so a legitimate WAL checkpoint may change size and timestamps without hiding a main-file replacement:

```python
def _same_file(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_uid,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_uid,
    )
```

- [ ] **Step 4: Replace `_database_snapshot()` with SQLite online backup**

Keep all validation and cleanup in the existing helper:

```python
def _database_snapshot(path: Path, deadline: float) -> sqlite3.Connection:
    if not isinstance(path, Path) or not path.is_absolute() or ".." in path.parts:
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    source = None
    snapshot = None
    try:
        for parent in path.parents:
            info = os.lstat(parent)
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or _reparse(info):
                raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        directory_before = os.lstat(path.parent)
        before = os.lstat(path)
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or _reparse(before)
            or not 100 <= before.st_size <= _DATABASE_LIMIT
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        _wal_sidecars_are_safe(path, before)
        if time.monotonic() >= deadline:
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        source = sqlite3.connect(
            f"{path.as_uri()}?mode=ro",
            uri=True,
            timeout=_DATABASE_TIMEOUT_SECONDS,
        )
        source.execute("PRAGMA query_only=ON")
        snapshot = sqlite3.connect(":memory:", timeout=_DATABASE_TIMEOUT_SECONDS)

        def progress(_status: int, _remaining: int, _total: int) -> None:
            if time.monotonic() >= deadline:
                raise VpnEndpointError("vpn_xui_inventory_unavailable") from None

        source.backup(snapshot, pages=64, progress=progress, sleep=0.01)
        after = os.lstat(path)
        directory_after = os.lstat(path.parent)
        _wal_sidecars_are_safe(path, after)
        if (
            time.monotonic() >= deadline
            or not _same_file(before, after)
            or not _same_file(directory_before, directory_after)
        ):
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
        source.close()
        source = None
        result = snapshot
        snapshot = None
        return result
    except VpnEndpointError:
        raise
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
    finally:
        close_failed = False
        for connection in (source, snapshot):
            if connection is not None:
                try:
                    connection.close()
                except sqlite3.Error:
                    close_failed = True
        if close_failed:
            raise VpnEndpointError("vpn_xui_inventory_unavailable") from None
```

- [ ] **Step 5: Consume the returned in-memory connection directly**

In `_local_inbound_ids()`, replace the deserialize path with:

```python
connection = _database_snapshot(path, deadline)
connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
connection.execute("PRAGMA query_only=ON")
```

Keep the existing bounded `SELECT`, validation, static exception mapping and `finally` close.

- [ ] **Step 6: Run the focused suite and confirm GREEN**

```powershell
python -m pytest tests/test_vpn_xui_node_observation.py -q
```

Expected: all tests pass and no warning or secret-bearing output appears.

- [ ] **Step 7: Run adjacent node-health tests**

```powershell
python -m pytest tests/test_vpn_node_health_entrypoint.py tests/test_vpn_node_bundle.py tests/test_vpn_fleet_health.py -q
python -m ruff check app/services/vpn_xui_node_observation.py tests/test_vpn_xui_node_observation.py
git diff --check
```

Expected: all tests and Ruff pass; whitespace check is empty.

- [ ] **Step 8: Commit the implementation**

```powershell
git add backend/app/services/vpn_xui_node_observation.py backend/tests/test_vpn_xui_node_observation.py
git commit -m "fix(vpn): observe live WAL inventory safely"
```

### Task 3: Verify, review and integrate

**Files:**
- Modify: `docs/current-state.md`

- [ ] **Step 1: Run the full affected backend gate**

```powershell
python -m pytest tests/test_vpn_xui_node_observation.py tests/test_vpn_node_health_entrypoint.py tests/test_vpn_node_bundle.py tests/test_vpn_node_transport.py tests/test_vpn_fleet_health.py tests/test_vpn_operations_watchdog.py -q
python -m ruff check app tests
git diff --check
```

Expected: zero failures and zero Ruff errors.

- [ ] **Step 2: Record the verified WAL behavior**

Replace the stale statement that live WAL is unsupported with: the node runner
uses SQLite online backup into memory, validates the main database and WAL/SHM
metadata before and after the snapshot, and still fails closed on unsafe
sidecars or deadlines. State that production fleet health remains disabled until
live rollout acceptance.

- [ ] **Step 3: Commit the documentation update**

```powershell
git add docs/current-state.md
git commit -m "docs(vpn): record WAL-safe health snapshot"
```

- [ ] **Step 4: Push and open a PR**

```powershell
git push -u origin codex/veltrix-wal-health-snapshot
$body = @"
## Summary
- snapshot live 3x-UI WAL safely through SQLite online backup
- keep strict metadata, deadline and bounded-inventory checks
- leave payment, public trial and production fleet health disabled

## Verification
- focused WAL test failed before implementation and passed afterward
- affected backend suites, Ruff and `git diff --check` passed
"@
gh pr create --repo d992-prog/bckordr --base main --head codex/veltrix-wal-health-snapshot --title "fix(vpn): observe live WAL inventory safely" --body $body
```

The final PR body must contain the actual test counts and commands from the
completed verification rather than copying unverified expected results.

- [ ] **Step 5: Require green CI, review, then merge**

```powershell
$pr = gh pr view --repo d992-prog/bckordr --json number --jq '.number'
gh pr checks $pr --repo d992-prog/bckordr --watch
gh pr merge $pr --repo d992-prog/bckordr --merge --delete-branch
```

Do not merge on a failed or pending required check.

### Task 4: Deploy disabled and accept on production

**Files:**
- Production checkout: `/opt/domain-drop-catcher`
- Production environment: `/opt/domain-drop-catcher/backend/.env`

- [ ] **Step 1: Deploy the exact merge commit with fleet health off**

Verify `VPN_FLEET_HEALTH_ENABLED=false`, deploy through the existing release procedure, and confirm local/public `/api/health` return 200 before touching the node runner.

- [ ] **Step 2: Atomically deploy the common node bundle**

Verify the existing `.previous` runner is a root-owned `0600` regular file and
atomically move it to a new non-existing name below the root-private
`/var/lib/veltrix-vpn/release-archive/`; never overwrite or delete an archived
runner. Then use `build_node_bundle()`, `build_node_deployment_helper()` and
`deploy_prebuilt_node_release()` through the pinned strict transport. Require
`installed`, which preserves the current runner as the new `.previous`; do not
change node config, token, journal, clients, Xray or endpoints.

- [ ] **Step 3: Run one manual strict health probe**

Require one eligible endpoint, one fresh result and one healthy result. Verify the runner import sentinel, absent deployment-state marker, public VPN listener, control/public health and Nginx configuration.

- [ ] **Step 4: Enable only fleet health**

Change exactly `VPN_FLEET_HEALTH_ENABLED=false` to `true`, restart only `domain-drop-control.service`, wait for a scheduled probe, then require fresh healthy endpoint evidence and HTTP 200 locally and publicly. Leave public trial, payment and ready notifications disabled.

- [ ] **Step 5: Verify watchdog recovery/dedupe**

Wait for the next five-minute watchdog run. Require `Result=success`, a fresh state timestamp only when the failing-code set changes, and no repeated identical Telegram alert.

- [ ] **Step 6: Roll back on any failed gate**

Set fleet health false and restart the control service first. If the runner caused the failure, restore the preserved `.previous` bundle atomically. Never modify the live 3x-UI database or delete WAL/SHM files.
