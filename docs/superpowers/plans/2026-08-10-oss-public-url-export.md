# OSS Public URL Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** Upload only generated product images to Alibaba Cloud OSS and export their public URLs into every generated product workbook.

**Architecture:** Add a focused oss_uploader.py module. It reads non-secret Bucket settings from local_settings.json and AccessKey credentials from Windows user environment variables. Collection remains local-only; single-item and batch generation upload completed output images before task state and Excel are saved.

**Tech Stack:** Python 3, oss2, JSON manifests, existing Node spreadsheet exporter, unittest.

## Global Constraints

- Do not place an AccessKey ID or AccessKey Secret in Git-tracked files, source code, logs, manifests, or Excel.
- Use Bucket transform-image, Endpoint https://oss-cn-shenzhen.aliyuncs.com, and prefix product-workflow/ as configurable defaults.
- Keep collected images local-only. Keep existing local paths and folder-opening workflow unchanged.
- If OSS is unavailable or one file upload fails, retain local workflow output and log a warning.
- Excel image fields use public URLs when present and local file links only when no public URL exists.

---

### Task 1: Add OSS configuration parsing and uploader tests

**Files:**
- Create: oss_uploader.py
- Create: test_oss_uploader.py
- Modify: requirements.txt
- Modify: local_settings.example.json

**Interfaces:**
- Consumes: local_settings.json oss object and environment variables PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID and PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET.
- Produces: OssUploader.from_settings_file(settings_path: Path) -> OssUploader | None and OssUploader.upload_file(path: Path, namespace: str) -> str.

- [x] Step 1: Write a test that returns None with a missing credential and a test using a fake Bucket that returns the expected public URL.
- [x] Step 2: Run python -m unittest test_oss_uploader -v. Expected: fail because oss_uploader does not exist.
- [x] Step 3: Implement OssConfig and OssUploader with oss2.Auth and Bucket.put_object_from_file. Object keys use prefix, namespace, content SHA-256 prefix, and original filename. URL-encode the object key.
- [x] Step 4: Add oss2>=2.19,<3 to requirements.txt. Add an oss object to the example settings with endpoint, bucket, and prefix only.
- [x] Step 5: Run python -m unittest test_oss_uploader -v. Expected: pass.
- [ ] Step 6: Commit task files with message feat: add OSS image uploader.

### Task 2: Upload generated images before results and workbook export

**Files:**
- Modify: web_app.py generation completion.
- Modify: batch_workflow.py batch runner and export path.
- Modify: test_batch_workflow.py.

**Interfaces:**
- Consumes: completed workflow records containing output_path, plus OssUploader.upload_file.
- Produces: output_public_url for successful generated records. Source image columns continue to use local file links.

- [x] Step 1: Add failing tests for upload_generation_records and an Excel output that prefers OSS URLs.
- [x] Step 2: Run python -m unittest test_batch_workflow -v. Expected: fail because generated-record upload is absent.
- [x] Step 3: Implement upload_generation_records(records, uploader). Only completed existing output files gain output_public_url.
- [x] Step 4: Apply it in RequestHandler._generate before STATE results are saved.
- [x] Step 5: Add optional uploader support to BatchRunner. Upload generated images before export_product_workbook. Do not upload source manifests or reference images.
- [x] Step 6: Run python -m unittest test_batch_workflow test_web_app -v. Expected: pass.
- [ ] Step 7: Commit task files with message feat: export generated image OSS URLs.

### Task 3: Verify real public upload and document local setup

**Files:**
- Create: docs/oss-local-setup.md.

**Interfaces:**
- Consumes: credentials set only on the Windows user account.
- Produces: an accessible public OSS URL and an exported workbook containing it.

- [x] Step 1: Document environment variables PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID and PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET using placeholders only. State that the service must be relaunched after setting them.
- [ ] Step 2: Upload one test image through OssUploader, request the returned URL with Invoke-WebRequest, and verify HTTP 200 without printing credentials. This remains pending until a newly rotated key is configured locally.
- [x] Step 3: Run python -m unittest discover -v. Expected: pass.
- [ ] Step 4: Commit task files with message docs: add OSS local configuration guide.

## Self-Review

- Task 1 configures secure credentials and URL construction.
- Task 2 covers generated images and Excel URLs.
- Task 3 validates public Bucket access and records user setup.
- The existing output_public_url field remains the Excel export contract; no public URL is added to collected image entries.

## Execution Handoff

Plan complete and saved to docs/superpowers/plans/2026-08-10-oss-public-url-export.md. Two execution options:

1. Subagent-Driven (recommended) - dispatch a fresh subagent per task and review between tasks.
2. Inline Execution - execute tasks in this session with checkpoints.
