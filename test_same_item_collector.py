import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from same_item_collector import (
    COLLECTION_PAUSED_FILENAME,
    CollectorConfig,
    CollectorPaused,
    MAX_ASSET_PRODUCT_PROBES,
    MAX_VIDEO_PRODUCT_PROBES,
    STORE_INSIGHT_DOWNLOAD_TIMEOUT_MS,
    collect_first_store_insight_main_video,
    close_project_browser_for_profile,
    connect_over_cdp,
    current_stop_marker,
    ensure_cdp_browser,
    is_taobao_or_tmall_url,
    main,
    normalize_video_url,
    parse_store_insight_sku_filename,
    ranked_products_for_asset_collection,
    remaining_bounded_store_insight_asset_targets,
    remaining_store_insight_asset_targets,
    select_video_url,
)


class EnsureCdpBrowserTests(unittest.TestCase):
    def test_profile_cleanup_returns_closed_process_count(self):
        class CompletedProcess:
            stdout = "32136\n41720\n"

        profile = Path(r"C:\Users\Example\AppData\Local\ProductImageWorkflow\store-insight-profile")
        with patch("same_item_collector.subprocess.run", return_value=CompletedProcess()):
            closed_count = close_project_browser_for_profile(profile)

        self.assertEqual(closed_count, 2)

    def test_closes_matching_profile_before_launch(self):
        events = []

        class BrowserProcess:
            def poll(self):
                return None

        with TemporaryDirectory() as temporary:
            profile = Path(temporary) / "store-insight-profile"
            config = CollectorConfig(
                reference_image=None,
                output_dir=Path(temporary) / "output",
                browser_profile_dir=profile,
                browser_executable=r"D:\WaXiangBrowser\waxiang.exe",
            )
            with (
                patch("same_item_collector.is_cdp_alive", side_effect=[False, True]),
                patch("same_item_collector.is_port_open", return_value=False),
                patch(
                    "same_item_collector.close_project_browser_for_profile",
                    create=True,
                    side_effect=lambda value: events.append(("close", value)) or 1,
                ),
                patch(
                    "same_item_collector.subprocess.Popen",
                    side_effect=lambda command, **_kwargs: events.append(("launch", command)) or BrowserProcess(),
                ),
                patch("same_item_collector.time.sleep"),
            ):
                result = ensure_cdp_browser(config)

        resolved_profile = profile.resolve()
        self.assertEqual(result, "http://127.0.0.1:9223")
        self.assertEqual(events[0], ("close", resolved_profile))
        self.assertIn("--remote-debugging-port=9223", events[1][1])
        self.assertIn(f"--user-data-dir={resolved_profile}", events[1][1])


class SameItemSkuParsingTests(unittest.TestCase):
    def test_tmall_global_is_treated_as_a_tmall_page(self):
        self.assertTrue(
            is_taobao_or_tmall_url("https://detail.tmall.hk/hk/item.htm?id=1001")
        )

    def test_lookalike_tmall_domain_is_not_treated_as_a_tmall_page(self):
        self.assertFalse(
            is_taobao_or_tmall_url("https://not-tmall.com/item.htm?id=1001")
        )

    def test_paused_collection_writes_a_pause_record_not_an_asset_manifest(self):
        with TemporaryDirectory() as temporary:
            output = Path(temporary) / "collected"
            with patch("same_item_collector.run_collection", side_effect=CollectorPaused("risk 429")):
                exit_code = main(["--output-dir", str(output)])

            self.assertEqual(exit_code, 2)
            self.assertTrue((output / COLLECTION_PAUSED_FILENAME).is_file())
            self.assertFalse((output / "main-image-manifest.json").exists())

    def test_unresponsive_cdp_fails_fast_with_recovery_message(self):
        class Chromium:
            def connect_over_cdp(self, _url, timeout):
                self.timeout = timeout
                raise TimeoutError("stuck CDP")

        class Playwright:
            chromium = Chromium()

        with self.assertRaisesRegex(CollectorPaused, "CDP session is unresponsive"):
            connect_over_cdp(Playwright(), "http://127.0.0.1:9223")
        self.assertEqual(Playwright.chromium.timeout, 15_000)

    def test_default_candidate_scan_uses_the_first_visible_result_page_only(self):
        config = CollectorConfig(reference_image=None, output_dir=Path("output"))

        self.assertEqual(config.candidate_limit, 8)
        self.assertEqual(config.scroll_rounds, 1)
        self.assertEqual(MAX_ASSET_PRODUCT_PROBES, 5)
        self.assertEqual(MAX_VIDEO_PRODUCT_PROBES, 3)
        self.assertEqual(STORE_INSIGHT_DOWNLOAD_TIMEOUT_MS, 5_000)

    def test_detects_image_verification_inside_an_iframe(self):
        class Locator:
            def __init__(self, text):
                self.text = text
                self.first = self

            def inner_text(self, timeout):
                return self.text

        class Frame:
            url = "https://captcha.taobao.com/image"

            def locator(self, _selector):
                return Locator("请选择符合镜像的图片")

        class Page:
            url = "https://detail.tmall.com/item.htm?id=1"
            frames = [Frame()]

            def locator(self, _selector):
                return Locator("正常商品页")

        self.assertEqual(current_stop_marker(Page()), "符合镜像")

    def test_detects_access_refused_dialog_as_a_stop_marker(self):
        class Locator:
            def __init__(self, text):
                self.text = text
                self.first = self

            def inner_text(self, timeout):
                return self.text

            def count(self):
                return 0

        class Page:
            url = "https://s.taobao.com/search"
            frames = []

            def locator(self, _selector):
                return Locator("亲，访问被拒绝")

        self.assertEqual(current_stop_marker(Page()), "访问被拒绝")

    def test_does_not_treat_a_product_number_as_a_risk_marker(self):
        class Locator:
            first = None

            def __init__(self):
                self.first = self

            def inner_text(self, timeout):
                return "商品编号 429，库存充足"

            def count(self):
                return 0

        class Page:
            url = "https://s.taobao.com/search"
            frames = []

            def locator(self, _selector):
                return Locator()

        self.assertEqual(current_stop_marker(Page()), "")

    def test_parses_clothing_color_and_letter_size(self):
        parsed = parse_store_insight_sku_filename("SKU图_1_颜色分类:黑色;尺码:M.jpg", 1)

        self.assertEqual(parsed["color_text"], "黑色")
        self.assertEqual(parsed["spec_text"], "M")
        self.assertEqual(parsed["parse_status"], "parsed")

    def test_parses_unstructured_clothing_label(self):
        parsed = parse_store_insight_sku_filename("SKU图_2_藏青色 XL.jpg", 2)

        self.assertEqual(parsed["color_text"], "藏青色")
        self.assertEqual(parsed["spec_text"], "XL")

    def test_selects_openable_original_video_url(self):
        selected = select_video_url([
            "https://cdn.example.com/segment.ts",
            "//cloud.video.taobao.com/play/123/e/6/t/1.mp4",
            "https://cdn.example.com/preview.jpg",
        ])

        self.assertEqual(
            selected,
            "https://cloud.video.taobao.com/play/123/e/6/t/1.mp4",
        )
        self.assertEqual(
            normalize_video_url("https:%2F%2Fcloud.video.taobao.com%2Fplay%2F123.mp4"),
            "https://cloud.video.taobao.com/play/123.mp4",
        )

    def test_fixed_quantity_keeps_ranked_fallback_products(self):
        products = [{"product_id": str(index)} for index in range(1, 4)]
        config = CollectorConfig(reference_image=None, output_dir=Path("output"), top_product_only=True)

        selected = ranked_products_for_asset_collection(config, products, {"main": 8})
        remaining = remaining_store_insight_asset_targets({"main": 8}, {"main": 5})

        self.assertEqual(selected, products)
        self.assertEqual(remaining, {"main": 3})

    def test_fallback_only_continues_fixed_main_image_target(self):
        remaining = remaining_store_insight_asset_targets(
            {"main": 10, "sku": None, "detail": None},
            {"main": 5, "sku": 4, "detail": 12},
            include_unbounded=False,
        )

        self.assertEqual(remaining, {"main": 5})

    def test_bounded_assets_use_caps_then_backfill_only_below_minimum(self):
        first_product = remaining_bounded_store_insight_asset_targets(
            {"main": 10, "sku": None, "detail": None},
            {"main": 5, "sku": 0, "detail": 0},
            1,
        )
        second_product = remaining_bounded_store_insight_asset_targets(
            {"main": 10, "sku": None, "detail": None},
            {"main": 5, "sku": 5, "detail": 10},
            2,
        )

        self.assertEqual(first_product, {"main": 5, "sku": 8, "detail": 15})
        self.assertEqual(second_product, {"main": 5})

    def test_bounded_asset_shortfall_can_use_fallback_products(self):
        products = [{"product_id": str(index)} for index in range(1, 4)]
        config = CollectorConfig(reference_image=None, output_dir=Path("output"), top_product_only=True)

        selected = ranked_products_for_asset_collection(config, products, {"sku": None})

        self.assertEqual(selected, products)

    def test_unbounded_top_product_collection_stays_on_first_product(self):
        products = [{"product_id": str(index)} for index in range(1, 4)]
        config = CollectorConfig(reference_image=None, output_dir=Path("output"), top_product_only=True)

        selected = ranked_products_for_asset_collection(config, products, {"main": None})

        self.assertEqual(selected, products[:1])

    def test_video_probe_stops_after_three_ranked_products(self):
        products = [
            {"product_id": str(index), "item_url": f"https://item.taobao.com/item.htm?id={index}"}
            for index in range(1, 9)
        ]

        def video_url(_context, product, _timeout_ms):
            return "https://cloud.video.taobao.com/play/7.mp4" if product["product_id"] == "7" else ""

        with patch("same_item_collector.collect_product_video_url", side_effect=video_url) as collect:
            metadata = collect_first_store_insight_main_video(
                object(),
                products,
                Path("downloads"),
                Path("output"),
                30_000,
            )

        self.assertEqual(collect.call_count, 3)
        self.assertEqual(metadata["main_video_status"], "not_found")
        self.assertEqual(metadata["main_video_source_product_id"], "")


if __name__ == "__main__":
    unittest.main()
