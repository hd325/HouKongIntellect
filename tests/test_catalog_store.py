"""Metadata catalog opens existing collections without initializing an embedder."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import tests  # Establish synthetic-only storage before application imports.
from services.knowledge import vector_store
from services.knowledge.embedder import LocalEmbedder, TfidfEmbedder


class CatalogStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(prefix="hk_catalog_store_")
        self.root = Path(self.folder.name)
        (self.root / "chroma.sqlite3").touch()
        self.client = Mock()
        self.patches = [
            patch.object(vector_store, "CHROMA_DIR", str(self.root)),
            patch.object(vector_store, "_STORE", None),
            patch.object(vector_store, "_CATALOG_STORE", None),
            patch.object(vector_store, "_CATALOG_KEY", None),
            patch.dict("os.environ", {"EMBEDDING_BACKEND": "local"}),
            patch.object(vector_store.chromadb, "PersistentClient", return_value=self.client),
            patch.object(vector_store, "get_embedder", side_effect=AssertionError("Model must stay unloaded")),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    def test_catalog_uses_static_model_identity_without_embedding_or_creating(self):
        catalog = vector_store.get_vector_store(catalog_only=True)
        self.assertIs(catalog._collection, self.client.get_collection.return_value)
        self.client.get_collection.assert_called_once_with(
            name=f"hk_knowledge_{LocalEmbedder.name}_{LocalEmbedder.dim}", embedding_function=None,
        )
        self.client.get_or_create_collection.assert_not_called()
        vector_store.get_embedder.assert_not_called()
        self.assertIsNone(vector_store._STORE)
        self.assertIs(vector_store.get_vector_store(catalog_only=True), catalog)

    def test_missing_database_has_empty_catalog_without_creating_a_directory(self):
        missing = str(self.root / "not-created")
        with patch.object(vector_store, "CHROMA_DIR", missing):
            catalog = vector_store.get_vector_store(catalog_only=True)
        self.assertEqual(catalog._collection.get(), {"ids": [], "documents": [], "metadatas": []})
        self.assertFalse(Path(missing).exists())
        vector_store.chromadb.PersistentClient.assert_not_called()
        self.assertEqual(catalog.stats(), {
            "backend": LocalEmbedder.name, "dim": LocalEmbedder.dim,
            "collection": f"hk_knowledge_{LocalEmbedder.name}_{LocalEmbedder.dim}", "total_chunks": 0,
        })

    def test_catalog_statistics_do_not_initialize_embedder(self):
        self.client.get_collection.return_value.count.return_value = 12
        catalog = vector_store.get_vector_store(catalog_only=True)
        self.assertEqual(catalog.stats(), {
            "backend": LocalEmbedder.name, "dim": LocalEmbedder.dim,
            "collection": f"hk_knowledge_{LocalEmbedder.name}_{LocalEmbedder.dim}", "total_chunks": 12,
        })
        vector_store.get_embedder.assert_not_called()

    def test_missing_collection_remains_empty_and_is_checked_again_later(self):
        self.client.get_collection.side_effect = vector_store.chromadb.errors.NotFoundError("fixture")
        empty = vector_store.get_vector_store(catalog_only=True)
        self.assertEqual(empty._collection.get()["ids"], [])
        self.client.get_or_create_collection.assert_not_called()
        self.client.get_collection.side_effect = None
        actual = vector_store.get_vector_store(catalog_only=True)
        self.assertIs(actual._collection, self.client.get_collection.return_value)
        self.assertEqual(self.client.get_collection.call_count, 3)

    def test_missing_local_collection_preserves_existing_known_fallback_without_probing(self):
        fallback_collection = Mock()
        fallback_collection.get.return_value = {"ids": ["existing-fallback-chunk"]}
        fallback_collection.count.return_value = 1
        fallback = TfidfEmbedder()
        fallback_name = f"hk_knowledge_{fallback.name}_{fallback.dim}"

        def existing_fallback_only(*, name, embedding_function):
            self.assertIsNone(embedding_function)
            if name == fallback_name:
                return fallback_collection
            raise vector_store.chromadb.errors.NotFoundError("fixture")

        self.client.get_collection.side_effect = existing_fallback_only
        with patch.object(TfidfEmbedder, "embed", side_effect=AssertionError("Do not probe")):
            catalog = vector_store.get_vector_store(catalog_only=True)
        self.assertEqual(catalog._collection.get()["ids"], ["existing-fallback-chunk"])
        self.assertEqual(catalog.stats(), {
            "backend": fallback.name, "dim": fallback.dim,
            "collection": fallback_name, "total_chunks": 1,
        })
        self.client.get_or_create_collection.assert_not_called()
        vector_store.get_embedder.assert_not_called()
        self.assertIsNone(vector_store._STORE)

        # An externally created preferred collection immediately replaces this view.
        local_collection = Mock()
        self.client.get_collection.side_effect = None
        self.client.get_collection.return_value = local_collection
        self.assertIs(vector_store.get_vector_store(catalog_only=True)._collection, local_collection)

    def test_initialized_rag_backend_and_revision_override_catalog_view(self):
        vector_store.get_vector_store(catalog_only=True)
        active = SimpleNamespace(_index_revision=7, _collection=Mock())
        vector_store._STORE = active
        self.assertIs(vector_store.get_vector_store(catalog_only=True), active)
        self.assertEqual(vector_store.get_vector_store(catalog_only=True)._index_revision, 7)

    def test_other_backend_configs_keep_existing_probe_and_fallback_factory(self):
        factory = Mock()
        with patch.dict("os.environ", {"EMBEDDING_BACKEND": "auto"}), \
                patch.object(vector_store, "VectorStore", factory):
            self.assertIs(vector_store.get_vector_store(catalog_only=True), factory.return_value)
        factory.assert_called_once_with()

    def test_normal_rag_store_keeps_actual_selected_backend_and_dimensions(self):
        embedder = SimpleNamespace(name="fallback-fixture", dim=123)
        with patch.object(vector_store, "get_embedder", return_value=embedder):
            store = vector_store.get_vector_store()
        self.assertIs(store._embedder, embedder)
        self.client.get_or_create_collection.assert_called_once_with(
            name="hk_knowledge_fallback-fixture_123", metadata={"hnsw:space": "cosine"},
        )


if __name__ == "__main__":
    unittest.main()
