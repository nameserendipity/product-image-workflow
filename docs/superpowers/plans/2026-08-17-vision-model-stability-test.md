# Vision Model Stability Test Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Test the complete Kuaishou workflow with `gpt-5.6-sol`, then restore `gpt-5.5` with process-wide visual concurrency two if any visual prompt analysis task still fails after retries.

**Architecture:** All image-bearing vision requests already consume `ApiSettings.vision_model` and pass through the module-level `_VISION_REQUEST_GATE`. The first trial changes only the shared model and model-specific web errors; the conditional fallback changes only the shared model and the global vision gate floor/initial limit, leaving `gpt-image-2` and generation concurrency unchanged.

**Tech Stack:** Python 3, `unittest`, `urllib`, `ThreadPoolExecutor`, the existing web batch API, Wanxiang CDP, Aliyun OSS.

## Global Constraints

- Work only in `D:\product-image-workflow-kuaishou-parameters` on `codex/kuaishou-parameters`.
- Use `C:\Users\Administrator\Desktop\快手链接.xlsx` with `batch_mode=direct_link` and `run_mode=full`.
- Keep `gpt-image-2`, image-generation concurrency 10, retry count five, task counts, collection, workbook export, and OSS behavior unchanged.
- Port 8011 is occupied; probe ports starting at 8012 and use the first available port.
- A single visual prompt analysis failure after five retries triggers the fallback trial.
- Do not change the text-only intent classifier in `agent_flow.py`.

---

### Task 1: Switch Image-Bearing Vision Requests To `gpt-5.6-sol`

**Files:**
- Modify: `test_image_workflows.py`
- Modify: `test_web_app.py`
- Modify: `image_workflows.py:225-232`
- Modify: `web_app.py:2865-2871`

**Interfaces:**
- Consumes: `ApiSettings(base_url: str, vision_api_key: str, image_api_key: str)` and `RequestHandler._friendly_generation_error(message: str) -> str`.
- Produces: a default `ApiSettings.vision_model` value of `gpt-5.6-sol`; every `VisionClient` payload automatically reads that value.

- [ ] **Step 1: Write failing model and error-message tests**

Add a test that constructs `ApiSettings` and asserts:

```python
self.assertEqual(settings.vision_model, "gpt-5.6-sol")
```

Add web error assertions for an unauthorized vision key and a `model_not_found` response containing `gpt-5.6-sol`.

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```powershell
python -m unittest test_image_workflows.ImageWorkflowTests.test_api_settings_defaults_to_gpt_5_6_sol test_web_app.WebAppTests.test_generation_errors_name_active_vision_model
```

Expected: FAIL because production defaults and messages still name `gpt-5.5`.

- [ ] **Step 3: Implement the minimal model switch**

Set:

```python
vision_model: str = "gpt-5.6-sol"
```

Update only the two model-specific friendly error strings and the `model_not_found` matcher to `gpt-5.6-sol`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the two focused tests again. Expected: both PASS.

- [ ] **Step 5: Run regression verification**

Run:

```powershell
python -m unittest discover -p "test_*.py"
python -m py_compile image_workflows.py web_app.py test_image_workflows.py test_web_app.py
git diff --check
```

Expected: all unit tests pass, compilation exits zero, and the diff check is empty.

- [ ] **Step 6: Commit the first-trial configuration**

```powershell
git add image_workflows.py web_app.py test_image_workflows.py test_web_app.py
git commit -m "test: trial gpt 5.6 sol for vision analysis"
```

### Task 2: Run The Formal `gpt-5.6-sol` Kuaishou Trial

**Files:**
- Read: `C:\Users\Administrator\Desktop\快手链接.xlsx`
- Create through application: `outputs/batches/<timestamp>/...`

**Interfaces:**
- Consumes: `POST /api/batch-upload`, `POST /api/batch-start`, and `GET /api/status`.
- Produces: a completed batch directory containing `batch-results.json`, `generated/analysis.json`, generated images, and the exported workbook.

- [ ] **Step 1: Find the first free port starting at 8012**

Use `Get-NetTCPConnection -State Listen` and select the first port without a listener.

- [ ] **Step 2: Start the updated formal web service**

Run `web_app.py --port <free-port> --no-browser` from this worktree and wait until `/api/status` responds.

- [ ] **Step 3: Submit the workbook through formal APIs**

Upload `快手链接.xlsx` with `batch_mode=direct_link`, then start it with `run_mode=full`. Do not call `BatchRunner` directly.

- [ ] **Step 4: Wait for the terminal batch state**

Poll `/api/status` until `batch.running` is false. Do not restart or stop the service while the batch is active.

- [ ] **Step 5: Measure the result**

Parse `generated/analysis.json` and report:

```text
vision_model
main completed/failed
sku completed/failed
detail completed/failed
failures grouped by failure_stage and error
```

Verify the exported workbook exists, product parameters are non-empty with non-empty `handling`, and each emitted OSS URL returns HTTP 200.

- [ ] **Step 6: Choose the validated branch**

If zero records fail at `视觉提示词分析`, retain `gpt-5.6-sol` and skip Task 3. If one or more records fail there, preserve this trial output and execute Task 3.

### Task 3: Conditional `gpt-5.5` And Concurrency-Two Fallback

**Files:**
- Modify: `test_image_workflows.py`
- Modify: `test_web_app.py`
- Modify: `image_workflows.py:29-35, 225-232`
- Modify: `web_app.py:2865-2871`

**Interfaces:**
- Consumes: `VISION_INITIAL_CONCURRENCY` and `_VISION_REQUEST_GATE`.
- Produces: a process-wide visual request limit of two shared by dossier, main, SKU, and detail analysis.

- [ ] **Step 1: Write failing fallback tests**

Require:

```python
self.assertEqual(image_workflows.VISION_INITIAL_CONCURRENCY, 2)
self.assertEqual(image_workflows._VISION_REQUEST_GATE.target_concurrency, 2)
self.assertEqual(image_workflows._VISION_REQUEST_GATE.min_concurrency, 2)
self.assertEqual(settings.vision_model, "gpt-5.5")
```

Update the web error test to require `gpt-5.5` again.

- [ ] **Step 2: Run focused tests and verify RED**

Expected: model and concurrency assertions fail while the first-trial production configuration remains active.

- [ ] **Step 3: Implement the minimal fallback**

Set `VISION_INITIAL_CONCURRENCY = 2`, keep the existing global gate construction, restore `ApiSettings.vision_model = "gpt-5.5"`, and restore matching web error text.

- [ ] **Step 4: Run focused and full verification**

Repeat Task 1 Step 5. Expected: all checks pass.

- [ ] **Step 5: Commit the fallback configuration**

```powershell
git add image_workflows.py web_app.py test_image_workflows.py test_web_app.py
git commit -m "fix: reduce shared vision request concurrency"
```

- [ ] **Step 6: Repeat the formal batch trial**

Start a new service on the next free port, submit the same workbook through the formal APIs, wait for completion, and perform the exact Task 2 Step 5 measurements. Retain both trial directories for comparison.
