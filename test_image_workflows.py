import unittest
import base64
import json
import ssl
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.client import RemoteDisconnected
from http.client import IncompleteRead
from io import BytesIO
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

from PIL import Image

import image_workflows


def _test_png_bytes(size: tuple[int, int] = (32, 32)) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", size, (35, 80, 125)).save(buffer, format="PNG")
    return buffer.getvalue()


class PromptCompositionTests(unittest.TestCase):
    @staticmethod
    def _analysis(contains_product: bool) -> dict:
        return {
            "product_fingerprint": {"visible_category": "test product"},
            "reference_visual_brief": {
                "contains_replaceable_product": contains_product,
                "composition": "reference composition",
            },
            "compliance_risks": [],
            "copy_plan": {
                "headline": "PRODUCT COPY FROM ANALYSIS",
                "subheadline": "",
                "selling_points": [
                    {
                        "text": "SELLING POINT FROM ANALYSIS",
                        "basis": "Image 1 visible evidence: test",
                        "placement": "top",
                    }
                ],
                "layout_instruction": "Preserve the reference layout",
            },
            "generation_prompt": "ADD THE USER PRODUCT NOW",
        }

    def test_product_free_reference_omits_conflicting_product_insertion_instructions(self):
        prompt = image_workflows.compose_generation_prompt(
            self._analysis(False),
            "main",
            "own_product",
        )

        self.assertIn("Do not insert the user's product", prompt)
        self.assertNotIn("ADD THE USER PRODUCT NOW", prompt)
        self.assertNotIn("PRODUCT COPY FROM ANALYSIS", prompt)
        self.assertIn("human presence and general pose", prompt)
        self.assertIn("any visible human model or person", prompt)
        self.assertNotIn("ordinary people", prompt)

    def test_own_product_sku_uses_reference_quantity_but_keeps_unit_identity(self):
        analysis = self._analysis(True)
        analysis["reference_visual_brief"]["visible_product_unit_count"] = 3
        analysis["generation_prompt"] = (
            "Preserve the exact single product from Image 1 and do not change quantity."
        )
        prompt = image_workflows.compose_generation_prompt(
            analysis,
            "sku",
            "own_product",
        )

        self.assertIn("Image 1 is the sole authority for each individual product unit", prompt)
        self.assertIn("Image 2 is the sole authority for the visible unit count", prompt)
        self.assertIn("EXACT TARGET UNIT COUNT: 3", prompt)
        self.assertIn("overrides every conflicting quantity statement", prompt)
        self.assertNotIn("focused on this single SKU", prompt)

    def test_reference_models_are_refreshed_in_both_generation_modes(self):
        for mode in ("own_product", "competitor_reference"):
            with self.subTest(mode=mode):
                prompt = image_workflows.compose_generation_prompt(
                    self._analysis(True),
                    "main",
                    mode,
                    None,
                    None,
                ).lower()

                self.assertIn("any visible human model or person", prompt)
                self.assertIn("distinct fictional, non-identifiable ai person", prompt)
                self.assertIn("preserve only the general pose, action, framing", prompt)
                self.assertIn("do not preserve the original model unchanged", prompt)
                self.assertIn("if the reference contains no person, do not add a person", prompt)
                self.assertIn("must not modify any product pixel", prompt)

    def test_direct_reference_main_renders_approved_copy_plan(self):
        prompt = image_workflows.compose_generation_prompt(
            self._analysis(True), "main", "competitor_reference"
        )

        self.assertIn("PRODUCT COPY FROM ANALYSIS", prompt)
        self.assertIn("SELLING POINT FROM ANALYSIS", prompt)
        self.assertIn("one to three selling points are mandatory", prompt.lower())
        self.assertIn("Reuse Image 1's current-task-reference information-zone hierarchy", prompt)
        self.assertNotIn("Reuse Image 2's headline scale", prompt)
        self.assertNotIn("Analysis copy plan for audit only", prompt)
        self.assertNotIn("Do not render new copy from the analysis copy_plan", prompt)

    def test_direct_reference_detail_renders_approved_copy_plan(self):
        prompt = image_workflows.compose_generation_prompt(
            self._analysis(True), "detail", "competitor_reference"
        )

        self.assertIn("PRODUCT COPY FROM ANALYSIS", prompt)
        self.assertIn("SELLING POINT FROM ANALYSIS", prompt)
        self.assertIn("Reuse Image 1's current-task-reference information-zone hierarchy", prompt)
        self.assertNotIn("Reuse Image 2's headline scale", prompt)
        self.assertNotIn("Analysis copy plan for audit only", prompt)
        self.assertNotIn("Do not render new copy from the analysis copy_plan", prompt)

    def test_manual_sku_condition_is_preserved_in_task_and_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            image = root / "product.jpg"
            image.write_bytes(b"image")
            manifest = root / "manifest.json"
            manual_sku = {
                "sku_name": "银色 500ml",
                "color": "银色",
                "spec": "500ml",
                "price": "39.90",
                "source_status": "text_conditioned",
            }
            manifest.write_text(
                json.dumps({"images": [{"type": "sku", "path": str(image), "manual_sku": manual_sku}]}),
                encoding="utf-8",
            )

            tasks = image_workflows.load_manifest_tasks(manifest, ("sku",))
            analysis = self._analysis(True)
            analysis["reference_visual_brief"]["visible_product_unit_count"] = 1
            prompt = image_workflows.compose_generation_prompt(
                analysis,
                "sku",
                "own_product",
                sku_condition=tasks[0].manual_sku,
            )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].manual_sku["spec"], "500ml")
        self.assertIn("银色 500ml", prompt)
        self.assertIn("text_conditioned", prompt)
        self.assertIn("Do not render the price", prompt)

    def test_screenshot_sku_prompt_does_not_treat_thumbnail_as_fine_detail_authority(self):
        analysis = self._analysis(True)
        analysis["reference_visual_brief"]["visible_product_unit_count"] = 1
        prompt = image_workflows.compose_generation_prompt(
            analysis,
            "sku",
            "competitor_reference",
            sku_condition={
                "sku_name": "蓝色 500ml",
                "color": "蓝色",
                "spec": "500ml",
                "price": "39.90",
                "source_status": "screenshot_thumbnail",
                "visual_confidence": 0.9,
            },
        )

        self.assertIn("cropped, low-resolution screenshot thumbnail", prompt)
        self.assertIn("collected main product image is the authority", prompt)
        self.assertIn("Never enlarge or invent unreadable screenshot text or texture", prompt)


    def test_sku_screenshot_quality_gate_rejects_unreliable_thumbnails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            screenshot = root / "sku-screenshot.png"
            Image.new("RGB", (220, 120), (120, 140, 160)).save(screenshot)
            analysis = {
                "skus": [
                    {
                        "sku_name": "蓝色 500ml",
                        "color": "蓝色",
                        "spec": "500ml",
                        "price": "39.90",
                        "confidence": 0.95,
                        "is_clear": True,
                        "thumbnail": {"x": 5, "y": 10, "width": 80, "height": 80},
                    },
                    {
                        "sku_name": "红色 500ml",
                        "color": "红色",
                        "spec": "500ml",
                        "price": "39.90",
                        "confidence": 0.92,
                        "is_clear": False,
                        "thumbnail": {"x": 110, "y": 10, "width": 40, "height": 40},
                    },
                ]
            }

            variants = image_workflows.materialize_sku_screenshot_references(
                screenshot,
                analysis,
                root / "references",
            )

            self.assertEqual(len(variants), 2)
            self.assertTrue(Path(variants[0]["reference_image"]).is_file())
            with Image.open(variants[0]["reference_image"]) as reference:
                self.assertGreaterEqual(max(reference.size), 512)
            self.assertEqual(variants[0]["source_status"], "screenshot_thumbnail")
            self.assertEqual(variants[1]["reference_image"], "")
            self.assertEqual(variants[1]["source_status"], "low_visual_confidence")

    def test_vision_client_normalizes_sku_screenshot_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            screenshot = Path(directory) / "sku-screenshot.png"
            screenshot.write_bytes(_test_png_bytes((120, 120)))
            response = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "skus": [
                                        {
                                            "name": "蓝色 500ml",
                                            "color": "蓝色",
                                            "specification": "500ml",
                                            "price": "39.90",
                                            "confidence": 0.8,
                                            "is_clear": True,
                                            "thumbnail": {"x": 1, "y": 2, "width": 80, "height": 80},
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            )
                        }
                    }
                ]
            }
            with patch("image_workflows._request_json", return_value=response):
                result = image_workflows.VisionClient(
                    image_workflows.ApiSettings("https://api.example", "vision", "image")
                ).analyze_sku_screenshot(screenshot)

        self.assertEqual(result["skus"][0]["sku_name"], "蓝色 500ml")
        self.assertEqual(result["skus"][0]["spec"], "500ml")
        self.assertEqual(result["skus"][0]["thumbnail"]["width"], 80)


class ImageRequestRetryTests(unittest.TestCase):
    def setUp(self):
        jitter = patch("image_workflows.random.uniform", return_value=0)
        jitter.start()
        self.addCleanup(jitter.stop)

    def test_json_parser_accepts_trailing_explanation_after_object(self):
        result = image_workflows._parse_json_response('{"ok": true}\nHere is the result.')

        self.assertEqual(result, {"ok": True})

    def test_json_request_retries_windows_connection_reset(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch("image_workflows.urlopen", side_effect=[ConnectionResetError(10054, "reset"), response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._send_json_request(Mock(), timeout=10)

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_visual_image_payload_is_downscaled_before_base64_encoding(self):
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "large-reference.png"
            Image.new("RGB", (2400, 1600), (35, 80, 125)).save(image_path, format="PNG")

            value = image_workflows._image_data_url(image_path)

        self.assertTrue(value.startswith("data:image/jpeg;base64,"))
        encoded = value.split(",", 1)[1]
        with Image.open(BytesIO(base64.b64decode(encoded))) as prepared:
            self.assertLessEqual(max(prepared.size), image_workflows.VISION_IMAGE_MAX_SIDE)

    def test_adaptive_request_gate_reduces_after_transport_failure(self):
        gate = image_workflows.AdaptiveRequestGate(max_concurrency=10, initial_concurrency=4)

        self.assertEqual(gate.target_concurrency, 4)
        gate.record_failure()

        self.assertEqual(gate.target_concurrency, 3)

    def test_workflow_request_gate_keeps_five_minimum_after_transport_failure(self):
        gate = image_workflows.AdaptiveRequestGate(
            max_concurrency=10,
            initial_concurrency=5,
            min_concurrency=5,
        )

        for _ in range(8):
            gate.record_failure()

        self.assertEqual(gate.target_concurrency, 5)

    def test_visual_requests_start_with_five_concurrent_slots(self):
        self.assertEqual(image_workflows.VISION_INITIAL_CONCURRENCY, 5)
        self.assertEqual(image_workflows._VISION_REQUEST_GATE.min_concurrency, 5)

    def test_image_requests_keep_ten_concurrent_slots(self):
        self.assertEqual(image_workflows._IMAGE_REQUEST_GATE.target_concurrency, 10)
        self.assertEqual(image_workflows._IMAGE_REQUEST_GATE.min_concurrency, 10)

    def test_duplicate_reference_analysis_is_reused_for_cycled_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "main", "path": str(reference)}]}),
                encoding="utf-8",
            )
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.VisionClient.analyze", return_value=analysis) as analyze,
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
            ):
                records = image_workflows.WorkflowRunner(
                    image_workflows.ApiSettings("https://example.test", "vision", "image")
                ).run(
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main",),
                    max_main_images=2,
                )

        self.assertEqual(analyze.call_count, 1)
        self.assertEqual(sorted(record["ordinal"] for record in records), [1, 2])

    def test_image_failure_keeps_visual_analysis_for_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps({"images": [{"type": "main", "path": str(reference)}]}),
                encoding="utf-8",
            )
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }
            first_runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            with (
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", side_effect=RuntimeError("transport")),
            ):
                failed = first_runner._run_task(
                    image_workflows.ImageTask("main", 1, reference),
                    product,
                    root / "generated",
                )

            second_runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.VisionClient.analyze") as analyze,
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
            ):
                retried = second_runner.run(
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main",),
                    generation_mode="own_product",
                    requested_ordinals={"main": [1]},
                    existing_records=[failed],
                    persist_records=False,
                )

        self.assertEqual(failed["analysis"], analysis)
        analyze.assert_not_called()
        self.assertEqual(retried[0]["status"], "completed")

    def test_json_request_retries_when_response_body_is_incomplete(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [
            IncompleteRead(b'{"choices":', 10492),
            b'{"choices":[]}',
        ]

        with (
            patch("image_workflows.urlopen", return_value=response) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._send_json_request(Mock(), timeout=10)

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_visual_analysis_retries_invalid_structured_output(self):
        invalid = {
            "product_fingerprint": "not-an-object",
            "reference_visual_brief": {},
            "compliance_risks": [],
            "copy_plan": {
                "headline": "测试标题",
                "subheadline": "",
                "selling_points": [{"text": "卖点", "basis": "Image 1 visible evidence: 可见", "placement": "顶部"}],
                "layout_instruction": "保持布局",
            },
            "generation_prompt": "生成商品图",
        }
        valid = dict(
            invalid,
            product_fingerprint={"category": "商品"},
            reference_visual_brief={"contains_replaceable_product": True},
        )
        responses = [
            {"choices": [{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}]},
            {"choices": [{"message": {"content": json.dumps(invalid, ensure_ascii=False)}}]},
            {"choices": [{"message": {"content": json.dumps(valid, ensure_ascii=False)}}]},
        ]

        with (
            patch("image_workflows._request_json", side_effect=responses) as request,
            patch("image_workflows._image_data_url", return_value="data:image/jpeg;base64,eA=="),
        ):
            result = image_workflows.VisionClient(
                image_workflows.ApiSettings("https://api.example", "vision-key", "image-key")
            ).analyze(Mock(), Mock(), "main")

        self.assertEqual(result["product_fingerprint"], {"category": "商品"})
        self.assertEqual(request.call_count, 3)

    def test_image_request_uses_high_quality_with_compatible_auto_size(self):
        with (
            patch("image_workflows._multipart_body", return_value=(b"body", "boundary")) as multipart,
            patch("image_workflows._send_image_request", return_value={"data": [{"b64_json": "aW1hZ2U="}]}),
        ):
            result = image_workflows._request_image(
                "https://example.test/v1/images/edits",
                "test-key",
                "gpt-image-2",
                "generate",
                [Mock()],
            )

        fields = multipart.call_args.args[0]
        self.assertEqual(result, b"image")
        self.assertEqual(fields["quality"], "high")
        self.assertEqual(fields["size"], "auto")

    def test_json_request_preserves_post_when_following_temporary_redirect(self):
        redirected_url = "https://redirected.example.test/v1/chat/completions"
        redirect = HTTPError(
            "https://example.test/v1/chat/completions",
            308,
            "Permanent Redirect",
            {"Location": redirected_url},
            BytesIO(b""),
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch("image_workflows.urlopen", side_effect=[redirect, response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._request_json(
                "https://example.test/v1/chat/completions",
                "test-key",
                {"model": "test-model"},
                timeout=10,
            )

        redirected_request = urlopen.call_args_list[1].args[0]
        self.assertEqual(result, {"choices": []})
        self.assertEqual(redirected_request.full_url, redirected_url)
        self.assertEqual(redirected_request.get_method(), "POST")
        self.assertIsNotNone(redirected_request.data)
        self.assertEqual(redirected_request.get_header("Authorization"), "Bearer test-key")
        sleep.assert_not_called()

    def test_json_request_survives_three_transient_upstream_failures(self):
        transient_errors = [
            HTTPError(
                "https://example.test/v1/chat/completions",
                502,
                "Bad Gateway",
                {},
                BytesIO(b'{"error":{"code":"upstream_response_incomplete"}}'),
            )
            for _ in range(3)
        ]
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch("image_workflows.urlopen", side_effect=[*transient_errors, response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._request_json(
                "https://example.test/v1/chat/completions",
                "test-key",
                {"model": "test-model"},
                timeout=10,
            )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 4)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4, 8])

    def test_json_request_retries_transient_http_error(self):
        transient = HTTPError(
            "https://example.test/v1/chat/completions",
            502,
            "Bad Gateway",
            {},
            BytesIO(b'{"error":{"code":"upstream_response_incomplete"}}'),
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch("image_workflows.urlopen", side_effect=[transient, response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._request_json(
                "https://example.test/v1/chat/completions",
                "test-key",
                {"model": "test-model"},
                timeout=10,
            )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_json_request_retries_upstream_http_408(self):
        transient = HTTPError(
            "https://example.test/v1/chat/completions",
            408,
            "Request Timeout",
            {},
            BytesIO(b'{"error":{"code":"upstream_unavailable"}}'),
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch("image_workflows.urlopen", side_effect=[transient, response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._request_json(
                "https://example.test/v1/chat/completions",
                "test-key",
                {"model": "test-model"},
                timeout=10,
            )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_json_request_retries_when_remote_closes_connection_without_response(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"choices":[]}'

        with (
            patch(
                "image_workflows.urlopen",
                side_effect=[RemoteDisconnected("Remote end closed connection without response"), response],
            ) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._request_json(
                "https://example.test/v1/chat/completions",
                "test-key",
                {"model": "test-model"},
                timeout=10,
            )

        self.assertEqual(result, {"choices": []})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_image_request_retries_remote_disconnect_and_tls_eof(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"b64_json":"aW1hZ2U="}]}'

        with (
            patch(
                "image_workflows.urlopen",
                side_effect=[
                    RemoteDisconnected("Remote end closed connection without response"),
                    ssl.SSLEOFError("UNEXPECTED_EOF_WHILE_READING"),
                    response,
                ],
            ) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
            patch("image_workflows.random.uniform", return_value=0),
        ):
            result = image_workflows._send_image_request(Mock(), timeout=10)

        self.assertEqual(result, {"data": [{"b64_json": "aW1hZ2U="}]})
        self.assertEqual(urlopen.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [2, 4])

    def test_image_requests_start_with_adaptive_global_cap(self):
        active = 0
        peak = 0
        lock = threading.Lock()
        first_wave_started = threading.Barrier(4)

        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"b64_json":"aW1hZ2U="}]}'

        def open_request(*_args, **_kwargs):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            if peak <= 4:
                first_wave_started.wait(timeout=1)
            with lock:
                active -= 1
            return response

        gate = image_workflows.AdaptiveRequestGate(max_concurrency=10, initial_concurrency=4)
        with (
            patch("image_workflows.urlopen", side_effect=open_request),
            patch("image_workflows._IMAGE_REQUEST_GATE", gate),
        ):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(
                    executor.map(
                        lambda _: image_workflows._send_image_request(Mock(), timeout=10),
                        range(8),
                    )
                )

        self.assertEqual(len(results), 8)
        self.assertEqual(peak, 4)


class IdentityAnalysisConcurrencyTests(unittest.TestCase):
    def setUp(self):
        jitter = patch("image_workflows.random.uniform", return_value=0)
        jitter.start()
        self.addCleanup(jitter.stop)

    def test_requested_supplement_tasks_keep_new_ordinals_and_cycle_sources(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.jpg"
            second = root / "second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps(
                    {"images": [{"type": "detail", "path": str(first)}, {"type": "detail", "path": str(second)}]}
                ),
                encoding="utf-8",
            )

            tasks = image_workflows.load_requested_tasks(manifest, {"detail": [2, 5]})

        self.assertEqual([task.ordinal for task in tasks], [2, 5])
        self.assertEqual([task.source_path for task in tasks], [second, first])

    def test_detail_dossier_failure_falls_back_to_original_detail_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            main = root / "main.jpg"
            detail = root / "detail.jpg"
            for path in (product, main, detail):
                path.write_bytes(b"image")
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
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            with (
                patch("image_workflows.VisionClient.verify"),
                patch(
                    "image_workflows.VisionClient.analyze_identity_source",
                    return_value={"source_index": 1},
                ),
                patch("image_workflows.VisionClient.synthesize_product_dossier", side_effect=RuntimeError("dossier unavailable")),
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
            ):
                runner = image_workflows.WorkflowRunner(
                    image_workflows.ApiSettings("https://example.test", "vision", "image")
                )
                records = runner.run(
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main", "detail"),
                    1,
                    None,
                    1,
                    generation_mode="competitor_reference",
                    identity_image=main,
                )

        self.assertEqual(
            {(record["category"], record["status"]) for record in records},
            {("main", "completed"), ("detail", "completed")},
        )

    def test_all_identity_network_failures_stop_before_per_task_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            main = root / "main.jpg"
            detail = root / "detail.jpg"
            for path in (product, main, detail):
                path.write_bytes(b"image")
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

            with (
                patch("image_workflows.VisionClient.verify"),
                patch(
                    "image_workflows.VisionClient.analyze_identity_source",
                    side_effect=RuntimeError(
                        "Network request failed after 5 attempts: "
                        "[SSL: UNEXPECTED_EOF_WHILE_READING]"
                    ),
                ),
                patch("image_workflows.VisionClient.analyze") as analyze_task,
            ):
                runner = image_workflows.WorkflowRunner(
                    image_workflows.ApiSettings("https://example.test", "vision", "image")
                )
                with self.assertRaisesRegex(RuntimeError, "视觉素材分析连续网络失败"):
                    runner.run(
                        manifest,
                        product,
                        root / "generated",
                        None,
                        ("main", "detail"),
                        1,
                        None,
                        1,
                        generation_mode="competitor_reference",
                        identity_image=main,
                    )

        analyze_task.assert_not_called()

    def test_identity_source_selection_caps_large_collections_and_keeps_all_categories(self):
        sources = []
        for category, count in (("main", 5), ("sku", 8), ("detail", 31)):
            for ordinal in range(1, count + 1):
                sources.append(
                    image_workflows.IdentitySource(
                        index=len(sources) + 1,
                        category=category,
                        path=Path(f"{category}-{ordinal}.jpg"),
                        is_anchor=category == "main" and ordinal == 1,
                    )
                )

        selected = image_workflows.select_identity_sources(sources)

        self.assertEqual(len(selected), image_workflows.IDENTITY_SOURCE_LIMIT)
        self.assertTrue(selected[0].is_anchor)
        self.assertEqual({source.category for source in selected}, {"main", "sku", "detail"})
        self.assertEqual(len({source.index for source in selected}), len(selected))

    def test_default_sku_and_detail_task_counts_are_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            images = []
            for category, count in (("sku", 12), ("detail", 36)):
                for ordinal in range(1, count + 1):
                    image = root / f"{category}-{ordinal}.jpg"
                    image.write_bytes(category.encode("ascii"))
                    images.append({"type": category, "path": str(image)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": images}), encoding="utf-8")

            defaults = image_workflows.load_manifest_tasks(manifest, ("sku", "detail"))
            explicit = image_workflows.load_manifest_tasks(
                manifest,
                ("sku", "detail"),
                max_sku_images=5,
                max_detail_images=5,
            )

        self.assertEqual(sum(task.category == "sku" for task in defaults), 8)
        self.assertEqual(sum(task.category == "detail" for task in defaults), 15)
        self.assertEqual(sum(task.category == "sku" for task in explicit), 5)
        self.assertEqual(sum(task.category == "detail" for task in explicit), 5)

    def test_identity_analysis_caps_concurrency_at_ten_without_limiting_generation(self):
        self.assertEqual(image_workflows.resolve_identity_worker_count(30, None), 10)
        self.assertEqual(image_workflows.resolve_identity_worker_count(8, None), 8)
        self.assertEqual(image_workflows.resolve_worker_count(30, None), 30)

    def test_generation_workers_are_dynamic_with_a_safe_upper_bound(self):
        self.assertEqual(image_workflows.resolve_generation_worker_count(46, None), 10)
        self.assertEqual(image_workflows.resolve_generation_worker_count(6, None), 6)
        self.assertEqual(image_workflows.resolve_generation_worker_count(46, 4), 4)
        self.assertEqual(image_workflows.resolve_generation_worker_count(46, 20), 10)

    def test_visual_prompt_analysis_is_capped_at_ten_and_released_before_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            product.write_bytes(b"product")
            images = []
            for ordinal in range(1, 12):
                image = root / f"reference-{ordinal}.jpg"
                image.write_bytes(b"reference")
                images.append({"type": "main", "path": str(image)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": images}), encoding="utf-8")

            active_analysis = 0
            peak_analysis = 0
            active_generation = 0
            peak_generation = 0
            analysis_lock = threading.Lock()
            generation_release = threading.Event()
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            def analyze(*_args, **_kwargs):
                nonlocal active_analysis, peak_analysis
                with analysis_lock:
                    active_analysis += 1
                    peak_analysis = max(peak_analysis, active_analysis)
                time.sleep(0.05)
                with analysis_lock:
                    active_analysis -= 1
                return analysis

            def generate(*_args, **_kwargs):
                nonlocal active_generation, peak_generation
                with analysis_lock:
                    active_generation += 1
                    peak_generation = max(peak_generation, active_generation)
                    if active_generation == image_workflows.IMAGE_GENERATION_CONCURRENCY:
                        generation_release.set()
                generation_release.wait(timeout=2)
                with analysis_lock:
                    active_generation -= 1
                return _test_png_bytes()

            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.VisionClient.analyze", side_effect=analyze),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", side_effect=generate),
            ):
                records = runner.run(
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main",),
                    11,
                )

        self.assertLessEqual(peak_analysis, 10)
        self.assertEqual(peak_generation, 10)
        self.assertEqual(sum(record["status"] == "completed" for record in records), 11)

    def test_visual_prompt_analysis_stays_within_per_workflow_limit_under_generation_cap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            product.write_bytes(b"product")
            images = []
            for category in ("main", "detail"):
                for ordinal in range(1, 12):
                    image = root / f"{category}-{ordinal}.jpg"
                    image.write_bytes(b"reference")
                    images.append({"type": category, "path": str(image)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": images}), encoding="utf-8")

            active = {"main": 0, "detail": 0}
            peaks = {"main": 0, "detail": 0}
            peak_total = 0
            analysis_lock = threading.Lock()
            release = threading.Event()
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            def analyze(_product, _reference, category, **_kwargs):
                nonlocal peak_total
                with analysis_lock:
                    active[category] += 1
                    peaks[category] = max(peaks[category], active[category])
                    peak_total = max(peak_total, sum(active.values()))
                    if sum(active.values()) == image_workflows.IMAGE_GENERATION_CONCURRENCY:
                        release.set()
                release.wait(timeout=1)
                with analysis_lock:
                    active[category] -= 1
                return analysis

            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.VisionClient.analyze", side_effect=analyze),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
            ):
                records = runner.run(
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main", "detail"),
                    11,
                    max_detail_images=11,
                )

        self.assertLessEqual(peaks["main"], 10)
        self.assertLessEqual(peaks["detail"], 10)
        self.assertEqual(peak_total, 10)
        self.assertEqual(sum(record["status"] == "completed" for record in records), 22)

    def test_visual_analysis_queue_continues_while_image_generation_is_busy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            product.write_bytes(b"product")
            images = []
            for ordinal in range(1, 13):
                image = root / f"reference-{ordinal}.jpg"
                image.write_bytes(f"reference-{ordinal}".encode("ascii"))
                images.append({"type": "main", "path": str(image)})
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"images": images}), encoding="utf-8")

            analyzed_count = 0
            analyzed_lock = threading.Lock()
            all_analyzed = threading.Event()
            generation_started = threading.Event()
            release_generation = threading.Event()
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {"contains_replaceable_product": True},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            def analyze(*_args, **_kwargs):
                nonlocal analyzed_count
                with analyzed_lock:
                    analyzed_count += 1
                    if analyzed_count == 12:
                        all_analyzed.set()
                return analysis

            def generate(*_args, **_kwargs):
                generation_started.set()
                release_generation.wait(timeout=3)
                return _test_png_bytes()

            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            with (
                patch("image_workflows.VisionClient.verify"),
                patch("image_workflows.VisionClient.analyze", side_effect=analyze),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", side_effect=generate),
                ThreadPoolExecutor(max_workers=1) as executor,
            ):
                future = executor.submit(
                    runner.run,
                    manifest,
                    product,
                    root / "generated",
                    None,
                    ("main",),
                    12,
                )
                self.assertTrue(generation_started.wait(timeout=1))
                analyzed_while_generation_busy = all_analyzed.wait(timeout=1)
                release_generation.set()
                records = future.result(timeout=5)

        self.assertTrue(analyzed_while_generation_busy)
        self.assertEqual(sum(record["status"] == "completed" for record in records), 12)

    def test_task_events_distinguish_analysis_prompt_and_image_generation(self):
        from pathlib import Path
        from tempfile import TemporaryDirectory

        with TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            events = []
            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image"),
                callback=events.append,
            )
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            with (
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=_test_png_bytes()),
            ):
                record = runner._run_task(
                    image_workflows.ImageTask("main", 1, reference),
                    product,
                    root / "generated",
                )

        self.assertEqual(record["status"], "completed")
        self.assertEqual(
            [event["status"] for event in events],
            ["analyzing", "prompt_ready", "generating", "completed"],
        )
        self.assertEqual(
            [event.get("stage_label") for event in events[:3]],
            ["视觉提示词分析", "提示词已生成", "调用 gpt-image-2 生图"],
        )

    def test_generated_image_is_saved_as_750px_compressed_jpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.jpg"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            generated = Image.new("RGB", (1500, 1000), (35, 80, 125))
            image_buffer = BytesIO()
            generated.save(image_buffer, format="PNG")
            events = []
            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image"),
                callback=events.append,
            )
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            with (
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=image_buffer.getvalue()),
            ):
                record = runner._run_task(
                    image_workflows.ImageTask("main", 1, reference),
                    product,
                    root / "generated",
                )

            output_path = Path(record["output_path"])
            output_size = output_path.stat().st_size
            with Image.open(output_path) as saved:
                saved_format = saved.format
                saved_size = saved.size

        self.assertEqual(record["status"], "completed")
        self.assertEqual(output_path.suffix, ".jpg")
        self.assertEqual(saved_format, "JPEG")
        self.assertEqual(saved_size, (750, 500))
        self.assertLessEqual(output_size, 2 * 1024 * 1024)
        self.assertEqual(events[-1]["output_path"], str(output_path))

    def test_generated_transparency_is_composited_over_white(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            product = root / "product.jpg"
            reference = root / "reference.png"
            product.write_bytes(b"product")
            reference.write_bytes(b"reference")
            generated = Image.new("RGBA", (1000, 500), (255, 0, 0, 0))
            generated.paste((30, 60, 90, 255), (250, 100, 750, 400))
            image_buffer = BytesIO()
            generated.save(image_buffer, format="PNG")
            runner = image_workflows.WorkflowRunner(
                image_workflows.ApiSettings("https://example.test", "vision", "image")
            )
            analysis = {
                "product_fingerprint": {},
                "reference_visual_brief": {},
                "compliance_risks": [],
                "copy_plan": {},
                "generation_prompt": "generate",
            }

            with (
                patch("image_workflows.VisionClient.analyze", return_value=analysis),
                patch("image_workflows.compose_generation_prompt", return_value="generate"),
                patch("image_workflows.ImageClient.generate", return_value=image_buffer.getvalue()),
            ):
                record = runner._run_task(
                    image_workflows.ImageTask("sku", 1, reference),
                    product,
                    root / "generated",
                )

            with Image.open(record["output_path"]) as saved:
                corner = saved.convert("RGB").getpixel((5, 5))

        self.assertTrue(all(channel >= 245 for channel in corner))

    def test_retries_transient_http_error_then_returns_image(self):
        transient = HTTPError(
            "https://example.test/v1/images/edits",
            502,
            "Bad Gateway",
            {},
            BytesIO(b'{"error":{"code":"upstream_response_incomplete"}}'),
        )
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"b64_json":"aW1hZ2U="}]}'

        with (
            patch("image_workflows.urlopen", side_effect=[transient, response]) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._send_image_request(Mock(), timeout=10)

        self.assertEqual(result, {"data": [{"b64_json": "aW1hZ2U="}]})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_does_not_retry_permanent_http_error(self):
        permanent = HTTPError(
            "https://example.test/v1/images/edits",
            401,
            "Unauthorized",
            {},
            BytesIO(b'{"error":{"message":"invalid token"}}'),
        )

        with (
            patch("image_workflows.urlopen", side_effect=permanent) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401"):
                image_workflows._send_image_request(Mock(), timeout=10)

        urlopen.assert_called_once()
        sleep.assert_not_called()

    def test_retries_temporary_network_error(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"data":[{"b64_json":"aW1hZ2U="}]}'

        with (
            patch(
                "image_workflows.urlopen",
                side_effect=[URLError("connection reset"), response],
            ) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._send_image_request(Mock(), timeout=10)

        self.assertEqual(result, {"data": [{"b64_json": "aW1hZ2U="}]})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_image_request_retries_when_response_body_is_incomplete(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.side_effect = [
            IncompleteRead(b'{"data":', 17477),
            b'{"data":[{"b64_json":"aW1hZ2U="}]}'
        ]

        with (
            patch("image_workflows.urlopen", return_value=response) as urlopen,
            patch("image_workflows.time.sleep") as sleep,
        ):
            result = image_workflows._send_image_request(Mock(), timeout=10)

        self.assertEqual(result, {"data": [{"b64_json": "aW1hZ2U="}]})
        self.assertEqual(urlopen.call_count, 2)
        sleep.assert_called_once_with(2)


if __name__ == "__main__":
    unittest.main()
