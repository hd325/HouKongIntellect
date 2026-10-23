"""Refresh regression tests with isolated memory records and a fake index."""
from __future__ import annotations

import asyncio
import copy
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts


class RefreshStore:
    def __init__(self):
        self._deleted_document_ids = set()
        self.delete_session = AsyncMock()
        self.records = []
        self.conversations = []
        self.sessions = {
            name: {"account": name, "created_at": datetime.now().isoformat()}
            for name in ("refresh-owner", "refresh-other")
        }
        self.delete_conversation = AsyncMock()
        self.save_data = AsyncMock()

    async def get_session(self, session_id):
        return self.sessions.get(session_id)

    async def get_user_conversations(self, account):
        return [item for item in self.conversations if item["user_id"] == account]

    async def get_all_files(self, account):
        return [copy.deepcopy(item) for item in self.records
                if main.check_file_permission(item, account)]

    async def list_file_records(self):
        return copy.deepcopy(self.records)

    async def get_file_content(self, file_id):
        return "Uploaded fixture text"


def index_snapshot(*, private_owner="refresh-owner"):
    return {
        "ids": ["public-chunk", "private-chunk"],
        "documents": ["Public fixture", "Private fixture"],
        "metadatas": [
            {"document_id": "public-document", "filename": "public.txt", "audience": "school"},
            {"document_id": "private-document", "filename": "private.txt", "audience": "personal",
             "owner": private_owner},
        ],
    }


class RefreshApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.folder = tempfile.TemporaryDirectory(prefix="hk_refresh_api_")
        self.root = Path(self.folder.name)
        self.store = RefreshStore()
        self.vector = SimpleNamespace(
            _index_revision=0,
            _collection=SimpleNamespace(get=Mock(side_effect=lambda **_: index_snapshot())),
        )
        self.provider = Mock(return_value=self.vector)
        self.patches = [
            patch.object(main, "DATA_DIR", str(self.root)),
            patch.object(main, "UPLOAD_DIR", str(self.root)),
            patch.object(main, "data_store", self.store),
            patch.object(main, "_indexed_catalog_source", None),
            patch.object(main, "_indexed_catalog_snapshot", None),
            patch.object(main, "_indexed_catalog_cached_at", 0.0),
            patch.dict("os.environ", {"EMBEDDING_BACKEND": "local"}),
            patch("services.knowledge.vector_store.get_vector_store", self.provider),
            patch("main.logging.warning"),
        ]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=main.app), base_url="http://refresh-fixture",
        )

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()
        self.folder.cleanup()

    async def files(self, account="refresh-owner"):
        response = await self.client.get("/api/files", headers={"X-Session-ID": account})
        self.assertEqual(response.status_code, 200)
        return {item["id"] for item in response.json()["data"]["files"]}

    async def test_cold_index_initialization_does_not_block_other_requests(self):
        entered, release = threading.Event(), threading.Event()
        loop_thread = threading.get_ident()
        worker_threads = []

        def blocked_initialization(**_kwargs):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise AssertionError("Index initialization blocked the event loop")
            return self.vector

        self.provider.side_effect = blocked_initialization
        pending_files = asyncio.create_task(self.files())
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            response = await asyncio.wait_for(self.client.get(
                "/api/conversations", headers={"X-Session-ID": "refresh-owner"},
            ), timeout=0.5)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(pending_files.done())
            self.assertNotEqual(worker_threads, [loop_thread])
        finally:
            release.set()
            await pending_files

    async def test_cached_snapshot_rechecks_each_users_permission(self):
        self.assertEqual(await self.files(), {"public-document", "private-document"})
        self.assertEqual(await self.files("refresh-other"), {"public-document"})
        self.vector._collection.get.assert_called_once()

    async def test_background_prewarm_reuses_raw_snapshot_for_first_file_request(self):
        await main.warm_file_catalog()
        self.assertEqual(await self.files(), {"public-document", "private-document"})
        self.vector._collection.get.assert_called_once()
        self.provider.assert_called_with(catalog_only=True)

    async def test_startup_and_shutdown_do_not_wait_for_background_catalog_read(self):
        entered, release = threading.Event(), threading.Event()

        def blocked_catalog_read():
            entered.set()
            release.wait(2)

        async def idle_cleanup():
            await asyncio.Event().wait()

        with patch.object(main, "_get_indexed_catalog_view", side_effect=blocked_catalog_read), \
                patch.object(main, "cleanup_expired_sessions", idle_cleanup):
            lifecycle = main.lifespan(main.app)
            await asyncio.wait_for(lifecycle.__aenter__(), timeout=0.5)
            try:
                self.assertTrue(await asyncio.to_thread(entered.wait, 1))
                await asyncio.wait_for(lifecycle.__aexit__(None, None, None), timeout=0.5)
            finally:
                release.set()

    async def test_index_revision_and_external_expiry_refresh_the_snapshot(self):
        self.assertIn("private-document", await self.files())
        self.vector._collection.get.side_effect = lambda **_: index_snapshot(private_owner="refresh-other")
        self.vector._index_revision += 1
        self.assertNotIn("private-document", await self.files())
        self.assertEqual(self.vector._collection.get.call_count, 2)
        main._indexed_catalog_cached_at -= main._INDEXED_CATALOG_TTL
        await self.files()
        self.assertEqual(self.vector._collection.get.call_count, 3)

    async def test_current_review_state_overrides_cached_index_metadata(self):
        self.assertIn("public-document", await self.files("refresh-other"))
        self.store.records.append({
            "id": "public-document", "name": "public.txt", "audience": "school",
            "owner": "refresh-owner", "review_status": "pending",
        })
        self.assertNotIn("public-document", await self.files("refresh-other"))
        self.vector._collection.get.assert_called_once()

    async def test_history_refresh_hides_empty_conversations_without_writes(self):
        created = datetime.now().isoformat()
        self.store.conversations = [
            {"id": "empty", "user_id": "refresh-owner", "created_at": created, "messages": []},
            {"id": "used", "user_id": "refresh-owner", "created_at": created,
             "messages": [{"role": "user", "content": "Fixture"}]},
        ]
        response = await self.client.get("/api/conversations", headers={"X-Session-ID": "refresh-owner"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["id"] for item in response.json()["data"]["conversations"]], ["used"])
        self.assertEqual(len(self.store.conversations), 2)
        self.store.delete_conversation.assert_not_awaited()
        self.store.save_data.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
