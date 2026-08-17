# CDP Profile Port Isolation Design

## Problem

The collector currently defaults to `http://127.0.0.1:9223` and reuses any
responsive CDP endpoint. When another Waxiang or Chromium profile already owns
that endpoint, the collector can connect to the wrong browser. On Windows, a
second browser may bind the same port on IPv6 (`::1`) while the old browser
continues to own IPv4 (`127.0.0.1`), so the newly visible browser is not the
browser controlled by Playwright.

## Goals

- Keep each collector attached to the requested browser profile.
- Reuse an already-running browser only after validating that it belongs to the
  requested profile and exposes the expected Store Insight extension.
- Select an available local CDP port when the preferred port is occupied by a
  different profile.
- Pass the selected endpoint through the existing collector flow without
  changing collection, download, or parsing behavior.
- Preserve the current login profile so a new CDP port does not require a new
  login.
- Return a clear diagnostic when a browser cannot expose a validated endpoint.

## Non-goals

- Do not terminate browsers belonging to another profile.
- Do not change the web service port, API configuration, or generation logic.
- Do not change the requested browser selection UI.
- Do not silently fall back to another browser family.

## Recommended Design

The collector owns endpoint selection because it is the component that starts
and connects to the browser. The existing `--cdp-url` remains an optional
preferred endpoint for explicitly controlled sessions.

1. Parse the preferred CDP endpoint and probe its `/json/version` and
   `/json/list` endpoints.
2. Validate the endpoint against the requested profile:
   - the browser target must expose a page or background target for the expected
     Store Insight extension when the extension is available;
   - on Windows, the endpoint owner is checked against the requested
     `--user-data-dir` when process inspection is available;
   - an endpoint that responds but belongs to another profile is treated as
     occupied, not reusable.
3. If validation succeeds and `--reuse-existing-cdp` is set, connect to it.
4. Otherwise, choose a free loopback port. Prefer the configured port when it
   is free; otherwise bind a temporary socket to port 0, release it, and use
   the selected port for the new browser. Launch with an explicit
   `--remote-debugging-address=127.0.0.1` so IPv4/IPv6 fallback cannot hide a
   port collision.
5. Poll the selected endpoint until it passes the same profile/extension
   validation. Only then call `connect_over_cdp`.

The endpoint selection is local to one collector process. No global port file
or shared mutable state is required; the persistent browser profile remains the
source of login state. A collector retry repeats validation and can select a
new port without touching an unrelated browser.

## Current-Run Recovery

The currently stuck batch must not be repaired by restarting the web service.
Stop the batch collector, close only the two conflicting collection browser
profiles, and continue the batch. The existing output directory and parsed SKU
screenshot remain reusable. Once the permanent fix is deployed, the collector
will select a separate port automatically when an old profile is still open.

## Error Handling

- If a preferred endpoint is occupied by another profile, log the owner/profile
  mismatch and continue with a new port.
- If no endpoint can be validated within the startup timeout, fail the collector
  with the selected endpoint, expected profile directory, and last validation
  reason.
- Never report an unrelated responsive browser as ready.
- Keep existing stop/cancel behavior and do not kill unrelated browser trees.

## Tests and Acceptance Criteria

- Unit tests cover free preferred port selection.
- Unit tests cover occupied preferred port selection.
- Unit tests cover rejection of a responsive endpoint belonging to another
  profile.
- Unit tests cover extension target validation and a missing-extension
  diagnostic.
- Collector tests verify that the chosen CDP URL is used for both launch polling
  and Playwright connection.
- Existing Python regression tests remain passing.
- A smoke test starts a collector-owned browser on a non-default port and
  confirms `/json/version` and `/json/list` are reachable before cleanup.
- The running `8765` service is not restarted during implementation or tests.

