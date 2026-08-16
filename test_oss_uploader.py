import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from oss_uploader import (
    ACCESS_KEY_ID_ENV,
    ACCESS_KEY_SECRET_ENV,
    OssConfig,
    OssUploader,
    upload_generation_records,
    upload_video_if_needed,
)


class FakeBucket:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None]] = []

    def put_object_from_file(self, key: str, path: str, headers=None) -> None:
        self.calls.append((key, path, headers))


class OssUploaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = self.root / "local_settings.json"
        self.settings.write_text(
            json.dumps(
                {
                    "oss": {
                        "endpoint": "https://oss-cn-shenzhen.aliyuncs.com",
                        "bucket": "transform-image",
                        "prefix": "product-workflow",
                    }
                }
            ),
            encoding="utf-8",
        )
        self.image = self.root / "result image.png"
        self.image.write_bytes(b"generated-image")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_returns_none_without_access_key_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(OssUploader.from_settings_file(self.settings))

    def test_upload_returns_public_url_and_encodes_filename(self) -> None:
        bucket = FakeBucket()
        config = OssConfig.from_document(json.loads(self.settings.read_text(encoding="utf-8")))
        assert config is not None
        uploader = OssUploader(config, bucket)

        public_url = uploader.upload_file(self.image, "generated/main")

        self.assertEqual(len(bucket.calls), 1)
        self.assertTrue(bucket.calls[0][0].startswith("product-workflow/generated/main/"))
        self.assertIn("result%20image.png", public_url)
        self.assertTrue(public_url.startswith("https://transform-image.oss-cn-shenzhen.aliyuncs.com/"))

    def test_generation_uploads_only_completed_existing_outputs(self) -> None:
        bucket = FakeBucket()
        config = OssConfig.from_document(json.loads(self.settings.read_text(encoding="utf-8")))
        assert config is not None
        records = upload_generation_records(
            [
                {"category": "main", "status": "completed", "output_path": str(self.image)},
                {"category": "detail", "status": "failed", "output_path": str(self.image)},
            ],
            OssUploader(config, bucket),
        )

        self.assertEqual(len(bucket.calls), 1)
        self.assertIn("output_public_url", records[0])
        self.assertNotIn("output_public_url", records[1])

    def test_local_video_is_uploaded_when_original_public_url_is_missing(self) -> None:
        video = self.root / "source video.mp4"
        video.write_bytes(b"video")
        bucket = FakeBucket()
        config = OssConfig.from_document(json.loads(self.settings.read_text(encoding="utf-8")))
        assert config is not None

        result = upload_video_if_needed(
            {
                "main_video_url": "",
                "main_video_local_path": str(video),
                "main_video_status": "local_only",
            },
            OssUploader(config, bucket),
            "item-1",
        )

        self.assertEqual(result["main_video_status"], "complete")
        self.assertTrue(result["main_video_url"].startswith("https://"))
        self.assertEqual(len(bucket.calls), 1)
        self.assertEqual(bucket.calls[0][2]["Content-Type"], "video/mp4")

    def test_original_video_url_is_preserved_without_upload(self) -> None:
        bucket = FakeBucket()
        config = OssConfig.from_document(json.loads(self.settings.read_text(encoding="utf-8")))
        assert config is not None

        result = upload_video_if_needed(
            {"main_video_url": "https://video.example/original.mp4", "main_video_status": "complete"},
            OssUploader(config, bucket),
            "item-1",
        )

        self.assertEqual(result["main_video_url"], "https://video.example/original.mp4")
        self.assertEqual(bucket.calls, [])

    def test_builds_uploader_without_exposing_environment_values(self) -> None:
        bucket = FakeBucket()
        with (
            patch.dict(
                os.environ,
                {ACCESS_KEY_ID_ENV: "test-id", ACCESS_KEY_SECRET_ENV: "test-secret"},
                clear=True,
            ),
            patch("oss_uploader.oss2.Auth"),
            patch("oss_uploader.oss2.Bucket", return_value=bucket),
        ):
            uploader = OssUploader.from_settings_file(self.settings)

        self.assertIsNotNone(uploader)
        assert uploader is not None
        self.assertIs(uploader.bucket, bucket)

    def test_builds_uploader_from_local_oss_credentials(self) -> None:
        document = json.loads(self.settings.read_text(encoding="utf-8"))
        document["oss"].update({"access_key_id": "local-id", "access_key_secret": "local-secret"})
        self.settings.write_text(json.dumps(document), encoding="utf-8")
        bucket = FakeBucket()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("oss_uploader.oss2.Auth"),
            patch("oss_uploader.oss2.Bucket", return_value=bucket),
        ):
            uploader = OssUploader.from_settings_file(self.settings)

        self.assertIsNotNone(uploader)
        assert uploader is not None
        self.assertIs(uploader.bucket, bucket)


if __name__ == "__main__":
    unittest.main()
