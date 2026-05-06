# Zone Rule Phase Preview Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add editable zone rules, editable zone phases, and an effective schedule preview so the multizone strategy model becomes operational in the control panel.

**Architecture:** This phase extends the zone strategy foundation with rule and phase CRUD plus pure service-layer schedule preview functions. The backend remains the source of truth for rule resolution and preview output, while the frontend strategy tab becomes the operator surface for creating and inspecting zone schedules.

**Tech Stack:** FastAPI, SQLAlchemy asyncio, Pydantic, pytest, React, TypeScript, Vite

---

## File Structure

- Modify: `backend/app/services/strategy_runtime.py`
  Add rule matching for hourly/daily/weekly/one-time schedules and preview generation.
- Modify: `backend/app/schemas/control.py`
  Add zone rule, phase, and preview schemas.
- Modify: `backend/app/api/routes/control.py`
  Add CRUD endpoints for rules/phases and preview endpoint.
- Modify: `frontend/src/api.ts`
  Add types and API methods for rules/phases/preview.
- Modify: `frontend/src/App.tsx`
  Add UI for creating rules, adding phases, and viewing preview output.
- Test: `backend/tests/test_strategy_runtime.py`
  Add rule resolution and preview tests.
- Test: `backend/tests/test_control_strategy_api.py`
  Add schema default tests for rule and phase payloads.

## Execution Notes

- We are intentionally not implementing the full attack-phase allocator in this phase.
- This phase should keep rule editing simple but real.
- Git commit steps in the generic template are skipped here because this workspace is not yet a git repository.
