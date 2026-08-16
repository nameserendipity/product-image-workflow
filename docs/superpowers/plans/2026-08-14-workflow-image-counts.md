# Workflow Image Counts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow chat requests to set main, SKU, and detail generation counts together or independently without changing collection behavior.

**Architecture:** Parse explicit count phrases deterministically in `AgentSession`, store optional SKU/detail overrides beside the existing main count, and pass all three values through the web state into task construction. Explicit counts cycle existing source images; absent overrides retain current dynamic defaults.

**Tech Stack:** Python dataclasses and unittest, React/TypeScript state types.

## Global Constraints

- Do not restart the running service.
- Do not commit Git changes.
- Main count range is 1-999, SKU count range is 1-8, and detail count range is 1-15.
- Explicit counts affect generation only; collection behavior is unchanged.

---

### Task 1: Parse And Persist Per-Workflow Counts

**Files:**
- Modify: `agent_flow.py`
- Test: `test_agent_flow.py`

**Interfaces:**
- Produces: `AgentSession.max_sku_images`, `AgentSession.max_detail_images`, and deterministic workflow count parsing.

- [ ] Write failing tests for unified, separate, partial, and invalid count requests.
- [ ] Run the focused tests and confirm they fail because per-workflow count fields are absent.
- [ ] Add the two session fields, parse explicit phrases, validate ranges, and expose counts in replies.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Build The Requested Number Of Tasks

**Files:**
- Modify: `image_workflows.py`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: optional main, SKU, and detail generation counts.
- Produces: `load_manifest_tasks(..., max_sku_images=None, max_detail_images=None)` and matching `WorkflowRunner.run(...)` parameters.

- [ ] Write failing tests proving explicit SKU/detail counts cycle sources and absent overrides retain defaults.
- [ ] Run the focused tests and confirm the new keyword arguments are unsupported.
- [ ] Add validated count parameters and pass them through `WorkflowRunner.run()`.
- [ ] Run the focused tests and confirm they pass.

### Task 3: Carry Counts Through Web State

**Files:**
- Modify: `web_app.py`
- Modify: `frontend/src/types.ts`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: count values stored in `AgentSession`.
- Produces: persisted count state, count-sensitive task signatures, and generation calls containing all three counts.

- [ ] Write failing tests for persistence, task signatures, and runner invocation.
- [ ] Run the focused tests and confirm they fail on missing count propagation.
- [ ] Restore, sign, and pass both count overrides; update the TypeScript state type.
- [ ] Run Python and frontend checks and confirm they pass.

### Task 4: Regression Verification

**Files:**
- Verify only.

**Interfaces:**
- Consumes: all changes above.
- Produces: reproducible test evidence without touching the live service.

- [ ] Run `python -m unittest test_agent_flow test_image_workflows test_web_app`.
- [ ] Run `python -m py_compile agent_flow.py image_workflows.py web_app.py`.
- [ ] Run `npm run check` in `frontend`.
- [ ] Run `git diff --check` and inspect the scoped diff.
