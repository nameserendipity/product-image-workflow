import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch
from zipfile import BadZipFile

import web_app


class GenerationErrorMessageTests(unittest.TestCase):
    def test_generation_errors_name_active_vision_model(self):
        handler = web_app.RequestHandler

        unauthorized = handler._friendly_generation_error("HTTP 401: invalid token")
        unavailable = handler._friendly_generation_error(
            "model_not_found: gpt-5.5 is unavailable"
        )

        self.assertIn("gpt-5.5", unauthorized)
        self.assertIn("gpt-5.5", unavailable)


class VisionTimingEventTests(unittest.TestCase):
    def test_vision_timing_event_does_not_change_workflow_progress(self):
        state = web_app.AppState()
        state.events = [
            {"category": "main", "ordinal": 1, "status": "vision_timing"},
        ]

        progress = state._workflow_progress()

        self.assertEqual(progress["main"], {
            "analyzing": 0,
            "prompt_ready": 0,
            "generating": 0,
            "completed": 0,
            "failed": 0,
        })

    def test_slow_vision_timing_event_is_logged_once(self):
        handler = object.__new__(web_app.RequestHandler)

        with patch.object(web_app.STATE, "log") as log:
            handler._on_batch_event({
                "status": "vision_timing",
                "request_kind": "analysis",
                "queue_seconds": 6.25,
                "request_seconds": 42.5,
                "attempt": 1,
                "success": True,
            })

        log.assert_called_once()
        self.assertIn("视觉分析", log.call_args.args[0])
        self.assertIn("42.5", log.call_args.args[0])
from agent_flow import AgentSession
from batch_workflow import DirectLinkBatchItem, DirectReplaceBatchItem, save_batch_results
from shared_library_client import CatalogPage, LockLease, SharedLibraryUnavailable, SharedProbe


class AppStateTaskIsolationTests(unittest.TestCase):
    def test_shared_library_list_filters_items_and_returns_private_preview_url(self):
        client = Mock()
        client.list_catalog.return_value = CatalogPage(
            (
                {
                    "product_key": "taobao-123",
                    "platform": "taobao",
                    "product_id": "123",
                    "preview_object": "private/preview.jpg",
                    "package_object": "private/complete-package.zip",
                    "package_sha256": "a" * 64,
                    "package_size": 1024,
                    "main_count": 10,
                    "sku_count": 3,
                    "detail_count": 6,
                    "created_at": "2026-08-15T10:00:00+00:00",
                },
                {
                    "product_key": "tmall-456",
                    "platform": "tmall",
                    "product_id": "456",
                },
            ),
            "next-page",
        )
        cache = Mock()
        cache.find_download.return_value = None
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/shared-library?platform=taobao&query=123"
        handler._json = Mock()

        with (
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_shared_library_cache", return_value=cache),
        ):
            handler.do_GET()

        response = handler._json.call_args.args[0]
        self.assertEqual([item["product_key"] for item in response["items"]], ["taobao-123"])
        self.assertEqual(
            response["items"][0]["preview_url"],
            "/api/shared-library/preview?product_key=taobao-123",
        )
        self.assertEqual(response["next_cursor"], "next-page")
        serialized = json.dumps(response).lower()
        self.assertNotIn("preview_object", serialized)
        self.assertNotIn("package_object", serialized)
        self.assertNotIn("signature", serialized)

    def test_shared_library_open_folder_rejects_paths_outside_reused_root(self):
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"product_key": "../../Windows"})
        handler._json = Mock()

        handler._open_shared_library_folder()

        self.assertEqual(handler._json.call_args.args[1], web_app.HTTPStatus.BAD_REQUEST)

    def test_shared_library_preview_is_streamed_through_local_proxy(self):
        catalog = {
            "product_key": "taobao-123",
            "preview_object": "private/preview.jpg",
        }
        client = Mock()
        client.read_preview.return_value = b"jpeg-preview"
        cache = Mock()
        cache.load_catalog.return_value = [catalog]
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/shared-library/preview?product_key=taobao-123"
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = BytesIO()

        with (
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_shared_library_cache", return_value=cache),
        ):
            handler.do_GET()

        self.assertEqual(handler.wfile.getvalue(), b"jpeg-preview")
        handler.send_response.assert_called_once_with(web_app.HTTPStatus.OK)
        handler.send_header.assert_any_call("Cache-Control", "private, max-age=300")
        client.read_preview.assert_called_once_with("private/preview.jpg")

    def test_shared_library_complete_reuse_downloads_materializes_and_records(self):
        output_root = self.root / "outputs"
        package_zip = self.root / "complete-package.zip"
        package_zip.write_bytes(b"package")
        materialized = output_root / "reused" / "taobao-123" / "materialized"
        materialized.mkdir(parents=True)
        catalog = {
            "product_key": "taobao-123",
            "package_object": "private/complete-package.zip",
            "package_sha256": "a" * 64,
            "package_size": 7,
            "downloads": {
                "complete": {
                    "object": "private/complete-package.zip",
                    "sha256": "a" * 64,
                    "size": 7,
                }
            },
        }
        client = Mock()
        client.download.return_value = package_zip
        cache = Mock()
        cache.load_catalog.return_value = [catalog]
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"product_key": "taobao-123", "package_kind": "complete"})
        handler._json = Mock()

        with (
            patch.object(web_app, "OUTPUT_ROOT", output_root),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_shared_library_cache", return_value=cache),
            patch.object(web_app, "materialize_reused_package", return_value=Mock(root=materialized)),
        ):
            handler._reuse_shared_library_item()

        client.download.assert_called_once()
        cache.record_download.assert_called_once_with(
            "taobao-123",
            "private/complete-package.zip",
            "a" * 64,
            materialized,
        )
        self.assertEqual(handler._json.call_args.args[0]["local_directory"], str(materialized))

    def test_shared_hit_prevents_single_collection(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        client = Mock()
        client.probe.return_value = SharedProbe(
            "available",
            {"product_key": "taobao-123", "package_object": "packages/complete.zip"},
            None,
        )
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            error = handler._begin_collection()

        self.assertIn("共享素材", error)
        self.assertEqual(state.shared_library["status"], "available")
        self.assertEqual(state.shared_library["product_key"], "taobao-123")
        self.assertFalse(state.collecting)
        start.assert_not_called()

    def test_collection_dispatch_uses_collection_scope_not_generation_scope(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "detail"),
            generation_mode="own_product",
        )
        state.agent.collection_types = ("main", "sku", "detail")
        handler = object.__new__(web_app.RequestHandler)
        thread = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading, "Thread", return_value=thread) as thread_factory,
        ):
            error = handler._begin_collection()

        self.assertIsNone(error)
        thread_factory.assert_called_once()
        self.assertEqual(thread_factory.call_args.kwargs["args"][2], ("main", "sku", "detail"))
        thread.start.assert_called_once()

    def test_oss_unavailable_marks_local_fallback_and_starts_collection(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=456",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        client = Mock()
        client.probe.side_effect = SharedLibraryUnavailable("共享素材库暂时不可用")
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            error = handler._begin_collection()

        self.assertIsNone(error)
        self.assertEqual(state.shared_library["status"], "local_fallback")
        self.assertTrue(state.collecting)
        start.assert_called_once()

    def test_shared_lock_blocks_duplicate_single_collection(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        client = Mock()
        client.probe.return_value = SharedProbe(
            "locked",
            None,
            {"product_key": "taobao-123", "expires_at": "2026-08-15T12:00:00+00:00"},
        )
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            error = handler._begin_collection()

        self.assertIn("其他用户", error)
        self.assertEqual(state.shared_library["status"], "locked")
        self.assertFalse(state.collecting)
        client.acquire_lock.assert_not_called()
        start.assert_not_called()

    def test_shared_miss_acquires_lease_before_single_collection(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        lease = LockLease(
            product_key="taobao-123",
            task_id="task-1",
            client_id="client-1",
            etag="etag-1",
            created_at="2026-08-15T10:00:00+00:00",
            expires_at="2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        client.probe.return_value = SharedProbe("missing", None, None)
        client.acquire_lock.return_value = lease
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            error = handler._begin_collection()

        self.assertIsNone(error)
        self.assertEqual(state.shared_library["status"], "generating")
        self.assertEqual(state.shared_lease, lease)
        self.assertIs(state.shared_client, client)
        self.assertTrue(state.collecting)
        client.acquire_lock.assert_called_once()
        self.assertGreaterEqual(start.call_count, 1)

    def test_existing_shared_lease_is_reused_for_supplemental_collection(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        identity = web_app.resolve_shared_identity(state.agent.reference_url)
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        state.shared_identity = identity
        state.shared_lease = lease
        state.shared_client = Mock()
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client") as load_client,
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            error = handler._begin_collection(("detail",))

        self.assertIsNone(error)
        self.assertTrue(state.collecting)
        load_client.assert_not_called()
        start.assert_called_once()

    def test_non_shared_or_own_product_single_jobs_skip_shared_client(self):
        handler = object.__new__(web_app.RequestHandler)
        for reference_url, generation_mode in (
            ("https://item.jd.com/123.html", "competitor_reference"),
            ("https://item.taobao.com/item.htm?id=123", "own_product"),
        ):
            with self.subTest(reference_url=reference_url, generation_mode=generation_mode):
                state = web_app.AppState()
                state.agent = AgentSession(
                    reference_url=reference_url,
                    awaiting="",
                    quantity_confirmed=True,
                    workflows=("main", "sku", "detail"),
                    generation_mode=generation_mode,
                )
                with (
                    patch.object(web_app, "STATE", state),
                    patch.object(web_app, "load_shared_library_client") as load_client,
                    patch.object(web_app, "load_browser_choice", return_value="edge"),
                    patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
                    patch.object(web_app.threading.Thread, "start"),
                ):
                    error = handler._begin_collection()

                self.assertIsNone(error)
                self.assertTrue(state.collecting)
                load_client.assert_not_called()

    def test_shared_generation_publishes_after_workbook_export_and_releases_lease(self):
        state = web_app.AppState()
        root = self.root / "single-shared"
        source = root / "collected" / "main.jpg"
        manifest = root / "collected" / "manifest.json"
        output = root / "generated"
        workbook = root / "collected" / "result.xlsx"
        source.parent.mkdir(parents=True)
        output.mkdir(parents=True)
        source.write_bytes(b"source")
        workbook.write_bytes(b"workbook")
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(source)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.generated_output = output
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            manifest_loaded=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock(root_prefix="product-workflow/shared-library", client_id="client-1")
        client.publish.return_value = {"product_key": "taobao-123"}
        state.shared_client = client
        state.shared_lease = lease
        state.shared_identity = web_app.resolve_shared_identity(state.agent.reference_url)
        state.shared_publish_allowed = True
        state.shared_heartbeat_stop = __import__("threading").Event()
        state.shared_library = {"status": "generating", "product_key": "taobao-123"}
        record = {
            "category": "main",
            "ordinal": 1,
            "status": "completed",
            "source_path": str(source),
            "output_path": str(source),
        }
        package = Mock()
        bundle = Mock()
        package.to_publish_bundle.return_value = bundle
        builder = Mock()
        builder.build.return_value = package
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "WorkflowRunner") as runner_class,
            patch.object(web_app, "load_optional_oss_uploader", return_value=(None, None)),
            patch.object(web_app, "upload_video_if_needed", side_effect=lambda document, *_args: document),
            patch.object(web_app, "upload_generation_records", side_effect=lambda records, _uploader: records),
            patch.object(
                web_app,
                "export_single_product_workbook",
                return_value=(workbook, [record], None),
            ) as export,
            patch.object(web_app, "SharedPackageBuilder", return_value=builder, create=True),
        ):
            runner_class.return_value.run.return_value = [record]
            handler._generate(
                Mock(),
                manifest,
                source,
                output,
                None,
                ("main", "sku", "detail"),
                1,
                None,
                None,
                state.task_signature(),
                "competitor_reference",
            )

        export.assert_called_once()
        client.publish.assert_called_once_with(bundle, lease)
        client.release_lock.assert_called_once_with(lease)
        self.assertEqual(state.shared_library["status"], "published")
        self.assertIsNone(state.shared_lease)
        self.assertTrue(state.shared_heartbeat_stop is None)

    def test_collection_failure_releases_shared_lease(self):
        state = web_app.AppState()
        state.collecting = True
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        stop_event = __import__("threading").Event()
        state.shared_client = client
        state.shared_lease = lease
        state.shared_heartbeat_stop = stop_event
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "allocate_collection_cdp_url", return_value="http://127.0.0.1:9223"),
            patch.object(web_app, "resolve_direct_item_url", side_effect=RuntimeError("collection failed")),
        ):
            handler._collect(state.agent.reference_url, 10, ("main", "sku", "detail"), None)

        self.assertTrue(stop_event.is_set())
        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)

    def test_status_does_not_serialize_shared_client_or_credentials(self):
        state = web_app.AppState()
        state.shared_client = Mock(secret="access-key-secret")
        state.shared_library = {
            "status": "available",
            "product_key": "taobao-123",
            "message": "已有共享素材",
            "catalog": {"product_key": "taobao-123"},
        }

        serialized = json.dumps(state.status(), ensure_ascii=False)

        self.assertNotIn("access-key-secret", serialized)
        self.assertNotIn("shared_client", serialized)
        self.assertEqual(state.status()["shared_library"]["product_key"], "taobao-123")

    def test_changing_reference_url_releases_previous_shared_lease(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        state.shared_client = client
        state.shared_lease = lease
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={"reference_url": "https://detail.tmall.com/item.htm?id=456"}
        )
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "find_prior_direct_collected_manifest", return_value=None),
            patch.object(handler, "_maybe_auto_collect"),
        ):
            handler._set_reference_url()

        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)
        self.assertEqual(state.agent.reference_url, "https://detail.tmall.com/item.htm?id=456")

    def test_shutdown_releases_shared_lease(self):
        state = web_app.AppState()
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        state.shared_client = client
        state.shared_lease = lease
        handler = object.__new__(web_app.RequestHandler)
        handler._json = Mock()
        handler.server = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "close_project_collection_browser"),
            patch.object(web_app.threading.Timer, "start"),
        ):
            handler._shutdown_application()

        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)

    def test_chat_reset_releases_shared_lease(self):
        state = web_app.AppState()
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        state.shared_client = client
        state.shared_lease = lease
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/chat"
        handler._json_body = Mock(return_value={"message": "reset"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "classify_message", return_value={"action": "reset"}),
            patch.object(handler, "_maybe_auto_collect"),
            patch.object(handler, "_maybe_auto_generate"),
        ):
            handler.do_POST()

        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)

    def test_generation_setup_failure_releases_shared_lease(self):
        state = web_app.AppState()
        collected = web_app.OUTPUT_ROOT / "store-insight" / "setup-failure"
        source = collected / "main.jpg"
        manifest = collected / "manifest.json"
        collected.mkdir(parents=True)
        source.write_bytes(b"source")
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(source)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.vision_api_key = "vision-key"
        state.image_api_key = "image-key"
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            manifest_loaded=True,
            workflows=("main",),
            generation_mode="competitor_reference",
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        state.shared_client = client
        state.shared_lease = lease
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", side_effect=RuntimeError("bad settings")),
        ):
            error = handler._begin_generation(force=True)

        self.assertEqual(error, "bad settings")
        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)

    def test_collector_thread_start_failure_releases_shared_lease(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        client.probe.return_value = SharedProbe("missing", None, None)
        client.acquire_lock.return_value = lease
        threads = {}

        def make_thread(*_args, **kwargs):
            thread = Mock()
            if kwargs.get("name") == "collector":
                thread.start.side_effect = RuntimeError("thread start failed")
            threads[kwargs.get("name")] = thread
            return thread

        handler = object.__new__(web_app.RequestHandler)
        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_shared_library_client", return_value=client),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
            patch.object(web_app.threading, "Thread", side_effect=make_thread),
        ):
            with self.assertRaisesRegex(RuntimeError, "thread start failed"):
                handler._begin_collection()

        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)
        self.assertFalse(state.collecting)

    def test_local_start_race_releases_newly_acquired_shared_lease(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://item.taobao.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            workflows=("main", "sku", "detail"),
            generation_mode="competitor_reference",
        )
        lease = LockLease(
            "taobao-123",
            "task-1",
            "client-1",
            "etag-1",
            "2026-08-15T10:00:00+00:00",
            "2026-08-15T12:00:00+00:00",
        )
        client = Mock()
        handler = object.__new__(web_app.RequestHandler)

        def prepare(_reference_url):
            state.shared_client = client
            state.shared_lease = lease
            state.collecting = True
            return None

        with (
            patch.object(web_app, "STATE", state),
            patch.object(handler, "_prepare_shared_collection", side_effect=prepare),
            patch.object(web_app, "load_browser_choice", return_value="edge"),
            patch.object(web_app, "load_browser_executable", return_value="edge.exe"),
        ):
            error = handler._begin_collection()

        self.assertIn("Another workflow", error)
        client.release_lock.assert_called_once_with(lease)
        self.assertIsNone(state.shared_lease)

    def test_api_settings_require_a_session_vision_key(self):
        with patch.object(
            web_app,
            "load_local_api_config",
            return_value=("https://api.example", "image-key", "built-in-vision-key"),
        ):
            with self.assertRaisesRegex(RuntimeError, "enter the vision model API key"):
                web_app.load_api_settings("")

    def test_api_settings_use_runtime_image_key_instead_of_local_config(self):
        with patch.object(
            web_app,
            "load_local_api_config",
            return_value=("https://api.example", "config-image-key", ""),
        ):
            settings = web_app.load_api_settings("runtime-vision-key", "runtime-image-key")

        self.assertEqual(settings.vision_api_key, "runtime-vision-key")
        self.assertEqual(settings.image_api_key, "runtime-image-key")

    def test_save_model_api_keys_preserves_unrelated_local_settings(self):
        existing = {
            "base_url": "https://api.example",
            "browser_choice": "waxiang",
            "oss": {"bucket": "product-assets"},
        }
        web_app.SETTINGS_PATH.write_text(json.dumps(existing), encoding="utf-8")

        web_app.save_model_api_keys("persisted-vision-key", "persisted-image-key")

        saved = json.loads(web_app.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["base_url"], "https://api.example")
        self.assertEqual(saved["browser_choice"], "waxiang")
        self.assertEqual(saved["oss"], {"bucket": "product-assets"})
        self.assertEqual(saved["vision_api_key"], "persisted-vision-key")
        self.assertEqual(saved["image_api_key"], "persisted-image-key")

    def test_save_model_api_keys_creates_missing_settings_from_template(self):
        web_app.SETTINGS_PATH.unlink()
        template_path = web_app.SETTINGS_PATH.with_name("local_settings.example.json")
        template_path.write_text(
            json.dumps(
                {
                    "base_url": "https://api.example",
                    "browser_choice": "",
                    "oss": {"bucket": "product-assets"},
                }
            ),
            encoding="utf-8",
        )

        web_app.save_model_api_keys("persisted-vision-key", "persisted-image-key")

        saved = json.loads(web_app.SETTINGS_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["base_url"], "https://api.example")
        self.assertEqual(saved["oss"], {"bucket": "product-assets"})
        self.assertEqual(saved["vision_api_key"], "persisted-vision-key")
        self.assertEqual(saved["image_api_key"], "persisted-image-key")

    def test_portable_launcher_disables_executable_browser_autostart(self):
        launcher = Path(web_app.__file__).with_name("启动程序.bat").read_text(encoding="utf-8")

        self.assertNotIn('start "" /b', launcher)
        self.assertIn('Start-Process -FilePath $env:WORKFLOW_EXE', launcher)
        self.assertIn('Start-Process -FilePath $env:WORKFLOW_PYTHON', launcher)
        self.assertIn("@('-m', 'web_app', '--no-browser')", launcher)
        self.assertIn('-WindowStyle Hidden', launcher)
        self.assertIn('-RedirectStandardOutput $env:WORKFLOW_STDOUT', launcher)
        self.assertIn('-RedirectStandardError $env:WORKFLOW_STDERR', launcher)
        self.assertIn(
            'bootstrap.ps1" -Mode Ensure -NonInteractive -Root "%ROOT:~0,-1%"',
            launcher,
        )

    def test_app_state_restores_persisted_model_api_keys(self):
        web_app.SETTINGS_PATH.write_text(
            json.dumps(
                {
                    "base_url": "https://api.example",
                    "vision_api_key": "persisted-vision-key",
                    "image_api_key": "persisted-image-key",
                }
            ),
            encoding="utf-8",
        )

        state = web_app.AppState()

        self.assertEqual(state.vision_api_key, "persisted-vision-key")
        self.assertEqual(state.image_api_key, "persisted-image-key")

    def test_setting_api_keys_persists_without_returning_key_values(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={
                "vision_api_key": "persisted-vision-key",
                "image_api_key": "persisted-image-key",
            }
        )
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "save_model_api_keys") as save_keys,
        ):
            handler._set_api_keys()

        save_keys.assert_called_once_with("persisted-vision-key", "persisted-image-key")
        response = handler._json.call_args.args[0]
        self.assertTrue(response["ready"])
        self.assertNotIn("persisted-vision-key", json.dumps(response))
        self.assertNotIn("persisted-image-key", json.dumps(response))

    def test_setting_api_keys_rejects_incomplete_configuration(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={
                "vision_api_key": "vision-only",
                "image_api_key": "",
            }
        )
        handler._json = Mock()

        with patch.object(web_app, "STATE", state):
            handler._set_api_keys()

        self.assertEqual(handler._json.call_args.args[1], web_app.HTTPStatus.BAD_REQUEST)
        self.assertEqual(state.vision_api_key, "")
        self.assertEqual(state.image_api_key, "")

    def test_setting_api_keys_does_not_start_generation(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={
                "vision_api_key": "runtime-vision-key",
                "image_api_key": "runtime-image-key",
            }
        )
        handler._json = Mock()
        handler._maybe_auto_generate = Mock()

        with patch.object(web_app, "STATE", state):
            handler._set_api_keys()

        self.assertEqual(state.vision_api_key, "runtime-vision-key")
        self.assertEqual(state.image_api_key, "runtime-image-key")
        handler._maybe_auto_generate.assert_not_called()

    def test_single_douyin_collection_resolves_short_url_before_launch(self):
        short_url = "https://v.douyin.com/abc123"
        resolved_url = "https://haohuo.jinritemai.com/views/product/item2.html?id=9001"
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url=short_url,
            awaiting="",
            quantity_confirmed=True,
            workflows=("main",),
        )
        manifest = self.root / "manifest.json"
        manifest.write_text("{}", encoding="utf-8")
        process = Mock(pid=1234, stdout=iter([f"[collector] manifest: {manifest}\n"]), returncode=0)
        process.wait.return_value = 0
        handler = object.__new__(web_app.RequestHandler)
        handler._maybe_auto_generate = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "allocate_collection_cdp_url", return_value="http://127.0.0.1:9223"),
            patch.object(web_app, "load_browser_executable", return_value=""),
            patch.object(web_app, "resolve_direct_item_url", return_value=resolved_url, create=True) as resolve,
            patch.object(web_app.subprocess, "Popen", return_value=process) as popen,
        ):
            handler._collect(short_url, 10, ("main",), None)

        resolve.assert_called_once_with(short_url)
        self.assertEqual(popen.call_args.args[0][2], resolved_url)

    def test_single_douyin_resolution_failure_resets_collection_state(self):
        short_url = "https://v.douyin.com/abc123"
        state = web_app.AppState()
        state.collecting = True
        state.agent = AgentSession(
            reference_url=short_url,
            awaiting="",
            quantity_confirmed=True,
            workflows=("main",),
        )
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "allocate_collection_cdp_url", return_value="http://127.0.0.1:9223"),
            patch.object(web_app, "resolve_direct_item_url", side_effect=RuntimeError("short-link failed")),
            patch.object(state, "save_session") as save_session,
            patch.object(web_app.subprocess, "Popen") as popen,
        ):
            handler._collect(short_url, 10, ("main",), None)

        self.assertFalse(state.collecting)
        self.assertTrue(state.collection_paused)
        self.assertIsNone(state.collector_pid)
        self.assertFalse(state.collection_stop_requested)
        save_session.assert_called_once()
        popen.assert_not_called()

    def test_supplement_has_independent_runtime_state(self):
        state = web_app.AppState()
        state.batch_running = True
        state.supplement_running = True
        state.supplement_stop_requested = True

        status = state.status()

        self.assertTrue(status["batch"]["running"])
        self.assertTrue(status["supplement"]["running"])
        self.assertTrue(status["supplement"]["stop_requested"])

    def test_supplement_can_start_while_batch_is_running(self):
        state = web_app.AppState()
        workbook = self.root / "selected.xlsx"
        workbook.write_bytes(b"workbook")
        state.supplement_workbook = workbook
        state.batch_running = True
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"category": "main", "count": 1})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(None, None)),
            patch.object(web_app, "resolve_supplement_workbook", return_value=Mock(item=DirectLinkBatchItem(1, 1, "https://item.jd.com/1.html", "jd"))),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch_supplement()

        self.assertTrue(state.supplement_running)
        self.assertTrue(state.supplement_running)
        self.assertFalse(runner_class.return_value.run.called)
        handler._json.assert_called_once()

    def test_batch_can_start_while_supplement_is_running(self):
        state = web_app.AppState()
        workbook = self.root / "input.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "direct_link"
        state.supplement_running = True
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"run_mode": "collect_only"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "extract_direct_link_items", return_value=[DirectLinkBatchItem(1, 1, "https://item.jd.com/1.html", "jd")]),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch()

        self.assertTrue(state.supplement_running)
        self.assertTrue(state.supplement_running)
        runner_class.assert_called_once()

    def test_stopping_supplement_does_not_cancel_batch_runner(self):
        state = web_app.AppState()
        supplement_runner = Mock()
        batch_runner = Mock()
        state.supplement_running = True
        state.supplement_runner = supplement_runner
        state.batch_running = True
        state.batch_runner = batch_runner
        handler = object.__new__(web_app.RequestHandler)
        handler._json = Mock()

        with patch.object(web_app, "STATE", state):
            handler._stop_batch_supplement()

        supplement_runner.cancel.assert_called_once()
        batch_runner.cancel.assert_not_called()
        self.assertTrue(state.supplement_stop_requested)

    def test_frontend_exposes_all_missing_and_independent_supplement_stop(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn('option value="all"', source)
        self.assertIn("/api/batch-supplement-stop", source)
        self.assertIn("supplement.running", source)

    def test_supplement_controls_are_stable_across_status_polling_rerenders(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        controls_start = source.index("function SupplementControls")
        app_start = source.index("function App()")

        self.assertLess(controls_start, app_start)
        self.assertNotIn("function SupplementControls", source[app_start:])

    def test_supplement_frontend_selects_exported_workbook_without_sequence(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        controls = source[source.index("function SupplementControls"):source.index("function App()")]
        supplement = source[source.index("async function supplementBatchImage"):source.index("async function openFolder")]

        self.assertIn("选择结果表格", controls)
        self.assertNotIn("商品序号", controls)
        self.assertIn("/api/supplement-select", source)
        self.assertNotIn("sequence,", supplement)

    def test_frontend_quantity_selection_is_not_overwritten_by_status_polling(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("mainQuantityEditing.current", source)
        self.assertIn("handleMainQuantityModeChange", source)
        self.assertIn("void persistMainQuantity(mode, null)", source)

    def test_single_workbook_export_button_invokes_export_handler(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("onClick={() => void exportSingleWorkbook()}", source)
        self.assertNotIn("onClick={() => void exportSingleWorkbook}>", source)

    def test_frontend_requires_model_api_setup_and_exposes_topbar_trigger(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")

        self.assertIn("setApiSetupOpen(true)", source)
        self.assertIn('className={`api-status-button', source)
        self.assertIn('role="dialog"', source)
        self.assertIn("保存并继续", source)
        self.assertIn("/api/api-keys", source)

    def test_generation_mode_endpoint_persists_explicit_mode(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"mode": "own_product"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(handler, "_maybe_auto_generate") as auto_generate,
        ):
            handler._set_generation_mode()

        self.assertEqual(state.agent.generation_mode, "own_product")
        auto_generate.assert_not_called()
        handler._json.assert_called_once()

    def test_confirming_new_reference_url_preserves_selected_generation_mode(self):
        state = web_app.AppState()
        state.agent.generation_mode = "own_product"
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={"reference_url": "https://detail.tmall.com/item.htm?id=123"}
        )
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "find_prior_direct_collected_manifest", return_value=None, create=True),
        ):
            handler._set_reference_url()

        self.assertEqual(state.agent.reference_url, "https://detail.tmall.com/item.htm?id=123")
        self.assertEqual(state.agent.generation_mode, "own_product")
        self.assertIsNone(state.manifest_path)
        handler._json.assert_called_once()

    def test_confirming_reference_url_reuses_historical_collection(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(
            return_value={"reference_url": "https://detail.tmall.com/item.htm?id=5001&spm=new"}
        )
        handler._json = Mock()
        manifest = Path(self.temp_dir.name) / "history" / "direct-manifest.json"
        asset = Path(self.temp_dir.name) / "history" / "main.jpg"
        manifest.parent.mkdir(parents=True)
        asset.write_bytes(b"main")
        manifest.write_text(
            json.dumps(
                {
                    "source_url": "https://detail.tmall.com/item.htm?id=5001&spm=old",
                    "product_id": "5001",
                    "images": [{"type": "main", "path": str(asset)}],
                }
            ),
            encoding="utf-8",
        )

        with (
            patch.object(web_app, "STATE", state),
            patch.object(
                web_app,
                "find_prior_direct_collected_manifest",
                return_value=(manifest, 1),
                create=True,
            ) as find_prior,
        ):
            handler._set_reference_url()

        find_prior.assert_called_once()
        self.assertNotEqual(state.manifest_path, manifest)
        self.assertTrue(state.manifest_path.is_file())
        self.assertEqual(state.manifest_path.parent.parent, web_app.OUTPUT_ROOT / "store-insight")
        reused = json.loads(state.manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(reused["product_id"], "5001")
        self.assertEqual(reused["images"][0]["path"], str(asset))
        self.assertTrue(state.agent.manifest_loaded)

    def test_generation_mode_endpoint_rejects_unknown_mode(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"mode": "unknown"})
        handler._json = Mock()

        with patch.object(web_app, "STATE", state):
            handler._set_generation_mode()

        self.assertEqual(state.agent.generation_mode, "competitor_reference")
        self.assertEqual(handler._json.call_args.args[1], web_app.HTTPStatus.BAD_REQUEST)

    def test_status_endpoint_does_not_start_automatic_work(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/status"
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(handler, "_maybe_auto_collect") as auto_collect,
            patch.object(handler, "_maybe_auto_generate") as auto_generate,
        ):
            handler.do_GET()

        auto_collect.assert_not_called()
        auto_generate.assert_not_called()
        handler._json.assert_called_once_with(state.status())

    def test_batch_status_exposes_explicit_direct_link_mode_and_counts(self):
        state = web_app.AppState()
        state.batch_mode = "direct_link"
        state.batch_total = 4
        state.batch_valid = 2
        state.batch_invalid = 1
        state.batch_unsupported = 1

        status = state.status()["batch"]

        self.assertEqual(status["mode"], "direct_link")
        self.assertEqual(status["valid"], 2)
        self.assertEqual(status["invalid"], 1)
        self.assertEqual(status["unsupported"], 1)

    def test_batch_upload_accepts_direct_replace_and_reports_valid_pair(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler.headers = {"Content-Type": "multipart/form-data; boundary=test"}
        handler.rfile = BytesIO(b"")
        handler._json = Mock()
        upload = Mock(filename="replace.xlsx", file=BytesIO(b"workbook"))

        class Form(dict):
            def getfirst(self, name, default=None):
                return "direct_replace" if name == "batch_mode" else default

        form = Form(workbook=upload)
        parsed_item = DirectReplaceBatchItem(
            1,
            "商品",
            2,
            self.root / "product.jpg",
            "https://item.jd.com/1.html",
            "jd",
        )

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app.cgi, "FieldStorage", return_value=form),
            patch.object(web_app, "extract_direct_replace_items", return_value=[parsed_item]) as extract,
        ):
            handler._upload_batch_workbook()

        self.assertEqual(state.batch_mode, "direct_replace")
        self.assertEqual(state.batch_valid, 1)
        extract.assert_called_once()
        self.assertTrue(handler._json.call_args.args[0]["accepted"])

    def test_batch_upload_reports_validation_error_categories(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler.headers = {"Content-Type": "multipart/form-data; boundary=test"}
        handler.rfile = BytesIO(b"")
        handler._json = Mock()
        upload = Mock(filename="replace.xlsx", file=BytesIO(b"workbook"))

        class Form(dict):
            def getfirst(self, name, default=None):
                return "direct_replace" if name == "batch_mode" else default

        form = Form(workbook=upload)
        items = [
            DirectReplaceBatchItem(1, "商品一", 2, self.root / "product.jpg", "https://item.jd.com/1.html", "jd"),
            DirectReplaceBatchItem(2, "商品二", 3, self.root / "product.jpg", "https://item.jd.com/2.html", "jd", validation_error="缺少我方商品图"),
            DirectReplaceBatchItem(3, "商品三", 4, self.root / "product.jpg", "", "invalid", validation_error="缺少商品链接"),
            DirectReplaceBatchItem(4, "商品四", 5, self.root / "product.jpg", "", "invalid", validation_error="图片或链接配对冲突"),
        ]

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app.cgi, "FieldStorage", return_value=form),
            patch.object(web_app, "extract_direct_replace_items", return_value=items),
        ):
            handler._upload_batch_workbook()

        response = handler._json.call_args.args[0]
        self.assertEqual(response["missing_images"], 1)
        self.assertEqual(response["missing_links"], 1)
        self.assertEqual(response["pairing_conflicts"], 1)
        self.assertEqual(state.status()["batch"]["missing_images"], 1)

    def test_frontend_exposes_direct_replace_as_a_batch_link_mode(self):
        source = (Path(web_app.ROOT) / "frontend" / "src" / "App.tsx").read_text(encoding="utf-8")
        types = (Path(web_app.ROOT) / "frontend" / "src" / "types.ts").read_text(encoding="utf-8")

        self.assertIn("产品图批量替换", source)
        self.assertIn("direct_replace", source)
        self.assertIn("'direct_replace'", types)

    def test_collect_only_batch_does_not_require_generation_api_settings(self):
        state = web_app.AppState()
        workbook = self.root / "links.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "direct_link"
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"run_mode": "collect_only"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", side_effect=AssertionError("API settings must not load")),
            patch.object(web_app, "load_optional_oss_uploader", side_effect=AssertionError("OSS must not load")),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch()

        self.assertTrue(runner_class.call_args.kwargs["collect_only"])
        handler._json.assert_called_once()

    def test_batch_start_forwards_user_generation_limits(self):
        state = web_app.AppState()
        workbook = self.root / "limits.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch-limits"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "direct_replace"
        state.agent.max_main_images = 5
        state.agent.max_sku_images = 4
        state.agent.max_detail_images = 6
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"run_mode": "full"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(None, None)),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch()

        self.assertEqual(runner_class.call_args.kwargs["max_main_images"], 5)
        self.assertEqual(runner_class.call_args.kwargs["max_sku_images"], 4)
        self.assertEqual(runner_class.call_args.kwargs["max_detail_images"], 6)

    def test_direct_link_batch_wires_shared_library_from_oss_uploader(self):
        state = web_app.AppState()
        workbook = self.root / "links.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "direct_link"
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"run_mode": "full"})
        handler._json = Mock()
        uploader = Mock(config=Mock(), bucket=Mock())
        cache = Mock(client_id="client-1")
        shared_client = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(uploader, None)),
            patch.object(web_app, "load_shared_library_cache", return_value=cache),
            patch.object(web_app, "SharedLibraryClient", return_value=shared_client) as client_class,
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch()

        client_class.assert_called_once_with(uploader.config, uploader.bucket, cache.client_id)
        self.assertIs(runner_class.call_args.kwargs["shared_library"], shared_client)
        self.assertIs(runner_class.call_args.kwargs["shared_cache"], cache)

    def test_non_direct_link_batch_does_not_initialize_shared_library(self):
        state = web_app.AppState()
        workbook = self.root / "images.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "image_search"
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"run_mode": "full"})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(Mock(), None)),
            patch.object(web_app, "load_shared_library_cache") as load_cache,
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch()

        load_cache.assert_not_called()
        self.assertIsNone(runner_class.call_args.kwargs["shared_library"])
        self.assertIsNone(runner_class.call_args.kwargs["shared_cache"])

    def test_batch_supplement_starts_one_product_without_running_the_full_batch(self):
        state = web_app.AppState()
        workbook = self.root / "links.xlsx"
        workbook.write_bytes(b"workbook")
        output = self.root / "batch"
        output.mkdir()
        state.batch_input = workbook
        state.batch_output = output
        state.batch_mode = "direct_link"
        state.batch_total = 3
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"sequence": 2, "category": "main", "count": 1})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(None, None)),
            patch.object(web_app, "resolve_supplement_workbook", return_value=Mock(item=DirectLinkBatchItem(1, 1, "https://item.jd.com/1.html", "jd"))),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start") as start,
        ):
            handler._start_batch_supplement()

        self.assertTrue(state.supplement_running)
        self.assertEqual(runner_class.call_args.kwargs["batch_mode"], "direct_link")
        runner_class.return_value.run.assert_not_called()
        start.assert_called_once()
        handler._json.assert_called_once()

    def test_result_workbook_selection_persists_real_path(self):
        state = web_app.AppState()
        workbook = self.root / "result.xlsx"
        workbook.write_bytes(b"workbook")
        handler = object.__new__(web_app.RequestHandler)
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "choose_supplement_workbook", return_value=workbook),
            patch.object(web_app, "resolve_supplement_workbook") as resolve,
        ):
            resolve.return_value = Mock(item=Mock(title="测试商品"))
            handler._select_supplement_workbook()

        self.assertEqual(state.supplement_workbook, workbook)
        self.assertEqual(handler._json.call_args.args[0]["workbook"], str(workbook))

    def test_result_workbook_selection_rejects_invalid_xlsx_cleanly(self):
        state = web_app.AppState()
        workbook = self.root / "invalid.xlsx"
        workbook.write_bytes(b"not-an-xlsx")
        handler = object.__new__(web_app.RequestHandler)
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "choose_supplement_workbook", return_value=workbook),
            patch.object(web_app, "resolve_supplement_workbook", side_effect=BadZipFile("File is not a zip file")),
        ):
            handler._select_supplement_workbook()

        self.assertIsNone(state.supplement_workbook)
        self.assertEqual(handler._json.call_args.args[1], web_app.HTTPStatus.BAD_REQUEST)

    def test_supplement_uses_selected_result_workbook_instead_of_batch_sequence(self):
        state = web_app.AppState()
        workbook = self.root / "selected.xlsx"
        workbook.write_bytes(b"workbook")
        state.supplement_workbook = workbook
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"category": "sku", "count": 1})
        handler._json = Mock()

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_api_settings", return_value=Mock()),
            patch.object(web_app, "load_optional_oss_uploader", return_value=(None, None)),
            patch.object(
                web_app,
                "resolve_supplement_workbook",
                return_value=Mock(item=DirectLinkBatchItem(1, 1, "https://item.jd.com/1.html", "jd")),
            ),
            patch.object(web_app, "BatchRunner") as runner_class,
            patch.object(web_app.threading.Thread, "start"),
        ):
            handler._start_batch_supplement()

        self.assertTrue(state.supplement_running)
        self.assertEqual(handler._json.call_args.args[0]["accepted"], True)
        self.assertEqual(runner_class.call_args.kwargs["batch_mode"], "direct_link")

    def test_interrupted_batch_restores_direct_link_collect_only_mode(self):
        workbook = self.root / "links.xlsx"
        workbook.write_bytes(b"workbook")
        output = web_app.OUTPUT_ROOT / "batches" / "direct-collect-only"
        save_batch_results(
            workbook,
            output,
            [{"sequence": 1, "row": 2, "status": "stopped"}],
            batch_mode="direct_link",
            run_mode="collect_only",
            total=3,
        )

        restored = web_app.find_interrupted_batch()

        self.assertIsNotNone(restored)
        _, restored_output, total, _, batch_mode, run_mode = restored
        self.assertEqual(restored_output, output)
        self.assertEqual(total, 3)
        self.assertEqual(batch_mode, "direct_link")
        self.assertEqual(run_mode, "collect_only")

    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_output_root = web_app.OUTPUT_ROOT
        self.original_session_path = web_app.SESSION_PATH
        self.original_settings_path = web_app.SETTINGS_PATH
        web_app.OUTPUT_ROOT = self.root / "outputs"
        web_app.SESSION_PATH = web_app.OUTPUT_ROOT / "current_session.json"
        web_app.SETTINGS_PATH = self.root / "local_settings.json"
        web_app.SETTINGS_PATH.write_text(
            json.dumps({"base_url": "https://api.example", "browser_choice": "waxiang"}),
            encoding="utf-8",
        )

    def tearDown(self):
        web_app.OUTPUT_ROOT = self.original_output_root
        web_app.SESSION_PATH = self.original_session_path
        web_app.SETTINGS_PATH = self.original_settings_path
        self.temp_dir.cleanup()

    def _configure_task(self, state, product_name="product.jpg"):
        manifest = self.root / "collected" / "manifest.json"
        product = self.root / "uploads" / product_name
        manifest.parent.mkdir(parents=True, exist_ok=True)
        product.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"images": []}), encoding="utf-8")
        product.write_bytes(b"product")
        state.manifest_path = manifest
        state.product_image = product
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            max_main_images=None,
            awaiting="",
            manifest_loaded=True,
            workflows=("main",),
            generation_mode="own_product",
        )

    def test_restores_only_results_for_the_same_task_signature(self):
        state = web_app.AppState()
        self._configure_task(state)
        output = self.root / "generated"
        output.mkdir()
        state.generated_output = output
        state.results = [{"category": "main", "ordinal": 1, "url": "/output/generated/main/001.png"}]
        state.completed_task_signature = state.task_signature()
        state.save_session()

        restored = web_app.AppState()

        self.assertEqual(restored.results, state.results)
        self.assertEqual(restored.completed_task_signature, state.completed_task_signature)

    def test_restores_sku_and_detail_generation_counts(self):
        state = web_app.AppState()
        self._configure_task(state)
        state.agent.max_sku_images = 2
        state.agent.max_detail_images = 4
        state.save_session()

        restored = web_app.AppState()

        self.assertEqual(restored.agent.max_sku_images, 2)
        self.assertEqual(restored.agent.max_detail_images, 4)

    def test_restores_separate_collection_and_generation_scopes(self):
        state = web_app.AppState()
        self._configure_task(state)
        state.agent.collection_types = ("main", "sku", "detail")
        state.agent.workflows = ("main", "detail")
        state.save_session()

        restored = web_app.AppState()

        self.assertEqual(restored.agent.collection_types, ("main", "sku", "detail"))
        self.assertEqual(restored.agent.workflows, ("main", "detail"))

    def test_legacy_session_restores_collection_scope_from_workflows(self):
        state = web_app.AppState()
        self._configure_task(state)
        state.save_session()
        document = json.loads(web_app.SESSION_PATH.read_text(encoding="utf-8"))
        document["agent"].pop("collection_types", None)
        web_app.SESSION_PATH.write_text(json.dumps(document), encoding="utf-8")

        restored = web_app.AppState()

        self.assertEqual(restored.agent.collection_types, restored.agent.workflows)

    def test_generation_scope_ignores_unselected_collection_category(self):
        state = web_app.AppState()
        root = web_app.OUTPUT_ROOT / "collected"
        images = []
        for category in ("main", "detail"):
            image = root / category / "001.jpg"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(b"image")
            images.append({"type": category, "path": str(image)})
        manifest = root / "manifest.json"
        manifest.write_text(json.dumps({"images": images}), encoding="utf-8")
        state.manifest_path = manifest
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=123",
            awaiting="",
            quantity_confirmed=True,
            collection_types=("main", "sku", "detail"),
            workflows=("main", "detail"),
        )

        self.assertEqual(state.missing_workflows(), ())
        self.assertEqual(state.missing_collection_workflows(), ("sku",))

    def test_task_signature_includes_sku_and_detail_generation_counts(self):
        state = web_app.AppState()
        self._configure_task(state)
        state.agent.max_sku_images = 2
        state.agent.max_detail_images = 4

        signature = json.loads(state.task_signature())

        self.assertEqual(signature["max_sku_images"], 2)
        self.assertEqual(signature["max_detail_images"], 4)

    def test_legacy_results_do_not_block_a_new_task(self):
        state = web_app.AppState()
        self._configure_task(state)
        output = self.root / "previous-generated"
        output.mkdir()
        web_app.SESSION_PATH.parent.mkdir(parents=True, exist_ok=True)
        web_app.SESSION_PATH.write_text(
            json.dumps(
                {
                    "agent": {
                        "reference_url": state.agent.reference_url,
                        "max_main_images": None,
                        "awaiting": "",
                        "manifest_loaded": True,
                        "workflows": ["main"],
                    },
                    "manifest_path": str(state.manifest_path),
                    "product_image": str(state.product_image),
                    "generated_output": str(output),
                    "results": [{"category": "main", "ordinal": 1, "url": "/output/old.png"}],
                }
            ),
            encoding="utf-8",
        )

        restored = web_app.AppState()

        self.assertEqual(restored.results, [])
        self.assertIsNone(restored.generated_output)
        self.assertIsNone(restored.completed_task_signature)

    def test_missing_selected_workflow_is_detected(self):
        state = web_app.AppState()
        image = web_app.OUTPUT_ROOT / "collected" / "main" / "001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"main")
        manifest = image.parents[1] / "manifest.json"
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(image)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            manifest_loaded=True,
            workflows=("main", "sku"),
        )

        self.assertEqual(state.missing_workflows(), ("sku",))

    def test_manifest_missing_asset_type_is_not_retried_forever(self):
        state = web_app.AppState()
        image = web_app.OUTPUT_ROOT / "collected" / "main" / "001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"main")
        manifest = image.parents[1] / "manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "images": [{"type": "main", "path": str(image)}],
                    "requested_asset_types": ["main", "sku"],
                    "missing_asset_types": ["sku"],
                }
            ),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            manifest_loaded=True,
            workflows=("main", "sku"),
        )

        self.assertEqual(state.missing_workflows(), ())
        self.assertEqual(state.unavailable_workflows(), ("sku",))
        self.assertEqual(state.runnable_workflows(), ("main",))

    def test_collection_only_mode_blocks_automatic_generation(self):
        state = web_app.AppState()
        self._configure_task(state)
        image = self.root / "collected" / "main" / "001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"main")
        state.manifest_path.write_text(
            json.dumps({"images": [{"type": "main", "path": str(image)}]}),
            encoding="utf-8",
        )
        state.agent.generation_enabled = False
        state.vision_api_key = "vision-key"
        state.image_api_key = "image-key"
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(handler, "_begin_generation") as begin_generation,
        ):
            handler._maybe_auto_generate()

        begin_generation.assert_not_called()

    def test_direct_reference_mode_can_auto_generate_without_uploaded_product(self):
        state = web_app.AppState()
        image = web_app.OUTPUT_ROOT / "collected" / "main" / "001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"main")
        manifest = image.parents[1] / "manifest.json"
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(image)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.product_image = None
        state.vision_api_key = "vision-key"
        state.image_api_key = "image-key"
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            manifest_loaded=True,
            workflows=("main",),
            generation_mode="competitor_reference",
        )
        handler = object.__new__(web_app.RequestHandler)

        with patch.object(web_app, "STATE", state), patch.object(handler, "_begin_generation") as begin_generation:
            handler._maybe_auto_generate()

        begin_generation.assert_called_once_with()

    def test_generate_passes_direct_reference_mode_and_identity_to_runner(self):
        state = web_app.AppState()
        state.agent.generation_mode = "competitor_reference"
        manifest = self.root / "manifest.json"
        identity = self.root / "main.jpg"
        output = self.root / "generated"
        manifest.write_text(json.dumps({"images": []}), encoding="utf-8")
        identity.write_bytes(b"main")
        runner = Mock()
        runner.run.return_value = []
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch("web_app.WorkflowRunner", return_value=runner),
            patch("web_app.load_optional_oss_uploader", return_value=(None, None)),
        ):
            handler._generate(
                Mock(),
                manifest,
                identity,
                output,
                None,
                ("main",),
                None,
                2,
                4,
                "signature",
                "competitor_reference",
            )

        runner.run.assert_called_once_with(
            manifest,
            identity,
            output,
            None,
            ("main",),
            None,
            2,
            4,
            generation_mode="competitor_reference",
            identity_image=identity,
        )
        self.assertIsNone(state.completed_task_signature)
        self.assertTrue(any("没有生成任何有效图片" in message for message in state.logs))

    def test_partial_single_generation_is_not_recorded_as_completed(self):
        state = web_app.AppState()
        manifest = self.root / "partial-manifest.json"
        identity = self.root / "partial-main.jpg"
        output = self.root / "partial-generated"
        identity.write_bytes(b"main")
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(identity)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            manifest_loaded=True,
            workflows=("main",),
            max_main_images=2,
            generation_mode="competitor_reference",
        )
        signature = state.task_signature()
        runner = Mock()
        runner.run.return_value = [
            {"category": "main", "ordinal": 1, "status": "completed", "output_path": str(identity)},
            {"category": "main", "ordinal": 2, "status": "failed", "error": "transport"},
        ]
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch("web_app.WorkflowRunner", return_value=runner),
            patch("web_app.load_optional_oss_uploader", return_value=(None, None)),
            patch("web_app.upload_generation_records", side_effect=lambda records, _uploader: records),
        ):
            handler._generate(
                Mock(),
                manifest,
                identity,
                output,
                None,
                ("main",),
                2,
                None,
                None,
                signature,
                "competitor_reference",
            )

        self.assertIsNone(state.completed_task_signature)
        self.assertTrue(any("未完成" in message for message in state.logs))

    def test_missing_generation_ordinals_reuse_completed_outputs(self):
        manifest = self.root / "retry-manifest.json"
        reference = self.root / "reference.jpg"
        reference.write_bytes(b"reference")
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(reference)}]}),
            encoding="utf-8",
        )
        completed_output = self.root / "generated" / "main" / "001.jpg"
        completed_output.parent.mkdir(parents=True)
        completed_output.write_bytes(b"generated")

        requested = web_app.plan_missing_generation_ordinals(
            manifest,
            ("main",),
            3,
            None,
            None,
            [{"category": "main", "ordinal": 1, "status": "completed", "output_path": str(completed_output)}],
        )

        self.assertEqual(requested, {"main": [2, 3]})

    def test_single_generation_uploads_local_video_manifest_to_oss(self):
        state = web_app.AppState()
        manifest = self.root / "manifest-with-video.json"
        identity = self.root / "main.jpg"
        video = self.root / "main.mp4"
        output = self.root / "generated"
        identity.write_bytes(b"main")
        video.write_bytes(b"video")
        manifest.write_text(
            json.dumps(
                {
                    "images": [{"type": "main", "path": str(identity)}],
                    "main_video_local_path": str(video),
                    "main_video_status": "local_only",
                }
            ),
            encoding="utf-8",
        )
        runner = Mock()
        runner.run.return_value = [
            {"category": "main", "ordinal": 1, "status": "completed", "output_path": str(identity)}
        ]
        uploader = Mock()
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch("web_app.WorkflowRunner", return_value=runner),
            patch("web_app.load_optional_oss_uploader", return_value=(uploader, None)),
            patch("web_app.upload_generation_records", side_effect=lambda records, _uploader: records),
            patch(
                "web_app.upload_video_if_needed",
                return_value={
                    "images": [{"type": "main", "path": str(identity)}],
                    "main_video_local_path": str(video),
                    "main_video_status": "complete",
                    "main_video_url": "https://oss.example/main.mp4",
                },
                create=True,
            ) as upload_video,
        ):
            handler._generate(
                Mock(),
                manifest,
                identity,
                output,
                None,
                ("main",),
                1,
                1,
                1,
                "signature",
                "competitor_reference",
            )

        upload_video.assert_called_once()
        saved = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(saved["main_video_url"], "https://oss.example/main.mp4")

    def test_task_signature_uses_collected_identity_in_direct_reference_mode(self):
        state = web_app.AppState()
        image = web_app.OUTPUT_ROOT / "collected" / "main" / "001.jpg"
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"main")
        manifest = image.parents[1] / "manifest.json"
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(image)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.product_image = None
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            manifest_loaded=True,
            workflows=("main",),
            generation_mode="competitor_reference",
        )

        signature = json.loads(state.task_signature())

        self.assertEqual(signature["generation_mode"], "competitor_reference")
        self.assertEqual(Path(signature["identity_image"]), image.resolve())

    def test_status_reports_collected_main_as_ready_identity(self):
        state = web_app.AppState()
        main = self.root / "collected" / "main" / "001.jpg"
        main.parent.mkdir(parents=True, exist_ok=True)
        main.write_bytes(b"main")
        manifest = main.parents[1] / "manifest.json"
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(main)}]}),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.agent.generation_mode = "competitor_reference"

        status = state.status()

        self.assertEqual(status["identity_source"], "collected_main")
        self.assertTrue(status["identity_ready"])

    def test_status_reports_uploaded_product_identity(self):
        state = web_app.AppState()
        product = self.root / "uploads" / "product.jpg"
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_bytes(b"product")
        state.product_image = product
        state.agent.generation_mode = "own_product"

        status = state.status()

        self.assertEqual(status["identity_source"], "uploaded_product")
        self.assertTrue(status["identity_ready"])

    def test_task_signature_requires_upload_in_own_product_mode(self):
        state = web_app.AppState()
        self._configure_task(state)
        state.product_image = None
        state.agent.generation_mode = "own_product"

        self.assertIsNone(state.task_signature())

    def test_main_quantity_endpoint_accepts_custom_count(self):
        state = web_app.AppState()
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"mode": "custom", "count": 7})
        handler._json = Mock()

        with patch.object(web_app, "STATE", state), patch.object(handler, "_maybe_auto_collect"):
            handler._set_main_quantity()

        self.assertEqual(state.agent.max_main_images, 7)
        self.assertEqual(state.agent.main_quantity_mode, "custom")
        handler._json.assert_called_once()

    def test_paused_collection_is_not_automatically_restarted(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            workflows=("main",),
        )
        state.collection_paused = True
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(handler, "_begin_collection") as begin_collection,
        ):
            handler._maybe_auto_collect()

        begin_collection.assert_not_called()

    def test_paused_collection_survives_service_restart(self):
        state = web_app.AppState()
        state.collection_paused = True
        state.save_session()

        restored = web_app.AppState()

        self.assertTrue(restored.collection_paused)

    def test_explicit_collection_command_resumes_a_paused_task(self):
        self.assertTrue(web_app.requests_collection_resume("采集所有类型的图片，不用生成"))
        self.assertTrue(web_app.requests_collection_resume("只采集 SKU 图"))
        self.assertFalse(web_app.requests_collection_resume("为什么采集没有反应"))

    def test_stop_supersedes_an_earlier_resume_request(self):
        state = web_app.AppState()
        state.collection_paused = True
        stale_version = state.collection_control_version
        state.collection_control_version += 1

        self.assertFalse(web_app.collection_resume_is_current(stale_version, state.collection_control_version))
        self.assertTrue(state.collection_paused)
        self.assertTrue(
            web_app.collection_resume_is_current(
                state.collection_control_version,
                state.collection_control_version,
            )
        )

    def test_inflight_chat_request_cannot_restart_after_stop(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            workflows=("main",),
        )
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/chat"
        handler._json_body = Mock(return_value={"message": "采集所有类型的图片，不用生成"})
        handler._json = Mock()

        def classify_after_stop(*_args):
            with state.lock:
                state.collection_control_version += 1
                state.collection_paused = True
            return {
                "action": "update_task",
                "reference_url": state.agent.reference_url,
                "quantity_mode": "reference",
                "main_count": None,
                "workflows": ["main", "sku", "detail"],
                "generate_images": False,
                "reply": "已切换为采集全部类型图片。",
            }

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_local_api_config", return_value=("https://api.example", "image", "vision")),
            patch.object(web_app, "classify_message", side_effect=classify_after_stop),
            patch.object(handler, "_maybe_auto_collect") as maybe_auto_collect,
        ):
            handler.do_POST()

        self.assertTrue(state.collection_paused)
        maybe_auto_collect.assert_called_once()

    def test_chat_checks_automatic_generation_after_updating_a_complete_task(self):
        state = web_app.AppState()
        collected = self.root / "collected"
        images = []
        for category in ("main", "sku", "detail"):
            image = collected / category / "001.jpg"
            image.parent.mkdir(parents=True, exist_ok=True)
            image.write_bytes(category.encode("ascii"))
            images.append({"type": category, "path": str(image)})
        manifest = collected / "manifest.json"
        manifest.write_text(json.dumps({"images": images}), encoding="utf-8")
        state.manifest_path = manifest
        state.vision_api_key = "vision-key"
        state.image_api_key = "image-key"
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            max_main_images=5,
            max_sku_images=5,
            max_detail_images=5,
            awaiting="",
            manifest_loaded=True,
            workflows=("main", "sku", "detail"),
            generation_enabled=True,
        )
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/chat"
        handler._json_body = Mock(return_value={"message": "collect all types, five images each"})
        handler._json = Mock()

        intent = {
            "action": "update_task",
            "reference_url": state.agent.reference_url,
            "quantity_mode": "custom",
            "main_count": 5,
            "sku_count": 5,
            "detail_count": 5,
            "workflows": ["main", "sku", "detail"],
            "generate_images": True,
            "reply": "Task configured for all types",
        }

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_local_api_config", return_value=("https://api.example", "image", "vision")),
            patch.object(web_app, "classify_message", return_value=intent),
            patch.object(handler, "_maybe_auto_collect") as maybe_auto_collect,
            patch.object(handler, "_maybe_auto_generate") as maybe_auto_generate,
        ):
            handler.do_POST()

        maybe_auto_collect.assert_called_once()
        maybe_auto_generate.assert_called_once()

    def test_chat_keeps_valid_ai_generation_scope_without_local_semantic_override(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            quantity_confirmed=True,
            collection_types=("main", "detail"),
            workflows=("main", "detail"),
        )
        handler = object.__new__(web_app.RequestHandler)
        handler.path = "/api/chat"
        handler._json_body = Mock(return_value={"message": "采集全部类型图片，生成范围保持不变"})
        handler._json = Mock()
        intent = {
            "action": "update_task",
            "reference_url": state.agent.reference_url,
            "quantity_mode": "unspecified",
            "collection_types": ["main", "sku", "detail"],
            "workflows": ["main", "detail"],
            "generate_images": True,
            "reply": "采集全部类型图片，只生成主图和详情图。",
        }

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_local_api_config", return_value=("https://api.example", "image", "vision")),
            patch.object(web_app, "classify_message", return_value=intent),
            patch.object(handler, "_maybe_auto_collect"),
            patch.object(handler, "_maybe_auto_generate"),
        ):
            handler.do_POST()

        self.assertEqual(state.agent.collection_types, ("main", "sku", "detail"))
        self.assertEqual(state.agent.workflows, ("main", "detail"))

    def test_stop_collection_keeps_busy_state_until_process_exits(self):
        state = web_app.AppState()
        state.collecting = True
        state.collector_pid = 12345
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app.subprocess, "run") as taskkill,
            patch.object(handler, "_json") as respond,
        ):
            handler._stop_collection()

        self.assertEqual(taskkill.call_args.args[0], ["taskkill", "/PID", "12345", "/F"])
        self.assertTrue(state.collecting)
        self.assertEqual(state.collector_pid, 12345)
        self.assertEqual(state.collection_control_version, 1)
        self.assertTrue(state.collection_stop_requested)
        self.assertTrue(state.collection_paused)
        respond.assert_called_once()

    def test_supplemental_collection_merges_only_the_missing_type(self):
        old_image = web_app.OUTPUT_ROOT / "old" / "main" / "001.jpg"
        new_image = web_app.OUTPUT_ROOT / "new" / "sku" / "001.jpg"
        old_image.parent.mkdir(parents=True, exist_ok=True)
        new_image.parent.mkdir(parents=True, exist_ok=True)
        old_image.write_bytes(b"main")
        new_image.write_bytes(b"sku")
        existing = web_app.OUTPUT_ROOT / "old" / "manifest.json"
        incoming = web_app.OUTPUT_ROOT / "new" / "manifest.json"
        existing.write_text(
            json.dumps({"images": [{"type": "main", "path": str(old_image)}]}),
            encoding="utf-8",
        )
        incoming.write_text(
            json.dumps({"images": [{"type": "sku", "path": str(new_image)}]}),
            encoding="utf-8",
        )

        web_app.merge_collected_manifest(existing, incoming, ("sku",))
        merged = json.loads(existing.read_text(encoding="utf-8"))

        self.assertEqual([image["type"] for image in merged["images"]], ["main", "sku"])
        self.assertTrue(all(Path(image["path"]).is_file() for image in merged["images"]))
        self.assertEqual(
            Path(merged["images"][1]["path"]).parent.resolve(),
            (existing.parent / "sku").resolve(),
        )

    def test_second_server_uses_a_different_port(self):
        first = web_app.ExclusiveThreadingHTTPServer(("127.0.0.1", 0), web_app.RequestHandler)
        second = None
        try:
            occupied_port = first.server_address[1]
            second, selected_port = web_app.create_local_server(occupied_port)
            self.assertNotEqual(selected_port, occupied_port)
        finally:
            first.server_close()
            if second is not None:
                second.server_close()


class BrowserChoiceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = TemporaryDirectory()
        self.settings_path = Path(self.temp_dir.name) / "local_settings.json"
        self.original_settings_path = web_app.SETTINGS_PATH
        web_app.SETTINGS_PATH = self.settings_path

    def tearDown(self):
        web_app.SETTINGS_PATH = self.original_settings_path
        self.temp_dir.cleanup()

    def test_selected_edge_resolves_to_detected_installation(self):
        executable = Path(self.temp_dir.name) / "msedge.exe"
        executable.write_bytes(b"edge")
        self.settings_path.write_text(json.dumps({"browser_choice": "edge"}), encoding="utf-8")

        with patch.object(web_app, "browser_candidates", return_value=(executable,)):
            self.assertEqual(web_app.load_browser_choice(), "edge")
            self.assertEqual(web_app.load_browser_executable(), str(executable.resolve()))

    def test_waxiang_can_be_found_from_the_windows_installation_registry(self):
        executable = Path(self.temp_dir.name) / "waxiang.exe"
        executable.write_bytes(b"waxiang")

        with (
            patch.object(web_app, "browser_candidates", return_value=()),
            patch.object(web_app, "waixiang_registry_candidates", return_value=(executable,)),
        ):
            self.assertEqual(web_app.find_browser_executable("waxiang"), executable.resolve())

    def test_unknown_browser_choice_is_not_accepted(self):
        self.settings_path.write_text(json.dumps({"browser_choice": "chrome"}), encoding="utf-8")
        self.assertEqual(web_app.load_browser_choice(), "")
        self.assertEqual(web_app.browser_choice_label("chrome"), "未选择")

    def test_browser_selection_saves_only_the_browser_id(self):
        executable = Path(self.temp_dir.name) / "msedge.exe"
        executable.write_bytes(b"edge")
        self.settings_path.write_text(json.dumps({"base_url": "https://api.example"}), encoding="utf-8")
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={"browser_choice": "edge"})
        handler._json = Mock()

        with patch.object(web_app, "find_browser_executable", return_value=executable):
            handler._set_browser_choice()

        saved = json.loads(self.settings_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["browser_choice"], "edge")
        self.assertNotIn("browser_executable", saved)

    def test_collection_requires_a_detected_browser_choice(self):
        state = web_app.AppState()
        state.agent = AgentSession(
            reference_url="https://detail.tmall.com/item.htm?id=1",
            awaiting="",
            workflows=("main",),
        )
        handler = object.__new__(web_app.RequestHandler)

        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "load_browser_choice", return_value=""),
        ):
            error = handler._begin_collection()

        self.assertIn("采集浏览器", error)
        self.assertFalse(state.collecting)

    def test_single_link_export_reuses_batch_workbook_exporter(self):
        state = web_app.AppState()
        state.generated_output = None
        root = Path(self.temp_dir.name)
        manifest = root / "store-insight" / "123" / "collected" / "direct-manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "source_url": "https://item.jd.com/123.html",
                    "product_id": "123",
                    "product_title": "测试商品",
                    "products": [{"product_id": "123", "title": "测试商品"}],
                    "extended_assets": [],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        state.manifest_path = manifest
        state.agent = AgentSession(
            reference_url="https://item.jd.com/123.html",
            awaiting="",
            workflows=("main", "sku", "detail"),
        )
        handler = object.__new__(web_app.RequestHandler)
        handler._json_body = Mock(return_value={})
        handler._json = Mock()
        exported = root / "store-insight" / "123" / "测试商品.xlsx"
        with (
            patch.object(web_app, "STATE", state),
            patch.object(web_app, "export_product_workbook", return_value=exported) as export,
            patch.object(web_app, "recover_generation_records", return_value=([], None)),
            patch.object(web_app, "_direct_link_platform", return_value=("jd", "")),
            patch.object(web_app.subprocess, "Popen"),
        ):
            handler._export_single_workbook()

        item = export.call_args.args[1]
        self.assertIsInstance(item, DirectLinkBatchItem)
        self.assertEqual(item.source_url, "https://item.jd.com/123.html")
        self.assertEqual(export.call_args.args[2], manifest)
        self.assertEqual(export.call_args.args[3], [])
        self.assertEqual(handler._json.call_args.args[0]["workbook"], str(exported))

    def test_single_link_export_recovers_matching_historical_generation_records(self):
        root = Path(self.temp_dir.name)
        manifest = root / "store-insight" / "123" / "manifest.json"
        source_image = manifest.parent / "main" / "001.jpg"
        generated_image = root / "generated" / "run-1" / "main" / "001.png"
        source_image.parent.mkdir(parents=True)
        generated_image.parent.mkdir(parents=True)
        source_image.write_bytes(b"source")
        generated_image.write_bytes(b"generated")
        manifest.write_text(
            json.dumps({"images": [{"type": "main", "path": str(source_image)}]}),
            encoding="utf-8",
        )
        analysis = generated_image.parents[1] / "analysis.json"
        analysis.write_text(
            json.dumps(
                {
                    "records": [
                        {
                            "category": "main",
                            "ordinal": 1,
                            "status": "completed",
                            "source_path": str(source_image),
                            "output_path": str(generated_image),
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )

        records, output = web_app.recover_generation_records(manifest, root / "generated")

        self.assertEqual(len(records), 1)
        self.assertEqual(output, analysis.parent)


if __name__ == "__main__":
    unittest.main()
