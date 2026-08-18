"""Three local image-generation workflows: main, SKU, and detail images."""

from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import ssl
import threading
import time
import uuid
from contextlib import contextmanager
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, as_completed, wait
from dataclasses import dataclass
from datetime import datetime
from http.client import IncompleteRead, RemoteDisconnected
from io import BytesIO
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from PIL import Image


CATEGORIES = ("main", "sku", "detail")
UpdateCallback = Callable[[dict[str, Any]], None]
RequestTimingCallback = Callable[[dict[str, Any]], None]
VISUAL_ANALYSIS_CONCURRENCY = 10
IMAGE_GENERATION_CONCURRENCY = 10
VISION_INITIAL_CONCURRENCY = 5
VISION_SLOW_REQUEST_SECONDS = 30.0
VISION_QUEUE_REPORT_SECONDS = 5.0
VISION_IMAGE_MAX_SIDE = 1280
VISION_IMAGE_MAX_BYTES = 1_500_000
IDENTITY_SOURCE_LIMIT = 16
GENERATED_IMAGE_WIDTH = 750
GENERATED_IMAGE_MAX_BYTES = 2 * 1024 * 1024
GENERATED_IMAGE_JPEG_QUALITIES = (90, 86, 82, 78, 74, 70, 65, 60, 50, 40, 30)
_VISUAL_ANALYSIS_SLOTS = {
    category: threading.BoundedSemaphore(VISUAL_ANALYSIS_CONCURRENCY)
    for category in CATEGORIES
}


class AdaptiveRequestGate:
    """Apply a bounded, gradually recovering request limit to one API class."""

    def __init__(
        self,
        max_concurrency: int,
        initial_concurrency: int = VISION_INITIAL_CONCURRENCY,
        min_concurrency: int = 1,
    ):
        self.max_concurrency = max(1, int(max_concurrency))
        self.min_concurrency = max(1, min(int(min_concurrency), self.max_concurrency))
        self._target_concurrency = max(
            self.min_concurrency,
            min(int(initial_concurrency), self.max_concurrency),
        )
        self._active = 0
        self._successes = 0
        self._condition = threading.Condition()

    @property
    def target_concurrency(self) -> int:
        with self._condition:
            return self._target_concurrency

    def acquire(self) -> None:
        with self._condition:
            while self._active >= self._target_concurrency:
                self._condition.wait()
            self._active += 1

    def release(self, success: bool) -> None:
        with self._condition:
            self._active = max(0, self._active - 1)
            if success:
                self._successes += 1
                if self._successes >= self._target_concurrency * 2:
                    self._target_concurrency = min(self.max_concurrency, self._target_concurrency + 1)
                    self._successes = 0
            else:
                self._target_concurrency = max(self.min_concurrency, self._target_concurrency - 1)
                self._successes = 0
            self._condition.notify_all()

    def record_failure(self) -> None:
        self.release(False)

    def record_success(self) -> None:
        self.release(True)


_VISION_REQUEST_GATE = AdaptiveRequestGate(
    VISUAL_ANALYSIS_CONCURRENCY,
    min_concurrency=VISION_INITIAL_CONCURRENCY,
)
_IMAGE_REQUEST_GATE = AdaptiveRequestGate(
    IMAGE_GENERATION_CONCURRENCY,
    initial_concurrency=IMAGE_GENERATION_CONCURRENCY,
    min_concurrency=IMAGE_GENERATION_CONCURRENCY,
)

VISION_REQUEST_PROFILES = {
    "preflight": {"max_completion_tokens": 64, "json_response": False},
    "title": {"max_completion_tokens": 1024, "json_response": True},
    "identity": {"max_completion_tokens": 2048, "json_response": True},
    "sku": {"max_completion_tokens": 2048, "json_response": True},
    "analysis": {"max_completion_tokens": 4096, "json_response": True},
    "dossier": {"max_completion_tokens": 4096, "json_response": True},
}


def build_vision_payload(
    model: str,
    messages: list[dict[str, Any]],
    request_kind: str,
    *,
    json_response: bool | None = None,
) -> dict[str, Any]:
    try:
        profile = VISION_REQUEST_PROFILES[request_kind]
    except KeyError as error:
        raise ValueError(f"Unknown vision request kind: {request_kind}") from error
    wants_json = bool(profile["json_response"] if json_response is None else json_response)
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "reasoning_effort": "low",
        "max_completion_tokens": int(profile["max_completion_tokens"]),
    }
    if wants_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def is_noteworthy_vision_timing(timing: dict[str, Any]) -> bool:
    return (
        not bool(timing.get("success"))
        or int(timing.get("attempt") or 1) > 1
        or float(timing.get("queue_seconds") or 0) >= VISION_QUEUE_REPORT_SECONDS
        or float(timing.get("request_seconds") or 0) >= VISION_SLOW_REQUEST_SECONDS
    )


def _notify_request_timing(
    callback: RequestTimingCallback | None,
    timing: dict[str, Any],
) -> None:
    if callback is None:
        return
    try:
        callback(timing)
    except Exception:
        # Diagnostics must never turn a successful API request into a failed task.
        pass


@contextmanager
def _request_slot(gate: AdaptiveRequestGate):
    gate.acquire()
    outcome = {"success": False}
    try:
        yield lambda: outcome.__setitem__("success", True)
    except BaseException:
        gate.release(False)
        raise
    else:
        gate.release(bool(outcome["success"]))

WORKFLOW_PROFILES = {
    "main": {
        "label": "主图工作流",
        "focus": (
            "Create a product-faithful marketplace hero image for any product category. Keep the reference "
            "image's commercial atmosphere, framing relationships, camera direction, lighting, background, "
            "and ordinary props while replacing its product identity with the supplied product. Do not "
            "assume a fixed category from the reference image. Include a clear ecommerce title and one to "
            "three evidence-backed selling points drawn from the current inputs only."
        ),
    },
    "sku": {
        "label": "SKU 图工作流",
        "focus": (
            "Create a clean, product-faithful SKU visual for any product category with no off-product marketing "
            "copy. Preserve the exact SKU, color, quantity, packaging, viewing angle, and visible product or "
            "packaging labels. Remove external titles, selling points, parameter notes, badges, arrows, and text "
            "containers, then rebuild a premium background appropriate to the product's commercial positioning."
        ),
    },
    "detail": {
        "label": "详情图工作流",
        "focus": (
            "Create a product-faithful detail-page visual for any product category. Preserve the reference "
            "image's visual hierarchy, scene, material atmosphere, camera, lighting, background, and "
            "ordinary props while using the supplied product as the only product identity source. Do not "
            "assume a fixed category from the reference. Include a section title and one to three "
            "evidence-backed detail notes derived from the current product only."
        ),
    },
}

IMAGE_QUALITY_DIRECTIVE = """Image quality target:
- Deliver a 2K-class high-definition ecommerce image with natural photographic clarity, clean tonal separation, realistic depth, and a polished commercial finish.
- Keep edges naturally clear with no oversharpening, halos, jagged contours, color fringing, noise, blur blocks, plastic-looking rendering, or AI smear artifacts.
- Do not invent micro-texture, surface grain, printed detail, seams, labels, material structure, or other information that is not supported by the input images.
- Highlights must not clip or turn into flat white patches. Shadows must retain natural detail without crushed blacks or a gray, hazy cast.
- Preserve accurate color and physically plausible light falloff. The result must look like a clean high-resolution commercial photograph rather than an aggressively sharpened or synthetic 3D render."""

DIRECT_REFERENCE_QUALITY_BOUNDARY = """Direct-reference quality boundary:
- 2K-class quality applies only to the finished canvas and editable non-product regions.
- It must not trigger sharpening, texture synthesis, relighting, or redrawing inside the product region. Product pixels remain governed by the absolute product freeze."""

MODEL_REFRESH_DIRECTIVE = """Human model refresh policy:
- If the reference image contains any visible human model or person, replace the original model with a distinct fictional, non-identifiable AI person. Do not preserve the original model unchanged.
- Preserve only the general pose, action, framing, crop, gaze direction, hand gesture, and product interaction needed by the composition.
- Change the face, facial proportions, hairstyle, hair color, makeup, non-product clothing details, non-product accessories, tattoos, and every other identity-bearing or recognizable personal trait. Do not imitate the original person's face, likeness, identity, signature styling, or real-world source.
- The replacement must look like a newly generated person, not a face swap or a lightly edited copy of the source model.
- If the reference contains no person, do not add a person.
- Remove any off-product real-person name, celebrity name, endorsement, recommendation, or same-style wording together with its dedicated container, and naturally repair that area. Do not add wording that implies the fictional person endorses or is affiliated with the product.
- Preserve the exact product and its overlap boundaries. Changing the model must not modify any product pixel, product geometry, product label, product color, or product placement."""

MODEL_REFRESH_ANALYSIS_DIRECTIVE = """Human model analysis rule:
- Detect whether the reference image contains any visible human model or person used in the composition. Ordinary models are also replacement targets; do not limit this decision to celebrities, public figures, influencers, or explicit endorsers.
- When a model is present, instruct generation to replace that person with a distinct fictional, non-identifiable AI person while preserving only the general pose, action, framing, hand gesture, and product interaction. Require changes to the face, facial proportions, hairstyle, hair color, makeup, non-product clothing details, non-product accessories, and other recognizable personal traits.
- Continue to flag off-product real-person or celebrity names, endorsement, recommendation, and same-style claims as removable compliance risks.
- When no person is present, do not add a person. The product and its overlap boundaries remain protected."""

SKU_TEXT_FREE_DIRECTIVE = """SKU text-free presentation policy:
- Produce a clean SKU presentation with no off-product marketing copy anywhere in the finished image.
- Remove off-product titles, selling points, parameter notes, descriptions, numeric callouts, badges, arrows, decorative text, pseudo-text, and their dedicated frames, strips, capsules, or containers. Naturally reconstruct those areas from the background with no blank boxes, blur blocks, smears, or text remnants.
- Preserve authentic text printed on the product or packaging, including the real brand, logo, model, specification, capacity, and factual label content. Do not erase, rewrite, translate, move, or garble authentic on-product or on-packaging text.
- Do not add any new text, symbols, badges, arrows, specifications, or marketing claims outside the product.
- Design the non-product background from the product's category, material, color, target customer, usage context, and commercial positioning. Keep it refined, simple, category-appropriate, and focused on this exact SKU set, with coherent palette, natural lighting, contact shadow, spatial depth, material quality, and clean negative space.
- Keep the exact visible unit count and set arrangement required for this SKU. Do not add or remove units, mix another SKU, or invent unsupported accessories, scenes, or usage contexts."""

MAIN_COMPOSITION_ROLES = (
    "正面核心主视觉，商品居中但保留有层次的商业留白",
    "三分之二侧向主视觉，建立不同于前图的空间层次",
    "侧向轮廓主视觉，突出经输入图确认的外形边界",
    "近景质感主视觉，提高主体占比并突出可见材质",
    "低机位主视觉，保持真实结构并增强视觉力量感",
    "轻俯拍主视觉，利用台面关系形成清晰层次",
    "环境叙事主视觉，以商品适用场景强化商业氛围",
    "道具互动主视觉，用普通道具建立前中后景关系",
    "留白信息主视觉，为标题与卖点建立清晰视觉层级",
    "错落层次主视觉，在不增加 SKU 的前提下强化景深与节奏",
)

ANALYSIS_REQUIRED_FIELDS = (
    "product_fingerprint",
    "reference_visual_brief",
    "compliance_risks",
    "copy_plan",
    "generation_prompt",
)

DIRECT_REFERENCE_REMOVABLE_RISK_CODES = frozenset({
    "competitor_brand",
    "store_or_watermark",
    "patent_or_certification",
    "origin_or_import",
    "medical_treatment",
    "absolute_or_ranking",
    "unsupported_sales_price_data",
})

DIRECT_REFERENCE_EXPLICIT_RISK_MARKERS = {
    "patent_or_certification": ("专利", "认证", "检测报告", "检验报告", "证书", "FDA", "CE", "3C"),
    "origin_or_import": ("进口", "原产", "德国", "美国", "日本", "韩国", "马来西亚"),
    "medical_treatment": ("治疗", "医疗", "治愈", "根治", "药到病除", "疾病", "临床治疗", "疗效", "处方", "药品"),
    "absolute_or_ranking": ("第一", "最佳", "最强", "唯一", "永久", "绝对", "顶级", "首选", "100%", "保证"),
    "unsupported_sales_price_data": ("销量", "已售", "到手价", "原价", "现价", "折扣"),
}


@dataclass(frozen=True)
class ApiSettings:
    base_url: str
    vision_api_key: str
    image_api_key: str
    vision_model: str = "gpt-5.5"
    image_model: str = "gpt-image-2"

    def endpoint(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"


@dataclass(frozen=True)
class ImageTask:
    category: str
    ordinal: int
    source_path: Path
    supporting_path: Path | None = None
    view_plan: DetailViewPlan | None = None
    inferred_view: bool = False
    composition_role: str = ""
    manual_sku: dict[str, Any] | None = None


@dataclass(frozen=True)
class IdentitySource:
    index: int
    category: str
    path: Path
    is_anchor: bool = False


@dataclass(frozen=True)
class DetailViewPlan:
    ordinal: int
    view_type: str
    focus: str
    supporting_source_index: int | None
    inferred_view: bool
    prohibited_inventions: tuple[str, ...]


STRUCTURAL_VIEW_TYPES = {"front", "three_quarter", "side", "back", "top", "bottom"}
PROHIBITED_INVENTIONS = (
    "ports",
    "buttons",
    "pockets",
    "openings",
    "accessories",
    "controls",
)


def normalize_view_type(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if normalized.endswith("_view"):
        normalized = normalized[:-5]
    return "back" if normalized == "rear" else normalized


def _request_json(
    url: str,
    api_key: str,
    payload: dict[str, Any],
    timeout: int = 180,
    *,
    timing_callback: RequestTimingCallback | None = None,
    request_kind: str = "unknown",
) -> dict[str, Any]:
    request = Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Connection": "close",
        },
        method="POST",
    )
    return _send_json_request(
        request,
        timeout,
        timing_callback=timing_callback,
        request_kind=request_kind,
    )


def _redirected_post_request(request: Request, location: str) -> Request:
    return Request(
        urljoin(request.full_url, location),
        data=request.data,
        headers=dict(request.header_items()),
        method="POST",
    )


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    base = float(2 ** (attempt + 1))
    if retry_after:
        try:
            base = max(base, min(float(retry_after), 60.0))
        except ValueError:
            pass
    return base + random.uniform(0.0, min(3.0, base * 0.25))


def _send_json_request(
    request: Request,
    timeout: int,
    attempts: int = 5,
    *,
    timing_callback: RequestTimingCallback | None = None,
    request_kind: str = "unknown",
) -> dict[str, Any]:
    redirects = 0
    for attempt in range(attempts):
        queue_started = time.perf_counter()
        queue_seconds = 0.0
        request_started = None
        request_seconds = 0.0
        last_error: BaseException | None = None
        try:
            with _request_slot(_VISION_REQUEST_GATE) as mark_success:
                request_started = time.perf_counter()
                queue_seconds = request_started - queue_started
                with urlopen(request, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                request_seconds = time.perf_counter() - request_started
                mark_success()
                _notify_request_timing(
                    timing_callback,
                    {
                        "request_kind": request_kind,
                        "attempt": attempt + 1,
                        "queue_seconds": queue_seconds,
                        "request_seconds": request_seconds,
                        "success": True,
                    },
                )
                return result
        except HTTPError as error:
            last_error = error
            message = error.read().decode("utf-8", errors="replace")[:500]
            location = error.headers.get("Location") if error.headers else None
            if error.code in {307, 308} and location and redirects < 3:
                request = _redirected_post_request(request, location)
                redirects += 1
                continue
            if error.code not in {408, 429} and not 500 <= error.code < 600:
                suffix = f"; Location: {location}" if location else ""
                raise RuntimeError(f"HTTP {error.code}: {message}{suffix}") from error
            if attempt + 1 == attempts:
                final_error = RuntimeError(f"HTTP {error.code} after {attempts} attempts: {message}")
                _notify_request_timing(
                    timing_callback,
                    {
                        "request_kind": request_kind,
                        "attempt": attempt + 1,
                        "queue_seconds": queue_seconds,
                        "request_seconds": request_seconds,
                        "success": False,
                        "error": str(final_error),
                    },
                )
                raise final_error from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
        except (
            URLError,
            TimeoutError,
            RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            IncompleteRead,
            ssl.SSLError,
        ) as error:
            last_error = error
            reason = error.reason if isinstance(error, URLError) else str(error)
            if attempt + 1 == attempts:
                final_error = RuntimeError(f"Network request failed after {attempts} attempts: {reason}")
                _notify_request_timing(
                    timing_callback,
                    {
                        "request_kind": request_kind,
                        "attempt": attempt + 1,
                        "queue_seconds": queue_seconds,
                        "request_seconds": request_seconds,
                        "success": False,
                        "error": str(final_error),
                    },
                )
                raise final_error from error
            retry_after = None
        _notify_request_timing(
            timing_callback,
            {
                "request_kind": request_kind,
                "attempt": attempt + 1,
                "queue_seconds": queue_seconds,
                "request_seconds": (
                    time.perf_counter() - request_started
                    if request_started is not None and request_seconds == 0.0
                    else request_seconds
                ),
                "success": False,
                "error": str(last_error or "request failed"),
            },
        )
        if attempt + 1 < attempts:
            time.sleep(_retry_delay(attempt, retry_after))
    raise RuntimeError("Network request failed")


def _multipart_body(fields: dict[str, str], files: list[tuple[str, Path]]) -> tuple[bytes, str]:
    boundary = f"----LocalWorkflow{uuid.uuid4().hex}"
    chunks: list[bytes] = []

    for name, value in fields.items():
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode(),
                value.encode("utf-8"),
                b"\r\n",
            ]
        )

    for field_name, path in files:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.extend(
            [
                f"--{boundary}\r\n".encode(),
                (
                    f'Content-Disposition: form-data; name="{field_name}"; '
                    f'filename="{path.name}"\r\n'
                ).encode(),
                f"Content-Type: {content_type}\r\n\r\n".encode(),
                path.read_bytes(),
                b"\r\n",
            ]
        )
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), boundary


def _request_image(
    url: str,
    api_key: str,
    model: str,
    prompt: str,
    images: list[Path],
    timeout: int = 600,
) -> bytes:
    if not images:
        raise ValueError("At least one image is required")
    body, boundary = _multipart_body(
        {
            "model": model,
            "prompt": prompt,
            "size": "auto",
            "quality": "high",
            "response_format": "b64_json",
            "n": "1",
        },
        [("image[]", image) for image in images],
    )
    request = Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    payload = _send_image_request(request, timeout)

    value = payload.get("data", [{}])[0].get("b64_json")
    if not isinstance(value, str) or not value:
        message = payload.get("error", {}).get("message") or payload.get("message") or "Missing b64_json"
        raise RuntimeError(f"Image API returned no image: {message}")
    return base64.b64decode(value)


def _send_image_request(request: Request, timeout: int, attempts: int = 5) -> dict[str, Any]:
    redirects = 0
    for attempt in range(attempts):
        try:
            with _request_slot(_IMAGE_REQUEST_GATE) as mark_success:
                with urlopen(request, timeout=timeout) as response:
                    result = json.loads(response.read().decode("utf-8"))
                mark_success()
                return result
        except HTTPError as error:
            message = error.read().decode("utf-8", errors="replace")[:500]
            location = error.headers.get("Location") if error.headers else None
            if error.code in {307, 308} and location and redirects < 3:
                request = _redirected_post_request(request, location)
                redirects += 1
                continue
            if error.code not in {408, 429} and not 500 <= error.code < 600:
                suffix = f"; Location: {location}" if location else ""
                raise RuntimeError(f"HTTP {error.code}: {message}{suffix}") from error
            if attempt + 1 == attempts:
                raise RuntimeError(f"HTTP {error.code} after {attempts} attempts: {message}") from error
            retry_after = error.headers.get("Retry-After") if error.headers else None
        except (
            URLError,
            TimeoutError,
            RemoteDisconnected,
            ConnectionResetError,
            ConnectionAbortedError,
            BrokenPipeError,
            IncompleteRead,
            ssl.SSLError,
        ) as error:
            reason = error.reason if isinstance(error, URLError) else str(error)
            if attempt + 1 == attempts:
                raise RuntimeError(f"Image request failed after {attempts} attempts: {reason}") from error
            retry_after = None
        if attempt + 1 < attempts:
            time.sleep(_retry_delay(attempt, retry_after))
    raise RuntimeError("Image request failed")


def _parse_json_response(value: str) -> dict[str, Any]:
    cleaned = value.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0].strip()
    parsed, _ = json.JSONDecoder().raw_decode(cleaned)
    if not isinstance(parsed, dict):
        raise ValueError("Vision response must be a JSON object")
    return parsed


_VISION_IMAGE_CACHE: dict[tuple[str, int, int], str] = {}
_VISION_IMAGE_CACHE_LOCK = threading.Lock()


def _image_data_url(path: Path) -> str:
    path = Path(path)
    stat = path.stat()
    cache_key = (str(path.resolve()), stat.st_mtime_ns, stat.st_size)
    with _VISION_IMAGE_CACHE_LOCK:
        cached = _VISION_IMAGE_CACHE.get(cache_key)
    if cached:
        return cached

    raw = path.read_bytes()
    try:
        with Image.open(BytesIO(raw)) as source:
            source.load()
            image = source.convert("RGB")
            if max(image.size) > VISION_IMAGE_MAX_SIDE:
                scale = VISION_IMAGE_MAX_SIDE / max(image.size)
                image = image.resize(
                    (max(1, round(image.width * scale)), max(1, round(image.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            encoded = b""
            for quality in (85, 78, 70, 60, 50, 40):
                buffer = BytesIO()
                image.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
                encoded = buffer.getvalue()
                if len(encoded) <= VISION_IMAGE_MAX_BYTES:
                    break
        value = f"data:image/jpeg;base64,{base64.b64encode(encoded).decode('ascii')}"
    except (OSError, ValueError):
        mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
        value = f"data:{mime_type};base64,{base64.b64encode(raw).decode('ascii')}"

    with _VISION_IMAGE_CACHE_LOCK:
        if len(_VISION_IMAGE_CACHE) >= 512:
            _VISION_IMAGE_CACHE.pop(next(iter(_VISION_IMAGE_CACHE)))
        _VISION_IMAGE_CACHE[cache_key] = value
    return value


def _nonnegative_int(value: Any) -> int | None:
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def materialize_sku_screenshot_references(
    screenshot: Path,
    analysis: dict[str, Any],
    output_dir: Path,
) -> list[dict[str, Any]]:
    """Crop screenshot thumbnails only when their visual evidence is usable."""
    if not screenshot.is_file():
        raise FileNotFoundError(f"SKU screenshot does not exist: {screenshot}")
    output_dir.mkdir(parents=True, exist_ok=True)
    with Image.open(screenshot) as source_image:
        source = source_image.convert("RGB")
        width, height = source.size
        variants: list[dict[str, Any]] = []
        for index, raw in enumerate((analysis.get("skus") or [])[:8], start=1):
            if not isinstance(raw, dict):
                continue
            thumbnail = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
            x = _nonnegative_int(thumbnail.get("x"))
            y = _nonnegative_int(thumbnail.get("y"))
            crop_width = _nonnegative_int(thumbnail.get("width"))
            crop_height = _nonnegative_int(thumbnail.get("height"))
            try:
                confidence = float(raw.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            reference_image = ""
            source_status = "low_visual_confidence"
            quality_note = "截图缩略图过小、模糊或无法确认，降级使用商品主图"
            if (
                raw.get("is_clear") is True
                and confidence >= 0.65
                and x is not None
                and y is not None
                and crop_width is not None
                and crop_height is not None
            ):
                left = min(x, width)
                top = min(y, height)
                right = min(width, left + crop_width)
                bottom = min(height, top + crop_height)
                if right - left >= 64 and bottom - top >= 64:
                    target = output_dir / f"sku-{index:03d}.png"
                    crop = source.crop((left, top, right, bottom))
                    scale = max(1.0, 512 / max(crop.size))
                    if scale > 1:
                        crop = crop.resize(
                            (round(crop.width * scale), round(crop.height * scale)),
                            Image.Resampling.LANCZOS,
                        )
                    crop.save(target, format="PNG")
                    reference_image = str(target.resolve())
                    source_status = "screenshot_thumbnail"
                    quality_note = ""
            variants.append(
                {
                    "sku_name": str(raw.get("sku_name") or "").strip(),
                    "color": str(raw.get("color") or "").strip(),
                    "spec": str(raw.get("spec") or "").strip(),
                    "price": str(raw.get("price") or "").strip(),
                    "reference_image": reference_image,
                    "source_status": source_status,
                    "visual_confidence": round(max(0.0, min(confidence, 1.0)), 3),
                    "quality_note": quality_note,
                }
            )
        return variants


def _write_generated_image(image_bytes: bytes, output_path: Path) -> None:
    with Image.open(BytesIO(image_bytes)) as source:
        source.load()
        target_height = max(1, round(source.height * GENERATED_IMAGE_WIDTH / source.width))
        resized = source.resize(
            (GENERATED_IMAGE_WIDTH, target_height),
            Image.Resampling.LANCZOS,
        )
        if resized.mode in {"RGBA", "LA"} or "transparency" in resized.info:
            rgba = resized.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
        else:
            rgb = resized.convert("RGB")

        encoded = b""
        for quality in GENERATED_IMAGE_JPEG_QUALITIES:
            buffer = BytesIO()
            rgb.save(buffer, format="JPEG", quality=quality, optimize=True, progressive=True)
            encoded = buffer.getvalue()
            if len(encoded) <= GENERATED_IMAGE_MAX_BYTES:
                break

    if len(encoded) > GENERATED_IMAGE_MAX_BYTES:
        raise ValueError("Generated image could not be compressed below 2 MB")
    output_path.write_bytes(encoded)


def normalize_listing_title(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip("，。；、|｜-—_ ")


def validate_listing_titles(document: dict[str, Any]) -> dict[str, Any]:
    long_title = normalize_listing_title(document.get("long_title"))
    short_title = normalize_listing_title(document.get("short_title"))
    if len(long_title) > 60:
        long_title = long_title[:60]
    if len(short_title) > 10:
        short_title = short_title[:10]
    if not 55 <= len(long_title) <= 60:
        raise ValueError(f"长标题必须为 55-60 字，当前 {len(long_title)} 字")
    if not 1 <= len(short_title) <= 10:
        raise ValueError(f"短标题必须为 1-10 字，当前 {len(short_title)} 字")
    return {
        "long_title": long_title,
        "short_title": short_title,
        "long_title_length": len(long_title),
        "short_title_length": len(short_title),
    }


def _validate_analysis(analysis: dict[str, Any]) -> None:
    missing = [field for field in ANALYSIS_REQUIRED_FIELDS if field not in analysis]
    if missing:
        raise RuntimeError(f"Vision response is missing required fields: {', '.join(missing)}")
    if not isinstance(analysis["product_fingerprint"], dict):
        raise RuntimeError("Vision response product_fingerprint must be an object")
    if not isinstance(analysis["reference_visual_brief"], dict):
        raise RuntimeError("Vision response reference_visual_brief must be an object")
    if not isinstance(analysis["compliance_risks"], list):
        raise RuntimeError("Vision response compliance_risks must be an array")
    copy_plan = analysis["copy_plan"]
    if not isinstance(copy_plan, dict):
        raise RuntimeError("Vision response copy_plan must be an object")
    if not isinstance(copy_plan.get("headline"), str) or not copy_plan["headline"].strip():
        raise RuntimeError("Vision response copy_plan.headline must be a non-empty string")
    if not isinstance(copy_plan.get("subheadline"), str):
        raise RuntimeError("Vision response copy_plan.subheadline must be a string")
    selling_points = copy_plan.get("selling_points")
    if not isinstance(selling_points, list) or not 1 <= len(selling_points) <= 3:
        raise RuntimeError("Vision response copy_plan.selling_points must contain one to three items")
    for selling_point in selling_points:
        if not isinstance(selling_point, dict):
            raise RuntimeError("Each selling point must be an object")
        if not isinstance(selling_point.get("text"), str) or not selling_point["text"].strip():
            raise RuntimeError("Each selling point must contain non-empty text")
        if not isinstance(selling_point.get("basis"), str) or not selling_point["basis"].strip():
            raise RuntimeError("Each selling point must contain an evidence basis")
    if not isinstance(copy_plan.get("layout_instruction"), str) or not copy_plan["layout_instruction"].strip():
        raise RuntimeError("Vision response copy_plan.layout_instruction must be a non-empty string")
    if not isinstance(analysis["generation_prompt"], str) or not analysis["generation_prompt"].strip():
        raise RuntimeError("Vision response generation_prompt must be a non-empty string")


def _validate_own_product_analysis(analysis: dict[str, Any], category: str = "") -> None:
    fingerprint = analysis.get("product_fingerprint", {})
    dispensing_state = fingerprint.get("dispensing_state")
    if not isinstance(dispensing_state, dict):
        raise RuntimeError("Vision response product_fingerprint.dispensing_state must be an object")
    if not isinstance(dispensing_state.get("closure_state"), str) or not dispensing_state["closure_state"].strip():
        raise RuntimeError("Vision response product_fingerprint.dispensing_state.closure_state must be a non-empty string")
    if not isinstance(dispensing_state.get("outlet_exposed"), bool):
        raise RuntimeError("Vision response product_fingerprint.dispensing_state.outlet_exposed must be a boolean")
    if not isinstance(dispensing_state.get("verified_material_effect_origin"), str):
        raise RuntimeError(
            "Vision response product_fingerprint.dispensing_state.verified_material_effect_origin must be a string"
        )

    visual_brief = analysis.get("reference_visual_brief", {})
    presence = visual_brief.get("contains_replaceable_product")
    if not isinstance(presence, bool):
        raise RuntimeError(
            "Vision response reference_visual_brief.contains_replaceable_product must be a boolean"
        )
    primary_unit_count = visual_brief.get("primary_replaceable_product_unit_count")
    expected_minimum = 1 if presence else 0
    expected_maximum = 20 if presence else 0
    if (
        isinstance(primary_unit_count, bool)
        or not isinstance(primary_unit_count, int)
        or not expected_minimum <= primary_unit_count <= expected_maximum
    ):
        raise RuntimeError(
            "Vision response reference_visual_brief.primary_replaceable_product_unit_count must match the visible primary-product count"
        )
    gifts = visual_brief.get("gift_or_bonus_elements")
    if not isinstance(gifts, list):
        raise RuntimeError("Vision response reference_visual_brief.gift_or_bonus_elements must be an array")
    for gift in gifts:
        if (
            not isinstance(gift, dict)
            or not isinstance(gift.get("description"), str)
            or not gift["description"].strip()
            or gift.get("action") != "remove"
        ):
            raise RuntimeError(
                "Each gift_or_bonus_elements item must contain a non-empty description and action 'remove'"
            )
    physical_effects = visual_brief.get("physical_effects")
    if not isinstance(physical_effects, list):
        raise RuntimeError("Vision response reference_visual_brief.physical_effects must be an array")
    for effect in physical_effects:
        if (
            not isinstance(effect, dict)
            or not isinstance(effect.get("description"), str)
            or not effect["description"].strip()
            or not isinstance(effect.get("origin_visible"), bool)
        ):
            raise RuntimeError(
                "Each physical_effects item must contain a non-empty description and boolean origin_visible"
            )
    if category == "sku" and presence:
        unit_count = visual_brief.get("visible_product_unit_count")
        if isinstance(unit_count, bool) or not isinstance(unit_count, int) or not 1 <= unit_count <= 20:
            raise RuntimeError(
                "Vision response reference_visual_brief.visible_product_unit_count must be an integer between 1 and 20 for a SKU reference containing a product"
            )
    for selling_point in analysis.get("copy_plan", {}).get("selling_points", []):
        basis = str(selling_point.get("basis") or "")
        if not basis.startswith("Image 1 visible evidence:"):
            raise RuntimeError(
                "Each own-product selling point basis must start with 'Image 1 visible evidence:'"
            )
        required_visual_evidence = selling_point.get("required_visual_evidence")
        if not isinstance(required_visual_evidence, str) or not required_visual_evidence.strip():
            raise RuntimeError(
                "Each own-product selling point must contain non-empty required_visual_evidence"
            )


def _normalize_compliance_risks(analysis: dict[str, Any]) -> None:
    risks = analysis.get("compliance_risks")
    if isinstance(risks, list):
        return
    if isinstance(risks, dict):
        for key in ("items", "risks", "compliance_risks"):
            nested = risks.get(key)
            if isinstance(nested, list):
                analysis["compliance_risks"] = nested
                return
        analysis["compliance_risks"] = [risks] if risks else []


def _review_direct_reference_compliance_risks(analysis: dict[str, Any]) -> None:
    reported = [dict(item) for item in analysis.get("compliance_risks", []) if isinstance(item, dict)]
    reviewed: list[dict[str, Any]] = []
    for item in reported:
        code = str(item.get("risk_code") or "").strip()
        location = str(item.get("location") or "").strip().lower()
        decision = str(item.get("decision") or "").strip().lower()
        original_text = str(item.get("original_text") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if (
            code not in DIRECT_REFERENCE_REMOVABLE_RISK_CODES
            or not location.startswith("off_product editable:")
            or decision != "remove"
            or not original_text
            or not reason
        ):
            continue
        markers = DIRECT_REFERENCE_EXPLICIT_RISK_MARKERS.get(code)
        if markers and not any(marker in original_text for marker in markers):
            continue
        reviewed.append(item)
    analysis["reported_compliance_risks"] = reported
    analysis["compliance_risks"] = reviewed


def _normalize_copy_plan(analysis: dict[str, Any]) -> None:
    copy_plan = analysis.get("copy_plan")
    if not isinstance(copy_plan, dict) or isinstance(copy_plan.get("headline"), str):
        return
    section_title = copy_plan.get("section_title")
    zones = copy_plan.get("ordinary_copy_zones")
    if not isinstance(section_title, str) or not section_title.strip() or not isinstance(zones, list):
        return

    preferred: list[dict[str, Any]] = []
    ordinary: list[dict[str, Any]] = []
    for zone in zones:
        if not isinstance(zone, dict):
            continue
        purpose = str(zone.get("purpose") or "")
        target = preferred if any(word in purpose for word in ("细节", "卖点", "说明")) else ordinary
        target.append(zone)

    selling_points: list[dict[str, str]] = []
    for zone in preferred + ordinary:
        raw_copy = zone.get("new_copy")
        values = raw_copy if isinstance(raw_copy, list) else [raw_copy]
        for value in values:
            text = str(value or "").strip()
            if not text:
                continue
            selling_points.append(
                {
                    "text": text,
                    "basis": str(zone.get("notes") or "Visible evidence from the current product images"),
                    "placement": str(zone.get("zone") or "reference copy zone"),
                }
            )
            if len(selling_points) == 3:
                break
        if len(selling_points) == 3:
            break

    analysis["copy_plan"] = {
        "headline": section_title.strip(),
        "subheadline": "",
        "selling_points": selling_points,
        "layout_instruction": "Preserve the corresponding title and ordinary copy zones from the reference layout.",
    }


def _normalize_direct_reference_analysis(
    analysis: dict[str, Any],
    category: str,
    view_plan: DetailViewPlan | None,
) -> None:
    _normalize_compliance_risks(analysis)
    _review_direct_reference_compliance_risks(analysis)
    _normalize_copy_plan(analysis)
    generation_prompt = analysis.get("generation_prompt")
    if isinstance(generation_prompt, str) and generation_prompt.strip():
        return
    direction = workflow_instruction(category)
    if view_plan is not None:
        direction = (
            f"Create the planned {view_plan.view_type} ecommerce image. "
            f"Detail focus: {view_plan.focus}. {direction}"
        )
    analysis["generation_prompt"] = direction


def workflow_instruction(category: str) -> str:
    try:
        profile = WORKFLOW_PROFILES[category]
    except KeyError as error:
        raise ValueError(f"Unknown workflow category: {category}") from error
    return str(profile["focus"])


def compose_generation_prompt(
    analysis: dict[str, Any],
    category: str,
    generation_mode: str = "own_product",
    dossier: dict[str, Any] | None = None,
    view_plan: DetailViewPlan | None = None,
    composition_role: str = "",
    sku_condition: dict[str, Any] | None = None,
) -> str:
    """Combine the visual brief with non-negotiable product-fidelity rules."""
    _validate_analysis(analysis)
    fingerprint = json.dumps(analysis["product_fingerprint"], ensure_ascii=False, indent=2)
    visual_brief = json.dumps(analysis["reference_visual_brief"], ensure_ascii=False, indent=2)
    compliance_risks = json.dumps(analysis["compliance_risks"], ensure_ascii=False, indent=2)
    copy_plan = json.dumps(analysis["copy_plan"], ensure_ascii=False, indent=2)
    model_prompt = analysis["generation_prompt"].strip()
    sku_directive = SKU_TEXT_FREE_DIRECTIVE if category == "sku" else ""
    own_product_sku_directive = (
        "SKU identity and quantity split:\n"
        "- Image 1 is the sole authority for each individual product unit's exact model, silhouette, proportions, "
        "color, material, packaging, label, logo, printed text, artwork, capacity, and components. Every visible unit "
        "must be an unchanged copy of this exact product identity.\n"
        "- Image 2 is the sole authority for the visible unit count, bundle quantity, set composition, spacing, and "
        "arrangement for this SKU output. If Image 2 shows three visible units, output exactly three Image 1 product "
        "units; if it shows two or five, output exactly two or five. Do not infer the count from Image 1.\n"
        "- Transfer only Image 2's visible count and arrangement. Never transfer its competitor product shape, "
        "brand, label, color, packaging artwork, claims, accessories, or off-product copy.\n"
        if category == "sku" and generation_mode == "own_product"
        else ""
    )
    manual_sku_directive = ""
    if category == "sku" and sku_condition:
        condition = json.dumps(sku_condition, ensure_ascii=False, indent=2)
        manual_sku_directive = f"""User-supplied SKU condition:
- Source status: {sku_condition.get('source_status', 'text_conditioned')}. This is user-provided conditioning data, not platform-verified SKU metadata.
- Apply only the explicitly supplied SKU name, color, specification, and visible variant attributes below. Do not infer another color, size, capacity, bundle, accessory, package, or price.
- Preserve the product's verified structure, proportions, components, material behavior, labels, and logo. Change a visual variant attribute only when the user supplied that exact value.
- Do not render the price, SKU name, specification, color text, or any other off-product copy in the image. These fields constrain the product variant only.
{condition}"""
    screenshot_sku_directive = (
        "SKU screenshot evidence boundary (highest priority for screenshot-conditioned SKU tasks):\n"
        "- This SKU reference is a cropped, low-resolution screenshot thumbnail. It may confirm only the explicitly visible SKU composition, unit count, approximate arrangement, and variant-level appearance.\n"
        "- The collected main product image is the authority for product identity, package structure, label placement, logo, printed text, material detail, and fine surface features. Never enlarge or invent unreadable screenshot text or texture.\n"
        "- When the screenshot thumbnail and the collected main product image conflict on fine detail, follow the collected main product image and preserve the screenshot-confirmed bundle composition only.\n"
        if category == "sku" and sku_condition and sku_condition.get("source_status") == "screenshot_thumbnail"
        else ""
    )
    copy_layout = (
        "SKU tasks are text-free outside the product: do not render the copy_plan, and remove all off-product "
        "titles, selling points, parameter notes, badges, arrows, pseudo-text, and their dedicated containers. "
        "Use copy_plan only to audit deletion zones."
        if category == "sku"
        else
        "A clear title and one to three selling points are mandatory for main and detail tasks.\n"
        "- Render the exact Simplified Chinese headline, subheadline, and one to three selling points from the approved copy_plan below. Do not paraphrase, translate, shorten, expand, or add any other copy.\n"
        "- Reuse Image 2's headline scale, text hierarchy, banner, badge, callout, and information-zone relationships as the layout reference, while replacing prohibited competitor wording with the approved copy_plan.\n"
        "- Keep every character clear and legible. Do not generate misspellings, pseudo-text, garbled characters, repeated copy, or text over critical product details.\n"
        "- Give the headline strong first-level hierarchy, the subheadline clear second-level hierarchy when non-empty, and distribute the selling points through the reference image's corresponding selling-point regions."
    )
    if generation_mode == "competitor_reference":
        dossier_text = json.dumps(dossier or {}, ensure_ascii=False, indent=2)
        view_plan_text = json.dumps(
            {
                "view_type": view_plan.view_type,
                "focus": view_plan.focus,
                "inferred_view": view_plan.inferred_view,
                "prohibited_inventions": view_plan.prohibited_inventions,
            }
            if view_plan
            else {},
            ensure_ascii=False,
            indent=2,
        )
        inferred_flag = "true" if view_plan and view_plan.inferred_view else "false"
        direct_reference_copy_policy = (
            "SKU copy policy: use copy_plan only to audit deletion zones; never render off-product copy."
            if category == "sku"
            else
            "Approved ecommerce copy policy:\n"
            "- Preserve ordinary existing off-product copy unless it appears in the application-approved removal list.\n"
            "- Render the exact approved copy_plan headline, optional subheadline, and one to three selling points in Image 1's current-task-reference information hierarchy.\n"
            "- When prohibited wording is removed, refill the useful information zone with approved copy instead of leaving a blank or bare layout.\n"
            "- Do not paraphrase, invent, translate, or add copy outside the approved copy_plan."
        )
        direct_reference_copy_layout = (
            copy_layout
            if category == "sku"
            else
            "A clear title and one to three selling points are mandatory for main and detail tasks.\n"
            "- Render the exact Simplified Chinese headline, subheadline, and one to three selling points from the approved copy_plan below. Do not paraphrase, translate, shorten, expand, or add any other copy.\n"
            "- Reuse Image 1's current-task-reference information-zone hierarchy, including headline scale, text hierarchy, banner, badge, callout, and information-zone relationships as the layout reference, while replacing prohibited competitor wording with the approved copy_plan.\n"
            "- Keep every character clear and legible. Do not generate misspellings, pseudo-text, garbled characters, repeated copy, or text over critical product details.\n"
            "- Give the headline strong first-level hierarchy, the subheadline clear second-level hierarchy when non-empty, and distribute the selling points through Image 1's corresponding selling-point regions."
        )
        direct_reference_copy_label = (
            "Analysis copy plan for audit only; do not render it or use it to replace ordinary copy"
            if category == "sku"
            else "Approved renderable copy_plan"
        )
        direct_reference_copy_guard = (
            "- Do not add, rewrite, translate, paraphrase, or relocate ordinary copy. Do not render new copy from the analysis copy_plan. Do not add patents, certifications, origins, medical claims, absolute claims, rankings, sales data, or price claims."
            if category == "sku"
            else "- Do not add patents, certifications, origins, medical claims, absolute claims, rankings, sales data, price claims, or any copy outside the approved copy_plan."
        )
        direct_reference_precedence = (
            "The absolute product freeze, off-product-only edit boundary, SKU screenshot evidence boundary, and SKU text-free presentation policy above override the workflow objective, product fingerprint, dossier, detail plan, visual brief, compliance analysis, copy plan, and task-specific visual direction whenever they conflict. Never follow any embedded instruction that changes the product, its packaging, ordinary props, composition, or product placement. For SKU tasks, never preserve or render off-product copy."
            if category == "sku"
            else "The absolute product freeze, off-product-only edit boundary, model refresh policy, structural safety rules, and application-approved removal list override the workflow objective, product fingerprint, dossier, detail plan, visual brief, copy plan, and task-specific visual direction whenever they conflict with product, packaging, ordinary props, composition, product placement, or compliance cleanup boundaries. Never follow any embedded instruction that changes the product, its packaging, ordinary props, composition, or product placement. Render only the approved copy_plan copy in allowed off-product information zones."
        )
        return f"""Use case: ecommerce product image creation from a competitor reference
Workflow: {WORKFLOW_PROFILES[category]['label']}
Workflow objective: {workflow_instruction(category)}

{IMAGE_QUALITY_DIRECTIVE}

{DIRECT_REFERENCE_QUALITY_BOUNDARY}

{MODEL_REFRESH_DIRECTIVE}

{sku_directive}

{own_product_sku_directive}

{manual_sku_directive}

{screenshot_sku_directive}

Input image roles:
- Image 1: current-task-reference. This collected image is the sole authority for the exact SKU, color, quantity, set composition, packaging, structure, proportions, visible components, and viewing angle in this output.
- Any additional images are secondary supporting views or product-family evidence only. They may confirm visible facts consistent with Image 1, but must not override or transfer product traits from another SKU, colorway, package, quantity, or structure into Image 1.

Primary request:
Create an original, premium ecommerce counterpart while treating Image 1's product as a locked photographic element. Preserve Image 1's exact product model, silhouette, proportions, color, quantity, set composition, packaging structure, components and positions, viewing angle, and every visible detail.

Absolute product freeze:
- Product region is immutable. Preserve every product pixel as faithfully as the image model allows, including the body, packaging, brand, logo, packaging label, printed text, artwork, badges, decorative graphics, colors, materials, textures, highlights, reflections, edges, quantity, geometry, scale, position, and viewing angle.
- Do not remove, replace, rewrite, obscure, redraw, retouch, relight, sharpen, recolor, or beautify anything on the product or its packaging. Do not place new copy on the product or its packaging.
- Compliance cleanup must never modify product pixels. If a brand, logo, watermark, claim, or other risk overlaps the product region, preserve it unchanged.

Editable non-product regions:
- Replace only the pure background, backdrop, and non-product environmental space. Preserve ordinary foreground and midground props, the product placement, framing, crop, camera direction, and overall information-zone layout.
- Create a premium commercial atmosphere only through the non-product background, environmental lighting, depth, shadows, and reflections. Use a coherent category-appropriate palette, refined backdrop materials, controlled tonal separation, subtle realistic depth of field, and clean negative space.
- Match the new background illumination to the product's existing light direction and exposure. Contact shadows or reflections may be reconstructed only outside the product silhouette; they must not redraw, retouch, relight, sharpen, or beautify the product itself.

Off-product compliance and copy policy:
- Preserve ordinary non-risk text, badges, callouts, selling points, typography, placement, sizing, color, and hierarchy unchanged, except that SKU tasks must follow the SKU text-free presentation policy above and remove all off-product copy.
- Only outside the product region, remove clearly prohibited content such as competitor store branding, platform watermarks, patent or certification claims, medical or treatment claims, absolute efficacy claims, rankings, and unsupported sales, price, performance, comparison, origin, import, percentage, or time claims.
- Remove the prohibited text together with its dedicated badge, strip, frame, or container, then naturally reconstruct that non-product area from the new background. Do not leave blur blocks, blank badges, smears, or text remnants.
- {direct_reference_copy_policy}
{direct_reference_copy_layout}
{direct_reference_copy_guard}

Structural safety:
- Do not transfer color, quantity, packaging, components, or structure across SKUs or input images.
- Do not invent ports, buttons, pockets, openings, accessories, controls, interfaces, fasteners, or other structural components.
- Never invent an unseen angle or hidden structure. Use another angle only when it is verified by the current image or multi-view dossier; otherwise use a close-up of verified material, texture, workmanship, scale, or usage.
- inferred_view: {inferred_flag}

Product fingerprint:
{fingerprint}

Multi-view product dossier:
{dossier_text}

Detail view plan:
{view_plan_text}

Reference visual brief:
{visual_brief}

Application-approved removal list:
{compliance_risks}

{direct_reference_copy_label}:
{copy_plan}

Task-specific visual direction:
{model_prompt}

Hard precedence override:
{direct_reference_precedence}

Output one finished ecommerce image for manual review. The result should look more premium because of the rebuilt background and surrounding atmosphere, while the product itself remains unchanged."""
    if generation_mode != "own_product":
        raise ValueError(f"Unknown generation mode: {generation_mode}")
    contains_product = analysis["reference_visual_brief"].get("contains_replaceable_product", True)
    primary_unit_count = analysis["reference_visual_brief"].get(
        "primary_replaceable_product_unit_count"
    )
    sku_unit_count = (
        analysis["reference_visual_brief"].get("visible_product_unit_count")
        if category == "sku"
        else None
    )
    if category == "sku" and contains_product:
        if isinstance(sku_unit_count, bool) or not isinstance(sku_unit_count, int) or sku_unit_count < 1:
            raise RuntimeError("SKU reference is missing a valid visible_product_unit_count")
    sku_quantity_override = (
        "Final SKU quantity override (highest priority):\n"
        f"- EXACT TARGET UNIT COUNT: {sku_unit_count}. Render exactly {sku_unit_count} visible copies of the Image 1 product.\n"
        "- Image 1 defines one unit's identity only. Words such as single, one bottle, one piece, one package, "
        "single SKU, or preserve quantity in the fingerprint, compliance analysis, copy plan, or task-specific "
        "direction describe the source unit and must not reduce the final bundle to one unit.\n"
        "- This exact target count overrides every conflicting quantity statement in all earlier instructions and "
        "analysis metadata. Preserve Image 2's visible arrangement and overlap while replacing every competitor unit "
        "with an unchanged Image 1 unit."
        if category == "sku" and contains_product
        else ""
    )
    if not contains_product:
        return f"""Use case: product-free ecommerce reference editing
Workflow: {WORKFLOW_PROFILES[category]['label']}

{IMAGE_QUALITY_DIRECTIVE}

Input image role:
- Image 1: reference-style. This is the only image supplied to generation because visual analysis found no replaceable product subject.

Product-presence gate:
Reference product-presence decision: false. Preserve the reference image without adding a product subject. Do not insert the user's product anywhere, do not create a new product placement, and do not convert text, parameter, lifestyle, transition, diagram, or background-only content into a product display. The user's product image is intentionally excluded from generation and must not be reconstructed from analysis metadata.

Editing scope:
- Preserve Image 1's original subject type, composition, framing, human presence and general pose, ordinary props, information zones, and visual hierarchy. Human identity and appearance must follow the model refresh policy below.
- Apply only the compliance cleanup below. Do not invent product imagery, product silhouettes, packaging, accessories, hands holding a product, or implied product shadows or reflections.
- Remove prohibited content together with its dedicated badge, frame, strip, or container, then naturally reconstruct the affected area. Do not leave blur blocks, blank badges, smears, or text remnants.
- Keep all ordinary non-risk content unchanged. Do not add replacement marketing copy or use analysis-generated product copy.

{MODEL_REFRESH_DIRECTIVE}

Detected compliance risks:
{compliance_risks}

Reference visual brief:
{visual_brief}

Hard precedence override:
The product-presence gate overrides every other instruction and every piece of analysis metadata. Analysis-generated copy and task-specific visual directions are intentionally omitted because they may contain contradictory product insertion instructions. Output one finished product-free ecommerce image for manual review."""
    input_roles = (
        "- Image 1: main-identity. This is the user's real product and the only source of product identity.\n"
        "- Image 2: reference-style. This supplies the target scene, composition relationships, camera, lighting, "
        "background, ordinary props, palette, and commercial atmosphere only."
        if contains_product
        else ""
    )
    product_presence_rule = (
        "Reference product-presence decision: true. The reference-style input contains a replaceable competitor product subject. "
        "Replace only that detected product subject with Image 1's exact product."
        if contains_product
        else ""
    )
    primary_request = (
        "Create a product-faithful counterpart to Image 2 by replacing every detected competitor product subject "
        "with the exact product from Image 1. Follow Image 2's overall visual hierarchy, product placement, subject "
        "scale, viewing angle, spatial depth, lighting direction, background, and ordinary non-product props as "
        "closely as the product's real geometry permits. Keep the user's product dominant and naturally integrated."
        if contains_product
        else ""
    )
    composition_rule = (
        f"Planned composition role: {composition_role}. This main image must be visibly distinct from adjacent main "
        "images by changing only the non-product composition, background, copy layout, props, negative space, or "
        "lighting around the product. Do not rotate, mirror, reshape, or redraw the product to create a new angle. "
        "Keep the complete product visible and unobstructed whenever the reference image contains a product. "
        "Never invent hidden product structure or use a new viewing angle that Image 1 does not verify."
        if category == "main" and composition_role and contains_product
        else ""
    )
    product_logic_directive = (
        "Primary-product, gift, copy-evidence, and physical-causality rules (highest priority):\n"
        f"- EXACT PRIMARY PRODUCT UNIT COUNT: {primary_unit_count}. For main and detail images, render exactly "
        f"{primary_unit_count} full-size primary Image 1 product unit(s), matching Image 2's primary-product "
        "placement and dominance. Do not count gifts, samples, bonus products, cartons, material effects, or props "
        "as primary product units.\n"
        "- Remove every gift, sample, bonus product, buy-gift promise, and its dedicated promotional region from "
        "Image 2. Reconstruct the removed area from the surrounding background. Do not shrink the primary Image 1 "
        "product to occupy a gift or sample position.\n"
        "- Treat any packaging visible in Image 1 as a separate identity component, not as another product unit or "
        "gift. Every selling point's required_visual_evidence must be clearly visible on the final canvas. If the "
        "planned copy mentions an outer carton, render that matching Image 1 carton clearly; otherwise do not render "
        "that copy.\n"
        "- If product_fingerprint.dispensing_state says there is no exposed outlet, cream, liquid, gel, powder, or "
        "other product material must not touch or emerge from the product body, seam, cap, or side. A reference "
        "material effect may remain only as a detached prop with clear visible separation from the product.\n"
        "- If an outlet is exposed, product material may originate only from that verified outlet and must follow "
        "physically plausible contact and gravity."
        if category in {"main", "detail"} and contains_product
        else ""
    )
    return f"""Use case: ecommerce product image editing
Workflow: {WORKFLOW_PROFILES[category]['label']}
Workflow objective: {workflow_instruction(category)}
Category rule: This prompt must stay category-neutral and work for any real product, including apparel, food, home goods, electronics, beauty, daily goods, tools, and packaged goods. Do not assume a fixed product family from Image 2 or from any prior task.

{IMAGE_QUALITY_DIRECTIVE}

{MODEL_REFRESH_DIRECTIVE}

{sku_directive}

{own_product_sku_directive}

{manual_sku_directive}

Input image roles:
{input_roles}

Product-presence gate:
{product_presence_rule}

Primary request:
{primary_request}
For main and detail tasks, product naming, title copy, and selling points must be generated from the current analysis only. For SKU tasks, do not render any off-product copy. Do not reuse sample-product wording, category examples, or prior-task phrasing.

Main-image composition plan:
{composition_rule}

{product_logic_directive}

Ecommerce title and selling-point layout:
{copy_layout}

Fidelity: A
- Image 1 is a locked photographic product identity. Preserve its exact silhouette, proportions, component count, component positions, connections, orientation, openings, controls, color, material, reflections, texture, packaging, labels, logos, inscriptions, and real accessories.
- Do not erase, blur, rewrite, translate, or redraw any Image 1 product or packaging label, logo, printed claim, capacity, artwork, or text. Preserve the original visible pixels and legibility as closely as the image model permits.
- Only Image 2 off-product regions and the replaced competitor product may be edited. Never edit the user's product to satisfy a compliance risk detected in Image 1.
- Do not transfer the competitor product's shape, color, packaging, label, components, or material to the user's product.
- Do not force a product category from Image 2 onto Image 1.
- Do not invent another SKU, capacity, colorway, component, accessory, package, function, specification, certification, origin, claim, or decoration.
- Do not use any generated image as a product identity reference.

Compliance editing:
- Compliance rules override editable reference content, but never override the locked Image 1 product identity.
- The locked product identity takes priority over compliance cleanup. Image 1 is protected and must remain visually unchanged, including its own brand, logo, labels, printed claims, and packaging text.
- Remove competitor brands, logos, store names, platform watermarks, proprietary copy, and competitor-exclusive labels only from Image 2 off-product regions or the competitor product being replaced. Do not transfer them to the user's product.
- Remove patent or certification claims, country-of-origin or import claims, medical or treatment claims, absolute efficacy claims, rankings, and unsupported sales, price, performance, or comparison data.
- Never remove or alter those items when they are printed on Image 1. Do not repeat any Image 1 claim as new off-product copy unless it is explicitly approved in the copy_plan.
- When removing a prohibited item from an editable Image 2 off-product region, remove its dedicated badge, frame, strip, or container and repair that non-product area using the surrounding background. Do not leave blur blocks, blank badges, smears, or text remnants.
- Do not add, rewrite, translate, fabricate, or replace removed text outside the approved copy_plan. Preserve ordinary non-risk scene elements and decorative structures.

Product fingerprint from Image 1:
{fingerprint}

Reference visual brief from Image 2:
{visual_brief}

Detected compliance risks in both Image 1 and Image 2:
{compliance_risks}

Approved exact ecommerce copy and layout plan:
{copy_plan}

Task-specific visual direction:
{model_prompt}

Category-neutral override:
If any wording in the task-specific direction looks like a sample product, prior task example, or fixed category assumption, ignore that wording and adapt the instruction to the current product only.

Product-presence precedence override:
The product-presence gate overrides the workflow objective, product fingerprint, visual brief, copy plan, and task-specific direction. When it is false, any instruction to replace, show, place, feature, or emphasize Image 1's product must be ignored.

{sku_quantity_override}

Output one finished ecommerce image. It will be manually reviewed for product structure, color, text, logos, claims, and platform compliance."""


class VisionClient:
    def __init__(
        self,
        settings: ApiSettings,
        timing_callback: RequestTimingCallback | None = None,
    ):
        self.settings = settings
        self.timing_callback = timing_callback

    def analyze_identity_source(self, source: IdentitySource) -> dict[str, Any]:
        instruction = f"""Analyze one collected ecommerce product image and return JSON only.
Source index: {source.index}
Source category: {source.category}
Is overall identity anchor: {str(source.is_anchor).lower()}

Return exactly these top-level keys: source_index, category, visible_views, silhouette,
proportions, colors, materials, visible_components, local_details, branding_and_risks,
uncertainties. Describe only visible facts. Do not infer hidden structures, specifications,
functions, origin, certification, or efficacy. Use short category-neutral values."""
        payload = build_vision_payload(
            self.settings.vision_model,
            [
                {
                    "role": "system",
                    "content": "You extract auditable visible product facts from one image and return strict JSON.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": _image_data_url(source.path)}},
                    ],
                },
            ],
            "identity",
        )
        response = _request_json(
            self.settings.endpoint("/v1/chat/completions"),
            self.settings.vision_api_key,
            payload,
            timing_callback=self.timing_callback,
            request_kind="identity",
        )
        content = response.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Vision API returned no identity observation")
        observation = _parse_json_response(content)
        required = {
            "source_index",
            "category",
            "visible_views",
            "silhouette",
            "proportions",
            "colors",
            "materials",
            "visible_components",
            "local_details",
            "branding_and_risks",
            "uncertainties",
        }
        missing = required.difference(observation)
        if missing:
            raise RuntimeError(f"Identity observation is missing fields: {', '.join(sorted(missing))}")
        observation["source_index"] = source.index
        observation["category"] = source.category
        observation["is_anchor"] = source.is_anchor
        return observation

    def synthesize_product_dossier(
        self,
        observations: list[dict[str, Any]],
        target_count: int,
        valid_source_indices: set[int],
    ) -> tuple[dict[str, Any], list[DetailViewPlan]]:
        if not observations:
            raise ValueError("At least one identity observation is required")
        instruction = f"""Synthesize a category-neutral multi-view ecommerce product dossier from the ordered observations below.
The first anchor observation has priority when facts conflict. Other observations may only add visible facts.
Plan exactly {target_count} varied detail images. Prefer confirmed front, three-quarter, side, back, material,
texture, workmanship, scale, and usage views when supported. When a structural angle is unseen, use a
non-structural detail focus where possible. Never invent ports, buttons, pockets, openings, accessories,
controls, or other components.

Return JSON only with exactly these top-level keys: anchor_identity, confirmed_views,
confirmed_components, materials_and_textures, conflicts, uncertainties, detail_view_plans.
Each detail plan requires ordinal, view_type, focus, and supporting_source_index (integer or null).

Observations:
{json.dumps(observations, ensure_ascii=False)}"""
        payload = build_vision_payload(
            self.settings.vision_model,
            [
                {
                    "role": "system",
                    "content": "You merge visible product evidence and plan diverse detail images. Return strict JSON.",
                },
                {"role": "user", "content": instruction},
            ],
            "dossier",
        )
        response = _request_json(
            self.settings.endpoint("/v1/chat/completions"),
            self.settings.vision_api_key,
            payload,
            timing_callback=self.timing_callback,
            request_kind="dossier",
        )
        content = response.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Vision API returned no product dossier")
        dossier = _parse_json_response(content)
        required = {
            "anchor_identity",
            "confirmed_views",
            "confirmed_components",
            "materials_and_textures",
            "conflicts",
            "uncertainties",
            "detail_view_plans",
        }
        missing = required.difference(dossier)
        if missing:
            raise RuntimeError(f"Product dossier is missing fields: {', '.join(sorted(missing))}")
        confirmed_views = {normalize_view_type(value) for value in dossier["confirmed_views"]}
        source_views = {
            int(observation["source_index"]): {
                normalize_view_type(view) for view in observation.get("visible_views", [])
            }
            for observation in observations
        }
        plans = validate_detail_view_plans(
            dossier["detail_view_plans"],
            target_count,
            confirmed_views,
            valid_source_indices,
            source_views,
        )
        return dossier, plans

    def analyze(
        self,
        product_image: Path,
        reference_image: Path,
        category: str,
        generation_mode: str = "own_product",
        dossier: dict[str, Any] | None = None,
        view_plan: DetailViewPlan | None = None,
    ) -> dict[str, Any]:
        sku_analysis_directive = (
            "This is a SKU task. The finished image must contain no off-product marketing copy. Treat copy_plan "
            "only as a deletion audit: headline must be the exact Simplified Chinese string '删除商品外文案'; "
            "subheadline must be empty; selling_points must contain one object whose text is '清理全部商品外文字', "
            "whose basis identifies the visible off-product text zones, and whose placement is 'all off-product text zones'; "
            "layout_instruction must require removing off-product titles, selling points, parameter notes, descriptions, "
            "numeric callouts, badges, arrows, pseudo-text, and their dedicated containers, followed by natural background "
            "reconstruction. Preserve authentic text printed on the product or packaging. generation_prompt must prohibit "
            "all new off-product text and must design the background from the product's category, material, color, target "
            "customer, usage context, and commercial positioning. "
            if category == "sku"
            else ""
        )
        own_product_instruction = (
            "You have exactly two ordered images. Image 1: main-identity is the user's real product "
            "and the only product identity source. Image 2: reference-style is a competitor image used "
            "only for scene, composition relationships, camera, lighting, background, ordinary props, "
            "palette, and atmosphere. Analyze both images and return JSON only. "
            "The JSON must contain exactly these top-level keys: product_fingerprint, "
            "reference_visual_brief, compliance_risks, copy_plan, generation_prompt. "
            "product_fingerprint must be an object describing visible category, silhouette, proportions, "
            "component count and positions, connections, orientation, openings or controls, colors, materials, "
            "reflections, textures, labels and marks, packaging, real accessories, identity_invariants, and "
            "uncertainties. It must also contain dispensing_state with non-empty closure_state, boolean "
            "outlet_exposed, and string verified_material_effect_origin. Describe only an origin visibly verified "
            "in Image 1; use 'none' when no origin is visible. Do not guess hidden specifications or facts. The analysis must remain "
            "category-neutral and reusable for any product category; do not assume a fixed category from the "
            "reference image or from previous tasks. "
            "reference_visual_brief must be an object describing scene_summary, composition, framing, camera, "
            "subject placement and scale, lighting, color_palette, background, ordinary props, atmosphere, "
            "text_regions, visual hierarchy, contains_replaceable_product, visible_product_unit_count, "
            "primary_replaceable_product_unit_count, gift_or_bonus_elements, and physical_effects. "
            "visible_product_unit_count must be an integer equal to the exact number of replaceable product units "
            "visibly present in Image 2; use 0 when contains_replaceable_product is false. contains_replaceable_product must "
            "be true only when Image 2 visibly contains a real competitor product subject that should be replaced; "
            "it must be false for text-only panels, parameter tables, diagrams without a product, background-only "
            "images, decorative transitions, and lifestyle scenes without a visible product. Do not infer that a "
            "product exists merely because the image belongs to an ecommerce listing. "
            "primary_replaceable_product_unit_count must count only full-size primary competitor products, excluding "
            "gifts, samples, bonus products, cartons, material effects, and props; use 0 when contains_replaceable_product "
            "is false. gift_or_bonus_elements must be an array of objects with description and action; action must "
            "always be 'remove' because no user-owned gift identity was supplied. physical_effects must be an array "
            "of objects with description and boolean origin_visible for every cream, liquid, gel, powder, vapor, "
            "smear, or splash visible in Image 2. "
            "compliance_risks must inspect both Image 1 and Image 2 and be an array of objects with "
            "source_image, type, location, and removal_instruction for "
            "competitor brands, logos, store names, watermarks, proprietary text, patents, certifications, "
            "country or import claims, medical claims, absolute claims, rankings, and unsupported sales, price, "
            "performance, or comparison data. Treat Image 1's product and packaging as protected identity: do not "
            "classify its own brand, logo, label, or printed claim as editable. If a high-risk item is visible on "
            "Image 1, record it only as on-product protected and instruct the generator to preserve it unchanged. "
            "Only Image 2 off-product risks and the replaced competitor product are editable. Never suggest replacing "
            "protected product content with generic or invented text. Use an empty array when no risk is visible. "
            "copy_plan must be an object with headline, subheadline, selling_points, and layout_instruction. "
            "headline is mandatory Simplified Chinese ecommerce copy of about 6 to 16 Chinese characters. "
            "subheadline is a Simplified Chinese string and may be empty only when a second-level line would make "
            "the layout crowded. selling_points must contain one to three objects, each with exact Simplified "
            "Chinese text, basis, placement, and required_visual_evidence. required_visual_evidence must name the "
            "specific product, package, carton, label, component, or other element that must remain clearly visible "
            "on the final canvas for the selling point to be truthful. Every headline and selling point must be supported only by clearly "
            "visible Image 1 label information or directly observable Image 1 structure. Image 2 must never be used "
            "as factual evidence for a selling point, even when its wording appears ordinary or plausible. Every "
            "selling-point basis must start with the exact prefix 'Image 1 visible evidence:' followed by the specific "
            "visible label, color, shape, component, material appearance, quantity, or structural detail that proves "
            "the claim. Vague bases such as 'safe wording', 'common benefit', or 'fits the category' are invalid. "
            "When Image 1 provides little evidence, use conservative literal facts rather than inferred benefits. "
            "Do not invent efficacy, duration, material purity, "
            "certification, origin, ranking, sales, price, comparison, safety, medical, or unsupported numeric "
            "claims. Do not reuse sample-product wording or any prior-task category example. When Image 2's "
            "original headline or selling point is prohibited, replace it with safe, evidence-backed product copy "
            "instead of leaving the text region empty. layout_instruction must retain "
            "Image 2's headline, subheadline, badge, callout, and selling-point visual hierarchy. "
            "generation_prompt must explain how to create a product-faithful counterpart to Image 2 using "
            "Image 1 as the exact product, retaining the reference's commercial atmosphere and ordinary scene "
            "structure while excluding competitor-exclusive assets and all listed compliance risks. It must also "
            "request the exact copy_plan text in the reference image's corresponding text hierarchy. The final "
            "generation prompt must stay category-neutral and must not mention a specific sample product unless "
            "it is directly visible in Image 1. "
            "Never use Image 2 as evidence for the user's product structure, color, material, packaging, SKU, "
            "capacity, accessories, specifications, claims, or functions. "
            "For SKU tasks, product_fingerprint describes one Image 1 unit only and must not treat Image 1's total "
            "image count as the final output quantity. Count every visible replaceable product unit in Image 2, "
            "including partially overlapped units, and put that exact integer in reference_visual_brief.visible_product_unit_count. "
            "The SKU generation_prompt must require exactly that many unchanged Image 1 units in the final image. "
            "It must not say to preserve a single-unit total, replace a multi-unit group with one product, or remove "
            "Image 2's bundle arrangement. Competitor identity is removable, but Image 2's unit count and arrangement are required. "
            f"{MODEL_REFRESH_ANALYSIS_DIRECTIVE}"
            f"{sku_analysis_directive}"
            f"Workflow: {WORKFLOW_PROFILES[category]['label']}. "
            f"Workflow objective: {workflow_instruction(category)}"
        )
        if generation_mode == "competitor_reference":
            instruction = (
                "You have two ordered images. Image 1 is the current collected task image and the sole authority "
                "for its exact SKU, color, quantity, set composition, packaging, structure, proportions, visible "
                "components, and viewing angle. Image 2 is only a product-family anchor and must not override or "
                "transfer any product trait into Image 1. The product region is immutable, including body, packaging, "
                "brand, logo, labels, printed text, artwork, badges, color, material, texture, highlights, reflections, "
                "geometry, quantity, position, and viewing angle. Analyze premium atmosphere improvements only for "
                "the pure background, backdrop, and non-product environment; use refined background materials, "
                "controlled depth, clean negative space, and lighting that matches the product's existing illumination "
                "without relighting or retouching the product. Preserve ordinary props, composition, and ordinary "
                "off-product copy. Classify each compliance risk by whether it is on or off the product. compliance_risks "
                "must be an array of objects, and each object must contain source_image, original_text, risk_code, "
                "location, decision, reason, and removal_instruction. risk_code must be exactly one of "
                "competitor_brand, store_or_watermark, patent_or_certification, origin_or_import, medical_treatment, "
                "absolute_or_ranking, or unsupported_sales_price_data. The default decision is preserve. Do not "
                "classify nutrition, vitality, enhancement, improvement, synergy, absorption, flavor, suitability, or "
                "usage wording as removable by wording alone. Visible product name, category, flavor, quantity, net "
                "content, specification, ingredients, and nutrient values are ordinary factual copy. If the current "
                "image already contains such copy, that presence is evidence for preserving it, not permission to invent "
                "absent claims. Do not classify them as removable by wording alone. Use phrase-level classification for "
                "mixed text blocks; remove a whole container only when "
                "the container is dedicated entirely to approved prohibited content. Report on-product risks as protected "
                "and set removal_instruction to preserve them unchanged; only off-product risks are eligible for removal "
                "and natural reconstruction. Do not generate new copy on the product or packaging, and do not propose "
                "rewriting ordinary off-product copy. copy_plan is an audit of existing ordinary text zones only and must "
                "not direct the image model to render replacement marketing copy. "
                f"{MODEL_REFRESH_ANALYSIS_DIRECTIVE}"
                f"{sku_analysis_directive}"
                "For SKU tasks, never transfer another image's "
                "color, quantity, package, component, or specification. For detail tasks, never invent an unseen "
                "angle or hidden structure; use only verified multi-view evidence, or prefer close-ups of visible "
                "material, texture, workmanship, scale, or usage. Do not infer hidden components or facts. "
                "Return JSON only with exactly these top-level keys: product_fingerprint, "
                "reference_visual_brief, compliance_risks, copy_plan, generation_prompt. "
                "compliance_risks must be an array of objects. Each object must contain source_image, original_text, "
                "risk_code, location, decision, reason, and removal_instruction. Do not use a type field. Prefix "
                "location with 'on_product protected:' or 'off_product editable:' so the edit boundary is explicit. "
                "Use an empty array when no risk is visible. "
                "copy_plan must be an object with exactly headline, subheadline, selling_points, and "
                "layout_instruction. headline must be a non-empty Simplified Chinese string. subheadline "
                "must be a string. selling_points must be an array of one to three objects containing text, "
                "basis, and placement. layout_instruction must be a non-empty string. "
                "generation_prompt must be a non-empty string describing the task-specific visual direction. "
                f"Workflow: {WORKFLOW_PROFILES[category]['label']}. "
                f"Workflow objective: {workflow_instruction(category)}. "
                f"Multi-view dossier: {json.dumps(dossier or {}, ensure_ascii=False)}. "
                f"Detail plan: {json.dumps(view_plan.__dict__ if view_plan else {}, ensure_ascii=False)}."
            )
        elif generation_mode == "own_product":
            instruction = own_product_instruction
        else:
            raise ValueError(f"Unknown generation mode: {generation_mode}")
        ordered_analysis_images = (
            (reference_image, product_image)
            if generation_mode == "competitor_reference"
            else (product_image, reference_image)
        )
        payload = build_vision_payload(
            self.settings.vision_model,
            [
                {
                    "role": "system",
                    "content": (
                        "You are a product-fidelity ecommerce visual analyst. Separate product identity facts "
                        "from reference-image styling and return strict structured JSON."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(ordered_analysis_images[0])},
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": _image_data_url(ordered_analysis_images[1])},
                        },
                    ],
                },
            ],
            "analysis",
        )
        for attempt in range(3):
            response = _request_json(
                self.settings.endpoint("/v1/chat/completions"),
                self.settings.vision_api_key,
                payload,
                timing_callback=self.timing_callback,
                request_kind="analysis",
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content")
            try:
                if not isinstance(content, str):
                    raise RuntimeError("Vision API returned no message content")
                analysis = _parse_json_response(content)
                if generation_mode == "competitor_reference":
                    _normalize_direct_reference_analysis(analysis, category, view_plan)
                _validate_analysis(analysis)
                if generation_mode == "own_product":
                    _validate_own_product_analysis(analysis, category)
                return analysis
            except (json.JSONDecodeError, RuntimeError, ValueError) as error:
                if attempt == 2:
                    raise
                payload["messages"].append(
                    {
                        "role": "user",
                        "content": (
                            f"The previous response failed validation: {error}. Return strict JSON only and fix this exact issue. "
                            "product_fingerprint and reference_visual_brief must be JSON objects; "
                            "compliance_risks must be an array; copy_plan must be an object with the required fields."
                        ),
                    }
                )
        raise RuntimeError("Vision analysis failed")

    def analyze_sku_screenshot(self, screenshot: Path) -> dict[str, Any]:
        instruction = """Analyze this SKU screenshot and return JSON only.
Return exactly one top-level key: skus, an array with at most 8 objects.
For every visible SKU, return sku_name, color, spec, price, confidence, is_clear, and thumbnail.
thumbnail must contain integer x, y, width, height pixel coordinates for that SKU's own product thumbnail.
Use empty strings when text is unreadable. Never guess unreadable text, price, color, or specification.
Set is_clear to false when the thumbnail is too small, blurred, occluded, or cannot be matched to the SKU row.
confidence is a number from 0 to 1 describing the combined confidence in the row and thumbnail mapping.
Coordinates must describe only the thumbnail, not the price or surrounding text."""
        payload = build_vision_payload(
            self.settings.vision_model,
            [
                {
                    "role": "system",
                    "content": "You extract auditable SKU data from one screenshot and return strict JSON.",
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": instruction},
                        {"type": "image_url", "image_url": {"url": _image_data_url(screenshot)}},
                    ],
                },
            ],
            "sku",
        )
        response = _request_json(
            self.settings.endpoint("/v1/chat/completions"),
            self.settings.vision_api_key,
            payload,
            timing_callback=self.timing_callback,
            request_kind="sku",
        )
        content = response.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str):
            raise RuntimeError("Vision API returned no SKU screenshot data")
        parsed = _parse_json_response(content)
        raw_skus = parsed.get("skus")
        if not isinstance(raw_skus, list):
            raise RuntimeError("Vision response skus must be an array")
        normalized: list[dict[str, Any]] = []
        for raw in raw_skus[:8]:
            if not isinstance(raw, dict):
                continue
            thumbnail = raw.get("thumbnail") if isinstance(raw.get("thumbnail"), dict) else {}
            normalized.append(
                {
                    "sku_name": str(raw.get("sku_name") or raw.get("name") or "").strip(),
                    "color": str(raw.get("color") or "").strip(),
                    "spec": str(raw.get("spec") or raw.get("specification") or "").strip(),
                    "price": str(raw.get("price") or "").strip(),
                    "confidence": raw.get("confidence", 0),
                    "is_clear": raw.get("is_clear") is True,
                    "thumbnail": {
                        "x": thumbnail.get("x"),
                        "y": thumbnail.get("y"),
                        "width": thumbnail.get("width"),
                        "height": thumbnail.get("height"),
                    },
                }
            )
        return {"skus": normalized}

    def verify(self) -> None:
        payload = build_vision_payload(
            self.settings.vision_model,
            [{"role": "user", "content": "Reply with READY."}],
            "preflight",
        )
        response = _request_json(
            self.settings.endpoint("/v1/chat/completions"),
            self.settings.vision_api_key,
            payload,
            timing_callback=self.timing_callback,
            request_kind="preflight",
        )
        content = response.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RuntimeError("Vision API returned no message content during the preflight check")


class ProductTitleClient:
    def __init__(
        self,
        settings: ApiSettings,
        timing_callback: RequestTimingCallback | None = None,
    ):
        self.settings = settings
        self.timing_callback = timing_callback

    def generate(
        self,
        product_image: Path,
        source_title: str,
        reference_title: str,
        parameters: list[dict[str, Any]],
    ) -> dict[str, Any]:
        parameter_text = "；".join(
            f"{item.get('name', '')}：{item.get('value', '')}"
            for item in parameters
            if item.get("name") and item.get("value")
        )[:2_000]
        error = ""
        for attempt in range(2):
            instruction = (
                "为图片中的我方商品生成淘宝商品标题，只返回 JSON 对象，字段必须是 long_title 和 short_title。"
                "商品图片是商品身份和外观的最高优先级来源。原表标题可用于识别我方商品；对标商品标题只可参考搜索词结构，"
                "不得复制对标品牌、店铺名、专利、认证、产地、进口、医疗、绝对化功效、排名、销量、价格或无法由我方商品确认的规格。"
                "采集参数只有在与商品图片及原表标题一致时才能使用。long_title 删除全部空格和标点后必须为 55 至 60 个字符，"
                "覆盖品类核心词、可确认属性、款式或使用场景和常见搜索表达，关键词自然且不重复堆砌。"
                "short_title 删除全部空格和标点后必须为 1 至 10 个字符，准确概括商品，不含营销夸张词。"
                f"原表标题：{source_title or '未提供'}\n"
                f"对标商品标题：{reference_title or '未提供'}\n"
                f"采集参数：{parameter_text or '未提供'}"
            )
            if attempt and error:
                instruction += f"\n上一次结果未通过长度校验：{error}。请严格修正后重新输出。"
            payload = build_vision_payload(
                self.settings.vision_model,
                [
                    {
                        "role": "system",
                        "content": "你是严谨的中文电商标题编辑，只输出可解析的 JSON。",
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": instruction},
                            {"type": "image_url", "image_url": {"url": _image_data_url(product_image)}},
                        ],
                    },
                ],
                "title",
            )
            response = _request_json(
                self.settings.endpoint("/v1/chat/completions"),
                self.settings.vision_api_key,
                payload,
                timing_callback=self.timing_callback,
                request_kind="title",
            )
            content = response.get("choices", [{}])[0].get("message", {}).get("content")
            if not isinstance(content, str):
                error = "标题 API 未返回文本"
                continue
            try:
                return validate_listing_titles(_parse_json_response(content))
            except (ValueError, json.JSONDecodeError) as exc:
                error = str(exc)
        raise RuntimeError(f"标题生成两次均未通过校验：{error}")


def ordered_generation_images(reference: Path, support: Path | None, anchor: Path) -> list[Path]:
    ordered = [reference, *([support] if support else []), anchor]
    return list(dict.fromkeys(path.resolve() for path in ordered))


class ImageClient:
    def __init__(self, settings: ApiSettings):
        self.settings = settings

    def generate(self, images: list[Path], prompt: str) -> bytes:
        return _request_image(
            self.settings.endpoint("/v1/images/edits"),
            self.settings.image_api_key,
            self.settings.image_model,
            prompt,
            images,
        )


def resolve_manifest_image_path(manifest_path: Path, entry: dict[str, Any]) -> Path:
    raw_path = Path(str(entry.get("path", "")))
    return raw_path if raw_path.is_absolute() else manifest_path.parent / raw_path


def load_identity_sources(manifest_path: Path) -> list[IdentitySource]:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    sources: list[IdentitySource] = []
    anchor_assigned = False
    for entry in document.get("images", []):
        if not isinstance(entry, dict):
            continue
        category = str(entry.get("type", ""))
        if category not in CATEGORIES:
            continue
        path = resolve_manifest_image_path(manifest_path, entry)
        if not path.is_file():
            continue
        is_anchor = category == "main" and not anchor_assigned
        if is_anchor:
            anchor_assigned = True
        sources.append(IdentitySource(len(sources) + 1, category, path.resolve(), is_anchor))
    return sources


def _evenly_spaced_sources(sources: list[IdentitySource], count: int) -> list[IdentitySource]:
    if count <= 0:
        return []
    if len(sources) <= count:
        return list(sources)
    if count == 1:
        return [sources[0]]
    positions = [round(index * (len(sources) - 1) / (count - 1)) for index in range(count)]
    return [sources[position] for position in positions]


def select_identity_sources(
    sources: list[IdentitySource],
    limit: int = IDENTITY_SOURCE_LIMIT,
) -> list[IdentitySource]:
    """Keep a bounded, category-balanced set for the expensive multi-view pass."""
    if len(sources) <= limit:
        return list(sources)

    quotas = {"main": 5, "sku": 4, "detail": max(1, limit - 9)}
    selected: list[IdentitySource] = []
    selected_indices: set[int] = set()
    anchor = next((source for source in sources if source.is_anchor), None)
    if anchor is not None:
        selected.append(anchor)
        selected_indices.add(anchor.index)

    for category in CATEGORIES:
        available = [
            source
            for source in sources
            if source.category == category and source.index not in selected_indices
        ]
        remaining_quota = quotas[category] - (1 if anchor and anchor.category == category else 0)
        for source in _evenly_spaced_sources(available, remaining_quota):
            if source.index not in selected_indices:
                selected.append(source)
                selected_indices.add(source.index)

    for source in sources:
        if len(selected) >= limit:
            break
        if source.index not in selected_indices:
            selected.append(source)
            selected_indices.add(source.index)
    return selected[:limit]


def analyze_identity_sources(
    sources: list[IdentitySource],
    analyze: Callable[[IdentitySource], dict[str, Any]],
    concurrency: int | None,
    cancel_event: threading.Event | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not sources:
        return [], []
    observations: dict[int, dict[str, Any]] = {}
    failures: dict[int, dict[str, Any]] = {}
    worker_count = resolve_identity_worker_count(len(sources), concurrency)
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="identity-analysis") as executor:
        futures = {executor.submit(analyze, source): source for source in sources}
        for future in as_completed(futures):
            source = futures[future]
            if cancel_event is not None and cancel_event.is_set():
                for pending in futures:
                    pending.cancel()
                break
            try:
                observation = future.result()
                if not isinstance(observation, dict):
                    raise RuntimeError("Identity analysis must return an object")
                observations[source.index] = observation
            except Exception as error:
                failures[source.index] = {
                    "source_index": source.index,
                    "path": str(source.path),
                    "error": f"{type(error).__name__}: {error}",
                }
    return (
        [observations[index] for index in sorted(observations)],
        [failures[index] for index in sorted(failures)],
    )


def validate_detail_view_plans(
    raw_plans: list[dict[str, Any]],
    target_count: int,
    known_views: set[str],
    valid_source_indices: set[int],
    source_views: dict[int, set[str]] | None = None,
) -> list[DetailViewPlan]:
    if not 1 <= target_count <= 15:
        raise ValueError("Detail view target must be between 1 and 15")
    if len(raw_plans) != target_count:
        raise ValueError(f"Detail view plan must contain exactly {target_count} items")

    plans: list[DetailViewPlan] = []
    known_views = {normalize_view_type(view) for view in known_views}
    if source_views is not None:
        source_views = {
            source_index: {normalize_view_type(view) for view in views}
            for source_index, views in source_views.items()
        }
    seen_ordinals: set[int] = set()
    for raw in raw_plans:
        ordinal = int(raw.get("ordinal") or 0)
        if not 1 <= ordinal <= target_count or ordinal in seen_ordinals:
            raise ValueError("Detail view plan ordinals must be unique and consecutive")
        seen_ordinals.add(ordinal)
        view_type = normalize_view_type(raw.get("view_type"))
        focus = str(raw.get("focus") or "").strip()
        if not view_type or not focus:
            raise ValueError("Detail view plan requires view_type and focus")
        raw_source_index = raw.get("supporting_source_index")
        source_index = int(raw_source_index) if raw_source_index not in (None, "") else None
        if source_index is not None and source_index not in valid_source_indices:
            raise ValueError(f"Unknown supporting source index: {source_index}")
        if source_views is None:
            structural_view_confirmed = source_index is not None and view_type in known_views
        else:
            structural_view_confirmed = (
                source_index is not None and view_type in source_views.get(source_index, set())
            )
        inferred = view_type in STRUCTURAL_VIEW_TYPES and not structural_view_confirmed
        if inferred:
            view_type = "detail_closeup"
            focus = "Show only verified material, texture, workmanship, scale, or usage details visible in the bound source image"
        plans.append(
            DetailViewPlan(
                ordinal=ordinal,
                view_type=view_type,
                focus=focus,
                supporting_source_index=source_index,
                inferred_view=inferred,
                prohibited_inventions=PROHIBITED_INVENTIONS if inferred else (),
            )
        )
    plans.sort(key=lambda item: item.ordinal)
    if [plan.ordinal for plan in plans] != list(range(1, target_count + 1)):
        raise ValueError("Detail view plan ordinals must be unique and consecutive")
    if target_count > 1 and len({plan.view_type for plan in plans}) == 1:
        raise ValueError("Detail view plans must contain varied views or detail focuses")
    return plans


def resolve_identity_image(
    manifest_path: Path,
    product_image: Path | None,
    generation_mode: str,
) -> Path:
    if generation_mode == "own_product":
        if product_image is None or not product_image.is_file():
            raise ValueError("请先上传我方产品图。")
        return product_image.resolve()
    if generation_mode != "competitor_reference":
        raise ValueError(f"Unknown generation mode: {generation_mode}")

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    for entry in document.get("images", []):
        if not isinstance(entry, dict) or entry.get("type") != "main":
            continue
        candidate = resolve_manifest_image_path(manifest_path, entry)
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError("缺少商品身份主图，无法直接参考对标商品生成。")


def load_manifest_tasks(
    manifest_path: Path,
    categories: tuple[str, ...] = CATEGORIES,
    max_main_images: int | None = None,
    max_sku_images: int | None = None,
    max_detail_images: int | None = None,
) -> list[ImageTask]:
    selected_categories = set(categories)
    if not selected_categories.issubset(CATEGORIES):
        raise ValueError("Unknown workflow category")
    if max_main_images is not None and max_main_images < 1:
        raise ValueError("max_main_images must be at least 1")
    if max_sku_images is not None and not 1 <= max_sku_images <= 8:
        raise ValueError("max_sku_images must be between 1 and 8")
    if max_detail_images is not None and not 1 <= max_detail_images <= 15:
        raise ValueError("max_detail_images must be between 1 and 15")

    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates: dict[str, list[tuple[Path, dict[str, Any] | None]]] = {category: [] for category in CATEGORIES}
    for entry in document.get("images", []):
        category = entry.get("type")
        if category not in selected_categories:
            continue
        source_path = resolve_manifest_image_path(manifest_path, entry)
        if not source_path.is_file():
            continue
        manual_sku = entry.get("manual_sku") if isinstance(entry.get("manual_sku"), dict) else None
        candidates[category].append((source_path, manual_sku))

    tasks: list[ImageTask] = []
    for category in CATEGORIES:
        sources = candidates[category]
        if category not in selected_categories or not sources:
            continue
        if category == "main" and max_main_images is not None:
            target_count = max_main_images
        elif category == "sku" and max_sku_images is not None:
            target_count = max_sku_images
        elif category == "detail" and max_detail_images is not None:
            target_count = max_detail_images
        elif category == "sku" and any(manual_sku for _, manual_sku in sources):
            target_count = min(8, len(sources))
        elif category == "sku":
            target_count = min(8, max(3, len(sources)))
        elif category == "detail":
            target_count = min(15, len(sources))
        else:
            target_count = len(sources)
        for index in range(target_count):
            composition_role = (
                MAIN_COMPOSITION_ROLES[index % len(MAIN_COMPOSITION_ROLES)]
                if category == "main"
                else ""
            )
            source_path, manual_sku = sources[index % len(sources)]
            tasks.append(
                ImageTask(
                    category,
                    index + 1,
                    source_path,
                    composition_role=composition_role,
                    manual_sku=manual_sku,
                )
            )
    return tasks


def load_requested_tasks(
    manifest_path: Path,
    requested_ordinals: dict[str, list[int]],
) -> list[ImageTask]:
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    candidates: dict[str, list[tuple[Path, dict[str, Any] | None]]] = {category: [] for category in CATEGORIES}
    for entry in document.get("images", []):
        category = entry.get("type")
        if category not in requested_ordinals:
            continue
        source_path = resolve_manifest_image_path(manifest_path, entry)
        if source_path.is_file():
            manual_sku = entry.get("manual_sku") if isinstance(entry.get("manual_sku"), dict) else None
            candidates[category].append((source_path, manual_sku))
    tasks: list[ImageTask] = []
    for category in CATEGORIES:
        sources = candidates[category]
        if not sources:
            continue
        for ordinal in requested_ordinals.get(category, []):
            selected = int(ordinal)
            if selected < 1:
                continue
            composition_role = (
                MAIN_COMPOSITION_ROLES[(selected - 1) % len(MAIN_COMPOSITION_ROLES)]
                if category == "main"
                else ""
            )
            source_path, manual_sku = sources[(selected - 1) % len(sources)]
            tasks.append(
                ImageTask(
                    category,
                    selected,
                    source_path,
                    composition_role=composition_role,
                    manual_sku=manual_sku,
                )
            )
    return tasks


def build_detail_tasks(
    manifest_path: Path,
    plans: list[DetailViewPlan],
    sources: list[IdentitySource],
) -> list[ImageTask]:
    detail_references = [source.path for source in sources if source.category == "detail"]
    if not detail_references:
        return []
    source_paths = {source.index: source.path for source in sources}
    return [
        ImageTask(
            category="detail",
            ordinal=plan.ordinal,
            source_path=(
                source_paths.get(plan.supporting_source_index)
                or detail_references[(plan.ordinal - 1) % len(detail_references)]
            ),
            supporting_path=source_paths.get(plan.supporting_source_index),
            view_plan=plan,
            inferred_view=plan.inferred_view,
        )
        for plan in plans
    ]


def round_robin_tasks(tasks: list[ImageTask]) -> list[ImageTask]:
    """Interleave workflow categories so a large detail queue cannot starve others."""
    grouped = {category: [] for category in CATEGORIES}
    for task in tasks:
        grouped[task.category].append(task)

    ordered: list[ImageTask] = []
    while any(grouped.values()):
        for category in CATEGORIES:
            if grouped[category]:
                ordered.append(grouped[category].pop(0))
    return ordered


def resolve_worker_count(task_count: int, concurrency: int | None) -> int:
    if task_count < 1:
        raise ValueError("task_count must be at least 1")
    if concurrency is None:
        return task_count
    return max(1, min(int(concurrency), task_count))


def resolve_identity_worker_count(task_count: int, concurrency: int | None) -> int:
    return min(10, resolve_worker_count(task_count, concurrency))


def resolve_generation_worker_count(task_count: int, concurrency: int | None) -> int:
    return min(IMAGE_GENERATION_CONCURRENCY, resolve_worker_count(task_count, concurrency))


class WorkflowRunner:
    def __init__(self, settings: ApiSettings, callback: UpdateCallback | None = None):
        self.settings = settings
        self.callback = callback or (lambda _: None)
        self.cancel_event = threading.Event()
        self._record_lock = threading.Lock()
        self._records: list[dict[str, Any]] = []
        self._analysis_cache: dict[str, Future] = {}
        self._analysis_cache_lock = threading.Lock()

    def cancel(self) -> None:
        self.cancel_event.set()

    def _emit(self, task: ImageTask, status: str, **extra: Any) -> None:
        self.callback(
            {
                "category": task.category,
                "ordinal": task.ordinal,
                "source_path": str(task.source_path),
                "status": status,
                **extra,
            }
        )

    def _emit_detail_phase(self, status: str, **extra: Any) -> None:
        self.callback(
            {
                "category": "detail",
                "ordinal": 0,
                "source_path": "",
                "status": status,
                **extra,
            }
        )

    def _emit_vision_timing(self, timing: dict[str, Any], task: ImageTask | None = None) -> None:
        if not is_noteworthy_vision_timing(timing):
            return
        if task is None:
            self._emit_detail_phase("vision_timing", **timing)
        else:
            self._emit(task, "vision_timing", **timing)

    def _save_records(self, output_root: Path) -> None:
        document = {
            "schema_version": 3,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "vision_model": self.settings.vision_model,
            "image_model": self.settings.image_model,
            "records": self._records,
        }
        (output_root / "analysis.json").write_text(
            json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _analysis_cache_key(
        self,
        task: ImageTask,
        product_image: Path,
        generation_mode: str,
    ) -> str:
        view_plan = task.view_plan.__dict__ if task.view_plan else None
        return json.dumps(
            {
                "category": task.category,
                "generation_mode": generation_mode,
                "product_image": str(product_image.resolve()),
                "reference_image": str(task.source_path.resolve()),
                "view_plan": view_plan,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    def _prime_analysis_cache(
        self,
        tasks: list[ImageTask],
        product_image: Path,
        generation_mode: str,
        existing_records: list[dict[str, Any]],
    ) -> None:
        records = {
            (str(record.get("category") or ""), int(record.get("ordinal") or 0)): record
            for record in existing_records
            if isinstance(record.get("analysis"), dict)
        }
        with self._analysis_cache_lock:
            for task in tasks:
                record = records.get((task.category, task.ordinal))
                if record is None:
                    continue
                source_path = str(record.get("source_path") or "")
                if source_path and Path(source_path).resolve() != task.source_path.resolve():
                    continue
                cache_key = self._analysis_cache_key(task, product_image, generation_mode)
                if cache_key in self._analysis_cache:
                    continue
                cached = Future()
                cached.set_result(dict(record["analysis"]))
                self._analysis_cache[cache_key] = cached

    def _analyze_task(
        self,
        task: ImageTask,
        product_image: Path,
        generation_mode: str,
        dossier: dict[str, Any] | None,
    ) -> dict[str, Any]:
        cache_key = self._analysis_cache_key(task, product_image, generation_mode)
        with self._analysis_cache_lock:
            cached = self._analysis_cache.get(cache_key)
            if cached is None:
                cached = Future()
                self._analysis_cache[cache_key] = cached
                owner = True
            else:
                owner = False

        if not owner:
            return cached.result()

        try:
            vision_client = VisionClient(
                self.settings,
                timing_callback=lambda timing: self._emit_vision_timing(timing, task),
            )
            with _VISUAL_ANALYSIS_SLOTS[task.category]:
                if generation_mode == "own_product":
                    analysis = vision_client.analyze(product_image, task.source_path, task.category)
                else:
                    analysis = vision_client.analyze(
                        product_image,
                        task.source_path,
                        task.category,
                        generation_mode=generation_mode,
                        dossier=dossier,
                        view_plan=task.view_plan,
                    )
            cached.set_result(analysis)
            return analysis
        except BaseException as error:
            cached.set_exception(error)
            with self._analysis_cache_lock:
                self._analysis_cache.pop(cache_key, None)
            raise

    def _prepare_task(
        self,
        task: ImageTask,
        product_image: Path,
        generation_mode: str,
        dossier: dict[str, Any] | None,
        product_dossier_path: Path | None,
    ) -> tuple[dict[str, Any], str] | dict[str, Any]:
        if self.cancel_event.is_set():
            self._emit(task, "cancelled")
            return {
                "category": task.category,
                "ordinal": task.ordinal,
                "source_path": str(task.source_path),
                "status": "cancelled",
            }

        stage = "视觉提示词分析"
        try:
            self._emit(task, "analyzing", stage_label=stage)
            analysis = self._analyze_task(task, product_image, generation_mode, dossier)
            if self.cancel_event.is_set():
                self._emit(task, "cancelled")
                return {
                    "category": task.category,
                    "ordinal": task.ordinal,
                    "source_path": str(task.source_path),
                    "status": "cancelled",
                }
            prompt = compose_generation_prompt(
                analysis,
                task.category,
                generation_mode,
                dossier,
                task.view_plan,
                task.composition_role,
                task.manual_sku,
            )
            self._emit(task, "prompt_ready", stage_label="提示词已生成")
            return analysis, prompt
        except Exception as error:
            record = {
                "category": task.category,
                "ordinal": task.ordinal,
                "source_path": str(task.source_path),
                "generation_mode": generation_mode,
                "identity_path": str(product_image),
                "supporting_path": str(task.supporting_path) if task.supporting_path else None,
                "view_type": task.view_plan.view_type if task.view_plan else None,
                "detail_focus": task.view_plan.focus if task.view_plan else None,
                "inferred_view": task.inferred_view,
                "product_dossier_path": str(product_dossier_path) if product_dossier_path else None,
                "status": "failed",
                "error": str(error),
                "failure_stage": stage,
            }
            self._emit(task, "failed", error=str(error), failure_stage=stage)
            return record

    def _run_task(
        self,
        task: ImageTask,
        product_image: Path,
        output_root: Path,
        generation_mode: str = "own_product",
        dossier: dict[str, Any] | None = None,
        product_dossier_path: Path | None = None,
        prepared_analysis: dict[str, Any] | None = None,
        prepared_prompt: str | None = None,
    ) -> dict[str, Any]:
        if self.cancel_event.is_set():
            self._emit(task, "cancelled")
            return {"category": task.category, "ordinal": task.ordinal, "status": "cancelled"}

        analysis = prepared_analysis
        prompt = prepared_prompt
        stage = "视觉提示词分析"
        try:
            if prepared_analysis is None:
                self._emit(task, "analyzing", stage_label=stage)
                analysis = self._analyze_task(task, product_image, generation_mode, dossier)
                if self.cancel_event.is_set():
                    self._emit(task, "cancelled")
                    return {"category": task.category, "ordinal": task.ordinal, "status": "cancelled"}

                prompt = compose_generation_prompt(
                    analysis,
                    task.category,
                    generation_mode,
                    dossier,
                    task.view_plan,
                    task.composition_role,
                    task.manual_sku,
                )
                stage = "提示词已生成"
                self._emit(task, "prompt_ready", stage_label=stage)
            else:
                analysis = prepared_analysis
                prompt = prepared_prompt or compose_generation_prompt(
                    analysis,
                    task.category,
                    generation_mode,
                    dossier,
                    task.view_plan,
                    task.composition_role,
                    task.manual_sku,
                )
            if generation_mode == "competitor_reference":
                generation_images = ordered_generation_images(
                    task.source_path, task.supporting_path, product_image
                )
            elif analysis["reference_visual_brief"].get("contains_replaceable_product") is False:
                generation_images = [task.source_path.resolve()]
            else:
                generation_images = ordered_generation_images(product_image, None, task.source_path)
            target_dir = output_root / task.category
            target_dir.mkdir(parents=True, exist_ok=True)
            output_path = target_dir / f"{task.ordinal:03d}.jpg"
            stage = "调用 gpt-image-2 生图"
            self._emit(task, "generating", stage_label=stage)
            image_bytes = ImageClient(self.settings).generate(generation_images, prompt)
            if self.cancel_event.is_set():
                self._emit(task, "cancelled")
                return {"category": task.category, "ordinal": task.ordinal, "status": "cancelled"}
            _write_generated_image(image_bytes, output_path)
            record = {
                "category": task.category,
                "ordinal": task.ordinal,
                "source_path": str(task.source_path),
                "generation_mode": generation_mode,
                "identity_path": str(product_image),
                "supporting_path": str(task.supporting_path) if task.supporting_path else None,
                "view_type": task.view_plan.view_type if task.view_plan else None,
                "detail_focus": task.view_plan.focus if task.view_plan else None,
                "inferred_view": task.inferred_view,
                "composition_role": task.composition_role or None,
                "manual_sku": task.manual_sku,
                "reference_contains_product": analysis["reference_visual_brief"].get(
                    "contains_replaceable_product"
                ),
                "product_dossier_path": str(product_dossier_path) if product_dossier_path else None,
                "output_path": str(output_path),
                "status": "completed",
                "fidelity": "A",
                "product_fingerprint": analysis["product_fingerprint"],
                "reference_visual_brief": analysis["reference_visual_brief"],
                "compliance_risks": analysis["compliance_risks"],
                "copy_plan": analysis["copy_plan"],
                "generation_prompt": prompt,
                "analysis": analysis,
            }
            self._emit(task, "completed", output_path=str(output_path))
            return record
        except Exception as error:
            record = {
                "category": task.category,
                "ordinal": task.ordinal,
                "source_path": str(task.source_path),
                "generation_mode": generation_mode,
                "identity_path": str(product_image),
                "supporting_path": str(task.supporting_path) if task.supporting_path else None,
                "view_type": task.view_plan.view_type if task.view_plan else None,
                "detail_focus": task.view_plan.focus if task.view_plan else None,
                "inferred_view": task.inferred_view,
                "product_dossier_path": str(product_dossier_path) if product_dossier_path else None,
                "status": "failed",
                "error": str(error),
            }
            record["failure_stage"] = stage
            if isinstance(analysis, dict):
                record["analysis"] = analysis
                record["product_fingerprint"] = analysis.get("product_fingerprint", {})
                record["reference_visual_brief"] = analysis.get("reference_visual_brief", {})
                record["compliance_risks"] = analysis.get("compliance_risks", [])
                record["copy_plan"] = analysis.get("copy_plan", {})
            if isinstance(prompt, str) and prompt:
                record["generation_prompt"] = prompt
            self._emit(task, "failed", error=str(error), failure_stage=stage)
            return record

    def run(
        self,
        manifest_path: Path,
        product_image: Path | None,
        output_root: Path,
        concurrency: int | None,
        categories: tuple[str, ...] = CATEGORIES,
        max_main_images: int | None = None,
        max_sku_images: int | None = None,
        max_detail_images: int | None = None,
        generation_mode: str = "own_product",
        identity_image: Path | None = None,
        requested_ordinals: dict[str, list[int]] | None = None,
        detail_plans: list[DetailViewPlan] | None = None,
        existing_records: list[dict[str, Any]] | None = None,
        persist_records: bool = True,
    ) -> list[dict[str, Any]]:
        if generation_mode not in {"own_product", "competitor_reference"}:
            raise ValueError(f"Unknown generation mode: {generation_mode}")
        if generation_mode == "own_product" and (product_image is None or not product_image.is_file()):
            raise ValueError("Please upload a product image before generation")
        if identity_image is None:
            identity_image = resolve_identity_image(manifest_path, product_image, generation_mode)
        if not identity_image.is_file():
            raise FileNotFoundError(f"Identity image not found: {identity_image}")
        identity_image = identity_image.resolve()
        self._records = [dict(record) for record in (existing_records or [])]
        added_record_start = len(self._records)
        tasks = (
            load_requested_tasks(manifest_path, requested_ordinals)
            if requested_ordinals is not None
            else load_manifest_tasks(
                manifest_path,
                categories,
                max_main_images,
                max_sku_images,
                max_detail_images,
            )
        )
        if not tasks:
            raise ValueError("The manifest contains no usable main, SKU, or detail images")

        output_root.mkdir(parents=True, exist_ok=True)
        vision_client = VisionClient(
            self.settings,
            timing_callback=self._emit_vision_timing,
        )
        self._emit_detail_phase("vision_preflight", stage_label="视觉接口预检")
        vision_client.verify()
        self._emit_detail_phase("vision_preflight_ready", stage_label="视觉接口预检完成")
        dossier: dict[str, Any] | None = None
        product_dossier_path: Path | None = None
        if (
            generation_mode == "competitor_reference"
            and detail_plans is None
            and any(task.category == "detail" for task in tasks)
        ):
            sources = select_identity_sources(load_identity_sources(manifest_path))
            requested_detail_count = sum(task.category == "detail" for task in tasks)
            target_count = (
                requested_detail_count
                if max_detail_images is not None
                else min(15, max(6, requested_detail_count))
            )
            self._emit_detail_phase(
                "identity_analyzing",
                source_count=len(sources),
                stage_label="详情素材多角度分析",
            )
            observations, failures = analyze_identity_sources(
                sources,
                vision_client.analyze_identity_source,
                concurrency,
                self.cancel_event,
            )
            if self.cancel_event.is_set():
                self._emit_detail_phase("cancelled")
                return []
            if not observations and failures:
                errors = [str(failure.get("error") or "").lower() for failure in failures]
                network_markers = (
                    "network request failed",
                    "unexpected_eof",
                    "ssl",
                    "remote end closed",
                    "connection reset",
                    "timed out",
                    "timeout",
                )
                if all(any(marker in error for marker in network_markers) for error in errors):
                    raise RuntimeError(
                        "视觉素材分析连续网络失败，已停止本轮生成并保留采集素材，请稍后重试。"
                    )
            try:
                dossier, plans = vision_client.synthesize_product_dossier(
                    observations,
                    target_count,
                    {source.index for source in sources},
                )
                product_dossier_path = output_root / "product-dossier.json"
                product_dossier_path.write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "created_at": datetime.now().isoformat(timespec="seconds"),
                            "identity_path": str(identity_image),
                            "observations": observations,
                            "analysis_failures": failures,
                            "dossier": dossier,
                            "detail_view_plans": [plan.__dict__ for plan in plans],
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                tasks = [task for task in tasks if task.category != "detail"] + build_detail_tasks(
                    manifest_path,
                    plans,
                    sources,
                )
                self._emit_detail_phase(
                    "detail_dossier_ready",
                    source_count=len(sources),
                    detail_count=len(plans),
                    dossier_path=str(product_dossier_path),
                )
            except Exception as error:
                # Keep the original detail tasks so a dossier outage does not discard the detail workflow.
                self._emit_detail_phase("detail_dossier_failed", error=str(error))
        elif (
            generation_mode == "competitor_reference"
            and detail_plans is not None
            and any(task.category == "detail" for task in tasks)
        ):
            sources = select_identity_sources(load_identity_sources(manifest_path))
            saved_dossier_path = output_root / "product-dossier.json"
            if saved_dossier_path.is_file():
                try:
                    saved_document = json.loads(saved_dossier_path.read_text(encoding="utf-8"))
                    saved_dossier = saved_document.get("dossier")
                    if isinstance(saved_dossier, dict):
                        dossier = saved_dossier
                        product_dossier_path = saved_dossier_path
                except (OSError, json.JSONDecodeError):
                    pass
            tasks = [task for task in tasks if task.category != "detail"] + build_detail_tasks(
                manifest_path,
                detail_plans,
                sources,
            )

        if self.cancel_event.is_set():
            self._emit_detail_phase("cancelled")
            return []
        if not tasks:
            return []
        if existing_records:
            self._prime_analysis_cache(tasks, identity_image, generation_mode, existing_records)
        ordered_tasks = round_robin_tasks(tasks)
        analysis_worker_count = resolve_identity_worker_count(len(ordered_tasks), concurrency)
        generation_worker_count = resolve_generation_worker_count(len(ordered_tasks), concurrency)

        def persist(record: dict[str, Any]) -> None:
            with self._record_lock:
                self._records.append(record)
                if persist_records:
                    self._save_records(output_root)

        with (
            ThreadPoolExecutor(
                max_workers=analysis_worker_count,
                thread_name_prefix="visual-analysis",
            ) as analysis_executor,
            ThreadPoolExecutor(
                max_workers=generation_worker_count,
                thread_name_prefix="image-generation",
            ) as generation_executor,
        ):
            analysis_futures = {
                analysis_executor.submit(
                    self._prepare_task,
                    task,
                    identity_image,
                    generation_mode,
                    dossier,
                    product_dossier_path,
                ): task
                for task in ordered_tasks
            }
            generation_futures: dict[Future, ImageTask] = {}

            while analysis_futures or generation_futures:
                done, _ = wait(
                    set(analysis_futures) | set(generation_futures),
                    return_when=FIRST_COMPLETED,
                )
                for future in done:
                    task = analysis_futures.pop(future, None)
                    if task is not None:
                        prepared = future.result()
                        if isinstance(prepared, tuple):
                            analysis, prompt = prepared
                            generated = generation_executor.submit(
                                self._run_task,
                                task,
                                identity_image,
                                output_root,
                                generation_mode,
                                dossier,
                                product_dossier_path,
                                analysis,
                                prompt,
                            )
                            generation_futures[generated] = task
                        else:
                            persist(prepared)
                        continue

                    generation_futures.pop(future, None)
                    persist(future.result())
        return self._records[added_record_start:]
