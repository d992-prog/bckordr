# Weighted Capacity Allocator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upgrade control-side capacity allocation so active domains first receive a minimum guaranteed RPS and then split the remaining capacity by weighted priority, while still respecting worker compatibility and per-worker ceilings.

**Architecture:** The new allocator stays in the control runtime as a pure planning layer. It computes per-domain target RPS from the available worker pool, then maps those targets onto workers and tasks. Existing scheduling and phase-aware target derivation remain inputs, but the old worker-count-first heuristic is replaced by domain-level RPS budgeting.

**Tech Stack:** Python 3.11+, FastAPI backend runtime, SQLAlchemy asyncio, pytest

---

## File Structure

- Modify: `backend/app/services/attack_runtime.py`
  Add pure allocation helpers for per-domain minima, weighted remainder splitting, and worker-to-domain RPS plans.
- Modify: `backend/app/services/control_runtime.py`
  Keep runtime cycle using the new allocation-driven planner and rebalance functions.
- Test: `backend/tests/test_attack_runtime.py`
  Add allocator tests for guaranteed minima, weighted remainder, and ceiling-respecting splits.

## Execution Notes

- This slice optimizes runtime power distribution; it does not add new UI.
- Existing strategy phases still determine a domain's desired target, but the allocator decides what each domain can actually get from the live pool.
- Worker/IP safety remains hard-capped by `target_rps` and `max_rps`.
