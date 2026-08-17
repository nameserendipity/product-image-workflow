# Kuaishou Product Parameters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ensure every completed Kuaishou workbook has traceable product parameters, preferring product-ID-bound platform data and falling back to existing visual-analysis evidence.

**Architecture:** Extend the conservative Kuaishou response parser to return normalized platform parameter rows. Add a focused fallback module that converts the existing product dossier and SKU metadata into evidence-backed rows only when platform rows are absent. The batch runner persists the completed parameter metadata before the existing workbook exporter reads it.

**Tech Stack:** Python 3.11+, `unittest`, Playwright response fixtures, JSON manifests, existing `@oai/artifact-tool` workbook exporter.

## Global Constraints

- Only Kuaishou `direct_link` behavior changes; other platforms keep existing behavior.
- Platform parameter rows always override visual-analysis rows.
- The visual fallback reuses `product-dossier.json`; it makes no additional model request.
- Inferred rows must say `图片识别，待核验`; unknown facts must not be invented.
- If no evidence exists, export one `待人工补充` status row instead of an empty parameter sheet.
- Completed parameter metadata is written back to the current `direct-manifest.json`.
- Use the Wanxiang browser for the final live Kuaishou verification.

---

### Task 1: Extract product-ID-bound platform parameters

**Files:**
- Modify: `kuaishou_collector.py:99-222`
- Test: `test_kuaishou_collector.py:20-113`

**Interfaces:**
- Consumes: `extract_product_payload(payload, base_url="", product_id="")` existing inputs.
- Produces: `result["productParameters"]: list[dict[str, str]]` with `name`, `value`, `source`, and `handling`.
- Produces: `merge_product_payloads()` preserving first-seen normalized parameter names.

- [ ] **Step 1: Write failing tests for ordinary and componentized payloads**

Add literal fixtures that prove identity scoping and supported row shapes:

```python
def test_extract_product_payload_keeps_only_current_product_parameters(self):
    payload = {
        "goodsInfo": {
            "goodsId": "26065497098904",
            "goodsParams": [
                {"attrName": "净含量", "attrValue": "800ml"},
                {"name": "包装形式", "value": "泵瓶"},
                {"name": "净含量", "value": "重复值"},
            ],
        },
        "recommendations": [{
            "goodsId": "999",
            "attributes": [{"name": "材质", "value": "不应采集"}],
        }],
    }

    result = extract_product_payload(payload, product_id="26065497098904")

    self.assertEqual(result["productParameters"], [
        {"name": "净含量", "value": "800ml", "source": "platform_api", "handling": "快手平台原值"},
        {"name": "包装形式", "value": "泵瓶", "source": "platform_api", "handling": "快手平台原值"},
    ])


def test_componentized_parameters_require_matching_primary_identity(self):
    payload = {"data": {"data": {
        "idToolbar": {"fields": {"data": {"itemId": "26065497098904"}}},
        "idGoodsParams": {"fields": {"data": {"propertyList": [
            {"propertyName": "适用发质", "propertyValue": "一般发质"},
        ]}}},
    }}}

    matched = extract_product_payload(payload, product_id="26065497098904")
    mismatched = extract_product_payload(payload, product_id="999")

    self.assertEqual(matched["productParameters"][0]["value"], "一般发质")
    self.assertEqual(mismatched["productParameters"], [])
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest `
  test_kuaishou_collector.KuaishouCollectorTests.test_extract_product_payload_keeps_only_current_product_parameters `
  test_kuaishou_collector.KuaishouCollectorTests.test_componentized_parameters_require_matching_primary_identity
```

Expected: both tests fail because `productParameters` is absent.

- [ ] **Step 3: Implement minimal normalized parameter extraction**

Add narrow helpers in `kuaishou_collector.py`:

```python
PARAMETER_CONTAINER_KEYS = {
    "productparameters", "goodsparams", "attributes", "specifications",
    "propertylist", "paramlist", "attributelist",
}
PARAMETER_NAME_KEYS = ("name", "attrname", "propertyname", "specname", "key", "label")
PARAMETER_VALUE_KEYS = ("value", "attrvalue", "propertyvalue", "specvalue", "text")


def _parameter_rows(value: object) -> list[dict[str, str]]:
    candidates = value if isinstance(value, list) else [value]
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        normalized = {
            str(key).replace("_", "").replace("-", "").lower(): child
            for key, child in candidate.items()
        }
        name = next(
            (_parameter_text(normalized.get(key)) for key in PARAMETER_NAME_KEYS if _parameter_text(normalized.get(key))),
            "",
        )
        content = next(
            (_parameter_text(normalized.get(key)) for key in PARAMETER_VALUE_KEYS if _parameter_text(normalized.get(key))),
            "",
        )
        if not name or not content or name in seen or content.startswith(("http://", "https://")):
            continue
        seen.add(name)
        rows.append({
            "name": name,
            "value": content,
            "source": "platform_api",
            "handling": "快手平台原值",
        })
    return rows
```

Implement `_parameter_text()` to accept strings and scalar numeric values only and to collapse whitespace. Do not recursively treat arbitrary key/value dictionaries as parameters. Traverse inside a value only after its parent key matches `PARAMETER_CONTAINER_KEYS`. For componentized data, inspect only components whose normalized name contains `param`, `spec`, or `attribute` after `_componentized_media_sources()` has already confirmed the primary identity. Extend `extract_product_payload()` and `merge_product_payloads()` to include `productParameters`.

- [ ] **Step 4: Run collector tests and verify GREEN**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest test_kuaishou_collector
```

Expected: all Kuaishou collector tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add kuaishou_collector.py test_kuaishou_collector.py
git commit -m "feat: extract verified kuaishou parameters"
```

---

### Task 2: Preserve platform parameters in Kuaishou metadata

**Files:**
- Modify: `store_insight_collector.py:1248-1303`
- Test: `test_kuaishou_integration.py:108-164`

**Interfaces:**
- Consumes: `extracted["productParameters"]` from Task 1.
- Produces: existing metadata keys `parameter_source_product_id`, `parameter_status`, `parameter_error`, `product_parameters`, and `product_parameters_text`.

- [ ] **Step 1: Extend the adapter test with platform parameters**

Add `goodsParams` to the existing response fixture and assertions:

```python
"goodsParams": [
    {"attrName": "净含量", "attrValue": "800ml"},
    {"attrName": "包装形式", "attrValue": "泵瓶"},
],
```

```python
self.assertEqual(metadata["parameter_status"], "complete")
self.assertEqual(metadata["parameter_source_product_id"], "26065497098904")
self.assertEqual(metadata["product_parameters"], [
    {"name": "净含量", "value": "800ml", "source": "platform_api", "handling": "快手平台原值"},
    {"name": "包装形式", "value": "泵瓶", "source": "platform_api", "handling": "快手平台原值"},
])
```

- [ ] **Step 2: Run the adapter test and verify RED**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest `
  test_kuaishou_integration.KuaishouIntegrationTests.test_kuaishou_adapter_collects_public_media_without_fabricating_sku_images
```

Expected: `parameter_status` remains `not_found`.

- [ ] **Step 3: Build complete metadata when platform rows exist**

In `collect_kuaishou_payload()`, replace the unconditional empty parameter metadata with:

```python
parameters = list(extracted.get("productParameters") or [])
parameter_metadata = (
    complete_parameter_metadata(product_id, parameters)
    if parameters
    else empty_parameter_metadata(
        product_id,
        "not_found",
        "快手公开页面未提供稳定的结构化商品参数",
    )
)
```

Pass `parameter_metadata` into the existing metadata mapping. `complete_parameter_metadata()` already retains the supplied row dictionaries, so `parameter_collector.py` does not change.

- [ ] **Step 4: Run integration and parameter tests**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest `
  test_kuaishou_integration test_parameter_collector
```

Expected: all selected tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add store_insight_collector.py test_kuaishou_integration.py
git commit -m "feat: retain kuaishou platform parameters"
```

---

### Task 3: Derive evidence-backed visual fallback parameters

**Files:**
- Create: `kuaishou_parameters.py`
- Create: `test_kuaishou_parameters.py`

**Interfaces:**
- Produces: `ensure_kuaishou_product_parameters(source_document: dict[str, Any], dossier_path: Path) -> dict[str, Any]`.
- Preserves: any non-empty `source_document["product_parameters"]` without reading or replacing it.
- Returns: a copied document with `parameter_status`, `parameter_error`, `product_parameters`, and `product_parameters_text` updated.

- [ ] **Step 1: Write failing tests for visual fallback, platform priority, and explicit manual review**

Use real temporary JSON files and literal expected rows:

```python
def test_visual_dossier_fills_empty_kuaishou_parameters(self):
    dossier_path = self.root / "product-dossier.json"
    dossier_path.write_text(json.dumps({
        "observations": [{
            "source_index": 1,
            "colors": ["white bottle", "gold label"],
        }],
        "dossier": {
            "anchor_identity": {
                "source_index": 1,
                "object": "one rectangular pump shampoo bottle",
                "visible_product_labeling": ["PEPTIDE KERATIN SHAMPOO", "800ml"],
                "brand_or_mark": "unclear",
            },
            "confirmed_components": ["rectangular bottle", "pump dispenser"],
            "materials_and_textures": [{
                "component": "bottle body",
                "confirmed_visible_material_or_texture": "smooth plastic-like surface",
            }],
        },
    }), encoding="utf-8")
    source = {
        "product_parameters": [],
        "sku_variants": [{"spec_text": "800ml*1瓶", "color_text": "白色"}],
    }

    result = ensure_kuaishou_product_parameters(source, dossier_path)

    self.assertEqual(result["parameter_status"], "inferred")
    self.assertTrue(result["product_parameters"])
    self.assertTrue(all(
        row["handling"] == "图片识别，待核验"
        for row in result["product_parameters"]
    ))
    self.assertIn(
        {"name": "可见规格/容量", "value": "800ml", "source": "visual_analysis", "handling": "图片识别，待核验"},
        result["product_parameters"],
    )


def test_platform_parameters_are_never_replaced(self):
    source = {"product_parameters": [{
        "name": "净含量", "value": "800ml", "source": "platform_api", "handling": "快手平台原值",
    }]}
    self.assertEqual(
        ensure_kuaishou_product_parameters(source, self.root / "missing.json")["product_parameters"],
        source["product_parameters"],
    )


def test_missing_dossier_requires_manual_review(self):
    result = ensure_kuaishou_product_parameters({}, self.root / "missing.json")
    self.assertEqual(result["parameter_status"], "needs_review")
    self.assertEqual(result["product_parameters"][0]["handling"], "待人工补充")
```

- [ ] **Step 2: Run the new module tests and verify RED**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest test_kuaishou_parameters
```

Expected: import failure because `kuaishou_parameters.py` does not exist.

- [ ] **Step 3: Implement the pure fallback module**

Implement `ensure_kuaishou_product_parameters()` with small helpers that:

- read valid JSON or use an empty dossier;
- select only the observation whose `source_index` equals the anchor source index;
- flatten strings, scalar lists, and known dossier dictionaries into readable text;
- detect capacity using case-insensitive units `ml`, `l`, `g`, `kg`, `克`, `千克`, `毫升`, `升`, `片`, `抽`, `卷`, `瓶`, `袋`, `盒`;
- omit unclear brands and promotional claims;
- deduplicate rows by normalized name and value;
- preserve existing platform rows unchanged;
- write one manual-review row when no evidence remains.

The function returns a copy and never mutates its input dictionary.

- [ ] **Step 4: Run the fallback tests and verify GREEN**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest test_kuaishou_parameters
```

Expected: all fallback tests pass.

- [ ] **Step 5: Commit Task 3**

```powershell
git add kuaishou_parameters.py test_kuaishou_parameters.py
git commit -m "feat: derive kuaishou visual parameters"
```

---

### Task 4: Persist fallback parameters before workbook export

**Files:**
- Modify: `batch_workflow.py:1645-1648,2528-2563`
- Test: `test_batch_workflow.py`

**Interfaces:**
- Consumes: `ensure_kuaishou_product_parameters()` from Task 3.
- Persists: updated `source_document` to the existing `source_manifest` path.
- Exports: each parameter row's own `handling`, defaulting to `采集原值` for legacy rows.

- [ ] **Step 1: Write a failing batch integration test**

Follow the existing mocked local workflow pattern. Use a `DirectLinkBatchItem` whose platform is `kuaishou`, a source manifest with no parameters, and a dossier written by the mocked workflow run. Capture the manifest read by `export_product_workbook`:

```python
def test_kuaishou_batch_persists_visual_parameters_before_export(self):
    workbook = self.root / "kuaishou.xlsx"
    workbook.write_bytes(b"workbook")
    output = self.root / "output"
    item = DirectLinkBatchItem(
        1,
        1,
        "https://app.kwaixiaodian.com/web/kwaishop-goods-detail-page-app?id=26065497098904",
        "kuaishou",
        "快手商品",
    )
    source_manifest = self._shared_local_manifest(output / "001-row-0001")
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    source.update({"product_parameters": [], "sku_variants": []})
    source_manifest.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")

    def run_workflow(*_args, **_kwargs):
        dossier = output / "001-row-0001" / "generated" / "product-dossier.json"
        dossier.parent.mkdir(parents=True, exist_ok=True)
        dossier.write_text(json.dumps({
            "observations": [],
            "dossier": {"anchor_identity": {
                "source_index": 1,
                "object": "one pump shampoo bottle",
                "visible_product_labeling": ["800ml"],
            }},
        }), encoding="utf-8")
        return self._shared_generation_records(1, 0, 0)

    exported_source = {}
    def export(path, _item, manifest, *_args, **_kwargs):
        exported_source.update(json.loads(manifest.read_text(encoding="utf-8")))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"xlsx")
        return path

    runner = BatchRunner(ApiSettings("https://api.example", "vision", "image"), self.root, self.root, batch_mode="direct_link")
    with (
        patch("batch_workflow.extract_direct_link_items", return_value=[item]),
        patch("batch_workflow.restore_collected_manifest", return_value=(source_manifest, 1)),
        patch("batch_workflow.ProductTitleClient.generate", return_value={"long_title": "快手商品长标题" * 5, "short_title": "快手商品"}),
        patch("batch_workflow.WorkflowRunner.run", side_effect=run_workflow),
        patch("batch_workflow.upload_generation_records", side_effect=lambda rows, _uploader: rows),
        patch("batch_workflow.export_product_workbook", side_effect=export),
    ):
        result = runner.run(workbook, output)

    self.assertEqual(result[0]["status"], "completed")
    self.assertEqual(exported_source["parameter_status"], "inferred")
    self.assertTrue(exported_source["product_parameters"])
```

Also extend `test_exports_seven_sheets_and_requested_sku_fields()` with a parameter row containing `handling: "图片识别，待核验"` and assert the generated workbook cell in column D keeps that value.

- [ ] **Step 2: Run both tests and verify RED**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest `
  test_batch_workflow.BatchWorkflowTests.test_kuaishou_batch_persists_visual_parameters_before_export `
  test_batch_workflow.BatchWorkflowTests.test_exports_seven_sheets_and_requested_sku_fields
```

Expected: the batch test sees no inferred parameters and the workbook test sees `采集原值`.

- [ ] **Step 3: Call the fallback and preserve per-row handling**

After generation records are finalized and before `summarize_generation_result()`/Excel export, add:

```python
if isinstance(item, (DirectLinkBatchItem, DirectReplaceBatchItem)) and item.platform == "kuaishou":
    source_document = ensure_kuaishou_product_parameters(
        source_document,
        item_root / "generated" / "product-dossier.json",
    )
    source_manifest.write_text(
        json.dumps(source_document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

Change the export mapping to:

```python
parameters = [
    {
        "type": "商品参数",
        "name": row.get("name", ""),
        "value": row.get("value", ""),
        "handling": row.get("handling") or "采集原值",
    }
    for row in source.get("product_parameters", [])
]
```

- [ ] **Step 4: Run focused and complete Python tests**

Run:

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest `
  test_batch_workflow.BatchWorkflowTests.test_kuaishou_batch_persists_visual_parameters_before_export `
  test_batch_workflow.BatchWorkflowTests.test_exports_seven_sheets_and_requested_sku_fields

& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m unittest discover -p 'test_*.py'
```

Expected: focused tests pass and the complete suite reports zero failures/errors.

- [ ] **Step 5: Commit Task 4**

```powershell
git add batch_workflow.py test_batch_workflow.py
git commit -m "fix: require kuaishou workbook parameters"
```

---

### Task 5: Verify the real Kuaishou workflow without regenerating images

**Files:**
- Verify: existing real task under `outputs/batches/20260817-1155-kuaishou-live-full`
- Produce: a new re-exported workbook in the isolated worktree's output directory

**Interfaces:**
- Uses: the supplied Kuaishou URL and existing successful 25 image generation records.
- Proves: parameter persistence and workbook export, without repeating successful image requests.

- [ ] **Step 1: Run static and frontend checks**

```powershell
& 'D:\product-image-workflow\.venv\Scripts\python.exe' -m py_compile `
  kuaishou_collector.py kuaishou_parameters.py store_insight_collector.py batch_workflow.py `
  test_kuaishou_collector.py test_kuaishou_parameters.py test_kuaishou_integration.py test_batch_workflow.py

npm.cmd run check --prefix frontend
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 2: Reuse the completed real run and re-export without image requests**

Copy only the source manifest into a new verification directory, complete its parameters from the existing dossier, and call the existing workbook exporter with the existing 25 generation records:

```python
import json
import shutil
from pathlib import Path

from batch_workflow import DirectLinkBatchItem, export_product_workbook
from kuaishou_parameters import ensure_kuaishou_product_parameters

source_root = Path(r"D:\product-image-workflow\outputs\batches\20260817-1155-kuaishou-live-full\001-row-0001")
verify_root = Path(r"D:\product-image-workflow-kuaishou-parameters\outputs\verification\kuaishou-parameters")
verify_root.mkdir(parents=True, exist_ok=True)
manifest_path = verify_root / "direct-manifest.json"
shutil.copy2(source_root / "collected" / "direct-manifest.json", manifest_path)
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
manifest = ensure_kuaishou_product_parameters(
    manifest,
    source_root / "generated" / "product-dossier.json",
)
manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
records = json.loads((source_root / "generated" / "analysis.json").read_text(encoding="utf-8"))["records"]
titles = json.loads((source_root / "titles.json").read_text(encoding="utf-8"))
item = DirectLinkBatchItem(
    1,
    1,
    "https://app.kwaixiaodian.com/web/kwaishop-goods-detail-page-app?id=26065497098904",
    "kuaishou",
    "商品-001",
)
export_product_workbook(
    verify_root / "商品-001-参数验证.xlsx",
    item,
    manifest_path,
    records,
    titles,
    Path(r"D:\product-image-workflow-kuaishou-parameters"),
    include_metadata_only_skus=True,
)
```

Run this script through the repository virtual environment. It calls no image-generation API and starts no browser. If a fresh collection later becomes necessary, connect only to Wanxiang and report before proceeding.

- [ ] **Step 3: Inspect final manifest and workbook**

Verify with a read-only script:

- `product_parameters` has at least one row;
- `parameter_status` is `complete`, `inferred`, or `needs_review`;
- every row has a non-empty `handling` value;
- the workbook “商品参数” sheet has a header plus at least one data row;
- the workbook handling column matches the manifest source;
- existing main/SKU/detail counts remain 10/6/9 and no generated image changed.

- [ ] **Step 4: Run final repository checks**

```powershell
git status --short --branch
git log --oneline --decorate -5
```

Expected: only intentionally ignored runtime outputs remain outside Git; the branch contains the design and implementation commits.
