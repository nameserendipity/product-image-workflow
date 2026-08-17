# Disable Post-Generation Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every image task call the image model once and publish any technically valid generated image without post-generation model review or corrective regeneration.

**Architecture:** Preserve the existing collection, visual-analysis, prompt-composition, image-decoding, upload, and export stages. Simplify `WorkflowRunner._run_task()` so all generation modes share one direct generate-and-write path, then remove the now-unused generated-image review method and review-specific tests and metadata.

**Tech Stack:** Python 3, `unittest`, `unittest.mock`, Pillow, existing OpenAI-compatible vision and image API clients.

## Global Constraints

- Remove post-generation model review from every image-generation workflow.
- Keep source collection, product/reference visual analysis, generation-prompt composition, image decoding and file validation, cancellation, OSS upload, workbook export, and operational failure reporting unchanged.
- Call the image model exactly once per task.
- Do not emit `quality_review` or `generation_attempts` in new records.
- Human review owns semantic defects after export.
- Preserve historical `analysis.json` compatibility without migration.

---

### Task 1: Remove The Semantic Review And Corrective Retry Path

**Files:**
- Modify: `test_agent_flow.py:1369-1710`
- Modify: `test_image_workflows.py:301-377,379-389,848-858`
- Modify: `image_workflows.py:1242-1323,2378-2541`

**Interfaces:**
- Consumes: `WorkflowRunner._run_task(task, product_image, output_root, generation_mode, ...) -> dict[str, Any]`, `ImageClient.generate(images: list[Path], prompt: str) -> bytes`, and `_write_generated_image(image_bytes: bytes, output_path: Path) -> None`.
- Produces: one direct image-generation request per task, a final image at `<output_root>/<category>/<ordinal>.jpg`, and a completed record without semantic-review metadata.

- [x] **Step 1: Replace review-retry tests with a failing single-generation test**

In `EcommerceWorkflowFidelityTests`, remove the tests that expect a failed review to regenerate, a second rejected candidate to fail, or cancellation during review. Replace them with this behavior test:

```python
def test_own_product_runner_generates_once_without_semantic_review(self):
    with TemporaryDirectory() as directory:
        root = Path(directory)
        product = root / "product.png"
        reference = root / "reference.jpg"
        product.write_bytes(b"product")
        reference.write_bytes(b"reference")
        runner = WorkflowRunner(self.settings)

        with (
            patch("image_workflows.VisionClient.analyze", return_value=self.analysis),
            patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()) as generate,
            patch("image_workflows.VisionClient.review_generated", create=True) as review,
        ):
            record = runner._run_task(
                ImageTask("main", 1, reference), product, root / "generated"
            )

        self.assertEqual(record["status"], "completed")
        self.assertTrue(Path(record["output_path"]).is_file())
        self.assertEqual(generate.call_count, 1)
        review.assert_not_called()
        self.assertNotIn("quality_review", record)
        self.assertNotIn("generation_attempts", record)
        self.assertEqual(list((root / "generated" / "main").glob(".*.candidate-*.jpg")), [])
```

Update `test_own_product_runner_omits_identity_image_when_reference_has_no_product` to patch `VisionClient.review_generated` with `create=True`, assert it is not called, and assert `ImageClient.generate` is called once. Keep the existing assertion that product-free generation receives only the reference image.

Keep `test_competitor_reference_runner_does_not_use_own_product_quality_review`, but patch the method with `create=True` so the test remains valid after the method is removed.

- [x] **Step 2: Run the focused tests and verify the new own-product assertion fails**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_own_product_runner_generates_once_without_semantic_review `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_own_product_runner_omits_identity_image_when_reference_has_no_product `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_competitor_reference_runner_does_not_use_own_product_quality_review -v
```

Expected: the own-product tests fail because the current runner calls `VisionClient.review_generated`; the competitor-reference test passes.

- [x] **Step 3: Simplify `_run_task()` to one direct generation**

In `image_workflows.py`, remove `quality_review`, `generation_attempts`, `candidate_paths`, the two-attempt loop, temporary candidate paths, semantic-review calls, reviewer correction instructions, review metadata, and candidate cleanup.

After selecting `generation_images` and creating `output_path`, use one shared path for every generation mode:

```python
stage = "调用 gpt-image-2 生图"
self._emit(task, "generating", stage_label=stage)
image_bytes = ImageClient(self.settings).generate(generation_images, prompt)
if self.cancel_event.is_set():
    self._emit(task, "cancelled")
    return {"category": task.category, "ordinal": task.ordinal, "status": "cancelled"}
_write_generated_image(image_bytes, output_path)
```

Leave the existing completed-record fields and exception handling intact, except remove all assignments to `quality_review` and `generation_attempts`.

- [x] **Step 4: Remove the unused generated-image review API**

Delete `VisionClient.review_generated()` from `image_workflows.py` and delete `GeneratedImageReviewTests` from `test_image_workflows.py`.

Remove the obsolete `VisionClient.review_generated` patches from `ImageRequestRetryTests.setUp`, `IdentityAnalysisConcurrencyTests.setUp`, and `EcommerceWorkflowFidelityTests.setUp`. Do not change their unrelated jitter or analysis setup.

- [x] **Step 5: Run focused tests and verify they pass**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_own_product_runner_generates_once_without_semantic_review `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_own_product_runner_omits_identity_image_when_reference_has_no_product `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_competitor_reference_runner_does_not_use_own_product_quality_review -v
```

Expected: all three tests pass, each generation path calls the image model once, and no review call occurs.

- [x] **Step 6: Verify no production review path or review metadata remains**

Run:

```powershell
rg -n "review_generated|成品语义审核|quality_review|generation_attempts|candidate-" image_workflows.py test_agent_flow.py test_image_workflows.py
```

Expected: only assertions proving absent metadata or the test-time `create=True` review sentinels may remain; `image_workflows.py` contains no matches.

- [x] **Step 7: Run the full regression suite**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -v
```

Expected: all tests pass.

- [x] **Step 8: Run static validation**

Run:

```powershell
& '.\.venv\Scripts\python.exe' -m py_compile image_workflows.py test_agent_flow.py test_image_workflows.py
git diff --check
```

Expected: both commands exit successfully with no syntax or whitespace errors.

- [x] **Step 9: Commit the implementation**

```powershell
git add -- image_workflows.py test_agent_flow.py test_image_workflows.py docs/superpowers/plans/2026-08-17-disable-post-generation-review.md
git commit -m "fix: remove post-generation model review"
```
