# Competitor Reference Copy Compliance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make shared direct-link main and detail generation preserve ordinary selling copy, remove only program-approved explicit risks, and render evidence-backed copy instead of producing bare product images.

**Architecture:** Keep the change inside the existing `image_workflows.py` analysis and prompt pipeline. Add one deterministic review boundary between vision output and prompt composition, then make the `competitor_reference` main/detail branch consume the reviewed removal list and approved `copy_plan`; keep SKU and `own_product` behavior unchanged.

**Tech Stack:** Python 3, standard-library `unittest`, existing vision Chat Completions client, existing `gpt-image-2` generation pipeline.

## Global Constraints

- Apply one shared policy to direct-link Taobao, Tmall, JD, and Douyin workflows; do not add a Douyin-only prompt branch.
- Default to preserving ordinary or uncertain copy.
- Remove only explicit off-product competitor branding, store/watermark, patent/certification, origin/import, medical treatment, absolute/ranking, and unsupported sales/price/data claims.
- Do not treat nutrition, vitality, enhancement, improvement, synergy, absorption, flavor, suitability, or usage wording as removable by itself.
- Preserve `活力生活嚼出来`, `牛磺酸协同吸收`, `增强免疫力`, and `快速改善疲劳` when present in current-task inputs; never invent them when absent.
- Product and packaging pixels, labels, logos, structure, quantity, color, and viewing angle remain immutable.
- Main images require one headline and one to three selling points; detail images preserve or safely refill useful information zones.
- SKU images remain free of off-product marketing copy.
- No OCR post-generation review and no changes to collection, image quantities, concurrency, export, OSS, browser, frontend, or API request formats.
- Do not restart or stop the running web service during code and unit-test work.

---

### Task 1: Deterministic Direct-Reference Risk Review

**Files:**
- Modify: `image_workflows.py:119-128`
- Modify: `image_workflows.py:564-640`
- Test: `test_agent_flow.py:850-1005`

**Interfaces:**
- Consumes: `analysis["compliance_risks"]`, a list or normalized single object returned by vision analysis.
- Produces: `_review_direct_reference_compliance_risks(analysis: dict[str, Any]) -> None`, `analysis["reported_compliance_risks"]`, and reviewed `analysis["compliance_risks"]`.
- Later prompt composition continues reading `analysis["compliance_risks"]`; no caller signature changes.

- [ ] **Step 1: Add failing tests for explicit removal and default preservation**

Add focused tests to `EcommerceWorkflowFidelityTests`:

```python
def test_direct_reference_review_removes_only_explicit_off_product_risks(self):
    analysis = json.loads(json.dumps(self.analysis))
    patent = {
        "source_image": "Image 1",
        "original_text": "国家发明专利",
        "risk_code": "patent_or_certification",
        "location": "off_product editable: top-right badge",
        "decision": "remove",
        "reason": "Explicit patent claim",
        "removal_instruction": "Remove the patent badge and rebuild the background",
    }
    analysis["compliance_risks"] = [patent]

    _normalize_direct_reference_analysis(analysis, "main", None)

    self.assertEqual(analysis["reported_compliance_risks"], [patent])
    self.assertEqual(analysis["compliance_risks"], [patent])

def test_direct_reference_review_preserves_uncertain_and_ordinary_selling_copy(self):
    for text, risk_code in (
        ("活力生活嚼出来", "medical_treatment"),
        ("牛磺酸协同吸收", "medical_treatment"),
        ("增强免疫力", "medical_treatment"),
        ("快速改善疲劳", "medical_treatment"),
        ("每份90mg维C", "unsupported_sales_price_data"),
    ):
        with self.subTest(text=text):
            analysis = json.loads(json.dumps(self.analysis))
            analysis["compliance_risks"] = [{
                "source_image": "Image 1",
                "original_text": text,
                "risk_code": risk_code,
                "location": "off_product editable: headline",
                "decision": "remove",
                "reason": "Model was uncertain about this selling point",
                "removal_instruction": "Remove it",
            }]

            _normalize_direct_reference_analysis(analysis, "main", None)

            self.assertEqual(analysis["compliance_risks"], [])
```

Also cover an on-product patent phrase, an unknown risk code, a missing reason, and `decision: preserve`; each must produce an empty reviewed removal list while retaining the item in `reported_compliance_risks`.

Add `_normalize_direct_reference_analysis` to the existing `from image_workflows import (...)` block in `test_agent_flow.py`; do not add a second module import style.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_review_removes_only_explicit_off_product_risks `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_review_preserves_uncertain_and_ordinary_selling_copy -v
```

Expected: FAIL because `_normalize_direct_reference_analysis` does not create `reported_compliance_risks` and does not review risk fields.

- [ ] **Step 3: Implement the fixed risk taxonomy and review helper**

Add constants near `ANALYSIS_REQUIRED_FIELDS`:

```python
DIRECT_REFERENCE_REMOVABLE_RISK_CODES = frozenset({
    "competitor_brand",
    "store_or_watermark",
    "patent_or_certification",
    "origin_or_import",
    "medical_treatment",
    "absolute_or_ranking",
    "unsupported_sales_price_data",
})

DIRECT_REFERENCE_EXPLICIT_RISK_MARKERS = {
    "patent_or_certification": ("专利", "认证", "检测报告", "检验报告"),
    "origin_or_import": ("进口", "原产国", "原产地", "德国", "美国", "日本", "韩国", "马来西亚"),
    "medical_treatment": ("治疗", "治愈", "根治", "药到病除", "疾病", "临床治疗"),
    "absolute_or_ranking": ("第一", "最佳", "最强", "唯一", "永久", "绝对", "100%", "保证"),
    "unsupported_sales_price_data": ("销量", "已售", "到手价", "原价", "现价", "折扣", "提升", "降低", "%"),
}
```

Implement the review without modifying the original risk objects:

```python
def _review_direct_reference_compliance_risks(analysis: dict[str, Any]) -> None:
    reported = [dict(item) for item in analysis.get("compliance_risks", []) if isinstance(item, dict)]
    reviewed: list[dict[str, Any]] = []
    for item in reported:
        code = str(item.get("risk_code") or "").strip()
        location = str(item.get("location") or "").strip().lower()
        decision = str(item.get("decision") or "").strip().lower()
        original_text = str(item.get("original_text") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if (
            code not in DIRECT_REFERENCE_REMOVABLE_RISK_CODES
            or not location.startswith("off_product editable:")
            or decision != "remove"
            or not original_text
            or not reason
        ):
            continue
        markers = DIRECT_REFERENCE_EXPLICIT_RISK_MARKERS.get(code)
        if markers and not any(marker in original_text for marker in markers):
            continue
        reviewed.append(item)
    analysis["reported_compliance_risks"] = reported
    analysis["compliance_risks"] = reviewed
```

Call it from `_normalize_direct_reference_analysis` immediately after `_normalize_compliance_risks` and before prompt fallback construction. Brand/store risks rely on structured vision classification plus all required fields; the five claim families additionally require an explicit marker in `original_text`.

- [ ] **Step 4: Run all direct-reference analysis tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_agent_flow.EcommerceWorkflowFidelityTests -v
```

Expected: all tests pass after updating the legacy single-risk normalization test to assert that a risk without the new required fields is reported but preserved.

- [ ] **Step 5: Commit Task 1 only**

```powershell
git add -- image_workflows.py test_agent_flow.py
git commit -m "fix: review direct reference compliance risks"
```

---

### Task 2: Tighten The Vision Analysis Contract

**Files:**
- Modify: `image_workflows.py:1168-1204`
- Test: `test_agent_flow.py:956-1007`

**Interfaces:**
- Consumes: current `VisionClient.analyze(..., generation_mode="competitor_reference")` inputs.
- Produces: the same top-level analysis schema plus structured fields inside each compliance item; no API caller changes.
- Depends on: Task 1 risk codes and review helper.

- [ ] **Step 1: Add failing prompt-contract tests**

Extend the existing direct-reference analysis prompt test:

```python
def test_direct_reference_analysis_requests_machine_reviewable_risks(self):
    # Use the existing temporary identity/reference images and patched _request_json.
    instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"]
    self.assertIn("risk_code", instruction)
    self.assertIn("original_text", instruction)
    self.assertIn("decision", instruction)
    self.assertIn("reason", instruction)
    self.assertIn("default decision is preserve", instruction.lower())
    self.assertIn("nutrition, vitality, enhancement, improvement, synergy", instruction.lower())
    self.assertIn("do not classify them as removable by wording alone", instruction.lower())
```

Assert that the instruction enumerates all seven `DIRECT_REFERENCE_REMOVABLE_RISK_CODES` and says that current-image presence is evidence for preserving existing copy, not permission to invent absent claims.

- [ ] **Step 2: Run the new contract test and verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_analysis_requests_machine_reviewable_risks -v
```

Expected: FAIL because the current instruction requests only free-form `type`, `location`, and `removal_instruction` fields.

- [ ] **Step 3: Replace the ambiguous compliance paragraph**

In the `competitor_reference` analysis instruction:

- Require `original_text`, `risk_code`, `decision`, `reason`, and `removal_instruction` for each risk item.
- Enumerate the seven fixed risk codes exactly.
- State that the default decision is preserve.
- State that nutrition, vitality, enhancement, improvement, synergy, absorption, flavor, suitability, and usage wording are not removal reasons by themselves.
- State that visible product name, category, flavor, quantity, net content, specification, ingredients, and nutrient values are ordinary factual copy.
- State that `活力生活嚼出来`, `牛磺酸协同吸收`, `增强免疫力`, and `快速改善疲劳` must be preserved when present in current-task inputs and must not be invented when absent.
- Require phrase-level classification for mixed text blocks and whole-container removal only when the container is dedicated entirely to approved prohibited content.
- Keep the existing immutable-product and off-product-only edit boundary unchanged.

- [ ] **Step 4: Run vision-analysis and normalization tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_analysis_requests_machine_reviewable_risks `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_analysis_protects_product_surface_from_compliance_edits `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_analysis_normalizes_layout_zone_copy_plan -v
```

Expected: PASS, proving the new contract did not weaken product protection or legacy copy-plan normalization.

- [ ] **Step 5: Commit Task 2 only**

```powershell
git add -- image_workflows.py test_agent_flow.py
git commit -m "fix: classify direct reference copy conservatively"
```

---

### Task 3: Render Approved Main And Detail Copy

**Files:**
- Modify: `image_workflows.py:697-793`
- Test: `test_image_workflows.py:26-145`
- Test: `test_agent_flow.py:932-955`
- Test: `test_agent_flow.py:1334-1370`

**Interfaces:**
- Consumes: reviewed `analysis["compliance_risks"]` and validated `analysis["copy_plan"]`.
- Produces: the existing `compose_generation_prompt(...) -> str` result with category-specific copy precedence.
- No function signature changes.

- [ ] **Step 1: Add failing main/detail prompt tests**

Add to `PromptCompositionTests`:

```python
def test_direct_reference_main_renders_approved_copy_plan(self):
    prompt = image_workflows.compose_generation_prompt(
        self._analysis(True), "main", "competitor_reference"
    )
    self.assertIn("PRODUCT COPY FROM ANALYSIS", prompt)
    self.assertIn("SELLING POINT FROM ANALYSIS", prompt)
    self.assertIn("one to three selling points are mandatory", prompt.lower())
    self.assertNotIn("Analysis copy plan for audit only", prompt)
    self.assertNotIn("Do not render new copy from the analysis copy_plan", prompt)

def test_direct_reference_detail_renders_approved_copy_plan(self):
    prompt = image_workflows.compose_generation_prompt(
        self._analysis(True), "detail", "competitor_reference"
    )
    self.assertIn("PRODUCT COPY FROM ANALYSIS", prompt)
    self.assertIn("SELLING POINT FROM ANALYSIS", prompt)
    self.assertNotIn("Analysis copy plan for audit only", prompt)
```

Strengthen the existing SKU prompt test to require `Analysis copy plan for audit only` and reject the main/detail mandatory-copy clause.

- [ ] **Step 2: Run prompt tests and verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_image_workflows.PromptCompositionTests.test_direct_reference_main_renders_approved_copy_plan `
  test_image_workflows.PromptCompositionTests.test_direct_reference_detail_renders_approved_copy_plan `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_sku_prompts_remove_off_product_copy_and_keep_product_labels -v
```

Expected: the two new tests FAIL because the current direct-reference branch labels `copy_plan` audit-only and forbids rendering it; the SKU regression test passes.

- [ ] **Step 3: Make copy policy category-specific inside the direct-reference branch**

Build one local `direct_reference_copy_policy` before the branch return:

```python
direct_reference_copy_policy = (
    "SKU copy policy: use copy_plan only to audit deletion zones; never render off-product copy."
    if category == "sku"
    else
    "Approved ecommerce copy policy:\n"
    "- Preserve ordinary existing off-product copy unless it appears in the application-approved removal list.\n"
    "- Render the exact approved copy_plan headline, optional subheadline, and one to three selling points in the reference information hierarchy.\n"
    "- When prohibited wording is removed, refill the useful information zone with approved copy instead of leaving a blank or bare layout.\n"
    "- Do not paraphrase, invent, translate, or add copy outside the approved copy_plan."
)
```

Use this policy in the returned prompt, label `compliance_risks` as the application-approved removal list, and label `copy_plan` as approved renderable copy for main/detail. Remove these contradictory main/detail instructions:

- `Do not render new copy from the analysis copy_plan.`
- `Analysis copy plan for audit only.`
- The hard-precedence statement that lets the off-product audit policy override approved main/detail copy.

Retain those restrictions for SKU only. Keep all product freeze, model refresh, structural safety, background, detail-view, and on-product protection rules unchanged.

- [ ] **Step 4: Run all prompt and fidelity tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_image_workflows.PromptCompositionTests `
  test_agent_flow.EcommerceWorkflowFidelityTests -v
```

Expected: PASS. Main/detail prompts contain approved copy and no audit-only contradiction; SKU and product-fidelity assertions remain intact.

- [ ] **Step 5: Commit Task 3 only**

```powershell
git add -- image_workflows.py test_image_workflows.py test_agent_flow.py
git commit -m "fix: render approved direct reference copy"
```

---

### Task 4: Persist Audit Evidence And Verify Shared Routing

**Files:**
- Modify: `test_agent_flow.py:1431-1470`
- Modify only if the new assertion exposes a gap: `image_workflows.py:1848-1867`
- Test: `test_batch_workflow.py:1280-1300`

**Interfaces:**
- Consumes: the normalized analysis returned by `VisionClient.analyze`.
- Produces: generated record `analysis` containing both `reported_compliance_risks` and reviewed `compliance_risks`.
- Direct-link platform routing continues selecting `generation_mode="competitor_reference"` in `batch_workflow.py`; no routing code change is expected.

- [ ] **Step 1: Add audit-persistence and shared-routing assertions**

Extend `test_direct_reference_runner_generates_with_current_task_image_first`. Return a copied analysis fixture containing both reported and reviewed lists from the patched `VisionClient.analyze`, then assert:

```python
self.assertEqual(record["analysis"]["reported_compliance_risks"], reported_risks)
self.assertEqual(
    record["compliance_risks"],
    record["analysis"]["compliance_risks"],
)
```

Extend the direct-link batch test so representative Taobao, Tmall, JD, and Douyin `DirectLinkBatchItem.platform` values all reach `ImageWorkflowRunner.run(..., generation_mode="competitor_reference")`. Use mocks only; do not open a browser or call either model API.

- [ ] **Step 2: Run the focused persistence and routing tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest `
  test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_runner_generates_with_current_task_image_first `
  test_batch_workflow.BatchWorkflowTests -v
```

Expected: PASS if the runner already persists the full analysis object. If the persistence assertion fails, copy `reported_compliance_risks` into the existing record's nested `analysis` without adding a new output file or changing the public API.

- [ ] **Step 3: Commit Task 4 tests and any minimal persistence fix**

```powershell
git add -- image_workflows.py test_agent_flow.py test_batch_workflow.py
git commit -m "test: cover shared direct reference copy policy"
```

---

### Task 5: Full Regression And One-Image Smoke Verification

**Files:**
- Verify only: `image_workflows.py`
- Verify only: `test_agent_flow.py`
- Verify only: `test_image_workflows.py`
- Verify only: `test_batch_workflow.py`
- Runtime artifact: a new timestamped directory under `outputs/` created by the existing workflow only after confirming the service is idle.

**Interfaces:**
- Consumes: completed Tasks 1-4.
- Produces: automated regression evidence and one manually inspected direct-link main image.

- [ ] **Step 1: Run the complete automated suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 2: Confirm the running service is idle without restarting it**

Run:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8765/api/status' -TimeoutSec 5 | ConvertTo-Json -Depth 8
```

Expected before any smoke generation: `collecting`, `generating`, and batch/supplement running flags are false. If any flag is true, do not run the smoke generation and report that live verification is deferred to avoid interrupting user work.

- [ ] **Step 3: Run one direct-reference main-image smoke task through the existing application path**

Use one already-collected main image from the latest valid manifest and request exactly one `main` output with `generation_mode="competitor_reference"`. Do not collect again, do not run SKU/detail generation, and do not restart the service.

Expected runtime evidence:

- `analysis.json` contains `reported_compliance_risks`, reviewed `compliance_risks`, and a non-empty `copy_plan`.
- The final generation prompt contains the approved headline and at least one approved selling point.
- The output image exists and the task status is completed.

- [ ] **Step 4: Manually inspect the output**

Verify all of the following:

- Product and packaging identity are unchanged.
- At least one clear title and one selling point are visible.
- Ordinary existing selling copy is not removed merely for mentioning nutrition, vitality, enhancement, improvement, synergy, or usage.
- Explicit approved risks are absent only from off-product regions.
- No blank badge, blur block, smear, pseudo-text region, or bare product-only layout was introduced.

If the image model produces illegible text despite a correct prompt, record that as manual-review failure rather than changing compliance classification or reporting success.

- [ ] **Step 5: Review the final diff and commit any test-only correction separately**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors. Do not stage unrelated pre-existing worktree changes or generated `outputs/` artifacts.
