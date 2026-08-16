# Competitor Reference Copy Compliance Design

## Goal

Fix the shared `competitor_reference` workflow so main and detail images preserve ordinary selling copy, remove only clearly prohibited content, and never become bare product images merely because the vision model is uncertain.

This applies uniformly to direct-link generation for Taobao, Tmall, JD, and Douyin. SKU generation remains text-free.

## Current Problem

The vision analysis already produces a `copy_plan`, but the final `competitor_reference` prompt treats that plan as audit-only and explicitly forbids rendering it. At the same time, uncertain nutrition or benefit wording may be classified as removable compliance risk. When the original copy is removed, no approved replacement is allowed, leaving a product with little or no marketing copy.

The current free-form risk output also has no deterministic application-level review. Any risk returned by the vision model can flow directly into the generation prompt.

## Scope

- Change compliance classification for `competitor_reference` analysis.
- Add deterministic review of vision-produced compliance risks.
- Allow approved copy to be rendered for main and detail images.
- Require main images to contain a clear title and one to three supported selling points.
- Require detail images to preserve or replace the reference image's useful information hierarchy when copy zones exist.
- Keep product pixels, packaging, labels, logos, structure, quantity, color, and viewing angle locked.
- Keep SKU images free of off-product marketing copy.
- Cover all direct-link platforms through the shared workflow rather than adding a Douyin-specific branch.

## Non-Goals

- No OCR post-generation review.
- No legal certification engine or external regulatory database.
- No automatic proof checking against documents that the user did not supply.
- No change to collection, image quantity, concurrency, export, OSS, or browser behavior.
- No change to `own_product` composition or product-replacement behavior except shared helpers required for validation.

## Compliance Policy

The default decision is preserve. Content is removed only when it can be assigned to one of these explicit risk codes:

| Risk code | Removal boundary |
| --- | --- |
| `competitor_brand` | Off-product competitor brand or competitor-exclusive identity only |
| `store_or_watermark` | Store name, account mark, platform watermark, or unrelated promotional identity |
| `patent_or_certification` | Patent, certification, inspection-report, or equivalent authority claim |
| `origin_or_import` | Country source, imported source, or imported ingredient claim |
| `medical_treatment` | Diagnosis, prevention, treatment, cure, or explicit disease claim |
| `absolute_or_ranking` | Absolute guarantee, best/first ranking, permanent result, or equivalent absolute claim |
| `unsupported_sales_price_data` | Unsupported sales volume, price, discount, percentage, comparison, or performance data |

The following words or concepts are not removal reasons by themselves:

- Nutrition, vitality, enhancement, improvement, synergy, absorption, flavor, suitability, or usage wording.
- Product name, category, color, quantity, net content, specification, bundle relationship, ingredient, or nutrient value visible in the current source image.
- Ordinary slogans and selling points whose violation cannot be confirmed.

Examples that must be preserved when present in the current source image include `活力生活嚼出来`, `牛磺酸协同吸收`, `增强免疫力`, and `快速改善疲劳`. The system must not invent these claims when they are absent from all current-task inputs.

On-product and on-packaging content is always protected in `competitor_reference` mode, even when it resembles a risk term. Compliance edits apply only to off-product regions.

## Structured Vision Output

Each compliance item will retain the existing fields and add machine-reviewable fields:

```json
{
  "source_image": "Image 1",
  "original_text": "国家发明专利",
  "risk_code": "patent_or_certification",
  "location": "off_product editable: top-right badge",
  "decision": "remove",
  "reason": "Explicit patent claim in an off-product badge",
  "removal_instruction": "Remove the text and its dedicated badge, then reconstruct the background"
}
```

For ordinary or uncertain copy, the model may report `decision: preserve`, but such items will not be passed to the image generator as removal instructions.

The application review accepts removal only when all conditions hold:

1. `risk_code` is in the fixed removable set.
2. `location` is explicitly off-product.
3. `decision` is `remove`.
4. The item includes non-empty original text and a concrete reason.

If any condition is missing, malformed, unknown, or uncertain, the decision becomes preserve. On-product entries are always preserve.

## Copy Selection And Rendering

For main and detail images, the approved `copy_plan` becomes renderable instead of audit-only.

Copy sources are prioritized as follows:

1. Preserve legible ordinary copy already present in the current reference image.
2. Exclude only phrases approved for removal by the application review.
3. Fill removed or empty information zones with conservative copy supported by visible current-task product facts.
4. Never use another SKU, another product-family image, prior-task text, or general category assumptions as factual evidence.

Main-image output requirements:

- One clear Simplified Chinese headline.
- One to three supported selling points.
- Copy must remain outside the protected product and packaging regions.
- The reference hierarchy, title zone, badge relationships, and visual balance may be reused, but prohibited wording must not be reproduced.

Detail-image output requirements:

- Preserve ordinary copy when the reference has useful information zones.
- Replace removed copy with supported copy in the corresponding information hierarchy.
- Do not force dense copy onto product-only close-ups that have no information zone; those images may use a concise title and one supported point placed in available negative space.

SKU output requirements remain unchanged:

- No off-product title, selling point, badge, parameter text, arrow, or marketing copy.
- Preserve authentic text printed on the product or packaging.

## Mixed Text Regions

A badge or copy block containing both ordinary and prohibited wording must not be deleted wholesale by default. The analysis must identify the exact prohibited phrase. The generator removes that phrase and reconstructs or reflows the remaining approved copy naturally.

The whole container is removed only when it is dedicated entirely to prohibited content. Blank badges, blur blocks, smears, pseudo-text, and visible repair boundaries remain forbidden.

## Prompt Precedence

The final prompt order will be explicit:

1. Product and packaging freeze.
2. On-product protection boundary.
3. Application-approved removal list.
4. Approved copy plan for main/detail rendering.
5. Background and atmosphere improvement.
6. Task-specific visual direction.

The prompt must no longer contain both "copy is mandatory" and "do not render copy_plan" for main/detail tasks. The audit-only instruction remains only for SKU tasks.

## Failure Handling

- Retry malformed vision JSON using the existing retry mechanism.
- Reject analysis when a main task cannot produce a non-empty approved headline and at least one supported selling point.
- Do not generate or export a main image as successful when its approved copy plan is missing.
- Unknown risk codes and ambiguous decisions preserve the original copy.
- Persist the original vision result and the application-reviewed removal list in `analysis.json` for manual audit.
- Final image quality remains subject to manual review; no OCR recheck is added.

## Compatibility

- Existing analysis records remain readable; missing machine-review fields default to preserve rather than remove.
- `own_product` behavior remains unchanged.
- Direct-link supplement generation uses the same reviewed `competitor_reference` policy.
- Existing API and frontend request formats do not change.

## Verification

Unit tests will verify:

- Ordinary phrases such as `活力生活嚼出来`, `牛磺酸协同吸收`, `增强免疫力`, and `快速改善疲劳` are not removed merely because of their wording.
- Visible facts such as `每份90mg维C`, `八大B族营养`, flavor, quantity, and net content remain available to the copy plan.
- Patent, certification, import origin, medical treatment, absolute ranking, and unsupported sales/price data are removable only when off-product.
- Unknown risk codes, missing reasons, malformed decisions, and on-product locations are preserved.
- Main and detail prompts render the approved copy plan.
- SKU prompts remain text-free.
- Direct-link Taobao, Tmall, JD, and Douyin tasks use the same policy.
- `own_product` tests remain unchanged and pass.

Regression verification will run the complete automated test suite. A real direct-link generation smoke test will be run only after confirming no active user task will be interrupted. The generated main images will be manually checked for product fidelity, retained ordinary copy, safe replacement copy, legibility, and absence of bare layouts.

## Acceptance Criteria

- No platform-specific Douyin prompt fork is introduced.
- Main images never become bare solely because ordinary copy was classified as uncertain.
- Only application-approved explicit risk categories are removed.
- Uncertain and ordinary selling copy is preserved.
- Removed copy is replaced with visible-evidence-supported copy when the reference contains an information zone.
- Product identity and on-product content remain unchanged.
- SKU output remains free of off-product copy.
- All automated tests pass before completion is reported.
