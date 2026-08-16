# Batch Generation Limits Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent oversized batch image plans from overwhelming the relay while preserving explicit counts and missing-only retries.

**Architecture:** Bound source-to-task expansion in `load_manifest_tasks()` so every caller receives safe SKU/detail plans. Use a generation-specific worker resolver for the final executor, leaving identity-analysis and caller-requested lower concurrency behavior intact.

**Tech Stack:** Python 3.11, `unittest`, `concurrent.futures.ThreadPoolExecutor`.

## Global Constraints

- Default non-manual SKU task count is clamped to 3 through 8.
- Default detail task count keeps the available source count up to a maximum of 15.
- Explicit valid SKU/detail counts are honored.
- Image generation uses no more than 10 concurrent workers.
- Existing five-attempt network retry behavior is unchanged.
- Current application service must not be restarted.

---

### Task 1: Bound Default SKU and Detail Task Counts

**Files:**
- Modify: `image_workflows.py:1382-1446`
- Test: `test_image_workflows.py`

**Interfaces:**
- Consumes: `load_manifest_tasks(manifest_path, categories, max_main_images, max_sku_images, max_detail_images)`.
- Produces: bounded `list[ImageTask]` with stable one-based ordinals.

- [ ] **Step 1: Write the failing task-count test**

Create a manifest containing 12 SKU and 36 detail source images. Assert omitted limits produce 8 SKU tasks and 15 detail tasks, while explicit counts of 5 produce exactly 5 each.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& 'D:\product-image-workflow-review-fixes\.venv\Scripts\python.exe' -m unittest test_image_workflows.IdentityAnalysisConcurrencyTests.test_default_sku_and_detail_task_counts_are_bounded
```

Expected: detail task count is 36 instead of 15.

- [ ] **Step 3: Implement default count clamps**

Use `min(8, max(3, len(sources)))` for non-manual SKU tasks and `min(15, len(sources))` for detail tasks when no explicit limit is supplied. Keep manual SKU count and explicit values unchanged.

- [ ] **Step 4: Run focused and module tests**

Run the focused test, then:

```powershell
& 'D:\product-image-workflow-review-fixes\.venv\Scripts\python.exe' -m unittest test_image_workflows
```

Expected: all tests pass.

### Task 2: Bound Image Generation Workers

**Files:**
- Modify: `image_workflows.py:1527-1536,1835-1836`
- Test: `test_image_workflows.py`

**Interfaces:**
- Produces: `resolve_generation_worker_count(task_count: int, concurrency: int | None) -> int`.
- Preserves: `resolve_worker_count()` and `resolve_identity_worker_count()` semantics.

- [ ] **Step 1: Write the failing worker-count test**

Assert 46 tasks with default concurrency use 10 workers, 6 tasks use 6 workers, and explicit concurrency 4 uses 4 workers.

- [ ] **Step 2: Run the focused test and verify RED**

Run:

```powershell
& 'D:\product-image-workflow-review-fixes\.venv\Scripts\python.exe' -m unittest test_image_workflows.IdentityAnalysisConcurrencyTests.test_generation_workers_are_dynamic_with_a_safe_upper_bound
```

Expected: `resolve_generation_worker_count` is missing.

- [ ] **Step 3: Implement and wire the resolver**

Add `IMAGE_GENERATION_CONCURRENCY = 10`, return `min(IMAGE_GENERATION_CONCURRENCY, resolve_worker_count(...))`, and use the new resolver only for the final `image-workflow` executor.

- [ ] **Step 4: Verify retry reuse remains intact**

Run:

```powershell
& 'D:\product-image-workflow-review-fixes\.venv\Scripts\python.exe' -m unittest test_batch_workflow.BatchWorkflowTests.test_batch_retry_only_requests_incomplete_generation_ordinals
```

Expected: pass without changing retry planning.

- [ ] **Step 5: Run full verification and commit**

Run all Python tests, `py_compile`, `git diff --check`, and frontend `npm.cmd run check`. Commit only source, tests, and these docs; do not include runtime dependencies or credentials.
