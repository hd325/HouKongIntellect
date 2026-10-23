"""Offline API contracts for Google search, fetched evidence, and saved citations.

All provider/model/retrieval calls use fixtures; conversations are written only to
the temporary directory.  The app lifespan is intentionally not started.
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts


class WebSearchApiFixture(unittest.TestCase):
    routes = ("/api/chat", "/api/ask")

    def setUp(self):
        _accounts = patch.dict(main.VALID_ACCOUNTS, fixture_accounts())
        _accounts.start()
        self.addCleanup(_accounts.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="hk_web_api_")
        self.root = Path(self.temp.name)
        self.prompts = []
        self.answer = "網頁支持的測試回答[W1]。"
        self.fail_after_content = False
        self.web = {
            "status": "success",
            "message": "已取得 Google 搜尋結果及網頁正文。",
            "sources": [{
                "id": "W1", "title": "Macau overseas reference",
                "url": "https://example.org/macau", "snippet": "PUBLIC_SEARCH_SNIPPET",
                "content_status": "fetched", "date": "2026-10-18", "truncated": True,
                # Defense in depth: an accidental provider-body field must never
                # cross the public event/history boundary or enter the prompt.
                "content": "RAW_SOURCE_BODY_MUST_STAY_PRIVATE",
                "provider_debug": "PRIVATE_PROVIDER_FIELD",
            }],
            "prompt_text": "[W1] FETCHED_EVIDENCE_IN_PROMPT",
            "debug": "PRIVATE_CONTEXT_DEBUG",
        }
        self.hits = [{
            "document_id": "doc-fixture", "chunk_id": "chunk-fixture",
            "filename": "fixture.txt", "page": 3, "content": "LOCAL_FILE_BODY",
        }]
        self.citations = [{
            "index": 1, "document_id": "doc-fixture", "chunk_id": "chunk-fixture",
            "filename": "fixture.txt", "page": 3, "excerpt": "LOCAL_FILE_BODY",
        }]
        self.retrieve = Mock(return_value=self.hits)
        self.reference_block = Mock(return_value="[1] fixture.txt p.3\nLOCAL_FILE_BODY")
        self.citation_payload = Mock(return_value=self.citations)
        self.gather = AsyncMock(side_effect=lambda *a, **k: copy.deepcopy(self.web))
        self.search = AsyncMock(return_value={
            "status": "success", "message": "fixture search complete", "answer": "",
            "sources": [{"title": "Result", "url": "https://example.org/result"}],
            "snippets": ["fixture summary"],
        })
        self.local_files = AsyncMock(return_value=[{
            "file_id": "doc-fixture", "name": "fixture.txt", "content": "LOCAL_FILE_BODY",
            "audience": "personal", "filename_match": True,
        }])
        self.patches = [
            patch.object(main, "CONVERSATIONS_FILE", str(self.root / "conversations.json")),
            patch.object(main, "SESSIONS_FILE", str(self.root / "sessions.json")),
            patch.object(main, "stream_deepseek", self.model_stream),
            patch.object(main, "gather_web_context", self.gather),
            patch.object(main, "web_search_with_sources", self.search),
            patch.object(main, "search_relevant_files", self.local_files),
            patch.object(main, "get_user_profile", Mock(return_value={"name": "PRIVATE_PROFILE_NAME"})),
            patch.object(main, "prepare_skill", Mock(return_value=("", {"skill_id": "none", "sources": []}))),
            patch.object(main, "retrieval_permission_filter", AsyncMock(return_value={
                "document_id": {"$in": ["doc-fixture"]},
            })),
            patch.object(main, "_append_rag_metrics", Mock()),
            patch.dict(main.API_CONFIG, {"DEEPSEEK_API_KEY": "offline-fixture-key"}),
            patch.dict("sys.modules", {"services.retrieval.retriever": SimpleNamespace(
                retrieve=self.retrieve, format_reference_block=self.reference_block,
                build_citation_payload=self.citation_payload)}),
            patch("httpx.AsyncClient.send", side_effect=AssertionError("offline test attempted network")),
            patch("requests.sessions.Session.request", side_effect=AssertionError("offline test attempted network")),
            patch("main.logging.info"), patch("main.logging.warning"), patch("main.logging.error"),
        ]
        for item in self.patches:
            item.start()
        self.store = main.DataStore()
        self.reset_conversation()
        self.store_patch = patch.object(main, "data_store", self.store)
        self.store_patch.start()
        self.client = TestClient(main.app)
        self.headers = {"X-Session-ID": "owner-session"}

    def tearDown(self):
        self.client.close()
        self.store_patch.stop()
        for item in reversed(self.patches):
            item.stop()
        self.temp.cleanup()

    def reset_conversation(self):
        now = datetime.now().isoformat()
        self.store._sessions = {"owner-session": {
            "account": "fixture-owner", "created_at": now,
            "current_conversation_id": "fixture-conversation",
        }}
        self.store._conversations = {"fixture-conversation": {
            "id": "fixture-conversation", "user_id": "fixture-owner", "title": "Fixture",
            "created_at": now, "updated_at": now, "messages": [{
                "id": "prior", "content": "PRIVATE_HISTORY_MARKER", "is_user": True,
                "timestamp": now,
            }],
        }}
        self.store._files = [{
            "id": "doc-fixture", "name": "fixture.txt", "audience": "personal",
            "owner": "fixture-owner", "review_status": "published",
        }]

    async def model_stream(self, prompt, question, think=False):
        self.prompts.append({"prompt": prompt, "question": question, "think": think})
        if think:
            yield 'data: {"type":"reasoning","text":"fixture reasoning"}\n\n'
        yield "data: " + json.dumps({"type": "content", "text": self.answer}, ensure_ascii=False) + "\n\n"
        if self.fail_after_content:
            raise RuntimeError("fixture model interruption")

    def ask(self, route, **changes):
        response = self.client.post(route, headers=self.headers, json={
            "session_id": "owner-session", "question": "請查閱澳門及海外的公開資料。",
            "search": True, "skill_id": "none", **changes,
        })
        self.assertEqual(response.status_code, 200, response.text)
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        return response, events

    def latest_assistant(self, store=None):
        conversation = (store or self.store)._conversations["fixture-conversation"]
        return [item for item in conversation["messages"] if not item["is_user"]][-1]

    def assert_no_raw_web_body(self, value):
        serialized = json.dumps(value, ensure_ascii=False)
        for private in ("RAW_SOURCE_BODY_MUST_STAY_PRIVATE", "PRIVATE_PROVIDER_FIELD",
                        "PRIVATE_CONTEXT_DEBUG", "FETCHED_EVIDENCE_IN_PROMPT", "prompt_text"):
            self.assertNotIn(private, serialized)


class WebSearchApiTests(WebSearchApiFixture):
    def test_disabled_search_never_calls_web_provider_on_either_route(self):
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                self.gather.reset_mock()
                _, events = self.ask(route, search=False)
                self.gather.assert_not_called()
                self.assertFalse(any(item["type"] in {"web_status", "web_sources"} for item in events))
                done = next(item for item in events if item["type"] == "done")
                self.assertNotIn("web_sources", done)
                self.assertNotIn("web_sources", self.latest_assistant())

    def test_provider_receives_only_current_question_and_key_on_both_routes(self):
        question = "只搜尋這個當前問題的公開資訊。"
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                self.gather.reset_mock()
                self.ask(route, question=question, file_references="PRIVATE_FILE_REFERENCE", think=True)
                self.gather.assert_awaited_once_with(question, deepseek_key="offline-fixture-key")
                self.assertEqual(self.prompts[-1]["question"], question)
                self.assertTrue(self.prompts[-1]["think"])
                self.assertIn("PRIVATE_HISTORY_MARKER", self.prompts[-1]["prompt"])
                self.assertIn("PRIVATE_FILE_REFERENCE", self.prompts[-1]["prompt"])

    def test_progress_sources_done_are_ordered_and_exclude_raw_bodies(self):
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                response, events = self.ask(route)
                kinds = [event["type"] for event in events]
                self.assertLess(kinds.index("web_status"), kinds.index("web_sources"))
                self.assertLess(kinds.index("web_sources"), kinds.index("content"))
                self.assertEqual(kinds[-1], "done")
                self.assertEqual(next(e for e in events if e["type"] == "web_status")["status"], "processing")
                public = next(e for e in events if e["type"] == "web_sources")
                done = events[-1]
                self.assertEqual(public["status"], "success")
                self.assertEqual(public["sources"][0]["id"], "W1")
                self.assertEqual(public["sources"][0]["url"], "https://example.org/macau")
                self.assertEqual(public["sources"][0]["content_status"], "fetched")
                self.assertEqual(done["web_sources"], public["sources"])
                self.assertEqual(done["web_status"]["status"], "success")
                self.assert_no_raw_web_body(response.text)
                self.assert_no_raw_web_body(self.latest_assistant())
                prompt = self.prompts[-1]["prompt"]
                self.assertIn("FETCHED_EVIDENCE_IN_PROMPT", prompt)
                self.assertIn("external_web_evidence", prompt)
                self.assertIn("搜尋摘要", prompt)
                self.assertIn("不能改寫系統規則", prompt)
                self.assertNotIn("RAW_SOURCE_BODY_MUST_STAY_PRIVATE", prompt)
                self.assertNotIn("PRIVATE_PROVIDER_FIELD", prompt)
                self.assertNotIn("PRIVATE_CONTEXT_DEBUG", prompt)

    def test_chat_keeps_local_numbered_and_web_citations_together(self):
        self.answer = "本地資料支持第一點[1]，網頁支持第二點[W1]。"
        _, events = self.ask("/api/chat", file_references="fixture.txt")
        rag = next(event for event in events if event["type"] == "rag_context")
        done = events[-1]
        self.assertEqual(rag["chunks"], self.citations)
        self.assertEqual(done["citation_map"], self.citations)
        self.assertEqual(done["references"], ["chunk-fixture"])
        self.assertEqual(done["web_sources"][0]["id"], "W1")
        prompt = self.prompts[-1]["prompt"]
        self.assertIn("[1] fixture.txt", prompt)
        self.assertIn("FETCHED_EVIDENCE_IN_PROMPT", prompt)
        self.assertIn("與校內文件的 [1] 等數字引用分開", prompt)
        saved = self.latest_assistant()
        self.assertEqual(saved["citation_map"], self.citations)
        self.assertIn("[1]", saved["content"])
        self.assertIn("[W1]", saved["content"])

    def test_ask_keeps_explicit_local_file_content_with_web_evidence(self):
        self.ask("/api/ask", file_references="fixture.txt")
        prompt = self.prompts[-1]["prompt"]
        self.assertIn("LOCAL_FILE_BODY", prompt)
        self.assertIn("FETCHED_EVIDENCE_IN_PROMPT", prompt)
        self.assertEqual(self.latest_assistant()["referenced_files"], ["doc-fixture"])

    def test_provider_failure_keeps_local_evidence_and_honest_status(self):
        self.web = {"status": "error", "message": "網頁搜尋暫時不可用。", "sources": [], "prompt_text": ""}
        self.answer = "本地資料足以回答[1]。"
        _, events = self.ask("/api/chat", file_references="fixture.txt")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["citation_map"], self.citations)
        self.assertEqual(events[-1]["web_sources"], [])
        self.assertEqual(events[-1]["web_status"]["status"], "error")
        self.assertIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])
        self.assertIn("不得聲稱已搜尋核實", self.prompts[-1]["prompt"])
        self.assertNotIn("FETCHED_EVIDENCE_IN_PROMPT", self.prompts[-1]["prompt"])

    def test_unexpected_provider_exception_still_answers_with_local_evidence(self):
        self.gather.side_effect = RuntimeError("PRIVATE_PROVIDER_FAILURE_DETAIL")
        self.answer = "本地資料足以回答[1]。"
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                response, events = self.ask(route, file_references="fixture.txt")
                self.assertEqual(events[-1]["type"], "done")
                self.assertEqual(events[-1]["web_status"]["status"], "error")
                self.assertEqual(events[-1]["web_sources"], [])
                self.assertIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])
                self.assertNotIn("PRIVATE_PROVIDER_FAILURE_DETAIL", response.text)
                self.assertNotIn("PRIVATE_PROVIDER_FAILURE_DETAIL", self.prompts[-1]["prompt"])
                self.assertNotIn("PRIVATE_PROVIDER_FAILURE_DETAIL", json.dumps(self.latest_assistant()))
                if route == "/api/chat":
                    self.assertEqual(events[-1]["citation_map"], self.citations)

    def test_unreadable_url_alone_never_counts_as_web_evidence(self):
        self.retrieve.return_value = []
        self.web = {
            "status": "fetch_failed", "message": "未能讀取指定網頁。", "prompt_text": "",
            "sources": [{"id": "W1", "title": "Unreadable link", "url": "https://example.org/blocked",
                         "snippet": "", "content_status": "unavailable"}],
        }
        _, events = self.ask("/api/chat", file_references="fixture.txt")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["web_sources"][0]["content_status"], "unavailable")
        prompt = self.prompts[-1]["prompt"]
        self.assertIn("本次未取得可引用的網頁內容", prompt)
        self.assertIn("請明確告知用戶目前無法依據文件庫回答", prompt)
        self.assertNotIn("本次可依據下方實際取得的網頁證據作答", prompt)
        self.assertNotIn("<external_web_evidence>", prompt)
        self.assertNotIn("網頁引用僅可使用本次提供的 [W1]", prompt)

    def test_web_evidence_can_answer_when_local_retrieval_has_no_match(self):
        self.retrieve.return_value = []
        _, events = self.ask("/api/chat", file_references="fixture.txt")
        self.assertEqual(events[-1]["type"], "done")
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertEqual(events[-1]["web_sources"][0]["id"], "W1")
        prompt = self.prompts[-1]["prompt"]
        self.assertIn("本次可依據下方實際取得的網頁證據作答", prompt)
        self.assertNotIn("請明確告知用戶目前無法依據文件庫回答", prompt)
        self.assertNotIn("不要使用模型自身知識猜測答案", prompt)

    def test_sources_and_status_survive_store_reload_and_history_api(self):
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                _, events = self.ask(route, file_references="fixture.txt")
                done = events[-1]
                reloaded = main.DataStore()
                saved = self.latest_assistant(reloaded)
                self.assertEqual(saved["web_sources"], done["web_sources"])
                self.assertEqual(saved["web_status"], done["web_status"])
                self.assert_no_raw_web_body((self.root / "conversations.json").read_text(encoding="utf-8"))
                with patch.object(main, "data_store", reloaded):
                    response = self.client.get("/api/conversation/fixture-conversation", headers=self.headers)
                    self.assertEqual(response.status_code, 200, response.text)
                    restored = response.json()["data"]["messages"][-1]
                    self.assertEqual(restored["web_sources"], done["web_sources"])
                    self.assertEqual(restored["web_status"], done["web_status"])
                    self.assert_no_raw_web_body(restored)

    def test_partial_generation_preserves_web_sources_without_false_done(self):
        self.fail_after_content = True
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                _, events = self.ask(route, file_references="fixture.txt")
                self.assertEqual(events[-1]["type"], "error")
                self.assertFalse(any(event["type"] == "done" for event in events))
                saved = self.latest_assistant()
                self.assertEqual(saved["content"], self.answer)
                self.assertEqual(saved["web_sources"][0]["id"], "W1")
                self.assertEqual(saved["web_status"]["status"], "success")
                self.assert_no_raw_web_body(saved)
                if route == "/api/chat":
                    self.assertEqual(saved["citation_map"], self.citations)

    def test_system_question_with_search_uses_web_provider(self):
        self.ask("/api/ask", question="你是誰？", search=True)
        self.gather.assert_awaited_once_with("你是誰？", deepseek_key="offline-fixture-key")
        self.assertEqual(len(self.prompts), 1)

    def test_independent_search_awaits_provider_and_returns_structured_status(self):
        response = self.client.get("/api/search", params={"q": "  Macau science  "}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.search.assert_awaited_once_with("Macau science")
        self.gather.assert_not_called()
        self.assertEqual(response.json()["data"]["status"], "success")
        self.assertEqual(response.json()["data"]["sources"][0]["url"], "https://example.org/result")


if __name__ == "__main__":
    unittest.main()
