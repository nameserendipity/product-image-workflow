import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from PIL import Image

from kuaishou_collector import (
    Asset,
    build_assets,
    classify_image_url,
    dedupe_urls,
    download_asset,
    extract_product_payload,
    materialize_assets,
)


class KuaishouCollectorTests(unittest.TestCase):
    def test_extract_product_payload_keeps_media_categories_and_metadata(self):
        payload = {
            "goodsInfo": {
                "goodsId": "26065497098904",
                "goodsTitle": "心相印茶语丝享餐巾纸",
                "salePrice": "￥19.90",
                "gallery": [
                    "https://p4-ec.ecukwai.com/ITEM_IMAGE-1.jpg",
                    "https://p4-ec.ecukwai.com/ITEM_DETAIL_IMAGE-1.jpg",
                ],
                "video": {"playUrl": "https://v4-ec.ecukwai.com/product.mp4"},
            }
        }

        result = extract_product_payload(payload, product_id="26065497098904")

        self.assertEqual(result["title"], "心相印茶语丝享餐巾纸")
        self.assertEqual(result["price"], "19.90")
        self.assertEqual(result["mainImageUrls"], ["https://p4-ec.ecukwai.com/ITEM_IMAGE-1.jpg"])
        self.assertEqual(result["detailImageUrls"], ["https://p4-ec.ecukwai.com/ITEM_DETAIL_IMAGE-1.jpg"])
        self.assertEqual(result["videoUrls"], ["https://v4-ec.ecukwai.com/product.mp4"])

    def test_extract_product_payload_excludes_other_products_and_sku_thumbnails(self):
        payload = {
            "goodsInfo": {
                "goodsId": "26065497098904",
                "gallery": ["https://p4-ec.ecukwai.com/ITEM_IMAGE-current-1.jpg"],
                "skuList": [
                    {"image": "https://p4-ec.ecukwai.com/ITEM_IMAGE-sku-1.jpg"},
                ],
            },
            "recommendations": [
                {
                    "goodsId": "999",
                    "image": "https://p4-ec.ecukwai.com/ITEM_IMAGE-recommended-1.jpg",
                }
            ],
            "floatingBanner": "https://p4-ec.ecukwai.com/ITEM_IMAGE-unbound-ad-1.jpg",
        }

        result = extract_product_payload(payload, product_id="26065497098904")

        self.assertEqual(
            result["mainImageUrls"],
            ["https://p4-ec.ecukwai.com/ITEM_IMAGE-current-1.jpg"],
        )

    def test_kuaishou_image_mirrors_are_deduplicated_in_source_order(self):
        first = "https://p4-ec.ecukwai.com/bs2/image-kwaishop-product/ITEM_IMAGE-1.jpg"
        mirror = (
            "https://p5-ec.ecukwai.com/bs2/image-kwaishop-product/ITEM_IMAGE-1.jpg"
            "?x-oss-process=image/format,webp"
        )

        self.assertEqual(dedupe_urls([first, mirror]), [first])

    def test_unrelated_page_images_are_not_classified_as_product_assets(self):
        self.assertIsNone(classify_image_url("https://cdn.example/icon.png"))

    def test_build_assets_rejects_local_and_untrusted_urls(self):
        assets = build_assets(
            {
                "mainImageUrls": [
                    "file:///C:/secret.txt?name=ITEM_IMAGE-1.jpg",
                    "http://127.0.0.1/ITEM_IMAGE-2.jpg",
                    "https://evil.example/ITEM_IMAGE-3.jpg",
                ],
                "detailImageUrls": [],
                "videoUrls": ["http://169.254.169.254/latest/meta-data/file.mp4"],
            }
        )

        self.assertEqual(assets, {"main": [], "detail": [], "video": []})

    def test_download_asset_rejects_html_error_page(self):
        class Response:
            headers = {"Content-Type": "text/html", "Content-Length": "18"}

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://p4-ec.ecukwai.com/ITEM_IMAGE-error.jpg"

            def read(self, _size):
                return b"<html>error</html>"

        with TemporaryDirectory() as directory:
            destination = Path(directory) / "image.jpg"
            with (
                patch("kuaishou_collector.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
                patch("kuaishou_collector.urlopen", return_value=Response()),
            ):
                with self.assertRaisesRegex(RuntimeError, "Content-Type"):
                    download_asset(
                        "https://p4-ec.ecukwai.com/ITEM_IMAGE-error.jpg",
                        destination,
                        category="main",
                        attempts=1,
                    )

        self.assertFalse(destination.exists())

    def test_download_asset_retries_once_and_never_a_third_time(self):
        with TemporaryDirectory() as directory:
            destination = Path(directory) / "image.jpg"
            with (
                patch("kuaishou_collector.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 443))]),
                patch("kuaishou_collector.urlopen", side_effect=OSError("network failure")) as open_url,
            ):
                with self.assertRaisesRegex(OSError, "network failure"):
                    download_asset("https://p4-ec.ecukwai.com/ITEM_IMAGE-1.jpg", destination)

        self.assertEqual(open_url.call_count, 2)

    def test_materialize_assets_keeps_categories_and_never_creates_sku_images(self):
        assets = {
            "main": [Asset("https://cdn.example/ITEM_IMAGE-1.jpg", "main-01.jpg", "main")],
            "detail": [Asset("https://cdn.example/ITEM_DETAIL_IMAGE-1.jpg", "detail-01.jpg", "detail")],
            "video": [Asset("https://cdn.example/product.mp4", "video-01.mp4", "video")],
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)

            def write_asset(_url, destination, **_kwargs):
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.suffix == ".mp4":
                    destination.write_bytes(b"\x00\x00\x00\x18ftypmp42")
                else:
                    Image.new("RGB", (2, 2), "white").save(destination)

            with patch("kuaishou_collector.download_asset", side_effect=write_asset):
                records, failures = materialize_assets(assets, root, "https://app.kwaixiaodian.com/item?id=1")

            self.assertEqual([record["type"] for record in records], ["main", "detail", "video"])
            self.assertEqual(failures, [])
            self.assertFalse((root / "sku").exists())


if __name__ == "__main__":
    unittest.main()
