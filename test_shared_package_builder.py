import json
import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from product_identity import ProductIdentity
from shared_package_builder import SharedPackageBuilder, materialize_reused_package


class SharedPackageBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.generated_root = self.root / "generated"
        self.generated_root.mkdir()
        self.source_root = self.root / "collected"
        self.source_root.mkdir()
        self.source_image = self.source_root / "source-main.jpg"
        self._image(self.source_image, (40, 80, 120))
        self.source_manifest = self.source_root / "manifest.json"
        self.source_manifest.write_text(
            json.dumps(
                {
                    "product_id": "123",
                    "images": [
                        {
                            "type": "main",
                            "path": str(self.source_image),
                            "url": "https://img.example/source.jpg",
                        }
                    ],
                    "products": [{"product_id": "123", "title": "测试商品"}],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.workbook = self.root / "result.xlsx"
        self.workbook.write_bytes(b"xlsx-content")
        self.identity = ProductIdentity(
            platform="taobao",
            product_id="123",
            product_key="taobao-123",
            source_url="https://item.taobao.com/item.htm?id=123&spm=test",
            canonical_url="https://item.taobao.com/item.htm?id=123",
        )
        self.builder = SharedPackageBuilder(
            self.root / "packages",
            "product-workflow/shared-library",
            "client-one",
            now=lambda: datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _image(path: Path, color: tuple[int, int, int]) -> None:
        Image.new("RGB", (96, 96), color).save(path, format="JPEG")

    def records(self, main: int = 10, sku: int = 3, detail: int = 6) -> list[dict]:
        records: list[dict] = []
        colors = {"main": (200, 20, 20), "sku": (20, 160, 50), "detail": (30, 70, 200)}
        for category, count in (("main", main), ("sku", sku), ("detail", detail)):
            for ordinal in range(1, count + 1):
                path = self.generated_root / f"{category}-{ordinal}.jpg"
                self._image(path, colors[category])
                records.append(
                    {
                        "category": category,
                        "ordinal": ordinal,
                        "status": "completed",
                        "output_path": str(path),
                    }
                )
        return records

    def build(self, **overrides):
        arguments = {
            "identity": self.identity,
            "task_id": "task-one",
            "source_manifest": self.source_manifest,
            "generated_records": self.records(),
            "titles": {"long_title": "测试商品长标题", "short_title": "测试商品"},
            "workbook_path": self.workbook,
            "generation_mode": "competitor_reference",
            "workflows": ("main", "sku", "detail"),
            "max_main_images": 10,
            "max_sku_images": None,
            "max_detail_images": None,
        }
        arguments.update(overrides)
        return self.builder.build(**arguments)

    def test_complete_default_job_is_publishable(self) -> None:
        package = self.build()

        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(package.catalog["main_count"], 10)
        self.assertEqual(package.catalog["sku_count"], 3)
        self.assertEqual(package.catalog["detail_count"], 6)
        self.assertEqual(
            set(package.files),
            {
                "preview.jpg",
                "main.zip",
                "sku.zip",
                "detail.zip",
                "complete-package.zip",
                "result.xlsx",
            },
        )
        self.assertTrue(all(path.is_file() for path in package.files.values()))

    def test_catalog_exposes_verified_complete_and_category_packages(self) -> None:
        package = self.build()

        assert package is not None
        downloads = package.catalog["downloads"]
        self.assertEqual(set(downloads), {"complete", "main", "sku", "detail"})
        for kind, filename in (
            ("complete", "complete-package.zip"),
            ("main", "main.zip"),
            ("sku", "sku.zip"),
            ("detail", "detail.zip"),
        ):
            self.assertEqual(downloads[kind], package.manifest["objects"][filename])

    def test_custom_or_partial_job_is_not_publishable(self) -> None:
        self.assertIsNone(self.build(max_main_images=5))
        self.assertIsNone(self.build(generated_records=self.records(detail=5)))
        self.assertIsNone(self.build(generation_mode="own_product"))

    def test_failed_record_is_not_publishable(self) -> None:
        records = self.records()
        records[-1]["status"] = "failed"

        self.assertIsNone(self.build(generated_records=records))

    def test_complete_package_contains_portable_manifests_and_matching_hash(self) -> None:
        package = self.build()
        assert package is not None

        complete = package.files["complete-package.zip"]
        with zipfile.ZipFile(complete) as archive:
            names = set(archive.namelist())
            source = json.loads(archive.read("source/manifest.json"))
            reuse = json.loads(archive.read("reuse-manifest.json"))

        self.assertIn("source/assets/main/001-source-main.jpg", names)
        self.assertEqual(
            source["images"][0]["path"],
            "assets/main/001-source-main.jpg",
        )
        self.assertTrue(
            all(record["output_path"] in names for record in reuse["generated_records"])
        )
        self.assertEqual(
            package.catalog["package_sha256"],
            package.manifest["objects"]["complete-package.zip"]["sha256"],
        )
        with Image.open(package.files["preview.jpg"]) as preview:
            self.assertEqual(preview.size, (1200, 1200))
        self.assertLessEqual(package.files["preview.jpg"].stat().st_size, 800 * 1024)

    def test_materialized_package_has_local_generated_and_source_paths(self) -> None:
        package = self.build()
        assert package is not None

        reused = materialize_reused_package(
            package.files["complete-package.zip"],
            self.root / "reused",
        )

        self.assertTrue(reused.source_manifest.is_file())
        self.assertEqual(reused.titles["short_title"], "测试商品")
        self.assertEqual(len(reused.generated_records), 19)
        self.assertTrue(
            all(Path(record["output_path"]).is_file() for record in reused.generated_records)
        )
        source = json.loads(reused.source_manifest.read_text(encoding="utf-8"))
        self.assertTrue(Path(source["images"][0]["path"]).is_file())

    def test_materialize_rejects_parent_directory_entries(self) -> None:
        archive_path = self.root / "malicious.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../escape.txt", "bad")
            archive.writestr("reuse-manifest.json", "{}")

        with self.assertRaisesRegex(ValueError, "不安全"):
            materialize_reused_package(archive_path, self.root / "malicious-output")

        self.assertFalse((self.root / "escape.txt").exists())

    def test_missing_collected_asset_is_not_left_as_a_broken_local_path(self) -> None:
        document = json.loads(self.source_manifest.read_text(encoding="utf-8"))
        document["images"].append(
            {
                "type": "sku",
                "path": str(self.source_root / "missing-sku.jpg"),
            }
        )
        self.source_manifest.write_text(json.dumps(document), encoding="utf-8")
        package = self.build()
        assert package is not None

        reused = materialize_reused_package(
            package.files["complete-package.zip"],
            self.root / "reused-with-missing-source",
        )

        source = json.loads(reused.source_manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(source["images"]), 1)
        self.assertTrue(Path(source["images"][0]["path"]).is_file())


if __name__ == "__main__":
    unittest.main()
