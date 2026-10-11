"""Offline tests for authorized, read-only views of already indexed files."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from services.document_catalog import MAX_TEXT_CHARS, build_indexed_file_entries


IMAGE_ID = "4f54c50f-51e4-4715-8291-3461ed570144"
PDF_ID = "6778419e-9064-4dde-8aa8-a7d99d2c1191"
TEXT_ID = "b8648b64-426e-4272-8966-109851db1bb4"


def chunk(document_id: object, *, filename: str = "课堂资料.docx", page: int = 1,
          offset: int = 0, content: str = "真实索引原文", audience: object = "school",
          owner: str = "", chunk_id: str = "chunk") -> dict:
    return {"chunk_id": chunk_id, "content": content,
            "metadata": {"document_id": document_id, "filename": filename,
                         "page": page, "char_start": offset,
                         "audience": audience, "owner": owner}}


class Collection:
    def __init__(self, chunks: list[dict]):
        self.raw = {"ids": [row["chunk_id"] for row in chunks],
                    "documents": [row["content"] for row in chunks],
                    "metadatas": [row["metadata"] for row in chunks]}
        self.calls: list[dict] = []

    def get(self, **kwargs: object) -> dict:
        self.calls.append(kwargs)
        return copy.deepcopy(self.raw)


class Store:
    def __init__(self, chunks: list[dict]):
        self._collection = Collection(chunks)


class DocumentCatalogTests(unittest.TestCase):
    def test_indexed_image_is_same_id_deduplicated_and_has_no_large_text(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            root = Path(temporary)
            (root / f"{IMAGE_ID}_课堂截图.png").write_bytes(b"fake-image-fixture")
            store = Store([chunk(IMAGE_ID, filename="课堂截图.png", chunk_id="one"),
                           chunk(IMAGE_ID, filename="课堂截图.png", page=2, chunk_id="two"),
                           chunk("known-document", chunk_id="already-visible")])
            before = copy.deepcopy(store._collection.raw)
            entries = build_indexed_file_entries(store, root, {"known-document"},
                                                 can_access=lambda meta: True,
                                                 inline_contents={IMAGE_ID: "甲" * 300_000})
            self.assertEqual(len(entries), 1)
            result = entries[0]
            self.assertEqual(result["id"], IMAGE_ID)
            self.assertEqual(result["name"], "课堂截图.png")
            self.assertEqual(result["original_filename"], "课堂截图.png")
            self.assertTrue(result["has_original"])
            self.assertTrue(result["indexed_only"])
            self.assertEqual(result["size"], len(b"fake-image-fixture"))
            self.assertEqual(result["page_count"], 2)
            self.assertEqual(result["extracted_text"], "")
            self.assertEqual(result["extraction_status"], "success")
            self.assertEqual(store._collection.raw, before)
            self.assertEqual(store._collection.calls, [{"include": ["metadatas", "documents"]}])

    def test_missing_or_unknown_permission_and_missing_id_are_ignored(self) -> None:
        rows = [chunk("allowed"), chunk("unknown", audience="unknown"),
                chunk("none", audience=None), chunk("malformed", audience=[]),
                chunk("no-owner", audience="personal"), chunk(None), chunk("")]
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            result = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True)
        self.assertEqual([entry["id"] for entry in result], ["allowed"])

    def test_inline_record_cannot_override_index_permissions_and_all_chunks_must_pass(self) -> None:
        rows = [chunk("private", audience="personal", owner="other-owner"),
                chunk("conflict", chunk_id="public"),
                chunk("conflict", audience="personal", owner="other-owner", chunk_id="private"),
                chunk("unknown-chunk", chunk_id="known-permission"),
                chunk("unknown-chunk", audience=None, chunk_id="unknown-permission"),
                chunk("mine", audience="personal", owner="current-owner")]
        can_access = lambda meta: meta["audience"] == "school" or meta.get("owner") == "current-owner"
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            result = build_indexed_file_entries(Store(rows), temporary, [], can_access=can_access,
                                                inline_records=[{"id": "private", "audience": "school"},
                                                                {"id": "mine", "audience": "school"}])
        self.assertEqual([entry["id"] for entry in result], ["mine"])
        self.assertEqual(result[0]["audience"], "personal")

    def test_text_chunks_follow_page_and_offset_order(self) -> None:
        rows = [chunk(TEXT_ID, page=2, offset=0, content="页2段1", chunk_id="last"),
                chunk(TEXT_ID, page=1, offset=20, content="页1段2", chunk_id="middle"),
                chunk(TEXT_ID, page=1, offset=0, content="页1段1", chunk_id="first")]
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            result = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True)[0]
        self.assertFalse(result["has_original"])
        self.assertEqual(result["extracted_text"], "页1段1\n\n页1段2\n\n页2段1")
        self.assertEqual(result["page_count"], 2)

    def test_cached_inline_text_and_original_name_are_preferred_and_bounded(self) -> None:
        row = chunk(TEXT_ID, filename="存储名称.docx")
        row["metadata"]["subject"] = "物理"
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            (Path(temporary) / f"{TEXT_ID}_存储名称.docx").write_bytes(b"fake-office-fixture")
            result = build_indexed_file_entries(Store([row]), temporary, [], can_access=lambda meta: True,
                                                inline_records=[{"id": TEXT_ID, "name": "stored_name.docx",
                                                                 "original_name": "原始名称.docx", "subject": "数学",
                                                                 "uploaded_at": "2026-10-03T10:00:00", "size": 999}],
                                                inline_contents={TEXT_ID: "缓存" * 150_000})[0]
        self.assertEqual(result["name"], "原始名称.docx")
        self.assertEqual(result["extracted_text"], ("缓存" * 150_000)[:MAX_TEXT_CHARS])
        self.assertEqual(result["uploaded_at"], "2026-10-03T10:00:00")
        self.assertEqual(result["subject"], "物理")
        self.assertTrue(result["has_original"])
        self.assertEqual(result["size"], len(b"fake-office-fixture"))

    def test_joined_chunk_text_is_bounded_including_separators(self) -> None:
        rows = [chunk(TEXT_ID, content="甲" * 120_000, chunk_id="one"),
                chunk(TEXT_ID, content="乙" * 120_000, page=2, chunk_id="two")]
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            result = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True)[0]
        self.assertEqual(len(result["extracted_text"]), MAX_TEXT_CHARS)
        self.assertTrue(result["extracted_text"].startswith("甲" * 120_000 + "\n\n"))

    def test_pdf_without_original_keeps_cached_or_indexed_text_fallback(self) -> None:
        rows = [chunk(PDF_ID, filename="原电池.pdf", content="化学能转化为电能")]
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            indexed = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True)[0]
            cached = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True,
                                                inline_contents={PDF_ID: "原文件已缓存的完整文字"})[0]
            (Path(temporary) / f"{PDF_ID}_原电池.pdf").write_bytes(b"fake-pdf-fixture")
            original = build_indexed_file_entries(Store(rows), temporary, [], can_access=lambda meta: True,
                                                  inline_contents={PDF_ID: "原文件已缓存的完整文字"})[0]
        self.assertFalse(indexed["has_original"])
        self.assertEqual(indexed["extracted_text"], "化学能转化为电能")
        self.assertEqual(cached["extracted_text"], "原文件已缓存的完整文字")
        self.assertTrue(original["has_original"])
        self.assertEqual(original["extracted_text"], "")

    def test_permission_checker_error_fails_closed(self) -> None:
        def unavailable(meta: dict) -> bool:
            raise RuntimeError("permission-fixture-detail")
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            result = build_indexed_file_entries(Store([chunk(TEXT_ID)]), temporary, [], can_access=unavailable)
        self.assertEqual(result, [])

    def test_symlink_outside_uploads_is_not_exposed_as_an_original(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hk_catalog_tests_") as temporary:
            root = Path(temporary)
            uploads = root / "uploads"
            uploads.mkdir()
            outside = root / "outside.txt"
            outside.write_text("outside-fixture", encoding="utf-8")
            try:
                (uploads / f"{TEXT_ID}_课堂资料.txt").symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("Creating symlinks is unavailable on this host")
            result = build_indexed_file_entries(Store([chunk(TEXT_ID, filename="课堂资料.txt")]),
                                                uploads, [], can_access=lambda meta: True)[0]
            self.assertFalse(result["has_original"])
            self.assertNotIn("outside-fixture", result["extracted_text"])


if __name__ == "__main__":
    unittest.main()
