# Store Insight Missing Image Compensation Design

## Goal

Keep the existing Store Insight "download all (multiple files)" flow, then recover any requested image category that is absent from that archive by downloading that category once.

## Scope

- Applies only to Taobao and Tmall Store Insight collection.
- Covers `main`, `sku`, and `detail` images.
- A category is compensated only when it was requested and the all-files archive produced zero records for it.
- Video, product parameters, SKU metadata, Douyin, Kuaishou, image generation, and OSS behavior do not change.

## Data Flow

1. Download and materialize the all-files archive as today.
2. Determine which requested categories have no materialized records.
3. For each missing category in the stable `main`, `sku`, `detail` order, download its single-category archive once.
4. Materialize the archive with the same hash set and counters used by the all-files archive so duplicate images are skipped and filenames continue sequentially.
5. Collect product metadata and compute final collected/missing categories from the merged records.

## Failure Handling

A failed compensation does not discard images already collected. The final manifest keeps the category in `missing_asset_types` and records the failure text under `asset_compensation_errors`. A valid archive that yields no new usable image records (including an empty or duplicate-only archive) is also a failure. Platform risk-control errors continue to abort collection as they do for the primary download. Successful categories are absent from that error map.

## Acceptance

- If the all-files archive already contains every requested category, no single-category download runs.
- If one or several requested categories are absent, each absent category is attempted exactly once.
- Compensation output is merged without duplicate image hashes or filename collisions.
- Unrequested categories are never compensated.
- A failed compensation preserves the successful records and exposes an explicit error.
