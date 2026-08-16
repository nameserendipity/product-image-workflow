from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal

from product_identity import ProductIdentity


class SharedLibraryUnavailable(RuntimeError):
    pass


class SharedMaterialCorrupt(RuntimeError):
    pass


class SharedLibraryLeaseLost(RuntimeError):
    pass


class SharedLibraryLockBusy(RuntimeError):
    def __init__(self, lock: dict[str, Any] | None = None) -> None:
        super().__init__("其他用户正在生成")
        self.lock = lock


@dataclass(frozen=True, slots=True)
class SharedProbe:
    status: Literal["missing", "available", "locked", "corrupt"]
    catalog: dict[str, Any] | None = None
    lock: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class LockLease:
    product_key: str
    task_id: str
    client_id: str
    etag: str
    created_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class CatalogPage:
    items: tuple[dict[str, Any], ...]
    next_cursor: str


@dataclass(frozen=True, slots=True)
class SharedPublishBundle:
    identity: ProductIdentity
    task_id: str
    files: dict[str, Path]
    manifest: dict[str, Any]
    catalog: dict[str, Any]


class SharedLibraryClient:
    LOCK_TTL = timedelta(hours=2)

    def __init__(
        self,
        config: Any,
        bucket: Any,
        client_id: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self.bucket = bucket
        self.client_id = client_id.strip()
        if not self.client_id:
            raise ValueError("共享素材库客户端标识不能为空")
        prefix = str(getattr(config, "prefix", "") or "").strip().strip("/")
        self.root_prefix = "/".join(value for value in (prefix, "shared-library") if value)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def probe(self, identity: ProductIdentity) -> SharedProbe:
        catalog, _ = self._read_json_optional(self._catalog_key(identity.product_key))
        if catalog is not None:
            return SharedProbe(
                status="available" if self._catalog_is_readable(catalog) else "corrupt",
                catalog=catalog,
            )
        lock, _ = self._read_json_optional(self._lock_key(identity.product_key))
        if lock is not None and not self._lock_expired(lock):
            return SharedProbe(status="locked", lock=lock)
        return SharedProbe(status="missing")

    def list_catalog(self, cursor: str = "", limit: int = 50) -> CatalogPage:
        if not 1 <= limit <= 100:
            raise ValueError("共享素材库分页大小必须在 1 到 100 之间")
        try:
            result = self.bucket.list_objects_v2(
                prefix=self._key("catalog", ""),
                continuation_token=cursor,
                max_keys=limit,
            )
        except Exception:
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        items: list[dict[str, Any]] = []
        for item in getattr(result, "object_list", ()):
            key = str(getattr(item, "key", "") or "")
            if not key:
                continue
            try:
                document, _ = self._read_json_optional(key)
            except SharedMaterialCorrupt:
                continue
            if document is not None:
                items.append(document)
        return CatalogPage(
            items=tuple(items),
            next_cursor=str(getattr(result, "next_continuation_token", "") or ""),
        )

    def acquire_lock(self, identity: ProductIdentity, task_id: str) -> LockLease:
        created_at = self._aware_now()
        document = {
            "schema_version": 1,
            "product_key": identity.product_key,
            "task_id": task_id,
            "client_id": self.client_id,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + self.LOCK_TTL).isoformat(),
        }
        key = self._lock_key(identity.product_key)
        try:
            result = self._create_lock_object(key, document)
        except Exception as error:
            if _status_code(error) in {409, 412}:
                lock, etag = self._read_json_optional(key)
                if lock is None or not self._lock_expired(lock):
                    raise SharedLibraryLockBusy(lock) from None
                try:
                    self.bucket.delete_object(key, headers={"If-Match": etag})
                    result = self._create_lock_object(key, document)
                except Exception as takeover_error:
                    if _status_code(takeover_error) in {409, 412}:
                        current, _ = self._read_json_optional(key)
                        raise SharedLibraryLockBusy(current) from None
                    raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
            else:
                raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        return self._lease_from_document(document, result)

    def refresh_lock(self, lease: LockLease) -> LockLease:
        document, etag = self._read_json_optional(self._lock_key(lease.product_key))
        if document is None or not self._lease_matches(lease, document, etag):
            raise SharedLibraryLeaseLost("共享任务锁已失效")
        refreshed = dict(document)
        refreshed["expires_at"] = (self._aware_now() + self.LOCK_TTL).isoformat()
        try:
            result = self.bucket.put_object(
                self._lock_key(lease.product_key),
                json.dumps(refreshed, ensure_ascii=False).encode("utf-8"),
                headers={"Content-Type": "application/json", "If-Match": lease.etag},
            )
        except Exception as error:
            if _status_code(error) in {404, 409, 412}:
                raise SharedLibraryLeaseLost("共享任务锁已失效") from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        return self._lease_from_document(refreshed, result)

    def release_lock(self, lease: LockLease) -> None:
        document, etag = self._read_json_optional(self._lock_key(lease.product_key))
        if document is None:
            return
        if not self._lease_matches(lease, document, etag):
            raise SharedLibraryLeaseLost("共享任务锁已失效")
        try:
            self.bucket.delete_object(
                self._lock_key(lease.product_key),
                headers={"If-Match": lease.etag},
            )
        except Exception as error:
            if _status_code(error) in {404, 409, 412}:
                raise SharedLibraryLeaseLost("共享任务锁已失效") from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None

    def publish(self, bundle: SharedPublishBundle, lease: LockLease) -> dict[str, Any]:
        if (
            bundle.identity.product_key != lease.product_key
            or bundle.task_id != lease.task_id
            or str(bundle.catalog.get("product_key") or "") != lease.product_key
        ):
            raise SharedLibraryLeaseLost("共享任务与锁不匹配")
        self._require_current_lease(lease)

        staged_keys: list[str] = []
        staging_root = self._key("staging", bundle.task_id)
        formal_root = self._key(
            "packages",
            f"{bundle.identity.platform}/{bundle.identity.product_id}",
        )
        try:
            for name, source in bundle.files.items():
                cleaned_name = _object_filename(name)
                path = Path(source).resolve()
                if not path.is_file():
                    raise ValueError(f"共享素材文件不存在：{path}")
                staged_key = f"{staging_root}/{cleaned_name}"
                self.bucket.put_object_from_file(staged_key, str(path))
                staged_keys.append(staged_key)

            staged_manifest = f"{staging_root}/manifest.json"
            staged_catalog = f"{staging_root}/catalog.json"
            self.bucket.put_object(
                staged_manifest,
                _json_bytes(bundle.manifest),
                headers={"Content-Type": "application/json"},
            )
            staged_keys.append(staged_manifest)
            self.bucket.put_object(
                staged_catalog,
                _json_bytes(bundle.catalog),
                headers={"Content-Type": "application/json"},
            )
            staged_keys.append(staged_catalog)

            for name in bundle.files:
                cleaned_name = _object_filename(name)
                self._copy_without_overwrite(
                    f"{staging_root}/{cleaned_name}",
                    f"{formal_root}/{cleaned_name}",
                )
            self._copy_without_overwrite(staged_manifest, f"{formal_root}/manifest.json")

            self._require_current_lease(lease)
            self.bucket.put_object(
                self._catalog_key(lease.product_key),
                _json_bytes(bundle.catalog),
                headers={
                    "Content-Type": "application/json",
                    "x-oss-forbid-overwrite": "true",
                },
            )
        except (SharedLibraryLeaseLost, ValueError):
            raise
        except Exception as error:
            if _status_code(error) in {409, 412}:
                raise SharedMaterialCorrupt("共享素材已存在，禁止覆盖") from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        finally:
            for key in staged_keys:
                try:
                    self.bucket.delete_object(key)
                except Exception:
                    pass
        return dict(bundle.catalog)

    def download(
        self,
        object_key: str,
        expected_size: int,
        expected_sha256: str,
        destination: Path,
    ) -> Path:
        self._require_package_object(object_key)
        if expected_size <= 0 or len(expected_sha256) != 64:
            raise SharedMaterialCorrupt("共享素材包校验信息无效")
        target = destination.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_suffix(target.suffix + ".part")
        if target.is_file():
            if target.stat().st_size == expected_size and _sha256_file(target) == expected_sha256:
                return target
            target.unlink()
        if part.is_file() and part.stat().st_size > expected_size:
            part.unlink()
        offset = part.stat().st_size if part.is_file() else 0
        if offset < expected_size:
            try:
                result = self.bucket.get_object(
                    object_key,
                    byte_range=(offset, expected_size - 1),
                )
                with part.open("ab") as stream:
                    while chunk := result.read(1024 * 1024):
                        stream.write(chunk)
            except Exception:
                raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        if part.stat().st_size != expected_size or _sha256_file(part) != expected_sha256:
            part.unlink(missing_ok=True)
            raise SharedMaterialCorrupt("共享素材包校验失败")
        part.replace(target)
        return target

    def read_preview(self, object_key: str) -> bytes:
        self._require_package_object(object_key)
        try:
            return bytes(self.bucket.get_object(object_key).read())
        except Exception as error:
            if _status_code(error) == 404:
                raise SharedMaterialCorrupt("共享素材预览不存在") from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None

    def _copy_without_overwrite(self, source_key: str, target_key: str) -> None:
        self.bucket.copy_object(
            str(getattr(self.config, "bucket", "")),
            source_key,
            target_key,
            headers={"x-oss-forbid-overwrite": "true"},
        )

    def _require_package_object(self, object_key: str) -> None:
        normalized = object_key.strip().replace("\\", "/")
        package_prefix = self._key("packages", "")
        if not normalized.startswith(package_prefix) or ".." in normalized.split("/"):
            raise ValueError("共享素材对象路径无效")

    def _require_current_lease(self, lease: LockLease) -> None:
        document, etag = self._read_json_optional(self._lock_key(lease.product_key))
        if document is None or not self._lease_matches(lease, document, etag):
            raise SharedLibraryLeaseLost("共享任务锁已失效")

    def _create_lock_object(self, key: str, document: dict[str, Any]) -> Any:
        return self.bucket.put_object(
            key,
            json.dumps(document, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-oss-forbid-overwrite": "true",
            },
        )

    @staticmethod
    def _lease_from_document(document: dict[str, Any], result: Any) -> LockLease:
        return LockLease(
            product_key=str(document["product_key"]),
            task_id=str(document["task_id"]),
            client_id=str(document["client_id"]),
            etag=_result_etag(result),
            created_at=str(document["created_at"]),
            expires_at=str(document["expires_at"]),
        )

    @staticmethod
    def _lease_matches(lease: LockLease, document: dict[str, Any], etag: str) -> bool:
        return (
            str(document.get("product_key") or "") == lease.product_key
            and str(document.get("task_id") or "") == lease.task_id
            and str(document.get("client_id") or "") == lease.client_id
            and etag == lease.etag
        )

    def _read_json_optional(self, key: str) -> tuple[dict[str, Any] | None, str]:
        try:
            result = self.bucket.get_object(key)
            document = json.loads(result.read().decode("utf-8"))
        except Exception as error:
            if _status_code(error) == 404:
                return None, ""
            if isinstance(error, (json.JSONDecodeError, UnicodeDecodeError, AttributeError)):
                raise SharedMaterialCorrupt("共享素材元数据损坏") from None
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        if not isinstance(document, dict):
            raise SharedMaterialCorrupt("共享素材元数据损坏")
        return document, _result_etag(result)

    def _lock_expired(self, document: dict[str, Any]) -> bool:
        try:
            expires_at = datetime.fromisoformat(str(document["expires_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= self._aware_now()

    def _catalog_is_readable(self, catalog: dict[str, Any]) -> bool:
        try:
            package_size = int(catalog.get("package_size") or 0)
        except (TypeError, ValueError):
            return False
        if (
            catalog.get("status") != "completed"
            or not str(catalog.get("manifest_object") or "")
            or not str(catalog.get("package_object") or "")
            or not str(catalog.get("package_sha256") or "")
            or package_size <= 0
        ):
            return False
        try:
            self.bucket.head_object(str(catalog["manifest_object"]))
            package = self.bucket.head_object(str(catalog["package_object"]))
        except Exception as error:
            if _status_code(error) == 404:
                return False
            raise SharedLibraryUnavailable("共享素材库暂时不可用") from None
        return _content_length(package) == package_size

    def _aware_now(self) -> datetime:
        value = self._now()
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    def _key(self, category: str, name: str) -> str:
        return f"{self.root_prefix}/{category.strip('/')}/{name.strip('/')}"

    def _catalog_key(self, product_key: str) -> str:
        return self._key("catalog", f"{product_key}.json")

    def _lock_key(self, product_key: str) -> str:
        return self._key("locks", f"{product_key}.json")


def _status_code(error: Exception) -> int:
    try:
        return int(getattr(error, "status", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _result_etag(result: Any) -> str:
    direct = str(getattr(result, "etag", "") or "").strip().strip('"')
    if direct:
        return direct
    headers = getattr(result, "headers", {})
    return str(headers.get("ETag", "") or "").strip().strip('"') if isinstance(headers, dict) else ""


def _content_length(result: Any) -> int:
    headers = getattr(result, "headers", {})
    try:
        return int(headers.get("Content-Length", 0) or 0)
    except (AttributeError, TypeError, ValueError):
        return 0


def _object_filename(value: str) -> str:
    name = value.strip().replace("\\", "/")
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("共享素材对象名称无效")
    return name


def _json_bytes(document: dict[str, Any]) -> bytes:
    return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
