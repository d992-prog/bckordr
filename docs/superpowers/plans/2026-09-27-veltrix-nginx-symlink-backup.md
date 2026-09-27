# Veltrix Nginx Symlink Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the validated production backup materialize only approved Nginx configuration symlinks as private regular files while preserving the existing fail-closed boundary for every other symlink.

**Architecture:** Extend the existing descriptor-anchored POSIX tree copier with an opt-in tuple of approved symlink roots. Only the Nginx source call supplies roots; the helper lexically bounds the target, opens every component without following links, copies the regular target through the existing bounded descriptor copier, and then revalidates both target and link. The snapshot remains manifest v1 with ordinary files, so validation and restore stay unchanged.

**Tech Stack:** Python 3.11+, `os` descriptor APIs, `pathlib`, `stat`, pytest, Ruff, GitHub Actions Linux release gate.

---

## File map

- Modify `backend/app/operations/backup.py`: add the Ubuntu Nginx module root constant, secure approved-link helpers, and the Nginx-only opt-in at the tree-copy call.
- Modify `backend/tests/test_vpn_operations_backup.py`: add POSIX integration coverage for materialization, rejection boundaries, and link-retarget races.
- No schema, dependency, systemd, Nginx, frontend, restore, or manifest-format files change.

### Task 1: Materialize approved Nginx regular-file links

**Files:**
- Modify: `backend/tests/test_vpn_operations_backup.py:1034`
- Modify: `backend/app/operations/backup.py:36,676-985`

- [ ] **Step 1: Write the failing production-shape regression**

Add this test immediately before the existing external-secret symlink test:

```python
@POSIX_SNAPSHOT_ONLY
def test_approved_nginx_symlinks_are_materialized_as_regular_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    sites_available = config.nginx_directory / "sites-available"
    sites_enabled = config.nginx_directory / "sites-enabled"
    modules_enabled = config.nginx_directory / "modules-enabled"
    module_root = tmp_path / "nginx-modules-available"
    for directory in (sites_available, sites_enabled, modules_enabled, module_root):
        directory.mkdir()

    site = sites_available / "veltrix.conf"
    module = module_root / "50-mod-http-geoip.conf"
    site.write_text("server { listen 443; }\n", encoding="utf-8")
    module.write_text("load_module modules/ngx_http_geoip_module.so;\n", encoding="utf-8")
    (sites_enabled / site.name).symlink_to(Path("..") / "sites-available" / site.name)
    (modules_enabled / module.name).symlink_to(module)
    monkeypatch.setattr(backup_module, "_NGINX_MODULES_DIRECTORY", module_root)

    result = _run(config, FakeRunner())

    copied_site = result.directory / "nginx/sites-enabled" / site.name
    copied_module = result.directory / "nginx/modules-enabled" / module.name
    assert copied_site.read_bytes() == site.read_bytes()
    assert copied_module.read_bytes() == module.read_bytes()
    assert copied_site.is_file() and not copied_site.is_symlink()
    assert copied_module.is_file() and not copied_module.is_symlink()
    manifest = _read_json(result.directory / "manifest.json")
    records = {item["path"]: item for item in manifest["files"]}
    for path in (copied_site, copied_module):
        relative = path.relative_to(result.directory).as_posix()
        raw = path.read_bytes()
        assert records[relative] == {
            "path": relative,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
```

- [ ] **Step 2: Run the test on Linux and verify the red state**

Run from `backend`:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_vpn_operations_backup.py::test_approved_nginx_symlinks_are_materialized_as_regular_files
```

Expected: one failure with `BackupError: backup_source_invalid`, because `_scan_directory_fd` still rejects the first Nginx link.

- [ ] **Step 3: Add the minimal secure materialization implementation**

Near the other module constants add:

```python
_NGINX_MODULES_DIRECTORY = Path("/usr/share/nginx/modules-available")
```

Add these helpers after `_copy_descriptor`:

```python
def _matches_stable_symlink(
    info: os.stat_result, expected: os.stat_result
) -> bool:
    return (
        stat.S_ISLNK(info.st_mode)
        and not _is_reparse(info)
        and _file_identity(info) == _file_identity(expected)
        and info.st_size == expected.st_size
        and info.st_mtime_ns == expected.st_mtime_ns
        and info.st_ctime_ns == expected.st_ctime_ns
    )


def _lexical_symlink_target(parent: Path, target: str) -> Path:
    candidate = Path(target)
    if not candidate.is_absolute():
        candidate = parent / candidate
    normalized = Path(os.path.normpath(candidate))
    if not normalized.is_absolute():
        raise BackupError("backup_source_invalid")
    return normalized


def _is_below_approved_root(path: Path, roots: tuple[Path, ...]) -> bool:
    for root in roots:
        normalized_root = Path(os.path.normpath(root))
        try:
            path.relative_to(normalized_root)
        except ValueError:
            continue
        return True
    return False


def _copy_approved_symlink(
    parent_fd: int,
    parent_source: Path,
    name: str,
    link_expected: os.stat_result,
    destination: Path,
    relative: Path,
    approved_roots: tuple[Path, ...],
    budget: _Budget,
) -> dict[str, object]:
    target_parent_fd = -1
    target_fd = -1
    try:
        link_text = os.readlink(name, dir_fd=parent_fd)
        linked = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _matches_stable_symlink(linked, link_expected):
            raise BackupError("backup_source_changed")
        target = _lexical_symlink_target(parent_source, link_text)
        if not _is_below_approved_root(target, approved_roots):
            raise BackupError("backup_source_invalid")

        target_parent_fd, target_parent_expected = _open_absolute_directory(
            target.parent, budget
        )
        target_expected = os.stat(
            target.name, dir_fd=target_parent_fd, follow_symlinks=False
        )
        if _is_reparse(target_expected) or not stat.S_ISREG(target_expected.st_mode):
            raise BackupError("backup_source_invalid")
        budget.add_file(target_expected.st_size)
        target_fd = os.open(
            target.name,
            os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_BINARY", 0),
            dir_fd=target_parent_fd,
        )
        record = _copy_descriptor(
            target_fd, destination, relative, target_expected, budget
        )

        target_linked = os.stat(
            target.name, dir_fd=target_parent_fd, follow_symlinks=False
        )
        if not _matches_stable_file(target_linked, target_expected):
            raise BackupError("backup_source_changed")
        if not _matches_identity(
            target.parent.lstat(), target_parent_expected, directory=True
        ):
            raise BackupError("backup_source_changed")
        link_after = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not _matches_stable_symlink(link_after, link_expected)
            or os.readlink(name, dir_fd=parent_fd) != link_text
        ):
            raise BackupError("backup_source_changed")
        return record
    except BackupError:
        raise
    except (OSError, RuntimeError, ValueError):
        raise BackupError("backup_source_invalid") from None
    finally:
        if target_fd >= 0:
            os.close(target_fd)
        if target_parent_fd >= 0:
            os.close(target_parent_fd)
```

Extend `_DIR_FD_SUPPORTED` with `and os.readlink in os.supports_dir_fd` so an
unsupported interpreter fails before creating a partial set.

Change `_scan_directory_fd` so links are accepted only when the caller opts in, without charging the file budget until the regular target is opened:

```python
def _scan_directory_fd(
    descriptor: int,
    depth: int,
    budget: _Budget,
    *,
    allow_symlinks: bool,
) -> list[tuple[str, os.stat_result]]:
    entries: list[tuple[str, os.stat_result]] = []
    try:
        with os.scandir(descriptor) as iterator:
            for entry in iterator:
                budget.check_deadline()
                info = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
                if _is_reparse(info):
                    raise BackupError("backup_source_invalid")
                if stat.S_ISLNK(info.st_mode):
                    if not allow_symlinks:
                        raise BackupError("backup_source_invalid")
                elif stat.S_ISDIR(info.st_mode):
                    if depth + 1 > budget.config.max_depth:
                        raise BackupError("backup_limits_exceeded")
                    budget.add_directory()
                elif stat.S_ISREG(info.st_mode):
                    budget.add_file(info.st_size)
                else:
                    raise BackupError("backup_source_invalid")
                entries.append((entry.name, info))
    except BackupError:
        raise
    except OSError:
        raise BackupError("backup_source_invalid") from None
    return sorted(entries, key=lambda item: item[0])
```

Add the opt-in parameter to `_copy_posix_tree`, pass it to the scan, and branch before the regular-file open:

```python
def _copy_posix_tree(
    source: Path,
    destination: Path,
    relative: Path,
    budget: _Budget,
    *,
    approved_symlink_roots: tuple[Path, ...] = (),
) -> list[dict[str, object]]:
```

```python
frame.entries = _scan_directory_fd(
    frame.descriptor,
    frame.depth,
    budget,
    allow_symlinks=bool(approved_symlink_roots),
)
```

```python
if stat.S_ISLNK(entry_info.st_mode):
    records.append(
        _copy_approved_symlink(
            frame.descriptor,
            frame.source,
            name,
            entry_info,
            child_destination,
            child_relative,
            approved_symlink_roots,
            budget,
        )
    )
    continue
```

Only the Nginx call opts in:

```python
_copy_posix_tree(
    config.nginx_directory,
    partial / "nginx",
    Path("nginx"),
    budget,
    approved_symlink_roots=(
        config.nginx_directory,
        _NGINX_MODULES_DIRECTORY,
    ),
)
```

- [ ] **Step 4: Run the focused acceptance and existing escape tests**

Run from `backend`:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_vpn_operations_backup.py::test_approved_nginx_symlinks_are_materialized_as_regular_files \
  tests/test_vpn_operations_backup.py::test_symlink_source_is_rejected_without_following_it
```

Expected: `2 passed`; the approved links are regular snapshot files and the external secret is not copied.

- [ ] **Step 5: Commit the working acceptance slice**

```bash
git add backend/app/operations/backup.py backend/tests/test_vpn_operations_backup.py
git commit -m "fix(ops): snapshot approved nginx symlinks"
```

### Task 2: Lock down unsupported links and retarget races

**Files:**
- Modify: `backend/tests/test_vpn_operations_backup.py:1034-1050`
- Modify: `backend/app/operations/backup.py:676-985`

- [ ] **Step 1: Add rejection tests for non-regular and indirect targets**

Add these tests beside the acceptance regression:

```python
@POSIX_SNAPSHOT_ONLY
@pytest.mark.parametrize("target_kind", ["broken", "directory", "link-to-link"])
def test_approved_nginx_symlink_rejects_unsupported_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_kind: str,
) -> None:
    config = _config(tmp_path)
    enabled = config.nginx_directory / "modules-enabled"
    module_root = tmp_path / "nginx-modules-available"
    enabled.mkdir()
    module_root.mkdir()
    target = module_root / "module.conf"
    if target_kind == "directory":
        target.mkdir()
    elif target_kind == "link-to-link":
        regular = module_root / "regular.conf"
        regular.write_text("load_module safe;\n", encoding="utf-8")
        target.symlink_to(regular.name)
    link = enabled / "module.conf"
    link.symlink_to(target)
    monkeypatch.setattr(backup_module, "_NGINX_MODULES_DIRECTORY", module_root)

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not (partial / "nginx/modules-enabled/module.conf").exists()


@POSIX_SNAPSHOT_ONLY
def test_relative_nginx_symlink_cannot_escape_approved_roots(tmp_path: Path) -> None:
    config = _config(tmp_path)
    enabled = config.nginx_directory / "sites-enabled"
    enabled.mkdir()
    secret = tmp_path / "outside-secret"
    secret.write_text("must not be copied", encoding="utf-8")
    (enabled / "escape.conf").symlink_to(
        Path("..") / ".." / ".." / secret.name
    )

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())

    partial = config.backup_root / f"20260924T031011.000000Z-{NONCE}.partial"
    assert not (partial / "nginx/sites-enabled/escape.conf").exists()


@POSIX_SNAPSHOT_ONLY
def test_frontend_symlink_remains_invalid(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = config.frontend_dist / "real.js"
    source.write_text("safe asset", encoding="utf-8")
    (config.frontend_dist / "linked.js").symlink_to(source.name)

    with pytest.raises(BackupError, match="^backup_source_invalid$"):
        _run(config, FakeRunner())
```

- [ ] **Step 2: Run the rejection tests**

Run from `backend`:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_vpn_operations_backup.py::test_approved_nginx_symlink_rejects_unsupported_target \
  tests/test_vpn_operations_backup.py::test_relative_nginx_symlink_cannot_escape_approved_roots \
  tests/test_vpn_operations_backup.py::test_frontend_symlink_remains_invalid
```

Expected: `5 passed`. Any failure must be fixed by tightening the descriptor-based helper, never by broadening an approved root.

- [ ] **Step 3: Add a failing link-retarget race test**

```python
@POSIX_SNAPSHOT_ONLY
def test_nginx_symlink_retarget_during_copy_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    available = config.nginx_directory / "sites-available"
    enabled = config.nginx_directory / "sites-enabled"
    available.mkdir()
    enabled.mkdir()
    original = available / "original.conf"
    replacement = available / "replacement.conf"
    original.write_text("server { listen 443; }\n", encoding="utf-8")
    replacement.write_text("server { listen 8443; }\n", encoding="utf-8")
    link = enabled / "veltrix.conf"
    link.symlink_to(Path("..") / "sites-available" / original.name)
    real_copy = backup_module._copy_descriptor

    def copy_then_retarget(*args, **kwargs):
        record = real_copy(*args, **kwargs)
        if record["path"] == "nginx/sites-enabled/veltrix.conf":
            link.unlink()
            link.symlink_to(Path("..") / "sites-available" / replacement.name)
        return record

    monkeypatch.setattr(backup_module, "_copy_descriptor", copy_then_retarget)

    with pytest.raises(BackupError, match="^backup_source_changed$"):
        _run(config, FakeRunner())

    assert not (config.backup_root / "latest-success.json").exists()
```

- [ ] **Step 4: Run the race test and verify post-copy revalidation**

Run from `backend`:

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_vpn_operations_backup.py::test_nginx_symlink_retarget_during_copy_is_rejected
```

Expected: `1 passed` and no successful marker. The post-copy identity and target-text checks in `_copy_approved_symlink` must raise `backup_source_changed`; initial missing or malformed targets remain `backup_source_invalid`.

- [ ] **Step 5: Run all symlink-focused cases and commit**

```bash
python -m pytest -q -p no:cacheprovider \
  tests/test_vpn_operations_backup.py -k "symlink or retarget"
git add backend/app/operations/backup.py backend/tests/test_vpn_operations_backup.py
git commit -m "test(ops): enforce nginx symlink boundaries"
```

Expected: all selected cases pass and unsupported links never create a successful marker.

### Task 3: Complete quality and release verification

**Files:**
- Verify: `backend/app/operations/backup.py`
- Verify: `backend/tests/test_vpn_operations_backup.py`
- Verify: `.github/workflows/veltrix-release-gate.yml`

- [ ] **Step 1: Run the complete backup suite**

From `backend`:

```bash
python -m pytest -q -p no:cacheprovider tests/test_vpn_operations_backup.py
```

Expected on Linux: every backup test passes with no skip caused by descriptor support. On Windows, POSIX-only cases are expected to skip, so the Linux release gate remains mandatory.

- [ ] **Step 2: Run backend lint and the complete backend suite**

```bash
python -m ruff check app/operations/backup.py tests/test_vpn_operations_backup.py
python -m pytest -q -p no:cacheprovider
```

Expected: Ruff exits zero and the complete backend suite passes.

- [ ] **Step 3: Inspect the patch for scope and accidental secrets**

From the repository root:

```bash
git diff --check origin/main...HEAD
git diff --stat origin/main...HEAD
git diff origin/main...HEAD -- backend/app/operations/backup.py backend/tests/test_vpn_operations_backup.py
git grep -n -E 'never-print-this-password|PGPASSWORD=' -- ':!backend/tests/test_vpn_operations_backup.py'
```

Expected: no whitespace errors, only the specification, plan, backup module and backup tests are changed, and no production secret appears.

- [ ] **Step 4: Push and require the Linux release gate**

```bash
git push -u origin codex/veltrix-backup-nginx-symlinks
gh pr create \
  --base main \
  --head codex/veltrix-backup-nginx-symlinks \
  --title "fix(ops): back up standard nginx symlinks safely" \
  --body-file docs/superpowers/specs/2026-09-27-veltrix-nginx-symlink-backup-design.md
gh pr checks --watch
```

Expected: the Linux release gate passes, including the complete backend suite, Ruff and the dedicated backup test command.

- [ ] **Step 5: Deploy without enabling automation and run production acceptance**

After review and merge, deploy the merge commit with the existing release procedure while preserving `VPN_BACKUP_ENABLED=false`, the disabled backup timer, and all unrelated feature flags. Run one transient manual backup, then verify without printing file contents:

```bash
systemctl is-enabled veltrix-backup.timer
systemctl is-active veltrix-backup.timer
jq -e '.set_name and .manifest_sha256' \
  /var/backups/domain-drop-catcher/latest-success.json
pg_restore --list \
  /var/backups/domain-drop-catcher/$(jq -r .set_name /var/backups/domain-drop-catcher/latest-success.json)/database.dump \
  >/dev/null
```

Expected: timer remains `disabled` and `inactive`; the new successful marker validates; `pg_restore --list` exits zero; materialized `sites-enabled` and `modules-enabled` paths are regular mode-0600 files represented by matching manifest hashes.

- [ ] **Step 6: Rehearse a full isolated database restore**

Run on the control server. The trap removes only the newly named disposable
database; it does not alter the running application's database:

```bash
set -euo pipefail
SET_NAME="$(jq -er .set_name /var/backups/domain-drop-catcher/latest-success.json)"
RESTORE_DB="veltrix_restore_$(date -u +%Y%m%d_%H%M%S)"
cleanup_restore() {
  sudo -u postgres dropdb --if-exists "$RESTORE_DB"
}
trap cleanup_restore EXIT
sudo -u postgres createdb "$RESTORE_DB"
sudo -u postgres /usr/bin/pg_restore \
  --exit-on-error \
  --dbname="$RESTORE_DB" \
  "/var/backups/domain-drop-catcher/$SET_NAME/database.dump"
sudo -u postgres psql --no-psqlrc --tuples-only --no-align \
  --dbname="$RESTORE_DB" \
  --command="SELECT count(*) FROM vpn_access_keys;"
sudo -u postgres psql --no-psqlrc --tuples-only --no-align \
  --dbname="$RESTORE_DB" \
  --command="SELECT count(*) FROM information_schema.columns WHERE table_name = 'vpn_endpoints' AND column_name IN ('external_config_fingerprint', 'external_verified_at', 'health_checked_at');"
sudo -u postgres psql --no-psqlrc --tuples-only --no-align \
  --dbname="$RESTORE_DB" \
  --command="SELECT count(*) FROM vpn_control_operations WHERE state IN ('pending', 'claimed', 'running');"
```

Expected: restore succeeds; the access-key count matches the predeployment
acceptance count; the required endpoint-column count is `3`; active operation
count is `0`; the application remains healthy; and both backup/watchdog timers
remain disabled. Keep the failed production `.partial` set until this rehearsal
succeeds; its later deletion is a separate explicit cleanup.
