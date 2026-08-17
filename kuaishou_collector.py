"""Conservative helpers for public Kuaishou product-page assets."""

from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from pathlib import Path
import re
import socket
from typing import Iterable
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from PIL import Image


TRUSTED_ASSET_DOMAINS = ("ecukwai.com",)
MAX_ASSET_BYTES = 100 * 1024 * 1024
IDENTITY_KEYS = {"goodsid", "productid", "itemid"}
EXCLUDED_PRODUCT_SUBTREES = ("sku", "spec", "variant", "recommend", "advert", "similar")


@dataclass(frozen=True)
class Asset:
    url: str
    filename: str
    category: str


def _trusted_asset_host(host: str) -> bool:
    normalized = host.lower().strip(".")
    return any(normalized == domain or normalized.endswith(f".{domain}") for domain in TRUSTED_ASSET_DOMAINS)


def is_safe_asset_url(url: str, resolve_dns: bool = False) -> bool:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not _trusted_asset_host(host):
        return False
    if parsed.username or parsed.password:
        return False
    if not resolve_dns:
        return True
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False
    resolved = {entry[4][0] for entry in addresses if entry[4]}
    return bool(resolved) and all(ipaddress.ip_address(address).is_global for address in resolved)


def classify_image_url(url: str) -> str | None:
    if not is_safe_asset_url(url):
        return None
    upper_url = url.upper()
    if "ITEM_DETAIL_IMAGE" in upper_url:
        return "detail"
    if "ITEM_IMAGE" in upper_url:
        return "main"
    return None


def dedupe_urls(urls: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if not url:
            continue
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.endswith("ecukwai.com") and "/image-kwaishop-product/" in parsed.path:
            key = f"kuaishou-image:{parsed.path}"
        else:
            key = url
        if key in seen:
            continue
        seen.add(key)
        result.append(url)
    return result


def _video_url(url: str) -> bool:
    return is_safe_asset_url(url) and bool(re.search(r"\.(?:mp4|m3u8)(?:$|[?#])", url, re.IGNORECASE))


def product_media_token(url: str) -> str:
    match = re.search(r"ITEM(?:_DETAIL)?_IMAGE-([^-/?]+)-", url, re.IGNORECASE)
    return match.group(1).lower() if match else ""


def _price_value(value: str) -> str | None:
    match = re.search(r"(?:¥|￥)?\s*([0-9]+(?:\.[0-9]+)?)", value)
    return match.group(1) if match else None


def extract_product_payload(
    payload: object,
    base_url: str = "",
    product_id: str = "",
) -> dict[str, object]:
    main_urls: list[str] = []
    detail_urls: list[str] = []
    video_urls: list[str] = []
    title: str | None = None
    price: str | None = None
    title_keys = {"title", "goodstitle", "producttitle", "itemtitle"}
    price_keys = {"price", "saleprice", "discountprice", "activityprice", "minprice"}

    def visit(value: object, key: str = "", identity_matched: bool = False) -> None:
        nonlocal title, price
        normalized_key = key.replace("_", "").replace("-", "").lower()
        if isinstance(value, str):
            if product_id and not identity_matched:
                return
            candidate = urljoin(base_url, value) if base_url and value.startswith(("/", "//")) else value
            category = classify_image_url(candidate)
            if category == "main":
                main_urls.append(candidate)
            elif category == "detail":
                detail_urls.append(candidate)
            if _video_url(candidate) or any(marker in normalized_key for marker in ("videourl", "playurl", "videoplay")):
                if candidate.startswith(("http://", "https://")):
                    video_urls.append(candidate)
            if title is None and normalized_key in title_keys and 2 <= len(value.strip()) <= 200:
                title = value.strip()
            if price is None and normalized_key in price_keys:
                price = _price_value(value)
            return
        if isinstance(value, dict):
            identifiers = {
                str(child_value).strip()
                for child_key, child_value in value.items()
                if str(child_key).replace("_", "").replace("-", "").lower() in IDENTITY_KEYS
                and isinstance(child_value, (str, int))
            }
            if product_id and identifiers and product_id not in identifiers:
                return
            current_identity_matched = identity_matched or bool(product_id and product_id in identifiers)
            for child_key, child_value in value.items():
                normalized_child_key = str(child_key).replace("_", "").replace("-", "").lower()
                if current_identity_matched and any(marker in normalized_child_key for marker in EXCLUDED_PRODUCT_SUBTREES):
                    continue
                visit(child_value, str(child_key), current_identity_matched)
            return
        if isinstance(value, list):
            for child in value:
                visit(child, key, identity_matched)

    visit(payload, identity_matched=not bool(product_id))
    result: dict[str, object] = {
        "mainImageUrls": dedupe_urls(main_urls),
        "detailImageUrls": dedupe_urls(detail_urls),
        "videoUrls": dedupe_urls(video_urls),
    }
    if title is not None:
        result["title"] = title
    if price is not None:
        result["price"] = price
    return result


def merge_product_payloads(payloads: Iterable[dict[str, object]]) -> dict[str, object]:
    merged: dict[str, object] = {
        "mainImageUrls": [],
        "detailImageUrls": [],
        "videoUrls": [],
    }
    for payload in payloads:
        for field in ("mainImageUrls", "detailImageUrls", "videoUrls"):
            values = payload.get(field)
            if isinstance(values, list):
                merged[field] = dedupe_urls([*merged[field], *values])
        for field in ("title", "price"):
            value = payload.get(field)
            if not merged.get(field) and isinstance(value, str) and value.strip():
                merged[field] = value.strip()
    return merged


def _asset_filename(category: str, index: int, url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    allowed = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".m3u8"}
    if suffix not in allowed:
        suffix = ".bin"
    return f"{category}-{index:02d}{suffix}"


def build_assets(payload: dict[str, object], max_main_images: int | None = None) -> dict[str, list[Asset]]:
    field_by_category = {
        "main": "mainImageUrls",
        "detail": "detailImageUrls",
        "video": "videoUrls",
    }
    assets: dict[str, list[Asset]] = {category: [] for category in field_by_category}
    for category, field in field_by_category.items():
        values = payload.get(field)
        urls = values if isinstance(values, list) else []
        if category == "main" and max_main_images is not None:
            urls = urls[:max_main_images]
        safe_urls = [url for url in dedupe_urls(str(value) for value in urls) if is_safe_asset_url(url)]
        for index, url in enumerate(safe_urls, start=1):
            assets[category].append(Asset(url, _asset_filename(category, index, url), category))
    return assets


def _content_type(response: object) -> str:
    headers = getattr(response, "headers", {})
    getter = getattr(headers, "get", None)
    value = getter("Content-Type", "") if callable(getter) else ""
    return str(value or "").split(";", 1)[0].strip().lower()


def _content_length(response: object) -> int | None:
    headers = getattr(response, "headers", {})
    getter = getattr(headers, "get", None)
    value = getter("Content-Length", "") if callable(getter) else ""
    try:
        return int(value) if value else None
    except (TypeError, ValueError):
        return None


def _validate_download(path: Path, category: str) -> None:
    if category in {"main", "detail"}:
        try:
            with Image.open(path) as image:
                image.verify()
        except Exception as error:
            raise RuntimeError(f"下载内容不是有效图片: {error}") from error
        return
    prefix = path.read_bytes()[:16]
    if path.suffix.lower() == ".m3u8":
        if not prefix.startswith(b"#EXTM3U"):
            raise RuntimeError("下载内容不是有效的 M3U8 视频清单")
    elif b"ftyp" not in prefix:
        raise RuntimeError("下载内容不是有效的 MP4 视频")


def download_asset(
    url: str,
    destination: Path,
    timeout: float = 30.0,
    attempts: int = 2,
    referer: str | None = None,
    category: str = "main",
) -> None:
    if attempts < 1:
        raise ValueError("attempts must be greater than zero")
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + ".part")
    headers = {"User-Agent": "Mozilla/5.0"}
    if referer:
        headers["Referer"] = referer
    request = Request(url, headers=headers)
    for attempt in range(attempts):
        partial.unlink(missing_ok=True)
        try:
            if not is_safe_asset_url(url, resolve_dns=True):
                raise RuntimeError("素材 URL 不是允许的快手公开 CDN 地址")
            with urlopen(request, timeout=timeout) as response, partial.open("wb") as output:
                final_url = str(response.geturl() if hasattr(response, "geturl") else url)
                if not is_safe_asset_url(final_url, resolve_dns=True):
                    raise RuntimeError("素材下载重定向到了不允许的地址")
                content_type = _content_type(response)
                allowed_content = content_type.startswith("image/") if category in {"main", "detail"} else (
                    content_type.startswith("video/")
                    or content_type in {"application/octet-stream", "application/vnd.apple.mpegurl", "application/x-mpegurl"}
                )
                if not allowed_content:
                    raise RuntimeError(f"素材响应 Content-Type 无效: {content_type or 'missing'}")
                content_length = _content_length(response)
                if content_length is not None and content_length > MAX_ASSET_BYTES:
                    raise RuntimeError("素材响应超过 100MB 限制")
                downloaded = 0
                while chunk := response.read(256 * 1024):
                    downloaded += len(chunk)
                    if downloaded > MAX_ASSET_BYTES:
                        raise RuntimeError("素材下载超过 100MB 限制")
                    output.write(chunk)
            if partial.stat().st_size == 0:
                raise OSError("下载结果为空")
            _validate_download(partial, category)
            partial.replace(destination)
            return
        except Exception:
            partial.unlink(missing_ok=True)
            if attempt + 1 >= attempts:
                raise


def materialize_assets(
    assets: dict[str, list[Asset]],
    output_root: Path,
    referer: str,
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    records: list[dict[str, str]] = []
    failures: list[dict[str, str]] = []
    for category in ("main", "detail", "video"):
        for asset in assets.get(category, []):
            destination = output_root / asset.category / asset.filename
            try:
                download_asset(asset.url, destination, referer=referer, category=category)
            except Exception as error:
                failures.append({"type": category, "url": asset.url, "error": str(error)})
                continue
            records.append(
                {
                    "type": category,
                    "path": str(destination.resolve()),
                    "source_url": asset.url,
                    "source_name": asset.filename,
                }
            )
    return records, failures
