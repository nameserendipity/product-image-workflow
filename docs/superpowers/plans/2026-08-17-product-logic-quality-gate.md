# Product Logic Quality Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent gift/primary-product inversion, copy-to-canvas contradictions, and physically impossible product-material effects in own-product generation.

**Architecture:** Extend the existing own-product analysis JSON with product hierarchy, dispensing state, physical effects, and copy evidence contracts. Compose those fields into deterministic generation constraints, then review each own-product candidate with the vision model and allow one corrective retry before failing closed.

**Tech Stack:** Python 3.12, `unittest`, Pillow, existing OpenAI-compatible chat-completions and image-edit clients.

## Global Constraints

- Modify only the own-product analysis, prompt, generation, and directly related tests.
- Do not change collection, spreadsheet parsing, workbook layout, OSS publishing, or competitor-reference generation behavior.
- Gifts and buy-gift promises are removed when no user-owned gift identity is supplied.
- Selling points must be both supported by Image 1 and visibly evidenced in the final canvas.
- Product material cannot touch or emerge from a closed product with no visible outlet.
- Permit one corrective regeneration after a failed semantic review, then fail closed.

---

### Task 1: Own-Product Analysis Contract and Prompt Rules

**Files:**
- Modify: `image_workflows.py:652-703`
- Modify: `image_workflows.py:817-1153`
- Modify: `image_workflows.py:1284-1485`
- Test: `test_image_workflows.py:27-181`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: existing `analysis` dictionaries returned by `VisionClient.analyze(...)`.
- Produces: validated fields `product_fingerprint.dispensing_state`, `reference_visual_brief.primary_replaceable_product_unit_count`, `gift_or_bonus_elements`, `physical_effects`, and `copy_plan.selling_points[*].required_visual_evidence`.

- [ ] **Step 1: Write failing prompt and validation tests**

Add focused tests that require the own-product prompt to contain an exact primary-unit directive, gift removal, final-canvas copy evidence, and the no-outlet physical rule. Add a validation test whose otherwise valid response omits the new fields and must trigger the existing analysis retry.

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_image_workflows.PromptCompositionTests test_agent_flow.EcommerceWorkflowFidelityTests
```

Expected: failures because the new analysis fields and prompt directives do not exist.

- [ ] **Step 3: Implement the minimum analysis contract and prompt directives**

Extend `_validate_own_product_analysis`, the own-product `VisionClient.analyze` instruction, and `compose_generation_prompt`. Keep the new fields nested inside existing top-level objects so workbook records and downstream consumers remain compatible.

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the same command and expect all selected tests to pass.

### Task 2: Generated-Image Semantic Reviewer

**Files:**
- Modify: `image_workflows.py:1156-1559`
- Test: `test_image_workflows.py`

**Interfaces:**
- Consumes: `product_image: Path`, `reference_image: Path`, `generated_image: Path`, `analysis: dict[str, Any]`, and `category: str`.
- Produces: `VisionClient.review_generated(...) -> dict[str, Any]` with `passed: bool`, `violations: list[dict[str, str]]`, and `retry_instruction: str`.

- [ ] **Step 1: Write failing reviewer schema tests**

Test a valid passing response, a failing response with normalized violations, and rejection of a malformed response. Assert that the request includes product, reference, and generated images in that order and includes copy-evidence and physical-causality criteria.

- [ ] **Step 2: Run reviewer tests and verify RED**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_image_workflows.GeneratedImageReviewTests
```

Expected: failure because `review_generated` does not exist.

- [ ] **Step 3: Implement the reviewer**

Add `VisionClient.review_generated` using the existing `_request_json` and `_image_data_url` helpers. Validate strict JSON and fail closed on malformed responses; do not add a second independent retry loop because `_run_task` owns the one permitted corrective generation retry.

- [ ] **Step 4: Run reviewer tests and verify GREEN**

Run the same command and expect all reviewer tests to pass.

### Task 3: Quality-Gated Generation and One Corrective Retry

**Files:**
- Modify: `image_workflows.py:2191-2313`
- Test: `test_image_workflows.py`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: `VisionClient.review_generated` from Task 2 and existing `ImageClient.generate`.
- Produces: completed records with `quality_review` and `generation_attempts`; failed records with the last quality review and no final image path.

- [ ] **Step 1: Write failing workflow tests**

Add one test where the first review fails and the second passes, asserting two image generations, two reviews, a corrective instruction in the second prompt, a completed final file, and `generation_attempts == 2`. Add one test where both reviews fail, asserting a failed record and absence of the final ordinal output. Add one competitor-reference test asserting that this new own-product review path is not invoked.

- [ ] **Step 2: Run workflow tests and verify RED**

Run the new test cases directly with `python -m unittest` and expect failures because generation is currently saved and marked complete without review.

- [ ] **Step 3: Implement candidate review and retry**

Write each candidate to a task-local temporary JPEG, review it, append the reviewer's correction to the original prompt for one retry, and promote the passing candidate atomically with `Path.replace`. Remove temporary candidates in `finally`. Preserve existing cancellation and failure metadata behavior.

- [ ] **Step 4: Run workflow tests and verify GREEN**

Run the new test cases and expect all to pass.

### Task 4: Regression and Full Verification

**Files:**
- Modify: `docs/superpowers/specs/2026-08-17-product-logic-quality-gate-design.md` only if implementation revealed an inconsistency.

**Interfaces:**
- Consumes: Tasks 1-3.
- Produces: verified branch ready for review.

- [ ] **Step 1: Run focused image-workflow tests**

```powershell
.\.venv\Scripts\python.exe -m unittest test_image_workflows test_agent_flow
```

Expected: all tests pass with no new warnings.

- [ ] **Step 2: Run the complete Python suite**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: all tests pass; the baseline contains 433 tests before the new cases are added.

- [ ] **Step 3: Inspect the diff and branch state**

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: only the design, plan, `image_workflows.py`, and directly related tests are changed; no whitespace errors.

- [ ] **Step 4: Commit the implementation**

```powershell
git add docs/superpowers/specs/2026-08-17-product-logic-quality-gate-design.md docs/superpowers/plans/2026-08-17-product-logic-quality-gate.md image_workflows.py test_image_workflows.py test_agent_flow.py
git commit -m "fix: enforce product image logic quality gate"
```
