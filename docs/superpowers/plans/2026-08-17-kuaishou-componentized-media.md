# Kuaishou Componentized Media Extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect primary Kuaishou media from the current componentized H5 response without admitting media from unrelated product components.

**Architecture:** Extend the existing payload extractor with a narrowly scoped componentized-response path. It verifies product identity in known primary identity components, traverses only known primary media components, and leaves the legacy identity-scoped traversal unchanged.

**Tech Stack:** Python 3, `unittest`, Playwright live verification.

## Global Constraints

- Do not weaken trusted CDN, DNS, redirect, content-type, file-format, or size validation.
- Do not fabricate Kuaishou SKU images.
- Do not modify batch generation, vision, image generation, OSS, frontend, or service lifecycle behavior.
- Do not include recommendation, advert, similar-product, comment, or SKU media.

---

### Task 1: Extract Verified Componentized Product Media

**Files:**
- Modify: `kuaishou_collector.py`
- Test: `test_kuaishou_collector.py`

**Interfaces:**
- Consumes: `extract_product_payload(payload: object, base_url: str = "", product_id: str = "") -> dict[str, object]`
- Produces: the same return shape with componentized main, detail, and video URLs included only after identity verification.

- [ ] **Step 1: Write the failing componentized-response tests**

Add a real nested dictionary fixture matching `payload.data.data`. Put the
requested `itemId` under `idToolbar`, valid product media under `idMainPic` and
`idDecorate___detailImage0`, and unrelated media under `idRecommend`. Assert
literal URL lists for main, detail, and video. Add a second assertion using a
different requested product ID and expect all three lists to be empty.

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```powershell
.\.venv\Scripts\python.exe -m unittest test_kuaishou_collector.KuaishouCollectorTests.test_extract_componentized_payload_requires_matching_primary_identity -v
```

Expected: FAIL because the current extractor returns no primary media from
sibling components.

- [ ] **Step 3: Implement the minimal extraction path**

Add private helpers that:

```python
def _contains_product_identity(value: object, product_id: str) -> bool: ...

def _componentized_media_sources(payload: object, product_id: str) -> list[object] | None: ...
```

The second helper must return `None` for non-componentized payloads, an empty
list for componentized payloads without matching primary identity, and only
`idMainPic` plus `idDecorate___detailImage*` values after a match. Feed returned
sources into the existing visitor with `identity_matched=True`; otherwise keep
the legacy visitor call unchanged.

Add a DNS boundary test that patches `socket.getaddrinfo` with `198.18.0.99`
and expects an allowlisted Kuaishou CDN URL to pass. In the same test, patch it
with `127.0.0.1` and expect rejection. Update the resolver check to accept only
global addresses or `198.18.0.0/15`; the hostname allowlist still runs first.

- [ ] **Step 4: Run focused and related tests**

```powershell
.\.venv\Scripts\python.exe -m unittest test_kuaishou_collector test_kuaishou_integration -v
```

Expected: all Kuaishou tests pass.

- [ ] **Step 5: Run full verification and live collection**

```powershell
.\.venv\Scripts\python.exe -m unittest discover -p "test_*.py"
.\.venv\Scripts\python.exe -m py_compile kuaishou_collector.py store_insight_collector.py test_kuaishou_collector.py
git diff --check
```

Then rerun the supplied Kuaishou URL through `store_insight_collector.py` with
the configured Waxiang executable and dedicated profile. Verify that the
manifest exists, every referenced file exists, main and detail counts are
nonzero, SKU count is zero, and video status reflects the actual download.

- [ ] **Step 6: Commit the implementation**

```powershell
git add kuaishou_collector.py test_kuaishou_collector.py docs/superpowers/plans/2026-08-17-kuaishou-componentized-media.md
git commit -m "fix: collect verified kuaishou component media"
```
