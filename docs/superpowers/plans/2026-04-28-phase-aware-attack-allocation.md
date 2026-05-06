# Phase-Aware Attack Allocation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make control-side attack planning aware of zone rules and zone phases so worker tasks receive strategy-derived windows and target RPS instead of relying only on legacy per-domain window fields.

**Architecture:** This phase keeps the control server as the single runtime brain. The backend resolves the effective strategy for a domain, picks the active or next rule window for the current drop day, computes the active phase target RPS, and uses that data when creating `AttackRun` and `WorkerTask`. Worker payloads then carry the derived target and optional registrar base URL so the worker executes the intended runtime plan without owning zone logic.

**Tech Stack:** FastAPI, SQLAlchemy asyncio, Pydantic, pytest, React/TypeScript unchanged for this slice, Python worker runtime with httpx

---

## File Structure

- Modify: `backend/app/services/strategy_runtime.py`
  Add helpers for effective rule selection, current phase resolution, and phase-derived target RPS.
- Modify: `backend/app/services/attack_runtime.py`
  Replace legacy-only planning assumptions with strategy-aware attack planning and task RPS targeting.
- Modify: `backend/app/api/routes/worker_runtime.py`
  Include registrar `api_base_url` and strategy-derived task fields in the worker payload.
- Modify: `backend/app/schemas/runtime.py`
  Extend worker task payload schema with phase/runtime fields used by workers.
- Modify: `worker/app/control_client.py`
  Parse the extended task payload.
- Modify: `worker/app/gandi.py`
  Respect registrar-specific base URL instead of using only the hardcoded Gandi endpoint.
- Modify: `worker/app/runner.py`
  Consume the richer task payload while keeping the current execution loop intact.
- Test: `backend/tests/test_strategy_runtime.py`
  Add tests for active rule selection, phase selection, and target RPS derivation.
- Test: `backend/tests/test_attack_runtime.py`
  Add tests for strategy-aware run planning.
- Test: `backend/tests/test_auto_scheduler.py`
  Keep auto-planning aligned with strategy-aware selection.

## Execution Notes

- This slice does not implement the full weighted allocator from the long-term spec.
- This slice does replace the current “flat worker target_rps everywhere” assumption for newly planned tasks.
- Existing manual window fields on `DropDomain` remain as fallback when no usable zone rule exists.
- Git commit steps are intentionally omitted because this workspace is not yet a git repository.
