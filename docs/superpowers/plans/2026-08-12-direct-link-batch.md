# Direct Link Batch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an XLSX-driven direct-link batch entry to the Link Generation view while preserving the existing image-search batch workflow.

**Architecture:** Keep one batch lifecycle and export pipeline, but add an explicit `batch_mode` and a direct-link item/collector strategy. Normalize direct collector manifests into the existing export model, then call `WorkflowRunner` in `competitor_reference` mode with the first collected main image as identity.

**Tech Stack:** Python 3, `openpyxl`, existing Playwright collectors, React/TypeScript/Vite, `unittest`.

## Global Constraints

- Direct-link batches support mixed Taobao, Tmall, and JD links.
- Prefer columns named 商品链接, 对标链接, or URL; otherwise inspect the first column.
- Unknown links fail their row and never fall back to image search.
- Direct collection never searches or supplements from another product.
- Collect main/SKU/detail images, product parameters, SKU color/specification/price, and original video URL.
- Missing asset types are skipped; available types continue.
- Batch direct-link generation always uses `competitor_reference` mode.
- Existing single-link modes and image-search spreadsheet behavior remain unchanged.
- Do not create a Git commit unless the user requests it.

---

### Task 1: Direct-Link Workbook Parsing

**Files:**
- Modify: `batch_workflow.py`
- Test: `test_batch_workflow.py`

**Interfaces:**
- Produces: `extract_direct_link_items(workbook_path: Path) -> list[DirectLinkBatchItem]`
- Produces: `DirectLinkBatchItem(sequence, row_number, source_url, platform, title)`

- [ ] Write tests for named columns, first-column fallback, mixed supported platforms, and invalid rows.
- [ ] Run the focused tests and confirm they fail before implementation.
- [ ] Implement strict URL normalization and platform classification without image-search fallback.
- [ ] Run focused tests and the existing batch parser tests.

### Task 2: Direct Collector Strategy and Manifest Normalization

**Files:**
- Modify: `batch_workflow.py`
- Test: `test_batch_workflow.py`

**Interfaces:**
- Produces: `DirectLinkCollector.collect(item, item_root) -> Path`
- Produces: `normalize_direct_manifest(source_manifest, target_path) -> Path`

- [ ] Write tests asserting the direct collector command calls `store_insight_collector.py` with the row URL and never invokes `same_item_collector.py`.
- [ ] Write tests for normalized images, parameters, SKU variants, price, and video URL.
- [ ] Run tests and confirm failure.
- [ ] Implement the collector strategy and normalization.
- [ ] Run focused tests.

### Task 3: Shared Batch Lifecycle With Explicit Mode

**Files:**
- Modify: `batch_workflow.py`
- Modify: `web_app.py`
- Test: `test_batch_workflow.py`
- Test: `test_web_app.py`

**Interfaces:**
- `BatchRunner(..., batch_mode: Literal["image_search", "direct_link"] = "image_search")`
- `POST /api/batch-upload` consumes multipart field `batch_mode`.
- App status exposes `batch.mode`, `valid`, `invalid`, and `unsupported`.

- [ ] Write tests for explicit mode selection, sequential checkpointing, invalid-row continuation, challenge pause, and missing-type generation.
- [ ] Run tests and confirm failure.
- [ ] Route image batches to the existing collector and link batches to `DirectLinkCollector`.
- [ ] Resolve the first collected main image and call `WorkflowRunner.run(..., generation_mode="competitor_reference")` only for available types.
- [ ] Preserve current stop/resume/checkpoint semantics and export each completed product.
- [ ] Run backend tests.

### Task 4: Link-View Batch UI

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Generated: `web/index.html`
- Generated: `web/assets/*`

**Interfaces:**
- Link view gains an explicit Single Link / Batch Link segmented control.
- Existing Spreadsheet Generation view continues to upload with `batch_mode=image_search`.
- Batch Link upload uses `batch_mode=direct_link` and displays recognition counts and progress.

- [ ] Add TypeScript status fields and explicit upload mode.
- [ ] Add the batch-link panel under Link Generation without showing product-image mode controls.
- [ ] Keep start/continue/stop/open-folder controls shared with batch status.
- [ ] Run `npm run check` and `npm run build`.

### Task 5: End-to-End Verification

**Files:**
- Test: all existing test modules

- [ ] Run all Python unit tests.
- [ ] Run Python compilation checks.
- [ ] Run TypeScript check and production build.
- [ ] Restart only the web service and confirm the current source process serves the latest frontend.
- [ ] Upload a fixture workbook through HTTP, verify direct-link mode/status, and confirm no task starts merely from status polling.
- [ ] Inspect `git diff --check` and ensure local secrets remain unstaged.
