import json
import zipfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from openpyxl import Workbook

from douyin_collector import download_douyin_package, douyin_checkbox_states, materialize_douyin_package

from store_insight_collector import (
    ASSET_LABELS,
    DOWNLOAD_COMPLETE_PATTERN,
    is_all_files_download_label,
    attach_sku_variants_to_images,
    build_manifest,
    close_project_browser_for_profile,
    collect_store_insight_payload,
    collect_product_summary,
    empty_parameter_metadata,
    parse_sku_variant_fields,
    empty_sku_metadata,
    empty_video_metadata,
    find_waxiang_store_insight_extension,
    materialize,
    platform_challenge_text,
    reload_item,
    RiskControlDetected,
    safe_extract,
    wait_for_store_insight_entry,
    wait_for_download_after_click,
    wait_for_platform_challenge,
    validate_item_url,
)


class CollectorTests(unittest.TestCase):
    @staticmethod
    def _douyin_download_page(
        product_id: str,
        labels: list[str],
        menu_visible_after_click: bool = False,
        selected_types: set[str] | None = None,
    ):
        page = Mock()
        query_input = Mock()
        current_product_id = {"value": product_id}
        query_input.input_value.side_effect = lambda: current_product_id["value"]
        query_input.fill.side_effect = lambda value: current_product_id.__setitem__("value", value)
        download_button = Mock()
        button_locator = Mock()
        button_locator.filter.return_value = download_button
        loading_mask = Mock()

        menu_items = Mock()
        menu_items.count.return_value = len(labels)
        menu_visibility = {"value": not menu_visible_after_click}
        item_locators = []
        for label in labels:
            item = Mock()
            item.inner_text.return_value = label
            item.is_visible.side_effect = lambda: menu_visibility["value"]
            item_locators.append(item)
        menu_items.nth.side_effect = item_locators.__getitem__
        download_button.click.side_effect = lambda: menu_visibility.__setitem__("value", True)
        download_button.evaluate.side_effect = lambda _: menu_visibility.__setitem__("value", True)

        checkbox_states = douyin_checkbox_states(selected_types or {"main"})

        def locate(selector: str):
            if selector == 'input[placeholder="请输入商品ID/口令"]':
                return query_input
            if selector == "button":
                return button_locator
            if selector == "li.el-dropdown-menu__item":
                return menu_items
            if selector == ".el-loading-mask":
                return loading_mask
            if selector.startswith('input[type="checkbox"]'):
                value = selector.split('value="', 1)[1].split('"', 1)[0]
                checkbox = Mock()
                checkbox.is_checked.return_value = checkbox_states[value]
                return checkbox
            raise AssertionError(f"Unexpected selector: {selector}")

        page.locator.side_effect = locate
        page.query_input = query_input
        page.download_button = download_button
        page.loading_mask = loading_mask
        download = Mock(suggested_filename="douyin.zip")

        def save_archive(target):
            with zipfile.ZipFile(target, "w") as bundle:
                bundle.writestr("主图/主图_1.jpg", b"main")
                if "detail" in (selected_types or {"main"}):
                    bundle.writestr("详情图/详情图_1.jpg", b"detail")

        download.save_as.side_effect = save_archive
        download_info = Mock(value=download)
        download_context = Mock()
        download_context.__enter__ = Mock(return_value=download_info)
        download_context.__exit__ = Mock(return_value=False)
        page.expect_download.return_value = download_context
        return page, item_locators

    def test_collect_store_insight_payload_dispatches_douyin_package(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "douyin.zip"
            archive.write_bytes(b"zip")
            expected_records = [{"type": "main", "path": str(root / "main.jpg")}]
            expected_metadata = {"sku_metadata_status": "not_found", "sku_variants": []}

            with (
                patch("store_insight_collector.download_douyin_package", return_value=archive) as download,
                patch(
                    "store_insight_collector.materialize_douyin_package",
                    return_value=(expected_records, expected_metadata),
                ) as materialize_douyin,
                patch("store_insight_collector.download_store_insight_all_zip") as legacy_download,
                patch("store_insight_collector.wait_for_platform_challenge", return_value=False) as wait_challenge,
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id=123",
                    "123",
                    "douyin",
                    root / "downloads",
                    root / "output",
                    {"main", "detail"},
                    60_000,
                    300,
                    None,
                )

            self.assertEqual(records, expected_records)
            self.assertEqual(metadata, expected_metadata)
            download.assert_called_once()
            materialize_douyin.assert_called_once()
            legacy_download.assert_not_called()
            wait_challenge.assert_called_once()

    def test_collect_store_insight_payload_recovers_empty_douyin_package_main_image(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "douyin.zip"
            archive.write_bytes(b"zip")
            item_url = "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?" + urlencode(
                {
                    "id": "123",
                    "goods_detail": json.dumps(
                        {
                            "img": {
                                "url_list": [
                                    "https://p3-item.ecombdimg.com/img/product.png",
                                ]
                            }
                        }
                    ),
                }
            )
            response = MagicMock()
            response.__enter__.return_value = response
            response.__exit__.return_value = False
            response.read.return_value = b"embedded-main-image"

            with (
                patch("store_insight_collector.download_douyin_package", return_value=archive),
                patch(
                    "store_insight_collector.materialize_douyin_package",
                    return_value=([], {"sku_metadata_status": "not_found", "sku_variants": []}),
                ),
                patch("urllib.request.urlopen", return_value=response),
                patch("store_insight_collector.wait_for_platform_challenge", return_value=False),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    item_url,
                    "123",
                    "douyin",
                    root / "downloads",
                    root / "output",
                    {"main", "sku", "detail"},
                    60_000,
                    300,
                    10,
                )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "main")
            self.assertEqual(Path(records[0]["path"]).read_bytes(), b"embedded-main-image")
            self.assertEqual(metadata["main_source_status"], "embedded_url_fallback")
            self.assertEqual(metadata["missing_asset_types"], ["detail", "sku"])

    def test_collect_store_insight_payload_keeps_empty_douyin_package_failed_without_embedded_image(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "douyin.zip"
            archive.write_bytes(b"zip")
            with (
                patch("store_insight_collector.download_douyin_package", return_value=archive),
                patch(
                    "store_insight_collector.materialize_douyin_package",
                    return_value=([], {"sku_metadata_status": "not_found", "sku_variants": []}),
                ),
                patch("urllib.request.urlopen") as urlopen,
                patch("store_insight_collector.wait_for_platform_challenge", return_value=False),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id=123",
                    "123",
                    "douyin",
                    root / "downloads",
                    root / "output",
                    {"main"},
                    60_000,
                    300,
                    10,
                )

            self.assertEqual(records, [])
            self.assertEqual(metadata["main_source_status"], "package_empty")
            self.assertIn("url_list", metadata["main_source_error"])
            urlopen.assert_not_called()

    def test_collect_store_insight_payload_compensates_each_missing_requested_image_type(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_archive = root / "all.zip"
            with zipfile.ZipFile(all_archive, "w"):
                pass
            compensation_archives = {}
            for asset_type in ("main", "sku", "detail"):
                archive = root / f"{asset_type}.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr(f"{asset_type}/{asset_type}.jpg", asset_type.encode("ascii"))
                compensation_archives[asset_type] = archive

            compensated_types = []

            def download_compensation(_page, _url, asset_type, *_args):
                compensated_types.append(asset_type)
                return compensation_archives[asset_type]

            with (
                patch("store_insight_collector.download_store_insight_all_zip", return_value=all_archive),
                patch(
                    "store_insight_collector.download_store_insight_zip",
                    side_effect=download_compensation,
                ),
                patch("store_insight_collector.collect_item_metadata", return_value={"sku_variants": []}),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://detail.tmall.com/item.htm?id=123",
                    "123",
                    "commerce",
                    root / "downloads",
                    root / "output",
                    {"main", "sku", "detail"},
                    60_000,
                    300,
                    None,
                )

            self.assertEqual(compensated_types, ["main", "sku", "detail"])
            self.assertEqual([record["type"] for record in records], ["main", "sku", "detail"])
            self.assertEqual(metadata["collected_asset_types"], ["main", "sku", "detail"])
            self.assertEqual(metadata["missing_asset_types"], [])
            self.assertNotIn("asset_compensation_errors", metadata)

    def test_collect_store_insight_payload_records_failed_compensation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_archive = root / "all.zip"
            with zipfile.ZipFile(all_archive, "w") as bundle:
                bundle.writestr("main/main.jpg", b"main")
            detail_archive = root / "detail.zip"
            with zipfile.ZipFile(detail_archive, "w") as bundle:
                bundle.writestr("detail/detail.jpg", b"detail")

            def download_compensation(_page, _url, asset_type, *_args):
                if asset_type == "sku":
                    raise RuntimeError("SKU package unavailable")
                return detail_archive

            with (
                patch("store_insight_collector.download_store_insight_all_zip", return_value=all_archive),
                patch(
                    "store_insight_collector.download_store_insight_zip",
                    side_effect=download_compensation,
                ),
                patch("store_insight_collector.collect_item_metadata", return_value={"sku_variants": []}),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://detail.tmall.com/item.htm?id=123",
                    "123",
                    "commerce",
                    root / "downloads",
                    root / "output",
                    {"main", "sku", "detail"},
                    60_000,
                    300,
                    None,
                )

            self.assertEqual([record["type"] for record in records], ["main", "detail"])
            self.assertEqual(metadata["collected_asset_types"], ["main", "detail"])
            self.assertEqual(metadata["missing_asset_types"], ["sku"])
            self.assertEqual(metadata["asset_compensation_errors"], {"sku": "SKU package unavailable"})

    def test_collect_store_insight_payload_propagates_risk_control_during_compensation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_archive = root / "all.zip"
            with zipfile.ZipFile(all_archive, "w"):
                pass

            with (
                patch("store_insight_collector.download_store_insight_all_zip", return_value=all_archive),
                patch(
                    "store_insight_collector.download_store_insight_zip",
                    side_effect=RiskControlDetected("risk control"),
                ),
            ):
                with self.assertRaisesRegex(RiskControlDetected, "risk control"):
                    collect_store_insight_payload(
                        Mock(),
                        Mock(),
                        "https://detail.tmall.com/item.htm?id=123",
                        "123",
                        "commerce",
                        root / "downloads",
                        root / "output",
                        {"main"},
                        60_000,
                        300,
                        None,
                    )

    def test_collect_store_insight_payload_reports_empty_compensation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_archive = root / "all.zip"
            with zipfile.ZipFile(all_archive, "w") as bundle:
                bundle.writestr("main/main.jpg", b"main")
            empty_sku_archive = root / "sku.zip"
            with zipfile.ZipFile(empty_sku_archive, "w"):
                pass

            with (
                patch("store_insight_collector.download_store_insight_all_zip", return_value=all_archive),
                patch("store_insight_collector.download_store_insight_zip", return_value=empty_sku_archive),
                patch("store_insight_collector.collect_item_metadata", return_value={"sku_variants": []}),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://detail.tmall.com/item.htm?id=123",
                    "123",
                    "commerce",
                    root / "downloads",
                    root / "output",
                    {"main", "sku"},
                    60_000,
                    300,
                    None,
                )

            self.assertEqual([record["type"] for record in records], ["main"])
            self.assertEqual(metadata["missing_asset_types"], ["sku"])
            self.assertIn("sku", metadata["asset_compensation_errors"])
            self.assertIn("no usable", metadata["asset_compensation_errors"]["sku"])

    def test_collect_store_insight_payload_deduplicates_compensation_hashes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            all_archive = root / "all.zip"
            with zipfile.ZipFile(all_archive, "w") as bundle:
                bundle.writestr("main/main.jpg", b"same")
            duplicate_sku_archive = root / "sku.zip"
            with zipfile.ZipFile(duplicate_sku_archive, "w") as bundle:
                bundle.writestr("sku/sku.jpg", b"same")

            with (
                patch("store_insight_collector.download_store_insight_all_zip", return_value=all_archive),
                patch("store_insight_collector.download_store_insight_zip", return_value=duplicate_sku_archive),
                patch("store_insight_collector.collect_item_metadata", return_value={"sku_variants": []}),
            ):
                records, metadata = collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    "https://detail.tmall.com/item.htm?id=123",
                    "123",
                    "commerce",
                    root / "downloads",
                    root / "output",
                    {"main", "sku"},
                    60_000,
                    300,
                    None,
                )

            self.assertEqual([record["type"] for record in records], ["main"])
            self.assertEqual(metadata["missing_asset_types"], ["sku"])
            self.assertIn("sku", metadata["asset_compensation_errors"])

    def test_douyin_checkbox_states_match_requested_assets(self):
        states = douyin_checkbox_states({"main", "detail"})

        self.assertEqual(
            states,
            {
                "main": True,
                "mainVideo": True,
                "detail": True,
                "detailLong": False,
                "productInfo": True,
                "productParam": True,
            },
        )

    def test_douyin_download_selects_all_files_by_label_when_menu_order_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, items = self._douyin_download_page(
                "123",
                [" 下载全部 ( 多文件 ) ", "自定义下载"],
            )

            result = download_douyin_package(page, "123", root, 60_000, {"main"})

            self.assertEqual(result, root / "douyin.zip")
            items[0].evaluate.assert_called_once_with("element => element.click()")
            items[1].evaluate.assert_not_called()

    def test_douyin_download_accepts_waxiang_split_file_menu(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, items = self._douyin_download_page("123", ["单文件", "多文件"])

            result = download_douyin_package(page, "123", root, 60_000, {"main"})

            self.assertEqual(result, root / "douyin.zip")
            items[0].evaluate.assert_not_called()
            items[1].evaluate.assert_called_once_with("element => element.click()")

    def test_douyin_download_opens_menu_before_waiting_for_multi_file_option(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, items = self._douyin_download_page(
                "123",
                ["单文件", "多文件"],
                menu_visible_after_click=True,
            )

            result = download_douyin_package(page, "123", root, 60_000, {"main"})

            self.assertEqual(result, root / "douyin.zip")
            page.locator("button").filter.return_value.evaluate.assert_called_once_with("element => element.click()")
            items[1].evaluate.assert_called_once_with("element => element.click()")

    def test_douyin_download_uses_page_id_and_accepts_decorated_multi_file_label(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, items = self._douyin_download_page("", ["单文件", "多文件\u200b下载"])
            page.url = "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id=123"

            result = download_douyin_package(page, "", root, 60_000, {"main"})

            self.assertEqual(result, root / "douyin.zip")
            page.query_input.fill.assert_called_once_with("123")
            items[0].evaluate.assert_not_called()
            items[1].evaluate.assert_called_once_with("element => element.click()")

    def test_douyin_download_waits_for_query_loading_mask_before_opening_menu(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, _ = self._douyin_download_page("", ["单文件", "多文件"], menu_visible_after_click=True)
            page.url = "https://haohuo.jinritemai.com/ecommerce/trade/detail/index.html?id=123"
            events: list[str] = []

            page.loading_mask.wait_for.side_effect = lambda **_: events.append("query-ready")
            original_click = page.download_button.evaluate.side_effect

            def click_download(script):
                events.append("download")
                original_click(script)

            page.download_button.evaluate.side_effect = click_download

            download_douyin_package(page, "", root, 60_000, {"main"})

            self.assertIn("query-ready", events)
            self.assertLess(events.index("query-ready"), events.index("download"))
            page.loading_mask.wait_for.assert_called_once_with(state="hidden", timeout=60_000)

    def test_douyin_download_waits_for_resource_refresh_after_setting_checkboxes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, _ = self._douyin_download_page(
                "123",
                ["单文件", "多文件"],
                menu_visible_after_click=True,
                selected_types={"main", "detail"},
            )

            with patch("douyin_collector._wait_for_resource_refresh", create=True) as wait_refresh:
                download_douyin_package(page, "123", root, 60_000, {"main", "detail"})

            wait_refresh.assert_called_once_with(page, 60_000)

    def test_douyin_download_rejects_data_only_archive_when_images_were_requested(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, _ = self._douyin_download_page(
                "123",
                ["单文件", "多文件"],
                selected_types={"main", "detail"},
            )

            def save_data_only(target):
                with zipfile.ZipFile(target, "w") as bundle:
                    bundle.writestr("商品数据/商品数据.xlsx", b"xlsx")

            page.expect_download.return_value.__enter__.return_value.value.save_as.side_effect = save_data_only

            with self.assertRaisesRegex(RuntimeError, "主图.*详情图"):
                download_douyin_package(page, "123", root, 60_000, {"main", "detail"})

    def test_douyin_download_rejects_archive_missing_one_requested_image_type(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, _ = self._douyin_download_page(
                "123",
                ["单文件", "多文件"],
                selected_types={"main", "detail"},
            )

            def save_main_only(target):
                with zipfile.ZipFile(target, "w") as bundle:
                    bundle.writestr("主图/主图_1.jpg", b"main")
                    bundle.writestr("商品数据/商品数据.xlsx", b"xlsx")

            page.expect_download.return_value.__enter__.return_value.value.save_as.side_effect = save_main_only

            with self.assertRaisesRegex(RuntimeError, "详情图"):
                download_douyin_package(page, "123", root, 60_000, {"main", "detail"})

    def test_douyin_download_rejects_menu_without_all_files_option(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            page, items = self._douyin_download_page(
                "123",
                ["自定义下载", "下载单文件"],
            )

            with self.assertRaisesRegex(RuntimeError, "自定义下载.*下载单文件"):
                download_douyin_package(page, "123", root, 60_000, {"main"})

            for item in items:
                item.evaluate.assert_not_called()

    def test_materialize_douyin_package_reads_images_video_and_product_workbook(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            metadata_workbook = root / "metadata.xlsx"
            workbook = Workbook()
            data = workbook.active
            data.title = "商品数据"
            data.append(["商品标题", "商品ID", "商品品牌", "商品类目", "商品价格", "SKU数量", "30天销量"])
            data.append(["测试商品", "3827632284920578347", "测试品牌", "家居>收纳", "39.90", 0, "300+"])
            parameters = workbook.create_sheet("商品参数")
            parameters.append(["材质", "颜色"])
            parameters.append(["不锈钢", "银色"])
            workbook.save(metadata_workbook)

            archive = root / "douyin.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("主图/主图_1.jpg", b"main")
                bundle.writestr("SKU图/SKU_1.jpg", b"sku")
                bundle.writestr("详情图/详情图_1.jpg", b"detail")
                bundle.writestr("主图视频.mp4", b"video")
                bundle.write(metadata_workbook, "商品数据/商品数据&商品参数.xlsx")

            records, metadata = materialize_douyin_package(
                archive,
                root / "output",
                {"main", "detail"},
            )

            self.assertEqual([record["type"] for record in records], ["main", "detail"])
            self.assertTrue(Path(metadata["main_video_local_path"]).is_file())
            self.assertEqual(metadata["main_video_status"], "local_only")
            self.assertEqual(metadata["product_title"], "测试商品")
            self.assertEqual(metadata["current_price"], "39.90")
            self.assertEqual(metadata["product_parameters"], [
                {"name": "材质", "value": "不锈钢"},
                {"name": "颜色", "value": "银色"},
            ])
            self.assertEqual(metadata["sku_metadata_status"], "not_found")
            self.assertEqual(metadata["sku_variants"], [])

    def test_materialize_douyin_package_caps_main_images(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "douyin.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("主图/主图_1.jpg", b"one")
                bundle.writestr("主图/主图_2.jpg", b"two")

            records, _ = materialize_douyin_package(
                archive,
                root / "output",
                {"main"},
                max_main_images=1,
            )

            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["type"], "main")

    def test_validate_item_url_extracts_douyin_product_id(self):
        url, product_id = validate_item_url(
            "https://haohuo.jinritemai.com/views/product/item2.html?id=3827632284920578347"
        )

        self.assertEqual(product_id, "3827632284920578347")
        self.assertEqual(
            url,
            "https://haohuo.jinritemai.com/views/product/item2.html?id=3827632284920578347",
        )

    def test_validate_item_url_accepts_tmall_global_product(self):
        url, product_id = validate_item_url(
            "https://detail.tmall.hk/hk/item.htm?id=1029369648253&skuId=6204435114411"
        )

        self.assertEqual(product_id, "1029369648253")
        self.assertEqual(
            url,
            "https://detail.tmall.hk/hk/item.htm?id=1029369648253&skuId=6204435114411",
        )

    def test_collects_normalized_product_title_and_current_price(self):
        page = Mock()
        page.evaluate.return_value = {"product_title": " 便携咖啡杯 ", "current_price": "￥ 39.90 起"}

        summary = collect_product_summary(page)

        self.assertEqual(summary["product_title"], "便携咖啡杯")
        self.assertEqual(summary["current_price"], "39.90")

    def test_quick_download_labels_cover_store_insight_variants(self):
        self.assertIn("1:1主图", ASSET_LABELS["main"])
        self.assertIn("主图图片", ASSET_LABELS["main"])
        self.assertIn("sku图片", ASSET_LABELS["sku"])
        self.assertIn("详情页长图", ASSET_LABELS["detail"])
        self.assertNotIn("主图", ASSET_LABELS["main"])

    def test_quick_download_does_not_use_custom_download_as_a_fallback(self):
        source = Path("store_insight_collector.py").read_text(encoding="utf-8")
        function_body = source.split("def download_store_insight_zip", 1)[1].split("def safe_extract", 1)[0]
        self.assertIn("click_store_insight_asset", function_body)
        self.assertNotIn("custom main download", function_body)

    def test_all_files_download_label_requires_multi_file_option(self):
        self.assertTrue(is_all_files_download_label("下载全部（多文件）"))
        self.assertTrue(is_all_files_download_label(" 下载全部  ( 多文件 ) "))
        self.assertFalse(is_all_files_download_label("下载全部（单文件）"))
        self.assertFalse(is_all_files_download_label("自定义下载"))

    def test_download_complete_dialog_text_is_recognized(self):
        self.assertIsNotNone(DOWNLOAD_COMPLETE_PATTERN.search("5个文件下载完成"))
        self.assertIsNotNone(DOWNLOAD_COMPLETE_PATTERN.search("文件下载完成"))

    def test_platform_challenge_uses_the_page_title_or_body(self):
        class Page:
            def __init__(self, title: str, body: str, url: str = "") -> None:
                self._title = title
                self._body = body
                self.url = url

            def title(self) -> str:
                return self._title

            def locator(self, selector: str):
                assert selector == "body"
                body = self._body

                class Body:
                    def inner_text(self, timeout: int) -> str:
                        return body

                return Body()

        self.assertEqual(platform_challenge_text(Page("验证码拦截", "")), "验证码")
        self.assertEqual(platform_challenge_text(Page("商品详情", "请完成滑块验证")), "滑块")
        self.assertEqual(platform_challenge_text(Page("商品详情", "符合镜像原理")), "符合镜像")
        self.assertEqual(platform_challenge_text(Page("商品详情", "图形验证")), "图形验证")
        self.assertEqual(platform_challenge_text(Page("商品详情", "", "https://sec.taobao.com/punish")), "平台验证")
        self.assertIsNone(platform_challenge_text(Page("商品详情", "正常页面")))

    def test_platform_challenge_detects_verification_inside_iframe(self):
        class Frame:
            url = "https://captcha.example/verify"

            @staticmethod
            def title() -> str:
                return ""

            @staticmethod
            def locator(selector: str):
                class Body:
                    @staticmethod
                    def inner_text(timeout: int) -> str:
                        return "请将符合描述的图片拖动到指定区域"

                return Body()

        class Page:
            url = "https://detail.tmall.com/item.htm?id=1"
            frames = [Frame()]

            @staticmethod
            def title() -> str:
                return "商品详情"

            @staticmethod
            def locator(selector: str):
                class Body:
                    @staticmethod
                    def inner_text(timeout: int) -> str:
                        return "正常商品页面"

                return Body()

        self.assertEqual(platform_challenge_text(Page()), "验证码")

    def test_download_wait_stops_clicking_when_challenge_appears(self):
        class Page:
            def __init__(self) -> None:
                self.handlers = {}

            def on(self, event: str, handler) -> None:
                self.handlers[event] = handler

            def remove_listener(self, event: str, handler) -> None:
                self.handlers.pop(event, None)

            def wait_for_timeout(self, timeout_ms: int) -> None:
                pass

        page = Page()
        click = Mock()
        with (
            patch("store_insight_collector.platform_challenge_text", return_value="符合镜像"),
            patch("store_insight_collector.wait_for_platform_challenge", return_value=True) as wait,
        ):
            with self.assertRaisesRegex(RuntimeError, "验证已解除"):
                wait_for_download_after_click(page, click, 5000, 600)

        click.assert_called_once_with()
        wait.assert_called_once_with(page, 600)
        self.assertEqual(page.handlers, {})

    def test_access_refused_dialog_stops_collection_without_waiting(self):
        class Page:
            url = "https://s.taobao.com/search"

            def title(self) -> str:
                return "淘宝网"

            def locator(self, selector: str):
                assert selector == "body"

                class Body:
                    def inner_text(self, timeout: int) -> str:
                        return "亲，访问被拒绝"

                return Body()

        with self.assertRaisesRegex(RiskControlDetected, "访问被拒绝"):
            wait_for_platform_challenge(Page(), 600)

    def test_reload_item_reuses_current_product_page_without_refreshing(self):
        item_url = "https://detail.tmall.com/item.htm?id=123"

        class Body:
            def inner_text(self, timeout: int) -> str:
                return "正常商品详情"

        class Page:
            url = item_url

            def __init__(self) -> None:
                self.goto_calls = 0

            def title(self) -> str:
                return "商品详情"

            def locator(self, selector: str) -> Body:
                self.assert_selector = selector
                return Body()

            def goto(self, *args, **kwargs) -> None:
                self.goto_calls += 1

            def wait_for_timeout(self, timeout: int) -> None:
                pass

        page = Page()
        with patch("store_insight_collector.wait_for_store_insight_entry") as wait_entry:
            reload_item(page, item_url, 60_000, 300)

        wait_entry.assert_called_once_with(page, 60_000, 300)
        self.assertEqual(page.goto_calls, 0)

    def test_reload_item_refreshes_once_when_extension_entry_is_not_ready(self):
        item_url = "https://detail.tmall.com/item.htm?id=123"

        class Page:
            url = item_url

            def __init__(self) -> None:
                self.reload_calls = 0

            def locator(self, selector: str):
                class Body:
                    def inner_text(self, timeout: int) -> str:
                        return "正常商品页面"

                return Body()

            def reload(self, *args, **kwargs) -> None:
                self.reload_calls += 1

            def wait_for_timeout(self, timeout: int) -> None:
                pass

        page = Page()
        with (
            patch("store_insight_collector.wait_for_store_insight_entry", side_effect=[RuntimeError("entry not ready"), None]) as wait_entry,
            patch("store_insight_collector.login_required", return_value=False),
            patch("store_insight_collector.platform_challenge_text", return_value=None),
        ):
            reload_item(page, item_url, 60_000, 300)

        self.assertEqual(page.reload_calls, 1)
        self.assertEqual(wait_entry.call_count, 2)

    def test_safe_extract_sanitizes_windows_invalid_path_components(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "invalid-names.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr(
                    "SKU|\u56fe/\u4eba\u6559\u793e\u8054\u5408 | OPP\u888b?.jpg",
                    b"invalid",
                )
                bundle.writestr("folder. /control\x01name. ", b"control")
                bundle.writestr("CON/NUL.jpg", b"reserved")

            target = root / "output"
            extracted = safe_extract(archive, target)
            resolved_target = target.resolve()

            self.assertEqual(
                {path.relative_to(resolved_target).as_posix(): path.read_bytes() for path in extracted},
                {
                    "SKU_\u56fe/\u4eba\u6559\u793e\u8054\u5408 _ OPP\u888b_.jpg": b"invalid",
                    "folder_/control_name_": b"control",
                    "_CON/_NUL.jpg": b"reserved",
                },
            )

    def test_safe_extract_deduplicates_sanitized_names(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "duplicate-names.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("SKU/a|b.jpg", b"first")
                bundle.writestr("SKU/a?b.jpg", b"second")

            target = root / "output"
            extracted = safe_extract(archive, target)
            resolved_target = target.resolve()

            self.assertEqual(
                [path.relative_to(resolved_target).as_posix() for path in extracted],
                ["SKU/a_b.jpg", "SKU/a_b_2.jpg"],
            )
            self.assertEqual([path.read_bytes() for path in extracted], [b"first", b"second"])

    def test_safe_extract_rejects_parent_traversal(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe-path.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("../escape.jpg", b"escape")

            with self.assertRaisesRegex(RuntimeError, "Unsafe ZIP member path"):
                safe_extract(archive, root / "output")

            self.assertFalse((root / "escape.jpg").exists())

    def test_materialize_keeps_only_selected_types(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "package.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("main/main.jpg", b"main")
                bundle.writestr("sku/sku.jpg", b"sku")
                bundle.writestr("detail/detail.jpg", b"detail")

            records = materialize(
                archive,
                "main",
                root / "output",
                set(),
                allowed_types={"main"},
            )

            self.assertEqual([record["type"] for record in records], ["main"])
            self.assertTrue((root / "output" / "main").is_dir())
            self.assertFalse((root / "output" / "sku").exists())
            self.assertFalse((root / "output" / "detail").exists())

    def test_materialize_uses_short_temporary_extraction_directory(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "package.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("main/main.jpg", b"main")
            output = root.joinpath(*(["nested-output"] * 12))
            extraction_targets = []

            def observe_extract(zip_path, target_dir):
                extraction_targets.append(Path(target_dir).resolve())
                return safe_extract(zip_path, target_dir)

            with patch("store_insight_collector.safe_extract", side_effect=observe_extract):
                records = materialize(archive, "main", output, set())

            resolved_output = output.resolve()
            self.assertEqual(len(extraction_targets), 1)
            self.assertNotIn(resolved_output, extraction_targets[0].parents)
            self.assertFalse(extraction_targets[0].exists())
            self.assertEqual(Path(records[0]["path"]).read_bytes(), b"main")

    def test_materialize_classifies_all_files_package_without_guessing_unknown_images(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "all.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("页面图/page.jpg", b"main")
                bundle.writestr("SKU图/sku.jpg", b"sku")
                bundle.writestr("详情图/detail.jpg", b"detail")
                bundle.writestr("unknown.jpg", b"unknown")

            records = materialize(
                archive,
                "all",
                root / "output",
                set(),
                allowed_types={"main", "sku", "detail"},
            )

            self.assertEqual([record["type"] for record in records], ["main", "sku", "detail"])

    def test_uses_installed_waxiang_extension_before_unpacking_crx(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "waxiang.exe"
            executable.write_bytes(b"browser")
            extension_dir = root / "local" / "Waxiang" / "User Data" / "Default" / "Extensions" / "extension-id" / "5.0.6_0"
            extension_dir.mkdir(parents=True)
            (extension_dir / "manifest.json").write_text('{"name":"店透视"}', encoding="utf-8")

            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "local")}):
                resolved = find_waxiang_store_insight_extension(str(executable), root / "profile")

            self.assertEqual(resolved, extension_dir)

    def test_unpacks_embedded_waxiang_extension_when_user_profile_is_unavailable(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            executable = root / "waxiang.exe"
            executable.write_bytes(b"browser")
            archive_path = root / "1.0.0" / "extensions" / "diantoushi.crx"
            archive_path.parent.mkdir(parents=True)
            bundle = root / "extension.zip"
            with zipfile.ZipFile(bundle, "w") as archive:
                archive.writestr("manifest.json", '{"name":"店透视"}')
            archive_path.write_bytes(b"Cr24" + bundle.read_bytes())

            with patch.dict("os.environ", {"LOCALAPPDATA": str(root / "empty")}, clear=False):
                resolved = find_waxiang_store_insight_extension(str(executable), root / "profile")

            self.assertIsNotNone(resolved)
            self.assertTrue((resolved / "manifest.json").is_file())

    def test_closes_only_project_profile_browser_processes(self):
        completed = Mock(stdout="32136\nnot-a-pid\n")
        profile = Path(r"C:\Users\Example\AppData\Local\ProductImageWorkflow\store-insight-profile")

        with patch("store_insight_collector.subprocess.run", return_value=completed) as run:
            closed_count = close_project_browser_for_profile(profile)

        self.assertEqual(closed_count, 1)
        self.assertEqual(run.call_args.args[0][:3], ["powershell.exe", "-NoProfile", "-NonInteractive"])
        self.assertEqual(run.call_args.kwargs["env"]["PRODUCT_IMAGE_PROFILE_DIR"], str(profile))
        script = run.call_args.args[0][-1]
        self.assertIn(".IndexOf($profile", script)

    def test_attaches_sku_spec_color_and_price_to_sku_images(self):
        images = [
            {"type": "sku", "source_name": "SKU图_1_颜色分类:黑色;尺码:M.jpg"},
            {"type": "main", "source_name": "main.jpg"},
        ]
        variants = [
            {
                "source_index": "1",
                "sku_id": "1001",
                "sku_label": "颜色分类:黑色;尺码:M",
                "spec_text": "M",
                "color_text": "黑色",
                "list_price": "99",
                "after_coupon_price": "79",
                "stock": "12",
                "net_content": "",
                "parse_status": "parsed",
            }
        ]

        attach_sku_variants_to_images(images, variants)

        self.assertEqual(images[0]["spec_text"], "M")
        self.assertEqual(images[0]["color_text"], "黑色")
        self.assertEqual(images[0]["after_coupon_price"], "79")
        self.assertEqual(images[0]["metadata_status"], "table_matched")
        self.assertNotIn("spec_text", images[1])

    def test_parses_color_and_spec_from_plain_sku_label(self):
        parsed = parse_sku_variant_fields("绿色 扩容款", 1)

        self.assertEqual(parsed["color_text"], "绿色")
        self.assertEqual(parsed["spec_text"], "扩容款")
        self.assertEqual(parsed["parse_status"], "parsed")

    def test_sku_images_match_table_source_index_when_archive_order_is_scrambled(self):
        images = [{"type": "sku", "source_name": "SKU图_2_灰色 扩容款.jpg"}]
        variants = [
            {"source_index": "1", "sku_label": "绿色 扩容款", "color_text": "绿色", "spec_text": "扩容款"},
            {"source_index": "2", "sku_label": "灰色 扩容款", "color_text": "灰色", "spec_text": "扩容款"},
        ]

        attach_sku_variants_to_images(images, variants)

        self.assertEqual(images[0]["sku_label"], "灰色 扩容款")
        self.assertEqual(images[0]["color_text"], "灰色")

    def test_manifest_keeps_video_parameters_and_sku_variants(self):
        metadata = empty_parameter_metadata("1001", "complete")
        metadata["product_parameters"] = [{"name": "材质", "value": "棉"}]
        metadata.update(empty_video_metadata())
        metadata.update(empty_sku_metadata())
        metadata.update(
            {
                "main_video_url": "https://cloud.video.taobao.com/example.mp4",
                "main_video_status": "complete",
                "sku_metadata_status": "complete",
                "sku_variants": [{"sku_label": "黑色 M", "list_price": "99"}],
                "product_title": "商品标题",
                "current_price": "79",
            }
        )

        manifest = build_manifest("https://item.taobao.com/item.htm?id=1001", "1001", Path("output"), [], metadata)

        self.assertEqual(manifest["schema_version"], 2)
        self.assertEqual(manifest["main_video_status"], "complete")
        self.assertEqual(manifest["product_parameters"][0]["name"], "材质")
        self.assertEqual(manifest["sku_variants"][0]["list_price"], "99")
        self.assertEqual(manifest["product_title"], "商品标题")
        self.assertEqual(manifest["current_price"], "79")


if __name__ == "__main__":
    unittest.main()
