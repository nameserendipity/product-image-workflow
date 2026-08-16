from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Sequence

from PIL import Image, ImageOps

from agent_flow import DEFAULT_MAIN_IMAGES
from product_identity import ProductIdentity
from shared_library_client import SharedPublishBundle


@dataclass(frozen=True, slots=True)
class SharedPackage:
    identity: ProductIdentity
    task_id: str
    root: Path
    files: dict[str, Path]
    manifest: dict[str, Any]
    catalog: dict[str, Any]

    def to_publish_bundle(self) -> SharedPublishBundle:
        return SharedPublishBundle(
            identity=self.identity,
            task_id=self.task_id,
            files=dict(self.files),
            manifest=dict(self.manifest),
            catalog=dict(self.catalog),
        )


@dataclass(frozen=True, slots=True)
class ReusedPackage:
    root: Path
    source_manifest: Path
    generated_records: list[dict[str, Any]]
    titles: dict[str, Any]
    workbook: Path


class SharedPackageBuilder:
    def __init__(
        self,
        work_root: Path,
        shared_root_prefix: str,
        client_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.work_root = work_root.resolve()
        self.shared_root_prefix = shared_root_prefix.strip().strip("/")
        self.client_id = client_id.strip()
        self._now = now or (lambda: datetime.now(timezone.utc))

    def build(
        self,
        *,
        identity: ProductIdentity,
        task_id: str,
        source_manifest: Path,
        generated_records: Sequence[Mapping[str, Any]],
        titles: Mapping[str, Any],
        workbook_path: Path,
        generation_mode: str,
        workflows: Sequence[str],
        max_main_images: int | None,
        max_sku_images: int | None,
        max_detail_images: int | None,
    ) -> SharedPackage | None:
        counts = _complete_counts(generated_records)
        if (
            counts is None
            or generation_mode != "competitor_reference"
            or set(workflows) != {"main", "sku", "detail"}
            or max_main_images != DEFAULT_MAIN_IMAGES
            or max_sku_images is not None
            or max_detail_images is not None
            or not source_manifest.is_file()
            or not workbook_path.is_file()
        ):
            return None

        package_root = self.work_root / task_id
        package_root.mkdir(parents=True, exist_ok=True)
        records = [dict(record) for record in generated_records]
        category_members = _category_members(records)

        files: dict[str, Path] = {}
        for category in ("main", "sku", "detail"):
            archive = package_root / f"{category}.zip"
            _write_category_zip(archive, category_members[category])
            files[archive.name] = archive

        preview = package_root / "preview.jpg"
        _write_preview(preview, [source for source, _ in category_members["main"]])
        files[preview.name] = preview

        copied_workbook = package_root / "result.xlsx"
        shutil.copy2(workbook_path, copied_workbook)
        files[copied_workbook.name] = copied_workbook

        complete_package = package_root / "complete-package.zip"
        reuse_document = {
            "schema_version": 1,
            "product_key": identity.product_key,
            "generated_records": [
                {
                    **record,
                    "output_path": archive_name,
                }
                for record, (_, archive_name) in zip(records, _ordered_members(records))
            ],
            "titles": dict(titles),
            "source_manifest": "source/manifest.json",
            "workbook": "result.xlsx",
        }
        _write_complete_zip(
            complete_package,
            records,
            source_manifest,
            titles,
            copied_workbook,
            reuse_document,
        )
        files[complete_package.name] = complete_package

        formal_root = (
            f"{self.shared_root_prefix}/packages/{identity.platform}/{identity.product_id}"
        )
        created_at = _aware(self._now()).isoformat()
        package_sha256 = _sha256(complete_package)
        manifest = {
            "schema_version": 1,
            "product_key": identity.product_key,
            "platform": identity.platform,
            "product_id": identity.product_id,
            "source_url": identity.source_url,
            "canonical_url": identity.canonical_url,
            "status": "completed",
            "counts": counts,
            "objects": {
                name: {
                    "object": f"{formal_root}/{name}",
                    "size": path.stat().st_size,
                    "sha256": _sha256(path),
                }
                for name, path in files.items()
            },
            "created_at": created_at,
            "created_by": self.client_id,
        }
        manifest_object = f"{formal_root}/manifest.json"
        catalog = {
            "schema_version": 1,
            "product_key": identity.product_key,
            "platform": identity.platform,
            "product_id": identity.product_id,
            "source_url": identity.source_url,
            "canonical_url": identity.canonical_url,
            "status": "completed",
            "preview_object": f"{formal_root}/preview.jpg",
            "package_object": f"{formal_root}/complete-package.zip",
            "manifest_object": manifest_object,
            "main_count": counts["main"],
            "sku_count": counts["sku"],
            "detail_count": counts["detail"],
            "package_size": complete_package.stat().st_size,
            "package_sha256": package_sha256,
            "downloads": {
                kind: dict(manifest["objects"][filename])
                for kind, filename in (
                    ("complete", "complete-package.zip"),
                    ("main", "main.zip"),
                    ("sku", "sku.zip"),
                    ("detail", "detail.zip"),
                )
            },
            "created_at": created_at,
            "created_by": self.client_id,
        }
        return SharedPackage(identity, task_id, package_root, files, manifest, catalog)


def _complete_counts(records: Sequence[Mapping[str, Any]]) -> dict[str, int] | None:
    if not records or any(record.get("status") != "completed" for record in records):
        return None
    for record in records:
        if not Path(str(record.get("output_path") or "")).is_file():
            return None
    counts = Counter(str(record.get("category") or "") for record in records)
    if (
        counts["main"] != 10
        or not 3 <= counts["sku"] <= 8
        or not 6 <= counts["detail"] <= 15
        or sum(counts.values()) != len(records)
    ):
        return None
    return {category: counts[category] for category in ("main", "sku", "detail")}


def _category_members(records: Sequence[Mapping[str, Any]]) -> dict[str, list[tuple[Path, str]]]:
    members: dict[str, list[tuple[Path, str]]] = {"main": [], "sku": [], "detail": []}
    for record in records:
        category = str(record["category"])
        source = Path(str(record["output_path"])).resolve()
        ordinal = int(record.get("ordinal") or len(members[category]) + 1)
        name = f"generated/{category}/{ordinal:03d}-{_safe_name(source.name)}"
        members[category].append((source, name))
    return members


def _ordered_members(records: Sequence[Mapping[str, Any]]) -> list[tuple[Path, str]]:
    by_category = _category_members(records)
    positions = {category: 0 for category in by_category}
    ordered: list[tuple[Path, str]] = []
    for record in records:
        category = str(record["category"])
        ordered.append(by_category[category][positions[category]])
        positions[category] += 1
    return ordered


def _write_category_zip(path: Path, members: Sequence[tuple[Path, str]]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in members:
            archive.write(source, Path(name).name)


def _write_complete_zip(
    path: Path,
    records: Sequence[Mapping[str, Any]],
    source_manifest: Path,
    titles: Mapping[str, Any],
    workbook: Path,
    reuse_document: Mapping[str, Any],
) -> None:
    portable_source, source_assets = _portable_source_manifest(source_manifest)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, name in _ordered_members(records):
            archive.write(source, name)
        for source, name in source_assets:
            archive.write(source, name)
        archive.writestr(
            "source/manifest.json",
            json.dumps(portable_source, ensure_ascii=False, indent=2),
        )
        archive.writestr(
            "source/titles.json",
            json.dumps(dict(titles), ensure_ascii=False, indent=2),
        )
        archive.write(workbook, "result.xlsx")
        archive.writestr(
            "reuse-manifest.json",
            json.dumps(dict(reuse_document), ensure_ascii=False, indent=2),
        )


def materialize_reused_package(package_zip: Path, destination: Path) -> ReusedPackage:
    archive_path = package_zip.resolve()
    if not archive_path.is_file():
        raise ValueError("共享素材包不存在")
    root = destination.resolve()
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            _safe_archive_target(root, info.filename)
        archive.extractall(root)

    reuse_path = root / "reuse-manifest.json"
    source_manifest = root / "source" / "manifest.json"
    if not reuse_path.is_file() or not source_manifest.is_file():
        raise ValueError("共享素材包缺少复用清单")
    reuse = json.loads(reuse_path.read_text(encoding="utf-8"))
    source = json.loads(source_manifest.read_text(encoding="utf-8"))
    if not isinstance(reuse, dict) or not isinstance(source, dict):
        raise ValueError("共享素材包复用清单无效")

    for image in source.get("images", []):
        if not isinstance(image, dict) or not image.get("path"):
            continue
        image["path"] = str(_resolve_member(source_manifest.parent, str(image["path"]), root))
    local_video = str(source.get("main_video_local_path") or "").strip()
    if local_video:
        source["main_video_local_path"] = str(
            _resolve_member(source_manifest.parent, local_video, root)
        )
    source_manifest.write_text(
        json.dumps(source, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    generated_records: list[dict[str, Any]] = []
    raw_records = reuse.get("generated_records", [])
    if not isinstance(raw_records, list):
        raise ValueError("共享素材包生成记录无效")
    for raw in raw_records:
        if not isinstance(raw, dict):
            raise ValueError("共享素材包生成记录无效")
        record = dict(raw)
        record["output_path"] = str(_resolve_member(root, str(record.get("output_path") or ""), root))
        generated_records.append(record)

    titles = reuse.get("titles")
    if not isinstance(titles, dict):
        raise ValueError("共享素材包标题记录无效")
    workbook = _resolve_member(root, str(reuse.get("workbook") or ""), root)
    return ReusedPackage(root, source_manifest, generated_records, dict(titles), workbook)


def _portable_source_manifest(source_manifest: Path) -> tuple[dict[str, Any], list[tuple[Path, str]]]:
    document = json.loads(source_manifest.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("采集 Manifest 无效")
    portable = json.loads(json.dumps(document, ensure_ascii=False))
    assets: list[tuple[Path, str]] = []
    counters: Counter[str] = Counter()
    portable_images: list[dict[str, Any]] = []
    for image in portable.get("images", []):
        if not isinstance(image, dict):
            continue
        source = _manifest_source_path(source_manifest, str(image.get("path") or ""))
        if not source.is_file():
            continue
        category = str(image.get("type") or "other")
        counters[category] += 1
        relative = f"assets/{category}/{counters[category]:03d}-{_safe_name(source.name)}"
        image["path"] = relative
        portable_images.append(image)
        assets.append((source, f"source/{relative}"))
    portable["images"] = portable_images
    local_video = str(portable.get("main_video_local_path") or "").strip()
    if local_video:
        source = _manifest_source_path(source_manifest, local_video)
        if source.is_file():
            relative = f"assets/video/main{source.suffix.lower() or '.mp4'}"
            portable["main_video_local_path"] = relative
            assets.append((source, f"source/{relative}"))
    return portable, assets


def _manifest_source_path(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (manifest_path.parent / path).resolve()


def _safe_archive_target(root: Path, name: str) -> Path:
    if "\\" in name:
        raise ValueError("共享素材包包含不安全路径")
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("共享素材包包含不安全路径")
    target = (root / Path(*relative.parts)).resolve()
    if target != root and root not in target.parents:
        raise ValueError("共享素材包包含不安全路径")
    return target


def _resolve_member(base: Path, value: str, root: Path) -> Path:
    if not value:
        raise ValueError("共享素材包引用路径为空")
    relative = PurePosixPath(value.replace("\\", "/"))
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("共享素材包包含不安全引用")
    target = (base / Path(*relative.parts)).resolve()
    if root != target and root not in target.parents:
        raise ValueError("共享素材包包含不安全引用")
    if not target.is_file():
        raise ValueError("共享素材包引用文件不存在")
    return target


def _write_preview(path: Path, images: Sequence[Path]) -> None:
    selected = list(images[:4])
    if not selected:
        raise ValueError("完整共享任务缺少主图预览")
    canvas = Image.new("RGB", (1200, 1200), "white")
    positions = ((0, 0), (600, 0), (0, 600), (600, 600))
    for source, position in zip(selected, positions):
        with Image.open(source) as image:
            tile = ImageOps.fit(image.convert("RGB"), (600, 600), Image.Resampling.LANCZOS)
            canvas.paste(tile, position)
    for quality in (88, 80, 72, 64, 56):
        canvas.save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= 800 * 1024:
            break


def _safe_name(value: str) -> str:
    cleaned = Path(value).name.replace("..", "_")
    return cleaned or "asset.bin"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
