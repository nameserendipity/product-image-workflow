from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


INFERRED_SOURCE = "visual_analysis"
INFERRED_HANDLING = "图片识别，待核验"
UNCERTAIN_MARKERS = (
    "unclear",
    "unknown",
    "uncertain",
    "illegible",
    "partially legible",
    "不清晰",
    "无法确认",
    "未知",
    "不明确",
    "难以辨认",
)
PROMOTIONAL_MARKERS = (
    "anti-dandruff",
    "official",
    "authentic",
    "certified",
    "去屑",
    "控油",
    "柔顺",
    "留香",
    "正品",
    "官方",
    "认证",
)
NON_PRODUCT_COMPONENT_MARKERS = (
    "advertising",
    "promotional",
    "portrait",
    "person",
    "banner",
    "badge",
    "graphic",
    "text block",
    "text area",
    "background",
    "clothing",
    "poster",
    "广告",
    "促销",
    "人物",
    "横幅",
    "徽章",
)
CAPACITY_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:kg|ml|g|l|千克|毫升|克|升|片|抽|卷|瓶|袋|盒)",
    re.IGNORECASE,
)
NON_PRODUCT_CONTEXT_PATTERN = re.compile(
    r"\s+(?:in|on|against|beside)\s+(?:an?\s+)?"
    r"(?:(?:advertising|promotional|digital|printed)\s+)?"
    r"(?:graphic|background|banner|poster|image)\b.*$",
    re.IGNORECASE,
)


def _text(value: object) -> str:
    if isinstance(value, bool):
        return ""
    if isinstance(value, (str, int, float)):
        return re.sub(r"\s+", " ", str(value)).strip()
    return ""


def _flatten(value: object) -> list[str]:
    scalar = _text(value)
    if scalar:
        return [scalar]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_flatten(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            values = _flatten(item)
            label = _text(key)
            result.extend(f"{label}: {entry}" if label else entry for entry in values)
        return result
    return []


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = re.sub(r"\s+", " ", value).strip(" ;；,，/")
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _reliable_brand(value: object) -> str:
    brand = _text(value)
    lowered = brand.casefold()
    if not brand or any(marker in lowered for marker in UNCERTAIN_MARKERS):
        return ""
    return brand


def _safe_labels(value: object) -> list[str]:
    return [
        label
        for label in _unique(_flatten(value))
        if not any(marker in label.casefold() for marker in PROMOTIONAL_MARKERS)
    ]


def _is_product_evidence(value: str) -> bool:
    return not any(marker in value.casefold() for marker in NON_PRODUCT_COMPONENT_MARKERS)


def _product_object(value: object) -> str:
    return NON_PRODUCT_CONTEXT_PATTERN.sub("", _text(value)).strip()


def _product_materials(value: object) -> list[str]:
    if not isinstance(value, list):
        return [item for item in _unique(_flatten(value)) if _is_product_evidence(item)]
    materials: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            materials.extend(_flatten(item))
            continue
        component = _text(item.get("component"))
        if component and not _is_product_evidence(component):
            continue
        materials.extend(_flatten(item.get("confirmed_visible_material_or_texture")))
    return [item for item in _unique(materials) if _is_product_evidence(item)]


def _capacity_values(values: list[str]) -> list[str]:
    matches: list[str] = []
    for value in values:
        matches.extend(match.group(0).replace(" ", "") for match in CAPACITY_PATTERN.finditer(value))
    return _unique(matches)


def _anchor_observation(document: dict[str, Any], source_index: object) -> dict[str, Any]:
    for observation in document.get("observations") or []:
        if isinstance(observation, dict) and str(observation.get("source_index")) == str(source_index):
            return observation
    return {}


def _visual_row(name: str, value: str) -> dict[str, str]:
    return {
        "name": name,
        "value": value,
        "source": INFERRED_SOURCE,
        "handling": INFERRED_HANDLING,
    }


def _visual_parameters(
    source_document: dict[str, Any],
    dossier_document: dict[str, Any],
) -> list[dict[str, str]]:
    dossier = dossier_document.get("dossier")
    dossier = dossier if isinstance(dossier, dict) else {}
    anchor = dossier.get("anchor_identity")
    anchor = anchor if isinstance(anchor, dict) else {}
    rows: list[dict[str, str]] = []

    product_object = _product_object(anchor.get("object"))
    if product_object:
        rows.append(_visual_row("商品类型", product_object))

    labels = _safe_labels(anchor.get("visible_product_labeling"))
    label_names = [value for value in labels if not CAPACITY_PATTERN.fullmatch(value)]
    if label_names:
        rows.append(_visual_row("可见品名/标签", " / ".join(label_names)))

    capacities = _capacity_values(labels)
    if not capacities:
        capacities = _capacity_values(
            [
                _text(variant.get("spec_text") or variant.get("net_content"))
                for variant in source_document.get("sku_variants") or []
                if isinstance(variant, dict)
            ]
        )
    if capacities:
        rows.append(_visual_row("可见规格/容量", " / ".join(capacities)))

    brand = _reliable_brand(anchor.get("brand_or_mark"))
    if brand:
        rows.append(_visual_row("可见品牌/标识", brand))

    components = [
        value
        for value in _unique(_flatten(dossier.get("confirmed_components")))
        if _is_product_evidence(value)
    ]
    if components:
        rows.append(_visual_row("包装结构", " / ".join(components)))

    observation = _anchor_observation(dossier_document, anchor.get("source_index"))
    appearance = _unique(
        [
            value
            for value in _flatten(observation.get("colors"))
            if _is_product_evidence(value)
        ]
        + _product_materials(dossier.get("materials_and_textures"))
    )
    if appearance:
        rows.append(_visual_row("外观颜色/材质", " / ".join(appearance)))

    sku_values: list[str] = []
    for variant in source_document.get("sku_variants") or []:
        if not isinstance(variant, dict):
            continue
        parts = _unique(
            [
                _text(variant.get("spec_text") or variant.get("net_content")),
                _text(variant.get("color_text")),
            ]
        )
        if parts:
            sku_values.append(" / ".join(parts))
    sku_values = _unique(sku_values)
    if sku_values:
        rows.append(_visual_row("SKU规格", "；".join(sku_values)))

    return rows


def ensure_kuaishou_product_parameters(
    source_document: dict[str, Any],
    dossier_path: Path,
) -> dict[str, Any]:
    updated = dict(source_document)
    existing = updated.get("product_parameters")
    if isinstance(existing, list) and any(isinstance(item, dict) for item in existing):
        return updated

    try:
        loaded = json.loads(dossier_path.read_text(encoding="utf-8"))
        dossier_document = loaded if isinstance(loaded, dict) else {}
    except (OSError, json.JSONDecodeError):
        dossier_document = {}

    parameters = _visual_parameters(updated, dossier_document)
    if parameters:
        updated["parameter_status"] = "inferred"
        updated["parameter_error"] = "快手平台参数缺失，已使用图片识别结果，需人工核验"
    else:
        parameters = [
            {
                "name": "参数识别状态",
                "value": "未识别到可靠参数，需人工补充",
                "source": "manual_required",
                "handling": "待人工补充",
            }
        ]
        updated["parameter_status"] = "needs_review"
        updated["parameter_error"] = "快手平台和图片分析均未提供可靠商品参数"

    updated["product_parameters"] = parameters
    updated["product_parameters_text"] = "\n".join(
        f"{row['name']}: {row['value']}" for row in parameters
    )
    return updated
