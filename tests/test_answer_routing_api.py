"""Offline API regressions for selective file retrieval and honest sources.

These exercise the real question router and both streaming handlers. Providers,
the model, and the vector retriever are fixtures; storage is temporary.
"""
from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import tests  # Establish synthetic-only storage before application imports.
import main
from tests.account_fixtures import fixture_accounts

# Keep the provider/body privacy fixture identical when this file is run either
# through unittest discovery or by its absolute path.
_fixture_path = Path(__file__).with_name("test_web_search_api.py")
_fixture_spec = importlib.util.spec_from_file_location("_answer_web_fixture", _fixture_path)
_fixture_module = importlib.util.module_from_spec(_fixture_spec)
_fixture_spec.loader.exec_module(_fixture_module)
WebSearchApiFixture = _fixture_module.WebSearchApiFixture


class AnswerRoutingApiTests(WebSearchApiFixture):
    def assert_local_skipped(self, events):
        rag = next(event for event in events if event["type"] == "rag_context")
        self.assertEqual(rag["status"], "skipped")
        self.assertEqual(rag["chunks"], [])
        self.assertEqual(events[-1]["rag_status"]["status"], "skipped")
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertEqual(events[-1]["references"], [])
        self.assertEqual(self.latest_assistant()["rag_status"]["status"], "skipped")
        self.retrieve.assert_not_called()

    def add_history(self, *messages):
        self.store._conversations["fixture-conversation"]["messages"].extend(copy.deepcopy(messages))

    def test_general_date_with_search_uses_web_without_local_candidates(self):
        _, events = self.ask("/api/chat", question="10月10日是什么日子", search=True)
        self.gather.assert_awaited_once_with("10月10日是什么日子", deepseek_key="offline-fixture-key")
        self.assert_local_skipped(events)
        prompt = self.prompts[-1]["prompt"]
        self.assertIn("FETCHED_EVIDENCE_IN_PROMPT", prompt)
        self.assertNotIn("LOCAL_FILE_BODY", prompt)
        self.assertNotIn("目前無法依據文件庫回答", prompt)
        self.assertNotIn("不要使用模型自身知識猜測答案", prompt)
        self.assert_no_raw_web_body(events)

    def test_general_date_without_search_still_generates_normal_answer(self):
        self.answer = "10月10日有不同地區的紀念日。"
        _, events = self.ask("/api/chat", question="10月10日是什么日子", search=False)
        self.assert_local_skipped(events)
        self.gather.assert_not_called()
        self.assertFalse(any(event["type"].startswith("web_") for event in events))
        self.assertEqual(len(self.prompts), 1)
        self.assertIn(self.answer, self.latest_assistant()["content"])
        prompt = self.prompts[-1]["prompt"]
        self.assertNotIn("LOCAL_FILE_BODY", prompt)
        self.assertNotIn("目前無法依據文件庫回答", prompt)
        self.assertNotIn("不要使用模型自身知識猜測答案", prompt)

    def test_both_handlers_skip_file_search_for_general_question(self):
        for route in self.routes:
            with self.subTest(route=route):
                self.reset_conversation()
                self.retrieve.reset_mock()
                self.local_files.reset_mock()
                self.gather.reset_mock()
                _, events = self.ask(route, question="10月10日是什么日子", search=False)
                self.retrieve.assert_not_called()
                self.local_files.assert_not_called()
                self.gather.assert_not_called()
                self.assertEqual(events[-1]["type"], "done")
                self.assertNotIn("目前無法依據文件庫回答", self.prompts[-1]["prompt"])

    def test_school_calendar_question_retrieves_relevant_local_evidence(self):
        self.hits[0].update(filename="校曆.pdf", content="校曆：10月10日舉行校慶活動。")
        self.citations[0].update(filename="校曆.pdf", excerpt=self.hits[0]["content"])
        self.reference_block.return_value = "[1] 校曆.pdf p.3\n校曆：10月10日舉行校慶活動。"
        self.answer = "根據校曆，當日舉行校慶活動[1]。"
        _, events = self.ask("/api/chat", question="根據校曆，10月10日有甚麼安排？", search=False)
        self.retrieve.assert_called_once()
        self.assertEqual(self.retrieve.call_args.kwargs["top_k"], 8)
        self.assertEqual(self.retrieve.call_args.kwargs["filters"]["document_id"]["$in"], ["doc-fixture"])
        self.assertEqual(events[-1]["citation_map"], self.citations)
        self.assertNotEqual(events[-1]["rag_status"]["status"], "skipped")
        self.gather.assert_not_called()

    def test_explicit_file_names_are_intersected_with_authorized_documents(self):
        self.store._files.extend([
            {"id": "doc-other", "name": "private.pdf", "audience": "personal", "owner": "other"},
            {"id": "doc-draft", "name": "draft.pdf", "audience": "school", "review_status": "pending"},
        ])
        self.ask("/api/chat", question="根據 [fixture.txt] 的內容作答。", search=False,
                 file_references="fixture.txt\nprivate.pdf\ndraft.pdf")
        self.retrieve.assert_called_once()
        allowed = self.retrieve.call_args.kwargs["filters"]["document_id"]["$in"]
        self.assertEqual(allowed, ["doc-fixture"])
        self.assertNotIn("doc-other", self.latest_assistant()["referenced_files"])
        self.assertNotIn("doc-draft", self.latest_assistant()["referenced_files"])

    def test_bracketed_file_name_limits_retrieval_without_separate_reference_field(self):
        self.store._files.append({"id": "doc-extra", "name": "other.txt", "audience": "school"})
        with patch.object(main, "retrieval_permission_filter", AsyncMock(return_value={
                "document_id": {"$in": ["doc-fixture", "doc-extra"]}})):
            self.ask("/api/chat", question="請總結 [fixture.txt]。", search=False)
        self.retrieve.assert_called_once()
        self.assertEqual(self.retrieve.call_args.kwargs["filters"]["document_id"]["$in"], ["doc-fixture"])

    def test_file_followup_inherits_actual_cited_document(self):
        self.add_history(
            {"id": "file-q", "is_user": True, "content": "解釋我的文件"},
            {"id": "file-a", "is_user": False, "content": "這是第一點[1]。",
             "citation_map": self.citations, "referenced_files": ["doc-fixture"]},
        )
        self.ask("/api/chat", question="那這份文件再解釋詳細一點", search=False)
        self.retrieve.assert_called_once()
        self.assertEqual(self.retrieve.call_args.kwargs["filters"]["document_id"]["$in"], ["doc-fixture"])

    def test_unused_candidate_citations_do_not_supply_followup_file_scope(self):
        self.add_history(
            {"id": "prior-q", "is_user": True, "content": "學校有甚麼活動？"},
            {"id": "prior-a", "is_user": False, "content": "暫時無法確認。",
             "citation_map": self.citations, "referenced_files": ["doc-fixture"]},
        )
        _, events = self.ask("/api/chat", question="繼續解釋這份文件", search=False)
        if self.retrieve.called:
            # A local clarification route may retrieve candidates, but a vague
            # file pointer must not turn unused candidates into evidence.
            self.assertEqual(events[-1]["citation_map"], [])
            self.assertNotIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])

    def test_history_from_different_owner_never_supplies_file_scope(self):
        self.store._conversations["fixture-conversation"]["user_id"] = "other-owner"
        self.add_history(
            {"id": "other-q", "is_user": True, "content": "[fixture.txt]"},
            {"id": "other-a", "is_user": False, "content": "另一用戶內容[1]。",
             "citation_map": self.citations, "referenced_files": ["doc-fixture"]},
        )
        _, events = self.ask("/api/chat", question="那這份文件繼續解釋", search=False)
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertNotIn("另一用戶內容", self.prompts[-1]["prompt"])
        self.assertNotIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])

    def test_unpermitted_retriever_result_is_removed_before_prompt_and_history(self):
        self.retrieve.return_value = [{
            "document_id": "doc-private", "chunk_id": "chunk-private",
            "filename": "fixture.txt", "page": 3, "content": "PRIVATE_RETRIEVER_BODY",
        }]
        _, events = self.ask("/api/chat", question="請總結 [fixture.txt]。", search=False)
        self.retrieve.assert_called_once()
        self.citation_payload.assert_not_called()
        self.reference_block.assert_not_called()
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertEqual(self.latest_assistant()["referenced_files"], [])
        self.assertNotIn("PRIVATE_RETRIEVER_BODY", self.prompts[-1]["prompt"])
        self.assertNotIn("PRIVATE_RETRIEVER_BODY", json.dumps(events))

    def test_file_followup_can_inherit_recent_explicit_file_even_without_answer_citation(self):
        self.add_history(
            {"id": "file-q", "is_user": True, "content": "先介紹這份文件", "file_references": "fixture.txt"},
            {"id": "file-a", "is_user": False, "content": "你想先看哪一部分？", "citation_map": []},
        )
        self.ask("/api/chat", question="繼續解釋這份文件", search=False)
        self.retrieve.assert_called_once()
        self.assertEqual(self.retrieve.call_args.kwargs["filters"]["document_id"]["$in"], ["doc-fixture"])

    def test_new_general_topic_is_not_polluted_by_previous_file_answer(self):
        self.add_history(
            {"id": "file-q", "is_user": True, "content": "解釋文件", "file_references": "fixture.txt"},
            {"id": "file-a", "is_user": False, "content": "文件說明[1]。",
             "citation_map": self.citations, "referenced_files": ["doc-fixture"]},
        )
        _, events = self.ask("/api/chat", question="10月10日是什么日子", search=False)
        self.assert_local_skipped(events)
        self.assertNotIn("LOCAL_FILE_BODY", self.prompts[-1]["prompt"])

    def test_no_local_evidence_for_school_question_keeps_honest_refusal(self):
        self.retrieve.return_value = []
        _, events = self.ask("/api/chat", question="根據校曆，10月10日有甚麼安排？", search=False)
        self.retrieve.assert_called_once()
        self.assertEqual(events[-1]["citation_map"], [])
        self.assertNotEqual(events[-1]["rag_status"]["status"], "skipped")
        self.assertIn("請明確告知用戶目前無法依據文件庫回答", self.prompts[-1]["prompt"])
        self.gather.assert_not_called()

    def test_skipped_rag_status_survives_reload_and_history_response(self):
        _, events = self.ask("/api/chat", question="10月10日是什么日子", search=False)
        self.assert_local_skipped(events)
        reloaded = main.DataStore()
        saved = self.latest_assistant(reloaded)
        self.assertEqual(saved["rag_status"], events[-1]["rag_status"])
        with patch.object(main, "data_store", reloaded):
            response = self.client.get("/api/conversation/fixture-conversation", headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        restored = response.json()["data"]["messages"][-1]
        self.assertEqual(restored["rag_status"], events[-1]["rag_status"])
        self.assertEqual(restored["citation_map"], [])
        self.assertEqual(restored["references"], [])
        self.assert_no_raw_web_body(json.loads((self.root / "conversations.json").read_text(encoding="utf-8")))

    def test_thought_and_code_citations_do_not_become_referenced_files(self):
        self.answer = "<think>候選[1]</think>示例：`[1]`\n```text\n[1]\n```\n本題暫無可確認的文件結論。"
        self.ask("/api/chat", question="請說明 [fixture.txt]", search=False)
        self.assertEqual(self.latest_assistant()["referenced_files"], [])
        self.assertEqual(self.latest_assistant()["citation_map"], self.citations)


if __name__ == "__main__":
    unittest.main()
