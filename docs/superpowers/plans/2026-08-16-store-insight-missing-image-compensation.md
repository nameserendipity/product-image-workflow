# Store Insight Missing Image Compensation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Recover missing main, SKU, or detail images after Store Insight returns an incomplete all-files archive.

**Architecture:** Extend the existing Taobao/Tmall collection orchestration in `collect_store_insight_payload`. Reuse `download_store_insight_zip` and `materialize`, sharing hashes and counters across the initial archive and compensation archives.

**Tech Stack:** Python 3, Playwright sync API, `unittest`, `unittest.mock`

## Global Constraints

- Do not change Douyin, Kuaishou, image generation, OSS, frontend, or service lifecycle behavior.
- Attempt only requested categories that produced zero records.
- Attempt each missing category once and preserve partial success on failure.

---

### Task 1: Missing-category compensation

**Files:**
- Modify: `store_insight_collector.py`
- Test: `test_collector.py`

**Interfaces:**
- Consumes: `download_store_insight_all_zip`, `download_store_insight_zip`, and `materialize`.
- Produces: merged image records plus optional `asset_compensation_errors` metadata from `collect_store_insight_payload`.

- [ ] **Step 1: Write failing orchestration tests**

Add tests where the all-files materialization omits one or more requested categories. Assert final real records and metadata, with the browser download boundary mocked because it is external.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `.venv\Scripts\python.exe -m unittest test_collector.CollectorTests.test_collect_store_insight_payload_compensates_each_missing_requested_image_type test_collector.CollectorTests.test_collect_store_insight_payload_records_failed_compensation`

Expected: FAIL because `collect_store_insight_payload` does not call the single-category downloader or report compensation errors.

- [ ] **Step 3: Implement minimal compensation orchestration**

Create one shared `known_hashes` set and `counters` dictionary, materialize the all-files archive with them, then loop over missing requested categories in `ASSET_TYPES` order. Catch ordinary download/materialization errors per category and record their text without discarding prior records; re-raise `RiskControlDetected`. Treat a compensation archive that adds zero records as a category failure.

- [ ] **Step 4: Run focused and full tests**

Run the focused command from Step 2, then `.venv\Scripts\python.exe -m unittest discover -p "test_*.py"`.

Expected: all tests pass.

- [ ] **Step 5: Run static checks**

Run: `.venv\Scripts\python.exe -m py_compile store_insight_collector.py test_collector.py`

Run: `git diff --check -- store_insight_collector.py test_collector.py docs/superpowers/specs/2026-08-16-store-insight-missing-image-compensation-design.md docs/superpowers/plans/2026-08-16-store-insight-missing-image-compensation.md`

Expected: both commands exit successfully.
