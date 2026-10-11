"""Offline upload integration tests; only temporary files and fake sessions."""
from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import main
from tests.account_fixtures import fixture_accounts
from services.upload_analysis import UploadAnalysisError


class MemoryUploadStore:
    def __init__(self):
        self._files = []
        self.contents = {}
        self.sessions = {"upload-test": {"account": "student-test", "created_at": datetime.now().isoformat()}}
        self.fail_save = False

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def get_all_files(self, user_id):
        return [dict(record) for record in self._files if main.check_file_permission(record, user_id)]

    async def get_file_content(self, file_id):
        return self.contents.get(file_id)

    async def add_file(self, record, content):
        if self.fail_save:
            raise OSError("mock disk failure")
        self._files.append(record)
        self.contents[record["id"]] = content

    async def set_session(self, session_id, value):
        self.sessions[session_id] = value

    async def delete_session(self, session_id):
        self.sessions.pop(session_id, None)


class UploadApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="hk_upload_api_")
        self.root = Path(self.folder.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.store = MemoryUploadStore()
        self.extraction_calls = []
        self.suggestions = {"subject": "chemistry", "study_period": "", "material_type": "review_paper",
                            "material_custom": "", "notes": ""}
        self.classify = AsyncMock(return_value=self.suggestions)
        self.index = AsyncMock()
        self.patches = [patch.object(main, "data_store", self.store),
                        patch.dict(main.VALID_ACCOUNTS, fixture_accounts(), clear=True),
                        patch.object(main, "UPLOAD_DIR", str(self.uploads)),
                        patch.object(main, "DATA_DIR", str(self.root)),
                        patch.object(main, "extract_text_from_file", self.extract),
                        patch.object(main, "classify_upload", self.classify),
                        patch.object(main, "study_period_for_month", return_value="first"),
                        patch.object(main, "_index_file_background", self.index),
                        patch("main.logging.warning"), patch("main.logging.error")]
        for item in self.patches:
            item.start()
        self.client = TestClient(main.app)
        self.headers = {"X-Session-ID": "upload-test"}
        self.raw = "第三段 化學 複習卷\n原電池將化學能轉化為電能。".encode("utf-8")

    def tearDown(self):
        self.client.close()
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def extract(self, file, *, progress_callback=None, retry=True):
        self.extraction_calls.append((file.filename, retry))
        if progress_callback:
            progress_callback({"type": "ocr_started", "total": 2})
            progress_callback({"type": "ocr_progress", "completed": 1, "total": 2})
            progress_callback({"type": "ocr_progress", "completed": 2, "total": 2})
        return {"content": file.file.read().decode("utf-8"), "page_count": 2,
                "extraction_status": "success", "error": ""}

    def analyze(self, raw=None, headers=None):
        response = self.client.post("/api/upload/analyze", headers=self.headers if headers is None else headers,
                                    files={"file": ("lesson.txt", self.raw if raw is None else raw, "text/plain")})
        self.assertEqual(response.status_code, 200)
        events = [json.loads(line) for line in response.text.splitlines() if line.strip()]
        result = next(event["data"] for event in events if event["type"] == "result")
        return response, events, result

    def upload(self, result=None, raw=None, request_id="upload-request-001", **changes):
        params = {"audience": "personal", "subject": "chemistry", "study_period": "third",
                  "material_type": "review_paper", "notes": "", **changes}
        data = {"upload_request_id": request_id}
        if result is not None:
            data.update(extraction_result=json.dumps(result["extraction"], ensure_ascii=False),
                        extraction_signature=result["extraction_signature"])
        return self.client.post("/api/upload", headers=self.headers, params=params, data=data,
                                files={"file": ("lesson.txt", self.raw if raw is None else raw, "text/plain")})

    def test_analysis_is_stateless_and_reports_real_page_events(self):
        response, events, result = self.analyze()
        self.assertIn("application/x-ndjson", response.headers["content-type"])
        self.assertEqual(response.headers["cache-control"], "no-store")
        self.assertEqual([event["completed"] for event in events if event["type"] == "ocr_progress"], [1, 2])
        self.assertEqual(result["study_period"], "first")
        self.assertEqual(result["notes"], "")
        self.assertEqual(result["extraction"]["page_count"], 2)
        self.assertFalse(result["possible_duplicate"])
        self.assertNotIn("draft_id", response.text)
        self.assertEqual(self.extraction_calls, [("lesson.txt", False)])
        self.assertEqual(self.store._files, [])
        self.assertEqual(list(self.root.iterdir()), [self.uploads])
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.index.assert_not_awaited()

    def test_ai_failure_is_silent_keeps_system_period_and_signs_extraction(self):
        self.classify.side_effect = UploadAnalysisError("mock upstream failure")
        _, events, result = self.analyze()
        self.assertTrue(all(result[field] == "" for field in self.suggestions if field != "study_period"))
        self.assertEqual(result["study_period"], "first")
        self.assertFalse(any(event["type"] in ("error", "failed") for event in events))
        self.assertTrue(result["extraction_signature"])
        self.assertEqual(self.classify.await_count, 1)

    def test_ocr_failure_is_silent_and_never_calls_the_model(self):
        with patch.object(main, "extract_text_from_file", side_effect=RuntimeError("mock OCR failure")):
            _, events, result = self.analyze()
        self.assertEqual(result["extraction"]["extraction_status"], "failed")
        self.assertEqual(result["extraction"]["content"], "")
        self.assertEqual(result["study_period"], "first")
        self.classify.assert_not_awaited()
        self.assertFalse(any(event["type"] in ("error", "failed", "ai_started") for event in events))
        response = self.upload(result)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store.contents[self.store._files[0]["id"]], "")
        self.index.assert_not_awaited()

    def test_system_period_overrides_document_and_model_period(self):
        self.suggestions["study_period"] = "third"
        _, _, result = self.analyze()
        self.assertEqual(result["study_period"], "first")

    def test_final_manual_values_and_empty_notes_are_saved_without_second_ocr(self):
        _, _, result = self.analyze()
        response = self.upload(result, subject="physics", notes="")
        self.assertEqual(response.status_code, 200)
        record = self.store._files[0]
        self.assertEqual(record["subject"], "physics")
        self.assertEqual(record["study_period"], "third")
        self.assertEqual(record["notes"], "")
        self.assertEqual(record["original_sha256"], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual(len(list(self.uploads.iterdir())), 1)
        self.assertEqual(self.extraction_calls, [("lesson.txt", False)])
        self.assertEqual(self.store.contents[record["id"]], self.raw.decode("utf-8"))

    def test_all_required_metadata_and_other_custom_name_are_enforced_on_server(self):
        for field in ("audience", "study_period", "subject", "material_type"):
            with self.subTest(field=field):
                response = self.upload(**{field: ""})
                self.assertEqual(response.status_code, 422)
        self.assertEqual(self.upload(material_type="other", material_custom="").status_code, 422)
        self.assertEqual(self.upload(subject="unknown").status_code, 422)
        self.assertEqual(self.upload(subject="", quick_upload="true").status_code, 422)
        self.assertEqual(self.store._files, [])
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_notes_accept_five_characters_and_reject_six_without_saving(self):
        _, _, result = self.analyze()
        response = self.upload(result, notes="有機物複習")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.store._files[0]["notes"], "有機物複習")
        rejected = self.upload(result, notes="有機化學複習", request_id="upload-request-002")
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(len(self.store._files), 1)
        self.assertEqual(len(list(self.uploads.iterdir())), 1)
        self.assertEqual(len(self.extraction_calls), 1)

    def test_tampered_text_or_wrong_original_is_rejected_without_disk_write(self):
        _, _, result = self.analyze()
        tampered = {**result, "extraction": {**result["extraction"], "content": "forged text"}}
        self.assertEqual(self.upload(tampered).status_code, 422)
        self.assertEqual(self.upload(result, raw=b"other original").status_code, 422)
        self.assertEqual(self.store._files, [])
        self.assertEqual(list(self.uploads.iterdir()), [])

    def test_duplicate_hint_never_blocks_another_explicit_upload(self):
        _, _, first = self.analyze()
        self.assertEqual(self.upload(first).status_code, 200)
        _, _, second = self.analyze()
        self.assertTrue(second["possible_duplicate"])
        self.assertEqual(self.upload(second, request_id="upload-request-002").status_code, 200)
        self.assertEqual(len(self.store._files), 2)
        self.assertEqual(len(list(self.uploads.iterdir())), 2)

    def test_private_files_of_other_users_do_not_trigger_duplicate_hint(self):
        self.store._files.append({"id": "hidden", "audience": "personal", "owner": "other-user",
                                  "original_sha256": hashlib.sha256(self.raw).hexdigest()})
        _, _, result = self.analyze()
        self.assertFalse(result["possible_duplicate"])

    def test_same_request_reuses_saved_result_but_changed_file_is_rejected(self):
        _, _, result = self.analyze()
        first = self.upload(result)
        second = self.upload(result)
        self.assertEqual(first.json()["data"]["file_id"], second.json()["data"]["file_id"])
        self.assertEqual(len(self.store._files), 1)
        self.assertEqual(len(list(self.uploads.iterdir())), 1)
        self.assertEqual(self.upload(raw=b"different").status_code, 409)

    def test_save_failure_removes_original_and_returns_no_success(self):
        _, _, result = self.analyze()
        self.store.fail_save = True
        response = self.upload(result)
        self.assertEqual(response.status_code, 500)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual(self.store._files, [])
        self.index.assert_not_awaited()

    def test_no_proof_after_analysis_transport_failure_saves_manual_metadata_without_ocr(self):
        response = self.upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.extraction_calls, [])
        self.assertEqual(self.store._files[0]["extraction_status"], "failed")

    def test_anonymous_analysis_and_upload_require_authentication(self):
        for path in ("/api/upload/analyze", "/api/upload?audience=personal"):
            response = self.client.post(path, files={"file": ("lesson.txt", self.raw)})
            self.assertEqual(response.status_code, 401)
        self.assertEqual(self.store._files, [])
        self.classify.assert_not_awaited()


class UploadPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_store_rolls_back_when_disk_write_fails(self):
        store = main.DataStore.__new__(main.DataStore)
        store._sessions, store._conversations, store._files, store._file_contents = {}, {}, [], {}
        store._lock, store._persist_lock = asyncio.Lock(), asyncio.Lock()
        with patch.object(store, "_write_json", side_effect=OSError("mock disk failure")), patch("main.logging.error"):
            with self.assertRaises(OSError):
                await store.add_file({"id": "new-file"}, "content")
        self.assertEqual(store._files, [])
        self.assertEqual(store._file_contents, {})

    async def test_atomic_write_preserves_previous_file_when_encoding_fails(self):
        with tempfile.TemporaryDirectory(prefix="hk_upload_persistence_") as folder:
            target = Path(folder) / "records.json"
            target.write_text('{"previous":true}', encoding="utf-8")
            store = main.DataStore.__new__(main.DataStore)
            with patch.object(main, "CONVERSATIONS_FILE", str(target)):
                with self.assertRaises(TypeError):
                    store._write_json({"invalid": object()})
            self.assertEqual(target.read_text(encoding="utf-8"), '{"previous":true}')
            self.assertEqual(list(Path(folder).iterdir()), [target])


if __name__ == "__main__":
    unittest.main()
