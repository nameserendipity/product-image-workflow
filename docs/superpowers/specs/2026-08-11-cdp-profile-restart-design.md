# CDP Profile Restart Design

## Problem

The web service on port 8765 is healthy, but batch collection launches a browser on fixed CDP port 9223 while the same `store-insight-profile` may already be open on a random CDP port. Chromium treats the profile as single-instance, forwards the launch to the existing browser, and the new process exits before port 9223 becomes ready.

## Design

Before `same_item_collector.ensure_cdp_browser()` launches a browser, it will close only top-level `waxiang.exe`, `msedge.exe`, or `chrome.exe` processes whose command line contains the exact collection profile passed through `--user-data-dir`. Child processes and unrelated browser profiles are excluded. If a matching process is closed, collection waits briefly before launching the browser on port 9223.

The 8765 web server and the direct collector's random CDP allocation remain unchanged. This keeps the change limited to the failing batch startup path.

## Error Handling

Process discovery or termination failures are treated as no matching process found; the existing 20-second CDP readiness check remains the authoritative failure signal.

## Verification

A regression test will confirm that `ensure_cdp_browser()` invokes profile cleanup before browser launch and that the launched process still receives port 9223 and the exact profile directory. Existing tests will verify no unrelated behavior regresses, followed by a live restart and HTTP/CDP checks.
