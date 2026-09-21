# Preserve concurrent VPN subscription extensions

> Use test-driven-development and verification-before-completion. This is a
> bounded correction to the approved lifecycle integration audit, not the durable
> operation dispatcher and not permission to deploy unfinished endpoint support.

**Observed cause:** lifecycle selects expired ORM objects and then changes their
status without locking or rereading. An extension committed between selection
and flush can therefore retain its new date but acquire `expired` status.

**Minimal correction:** select candidate IDs in ascending order, then select each
subscription with `FOR UPDATE` and `populate_existing` before changing status.
Recheck status/date while holding the lock, including refreshed candidates which
are no longer due so later key processing cannot reuse old ORM state. Count only actual
transitions. Keep the caller's transaction/commit boundary and existing key
processing; no SSH, new dependencies, scheduler, or schema changes.

This fixes only expiration selection. It does not claim a global lock order,
cross-process remote mutation serialization, stale key finalization protection,
or implementation of the still-required control operation queue.

## Checklist

- [x] RED: real SQLite regression commits an extension after candidate selection
  through a second session; lifecycle must preserve active status and new date.
- [x] GREEN: implement locked, refreshed, conditional expiration and rerun existing
  lifecycle/subscription tests.
- [x] PostgreSQL: two real connections, pause candidate selection, hold an extension
  row lock, start lifecycle lock acquisition, commit extension, verify lifecycle
  skips it. Also cover cancellation, stale ORM state and unchanged due rows.
- [x] Independent review, Ruff, full backend verification. Use only the authorized
  isolated synthetic test PostgreSQL, stop and remove it afterward.

Files: `backend/app/services/vpn_lifecycle.py`,
`backend/tests/test_vpn_lifecycle.py`, and focused PostgreSQL tests in
`backend/tests/test_vpn_lifecycle_postgres.py` if the existing test infrastructure
cannot express the independent-connection proof in the original file.

Evidence: both original SQLite regressions failed with an incorrect expiration
count before the fix; lifecycle/subscription suites then passed41tests. The four
PostgreSQL cases passed in102.31s, including actual `pg_blocking_pids` evidence
that lifecycle waited for the independent editor. The engine used a private
random schema in the authorized synthetic database via a loopback SSH tunnel.
Independent review approved and reran41localtests. Final whole-backend run with
the real isolated PostgreSQL passed1601tests with1POSIX-only skip on Windows;
Ruff passed. The exact temporary cluster `/tmp/veltrix-portal-test-18gp4z3o` was
stopped and deleted, its port45065 closed, and the control service stayed active.
Only generated synthetic data were removed; rerunning tests regenerates them.
