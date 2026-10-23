"""Evidence routing regressions; no storage, credentials or provider calls."""
import unittest

import tests  # Establish synthetic-only storage before application imports.
from services.question_routing import route_question, restrict_document_filter, filter_local_evidence


class QuestionRoutingTests(unittest.TestCase):
    def test_date_fact_is_general_in_both_chinese_forms(self):
        for question in ("10月10日是什么日子", "10月10日是甚麼日子"):
            route = route_question(question)
            self.assertFalse(route["retrieve_local"])
            self.assertTrue(route["simple_fact"])
        self.assertFalse(route_question("10月10日是什么日子", skill_id="chemistry-teacher")["simple_fact"])

    def test_school_date_excludes_math_and_wrong_calendar_day(self):
        question = "根據校曆，10月10日有甚麼安排？"
        route = route_question(question)
        hits = [
            {"chunk_id": "math", "filename": "高二數學練習.pdf", "content": "10月10日：函數練習"},
            {"chunk_id": "wrong-day", "filename": "校曆.pdf", "content": "10月11日：校慶"},
            {"chunk_id": "right", "filename": "校曆.pdf", "content": "10月10日：校慶"},
        ]
        kept, rejected = filter_local_evidence(question, hits, route)
        self.assertEqual([hit["chunk_id"] for hit in kept], ["right"])
        self.assertEqual(len(rejected), 2)

    def test_page_without_file_scope_cannot_search_arbitrary_documents(self):
        route = route_question("那第33頁呢？")
        allowed = {"document_id": {"$in": ["math-a", "math-b"]}}
        scoped = restrict_document_filter(allowed, route, [])
        self.assertEqual(scoped["document_id"]["$in"], ["__no_authorized_document__"])

    def test_followup_inherits_only_actual_answer_citation(self):
        messages = [{"is_user": True, "content": "請解釋文件"},
                    {"is_user": False, "content": "結論[1]。", "citation_map": [
                        {"index": 1, "document_id": "used"}, {"index": 2, "document_id": "unused"}]}]
        route = route_question("再解釋詳細一點", recent_messages=messages,
                               conversation_owner="owner", user_id="owner")
        self.assertEqual(route["document_ids"], ["used"])
        self.assertTrue(route["inherited"])
        general = route_question("10月10日是什么日子", recent_messages=messages,
                                 conversation_owner="owner", user_id="owner")
        self.assertFalse(general["retrieve_local"])

    def test_thinking_and_code_markers_do_not_establish_file_scope(self):
        messages = [{"is_user": True, "content": "普通問題"},
                    {"is_user": False, "content": "<think>[1]</think>例子：`[1]`\n```text\n[1]\n```",
                     "citation_map": [{"index": 1, "document_id": "candidate"}]}]
        route = route_question("那第33頁呢？", recent_messages=messages,
                               conversation_owner="owner", user_id="owner")
        self.assertEqual(route["document_ids"], [])
        self.assertFalse(route["inherited"])

    def test_scope_does_not_cross_owner_or_intervening_topic(self):
        messages = [{"is_user": True, "content": "[秘密.pdf]"}]
        route = route_question("那第2頁呢？", recent_messages=messages,
                               conversation_owner="other", user_id="owner")
        self.assertFalse(route["inherited"])
        messages += [{"is_user": False, "content": "暫無資料。"},
                     {"is_user": True, "content": "介紹一種植物"},
                     {"is_user": False, "content": "這是普通知識回答。"}]
        route = route_question("那第2頁呢？", recent_messages=messages,
                               conversation_owner="owner", user_id="owner")
        self.assertFalse(route["inherited"])

    def test_matching_name_never_expands_permission(self):
        route = route_question("請解釋 [數學.pdf]")
        records = [{"id": "allowed", "name": "数学.pdf"},
                   {"id": "private", "name": "數學.pdf"}]
        permission = {"document_id": {"$in": ["allowed"]}}
        self.assertEqual(restrict_document_filter(permission, route, records), permission)

    def test_explicit_page_rejects_other_pages_without_backfill(self):
        question = "解釋 [math.pdf] 第33頁"
        route = route_question(question)
        kept, rejected = filter_local_evidence(question, [
            {"filename": "math.pdf", "page": 32, "content": "wrong page"},
            {"filename": "math.pdf", "page": 33, "content": "right page"},
        ], route)
        self.assertEqual([hit["page"] for hit in kept], [33])
        self.assertEqual(rejected[0]["reason"], "page_mismatch")


if __name__ == "__main__":
    unittest.main()
