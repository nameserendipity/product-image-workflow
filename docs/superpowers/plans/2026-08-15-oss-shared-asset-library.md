# OSS Shared Asset Library Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a private OSS-backed shared asset library that deduplicates and reuses complete Taobao/Tmall competitor-reference jobs across local clients without blocking local output when OSS is unavailable.

**Architecture:** Keep OSS operations, identity parsing, packaging, and cache persistence in four focused root modules. Integrate them at the existing pre-collection and post-generation boundaries in `web_app.py` and `batch_workflow.py`; expose a local HTTP API to a dedicated React library view. Catalog publication is the final visibility boundary, while a conditional OSS lock prevents two clients from publishing the same product.

**Tech Stack:** Python 3.12, `oss2>=2.19,<3`, Pillow, `zipfile`, `hashlib`, JSON, existing `unittest` suite, React 18, TypeScript, Vite, Lucide React.

## Global Constraints

- Shared Catalog supports only Taobao and Tmall identities in `{platform}-{product_id}` form.
- Enable shared behavior only for competitor-reference single-link jobs and `direct_link` batch jobs.
- `direct_replace`, `image_search`, own-product single-link jobs, JD, Douyin, and Kuaishou must retain their existing behavior without shared lookup, locks, or Catalog publication.
- Shared lookup is automatic; there is no enable toggle.
- OSS network, authentication, query, renewal, or upload failure falls back to local completion and never publishes Catalog.
- A valid lock owned by another client is normal contention and must block duplicate work rather than trigger local fallback.
- Publish only complete default jobs: main exactly 10, SKU 3-8, detail 6-15, all planned records completed, and product Excel available.
- Completed Package and Catalog objects are private and cannot be overwritten by an ordinary task.
- Never place AccessKey ID, AccessKey Secret, signed query strings, or permanent public package URLs in logs, API responses, Catalog, Manifest, or Excel.
- Preserve the current `outputs/` layout; reused assets go under `outputs/reused/{product_key}/`.
- Each task uses TDD and commits only the files listed for that task.

---

## File Structure

- Create `product_identity.py`: Taobao/Tmall canonical identity resolution.
- Create `shared_library_client.py`: OSS Catalog, lock, list, publish, preview, and resumable download operations.
- Create `shared_package_builder.py`: completeness validation, preview creation, archives, Manifest/Catalog, and reused-package materialization.
- Create `shared_library_cache.py`: client ID, Catalog cache, and download records.
- Create `frontend/src/SharedLibraryView.tsx`: shared-library page and download actions.
- Modify `web_app.py`: shared state, single-link integration, local API, and private preview proxy.
- Modify `batch_workflow.py`: `direct_link` lookup/reuse/publish integration only.
- Modify `frontend/src/App.tsx`, `frontend/src/types.ts`, and `frontend/src/styles.css`: navigation, state types, and page styling.
- Modify `local_settings.example.json`, `README.md`, `操作说明书.md`, and `build_release.ps1`: configuration, user workflow, and release packaging.
- Create focused tests beside the existing root-level test suite.

---

### Task 1: Resolve Stable Taobao and Tmall Identities

**Files:**
- Create: `product_identity.py`
- Create: `test_product_identity.py`

**Interfaces:**
- Produces: `ProductIdentity(platform, product_id, product_key, source_url, canonical_url)`.
- Produces: `ProductIdentityResolver.resolve(value: str) -> ProductIdentity | None`.
- Raises: `ProductIdentityError` only when a Taobao/Tmall candidate cannot produce a stable ID; returns `None` for platforms outside shared-library scope.

- [x] **Step 1: Write failing direct-link and canonicalization tests**

```python
class ProductIdentityResolverTests(unittest.TestCase):
    def test_taobao_promotional_parameters_share_one_key(self) -> None:
        resolver = ProductIdentityResolver()
        first = resolver.resolve("https://item.taobao.com/item.htm?id=123&spm=a1&skuId=9")
        second = resolver.resolve("https://item.taobao.com/item.htm?pvid=x&id=123")
        self.assertEqual(first.product_key, "taobao-123")
        self.assertEqual(first.product_key, second.product_key)
        self.assertEqual(first.canonical_url, second.canonical_url)

    def test_tmall_uses_a_platform_specific_key(self) -> None:
        identity = ProductIdentityResolver().resolve(
            "https://detail.tmall.com/item.htm?id=456&abbucket=1"
        )
        self.assertEqual(identity.product_key, "tmall-456")
        self.assertEqual(identity.canonical_url, "https://detail.tmall.com/item.htm?id=456")

    def test_non_shared_platform_returns_none(self) -> None:
        self.assertIsNone(ProductIdentityResolver().resolve("https://item.jd.com/123.html"))
```

- [x] **Step 2: Run the tests and verify the missing-module failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_product_identity -v`

Expected: FAIL because `product_identity.py` does not exist.

- [x] **Step 3: Implement the immutable identity and resolver**

```python
@dataclass(frozen=True, slots=True)
class ProductIdentity:
    platform: Literal["taobao", "tmall"]
    product_id: str
    product_key: str
    source_url: str
    canonical_url: str


class ProductIdentityResolver:
    def __init__(self, redirect_resolver: Callable[[str, float], str] | None = None) -> None:
        self.redirect_resolver = redirect_resolver

    def resolve(self, value: str, timeout: float = 15.0) -> ProductIdentity | None:
        source_url = value.strip()
        parsed = urlparse(source_url)
        host = (parsed.hostname or "").lower()
        if host == "m.tb.cn":
            if self.redirect_resolver is None:
                raise ProductIdentityError("淘宝短链接暂时无法建立共享商品标识")
            source = self.redirect_resolver(source_url, timeout)
            parsed = urlparse(source)
            host = (parsed.hostname or "").lower()
        platform = _shared_platform(host)
        if platform is None:
            return None
        product_id = parse_qs(parsed.query).get("id", [""])[0].strip()
        if not product_id.isdigit():
            raise ProductIdentityError("淘宝或天猫链接缺少稳定商品 ID")
        canonical_url = _canonical_url(platform, product_id)
        return ProductIdentity(platform, product_id, f"{platform}-{product_id}", source_url, canonical_url)
```

- [x] **Step 4: Add short-link injection and malformed-link tests, then run the module**

Run: `.\.venv\Scripts\python.exe -m unittest test_product_identity -v`

Expected: PASS.

- [x] **Step 5: Commit the identity boundary**

```powershell
git add product_identity.py test_product_identity.py
git commit -m "feat: resolve shared product identities"
```

---

### Task 2: Persist Shared Catalog Cache and Client Identity

**Files:**
- Create: `shared_library_cache.py`
- Create: `test_shared_library_cache.py`

**Interfaces:**
- Consumes: Catalog dictionaries with `product_key`, `package_object`, and `package_sha256`.
- Produces: `SharedLibraryCache.client_id: str`.
- Produces: `load_catalog() -> list[dict[str, Any]]`, `replace_catalog(entries)`, `record_download(...)`, and `find_download(...)`.

- [x] **Step 1: Write failing persistence and atomicity tests**

```python
class SharedLibraryCacheTests(unittest.TestCase):
    def test_client_id_is_stable_across_instances(self) -> None:
        first = SharedLibraryCache(self.root).client_id
        second = SharedLibraryCache(self.root).client_id
        self.assertEqual(first, second)

    def test_download_record_requires_matching_sha_and_existing_directory(self) -> None:
        cache = SharedLibraryCache(self.root)
        local_dir = self.root / "reused" / "taobao-123"
        local_dir.mkdir(parents=True)
        cache.record_download("taobao-123", "objects/package.zip", "abc", local_dir)
        self.assertEqual(cache.find_download("taobao-123", "abc"), local_dir)
        self.assertIsNone(cache.find_download("taobao-123", "different"))
```

- [x] **Step 2: Run the tests and verify the missing-module failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_library_cache -v`

Expected: FAIL because `shared_library_cache.py` does not exist.

- [x] **Step 3: Implement JSON persistence through same-directory temporary files**

```python
class SharedLibraryCache:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._client_path = self.root / "client.json"
        self._catalog_path = self.root / "catalog.json"
        self._downloads_path = self.root / "downloads.json"
        self.client_id = self._load_or_create_client_id()

    @staticmethod
    def _write_json(path: Path, document: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
```

Store only object keys, hashes, local directories, ETags, and timestamps. Do not store credentials or signed URLs.

- [x] **Step 4: Run cache tests**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_library_cache -v`

Expected: PASS.

- [x] **Step 5: Commit cache persistence**

```powershell
git add shared_library_cache.py test_shared_library_cache.py
git commit -m "feat: persist shared library cache"
```

---

### Task 3: Implement OSS Catalog, Lock, Publish, and Download Operations

**Files:**
- Create: `shared_library_client.py`
- Create: `test_shared_library_client.py`

**Interfaces:**
- Consumes: `OssUploader.config` and `OssUploader.bucket` through constructor injection; preserve existing upload behavior without modifying the currently dirty uploader module.
- Produces: `SharedProbe(status, catalog, lock)` where status is `missing`, `available`, `locked`, or `corrupt`.
- Produces: `LockLease(product_key, task_id, client_id, etag, expires_at)`.
- Produces: `SharedLibraryClient.probe`, `acquire_lock`, `refresh_lock`, `release_lock`, `list_catalog`, `publish`, `download`, and `read_preview`.
- Raises: `SharedLibraryUnavailable`, `SharedLibraryLockBusy`, or `SharedMaterialCorrupt`; never include SDK request URLs or credentials in public exception text.

- [x] **Step 1: Build an in-memory fake bucket and write failing lock race tests**

```python
def test_two_clients_cannot_acquire_the_same_product(self) -> None:
    bucket = FakeSharedBucket()
    first = self.client(bucket, client_id="one")
    second = self.client(bucket, client_id="two")
    first.acquire_lock(self.identity, task_id="task-one")
    with self.assertRaises(SharedLibraryLockBusy):
        second.acquire_lock(self.identity, task_id="task-two")

def test_lock_busy_is_not_reported_as_oss_unavailable(self) -> None:
    bucket = FakeSharedBucket()
    self.client(bucket, "one").acquire_lock(self.identity, "task-one")
    probe = self.client(bucket, "two").probe(self.identity)
    self.assertEqual(probe.status, "locked")
```

The fake bucket must enforce `x-oss-forbid-overwrite: true`, return ETags, honor `If-Match`, expose `list_objects_v2`, and support ranged reads.

- [x] **Step 2: Write failing publication-order and unavailable-service tests**

```python
def test_catalog_is_the_last_published_object(self) -> None:
    lease = self.client.acquire_lock(self.identity, "task-one")
    self.client.publish(self.bundle, lease)
    self.assertTrue(self.bucket.write_keys[-1].endswith("catalog/taobao-123.json"))

def test_sdk_error_is_sanitized_as_unavailable(self) -> None:
    self.bucket.get_error = RuntimeError("https://id:secret@bucket.example?Signature=secret")
    with self.assertRaisesRegex(SharedLibraryUnavailable, "共享素材库暂时不可用"):
        self.client.probe(self.identity)
```

- [x] **Step 3: Run the focused tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_library_client -v`

Expected: FAIL because the shared client and bucket factory are absent.

- [x] **Step 4: Construct the shared client from the existing uploader boundary**

```python
uploader = OssUploader.from_settings_file(settings_path)
client = (
    SharedLibraryClient(uploader.config, uploader.bucket, client_id)
    if uploader is not None
    else None
)
```

Keep `oss_uploader.py`, `upload_file()`, and their current uncommitted video behavior unchanged so review-fix commits remain compatible.

- [x] **Step 5: Implement shared object keys, probes, and conditional locks**

```python
class SharedLibraryClient:
    LOCK_TTL = timedelta(hours=2)

    def acquire_lock(self, identity: ProductIdentity, task_id: str) -> LockLease:
        key = self._key("locks", f"{identity.product_key}.json")
        body = self._lock_document(identity, task_id)
        try:
            result = self.bucket.put_object(
                key,
                json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json", "x-oss-forbid-overwrite": "true"},
            )
        except Exception as error:
            if _is_object_exists(error):
                raise SharedLibraryLockBusy(self.read_lock(identity)) from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        return LockLease.from_document(body, result.etag)
```

Refresh and release must send `If-Match: lease.etag`. If the server rejects the condition, invalidate the lease and prevent publication.

- [x] **Step 6: Implement list, staging-first publication, preview reads, and ranged download**

Use `bucket.list_objects_v2(prefix=..., continuation_token=..., max_keys=50)` for pages. Upload every local file under `staging/{task_id}/`, verify object sizes, copy to formal keys, upload formal Manifest, verify the lease, and upload Catalog last with `x-oss-forbid-overwrite: true`.

Resume downloads by appending a ranged `bucket.get_object(key, byte_range=(part_size, total_size - 1))` response to `.part`; after size and SHA-256 match, atomically replace the final ZIP.

- [x] **Step 7: Run shared client and legacy OSS tests**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_library_client test_oss_uploader -v`

Expected: PASS, including a threaded two-client lock race with one winner.

- [x] **Step 8: Commit the OSS client boundary**

```powershell
git add shared_library_client.py test_shared_library_client.py
git commit -m "feat: add OSS shared library client"
```

---

### Task 4: Build and Validate Complete Shared Packages

**Files:**
- Create: `shared_package_builder.py`
- Create: `test_shared_package_builder.py`

**Interfaces:**
- Consumes: `ProductIdentity`, source Manifest path, generated records, titles, workbook path, generation mode, selected workflows, and configured counts.
- Produces: `SharedPackage(task_id, root, files, manifest, catalog)` or `None` for an ineligible/incomplete job.
- Produces: `materialize_reused_package(package_zip, destination) -> ReusedPackage` with source Manifest, generated records, titles, and extracted paths.

- [x] **Step 1: Write failing completeness tests**

```python
def test_complete_default_job_is_publishable(self) -> None:
    records = self.records(main=10, sku=3, detail=6, status="completed")
    package = self.builder.build(
        identity=self.identity,
        source_manifest=self.source_manifest,
        generated_records=records,
        titles={"long_title": "测试商品"},
        workbook_path=self.workbook,
        generation_mode="competitor_reference",
        workflows=("main", "sku", "detail"),
        max_main_images=10,
        max_sku_images=None,
        max_detail_images=None,
    )
    self.assertIsNotNone(package)

def test_custom_or_partial_job_is_not_publishable(self) -> None:
    self.assertIsNone(self.build(max_main_images=5))
    self.assertIsNone(self.build(records=self.records(main=10, sku=3, detail=5)))
    self.assertIsNone(self.build(generation_mode="own_product"))
```

- [x] **Step 2: Write failing archive, preview, and hash tests**

Assert category ZIP members, complete-package members, a valid JPEG preview under 800 KB, and matching SHA-256 values in Catalog and Manifest.

- [x] **Step 3: Run the tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_package_builder -v`

Expected: FAIL because the builder is absent.

- [x] **Step 4: Implement exact completeness validation**

```python
def _complete_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    if any(record.get("status") != "completed" for record in records):
        return None
    counts = Counter(str(record.get("category")) for record in records)
    if counts["main"] != 10 or not 3 <= counts["sku"] <= 8 or not 6 <= counts["detail"] <= 15:
        return None
    if any(not Path(str(record.get("output_path") or "")).is_file() for record in records):
        return None
    return dict(counts)
```

Require competitor-reference mode, all three workflows, default count settings, and an existing workbook before packaging.

- [x] **Step 5: Implement previews, ZIPs, Manifest, Catalog, and reuse materialization**

Use Pillow to create a fixed 2x2 JPEG preview from up to four main images. Use `zipfile.ZipFile(..., ZIP_DEFLATED, compresslevel=6)` and deterministic archive-relative paths:

```text
generated/main/*
generated/sku/*
generated/detail/*
source/manifest.json
source/titles.json
result.xlsx
reuse-manifest.json
```

The formal Manifest is stored beside the completed package and records the complete ZIP SHA-256. The ZIP therefore contains a reuse-only manifest instead of the formal Manifest, avoiding a self-referential hash cycle.

Reject archive entries containing absolute paths or `..` during extraction.

- [x] **Step 6: Run package tests**

Run: `.\.venv\Scripts\python.exe -m unittest test_shared_package_builder -v`

Expected: PASS.

- [x] **Step 7: Commit package creation**

```powershell
git add shared_package_builder.py test_shared_package_builder.py
git commit -m "feat: build complete shared asset packages"
```

---

### Task 5: Integrate Shared Lookup and Publication into Single-Link Jobs

**Files:**
- Modify: `web_app.py`
- Modify: `test_web_app.py`

**Interfaces:**
- Consumes: Tasks from Tasks 1-4.
- Extends: `AppState.status()` with `shared_library`.
- Adds: an internal eligibility predicate for competitor-reference/default/all-workflow jobs.
- Guarantees: lookup before `_begin_collection`, lock release on every terminal path, and local fallback on OSS infrastructure failure.

- [x] **Step 1: Write failing pre-collection behavior tests**

```python
def test_shared_hit_prevents_single_collection(self) -> None:
    state.agent = AgentSession(
        reference_url="https://item.taobao.com/item.htm?id=123",
        workflows=("main", "sku", "detail"),
        awaiting="",
        quantity_confirmed=True,
        generation_mode="competitor_reference",
    )
    with patch("web_app.load_shared_library_client", return_value=self.available_client):
        error = handler._begin_collection()
    self.assertEqual(error, "已有共享素材，可直接复用。")
    collector.assert_not_called()

def test_oss_unavailable_marks_local_fallback_and_starts_collection(self) -> None:
    self.client.probe.side_effect = SharedLibraryUnavailable("共享素材库暂时不可用")
    self.assertIsNone(handler._begin_collection())
    self.assertEqual(state.shared_library["status"], "local_fallback")
```

- [x] **Step 2: Write failing lock-contention and mode-exclusion tests**

Verify `locked` blocks collection, while own-product mode and non-Taobao/Tmall links never call the shared client.

- [x] **Step 3: Run focused web tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_web_app -v`

Expected: new tests FAIL because `AppState` has no shared state or hook.

- [x] **Step 4: Add shared state and the eligibility predicate**

```python
def shared_job_is_eligible(agent: AgentSession) -> bool:
    return (
        agent.generation_mode == "competitor_reference"
        and tuple(agent.workflows) == ("main", "sku", "detail")
        and agent.max_main_images == DEFAULT_MAIN_IMAGES
        and agent.max_sku_images is None
        and agent.max_detail_images is None
    )
```

Store only status, product key, message, Catalog summary, and lease metadata in application state. Never serialize bucket clients or credentials.

- [x] **Step 5: Probe and acquire immediately before collection**

Resolve the link, query Catalog/Manifest, block on `available` or `locked`, acquire on `missing`, and start a lock heartbeat. Convert only `SharedLibraryUnavailable` to `local_fallback`; do not catch `SharedLibraryLockBusy` as a fallback.

- [x] **Step 6: Export the single product workbook and publish after successful generation**

Refactor `_export_single_workbook` so `_generate` can call the same export helper without issuing another HTTP request. After records complete, build the shared package and call `client.publish()`. On any shared failure, keep the generated output and log a sanitized warning.

- [x] **Step 7: Guarantee lock shutdown**

In generation success, generation failure, collection failure, stop, reset, and application shutdown paths: stop heartbeat first, then release only the current lease. If refresh fails, clear publication eligibility and leave safe expiry handling to OSS.

- [x] **Step 8: Run web and workflow regression tests**

Run: `.\.venv\Scripts\python.exe -m unittest test_web_app test_agent_flow test_image_workflows -v`

Expected: PASS.

- [x] **Step 9: Commit single-link integration**

```powershell
git add web_app.py test_web_app.py
git commit -m "feat: share complete single-link jobs"
```

---

### Task 6: Integrate Shared Reuse and Publication into Direct-Link Batches

**Files:**
- Modify: `batch_workflow.py`
- Modify: `test_batch_workflow.py`
- Modify: `web_app.py`
- Modify: `test_web_app.py`

**Interfaces:**
- Extends: `BatchRunner.__init__(..., shared_library: SharedLibraryClient | None = None, shared_cache: SharedLibraryCache | None = None)`.
- Adds result metadata: `shared_status` equal to `reused`, `published`, `local_fallback`, `locked`, or absent.
- Leaves `image_search` and `direct_replace` call paths byte-for-byte behaviorally unchanged.

- [x] **Step 1: Write failing batch-hit reuse test**

```python
def test_direct_link_shared_hit_skips_collector_and_generator(self) -> None:
    runner = self.runner(batch_mode="direct_link", shared_library=self.available_client)
    results = runner.run(self.workbook, self.output)
    self.assertEqual(results[0]["status"], "completed")
    self.assertEqual(results[0]["shared_status"], "reused")
    runner.direct_collector.collect.assert_not_called()
    workflow_runner.run.assert_not_called()
    self.assertTrue(Path(results[0]["workbook"]).is_file())
```

- [x] **Step 2: Write failing lock, fallback, publish, and exclusion tests**

Cover these exact cases:

- valid lock marks only that row failed with “其他用户正在生成” and continues later rows;
- OSS unavailable runs the existing local row and records `local_fallback`;
- complete `direct_link` row publishes after Excel export;
- `direct_replace` and `image_search` never call `probe` or `acquire_lock`.

- [x] **Step 3: Run batch tests and verify failure**

Run: `.\.venv\Scripts\python.exe -m unittest test_batch_workflow test_web_app -v`

Expected: new tests FAIL because `BatchRunner` has no shared dependencies.

- [x] **Step 4: Add per-row shared preparation before historical local reuse**

For `direct_link` Taobao/Tmall rows only:

```python
probe = self.shared_library.probe(identity)
if probe.status == "available":
    package_zip = self.shared_library.download(
        probe.catalog["package_object"],
        probe.catalog["package_size"],
        probe.catalog["package_sha256"],
        item_root / "reused" / "complete-package.zip",
    )
    reused = materialize_reused_package(package_zip, item_root / "reused" / "materialized")
    exported = export_product_workbook(
        item_root / workbook_name,
        item,
        reused.source_manifest,
        reused.generated_records,
        reused.titles,
        self.project_root,
        include_metadata_only_skus=True,
    )
```

Do not launch the collector or workflow runner on this path.

- [x] **Step 5: Acquire and release per-row leases for misses**

Acquire only after a fresh missing probe. Keep the lease through local collection, generation, Excel export, package build, and publication. Release in a per-row `finally` block. Infrastructure failure changes the row to local fallback; contention does not.

- [x] **Step 6: Wire the shared client from `web_app.py`**

Pass the optional client/cache into `BatchRunner` only for `batch_mode == "direct_link"`. Keep existing `oss_uploader` injection for ordinary per-image uploads and all other modes.

- [x] **Step 7: Run focused and full batch regression tests**

Run: `.\.venv\Scripts\python.exe -m unittest test_batch_workflow test_web_app -v`

Expected: PASS.

- [x] **Step 8: Commit batch integration**

```powershell
git add batch_workflow.py test_batch_workflow.py web_app.py test_web_app.py
git commit -m "feat: reuse shared assets in direct-link batches"
```

---

### Task 7: Add Private Local APIs and the Shared Library Page

**Files:**
- Create: `frontend/src/SharedLibraryView.tsx`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/types.ts`
- Modify: `frontend/src/styles.css`
- Modify: `web_app.py`
- Modify: `test_web_app.py`

**Interfaces:**
- Adds: `GET /api/shared-library?platform=&query=&cursor=`.
- Adds: `GET /api/shared-library/preview?product_key=` as a private local proxy.
- Adds: `POST /api/shared-library/reuse` with `{product_key, package_kind}`.
- Adds: `POST /api/shared-library/open-folder` with `{product_key}`.
- Adds frontend types `SharedCatalogItem`, `SharedLibraryPage`, and `SharedLibraryStatus`.

- [x] **Step 1: Write failing local API tests**

```python
def test_shared_library_list_filters_taobao_and_returns_local_preview_url(self) -> None:
    response = self.get_json("/api/shared-library?platform=taobao&query=123")
    self.assertEqual(response["items"][0]["product_key"], "taobao-123")
    self.assertEqual(response["items"][0]["preview_url"], "/api/shared-library/preview?product_key=taobao-123")
    self.assertNotIn("signature", json.dumps(response).lower())

def test_open_folder_rejects_paths_outside_reused_root(self) -> None:
    response = self.post_json("/api/shared-library/open-folder", {"product_key": "../../Windows"})
    self.assertEqual(response.status, HTTPStatus.BAD_REQUEST)
```

- [x] **Step 2: Run the API tests and verify 404 failures**

Run: `.\.venv\Scripts\python.exe -m unittest test_web_app -v`

Expected: new endpoint tests FAIL.

- [x] **Step 3: Implement API routing and private preview streaming**

Parse GET paths with `urlparse(self.path)` and `parse_qs`. Preview responses must stream bytes from `SharedLibraryClient.read_preview()` with `Cache-Control: private, max-age=300`; never redirect the browser to a permanent OSS URL.

Reuse downloads into `OUTPUT_ROOT / "reused" / product_key`, record the result in `SharedLibraryCache`, and validate every opened folder against the recorded reused root.

- [x] **Step 4: Add failing TypeScript references for the third view**

Extend the view union to:

```typescript
type ActiveView = 'link' | 'batch' | 'library';
```

Add `SharedLibraryView` with search, platform segmented control, paging, preview cards, complete/category download buttons, progress labels, and open-folder action. Use Lucide `Library`, `Search`, `Download`, `FolderOpen`, and `LoaderCircle` icons.

- [x] **Step 5: Implement frontend types and the library view**

```typescript
export interface SharedCatalogItem {
  product_key: string;
  platform: 'taobao' | 'tmall';
  product_id: string;
  preview_url: string;
  main_count: number;
  sku_count: number;
  detail_count: number;
  package_size: number;
  created_at: string;
  local_directory: string | null;
}
```

Keep cards at radius 8px or less, use fixed preview aspect ratio, and show “共享素材库仅适用于参考对标商品创作” near the filters without turning it into a marketing section.

- [x] **Step 6: Run backend and frontend checks**

Run: `.\.venv\Scripts\python.exe -m unittest test_web_app -v`

Run: `& 'D:\nodejs\npm.cmd' run check --prefix frontend`

Run: `& 'D:\nodejs\npm.cmd' run build --prefix frontend`

Expected: all PASS.

- [x] **Step 7: Start the local server and visually verify desktop/mobile**

Start the app on an unused local port. Use Playwright screenshots at 1440x900 and 390x844. Verify the library view is nonblank, preview cards do not shift while loading, button labels fit, filters remain usable, and no controls overlap.

- [x] **Step 8: Commit the local API and page**

```powershell
git add frontend/src/SharedLibraryView.tsx frontend/src/App.tsx frontend/src/types.ts frontend/src/styles.css web_app.py test_web_app.py web/index.html web/assets
git commit -m "feat: add shared asset library page"
```

---

### Task 8: Document Private OSS Setup and Complete Release Verification

**Files:**
- Create: `docs/oss-shared-library-setup.md`
- Modify: `local_settings.example.json`
- Modify: `README.md`
- Modify: `操作说明书.md`
- Modify: `build_release.ps1`

**Interfaces:**
- Documents: dedicated RAM user, private Bucket, `shared-library/*` prefix, environment variables, concurrency behavior, fallback behavior, and two-client acceptance steps.
- Packages: all four new Python modules and the built frontend through existing PyInstaller import discovery and `web/` copy.

- [x] **Step 1: Write the private OSS configuration guide**

Include exact user environment variable commands with placeholders only:

```powershell
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID', 'YOUR_RAM_ACCESS_KEY_ID', 'User')
[Environment]::SetEnvironmentVariable('PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET', 'YOUR_RAM_ACCESS_KEY_SECRET', 'User')
```

Document least-privilege object permissions for the configured `{prefix}/shared-library/*`; do not include any real bucket secret or credential.

- [x] **Step 2: Update user-facing mode and fallback documentation**

State that only competitor-reference single-link and “参考对标商品创作” batch mode enter the shared Catalog. Explain that OSS infrastructure failure keeps local output, while another user's valid lock blocks duplicate generation.

- [x] **Step 3: Verify release discovery and copy the setup guide**

Add `docs/oss-shared-library-setup.md` to the release copy list. Confirm PyInstaller sees the four modules from imports in `web_app.py`; add hidden imports only if the build proves they are missing.

- [x] **Step 4: Run the complete automated verification suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -v
.\.venv\Scripts\python.exe -m py_compile product_identity.py shared_library_client.py shared_package_builder.py shared_library_cache.py web_app.py batch_workflow.py
& 'D:\nodejs\npm.cmd' run check --prefix frontend
& 'D:\nodejs\npm.cmd' run build --prefix frontend
git diff --check
```

Expected: all commands exit 0.

- [ ] **Step 5: Perform the two-client OSS acceptance matrix**

Use a private test prefix and non-production product IDs:

1. Two clients submit the same Taobao product simultaneously: one lock winner, one “其他用户正在生成”.
2. Two clients publish different products simultaneously: both complete.
3. Disable OSS access: local single and direct-link batch jobs complete with no Catalog.
4. Interrupt a package download: `.part` remains and the next attempt resumes.
5. Corrupt a downloaded test package: SHA-256 fails and extraction does not occur.
6. Complete one standard job: Catalog is last and a second client reuses without collector/model calls.

- [x] **Step 6: Run the release build**

Run: `powershell -ExecutionPolicy Bypass -File build_release.ps1 -Version v19-20260815`

Expected: the release directory and ZIP are produced; the packaged app opens all three views and can read the private test Catalog.

- [x] **Step 7: Commit docs and release changes**

```powershell
git add docs/oss-shared-library-setup.md local_settings.example.json README.md 操作说明书.md build_release.ps1
git commit -m "docs: add shared library deployment guide"
```

---

## Integration Checkpoint

Before Task 5 touches shared workflow files, collect the final `codex/review-fixes` commit list. Integrate those commits without staging unrelated working-tree files, rerun the 279-test baseline, and resolve `web_app.py`, `batch_workflow.py`, `frontend/src/App.tsx`, and `frontend/src/types.ts` against the reviewed behavior. Do not overwrite user changes or add `local_settings.json`.

After every task, update this plan's checkboxes and report the focused test evidence before moving to the next task.
