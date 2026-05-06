# Runtime Visibility RPS Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose allocator output in the control API and panel so each domain shows its runtime minimum, desired, and allocated RPS.

**Architecture:** Add a small runtime snapshot helper in the backend that derives per-domain metrics from effective strategies, active runs, active tasks, and worker capacity. Extend control response schemas to carry those fields, then render them in the domains and attacks tables.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic, React, TypeScript

---

### Task 1: Backend runtime snapshot helper

**Files:**
- Modify: `backend/tests/test_attack_runtime.py`
- Modify: `backend/app/services/attack_runtime.py`

- [ ] Add failing tests for per-domain runtime metric calculation.
- [ ] Run targeted pytest and confirm failure.
- [ ] Implement the helper and minimal supporting types.
- [ ] Re-run targeted pytest and confirm pass.

### Task 2: Control schemas and route wiring

**Files:**
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`

- [ ] Add response fields for domain and attack runtime metrics.
- [ ] Populate those fields from the runtime snapshot helper in list endpoints.
- [ ] Add or extend tests where coverage is practical.

### Task 3: Frontend visibility

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`

- [ ] Extend frontend types for the new runtime metrics.
- [ ] Show `min / desired / allocated` RPS in the domains view.
- [ ] Show matching phase/runtime details in the attacks view.
- [ ] Run frontend build verification.
