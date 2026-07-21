# Load Testing

## Purpose

This project can now be load-tested without touching the real Gandi API.

Use `SIMULATE_MODE=true` on workers to generate controlled request loops while control still runs the real scheduler, allocator, rebalance logic and worker supervision.

## What Is Covered

- control auto-planning
- live `planned_rps` updates
- worker polling and progress reporting
- weighted allocator and rebalance
- worker stall detection and automatic reassignment
- success-path or failure-only worker behavior, depending on simulate settings

## Control Settings

Recommended control-side knobs for load runs:

- `CONTROL_SCHEDULER_INTERVAL_SECONDS=1`
- `WORKER_SUPERVISOR_INTERVAL_SECONDS=5`
- `WORKER_STALL_THRESHOLD_SECONDS=15`

Lower supervisor values make stalled workers drop out faster during tests.

## Worker Simulate Settings

Available worker-side simulate controls:

- `SIMULATE_MODE=true`
- `SIMULATE_LATENCY_MS=20`
- `SIMULATE_JITTER_MS=10`
- `SIMULATE_SUCCESS_RATE=0`
- `SIMULATE_SUCCESS_STATUS_CODE=200`
- `SIMULATE_FAILURE_STATUS_CODE=503`
- `SIMULATE_RANDOM_SEED=12345`
- `GANDI_CREATE_STATUS_POLL_ENABLED=false`
- `GANDI_STATUS_POLL_INTERVAL_SECONDS=0.5`
- `GANDI_STATUS_POLL_MAX_ATTEMPTS=8`

## Recommended Test Profiles

### 1. Pure Load / No Success

Use this when you want sustained load and no domains to finish early.

```env
SIMULATE_MODE=true
SIMULATE_LATENCY_MS=20
SIMULATE_JITTER_MS=10
SIMULATE_SUCCESS_RATE=0
SIMULATE_FAILURE_STATUS_CODE=503
```

### 2. Rare Success / Rebalance Under Pressure

Use this when you want occasional wins so freed workers move to other domains.

```env
SIMULATE_MODE=true
SIMULATE_LATENCY_MS=20
SIMULATE_JITTER_MS=10
SIMULATE_SUCCESS_RATE=0.01
SIMULATE_SUCCESS_STATUS_CODE=200
SIMULATE_FAILURE_STATUS_CODE=503
```

### 3. Fast Success Smoke

Use this only for a short smoke check.

```env
SIMULATE_MODE=true
SIMULATE_LATENCY_MS=5
SIMULATE_JITTER_MS=0
SIMULATE_SUCCESS_RATE=1
SIMULATE_SUCCESS_STATUS_CODE=200
SIMULATE_FAILURE_STATUS_CODE=503
```

## Minimum Practical Scenario

For a meaningful load test:

- run 1 control server
- register 3-10 workers
- set each worker `target_rps` to `16`
- import 20-200 due-today domains
- use one hourly window that is active soon
- start with `SIMULATE_SUCCESS_RATE=0`

This gives you stable pressure on:

- scheduler
- allocator
- task creation
- worker heartbeats
- progress writes
- runtime tables and UI

## Failure / Stall Test

To test worker-loss recovery:

1. Start several workers in simulate mode.
2. Let attacks become active.
3. Stop one worker process completely.
4. Wait longer than `WORKER_STALL_THRESHOLD_SECONDS`.
5. Confirm in control that:
   - worker becomes `offline`
   - its active task becomes `failed`
   - another online worker is rebalanced onto the same domain

## What This Does Not Prove

Simulate mode does not validate:

- real registrar latency
- real HTTP keep-alive behavior against Gandi
- real API throttling from registrar side
- real response semantics from Gandi
- dry-run correctness for real create payloads

It is for system load and orchestration testing, not registrar correctness.

## Final Real-API Step

After simulate load passes:

1. pick a small number of workers
2. lower `target_rps`
3. disable simulate mode
4. run control-side `Dry run` on the real domains first
5. use a controlled dry scenario or a non-critical live scenario
6. watch events, task statuses, worker heartbeat freshness and latency
