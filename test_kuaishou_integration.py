import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from openpyxl import Workbook
from PIL import Image

import store_insight_collector
from agent_flow import AgentSession
from batch_workflow import extract_direct_link_items


KUAISHOU_URL = (
    "https://app.kwaixiaodian.com/web/kwaishop-goods-detail-page-app"
    "?id=26065497098904&source=goodsListshare_pc_kwaixiaodian"
)


class KuaishouIntegrationTests(unittest.TestCase):
    def test_single_link_session_accepts_kuaishou_product_page(self):
        self.assertTrue(AgentSession._is_supported_url(KUAISHOU_URL))

    def test_single_link_session_rejects_invalid_kuaishou_product_urls(self):
        for value in (
            "httpx://app.kwaixiaodian.com/web/kwaishop-goods-detail-page-app?id=123",
            "https://app.kwaixiaodian.com/web/kwaishop-goods-detail-page-app?id=abc",
            "https://evil.kwaixiaodian.com/not-a-product?id=123",
        ):
            with self.subTest(value=value):
                self.assertFalse(AgentSession._is_supported_url(value))
                with self.assertRaises(ValueError):
                    store_insight_collector.validate_item_url(value)

    def test_validate_item_url_accepts_kuaishou_product_page(self):
        try:
            url, product_id = store_insight_collector.validate_item_url(KUAISHOU_URL)
        except ValueError as error:
            self.fail(f"Kuaishou URL was rejected: {error}")

        self.assertEqual(url, KUAISHOU_URL)
        self.assertEqual(product_id, "26065497098904")

    def test_direct_link_batch_keeps_kuaishou_url_and_sku_screenshot(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workbook = Workbook()
            sheet = workbook.active
            sheet.append(["商品链接", "SKU截图"])
            sheet.append([KUAISHOU_URL, (root / "sku.png").as_uri()])
            Image.new("RGB", (600, 900), "white").save(root / "sku.png")
            source = root / "kuaishou.xlsx"
            workbook.save(source)

            items = extract_direct_link_items(source, root / "staged")
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].platform, "kuaishou")
            self.assertIsNotNone(items[0].sku_screenshot)
            self.assertTrue(items[0].sku_screenshot.is_file())

    def test_kuaishou_collection_dispatches_to_kuaishou_adapter(self):
        adapter = getattr(store_insight_collector, "collect_kuaishou_payload", None)
        self.assertTrue(
            callable(adapter),
            "Kuaishou collection needs a dedicated adapter entry point",
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            expected_records = [
                {"type": "main", "path": str(root / "main-images" / "main-01.jpg")},
                {"type": "detail", "path": str(root / "detail-images" / "detail-01.jpg")},
                {"type": "video", "path": str(root / "video" / "asset-01.mp4")},
            ]
            expected_metadata = {
                "product_title": "多肽洗发水",
                "sku_metadata_status": "text_conditioned",
                "sku_variants": [
                    {
                        "sku_name": "洗发水800ml+沐浴露800ml",
                        "source_status": "text_conditioned",
                    }
                ],
            }
            with (
                patch.object(store_insight_collector, "collect_kuaishou_payload", return_value=(expected_records, expected_metadata)) as collect,
                patch.object(store_insight_collector, "download_store_insight_all_zip") as legacy,
                patch.object(store_insight_collector, "wait_for_platform_challenge", return_value=False),
            ):
                records, metadata = store_insight_collector.collect_store_insight_payload(
                    Mock(),
                    Mock(),
                    KUAISHOU_URL,
                    "26065497098904",
                    "kuaishou",
                    root / "downloads",
                    root / "output",
                    {"main", "sku", "detail"},
                    60_000,
                    300,
                    None,
                )

        self.assertEqual(records, expected_records)
        self.assertEqual(metadata, expected_metadata)
        collect.assert_called_once()
        legacy.assert_not_called()

    def test_kuaishou_adapter_collects_public_media_without_fabricating_sku_images(self):
        response = Mock()
        response.url = "https://app.kwaixiaodian.com/api/goods/detail"
        response.header_value.return_value = "application/json"
        response.json.return_value = {
            "goodsInfo": {
                "goodsId": "26065497098904",
                "goodsTitle": "心相印茶语丝享餐巾纸",
                "salePrice": "￥19.90",
                "gallery": [
                    "https://p4-ec.ecukwai.com/ITEM_IMAGE-current-1.jpg",
                    "https://p4-ec.ecukwai.com/ITEM_DETAIL_IMAGE-current-1.jpg",
                ],
                "video": {"playUrl": "https://v4-ec.ecukwai.com/product.mp4"},
                "goodsParams": [
                    {"attrName": "净含量", "attrValue": "800ml"},
                    {"attrName": "包装形式", "attrValue": "泵瓶"},
                ],
            }
        }
        page = Mock()
        response_handler = {}
        page.on.side_effect = lambda event, handler: response_handler.__setitem__(event, handler)
        page.reload.side_effect = lambda **_kwargs: response_handler["response"](response)
        body = Mock()
        body.inner_text.return_value = "心相印茶语丝享餐巾纸\n￥19.90"
        images = Mock()
        images.evaluate_all.return_value = []
        page.locator.side_effect = lambda selector: body if selector == "body" else images
        page.title.return_value = "心相印茶语丝享餐巾纸"

        with TemporaryDirectory() as directory:
            root = Path(directory)

            def write_asset(_url, destination, **_kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"asset")

            with (
                patch("kuaishou_collector.download_asset", side_effect=write_asset),
                patch("store_insight_collector.wait_for_platform_challenge", return_value=False) as wait_challenge,
            ):
                records, metadata = store_insight_collector.collect_kuaishou_payload(
                    page,
                    KUAISHOU_URL,
                    "26065497098904",
                    root,
                    {"main", "sku", "detail"},
                    60_000,
                    300,
                    10,
                )

        self.assertEqual([record["type"] for record in records], ["main", "detail", "video"])
        self.assertEqual(metadata["product_title"], "心相印茶语丝享餐巾纸")
        self.assertEqual(metadata["parameter_status"], "complete")
        self.assertEqual(metadata["parameter_source_product_id"], "26065497098904")
        self.assertEqual(
            metadata["product_parameters"],
            [
                {
                    "name": "净含量",
                    "value": "800ml",
                    "source": "platform_api",
                    "handling": "快手平台原值",
                },
                {
                    "name": "包装形式",
                    "value": "泵瓶",
                    "source": "platform_api",
                    "handling": "快手平台原值",
                },
            ],
        )
        self.assertEqual(metadata["sku_metadata_status"], "not_found")
        self.assertEqual(metadata["sku_variants"], [])
        self.assertEqual(metadata["missing_asset_types"], ["sku"])
        self.assertFalse(any(record["type"] == "sku" for record in records))
        wait_challenge.assert_called_once_with(page, 300)
        page.remove_listener.assert_called_once()

    def test_kuaishou_adapter_rejects_empty_collection_after_reload(self):
        page = Mock()
        page.locator.return_value.evaluate_all.return_value = []
        page.title.return_value = "账号登录"
        with TemporaryDirectory() as directory:
            with patch("store_insight_collector.wait_for_platform_challenge", return_value=False):
                with self.assertRaisesRegex(RuntimeError, "未采集到有效主图"):
                    store_insight_collector.collect_kuaishou_payload(
                        page,
                        KUAISHOU_URL,
                        "26065497098904",
                        Path(directory),
                        {"main", "sku", "detail"},
                        60_000,
                        300,
                        10,
                    )

    def test_kuaishou_manifest_keeps_media_categories_and_text_only_sku_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            images = [
                {"type": "main", "path": str(root / "main-images" / "main-01.jpg")},
                {"type": "detail", "path": str(root / "detail-images" / "detail-01.jpg")},
                {"type": "video", "path": str(root / "video" / "asset-01.mp4")},
            ]
            metadata = {
                "product_title": "多肽洗发水",
                "sku_metadata_status": "text_conditioned",
                "sku_variants": [{"sku_name": "洗发水800ml+沐浴露800ml"}],
            }

            manifest = store_insight_collector.build_manifest(
                KUAISHOU_URL,
                "26065497098904",
                root,
                images,
                metadata,
            )

        self.assertEqual(
            [image["type"] for image in manifest["images"]],
            ["main", "detail", "video"],
        )
        self.assertEqual(manifest["sku_metadata_status"], "text_conditioned")
        self.assertEqual(manifest["sku_variants"][0]["sku_name"], "洗发水800ml+沐浴露800ml")


if __name__ == "__main__":
    unittest.main()
