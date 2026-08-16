# Short Extraction Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Store Insight ZIP extraction failures caused by Windows 260-character paths.

**Architecture:** Extract archives into an OS temporary directory, process files inside that context, and copy only final normalized assets into the existing output tree. Cleanup is owned by `TemporaryDirectory`.

**Tech Stack:** Python 3.12, `tempfile.TemporaryDirectory`, `unittest`, PyInstaller, PowerShell.

## Global Constraints

- Preserve the final output directory structure and manifest paths.
- Preserve ZIP traversal checks, filename sanitization, and duplicate handling.
- Do not change Windows registry settings or delete existing outputs.

---

### Task 1: Use a short temporary extraction directory

**Files:**
- Modify: `store_insight_collector.py:736-774`
- Test: `test_collector.py`

**Interfaces:**
- Consumes: `materialize(zip_path, asset_type, output_root, known_hashes, ...)`
- Produces: the same list of asset records and final files as before

- [ ] **Step 1: Write the failing regression test**

Create a real ZIP, wrap `safe_extract` only to record its target, call `materialize()` with a deeply nested output root, and assert the extraction target is outside the output root, the final copied image exists, and the temporary extraction target is removed afterward.

- [ ] **Step 2: Verify RED**

Run: `.venv\Scripts\python.exe -m unittest test_collector.CollectorTests.test_materialize_uses_short_temporary_extraction_directory -v`

Expected: FAIL because the current extraction target is `output_root/_work/all_extracted`.

- [ ] **Step 3: Implement the minimal change**

Import `TemporaryDirectory`, wrap the current materialization loop in `with TemporaryDirectory(prefix="product-image-extract-")`, and pass that path to `safe_extract`.

- [ ] **Step 4: Verify tests**

Run the targeted test, all collector tests, then `.venv\Scripts\python.exe -m unittest discover -v`.

- [ ] **Step 5: Build and deploy**

Run `powershell -ExecutionPolicy Bypass -File build_release.ps1 -Version v18-20260812`, verify the rebuilt collector executable, update the currently tested release directory without deleting its outputs or settings, restart the application, and confirm its HTTP endpoint responds.
