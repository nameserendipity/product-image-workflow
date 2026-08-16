from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class SharedLibraryCache:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._client_path = self.root / "client.json"
        self._catalog_path = self.root / "catalog.json"
        self._downloads_path = self.root / "downloads.json"
        self._lock = threading.RLock()
        self.client_id = self._load_or_create_client_id()

    def load_catalog(self) -> list[dict[str, Any]]:
        with self._lock:
            document = self._read_json(self._catalog_path)
        entries = document.get("entries", [])
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def replace_catalog(self, entries: list[dict[str, Any]]) -> None:
        document = {
            "schema_version": 1,
            "updated_at": _now_iso(),
            "entries": [dict(entry) for entry in entries],
        }
        with self._lock:
            self._write_json(self._catalog_path, document)

    def record_download(
        self,
        product_key: str,
        package_object: str,
        package_sha256: str,
        local_directory: Path,
    ) -> None:
        with self._lock:
            document = self._read_json(self._downloads_path)
            downloads = document.get("downloads")
            if not isinstance(downloads, dict):
                downloads = {}
            downloads[product_key] = {
                "package_object": package_object,
                "package_sha256": package_sha256,
                "local_directory": str(local_directory.resolve()),
                "downloaded_at": _now_iso(),
            }
            self._write_json(
                self._downloads_path,
                {"schema_version": 1, "downloads": downloads},
            )

    def find_download(self, product_key: str, package_sha256: str) -> Path | None:
        with self._lock:
            document = self._read_json(self._downloads_path)
        downloads = document.get("downloads")
        if not isinstance(downloads, dict):
            return None
        entry = downloads.get(product_key)
        if not isinstance(entry, dict):
            return None
        if str(entry.get("package_sha256") or "") != package_sha256:
            return None
        local_directory = Path(str(entry.get("local_directory") or ""))
        return local_directory.resolve() if local_directory.is_dir() else None

    def _load_or_create_client_id(self) -> str:
        with self._lock:
            document = self._read_json(self._client_path)
            client_id = str(document.get("client_id") or "").strip()
            if client_id:
                return client_id
            client_id = str(uuid.uuid4())
            self._write_json(
                self._client_path,
                {"schema_version": 1, "client_id": client_id},
            )
            return client_id

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}

    @staticmethod
    def _write_json(path: Path, document: Any) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
