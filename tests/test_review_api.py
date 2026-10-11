"""Isolated approval integration tests; no real records, model or vector writes."""
from __future__ import annotations

import asyncio
import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from tests.account_fixtures import fixture_accounts


class ReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="hk_review_api_")
        self.root = Path(self.folder.name)
        self.uploads = self.root / "uploads"
        self.pending = self.root / "review_uploads"
        self.uploads.mkdir()
        self.pending.mkdir()
        self.records_path = self.root / "conversations.json"
        self.sessions_path = self.root / "sessions.json"
        self.index = AsyncMock()
        self.vector_chunks = {}
        self.vector = SimpleNamespace(
            _collection=SimpleNamespace(get=Mock(return_value={"ids": [], "documents": [], "metadatas": []})),
            get_chunk=Mock(side_effect=lambda chunk_id: self.vector_chunks.get(chunk_id)),
            get_chunk_context=Mock(side_effect=lambda chunk_id: (
                {"target": self.vector_chunks[chunk_id], "context_before": [], "context_after": []} if chunk_id in self.vector_chunks else None)),
        )
        self.patches = [
            patch.object(main, "DATA_DIR", str(self.root)),
            patch.object(main, "UPLOAD_DIR", str(self.uploads)),
            patch.object(main, "REVIEW_UPLOAD_DIR", str(self.pending), create=True),
            patch.object(main, "CONVERSATIONS_FILE", str(self.records_path)),
            patch.object(main, "SESSIONS_FILE", str(self.sessions_path)),
            patch.object(main, "_index_file_background", self.index),
            patch.object(main, "extract_text_from_file", self.extract),
            patch("services.knowledge.vector_store.get_vector_store", return_value=self.vector),
            patch("main.logging.info"), patch("main.logging.warning"), patch("main.logging.error"),
            patch.dict(main.VALID_ACCOUNTS, fixture_accounts(), clear=True),
        ]
        for item in self.patches:
            item.start()
        self.store = main.DataStore()
        self.store._sessions = {
            "review-owner-session": self.session("review-student"),
            "review-other-session": self.session("review-other"),
            "review-teacher-session": self.session("fixture-teacher-0"),
            "review-prefix-session": self.session("fixture-prefix-student"),
            # Client-controlled/stale session profile must never grant authority.
            "review-forged-session": {**self.session("review-student"), "user_info": {"role": "老師", "is_teacher": True}},
        }
        self.store_patch = patch.object(main, "data_store", self.store)
        self.store_patch.start()
        # Do not enter app lifespan: it initializes the real knowledge service.
        self.client = TestClient(main.app)
        self.owner = {"X-Session-ID": "review-owner-session"}
        self.other = {"X-Session-ID": "review-other-session"}
        self.teacher = {"X-Session-ID": "review-teacher-session"}
        self.raw = "化學測試原件：原電池將化學能轉化為電能。".encode("utf-8")

    def tearDown(self):
        self.client.close()
        self.store_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    @staticmethod
    def session(account):
        return {"account": account, "created_at": datetime.now().isoformat()}

    @staticmethod
    def extract(file, **kwargs):
        return {"content": file.file.read().decode("utf-8"), "page_count": 1,
                "extraction_status": "success", "error": ""}

    def upload(self, audience="school", headers=None, filename="lesson.txt", **params):
        return self.client.post(
            "/api/upload", headers=headers or self.owner,
            params={"audience": audience, "subject": "chemistry", "study_period": "first",
                    "material_type": "review_paper", **params},
            files={"file": (filename, self.raw, "text/plain")},
        )

    def submit(self, **kwargs):
        response = self.upload(**kwargs)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["data"]["file_id"]

    def record(self, file_id):
        return next(item for item in self.store._files if item["id"] == file_id)

    def reviews(self, headers=None):
        response = self.client.get("/api/reviews", headers=headers or self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()["data"]
        self.assertIn("is_teacher", data)
        self.assertIn("reviewing", data)
        self.assertIn("published", data)
        return data

    def decision(self, file_id, action="approve", headers=None, reason=""):
        return self.client.post(f"/api/reviews/{file_id}/decision", headers=headers or self.teacher,
                                json={"action": action, "reason": reason})

    def file_ids(self, headers=None):
        response = self.client.get("/api/files", headers=headers or self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        return {item["id"] for item in response.json()["data"]["files"]}

    def test_four_registered_teacher_accounts_login_without_credentials_in_response_or_session(self):
        for account in ("fixture-teacher-0", "fixture-teacher-1", "fixture-teacher-2", "fixture-teacher-3"):
            with self.subTest(account=account):
                response = self.client.post("/api/login", json={"account": account, "password": "fixture-only-password"})
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()["data"]
                self.assertEqual(data["user_info"]["account"], account)
                self.assertTrue(data["user_info"]["is_teacher"])
                self.assertEqual(data["user_info"]["role"], "老師")
                for value in (data, self.store._sessions[data["session_id"]]):
                    text = json.dumps(value)
                    self.assertNotIn("fixture-only-password", text)
                    self.assertFalse(any(word in text.lower() for word in ("password", "password_hash")))
        self.assertEqual(self.client.post("/api/login", json={"account": "fixture-teacher-0", "password": "wrong"}).status_code, 401)
        self.assertEqual(self.client.post("/api/login", json={"account": "fixture-teacher-4", "password": "fixture-only-password"}).status_code, 401)

    def test_student_shared_original_is_isolated_and_pending_is_absent_from_formal_lists(self):
        for audience in ("class", "grade", "school"):
            with self.subTest(audience=audience):
                file_id = self.submit(audience=audience)
                record = self.record(file_id)
                self.assertEqual(record["review_status"], "pending")
                self.assertEqual(record["publisher_role"], "student")
                self.assertEqual(record["owner"], "review-student")
                for headers in (self.owner, self.other, self.teacher):
                    self.assertNotIn(file_id, self.file_ids(headers))
        self.assertEqual(len(list(self.pending.iterdir())), 3)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.assertEqual({path.read_bytes() for path in self.pending.iterdir()}, {self.raw})
        self.index.assert_not_awaited()

    def test_personal_and_teacher_school_upload_publish_immediately(self):
        personal_id = self.submit(audience="personal")
        teacher_id = self.submit(headers=self.teacher)
        self.assertEqual(self.record(personal_id)["review_status"], "published")
        self.assertEqual(self.record(teacher_id)["review_status"], "published")
        self.assertEqual(self.record(teacher_id)["publisher_role"], "teacher")
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(len(list(self.uploads.iterdir())), 2)
        self.assertIn(personal_id, self.file_ids())
        self.assertNotIn(personal_id, self.file_ids(self.other))
        self.assertIn(teacher_id, self.file_ids(self.other))
        self.assertEqual(self.index.await_count, 2)

    def test_teacher_class_target_preserves_shared_scope(self):
        self.assertEqual(self.upload(audience="class", headers=self.teacher).status_code, 422)
        self.assertEqual(self.upload(audience="grade", headers=self.teacher, target_class="missing-class").status_code, 422)
        file_id = self.submit(audience="class", headers=self.teacher, target_class="高二9班")
        self.assertEqual(self.record(file_id)["review_status"], "published")
        self.assertIn(file_id, self.file_ids(self.owner))
        self.assertIn(file_id, self.file_ids(self.other))
        self.assertNotIn(file_id, self.file_ids({"X-Session-ID": "review-prefix-session"}))

    def test_student_cannot_use_prefix_query_or_forged_session_profile_to_publish_or_review(self):
        for session_id in ("review-owner-session", "review-prefix-session", "review-forged-session"):
            with self.subTest(session=session_id):
                headers = {"X-Session-ID": session_id}
                file_id = self.submit(headers=headers, is_teacher="true", role="teacher", publisher_role="teacher")
                self.assertEqual(self.record(file_id)["review_status"], "pending")
                self.assertEqual(self.record(file_id)["publisher_role"], "student")
                self.assertEqual(self.decision(file_id, headers=headers).status_code, 403)
                self.assertFalse(self.reviews(headers)["is_teacher"])
        self.index.assert_not_awaited()

    def test_review_queue_is_owner_scoped_for_students_and_complete_for_teacher(self):
        owned = self.submit()
        another = self.submit(headers=self.other)
        private = self.submit(audience="personal", headers=self.other)
        teacher_file = self.submit(headers=self.teacher)
        student_data = self.reviews()
        self.assertFalse(student_data["is_teacher"])
        self.assertEqual({item["id"] for item in student_data["reviewing"]}, {owned})
        student_published = {item["id"] for item in student_data["published"]}
        self.assertIn(teacher_file, student_published)
        self.assertNotIn(private, student_published)
        teacher_data = self.reviews(self.teacher)
        self.assertTrue(teacher_data["is_teacher"])
        self.assertEqual({item["id"] for item in teacher_data["reviewing"]}, {owned, another})
        self.assertNotIn(private, {item["id"] for item in teacher_data["published"]})

    def test_pending_preview_and_original_download_allow_only_owner_or_teacher(self):
        file_id = self.submit()
        for endpoint in ("preview", "download"):
            for headers in (self.owner, self.teacher):
                response = self.client.get(f"/api/files/{file_id}/{endpoint}", headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                if endpoint == "download":
                    self.assertEqual(response.content, self.raw)
                else:
                    self.assertEqual(response.json()["data"]["content"], self.raw.decode("utf-8"))
            self.assertEqual(self.client.get(f"/api/files/{file_id}/{endpoint}", headers=self.other).status_code, 403)
            self.assertEqual(self.client.get(f"/api/files/{file_id}/{endpoint}").status_code, 401)

    def test_approval_moves_original_preserves_owner_and_indexes_only_once(self):
        file_id = self.submit()
        before = copy.deepcopy(self.record(file_id))
        response = self.decision(file_id)
        self.assertEqual(response.status_code, 200, response.text)
        record = self.record(file_id)
        self.assertEqual(record["review_status"], "published")
        for key in ("owner", "audience", "subject", "original_sha256", "publisher_role"):
            self.assertEqual(record[key], before[key])
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(len(list(self.uploads.iterdir())), 1)
        self.assertEqual(next(self.uploads.iterdir()).read_bytes(), self.raw)
        self.assertIn(file_id, self.file_ids(self.other))
        self.index.assert_awaited_once()
        repeat = self.decision(file_id)
        self.assertIn(repeat.status_code, (200, 409))
        self.assertEqual(len(self.store._files), 1)
        self.assertEqual(len(list(self.uploads.iterdir())), 1)
        self.index.assert_awaited_once()

    def test_reject_requires_reason_and_only_owner_can_resubmit(self):
        file_id = self.submit()
        self.assertEqual(self.decision(file_id, "reject", reason="  ").status_code, 422)
        self.assertEqual(self.record(file_id)["review_status"], "pending")
        self.assertEqual(self.decision(file_id, "invalid").status_code, 422)
        reason = "請刪去資料中的個人資料"
        response = self.decision(file_id, "reject", reason=reason)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.record(file_id)["review_status"], "rejected")
        self.assertIn(reason, json.dumps(self.reviews(), ensure_ascii=False))
        self.assertNotIn(file_id, self.file_ids(self.other))
        for headers in (self.other, self.teacher):
            self.assertEqual(self.client.post(f"/api/reviews/{file_id}/resubmit", headers=headers).status_code, 403)
        response = self.client.post(f"/api/reviews/{file_id}/resubmit", headers=self.owner)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.record(file_id)["review_status"], "pending")
        self.assertEqual(len(list(self.pending.iterdir())), 1)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.index.assert_not_awaited()

    def test_upload_persistence_failure_removes_original_and_rolls_back_record(self):
        with patch.object(self.store, "_write_json", side_effect=OSError("fixture disk error")):
            response = self.upload()
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.store._files, [])
        self.assertEqual(self.store._file_contents, {})
        self.assertEqual(list(self.pending.iterdir()), [])
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.index.assert_not_awaited()

    def test_approval_persistence_failure_restores_pending_file_and_record(self):
        file_id = self.submit()
        before = copy.deepcopy(self.record(file_id))
        saved_before = self.records_path.read_bytes()
        with patch.object(self.store, "_write_json", side_effect=OSError("fixture disk error")):
            response = self.decision(file_id)
        self.assertEqual(response.status_code, 500, response.text)
        self.assertEqual(self.record(file_id), before)
        self.assertEqual(self.records_path.read_bytes(), saved_before)
        self.assertEqual(len(list(self.pending.iterdir())), 1)
        self.assertEqual(list(self.uploads.iterdir()), [])
        self.index.assert_not_awaited()

    def test_restart_recovers_pending_metadata_content_and_original(self):
        file_id = self.submit()
        reloaded = main.DataStore()
        self.assertEqual(next(item for item in reloaded._files if item["id"] == file_id)["review_status"], "pending")
        self.assertEqual(reloaded._file_contents[file_id], self.raw.decode("utf-8"))
        with patch.object(main, "data_store", reloaded):
            self.assertEqual({item["id"] for item in self.reviews()["reviewing"]}, {file_id})
            response = self.client.get(f"/api/files/{file_id}/download", headers=self.teacher)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.content, self.raw)
        self.index.assert_not_awaited()

    def test_review_list_preview_and_download_are_read_only(self):
        file_id = self.submit()
        def snapshot():
            return ({str(path.relative_to(self.root)): path.read_bytes() for path in self.root.rglob("*") if path.is_file()},
                    copy.deepcopy(self.store._files), copy.deepcopy(self.store._sessions), copy.deepcopy(self.store._file_contents))
        before = snapshot()
        for headers in (self.owner, self.teacher, self.other):
            self.reviews(headers)
            self.file_ids(headers)
            self.client.get(f"/api/files/{file_id}/preview", headers=headers)
            self.client.get(f"/api/files/{file_id}/download", headers=headers)
        self.assertEqual(snapshot(), before)
        self.index.assert_not_awaited()

    def test_review_endpoints_require_login(self):
        file_id = self.submit()
        self.assertEqual(self.client.get("/api/reviews").status_code, 401)
        self.assertEqual(self.client.post(f"/api/reviews/{file_id}/decision", json={"action": "approve"}).status_code, 401)
        self.assertEqual(self.client.post(f"/api/reviews/{file_id}/resubmit").status_code, 401)

    def test_source_chunk_and_document_routes_cannot_bypass_pending_publication(self):
        file_id = self.submit()
        chunk_id = file_id + "_P1_C1"
        self.vector_chunks[chunk_id] = {"chunk_id": chunk_id, "content": "隔離引用內容不得洩漏",
                                         "metadata": {"document_id": file_id, "audience": "school", "review_status": "published"}}
        routes = (f"/api/documents/{file_id}/chunks/{chunk_id}", f"/api/knowledge/chunks/{chunk_id}",
                  f"/api/documents/{file_id}/file")
        for headers in (self.owner, self.other, self.teacher):
            for route in routes:
                with self.subTest(route=route, headers=headers):
                    response = self.client.get(route, headers=headers)
                    self.assertEqual(response.status_code, 403, response.text)
                    self.assertNotIn("隔離引用內容", response.text)
        self.assertEqual(self.decision(file_id).status_code, 200)
        for route in routes:
            response = self.client.get(route, headers=self.other)
            self.assertEqual(response.status_code, 200, response.text)
        mismatch = self.client.get(f"/api/documents/different-id/chunks/{chunk_id}", headers=self.owner)
        self.assertEqual(mismatch.status_code, 404)

    def test_private_chunk_routes_preserve_owner_access_after_direct_upload(self):
        file_id = self.submit(audience="personal")
        chunk_id = file_id + "_P1_C1"
        self.vector_chunks[chunk_id] = {"chunk_id": chunk_id, "content": "學生私人內容",
                                         "metadata": {"document_id": file_id, "audience": "school"}}
        for route in (f"/api/documents/{file_id}/chunks/{chunk_id}", f"/api/knowledge/chunks/{chunk_id}"):
            self.assertEqual(self.client.get(route, headers=self.owner).status_code, 200)
            for headers in (self.other, self.teacher):
                response = self.client.get(route, headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertNotIn("學生私人內容", response.text)

    def test_retrieval_filter_uses_current_publication_and_rejects_missing_vector_permissions(self):
        pending = self.submit()
        rejected = self.submit()
        self.assertEqual(self.decision(rejected, "reject", reason="待修改").status_code, 200)
        private = self.submit(audience="personal", headers=self.other)
        teacher_file = self.submit(headers=self.teacher)
        own_private = self.submit(audience="personal")
        self.vector._collection.get.return_value = {"metadatas": [
            {"document_id": file_id, "audience": "school", "review_status": "published"}
            for file_id in (pending, rejected, private, teacher_file, own_private)
        ] + [
            {"document_id": "legacy-school", "audience": "school"},
            {"document_id": "legacy-private", "audience": "personal", "owner": "review-other"},
            {"document_id": "unpublished-vector", "audience": "school", "review_status": "pending"},
            {"document_id": "no-permission-metadata"},
        ]}
        result = asyncio.run(main.retrieval_permission_filter("review-student"))
        self.assertEqual(set(result["document_id"]["$in"]), {teacher_file, own_private, "legacy-school"})

    def test_approval_missing_original_never_publishes(self):
        file_id = self.submit()
        next(self.pending.iterdir()).unlink()
        response = self.decision(file_id)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.record(file_id)["review_status"], "pending")
        self.assertNotIn(file_id, self.file_ids(self.other))
        self.index.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
