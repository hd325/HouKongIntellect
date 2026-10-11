"""Offline review/index isolation contracts with temporary source files only."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from scripts import reindex_all
from services.knowledge.vector_store import VectorStore
from services.review_policy import is_published_record


PENDING_ID = "4f54c50f-51e4-4715-8291-3461ed570144"
REJECTED_ID = "6778419e-9064-4dde-8aa8-a7d99d2c1191"
PERSONAL_ID = "b8648b64-426e-4272-8966-109851db1bb4"
LEGACY_ID = "f39f8dda-a600-45a8-82b2-a8c2a015defc"


class ReviewIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="hk_review_index_")
        self.root = Path(self.temporary.name)
        self.uploads = self.root / "uploads"
        self.uploads.mkdir()
        self.legacy = self.root / "files_data.json"
        self.current = self.root / "conversations_data.json"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_catalog(self, path: Path, records: list[dict], contents: dict | None = None) -> None:
        path.write_text(json.dumps({"files": records, "file_contents": contents or {}}), encoding="utf-8")

    def vector(self) -> VectorStore:
        # Bypass __init__: no PersistentClient, persisted store or real embedder.
        vector = object.__new__(VectorStore)
        vector._collection = SimpleNamespace(upsert=Mock())
        vector._embedder = SimpleNamespace(name="fake", embed=Mock(side_effect=lambda docs: [[0.5] for _ in docs]))
        vector._index_revision = 0
        return vector

    def test_policy_preserves_legacy_and_rejects_every_explicit_unpublished_state(self) -> None:
        self.assertTrue(is_published_record({"id": LEGACY_ID}))
        self.assertTrue(is_published_record({"review_status": "published"}))
        for status in ("pending", "rejected", "", None, "PUBLISHED", "unknown"):
            with self.subTest(status=status):
                self.assertFalse(is_published_record({"review_status": status}))

    def test_current_catalog_overrides_old_status_scope_and_text(self) -> None:
        self.write_catalog(self.legacy, [
            {"id": PENDING_ID, "name": "旧名称.txt", "review_status": "published", "audience": "school"},
            {"id": REJECTED_ID, "name": "旧退回.txt"},
            {"id": PERSONAL_ID, "name": "旧个人.txt", "owner": "old-owner", "audience": "school", "grade": "高二"},
            {"id": LEGACY_ID, "name": "历史.txt"},
        ], {PENDING_ID: "旧待审内容", REJECTED_ID: "旧退回内容", PERSONAL_ID: "旧个人内容", LEGACY_ID: "历史内容"})
        self.write_catalog(self.current, [
            {"id": PENDING_ID, "name": "待审.txt", "review_status": "pending"},
            {"id": REJECTED_ID, "name": "退回.txt", "review_status": "rejected"},
            {"id": PERSONAL_ID, "name": "个人.txt", "owner": "student-1", "audience": "personal",
             "subject": "数学", "review_status": "published", "publisher_role": "student",
             "audience_class": "2A", "audience_grade": "高二"},
        ], {PERSONAL_ID: "当前个人内容"})
        documents, skipped = reindex_all.load_inline_documents(self.legacy, conversations_path=self.current)
        self.assertEqual({document.document_id for document in documents}, {PERSONAL_ID, LEGACY_ID})
        personal = next(document for document in documents if document.document_id == PERSONAL_ID)
        self.assertEqual(personal.content, "当前个人内容")
        self.assertEqual(personal.file_info, {
            "id": PERSONAL_ID, "name": "个人.txt", "owner": "student-1", "audience": "personal",
            "subject": "数学", "grade": "高二", "review_status": "published", "publisher_role": "student",
            "audience_class": "2A", "audience_grade": "高二",
        })
        self.assertTrue(any("[pending]" in reason for reason in skipped))
        self.assertTrue(any("[rejected]" in reason for reason in skipped))

        # An old published disk copy cannot bypass current pending/rejected state.
        for document_id in (PENDING_ID, REJECTED_ID):
            (self.uploads / f"{document_id}_旧原件.txt").write_text("stale", encoding="utf-8")
        metadata, _ = reindex_all.load_catalog_data(self.legacy, self.current)
        extractor = Mock(return_value={"content": "不能发布", "extraction_status": "success"})
        upload_documents, upload_skipped, _ = reindex_all.load_upload_documents(
            self.uploads, use_cache=False, extractor=extractor, metadata=metadata
        )
        self.assertEqual(upload_documents, [])
        self.assertEqual(len(upload_skipped), 2)
        extractor.assert_not_called()

    def test_upload_scan_skips_unpublished_before_extraction_and_preserves_personal_metadata(self) -> None:
        metadata = {
            PENDING_ID: {"review_status": "pending"},
            REJECTED_ID: {"review_status": "rejected"},
            PERSONAL_ID: {"name": "个人正式名称.txt", "owner": "student-1", "audience": "personal",
                          "subject": "数学", "review_status": "published", "publisher_role": "student",
                          "audience_class": "2A", "audience_grade": "高二"},
        }
        for document_id in (PENDING_ID, REJECTED_ID, PERSONAL_ID, LEGACY_ID):
            (self.uploads / f"{document_id}_磁盘名称.txt").write_text("fixture", encoding="utf-8")
        pending_folder = self.root / "review_uploads"
        pending_folder.mkdir()
        (pending_folder / "秘密.txt").write_text("pending-fixture", encoding="utf-8")
        extractor = Mock(return_value={"content": "磁盘文本", "extraction_status": "success"})
        documents, skipped, hits = reindex_all.load_upload_documents(
            self.uploads, use_cache=False, extractor=extractor, metadata=metadata
        )
        self.assertEqual(extractor.call_count, 2)
        self.assertEqual(hits, 0)
        self.assertEqual({document.document_id for document in documents}, {PERSONAL_ID, LEGACY_ID})
        self.assertEqual(len(skipped), 2)
        personal = next(document for document in documents if document.document_id == PERSONAL_ID)
        self.assertEqual(personal.file_info["audience"], "personal")
        self.assertEqual(personal.file_info["owner"], "student-1")
        self.assertEqual(personal.file_info["name"], "个人正式名称.txt")
        self.assertEqual(personal.file_info["audience_class"], "2A")
        self.assertEqual(personal.file_info["publisher_role"], "student")
        self.assertEqual(reindex_all.load_upload_documents(pending_folder, extractor=extractor), ([], [], 0))
        self.assertEqual(extractor.call_count, 2)

    def test_single_and_bulk_indexing_never_embed_unpublished_text(self) -> None:
        vector = self.vector()
        for status in ("pending", "rejected"):
            self.assertEqual(vector.index_document({"id": PENDING_ID, "review_status": status}, "待审原文"), 0)
        self.assertEqual(vector.bulk_index_documents([
            ({"id": PENDING_ID, "review_status": "pending"}, "待审原文"),
            ({"id": REJECTED_ID, "review_status": "rejected"}, "退回原文"),
        ]), 0)
        vector._embedder.embed.assert_not_called()
        vector._collection.upsert.assert_not_called()

    def test_index_metadata_keeps_scope_and_teacher_label_in_single_and_bulk_paths(self) -> None:
        vector = self.vector()
        info = {"id": PERSONAL_ID, "name": "老师资料.txt", "audience": "class", "owner": "fixture-teacher-0",
                "subject": "数学", "grade": "高二", "review_status": "published", "publisher_role": "teacher",
                "audience_class": "2A", "audience_grade": "高二"}
        self.assertEqual(vector.index_document(info, "教学原文"), 1)
        single_meta = vector._collection.upsert.call_args.kwargs["metadatas"][0]
        for key in ("owner", "audience", "subject", "grade", "review_status", "publisher_role", "audience_class", "audience_grade"):
            self.assertEqual(single_meta[key], info[key])
        self.assertEqual(vector.bulk_index_documents([(info, "教学原文")]), 1)
        self.assertEqual(vector._collection.upsert.call_args.kwargs["metadatas"][0], single_meta)
        self.assertEqual(vector.index_document({"id": LEGACY_ID, "name": "旧资料.txt"}, "历史原文"), 1)

    def test_rebuild_defensively_removes_stale_pending_chunks_without_indexing_them(self) -> None:
        indexed_ids: list[str] = []
        deleted_ids: list[str] = []
        store = SimpleNamespace(
            _collection=SimpleNamespace(get=lambda **kwargs: {"metadatas": [
                {"document_id": PENDING_ID}, {"document_id": REJECTED_ID}, {"document_id": LEGACY_ID}
            ]}),
            delete_by_document=lambda document_id: deleted_ids.append(document_id) or 1,
            index_document=lambda file_info, content: indexed_ids.append(file_info["id"]) or 1,
        )
        documents = [
            reindex_all.SourceDocument({"id": PENDING_ID, "review_status": "pending"}, "待审原文", "fake"),
            reindex_all.SourceDocument({"id": REJECTED_ID, "review_status": "rejected"}, "退回原文", "fake"),
            reindex_all.SourceDocument({"id": LEGACY_ID}, "历史原文", "fake"),
        ]
        result = reindex_all.rebuild(store, documents)
        self.assertEqual(indexed_ids, [LEGACY_ID])
        self.assertEqual(result["requested_documents"], 1)
        self.assertIn(PENDING_ID, deleted_ids)
        self.assertIn(REJECTED_ID, deleted_ids)

    def test_run_deduplicates_disk_and_inline_sources_with_inline_text_preferred(self) -> None:
        info = {"id": PERSONAL_ID, "name": "个人.txt", "owner": "student-1", "audience": "personal"}
        inline = reindex_all.SourceDocument(info, "主库原文", "files_data")
        upload = reindex_all.SourceDocument(info, "磁盘原文", "uploads")
        store = SimpleNamespace(flush=lambda: True)
        with patch("services.knowledge.vector_store.get_vector_store", return_value=store), \
             patch.object(reindex_all, "load_inline_documents", return_value=([inline], [])), \
             patch.object(reindex_all, "load_catalog_data", return_value=({}, {})), \
             patch.object(reindex_all, "load_upload_documents", return_value=([upload], [], 0)), \
             patch.object(reindex_all, "rebuild", return_value={"failures": []}) as rebuild, \
             patch.object(reindex_all, "verify", return_value={"missing_document_ids": [], "orphan_document_ids": []}):
            result = reindex_all.run()
        self.assertEqual(rebuild.call_args.args[1], [inline])
        self.assertEqual(result["sources"]["union_indexable"], 1)

    def test_unpublished_metadata_cannot_be_overridden_by_a_published_duplicate_source(self) -> None:
        published = reindex_all.SourceDocument({"id": PENDING_ID, "review_status": "published"}, "旧发布原文", "uploads")
        pending = reindex_all.SourceDocument({"id": PENDING_ID, "review_status": "pending"}, "待审原文", "files_data")
        self.assertEqual(reindex_all.combine_source_documents([published], [pending]), [])
        self.assertEqual(reindex_all.combine_source_documents([pending], [published]), [])


if __name__ == "__main__":
    unittest.main()
