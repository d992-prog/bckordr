# Domain Manual Override Rules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a domain in `manual_override` mode own its own rules/phases instead of only overriding minimum RPS.

**Architecture:** Add domain-scoped rule and phase tables tied to `DomainRuleOverride`, load them through the existing strategy runtime abstraction, and expose CRUD + preview via control API and a basic domains-panel editor.

**Tech Stack:** FastAPI, SQLAlchemy async, Pydantic, React, TypeScript

---

### Task 1: Data model and runtime resolution

**Files:**
- Modify: `backend/app/db/models.py`
- Modify: `backend/app/db/migrations.py`
- Modify: `backend/app/services/strategy_runtime.py`
- Modify: `backend/app/services/attack_runtime.py`
- Test: `backend/tests/test_strategy_runtime.py`

- [x] Add failing runtime tests for manual override rule/phase resolution.
- [x] Add domain override rule/phase models and migrations.
- [x] Load domain override rules/phases into `resolve_effective_strategy` and runtime planning.
- [x] Re-run targeted tests and make them green.

### Task 2: Control API for domain override CRUD and preview

**Files:**
- Modify: `backend/app/schemas/control.py`
- Modify: `backend/app/api/routes/control.py`
- Test: `backend/tests/test_control_strategy_api.py`

- [x] Add request/response schemas for domain override settings, rules, phases.
- [x] Add endpoints to create/update override settings, rules, phases and preview a domain override schedule.
- [x] Add targeted API tests and make them pass.

### Task 3: Domains UI editor

**Files:**
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.tsx`

- [x] Add frontend types and API calls for domain override CRUD/preview.
- [x] Add a basic manual override editor in the domains tab.
- [x] Show preview and current override rules/phases for the selected domain.
- [x] Run frontend build verification.
