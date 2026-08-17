# Fresh Clone Self-Bootstrap and Python Workbook Export Implementation Plan

> For agentic workers: REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Make a fresh Windows clone runnable through 启动程序.bat with an idempotent network bootstrap, and make XLSX export independent of the missing @oai/artifact-tool package.

**Architecture:** Keep the existing Python payload construction, replace the Node exporter with a Python openpyxl/Pillow exporter, and let bootstrap.ps1 prepare a pinned Python 3.12 runtime, virtual environment, dependencies, Chromium, and local configuration. Keep compiled frontend assets in web; Node is CI-only.

**Tech Stack:** Python 3.12.10, openpyxl, Pillow, Playwright, PowerShell, Windows batch, unittest, React/Vite build in CI.

## Global Constraints

- The application runtime must not depend on Node.js or @oai/artifact-tool.
- The workbook must contain exactly 总览, 主图, 详情图, SKU, 商品参数, 标题, 视频 in that order.
- The SKU sheet must omit 解析状态 while internal parse_status data remains available to Python logic.
- local_settings.example.json must contain no model API key, OSS access key, or OSS access-key secret.
- Bootstrap must be repeatable after every git pull and reinstall only when the runtime manifest, dependency lock, or bootstrap version changes.
- End users must not need Node.js, npm, or manual copying of local_settings.json.
- Tests must run from a clean Windows checkout without an absolute path to a developer's personal files.

---

### Task 1: Replace the Missing Workbook Runtime

**Files:**
- Create: workbook_exporter.py
- Create: test_workbook_exporter.py
- Modify: batch_workflow.py:1568-1713
- Modify: test_batch_workflow.py:1689-1781,2317-2475
- Delete: spreadsheet_runtime/exporter.mjs

**Interfaces:**
- Consumes: the existing payload dictionary assembled by export_product_workbook.
- Produces: export_workbook_payload(output_path: Path, payload: dict[str, Any]) -> Path.

- [ ] Step 1: Write failing exporter tests

Create temporary manifest and PNG fixtures in test_workbook_exporter.py. Assert the exact sheet order and headers, including the 11-column SKU header:

~~~python
expected_sku = [
    "序号", "商品ID", "SKU标签", "规格", "颜色", "价格",
    "采集图缩略图", "采集图路径", "生成图缩略图", "生成图路径", "生成图状态",
]
assert workbook.sheetnames == ["总览", "主图", "详情图", "SKU", "商品参数", "标题", "视频"]
assert [cell.value for cell in workbook["SKU"][1]] == expected_sku
assert "解析状态" not in [cell.value for cell in workbook["SKU"][1]]
~~~

Also assert six main-sheet images, public URL preference, numeric price, long-title row height above 22, and WebP conversion into an embedded image.

- [ ] Step 2: Run the focused tests and verify failure

Run:

~~~powershell
python -m unittest test_workbook_exporter -v
~~~

Expected: fail because workbook_exporter.py does not exist.

- [ ] Step 3: Implement the Python exporter

Implement the public function with atomic output:

~~~python
def export_workbook_payload(output_path: Path, payload: dict[str, Any]) -> Path:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for name in ("总览", "主图", "详情图", "SKU", "商品参数", "标题", "视频"):
        workbook.create_sheet(name)
    # write the payload into those seven sheets
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    workbook.save(temporary)
    os.replace(temporary, output_path)
    return output_path
~~~

Use openpyxl.drawing.image.Image for PNG/JPEG previews. Convert WebP through Pillow and BytesIO, keeping streams alive until save completes. Set IDs as strings, prices as numbers, wrap text, freeze row 1, apply the existing header/border style, calculate dynamic row heights, and prefer public URLs over local paths.

- [ ] Step 4: Wire Python export into batch_workflow.py

Import export_workbook_payload and replace the final exporter call with:

~~~python
return export_workbook_payload(output_path, payload)
~~~

Delete _find_node_executable, _export_with_artifact_tool, temporary JSON payload creation, and artifact-tool error translation. Update existing tests to patch batch_workflow.export_workbook_payload.

- [ ] Step 5: Run exporter and batch tests

~~~powershell
python -m unittest test_workbook_exporter test_batch_workflow -q
~~~

Expected: all exporter tests pass and no test invokes Node.js.

- [ ] Step 6: Commit

~~~powershell
git add workbook_exporter.py test_workbook_exporter.py batch_workflow.py test_batch_workflow.py spreadsheet_runtime/exporter.mjs
git commit -m "feat: replace node workbook exporter with python"
~~~

### Task 2: Add Deterministic Runtime and Dependency Manifests

**Files:**
- Create: runtime-versions.json
- Create: requirements.lock.txt
- Modify: requirements.txt
- Modify: .gitignore

- [ ] Step 1: Pin dependencies

Pin the direct versions in requirements.txt to the versions verified in this checkout: playwright==1.60.0, openpyxl==3.1.5, Pillow==12.3.0, and oss2==2.19.1. Install them in a temporary Python 3.12 environment, run pip freeze, remove local/editable entries, and save the resolved list as requirements.lock.txt. Bootstrap and CI use the lock file.

- [ ] Step 2: Add the runtime manifest

Create runtime-versions.json with bootstrap_version 1 and a python object containing version 3.12.10, installer_url https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe, and a sha256 property. Download the installer once, run Get-FileHash, and write that command's 64-character lowercase digest into sha256 before committing.

- [ ] Step 3: Extend Git ignore rules

Add .runtime/, .bootstrap-state.json, and bootstrap download cache paths to .gitignore. Keep the runtime manifest and lock file tracked.

- [ ] Step 4: Validate metadata

~~~powershell
python -c "import json; json.load(open('runtime-versions.json', encoding='utf-8')); print('runtime manifest: PASS')"
rg -n "path|file:|editable" requirements.lock.txt
git diff --check
~~~

Expected: valid JSON, no local dependency paths, and no whitespace errors.

- [ ] Step 5: Commit

~~~powershell
git add runtime-versions.json requirements.txt requirements.lock.txt .gitignore
git commit -m "build: pin self bootstrap dependencies"
~~~

### Task 3: Implement Idempotent PowerShell Bootstrap

**Files:**
- Create: bootstrap.ps1
- Create: test_bootstrap_contract.py

**Interfaces:**
- Consumes: runtime-versions.json, requirements.lock.txt, web, and the sanitized settings template.
- Produces: .runtime, .venv, Chromium, local_settings.json, and .runtime/bootstrap-state.json.

- [ ] Step 1: Write contract tests

Assert that bootstrap.ps1 declares Ensure and Check modes, accepts NonInteractive, references the runtime manifest and lock file, verifies SHA-256 before running an installer, creates local_settings.json, and writes a state marker. Run before implementation and verify failure because the script is absent.

- [ ] Step 2: Implement the script entrypoint

Use this parameter contract:

~~~powershell
param(
    [ValidateSet("Ensure", "Check")]
    [string]$Mode = "Ensure",
    [switch]$NonInteractive,
    [string]$Root = $PSScriptRoot
)
~~~

Implement Get-FileSha256, Ensure-PythonRuntime, Ensure-Venv, Ensure-PythonDependencies, Ensure-PlaywrightChromium, Ensure-LocalSettings, Ensure-WebAssets, and Write-BootstrapState. Resolve Python in this order: .runtime\python-3.12.10\python.exe, py -3.12, then the verified official installer. Use python -m venv .venv, python -m pip install --requirement requirements.lock.txt, and python -m playwright install chromium.

- [ ] Step 3: Make repeated runs safe

Hash runtime-versions.json, requirements.lock.txt, and the bootstrap version. Skip satisfied steps when stored values match. Download to a .part file, verify, then rename. On failure remove only the partial download and print the failed command.

- [ ] Step 4: Test Check mode

~~~powershell
python -m unittest test_bootstrap_contract -v
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Mode Check -NonInteractive
~~~

Expected: contract tests pass; Check reports missing components clearly and exits nonzero without downloading.

- [ ] Step 5: Commit

~~~powershell
git add bootstrap.ps1 test_bootstrap_contract.py
git commit -m "build: add idempotent windows bootstrap"
~~~

### Task 4: Make Startup and Configuration Self-Initializing

**Files:**
- Modify: 启动程序.bat
- Modify: 安装依赖.bat
- Modify: local_settings.example.json
- Modify: test_web_app.py
- Create: test_repository_hygiene.py

- [ ] Step 1: Add hygiene tests

Assert that the example JSON has no image_api_key, vision_api_key, access_key_id, or access_key_secret, and reject values matching sk- or non-empty OSS secret fields. Run against the current checkout and record the expected failure from the dirty example.

- [ ] Step 2: Restore the sanitized template

Keep only the approved non-secret base_url, browser_choice, and OSS endpoint/bucket/prefix fields. Do not copy credentials from the current worktree into the committed template.

- [ ] Step 3: Delegate startup to bootstrap

At the beginning of 启动程序.bat, run:

~~~bat
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%ROOT%bootstrap.ps1" -Mode Ensure
if errorlevel 1 goto :failed
~~~

Launch .venv\Scripts\python.exe afterward. Convert 安装依赖.bat into a wrapper that invokes the same bootstrap.

- [ ] Step 4: Test first-run configuration

Run hygiene tests and a temporary-directory Windows bootstrap smoke test. Assert that missing local_settings.json is created from the template, API keys remain empty, and the server answers /api/status.

- [ ] Step 5: Commit

~~~powershell
git add 启动程序.bat 安装依赖.bat local_settings.example.json test_web_app.py test_repository_hygiene.py
git commit -m "build: self initialize startup configuration"
~~~

### Task 5: Remove Runtime Node Assumptions and Update Documentation

**Files:**
- Modify: build_release.ps1
- Modify: README.md
- Modify: 操作说明书.md
- Modify: docs/douyin-direct-replace-user-guide.md

- [ ] Step 1: Remove Node packaging

Delete the hard-coded Node requirement and runtime/node.exe and spreadsheet_runtime/node_modules copy steps from build_release.ps1. Node remains only in the frontend CI build path.

- [ ] Step 2: Update setup instructions

Document clone or pull, double-click 启动程序.bat, wait for first-run download, then enter API keys in the UI. State that Node.js and manual local_settings.json copying are not required.

- [ ] Step 3: Document final workbook output

List all seven sheets and show the 11 SKU columns without 解析状态. Keep the statement that internal SKU evidence is retained for processing.

- [ ] Step 4: Validate docs and packaging

~~~powershell
rg -n "artifact-tool|spreadsheet_runtime|runtime\\node|解析状态" README.md 操作说明书.md docs\douyin-direct-replace-user-guide.md build_release.ps1
git diff --check
~~~

Expected: no user-facing instruction requires the removed runtime or removed SKU column.

- [ ] Step 5: Commit

~~~powershell
git add build_release.ps1 README.md 操作说明书.md docs\douyin-direct-replace-user-guide.md
git commit -m "docs: describe self bootstrap and workbook output"
~~~

### Task 6: Replace the Non-Portable Fixture and Add Windows CI

**Files:**
- Modify: test_batch_workflow.py:1079-1092
- Create: .github/workflows/windows-smoke.yml

- [ ] Step 1: Replace the personal-path fixture

Build the WPS DISPIMG workbook inside the test TemporaryDirectory, create four rows with generated image references, and retain the existing assertions for row numbers, sheet name, platform, and local images. Remove the C:\Users\Administrator lookup.

- [ ] Step 2: Add Windows workflow

On windows-latest, checkout, run bootstrap.ps1 -Mode Ensure -NonInteractive, run python -m unittest discover -q, run npm ci --prefix frontend, run the frontend build, and fail when git diff --exit-code -- web detects stale committed assets. Do not provide API keys or upload secrets.

- [ ] Step 3: Run the portable test

~~~powershell
python -m unittest test_batch_workflow.BatchWorkflowTests.test_direct_replace_extracts_wps_dispimg_rows_without_fixed_columns -v
~~~

Expected: it passes without any user-specific path.

- [ ] Step 4: Commit

~~~powershell
git add test_batch_workflow.py .github/workflows/windows-smoke.yml
git commit -m "test: verify clean windows checkout"
~~~

### Task 7: Full Verification and Final Checkpoint

- [ ] Step 1: Run compilation and tests

~~~powershell
.\.venv\Scripts\python.exe -m compileall -q agent_flow.py batch_workflow.py workbook_exporter.py store_insight_collector.py douyin_collector.py spreadsheet_inputs.py image_workflows.py oss_uploader.py web_app.py
.\.venv\Scripts\python.exe -m unittest discover -q
~~~

Expected: compilation succeeds and the complete suite passes without Node or artifact-tool errors.

- [ ] Step 2: Run exporter smoke verification

Create a temporary manifest, call export_product_workbook, load the XLSX with openpyxl, and assert the exact seven sheet names, main/detail headers, 11-column SKU header, embedded images, numeric prices, and output URLs.

- [ ] Step 3: Run startup self-healing checks

~~~powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\bootstrap.ps1 -Mode Check -NonInteractive
cmd /c 启动程序.bat
~~~

Verify that the browser opens, /api/status responds, and a second launch reuses unchanged packages.

- [ ] Step 4: Run repository hygiene checks

~~~powershell
git diff --check
git status --short
rg -n "sk-[A-Za-z0-9]|access_key_secret|access_key_id" --glob "!local_settings.json" --glob "!outputs/**" --glob "!.git/**"
~~~

Expected: no tracked secret values and only intentional user-local changes remain.

- [ ] Step 5: Record verification without staging unrelated files

Do not stage generated outputs, local settings, `.idea/`, or any other user-local file. Keep the implementation commits from Tasks 1 through 6 as the final checkpoints and report the exact test commands and results.
