# Disable Post-Generation Review Design

## Context

The current own-product workflow sends every generated candidate to the vision model for semantic review. A failed review triggers one corrective image-generation attempt, and a second failed review marks the task failed. This improves automatic filtering but reduces throughput, adds model cost, and prevents generated images from reaching the workbook for human review.

The production policy is changing: humans will review every generated image and feed recurring issues back into the product-analysis and generation-prompt logic. Automated semantic review must not delay generation or reject a technically valid image.

## Scope

Remove post-generation model review from every image-generation workflow.

Keep these existing stages unchanged:

- source collection and reuse;
- product and reference visual analysis;
- generation-prompt composition, including product, gift, copy-evidence, physical-causality, and model-refresh instructions;
- the single image-model request;
- generated-image decoding and file validation;
- cancellation, OSS upload, workbook export, and failure reporting.

This change does not weaken generation prompts. It removes only the model-based inspection of the finished candidate and its corrective regeneration.

## Generation Flow

For every task:

1. Analyze the supplied product and reference images as required by the existing generation mode.
2. Compose the existing generation prompt.
3. Call the image model exactly once.
4. Decode and write the response directly to the final output path.
5. Mark the task completed when the output is a valid readable image.

The workflow must not call `VisionClient.review_generated`, create review candidate files, append reviewer correction instructions, or call the image model a second time because of semantic content.

## Success And Failure Semantics

A task succeeds when the image API returns a valid image that can be written to the final output path.

A task may still fail for operational reasons, including:

- visual-analysis API failure;
- image-generation API failure;
- malformed or unreadable generated image data;
- file-system write failure;
- cancellation.

Product identity drift, incorrect copy, an unchanged model, unsupported visual claims, incorrect product quantity, gifts, or physically implausible material effects do not automatically fail the task. Those issues are handled by human review after export.

## Record Contract

Generated records keep the existing analysis, prompt, source, identity, and output metadata. New completed records do not include `quality_review` or `generation_attempts`, because no review or semantic retry occurs.

Existing historical `analysis.json` files remain readable; no migration is required.

## Verification

Automated acceptance criteria:

- own-product tasks call the image model exactly once;
- no generation path calls `VisionClient.review_generated`;
- a generated image that would previously fail semantic review is saved as completed;
- no temporary review candidate files are created;
- existing visual analysis and generation-prompt rules remain active;
- invalid image data and operational API errors still fail normally;
- competitor-reference behavior remains functionally unchanged;
- the complete test suite passes.

Manual acceptance criteria:

- a full direct-replace workbook run exports every technically successful generated image without waiting for post-generation semantic review;
- the run contains no `成品语义审核` stage and no semantic-review failure messages.
