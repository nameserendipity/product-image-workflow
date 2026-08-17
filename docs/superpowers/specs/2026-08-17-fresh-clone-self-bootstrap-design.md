# Fresh Clone Self-Bootstrap and Python Workbook Export Design

Date: 2026-08-17

## Goal

After a fresh clone or `git pull`, a Windows user can double-click `启动程序.bat` and run the application. The first run downloads and prepares the required runtime and dependencies, while later runs only perform a fast consistency check. Excel export must work without the missing `@oai/artifact-tool` package or a Node.js runtime.

## Scope

This design covers:

- idempotent Windows dependency bootstrap;
- Python 3.12 runtime selection and Playwright browser preparation;
- automatic creation of a local configuration file without committing secrets;
- replacement of the missing Node workbook exporter with a Python exporter;
- the final workbook contract and its tests;
- clean-clone and post-pull verification.

CSRF protection, task concurrency control, and other runtime hardening findings from the code review remain separate follow-up work.

## Architecture

The application runtime becomes Python-only. `启动程序.bat` invokes an idempotent `bootstrap.ps1`, then launches the existing `web_app.py` with the prepared virtual-environment interpreter. The repository continues to track the built `web` directory, so end users do not need Node.js; CI builds the frontend and verifies that the generated files are committed.

The workbook export path keeps the existing Python payload assembly in `batch_workflow.py`, but sends that payload to a new `workbook_exporter.py` module using `openpyxl` and Pillow. The exporter writes an XLSX file atomically and preserves the current workbook layout, image previews, links, and formatting without invoking a child process.

## Runtime Flow

1. `启动程序.bat` calls `bootstrap.ps1 -Mode Ensure`.
2. The bootstrap checks a repository-local `.runtime` directory and a `.venv` directory. Both are ignored by Git.
3. If Python 3.12.10 is unavailable, the bootstrap downloads the official per-user Windows installer, verifies its SHA-256 value from `runtime-versions.json`, and installs it under the local runtime directory without requiring administrator privileges.
4. The bootstrap creates or updates `.venv` and installs the exact packages from `requirements.lock.txt`.
5. The bootstrap runs `playwright install chromium` when the browser marker is absent or the runtime version changes.
6. If `local_settings.json` is absent, it is copied from the sanitized `local_settings.example.json`. The file remains Git-ignored and contains no API keys.
7. The bootstrap checks that `web/index.html` and its referenced assets exist. It does not rebuild the frontend during normal application startup.
8. A `.runtime/bootstrap-state.json` marker stores the hashes of the runtime manifest, dependency lock file, and bootstrap version. A `git pull` that changes any of these causes only the affected step to run again.
9. The batch file starts `web_app.py` with `.venv\Scripts\python.exe`. If bootstrap fails, it prints the exact failed step and leaves the application stopped.

There is intentionally no Git post-merge hook. Hooks are not reliably distributed by Git, so the startup path is the consistency boundary after every pull.

## Dependency Sources

The repository adds `runtime-versions.json` containing the exact Python 3.12.10 installer URL and SHA-256 checksum. It also adds `requirements.lock.txt` with pinned versions for Playwright, openpyxl, Pillow, and oss2 plus their resolved dependencies. `requirements.txt` remains the human-readable direct dependency list; bootstrap and CI use the lock file.

No Node dependency is required by the running application. Node remains a CI-only tool for rebuilding `frontend` when frontend source changes. The missing `spreadsheet_runtime/exporter.mjs` and its `@oai/artifact-tool` import are removed from the application export path.

## Configuration

`local_settings.example.json` is restored to a sanitized template. It may contain the approved non-secret API base URL and OSS endpoint/bucket/prefix, but must not contain model keys, OSS access keys, or access-key secrets.

Bootstrap creates `local_settings.json` when needed. The web UI continues to collect model API keys and stores them only in the ignored local file. A preflight test rejects secret-shaped values in the example file and in release inputs.

## Workbook Export Contract

The workbook contains exactly seven sheets in this order:

1. `总览`
2. `主图`
3. `详情图`
4. `SKU`
5. `商品参数`
6. `标题`
7. `视频`

The sheet headers are:

- `总览`: `字段`, `值`.
- `主图`: `序号`, `采集图缩略图`, `采集图路径`, `生成图缩略图`, `生成图路径`, `生成状态`.
- `详情图`: the same six headers as `主图`.
- `SKU`: `序号`, `商品ID`, `SKU标签`, `规格`, `颜色`, `价格`, `采集图缩略图`, `采集图路径`, `生成图缩略图`, `生成图路径`, `生成图状态`.
- `商品参数`: `类型`, `参数名`, `参数值`, `处理方式`.
- `标题`: `序号`, `长标题`, `短标题`.
- `视频`: `序号`, `视频名称`, `公网播放地址`, `访问说明`.

The SKU parser's internal `parse_status` value remains available to Python decision logic and diagnostics, but it is not exported as a column.

The exporter preserves the current presentation rules: frozen header rows, dark header fill with white bold text, borders, wrapping, configured column widths, dynamic row heights, blue path/URL text, and embedded thumbnails. Image paths prefer a public URL when one exists and otherwise use a relative local path. Product IDs are written as text, prices remain numeric, and WebP previews are converted to PNG in memory before embedding. The output is first written to a sibling temporary file and then atomically replaced into the requested path.

## Python Exporter Interface

Create `workbook_exporter.py` with this public function:

```python
def export_workbook_payload(output_path: Path, payload: dict[str, Any]) -> Path:
    """Write one validated payload to an XLSX file and return output_path."""
```

`batch_workflow.py` continues to build the existing payload at `export_product_workbook`, then calls `export_workbook_payload`. The existing `ensure_workbook_available` lock-file check remains before writing. The Node executable lookup, JSON payload subprocess file, and artifact-tool error translation are removed.

## Testing and Verification

Add or update tests to cover:

- exact seven-sheet order and all seven header rows;
- SKU column positions after removing `parse_status`;
- six embedded image previews in the main-sheet fixture and the expected shifted SKU image columns;
- long-title and long-parameter row-height behavior;
- public URL preference and local-path fallback;
- WebP preview conversion;
- atomic output replacement and failure cleanup;
- a generated Excel fixture instead of the current absolute path under `C:\Users\Administrator`;
- bootstrap state reuse and dependency-marker invalidation;
- sanitized configuration creation and secret rejection;
- a clean Windows checkout smoke test that starts the local server without API keys.

CI runs on Windows after a clean checkout, executes the bootstrap in non-interactive mode, runs `python -m unittest discover -q`, runs the exporter smoke test, and runs the frontend build. The frontend build must leave no diff under `web`; otherwise the change fails CI and cannot be merged.

## Acceptance Criteria

- On a clean Windows machine with Git, PowerShell, and network access, double-clicking `启动程序.bat` prepares the app and opens the local UI without manually copying configuration or installing Node.
- Re-running the batch file after an unchanged `git pull` does not reinstall unchanged dependencies.
- Changing `requirements.lock.txt` or `runtime-versions.json` causes the corresponding bootstrap step to run again.
- Excel export succeeds from a fresh clone and produces exactly the seven sheets and headers above.
- The SKU sheet has no `解析状态` column, while internal SKU parsing behavior remains intact.
- No API key or OSS secret is present in tracked files, release templates, or CI logs.
- The complete Python test suite, exporter smoke test, and frontend build pass in CI.
