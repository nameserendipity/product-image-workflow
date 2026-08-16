# Model API Setup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist both model API keys locally and require an initial setup modal before users can run workflows.

**Architecture:** Keep secrets server-side in the gitignored `local_settings.json`. The backend restores and atomically updates keys, while the React frontend renders a single reusable modal driven only by readiness booleans from `/api/status`.

**Tech Stack:** Python standard library, existing HTTP server, React, TypeScript, Vite, unittest.

## Global Constraints

- Do not restart or terminate the currently running service.
- Never return API key values from an HTTP response.
- Preserve all unrelated `local_settings.json` fields.
- Saving keys must not start collection or generation.

---

### Task 1: Backend persistence

**Files:**
- Modify: `web_app.py`
- Test: `test_web_app.py`

**Interfaces:**
- Produces: `save_model_api_keys(vision_api_key: str, image_api_key: str) -> None`
- Produces: `load_model_api_keys() -> tuple[str, str]`

- [ ] Add failing tests for preserving existing settings, restoring keys into a new `AppState`, and rejecting incomplete saves.
- [ ] Run the focused tests and confirm they fail because persistence is absent.
- [ ] Implement atomic configuration updates and startup restoration.
- [ ] Update `_set_api_keys` to persist before changing memory and to return readiness booleans only.
- [ ] Run focused backend tests and confirm they pass.

### Task 2: Frontend setup gate

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: `AppStatus.vision_api_ready` and `AppStatus.image_api_ready`
- Consumes: `POST /api/api-keys`

- [ ] Add a failing source-level regression test for the automatic modal and visible API status trigger.
- [ ] Run the focused test and confirm it fails.
- [ ] Add modal state that opens after the first status response when either key is missing.
- [ ] Replace duplicate sidebar forms with buttons that open the shared modal.
- [ ] Add responsive modal and topbar status styles with keyboard focus visibility.
- [ ] Run frontend typecheck and production build.

### Task 3: Verification and documentation

**Files:**
- Modify: `README.md`
- Modify: `操作说明书.md`

- [ ] Document local persistence and the first-run modal without exposing keys.
- [ ] Run `python -m unittest discover -p 'test_*.py'` using the project virtual environment.
- [ ] Run frontend typecheck and production build.
- [ ] Run `git diff --check` and inspect the final diff for unrelated changes.
- [ ] Confirm port `8765` still belongs to the original service process and was not restarted.
