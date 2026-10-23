"""Guard full index resets against deleting a different store or unrelated files."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import Mock, patch

import tests  # Establish synthetic-only storage before application imports.
from services.knowledge.vector_store import ChromaUnavailable, VectorStore


class PublicVectorStoreResetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="houkong_reset_")
        self.base = Path(self.folder.name)
        self.index = self.base / "instance-index"
        self.index.mkdir()
        (self.index / "chroma.sqlite3").write_bytes(b"fake database for isolated reset")
        self.other = self.base / "other-index"
        self.other.mkdir()
        self.sentinel = self.other / "keep.txt"
        self.sentinel.write_text("unrelated store", encoding="utf-8")
        self.store = object.__new__(VectorStore)
        self.store._persist_dir = str(self.index)
        self.store._client = Mock()
        self.store._collection_name = "hk_knowledge_tfidf_2048"
        self.store._index_revision = 7
        self.client = Mock()
        self.sdk = ModuleType("chromadb")
        self.sdk.PersistentClient = Mock(return_value=self.client)
        self.sdk_config = ModuleType("chromadb.config")
        self.sdk_config.Settings = Mock(return_value=object())
        self.module_patch = patch.dict(sys.modules, {"chromadb": self.sdk,
                                                     "chromadb.config": self.sdk_config})
        self.module_patch.start()

    def tearDown(self):
        self.module_patch.stop()
        self.folder.cleanup()

    def test_reset_uses_instance_directory_and_disables_telemetry(self):
        previous = self.store._client
        with patch("services.knowledge.vector_store.CHROMA_DIR", str(self.other)):
            self.store.reset_collection()
        previous.close.assert_called_once_with()
        self.assertTrue(self.sentinel.exists())
        self.assertFalse(self.index.exists())
        self.sdk.PersistentClient.assert_called_once_with(
            path=str(self.index.resolve()), settings=self.sdk_config.Settings.return_value)
        self.sdk_config.Settings.assert_called_once_with(anonymized_telemetry=False)
        self.assertEqual(self.store._index_revision, 8)
        self.client.get_or_create_collection.assert_called_once()

    def test_unrelated_files_stop_reset_before_closing_or_deleting(self):
        valuable = self.index / "accounts.json"
        valuable.write_text("unrelated data", encoding="utf-8")
        with patch("services.knowledge.vector_store.shutil.rmtree") as remove:
            with self.assertRaises(ChromaUnavailable):
                self.store.reset_collection()
            remove.assert_not_called()
        self.store._client.close.assert_not_called()
        self.sdk.PersistentClient.assert_not_called()
        self.assertTrue(valuable.exists())

    def test_missing_chroma_database_is_not_a_reset_target(self):
        (self.index / "chroma.sqlite3").unlink()
        with patch("services.knowledge.vector_store.shutil.rmtree") as remove:
            with self.assertRaises(ChromaUnavailable):
                self.store.reset_collection()
            remove.assert_not_called()
        self.store._client.close.assert_not_called()

    def test_deletion_failure_stops_recreation(self):
        with patch("services.knowledge.vector_store.shutil.rmtree", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.store.reset_collection()
        self.sdk.PersistentClient.assert_not_called()
        self.assertEqual(self.store._index_revision, 7)

    def test_symlink_in_store_is_rejected(self):
        linked = self.index / "chroma.sqlite3-wal"
        try:
            os.symlink(self.sentinel, linked)
        except (OSError, NotImplementedError):
            self.skipTest("This environment does not permit symlink creation")
        with patch("services.knowledge.vector_store.shutil.rmtree") as remove:
            with self.assertRaises(ChromaUnavailable):
                self.store.reset_collection()
            remove.assert_not_called()
        self.assertTrue(self.sentinel.exists())


if __name__ == "__main__":
    unittest.main()
