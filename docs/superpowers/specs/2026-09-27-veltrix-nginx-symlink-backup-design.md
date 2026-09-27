# Veltrix Nginx Symlink Backup Design

## Status

Approved design for the production backup blocker found on 2026-09-27. This
change is limited to safely snapshotting the regular-file targets of standard
Nginx configuration symlinks. It does not enable the backup timer, watchdog,
fleet health, public trial or payments.

## Problem

The validated backup walks `/etc/nginx` with descriptor-relative, no-follow
filesystem operations and currently rejects every symbolic link. A real Ubuntu
Nginx installation uses regular-file symlinks in both `sites-enabled` and
`modules-enabled`, so the first production run completed `pg_dump` and then
failed closed while copying Nginx. It left a private `.partial` set and did not
replace the successful-backup marker.

The existing blanket rejection protects against a link that escapes the source
tree and copies an unrelated secret. The fix must preserve that boundary while
supporting the two normal Nginx link locations.

## Chosen Approach

Materialize an approved symlink's target as a regular file at the link's
relative path in the backup snapshot. The snapshot and manifest therefore keep
their existing format: restore copies ordinary files and does not need a new
symlink-aware tool or manifest version.

Only direct links to regular files under either of these roots are approved:

- the configured Nginx source tree, normally `/etc/nginx`;
- Ubuntu's Nginx module configuration directory,
  `/usr/share/nginx/modules-available`.

The module directory is a deliberate deployment-platform constant. Veltrix's
tracked service and installation documentation already target Ubuntu. Supporting
another distribution requires a separately reviewed path change rather than a
generic arbitrary-root setting.

All other links remain invalid, including links to directories, devices,
sockets, pipes, missing paths, other symlinks and regular files outside the two
approved roots.

## Secure Copy Flow

The existing directory walk remains descriptor-relative and never follows a
link during scanning.

When it encounters a link in the Nginx tree, the backup:

1. records the link inode metadata and target text through the already-open
   parent descriptor;
2. rejects cancellation of normal components and trailing directory syntax
   before lexical normalization, without resolving filesystem links;
3. requires the normalized target to remain below an approved root;
4. opens every target parent component with `O_NOFOLLOW` and requires the final
   target to be a regular file;
5. copies the target through the existing bounded descriptor-copy function to
   the link's relative destination path;
6. rechecks the link inode and target text after the copy, while the existing
   file checks verify that the target itself did not change.

A race, unsupported link, changed target or path escape returns the existing
bounded `backup_source_invalid` or `backup_source_changed` error. The partial
set stays diagnostic and the latest-success marker stays untouched.

Frontend trees, environment files, systemd units, backup roots and successful
snapshot validation continue to reject symlinks exactly as before. Symlink
materialization is enabled only for the Nginx source call.

## Alternatives Rejected

### Store live symlinks in the backup

This would require weakening snapshot-tree validation and would leave absolute
links pointing outside the private backup. Generic file tools could follow them
later. The extra security surface is unnecessary.

### Add a symlink manifest and custom restore procedure

This preserves topology but changes the backup format, validation and recovery
runbook. Materialized regular files restore the effective Nginx configuration
with less code and fewer failure modes.

### Build a separate flattened Nginx staging directory

An external snapshot job introduces another mutable tree, cleanup lifecycle and
staleness boundary. Performing the bounded materialization inside the existing
snapshot transaction is smaller and keeps one source of truth.

## Tests

Test-driven implementation starts with a failing POSIX regression that creates
the same shapes as production:

- `sites-enabled/name -> ../sites-available/name`;
- `modules-enabled/name -> <approved module root>/name`.

The successful snapshot must contain regular files at both enabled paths with
the exact target bytes and matching manifest hashes. Existing malicious external
link coverage must continue to fail without copying its target.

Additional focused cases cover a broken link, directory link, link-to-link,
lexical escape and link mutation during copy. The complete backup test file,
Ruff, diff checks and the Linux release gate must pass before deployment.

## Production Acceptance

1. Merge the reviewed patch and fast-forward production from `45a58da`.
2. Keep `VPN_BACKUP_ENABLED=false` and both timers disabled.
3. Run one transient manual backup that restores the flag to `false` on every
   exit path.
4. Require zero `.partial` promotion, a valid `latest-success.json`, matching
   `backup.json`, full manifest verification and `pg_restore --list` success.
5. Confirm the materialized `sites-enabled` and `modules-enabled` entries are
   regular private files with the expected hashes, without printing their
   contents.
6. Complete an isolated local PostgreSQL restore and remove only the disposable
   database after verification.
7. Leave the timer disabled. Enabling scheduled backup remains a separate gate.

The failed production partial set is retained until the corrected backup and
restore rehearsal succeed. Its removal is a separate explicit cleanup action.
