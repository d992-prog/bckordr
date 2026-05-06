# Live Control Worker Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an in-process end-to-end harness that runs control worker-runtime endpoints and the worker client/runner together in tests.

**Architecture:** Use a test async SQLite database plus a FastAPI app with `get_db` override and `ASGITransport`. Seed worker/account/contact/domain/run/task state directly, then execute a real worker scenario through HTTP endpoints and assert final database state.

**Tech Stack:** FastAPI, SQLAlchemy async, httpx ASGITransport, pytest, asyncio

---

### Task 1: Test harness foundation

**Files:**
- Create: `backend/tests/test_runtime_harness.py`

- [ ] Add test helpers for async engine, session factory, seeded records, and test app overrides.
- [ ] Add the first failing end-to-end worker success scenario.
- [ ] Run targeted pytest and confirm the test fails for the intended missing behavior or missing test support.

### Task 2: Minimal production support for testability

**Files:**
- Modify: `worker/app/control_client.py` if dependency injection is needed
- Modify: `worker/app/runner.py` only if the harness exposes a real testability gap

- [ ] Add only the smallest injection seam needed for the harness.
- [ ] Keep runtime behavior unchanged outside tests.

### Task 3: Verify the success path

**Files:**
- Modify: `backend/tests/test_runtime_harness.py`

- [ ] Assert heartbeat, next-task fetch, ack, live planned-rps update, success result, sibling cancellation, and final domain/run status.
- [ ] Run targeted pytest and make it green.
- [ ] Run the full backend suite.
