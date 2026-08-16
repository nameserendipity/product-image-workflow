# Per-Image Product Fidelity Design

## Goal

Every generated main, SKU, and detail image must preserve the exact product shown in its corresponding collected image while allowing commercial visual improvement.

## Identity Rule

- The current collected image is the only identity truth for that generation task.
- Preserve its product model, silhouette, proportions, component count, component positions, color, SKU, quantity, package structure, viewing angle, and visible details.
- The first main image may establish product-family context, but must never override the current image.
- Other collected images may only confirm already visible geometry. They cannot transfer another SKU, color, quantity, package, or structure into the current task.

## Allowed Changes

- Remove competitor brands, logos, shop names, and platform watermarks, then reconstruct the affected surface naturally.
- Improve background, ordinary marketing copy, lighting, sharpness, material rendering, and layout.
- Improve commercial quality without redesigning the product.

## Detail Views

- Use collected multi-view evidence for alternate angles.
- Never invent unseen back panels, openings, controls, interfaces, accessories, or components.
- When structural evidence is insufficient, prefer a verified close-up, material, texture, scale, or usage view.

## Verification

- Tests must verify that the current source image is first in every image-generation request.
- Prompts must state that the current image controls SKU, quantity, color, structure, and angle.
- Tests must verify that global anchors and supporting images cannot override the current image.
- Brand removal must remain mandatory.
