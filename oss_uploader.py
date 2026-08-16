from __future__ import annotations

import hashlib
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import oss2


ACCESS_KEY_ID_ENV = "PRODUCT_WORKFLOW_OSS_ACCESS_KEY_ID"
ACCESS_KEY_SECRET_ENV = "PRODUCT_WORKFLOW_OSS_ACCESS_KEY_SECRET"


class OssConfigurationError(RuntimeError):
    pass


class OssUploadError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OssConfig:
    endpoint: str
    bucket: str
    prefix: str
    public_base_url: str

    @classmethod
    def from_document(cls, document: dict[str, Any]) -> "OssConfig | None":
        value = document.get("oss")
        if value is None:
            return None
        if not isinstance(value, dict):
            raise OssConfigurationError("local_settings.json 的 oss 配置必须是对象。")
        endpoint = str(value.get("endpoint") or "").strip().rstrip("/")
        bucket = str(value.get("bucket") or "").strip()
        prefix = str(value.get("prefix") or "product-workflow").strip().strip("/")
        if not endpoint.startswith("https://") or not bucket:
            raise OssConfigurationError("OSS 配置需要 https endpoint 和 bucket。")
        host = urlparse(endpoint).netloc
        if not host:
            raise OssConfigurationError("OSS endpoint 无效。")
        public_base_url = str(value.get("public_base_url") or f"https://{bucket}.{host}").strip().rstrip("/")
        if not public_base_url.startswith("https://"):
            raise OssConfigurationError("OSS public_base_url 必须是 https 地址。")
        return cls(endpoint=endpoint, bucket=bucket, prefix=prefix, public_base_url=public_base_url)


class OssUploader:
    def __init__(self, config: OssConfig, bucket: Any) -> None:
        self.config = config
        self.bucket = bucket

    @classmethod
    def from_settings_file(cls, settings_path: Path) -> "OssUploader | None":
        try:
            document = json.loads(settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError as error:
            raise OssConfigurationError("未找到 local_settings.json。") from error
        except json.JSONDecodeError as error:
            raise OssConfigurationError("local_settings.json 不是有效 JSON。") from error
        if not isinstance(document, dict):
            raise OssConfigurationError("local_settings.json 根节点必须是对象。")
        config = OssConfig.from_document(document)
        oss_document = document.get("oss") if isinstance(document.get("oss"), dict) else {}
        access_key_id = (
            os.getenv(ACCESS_KEY_ID_ENV, "").strip()
            or str(oss_document.get("access_key_id") or "").strip()
        )
        access_key_secret = (
            os.getenv(ACCESS_KEY_SECRET_ENV, "").strip()
            or str(oss_document.get("access_key_secret") or "").strip()
        )
        if config is None or not access_key_id or not access_key_secret:
            return None
        auth = oss2.Auth(access_key_id, access_key_secret)
        return cls(config, oss2.Bucket(auth, config.endpoint, config.bucket))

    def upload_file(self, path: Path, namespace: str) -> str:
        source = path.resolve()
        if not source.is_file():
            raise OssUploadError(f"待上传文件不存在：{source}")
        cleaned_namespace = namespace.strip().strip("/")
        if not cleaned_namespace:
            raise OssUploadError("OSS 上传目录不能为空。")
        digest = _sha256(source)[:16]
        object_key = "/".join(
            value
            for value in (self.config.prefix, cleaned_namespace, f"{digest}-{source.name}")
            if value
        )
        headers: dict[str, str] = {}
        content_type, _ = mimetypes.guess_type(source.name)
        if content_type:
            headers["Content-Type"] = content_type
        try:
            self.bucket.put_object_from_file(object_key, str(source), headers=headers or None)
        except Exception as error:
            raise OssUploadError(f"上传 OSS 失败：{error}") from error
        return f"{self.config.public_base_url}/{quote(object_key, safe='/')}"


def upload_generation_records(
    records: list[dict[str, Any]], uploader: OssUploader | None
) -> list[dict[str, Any]]:
    updated: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        if uploader is None or item.get("status") != "completed":
            updated.append(item)
            continue
        output_path = Path(str(item.get("output_path") or ""))
        if not output_path.is_file():
            updated.append(item)
            continue
        category = str(item.get("category") or "generated").strip() or "generated"
        try:
            item["output_public_url"] = uploader.upload_file(output_path, f"generated/{category}")
        except OssUploadError as error:
            item["oss_upload_error"] = str(error)
        updated.append(item)
    return updated


def upload_video_if_needed(
    manifest: dict[str, Any],
    uploader: OssUploader | None,
    namespace: str,
) -> dict[str, Any]:
    updated = dict(manifest)
    original_url = str(updated.get("main_video_url") or "").strip()
    if original_url.startswith(("http://", "https://")):
        updated["main_video_status"] = "complete"
        updated["main_video_error"] = ""
        return updated

    local_value = str(updated.get("main_video_local_path") or "").strip()
    if not local_value:
        return updated
    local_path = Path(local_value)
    if not local_path.is_file():
        updated["main_video_status"] = "failed"
        updated["main_video_error"] = f"本地主视频不存在：{local_path}"
        return updated
    if uploader is None:
        updated["main_video_status"] = "local_only"
        updated["main_video_error"] = "OSS 未配置，主视频仅保存在本地"
        return updated
    try:
        public_url = uploader.upload_file(local_path, f"videos/{namespace.strip().strip('/')}")
    except OssUploadError as error:
        updated["main_video_status"] = "failed"
        updated["main_video_error"] = str(error)
        return updated
    updated["main_video_url"] = public_url
    updated["main_video_public_url"] = public_url
    updated["main_video_status"] = "complete"
    updated["main_video_error"] = ""
    return updated


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
