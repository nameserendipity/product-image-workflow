# Competitor Reference Generation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit competitor-reference generation mode, build a multi-view product dossier for varied detail images, and reorganize the React interface into Link Generation and Spreadsheet Generation sidebar views.

**Architecture:** Persist `generation_mode` in the Agent session and resolve a concrete identity image before generation. Keep the current replacement pipeline for `own_product`; for `competitor_reference`, use the first collected main image as the identity anchor, analyze all collected images concurrently, and attach a distinct view plan to each detail task. Keep React/Vite and apply a Vue-style operations-console layout without adding Vue or a router.

**Tech Stack:** Python 3, `unittest`, `concurrent.futures`, existing OpenAI-compatible vision/image relay, React 18, TypeScript, Vite, Lucide React, CSS.

## Global Constraints

- Treat this as an experiment: do not run `git commit`, create a branch, or alter Git history.
- Do not edit or expose `local_settings.json`.
- Preserve the existing `own_product` replacement behavior and spreadsheet workflow.
- The sidebar shows only `链接生图` and `表格生图`.
- Direct-reference mode does not require an uploaded product image.
- The first valid collected main image is the overall identity anchor.
- All valid main, SKU, and detail images may contribute to multi-view analysis.
- Inferred views cannot invent structural components and carry `inferred_view=true`.
- Direct-reference detail generation produces 6-15 outputs.
- Remove competitor brand, Logo, store name, watermark, and prohibited claims.
- Do not copy ordinary competitor selling copy; preserve its layout role and generate generic, verifiable copy.
- Use red-green TDD. End each task with an uncommitted diff checkpoint instead of a commit.

---

### Task 1: Persist an explicit generation mode

**Files:**
- Modify: `agent_flow.py:97-330`
- Modify: `web_app.py:334-455`
- Modify: `web_app.py:655-760`
- Test: `test_agent_flow.py`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: `AgentSession`, session JSON, `/api/status`, `/api/chat`.
- Produces: `GenerationMode`, `AgentSession.generation_mode`, and `POST /api/generation-mode`.

- [ ] **Step 1: Write failing Agent state tests**

```python
def test_generation_mode_defaults_to_competitor_reference(self):
    self.assertEqual(AgentSession().generation_mode, "competitor_reference")

def test_agent_can_switch_to_own_product_mode(self):
    session = AgentSession()
    session.handle("使用我上传的产品图替换")
    self.assertEqual(session.generation_mode, "own_product")

def test_restored_generation_mode_is_preserved(self):
    session = AgentSession(generation_mode="competitor_reference")
    self.assertEqual(session.generation_mode, "competitor_reference")
```

- [ ] **Step 2: Run tests and verify red**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow.AgentSessionTests.test_generation_mode_defaults_to_competitor_reference test_agent_flow.AgentSessionTests.test_agent_can_switch_to_own_product_mode test_agent_flow.AgentSessionTests.test_restored_generation_mode_is_preserved
```

Expected: failures because `generation_mode` does not exist.

- [ ] **Step 3: Add the mode to `AgentSession`**

```python
GenerationMode = Literal["own_product", "competitor_reference"]

@dataclass
class AgentSession:
    generation_mode: GenerationMode = "competitor_reference"
```

Extend local and LLM intent handling. “不上传产品图 / 直接参考对标商品 / 按对标商品直接生成” selects `competitor_reference`; “使用我方产品图 / 用我上传的产品图 / 替换成我的商品” selects `own_product`. Do not delete a retained upload when switching modes.

- [ ] **Step 4: Write failing endpoint tests**

```python
def test_generation_mode_endpoint_persists_explicit_mode(self):
    state = web_app.AppState()
    handler = object.__new__(web_app.RequestHandler)
    handler._json_body = Mock(return_value={"mode": "competitor_reference"})
    handler._json = Mock()
    with patch.object(web_app, "STATE", state):
        handler._set_generation_mode()
    self.assertEqual(state.agent.generation_mode, "competitor_reference")
```

Also test invalid mode returns HTTP 400.

- [ ] **Step 5: Implement persistence and endpoint**

Add `POST /api/generation-mode`, restore the field from session JSON with `competitor_reference` as the backward-compatible default, and include it in `task_signature()`.

```python
def _set_generation_mode(self) -> None:
    mode = str(self._json_body().get("mode", ""))
    if mode not in {"own_product", "competitor_reference"}:
        self._json({"error": "生成模式不受支持。"}, HTTPStatus.BAD_REQUEST)
        return
    with STATE.lock:
        STATE.agent.generation_mode = mode
        STATE.reset_generation()
        STATE.save_session()
    self._maybe_auto_generate()
    self._json({"accepted": True, "status": STATE.status()})
```

- [ ] **Step 6: Verify Task 1**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow test_web_app
git diff --check
git status --short
```

Expected: tests pass and files remain uncommitted.

---

### Task 2: Resolve identity images without requiring an upload

**Files:**
- Modify: `image_workflows.py:45-75`
- Modify: `image_workflows.py:500-535`
- Modify: `web_app.py:427-438`
- Modify: `web_app.py:1255-1338`
- Test: `test_agent_flow.py`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: manifest, optional product upload, generation mode.
- Produces: `resolve_identity_image(manifest_path, product_image, generation_mode) -> Path` and mode-aware readiness.

- [ ] **Step 1: Write failing resolver tests**

```python
def test_direct_reference_uses_first_valid_main_image(self):
    self.assertEqual(resolve_identity_image(manifest, None, "competitor_reference"), first_main.resolve())

def test_direct_reference_never_falls_back_to_sku(self):
    with self.assertRaisesRegex(ValueError, "缺少商品身份主图"):
        resolve_identity_image(sku_only_manifest, None, "competitor_reference")

def test_own_product_requires_uploaded_image(self):
    with self.assertRaisesRegex(ValueError, "请先上传我方产品图"):
        resolve_identity_image(manifest, None, "own_product")
```

- [ ] **Step 2: Run tests and verify red**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow.AgentSessionTests.test_direct_reference_uses_first_valid_main_image test_agent_flow.AgentSessionTests.test_direct_reference_never_falls_back_to_sku test_agent_flow.AgentSessionTests.test_own_product_requires_uploaded_image
```

Expected: import or attribute failures.

- [ ] **Step 3: Implement resolver and shared manifest path handling**

```python
def resolve_identity_image(manifest_path: Path, product_image: Path | None, generation_mode: str) -> Path:
    if generation_mode == "own_product":
        if product_image is None or not product_image.is_file():
            raise ValueError("请先上传我方产品图。")
        return product_image.resolve()
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in document.get("images", []):
        if entry.get("type") == "main":
            candidate = resolve_manifest_image_path(manifest_path, entry)
            if candidate.is_file():
                return candidate.resolve()
    raise ValueError("缺少商品身份主图，无法直接参考对标商品生成。")
```

Reuse `resolve_manifest_image_path` in `load_manifest_tasks`.

- [ ] **Step 4: Write failing web readiness tests**

Prove `_begin_generation` and `_maybe_auto_generate` accept `product_image is None` when direct mode has a first main image, while own-product mode still rejects it.

- [ ] **Step 5: Make readiness mode-aware**

Resolve `identity_image` before starting the thread, pass it with mode to the runner, and retain `STATE.product_image` only as an optional upload. Include resolved identity and mode in the task signature.

- [ ] **Step 6: Verify Task 2**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow test_web_app
git diff --check
```

Expected: direct mode starts without upload; own-product mode remains guarded.

---

### Task 3: Build a multi-view product dossier and detail plan

**Files:**
- Modify: `image_workflows.py:45-75`
- Modify: `image_workflows.py:328-420`
- Modify: `image_workflows.py:500-575`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: `ApiSettings`, manifest images, anchor image, requested detail count, cancellation event.
- Produces: `IdentitySource`, `DetailViewPlan`, `load_identity_sources`, concurrent observations, dossier, and validated detail plans.

- [ ] **Step 1: Write failing source enumeration tests**

```python
def test_identity_sources_include_all_valid_collected_images(self):
    sources = load_identity_sources(manifest)
    self.assertEqual([item.category for item in sources], ["main", "main", "sku", "detail"])
    self.assertTrue(sources[0].is_anchor)
```

- [ ] **Step 2: Implement source and plan dataclasses**

```python
@dataclass(frozen=True)
class IdentitySource:
    index: int
    category: str
    path: Path
    is_anchor: bool = False

@dataclass(frozen=True)
class DetailViewPlan:
    ordinal: int
    view_type: str
    focus: str
    supporting_source_index: int | None
    inferred_view: bool
    prohibited_inventions: tuple[str, ...]
```

`load_identity_sources` preserves manifest order, excludes missing paths, and marks only the first valid main image as anchor.

- [ ] **Step 3: Write failing failure-isolation tests**

```python
def test_identity_analysis_skips_one_failed_source(self):
    def analyze(source):
        if source.index == 2:
            raise RuntimeError("bad")
        return {"index": source.index}
    observations, failures = analyze_identity_sources(sources, analyze, concurrency=None)
    self.assertEqual([item["index"] for item in observations], [1, 3, 4])
    self.assertEqual(failures[0]["source_index"], 2)
```

- [ ] **Step 4: Implement per-image concurrent analysis**

`VisionClient.analyze_identity_source` returns strict JSON fields: `source_index`, `category`, `visible_views`, `silhouette`, `proportions`, `colors`, `materials`, `visible_components`, `local_details`, `branding_and_risks`, and `uncertainties`. Use `ThreadPoolExecutor`; preserve source order after futures complete; one failed source does not discard successful observations.

- [ ] **Step 5: Write failing dossier and plan validation tests**

Test anchor priority, valid supporting indices, unique ordinals, 6-15 plans, non-repeated view mix, and inferred marking:

```python
def test_detail_plans_mark_unseen_side_as_inferred(self):
    plans = validate_detail_view_plans(raw_plans, 6, known_views={"front"}, valid_source_indices={1})
    side = next(item for item in plans if item.view_type == "side")
    self.assertTrue(side.inferred_view)
    self.assertIn("ports", side.prohibited_inventions)
```

- [ ] **Step 6: Implement dossier synthesis and plan validation**

Send ordered observations to one synthesis request requiring: `anchor_identity`, `confirmed_views`, `confirmed_components`, `materials_and_textures`, `conflicts`, `uncertainties`, and `detail_view_plans`.

Use:

```python
target_count = min(15, max(6, requested_or_collected_count))
```

Reject duplicate ordinals and unknown source indices. Mark a plan inferred whenever its view is absent from `confirmed_views`. When structural views are unavailable, fill remaining plans with material, texture, workmanship, scale, or usage details rather than invented components.

- [ ] **Step 7: Verify Task 3**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow
git diff --check
```

Expected: source enumeration, failure isolation, dossier, and view-plan tests pass without network access.

---

### Task 4: Make model prompts and image inputs mode-aware

**Files:**
- Modify: `image_workflows.py:250-408`
- Modify: `image_workflows.py:486-497`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: mode, identity anchor, task reference, optional support image, dossier, view plan.
- Produces: mode-aware analysis/generation prompts and ordered variable image lists.

- [ ] **Step 1: Write failing prompt tests**

```python
def test_direct_reference_prompt_preserves_structure_and_removes_branding(self):
    prompt = compose_generation_prompt(analysis, "detail", "competitor_reference", dossier, view_plan)
    self.assertIn("preserve the competitor product structure", prompt)
    self.assertIn("remove competitor brands", prompt)
    self.assertIn("do not copy ordinary competitor selling copy", prompt)
    self.assertNotIn("replace every competitor product", prompt)

def test_own_product_prompt_keeps_replacement_contract(self):
    prompt = compose_generation_prompt(analysis, "main", "own_product", None, None)
    self.assertIn("only source of product identity", prompt)
    self.assertIn("replacing every competitor product", prompt)
```

- [ ] **Step 2: Run tests and verify red**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow.EcommerceWorkflowFidelityTests.test_direct_reference_prompt_preserves_structure_and_removes_branding test_agent_flow.EcommerceWorkflowFidelityTests.test_own_product_prompt_keeps_replacement_contract
```

Expected: signature mismatch or missing direct-reference rules.

- [ ] **Step 3: Split prompt contracts**

Keep existing wording for `own_product`. Direct-reference wording identifies Image 1 as the first collected main image, preserves confirmed structure/material/color, removes brand/Logo/store/watermark/prohibited claims, rewrites ordinary copy from `copy_plan`, and forbids invented ports, buttons, pockets, openings, accessories, and controls. Detail prompts append dossier, view plan, and inferred-view warning.

- [ ] **Step 4: Write failing image-order tests**

```python
def test_generation_images_keep_anchor_support_reference_order(self):
    self.assertEqual(ordered_generation_images(anchor, support, reference), [anchor, support, reference])

def test_generation_images_deduplicate_same_path(self):
    self.assertEqual(ordered_generation_images(anchor, anchor, reference), [anchor, reference])
```

- [ ] **Step 5: Implement variable image inputs**

```python
def ordered_generation_images(anchor: Path, support: Path | None, reference: Path) -> list[Path]:
    ordered = [anchor, *([support] if support else []), reference]
    return list(dict.fromkeys(path.resolve() for path in ordered))
```

Change `ImageClient.generate` to accept `images: list[Path]`, and ensure the vision instruction names image roles in their actual order.

- [ ] **Step 6: Verify Task 4**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow
git diff --check
```

Expected: both prompt contracts and multipart image ordering pass.

---

### Task 5: Integrate the dossier into workflow execution

**Files:**
- Modify: `image_workflows.py:500-680`
- Modify: `web_app.py:1274-1375`
- Test: `test_agent_flow.py`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: resolved identity, mode, manifest tasks, multi-view dossier.
- Produces: extended `ImageTask`, persisted dossier, varied detail tasks, partial-failure behavior, audit fields.

- [ ] **Step 1: Write failing task-planning tests**

Extend the expected task contract:

```python
@dataclass(frozen=True)
class ImageTask:
    category: str
    ordinal: int
    source_path: Path
    supporting_path: Path | None = None
    view_plan: DetailViewPlan | None = None
    inferred_view: bool = False
```

Test direct-reference detail planning creates 6-15 tasks and maps supporting indices to paths. Test own-product mode retains existing counts and does not require a dossier.

- [ ] **Step 2: Run planning tests and verify red**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow.AgentSessionTests.test_direct_reference_detail_tasks_use_view_plans test_agent_flow.AgentSessionTests.test_own_product_tasks_keep_existing_counts
```

Expected: missing task fields and mode-aware planning.

- [ ] **Step 3: Build the dossier once per run**

Extend `WorkflowRunner.run` with `generation_mode: str = "own_product"` and `identity_image: Path | None = None`. When direct mode selects `detail`, load every source, emit `identity_analyzing`, analyze concurrently, synthesize once, save `product-dossier.json`, and construct detail tasks from validated plans. Never rebuild the dossier per image.

- [ ] **Step 4: Write failing partial-failure and cancellation tests**

Prove total dossier failure removes only detail tasks while main/SKU continue. Prove cancellation during identity analysis prevents image generation and records cancellation events.

- [ ] **Step 5: Integrate per-task execution**

```python
analysis = VisionClient(settings).analyze(
    identity_image,
    task.source_path,
    task.category,
    generation_mode=generation_mode,
    dossier=dossier,
    view_plan=task.view_plan,
)
images = ordered_generation_images(identity_image, task.supporting_path, task.source_path)
image_bytes = ImageClient(settings).generate(images, prompt)
```

Persist `generation_mode`, `identity_path`, `supporting_path`, `view_type`, `detail_focus`, `inferred_view`, and `product_dossier_path` in generation records.

- [ ] **Step 6: Add user-visible progress text**

Use logs and Agent status without adding a fourth workflow lane:

```text
正在建立多视角商品档案（N 张素材）
多视角商品档案完成，正在生成不同角度详情图
详情图多视角分析失败，主图和 SKU 图继续运行
```

- [ ] **Step 7: Verify Task 5**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_agent_flow test_web_app
git diff --check
```

Expected: integration, partial failure, cancellation, and audit records pass.

---

### Task 6: Rebuild the React interface as a two-view workbench

**Files:**
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/styles.css`
- Modify: `web_app.py:483-528`
- Test: `test_web_app.py`

**Interfaces:**
- Consumes: mode in `AppStatus`, `/api/generation-mode`, current link/batch endpoints.
- Produces: two-entry sidebar, Link Generation view, Spreadsheet Generation view, explicit mode control, responsive task-first layout.

- [ ] **Step 1: Write failing status contract tests**

Assert status includes:

```json
{
  "agent": {"generation_mode": "competitor_reference"},
  "identity_source": "collected_main",
  "identity_ready": true
}
```

`identity_source` becomes `uploaded_product` in own-product mode.

- [ ] **Step 2: Extend TypeScript contracts**

```typescript
export type GenerationMode = 'own_product' | 'competitor_reference';

export interface AgentState {
  generation_mode: GenerationMode;
}
```

Retain existing fields and add `identity_source` plus `identity_ready` to `AppStatus`.

- [ ] **Step 3: Add two-view local navigation**

```typescript
type WorkspaceView = 'link' | 'batch';
const [activeView, setActiveView] = useState<WorkspaceView>(
  window.location.hash === '#batch' ? 'batch' : 'link',
);
```

Render exactly two sidebar controls using Lucide `Link2` and `FileSpreadsheet`. Do not render placeholder Agent or settings entries.

- [ ] **Step 4: Build the task-first Link Generation view**

Layout:

```text
sidebar
topbar
main column: mode, link, conditional upload/identity status, workflow lanes, progress
right rail: task Agent, stop controls, folders, collapsible browser/API controls
```

Mode control posts to `/api/generation-mode`. Direct mode displays “对标主图第 1 张” and does not require upload. Own-product mode displays the existing upload control and required state. Switching modes does not delete the retained upload.

- [ ] **Step 5: Build the Spreadsheet Generation view**

Move the existing Excel queue, progress, continue/stop actions, output-folder action, and batch events into the `batch` view without changing backend batch behavior.

- [ ] **Step 6: Apply Vue-style console design using current CSS**

Use a fixed dark neutral-green sidebar, compact 5-6px radii, restrained white surfaces, teal primary actions, amber warnings, red stop actions, and stable responsive columns. Do not use gradients, decorative blobs, nested cards, oversized headings, Vue, React Router, Tailwind, or another component library. On mobile, convert the sidebar to a two-item top navigation and keep all Chinese labels contained.

- [ ] **Step 7: Type-check and build**

```powershell
& 'D:\NodeJS\npm.cmd' --prefix frontend run check
& 'D:\NodeJS\npm.cmd' --prefix frontend run build
```

Expected: TypeScript and Vite exit 0 and update `web/`.

- [ ] **Step 8: Verify Task 6**

```powershell
.\.venv\Scripts\python.exe -m unittest -v test_web_app
git diff --check
```

Expected: status contracts and frontend build pass; no commit exists.

---

### Task 7: End-to-end verification

**Files:**
- Modify only when verification identifies a defect in files listed above.
- Verify: `outputs/`, generated `web/`, and `http://127.0.0.1:8765/`.

**Interfaces:**
- Consumes: complete experimental implementation.
- Produces: fresh regression evidence, responsive screenshots, one controlled real workflow result.

- [ ] **Step 1: Run the complete Python suite**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
```

Expected: zero failures and zero errors.

- [ ] **Step 2: Run a fresh frontend check and build**

```powershell
& 'D:\NodeJS\npm.cmd' --prefix frontend run check
& 'D:\NodeJS\npm.cmd' --prefix frontend run build
```

Expected: both commands exit 0.

- [ ] **Step 3: Restart only the local web service**

Stop only the process listening on port 8765; do not close the dedicated collection browser. Start:

```powershell
Start-Process -FilePath '.\.venv\Scripts\python.exe' `
  -ArgumentList 'web_app.py','--no-browser' `
  -WorkingDirectory 'C:\Users\Administrator\Documents\Codex\2026-08-07\new-chat' `
  -WindowStyle Hidden
```

Verify `/api/status` returns HTTP 200 with mode and identity fields.

- [ ] **Step 4: Verify responsive layout visually**

Capture approximately 1440x900, 1024x768, and 390x844. Confirm only two navigation entries appear; views switch; mode, Agent, stop/folder controls remain reachable; long URLs and Chinese labels remain contained; mobile navigation is usable.

- [ ] **Step 5: Run one controlled direct-reference workflow**

Use an already logged-in browser session and one test link. Confirm this sequence:

```text
complete multi-file collection
first main selected as identity
all collected images analyzed once for dossier
6-15 varied detail plans created
generation starts without uploaded product image
records include generation_mode and inferred_view
completed images receive OSS URLs when configured
```

If platform verification or risk control appears, stop navigation and wait for the user instead of refreshing.

- [ ] **Step 6: Verify own-product regression**

Switch to own-product mode, confirm generation is blocked until an upload exists, then use an existing test image and verify the uploaded product remains Image 1.

- [ ] **Step 7: Inspect final uncommitted changes**

```powershell
git diff --check
git status --short
git diff --stat
```

Expected: no whitespace errors, no implementation changes to `local_settings.json`, and no commit.

## Execution Notes

- Execute tasks in order; Tasks 3-5 depend on Tasks 1-2.
- Use the current dirty working tree and preserve the experimental multi-file collector and Agent fixes.
- Do not refresh automatically when platform verification appears.
- Unit tests alone do not complete the work; Task 7 requires frontend build evidence and a controlled real direct-reference run.
