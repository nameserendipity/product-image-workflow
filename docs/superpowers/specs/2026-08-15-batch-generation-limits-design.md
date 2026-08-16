# Batch Generation Limits and EOF Resilience

## Problem

A batch item with 36 collected detail images created 36 detail generation tasks plus 10 main tasks. Visual analysis was limited to 10 concurrent requests per category, but image generation used one worker per task. The relay closed several TLS responses early with `UNEXPECTED_EOF_WHILE_READING` after all five retries.

## Decision

Apply limits at the shared task-planning and worker-planning boundaries:

- Non-manual SKU generation defaults to 3 through 8 tasks.
- Detail generation keeps the available source count up to a maximum of 15 tasks.
- Explicit SKU/detail counts remain supported but must stay within 8/15.
- Image generation remains dynamic below the limit and uses at most 10 concurrent workers.
- Visual analysis keeps its existing per-category limit of 10.
- Existing successful batch records remain reusable; retries request only failed or missing ordinals.

This is preferred over limiting only `BatchRunner`, because single-link, supplement, and future callers all use the shared planners. It is preferred over a new persistent server-side queue because that would add unrelated lifecycle and recovery complexity.

## Data Flow

1. `load_manifest_tasks()` reads all available source images.
2. It resolves requested counts, applying default SKU/detail caps when counts are omitted.
3. `WorkflowRunner.run()` creates tasks from the bounded plan.
4. `resolve_generation_worker_count()` selects `min(task_count, requested_concurrency, 10)` as applicable.
5. Existing record planning continues to exclude already completed ordinals from retries.

## Error Handling

The existing five-attempt retry and exponential backoff remain unchanged. The fix reduces avoidable relay pressure; it does not hide persistent relay failures or mark failed images successful.

## Verification

- 36 detail sources with no explicit count produce 15 tasks.
- More than 8 SKU sources with no explicit count produce 8 tasks.
- Explicit smaller counts are preserved.
- 46 generation tasks use 10 workers; smaller jobs use their task count; an explicit lower concurrency is honored.
- Existing completed records are not regenerated during batch retry.
- Full Python and frontend checks pass.
