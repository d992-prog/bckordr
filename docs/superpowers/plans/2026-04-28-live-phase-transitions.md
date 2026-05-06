# Live Phase Transition Runtime Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let active worker tasks pick up new `planned_rps` values while a run is already in progress so zone phases can change effective firepower inside one attack window.

**Architecture:** Control remains the source of truth. Each control runtime cycle recalculates the active phase target for running or queued tasks and updates `WorkerTask.planned_rps` plus `AttackRun.planned_rps`. Workers already poll task status, so we extend that status payload with the current `planned_rps` and let the worker execution loop adapt its dispatch interval and concurrency without restarting the task.

**Tech Stack:** FastAPI, SQLAlchemy asyncio, Pydantic, pytest, Python worker runtime with httpx and asyncio

---

## File Structure

- Modify: `backend/app/services/attack_runtime.py`
  Add refresh logic for active task targets and run-level planned RPS updates.
- Modify: `backend/app/services/control_runtime.py`
  Invoke the refresh step every cycle before rebalance.
- Modify: `backend/app/schemas/runtime.py`
  Extend task status response with current `planned_rps`.
- Modify: `backend/app/api/routes/worker_runtime.py`
  Return live `planned_rps` in task status payload.
- Modify: `worker/app/control_client.py`
  Parse the richer status response.
- Modify: `worker/app/runner.py`
  Adjust dispatch interval, concurrency, and `_current_capacity_rps` when control changes `planned_rps`.
- Test: `backend/tests/test_attack_runtime.py`
  Add tests for live task target refresh.
- Test: `backend/tests/test_strategy_runtime.py`
  Reuse current strategy tests as guard rails for phase resolution.

## Execution Notes

- This slice does not yet add domain-level override phase sets.
- This slice keeps worker polling-based coordination; no websockets or push channel.
- Existing tasks without strategy rules continue to use their current fallback behavior.
