# Domain Drop Catcher

Control/worker architecture for domain drop catching with a central control panel and separate worker agents.

## What This Repository Contains

- `backend/`
  Control server API, database models, owner/admin auth, Telegram settings, attack planning, worker runtime endpoints
- `frontend/`
  React control panel for domains, workers, registrar accounts, contact profiles, attacks, and settings
- `worker/`
  Separate worker agent project that polls control, receives tasks, waits for the attack window, and executes Gandi registration attempts
- `deploy/`
  Sample `systemd` units for both control and worker services

## Current Architecture

### Control server

The control server is now the main project runtime. It keeps:

- drop domains with required `drop_date`
- worker nodes with RPS limits and health data
- registrar accounts such as Gandi PAT credentials
- contact profiles for registration payloads
- attack runs
- worker tasks
- attack events

The control API now exposes:

- `/api/control/overview`
- `/api/control/domains`
- `/api/control/domains/import`
- `/api/control/domains/{id}/dry-run`
- `/api/control/workers`
- `/api/control/registrar-accounts`
- `/api/control/contact-profiles`
- `/api/control/attacks`
- `/api/control/attacks/start`
- `/api/control/attacks/rebalance`
- `/api/control/attacks/stop`
- `/api/control/tasks`
- `/api/control/events`

It also exposes worker runtime endpoints:

- `/api/worker-runtime/heartbeat`
- `/api/worker-runtime/tasks/next`
- `/api/worker-runtime/tasks/{id}/ack`
- `/api/worker-runtime/tasks/{id}/status`
- `/api/worker-runtime/tasks/{id}/progress`
- `/api/worker-runtime/tasks/{id}/result`

### Worker agent

The worker agent lives in its own subproject under `worker/`.

Current worker scaffold includes:

- environment-based configuration
- control client with heartbeat, task polling, task ack, task status, result reporting
- Gandi request builder and registration workflow with `POST /domain/domains` plus `createstatus` follow-up
- Gandi support for top-level `extra_parameters` and contact-level `extra_parameters`
- async runtime loop that:
  - polls control
  - waits for `planned_start_at`
  - executes repeated registration attempts inside the attack window
  - reports success/failure back to control

This is the beginning of the worker-side implementation, not the finished high-frequency runtime yet.

## Product Decisions Already Applied

- Public registration is disabled in this build.
- Manual user creation from the admin API is disabled in this build.
- The login screen remains visible.
- Domains now require `drop_date`.
- Default `.fr` window is modeled as `31:59 + 61 seconds`, adjustable per domain.
- Capacity is represented as `current_rps`, `target_rps`, and `max_rps`.
- Strategy defaults to priority-first planning on the control side.
- Real create-flow validation is now done per domain with `Dry run`, not only at account level.

## Backend Setup

```bash
cd backend
python -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

Create `backend/.env` from `backend/.env.example` and fill at least:

```env
DB_URL=postgresql+asyncpg://postgres:postgres@localhost:5432/dropcatcher
SESSION_SECRET_KEY=change-me-session-secret
OWNER_LOGIN=owner
OWNER_PASSWORD=change-me-owner-password
```

Optional worker runtime allowlist knobs for nginx-managed origin protection:

```env
WORKER_RUNTIME_ALLOWLIST_PATH=/etc/nginx/includes/domain-drop-worker-allowlist.conf
WORKER_RUNTIME_ALLOWLIST_RELOAD_COMMAND=systemctl reload nginx
```

Run control API:

```bash
cd backend
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Frontend Setup

```bash
cd frontend
npm install
npm run build
```

When `frontend/dist` exists, FastAPI serves the built frontend automatically.

## Worker Setup

```bash
cd worker
python -m venv .venv
. .venv/bin/activate
pip install -e .
```

Create `worker/.env` from `worker/.env.example` and configure:

```env
CONTROL_BASE_URL=https://control.example.com
WORKER_ID=1
CONTROL_TOKEN=change-me
POLL_INTERVAL_SECONDS=2
HEARTBEAT_INTERVAL_SECONDS=5
SIMULATE_MODE=false
GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5
GANDI_STATUS_POLL_MAX_ATTEMPTS=8
```

Run worker:

```bash
cd worker
python -m app.main
```

## Deployment Units

- `deploy/domain-drop-monitor.service`
  Control server service
- `deploy/domain-drop-worker.service`
  Worker agent service

## Important Notes

- The previous in-process domain checking engine is no longer the main architecture.
- The repository is now centered on control-side attack planning and a separate worker agent.
- Basic automatic rebalancing is now present: when a worker reports success, sibling tasks for that domain are cancelled and freed workers can be reassigned by control to other active runs. Advanced strategy tuning is still a next step.
- Fresh verification in this workspace has already been run with:
  - backend tests
  - frontend production build
  - backend app import
  - worker import
