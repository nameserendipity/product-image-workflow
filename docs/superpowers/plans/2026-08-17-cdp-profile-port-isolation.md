# CDP Profile Port Isolation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent Store Insight collection from connecting to a responsive CDP browser that belongs to another browser profile, and automatically launch the requested profile on a free IPv4 port.

**Architecture:** Keep endpoint ownership inside `store_insight_collector.py`. Add small pure helpers for profile matching and endpoint selection, probe the CDP HTTP targets before Playwright connects, and use the selected URL consistently for browser launch polling and connection.

**Tech Stack:** Python 3.11+, standard library (`socket`, `urllib`, `subprocess`, `hashlib`), Playwright sync API, `unittest`.

## Global Constraints

- Work only in `D:\product-image-workflow-cdp-port-fix` on `codex/cdp-profile-port-fix`.
- Do not restart or modify the running `8765` service.
- Do not terminate browsers belonging to another profile.
- Do not change collection, download, parsing, generation, API, or frontend behavior.
- Do not add a new runtime dependency.
- Reuse an endpoint only when both the requested profile and expected Store Insight extension match.

---

### Task 1: Profile-aware CDP endpoint probe

**Files:**
- Modify: `store_insight_collector.py:1-285`
- Test: `test_collector.py`

**Interfaces:**
- Produces: `CdpEndpointStatus`, `chrome_extension_id(Path) -> str`, `probe_cdp_endpoint(str, Path, Path | None) -> CdpEndpointStatus`.
- Consumes: existing browser profile path and optional extension directory from `find_waxiang_store_insight_extension`.

- [ ] **Step 1: Write failing profile and extension probe tests**

Add imports and tests that assert:

```python
def test_probe_cdp_endpoint_rejects_browser_from_another_profile(self):
    with patch("store_insight_collector.cdp_owner_command_lines", return_value=[
        'waxiang.exe --remote-debugging-port=9223 --user-data-dir=D:\\other-profile'
    ]), patch("store_insight_collector.fetch_cdp_targets", return_value=[
        {"url": "chrome-extension://expected/background.html"}
    ]):
        status = probe_cdp_endpoint(
            "http://127.0.0.1:9223",
            Path("D:/requested-profile"),
            "expected",
        )
    self.assertFalse(status.reusable)
    self.assertIn("profile", status.reason.lower())

def test_probe_cdp_endpoint_accepts_matching_profile_and_extension(self):
    with patch("store_insight_collector.cdp_owner_command_lines", return_value=[
        'waxiang.exe --remote-debugging-port=9223 --user-data-dir=D:\\requested-profile'
    ]), patch("store_insight_collector.fetch_cdp_targets", return_value=[
        {"url": "chrome-extension://expected/background.html"}
    ]):
        status = probe_cdp_endpoint(
            "http://127.0.0.1:9223",
            Path("D:/requested-profile"),
            "expected",
        )
    self.assertTrue(status.reusable)
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
& "D:\product-image-workflow\.venv\Scripts\python.exe" -m unittest \
  test_collector.CollectorTests.test_probe_cdp_endpoint_rejects_browser_from_another_profile \
  test_collector.CollectorTests.test_probe_cdp_endpoint_accepts_matching_profile_and_extension -v
```

Expected: import or attribute failure because the probe API does not exist.

- [ ] **Step 3: Implement the minimal probe helpers**

Add:

```python
@dataclass(frozen=True)
class CdpEndpointStatus:
    reachable: bool
    reusable: bool
    reason: str

def fetch_cdp_targets(cdp_url: str, timeout: float = 2.0) -> list[dict[str, Any]]:
    ...

def cdp_owner_command_lines(cdp_url: str) -> list[str]:
    ...

def chrome_extension_id(extension_dir: Path | None) -> str:
    ...

def probe_cdp_endpoint(
    cdp_url: str,
    profile_dir: Path,
    expected_extension_id: str = "",
) -> CdpEndpointStatus:
    ...
```

Use `/json/list` for target inspection. On Windows, inspect only the listener for the endpoint's host and port and compare its root command line with the normalized profile path. Derive a Chrome extension ID from the manifest `key` when present, otherwise accept a 32-character `a-p` parent directory name.

- [ ] **Step 4: Run focused probe tests and verify GREEN**

Run the Step 2 command. Expected: both tests pass.

- [ ] **Step 5: Commit Task 1**

```powershell
git add store_insight_collector.py test_collector.py
git commit -m "fix: validate CDP browser profile before reuse"
```

### Task 2: Dynamic loopback endpoint selection

**Files:**
- Modify: `store_insight_collector.py`
- Test: `test_collector.py`

**Interfaces:**
- Consumes: `probe_cdp_endpoint(...) -> CdpEndpointStatus` from Task 1.
- Produces: `CdpEndpointSelection`, `select_cdp_endpoint(str, Path, str, bool) -> CdpEndpointSelection`.

- [ ] **Step 1: Write failing endpoint selection tests**

Add tests for these behaviors:

```python
def test_select_cdp_endpoint_reuses_matching_preferred_endpoint(self):
    with patch("store_insight_collector.probe_cdp_endpoint", return_value=CdpEndpointStatus(True, True, "ready")):
        selected = select_cdp_endpoint(
            "http://127.0.0.1:9223", Path("D:/profile"), "expected", True
        )
    self.assertEqual(selected.url, "http://127.0.0.1:9223")
    self.assertTrue(selected.reuse)

def test_select_cdp_endpoint_moves_wrong_profile_to_free_port(self):
    with patch("store_insight_collector.probe_cdp_endpoint", return_value=CdpEndpointStatus(True, False, "profile mismatch")), patch(
        "store_insight_collector.find_free_loopback_port", return_value=43123
    ):
        selected = select_cdp_endpoint(
            "http://127.0.0.1:9223", Path("D:/profile"), "expected", True
        )
    self.assertEqual(selected.url, "http://127.0.0.1:43123")
    self.assertFalse(selected.reuse)
```

Also test that an unreachable and free preferred port remains selected for launch.

- [ ] **Step 2: Run selection tests and verify RED**

Run the three new test methods with `python -m unittest -v`. Expected: missing selection API.

- [ ] **Step 3: Implement minimal selection helpers**

Add:

```python
@dataclass(frozen=True)
class CdpEndpointSelection:
    url: str
    reuse: bool
    reason: str

def is_loopback_port_free(port: int) -> bool:
    ...

def find_free_loopback_port() -> int:
    ...

def select_cdp_endpoint(
    preferred_url: str,
    profile_dir: Path,
    expected_extension_id: str,
    reuse_existing: bool,
) -> CdpEndpointSelection:
    ...
```

Keep URL generation fixed to `http://127.0.0.1:<port>`. Reuse is allowed only for a reusable probe result. A reachable mismatch always gets a new free port. An unreachable preferred port is retained only when an IPv4 bind confirms it is free.

- [ ] **Step 4: Run selection tests and verify GREEN**

Run the Step 2 command. Expected: all selection tests pass.

- [ ] **Step 5: Commit Task 2**

```powershell
git add store_insight_collector.py test_collector.py
git commit -m "fix: allocate a free CDP port on profile conflict"
```

### Task 3: Use the selected endpoint for browser launch and Playwright

**Files:**
- Modify: `store_insight_collector.py:234-285`
- Test: `test_collector.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: `select_cdp_endpoint(...) -> CdpEndpointSelection`.
- Produces: updated `connect_browser(playwright, args) -> Browser` behavior.

- [ ] **Step 1: Write a failing integration-style unit test**

Create an `argparse.Namespace` with auto-launch enabled and mock endpoint selection to return `http://127.0.0.1:43123`. Assert that:

```python
self.assertIn("--remote-debugging-port=43123", launch_args)
self.assertIn("--remote-debugging-address=127.0.0.1", launch_args)
playwright.chromium.connect_over_cdp.assert_called_with("http://127.0.0.1:43123")
```

The test must also assert that no unrelated profile-closing function is called when a matching endpoint is reused.

- [ ] **Step 2: Run the integration test and verify RED**

Run the new method with `python -m unittest -v`. Expected: current code still launches and connects using `args.cdp_url`.

- [ ] **Step 3: Integrate endpoint selection into `connect_browser`**

Resolve `executable`, `profile_dir`, `extension_dir`, and extension ID before attempting reuse. Log a concise message when the preferred endpoint belongs to another profile. Use `selection.url` for the launch port, readiness polling, and final Playwright connection. Add `--remote-debugging-address=127.0.0.1`. Keep `close_project_browser_for_profile(profile_dir)` scoped to the requested profile only.

- [ ] **Step 4: Document the behavior**

Add a troubleshooting note to `README.md`: occupied `9223` endpoints are validated and a free local port is selected automatically; unrelated Waxiang sessions remain open.

- [ ] **Step 5: Run focused and full regression tests**

Run:

```powershell
& "D:\product-image-workflow\.venv\Scripts\python.exe" -m unittest test_collector -q
& "D:\product-image-workflow\.venv\Scripts\python.exe" -m unittest discover -q
git diff --check
```

Expected: focused collector tests and all 442+ regression tests pass; `git diff --check` has no output.

- [ ] **Step 6: Perform a non-destructive CDP smoke test**

With a temporary profile and a free non-default port, launch the selected Waxiang executable, confirm `/json/version` and `/json/list` respond on `127.0.0.1`, then close only the temporary-profile browser process. Do not touch `8765` or existing user profiles.

- [ ] **Step 7: Commit Task 3**

```powershell
git add store_insight_collector.py test_collector.py README.md
git commit -m "fix: isolate collector browser CDP sessions"
```

