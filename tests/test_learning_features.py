"""Offline contract tests for the learning features; never call a real LLM.

Run with: venv312/Scripts/python.exe -m unittest discover -s tests -v
"""
from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from services.document_files import display_upload_name, stable_upload_id
from services.metrics_summary import build_metrics_summary, build_retrieval_record, summarize_metric_records
from services.quiz_service import (
    MAX_MATERIAL_CHARS,
    QuizGenerationError,
    QuizValidationError,
    generate_quiz,
    load_document_material,
    validate_options,
    validate_quiz_json,
)


DOCUMENT_ID = "lesson-document"
SOURCE_TEXT = "原电池将化学能转化为电能，负极发生氧化反应，正极发生还原反应。"
SECOND_SOURCE_TEXT = "电解质溶液形成内电路，导线连接电极组成外电路。"
NOW = datetime.fromisoformat("2026-10-03T12:00:00+08:00")


def source_chunk(chunk_id: str, content: str = SOURCE_TEXT, *, page: int = 1,
                 offset: int = 0, document_id: str = DOCUMENT_ID) -> dict:
    return {"chunk_id": chunk_id, "content": content,
            "metadata": {"document_id": document_id, "filename": "化学资料.pdf",
                         "page": page, "char_start": offset}}


class MaterialStore:
    def __init__(self, chunks: list[dict]):
        self.chunks = chunks
        self.requested_ids: list[str] = []

    def get_document_chunks(self, document_id: str) -> list[dict]:
        self.requested_ids.append(document_id)
        return copy.deepcopy(self.chunks)


def material_fixture() -> dict:
    return load_document_material(DOCUMENT_ID, MaterialStore([
        source_chunk("chemistry-1"),
        source_chunk("chemistry-2", SECOND_SOURCE_TEXT, page=2, offset=100),
    ]))


def quiz_item(kind: str = "blank", *, stem: str = "原电池将哪一种能量转化为电能？",
              source_id: int = 1, evidence: str = SOURCE_TEXT) -> dict:
    result = {"type": kind, "stem": stem, "answer": "化学能",
              "explanation": "所选资料明确说明原电池把化学能转化为电能。",
              "source_id": source_id, "evidence": evidence}
    if kind == "choice":
        result.update({"options": ["化学能", "太阳能", "机械能", "光能"], "answer": "A"})
    return result


def validate_payload(items: list[dict], *, types: list[str] | None = None,
                     count: int | None = None, material: dict | None = None) -> list[dict]:
    return validate_quiz_json(json.dumps({"questions": items}, ensure_ascii=False),
                              question_types=types or ["blank"],
                              count=len(items) if count is None else count,
                              difficulty="medium", material=material or material_fixture())


def metric_row(request_id: str, latency: float | None = 100.0, **updates: object) -> dict:
    result = {"request_id": request_id, "event": "retrieval",
              "timestamp": "2026-10-03T10:00:00+08:00", "status": "ok",
              "hit_chunk_count": 8, "hit_file_count": 3,
              "top1_score": 0.7, "retrieval_latency_ms": latency, "empty": False}
    result.update(updates)
    return result


class DocumentIdentityTests(unittest.TestCase):
    def test_valid_uuid_prefix_is_preserved_and_display_name_is_unwrapped(self) -> None:
        fixtures = [
            ("4F54C50F-51E4-4715-8291-3461ED570144_原电池.pdf",
             "4f54c50f-51e4-4715-8291-3461ed570144", "原电池.pdf"),
            ("4f54c50f-51e4-4715-8291-3461ed570144_lesson_with_underscores.pdf",
             "4f54c50f-51e4-4715-8291-3461ed570144", "lesson_with_underscores.pdf"),
        ]
        for stored_name, expected_id, expected_display_name in fixtures:
            for name in [stored_name, str(Path("arbitrary-parent") / stored_name)]:
                with self.subTest(filename=name):
                    self.assertEqual(stable_upload_id(name), expected_id)
                    self.assertEqual(display_upload_name(name), expected_display_name)

    def test_fixed_utf8_filename_hashes_and_invalid_uuid_prefixes(self) -> None:
        # Fixed SHA-256 test vectors, independently calculated; no maintenance
        # script or ignored local upload directory is needed to run the suite.
        fixtures = [
            ("未注册的数学练习.pdf", "upload_45dd57557adf985567bc"),
            ("plain_name.pdf", "upload_64d88e8557b7dc9ce2e5"),
            ("invalid_uuid_lesson.pdf", "upload_95f561d8c42e901f5d4c"),
            ("59E58A8F-AA69-0C45-8232-B1C16A7AF7F9_原电池.pdf", "upload_a833a7ffc99cbb96a204"),
            ("B4DF86DA-2F17-44D9-7D88-41F95212E36B_原电池.pdf", "upload_085cb568048c696baa8e"),
        ]
        for stored_name, expected_id in fixtures:
            for name in [stored_name, str(Path("arbitrary-parent") / stored_name)]:
                with self.subTest(filename=name):
                    self.assertEqual(stable_upload_id(name), expected_id)
                    self.assertEqual(display_upload_name(name), stored_name)


class QuizMaterialTests(unittest.TestCase):
    def test_exact_document_only_and_page_offset_order(self) -> None:
        store = MaterialStore([
            source_chunk("page-2", page=2, offset=0),
            source_chunk("page-1-later", page=1, offset=30),
            source_chunk("page-1-first", page=1, offset=0),
            source_chunk("other-document", document_id="not-selected"),
        ])
        result = load_document_material(DOCUMENT_ID, store)
        self.assertEqual(store.requested_ids, [DOCUMENT_ID])
        self.assertEqual([source["chunk_id"] for source in result["sources"]],
                         ["page-1-first", "page-1-later", "page-2"])
        self.assertEqual(result["source_pages"], [1, 2])
        self.assertEqual(result["total_chunks"], 3)

    def test_full_material_including_labels_never_exceeds_3000(self) -> None:
        result = load_document_material(DOCUMENT_ID, MaterialStore([
            source_chunk("long-chunk", "甲" * 4000), source_chunk("later-chunk", page=2)
        ]))
        self.assertEqual(len(result["text"]), MAX_MATERIAL_CHARS)
        self.assertEqual(result["source_chars"], len(result["text"]))
        self.assertTrue(result["truncated"])
        self.assertLess(len(result["sources"][0]["content"]), 3000)
        self.assertEqual(result["sources"][0]["source_id"], 1)

    def test_explicit_global_chunk_indices_take_precedence_when_all_present(self) -> None:
        earlier = source_chunk("indexed-first", page=2)
        earlier["metadata"]["chunk_index"] = 1
        later = source_chunk("indexed-later", page=1)
        later["metadata"]["chunk_index"] = 2
        result = load_document_material(DOCUMENT_ID, MaterialStore([later, earlier]))
        self.assertEqual([source["chunk_id"] for source in result["sources"]],
                         ["indexed-first", "indexed-later"])

    def test_evidence_beyond_truncated_material_is_rejected(self) -> None:
        material = load_document_material(DOCUMENT_ID, MaterialStore([
            source_chunk("long-chunk", "甲" * 4000 + "资料末尾未给模型看的证据")
        ]))
        with self.assertRaises(QuizValidationError):
            validate_payload([quiz_item(evidence="资料末尾未给模型看的证据")], material=material)

    def test_insufficient_material_and_empty_document_id_are_rejected(self) -> None:
        for document_id, chunks in [("", [source_chunk("first")]),
                                    (DOCUMENT_ID, [source_chunk("tiny", "短文字")]),
                                    (DOCUMENT_ID, [])]:
            with self.subTest(document_id=document_id, chunks=len(chunks)):
                with self.assertRaises(QuizValidationError):
                    load_document_material(document_id, MaterialStore(chunks))

    def test_collection_fallback_uses_exact_document_filter(self) -> None:
        class Collection:
            def get(inner_self, **kwargs: object) -> dict:
                inner_self.kwargs = kwargs
                return {"ids": ["exact-chunk"], "documents": [SOURCE_TEXT],
                        "metadatas": [{"document_id": DOCUMENT_ID, "filename": "化学.pdf",
                                       "page": 3, "char_start": 10}]}
        class Store:
            _collection = Collection()
        store = Store()
        result = load_document_material(DOCUMENT_ID, store)
        self.assertEqual(store._collection.kwargs["where"], {"document_id": DOCUMENT_ID})
        self.assertEqual(result["sources"][0]["page"], 3)


class QuizValidationTests(unittest.TestCase):
    def test_valid_mixed_questions_receive_server_controlled_source_anchors(self) -> None:
        items = [quiz_item("choice", stem="原电池转换哪一种能量？"),
                 quiz_item("blank", stem="原电池把____转化为电能。"),
                 quiz_item("short", stem="说明原电池的能量转换。")]
        result = validate_payload(items, types=["choice", "blank", "short"])
        self.assertEqual([item["type"] for item in result], ["choice", "blank", "short"])
        for item in result:
            self.assertEqual(item["document_id"], DOCUMENT_ID)
            self.assertEqual(item["page"], 1)
            self.assertEqual(item["chunk_id"], "chemistry-1")
            self.assertEqual(item["evidence"], SOURCE_TEXT)
            self.assertNotIn("source_id", item)

    def test_source_id_maps_to_that_specific_pages_original_chunk(self) -> None:
        item = quiz_item(source_id=2, evidence=SECOND_SOURCE_TEXT)
        item["answer"] = "电解质溶液"
        result = validate_payload([item])
        self.assertEqual(result[0]["page"], 2)
        self.assertEqual(result[0]["chunk_id"], "chemistry-2")

    def test_strict_json_rejects_markdown_extra_fields_and_wrong_root(self) -> None:
        valid = json.dumps({"questions": [quiz_item()]}, ensure_ascii=False)
        for raw in ["```json\n" + valid + "\n```", "说明：" + valid,
                    json.dumps({"questions": [quiz_item()], "note": "extra"}),
                    json.dumps([quiz_item()]), json.dumps({"questions": {}})]:
            with self.subTest(raw=raw[:30]):
                with self.assertRaises(QuizValidationError):
                    validate_quiz_json(raw, question_types=["blank"], count=1,
                                       difficulty="medium", material=material_fixture())

    def test_invalid_source_id_or_forged_anchor_is_rejected(self) -> None:
        for update in [{"source_id": 99}, {"source_id": "1"}, {"source_id": True},
                       {"page": 999}, {"chunk_id": "invented-chunk"}]:
            with self.subTest(update=update):
                with self.assertRaises(QuizValidationError):
                    validate_payload([{**quiz_item(), **update}])

    def test_duplicate_json_fields_are_rejected_at_every_depth(self) -> None:
        item_json = json.dumps(quiz_item(), ensure_ascii=False)
        duplicate_source = item_json.replace('"source_id": 1', '"source_id": 1, "source_id": 2')
        payloads = [
            '{"questions": [' + item_json + '], "questions": [' + item_json + '] }',
            '{"questions": [' + duplicate_source + '] }',
        ]
        for raw in payloads:
            with self.subTest(payload=raw[:40]):
                with self.assertRaises(QuizValidationError):
                    validate_quiz_json(raw, question_types=["blank"], count=1,
                                       difficulty="medium", material=material_fixture())

    def test_evidence_requires_original_text_from_selected_source(self) -> None:
        for update in [{"evidence": "原电池产生的是太阳能"},
                       {"source_id": 2, "evidence": SOURCE_TEXT}]:
            with self.subTest(update=update):
                with self.assertRaises(QuizValidationError):
                    validate_payload([{**quiz_item(), **update}])
        spaced = SOURCE_TEXT.replace("原电池", "原 电 池").replace("化学能", "化 学 能")
        self.assertEqual(len(validate_payload([quiz_item(evidence=spaced)])), 1)

    def test_evidence_length_limits_and_complete_short_source_exception(self) -> None:
        long_material = load_document_material(DOCUMENT_ID, MaterialStore([
            source_chunk("long", "甲" * 400)
        ]))
        long_item = quiz_item(evidence="甲" * 200)
        long_item["answer"] = "甲"
        self.assertEqual(len(validate_payload([long_item], material=long_material)), 1)
        for length in [19, 201]:
            with self.subTest(evidence_length=length):
                with self.assertRaises(QuizValidationError):
                    validate_payload([{**long_item, "evidence": "甲" * length}], material=long_material)
        short_text = "负极发生氧化反应"
        short_material = load_document_material(DOCUMENT_ID, MaterialStore([
            source_chunk("short", short_text), source_chunk("full", offset=100)
        ]))
        short_item = quiz_item(evidence=short_text)
        short_item["answer"] = "氧化反应"
        self.assertEqual(len(validate_payload([short_item], material=short_material)), 1)
        with self.assertRaises(QuizValidationError):
            validate_payload([{**short_item, "evidence": "发生氧化反应"}], material=short_material)

    def test_unrelated_answers_are_rejected_despite_a_valid_source_quote(self) -> None:
        for kind in ["choice", "blank", "short"]:
            unrelated = quiz_item(kind, stem="巴黎是哪个国家的首都？")
            if kind == "choice":
                unrelated.update({"options": ["法国", "德国", "西班牙", "意大利"], "answer": "A"})
            else:
                unrelated["answer"] = "法国" if kind == "blank" else "巴黎是法国的首都。"
            with self.subTest(question_type=kind):
                with self.assertRaises(QuizValidationError):
                    validate_payload([unrelated], types=[kind])

    def test_count_missing_selected_types_and_duplicate_stems_are_rejected(self) -> None:
        with self.assertRaises(QuizValidationError):
            validate_payload([quiz_item()], count=2)
        with self.assertRaises(QuizValidationError):
            validate_payload([quiz_item("choice", stem="第一题"), quiz_item("choice", stem="第二题")],
                             types=["choice", "blank"])
        with self.assertRaises(QuizValidationError):
            validate_payload([quiz_item(stem="相同题目"), quiz_item(stem="相 同 题 目")])

    def test_choice_options_and_answer_and_unselected_types_are_validated(self) -> None:
        for update in [{"options": ["a", "b", "c"]},
                       {"options": ["a", "a", "c", "d"]}, {"answer": "E"}]:
            with self.subTest(update=update):
                with self.assertRaises(QuizValidationError):
                    validate_payload([{**quiz_item("choice"), **update}], types=["choice"])
        with self.assertRaises(QuizValidationError):
            validate_payload([quiz_item("short")], types=["blank"])
        with self.assertRaises(QuizValidationError):
            validate_payload([{**quiz_item(), "options": ["unexpected"]}])

    def test_request_options_reject_bool_count_duplicates_and_insufficient_count(self) -> None:
        for types, count, difficulty in [(["blank"], True, "medium"),
                                          (["blank"], 21, "medium"),
                                          (["choice", "short"], 1, "medium"),
                                          (["blank", "blank"], 2, "medium"),
                                          (["unknown"], 1, "medium"),
                                          (["blank"], 1, "unknown")]:
            with self.subTest(types=types, count=count, difficulty=difficulty):
                with self.assertRaises(QuizValidationError):
                    validate_options(types, count, difficulty)


class QuizGenerationTests(unittest.IsolatedAsyncioTestCase):
    async def test_generation_uses_only_bounded_document_material_without_network(self) -> None:
        calls: list[dict] = []
        response_body = {"choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"questions": [quiz_item()]}, ensure_ascii=False)}}]}
        class Response:
            def raise_for_status(self) -> None:
                pass
            def json(self) -> dict:
                return response_body
        class OfflineClient:
            def __init__(self, **kwargs: object):
                pass
            async def __aenter__(self) -> "OfflineClient":
                return self
            async def __aexit__(self, *args: object) -> None:
                pass
            async def post(self, url: str, **kwargs: object) -> Response:
                calls.append({"url": url, **kwargs})
                return Response()
        with patch("services.quiz_service.httpx.AsyncClient", OfflineClient):
            result = await generate_quiz(DOCUMENT_ID, question_types=["blank"], count=1,
                                         api_config={"DEEPSEEK_API_URL": "https://offline.invalid",
                                                     "DEEPSEEK_API_KEY": "dummy-test-value"},
                                         store=MaterialStore([source_chunk("one")]))
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0]["json"]["stream"])
        self.assertEqual(calls[0]["json"]["response_format"], {"type": "json_object"})
        self.assertLessEqual(result["source_chars"], MAX_MATERIAL_CHARS)
        self.assertEqual(result["questions"][0]["chunk_id"], "one")

    async def test_missing_config_does_not_attempt_network(self) -> None:
        with patch("services.quiz_service.httpx.AsyncClient") as client:
            with self.assertRaises(QuizGenerationError):
                await generate_quiz(DOCUMENT_ID, question_types=["blank"], count=1,
                                    api_config={}, store=MaterialStore([source_chunk("one")]))
            client.assert_not_called()

    async def test_malformed_upstream_shapes_are_safe_generation_errors(self) -> None:
        bodies = [None, [], {}, {"choices": []}, {"choices": [None]},
                  {"choices": "not-a-list"}, {"choices": [{"finish_reason": "stop", "message": None}]},
                  {"choices": [{"finish_reason": "stop", "message": {}}]},
                  {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}]
        for body in bodies:
            with self.subTest(body_type=type(body).__name__, body=body):
                calls: list[str] = []
                class Response:
                    def raise_for_status(self) -> None:
                        pass
                    def json(self) -> object:
                        return body
                class OfflineClient:
                    def __init__(self, **kwargs: object):
                        pass
                    async def __aenter__(self) -> "OfflineClient":
                        return self
                    async def __aexit__(self, *args: object) -> None:
                        pass
                    async def post(self, url: str, **kwargs: object) -> Response:
                        calls.append(url)
                        return Response()
                with patch("services.quiz_service.httpx.AsyncClient", OfflineClient):
                    with self.assertRaises(QuizGenerationError):
                        await generate_quiz(DOCUMENT_ID, question_types=["blank"], count=1,
                                            api_config={"DEEPSEEK_API_URL": "https://offline.invalid",
                                                        "DEEPSEEK_API_KEY": "dummy-test-value"},
                                            store=MaterialStore([source_chunk("one")]))
                self.assertEqual(len(calls), 1)

    async def test_unavailable_store_is_safe_and_does_not_attempt_network(self) -> None:
        class UnavailableStore:
            def get_document_chunks(self, document_id: str) -> list:
                raise RuntimeError("private-store-fixture-detail")
        with patch("services.quiz_service.httpx.AsyncClient") as client:
            with self.assertRaises(QuizGenerationError) as caught:
                await generate_quiz(DOCUMENT_ID, question_types=["blank"], count=1,
                                    api_config={"DEEPSEEK_API_URL": "https://offline.invalid",
                                                "DEEPSEEK_API_KEY": "dummy-test-value"},
                                    store=UnavailableStore())
            self.assertNotIn("private-store-fixture-detail", str(caught.exception))
            client.assert_not_called()


class RetrievalRecordTests(unittest.TestCase):
    def test_plain_chunks_log_only_real_metadata_and_do_not_mutate_sources(self) -> None:
        chunks = [
            {"document_id": "math", "filename": "数学.pdf", "vector_score": 0.66,
             "retrieval_sources": ["vector"], "content": "private-source-text",
             "question": "private-question"},
            {"metadata": {"document_id": "math", "filename": "数学.pdf"},
             "vector_score": 0.9, "retrieval_sources": ["vector", "bm25"]},
            {"metadata": {"document_id": "chemistry", "filename": "化学.pdf"},
             "score": 0.4, "retrieval_sources": ["bm25"], "snippet": "private-snippet"},
        ]
        before = copy.deepcopy(chunks)
        result = build_retrieval_record(chunks, 12.5, timestamp=NOW, request_id="test-request")
        self.assertEqual(result["request_id"], "test-request")
        self.assertEqual(result["timestamp"], "2026-10-03T12:00:00+08:00")
        self.assertEqual(result["event"], "retrieval")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["hit_chunk_count"], 3)
        self.assertEqual(result["hit_file_count"], 2)
        self.assertEqual(result["top1_score"], 0.66)
        self.assertEqual(result["retrieval_latency_ms"], 12.5)
        self.assertEqual(result["retrieval_sources"], {"vector": 1, "bm25": 1, "both": 1})
        self.assertCountEqual(result["filenames"], ["数学.pdf", "化学.pdf"])
        self.assertFalse(result["empty"])
        self.assertEqual(chunks, before)
        serialized = json.dumps(result)
        for sensitive in ["private-source-text", "private-question", "private-snippet"]:
            self.assertNotIn(sensitive, serialized)

    def test_existing_diagnostics_metrics_and_thresholds_remain_authoritative(self) -> None:
        class DiagnosedChunks(list):
            def diagnostics(inner_self) -> dict:
                return inner_self.details
        chunks = DiagnosedChunks([{"document_id": "lesson", "filename": "化学.pdf",
                                   "vector_score": None, "score": 0.0}])
        chunks.details = {"status": "degraded", "threshold_used": 0.4,
                          "threshold_path": [0.5, 0.4],
                          "message": "private-question-must-not-be-logged",
                          "metrics": {"hit_chunk_count": 1, "hit_file_count": 1,
                                      "top1_score": None, "threshold_degraded": True,
                                      "retrieval_latency_ms": 321.0, "empty": False,
                                      "retrieval_sources": {"vector": 0, "bm25": 1, "both": 0},
                                      "content": "private-content-must-not-be-logged"}}
        before = copy.deepcopy(chunks.details)
        result = build_retrieval_record(chunks, 789.0, timestamp=NOW, request_id="diagnosed")
        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["threshold_degraded"])
        self.assertIsNone(result["top1_score"])
        self.assertEqual(result["retrieval_latency_ms"], 321.0)
        self.assertEqual(result["threshold_used"], 0.4)
        self.assertEqual(result["threshold_path"], [0.5, 0.4])
        self.assertNotIn("message", result)
        self.assertNotIn("content", result)
        result["threshold_path"].append(0.3)
        result["retrieval_sources"]["bm25"] = 999
        self.assertEqual(chunks.details, before)

    def test_empty_results_and_bm25_only_missing_score_are_not_fabricated(self) -> None:
        empty = build_retrieval_record([], 5.0, timestamp=NOW, request_id="empty")
        self.assertEqual(empty["status"], "no_relevant_context")
        self.assertTrue(empty["empty"])
        self.assertEqual(empty["hit_chunk_count"], 0)
        self.assertEqual(empty["hit_file_count"], 0)
        self.assertEqual(empty["filenames"], [])
        self.assertIsNone(empty["top1_score"])
        bm25 = build_retrieval_record([{"vector_score": None, "score": 0.0}],
                                      2.0, timestamp=NOW, request_id="bm25")
        self.assertIsNone(bm25["top1_score"])
        legacy = build_retrieval_record([{"score": 0.75}], 2.0, timestamp=NOW)
        self.assertEqual(legacy["top1_score"], 0.75)

    def test_broken_diagnostics_and_invalid_latency_use_observed_fallback(self) -> None:
        class BrokenDiagnostics(list):
            def diagnostics(self) -> dict:
                raise RuntimeError("optional-diagnostics-failed")
        result = build_retrieval_record(BrokenDiagnostics([{"document_id": "lesson"}]),
                                        15.0, timestamp=NOW)
        self.assertEqual(result["hit_chunk_count"], 1)
        self.assertEqual(result["retrieval_latency_ms"], 15.0)
        class Diagnostics(list):
            def diagnostics(self) -> dict:
                return {"status": "ok", "metrics": {"retrieval_latency_ms": self.latency}}
        for invalid in [None, -1, math.nan]:
            with self.subTest(invalid_latency=invalid):
                chunks = Diagnostics([{"document_id": "lesson"}])
                chunks.latency = invalid
                self.assertEqual(build_retrieval_record(chunks, 15.0, timestamp=NOW)["retrieval_latency_ms"], 15.0)

    def test_new_records_supply_subject_evidence_to_summary_without_synthetic_fields(self) -> None:
        records = [build_retrieval_record(
            [{"document_id": "math", "filename": "数学.pdf", "vector_score": 0.7}],
            20.0, timestamp=NOW, request_id=f"subject-record-{index}"
        ) for index in range(5)]
        result = summarize_metric_records(records, now=NOW)
        self.assertFalse(result["data_insufficient"])
        self.assertFalse(result["subject_data_insufficient"])
        self.assertEqual(result["filename_record_count"], 5)
        self.assertEqual(result["subjects"], [{"subject": "数学", "count": 5}])


class MetricsSummaryTests(unittest.TestCase):
    def test_answer_events_excluded_and_duplicate_request_ids_count_once(self) -> None:
        initial = metric_row("same", 100)
        latest = metric_row("same", 200, timestamp="2026-10-03T11:00:00+08:00")
        result = summarize_metric_records([initial, {**initial, "event": "answer"}, latest, initial], now=NOW)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["summary"]["p50_latency_ms"], 200)
        self.assertEqual(result["diagnostics"]["answer_events_excluded"], 1)
        self.assertEqual(result["diagnostics"]["duplicate_records"], 2)

    def test_calendar_window_and_utc_timestamps_use_macau_date(self) -> None:
        records = [metric_row("utc", timestamp="2026-10-02T17:00:00Z"),
                   metric_row("start", timestamp="2026-09-04T00:00:00+08:00"),
                   metric_row("old", timestamp="2026-09-03T23:59:59+08:00"),
                   metric_row("future", timestamp="2026-10-03T12:00:01+08:00")]
        result = summarize_metric_records(records, now=NOW)
        self.assertEqual(result["timezone"], "Asia/Macau")
        self.assertEqual(result["start_date"], "2026-09-04")
        self.assertEqual(result["record_count"], 2)
        self.assertEqual(len(result["daily"]), 30)
        self.assertEqual(result["daily"][0]["questions"], 1)
        self.assertEqual(result["daily"][-1]["questions"], 1)

    def test_percentiles_and_histogram_have_correct_finite_sample_counts(self) -> None:
        records = [metric_row(str(i), latency) for i, latency in enumerate([100, 200, 300, 400, 500])]
        records += [metric_row("missing", None), metric_row("nan", math.nan), metric_row("negative", -1)]
        result = summarize_metric_records(records, now=NOW)
        self.assertEqual(result["summary"]["p50_latency_ms"], 300)
        self.assertEqual(result["summary"]["p95_latency_ms"], 480)
        self.assertEqual(result["summary"]["latency_sample_count"], 5)
        self.assertEqual(sum(bucket["count"] for bucket in result["latency_histogram"]), 5)
        self.assertIsNone(result["latency_histogram"][-1]["max_ms"])

    def test_empty_rate_includes_explicit_zero_hits_and_failure(self) -> None:
        result = summarize_metric_records([
            metric_row("unavailable", hit_chunk_count=0, status="retrieval_unavailable"),
            metric_row("no-context", status="no_relevant_context"),
            metric_row("empty-flag", empty=True), metric_row("hit"),
        ], now=NOW)
        self.assertEqual(result["summary"]["empty_rate"], 0.75)

    def test_missing_scores_are_not_turned_into_zero_and_empty_data_stays_missing(self) -> None:
        result = summarize_metric_records([metric_row("none", top1_score=None),
                                           metric_row("score", top1_score=0.8)], now=NOW)
        self.assertEqual(result["summary"]["avg_top1_score"], 0.8)
        self.assertEqual(result["summary"]["top1_score_sample_count"], 1)
        empty = summarize_metric_records([], now=NOW)
        self.assertIsNone(empty["summary"]["empty_rate"])
        self.assertIsNone(empty["summary"]["p95_latency_ms"])
        self.assertTrue(empty["data_insufficient"])

    def test_small_sample_threshold_and_subject_evidence_are_independent(self) -> None:
        records = [metric_row(str(i)) for i in range(5)]
        records[0]["filenames"] = ["数学练习.pdf", "数学练习.pdf", "化学练习.pdf"]
        result = summarize_metric_records(records, now=NOW)
        self.assertFalse(result["data_insufficient"])
        self.assertEqual(result["filename_record_count"], 1)
        self.assertTrue(result["subject_data_insufficient"])
        self.assertEqual({item["subject"]: item["count"] for item in result["subjects"]},
                         {"数学": 1, "化学": 1})
        legacy = summarize_metric_records([metric_row("legacy")], now=NOW)
        self.assertFalse(legacy["subject_distribution_available"])
        self.assertEqual(legacy["subjects"], [])

    def test_invalid_and_legacy_records_are_handled_without_mutating_input(self) -> None:
        legacy = metric_row("legacy")
        legacy.pop("request_id")
        legacy.pop("event")
        before = copy.deepcopy(legacy)
        result = summarize_metric_records([legacy, copy.deepcopy(legacy),
                                           metric_row("bad-date", timestamp="not-a-date"),
                                           {"event": "debug", "timestamp": "2026-10-03T10:00:00"}, 42], now=NOW)
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(legacy, before)
        self.assertEqual(result["diagnostics"]["invalid_timestamps"], 1)
        self.assertEqual(result["diagnostics"]["other_events_excluded"], 1)
        self.assertEqual(result["diagnostics"]["invalid_records"], 1)

    def test_jsonl_reader_ignores_malformed_lines_and_missing_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="hk_learning_tests_") as temporary:
            path = Path(temporary) / "metrics.jsonl"
            path.write_text(json.dumps(metric_row("valid")) + "\n{incomplete\n\n", encoding="utf-8-sig")
            result = build_metrics_summary(path, now=NOW)
            self.assertEqual(result["record_count"], 1)
            self.assertEqual(result["diagnostics"]["malformed_lines"], 1)
            self.assertEqual(result["diagnostics"]["blank_lines"], 1)
            missing = build_metrics_summary(Path(temporary) / "missing.jsonl", now=NOW)
            self.assertEqual(missing["record_count"], 0)
            self.assertEqual(missing["diagnostics"]["log_status"], "missing")


if __name__ == "__main__":
    unittest.main()
