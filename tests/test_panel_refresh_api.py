"""Panel refresh reads stay nonblocking and reuse only current raw aggregates."""
from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts
from services import metrics_summary


class PanelRefreshApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.sessions = {"panel-session": {"account": "panel-fixture", "created_at": datetime.now().isoformat()}}

        async def get_session(session_id):
            return self.sessions.get(session_id)

        self.stats = Mock(return_value={"backend": "fixture", "dim": 2,
                                        "collection": "fixture", "total_chunks": 3})
        self.collection = SimpleNamespace(get=Mock(return_value={
            "metadatas": [{"document_id": "a"}, {"document_id": "a"}, {"document_id": "b"}],
        }))
        self.store = SimpleNamespace(stats=self.stats, _collection=self.collection, _index_revision=0)
        self.provider = Mock(return_value=self.store)
        self.patches = [
            patch.object(main, "data_store", SimpleNamespace(get_session=get_session)),
            patch.object(main, "_rag_status_source", None),
            patch.object(main, "_rag_status_snapshot", None),
            patch.object(main, "_rag_status_cached_at", 0.0),
            patch("services.knowledge.vector_store.get_vector_store", self.provider),
        ]
        for item in self.patches:
            item.start()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://panel-fixture")
        self.headers = {"X-Session-ID": "panel-session"}

    async def asyncTearDown(self):
        await self.client.aclose()
        for item in reversed(self.patches):
            item.stop()

    async def test_status_uses_catalog_only_and_counts_distinct_documents(self):
        response = await self.client.get("/api/rag/status", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total_chunks"], 3)
        self.assertEqual(response.json()["data"]["document_count"], 2)
        self.assertTrue(response.json()["data"]["available"])
        self.provider.assert_called_once_with(catalog_only=True)

    async def test_status_cache_rechecks_session_and_index_revision(self):
        for _ in range(2):
            await self.client.get("/api/rag/status", headers=self.headers)
        self.stats.assert_called_once()
        self.collection.get.assert_called_once()
        self.store._index_revision += 1
        await self.client.get("/api/rag/status", headers=self.headers)
        self.assertEqual(self.stats.call_count, 2)
        main._rag_status_cached_at -= main._RAG_STATUS_TTL
        await self.client.get("/api/rag/status", headers=self.headers)
        self.assertEqual(self.stats.call_count, 3)
        self.sessions.clear()
        rejected = await self.client.get("/api/rag/status", headers=self.headers)
        self.assertEqual(rejected.status_code, 401)
        self.assertEqual(self.stats.call_count, 3)

    async def test_cold_status_initialization_does_not_block_other_authenticated_routes(self):
        entered, release = threading.Event(), threading.Event()
        request_thread = threading.get_ident()
        worker_threads = []

        def blocked_initialization(**_kwargs):
            worker_threads.append(threading.get_ident())
            entered.set()
            if not release.wait(2):
                raise AssertionError("Status initialization blocked the request event loop")
            return self.store

        self.provider.side_effect = blocked_initialization
        pending = asyncio.create_task(self.client.get("/api/rag/status", headers=self.headers))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait, 1))
            response = await asyncio.wait_for(self.client.get("/api/session", headers=self.headers), timeout=0.5)
            self.assertEqual(response.status_code, 200)
            self.assertFalse(pending.done())
            self.assertNotEqual(worker_threads, [request_thread])
        finally:
            release.set()
            await pending


class MetricsSummaryCacheTests(unittest.TestCase):
    def setUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.folder = tempfile.TemporaryDirectory(prefix="hk_metrics_cache_")
        self.root = Path(self.folder.name)
        self.path = self.root / "rag_metrics.jsonl"
        self.row = {"event": "retrieval", "request_id": "one",
                    "timestamp": datetime.now(metrics_summary.LOCAL_TIMEZONE).isoformat(),
                    "retrieval_latency_ms": 125.0, "hit_chunk_count": 2, "hit_file_count": 1,
                    "top1_score": 0.8, "empty": False, "status": "ok"}
        self.path.write_text(json.dumps(self.row) + "\n", encoding="utf-8")
        with metrics_summary._summary_cache_lock:
            metrics_summary._summary_cache.clear()

    def tearDown(self):
        self.folder.cleanup()

    def test_reuses_unchanged_file_without_scanning_and_returns_independent_objects(self):
        before = self.path.read_bytes()
        with patch.object(metrics_summary, "_read_metrics_summary", wraps=metrics_summary._read_metrics_summary) as read:
            result = metrics_summary.build_metrics_summary(self.path)
            result["summary"]["p95_latency_ms"] = -1
            current = metrics_summary.build_metrics_summary(self.path)
            self.assertEqual(current["record_count"], 1)
            self.assertEqual(current["summary"]["p95_latency_ms"], 125.0)
            read.assert_called_once()
        self.assertEqual(self.path.read_bytes(), before)

    def test_appended_record_invalidates_immediately(self):
        self.assertEqual(metrics_summary.build_metrics_summary(self.path)["record_count"], 1)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({**self.row, "request_id": "two"}) + "\n")
        self.assertEqual(metrics_summary.build_metrics_summary(self.path)["record_count"], 2)

    def test_short_ttl_rechecks_and_cache_is_bounded(self):
        with patch.object(metrics_summary, "monotonic", return_value=10.0), \
                patch.object(metrics_summary, "_read_metrics_summary", wraps=metrics_summary._read_metrics_summary) as read:
            metrics_summary.build_metrics_summary(self.path)
            with patch.object(metrics_summary, "monotonic", return_value=16.0):
                metrics_summary.build_metrics_summary(self.path)
            self.assertEqual(read.call_count, 2)
        for index in range(metrics_summary._SUMMARY_CACHE_LIMIT + 2):
            metrics_summary.build_metrics_summary(self.root / f"missing-{index}.jsonl")
        self.assertEqual(len(metrics_summary._summary_cache), metrics_summary._SUMMARY_CACHE_LIMIT)

    def test_concurrent_reads_share_one_scan(self):
        with patch.object(metrics_summary, "_read_metrics_summary", wraps=metrics_summary._read_metrics_summary) as read:
            with ThreadPoolExecutor(max_workers=4) as workers:
                results = list(workers.map(lambda _: metrics_summary.build_metrics_summary(self.path), range(8)))
            self.assertEqual([result["record_count"] for result in results], [1] * 8)
            read.assert_called_once()

    def test_explicit_clock_bypasses_cache_and_preserves_date_window(self):
        metrics_summary.build_metrics_summary(self.path)
        future = datetime.now(metrics_summary.LOCAL_TIMEZONE) + timedelta(days=40)
        self.assertEqual(metrics_summary.build_metrics_summary(self.path, now=future)["record_count"], 0)


if __name__ == "__main__":
    unittest.main()
