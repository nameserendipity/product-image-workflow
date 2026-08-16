import hashlib
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from product_identity import ProductIdentity
from shared_library_client import (
    SharedLibraryClient,
    SharedLibraryLeaseLost,
    SharedLibraryLockBusy,
    SharedLibraryUnavailable,
    SharedPublishBundle,
)


class FakeOssError(RuntimeError):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code


class FakeResult:
    def __init__(self, data: bytes = b"", etag: str = "") -> None:
        self._data = data
        self._position = 0
        self.etag = etag
        self.headers = {"ETag": etag, "Content-Length": str(len(data))}

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._position :]
            self._position = len(self._data)
            return chunk
        chunk = self._data[self._position : self._position + size]
        self._position += len(chunk)
        return chunk


class FakeListResult:
    def __init__(self, keys: list[str], next_cursor: str = "") -> None:
        self.object_list = [SimpleNamespace(key=key) for key in keys]
        self.next_continuation_token = next_cursor
        self.is_truncated = bool(next_cursor)


class HeaderMapping:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def get(self, key: str, default=None):
        return self.values.get(key, default)


class FakeSharedBucket:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.write_keys: list[str] = []
        self.get_ranges: list[tuple[str, tuple[int, int] | None]] = []

    def put_object(self, key: str, data: bytes, headers=None) -> FakeResult:
        body = bytes(data)
        with self._lock:
            if headers and headers.get("x-oss-forbid-overwrite") == "true" and key in self.objects:
                raise FakeOssError(409, "ObjectAlreadyExists")
            if headers and headers.get("If-Match"):
                current = self.objects.get(key)
                if current is None or current[1] != headers["If-Match"]:
                    raise FakeOssError(412, "PreconditionFailed")
            etag = hashlib.sha256(body).hexdigest()
            self.objects[key] = (body, etag)
            self.write_keys.append(key)
            return FakeResult(body, etag)

    def put_object_from_file(self, key: str, filename: str, headers=None) -> FakeResult:
        return self.put_object(key, Path(filename).read_bytes(), headers=headers)

    def get_object(self, key: str, byte_range=None) -> FakeResult:
        with self._lock:
            if key not in self.objects:
                raise FakeOssError(404, "NoSuchKey")
            body, etag = self.objects[key]
            self.get_ranges.append((key, byte_range))
        if byte_range is not None:
            start, end = byte_range
            body = body[start : end + 1]
        return FakeResult(body, etag)

    def delete_object(self, key: str, params=None, headers=None) -> FakeResult:
        del params
        with self._lock:
            current = self.objects.get(key)
            if current is None:
                raise FakeOssError(404, "NoSuchKey")
            if headers and headers.get("If-Match") != current[1]:
                raise FakeOssError(412, "PreconditionFailed")
            del self.objects[key]
        return FakeResult()

    def head_object(self, key: str, headers=None, params=None) -> FakeResult:
        del headers, params
        with self._lock:
            if key not in self.objects:
                raise FakeOssError(404, "NoSuchKey")
            body, etag = self.objects[key]
        return FakeResult(body, etag)

    def copy_object(
        self,
        source_bucket_name: str,
        source_key: str,
        target_key: str,
        headers=None,
        params=None,
    ) -> FakeResult:
        del source_bucket_name, params
        source = self.get_object(source_key).read()
        return self.put_object(target_key, source, headers=headers)

    def list_objects_v2(
        self,
        prefix="",
        delimiter="",
        continuation_token="",
        start_after="",
        fetch_owner=False,
        encoding_type="url",
        max_keys=100,
        headers=None,
    ) -> FakeListResult:
        del delimiter, start_after, fetch_owner, encoding_type, headers
        keys = sorted(key for key in self.objects if key.startswith(prefix))
        start = int(continuation_token or 0)
        page = keys[start : start + max_keys]
        next_cursor = str(start + max_keys) if start + max_keys < len(keys) else ""
        return FakeListResult(page, next_cursor)


class SharedLibraryClientLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bucket = FakeSharedBucket()
        self.config = SimpleNamespace(prefix="product-workflow", bucket="test-bucket")
        self.identity = ProductIdentity(
            platform="taobao",
            product_id="123",
            product_key="taobao-123",
            source_url="https://item.taobao.com/item.htm?id=123&spm=test",
            canonical_url="https://item.taobao.com/item.htm?id=123",
        )

    def client(self, client_id: str) -> SharedLibraryClient:
        return SharedLibraryClient(
            self.config,
            self.bucket,
            client_id,
            now=lambda: datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc),
        )

    def test_missing_product_probe_is_available_for_new_work(self) -> None:
        probe = self.client("client-one").probe(self.identity)

        self.assertEqual(probe.status, "missing")
        self.assertIsNone(probe.catalog)
        self.assertIsNone(probe.lock)

    def test_two_clients_cannot_acquire_the_same_product(self) -> None:
        first = self.client("client-one")
        second = self.client("client-two")

        lease = first.acquire_lock(self.identity, task_id="task-one")

        self.assertEqual(lease.task_id, "task-one")
        self.assertEqual(lease.client_id, "client-one")
        with self.assertRaises(SharedLibraryLockBusy):
            second.acquire_lock(self.identity, task_id="task-two")

    def test_lock_contention_is_reported_as_locked(self) -> None:
        self.client("client-one").acquire_lock(self.identity, task_id="task-one")

        probe = self.client("client-two").probe(self.identity)

        self.assertEqual(probe.status, "locked")
        self.assertEqual(probe.lock["task_id"], "task-one")
        self.assertIsNone(probe.catalog)

    def test_refresh_changes_the_lease_and_stale_lease_cannot_release_it(self) -> None:
        current_time = [datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc)]
        client = SharedLibraryClient(
            self.config,
            self.bucket,
            "client-one",
            now=lambda: current_time[0],
        )
        original = client.acquire_lock(self.identity, task_id="task-one")
        current_time[0] = datetime(2026, 8, 15, 5, 0, tzinfo=timezone.utc)

        refreshed = client.refresh_lock(original)

        self.assertNotEqual(refreshed.etag, original.etag)
        with self.assertRaises(SharedLibraryLeaseLost):
            client.release_lock(original)
        self.assertEqual(client.probe(self.identity).status, "locked")
        client.release_lock(refreshed)
        self.assertEqual(client.probe(self.identity).status, "missing")

    def test_expired_lock_is_conditionally_replaced(self) -> None:
        first = self.client("client-one")
        first.acquire_lock(self.identity, task_id="task-one")
        second = SharedLibraryClient(
            self.config,
            self.bucket,
            "client-two",
            now=lambda: datetime(2026, 8, 15, 7, 0, tzinfo=timezone.utc),
        )

        replacement = second.acquire_lock(self.identity, task_id="task-two")

        self.assertEqual(replacement.client_id, "client-two")
        self.assertEqual(second.probe(self.identity).lock["task_id"], "task-two")


class SharedLibraryClientCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bucket = FakeSharedBucket()
        self.config = SimpleNamespace(prefix="product-workflow", bucket="test-bucket")
        self.client = SharedLibraryClient(self.config, self.bucket, "client-one")
        self.identity = ProductIdentity(
            platform="taobao",
            product_id="123",
            product_key="taobao-123",
            source_url="https://item.taobao.com/item.htm?id=123",
            canonical_url="https://item.taobao.com/item.htm?id=123",
        )
        self.catalog = {
            "schema_version": 1,
            "product_key": "taobao-123",
            "platform": "taobao",
            "product_id": "123",
            "status": "completed",
            "manifest_object": "product-workflow/shared-library/packages/taobao/123/manifest.json",
            "package_object": "product-workflow/shared-library/packages/taobao/123/complete-package.zip",
            "package_size": 7,
            "package_sha256": hashlib.sha256(b"package").hexdigest(),
        }

    def put_json(self, key: str, document: dict) -> None:
        self.bucket.put_object(key, json.dumps(document).encode("utf-8"))

    def test_probe_is_available_only_when_manifest_and_package_exist(self) -> None:
        self.bucket.put_object(self.catalog["manifest_object"], b"{}")
        self.bucket.put_object(self.catalog["package_object"], b"package")
        self.put_json(
            "product-workflow/shared-library/catalog/taobao-123.json",
            self.catalog,
        )

        probe = self.client.probe(self.identity)

        self.assertEqual(probe.status, "available")
        self.assertEqual(probe.catalog["package_sha256"], self.catalog["package_sha256"])

    def test_probe_accepts_sdk_header_mapping_for_package_size(self) -> None:
        self.bucket.put_object(self.catalog["manifest_object"], b"{}")
        self.bucket.put_object(self.catalog["package_object"], b"package")
        self.put_json(
            "product-workflow/shared-library/catalog/taobao-123.json",
            self.catalog,
        )
        original_head_object = self.bucket.head_object

        def head_object(key: str) -> FakeResult:
            result = original_head_object(key)
            result.headers = HeaderMapping(result.headers)
            return result

        self.bucket.head_object = head_object

        probe = self.client.probe(self.identity)

        self.assertEqual(probe.status, "available")

    def test_catalog_with_missing_formal_package_is_corrupt(self) -> None:
        self.bucket.put_object(self.catalog["manifest_object"], b"{}")
        self.put_json(
            "product-workflow/shared-library/catalog/taobao-123.json",
            self.catalog,
        )

        probe = self.client.probe(self.identity)

        self.assertEqual(probe.status, "corrupt")
        self.assertEqual(probe.catalog["product_key"], "taobao-123")

    def test_catalog_with_non_numeric_package_size_is_corrupt(self) -> None:
        invalid = dict(self.catalog, package_size="not-a-number")
        self.put_json(
            "product-workflow/shared-library/catalog/taobao-123.json",
            invalid,
        )

        probe = self.client.probe(self.identity)

        self.assertEqual(probe.status, "corrupt")

    def test_catalog_list_is_paginated_and_returns_documents(self) -> None:
        second = dict(self.catalog, product_key="tmall-456", platform="tmall", product_id="456")
        self.put_json(
            "product-workflow/shared-library/catalog/taobao-123.json",
            self.catalog,
        )
        self.put_json(
            "product-workflow/shared-library/catalog/tmall-456.json",
            second,
        )

        first_page = self.client.list_catalog(limit=1)
        second_page = self.client.list_catalog(cursor=first_page.next_cursor, limit=1)

        self.assertEqual([item["product_key"] for item in first_page.items], ["taobao-123"])
        self.assertEqual(first_page.next_cursor, "1")
        self.assertEqual([item["product_key"] for item in second_page.items], ["tmall-456"])
        self.assertEqual(second_page.next_cursor, "")


class SharedLibraryClientPublishTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bucket = FakeSharedBucket()
        self.config = SimpleNamespace(prefix="product-workflow", bucket="test-bucket")
        self.client = SharedLibraryClient(
            self.config,
            self.bucket,
            "client-one",
            now=lambda: datetime(2026, 8, 15, 4, 0, tzinfo=timezone.utc),
        )
        self.identity = ProductIdentity(
            platform="taobao",
            product_id="123",
            product_key="taobao-123",
            source_url="https://item.taobao.com/item.htm?id=123",
            canonical_url="https://item.taobao.com/item.htm?id=123",
        )
        self.preview = self.root / "preview.jpg"
        self.package = self.root / "complete-package.zip"
        self.preview.write_bytes(b"preview")
        self.package.write_bytes(b"package")
        self.catalog = {
            "schema_version": 1,
            "product_key": "taobao-123",
            "platform": "taobao",
            "product_id": "123",
            "status": "completed",
            "manifest_object": "product-workflow/shared-library/packages/taobao/123/manifest.json",
            "package_object": "product-workflow/shared-library/packages/taobao/123/complete-package.zip",
            "package_size": len(b"package"),
            "package_sha256": hashlib.sha256(b"package").hexdigest(),
        }
        self.bundle = SharedPublishBundle(
            identity=self.identity,
            task_id="task-one",
            files={
                "preview.jpg": self.preview,
                "complete-package.zip": self.package,
            },
            manifest={"schema_version": 1, "status": "completed"},
            catalog=self.catalog,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_catalog_is_the_last_published_object(self) -> None:
        lease = self.client.acquire_lock(self.identity, task_id="task-one")

        self.client.publish(self.bundle, lease)

        self.assertEqual(
            self.bucket.write_keys[-1],
            "product-workflow/shared-library/catalog/taobao-123.json",
        )
        self.assertIn(
            "product-workflow/shared-library/packages/taobao/123/manifest.json",
            self.bucket.objects,
        )
        self.assertIn(
            "product-workflow/shared-library/packages/taobao/123/complete-package.zip",
            self.bucket.objects,
        )

    def test_stale_lease_cannot_publish_catalog(self) -> None:
        lease = self.client.acquire_lock(self.identity, task_id="task-one")
        self.bucket.put_object(
            "product-workflow/shared-library/locks/taobao-123.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "product_key": "taobao-123",
                    "task_id": "task-two",
                    "client_id": "client-two",
                    "created_at": "2026-08-15T04:30:00+00:00",
                    "expires_at": "2026-08-15T06:30:00+00:00",
                }
            ).encode("utf-8"),
        )

        with self.assertRaises(SharedLibraryLeaseLost):
            self.client.publish(self.bundle, lease)

        self.assertNotIn(
            "product-workflow/shared-library/catalog/taobao-123.json",
            self.bucket.objects,
        )

    def test_sdk_error_text_is_not_exposed(self) -> None:
        class FailingBucket(FakeSharedBucket):
            def get_object(self, key: str, byte_range=None) -> FakeResult:
                del key, byte_range
                raise RuntimeError("https://access-id:secret@example.test?Signature=secret")

        client = SharedLibraryClient(self.config, FailingBucket(), "client-one")

        with self.assertRaisesRegex(SharedLibraryUnavailable, "^共享素材库暂时不可用$"):
            client.probe(self.identity)


class SharedLibraryClientDownloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.bucket = FakeSharedBucket()
        self.client = SharedLibraryClient(
            SimpleNamespace(prefix="product-workflow", bucket="test-bucket"),
            self.bucket,
            "client-one",
        )
        self.object_key = (
            "product-workflow/shared-library/packages/taobao/123/complete-package.zip"
        )
        self.content = b"complete-package-content"
        self.bucket.put_object(self.object_key, self.content)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_download_resumes_from_existing_part_file(self) -> None:
        destination = self.root / "complete-package.zip"
        part = destination.with_suffix(destination.suffix + ".part")
        part.write_bytes(self.content[:8])

        result = self.client.download(
            self.object_key,
            len(self.content),
            hashlib.sha256(self.content).hexdigest(),
            destination,
        )

        self.assertEqual(result, destination.resolve())
        self.assertEqual(destination.read_bytes(), self.content)
        self.assertFalse(part.exists())
        self.assertIn(
            (self.object_key, (8, len(self.content) - 1)),
            self.bucket.get_ranges,
        )

    def test_matching_final_file_is_reused_without_an_oss_read(self) -> None:
        destination = self.root / "complete-package.zip"
        destination.write_bytes(self.content)
        reads_before = len(self.bucket.get_ranges)

        result = self.client.download(
            self.object_key,
            len(self.content),
            hashlib.sha256(self.content).hexdigest(),
            destination,
        )

        self.assertEqual(result, destination.resolve())
        self.assertEqual(len(self.bucket.get_ranges), reads_before)

    def test_bad_sha_does_not_create_a_formal_zip(self) -> None:
        destination = self.root / "complete-package.zip"
        part = destination.with_suffix(destination.suffix + ".part")

        with self.assertRaisesRegex(Exception, "校验失败"):
            self.client.download(
                self.object_key,
                len(self.content),
                "0" * 64,
                destination,
            )

        self.assertFalse(destination.exists())
        self.assertFalse(part.exists())

    def test_private_preview_is_read_through_the_client(self) -> None:
        preview_key = "product-workflow/shared-library/packages/taobao/123/preview.jpg"
        self.bucket.put_object(preview_key, b"preview-bytes")

        self.assertEqual(self.client.read_preview(preview_key), b"preview-bytes")


if __name__ == "__main__":
    unittest.main()
