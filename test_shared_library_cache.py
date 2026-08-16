import tempfile
import unittest
from pathlib import Path

from shared_library_cache import SharedLibraryCache


class SharedLibraryCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_client_id_is_stable_across_instances(self) -> None:
        first = SharedLibraryCache(self.root).client_id
        second = SharedLibraryCache(self.root).client_id

        self.assertEqual(first, second)
        self.assertTrue(first)

    def test_catalog_entries_are_replaced_as_one_snapshot(self) -> None:
        cache = SharedLibraryCache(self.root)
        cache.replace_catalog(
            [
                {
                    "product_key": "taobao-123",
                    "package_object": "shared/packages/taobao/123/complete-package.zip",
                    "package_sha256": "abc",
                    "etag": "etag-1",
                }
            ]
        )

        self.assertEqual(
            SharedLibraryCache(self.root).load_catalog(),
            [
                {
                    "product_key": "taobao-123",
                    "package_object": "shared/packages/taobao/123/complete-package.zip",
                    "package_sha256": "abc",
                    "etag": "etag-1",
                }
            ],
        )

    def test_download_record_requires_matching_sha_and_existing_directory(self) -> None:
        cache = SharedLibraryCache(self.root)
        local_dir = self.root / "reused" / "taobao-123"
        local_dir.mkdir(parents=True)
        cache.record_download(
            "taobao-123",
            "shared/packages/taobao/123/complete-package.zip",
            "abc",
            local_dir,
        )

        self.assertEqual(cache.find_download("taobao-123", "abc"), local_dir.resolve())
        self.assertIsNone(cache.find_download("taobao-123", "different"))
        local_dir.rmdir()
        self.assertIsNone(cache.find_download("taobao-123", "abc"))


if __name__ == "__main__":
    unittest.main()
