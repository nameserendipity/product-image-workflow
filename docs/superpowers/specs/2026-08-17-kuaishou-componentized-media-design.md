# Kuaishou Componentized Media Extraction Design

## Problem

The Kuaishou H5 `componentized` response keeps the requested product ID and the
primary media in separate sibling components. The existing recursive extractor
requires each media subtree to carry the product ID, so it discards valid main
and detail images and the collector exits before writing a manifest.

On systems using a local proxy with Fake-IP DNS, the trusted Kuaishou CDN can
resolve into `198.18.0.0/15`. Browsers route that address through the proxy, but
the downloader previously rejected it before making the HTTPS request.

## Chosen Approach

Recognize the componentized response shape without weakening the generic
extractor:

- Confirm the requested product ID in a primary identity component:
  `idToolbar`, `idBottomBar`, or `idFullList`.
- After that confirmation, extract media only from `idMainPic` and components
  whose names start with `idDecorate___detailImage`.
- Keep the existing identity-scoped traversal for legacy response shapes.
- Continue excluding SKU, recommendation, advert, similar-product, comment,
  and unrelated DOM images.
- Accept the IANA benchmarking range `198.18.0.0/15` only after the hostname
  has passed the existing Kuaishou CDN allowlist. Continue rejecting loopback,
  private, link-local, and other non-global addresses.

This is narrower than traversing the complete componentized payload and avoids
turning recommendation or review images into product assets.

## Data Flow

1. Receive one JSON response and the product ID parsed from the requested URL.
2. Detect `payload.data.data` as the component map.
3. Verify that an approved identity component contains the same product ID.
4. Traverse only approved primary-media components with identity already
   established.
5. Apply the existing trusted-CDN, image-category, video, deduplication, and
   download validation rules.
6. Permit the proxy Fake-IP range while retaining HTTPS hostname validation,
   redirect validation, response-type validation, format validation, and the
   100 MB size limit.

## Failure Behavior

- Missing or mismatched identity evidence returns no componentized media.
- A response with no valid main-image media token continues to fail collection.
- Kuaishou still does not fabricate SKU images; the spreadsheet screenshot is
  handled by the existing batch input flow.

## Verification

- A fixture matching the live componentized layout yields only its main,
  detail, and trusted video assets.
- A mismatched product ID yields no assets.
- A recommendation component containing another product remains excluded.
- A trusted CDN hostname resolving to `198.18.0.0/15` passes, while the same
  hostname resolving to `127.0.0.1` remains blocked.
- Existing collector and integration tests remain green.
- The supplied live URL completes collection and writes a manifest whose files
  exist locally.
