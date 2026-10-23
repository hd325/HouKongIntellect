"""Offline HTTP contracts for the three new learning-product routes.

TestClient is deliberately used without its lifespan context. The real session
store, model calls, vector-store getter and metrics-log reader are all replaced;
only temporary file fixtures are written. No real upload or telemetry is touched.
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts
from services.document_files import stable_upload_id
from services.quiz_service import QuizGenerationError, QuizValidationError, validate_options


class _MemorySessionsAndFiles:
    def __init__(self):
        self._deleted_document_ids = set()
        self.files = {}
        self.sessions = {"test-session": {"account": "student-test", "created_at": datetime.now().isoformat()}}
        self.delete_session = AsyncMock()

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def find_file_by_id(self, document_id):
        return self.files.get(document_id)


class LearningApiTests(unittest.TestCase):
    def setUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.folder = tempfile.TemporaryDirectory(prefix="hk_learning_api_")
        self.root = Path(self.folder.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.data = _MemorySessionsAndFiles()
        self.vector_metadata = {}
        self.vector_get = Mock(side_effect=self._get_vector_metadata)
        self.vector = SimpleNamespace(_collection=SimpleNamespace(get=self.vector_get))
        self.generate = AsyncMock(return_value={
            "document_id": "lesson", "question_types": ["choice"], "count": 5, "difficulty": "medium",
            "questions": [{"type": "choice", "stem": "测试题", "options": ["甲", "乙", "丙", "丁"],
                           "answer": "A", "explanation": "原文说明", "page": 1,
                           "chunk_id": "lesson_P1_C1", "document_id": "lesson", "evidence": "测试原文依据"}],
        })
        self.metrics = Mock(return_value={"total_queries": 6, "days": 30, "empty_rate": 0.0,
                                         "latency_p50_ms": 80.0, "latency_p95_ms": 120.0})
        self.patches = [patch.object(main, "data_store", self.data),
                        patch.dict(main.VALID_ACCOUNTS, fixture_accounts(), clear=True),
                        patch.object(main, "UPLOAD_DIR", str(self.uploads)),
                        patch.object(main, "DATA_DIR", str(self.root)),
                        patch("services.knowledge.vector_store.get_vector_store", return_value=self.vector),
                        patch("services.quiz_service.generate_quiz", self.generate),
                        patch("services.metrics_summary.build_metrics_summary", self.metrics),
                        patch("main.logging.warning"), patch("main.logging.error")]
        for item in self.patches:
            item.start()
        self.client = TestClient(main.app)
        self.headers = {"X-Session-ID": "test-session"}

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def _get_vector_metadata(self, *, where, include, limit):
        self.assertEqual(include, ["metadatas"])
        self.assertEqual(limit, 1)
        record = self.vector_metadata.get(where["document_id"])
        return {"metadatas": [record] if record else []}

    def _add_file(self, filename, *, audience="school", owner="student-test", content=b"fixture"):
        stored = str(uuid4()) + "_" + filename
        path = self.uploads / stored
        path.write_bytes(content)
        document_id = stable_upload_id(stored)
        self.data.files[document_id] = {"id": document_id, "name": filename,
                                        "audience": audience, "owner": owner}
        return document_id, path

    def _quiz_payload(self, document_id="lesson", **changes):
        return {"document_id": document_id, "question_types": ["choice"], "count": 5,
                "difficulty": "medium", **changes}

    def test_all_three_routes_have_the_real_session_dependency(self):
        expected = {"/api/documents/{document_id}/file", "/api/quiz/generate", "/api/metrics/summary"}
        seen = set()
        for route in main.app.routes:
            if getattr(route, "path", None) in expected:
                seen.add(route.path)
                self.assertTrue(any(d.call is main.verify_session for d in route.dependant.dependencies), route.path)
        self.assertEqual(seen, expected)

    def test_anonymous_and_invalid_sessions_are_401_for_all_three_routes(self):
        requests = [("GET", "/api/documents/lesson/file", None),
                    ("POST", "/api/quiz/generate", self._quiz_payload()),
                    ("GET", "/api/metrics/summary", None)]
        for headers in ({}, {"X-Session-ID": "invalid-session"}):
            for method, path, body in requests:
                with self.subTest(method=method, path=path, headers=headers):
                    response = self.client.request(method, path, headers=headers, json=body)
                    self.assertEqual(response.status_code, 401)
        self.generate.assert_not_awaited()
        self.metrics.assert_not_called()
        self.vector_get.assert_not_called()

    def test_private_file_and_quiz_are_403_before_reading_source_or_calling_model(self):
        document_id, _ = self._add_file("私有.pdf", audience="personal", owner="another-user")
        response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/api/quiz/generate", headers=self.headers,
                                    json=self._quiz_payload(document_id))
        self.assertEqual(response.status_code, 403)
        self.generate.assert_not_awaited()
        self.vector_get.assert_not_called()

    def test_file_response_mime_inline_and_security_headers(self):
        cases = [("资料.pdf", "application/pdf"), ("圖片.png", "image/png"),
                 ("照片.jpg", "image/jpeg"),
                 ("讲义.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
                 ("笔记.txt", "text/plain"), ("说明.md", "text/plain")]
        for filename, mime in cases:
            with self.subTest(filename=filename):
                document_id, path = self._add_file(filename, content=b"temporary source bytes")
                response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, path.read_bytes())
                self.assertEqual(response.headers["content-type"].split(";", 1)[0], mime)
                self.assertTrue(response.headers["content-disposition"].startswith("inline; "))
                self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
                self.assertNotIn(document_id, response.headers["content-disposition"])
                self.assertEqual(response.headers["x-content-type-options"], "nosniff")
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertIn("sandbox", response.headers["content-security-policy"])
        self.generate.assert_not_awaited()

    def test_bearer_session_can_open_an_authorized_file(self):
        document_id, _ = self._add_file("资料.pdf")
        response = self.client.get(f"/api/documents/{document_id}/file",
                                   headers={"Authorization": "Bearer test-session"})
        self.assertEqual(response.status_code, 200)

    def test_missing_documents_and_missing_physical_uploads_are_404(self):
        response = self.client.get("/api/documents/not-indexed/file", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        document_id, path = self._add_file("removed.pdf")
        path.unlink()
        response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
        self.assertEqual(response.status_code, 404)
        self.generate.assert_not_awaited()

    def test_dangerous_or_unsupported_extensions_are_404(self):
        for filename in ("active.html", "script.js", "binary.exe", "active.xml", "payload.json", "archive.zip"):
            with self.subTest(filename=filename):
                document_id, _ = self._add_file(filename)
                response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
                self.assertEqual(response.status_code, 404)

    def test_index_metadata_authorization_does_not_bypass_private_file_permissions(self):
        document_id, _ = self._add_file("indexed-only.pdf")
        del self.data.files[document_id]
        self.vector_metadata[document_id] = {"document_id": document_id, "audience": "personal", "owner": "another-user"}
        response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
        self.assertEqual(response.status_code, 403)
        self.vector_metadata[document_id]["audience"] = "school"
        response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
        self.assertEqual(response.status_code, 200)

    def test_index_metadata_missing_access_policy_fails_closed(self):
        document_id, _ = self._add_file("unknown-audience.pdf")
        del self.data.files[document_id]
        self.vector_metadata[document_id] = {"document_id": document_id, "owner": "another-user"}
        response = self.client.get(f"/api/documents/{document_id}/file", headers=self.headers)
        self.assertEqual(response.status_code, 403)

    def test_quiz_invalid_body_parameters_are_422_without_calling_model(self):
        self.data.files["lesson"] = {"id": "lesson", "audience": "school"}
        changes = [{"document_id": ""}, {"document_id": "d" * 129},
                   {"question_types": []}, {"question_types": ["fill"]},
                   {"question_types": ["choice", "blank", "short", "choice"]},
                   {"count": 0}, {"count": 21}, {"count": True}, {"count": 2.5},
                   {"count": "5"}, {"difficulty": "easy"}]
        for update in changes:
            with self.subTest(update=update):
                response = self.client.post("/api/quiz/generate", headers=self.headers,
                                            json=self._quiz_payload(**update))
                self.assertEqual(response.status_code, 422)
        response = self.client.post("/api/quiz/generate", headers=self.headers, json={})
        self.assertEqual(response.status_code, 422)
        self.generate.assert_not_awaited()

    def test_quiz_valid_request_calls_mock_service_after_authorization(self):
        self.data.files["lesson"] = {"id": "lesson", "audience": "school"}
        response = self.client.post("/api/quiz/generate", headers=self.headers,
                                    json=self._quiz_payload(question_types=["choice", "blank"], count=6, difficulty="basic"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 200)
        self.assertIn("questions", response.json()["data"])
        self.assertEqual(self.generate.await_count, 1)
        args, kwargs = self.generate.await_args
        self.assertEqual(args, ("lesson",))
        self.assertEqual(kwargs["question_types"], ["choice", "blank"])
        self.assertEqual(kwargs["count"], 6)
        self.assertEqual(kwargs["difficulty"], "basic")
        self.assertTrue(kwargs["api_config"] is main.API_CONFIG, "The existing application config must be reused")

    def test_quiz_duplicate_types_and_insufficient_mixed_count_are_422(self):
        self.data.files["lesson"] = {"id": "lesson", "audience": "school"}

        async def validate_mock_service(document_id, *, question_types, count, difficulty, api_config):
            validate_options(question_types, count, difficulty)
            return {"questions": []}

        self.generate.side_effect = validate_mock_service
        invalid = [("duplicate", {"question_types": ["choice", "choice"], "count": 5}),
                   ("count", {"question_types": ["choice", "blank", "short"], "count": 2})]
        for label, changes in invalid:
            with self.subTest(label=label):
                response = self.client.post("/api/quiz/generate", headers=self.headers,
                                            json=self._quiz_payload(**changes))
                self.assertEqual(response.status_code, 422)

    def test_quiz_validation_and_upstream_errors_are_safe_http_errors(self):
        self.data.files["lesson"] = {"id": "lesson", "audience": "school"}
        for error, code in [(QuizValidationError("题目数量不得少于所选题型数量"), 422),
                            (QuizGenerationError("出题服务暂时不可用，请稍后重试"), 502)]:
            with self.subTest(code=code):
                self.generate.side_effect = error
                response = self.client.post("/api/quiz/generate", headers=self.headers, json=self._quiz_payload())
                self.assertEqual(response.status_code, code)
                self.assertEqual(response.json()["detail"], str(error))

    def test_metrics_summary_returns_mock_aggregate_without_reading_real_log(self):
        response = self.client.get("/api/metrics/summary", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["code"], 200)
        self.assertEqual(response.json()["data"]["total_queries"], 6)
        self.metrics.assert_called_once_with(str(self.root / "work" / "rag_metrics.jsonl"))
        self.generate.assert_not_awaited()
        self.vector_get.assert_not_called()


class FrontendStaticBoundaryTests(unittest.TestCase):
    """Exercise actual existing fixtures through the production static class."""

    PUBLIC = {"tailwindcss.js", "marked.js", "pdf.min.js", "pdf.worker.min.js",
              "font-awesome.css", "logo.png", "fonts/fontawesome-webfont.woff2",
              "dompurify.min.js", "tailwind.generated.css"}
    PRIVATE = ("uploads/known.png", "main.py", "config/settings.py", "sessions_data.json",
               "files_data.json", "conversations_data.json", "work/log", "work/rag_metrics.jsonl")

    def setUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.folder = tempfile.TemporaryDirectory(prefix="hk_static_boundary_")
        self.root = Path(self.folder.name)
        self.fixture = b"temporary-static-contract-fixture"
        for name in self.PUBLIC | set(self.PRIVATE):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self.fixture)
        self.static_app = FastAPI()
        self.static_app.mount("/static", main.FrontendStaticFiles(directory=str(self.root)), name="static")
        self.client = TestClient(self.static_app)

    def tearDown(self):
        self.client.close()
        self.folder.cleanup()

    def test_production_mount_uses_exact_asset_allowlist(self):
        mounts = [route for route in main.app.routes if getattr(route, "path", None) == "/static"]
        self.assertEqual(len(mounts), 1)
        self.assertIsInstance(mounts[0].app, main.FrontendStaticFiles)
        self.assertEqual(set(main.FrontendStaticFiles.PUBLIC_FILES), self.PUBLIC)

    def test_allowed_assets_serve_get_and_head_from_temporary_fixtures(self):
        for filename in sorted(self.PUBLIC):
            for method in ("GET", "HEAD"):
                with self.subTest(filename=filename, method=method):
                    response = self.client.request(method, "/static/" + filename)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["content-length"], str(len(self.fixture)))
                    self.assertEqual(response.content, self.fixture if method == "GET" else b"")

    def test_real_existing_private_fixtures_are_404_with_or_without_session(self):
        for filename in self.PRIVATE:
            for method in ("GET", "HEAD"):
                for headers in ({}, {"X-Session-ID": "test-session"}):
                    with self.subTest(filename=filename, method=method, authenticated=bool(headers)):
                        response = self.client.request(method, "/static/" + filename, headers=headers)
                        self.assertEqual(response.status_code, 404)
                        self.assertNotEqual(response.content, self.fixture)

    def test_encoded_parent_and_backslash_paths_cannot_expose_private_fixtures(self):
        paths = ["/static/%2e%2e/main.py", "/static/%2e%2e%2fmain.py",
                 "/static/%2e%2e%5cmain.py", "/static/config%2fsettings.py",
                 "/static/config%5csettings.py", "/static/uploads%2fknown.png",
                 "/static/uploads%5cknown.png", "/static/%252e%252e%252fmain.py"]
        for path in paths:
            for method in ("GET", "HEAD"):
                for headers in ({}, {"X-Session-ID": "test-session"}):
                    with self.subTest(path=path, method=method, authenticated=bool(headers)):
                        response = self.client.request(method, path, headers=headers)
                        self.assertEqual(response.status_code, 404)
                        self.assertNotEqual(response.content, self.fixture)


if __name__ == "__main__":
    unittest.main()
