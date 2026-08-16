# Ecommerce Workflow Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Distill the ecommerce product-image skill into the existing three workflows so analysis and generation both preserve the user's product identity while using each competitor image as a visual reference.

**Architecture:** Keep the current vision relay, image relay, task scheduling, and human review flow. Each task sends the user's product image as `Image 1: main-identity` and one collected image as `Image 2: reference-style`; the vision response becomes a structured product fingerprint and visual brief, and `gpt-image-2` receives both source images plus a fixed Fidelity A prompt.

**Tech Stack:** Python standard library, OpenAI-compatible chat completions and image edits endpoints, `unittest`.

## Global Constraints

- The user's original product image is the only product identity source.
- The collected image may guide scene, composition, camera, light, props, palette, and atmosphere only.
- Preserve silhouette, proportions, components, orientation, color, material, texture, packaging, labels, and real accessories at Fidelity A.
- Never invent another SKU, capacity, colorway, component, accessory, feature, claim, certification, origin, or specification.
- Remove competitor brands, logos, store names, watermarks, proprietary copy, patents, certifications, origin claims, medical claims, absolute claims, and false sales, rank, price, or performance data.
- Keep the current `gpt-5.5` and `gpt-image-2` relays, selected workflow categories, reference image count, and dynamic concurrency.
- Results remain subject to human review; do not add OCR or automatic image acceptance.

---

### Task 1: Lock the distilled contracts with tests

**Files:**
- Modify: `test_agent_flow.py`

**Interfaces:**
- Consumes: `VisionClient.analyze(product_image, reference_image, category)` and `ImageClient.generate(product_image, reference_image, prompt)`.
- Produces: regression coverage for image roles, multiple multipart files, Fidelity A prompt rules, and analysis record metadata.

- [x] Add a mocked vision request test that verifies two image inputs and explicit `main-identity` / `reference-style` roles.
- [x] Add a multipart generation test that verifies both files are sent as `image[]` in product-first order.
- [x] Add a runner task test that verifies `product_fingerprint`, `generation_prompt`, and `fidelity: A` are saved.
- [x] Run `python -m unittest test_agent_flow.py` and confirm the new tests fail before implementation.

### Task 2: Implement product-fidelity analysis and generation

**Files:**
- Modify: `image_workflows.py`

**Interfaces:**
- Consumes: one user product image and one collected reference image per task.
- Produces: structured analysis with `product_fingerprint`, `reference_visual_brief`, `compliance_risks`, and `generation_prompt`; one generated image and auditable metadata record.

- [x] Change the vision request to send product and reference images together with fixed roles and a strict JSON contract.
- [x] Validate required analysis fields before generation so a weak or malformed brief cannot silently continue.
- [x] Build the final prompt from fixed Fidelity A and compliance rules plus the model's product fingerprint and visual brief.
- [x] Send both images to `/v1/images/edits` as ordered `image[]` parts.
- [x] Save fidelity, product fingerprint, reference brief, compliance risks, and final prompt in each completed record.

### Task 3: Verify the integrated workflows

**Files:**
- Verify: `image_workflows.py`
- Verify: `web_app.py`
- Verify: `store_insight_collector.py`

**Interfaces:**
- Consumes: the existing web-triggered workflow arguments and collector manifest.
- Produces: unchanged UI behavior with higher-fidelity analysis and generation internals.

- [x] Run `python -m py_compile image_workflows.py web_app.py`.
- [x] Run `python -m unittest test_agent_flow.py test_collector.py` and require all tests to pass.
- [x] Restart the local service and confirm `http://127.0.0.1:8765` responds.
- [x] Run one real main-image task when a session vision API key is available and inspect `analysis.json` plus the generated image manually.
