import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from agent_flow import DEFAULT_MAIN_IMAGES, AgentSession, IntentRecognitionError, classify_message
from image_workflows import (
    ApiSettings,
    build_detail_tasks,
    DetailViewPlan,
    ImageClient,
    ImageTask,
    VisionClient,
    WORKFLOW_PROFILES,
    WorkflowRunner,
    _multipart_body,
    analyze_identity_sources,
    compose_generation_prompt,
    load_identity_sources,
    load_manifest_tasks,
    _normalize_direct_reference_analysis,
    ordered_generation_images,
    resolve_identity_image,
    resolve_worker_count,
    round_robin_tasks,
    validate_detail_view_plans,
    workflow_instruction,
)


def _test_png_bytes() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (32, 32), (35, 80, 125)).save(buffer, format="PNG")
    return buffer.getvalue()


class AgentSessionTests(unittest.TestCase):
    def test_direct_reference_detail_tasks_use_view_plans(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jpg"
            detail_one = root / "detail-1.jpg"
            detail_two = root / "detail-2.jpg"
            for path in (main, detail_one, detail_two):
                path.write_bytes(b"image")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "main", "path": str(main)},
                            {"type": "detail", "path": str(detail_one)},
                            {"type": "detail", "path": str(detail_two)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            sources = load_identity_sources(manifest)
            plans = [
                DetailViewPlan(index, "material" if index > 1 else "front", f"focus-{index}", 2, False, ())
                for index in range(1, 7)
            ]

            tasks = build_detail_tasks(manifest, plans, sources)

            self.assertEqual(len(tasks), 6)
            self.assertEqual(tasks[0].view_plan, plans[0])
            self.assertEqual(tasks[0].source_path, detail_one.resolve())
            self.assertEqual(tasks[0].supporting_path, detail_one.resolve())
            self.assertTrue(all(task.source_path == detail_one.resolve() for task in tasks))
            self.assertTrue(all(task.inferred_view is False for task in tasks))

    def test_identity_sources_include_all_valid_collected_images(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / name for name in ("main-1.jpg", "main-2.jpg", "sku.jpg", "detail.jpg")]
            for index, path in enumerate(paths):
                path.write_bytes(str(index).encode())
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "main", "path": str(paths[0])},
                            {"type": "main", "path": str(paths[1])},
                            {"type": "sku", "path": str(paths[2])},
                            {"type": "detail", "path": str(paths[3])},
                            {"type": "detail", "path": str(root / "missing.jpg")},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            sources = load_identity_sources(manifest)

            self.assertEqual([source.category for source in sources], ["main", "main", "sku", "detail"])
            self.assertTrue(sources[0].is_anchor)
            self.assertFalse(any(source.is_anchor for source in sources[1:]))

    def test_identity_analysis_skips_one_failed_source(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = [root / f"{index}.jpg" for index in range(1, 5)]
            for path in paths:
                path.write_bytes(b"image")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "main", "path": str(path)} for path in paths]}),
                encoding="utf-8",
            )
            sources = load_identity_sources(manifest)

            def analyze(source):
                if source.index == 2:
                    raise RuntimeError("bad image")
                return {"source_index": source.index}

            observations, failures = analyze_identity_sources(sources, analyze, concurrency=None)

            self.assertEqual([item["source_index"] for item in observations], [1, 3, 4])
            self.assertEqual(failures[0]["source_index"], 2)

    def test_detail_plans_mark_unseen_side_as_inferred(self):
        raw_plans = [
            {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
            {"ordinal": 2, "view_type": "side", "focus": "侧面轮廓", "supporting_source_index": None},
            {"ordinal": 3, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
            {"ordinal": 4, "view_type": "workmanship", "focus": "做工细节", "supporting_source_index": 1},
            {"ordinal": 5, "view_type": "scale", "focus": "比例展示", "supporting_source_index": 1},
            {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
        ]

        plans = validate_detail_view_plans(raw_plans, 6, known_views={"front"}, valid_source_indices={1})

        self.assertEqual(len(plans), 6)
        self.assertTrue(all(isinstance(plan, DetailViewPlan) for plan in plans))
        fallback = plans[1]
        self.assertEqual(fallback.view_type, "detail_closeup")
        self.assertTrue(fallback.inferred_view)
        self.assertIn("ports", fallback.prohibited_inventions)
        self.assertNotIn("侧面轮廓", fallback.focus)
        self.assertFalse(plans[0].inferred_view)

    def test_detail_structural_view_requires_bound_source_with_matching_visible_view(self):
        raw_plans = [
            {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
            {"ordinal": 2, "view_type": "side", "focus": "侧面轮廓", "supporting_source_index": None},
            {"ordinal": 3, "view_type": "side", "focus": "侧面结构", "supporting_source_index": 1},
            {"ordinal": 4, "view_type": "side", "focus": "侧面细节", "supporting_source_index": 2},
            {"ordinal": 5, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
            {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
        ]

        plans = validate_detail_view_plans(
            raw_plans,
            6,
            known_views={"front", "side"},
            valid_source_indices={1, 2},
            source_views={1: {"front"}, 2: {"side"}},
        )

        self.assertEqual(plans[1].view_type, "detail_closeup")
        self.assertEqual(plans[2].view_type, "detail_closeup")
        self.assertEqual(plans[3].view_type, "side")
        self.assertTrue(plans[1].inferred_view)
        self.assertTrue(plans[2].inferred_view)
        self.assertFalse(plans[3].inferred_view)

    def test_detail_three_quarter_alias_without_bound_evidence_falls_back_to_closeup(self):
        raw_plans = [
            {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
            {"ordinal": 2, "view_type": "three-quarter", "focus": "四分之三视角", "supporting_source_index": None},
            {"ordinal": 3, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
            {"ordinal": 4, "view_type": "workmanship", "focus": "做工细节", "supporting_source_index": 1},
            {"ordinal": 5, "view_type": "scale", "focus": "比例展示", "supporting_source_index": 1},
            {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
        ]

        plans = validate_detail_view_plans(
            raw_plans,
            6,
            known_views={"front", "three-quarter"},
            valid_source_indices={1},
            source_views={1: {"front"}},
        )

        self.assertEqual(plans[1].view_type, "detail_closeup")
        self.assertTrue(plans[1].inferred_view)
        self.assertNotIn("四分之三视角", plans[1].focus)

    def test_detail_three_quarter_alias_with_bound_evidence_is_preserved(self):
        raw_plans = [
            {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
            {"ordinal": 2, "view_type": "three-quarter", "focus": "四分之三视角", "supporting_source_index": 2},
            {"ordinal": 3, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
            {"ordinal": 4, "view_type": "workmanship", "focus": "做工细节", "supporting_source_index": 1},
            {"ordinal": 5, "view_type": "scale", "focus": "比例展示", "supporting_source_index": 1},
            {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
        ]

        plans = validate_detail_view_plans(
            raw_plans,
            6,
            known_views={"front", "three-quarter"},
            valid_source_indices={1, 2},
            source_views={1: {"front"}, 2: {"three-quarter"}},
        )

        self.assertEqual(plans[1].view_type, "three_quarter")
        self.assertFalse(plans[1].inferred_view)

    def test_detail_structural_view_semantic_aliases_cannot_bypass_evidence_gate(self):
        for alias in ("rear", "side view", "three-quarter view"):
            with self.subTest(alias=alias):
                raw_plans = [
                    {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
                    {"ordinal": 2, "view_type": alias, "focus": "未验证结构角度", "supporting_source_index": None},
                    {"ordinal": 3, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
                    {"ordinal": 4, "view_type": "workmanship", "focus": "做工细节", "supporting_source_index": 1},
                    {"ordinal": 5, "view_type": "scale", "focus": "比例展示", "supporting_source_index": 1},
                    {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
                ]

                plans = validate_detail_view_plans(
                    raw_plans,
                    6,
                    known_views={"front"},
                    valid_source_indices={1},
                    source_views={1: {"front"}},
                )

                self.assertEqual(plans[1].view_type, "detail_closeup")
                self.assertTrue(plans[1].inferred_view)

    def test_direct_reference_uses_first_valid_main_image(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            first_main = root / "main-1.jpg"
            second_main = root / "main-2.jpg"
            first_main.write_bytes(b"first")
            second_main.write_bytes(b"second")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "main", "path": str(first_main)},
                            {"type": "main", "path": str(second_main)},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            identity = resolve_identity_image(manifest, None, "competitor_reference")

            self.assertEqual(identity, first_main.resolve())

    def test_direct_reference_never_falls_back_to_sku(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sku = root / "sku.jpg"
            sku.write_bytes(b"sku")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "sku", "path": str(sku)}]}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "缺少商品身份主图"):
                resolve_identity_image(manifest, None, "competitor_reference")

    def test_own_product_requires_uploaded_image(self):
        with TemporaryDirectory() as directory:
            manifest = Path(directory) / "manifest.json"
            manifest.write_text(json.dumps({"images": []}), encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "请先上传我方产品图"):
                resolve_identity_image(manifest, None, "own_product")

    def test_generation_mode_defaults_to_competitor_reference(self):
        self.assertEqual(AgentSession().generation_mode, "competitor_reference")

    def test_agent_can_switch_to_own_product_mode(self):
        session = AgentSession()
        session.handle("使用我上传的产品图替换")

        self.assertEqual(session.generation_mode, "own_product")

    def test_restored_generation_mode_is_preserved(self):
        session = AgentSession(generation_mode="competitor_reference")

        self.assertEqual(session.generation_mode, "competitor_reference")

    def test_restored_complete_task_clears_stale_workflow_wait(self):
        session = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=123",
            max_main_images=None,
            main_quantity_mode="reference",
            quantity_confirmed=True,
            awaiting="workflow",
            workflows=("main", "sku", "detail"),
        )

        self.assertEqual(session.awaiting, "")

    def test_collection_only_intent_disables_generation(self):
        session = AgentSession()
        reply = session.apply_intent(
            {
                "action": "update_task",
                "reference_url": "https://detail.tmall.com/item.htm?id=123",
                "quantity_mode": "reference",
                "main_count": None,
                "workflows": ["main", "sku", "detail"],
                "generate_images": False,
                "reply": "将只采集全部图片，不生成图片。",
            }
        )

        self.assertEqual(reply.message, "将只采集全部图片，不生成图片。")
        self.assertFalse(session.generation_enabled)
        self.assertEqual(session.workflows, ("main", "sku", "detail"))
        self.assertEqual(session.awaiting, "")

    def test_quantity_without_workflow_prompts_for_image_types(self):
        session = AgentSession()
        reply = session.apply_intent(
            {
                "action": "update_task",
                "reference_url": "https://detail.tmall.com/item.htm?id=123",
                "quantity_mode": "reference",
                "main_count": None,
                "workflows": [],
                "generate_images": False,
                "reply": "已设置为按对标商品数量采集，不生成照片。",
            }
        )

        self.assertEqual(session.awaiting, "workflow")
        self.assertEqual(session.workflows, ())
        self.assertIn("选择采集类型", reply.message)

    def test_workflow_without_quantity_uses_ten_main_images(self):
        session = AgentSession(reference_url="https://detail.tmall.com/item.htm?id=123", awaiting="workflow")
        reply = session.apply_intent(
            {
                "action": "update_task",
                "reference_url": session.reference_url,
                "quantity_mode": "unspecified",
                "main_count": None,
                "workflows": ["main", "sku", "detail"],
                "generate_images": False,
                "reply": "请告知主图采集数量。",
            }
        )

        self.assertEqual(session.awaiting, "")
        self.assertEqual(session.workflows, ("main", "sku", "detail"))
        self.assertEqual(session.max_main_images, DEFAULT_MAIN_IMAGES)
        self.assertIn("默认主图生成 10 张", reply.message)

    def test_local_parser_understands_collection_only_request(self):
        session = AgentSession()
        session.handle("https://detail.tmall.com/item.htm?id=123")
        reply = session.handle("采集所有图片先，不要生成图片")

        self.assertFalse(session.generation_enabled)
        self.assertEqual(reply.workflows, ("main", "sku", "detail"))

    def test_collection_and_generation_scopes_are_separate(self):
        session = AgentSession()
        session.handle("https://detail.tmall.com/item.htm?id=123")

        reply = session.handle("采集对标全部类型，只生成主图和详情图")

        self.assertEqual(session.collection_types, ("main", "sku", "detail"))
        self.assertEqual(session.workflows, ("main", "detail"))
        self.assertEqual(reply.collection_types, ("main", "sku", "detail"))
        self.assertEqual(reply.workflows, ("main", "detail"))

    def test_only_need_generation_phrase_keeps_sku_out_of_generation_scope(self):
        session = AgentSession()
        session.handle("https://detail.tmall.com/item.htm?id=123")

        reply = session.handle("采集全部类型图片，只要生成主图和详情图")

        self.assertEqual(session.collection_types, ("main", "sku", "detail"))
        self.assertEqual(session.workflows, ("main", "detail"))
        self.assertEqual(reply.collection_types, ("main", "sku", "detail"))
        self.assertEqual(reply.workflows, ("main", "detail"))

    def test_legacy_session_uses_generation_scope_for_collection(self):
        session = AgentSession(workflows=("main", "detail"))

        self.assertEqual(session.collection_types, ("main", "detail"))

    def test_llm_intent_can_set_collection_and_generation_scopes_independently(self):
        session = AgentSession()

        session.apply_intent(
            {
                "action": "update_task",
                "reference_url": "https://detail.tmall.com/item.htm?id=123",
                "quantity_mode": "reference",
                "collection_types": ["main", "sku", "detail"],
                "workflows": ["main", "detail"],
                "generate_images": True,
            }
        )

        self.assertEqual(session.collection_types, ("main", "sku", "detail"))
        self.assertEqual(session.workflows, ("main", "detail"))

    def test_llm_intent_can_apply_url_quantity_and_workflows_together(self):
        session = AgentSession()
        reply = session.apply_intent(
            {
                "action": "update_task",
                "reference_url": "https://detail.tmall.com/item.htm?id=123",
                "quantity_mode": "custom",
                "main_count": 4,
                "workflows": ["main", "sku", "detail"],
                "reply": "已理解，准备采集并生成三类图片。",
            }
        )

        self.assertEqual(reply.message, "已理解，准备采集并生成三类图片。")
        self.assertEqual(session.max_main_images, 4)
        self.assertEqual(session.workflows, ("main", "sku", "detail"))
        self.assertEqual(session.awaiting, "")

    def test_llm_classifier_returns_structured_json(self):
        response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "action": "reset",
                                "reference_url": None,
                                "quantity_mode": "unspecified",
                                "main_count": None,
                                "workflows": [],
                                "reply": "已重新开始。",
                            }
                        )
                    }
                }
            ]
        }
        with patch("agent_flow.urlopen") as urlopen:
            urlopen.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
            intent = classify_message(
                "我要重新开始",
                {"agent": {"workflows": []}},
                "https://relay.example",
                "vision-key",
            )

        self.assertEqual(intent["action"], "reset")
        payload = json.loads(urlopen.call_args.args[0].data.decode())
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 12)

    def test_llm_classifier_wraps_connection_reset_as_fallback_error(self):
        with patch("agent_flow.urlopen", side_effect=ConnectionResetError("relay closed the connection")):
            with self.assertRaises(IntentRecognitionError):
                classify_message(
                    "测试连接",
                    {"agent": {"workflows": []}},
                    "https://relay.example",
                    "vision-key",
                )

    def test_llm_answer_intent_does_not_change_the_task(self):
        session = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=123",
            max_main_images=None,
            awaiting="",
            manifest_loaded=True,
            workflows=("main", "sku"),
        )

        reply = session.apply_intent(
            {
                "action": "answer",
                "reply": "Current task has main and SKU images.",
            }
        )

        self.assertEqual(reply.message, "Current task has main and SKU images.")
        self.assertEqual(session.workflows, ("main", "sku"))

    def test_url_is_required_before_any_other_request(self):
        session = AgentSession()
        reply = session.handle("只生成主图")
        self.assertIn("对标商品链接", reply.message)
        self.assertEqual(reply.state, "reference_url")

    def test_default_main_count_is_ten_after_a_url(self):
        session = AgentSession()
        reply = session.handle("https://detail.tmall.com/item.htm?id=123")
        self.assertEqual(reply.state, "workflow")
        self.assertEqual(reply.max_main_images, DEFAULT_MAIN_IMAGES)
        self.assertEqual(session.main_quantity_mode, "default")

    def test_douyin_short_url_is_accepted_as_a_single_reference(self):
        session = AgentSession()

        reply = session.handle("https://v.douyin.com/abc123")

        self.assertEqual(reply.state, "workflow")
        self.assertEqual(session.reference_url, "https://v.douyin.com/abc123")

    def test_tmall_global_url_is_accepted_as_a_single_reference(self):
        session = AgentSession()

        reply = session.handle("https://detail.tmall.hk/hk/item.htm?id=124")

        self.assertEqual(reply.state, "workflow")
        self.assertEqual(session.reference_url, "https://detail.tmall.hk/hk/item.htm?id=124")

    def test_taobao_share_urls_are_accepted_as_single_references(self):
        for url in ("https://m.tb.cn/h.first", "https://e.tb.cn/h.second"):
            with self.subTest(url=url):
                session = AgentSession()

                reply = session.handle(url)

                self.assertEqual(reply.state, "workflow")
                self.assertEqual(session.reference_url, url)

    def test_reference_count_can_override_the_default(self):
        session = AgentSession()
        session.handle("https://detail.tmall.com/item.htm?id=123")
        reply = session.handle("按对标数量")
        self.assertIsNone(reply.max_main_images)
        self.assertEqual(session.main_quantity_mode, "reference")
        self.assertEqual(reply.state, "workflow")

    def test_explicit_reference_count_overrides_llm_clarification(self):
        session = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=123",
            awaiting="workflow",
        )
        reply = session.apply_intent(
            {"action": "clarify", "quantity_mode": "unspecified", "workflows": []},
            "对标数量",
        )

        self.assertTrue(session.quantity_confirmed)
        self.assertIsNone(reply.max_main_images)
        self.assertEqual(reply.state, "workflow")

    def test_confirmed_reference_count_is_not_lost_when_selecting_all_types(self):
        session = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=123",
            awaiting="workflow",
        )
        session.apply_intent({"action": "clarify", "workflows": []}, "按对标数量")
        reply = session.apply_intent(
            {"action": "clarify", "quantity_mode": "unspecified", "workflows": []},
            "全部类型",
        )

        self.assertTrue(session.quantity_confirmed)
        self.assertEqual(reply.workflows, ("main", "sku", "detail"))
        self.assertEqual(reply.state, "")

    def test_local_parser_clears_workflow_wait_after_selecting_all_types(self):
        session = AgentSession()
        session.handle("https://detail.tmall.com/item.htm?id=123")
        session.handle("按对标数量")

        reply = session.handle("主图、SKU 图、详情图")

        self.assertEqual(reply.workflows, ("main", "sku", "detail"))
        self.assertEqual(reply.state, "")
        self.assertEqual(session.awaiting, "")

    def test_user_specified_main_count_wins(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")
        reply = session.handle("主图 3 张")
        self.assertEqual(reply.max_main_images, 3)
        self.assertEqual(session.main_quantity_mode, "custom")

    def test_unqualified_count_keeps_legacy_main_image_behavior(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        session.handle("生成 5 张图片")

        self.assertEqual(session.max_main_images, 5)

    def test_each_type_count_sets_all_workflow_generation_counts(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        reply = session.handle("生成全部类型的图片，每种类型 5 张")

        self.assertEqual(reply.workflows, ("main", "sku", "detail"))
        self.assertEqual(session.max_main_images, 5)
        self.assertEqual(session.max_sku_images, 5)
        self.assertEqual(session.max_detail_images, 5)

    def test_chinese_numeral_each_type_count_sets_all_workflow_generation_counts(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        session.handle("采集所有类型，每类五张")

        self.assertEqual(session.max_main_images, 5)
        self.assertEqual(session.max_sku_images, 5)
        self.assertEqual(session.max_detail_images, 5)

    def test_chinese_numerals_are_parsed_for_separate_workflow_counts(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        session.handle("主图三张、SKU图两张、详情图十五张")

        self.assertEqual(session.max_main_images, 3)
        self.assertEqual(session.max_sku_images, 2)
        self.assertEqual(session.max_detail_images, 15)

    def test_separate_workflow_counts_are_applied_independently(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        reply = session.handle("主图 3 张、SKU 图 2 张、详情图 4 张")

        self.assertEqual(reply.workflows, ("main", "sku", "detail"))
        self.assertEqual(session.max_main_images, 3)
        self.assertEqual(session.max_sku_images, 2)
        self.assertEqual(session.max_detail_images, 4)

    def test_sku_only_count_keeps_other_workflow_defaults(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        session.handle("SKU 图 2 张")

        self.assertEqual(session.max_main_images, DEFAULT_MAIN_IMAGES)
        self.assertEqual(session.max_sku_images, 2)
        self.assertIsNone(session.max_detail_images)

    def test_explicit_sku_count_overrides_llm_main_count_misclassification(self):
        session = AgentSession(
            reference_url="https://item.jd.com/100000.html",
            awaiting="workflow",
            quantity_confirmed=True,
        )

        session.apply_intent(
            {
                "action": "update_task",
                "quantity_mode": "custom",
                "main_count": 2,
                "sku_count": 2,
                "workflows": ["sku"],
            },
            "SKU 图 2 张",
        )

        self.assertEqual(session.max_main_images, DEFAULT_MAIN_IMAGES)
        self.assertEqual(session.max_sku_images, 2)

    def test_out_of_range_sku_and_detail_counts_are_rejected(self):
        session = AgentSession()
        session.handle("https://item.jd.com/100000.html")

        sku_reply = session.handle("SKU 图 9 张")
        detail_reply = session.handle("详情图 16 张")

        self.assertIn("SKU 图数量必须是 1 到 8 张", sku_reply.message)
        self.assertIn("详情图数量必须是 1 到 15 张", detail_reply.message)
        self.assertIsNone(session.max_sku_images)
        self.assertIsNone(session.max_detail_images)

    def test_workflows_are_selected_only_after_collection(self):
        session = AgentSession()
        session.handle("https://item.taobao.com/item.htm?id=123")
        session.handle("按对标数量")
        blocked = session.handle("主图和详情图一起运行")
        self.assertIn("自动开始采集", blocked.message)
        self.assertEqual(blocked.workflows, ("main", "detail"))
        session.mark_collected()
        reply = session.handle("主图和详情图一起运行")
        self.assertEqual(reply.workflows, ("main", "detail"))

    def test_main_limit_and_round_robin_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for category, count in (("main", 3), ("sku", 2), ("detail", 1)):
                for ordinal in range(count):
                    path = root / f"{category}-{ordinal}.jpg"
                    path.write_bytes(b"image")
                    images.append({"type": category, "path": str(path)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": images}), encoding="utf-8")
            tasks = load_manifest_tasks(manifest, max_main_images=2)
            ordered = round_robin_tasks(tasks)
            self.assertEqual([task.category for task in ordered], ["main", "sku", "detail", "main", "sku", "sku"])

    def test_main_target_repeats_reference_images_to_reach_requested_count(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "main-0.jpg"
            path.write_bytes(b"image")
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": [{"type": "main", "path": str(path)}]}), encoding="utf-8")

            tasks = load_manifest_tasks(manifest, categories=("main",), max_main_images=3)

            self.assertEqual([task.ordinal for task in tasks], [1, 2, 3])
            self.assertEqual([task.source_path for task in tasks], [path, path, path])

    def test_explicit_sku_and_detail_targets_cycle_available_sources(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            sku = root / "sku.jpg"
            detail = root / "detail.jpg"
            sku.write_bytes(b"sku")
            detail.write_bytes(b"detail")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "sku", "path": str(sku)},
                            {"type": "detail", "path": str(detail)},
                        ]
                    }
                ),
                encoding="utf-8",
            )

            tasks = load_manifest_tasks(
                manifest,
                categories=("sku", "detail"),
                max_sku_images=2,
                max_detail_images=1,
            )

            self.assertEqual([task.category for task in tasks], ["sku", "sku", "detail"])
            self.assertEqual([task.source_path for task in tasks], [sku, sku, detail])

    def test_single_explicit_detail_plan_is_valid(self):
        plans = validate_detail_view_plans(
            [
                {
                    "ordinal": 1,
                    "view_type": "front",
                    "focus": "整体正面",
                    "supporting_source_index": 1,
                }
            ],
            1,
            known_views={"front"},
            valid_source_indices={1},
            source_views={1: {"front"}},
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0].view_type, "front")

    def test_main_tasks_receive_distinct_composition_roles(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "main.jpg"
            path.write_bytes(b"main")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "main", "path": str(path)}]}),
                encoding="utf-8",
            )

            tasks = load_manifest_tasks(manifest, categories=("main",), max_main_images=10)

            self.assertEqual(len(tasks), 10)
            self.assertEqual(len({task.composition_role for task in tasks}), 10)
            self.assertIn("正面", tasks[0].composition_role)
            self.assertTrue(all(task.composition_role for task in tasks))

    def test_sku_tasks_are_clamped_between_three_and_eight(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            single = root / "sku-single.jpg"
            single.write_bytes(b"single")
            single_manifest = root / "single.json"
            single_manifest.write_text(
                json.dumps({"images": [{"type": "sku", "path": str(single)}]}),
                encoding="utf-8",
            )
            many_images = []
            for index in range(10):
                path = root / f"sku-{index}.jpg"
                path.write_bytes(str(index).encode())
                many_images.append({"type": "sku", "path": str(path)})
            many_manifest = root / "many.json"
            many_manifest.write_text(json.dumps({"images": many_images}), encoding="utf-8")

            minimum_tasks = load_manifest_tasks(single_manifest, categories=("sku",))
            maximum_tasks = load_manifest_tasks(many_manifest, categories=("sku",))

            self.assertEqual(len(minimum_tasks), 3)
            self.assertEqual(len(maximum_tasks), 8)
            self.assertEqual([task.source_path for task in minimum_tasks], [single, single, single])

    def test_each_workflow_has_a_distinct_generation_focus(self):
        instructions = {category: workflow_instruction(category) for category in WORKFLOW_PROFILES}
        self.assertEqual(set(instructions), {"main", "sku", "detail"})
        self.assertEqual(len(set(instructions.values())), 3)

    def test_dynamic_concurrency_uses_every_selected_task(self):
        self.assertEqual(resolve_worker_count(27, None), 27)
        self.assertEqual(resolve_worker_count(4, 10), 4)
        self.assertEqual(resolve_worker_count(4, 2), 2)


class EcommerceWorkflowFidelityTests(unittest.TestCase):
    def test_identity_source_analysis_returns_structured_observation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "main.jpg"
            path.write_bytes(b"main")
            manifest = path.parent / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "main", "path": str(path)}]}),
                encoding="utf-8",
            )
            source = load_identity_sources(manifest)[0]
            observation = {
                "source_index": 1,
                "category": "main",
                "visible_views": ["front"],
                "silhouette": "straight",
                "proportions": "balanced",
                "colors": ["white"],
                "materials": ["fabric"],
                "visible_components": ["body"],
                "local_details": ["woven texture"],
                "branding_and_risks": ["logo"],
                "uncertainties": ["back"],
            }
            response = {"choices": [{"message": {"content": json.dumps(observation)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                result = VisionClient(ApiSettings("https://relay", "vision", "image")).analyze_identity_source(source)

            self.assertEqual(result["source_index"], 1)
            payload = request_json.call_args.args[2]
            self.assertEqual(payload["messages"][1]["content"][1]["type"], "image_url")

    def test_dossier_synthesis_returns_validated_detail_plans(self):
        observations = [
            {
                "source_index": 1,
                "category": "main",
                "visible_views": ["front"],
                "visible_components": ["body"],
            }
        ]
        raw_plans = [
            {"ordinal": 1, "view_type": "front", "focus": "整体正面", "supporting_source_index": 1},
            {"ordinal": 2, "view_type": "side", "focus": "侧面轮廓", "supporting_source_index": None},
            {"ordinal": 3, "view_type": "material", "focus": "材质纹理", "supporting_source_index": 1},
            {"ordinal": 4, "view_type": "workmanship", "focus": "做工细节", "supporting_source_index": 1},
            {"ordinal": 5, "view_type": "scale", "focus": "比例展示", "supporting_source_index": 1},
            {"ordinal": 6, "view_type": "usage", "focus": "使用场景", "supporting_source_index": 1},
        ]
        dossier = {
            "anchor_identity": {"silhouette": "straight"},
            "confirmed_views": ["front"],
            "confirmed_components": ["body"],
            "materials_and_textures": ["woven"],
            "conflicts": [],
            "uncertainties": ["side", "back"],
            "detail_view_plans": raw_plans,
        }
        response = {"choices": [{"message": {"content": json.dumps(dossier)}}]}

        with patch("image_workflows._request_json", return_value=response):
            result, plans = VisionClient(ApiSettings("https://relay", "vision", "image")).synthesize_product_dossier(
                observations,
                target_count=6,
                valid_source_indices={1},
            )

        self.assertEqual(result["confirmed_views"], ["front"])
        self.assertEqual(len(plans), 6)
        self.assertTrue(plans[1].inferred_view)

    def setUp(self):
        self.settings = ApiSettings(
            base_url="https://relay.example",
            vision_api_key="vision-key",
            image_api_key="image-key",
        )
        self.analysis = {
            "product_fingerprint": {
                "silhouette": "rectangular retail package with a flip-top lid",
                "identity_invariants": ["single body", "white shell", "blue lid"],
                "uncertainties": [],
                "dispensing_state": {
                    "closure_state": "closed",
                    "outlet_exposed": False,
                    "verified_material_effect_origin": "none",
                },
            },
            "reference_visual_brief": {
                "scene_summary": "bright commercial tabletop scene",
                "composition": "product centered with clean negative space",
                "lighting": "soft studio light",
                "contains_replaceable_product": True,
                "visible_product_unit_count": 1,
                "primary_replaceable_product_unit_count": 1,
                "gift_or_bonus_elements": [],
                "physical_effects": [],
            },
            "compliance_risks": [
                {"type": "competitor_brand", "location": "upper-left badge"}
            ],
            "copy_plan": {
                "headline": "便携商品主视觉",
                "subheadline": "简洁展示 清晰卖点",
                "selling_points": [
                    {
                        "text": "单件主体",
                        "basis": "Image 1 visible evidence: one product package is visible",
                        "placement": "upper selling-point area",
                        "required_visual_evidence": "one complete product package",
                    },
                    {
                        "text": "翻盖结构",
                        "basis": "Image 1 visible evidence: a flip-top lid is directly visible",
                        "placement": "left circular badge",
                        "required_visual_evidence": "the visible flip-top lid",
                    },
                    {
                        "text": "清晰标签",
                        "basis": "Image 1 visible evidence: the label area is clearly visible",
                        "placement": "lower information strip",
                        "required_visual_evidence": "the visible label area",
                    },
                ],
                "layout_instruction": "Follow Image 2's headline and selling-point hierarchy.",
            },
            "generation_prompt": "Use the reference's bright tabletop atmosphere and framing.",
        }

    def test_vision_analysis_receives_identity_and_reference_images(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.png"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            response = {
                "choices": [{"message": {"content": json.dumps(self.analysis)}}]
            }

            with patch("image_workflows._request_json", return_value=response) as request_json:
                result = VisionClient(self.settings).analyze(product, reference, "main")

            payload = request_json.call_args.args[2]
            content = payload["messages"][1]["content"]
            instruction = content[0]["text"]
            image_parts = [part for part in content if part["type"] == "image_url"]
            self.assertEqual(result, self.analysis)
            self.assertEqual(len(image_parts), 2)
            self.assertIn("Image 1: main-identity", instruction)
            self.assertIn("Image 2: reference-style", instruction)
            self.assertIn("product_fingerprint", instruction)
            self.assertIn("compliance_risks", instruction)
            self.assertIn("copy_plan", instruction)
            self.assertIn("category-neutral", instruction)
            self.assertIn("both Image 1 and Image 2", instruction)

    def test_direct_reference_prompt_freezes_product_and_only_edits_outside_it(self):
        view_plan = DetailViewPlan(2, "side", "侧面轮廓", None, True, ("ports", "buttons"))
        dossier = {"confirmed_views": ["front"], "confirmed_components": ["body"]}

        prompt = compose_generation_prompt(
            self.analysis,
            "detail",
            "competitor_reference",
            dossier,
            view_plan,
        )

        lowered = prompt.lower()
        self.assertIn("preserve image 1's exact product model", lowered)
        self.assertIn("product region is immutable", lowered)
        self.assertIn("brand, logo, packaging label, printed text, artwork, badges", lowered)
        self.assertIn("compliance cleanup must never modify product pixels", lowered)
        self.assertIn("only outside the product region", lowered)
        self.assertIn("do not place new copy on the product or its packaging", lowered)
        self.assertIn("background, environmental lighting, depth, shadows, and reflections", lowered)
        self.assertIn("must not redraw, retouch, relight, sharpen, or beautify the product itself", lowered)
        self.assertIn("inferred_view: true", prompt)
        self.assertNotIn("replacing every competitor product", prompt)

    def test_direct_reference_analysis_normalizes_one_compliance_risk_object(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            analysis = json.loads(json.dumps(self.analysis))
            risk = {
                "source_image": "Image 1",
                "type": "competitor_brand",
                "location": "neck label",
                "removal_instruction": "remove the mark",
            }
            analysis["compliance_risks"] = risk
            response = {"choices": [{"message": {"content": json.dumps(analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                result = VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "detail",
                    generation_mode="competitor_reference",
                )

            instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"]
            self.assertEqual(result["reported_compliance_risks"], [risk])
            self.assertEqual(result["compliance_risks"], [])
            self.assertIn("compliance_risks must be an array", instruction)

    def test_direct_reference_review_keeps_explicit_risk_variants(self):
        analysis = json.loads(json.dumps(self.analysis))
        risks = [
            {
                "source_image": "Image 1",
                "original_text": text,
                "risk_code": risk_code,
                "location": "off_product editable: badge",
                "decision": "remove",
                "reason": "Explicit prohibited claim",
                "removal_instruction": "Remove the dedicated badge",
            }
            for text, risk_code in (
                ("FDA", "patent_or_certification"),
                ("日本原产", "origin_or_import"),
                ("医疗级", "medical_treatment"),
                ("顶级", "absolute_or_ranking"),
            )
        ]
        analysis["compliance_risks"] = risks

        _normalize_direct_reference_analysis(analysis, "main", None)

        self.assertEqual(analysis["compliance_risks"], risks)

    def test_direct_reference_analysis_protects_product_surface_from_compliance_edits(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            response = {"choices": [{"message": {"content": json.dumps(self.analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "main",
                    generation_mode="competitor_reference",
                )

            instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"].lower()
            self.assertIn("product region is immutable", instruction)
            self.assertIn("report on-product risks as protected", instruction)
            self.assertIn("only off-product risks are eligible for removal", instruction)
            self.assertIn("do not generate new copy on the product or packaging", instruction)

    def test_direct_reference_analysis_refreshes_any_visible_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            response = {"choices": [{"message": {"content": json.dumps(self.analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "main",
                    generation_mode="competitor_reference",
                )

            instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"].lower()
            self.assertIn("any visible human model or person", instruction)
            self.assertIn("ordinary models are also replacement targets", instruction)
            self.assertIn("when no person is present, do not add a person", instruction)
            self.assertNotIn("do not flag an ordinary unidentified model", instruction)

    def test_analysis_requires_refresh_for_any_visible_reference_model(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            response = {"choices": [{"message": {"content": json.dumps(self.analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "main",
                    generation_mode="own_product",
                )

            instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"].lower()

        self.assertIn("any visible human model or person", instruction)
        self.assertIn("ordinary models are also replacement targets", instruction)
        self.assertIn("when no person is present, do not add a person", instruction)
        self.assertNotIn("do not flag an ordinary unidentified model", instruction)

    def test_direct_reference_prompt_refreshes_model_without_touching_product(self):
        prompt = compose_generation_prompt(
            self.analysis,
            "main",
            "competitor_reference",
            None,
            None,
        ).lower()

        self.assertIn("any visible human model or person", prompt)
        self.assertIn("distinct fictional, non-identifiable ai person", prompt)
        self.assertIn("preserve only the general pose, action, framing", prompt)
        self.assertIn("do not preserve the original model unchanged", prompt)
        self.assertIn("do not imitate the original person's face", prompt)
        self.assertIn("if the reference contains no person, do not add a person", prompt)
        self.assertIn("celebrity name, endorsement, recommendation, or same-style", prompt)
        self.assertIn("must not modify any product pixel", prompt)

    def test_direct_reference_prompt_refreshes_ordinary_model_without_touching_product(self):
        prompt = compose_generation_prompt(
            self.analysis,
            "main",
            "competitor_reference",
            None,
            None,
        ).lower()

        self.assertIn("any visible human model or person", prompt)
        self.assertIn("do not preserve the original model unchanged", prompt)
        self.assertIn("change the face, facial proportions, hairstyle, hair color, makeup", prompt)
        self.assertIn("preserve only the general pose, action, framing", prompt)
        self.assertIn("if the reference contains no person, do not add a person", prompt)
        self.assertIn("must not modify any product pixel", prompt)

    def test_direct_reference_analysis_sends_current_task_image_first(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "anchor.jpg"
            current = root / "current-sku.jpg"
            anchor.write_bytes(b"anchor")
            current.write_bytes(b"current")
            response = {"choices": [{"message": {"content": json.dumps(self.analysis)}}]}

            with (
                patch("image_workflows._image_data_url", side_effect=lambda path: f"encoded:{path.name}"),
                patch("image_workflows._request_json", return_value=response) as request_json,
            ):
                VisionClient(self.settings).analyze(
                    anchor,
                    current,
                    "sku",
                    generation_mode="competitor_reference",
                )

            content = request_json.call_args.args[2]["messages"][1]["content"]
            image_urls = [part["image_url"]["url"] for part in content if part["type"] == "image_url"]
            self.assertEqual(image_urls, ["encoded:current-sku.jpg", "encoded:anchor.jpg"])

    def test_direct_reference_analysis_normalizes_layout_zone_copy_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            analysis = json.loads(json.dumps(self.analysis))
            analysis["copy_plan"] = {
                "section_title": "版型与细节",
                "ordinary_copy_zones": [
                    {
                        "zone": "detail_notes",
                        "purpose": "商品细节说明",
                        "new_copy": ["宽松落肩版型", "圆领短袖结构"],
                        "notes": "来自商品可见结构",
                    }
                ],
            }
            response = {"choices": [{"message": {"content": json.dumps(analysis)}}]}

            with patch("image_workflows._request_json", return_value=response):
                result = VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "detail",
                    generation_mode="competitor_reference",
                )

            self.assertEqual(result["copy_plan"]["headline"], "版型与细节")
            self.assertEqual(result["copy_plan"]["selling_points"][0]["text"], "宽松落肩版型")
            self.assertEqual(result["copy_plan"]["selling_points"][0]["basis"], "来自商品可见结构")
            self.assertEqual(result["copy_plan"]["selling_points"][0]["placement"], "detail_notes")

    def test_direct_reference_analysis_builds_missing_generation_direction_from_view_plan(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            analysis = json.loads(json.dumps(self.analysis))
            analysis["generation_prompt"] = ""
            response = {"choices": [{"message": {"content": json.dumps(analysis)}}]}
            view_plan = DetailViewPlan(1, "front worn view", "show the overall silhouette", 1, False, ())

            with patch("image_workflows._request_json", return_value=response):
                result = VisionClient(self.settings).analyze(
                    identity,
                    reference,
                    "detail",
                    generation_mode="competitor_reference",
                    view_plan=view_plan,
                )

            self.assertIn("front worn view", result["generation_prompt"])
            self.assertIn("show the overall silhouette", result["generation_prompt"])

    def test_own_product_prompt_keeps_replacement_contract(self):
        prompt = compose_generation_prompt(self.analysis, "main", "own_product", None, None)

        self.assertIn("only source of product identity", prompt)
        self.assertIn("replacing every detected competitor product", prompt)

    def test_own_product_prompt_freezes_user_product_and_original_label(self):
        prompt = compose_generation_prompt(self.analysis, "main", "own_product", None, None)

        self.assertIn("Image 1 is a locked photographic product identity", prompt)
        self.assertIn("Do not erase, blur, rewrite, translate, or redraw any Image 1 product or packaging label", prompt)
        self.assertIn("Only Image 2 off-product regions and the replaced competitor product may be edited", prompt)
        self.assertNotIn("Inspect and remove prohibited content from both Image 1 and Image 2", prompt)

    def test_own_product_main_prompt_does_not_force_unverified_product_angle_changes(self):
        analysis = json.loads(json.dumps(self.analysis))
        analysis["reference_visual_brief"]["contains_replaceable_product"] = True

        prompt = compose_generation_prompt(
            analysis,
            "main",
            "own_product",
            None,
            None,
            composition_role="三分之二侧向主视觉，建立不同于前图的空间层次",
        )

        self.assertIn("Do not rotate, mirror, reshape, or redraw the product to create a new angle", prompt)
        self.assertIn("changing only the non-product composition, background", prompt)

    def test_own_product_prompt_does_not_insert_product_when_reference_has_none(self):
        analysis = json.loads(json.dumps(self.analysis))
        analysis["reference_visual_brief"]["contains_replaceable_product"] = False

        prompt = compose_generation_prompt(
            analysis,
            "detail",
            "own_product",
            None,
            None,
            composition_role="细节信息承接图",
        )

        self.assertIn("Reference product-presence decision: false", prompt)
        self.assertIn("Do not insert the user's product", prompt)
        self.assertIn("Preserve the reference image without adding a product subject", prompt)
        self.assertIn("Image 1: reference-style", prompt)
        self.assertIn("user's product image is intentionally excluded", prompt)
        self.assertNotIn("Image 2: reference-style", prompt)
        self.assertNotIn("Product fingerprint from Image 1", prompt)
        self.assertNotIn("replacing every detected competitor product", prompt)

    def test_own_product_main_prompt_includes_planned_composition_role(self):
        analysis = json.loads(json.dumps(self.analysis))
        analysis["reference_visual_brief"]["contains_replaceable_product"] = True

        prompt = compose_generation_prompt(
            analysis,
            "main",
            "own_product",
            None,
            None,
            composition_role="三分之二侧向主视觉，建立不同于前图的空间层次",
        )

        self.assertIn("Planned composition role", prompt)
        self.assertIn("三分之二侧向主视觉", prompt)
        self.assertIn("must be visibly distinct from adjacent main images", prompt)

    def test_own_product_analysis_requires_reference_presence_and_product_only_copy_evidence(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            analysis = json.loads(json.dumps(self.analysis))
            analysis["reference_visual_brief"]["contains_replaceable_product"] = True
            response = {"choices": [{"message": {"content": json.dumps(analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                VisionClient(self.settings).analyze(product, reference, "main")

            instruction = request_json.call_args.args[2]["messages"][1]["content"][0]["text"]
            self.assertIn("contains_replaceable_product", instruction)
            self.assertIn("Image 2 must never be used as factual evidence", instruction)
            self.assertIn("specific visible label, color, shape, component", instruction)

    def test_own_product_analysis_retries_when_product_logic_contract_is_missing(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            analysis = json.loads(json.dumps(self.analysis))
            del analysis["product_fingerprint"]["dispensing_state"]
            del analysis["reference_visual_brief"]["primary_replaceable_product_unit_count"]
            del analysis["reference_visual_brief"]["gift_or_bonus_elements"]
            del analysis["reference_visual_brief"]["physical_effects"]
            for selling_point in analysis["copy_plan"]["selling_points"]:
                del selling_point["required_visual_evidence"]
            response = {"choices": [{"message": {"content": json.dumps(analysis)}}]}

            with patch("image_workflows._request_json", return_value=response) as request_json:
                with self.assertRaisesRegex(RuntimeError, "dispensing_state"):
                    VisionClient(self.settings).analyze(product, reference, "main")

            self.assertEqual(request_json.call_count, 3)

    def test_own_product_runner_omits_identity_image_when_reference_has_no_product(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            task = ImageTask("detail", 1, reference)
            analysis = json.loads(json.dumps(self.analysis))
            analysis["reference_visual_brief"]["contains_replaceable_product"] = False
            runner = WorkflowRunner(self.settings)

            with (
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()) as generate,
                patch("image_workflows.VisionClient.review_generated", create=True) as review,
            ):
                record = runner._run_task(task, product, root / "generated")

            self.assertEqual(record["status"], "completed")
            self.assertEqual(generate.call_args.args[0], [reference.resolve()])
            self.assertEqual(generate.call_count, 1)
            review.assert_not_called()
            self.assertFalse(record["reference_contains_product"])

    def test_all_workflow_prompts_request_2k_quality_without_inventing_texture(self):
        for mode in ("own_product", "competitor_reference"):
            for category in ("main", "sku", "detail"):
                with self.subTest(mode=mode, category=category):
                    prompt = compose_generation_prompt(
                        self.analysis,
                        category,
                        mode,
                        None,
                        None,
                    ).lower()

                    self.assertIn("2k-class high-definition", prompt)
                    self.assertIn("no oversharpening", prompt)
                    self.assertIn("do not invent micro-texture", prompt)
                    self.assertIn("highlights must not clip", prompt)
                    self.assertIn("shadows must retain natural detail", prompt)

    def test_direct_reference_2k_quality_rule_cannot_modify_product(self):
        prompt = compose_generation_prompt(
            self.analysis,
            "main",
            "competitor_reference",
            None,
            None,
        ).lower()

        self.assertIn("2k-class quality applies only to the finished canvas and editable non-product regions", prompt)
        self.assertIn("must not trigger sharpening, texture synthesis, relighting, or redrawing inside the product region", prompt)

    def test_generation_images_keep_current_reference_first(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "anchor.jpg"
            support = root / "support.jpg"
            reference = root / "reference.jpg"

            self.assertEqual(
                ordered_generation_images(reference, support, anchor),
                [reference.resolve(), support.resolve(), anchor.resolve()],
            )

    def test_generation_images_deduplicate_same_path(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "anchor.jpg"
            reference = root / "reference.jpg"

            self.assertEqual(
                ordered_generation_images(reference, anchor, anchor),
                [reference.resolve(), anchor.resolve()],
            )

    def test_direct_reference_prompt_locks_identity_to_current_image(self):
        prompt = compose_generation_prompt(
            self.analysis,
            "sku",
            "competitor_reference",
            {"confirmed_views": ["front"]},
            None,
        )

        self.assertIn("Image 1: current-task-reference", prompt)
        self.assertIn("sole authority for the exact SKU, color, quantity", prompt)
        self.assertIn("must not override or transfer product traits", prompt)
        self.assertIn("Product region is immutable", prompt)
        self.assertIn("Do not transfer color, quantity, packaging, components, or structure", prompt)
        self.assertIn(
            "Create a premium commercial atmosphere only through the non-product background, environmental "
            "lighting, depth, shadows, and reflections",
            prompt,
        )

    def test_sku_prompts_remove_off_product_copy_and_keep_product_labels(self):
        for mode in ("own_product", "competitor_reference"):
            with self.subTest(mode=mode):
                prompt = compose_generation_prompt(
                    self.analysis,
                    "sku",
                    mode,
                    None,
                    None,
                ).lower()

                self.assertIn("no off-product marketing copy", prompt)
                self.assertIn("preserve authentic text printed on the product or packaging", prompt)
                self.assertIn("remove off-product titles, selling points, parameter notes", prompt)
                self.assertIn("category, material, color, target customer, usage context, and commercial positioning", prompt)
                self.assertNotIn("a clear title and one to three selling points are mandatory", prompt)

    def test_direct_reference_detail_prompt_rejects_unsupported_angles(self):
        view_plan = DetailViewPlan(2, "side", "show side structure", None, True, ("ports",))

        prompt = compose_generation_prompt(
            self.analysis,
            "detail",
            "competitor_reference",
            {"confirmed_views": ["front"]},
            view_plan,
        )

        self.assertIn("Never invent an unseen angle or hidden structure", prompt)
        self.assertIn("use a close-up of verified material, texture, workmanship, scale, or usage", prompt)

    def test_direct_reference_runner_generates_with_current_task_image_first(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            anchor = root / "anchor.jpg"
            support = root / "support.jpg"
            current = root / "current-sku.jpg"
            for path in (anchor, support, current):
                path.write_bytes(path.name.encode())
            task = ImageTask("sku", 1, current, supporting_path=support)
            runner = WorkflowRunner(self.settings)

            with (
                patch("image_workflows.VisionClient.analyze", return_value=self.analysis),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()) as generate,
            ):
                record = runner._run_task(
                    task,
                    anchor,
                    root / "generated",
                    generation_mode="competitor_reference",
                )

            self.assertEqual(record["status"], "completed")
            self.assertEqual(
                generate.call_args.args[0],
                [current.resolve(), support.resolve(), anchor.resolve()],
            )

    def test_image_generation_sends_both_images_in_product_first_order(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.png"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")

            with patch("image_workflows._request_image", return_value=b"png") as request_image:
                value = ImageClient(self.settings).generate([product, reference], "prompt")

            self.assertEqual(value, b"png")
            self.assertEqual(request_image.call_args.args[4], [product, reference])

            body, _ = _multipart_body({}, [("image[]", product), ("image[]", reference)])
            self.assertEqual(body.count(b'name="image[]"'), 2)
            self.assertLess(body.index(b'filename="product.png"'), body.index(b'filename="reference.jpg"'))

    def test_final_prompt_enforces_fidelity_and_compliance(self):
        prompt = compose_generation_prompt(self.analysis, "main")
        self.assertIn("Image 1: main-identity", prompt)
        self.assertIn("Image 2: reference-style", prompt)
        self.assertIn("Fidelity: A", prompt)
        self.assertIn("competitor brands", prompt)
        self.assertIn("Do not invent", prompt)
        self.assertIn("Compliance rules override", prompt)
        self.assertIn("both Image 1 and Image 2", prompt)
        self.assertIn("category-neutral", prompt)
        self.assertIn("rectangular retail package with a flip-top lid", prompt)
        self.assertIn("A clear title and one to three selling points are mandatory", prompt)
        self.assertIn("便携商品主视觉", prompt)
        self.assertIn("单件主体", prompt)
        self.assertNotIn("冰箱", prompt)
        self.assertNotIn("除味盒", prompt)

    def test_completed_record_preserves_auditable_fidelity_metadata(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.png"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            task = ImageTask("main", 1, reference)
            runner = WorkflowRunner(self.settings)

            with (
                patch("image_workflows.VisionClient.analyze", return_value=self.analysis) as analyze,
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()) as generate,
            ):
                record = runner._run_task(task, product, root / "generated")

            self.assertEqual(record["status"], "completed")
            self.assertEqual(record["fidelity"], "A")
            self.assertEqual(record["product_fingerprint"], self.analysis["product_fingerprint"])
            self.assertEqual(record["reference_visual_brief"], self.analysis["reference_visual_brief"])
            self.assertEqual(record["copy_plan"], self.analysis["copy_plan"])
            self.assertIn("Fidelity: A", record["generation_prompt"])
            analyze.assert_called_once_with(product, reference, "main")
            generate.assert_called_once_with(
                [product.resolve(), reference.resolve()],
                record["generation_prompt"],
            )

    def test_own_product_runner_generates_once_without_semantic_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.png"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            runner = WorkflowRunner(self.settings)

            with (
                patch("image_workflows.VisionClient.analyze", return_value=self.analysis),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()) as generate,
                patch("image_workflows.VisionClient.review_generated", create=True) as review,
            ):
                record = runner._run_task(
                    ImageTask("main", 1, reference), product, root / "generated"
                )

            self.assertEqual(record["status"], "completed")
            self.assertTrue(Path(record["output_path"]).is_file())
            self.assertEqual(generate.call_count, 1)
            review.assert_not_called()
            self.assertNotIn("quality_review", record)
            self.assertNotIn("generation_attempts", record)
            self.assertEqual(list((root / "generated" / "main").glob(".*.candidate-*.jpg")), [])

    def test_competitor_reference_runner_does_not_use_own_product_quality_review(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            identity = root / "identity.jpg"
            reference = root / "reference.jpg"
            identity.write_bytes(b"identity")
            reference.write_bytes(b"reference")
            runner = WorkflowRunner(self.settings)

            with (
                patch("image_workflows.VisionClient.analyze", return_value=self.analysis),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
                patch("image_workflows.VisionClient.review_generated", create=True) as review,
            ):
                record = runner._run_task(
                    ImageTask("main", 1, reference),
                    identity,
                    root / "generated",
                    generation_mode="competitor_reference",
                )

            self.assertEqual(record["status"], "completed")
            self.assertNotIn("quality_review", record)
            review.assert_not_called()

    def test_direct_reference_runner_builds_one_dossier_and_schedules_detail_plans(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jpg"
            detail = root / "detail.jpg"
            main.write_bytes(b"main")
            detail.write_bytes(b"detail")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "main", "path": str(main)},
                            {"type": "detail", "path": str(detail)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            plans = [
                DetailViewPlan(index, "front" if index == 1 else f"detail-{index}", f"focus-{index}", 1, False, ())
                for index in range(1, 7)
            ]
            dossier = {"confirmed_views": ["front"], "confirmed_components": ["body"]}
            runner = WorkflowRunner(self.settings)

            def complete(task, *_args, **_kwargs):
                return {"category": task.category, "ordinal": task.ordinal, "status": "completed"}

            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.analyze_identity_sources", return_value=([{"source_index": 1}], [])),
                patch(
                    "image_workflows.VisionClient.synthesize_product_dossier",
                    return_value=(dossier, plans),
                ) as synthesize,
                patch.object(runner, "_prepare_task", side_effect=lambda task, *_args, **_kwargs: ({}, "generate")),
                patch.object(runner, "_run_task", side_effect=complete) as run_task,
            ):
                records = runner.run(
                    manifest,
                    None,
                    root / "generated",
                    None,
                    categories=("main", "detail"),
                    generation_mode="competitor_reference",
                    identity_image=main,
                )

            self.assertEqual(len(records), 7)
            self.assertEqual(sum(record["category"] == "detail" for record in records), 6)
            synthesize.assert_called_once_with([{"source_index": 1}], 6, {1, 2})
            detail_calls = [call for call in run_task.call_args_list if call.args[0].category == "detail"]
            self.assertEqual(
                sorted((call.args[0].view_plan for call in detail_calls), key=lambda plan: plan.ordinal),
                plans,
            )
            self.assertTrue((root / "generated" / "product-dossier.json").is_file())

    def test_direct_reference_dossier_failure_keeps_original_detail_tasks(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {name: root / f"{name}.jpg" for name in ("main", "sku", "detail")}
            for name, path in paths.items():
                path.write_bytes(name.encode())
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {"images": [{"type": name, "path": str(path)} for name, path in paths.items()]}
                ),
                encoding="utf-8",
            )
            events = []
            runner = WorkflowRunner(self.settings, callback=events.append)

            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.analyze_identity_sources", return_value=([{"source_index": 1}], [])),
                patch(
                    "image_workflows.VisionClient.synthesize_product_dossier",
                    side_effect=RuntimeError("dossier failed"),
                ),
                patch.object(runner, "_prepare_task", side_effect=lambda task, *_args, **_kwargs: ({}, "generate")),
                patch.object(
                    runner,
                    "_run_task",
                    side_effect=lambda task, *_args, **_kwargs: {
                        "category": task.category,
                        "ordinal": task.ordinal,
                        "status": "completed",
                    },
                ),
            ):
                records = runner.run(
                    manifest,
                    None,
                    root / "generated",
                    None,
                    generation_mode="competitor_reference",
                    identity_image=paths["main"],
                )

            self.assertEqual({record["category"] for record in records}, {"main", "sku", "detail"})
            self.assertTrue(any(event["status"] == "detail_dossier_failed" for event in events))

    def test_cancellation_during_identity_analysis_prevents_generation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            main = root / "main.jpg"
            detail = root / "detail.jpg"
            main.write_bytes(b"main")
            detail.write_bytes(b"detail")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "images": [
                            {"type": "main", "path": str(main)},
                            {"type": "detail", "path": str(detail)},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            events = []
            runner = WorkflowRunner(self.settings, callback=events.append)

            def cancel_during_analysis(*_args, **_kwargs):
                runner.cancel()
                return [], []

            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.analyze_identity_sources", side_effect=cancel_during_analysis),
                patch.object(runner, "_run_task") as run_task,
            ):
                records = runner.run(
                    manifest,
                    None,
                    root / "generated",
                    None,
                    categories=("detail",),
                    generation_mode="competitor_reference",
                    identity_image=main,
                )

            self.assertEqual(records, [])
            run_task.assert_not_called()
            self.assertTrue(any(event["status"] == "cancelled" for event in events))


if __name__ == "__main__":
    unittest.main()
    analyze_identity_sources,
    validate_detail_view_plans,
