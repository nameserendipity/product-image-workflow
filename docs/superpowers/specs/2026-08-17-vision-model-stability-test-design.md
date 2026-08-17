# Vision Model Stability Test Design

## Goal

Verify whether `gpt-5.6-sol` can complete the Kuaishou full workflow without a visual prompt analysis failure. If it cannot, restore `gpt-5.5`, reduce the process-wide visual request concurrency to two, and repeat the same formal workflow.

## Scope

- Change the shared `ApiSettings.vision_model` used by product dossier, main-image, SKU-image, and detail-image visual analysis.
- Keep `gpt-image-2`, image-generation concurrency, collection behavior, task counts, retry count, and OSS behavior unchanged.
- Run through the web application's formal batch upload and batch start APIs using `C:\Users\Administrator\Desktop\快手链接.xlsx` in `direct_link` and `full` mode.
- Use the next available port because the existing service occupies port 8011.

## Test Sequence

1. Add a regression test requiring the default vision model and outbound visual payload to use `gpt-5.6-sol`.
2. Change the shared vision model and model-specific web error messages to `gpt-5.6-sol`.
3. Run focused and full automated verification.
4. Start the updated service on the next available port and submit the Kuaishou workbook through the formal web API.
5. Wait for the batch to finish, then classify every record by `status` and `failure_stage`.
6. Treat any record that ends in `视觉提示词分析` failure after its built-in five attempts as a failed `gpt-5.6-sol` stability trial.
7. If the trial fails, add a regression test requiring a process-wide visual concurrency of two, restore `gpt-5.5`, implement the lower global limit, and repeat the formal batch run.

## Concurrency Semantics

The visual limit is process-wide. Product dossier analysis and the main, SKU, and detail workflows share the same request gate. A fallback limit of two therefore means at most two visual API requests in total, not two requests per category.

## Evidence And Acceptance

Each trial must retain its batch output. The report must include:

- requested vision model recorded in `generated/analysis.json`;
- completed and failed counts for main, SKU, and detail tasks;
- failures grouped by `failure_stage` and exact error text;
- exported workbook and non-empty product parameters;
- successful generated files and their OSS accessibility;
- service port and batch output directory.

The `gpt-5.6-sol` version is retained only when the formal batch finishes with zero visual prompt analysis failures. Otherwise the accepted configuration is `gpt-5.5` with a shared visual concurrency of two, subject to the second trial's measured result.
