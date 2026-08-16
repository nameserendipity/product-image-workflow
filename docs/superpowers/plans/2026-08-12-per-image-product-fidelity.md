# Per-Image Product Fidelity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every generated image preserve the exact product shown in its matching collected image.

**Architecture:** Keep the existing workflow runner, but change task-level identity precedence and prompt contracts. The current source image becomes the first and authoritative model input; global and supporting images become secondary structural evidence only.

**Tech Stack:** Python, unittest, existing OpenAI-compatible vision and image endpoints.

## Global Constraints

- Do not change collection, export, OSS, or batch-resume behavior.
- Keep competitor brand, logo, shop-name, and watermark removal mandatory.
- Do not restart an active service.
- Do not commit without explicit user approval.

---

### Task 1: Lock identity to the current collected image

**Files:**
- Modify: `image_workflows.py`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: `ImageTask.source_path`, optional global anchor, optional supporting path.
- Produces: ordered image inputs with `source_path` first and a prompt that gives it highest identity priority.

- [ ] Add failing tests for per-image input order and identity wording.
- [ ] Run the focused tests and confirm they fail against global-anchor precedence.
- [ ] Change task-level image ordering and prompt contracts.
- [ ] Run the focused tests and confirm they pass.

### Task 2: Preserve SKU and detail evidence boundaries

**Files:**
- Modify: `image_workflows.py`
- Test: `test_agent_flow.py`

**Interfaces:**
- Consumes: current image category, current SKU metadata, dossier, optional supporting source.
- Produces: generation prompts that prohibit cross-image SKU, color, quantity, and structure transfer.

- [ ] Add failing tests for SKU isolation and detail evidence constraints.
- [ ] Implement the minimal prompt and analysis changes.
- [ ] Run focused and full regression tests.

### Task 3: Load and verify safely

**Files:**
- No production file changes.

**Interfaces:**
- Consumes: current `/api/status` state.
- Produces: loaded code only when collection and generation are idle.

- [ ] Confirm the service is idle.
- [ ] Restart only the local web service and preserve the collection browser.
- [ ] Confirm the service remains idle and the browser remains open.
