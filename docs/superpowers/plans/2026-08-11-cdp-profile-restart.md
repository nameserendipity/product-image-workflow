# CDP Profile Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent batch collection from failing when the collection profile is already open on a different CDP port.

**Architecture:** Add a Windows-scoped helper that closes only a top-level supported browser process using the exact collection profile. Call it immediately before the fixed-port browser launch, preserving all existing port and readiness behavior.

**Tech Stack:** Python 3.12, `unittest`, PowerShell process discovery on Windows, Playwright CDP.

## Global Constraints

- Keep the web service on port 8765.
- Do not terminate browsers using unrelated profiles.
- Do not change the direct collector's random CDP allocation.

---

### Task 1: Restart the collection profile browser

**Files:**
- Modify: `same_item_collector.py:666`
- Test: `test_same_item_collector.py`

**Interfaces:**
- Consumes: `CollectorConfig.browser_profile_dir: Path`
- Produces: `close_project_browser_for_profile(profile_dir: Path) -> int`

- [ ] **Step 1: Write the failing test**

Add a test that patches `close_project_browser_for_profile`, browser startup, CDP readiness, and sleep. Assert cleanup receives the resolved profile before `subprocess.Popen`, then assert the launch command contains `--remote-debugging-port=9223` and the exact `--user-data-dir`.

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\Scripts\python.exe -m unittest test_same_item_collector.EnsureCdpBrowserTests.test_closes_matching_profile_before_launch -v`

Expected: FAIL because `ensure_cdp_browser()` does not call profile cleanup.

- [ ] **Step 3: Write minimal implementation**

Add the Windows process cleanup helper using the established PowerShell logic from `store_insight_collector.py`, call it after resolving/creating the profile, and wait one second only when at least one process was closed.

- [ ] **Step 4: Run targeted and full tests**

Run: `.venv\Scripts\python.exe -m unittest test_same_item_collector.EnsureCdpBrowserTests.test_closes_matching_profile_before_launch -v`

Run: `.venv\Scripts\python.exe -m unittest discover -v`

Expected: PASS with zero failures.

- [ ] **Step 5: Restart and verify service**

Restart `web_app.py --no-browser`, confirm `127.0.0.1:8765` is listening and returns HTTP 200, then exercise browser startup and confirm `/json/version` responds on port 9223.
