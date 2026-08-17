# Product Logic Quality Gate Design

## Context

The `direct_replace` workflow uses the row product image as Image 1 and each collected competitor image as Image 2. Current analysis distinguishes only whether Image 2 contains a replaceable product and how many product-like units are visible. It does not distinguish the primary retail product from gifts, samples, bonus items, packaging, swatches, or decorative product-material effects.

The reviewed workbooks demonstrate three related failures:

- A gift or bonus region can be counted as ordinary replaceable product units, allowing the main product to be reduced to gift scale.
- A selling point can be supported by Image 1 but absent from the generated canvas, such as copy claiming an outer carton while no carton is rendered.
- A detached cream smear from Image 2 can be attached to the replacement tube even though Image 1 shows a closed cap and no exposed outlet.

The workflow currently performs structured analysis before generation but only validates the generated file as a readable, compressible image. There is no semantic review of the finished canvas.

## Scope

This change applies to `generation_mode="own_product"`, including Excel product-image batch replacement and the equivalent single-product workflow. It does not change the `competitor_reference` product-freeze workflow, collection, spreadsheet parsing, workbook export structure, SKU metadata, or OSS behavior.

## Analysis Contract

Own-product visual analysis must add the following auditable fields:

- `product_fingerprint.dispensing_state`: closure state, whether an outlet is visibly exposed, and any verified material-effect origin.
- `reference_visual_brief.primary_replaceable_product_unit_count`: count of full-size primary competitor products only.
- `reference_visual_brief.gift_or_bonus_elements`: visible gifts, samples, bonus products, and associated promotional regions. Their action is `remove` because no user-owned gift identity is supplied.
- `reference_visual_brief.physical_effects`: cream, liquid, powder, vapor, smear, or splash elements and whether their origin is visible.
- `copy_plan.selling_points[*].required_visual_evidence`: the exact element that must be visible on the final canvas for the copy to be truthful.

The existing `visible_product_unit_count` remains for compatibility and SKU quantity handling. New validation rejects missing or malformed own-product fields and lets the existing three-attempt vision-analysis retry request corrected JSON.

## Generation Rules

For a reference containing a product:

- Replace exactly `primary_replaceable_product_unit_count` primary product units.
- Keep the replacement product at the primary reference subject's scale and hierarchy.
- Remove gifts, samples, bonus products, their labels, and buy-gift promises. Reconstruct those areas from the surrounding background.
- Treat packaging from Image 1 as a separate component, not as another product unit or gift.
- Render every component named by `required_visual_evidence`. If a matching carton is mentioned, the carton must be clearly visible.
- Do not render a copy point whose evidence is not planned for the final canvas.
- When Image 1 has no exposed outlet, no cream, liquid, gel, powder, or other product material may touch or emerge from the body, seam, cap, or side. Reference material effects may remain only as detached props with visible separation from the product.
- When an outlet is visibly exposed, material may originate only at that verified outlet and follow physically plausible gravity and contact.

Product-free references continue to preserve ordinary copy and omit Image 1 from generation.

## Post-Generation Quality Gate

Every own-product candidate is reviewed using Image 1, Image 2, the candidate image, and the approved analysis. The reviewer checks:

- primary product dominance and exact primary-unit count;
- removal of gift and bonus elements;
- Image 1 identity and component fidelity;
- agreement between rendered copy and visible evidence;
- physically plausible material origin and absence of leakage;
- no product insertion for product-free references.

Only clear, high-confidence violations fail the review. A failed first candidate is regenerated once with the reviewer's concrete correction instructions. A second failure marks the task failed and prevents that image from being exported as a successful result. Review API or schema failures also fail closed.

Completed records store `quality_review` and `generation_attempts` for auditability. Failed records retain the final review and prompt alongside existing analysis diagnostics.

## Error Handling

- Candidate images use a task-local temporary filename and are promoted to the final ordinal path only after review passes.
- Failed candidate files are removed in a `finally` block.
- Cancellation between generation and review returns `cancelled` without publishing a candidate.
- Review failures use a concise error message containing violation codes and explanations.

## Verification

Automated acceptance criteria:

- prompts distinguish primary products from gifts and remove unsupported gift regions;
- main and detail prompts enforce the primary-unit count, not the total product-like count;
- copy points include required final-canvas evidence;
- closed products prohibit attached material effects;
- a failed review triggers exactly one corrective retry;
- a passing retry produces one completed record with review metadata;
- two failed reviews produce a failed record and no final output;
- `competitor_reference` behavior and existing product-free behavior remain unchanged;
- the complete Python test suite passes.
