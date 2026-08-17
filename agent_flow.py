"""Conversation state for collection and generation requests."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlparse

from platform_urls import (
    TAOBAO_SHORT_HOSTS,
    is_douyin_product_host,
    kuaishou_product_id,
    is_taobao_host,
    is_tmall_host,
)


WorkflowCategory = Literal["main", "sku", "detail"]
GenerationMode = Literal["own_product", "competitor_reference"]
WORKFLOWS: tuple[WorkflowCategory, ...] = ("main", "sku", "detail")
DEFAULT_MAIN_IMAGES = 10
URL_PATTERN = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


class IntentRecognitionError(RuntimeError):
    pass


def classify_message(message: str, state: dict, base_url: str, api_key: str) -> dict:
    if not base_url or not api_key:
        raise IntentRecognitionError("LLM intent service is not configured")
    prompt = f"""你是电商图片工作流的意图识别器。只返回 JSON，不要 Markdown，不要解释。
根据用户消息和当前状态，提取以下字段：
action: reset、update_task、answer、clarify 四选一；
reference_url: 淘宝、天猫、京东、抖音或快手商品链接，没有则为 null；
quantity_mode: reference、custom、unspecified 三选一；
main_count: quantity_mode 为 custom 时填 1 到 999 的整数，否则为 null；
sku_count: 用户明确指定 SKU 图数量时填 1 到 8 的整数，否则为 null；
detail_count: 用户明确指定详情图数量时填 1 到 15 的整数，否则为 null；
collection_types: 采集时从 main、sku、detail 中选择，可为空数组；
workflows: 生图时从 main、sku、detail 中选择，可为空数组；
generation_mode: own_product、competitor_reference 或 null；
reply: 简短中文回复，说明下一步或需要追问的内容。

规则：
1. “重新开始、换个任务、清空当前任务”识别为 reset。
2. “按对标数量、全部主图”识别为 reference；未指定数量时主图默认为 10 张。
3. “主图 5 张、生成 5 张主图”识别为 custom 且 main_count 为 5。
4. 主图、SKU、详情图分别对应 main、sku、detail；“全部类型”对应三个工作流。
4.1 如果用户明确说“采集全部类型，只生成主图和详情图”，collection_types 必须是 [main, sku, detail]，workflows 必须是 [main, detail]。
4.1 “每种类型 5 张”表示 main_count、sku_count、detail_count 都为 5；分别指定时分别填写。
5. 用户一句话同时给出链接、数量和工作流时一次性提取，不要逐项追问。
6. 不要臆造链接、数量或工作流；无法确定时使用 clarify。
7. 用户在问功能、当前状态、采集数量、文件夹位置、缺失原因或工作流进度时使用 answer，reply 必须根据当前状态直接回答，不能要求用户确认或重复输入参数。
8. 用户问采集文件夹时，reply 必须给出 current_state 中的 collection_folder；图片数量以 collected_summary 为准。
9. “不上传产品图、直接参考对标商品、按对标商品直接生成”识别为 competitor_reference。
10. “使用我方产品图、用我上传的产品图、替换成我的商品”识别为 own_product。

当前状态：{json.dumps(state, ensure_ascii=False)}
用户消息：{message}
"""
    prompt += """
额外输出字段：generate_images，只能是 true、false 或 null。
用户说“不要生成、不生成、不要生图、只采集、仅采集、先采集、暂不生成”时，generate_images=false。
用户明确说“开始生成、继续生成、采集后生成、需要生成图片”时，generate_images=true。
未表达是否生成时，generate_images=null，不得自行改变当前设置。
“只采集全部图片，不要生成”属于 update_task，workflows=[main,sku,detail]，generate_images=false。
"""
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {
                "role": "system",
                "content": "你负责可靠地提取工作流参数，必须输出合法 JSON。",
            },
            {"role": "user", "content": prompt},
        ],
    }
    request = Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=12) as response:
            document = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise IntentRecognitionError(f"HTTP {error.code}") from error
    except (URLError, TimeoutError, OSError, ValueError) as error:
        raise IntentRecognitionError("LLM intent request failed") from error
    try:
        content = document.get("choices", [{}])[0].get("message", {}).get("content")
    except (AttributeError, IndexError, TypeError) as error:
        raise IntentRecognitionError("LLM intent response has no content") from error
    if not isinstance(content, str):
        raise IntentRecognitionError("LLM intent response has no content")
    cleaned = content.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        intent = json.loads(cleaned)
    except json.JSONDecodeError as error:
        raise IntentRecognitionError("LLM intent response is not valid JSON") from error
    if not isinstance(intent, dict):
        raise IntentRecognitionError("LLM intent response is not an object")
    return intent


@dataclass
class AgentReply:
    message: str
    state: str
    reference_url: str | None
    max_main_images: int | None
    workflows: tuple[WorkflowCategory, ...] = ()
    main_quantity_mode: Literal["default", "reference", "custom"] = "default"
    max_sku_images: int | None = None
    max_detail_images: int | None = None
    collection_types: tuple[WorkflowCategory, ...] = ()


@dataclass
class AgentSession:
    reference_url: str | None = None
    max_main_images: int | None = DEFAULT_MAIN_IMAGES
    max_sku_images: int | None = None
    max_detail_images: int | None = None
    main_quantity_mode: Literal["default", "reference", "custom"] = "default"
    quantity_confirmed: bool = False
    awaiting: Literal["reference_url", "main_quantity", "workflow", ""] = "reference_url"
    manifest_loaded: bool = False
    workflows: tuple[WorkflowCategory, ...] = field(default_factory=tuple)
    collection_types: tuple[WorkflowCategory, ...] = field(default_factory=tuple)
    generation_enabled: bool = True
    generation_mode: GenerationMode = "competitor_reference"

    def __post_init__(self) -> None:
        if self.max_main_images is None and self.main_quantity_mode == "default":
            self.main_quantity_mode = "reference"
        if not self.collection_types and self.workflows:
            self.collection_types = self.workflows
        if self.reference_url and self.quantity_confirmed and (self.workflows or self.collection_types):
            self.awaiting = ""

    def apply_intent(self, intent: dict, message: str = "") -> AgentReply:
        intent = dict(intent)
        try:
            explicit_command = self._merge_explicit_message(intent, message)
        except ValueError as error:
            return self._reply(str(error))
        action = str(intent.get("action", "clarify"))
        generation_directive = intent.get("generate_images")
        if isinstance(generation_directive, bool):
            self.generation_enabled = generation_directive
        generation_mode = intent.get("generation_mode")
        if generation_mode in {"own_product", "competitor_reference"}:
            self.generation_mode = generation_mode
        if action in {"answer", "clarify"} and not explicit_command:
            return self._reply(str(intent.get("reply") or "请说明商品链接、数量和要生成的图片类型。"))

        raw_url = intent.get("reference_url")
        if isinstance(raw_url, str) and raw_url.strip():
            url = self._find_url(raw_url.strip()) or raw_url.strip()
            if not self._is_supported_url(url):
                return self._reply("请提供淘宝、天猫、京东、抖音或快手商品链接。")
            if url != self.reference_url:
                self.max_main_images = DEFAULT_MAIN_IMAGES
                self.max_sku_images = None
                self.max_detail_images = None
                self.main_quantity_mode = "default"
                self.quantity_confirmed = True
                self.workflows = ()
                self.collection_types = ()
            self.reference_url = url

        quantity_mode = str(intent.get("quantity_mode", "unspecified"))
        if quantity_mode == "custom":
            try:
                count = int(intent.get("main_count"))
            except (TypeError, ValueError):
                count = 0
            if not 1 <= count <= 999:
                return self._reply("请告诉我主图数量，例如“生成 5 张主图”。")
            self.max_main_images = count
            self.main_quantity_mode = "custom"
            self.quantity_confirmed = True
        elif quantity_mode == "reference":
            self.max_main_images = None
            self.main_quantity_mode = "reference"
            self.quantity_confirmed = True

        for field_name, attribute, label, maximum in (
            ("sku_count", "max_sku_images", "SKU 图", 8),
            ("detail_count", "max_detail_images", "详情图", 15),
        ):
            raw_count = intent.get(field_name)
            if raw_count is None:
                continue
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                count = 0
            if not 1 <= count <= maximum:
                return self._reply(f"{label}数量必须是 1 到 {maximum} 张。")
            setattr(self, attribute, count)

        raw_collection_types = intent.get("collection_types")
        if isinstance(raw_collection_types, list):
            selected_collection = tuple(value for value in raw_collection_types if value in WORKFLOWS)
            if selected_collection:
                self.collection_types = selected_collection

        raw_workflows = intent.get("workflows")
        if isinstance(raw_workflows, list):
            selected = tuple(value for value in raw_workflows if value in WORKFLOWS)
            if selected:
                self.workflows = selected
                if not self.collection_types:
                    self.collection_types = selected
                if quantity_mode == "unspecified" and not self.quantity_confirmed:
                    self.max_main_images = DEFAULT_MAIN_IMAGES
                    self.main_quantity_mode = "default"
                    self.quantity_confirmed = True

        if not self.reference_url:
            self.awaiting = "reference_url"
            return self._reply(str(intent.get("reply") or "请先提供淘宝、天猫、京东、抖音或快手商品链接。"))
        if not self.quantity_confirmed:
            self.awaiting = "main_quantity"
            return self._reply("请确认按对标商品数量，或指定主图数量。")
        if not self.workflows and not self.collection_types:
            self.awaiting = "workflow"
            if self.generation_enabled:
                return self._reply("数量已确认。请继续选择图片类型：主图、SKU 图、详情图，或全部类型。")
            return self._reply("已设置为只采集、不生成图片。请继续选择采集类型：主图、SKU 图、详情图，或全部类型。")
        self.awaiting = ""
        if quantity_mode == "unspecified" and self.main_quantity_mode == "default":
            names = "、".join({"main": "主图", "sku": "SKU 图", "detail": "详情图"}[name] for name in self.workflows)
            quantity_summary = f"默认主图生成 {DEFAULT_MAIN_IMAGES} 张。"
            if self.generation_enabled:
                return self._reply(f"{quantity_summary}已选择 {names}，采集完成后自动生成图片。")
            return self._reply(f"{quantity_summary}已选择采集 {names}，不生成图片。")
        return self._reply(str(intent.get("reply") or "已理解你的需求，正在检查采集素材并安排工作流。"))

    def handle(self, message: str) -> AgentReply:
        message = message.strip()
        try:
            workflow_counts = self._find_workflow_counts(message)
        except ValueError as error:
            return self._reply(str(error))
        url = self._find_url(message)
        generation_directive = self._find_generation_directive(message)
        if generation_directive is not None:
            self.generation_enabled = generation_directive
        generation_mode = self._find_generation_mode(message)
        if generation_mode is not None:
            self.generation_mode = generation_mode

        if self.awaiting == "reference_url":
            if not url:
                return self._reply("请先提供对标商品链接（淘宝、天猫、京东、抖音或快手）。")
            if not self._is_supported_url(url):
                return self._reply("该链接不是支持的淘宝、天猫、京东、抖音或快手商品链接，请重新提供对标商品链接。")
            self.reference_url = url
            self.quantity_confirmed = True
            self.max_main_images = DEFAULT_MAIN_IMAGES
            self.main_quantity_mode = "default"
            self.awaiting = "workflow"
            return self._reply(
                f"已收到对标商品链接。默认主图生成 {DEFAULT_MAIN_IMAGES} 张；"
                "如需按对标数量或指定数量，请继续告诉我，再选择主图、SKU 图、详情图。"
            )

        if self.awaiting == "main_quantity":
            if url:
                if not self._is_supported_url(url):
                    return self._reply("该链接不受支持，请提供淘宝、天猫、京东、抖音或快手商品链接。")
                self.reference_url = url
                return self._reply(
                    "已更新对标商品链接。是否按对标商品的主图实际数量采集？"
                    "回复“按对标数量”，或直接回复“主图 N 张”。"
                )
            selected = self._find_workflows(message)
            if selected:
                self.workflows = selected
            self._apply_workflow_counts(workflow_counts)
            count = workflow_counts.get("main")
            if count is not None:
                self.max_main_images = count
                self.main_quantity_mode = "custom"
                self.quantity_confirmed = True
                self.awaiting = ""
                return self._confirm_quantity(f"将采集完整图片包，并仅编入前 {count} 张主图。", message)
            if self._uses_reference_count(message):
                self.max_main_images = None
                self.main_quantity_mode = "reference"
                self.quantity_confirmed = True
                self.awaiting = ""
                return self._confirm_quantity("将采集完整图片包，并按对标商品实际主图数量编入任务。", message)
            self.max_main_images = DEFAULT_MAIN_IMAGES
            self.main_quantity_mode = "default"
            self.quantity_confirmed = True
            self.awaiting = "workflow"
            return self._reply(f"未指定数量，默认主图生成 {DEFAULT_MAIN_IMAGES} 张。请说明要生成主图、SKU 图还是详情图。")

        self._apply_workflow_counts(workflow_counts)
        count = workflow_counts.get("main")
        if count is None and self._uses_reference_count(message):
            self.max_main_images = None
            self.main_quantity_mode = "reference"
            self.quantity_confirmed = True

        collection_types = self._find_collection_types(message)
        generation_workflows = self._find_generation_workflows(message)
        selected = generation_workflows or self._find_workflows(message)
        if collection_types is not None:
            self.collection_types = collection_types
        if selected:
            if collection_types is None:
                self.collection_types = selected
            self.workflows = selected
            if collection_types is not None and generation_workflows is None:
                self.workflows = collection_types
            self.awaiting = ""
            names = "、".join({"main": "主图", "sku": "SKU 图", "detail": "详情图"}[name] for name in selected)
            if not self.manifest_loaded:
                return self._reply(f"已选择 {names} 工作流，正在自动开始采集。")
            return self._reply(f"已选择 {names} 工作流，检测到产品图和视觉 API Key 后将自动开始生成。")
        if not self.manifest_loaded:
            quantity = (
                "默认主图 10 张"
                if self.main_quantity_mode == "default"
                else "按对标商品实际主图数量"
                if self.main_quantity_mode == "reference"
                else f"主图 {self.max_main_images} 张"
            )
            return self._reply(f"已设置为{quantity}。请说明要生成主图、SKU 图还是详情图。")
        return self._reply("请说明要生成的类型，例如“只生成主图”或“主图和详情图一起运行”。")

    def mark_collected(self) -> None:
        self.manifest_loaded = True
        self.awaiting = "" if (self.workflows or self.collection_types) else "workflow"

    def set_main_quantity(self, mode: str, count: int | None = None) -> None:
        if mode == "default":
            self.max_main_images = DEFAULT_MAIN_IMAGES
            self.main_quantity_mode = "default"
        elif mode == "reference":
            self.max_main_images = None
            self.main_quantity_mode = "reference"
        elif mode == "custom":
            if count is None or not 1 <= int(count) <= 999:
                raise ValueError("主图数量必须是 1 到 999 的整数。")
            self.max_main_images = int(count)
            self.main_quantity_mode = "custom"
        else:
            raise ValueError("主图数量模式不受支持。")
        self.quantity_confirmed = True
        if self.reference_url and not self.manifest_loaded:
            self.awaiting = "workflow"

    def _confirm_quantity(self, summary: str, message: str) -> AgentReply:
        collection_types = self._find_collection_types(message)
        generation_workflows = self._find_generation_workflows(message)
        selected = generation_workflows or self._find_workflows(message)
        if collection_types is not None:
            self.collection_types = collection_types
        if selected:
            if collection_types is None:
                self.collection_types = selected
            self.workflows = selected
            if collection_types is not None and generation_workflows is None:
                self.workflows = collection_types
            names = "、".join({"main": "主图", "sku": "SKU 图", "detail": "详情图"}[name] for name in selected)
            return self._reply(f"{summary} 已选择 {names} 工作流，正在自动开始采集。")
        return self._reply(f"{summary} 请说明要生成主图、SKU 图还是详情图。")

    def _reply(self, message: str) -> AgentReply:
        return AgentReply(
            message=message,
            state=self.awaiting,
            reference_url=self.reference_url,
            max_main_images=self.max_main_images,
            workflows=self.workflows,
            main_quantity_mode=self.main_quantity_mode,
            max_sku_images=self.max_sku_images,
            max_detail_images=self.max_detail_images,
            collection_types=self.collection_types,
        )

    def _merge_explicit_message(self, intent: dict, message: str) -> bool:
        """Prefer unambiguous task commands over an incomplete LLM classification."""
        message = message.strip()
        if not message or str(intent.get("action", "")) == "reset":
            return False

        changed = False
        workflow_counts = self._find_workflow_counts(message)
        count = workflow_counts.get("main")
        if count is not None:
            intent["quantity_mode"] = "custom"
            intent["main_count"] = count
            changed = True
        elif self._uses_reference_count(message):
            intent["quantity_mode"] = "reference"
            intent["main_count"] = None
            changed = True
        elif workflow_counts:
            intent["quantity_mode"] = "unspecified"
            intent["main_count"] = None

        if "sku" in workflow_counts:
            intent["sku_count"] = workflow_counts["sku"]
            changed = True
        if "detail" in workflow_counts:
            intent["detail_count"] = workflow_counts["detail"]
            changed = True

        collection_types = self._find_collection_types(message)
        if collection_types is not None:
            intent["collection_types"] = list(collection_types)
            changed = True

        workflows = self._find_generation_workflows(message) or self._find_workflows(message)
        if workflows:
            intent["workflows"] = list(workflows)
            changed = True

        generation_mode = self._find_generation_mode(message)
        if generation_mode is not None:
            intent["generation_mode"] = generation_mode
            changed = True

        if changed:
            intent["action"] = "update_task"
        return changed

    @staticmethod
    def _find_url(message: str) -> str | None:
        matched = URL_PATTERN.search(message)
        return matched.group(0).rstrip(".,!?;:，。！？；：") if matched else None

    @staticmethod
    def _is_supported_url(value: str) -> bool:
        host = (urlparse(value).hostname or "").lower()
        return (
            host in TAOBAO_SHORT_HOSTS
            or host == "v.douyin.com"
            or is_taobao_host(host)
            or is_tmall_host(host)
            or is_douyin_product_host(host)
            or bool(kuaishou_product_id(value))
            or host == "jd.com"
            or host.endswith(".jd.com")
        )

    @staticmethod
    def _find_main_count(message: str) -> int | None:
        return AgentSession._find_workflow_counts(message).get("main")

    @staticmethod
    def _find_workflow_counts(message: str) -> dict[WorkflowCategory, int]:
        counts: dict[WorkflowCategory, int] = {}
        count_token = r"(\d{1,3}|[零〇一二三四五六七八九十百千万两]+)"
        unified = re.search(
            rf"(?:每种类型|每个类型|每类|全部(?:图片)?类型|三种(?:图片)?类型)\s*(?:都|各)?\s*(?:生成)?\s*{count_token}\s*张",
            message,
            re.IGNORECASE,
        )
        if unified:
            value = AgentSession._parse_count_token(unified.group(1))
            counts.update({"main": value, "sku": value, "detail": value})

        aliases = {
            "main": r"主图",
            "sku": r"SKU\s*图|SKU",
            "detail": r"详情图|详情",
        }
        for category, alias in aliases.items():
            matched = re.search(rf"(?:{alias})\s*(?:生成)?\s*{count_token}\s*张", message, re.IGNORECASE)
            if not matched:
                matched = re.search(rf"{count_token}\s*张\s*(?:的)?\s*(?:{alias})", message, re.IGNORECASE)
            if matched:
                counts[category] = AgentSession._parse_count_token(matched.group(1))

        if not counts:
            matched = re.search(rf"{count_token}\s*张", message, re.IGNORECASE)
            if matched:
                counts["main"] = AgentSession._parse_count_token(matched.group(1))

        limits = {"main": 999, "sku": 8, "detail": 15}
        labels = {"main": "主图", "sku": "SKU 图", "detail": "详情图"}
        for category, count in counts.items():
            if not 1 <= count <= limits[category]:
                raise ValueError(f"{labels[category]}数量必须是 1 到 {limits[category]} 张。")
        return counts

    @staticmethod
    def _parse_count_token(token: str) -> int:
        if token.isdigit():
            return int(token)
        digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
        units = {"十": 10, "百": 100, "千": 1000, "万": 10000}
        total = section = number = 0
        for char in token:
            if char in digits:
                number = digits[char]
                continue
            unit = units[char]
            if unit == 10000:
                section += number
                total += section * unit
                section = number = 0
                continue
            section += (number or 1) * unit
            number = 0
        return total + section + number

    def _apply_workflow_counts(self, counts: dict[WorkflowCategory, int]) -> None:
        if "main" in counts:
            self.max_main_images = counts["main"]
            self.main_quantity_mode = "custom"
            self.quantity_confirmed = True
        if "sku" in counts:
            self.max_sku_images = counts["sku"]
        if "detail" in counts:
            self.max_detail_images = counts["detail"]

    @staticmethod
    def _uses_reference_count(message: str) -> bool:
        lowered = message.lower()
        phrases = (
            "按对标",
            "对标数量",
            "对标商品数量",
            "原图数量",
            "实际主图数量",
            "按实际数量",
            "默认数量",
            "默认按",
        )
        return any(phrase in lowered for phrase in phrases)

    @staticmethod
    def _find_generation_directive(message: str) -> bool | None:
        lowered = message.lower()
        disabled = ("不要生成", "不生成", "不要生图", "不生图", "只采集", "仅采集", "先采集", "暂不生成")
        if any(token in lowered for token in disabled):
            return False
        enabled = ("开始生成", "继续生成", "采集后生成", "需要生成", "生成图片")
        if any(token in lowered for token in enabled):
            return True
        return None

    @staticmethod
    def _find_generation_mode(message: str) -> GenerationMode | None:
        lowered = message.lower()
        direct_reference = ("不上传产品图", "直接参考对标商品", "按对标商品直接生成", "不用产品图")
        if any(token in lowered for token in direct_reference):
            return "competitor_reference"
        own_product = ("使用我方产品图", "用我上传的产品图", "替换成我的商品", "上传自己的产品图")
        if any(token in lowered for token in own_product):
            return "own_product"
        return None

    @staticmethod
    def _find_collection_types(message: str) -> tuple[WorkflowCategory, ...] | None:
        marker = "\u91c7\u96c6"
        if marker not in message:
            return None
        return AgentSession._find_workflows(message[message.find(marker) + len(marker) :])

    @staticmethod
    def _find_generation_workflows(message: str) -> tuple[WorkflowCategory, ...] | None:
        for marker in (
            "\u53ea\u751f\u6210",
            "\u4ec5\u751f\u6210",
            "\u53ea\u8981\u751f\u6210",
            "\u53ea\u9700\u8981\u751f\u6210",
            "\u4ec5\u9700\u751f\u6210",
        ):
            if marker in message:
                return AgentSession._find_workflows(message[message.find(marker) + len(marker) :])
        return None

    @staticmethod
    def _find_workflows(message: str) -> tuple[WorkflowCategory, ...]:
        lowered = message.lower()
        if any(token in lowered for token in ("全部", "三个", "全都", "所有")):
            return WORKFLOWS
        if any(token in lowered for token in ("全部", "三个", "全都", "所有")):
            return WORKFLOWS
        selected: list[WorkflowCategory] = []
        if "主图" in message:
            selected.append("main")
        if "sku" in lowered:
            selected.append("sku")
        if "详情" in message:
            selected.append("detail")
        return tuple(selected)
