from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import BrowserContext, Page


PARAMETER_ENTRY_TEXTS = ("查看全部参数", "全部参数", "商品参数", "规格参数", "参数")
MAX_PARAMETER_ERROR_LENGTH = 180
PARAMETER_PROBE_LIMIT = 3
MIN_EXPECTED_PARAMETER_COUNT = 5
IGNORED_PARAMETER_LABELS = {
    "参数",
    "商品参数",
    "规格参数",
    "全部参数",
    "查看全部参数",
    "收起",
}


def empty_parameter_metadata(
    product_id: str,
    status: str,
    error: str = "",
) -> dict[str, Any]:
    return {
        "parameter_source_product_id": product_id,
        "parameter_status": status,
        "parameter_error": error,
        "product_parameters": [],
        "product_parameters_text": "",
    }


def sanitize_parameter_error(error: Exception | str) -> str:
    text = re.sub(r"\s+", " ", str(error)).strip()
    return text[:MAX_PARAMETER_ERROR_LENGTH]


def normalize_product_parameters(raw_parameters: list[Any]) -> list[dict[str, str]]:
    """规范化参数行，保留首次出现的同名参数和页面顺序。"""
    parameters: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for item in raw_parameters:
        if not isinstance(item, dict):
            continue
        name = re.sub(r"\s+", " ", str(item.get("name") or "")).strip(" ：:")
        value = re.sub(r"\s+", " ", str(item.get("value") or "")).strip()
        if (
            not name
            or not value
            or name == value
            or name in seen_names
            or name in IGNORED_PARAMETER_LABELS
            or value in IGNORED_PARAMETER_LABELS
        ):
            continue
        if len(name) > 50 or len(value) > 500:
            continue
        seen_names.add(name)
        parameters.append({"name": name, "value": value})
    return parameters


def complete_parameter_metadata(
    product_id: str,
    parameters: list[dict[str, str]],
) -> dict[str, Any]:
    result = empty_parameter_metadata(product_id, "complete")
    result["product_parameters"] = parameters
    result["product_parameters_text"] = "\n".join(
        f"{item['name']}：{item['value']}" for item in parameters
    )
    return result


def partial_parameter_metadata(
    product_id: str,
    parameters: list[dict[str, str]],
) -> dict[str, Any]:
    result = complete_parameter_metadata(product_id, parameters)
    result["parameter_status"] = "partial"
    result["parameter_error"] = (
        f"仅识别到 {len(parameters)} 项参数，未达到完整参数最低数量 {MIN_EXPECTED_PARAMETER_COUNT}"
    )
    return result


def first_visible_text_click(page: Any, text: str, timeout_ms: int) -> tuple[bool, bool]:
    """返回是否发现入口及是否成功点击，避免把点击异常误判为入口缺失。"""
    for exact in (True, False):
        try:
            locator = page.get_by_text(text, exact=exact)
            count = min(locator.count(), 4)
        except Exception:
            continue
        for index in range(count):
            item = locator.nth(index)
            try:
                if not item.is_visible(timeout=min(timeout_ms, 2_000)):
                    continue
            except Exception:
                continue
            try:
                item.click(timeout=timeout_ms)
                return True, True
            except Exception:
                return True, False
    return False, False


def parameter_surfaces(page: Page) -> list[Any]:
    surfaces: list[Any] = [page]
    try:
        frames = page.frames
    except Exception:
        return surfaces
    for frame in frames:
        if frame is not page:
            surfaces.append(frame)
    return surfaces


def open_parameter_panel(page: Page, timeout_ms: int) -> tuple[Any | None, bool, bool]:
    entry_found = False
    poll_interval_ms = 750
    max_polls = max(1, (timeout_ms + poll_interval_ms - 1) // poll_interval_ms)
    for poll_index in range(max_polls):
        for surface in parameter_surfaces(page):
            for text in PARAMETER_ENTRY_TEXTS:
                found, clicked = first_visible_text_click(surface, text, min(timeout_ms, 2_000))
                entry_found = entry_found or found
                if clicked:
                    surface.wait_for_timeout(1_000)
                    return surface, True, True

        if poll_index + 1 < max_polls:
            page.wait_for_timeout(poll_interval_ms)
    return None, entry_found, False


def extract_visible_parameter_rows(page: Any) -> list[Any]:
    """仅从淘宝可见参数区读取结构化行，不回退扫描整个详情页正文。"""
    return page.evaluate(
        """
        () => {
            const normalize = value => (value || '').replace(/\\s+/g, ' ').trim();
            const visible = element => {
                const rect = element.getBoundingClientRect();
                const style = getComputedStyle(element);
                return rect.width > 0 && rect.height > 0
                    && style.display !== 'none' && style.visibility !== 'hidden';
            };
            const rows = [];
            const add = (name, value) => {
                name = normalize(name).replace(/[：:]$/, '');
                value = normalize(value);
                if (name && value) rows.push({name, value});
            };

            const panels = Array.from(document.querySelectorAll([
                '[class*="paramsInfoArea"]',
                '[class*="ParamsInfoArea"]',
                '[class*="paramsInfoContent"]',
                '[class*="ParamsInfoContent"]',
                '#J_AttrUL',
                '#J_AttrList',
                '.attributes-list',
                '.tm-attributes',
                '.tb-attributes',
                '[role="dialog"] [class*="param"]',
                '[role="dialog"] [class*="Param"]'
            ].join(',')))
                .filter(visible);

            // 参数卡片有时会被 portal 到页面根节点，不一定仍是参数区容器的子节点。
            for (const item of document.querySelectorAll('[class*="emphasisParamsInfoItem--"]')) {
                if (!visible(item)) continue;
                const value = item.querySelector('[class*="emphasisParamsInfoItemTitle--"]');
                const name = item.querySelector('[class*="emphasisParamsInfoItemSubTitle--"]');
                if (name && value && visible(name) && visible(value)) {
                    add(name.innerText || name.textContent, value.innerText || value.textContent);
                }
            }
            for (const item of document.querySelectorAll('[class*="generalParamsInfoItem--"]')) {
                if (!visible(item)) continue;
                const name = item.querySelector('[class*="generalParamsInfoItemTitle--"]');
                const value = item.querySelector('[class*="generalParamsInfoItemSubTitle--"]');
                if (name && value && visible(name) && visible(value)) {
                    add(name.innerText || name.textContent, value.innerText || value.textContent);
                }
            }
            for (const panel of panels) {
                // 淘宝重点参数卡片：标题节点为参数值，副标题节点为参数名称。
                for (const item of panel.querySelectorAll('[class*="emphasisParamsInfoItem--"]')) {
                    if (!visible(item)) continue;
                    const value = item.querySelector('[class*="emphasisParamsInfoItemTitle--"]');
                    const name = item.querySelector('[class*="emphasisParamsInfoItemSubTitle--"]');
                    if (name && value && visible(name) && visible(value)) {
                        add(name.innerText || name.textContent, value.innerText || value.textContent);
                    }
                }

                // 淘宝普通参数卡片：标题节点为参数名称，副标题节点为参数值。
                for (const item of panel.querySelectorAll('[class*="generalParamsInfoItem--"]')) {
                    if (!visible(item)) continue;
                    const name = item.querySelector('[class*="generalParamsInfoItemTitle--"]');
                    const value = item.querySelector('[class*="generalParamsInfoItemSubTitle--"]');
                    if (name && value && visible(name) && visible(value)) {
                        add(name.innerText || name.textContent, value.innerText || value.textContent);
                    }
                }
            }

            // 仅在参数区内保留表格和定义列表兼容，不扫描商品页其它区域。
            for (const panel of panels) {
                for (const row of panel.querySelectorAll('tr')) {
                if (!visible(row)) continue;
                const cells = Array.from(row.querySelectorAll('th,td')).filter(visible);
                for (let index = 0; index + 1 < cells.length; index += 2) {
                    add(cells[index].innerText || cells[index].textContent,
                        cells[index + 1].innerText || cells[index + 1].textContent);
                }
                }
                for (const list of panel.querySelectorAll('dl')) {
                    if (!visible(list)) continue;
                    const names = Array.from(list.querySelectorAll('dt')).filter(visible);
                    const values = Array.from(list.querySelectorAll('dd')).filter(visible);
                    for (let index = 0; index < Math.min(names.length, values.length); index += 1) {
                        add(names[index].innerText || names[index].textContent,
                            values[index].innerText || values[index].textContent);
                    }
                }
                for (const item of panel.querySelectorAll('li')) {
                    if (!visible(item)) continue;
                    const text = normalize(item.innerText || item.textContent);
                    const separator = text.search(/[:：]/);
                    if (separator > 0) {
                        add(text.slice(0, separator), text.slice(separator + 1));
                    }
                }

                // 新版参数弹层会把参数名和值渲染成连续文本节点，类名每次发布都会变化。
                const ignored = new Set(['参数', '商品参数', '规格参数', '全部参数', '查看全部参数', '收起']);
                const lines = (panel.innerText || panel.textContent || '')
                    .split(/\\n+/)
                    .map(normalize)
                    .filter(Boolean);
                for (let index = 0; index < lines.length; index += 1) {
                    const line = lines[index];
                    const separator = line.search(/[:：]/);
                    if (separator > 0) {
                        add(line.slice(0, separator), line.slice(separator + 1));
                        continue;
                    }
                    if (ignored.has(line) || index + 1 >= lines.length) continue;
                    const value = lines[index + 1];
                    if (ignored.has(value) || line.length > 50 || value.length > 500) continue;
                    add(line, value);
                    index += 1;
                }
            }
            return rows;
        }
        """
    )


def collect_product_parameters(
    page: Page,
    product_id: str,
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    try:
        summary_parameters: list[dict[str, str]] = []
        for surface in parameter_surfaces(page):
            summary_parameters.extend(extract_visible_parameter_rows(surface))
        summary_parameters = normalize_product_parameters(summary_parameters)

        surface, entry_found, opened = open_parameter_panel(page, min(timeout_ms, 12_000))
        expanded_parameters: list[dict[str, str]] = []
        if opened and surface is not None:
            for current_surface in parameter_surfaces(page):
                expanded_parameters.extend(extract_visible_parameter_rows(current_surface))

        parameters = normalize_product_parameters(summary_parameters + expanded_parameters)
        if len(parameters) >= MIN_EXPECTED_PARAMETER_COUNT:
            return complete_parameter_metadata(product_id, parameters)
        if parameters:
            return partial_parameter_metadata(product_id, parameters)
        if not entry_found:
            return empty_parameter_metadata(product_id, "not_found", "未找到商品参数入口")
        if not opened or surface is None:
            return empty_parameter_metadata(product_id, "unavailable", "无法打开商品参数面板")
        return empty_parameter_metadata(product_id, "not_found", "未找到可识别的商品参数")
    except Exception as error:  # 参数异常不能影响图片采集主流程。
        return empty_parameter_metadata(product_id, "unavailable", sanitize_parameter_error(error))


def collect_first_product_parameters(
    context: BrowserContext,
    ranked_products: list[dict[str, Any]],
    timeout_ms: int = 30_000,
) -> dict[str, Any]:
    if not ranked_products:
        return empty_parameter_metadata("", "unavailable", "销量排序后没有可用商品")

    from same_item_collector import CollectorPaused, detect_stop

    candidates = ranked_products[:PARAMETER_PROBE_LIMIT]
    attempts: list[dict[str, Any]] = []
    for probe_index, product in enumerate(candidates, start=1):
        product_id = str(product.get("product_id") or "")
        item_url = str(product.get("item_url") or "")
        if not item_url:
            attempts.append(
                empty_parameter_metadata(product_id, "unavailable", "商品缺少详情页链接")
            )
            continue

        page: Page | None = None
        try:
            page = context.new_page()
            print(
                f"[collector] collecting parameters {probe_index}/{len(candidates)}: {product_id}",
                flush=True,
            )
            page.goto(item_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(3_000)
            detect_stop(page)
            result = collect_product_parameters(page, product_id, timeout_ms)
        except CollectorPaused:
            raise
        except Exception as error:
            result = empty_parameter_metadata(
                product_id,
                "unavailable",
                sanitize_parameter_error(error),
            )
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

        print(
            f"[collector] parameter collection {result['parameter_status']}: {product_id}",
            flush=True,
        )
        if result["parameter_status"] == "complete":
            return result
        attempts.append(result)

    partial = max(
        (result for result in attempts if result.get("product_parameters")),
        key=lambda result: len(result.get("product_parameters") or []),
        default=None,
    )
    if partial is not None:
        return partial
    not_found = next(
        (result for result in attempts if result["parameter_status"] == "not_found"),
        attempts[0],
    )
    return {
        **not_found,
        "parameter_error": f"前 {len(candidates)} 个商品均未找到可识别参数",
    }
