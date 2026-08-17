"""Local web interface for Store Insight collection and image workflows."""

from __future__ import annotations

import cgi
import argparse
import json
import mimetypes
import os
import socket
import shutil
import subprocess
import sys
import threading
import uuid
import webbrowser
from dataclasses import asdict
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from zipfile import BadZipFile

from agent_flow import DEFAULT_MAIN_IMAGES, AgentReply, AgentSession, IntentRecognitionError, classify_message
from batch_workflow import (
    BatchRunner,
    DirectLinkBatchItem,
    DirectReplaceBatchItem,
    _direct_link_platform,
    _safe_name,
    extract_batch_items,
    extract_direct_link_items,
    extract_direct_replace_items,
    export_product_workbook,
    find_prior_direct_collected_manifest,
    load_batch_results,
    normalize_direct_manifest,
    resolve_direct_item_url,
    resolve_supplement_workbook,
    save_batch_results,
)
from image_workflows import ApiSettings, WorkflowRunner, load_manifest_tasks, resolve_identity_image
from oss_uploader import OssConfigurationError, OssUploader, upload_generation_records, upload_video_if_needed
from product_identity import ProductIdentity, ProductIdentityError, ProductIdentityResolver
from shared_library_cache import SharedLibraryCache
from shared_library_client import (
    LockLease,
    SharedLibraryClient,
    SharedLibraryLockBusy,
    SharedLibraryUnavailable,
)
from shared_package_builder import SharedPackageBuilder, materialize_reused_package


FROZEN = bool(getattr(sys, "frozen", False))
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
BUNDLE_ROOT = Path(getattr(sys, "_MEIPASS", ROOT)) if FROZEN else ROOT
STATIC_ROOT = BUNDLE_ROOT / "web"
OUTPUT_ROOT = ROOT / "outputs"
COLLECTOR_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
COLLECTOR_PROFILE_ROOT = Path(os.getenv("LOCALAPPDATA") or ROOT) / "ProductImageWorkflow" / "store-insight-profile"
SETTINGS_PATH = ROOT / "local_settings.json"
SESSION_PATH = OUTPUT_ROOT / "current_session.json"
STARTUP_URL_PATH = ROOT / "startup_url.txt"
STARTUP_ERROR_PATH = ROOT / "startup_error.log"
BROWSER_LABELS = {
    "waxiang": "挖象浏览器",
    "edge": "微软 Edge",
}


def close_project_collection_browser() -> None:
    if os.name != "nt":
        return
    environment = os.environ.copy()
    environment["PRODUCT_IMAGE_PROFILE_DIR"] = str(COLLECTOR_PROFILE_ROOT)
    script = """
$profile = [Environment]::GetEnvironmentVariable('PRODUCT_IMAGE_PROFILE_DIR')
Get-CimInstance Win32_Process | Where-Object {
  $_.CommandLine -and
  $_.Name -in @('waxiang.exe', 'msedge.exe', 'chrome.exe') -and
  $_.CommandLine -notmatch '--type=' -and
  $_.CommandLine -match [regex]::Escape($profile)
} | ForEach-Object {
  taskkill.exe /PID $_.ProcessId /T /F | Out-Null
}
"""
    try:
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            env=environment,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        pass
COLLECTION_RESUME_PHRASES = (
    "继续采集",
    "重新采集",
    "开始采集",
    "重试采集",
    "采集所有",
    "采集全部",
    "只采集",
    "仅采集",
    "采集主图",
    "采集sku",
    "采集详情",
)


def requests_collection_resume(message: str) -> bool:
    normalized = message.strip().lower()
    return any(phrase in normalized for phrase in COLLECTION_RESUME_PHRASES)


def collection_resume_is_current(expected_version: int, current_version: int) -> bool:
    return expected_version == current_version


def load_local_api_config() -> tuple[str, str, str]:
    try:
        document = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RuntimeError("Missing local_settings.json. Add the image API key to the local configuration.") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("local_settings.json is not valid JSON.") from error

    base_url = str(document.get("base_url", "")).strip()
    image_key = str(document.get("image_api_key", "")).strip()
    vision_key = str(document.get("vision_api_key", "")).strip()
    if not base_url.startswith("http"):
        raise RuntimeError("local_settings.json must include base_url.")
    return base_url, image_key, vision_key


def load_model_api_keys() -> tuple[str, str]:
    try:
        _, image_key, vision_key = load_local_api_config()
    except RuntimeError:
        return "", ""
    return vision_key, image_key


def save_model_api_keys(vision_api_key: str, image_api_key: str) -> None:
    vision_key = vision_api_key.strip()
    image_key = image_api_key.strip()
    if not vision_key or not image_key:
        raise RuntimeError("视觉模型和生图模型 API Key 都不能为空。")
    try:
        document = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        template_path = SETTINGS_PATH.with_name("local_settings.example.json")
        try:
            document = json.loads(template_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise RuntimeError("缺少 local_settings.json 和配置模板，无法保存模型 API Key。") from error
        except json.JSONDecodeError as error:
            raise RuntimeError("local_settings.example.json 格式无效，无法保存模型 API Key。") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("local_settings.json 格式无效，无法保存模型 API Key。") from error
    if not isinstance(document, dict):
        raise RuntimeError("local_settings.json 必须是 JSON 对象。")

    document["vision_api_key"] = vision_key
    document["image_api_key"] = image_key
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = SETTINGS_PATH.with_name(f".{SETTINGS_PATH.name}.{os.getpid()}.tmp")
    try:
        temporary_path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary_path, SETTINGS_PATH)
    finally:
        temporary_path.unlink(missing_ok=True)


def browser_choice_label(choice: str) -> str:
    return BROWSER_LABELS.get(choice, "未选择")


def browser_candidates(choice: str) -> tuple[Path, ...]:
    program_files = os.getenv("ProgramFiles") or r"C:\Program Files"
    program_files_x86 = os.getenv("ProgramFiles(x86)") or r"C:\Program Files (x86)"
    local_app_data = os.getenv("LOCALAPPDATA") or ""
    if choice == "waxiang":
        return tuple(
            Path(candidate)
            for candidate in (
                r"D:\WaXiangBrowser\waxiang.exe",
                r"C:\WaXiangBrowser\waxiang.exe",
                str(Path(program_files) / "WaXiangBrowser" / "waxiang.exe"),
                str(Path(program_files_x86) / "WaXiangBrowser" / "waxiang.exe"),
                str(Path(local_app_data) / "WaXiangBrowser" / "waxiang.exe") if local_app_data else "",
            )
            if candidate
        )
    if choice == "edge":
        return tuple(
            Path(candidate)
            for candidate in (
                str(Path(program_files_x86) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(program_files) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                str(Path(local_app_data) / "Microsoft" / "Edge" / "Application" / "msedge.exe") if local_app_data else "",
            )
            if candidate
        )
    return ()


def waixiang_registry_candidates() -> tuple[Path, ...]:
    if os.name != "nt":
        return ()
    try:
        import winreg
    except ImportError:
        return ()

    candidates: list[Path] = []
    roots = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall", 0),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall", 0),
    )
    for hive, key_path, flag in roots:
        try:
            with winreg.OpenKey(hive, key_path, 0, winreg.KEY_READ | flag) as uninstall_key:
                subkey_count = winreg.QueryInfoKey(uninstall_key)[0]
                for index in range(subkey_count):
                    subkey_name = winreg.EnumKey(uninstall_key, index)
                    try:
                        with winreg.OpenKey(uninstall_key, subkey_name) as app_key:
                            display_name = str(winreg.QueryValueEx(app_key, "DisplayName")[0])
                            if "挖象" not in display_name and "waxiang" not in display_name.lower():
                                continue
                            for value_name in ("DisplayIcon", "InstallLocation"):
                                try:
                                    value = str(winreg.QueryValueEx(app_key, value_name)[0]).split(",", 1)[0].strip()
                                except FileNotFoundError:
                                    continue
                                path = Path(value)
                                candidates.append(path if path.suffix.lower() == ".exe" else path / "waxiang.exe")
                    except OSError:
                        continue
        except OSError:
            continue
    return tuple(candidates)


def find_browser_executable(choice: str) -> Path | None:
    if choice not in BROWSER_LABELS:
        return None
    candidates = list(browser_candidates(choice))
    if choice == "waxiang":
        candidates.extend(waixiang_registry_candidates())
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    return None


def load_browser_choice() -> str:
    try:
        document = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ""
    choice = str(document.get("browser_choice", "")).strip().lower()
    if choice in BROWSER_LABELS:
        return choice
    return ""


def load_browser_executable() -> str:
    executable = find_browser_executable(load_browser_choice())
    return str(executable) if executable else ""


def allocate_collection_cdp_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{listener.getsockname()[1]}"


def load_image_api_config() -> tuple[str, str]:
    base_url, image_key, _ = load_local_api_config()
    return base_url, image_key


def load_default_vision_api_key() -> str:
    _, _, vision_key = load_local_api_config()
    return vision_key


def load_api_settings(vision_api_key: str, image_api_key: str = "") -> ApiSettings:
    selected_vision_key = vision_api_key.strip()
    selected_image_key = image_api_key.strip()
    if not selected_vision_key:
        raise RuntimeError("Please enter the vision model API key.")
    if not selected_image_key:
        raise RuntimeError("Please enter the image generation API key.")
    base_url, _, _ = load_local_api_config()
    return ApiSettings(base_url=base_url, vision_api_key=selected_vision_key, image_api_key=selected_image_key)


def load_optional_oss_uploader() -> tuple[OssUploader | None, str | None]:
    try:
        uploader = OssUploader.from_settings_file(SETTINGS_PATH)
    except OssConfigurationError as error:
        return None, f"OSS 配置不可用，生成图片将仅保存在本地：{error}"
    if uploader is None:
        return None, "OSS 未配置，生成图片将仅保存在本地。"
    return uploader, None


def load_shared_library_cache() -> SharedLibraryCache:
    return SharedLibraryCache(OUTPUT_ROOT / "shared-library-cache")


def load_shared_library_client() -> SharedLibraryClient | None:
    uploader, _ = load_optional_oss_uploader()
    if uploader is None:
        return None
    cache = load_shared_library_cache()
    return SharedLibraryClient(uploader.config, uploader.bucket, cache.client_id)


def _valid_shared_product_key(value: str) -> bool:
    platform, separator, product_id = value.partition("-")
    return separator == "-" and platform in {"taobao", "tmall"} and product_id.isdigit()


def _path_within_shared_reused_root(value: Path) -> bool:
    root = (OUTPUT_ROOT / "reused").resolve()
    target = Path(value).resolve()
    return target == root or root in target.parents


def shared_job_is_eligible(agent: AgentSession) -> bool:
    return (
        agent.generation_enabled
        and agent.generation_mode == "competitor_reference"
        and tuple(agent.workflows) == ("main", "sku", "detail")
        and agent.max_main_images == DEFAULT_MAIN_IMAGES
        and agent.max_sku_images is None
        and agent.max_detail_images is None
    )


def resolve_shared_identity(reference_url: str) -> ProductIdentity | None:
    return ProductIdentityResolver(
        redirect_resolver=lambda value, _timeout: resolve_direct_item_url(value)
    ).resolve(reference_url)


def summarize_batch_validation(batch_items: list[Any]) -> dict[str, int]:
    counts = {
        "missing_images": 0,
        "missing_links": 0,
        "pairing_conflicts": 0,
    }
    for item in batch_items:
        error = str(getattr(item, "validation_error", "") or "")
        if "缺少我方商品图" in error:
            counts["missing_images"] += 1
        if "缺少商品链接" in error:
            counts["missing_links"] += 1
        if "配对冲突" in error:
            counts["pairing_conflicts"] += 1
    return counts


def choose_supplement_workbook() -> Path | None:
    if os.name != "nt":
        return None
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$dialog = New-Object System.Windows.Forms.OpenFileDialog; "
        "$dialog.Filter = 'Excel 工作簿 (*.xlsx)|*.xlsx'; "
        "$dialog.Title = '选择此前导出的单商品结果表格'; "
        "if ($dialog.ShowDialog() -eq [System.Windows.Forms.DialogResult]::OK) { "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; $dialog.FileName }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-STA", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    selected = Path(completed.stdout.strip()) if completed.returncode == 0 and completed.stdout.strip() else None
    return selected if selected and selected.is_file() else None


def merge_collected_manifest(existing_path: Path, incoming_path: Path, refreshed_types: tuple[str, ...]) -> Path:
    existing = json.loads(existing_path.read_text(encoding="utf-8"))
    incoming = json.loads(incoming_path.read_text(encoding="utf-8"))
    refreshed = set(refreshed_types)
    existing_images = [
        image for image in existing.get("images", [])
        if isinstance(image, dict) and image.get("type") not in refreshed
    ]
    incoming_images = [
        image for image in incoming.get("images", [])
        if isinstance(image, dict) and image.get("type") in refreshed
    ]
    existing["images"] = existing_images + incoming_images
    incoming_missing = set(incoming.get("missing_asset_types") or [])
    existing_missing = set(existing.get("missing_asset_types") or [])
    existing_missing.difference_update(refreshed)
    existing_missing.update(asset_type for asset_type in refreshed if asset_type in incoming_missing)
    existing["requested_asset_types"] = list(dict.fromkeys([
        *(existing.get("requested_asset_types") or []),
        *(incoming.get("requested_asset_types") or []),
    ]))
    existing["collected_asset_types"] = [
        category
        for category in ("main", "sku", "detail")
        if any(image.get("type") == category for image in existing["images"])
    ]
    existing["missing_asset_types"] = [
        category for category in ("main", "sku", "detail") if category in existing_missing
    ]
    existing["updated_at"] = datetime.now().isoformat(timespec="seconds")
    existing_path.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    return materialize_manifest_assets(existing_path)


def recover_generation_records(manifest_path: Path, generated_root: Path) -> tuple[list[dict], Path | None]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], None
    source_paths = {
        os.path.normcase(str(Path(str(image.get("path") or "")).resolve()))
        for image in manifest.get("images", [])
        if isinstance(image, dict) and image.get("path")
    }
    if not source_paths or not generated_root.is_dir():
        return [], None
    candidates = sorted(
        generated_root.glob("*/analysis.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    for analysis_path in candidates:
        try:
            document = json.loads(analysis_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        records = [record for record in document.get("records", []) if isinstance(record, dict)]
        record_sources = {
            os.path.normcase(str(Path(str(record.get("source_path") or "")).resolve()))
            for record in records
            if record.get("source_path")
        }
        if not records or not record_sources or not record_sources.issubset(source_paths):
            continue
        usable = [
            record
            for record in records
            if record.get("status") == "completed"
            and record.get("output_path")
            and Path(str(record["output_path"])).is_file()
        ]
        if usable:
            return usable, analysis_path.parent
    return [], None


def plan_missing_generation_ordinals(
    manifest: Path,
    workflows: tuple[str, ...],
    max_main_images: int | None,
    max_sku_images: int | None,
    max_detail_images: int | None,
    previous_records: list[dict],
) -> dict[str, list[int]]:
    expected_tasks = load_manifest_tasks(
        manifest,
        workflows,
        max_main_images,
        max_sku_images,
        max_detail_images,
    )
    valid_keys = {
        (str(record.get("category") or ""), int(record.get("ordinal") or 0))
        for record in previous_records
        if record.get("status") == "completed"
        and record.get("output_path")
        and Path(str(record["output_path"])).is_file()
    }
    missing: dict[str, list[int]] = {}
    for task in expected_tasks:
        key = (task.category, task.ordinal)
        if key not in valid_keys:
            missing.setdefault(task.category, []).append(task.ordinal)
    return missing


def export_single_product_workbook(
    manifest: Path,
    reference_url: str,
    generated_output: Path | None,
    records: list[dict] | None = None,
) -> tuple[Path, list[dict], Path | None]:
    source = json.loads(manifest.read_text(encoding="utf-8"))
    platform, validation_error = _direct_link_platform(reference_url)
    if validation_error or platform in {"invalid", "unsupported"}:
        raise ValueError(validation_error or "当前链接不是受支持的商品链接。")
    top_product = (source.get("products") or [{}])[0]
    product_id = str(source.get("product_id") or top_product.get("product_id") or "")
    title = str(source.get("product_title") or top_product.get("title") or "").strip()
    item = DirectLinkBatchItem(
        sequence=1,
        row_number=1,
        source_url=reference_url,
        platform=platform,
        title=title,
    )
    selected_records = [dict(record) for record in records] if records is not None else []
    recovered_output: Path | None = None
    if records is None and generated_output:
        analysis_path = generated_output / "analysis.json"
        if analysis_path.is_file():
            document = json.loads(analysis_path.read_text(encoding="utf-8"))
            selected_records = [
                record for record in document.get("records", []) if isinstance(record, dict)
            ]
    if records is None and not selected_records:
        selected_records, recovered_output = recover_generation_records(
            manifest,
            OUTPUT_ROOT / "generated",
        )
    titles: dict = {}
    titles_path = manifest.parent / "titles.json"
    if titles_path.is_file():
        loaded_titles = json.loads(titles_path.read_text(encoding="utf-8"))
        if isinstance(loaded_titles, dict):
            titles = loaded_titles
    fallback = f"商品-{product_id or 'single'}"
    output_path = manifest.parent / f"{_safe_name(title, fallback)}.xlsx"
    exported = export_product_workbook(
        output_path,
        item,
        manifest,
        selected_records,
        titles,
        ROOT,
        include_metadata_only_skus=True,
    )
    return Path(exported), selected_records, recovered_output


def materialize_manifest_assets(manifest_path: Path) -> Path:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    root = manifest_path.parent.resolve()
    counters = {category: 0 for category in ("main", "sku", "detail")}
    changed = False
    for image in document.get("images", []):
        if not isinstance(image, dict):
            continue
        category = str(image.get("type", ""))
        if category not in counters:
            continue
        source = Path(str(image.get("path", ""))).resolve()
        if not source.is_file():
            continue
        counters[category] += 1
        suffix = source.suffix.lower() or ".jpg"
        target = root / category / f"{counters[category]:03d}{suffix}"
        if source != target.resolve():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            image["path"] = str(target)
            changed = True
    if changed:
        document["updated_at"] = datetime.now().isoformat(timespec="seconds")
        manifest_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest_path


def find_interrupted_batch() -> tuple[Path, Path, int, list[dict], str, str] | None:
    checkpoints = sorted(
        (OUTPUT_ROOT / "batches").glob("*/batch-results.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if (OUTPUT_ROOT / "batches").is_dir() else []
    for checkpoint in checkpoints:
        try:
            document = json.loads(checkpoint.read_text(encoding="utf-8"))
            workbook_path = Path(str(document.get("source", "")))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not workbook_path.is_file():
            continue
        results = load_batch_results(workbook_path, checkpoint.parent)
        if not results or not any(item.get("status") != "completed" for item in results):
            continue
        batch_mode = str(document.get("batch_mode") or "image_search")
        run_mode = str(document.get("run_mode") or "full")
        source_images = checkpoint.parent / "source-images"
        total = sum(1 for path in source_images.iterdir() if path.is_file()) if source_images.is_dir() else 0
        total = int(document.get("total") or total or max((int(item.get("sequence") or 0) for item in results), default=0))
        return workbook_path, checkpoint.parent, total, results, batch_mode, run_mode
    return None


class AppState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.agent = AgentSession()
        self.manifest_path: Path | None = None
        self.product_image: Path | None = None
        self.generated_output: Path | None = None
        self.vision_api_key, self.image_api_key = load_model_api_keys()
        self.vision_api_error = ""
        self.collecting = False
        self.generating = False
        self.collector_pid: int | None = None
        self.collection_stop_requested = False
        self.collection_paused = False
        self.collection_control_version = 0
        self.logs: list[str] = []
        self.events: list[dict] = []
        self.results: list[dict[str, str | int]] = []
        self.completed_task_signature: str | None = None
        self.runner: WorkflowRunner | None = None
        self.batch_input: Path | None = None
        self.batch_input_name = ""
        self.batch_output: Path | None = None
        self.batch_running = False
        self.batch_stop_requested = False
        self.batch_total = 0
        self.batch_mode = "image_search"
        self.batch_run_mode = "full"
        self.batch_valid = 0
        self.batch_invalid = 0
        self.batch_unsupported = 0
        self.batch_missing_images = 0
        self.batch_missing_links = 0
        self.batch_pairing_conflicts = 0
        self.batch_events: list[dict] = []
        self.batch_results: list[dict] = []
        self.batch_runner: BatchRunner | None = None
        self.supplement_workbook: Path | None = None
        self.supplement_running = False
        self.supplement_stop_requested = False
        self.supplement_runner: BatchRunner | None = None
        self.supplement_events: list[dict] = []
        self.shared_library: dict[str, object] = {
            "status": "idle",
            "product_key": "",
            "message": "",
            "catalog": None,
        }
        self.shared_client: SharedLibraryClient | None = None
        self.shared_lease: LockLease | None = None
        self.shared_identity: ProductIdentity | None = None
        self.shared_publish_allowed = False
        self.shared_heartbeat_stop: threading.Event | None = None
        self.shared_heartbeat_thread: threading.Thread | None = None
        self._restore_session()

    def _restore_session(self) -> None:
        try:
            document = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
            manifest_path = Path(str(document.get("manifest_path", "")))
            product_image = Path(str(document.get("product_image", "")))
            generated_output = Path(str(document.get("generated_output", "")))
            agent = document.get("agent", {})
            workflows = tuple(value for value in agent.get("workflows", []) if value in {"main", "sku", "detail"})
            raw_collection_types = agent.get("collection_types") or workflows
            collection_types = tuple(
                value for value in raw_collection_types if value in {"main", "sku", "detail"}
            )
            self.agent = AgentSession(
                reference_url=agent.get("reference_url") or None,
                max_main_images=agent.get("max_main_images"),
                max_sku_images=agent.get("max_sku_images"),
                max_detail_images=agent.get("max_detail_images"),
                main_quantity_mode=agent.get(
                    "main_quantity_mode",
                    "reference" if agent.get("max_main_images") is None else "default"
                    if int(agent.get("max_main_images") or DEFAULT_MAIN_IMAGES) == DEFAULT_MAIN_IMAGES
                    else "custom",
                ),
                quantity_confirmed=bool(
                    agent.get(
                        "quantity_confirmed",
                        bool(agent.get("reference_url"))
                        and agent.get("awaiting") not in {"reference_url", "main_quantity"},
                    )
                ),
                awaiting=agent.get("awaiting", "reference_url"),
                manifest_loaded=manifest_path.is_file(),
                workflows=workflows,
                collection_types=collection_types,
                generation_enabled=bool(agent.get("generation_enabled", True)),
                generation_mode=agent.get("generation_mode", "competitor_reference"),
            )
            self.manifest_path = manifest_path if manifest_path.is_file() else None
            self.product_image = product_image if product_image.is_file() else None
            self.generated_output = generated_output if generated_output.is_dir() else None
            self.collection_paused = bool(document.get("collection_paused", False))
            stored_results = document.get("results", [])
            completed_signature = document.get("completed_task_signature")
            reusable_partial = bool(
                self.generated_output
                and (self.generated_output / "analysis.json").is_file()
                and isinstance(stored_results, list)
            )
            if (
                isinstance(stored_results, list)
                and self.generated_output
                and (
                    isinstance(completed_signature, str)
                    and completed_signature == self.task_signature()
                    or reusable_partial
                )
            ):
                self.results = [item for item in stored_results if isinstance(item, dict)]
                if isinstance(completed_signature, str) and completed_signature == self.task_signature():
                    self.completed_task_signature = completed_signature
            else:
                self.generated_output = None
            batch = document.get("batch", {})
            supplement_workbook = Path(str(document.get("supplement_workbook", "")))
            self.supplement_workbook = supplement_workbook if supplement_workbook.is_file() else None
            batch_input = Path(str(batch.get("input_path", "")))
            batch_output = Path(str(batch.get("output_path", "")))
            if batch_input.is_file() and batch_output.is_dir():
                self.batch_input = batch_input
                self.batch_input_name = str(batch.get("input_name", ""))
                self.batch_output = batch_output
                self.batch_total = int(batch.get("total") or 0)
                self.batch_mode = str(batch.get("mode") or "image_search")
                self.batch_run_mode = str(batch.get("run_mode") or "full")
                self.batch_valid = int(batch.get("valid") or self.batch_total)
                self.batch_invalid = int(batch.get("invalid") or 0)
                self.batch_unsupported = int(batch.get("unsupported") or 0)
                self.batch_missing_images = int(batch.get("missing_images") or 0)
                self.batch_missing_links = int(batch.get("missing_links") or 0)
                self.batch_pairing_conflicts = int(batch.get("pairing_conflicts") or 0)
                self.batch_results = load_batch_results(batch_input, batch_output)
            if not self.batch_input:
                interrupted = find_interrupted_batch()
                if interrupted:
                    (
                        self.batch_input,
                        self.batch_output,
                        self.batch_total,
                        self.batch_results,
                        self.batch_mode,
                        self.batch_run_mode,
                    ) = interrupted
                    self.batch_input_name = "已恢复的批处理表格"
        except (OSError, ValueError, json.JSONDecodeError, TypeError):
            return

    def task_signature(self) -> str | None:
        if not self.manifest_path or not self.agent.workflows:
            return None
        try:
            identity_image = resolve_identity_image(
                self.manifest_path,
                self.product_image,
                self.agent.generation_mode,
            )
        except (OSError, ValueError, json.JSONDecodeError):
            return None
        return json.dumps(
            {
                "manifest_path": str(self.manifest_path.resolve()),
                "identity_image": str(identity_image),
                "generation_mode": self.agent.generation_mode,
                "workflows": self.agent.workflows,
                "max_main_images": self.agent.max_main_images,
                "max_sku_images": self.agent.max_sku_images,
                "max_detail_images": self.agent.max_detail_images,
            },
            ensure_ascii=True,
            sort_keys=True,
        )

    def reset_generation(self) -> None:
        self.generated_output = None
        self.completed_task_signature = None
        self.events = []
        self.results = []

    def save_session(self) -> None:
        document = {
            "agent": asdict(self.agent),
            "manifest_path": str(self.manifest_path) if self.manifest_path else "",
            "product_image": str(self.product_image) if self.product_image else "",
            "generated_output": str(self.generated_output) if self.generated_output else "",
            "results": self.results,
            "completed_task_signature": self.completed_task_signature,
            "collection_paused": self.collection_paused,
            "batch": {
                "input_path": str(self.batch_input) if self.batch_input else "",
                "input_name": self.batch_input_name,
                "output_path": str(self.batch_output) if self.batch_output else "",
                "total": self.batch_total,
                "mode": self.batch_mode,
                "run_mode": self.batch_run_mode,
                "valid": self.batch_valid,
                "invalid": self.batch_invalid,
                "unsupported": self.batch_unsupported,
                "missing_images": self.batch_missing_images,
                "missing_links": self.batch_missing_links,
                "pairing_conflicts": self.batch_pairing_conflicts,
            },
            "supplement_workbook": str(self.supplement_workbook) if self.supplement_workbook else "",
        }
        SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        SESSION_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    def log(self, message: str) -> None:
        with self.lock:
            self.logs.append(f"{datetime.now():%H:%M:%S} {message}")
            self.logs = self.logs[-100:]

    def status(self) -> dict:
        with self.lock:
            api_configured = False
            try:
                load_local_api_config()
                api_configured = True
            except RuntimeError:
                pass
            browser_choice = load_browser_choice()
            browser_executable = load_browser_executable()
            identity_source = (
                "uploaded_product"
                if self.agent.generation_mode == "own_product"
                else "collected_main"
            )
            identity_ready = False
            if self.agent.generation_mode == "own_product":
                identity_ready = bool(self.product_image and self.product_image.is_file())
            elif self.manifest_path and self.manifest_path.is_file():
                try:
                    resolve_identity_image(self.manifest_path, None, "competitor_reference")
                    identity_ready = True
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
            return {
                "agent": asdict(self.agent),
                "identity_source": identity_source,
                "identity_ready": identity_ready,
                "manifest_path": str(self.manifest_path) if self.manifest_path else None,
                "product_image": str(self.product_image) if self.product_image else None,
                "collecting": self.collecting,
                "generating": self.generating,
                "collector_pid": self.collector_pid,
                "collection_stop_requested": self.collection_stop_requested,
                "collection_paused": self.collection_paused,
                "logs": self.logs,
                "events": self.events[-100:],
                "api_configured": api_configured,
                "vision_api_ready": bool(self.vision_api_key),
                "vision_api_builtin": False,
                "image_api_ready": bool(self.image_api_key),
                "vision_api_error": self.vision_api_error or None,
                "browser_choice": browser_choice,
                "browser_label": browser_choice_label(browser_choice),
                "browser_detected": bool(browser_executable),
                "browser_executable": browser_executable,
                "generated_count": len(self.results),
                "collected_summary": self._collected_summary(),
                "missing_workflows": self.missing_workflows(),
                "unavailable_workflows": self.unavailable_workflows(),
                "folders": {
                    "collected": bool(self.manifest_path and self.manifest_path.parent.is_dir()),
                    "generated": bool(self.generated_output and self.generated_output.is_dir()),
                },
                "workflow_progress": self._workflow_progress(),
                "shared_library": dict(self.shared_library),
                "batch": {
                    "input_path": str(self.batch_input) if self.batch_input else None,
                    "input_name": self.batch_input_name or None,
                    "output_path": str(self.batch_output) if self.batch_output else None,
                    "running": self.batch_running,
                    "stop_requested": self.batch_stop_requested,
                    "total": self.batch_total,
                    "mode": self.batch_mode,
                    "run_mode": self.batch_run_mode,
                    "valid": self.batch_valid,
                    "invalid": self.batch_invalid,
                    "unsupported": self.batch_unsupported,
                    "missing_images": self.batch_missing_images,
                    "missing_links": self.batch_missing_links,
                    "pairing_conflicts": self.batch_pairing_conflicts,
                    "completed": sum(item.get("status") == "completed" for item in self.batch_results),
                    "collected": sum(item.get("status") == "collected" for item in self.batch_results),
                    "failed": sum(item.get("status") == "failed" for item in self.batch_results),
                    "stopped": sum(item.get("status") == "stopped" for item in self.batch_results),
                    "current": max(
                        [int(item.get("sequence") or 0) for item in self.batch_events]
                        + [int(item.get("sequence") or 0) for item in self.batch_results],
                        default=0,
                    ),
                    "events": self.batch_events[-30:],
                },
                "supplement": {
                    "running": self.supplement_running,
                    "stop_requested": self.supplement_stop_requested,
                    "workbook": str(self.supplement_workbook) if self.supplement_workbook else None,
                    "events": self.supplement_events[-30:],
                    "completed": sum(
                        status == "completed"
                        for status in self._latest_supplement_statuses().values()
                    ),
                    "failed": sum(
                        status == "failed"
                        for status in self._latest_supplement_statuses().values()
                    ),
                },
                "supplement_workbook": str(self.supplement_workbook) if self.supplement_workbook else None,
            }

    def _latest_supplement_statuses(self) -> dict[tuple[str, int], str]:
        latest: dict[tuple[str, int], str] = {}
        for event in self.supplement_events:
            category = str(event.get("category") or "")
            ordinal = int(event.get("ordinal") or 0)
            if category and ordinal > 0:
                latest[(category, ordinal)] = str(event.get("status") or "")
        return latest

    def _collected_summary(self) -> dict[str, int]:
        if not self.manifest_path:
            return {"main": 0, "sku": 0, "detail": 0, "total": 0}
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"main": 0, "sku": 0, "detail": 0, "total": 0}

        counters = {category: 0 for category in ("main", "sku", "detail")}
        for entry in document.get("images", []):
            category = str(entry.get("type", ""))
            if category not in counters:
                continue
            target = Path(str(entry.get("path", ""))).resolve()
            try:
                relative = target.relative_to(OUTPUT_ROOT.resolve())
            except ValueError:
                continue
            if not target.is_file():
                continue
            counters[category] += 1
        counters["total"] = sum(counters.values())
        return counters

    def missing_workflows(self) -> tuple[str, ...]:
        counters = self._collected_summary()
        unavailable = set(self.unavailable_workflows())
        return tuple(
            category for category in self.agent.workflows if not counters[category] and category not in unavailable
        )

    def missing_collection_workflows(self) -> tuple[str, ...]:
        counters = self._collected_summary()
        unavailable = set(self.unavailable_collection_workflows())
        return tuple(
            category
            for category in self.agent.collection_types
            if not counters[category] and category not in unavailable
        )

    def unavailable_workflows(self) -> tuple[str, ...]:
        if not self.manifest_path:
            return ()
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        missing = set(document.get("missing_asset_types") or [])
        counters = self._collected_summary()
        return tuple(
            category for category in self.agent.workflows if category in missing and not counters[category]
        )

    def unavailable_collection_workflows(self) -> tuple[str, ...]:
        if not self.manifest_path:
            return ()
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ()
        missing = set(document.get("missing_asset_types") or [])
        counters = self._collected_summary()
        return tuple(
            category
            for category in self.agent.collection_types
            if category in missing and not counters[category]
        )

    def runnable_workflows(self) -> tuple[str, ...]:
        counters = self._collected_summary()
        return tuple(category for category in self.agent.workflows if counters[category])

    @staticmethod
    def _records_to_results(records) -> list[dict[str, str | int]]:
        results: list[dict[str, str | int]] = []
        for record in records:
            if not isinstance(record, dict) or record.get("status") != "completed" or not record.get("output_path"):
                continue
            target = Path(str(record["output_path"])).resolve()
            try:
                relative = target.relative_to(OUTPUT_ROOT.resolve())
            except ValueError:
                continue
            if not target.is_file():
                continue
            results.append(
                {
                    "category": str(record["category"]),
                    "ordinal": int(record["ordinal"]),
                    "url": "/output/" + relative.as_posix(),
                    "public_url": str(record.get("output_public_url") or ""),
                }
            )
        return results

    def _workflow_progress(self) -> dict[str, dict[str, int]]:
        latest: dict[tuple[str, int], str] = {}
        for event in self.events:
            latest[(str(event["category"]), int(event["ordinal"]))] = str(event["status"])
        progress = {
            category: {"analyzing": 0, "prompt_ready": 0, "generating": 0, "completed": 0, "failed": 0}
            for category in ("main", "sku", "detail")
        }
        for (category, _), status in latest.items():
            if category in progress and status in progress[category]:
                progress[category][status] += 1
        return progress


STATE = AppState()


class RequestHandler(SimpleHTTPRequestHandler):
    server_version = "ProductWorkflow/1.0"

    def do_GET(self) -> None:
        parsed_path = urlparse(self.path)
        if parsed_path.path == "/api/status":
            self._json(STATE.status())
            return
        if parsed_path.path == "/api/shared-library":
            self._list_shared_library(parsed_path.query)
            return
        if parsed_path.path == "/api/shared-library/preview":
            self._serve_shared_library_preview(parsed_path.query)
            return
        if self.path.startswith("/output/"):
            self._serve_output()
            return
        relative = "index.html" if self.path in ("/", "/index.html") else self.path.lstrip("/")
        target = (STATIC_ROOT / relative).resolve()
        if STATIC_ROOT.resolve() not in target.parents and target != STATIC_ROOT.resolve():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def _serve_output(self) -> None:
        relative = unquote(urlparse(self.path).path.removeprefix("/output/"))
        target = (OUTPUT_ROOT / relative).resolve()
        if OUTPUT_ROOT.resolve() not in target.parents or not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", mimetypes.guess_type(target.name)[0] or "application/octet-stream")
        self.end_headers()
        self.wfile.write(target.read_bytes())

    def do_POST(self) -> None:
        if self.path == "/api/reference-url":
            self._set_reference_url()
            return
        if self.path == "/api/main-quantity":
            self._set_main_quantity()
            return
        if self.path == "/api/generation-mode":
            self._set_generation_mode()
            return
        if self.path == "/api/stop-collection":
            self._stop_collection()
            return
        if self.path == "/api/stop-generation":
            self._stop_generation()
            return
        if self.path == "/api/batch-upload":
            self._upload_batch_workbook()
            return
        if self.path == "/api/batch-start":
            self._start_batch()
            return
        if self.path == "/api/batch-stop":
            self._stop_batch()
            return
        if self.path == "/api/batch-supplement":
            self._start_batch_supplement()
            return
        if self.path == "/api/batch-supplement-stop":
            self._stop_batch_supplement()
            return
        if self.path == "/api/supplement-select":
            self._select_supplement_workbook()
            return
        if self.path == "/api/shutdown":
            self._shutdown_application()
            return
        if self.path == "/api/chat":
            body = self._json_body()
            message = str(body.get("message", ""))
            resume_collection = requests_collection_resume(message)
            with STATE.lock:
                context = {
                    "agent": asdict(STATE.agent),
                    "collected_summary": STATE._collected_summary(),
                    "collection_folder": str(STATE.manifest_path.parent) if STATE.manifest_path else None,
                    "manifest_path": str(STATE.manifest_path) if STATE.manifest_path else None,
                    "product_image_loaded": bool(STATE.product_image),
                    "collecting": STATE.collecting,
                    "generating": STATE.generating,
                }
                current_url = STATE.agent.reference_url
                collection_control_version = STATE.collection_control_version
                vision_key = STATE.vision_api_key
            try:
                base_url, _, _ = load_local_api_config()
                intent = classify_message(message, context, base_url, vision_key)
            except (IntentRecognitionError, OSError, RuntimeError, ValueError) as error:
                intent = None
                STATE.log(f"LLM intent recognition unavailable; using local parser ({type(error).__name__}).")
            release_shared = False
            with STATE.lock:
                previous_signature = STATE.task_signature()
                if intent and intent.get("action") == "reset" and (STATE.collecting or STATE.generating or STATE.batch_running):
                    reply = AgentReply(
                        "当前任务正在执行，完成后才能重新开始或更换商品链接。",
                        STATE.agent.awaiting,
                        STATE.agent.reference_url,
                        STATE.agent.max_main_images,
                        STATE.agent.workflows,
                    )
                elif intent and intent.get("action") == "reset":
                    release_shared = True
                    STATE.agent = AgentSession()
                    STATE.manifest_path = None
                    STATE.reset_generation()
                    reply = AgentReply(
                        "已重新开始当前任务。产品图仍保留，请提供新的对标商品链接。",
                        STATE.agent.awaiting,
                        None,
                        None,
                        (),
                    )
                elif intent:
                    incoming_url = str(intent.get("reference_url") or "").strip()
                    if incoming_url and incoming_url != current_url and (STATE.collecting or STATE.generating or STATE.batch_running):
                        reply = AgentReply(
                            "当前任务正在执行，完成后才能更换商品链接。",
                            STATE.agent.awaiting,
                            STATE.agent.reference_url,
                            STATE.agent.max_main_images,
                            STATE.agent.workflows,
                        )
                    else:
                        reply = STATE.agent.apply_intent(intent)
                        if current_url != STATE.agent.reference_url:
                            release_shared = True
                            STATE.manifest_path = None
                            STATE.agent.manifest_loaded = False
                            STATE.reset_generation()
                else:
                    reply = STATE.agent.handle(message)
                if resume_collection and collection_resume_is_current(
                    collection_control_version,
                    STATE.collection_control_version,
                ):
                    STATE.collection_paused = False
                if previous_signature != STATE.task_signature():
                    STATE.reset_generation()
                STATE.save_session()
            if release_shared:
                self._release_shared_session()
            STATE.log(f"Agent: {reply.message}")
            self._maybe_auto_collect()
            self._maybe_auto_generate()
            self._json({"reply": asdict(reply), "status": STATE.status()})
            return
        if self.path == "/api/collect":
            self._start_collection()
            return
        if self.path == "/api/product-image":
            self._upload_product_image()
            return
        if self.path == "/api/visual-key":
            self._set_vision_api_key()
            return
        if self.path == "/api/api-keys":
            self._set_api_keys()
            return
        if self.path == "/api/browser-choice":
            self._set_browser_choice()
            return
        if self.path == "/api/generate":
            self._start_generation()
            return
        if self.path == "/api/open-folder":
            self._open_folder()
            return
        if self.path == "/api/shared-library/open-folder":
            self._open_shared_library_folder()
            return
        if self.path == "/api/shared-library/reuse":
            self._reuse_shared_library_item()
            return
        if self.path == "/api/export-single":
            self._export_single_workbook()
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _set_reference_url(self) -> None:
        body = self._json_body()
        reference_url = str(body.get("reference_url", "")).strip()
        if not reference_url:
            self._json({"error": "请填写对标商品链接。"}, HTTPStatus.BAD_REQUEST)
            return
        if not AgentSession._is_supported_url(reference_url):
            self._json({"error": "该链接不是支持的淘宝、天猫、京东、抖音或快手商品链接。"}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            if STATE.collecting or STATE.generating or STATE.batch_running:
                busy = True
            else:
                busy = False
                generation_mode = STATE.agent.generation_mode
                STATE.agent = AgentSession(generation_mode=generation_mode)
                reply = STATE.agent.handle(reference_url)
                STATE.manifest_path = None
                STATE.collection_paused = False
                STATE.reset_generation()
                STATE.save_session()
        if busy:
            self._json({"error": "当前正在执行任务，请等待完成后再更换链接。"}, HTTPStatus.BAD_REQUEST)
            return
        self._release_shared_session()
        STATE.log("已设置新的对标商品链接。")
        recovered = find_prior_direct_collected_manifest(
            reference_url,
            (OUTPUT_ROOT / "batches", OUTPUT_ROOT / "store-insight"),
        )
        if recovered:
            manifest, asset_count = recovered
            reused_manifest = normalize_direct_manifest(
                manifest,
                OUTPUT_ROOT
                / "store-insight"
                / f"reused-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}"
                / "direct-manifest.json",
            )
            with STATE.lock:
                if STATE.agent.reference_url == reference_url and not STATE.manifest_path:
                    STATE.manifest_path = reused_manifest
                    STATE.agent.mark_collected()
                    STATE.save_session()
            STATE.log(f"Reused {asset_count} previously collected assets for the same product link.")
        self._json({"reply": asdict(reply), "status": STATE.status()})

    def _set_main_quantity(self) -> None:
        body = self._json_body()
        mode = str(body.get("mode", "")).strip().lower()
        raw_count = body.get("count")
        try:
            count = int(raw_count) if raw_count not in (None, "") else None
            with STATE.lock:
                if STATE.collecting or STATE.generating or STATE.batch_running:
                    busy = True
                else:
                    busy = False
                    STATE.agent.set_main_quantity(mode, count)
                    STATE.reset_generation()
                    STATE.save_session()
        except (TypeError, ValueError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        if busy:
            self._json({"error": "当前正在执行任务，请等待完成后再调整主图数量。"}, HTTPStatus.BAD_REQUEST)
            return
        labels = {"default": f"默认主图数量已设置为 {DEFAULT_MAIN_IMAGES} 张。", "reference": "主图数量已设置为按对标商品实际数量。", "custom": f"主图数量已设置为 {STATE.agent.max_main_images} 张。"}
        STATE.log(labels[mode])
        self._maybe_auto_collect()
        self._json({"accepted": True, "status": STATE.status()})

    def _set_generation_mode(self) -> None:
        mode = str(self._json_body().get("mode", "")).strip().lower()
        if mode not in {"own_product", "competitor_reference"}:
            self._json({"error": "生成模式不受支持。"}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            if STATE.collecting or STATE.generating or STATE.batch_running:
                busy = True
            else:
                busy = False
                STATE.agent.generation_mode = mode
                STATE.reset_generation()
                STATE.save_session()
        if busy:
            self._json({"error": "当前正在执行任务，请等待完成后再切换生成模式。"}, HTTPStatus.BAD_REQUEST)
            return
        STATE.log("生成模式已切换为使用我方产品图。" if mode == "own_product" else "生成模式已切换为直接参考对标商品。")
        self._json({"accepted": True, "status": STATE.status()})

    def _set_browser_choice(self) -> None:
        body = self._json_body()
        choice = str(body.get("browser_choice", "")).strip().lower()
        if choice not in BROWSER_LABELS:
            self._json({"error": "请选择挖象浏览器或微软 Edge。"}, HTTPStatus.BAD_REQUEST)
            return
        browser_name = browser_choice_label(choice)
        if find_browser_executable(choice) is None:
            self._json({"error": f"未检测到{browser_name}，请安装后重新选择。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            document = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            document.pop("browser_executable", None)
            document["browser_choice"] = choice
            SETTINGS_PATH.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except (FileNotFoundError, json.JSONDecodeError, OSError) as error:
            self._json({"error": f"浏览器配置保存失败：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        STATE.log(f"采集浏览器已设置为：{browser_name}")
        with STATE.lock:
            STATE.collection_paused = False
        self._maybe_auto_collect()
        self._json({"accepted": True, "status": STATE.status()})

    def _stop_collection(self) -> None:
        with STATE.lock:
            if not STATE.collecting or not STATE.collector_pid:
                pid = None
            else:
                pid = STATE.collector_pid
                STATE.collection_control_version += 1
                STATE.collection_stop_requested = True
                STATE.collection_paused = True
        if not pid:
            self._json({"error": "当前没有正在进行的采集任务。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F"],
                check=False,
                capture_output=True,
                text=True,
            )
            with STATE.lock:
                STATE.save_session()
            STATE.log("已停止采集进程。")
            self._json({"accepted": True, "stopped": True, "status": STATE.status()})
        except OSError as error:
            STATE.log(f"停止采集失败：{error}")
            self._json({"error": f"无法停止采集进程：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _stop_generation(self) -> None:
        with STATE.lock:
            runner = STATE.runner if STATE.generating else None
        if runner is None:
            self._json({"error": "当前没有正在进行的生成任务。"}, HTTPStatus.BAD_REQUEST)
            return
        runner.cancel()
        STATE.log("已提交停止生成请求，正在结束当前请求并保留已完成图片。")
        self._json({"accepted": True, "status": STATE.status()})

    def _open_folder(self) -> None:
        body = self._json_body()
        kind = str(body.get("kind", ""))
        manifest = None
        with STATE.lock:
            if kind == "collected":
                manifest = STATE.manifest_path
                target = manifest.parent if manifest else None
            elif kind == "generated":
                target = STATE.generated_output
            elif kind == "batch":
                target = STATE.batch_output
            else:
                target = None
        if not target or not target.is_dir():
            self._json({"error": "对应文件夹尚未生成。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            if manifest:
                materialize_manifest_assets(manifest)
            subprocess.Popen(["explorer.exe", str(target)])
        except OSError as error:
            self._json({"error": f"无法打开文件夹：{error}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"accepted": True})

    def _list_shared_library(self, query_string: str) -> None:
        query = parse_qs(query_string)
        platform = str(query.get("platform", [""])[0]).strip().lower()
        search = str(query.get("query", [""])[0]).strip().lower()
        cursor = str(query.get("cursor", [""])[0]).strip()
        if platform not in {"", "taobao", "tmall"}:
            self._json({"error": "共享素材平台筛选无效。"}, HTTPStatus.BAD_REQUEST)
            return
        client = load_shared_library_client()
        if client is None:
            self._json({"error": "共享素材库暂时不可用。"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        cache = load_shared_library_cache()
        try:
            page = client.list_catalog(cursor=cursor, limit=50)
        except SharedLibraryUnavailable:
            self._json({"error": "共享素材库暂时不可用。"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        cache.replace_catalog([dict(item) for item in page.items])
        items = []
        for catalog in page.items:
            product_key = str(catalog.get("product_key") or "")
            item_platform = str(catalog.get("platform") or "")
            product_id = str(catalog.get("product_id") or "")
            if not _valid_shared_product_key(product_key):
                continue
            if platform and item_platform != platform:
                continue
            if search and search not in f"{product_key} {product_id}".lower():
                continue
            local_directory = cache.find_download(
                product_key,
                str(catalog.get("package_sha256") or ""),
            )
            if local_directory is not None and not _path_within_shared_reused_root(local_directory):
                local_directory = None
            items.append(
                {
                    "product_key": product_key,
                    "platform": item_platform,
                    "product_id": product_id,
                    "preview_url": (
                        "/api/shared-library/preview?product_key=" + product_key
                    ),
                    "main_count": int(catalog.get("main_count") or 0),
                    "sku_count": int(catalog.get("sku_count") or 0),
                    "detail_count": int(catalog.get("detail_count") or 0),
                    "package_size": int(catalog.get("package_size") or 0),
                    "created_at": str(catalog.get("created_at") or ""),
                    "local_directory": str(local_directory) if local_directory else None,
                    "available_packages": (
                        [
                            kind
                            for kind in ("complete", "main", "sku", "detail")
                            if isinstance(catalog.get("downloads"), dict)
                            and kind in catalog["downloads"]
                        ]
                        or (["complete"] if catalog.get("package_object") else [])
                    ),
                }
            )
        self._json({"items": items, "next_cursor": page.next_cursor})

    def _open_shared_library_folder(self) -> None:
        product_key = str(self._json_body().get("product_key") or "").strip()
        if not _valid_shared_product_key(product_key):
            self._json({"error": "共享商品标识无效。"}, HTTPStatus.BAD_REQUEST)
            return
        cache = load_shared_library_cache()
        catalog = next(
            (
                item
                for item in cache.load_catalog()
                if str(item.get("product_key") or "") == product_key
            ),
            None,
        )
        local_directory = (
            cache.find_download(product_key, str(catalog.get("package_sha256") or ""))
            if catalog
            else None
        )
        if local_directory is None or not _path_within_shared_reused_root(local_directory):
            self._json({"error": "该共享素材尚未下载到本地。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            subprocess.Popen(["explorer.exe", str(local_directory)])
        except OSError:
            self._json({"error": "无法打开共享素材文件夹。"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"accepted": True})

    def _serve_shared_library_preview(self, query_string: str) -> None:
        product_key = str(parse_qs(query_string).get("product_key", [""])[0]).strip()
        if not _valid_shared_product_key(product_key):
            self.send_error(HTTPStatus.BAD_REQUEST)
            return
        catalog = self._cached_shared_catalog(product_key)
        preview_object = str((catalog or {}).get("preview_object") or "")
        client = load_shared_library_client()
        if not preview_object or client is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            payload = client.read_preview(preview_object)
        except SharedLibraryUnavailable:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE)
            return
        except Exception:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "private, max-age=300")
        self.end_headers()
        self.wfile.write(payload)

    def _reuse_shared_library_item(self) -> None:
        body = self._json_body()
        product_key = str(body.get("product_key") or "").strip()
        package_kind = str(body.get("package_kind") or "complete").strip().lower()
        if not _valid_shared_product_key(product_key) or package_kind not in {
            "complete",
            "main",
            "sku",
            "detail",
        }:
            self._json({"error": "共享素材下载参数无效。"}, HTTPStatus.BAD_REQUEST)
            return
        catalog = self._cached_shared_catalog(product_key)
        client = load_shared_library_client()
        if catalog is None or client is None:
            self._json({"error": "共享素材库暂时不可用。"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        downloads = catalog.get("downloads")
        metadata = downloads.get(package_kind) if isinstance(downloads, dict) else None
        if package_kind == "complete" and not isinstance(metadata, dict):
            metadata = {
                "object": catalog.get("package_object"),
                "size": catalog.get("package_size"),
                "sha256": catalog.get("package_sha256"),
            }
        if not isinstance(metadata, dict):
            self._json({"error": "该共享素材没有可下载的分类包。"}, HTTPStatus.BAD_REQUEST)
            return
        object_key = str(metadata.get("object") or "")
        expected_sha256 = str(metadata.get("sha256") or "")
        target_root = (OUTPUT_ROOT / "reused" / product_key).resolve()
        filename = "complete-package.zip" if package_kind == "complete" else f"{package_kind}.zip"
        try:
            package_zip = client.download(
                object_key,
                int(metadata.get("size") or 0),
                expected_sha256,
                target_root / filename,
            )
            local_directory = target_root
            if package_kind == "complete":
                reused = materialize_reused_package(package_zip, target_root / "materialized")
                local_directory = reused.root
                load_shared_library_cache().record_download(
                    product_key,
                    object_key,
                    expected_sha256,
                    local_directory,
                )
        except SharedLibraryUnavailable:
            self._json({"error": "共享素材库暂时不可用。"}, HTTPStatus.SERVICE_UNAVAILABLE)
            return
        except Exception:
            self._json({"error": "共享素材包校验或解压失败。"}, HTTPStatus.BAD_REQUEST)
            return
        self._json(
            {
                "accepted": True,
                "product_key": product_key,
                "package_kind": package_kind,
                "file": str(package_zip),
                "local_directory": str(local_directory),
            }
        )

    @staticmethod
    def _cached_shared_catalog(product_key: str) -> dict | None:
        return next(
            (
                dict(item)
                for item in load_shared_library_cache().load_catalog()
                if str(item.get("product_key") or "") == product_key
            ),
            None,
        )

    def _export_single_workbook(self) -> None:
        with STATE.lock:
            if STATE.collecting or STATE.generating or STATE.batch_running:
                self._json({"error": "当前任务正在运行，请完成或停止后再导出表格。"}, HTTPStatus.BAD_REQUEST)
                return
            manifest = STATE.manifest_path
            reference_url = STATE.agent.reference_url or ""
            generated_output = STATE.generated_output

        if not manifest or not manifest.is_file():
            self._json({"error": "请先完成单个链接采集，再导出表格。"}, HTTPStatus.BAD_REQUEST)
            return
        if not reference_url:
            self._json({"error": "当前任务没有商品链接。"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            exported, records, recovered_output = export_single_product_workbook(
                manifest,
                reference_url,
                generated_output,
            )
            if recovered_output:
                with STATE.lock:
                    if STATE.manifest_path == manifest:
                        STATE.generated_output = recovered_output
                        STATE.results = STATE._records_to_results(records)
                        STATE.completed_task_signature = STATE.task_signature()
                        STATE.save_session()
        except (OSError, ValueError, TypeError, json.JSONDecodeError, RuntimeError) as error:
            self._json({"error": f"表格导出失败：{error}"}, HTTPStatus.BAD_REQUEST)
            return

        try:
            subprocess.Popen(["explorer.exe", str(Path(exported).parent)])
        except OSError:
            pass
        self._json({"accepted": True, "workbook": str(exported), "folder": str(Path(exported).parent)})

    def _start_collection(self) -> None:
        with STATE.lock:
            STATE.collection_paused = False
        error = self._begin_collection()
        if error:
            self._json({"error": error}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"accepted": True})

    def _begin_collection(self, selected_types: tuple[str, ...] | None = None) -> str | None:
        browser_choice = load_browser_choice()
        if not browser_choice:
            return "请先在右侧“采集浏览器”中选择挖象浏览器或微软 Edge。"
        if not load_browser_executable():
            return f"未检测到{browser_choice_label(browser_choice)}，请安装后重新选择。"
        with STATE.lock:
            if STATE.collecting:
                return "Collection is already running."
            if STATE.generating:
                return "Generation is already running."
            if STATE.batch_running:
                return "Batch workflow is already running."
            if STATE.agent.awaiting or not STATE.agent.reference_url or not STATE.agent.collection_types:
                return "Reference URL, quantity, and workflow selection are required before collection."
            if selected_types is None:
                selected_types = (
                    STATE.agent.collection_types
                    if not STATE.manifest_path
                    else STATE.missing_collection_workflows()
                )
            if not selected_types:
                return None
            reference_url = STATE.agent.reference_url
            max_main_images = STATE.agent.max_main_images
            existing_manifest = STATE.manifest_path
            eligible = shared_job_is_eligible(STATE.agent)
            prior_shared_lease = STATE.shared_lease

        if eligible:
            shared_error = self._prepare_shared_collection(reference_url)
            if shared_error:
                return shared_error

        with STATE.lock:
            busy_after_prepare = STATE.collecting or STATE.generating or STATE.batch_running
            release_new_shared_lease = (
                busy_after_prepare
                and STATE.shared_lease is not None
                and STATE.shared_lease != prior_shared_lease
            )
            if not busy_after_prepare:
                STATE.collecting = True
                STATE.collection_stop_requested = False
        if busy_after_prepare:
            if release_new_shared_lease:
                self._release_shared_session()
            return "Another workflow started before collection could begin."
        thread = threading.Thread(
            target=self._collect,
            args=(reference_url, max_main_images, selected_types, existing_manifest),
            daemon=True,
            name="collector",
        )
        try:
            thread.start()
        except Exception:
            with STATE.lock:
                STATE.collecting = False
                STATE.collection_stop_requested = False
            self._release_shared_session()
            raise
        return None

    def _prepare_shared_collection(self, reference_url: str) -> str | None:
        identity: ProductIdentity | None = None
        try:
            identity = resolve_shared_identity(reference_url)
            if identity is None:
                return None
            with STATE.lock:
                if (
                    STATE.shared_identity == identity
                    and STATE.shared_lease is not None
                    and STATE.shared_client is not None
                ):
                    return None
            client = load_shared_library_client()
            if client is None:
                self._set_shared_status(
                    "local_fallback",
                    identity.product_key,
                    "OSS 未配置，共享查询已转为本地执行。",
                )
                return None
            probe = client.probe(identity)
            if probe.status == "available":
                self._set_shared_status(
                    "available",
                    identity.product_key,
                    "已有共享素材，可直接复用。",
                    dict(probe.catalog or {}),
                )
                return "已有共享素材，可直接复用。"
            if probe.status == "locked":
                self._set_shared_status(
                    "locked",
                    identity.product_key,
                    "其他用户正在生成同一商品，请稍后重试。",
                )
                return "其他用户正在生成同一商品，请稍后重试。"
            if probe.status != "missing":
                self._set_shared_status(
                    "local_fallback",
                    identity.product_key,
                    "共享素材记录不可用，已转为本地执行。",
                )
                return None
            try:
                lease = client.acquire_lock(identity, uuid.uuid4().hex)
            except SharedLibraryLockBusy:
                self._set_shared_status(
                    "locked",
                    identity.product_key,
                    "其他用户正在生成同一商品，请稍后重试。",
                )
                return "其他用户正在生成同一商品，请稍后重试。"
            stop_event = threading.Event()
            with STATE.lock:
                STATE.shared_client = client
                STATE.shared_lease = lease
                STATE.shared_identity = identity
                STATE.shared_publish_allowed = True
                STATE.shared_heartbeat_stop = stop_event
                STATE.shared_library = {
                    "status": "generating",
                    "product_key": identity.product_key,
                    "message": "已取得共享任务锁，正在本地生成。",
                    "catalog": None,
                    "task_id": lease.task_id,
                    "expires_at": lease.expires_at,
                }
            heartbeat = threading.Thread(
                target=self._shared_lock_heartbeat,
                args=(client, lease, stop_event),
                daemon=True,
                name="shared-lock-heartbeat",
            )
            with STATE.lock:
                STATE.shared_heartbeat_thread = heartbeat
            try:
                heartbeat.start()
            except Exception:
                self._release_shared_session()
                raise
        except (ProductIdentityError, SharedLibraryUnavailable):
            self._set_shared_status(
                "local_fallback",
                identity.product_key if identity else "",
                "共享素材库暂时不可用，已转为本地执行。",
            )
        return None

    @staticmethod
    def _set_shared_status(
        status: str,
        product_key: str,
        message: str,
        catalog: dict | None = None,
    ) -> None:
        with STATE.lock:
            STATE.shared_library = {
                "status": status,
                "product_key": product_key,
                "message": message,
                "catalog": catalog,
            }

    def _shared_lock_heartbeat(
        self,
        client: SharedLibraryClient,
        lease: LockLease,
        stop_event: threading.Event,
    ) -> None:
        current = lease
        while not stop_event.wait(20 * 60):
            try:
                refreshed = client.refresh_lock(current)
            except Exception:
                with STATE.lock:
                    if STATE.shared_lease == current:
                        STATE.shared_publish_allowed = False
                        STATE.shared_library = {
                            **STATE.shared_library,
                            "status": "local_fallback",
                            "message": "共享任务锁续期失败，本次结果仅保存在本地。",
                        }
                return
            with STATE.lock:
                if STATE.shared_client is not client or STATE.shared_lease != current:
                    return
                STATE.shared_lease = refreshed
                STATE.shared_library = {
                    **STATE.shared_library,
                    "expires_at": refreshed.expires_at,
                }
            current = refreshed

    def _publish_single_shared_result(
        self,
        *,
        manifest: Path,
        output: Path,
        records: list[dict],
        workflows: tuple[str, ...],
        max_main_images: int | None,
        max_sku_images: int | None,
        max_detail_images: int | None,
        generation_mode: str,
    ) -> None:
        with STATE.lock:
            client = STATE.shared_client
            lease = STATE.shared_lease
            identity = STATE.shared_identity
            publish_allowed = STATE.shared_publish_allowed
            reference_url = STATE.agent.reference_url or ""
        if not client or not lease or not identity or not publish_allowed:
            return
        try:
            workbook, _, _ = export_single_product_workbook(
                manifest,
                reference_url,
                output,
                records,
            )
            titles: dict = {}
            titles_path = manifest.parent / "titles.json"
            if titles_path.is_file():
                loaded = json.loads(titles_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    titles = loaded
            package = SharedPackageBuilder(
                output / "shared-package",
                client.root_prefix,
                client.client_id,
            ).build(
                identity=identity,
                task_id=lease.task_id,
                source_manifest=manifest,
                generated_records=records,
                titles=titles,
                workbook_path=workbook,
                generation_mode=generation_mode,
                workflows=workflows,
                max_main_images=max_main_images,
                max_sku_images=max_sku_images,
                max_detail_images=max_detail_images,
            )
            if package is None:
                self._set_shared_status(
                    "local_only",
                    identity.product_key,
                    "当前任务不满足完整共享包标准，结果仅保存在本地。",
                )
                return
            catalog = client.publish(package.to_publish_bundle(), lease)
            self._set_shared_status(
                "published",
                identity.product_key,
                "完整结果已发布到共享素材库。",
                dict(catalog),
            )
        except Exception:
            self._set_shared_status(
                "local_fallback",
                identity.product_key,
                "共享发布失败，本次结果已保存在本地。",
            )
            STATE.log("共享素材发布失败，本次生成结果已保存在本地。")

    def _release_shared_session(self) -> None:
        with STATE.lock:
            client = STATE.shared_client
            lease = STATE.shared_lease
            stop_event = STATE.shared_heartbeat_stop
            heartbeat = STATE.shared_heartbeat_thread
            STATE.shared_client = None
            STATE.shared_lease = None
            STATE.shared_identity = None
            STATE.shared_publish_allowed = False
            STATE.shared_heartbeat_stop = None
            STATE.shared_heartbeat_thread = None
        if stop_event is not None:
            stop_event.set()
        if (
            heartbeat is not None
            and heartbeat is not threading.current_thread()
            and heartbeat.is_alive()
        ):
            heartbeat.join(timeout=2)
        if client is not None and lease is not None:
            try:
                client.release_lock(lease)
            except Exception:
                STATE.log("共享任务锁释放失败，将等待 OSS 租约自动过期。")

    def _maybe_auto_collect(self) -> None:
        with STATE.lock:
            selected_types = (
                STATE.agent.collection_types
                if not STATE.manifest_path
                else STATE.missing_collection_workflows()
            )
            ready = bool(
                not STATE.collecting
                and not STATE.generating
                and not STATE.batch_running
                and not STATE.collection_stop_requested
                and not STATE.collection_paused
                and not STATE.agent.awaiting
                and STATE.agent.reference_url
                and selected_types
            )
        if ready:
            error = self._begin_collection(selected_types)
            if error:
                with STATE.lock:
                    STATE.collection_paused = True
                STATE.log(f"Automatic collection did not start: {error}")

    def _collect(
        self,
        reference_url: str,
        max_main_images: int | None,
        selected_types: tuple[str, ...],
        existing_manifest: Path | None,
    ) -> None:
        process: subprocess.Popen[str] | None = None
        collection_completed = False
        try:
            cdp_url = allocate_collection_cdp_url()
            item_url = resolve_direct_item_url(reference_url)
            if FROZEN:
                collector = ROOT / "store_insight_collector.exe"
                if not collector.is_file():
                    raise RuntimeError("Missing bundled collector executable: store_insight_collector.exe")
                command = [
                    str(collector),
                    item_url,
                    "--output",
                    str(OUTPUT_ROOT / "store-insight"),
                    "--profile-dir",
                    str(COLLECTOR_PROFILE_ROOT),
                    "--cdp-url",
                    cdp_url,
                    "--auto-launch",
                ]
            else:
                interpreter = str(COLLECTOR_PYTHON) if COLLECTOR_PYTHON.is_file() else sys.executable
                command = [
                    interpreter,
                    str(ROOT / "store_insight_collector.py"),
                    item_url,
                    "--output",
                    str(OUTPUT_ROOT / "store-insight"),
                    "--profile-dir",
                    str(COLLECTOR_PROFILE_ROOT),
                    "--cdp-url",
                    cdp_url,
                    "--auto-launch",
                ]
            browser_executable = load_browser_executable()
            if browser_executable:
                command.extend(["--browser-executable", browser_executable])
            if max_main_images is not None:
                command.extend(["--max-main-images", str(max_main_images)])
            command.extend(["--types", *selected_types])
            STATE.log("开始调用店透视采集。需要登录时请在弹出的浏览器中完成登录。")
            process = subprocess.Popen(
                command,
                cwd=str(ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            with STATE.lock:
                STATE.collector_pid = process.pid
            manifest: Path | None = None
            assert process.stdout is not None
            for line in process.stdout:
                line = line.strip()
                if line:
                    STATE.log(line)
                if line.startswith("[collector] manifest: "):
                    manifest = Path(line.removeprefix("[collector] manifest: ").strip())
            if process.wait() != 0:
                with STATE.lock:
                    stopped = STATE.collection_stop_requested
                if stopped:
                    STATE.log("采集已由用户停止。")
                    return
                raise RuntimeError(f"采集脚本退出码为 {process.returncode}")
            if not manifest or not manifest.is_file():
                raise RuntimeError("采集完成后未找到 manifest.json")
            if existing_manifest:
                manifest = merge_collected_manifest(existing_manifest, manifest, selected_types)
            with STATE.lock:
                STATE.manifest_path = manifest
                STATE.collection_paused = False
                STATE.agent.mark_collected()
                STATE.reset_generation()
                STATE.save_session()
            collection_completed = True
            if STATE.agent.generation_enabled:
                STATE.log("采集完成。正在检查产品图与视觉 API Key，条件齐全后自动进入提示词分析和生图。")
            else:
                STATE.log("采集完成。已按对话指令停在采集阶段，不会自动生成图片。")
            self._maybe_auto_generate()
        except Exception as error:
            with STATE.lock:
                stopped = STATE.collection_stop_requested
                if not stopped:
                    STATE.collection_paused = True
                    STATE.save_session()
            if stopped:
                STATE.log("采集已由用户停止。")
            else:
                STATE.log(f"采集失败，已暂停自动重试：{error}")
        finally:
            with STATE.lock:
                if process is None or STATE.collector_pid == process.pid:
                    STATE.collecting = False
                    STATE.collector_pid = None
                    STATE.collection_stop_requested = False
            if not collection_completed:
                self._release_shared_session()

    def _upload_product_image(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json({"error": "请以图片文件上传。"}, HTTPStatus.BAD_REQUEST)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
        )
        item = form["image"] if "image" in form else None
        if item is None or not getattr(item, "file", None) or not item.filename:
            self._json({"error": "未收到产品图片。"}, HTTPStatus.BAD_REQUEST)
            return
        suffix = Path(item.filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
            self._json({"error": "只支持 JPG、PNG 或 WebP 产品图。"}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            if STATE.generating or STATE.batch_running:
                self._json({"error": "Generation is running. Wait for it to finish before replacing the product image."}, HTTPStatus.BAD_REQUEST)
                return
        target = OUTPUT_ROOT / "uploads" / f"{uuid.uuid4().hex}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(item.file.read())
        with STATE.lock:
            STATE.product_image = target
            STATE.reset_generation()
            STATE.save_session()
        STATE.log(f"已载入我方产品图：{item.filename}")
        self._maybe_auto_generate()
        self._json({"path": str(target)})

    def _upload_batch_workbook(self) -> None:
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._json({"error": "请上传 XLSX 表格。"}, HTTPStatus.BAD_REQUEST)
            return
        form = cgi.FieldStorage(
            fp=self.rfile,
            headers=self.headers,
            environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type},
        )
        item = form["workbook"] if "workbook" in form else None
        batch_mode = str(form.getfirst("batch_mode", "image_search")).strip().lower()
        if batch_mode not in {"image_search", "direct_link", "direct_replace"}:
            self._json({"error": "不支持的批处理模式。"}, HTTPStatus.BAD_REQUEST)
            return
        if item is None or not getattr(item, "file", None) or not item.filename:
            self._json({"error": "未收到 Excel 文件。"}, HTTPStatus.BAD_REQUEST)
            return
        if Path(item.filename).suffix.lower() != ".xlsx":
            self._json({"error": "当前只支持 XLSX 表格。"}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            busy = STATE.collecting or STATE.generating or STATE.batch_running
        if busy:
            self._json({"error": "当前有任务正在执行，请先停止或等待完成。"}, HTTPStatus.BAD_REQUEST)
            return

        upload_root = OUTPUT_ROOT / "batch-uploads" / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        upload_root.mkdir(parents=True, exist_ok=True)
        target = upload_root / f"{uuid.uuid4().hex}.xlsx"
        target.write_bytes(item.file.read())
        try:
            batch_output = OUTPUT_ROOT / "batches" / datetime.now().strftime("%Y%m%d-%H%M%S")
            if batch_mode == "direct_link":
                batch_items = extract_direct_link_items(target)
            elif batch_mode == "direct_replace":
                batch_items = extract_direct_replace_items(target, batch_output)
            else:
                batch_items = extract_batch_items(target, batch_output)
        except Exception as error:
            self._json({"error": f"Excel 解析失败：{error}"}, HTTPStatus.BAD_REQUEST)
            return
        if not batch_items:
            message = (
                "未找到商品链接列。请使用“商品链接”“对标链接”或“URL”列，或将链接放在第一列。"
                if batch_mode in {"direct_link", "direct_replace"}
                else "未找到可用商品图。请使用标题含“1688”和“商品图”的列，或提供仅包含商品图的表格。"
            )
            self._json(
                {"error": message},
                HTTPStatus.BAD_REQUEST,
            )
            return
        valid = sum(not getattr(batch_item, "validation_error", "") for batch_item in batch_items)
        invalid = sum(getattr(batch_item, "platform", "") == "invalid" for batch_item in batch_items)
        unsupported = sum(getattr(batch_item, "platform", "") == "unsupported" for batch_item in batch_items)
        validation_counts = summarize_batch_validation(batch_items)
        with STATE.lock:
            STATE.batch_input = target
            STATE.batch_input_name = str(item.filename)
            STATE.batch_output = batch_output
            STATE.batch_total = len(batch_items)
            STATE.batch_mode = batch_mode
            STATE.batch_valid = valid
            STATE.batch_invalid = invalid
            STATE.batch_unsupported = unsupported
            STATE.batch_missing_images = validation_counts["missing_images"]
            STATE.batch_missing_links = validation_counts["missing_links"]
            STATE.batch_pairing_conflicts = validation_counts["pairing_conflicts"]
            STATE.batch_events = []
            STATE.batch_results = []
            STATE.batch_stop_requested = False
            STATE.save_session()
        STATE.log(f"已载入批处理表格：{item.filename}，识别 {len(batch_items)} 行，有效 {valid} 行。")
        self._json(
            {
                "accepted": True,
                "count": len(batch_items),
                "valid": valid,
                "invalid": invalid,
                "unsupported": unsupported,
                **validation_counts,
                "status": STATE.status(),
            }
        )

    def _start_batch(self) -> None:
        body = self._json_body()
        run_mode = str(body.get("run_mode") or "full").strip().lower()
        if run_mode not in {"full", "collect_only"}:
            self._json({"error": "不支持的批处理运行方式。"}, HTTPStatus.BAD_REQUEST)
            return
        collect_only = run_mode == "collect_only"
        with STATE.lock:
            busy = STATE.collecting or STATE.generating or STATE.batch_running
            workbook_path = STATE.batch_input
            output = STATE.batch_output
            vision_key = STATE.vision_api_key
            image_key = STATE.image_api_key
            batch_mode = STATE.batch_mode
        if busy:
            self._json({"error": "当前有任务正在执行。"}, HTTPStatus.BAD_REQUEST)
            return
        if not workbook_path or not workbook_path.is_file() or not output:
            self._json({"error": "请先上传包含 E 列商品图的 XLSX 表格。"}, HTTPStatus.BAD_REQUEST)
            return
        settings = None
        if not collect_only:
            try:
                settings = load_api_settings(vision_key, image_key)
            except RuntimeError as config_error:
                self._json({"error": str(config_error)}, HTTPStatus.BAD_REQUEST)
                return
        resume_results = load_batch_results(workbook_path, output)
        oss_uploader = None
        shared_cache = None
        shared_library = None
        if not collect_only:
            oss_uploader, oss_message = load_optional_oss_uploader()
            if oss_message:
                STATE.log(oss_message)
            if batch_mode == "direct_link" and oss_uploader is not None:
                try:
                    shared_cache = load_shared_library_cache()
                    shared_library = SharedLibraryClient(
                        oss_uploader.config,
                        oss_uploader.bucket,
                        shared_cache.client_id,
                    )
                except Exception:
                    shared_cache = None
                    shared_library = None
                    STATE.log("共享素材库初始化失败，本批次将继续保存到本地。")
        runner = BatchRunner(
            settings,
            ROOT,
            COLLECTOR_PROFILE_ROOT,
            browser_executable=load_browser_executable(),
            max_main_images=DEFAULT_MAIN_IMAGES if batch_mode in {"direct_link", "direct_replace"} else STATE.agent.max_main_images,
            callback=self._on_batch_event,
            oss_uploader=oss_uploader,
            batch_mode=batch_mode,
            collect_only=collect_only,
            shared_library=shared_library,
            shared_cache=shared_cache,
        )
        with STATE.lock:
            STATE.batch_running = True
            STATE.batch_stop_requested = False
            STATE.batch_events = []
            STATE.batch_results = resume_results
            STATE.batch_run_mode = run_mode
            STATE.batch_runner = runner
            STATE.save_session()
        threading.Thread(
            target=self._run_batch,
            args=(runner, workbook_path, output),
            daemon=True,
            name="batch-workflow",
        ).start()
        action = "直接采集指定商品" if batch_mode in {"direct_link", "direct_replace"} else "淘宝搜同款"
        suffix = "，仅保存采集素材和数据，不执行生成。" if collect_only else "、生成和导出。"
        STATE.log(f"批处理已开始，将按 Excel 原行顺序逐个执行{action}{suffix}")
        self._json({"accepted": True, "status": STATE.status()})

    def _on_batch_event(self, event: dict) -> None:
        with STATE.lock:
            STATE.batch_events.append(dict(event))
            STATE.batch_events = STATE.batch_events[-200:]
            if event.get("stage") in {"collected", "completed", "failed", "stopped"}:
                sequence = int(event.get("sequence") or 0)
                STATE.batch_results = [
                    item for item in STATE.batch_results
                    if int(item.get("sequence") or 0) != sequence
                ]
                STATE.batch_results.append(dict(event))
        message = str(event.get("message") or "").strip()
        if not message:
            status = str(event.get("status") or "")
            stage_label = str(event.get("stage_label") or "").strip()
            if status in {"vision_preflight", "vision_preflight_ready", "identity_analyzing"}:
                suffix = f"（{event.get('source_count', 0)} 张素材）" if status == "identity_analyzing" else ""
                message = f"{stage_label}{suffix}"
            elif int(event.get("ordinal") or 0) > 0 and status in {"analyzing", "prompt_ready", "generating"}:
                message = f"{event.get('category')} #{event.get('ordinal')}：{stage_label}"
            elif status == "failed":
                failure_stage = str(event.get("failure_stage") or "生成流程")
                message = f"{event.get('category')} #{event.get('ordinal')}：{failure_stage}失败：{event.get('error', '')}"
        if message:
            STATE.log(f"批处理：{message}")

    def _run_batch(self, runner: BatchRunner, workbook_path: Path, output: Path) -> None:
        try:
            results = runner.run(workbook_path, output)
            with STATE.lock:
                STATE.batch_results = results
                STATE.save_session()
            completed = sum(item.get("status") == "completed" for item in results)
            collected = sum(item.get("status") == "collected" for item in results)
            failed = sum(item.get("status") == "failed" for item in results)
            if runner.collect_only:
                STATE.log(f"仅采集批处理结束：已采集 {collected}，失败 {failed}。")
            else:
                STATE.log(f"批处理结束：完成 {completed}，失败 {failed}。")
        except Exception as error:
            STATE.log(f"批处理失败：{error}")
        finally:
            with STATE.lock:
                STATE.batch_running = False
                STATE.batch_stop_requested = False
                STATE.batch_runner = None

    def _stop_batch(self) -> None:
        with STATE.lock:
            runner = STATE.batch_runner if STATE.batch_running else None
            if runner:
                STATE.batch_stop_requested = True
        if not runner:
            self._json({"error": "当前没有正在运行的批处理。"}, HTTPStatus.BAD_REQUEST)
            return
        runner.cancel()
        STATE.log("已提交停止批处理请求，当前采集或生成请求结束后不会继续下一行。")
        self._json({"accepted": True, "status": STATE.status()})

    def _start_batch_supplement(self) -> None:
        body = self._json_body()
        try:
            sequence = int(body.get("sequence") or 0)
            count = int(body.get("count") or 0)
        except (TypeError, ValueError):
            sequence = count = 0
        category = str(body.get("category") or "").strip().lower()
        all_categories = category == "all"
        with STATE.lock:
            selected_workbook = STATE.supplement_workbook
        if (not all_categories and (count < 1 or category not in {"main", "sku", "detail"})) or (selected_workbook is None and sequence < 1):
            self._json({"error": "请选择有效的商品序号、图片类型和补充数量。"}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            if STATE.supplement_running:
                self._json({"error": "当前有任务正在执行，请完成或停止后再补图。"}, HTTPStatus.BAD_REQUEST)
                return
            workbook_path = selected_workbook or STATE.batch_input
            output = STATE.batch_output
            batch_mode = STATE.batch_mode
        if not workbook_path or not workbook_path.is_file() or (selected_workbook is None and (not output or not output.is_dir())):
            self._json({"error": "请先载入包含该商品的批处理表格。"}, HTTPStatus.BAD_REQUEST)
            return
        if selected_workbook:
            try:
                context = resolve_supplement_workbook(selected_workbook, ROOT)
                if isinstance(context.item, DirectReplaceBatchItem):
                    batch_mode = "direct_replace"
                elif isinstance(context.item, DirectLinkBatchItem):
                    batch_mode = "direct_link"
                else:
                    batch_mode = "image_search"
            except BadZipFile:
                self._json({"error": "所选文件不是有效的 Excel 工作簿。"}, HTTPStatus.BAD_REQUEST)
                return
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
                self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
                return
        try:
            settings = load_api_settings(STATE.vision_api_key, STATE.image_api_key)
            uploader, warning = load_optional_oss_uploader()
        except RuntimeError as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        runner = BatchRunner(
            settings,
            ROOT,
            COLLECTOR_PROFILE_ROOT,
            browser_executable=load_browser_executable(),
            callback=self._on_supplement_event,
            oss_uploader=uploader,
            batch_mode=batch_mode,
        )
        with STATE.lock:
            STATE.supplement_running = True
            STATE.supplement_stop_requested = False
            STATE.supplement_runner = runner
            STATE.supplement_events = []
        if warning:
            STATE.log(warning)
        threading.Thread(
            target=self._run_batch_supplement,
            args=(runner, workbook_path, output, sequence, category, count, bool(selected_workbook)),
            daemon=True,
            name="batch-supplement",
        ).start()
        target_label = Path(workbook_path).name if selected_workbook else f"第 {sequence} 个商品"
        STATE.log(f"开始补充 {target_label} 的 {category} 图 {count} 张，优先填入原空缺位置。")
        self._json({"accepted": True, "status": STATE.status()})

    def _stop_batch_supplement(self) -> None:
        with STATE.lock:
            runner = STATE.supplement_runner if STATE.supplement_running else None
            if runner:
                STATE.supplement_stop_requested = True
        if runner is None:
            self._json({"error": "当前没有正在运行的补图任务。"}, HTTPStatus.BAD_REQUEST)
            return
        runner.cancel()
        STATE.log("已提交停止补图请求，已完成的图片会保留。")
        self._json({"accepted": True, "status": STATE.status()})

    def _select_supplement_workbook(self) -> None:
        with STATE.lock:
            busy = STATE.collecting or STATE.generating or STATE.batch_running
        if busy:
            self._json({"error": "当前有任务正在执行，请完成或停止后再选择补图表格。"}, HTTPStatus.BAD_REQUEST)
            return
        selected = choose_supplement_workbook()
        if selected is None:
            self._json({"error": "未选择表格。"}, HTTPStatus.BAD_REQUEST)
            return
        try:
            context = resolve_supplement_workbook(selected, ROOT)
        except BadZipFile:
            self._json({"error": "所选文件不是有效的 Excel 工作簿。"}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            STATE.supplement_workbook = selected
            STATE.save_session()
        self._json(
            {
                "accepted": True,
                "workbook": str(selected),
                "product_title": context.item.title,
                "status": STATE.status(),
            }
        )

    def _run_batch_supplement(
        self,
        runner: BatchRunner,
        workbook_path: Path,
        output: Path,
        sequence: int,
        category: str,
        count: int,
        selected_result_workbook: bool = False,
    ) -> None:
        try:
            if selected_result_workbook:
                result = runner.supplement_all_exported_workbook(workbook_path) if category == "all" else runner.supplement_exported_workbook(workbook_path, category, count)
            else:
                result = runner.supplement_all(workbook_path, output, sequence) if category == "all" else runner.supplement(workbook_path, output, sequence, category, count)
            if selected_result_workbook:
                STATE.log(f"补图完成：新增成功 {result['supplemented']} 张，已更新 {Path(result['workbook']).name}。")
            else:
                STATE.log(f"补图完成：新增成功 {result['supplemented']} 张，失败 {result.get('failed', 0)} 张。")
            with STATE.lock:
                STATE.supplement_events.append(result)
        except Exception as error:
            STATE.log(f"补图失败：{error}")
        finally:
            with STATE.lock:
                STATE.supplement_running = False
                STATE.supplement_stop_requested = False
                STATE.supplement_runner = None

    def _on_supplement_event(self, event: dict) -> None:
        with STATE.lock:
            STATE.supplement_events.append(dict(event))
            STATE.supplement_events = STATE.supplement_events[-200:]
        if int(event.get("ordinal") or 0) > 0:
            status = str(event.get("status") or "")
            if status in {"analyzing", "prompt_ready", "generating", "failed"}:
                STATE.log(
                    f"补图：{event.get('category')} #{event.get('ordinal')} "
                    f"{event.get('stage_label') or status}"
                )

    def _shutdown_application(self) -> None:
        with STATE.lock:
            collector_pid = STATE.collector_pid if STATE.collecting else None
            generation_runner = STATE.runner if STATE.generating else None
            batch_runner = STATE.batch_runner if STATE.batch_running else None
            supplement_runner = STATE.supplement_runner if STATE.supplement_running else None
            STATE.collection_control_version += 1
            STATE.collection_stop_requested = bool(collector_pid)
            STATE.collection_paused = True
            STATE.batch_stop_requested = bool(batch_runner)
        if collector_pid:
            subprocess.run(["taskkill", "/PID", str(collector_pid), "/T", "/F"], check=False, capture_output=True)
        if generation_runner:
            generation_runner.cancel()
        if batch_runner:
            batch_runner.cancel()
        if supplement_runner:
            supplement_runner.cancel()
        self._release_shared_session()
        close_project_collection_browser()
        STATE.log("正在退出本地服务。")
        self._json({"accepted": True})
        threading.Timer(0.2, self.server.shutdown).start()

    def _set_vision_api_key(self) -> None:
        body = self._json_body()
        vision_api_key = str(body.get("vision_api_key", "")).strip()
        with STATE.lock:
            STATE.vision_api_key = vision_api_key
            STATE.vision_api_error = ""
        self._json({"accepted": True, "ready": bool(vision_api_key)})

    def _set_api_keys(self) -> None:
        body = self._json_body()
        vision_api_key = str(body.get("vision_api_key", "")).strip()
        image_api_key = str(body.get("image_api_key", "")).strip()
        if not vision_api_key or not image_api_key:
            self._json(
                {"error": "视觉模型和生图模型 API Key 都不能为空。"},
                HTTPStatus.BAD_REQUEST,
            )
            return
        try:
            save_model_api_keys(vision_api_key, image_api_key)
        except (OSError, RuntimeError) as error:
            self._json({"error": str(error)}, HTTPStatus.BAD_REQUEST)
            return
        with STATE.lock:
            STATE.vision_api_key = vision_api_key
            STATE.image_api_key = image_api_key
            STATE.vision_api_error = ""
        self._json(
            {
                "accepted": True,
                "vision_ready": bool(vision_api_key),
                "image_ready": bool(image_api_key),
                "ready": bool(vision_api_key and image_api_key),
            }
        )

    def _start_generation(self) -> None:
        body = self._json_body()
        vision_api_key = str(body.get("vision_api_key", "")).strip()
        image_api_key = str(body.get("image_api_key", "")).strip()
        if vision_api_key:
            with STATE.lock:
                STATE.vision_api_key = vision_api_key
        if image_api_key:
            with STATE.lock:
                STATE.image_api_key = image_api_key
        error = self._begin_generation(force=True)
        if error:
            self._json({"error": error}, HTTPStatus.BAD_REQUEST)
            return
        self._json({"accepted": True})

    def _maybe_auto_generate(self) -> None:
        with STATE.lock:
            ready = bool(
                STATE.manifest_path
                and STATE.agent.workflows
                and STATE.agent.generation_enabled
                and not STATE.missing_workflows()
                and STATE.runnable_workflows()
                and STATE.task_signature()
                and STATE.vision_api_key
                and STATE.image_api_key
                and not STATE.vision_api_error
                and not STATE.generating
                and not STATE.batch_running
                and STATE.completed_task_signature != STATE.task_signature()
            )
        if ready:
            error = self._begin_generation()
            if error:
                STATE.log(f"Automatic generation did not start: {error}")

    def _begin_generation(self, force: bool = False) -> str | None:
        with STATE.lock:
            if STATE.generating:
                return "Generation is already running."
            if STATE.batch_running:
                return "Batch workflow is already running."
            if not STATE.manifest_path:
                return "Collection is required before generation."
            if not STATE.agent.workflows:
                return "A workflow must be selected before generation."
            missing_workflows = STATE.missing_workflows()
            if missing_workflows:
                return f"Missing collected images for: {', '.join(missing_workflows)}."
            runnable_workflows = STATE.runnable_workflows()
            if not runnable_workflows:
                return "当前商品未采集到所选类型的可用图片，无法开始生成。"
            if STATE.vision_api_error:
                return STATE.vision_api_error
            task_signature = STATE.task_signature()
            if not task_signature:
                try:
                    resolve_identity_image(
                        STATE.manifest_path,
                        STATE.product_image,
                        STATE.agent.generation_mode,
                    )
                except (OSError, ValueError, json.JSONDecodeError) as error:
                    return str(error)
                return "Current task inputs are incomplete."
            if not force and STATE.completed_task_signature == task_signature:
                return None
            manifest = STATE.manifest_path
            product = resolve_identity_image(manifest, STATE.product_image, STATE.agent.generation_mode)
            workflows = runnable_workflows
            max_main_images = STATE.agent.max_main_images
            max_sku_images = STATE.agent.max_sku_images
            max_detail_images = STATE.agent.max_detail_images
            generation_mode = STATE.agent.generation_mode
            vision_api_key = STATE.vision_api_key
            image_api_key = STATE.image_api_key
            previous_output = STATE.generated_output
            previous_records: list[dict] = []
            requested_ordinals: dict[str, list[int]] | None = None
            if previous_output and (previous_output / "analysis.json").is_file():
                try:
                    previous_document = json.loads(
                        (previous_output / "analysis.json").read_text(encoding="utf-8")
                    )
                    previous_records = [
                        record
                        for record in previous_document.get("records", [])
                        if isinstance(record, dict)
                    ]
                except (OSError, json.JSONDecodeError):
                    previous_records = []
                if previous_records:
                    missing_ordinals = plan_missing_generation_ordinals(
                        manifest,
                        tuple(workflows),
                        max_main_images,
                        max_sku_images,
                        max_detail_images,
                        previous_records,
                    )
                    if missing_ordinals:
                        requested_ordinals = missing_ordinals
                    elif not force:
                        STATE.completed_task_signature = task_signature
                        STATE.save_session()
                        return None
            retrying = bool(requested_ordinals)
            STATE.reset_generation()
            STATE.save_session()
            STATE.generating = True
        try:
            settings = load_api_settings(vision_api_key, image_api_key)
        except RuntimeError as error:
            with STATE.lock:
                STATE.generating = False
            self._release_shared_session()
            return str(error)
        concurrency = None
        output = (
            previous_output
            if retrying and previous_output
            else OUTPUT_ROOT / "generated" / datetime.now().strftime("%Y%m%d-%H%M%S")
        )
        with STATE.lock:
            STATE.generated_output = output
            STATE.save_session()
        thread = threading.Thread(
            target=self._generate,
            args=(
                settings,
                manifest,
                product,
                output,
                concurrency,
                workflows,
                max_main_images,
                max_sku_images,
                max_detail_images,
                task_signature,
                generation_mode,
                requested_ordinals,
                previous_records if retrying else None,
            ),
            daemon=True,
            name="generator",
        )
        thread.start()
        return None

    @staticmethod
    def _default_vision_key_available() -> bool:
        try:
            return bool(load_default_vision_api_key())
        except RuntimeError:
            return False

    def _generate(
        self,
        settings,
        manifest,
        product,
        output,
        concurrency,
        workflows,
        max_main_images,
        max_sku_images,
        max_detail_images,
        task_signature,
        generation_mode,
        requested_ordinals: dict[str, list[int]] | None = None,
        existing_records: list[dict] | None = None,
    ) -> None:
        def on_event(event: dict) -> None:
            with STATE.lock:
                STATE.events.append(event)
            status = event["status"]
            if status == "vision_preflight":
                STATE.log("正在执行视觉接口预检")
            elif status == "vision_preflight_ready":
                STATE.log("视觉接口预检完成")
            elif status == "identity_analyzing":
                STATE.log(f"正在建立多视角商品档案（{event.get('source_count', 0)} 张素材）")
            elif status == "detail_dossier_ready":
                STATE.log("多视角商品档案完成，正在生成不同角度详情图。")
            elif status == "detail_dossier_failed":
                selected_labels = {"main": "主图", "sku": "SKU 图", "detail": "详情图"}
                selected = "、".join(selected_labels[name] for name in workflows if name in selected_labels)
                STATE.log(
                    f"详情图多视角分析失败，将退回逐张视觉分析；所选工作流 {selected} 继续运行。"
                )
            elif event.get("ordinal", 0) > 0:
                stage_label = str(event.get("stage_label") or status)
                if status == "failed":
                    stage_label = f"{event.get('failure_stage', '生成流程')}失败：{event.get('error', '')}"
                STATE.log(f"{event['category']} #{event['ordinal']}: {stage_label}")

        try:
            runner = WorkflowRunner(settings, callback=on_event)
            with STATE.lock:
                STATE.runner = runner
            runner_options = {}
            if requested_ordinals is not None:
                runner_options["requested_ordinals"] = requested_ordinals
            if existing_records is not None:
                runner_options["existing_records"] = existing_records
            added_records = runner.run(
                manifest,
                product,
                output,
                concurrency,
                workflows,
                max_main_images,
                max_sku_images,
                max_detail_images,
                generation_mode=generation_mode,
                identity_image=product,
                **runner_options,
            )
            merged_records: dict[tuple[str, int], dict] = {}
            for record in [*(existing_records or []), *added_records]:
                key = (str(record.get("category") or ""), int(record.get("ordinal") or 0))
                if key[0] and key[1] > 0:
                    merged_records[key] = dict(record)
            records = list(merged_records.values()) if existing_records is not None else added_records
            oss_uploader, oss_message = load_optional_oss_uploader()
            if oss_message:
                STATE.log(oss_message)
            manifest_document = json.loads(Path(manifest).read_text(encoding="utf-8"))
            manifest_document = upload_video_if_needed(
                manifest_document,
                oss_uploader,
                Path(output).name,
            )
            Path(manifest).write_text(
                json.dumps(manifest_document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            if manifest_document.get("main_video_local_path") and manifest_document.get("main_video_status") != "complete":
                STATE.log(str(manifest_document.get("main_video_error") or "主视频未生成公网 URL"))
            records = upload_generation_records(records, oss_uploader)
            for record in records:
                if record.get("oss_upload_error"):
                    STATE.log(str(record["oss_upload_error"]))
            completed = sum(record["status"] == "completed" for record in records)
            failed = sum(record["status"] == "failed" for record in records)
            expected_tasks = load_manifest_tasks(
                Path(manifest),
                tuple(workflows),
                max_main_images,
                max_sku_images,
                max_detail_images,
            )
            expected_keys = {(task.category, task.ordinal) for task in expected_tasks}
            valid_keys = {
                (str(record.get("category") or ""), int(record.get("ordinal") or 0))
                for record in records
                if record.get("status") == "completed"
                and record.get("output_path")
                and Path(str(record["output_path"])).is_file()
            }
            task_complete = bool(expected_keys) and expected_keys.issubset(valid_keys)
            missing_count = len(expected_keys - valid_keys)
            if completed == 0:
                STATE.log(f"生成失败：没有生成任何有效图片，失败 {failed}。可直接重试当前任务。")
            elif not task_complete:
                STATE.log(
                    f"生成未完成：成功 {completed}，失败 {failed}，仍缺少 {missing_count} 张。"
                    "已完成图片会保留，重试时只需补齐缺失位置。"
                )
            else:
                STATE.log(f"生成结束：完成 {completed}，失败 {failed}。输出目录：{output}")
            if task_complete:
                self._publish_single_shared_result(
                    manifest=Path(manifest),
                    output=Path(output),
                    records=records,
                    workflows=tuple(workflows),
                    max_main_images=max_main_images,
                    max_sku_images=max_sku_images,
                    max_detail_images=max_detail_images,
                    generation_mode=generation_mode,
                )
            with STATE.lock:
                if STATE.task_signature() == task_signature:
                    STATE.results = STATE._records_to_results(records)
                    STATE.completed_task_signature = task_signature if task_complete else None
                    STATE.save_session()
        except Exception as error:
            message = self._friendly_generation_error(str(error))
            with STATE.lock:
                STATE.vision_api_error = message if self._is_vision_key_error(str(error)) else STATE.vision_api_error
            STATE.log(f"生成失败：{message}")
        finally:
            self._release_shared_session()
            with STATE.lock:
                STATE.generating = False
                STATE.runner = None

    @staticmethod
    def _is_vision_key_error(message: str) -> bool:
        lower = message.lower()
        return "http 401" in lower or "invalid token" in lower

    @staticmethod
    def _friendly_generation_error(message: str) -> str:
        lower = message.lower()
        if "http 401" in lower and "invalid token" in lower:
            return "视觉 API Key 无效，请更换具备 gpt-5.6-sol 视觉权限的 Key。"
        if "model_not_found" in lower and "gpt-5.6-sol" in lower:
            return "当前中转站没有 gpt-5.6-sol 视觉通道，请切换到支持该模型的线路。"
        return message

    def _json_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return {}

    def _json(self, body: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        return


class ExclusiveThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False

    def server_bind(self) -> None:
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def create_local_server(preferred_port: int) -> tuple[ThreadingHTTPServer, int]:
    last_error: OSError | None = None
    for port in range(preferred_port, min(preferred_port + 20, 65536)):
        try:
            return ExclusiveThreadingHTTPServer(("127.0.0.1", port), RequestHandler), port
        except OSError as error:
            address_in_use = error.errno in {48, 98} or getattr(error, "winerror", None) == 10048
            if not address_in_use:
                raise
            last_error = error
    raise RuntimeError(f"No available local port from {preferred_port} to {min(preferred_port + 19, 65535)}") from last_error


def report_startup_error(error: Exception) -> None:
    message = f"程序启动失败：{error}\n\n请确认压缩包已完整解压，并检查 Windows 安全中心是否隔离了程序文件。"
    STARTUP_ERROR_PATH.write_text(message + "\n", encoding="utf-8")
    if FROZEN and os.name == "nt":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "商品图片工作流", 0x10)
        except Exception:
            pass


def open_local_interface(address: str) -> None:
    if os.name == "nt":
        os.startfile(address)
        return
    if not webbrowser.open(address):
        raise RuntimeError("Could not open the default browser")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local product workflow interface.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    try:
        server, port = create_local_server(args.port)
    except Exception as error:
        report_startup_error(error)
        raise
    STARTUP_ERROR_PATH.unlink(missing_ok=True)
    address = f"http://127.0.0.1:{port}"
    STARTUP_URL_PATH.write_text(address, encoding="utf-8")
    print(f"Local interface: {address}")
    if not args.no_browser:
        try:
            open_local_interface(address)
        except Exception as error:
            server.server_close()
            report_startup_error(error)
            raise
    server.serve_forever()


if __name__ == "__main__":
    main()
