"""Download assets from a Taobao/Tmall item page through Store Insight.

The script intentionally uses the extension's visible quick-download actions.
It does not call a Store Insight API or bypass login/captcha challenges.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Browser, Page, Playwright, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from douyin_collector import download_douyin_package, materialize_douyin_package
from platform_urls import is_douyin_product_host, is_taobao_host, is_tmall_host
from parameter_collector import collect_product_parameters, empty_parameter_metadata
from same_item_collector import (
    click_first_visible_text,
    collect_product_video_url,
    download_store_insight_sku_export_on_page,
    parse_store_insight_sku_filename,
    parse_store_insight_sku_table,
)


ASSET_LABELS = {
    "main": ("1:1图", "1:1主图", "主图图片", "页面图", "页面展示主图"),
    "sku": ("仅SKU图", "SKU图", "仅 SKU 图", "sku图", "sku图片"),
    "detail": ("仅详情图", "长图", "详情图", "详情图片", "详情长图", "详情页长图"),
}
ASSET_TYPES = ("main", "sku", "detail")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".avif"}
DOUYIN_IMAGE_HOST_SUFFIXES = ("ecombdimg.com", "byteimg.com", "pstatp.com", "snssdk.com")
DOUYIN_EMBEDDED_IMAGE_MAX_BYTES = 20 * 1024 * 1024
ACTION_DELAY_MS = 2500
DOWNLOAD_COMPLETE_PATTERN = re.compile(r"(?:\d+\s*个)?文件下载完成")
HARD_RISK_MARKERS = (
    "访问被拒绝",
    "访问被拒",
    "访问受限",
    "请求太频繁",
    "操作过于频繁",
)
SKU_COLOR_VALUE_PATTERN = re.compile(
    r"(?:浅|深|亮|暗|纯|高级|清新|奶油|象牙|珍珠|香槟|玫瑰|雾霾|天空|宝石|藏|墨)?"
    r"(?:白色|黑色|灰色|红色|橙色|黄色|绿色|青色|蓝色|紫色|粉色|棕色|米色|杏色|金色|银色|卡其色|咖啡色|驼色|白|黑|灰|红|橙|黄|绿|青|蓝|紫|粉|棕|米|杏|金|银)"
)


class RiskControlDetected(RuntimeError):
    """Raised when Taobao explicitly rejects access and collection must stop."""


def configure_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Download Store Insight main/SKU/detail images.")
    parser.add_argument("url", help="Taobao or Tmall item URL")
    parser.add_argument("--output", default="outputs/store-insight", help="Output directory")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9223", help="Chrome CDP endpoint")
    parser.add_argument("--browser-executable", default="", help="Chrome/Edge executable for --auto-launch")
    parser.add_argument("--profile-dir", default=".store-insight-profile", help="Browser profile used by --auto-launch")
    parser.add_argument("--auto-launch", action="store_true", help="Launch a browser when CDP is unavailable")
    parser.add_argument("--reuse-existing-cdp", action="store_true", help="Reuse an already-open CDP browser")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("--login-wait-seconds", type=int, default=300, help="Seconds to wait for manual Taobao login")
    parser.add_argument("--action-delay-seconds", type=float, default=2.5, help="Fixed wait after each Store Insight click")
    parser.add_argument("--package-delay-seconds", type=float, default=8.0, help="Fixed wait before a fallback package download")
    parser.add_argument("--max-main-images", type=int, default=0, help="Keep only the first N main images; 0 keeps all")
    parser.add_argument(
        "--types",
        nargs="+",
        choices=ASSET_TYPES,
        default=list(ASSET_TYPES),
        help="Only collect the selected asset types; default collects main, sku, and detail",
    )
    return parser.parse_args()


def validate_item_url(value: str) -> tuple[str, str]:
    parsed = urlparse(value.strip())
    host = parsed.hostname.lower() if parsed.hostname else ""
    supported = (
        is_taobao_host(host)
        or is_tmall_host(host)
        or host == "jd.com"
        or host.endswith(".jd.com")
        or is_douyin_product_host(host)
    )
    if not parsed.scheme.startswith("http") or not supported:
        raise ValueError("Only Taobao, Tmall, JD, and Douyin item URLs are supported by this collector")
    product_id = parse_qs(parsed.query).get("id", [""])[0]
    if not product_id and (host == "jd.com" or host.endswith(".jd.com")):
        matched = re.search(r"/(\d+)\.html", parsed.path)
        product_id = matched.group(1) if matched else ""
    if not product_id:
        raise ValueError("The item URL does not contain a recognized product id")
    return value.strip(), product_id


def find_browser_executable(explicit: str) -> str:
    if explicit:
        return explicit
    candidates = [
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    ]
    for candidate in candidates:
        if Path(candidate).is_file():
            return candidate
    raise FileNotFoundError("Chrome/Edge executable not found; pass --browser-executable")


def find_waxiang_store_insight_extension(executable: str, profile_dir: Path) -> Path | None:
    browser_path = Path(executable)
    if browser_path.name.lower() != "waxiang.exe":
        return None

    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        extension_root = Path(local_app_data) / "Waxiang" / "User Data" / "Default" / "Extensions"
        for manifest_path in extension_root.glob("*/*/manifest.json"):
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(manifest.get("name", "")).strip() == "店透视":
                return manifest_path.parent

    archives = list(browser_path.parent.glob("*/extensions/diantoushi.crx"))
    if not archives:
        return None
    archive_path = max(archives, key=lambda path: path.stat().st_mtime)
    archive_data = archive_path.read_bytes()
    digest = hashlib.sha256(archive_data).hexdigest()[:16]
    extension_dir = profile_dir / "extensions" / f"diantoushi-{digest}"
    if (extension_dir / "manifest.json").is_file():
        return extension_dir

    zip_offset = archive_data.find(b"PK\x03\x04")
    if zip_offset < 0:
        raise RuntimeError("挖象浏览器内置店透视扩展格式无效。")
    extension_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(archive_data[zip_offset:])) as archive:
        for member in archive.infolist():
            destination = (extension_dir / member.filename).resolve()
            if extension_dir.resolve() not in destination.parents and destination != extension_dir.resolve():
                raise RuntimeError("挖象浏览器内置店透视扩展包含无效文件路径。")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as target:
                shutil.copyfileobj(source, target)
    return extension_dir if (extension_dir / "manifest.json").is_file() else None


def close_project_browser_for_profile(profile_dir: Path) -> int:
    if os.name != "nt":
        return 0
    environment = os.environ.copy()
    environment["PRODUCT_IMAGE_PROFILE_DIR"] = str(profile_dir)
    script = """
$profile = [Environment]::GetEnvironmentVariable('PRODUCT_IMAGE_PROFILE_DIR')
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and
  $_.Name -in @('waxiang.exe', 'msedge.exe', 'chrome.exe') -and
  $_.CommandLine -notmatch '--type=' -and
  $_.CommandLine.IndexOf($profile, [System.StringComparison]::OrdinalIgnoreCase) -ge 0
} | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Output $_.ProcessId
}
"""
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return 0
    return sum(line.strip().isdigit() for line in result.stdout.splitlines())


def connect_browser(playwright: Playwright, args: argparse.Namespace) -> Browser:
    first_error: Exception | None = None
    if args.reuse_existing_cdp:
        try:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            print("[collector] 已连接现有采集浏览器。", flush=True)
            return browser
        except Exception as error:
            first_error = error
    if not args.auto_launch:
        raise RuntimeError(
            f"Could not connect to {args.cdp_url}. Start Chrome/Edge with remote debugging "
            "or pass --auto-launch."
        ) from first_error

    executable = find_browser_executable(args.browser_executable)
    profile_dir = Path(args.profile_dir).expanduser().resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    closed_count = close_project_browser_for_profile(profile_dir)
    if closed_count:
        print("[collector] 已关闭遗留的采集浏览器窗口，正在使用同一登录档案重新启动。", flush=True)
        time.sleep(1)
    parsed_cdp_url = urlparse(args.cdp_url)
    cdp_port = parsed_cdp_url.port or 9223
    launch_args = [
        executable,
        f"--remote-debugging-port={cdp_port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    extension_dir = find_waxiang_store_insight_extension(executable, profile_dir)
    if extension_dir:
        launch_args.append(f"--load-extension={extension_dir}")
        print("[collector] 已加载挖象浏览器内置店透视扩展。", flush=True)
    print("[collector] 正在启动采集浏览器。", flush=True)
    subprocess.Popen(
        launch_args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            browser = playwright.chromium.connect_over_cdp(args.cdp_url)
            print("[collector] 采集浏览器已启动。", flush=True)
            return browser
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(
        "Browser did not expose the CDP endpoint. Verify the selected browser can start with remote debugging."
    ) from first_error


def visible_text_locator(page: Page, label: str):
    locator = page.get_by_text(label, exact=True)
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    locator = page.get_by_text(label, exact=False)
    for index in range(locator.count()):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except Exception:
            continue
    return None


def click_text(page: Page, labels: tuple[str, ...], timeout_ms: int) -> str:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for label in labels:
            candidate = visible_text_locator(page, label)
            if candidate is not None:
                candidate.click()
                page.wait_for_timeout(ACTION_DELAY_MS)
                return label
        page.wait_for_timeout(250)
    raise RuntimeError(
        "当前采集专用浏览器未检测到店透视快捷下载入口。"
        "请确认采集专用浏览器已安装、启用并登录店透视，并关闭可能遮挡页面的下载完成弹窗。"
        "刷新商品页后再发送‘继续采集’。"
    )


def dismiss_download_complete_dialog(page: Page, timeout_ms: int = 3000) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        for frame in page.frames:
            markers = frame.get_by_text(DOWNLOAD_COMPLETE_PATTERN, exact=False)
            for index in range(markers.count()):
                marker = markers.nth(index)
                try:
                    if not marker.is_visible():
                        continue
                    clicked = marker.evaluate(
                        """element => {
                            let dialog = element;
                            while (dialog.parentElement) {
                                const text = dialog.innerText || '';
                                if (text.includes('下载任务') && text.includes('文件下载完成')) break;
                                dialog = dialog.parentElement;
                            }
                            const text = dialog.innerText || '';
                            if (!text.includes('文件下载完成')) return false;
                            const candidates = [...dialog.querySelectorAll(
                                "button,[role='button'],[aria-label],[title],[class*='close'],[class*='Close']"
                            )];
                            const close = candidates.find(node => {
                                const key = [
                                    node.textContent || '',
                                    node.getAttribute('aria-label') || '',
                                    node.getAttribute('title') || '',
                                    String(node.className || '')
                                ].join(' ').trim().toLowerCase();
                                return key === 'x' || key === '×' || key.includes('关闭') || key.includes('close');
                            });
                            if (close) {
                                (close.closest("button,[role='button']") || close).click();
                                return true;
                            }
                            const rect = dialog.getBoundingClientRect();
                            const target = document.elementFromPoint(rect.right - 18, rect.top + 18);
                            if (target && dialog.contains(target)) {
                                (target.closest("button,[role='button']") || target).click();
                                return true;
                            }
                            return false;
                        }"""
                    )
                    if clicked:
                        page.wait_for_timeout(500)
                        return True
                except Exception:
                    continue
        page.wait_for_timeout(250)
    return False


def login_required(page: Page) -> bool:
    current_url = page.url.lower()
    body_text = page.locator("body").inner_text(timeout=5000)
    return "login.taobao.com" in current_url or "亲，请登录" in body_text


def wait_for_login(page: Page, timeout_seconds: int) -> None:
    if not login_required(page):
        return
    print(f"[collector] 检测到登录页面，请在打开的浏览器中完成登录，最多等待 {timeout_seconds} 秒。", flush=True)
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        if not login_required(page):
            print("[collector] 已检测到登录完成，继续采集。", flush=True)
            return
    raise RuntimeError("Taobao/Tmall login was not completed before the wait timeout")


def platform_challenge_text(page: Page) -> str | None:
    url_markers = {
        "sec.taobao.com": "平台验证",
        "punish": "平台验证",
        "captcha": "验证码",
        "/verify": "安全验证",
        "baxia": "安全验证",
    }
    markers = (
        "符合镜像",
        "图形验证",
        "验证码",
        "滑块",
        "安全验证",
        "人机验证",
        "请完成验证",
        "请按住滑块",
        "拖动滑块",
        "点击验证",
    )
    frames = [page, *(getattr(page, "frames", []) or [])]
    for frame in frames:
        current_url = str(getattr(frame, "url", "") or "").lower()
        url_marker = next((label for marker, label in url_markers.items() if marker in current_url), None)
        if url_marker:
            return url_marker
        try:
            title = frame.title().lower()
        except Exception:
            title = ""
        try:
            body_text = frame.locator("body").inner_text(timeout=5000).lower()
        except Exception:
            body_text = ""
        risk_marker = next((marker for marker in HARD_RISK_MARKERS if marker in title or marker in body_text), None)
        if risk_marker:
            return risk_marker
        challenge_marker = next((marker for marker in markers if marker in title or marker in body_text), None)
        if challenge_marker:
            return challenge_marker
    return None


def wait_for_platform_challenge(page: Page, timeout_seconds: int) -> bool:
    marker = platform_challenge_text(page)
    if not marker:
        return False
    if marker in HARD_RISK_MARKERS:
        raise RiskControlDetected(f"淘宝风控已拒绝访问（{marker}），采集脚本已停止，不会自动重试。")
    print(
        f"[collector] 检测到平台验证页面（{marker}），采集已暂停导航和点击。"
        "请在当前浏览器页面完成验证，程序会保持等待并自动继续。",
        flush=True,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        page.wait_for_timeout(2000)
        marker = platform_challenge_text(page)
        if marker in HARD_RISK_MARKERS:
            raise RiskControlDetected(f"淘宝风控已拒绝访问（{marker}），采集脚本已停止，不会自动重试。")
        if not marker:
            print("[collector] 平台验证已解除，继续采集。", flush=True)
            return True
    raise RuntimeError("平台验证未完成，采集已暂停。请完成验证后发送‘继续采集’。")


def item_product_id(url: str) -> str:
    try:
        return str(parse_qs(urlparse(url).query).get("id", [""])[0] or "")
    except (AttributeError, TypeError, ValueError):
        return ""


def wait_for_store_insight_entry(page: Page, timeout_ms: int, login_wait_seconds: int) -> None:
    """Wait until the Store Insight download entry has been injected."""
    deadline = time.monotonic() + timeout_ms / 1000
    next_gate_check = 0.0
    entry = page.locator("#dts_item .goods-image-download .down").first
    while time.monotonic() < deadline:
        if time.monotonic() >= next_gate_check:
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            next_gate_check = time.monotonic() + 2
        try:
            if entry.is_visible():
                return
        except Exception:
            pass
        page.wait_for_timeout(250)
    raise RuntimeError("Store Insight entry was not injected into the item page")


def reload_item(page: Page, item_url: str, timeout_ms: int, login_wait_seconds: int) -> None:
    wait_for_platform_challenge(page, login_wait_seconds)
    wait_for_login(page, login_wait_seconds)
    if item_product_id(page.url) == item_product_id(item_url):
        try:
            wait_for_store_insight_entry(page, timeout_ms, login_wait_seconds)
        except RuntimeError:
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            print("[collector] 店透视入口尚未就绪，刷新当前商品页后继续等待。", flush=True)
            page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            wait_for_store_insight_entry(page, timeout_ms, login_wait_seconds)
        print("[collector] 继续使用当前商品页，不重复刷新。", flush=True)
        return
    page.goto(item_url, wait_until="domcontentloaded", timeout=timeout_ms)
    wait_for_login(page, login_wait_seconds)
    wait_for_platform_challenge(page, login_wait_seconds)
    wait_for_store_insight_entry(page, timeout_ms, login_wait_seconds)


def save_download(download, asset_type: str, downloads_dir: Path) -> Path:
    target = downloads_dir / f"{asset_type}-{int(time.time())}.zip"
    download.save_as(str(target))
    return target


def open_store_insight_menu(page: Page, item_url: str, timeout_ms: int, login_wait_seconds: int) -> None:
    reload_item(page, item_url, timeout_ms, login_wait_seconds)
    if dismiss_download_complete_dialog(page, 1000):
        print("[collector] 已关闭店透视下载完成弹窗。", flush=True)
    deadline = time.monotonic() + timeout_ms / 1000
    entry = page.locator("#dts_item .goods-image-download .down").first
    next_gate_check = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_gate_check:
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            next_gate_check = time.monotonic() + 2
        try:
            if entry.is_visible():
                entry.click()
                page.wait_for_timeout(ACTION_DELAY_MS)
                return
        except Exception:
            pass
        page.wait_for_timeout(250)
    raise RuntimeError("当前商品页未加载店透视的商品图下载入口。")


def click_store_insight_asset(page: Page, asset_type: str, timeout_ms: int, login_wait_seconds: int) -> None:
    menu = page.locator(".el-dropdown-menu.el-dropdown-menu--mini").filter(has=page.locator(".combo-download-action, .el-dropdown-menu__item"))
    deadline = time.monotonic() + timeout_ms / 1000
    next_gate_check = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_gate_check:
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            next_gate_check = time.monotonic() + 2
        try:
            visible_menu = next(
                (menu.nth(index) for index in range(menu.count()) if menu.nth(index).is_visible()),
                None,
            )
            if visible_menu is None:
                page.wait_for_timeout(250)
                continue
            if asset_type == "main":
                button = visible_menu.locator(".combo-download-action-btn").nth(0)
            elif asset_type == "detail":
                button = visible_menu.locator(".combo-download-action-btn").nth(2)
            else:
                button = next(
                    (
                        visible_menu.locator(".el-dropdown-menu__item").nth(index)
                        for index in range(visible_menu.locator(".el-dropdown-menu__item").count())
                        if "sku" in visible_menu.locator(".el-dropdown-menu__item").nth(index).inner_text().lower()
                    ),
                    None,
                )
            if button is not None and button.is_visible():
                button.click()
                page.wait_for_timeout(ACTION_DELAY_MS)
                return
        except Exception:
            pass
        page.wait_for_timeout(250)
    raise RuntimeError(f"店透视未显示 {asset_type} 图片的快捷下载按钮。")


def is_all_files_download_label(value: str) -> bool:
    normalized = re.sub(r"\s+", "", value).replace("（", "(").replace("）", ")")
    return "下载全部" in normalized and "多文件" in normalized


def click_store_insight_all_files(page: Page, timeout_ms: int, login_wait_seconds: int) -> None:
    menus = page.locator(".el-dropdown-menu.el-dropdown-menu--mini").filter(
        has=page.locator(".combo-download-action, .el-dropdown-menu__item")
    )
    deadline = time.monotonic() + timeout_ms / 1000
    next_gate_check = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_gate_check:
            wait_for_platform_challenge(page, login_wait_seconds)
            wait_for_login(page, login_wait_seconds)
            next_gate_check = time.monotonic() + 2
        try:
            visible_menu = next(
                (menus.nth(index) for index in range(menus.count()) if menus.nth(index).is_visible()),
                None,
            )
            if visible_menu is None:
                page.wait_for_timeout(250)
                continue
            items = visible_menu.locator(".el-dropdown-menu__item")
            button = next(
                (
                    items.nth(index)
                    for index in range(items.count())
                    if is_all_files_download_label(items.nth(index).inner_text())
                ),
                None,
            )
            if button is not None and button.is_visible():
                button.click()
                page.wait_for_timeout(ACTION_DELAY_MS)
                return
        except Exception:
            pass
        page.wait_for_timeout(250)
    raise RuntimeError("店透视未显示“下载全部（多文件）”按钮。")


def wait_for_download_after_click(
    page: Page,
    click_action,
    timeout_ms: int,
    login_wait_seconds: int,
):
    downloads: list[object] = []

    def capture(download) -> None:
        downloads.append(download)

    page.on("download", capture)
    try:
        click_action()
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if downloads:
                return downloads[0]
            if platform_challenge_text(page):
                wait_for_platform_challenge(page, login_wait_seconds)
                raise RuntimeError("平台验证已解除，需要重新触发下载。")
            page.wait_for_timeout(500)
        raise RuntimeError("等待店透视下载超时。")
    finally:
        page.remove_listener("download", capture)


def download_store_insight_zip(
    page: Page,
    item_url: str,
    asset_type: str,
    downloads_dir: Path,
    timeout_ms: int,
    login_wait_seconds: int,
) -> Path:
    errors: list[str] = []

    for attempt in range(2):
        try:
            open_store_insight_menu(page, item_url, timeout_ms, login_wait_seconds)
            download = wait_for_download_after_click(
                page,
                lambda: click_store_insight_asset(page, asset_type, timeout_ms, login_wait_seconds),
                timeout_ms,
                login_wait_seconds,
            )
            saved = save_download(download, asset_type, downloads_dir)
            print(f"[collector] {asset_type} 图片包已保存，正在整理图片。", flush=True)
            if dismiss_download_complete_dialog(page):
                print("[collector] 已关闭店透视下载完成弹窗。", flush=True)
            return saved
        except RiskControlDetected:
            raise
        except (RuntimeError, PlaywrightTimeoutError) as error:
            errors.append(f"quick download attempt {attempt + 1}: {error}")
            waited = wait_for_platform_challenge(page, login_wait_seconds)
            if attempt == 0 and (waited or item_product_id(page.url) == item_product_id(item_url)):
                print("[collector] 验证完成或下载未响应，保持当前页面重试一次，不刷新商品页。", flush=True)
                continue
            break

    raise RuntimeError("Store Insight download failed: " + " | ".join(errors))


def download_store_insight_all_zip(
    page: Page,
    item_url: str,
    downloads_dir: Path,
    timeout_ms: int,
    login_wait_seconds: int,
) -> Path:
    errors: list[str] = []

    for attempt in range(2):
        try:
            open_store_insight_menu(page, item_url, timeout_ms, login_wait_seconds)
            download = wait_for_download_after_click(
                page,
                lambda: click_store_insight_all_files(page, timeout_ms, login_wait_seconds),
                timeout_ms,
                login_wait_seconds,
            )
            saved = save_download(download, "all", downloads_dir)
            print("[collector] 店透视全部图片（多文件）包已保存，正在分类整理。", flush=True)
            if dismiss_download_complete_dialog(page):
                print("[collector] 已关闭店透视下载完成弹窗。", flush=True)
            return saved
        except RiskControlDetected:
            raise
        except (RuntimeError, PlaywrightTimeoutError) as error:
            errors.append(f"all-files download attempt {attempt + 1}: {error}")
            waited = wait_for_platform_challenge(page, login_wait_seconds)
            if attempt == 0 and (waited or item_product_id(page.url) == item_product_id(item_url)):
                print("[collector] 全部图片下载未响应，保持当前商品页重试一次。", flush=True)
                continue
            break

    raise RuntimeError("Store Insight all-files download failed: " + " | ".join(errors))


WINDOWS_INVALID_FILENAME_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
WINDOWS_RESERVED_FILENAMES = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def sanitize_zip_component(value: str) -> str:
    sanitized = WINDOWS_INVALID_FILENAME_CHARS.sub("_", value)
    sanitized = re.sub(r"[ .]+$", "_", sanitized) or "_"
    if sanitized.split(".", 1)[0].upper() in WINDOWS_RESERVED_FILENAMES:
        sanitized = f"_{sanitized}"
    return sanitized


def _safe_zip_components(member_name: str) -> list[str]:
    normalized = member_name.replace("\\", "/")
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:/", normalized):
        raise RuntimeError(f"Unsafe ZIP member path: {member_name}")
    raw_components = normalized.split("/")
    if any(component == ".." for component in raw_components):
        raise RuntimeError(f"Unsafe ZIP member path: {member_name}")
    return [
        sanitize_zip_component(component)
        for component in raw_components
        if component not in {"", "."}
    ]


def _unique_zip_destination(destination: Path, root: Path, allocated: set[str]) -> Path:
    def key(path: Path) -> str:
        return str(path.relative_to(root)).replace("\\", "/").casefold()

    candidate = destination
    index = 2
    while candidate.exists() or key(candidate) in allocated:
        candidate = destination.with_name(f"{destination.stem}_{index}{destination.suffix}")
        index += 1
    allocated.add(key(candidate))
    return candidate


def safe_extract(zip_path: Path, target_dir: Path) -> list[Path]:
    target_dir.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []
    root = target_dir.resolve()
    allocated: set[str] = set()
    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            components = _safe_zip_components(member.filename)
            if not components:
                continue
            destination = (target_dir.joinpath(*components)).resolve()
            if root != destination and root not in destination.parents:
                raise RuntimeError(f"Unsafe ZIP member path: {member.filename}")
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            destination = _unique_zip_destination(destination, root, allocated)
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, destination.open("wb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(destination)
    return extracted


def classify_file(path: Path, requested_type: str) -> str | None:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    tokens = {token.lower() for token in re.split(r"[\\/_\-\s:]+", str(path))}
    if requested_type == "sku" or "sku" in tokens or "sku图" in tokens:
        return "sku"
    if requested_type == "detail" or "detail" in tokens or "详情图" in tokens:
        return "detail"
    return "main"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _embedded_douyin_image_urls(item_url: str) -> list[str]:
    raw_value = parse_qs(urlparse(item_url).query).get("goods_detail", [""])[0]
    if not raw_value:
        return []
    value: Any = raw_value
    for _ in range(2):
        if not isinstance(value, str):
            break
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return []
    if not isinstance(value, dict):
        return []
    image_info = value.get("img")
    if not isinstance(image_info, dict):
        return []
    candidates = image_info.get("url_list")
    if not isinstance(candidates, list):
        return []
    urls: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate:
            continue
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme in {"http", "https"} and any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in DOUYIN_IMAGE_HOST_SUFFIXES
        ):
            urls.append(candidate)
    return urls


def _download_douyin_embedded_main_image(
    item_url: str,
    output_root: Path,
    max_main_images: int | None,
) -> tuple[dict[str, Any] | None, str]:
    if max_main_images is not None and max_main_images <= 0:
        return None, "main image fallback disabled"
    urls = _embedded_douyin_image_urls(item_url)
    if not urls:
        return None, "goods_detail.img.url_list not found"

    main_root = output_root / "main"
    main_root.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    for image_url in urls:
        try:
            request = urllib.request.Request(
                image_url,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://haohuo.jinritemai.com/",
                },
            )
            with urllib.request.urlopen(request, timeout=30) as response:
                image_data = response.read(DOUYIN_EMBEDDED_IMAGE_MAX_BYTES + 1)
            if not image_data:
                raise ValueError("empty image response")
            if len(image_data) > DOUYIN_EMBEDDED_IMAGE_MAX_BYTES:
                raise ValueError("image response exceeds size limit")
            suffix = Path(urlparse(image_url).path).suffix.lower()
            if suffix not in IMAGE_EXTENSIONS:
                suffix = ".jpg"
            destination = main_root / f"001{suffix}"
            destination.write_bytes(image_data)
            digest = sha256(destination)
            return (
                {
                    "type": "main",
                    "path": str(destination.resolve()),
                    "source_name": "goods_detail.img.url_list",
                    "source_url": image_url,
                    "sha256": digest,
                },
                "",
            )
        except Exception as error:  # A second embedded URL may still work.
            errors.append(f"{type(error).__name__}: {error}")
    return None, "; ".join(errors) or "embedded image download failed"


def classify_archive_path(path: Path, requested_type: str) -> str | None:
    if path.suffix.lower() not in IMAGE_EXTENSIONS:
        return None
    normalized = str(path).lower()
    if "sku" in normalized or "sku\u56fe" in normalized:
        return "sku"
    if "detail" in normalized or "\u8be6\u60c5\u56fe" in normalized or "\u8be6\u60c5\u957f\u56fe" in normalized:
        return "detail"
    if (
        "main" in normalized
        or "\u4e3b\u56fe" in normalized
        or "\u9875\u9762\u5c55\u793a" in normalized
        or "\u9875\u9762\u56fe" in normalized
        or "1:1\u56fe" in normalized
    ):
        return "main"
    return requested_type if requested_type in ASSET_TYPES else None


def materialize(
    zip_path: Path,
    asset_type: str,
    output_root: Path,
    known_hashes: set[str],
    max_main_images: int | None = None,
    allowed_types: set[str] | None = None,
    counters: dict[str, int] | None = None,
) -> list[dict]:
    records = []
    counters = counters if counters is not None else {}
    with TemporaryDirectory(prefix="product-image-extract-") as temporary:
        files = safe_extract(zip_path, Path(temporary))
        for source in files:
            classified = classify_archive_path(source, asset_type)
            if classified is None:
                continue
            if allowed_types is not None and classified not in allowed_types:
                continue
            if classified == "main" and max_main_images is not None and counters.get("main", 0) >= max_main_images:
                continue
            digest = sha256(source)
            if digest in known_hashes:
                continue
            known_hashes.add(digest)
            counters[classified] = counters.get(classified, 0) + 1
            destination_root = output_root / classified
            destination_root.mkdir(parents=True, exist_ok=True)
            destination = destination_root / f"{counters[classified]:03d}{source.suffix.lower()}"
            shutil.copy2(source, destination)
            records.append(
                {
                    "type": classified,
                    "path": str(destination.resolve()),
                    "source_name": source.name,
                    "sha256": digest,
                }
            )
    return records


def empty_video_metadata() -> dict:
    return {
        "main_video_requested": True,
        "main_video_url": "",
        "main_video_status": "not_found",
        "main_video_error": "",
    }


def empty_sku_metadata() -> dict:
    return {
        "sku_metadata_status": "not_found",
        "sku_metadata_error": "",
        "sku_variants": [],
    }


def collect_product_summary(page: Page) -> dict:
    raw = page.evaluate(
        """
        () => {
            const text = element => (element?.content || element?.innerText || element?.textContent || '').trim();
            const first = selectors => {
                for (const selector of selectors) {
                    const value = text(document.querySelector(selector));
                    if (value) return value;
                }
                return '';
            };
            return {
                product_title: first([
                    'meta[property="og:title"]',
                    'h1',
                    '[class*="ItemTitle"]',
                    '[class*="itemTitle"]',
                    '[class*="title--"]'
                ]) || document.title,
                current_price: first([
                    'meta[property="product:price:amount"]',
                    '[class*="Price--"]',
                    '[class*="price--"]',
                    '[class*="priceText"]',
                    '[class*="price"]'
                ])
            };
        }
        """
    )
    title = re.sub(r"\s+", " ", str((raw or {}).get("product_title") or "")).strip()
    price_text = re.sub(r"\s+", "", str((raw or {}).get("current_price") or ""))
    price_match = re.search(r"\d+(?:\.\d{1,2})?", price_text.replace(",", ""))
    return {
        "product_title": title[:300],
        "current_price": price_match.group(0) if price_match else "",
    }


def parse_sku_variant_fields(label: str, fallback_index: int) -> dict:
    parsed = parse_store_insight_sku_filename(
        f"SKU图_{fallback_index}_{label}.jpg",
        fallback_index,
    )
    color_text = parsed.get("color_text", "")
    matches = SKU_COLOR_VALUE_PATTERN.findall(label)
    if not color_text and matches:
        color_text = "+".join(dict.fromkeys(matches))

    spec_text = parsed.get("spec_text", "")
    if not spec_text:
        remainder = SKU_COLOR_VALUE_PATTERN.sub("", label)
        remainder = re.sub(r"(?:颜色分类|颜色|色号|规格|型号|尺码|大小|容量|净含量)\s*[:：=]?", "", remainder, flags=re.I)
        remainder = re.sub(r"[;；,，|/＋+]+", " ", remainder)
        remainder = re.sub(r"\s+", " ", remainder).strip(" _-()（）")
        spec_text = remainder

    parse_status = "parsed" if spec_text and color_text else "partial" if spec_text or color_text else "unparsed"
    return {
        "sku_label": label,
        "spec_text": spec_text,
        "color_text": color_text,
        "parse_status": parse_status,
    }


def collect_sku_variants(
    page: Page,
    product_id: str,
    downloads_dir: Path,
    timeout_ms: int,
) -> dict:
    """Export the Store Insight SKU table only; do not download SKU images here."""
    metadata = empty_sku_metadata()
    preview_page: Page | None = None
    try:
        context = page.context
        original_pages = set(context.pages)
        if not click_first_visible_text(page, "SKU预览", timeout_ms=min(timeout_ms, 10_000)):
            metadata["sku_metadata_error"] = "店透视未显示 SKU 预览入口"
            return metadata
        page.wait_for_timeout(800)
        preview_page = next(
            (candidate for candidate in context.pages if candidate not in original_pages),
            page,
        )
        if preview_page is not page:
            preview_page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        table_path = download_store_insight_sku_export_on_page(
            preview_page,
            {"product_id": product_id},
            "导出表格",
            downloads_dir,
            timeout_ms,
            "sku-table.xlsx",
        )
        if table_path is None:
            metadata["sku_metadata_error"] = "店透视 SKU 表格导出失败"
            return metadata
        rows = parse_store_insight_sku_table(table_path)
        if not rows:
            metadata["sku_metadata_error"] = "店透视 SKU 表格中未识别到可用数据"
            return metadata
        variants = []
        for index, row in enumerate(rows, start=1):
            parsed = parse_sku_variant_fields(row.get("sku_label", ""), index)
            variants.append(
                {
                    "source_index": row.get("source_index", str(index)),
                    "sku_id": row.get("sku_id", ""),
                    "sku_label": row.get("sku_label", ""),
                    "spec_text": parsed.get("spec_text", "") or row.get("net_content", ""),
                    "color_text": parsed.get("color_text", ""),
                    "list_price": row.get("list_price", ""),
                    "after_coupon_price": row.get("after_coupon_price", ""),
                    "stock": row.get("stock", ""),
                    "net_content": row.get("net_content", ""),
                    "parse_status": parsed.get("parse_status", "unparsed"),
                }
            )
        metadata.update({"sku_metadata_status": "complete", "sku_variants": variants})
    except Exception as error:  # Metadata failures must not discard downloaded images.
        metadata["sku_metadata_status"] = "failed"
        metadata["sku_metadata_error"] = f"{type(error).__name__}: {error}"[:300]
    finally:
        if preview_page is not None and preview_page is not page:
            try:
                preview_page.close()
            except Exception:
                pass
    return metadata


def collect_item_metadata(
    context,
    page: Page,
    item_url: str,
    product_id: str,
    downloads_dir: Path,
    timeout_ms: int,
) -> dict:
    metadata = empty_parameter_metadata(product_id, "not_found")
    metadata.update(empty_video_metadata())
    metadata.update(empty_sku_metadata())

    try:
        metadata.update(collect_product_summary(page))
    except Exception as error:
        metadata.update({"product_title": "", "current_price": "", "product_summary_error": str(error)[:300]})

    try:
        metadata.update(collect_product_parameters(page, product_id, timeout_ms))
    except Exception as error:
        metadata["parameter_status"] = "failed"
        metadata["parameter_error"] = f"{type(error).__name__}: {error}"[:300]

    try:
        video_url = collect_product_video_url(context, {"item_url": item_url}, timeout_ms)
        if video_url:
            metadata.update({"main_video_url": video_url, "main_video_status": "complete"})
        else:
            metadata["main_video_error"] = "商品页未发现可打开的原视频 URL"
    except Exception as error:
        metadata["main_video_status"] = "failed"
        metadata["main_video_error"] = f"{type(error).__name__}: {error}"[:300]

    metadata.update(collect_sku_variants(page, product_id, downloads_dir, timeout_ms))
    return metadata


def attach_sku_variants_to_images(images: list[dict], sku_variants: list[dict]) -> None:
    """Attach exported SKU table rows to SKU images without relying on image downloads."""
    sku_images = [record for record in images if record.get("type") == "sku"]
    by_label = {row.get("sku_label", ""): row for row in sku_variants if row.get("sku_label")}
    by_source_index = {
        str(row.get("source_index", "")): row
        for row in sku_variants
        if row.get("source_index") not in (None, "")
    }
    for ordinal, record in enumerate(sku_images):
        source_name = record.get("source_name", "")
        filename_metadata = parse_store_insight_sku_filename(source_name, ordinal + 1)
        parsed = parse_sku_variant_fields(Path(source_name).stem, ordinal + 1)
        row = by_source_index.get(str(filename_metadata.get("index", "")))
        if row is None:
            row = by_label.get(parsed.get("sku_label", ""))
        if row is None and ordinal < len(sku_variants):
            row = sku_variants[ordinal]
        if row is None:
            record.update(parsed)
            record.pop("index", None)
            record["metadata_status"] = "image_only"
            continue
        record.update(row)
        record["metadata_status"] = "table_matched"


def build_manifest(
    item_url: str,
    product_id: str,
    output_root: Path,
    images: list[dict],
    metadata: dict,
) -> dict:
    return {
        "schema_version": 2,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_url": item_url,
        "product_id": product_id,
        "output_root": str(output_root),
        "images": images,
        **metadata,
    }


def collect_store_insight_payload(
    page: Page,
    context: Any,
    item_url: str,
    product_id: str,
    platform: str,
    downloads_dir: Path,
    output_root: Path,
    selected_types: set[str],
    timeout_ms: int,
    login_wait_seconds: int,
    max_main_images: int | None,
) -> tuple[list[dict], dict]:
    if platform == "douyin":
        wait_for_platform_challenge(page, login_wait_seconds)
        print("[collector] 正在下载店透视抖音商品资料（多文件）包。", flush=True)
        archive = download_douyin_package(
            page,
            product_id,
            downloads_dir,
            timeout_ms,
            selected_types,
        )
        records, metadata = materialize_douyin_package(
            archive,
            output_root,
            selected_types,
            max_main_images,
        )
        if "main" in selected_types and not any(record.get("type") == "main" for record in records):
            fallback_record, fallback_error = _download_douyin_embedded_main_image(
                item_url,
                output_root,
                max_main_images,
            )
            if fallback_record is not None:
                records.append(fallback_record)
                metadata["main_source_status"] = "embedded_url_fallback"
                metadata["main_source_url"] = fallback_record["source_url"]
            else:
                metadata["main_source_status"] = "package_empty"
                metadata["main_source_error"] = fallback_error
        metadata["collected_asset_types"] = [
            asset_type
            for asset_type in ASSET_TYPES
            if asset_type in selected_types and any(record.get("type") == asset_type for record in records)
        ]
        metadata["missing_asset_types"] = [
            asset_type for asset_type in sorted(selected_types) if asset_type not in metadata["collected_asset_types"]
        ]
        return records, metadata

    print("[collector] 正在下载店透视全部图片（多文件）包。", flush=True)
    archive = download_store_insight_all_zip(
        page,
        item_url,
        downloads_dir,
        timeout_ms,
        login_wait_seconds,
    )
    known_hashes: set[str] = set()
    counters: dict[str, int] = {}
    records = materialize(
        archive,
        "all",
        output_root,
        known_hashes,
        max_main_images,
        selected_types,
        counters,
    )
    compensation_errors: dict[str, str] = {}
    for asset_type in ASSET_TYPES:
        if asset_type not in selected_types or any(record["type"] == asset_type for record in records):
            continue
        print(f"[collector] 全部图片包缺少 {asset_type}，正在单独补充下载。", flush=True)
        try:
            compensation_archive = download_store_insight_zip(
                page,
                item_url,
                asset_type,
                downloads_dir,
                timeout_ms,
                login_wait_seconds,
            )
            compensated_records = materialize(
                compensation_archive,
                asset_type,
                output_root,
                known_hashes,
                max_main_images,
                selected_types,
                counters,
            )
            if not compensated_records:
                compensation_errors[asset_type] = (
                    f"{asset_type} compensation archive contained no usable images"
                )
                continue
            records.extend(compensated_records)
        except RiskControlDetected:
            raise
        except (RuntimeError, OSError, zipfile.BadZipFile) as error:
            compensation_errors[asset_type] = str(error)
            print(f"[collector] {asset_type} 图片补充下载失败：{error}", flush=True)
    print("[collector] 正在采集商品参数、SKU规格价格颜色和原视频链接。", flush=True)
    metadata = collect_item_metadata(
        context,
        page,
        item_url,
        product_id,
        downloads_dir,
        timeout_ms,
    )
    collected_types = [
        asset_type
        for asset_type in ASSET_TYPES
        if asset_type in selected_types and any(record["type"] == asset_type for record in records)
    ]
    metadata.update(
        {
            "requested_asset_types": sorted(selected_types),
            "collected_asset_types": collected_types,
            "missing_asset_types": [
                asset_type for asset_type in sorted(selected_types) if asset_type not in collected_types
            ],
        }
    )
    if compensation_errors:
        metadata["asset_compensation_errors"] = compensation_errors
    attach_sku_variants_to_images(records, metadata.get("sku_variants") or [])
    return records, metadata


def main() -> int:
    global ACTION_DELAY_MS
    args = parse_args()
    if args.max_main_images < 0:
        raise ValueError("--max-main-images must be 0 or greater")
    ACTION_DELAY_MS = max(0, round(args.action_delay_seconds * 1000))
    item_url, product_id = validate_item_url(args.url)
    item_host = (urlparse(item_url).hostname or "").lower()
    platform = "douyin" if is_douyin_product_host(item_host) else "commerce"
    selected_types = tuple(dict.fromkeys(args.types))
    allowed_types = set(selected_types)
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_root = Path(args.output).expanduser().resolve() / product_id / run_id
    downloads_dir = output_root / "_work" / "downloads"
    downloads_dir.mkdir(parents=True, exist_ok=True)
    all_records = []

    print("[collector] 采集器已启动，正在连接浏览器。", flush=True)
    with sync_playwright() as playwright:
        browser = connect_browser(playwright, args)
        if not browser.contexts:
            raise RuntimeError("CDP browser has no available context")
        context = browser.contexts[0]
        page = context.new_page()
        metadata: dict = {}
        try:
            page.goto(item_url, wait_until="domcontentloaded", timeout=args.timeout_seconds * 1000)
            page.wait_for_timeout(2500)
            all_records, metadata = collect_store_insight_payload(
                page,
                context,
                item_url,
                product_id,
                platform,
                downloads_dir,
                output_root,
                allowed_types,
                args.timeout_seconds * 1000,
                args.login_wait_seconds,
                args.max_main_images or None,
            )

            for asset_type in selected_types:
                count = sum(record["type"] == asset_type for record in all_records)
                print(f"[collector] {asset_type}: {count} images", flush=True)

            print(
                "[collector] 元数据采集完成："
                f"参数 {metadata.get('parameter_status')}，"
                f"SKU {metadata.get('sku_metadata_status')}，"
                f"视频 {metadata.get('main_video_status')}。",
                flush=True,
            )
        finally:
            page.close()

    manifest = build_manifest(item_url, product_id, output_root, all_records, metadata)
    manifest_path = output_root / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[collector] manifest: {manifest_path}", flush=True)
    return 0


if __name__ == "__main__":
    configure_output()
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"[collector] 采集失败：{error}", file=sys.stderr, flush=True)
        raise
