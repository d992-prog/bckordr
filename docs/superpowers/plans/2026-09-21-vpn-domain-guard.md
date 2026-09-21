# VPN domain-work guard correction

> **For agentic workers:** Use superpowers:executing-plans or subagent-driven-development; retain the existing customer-portal worktree.

**Goal:** Prevent VPN mutations and node removal while assigned domain work is planned, running or verifying.

**Architecture:** Correct the existing shared database query and reuse it in decommission. No new lock, scheduler, dependency or schema. This fixes status coverage, not the separate cross-transaction reservation race.

**Tech Stack:** Existing SQLAlchemy, SQLite-backed pytest integration tests and Ruff.

## Steps

- [x] RED: parameterize `test_select_vpn_node_excludes_worker_with_active_attack`
  in `backend/tests/test_vpn_policy.py` for `(running,running)`,
  `(planned,planned)`, `(running,planned)`, `(verifying,cancelled)`,
  `(verifying,succeeded)`, `(verifying,failed)`. Use the parameters when seeding
  `AttackRun.status` and `WorkerTask.status`; existing assertions require the
  busy worker be excluded and the free worker remain selectable.
- [x] RED: parameterize the no-partial-changes decommission test and API409 test
  in `backend/tests/test_worker_decommission.py` for `(running,running)`,
  `(planned,planned)`, `(verifying,cancelled)`, `(verifying,succeeded)`.
  Keep assertions that worker credentials and existing key stay unchanged.
- [x] GREEN: import `and_, or_` in `backend/app/services/vpn_policy.py`, replace
  the active-work predicate in `active_attack_worker_ids` with:

```python
or_(
    AttackRun.status == "verifying",
    and_(
        AttackRun.status.in_(("planned", "running")),
        WorkerTask.status.in_(("planned", "queued", "running")),
    ),
)
```

- [x] GREEN: replace the duplicated task query in
  `backend/app/services/worker_decommission.py` with
  `if worker_id in await active_attack_worker_ids(session):` and retain the
  existing conflict error. Remove its unused `AttackRun`/`WorkerTask` imports.
- [x] Verify terminal-run negative cases do not block an otherwise eligible
  worker. Run both files plus lifecycle/maintenance regressions; Ruff and
  `git diff --check`. Independent review, then explicit-file commit.

Commands from backend:

```powershell
$guardScratch = Join-Path (Get-Location).Path ('.pytest_cache/domain-guard-' + [guid]::NewGuid().ToString('N'))
.\.venv\Scripts\python.exe -m pytest tests/test_vpn_policy.py tests/test_worker_decommission.py -q --basetemp $guardScratch
.\.venv\Scripts\python.exe -m ruff check app/services/vpn_policy.py app/services/worker_decommission.py tests/test_vpn_policy.py tests/test_worker_decommission.py
```

No network calls, production mutation or claim of PostgreSQL race coverage.

Evidence: RED9failed/19passed for the missing planned/verifying guards;
GREEN28passed, then terminal-run negative cases added and the selector test
renamed `test_vpn_node_selection_respects_domain_work_phase`. Combined policy,
decommission, lifecycle and maintenance regression run:56passed; attack-runtime
regressions14passed; Ruff clean. Independent diff review approved and reran the
31 policy/decommission cases successfully. No cross-transaction reservation
or control-operation queue was implemented by this small correction.
