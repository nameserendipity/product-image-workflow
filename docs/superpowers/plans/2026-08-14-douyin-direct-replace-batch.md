# 抖音直链与批量产品替换实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在不影响现有淘宝/天猫/京东搜同款流程的前提下，支持抖音直链采集、批量直链产品替换、SKU 手工依据、采集-only、断点恢复、OSS 视频/生成图 URL 和可靠的独立商品表格导出。

**Architecture:** 复用现有 `DirectLinkCollector`、`WorkflowRunner`、OSS 上传器和导出器。平台识别与表格输入解析独立为纯函数；抖音页面只负责调用既有店透视可见下载动作，下载后的 ZIP 统一转换为 schema v2 manifest。批量 `direct_replace` 只负责从每行识别我方图片和链接，再把当前行的图片作为 `own_product` 身份源传入现有生成链路。

**Tech Stack:** Python 3、Playwright CDP、openpyxl、zipfile/xml、现有 Image-2/视觉 API、阿里云 OSS、React/TypeScript/Vite。

## Global Constraints

- 不使用浏览器控制之外的店透视私有 API，不绕过登录、验证码或平台风控。
- 抖音采集只允许使用独立采集器创建的标签页，结束时只关闭该标签页，不关闭用户浏览器。
- 验证码、登录、访问限制出现时停止点击、滚动和刷新，等待用户处理后继续当前商品。
- 采集原图保存在本地；只有生成图片和无原始公网 URL 的视频上传 OSS。
- 抖音没有真实 SKU 数据时写入 `missing`，不得从标题或图片编造 SKU；用户补录的 SKU 标记为 `manual` 或 `text_conditioned`。
- 导出成功必须有真实存在的输出图片，或在采集-only 模式下有有效 manifest；空表格不得标记成功。
- 不重启现有服务，不覆盖工作区已有未提交修改；提交时只暂存本功能文件。
- 主图默认 10 张；用户指定数量优先。SKU 生成数量上限 8，详情图生成数量上限 15。

---

### Task 1: 平台识别与抖音 URL 解析

**Files:**
- Modify: `batch_workflow.py:81-133`
- Modify: `store_insight_collector.py:70-113`
- Test: `test_batch_workflow.py`
- Test: `test_collector.py`

**Interfaces:**
- `batch_workflow._direct_link_platform(value: str) -> tuple[str, str]` accepts `v.douyin.com`, `haohuo.jinritemai.com` and resolved `jinritemai.com` item URLs and returns `("douyin", "")` when an item id is present.
- `store_insight_collector.validate_item_url(value: str) -> tuple[str, str]` accepts Douyin short/detail URLs and returns the normalized URL plus product id.
- `resolve_direct_item_url` follows supported short links only and rejects unresolved or non-item URLs without falling back to image search.

- [ ] **Step 1: Write failing tests**

```python
def test_direct_link_platform_accepts_douyin_short_and_detail_urls():
    assert _direct_link_platform("https://v.douyin.com/abc")[0] == "douyin"
    assert _direct_link_platform("https://haohuo.jinritemai.com/views/product/item2.html?id=123")[0] == "douyin"

def test_validate_item_url_extracts_douyin_product_id():
    url, product_id = validate_item_url("https://haohuo.jinritemai.com/views/product/item2.html?id=123")
    assert product_id == "123"
    assert url.startswith("https://")
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `python -m pytest test_batch_workflow.py -k douyin -q` and `python -m pytest test_collector.py -k douyin -q`.

Expected: FAIL because the current platform allow-list excludes Douyin.

- [ ] **Step 3: Implement the minimal platform predicates**

Recognize short links by hostname without pretending the short token is an item id. Resolve them before validation. Accept only `v.douyin.com`, `haohuo.jinritemai.com` and `*.jinritemai.com` item URLs; require a numeric or opaque product id from the query/path after resolution.

- [ ] **Step 4: Run the focused tests**

Run: `python -m pytest test_batch_workflow.py -k douyin -q` and `python -m pytest test_collector.py -k douyin -q`.

Expected: PASS, with existing Taobao/Tmall/JD URL tests unchanged.

- [ ] **Step 5: Commit only this task if the user requests commits**

```powershell
git add batch_workflow.py store_insight_collector.py test_batch_workflow.py test_collector.py
git commit -m "feat: recognize Douyin direct links"
```

---

### Task 2: 抖音店透视 ZIP 适配与 schema v2 manifest

**Files:**
- Create: `douyin_collector.py`
- Modify: `store_insight_collector.py:626-720, 807-1140`
- Modify: `batch_workflow.py:183-205, 307-389`
- Test: `test_collector.py`
- Test: `test_batch_workflow.py`

**Interfaces:**
- `collect_douyin_package(page, item_url, downloads_dir, timeout_ms, login_wait_seconds) -> Path` returns a completed multi-file ZIP or raises `RiskControlDetected`/`RuntimeError` without refreshing on a challenge.
- `materialize_douyin_package(zip_path: Path, output_root: Path, selected_types: set[str], max_main_images: int | None = None) -> list[dict[str, Any]]` classifies `主图/页面图`, `详情图`, `SKU图`, and local MP4 without inventing missing categories.
- `build_douyin_manifest(item_url, product_id, output_root, images, metadata) -> dict[str, Any]` emits schema version 2 and explicit `requested_asset_types`, `missing_asset_types`, `main_video_status`, `sku_metadata_status`, and `product_parameters`.
- `DirectLinkCollector.collect` dispatches `douyin` to this adapter and still dispatches existing platforms to existing collectors.

- [ ] **Step 1: Write failing ZIP and manifest tests**

```python
def test_materialize_douyin_package_keeps_only_requested_types(tmp_path):
    archive = make_zip(tmp_path, {
        "主图/1.jpg": b"main", "详情图/1.jpg": b"detail",
        "SKU图/1.jpg": b"sku", "主图视频.mp4": b"video",
    })
    records = materialize_douyin_package(archive, tmp_path / "out", {"main"})
    assert [record["type"] for record in records] == ["main"]
    assert not (tmp_path / "out" / "detail").exists()

def test_douyin_manifest_marks_missing_sku_instead_of_fabricating_it(tmp_path):
    manifest = build_douyin_manifest("https://v.douyin.com/x", "123", tmp_path, [], {
        "sku_variants": [], "sku_metadata_status": "not_found",
    })
    assert manifest["sku_metadata_status"] == "not_found"
    assert manifest["sku_variants"] == []
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest test_collector.py -k "douyin_package or douyin_manifest" -q`.

Expected: FAIL because the adapter functions do not exist.

- [ ] **Step 3: Implement ZIP classification and manifest normalization**

Use `safe_extract` and existing hash/dedup behavior. Treat a local MP4 as `main_video_local_path`; preserve any original video URL found in metadata. Read the downloaded 商品数据 and 商品参数 workbooks through existing parsers where possible and keep parse failures as explicit metadata statuses.

- [ ] **Step 4: Implement collector dispatch and pause behavior**

Use the same CDP/profile and new page lifecycle as the current collector. The adapter must wait for download completion, not only a download event, and must return control to the existing `BatchRunner` checkpoint path.

- [ ] **Step 5: Run focused and existing collector tests**

Run: `python -m pytest test_collector.py -q`.

Expected: PASS with no regression in existing all-files downloads, verification pause, ZIP safety, or SKU metadata tests.

---

### Task 3: WPS `DISPIMG` and generic batch input pairing

**Files:**
- Create: `spreadsheet_inputs.py`
- Modify: `batch_workflow.py:662-724`
- Test: `test_batch_workflow.py`

**Interfaces:**
- `extract_embedded_images(workbook_path: Path) -> dict[tuple[str, int, int], Path]` maps ordinary Excel drawings and WPS `DISPIMG` cell images to extracted local image files.
- `extract_direct_replace_items(workbook_path: Path, output_dir: Path) -> list[DirectReplaceBatchItem]` scans all non-empty sheets in workbook/row order, uses header synonyms first, and falls back to exactly one image plus one supported URL in the same row.
- `DirectReplaceBatchItem` contains `sequence`, `sheet_name`, `row_number`, `product_image`, `source_url`, optional title and manual SKU fields, and `validation_error`.

- [ ] **Step 1: Write failing pairing tests**

```python
def test_extract_direct_replace_reads_wps_dispimg_and_non_fixed_link_column(tmp_path):
    workbook = copy_fixture("测试.xlsx", tmp_path / "input.xlsx")
    items = extract_direct_replace_items(workbook, tmp_path / "out")
    assert len(items) == 4
    assert items[0].source_url.startswith("http")
    assert items[0].product_image.is_file()

def test_extract_direct_replace_rejects_row_with_two_images_or_two_links(tmp_path):
    workbook = make_conflicting_workbook(tmp_path / "input.xlsx")
    items = extract_direct_replace_items(workbook, tmp_path / "out")
    assert items[0].validation_error == "图片或链接配对冲突"
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest test_batch_workflow.py -k "direct_replace or dispimg" -q`.

Expected: FAIL because current `openpyxl` image enumeration does not resolve WPS cell images and current direct-link extraction only reads one sheet/column.

- [ ] **Step 3: Implement structured XLSX extraction**

Read the XLSX ZIP with `zipfile` and XML namespaces. Resolve `xl/cellimages.xml` `r:embed` through `xl/_rels/cellimages.xml.rels`, map `DISPIMG("ID_...")` to the corresponding `xl/media/*`, and use cell coordinates from sheet XML. Extract ordinary `_images` as an additional source. Do not guess neighboring rows.

- [ ] **Step 4: Implement headers, fallback, and manual SKU fields**

Recognize product-image, source-link, SKU name/color/spec/price/reference-image synonyms case-insensitively. In no-header mode require exactly one image and one supported URL per row. Mark unsupported or ambiguous rows while preserving their order.

- [ ] **Step 5: Run focused tests plus the supplied workbook fixture**

Run: `python -m pytest test_batch_workflow.py -k "direct_replace or dispimg" -q` and verify the four rows from `C:\Users\Administrator\Documents\xwechat_files\wxid_ex358te1357c22_6c76\msg\file\2026-08\测试.xlsx` are extracted without assuming D/A columns.

Expected: PASS and no image/link pairing guesses.

---

### Task 4: `direct_replace` batch runner and checkpoint semantics

**Files:**
- Modify: `batch_workflow.py:49-72, 1170-1820`
- Modify: `web_app.py:1390-1575`
- Modify: `frontend/src/types.ts`
- Test: `test_batch_workflow.py`
- Test: `test_web_app.py`

**Interfaces:**
- `BatchRunner(..., batch_mode="direct_replace", collect_only=False)` accepts `DirectReplaceBatchItem` and does not invoke image search.
- `BatchRunner.run` writes an item checkpoint after collection, generation, and export; an existing valid manifest skips collection, while a failed/partial generation resumes from its manifest.
- `/api/batch-upload` accepts `batch_mode=direct_replace`; `/api/batch-start` runs `collect_only` or `full` without changing the existing image-search behavior.

- [ ] **Step 1: Write failing runner tests**

```python
def test_direct_replace_uses_current_row_image_as_identity(tmp_path):
    runner = BatchRunner(None, tmp_path, tmp_path, batch_mode="direct_replace", collect_only=True)
    with patch("batch_workflow.extract_direct_replace_items", return_value=[item]) as extract:
        runner.run(workbook, tmp_path / "batch")
    extract.assert_called_once()

def test_direct_replace_checkpoint_reuses_collected_manifest_after_stop(tmp_path):
    # First run records status=collected; second run must not call collector again.
    assert second_run_events.count("collection_reused") == 1
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest test_batch_workflow.py -k direct_replace -q`.

Expected: FAIL because `BatchRunner` currently accepts only `image_search` and `direct_link`.

- [ ] **Step 3: Implement the minimal task model and runner branch**

Keep `direct_link` as the existing competitor-reference mode. `direct_replace` requires one valid current-row product image and one direct source URL, stores source images under a stable `source-images` batch directory, and passes `generation_mode="own_product"` with that image to `WorkflowRunner`. Do not share product identity across rows.

- [ ] **Step 4: Add collect-only and resume assertions**

A collect-only result is valid only when its manifest exists and contains at least one requested collected asset or an explicit missing-type status. A full result is complete only when required generated records have `status == "completed"` and output files exist. Keep `failed`/`stopped` records resumable.

- [ ] **Step 5: Run focused web/runner tests**

Run: `python -m pytest test_batch_workflow.py -k direct_replace -q` and `python -m pytest test_web_app.py -k "batch_upload or batch_start or direct_replace" -q`.

Expected: PASS; image-search/direct-link tests remain unchanged.

---

### Task 5: SKU/manual metadata, video OSS, and export payload

**Files:**
- Modify: `batch_workflow.py:766-1165`
- Modify: `oss_uploader.py`
- Modify: `store_insight_collector.py:1040-1080`
- Test: `test_batch_workflow.py`
- Test: `test_oss_uploader.py`

**Interfaces:**
- `upload_video_if_needed(manifest: dict, uploader: OssUploader | None, namespace: str) -> dict` preserves an original `main_video_url`, otherwise uploads an existing local MP4 and records `main_video_status`, `main_video_public_url`, and `main_video_error`.
- `export_product_workbook(..., include_metadata_only_skus=True)` includes only real collected/manual SKU rows, never extra image-less platform rows unless explicitly supplied by the input workbook.
- `_record_output_is_valid` and export result validation reject nonexistent files and empty generated outputs.

- [ ] **Step 1: Write failing tests**

```python
def test_video_without_public_url_is_uploaded_and_exported(tmp_path):
    result = upload_video_if_needed(manifest_with_local_mp4, uploader, "item-1")
    assert result["main_video_status"] == "complete"
    assert result["main_video_public_url"].startswith("https://")

def test_empty_generation_does_not_export_success_workbook(tmp_path):
    with pytest.raises(RuntimeError, match="没有生成任何有效图片"):
        finalize_generation_export([], required_types=("main",))
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest test_batch_workflow.py test_oss_uploader.py -k "video or empty_generation" -q`.

Expected: FAIL because video-local upload and final output validation are not exposed as one operation.

- [ ] **Step 3: Implement video URL handling and strict export status**

Use the configured uploader only for local MP4s. Keep local paths when upload fails and mark the item partial/failed. Do not make collected local images public. Preserve the existing workbook lock check and atomic exporter payload.

- [ ] **Step 4: Implement manual SKU merge rules**

Merge rows from the input item only when a SKU name/color/spec/price or reference image is present. Mark source as `manual`, `text_conditioned`, or `reference_image`; do not infer missing values from unrelated platform rows.

- [ ] **Step 5: Run export regression tests**

Run: `python -m pytest test_batch_workflow.py test_oss_uploader.py -q`.

Expected: PASS, including existing no-empty-export, SKU cap, and workbook-lock tests.

---

### Task 6: Single-link Douyin and batch mode UI

**Files:**
- Modify: `web_app.py`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`
- Test: `test_web_app.py`
- Build: `frontend` via `npm run build`

**Interfaces:**
- Single-link URL submission accepts Douyin and keeps the selected generation mode and quantities persistent until explicitly changed.
- Batch UI exposes `direct_link`, `direct_replace`, and existing `image_search` only where applicable; unsupported future modes remain hidden.
- Status includes platform, collected counts, missing asset types, SKU metadata status, video status, and output folder buttons without rendering all images into the page.

- [ ] **Step 1: Write failing API/UI state tests**

```python
def test_single_douyin_url_is_routed_to_direct_collector():
    response = submit_reference_url("https://v.douyin.com/abc")
    assert response.status_code == 200
    assert state.agent.reference_url == "https://v.douyin.com/abc"

def test_direct_replace_upload_reports_pairing_counts():
    payload = upload_batch(workbook, batch_mode="direct_replace")
    assert payload["valid"] == 4
    assert payload["invalid"] == 0
```

- [ ] **Step 2: Run tests and verify RED**

Run: `python -m pytest test_web_app.py -k "douyin or direct_replace" -q`.

Expected: FAIL because the API only recognizes existing batch modes and status types.

- [ ] **Step 3: Implement API state and mode controls**

Extend type unions and server validation only. Keep mode changes side-effect free: changing a segmented control must not start collection or generation. Reuse current stop/continue endpoints.

- [ ] **Step 4: Implement the minimal React controls**

Add direct replacement as a child mode under batch link workflow. Show the product-image column/link pairing summary before confirmation, and show only folder/open/export actions after completion.

- [ ] **Step 5: Run API tests and frontend build**

Run: `python -m pytest test_web_app.py -q` and `npm run build --prefix frontend`.

Expected: PASS and a clean Vite build.

---

### Task 7: Documentation and regression verification

**Files:**
- Create: `docs/douyin-direct-replace-user-guide.md`
- Modify: `README.md` only if an existing run command needs correction
- Test: all existing Python tests

- [ ] **Step 1: Document supported inputs and honest limitations**

Document single URL, batch URL workbook, direct replacement workbook, collect-only, full run, pause/resume on validation, SKU manual columns, video URL behavior, OSS settings, and output locations. State that Douyin missing SKU data is not generated.

- [ ] **Step 2: Run the complete test suite**

Run: `python -m pytest -q`.

Expected: PASS with no warnings treated as failures.

- [ ] **Step 3: Run static/build checks**

Run: `python -m compileall -q agent_flow.py batch_workflow.py store_insight_collector.py douyin_collector.py spreadsheet_inputs.py web_app.py` and `npm run build --prefix frontend`.

Expected: exit code 0.

- [ ] **Step 4: Perform non-disruptive fixture smoke tests**

Use a temporary output directory and mocked download/vision boundaries to verify: four-row WPS extraction, Douyin manifest classification, collect-only checkpoint, full-run validation, and OSS URL fields. Do not touch the live service or its current `outputs` directory.

- [ ] **Step 5: Review the diff and commit selectively**

Run: `git diff --check` and `git status --short`. Stage only the files listed in Tasks 1-7; leave existing unrelated modifications and generated assets unstaged. Commit only if the user later asks for a commit.

## Self-review

- Spec coverage: platform routing, ZIP extraction, WPS mapping, direct replacement, manual SKU truthfulness, video OSS, export validation, checkpointing, UI and documentation are covered by Tasks 1-7.
- Placeholder scan: no unfinished placeholder marker or undefined implementation task appears in this plan.
- Interface consistency: `DirectReplaceBatchItem`, `direct_replace`, `materialize_douyin_package`, `extract_direct_replace_items`, and `upload_video_if_needed` are named consistently across tasks.
